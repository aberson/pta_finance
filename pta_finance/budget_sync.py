"""Reconcile the editable "FY<fy> Budget" tab back into the "Budget Timeseries" DB.

The operator edits a readable, per-fiscal-year budget tab (proposed amounts, notes, and new
lines); this module reconciles those edits into the operator-maintained "Budget Timeseries"
long dataset — the single source ``report`` / ``analyze`` read. Split like ``budget_import``:

* PURE, no-I/O planning — :func:`parse_budget_tab` (the tab's raw grid -> a :class:`ParsedTab`)
  and :func:`plan_budget_sync` (lines + the live timeseries grid -> a :class:`SyncPlan` of exact
  A1 cell updates + full-width row appends). Fully unit-testable without Google.
* The CLI (:func:`pta_finance.cli._cmd_sync_budget`) does the reads, prints the diff + warnings,
  snapshots the timeseries BEFORE any write, and applies the plan via ``SheetsClient``.

Match key = ``(type, raw_category)`` within the target fiscal year's ``measure == "proposed"``
rows — the same identity the toolkit already uses for a budget line
(:func:`pta_finance.ids.budget_id` is ``f(fy, raw_category)``). The key is normalized IDENTICALLY
on both sides (``type`` casefolded, ``raw_category`` whitespace-collapsed + casefolded) so a
case/spacing-only cell edit is NOT mistaken for a rename. ONLY ``measure == "proposed"`` rows of
the requested ``fy`` are ever touched; actuals, other fiscal years, and every enrichment column
(``strategic_group``, ``strategic_goal``, ``standard_category``, ...) are left untouched. A
changed amount/note becomes a single targeted cell write; a tab line with no DB match becomes an
appended row (enrichment blank, flagged for tagging); a ``proposed`` FY row absent from the tab is
FLAGGED, never deleted. A genuine (word-level) rename shows as a remove + an add and is surfaced
as a ``suspected_rename`` so the operator does not delete the old row (which carries the tags).

Tab-parsing is deliberately ROBUST to operator-authored free text (a public reusable toolkit):
sections match the exact all-caps ``INCOME`` / ``EXPENSE`` banners only (so a category_group
literally named "Income" cannot flip the type); rollup rows match the anchored shapes the tab
emits ("Subtotal — …", "TOTAL INCOME/EXPENSE", "NET (…)") — NOT a bare prefix, so a real line
like "Net Store Sales" or "Total Rewards" is kept; a sub-header is a blank-amount + blank-notes
row; and a row that looks like data but has a non-numeric/blank amount, or sits before any
section, is reported (``skipped`` / ``orphaned``) rather than silently dropped or mis-grouped.

The Budget Timeseries tab is NOT in ``schema.TABS`` (an operator-maintained 14-column tab, wider
than the 9 required :data:`report_source.TIMESERIES_COLUMNS`), so this module reads its LIVE
header dynamically and addresses cells by the column NAMES it finds — never a hard-coded order.
Column-name constants come from :mod:`pta_finance.report_source` (single source of truth).
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from gspread.utils import rowcol_to_a1

from pta_finance import report_source

__all__ = [
    "BudgetLine",
    "ParsedTab",
    "SyncPlan",
    "budget_tab_name",
    "parse_budget_tab",
    "plan_budget_sync",
]

# Columns the reconcile requires on the live "Budget Timeseries" tab (looked up by name).
_REQUIRED_COLS: tuple[str, ...] = (
    report_source.FISCAL_YEAR,
    report_source.TYPE,
    report_source.MEASURE,
    report_source.AMOUNT,
    report_source.RAW_CATEGORY,
    report_source.CATEGORY_GROUP,
    report_source.NOTES,
)

_INCOME = "income"
_EXPENSE = "expense"
# Section banners are matched CASE-SENSITIVELY against these exact all-caps strings, so a
# category_group titled "Income"/"Expense" (mixed case) can never be mistaken for a section
# header (which would silently flip the running type).
_SECTION_TITLES: dict[str, str] = {"INCOME": _INCOME, "EXPENSE": _EXPENSE}
# Rename-suspicion threshold: two same-type item names this similar (SequenceMatcher ratio) that
# appear as a remove + an add are flagged as a probable rename rather than a delete + a new line.
_RENAME_RATIO = 0.7


def budget_tab_name(fy: int) -> str:
    """The editable budget tab name for a fiscal year, e.g. ``"FY2027 Budget"``."""
    return f"FY{fy} Budget"


def _is_rollup(upper: str) -> bool:
    """True when a column-A label (already uppercased) is a subtotal/total/net ROLLUP row.

    Matches ONLY the anchored shapes the tab emits — ``"Subtotal — <group>"``,
    ``"TOTAL"`` / ``"TOTAL INCOME"`` / ``"TOTAL EXPENSE"``, and ``"NET (…)"`` — so a legitimate
    line item that merely BEGINS with one of those words (``"Net Store Sales"``,
    ``"Total Rewards Program"``, ``"Subtotals Software"``) is NOT swallowed as a rollup.
    """
    if upper in {"TOTAL", "TOTAL INCOME", "TOTAL EXPENSE"}:
        return True
    if upper.startswith(("SUBTOTAL —", "SUBTOTAL -", "SUBTOTAL:")):
        return True
    return upper.startswith(("NET (", "NET INCOME", "NET EXPENSE"))


def _num(s: str) -> float | None:
    """Parse a money-ish cell to float, or ``None`` if not numeric.

    Strips ``$`` + thousands commas + surrounding whitespace; treats ``(1,234)`` as ``-1234``.
    Returns ``None`` (NOT 0) for blank/non-numeric text so :func:`parse_budget_tab` can tell a
    numeric data amount from a blank sub-header cell or a placeholder ("TBD", "5%", "-").
    """
    t = (s or "").replace(",", "").replace("$", "").strip()
    if t == "":
        return None
    negative = t.startswith("(") and t.endswith(")")
    if negative:
        t = t[1:-1]
    try:
        value = float(t)
    except ValueError:
        return None
    return -value if negative else value


def _fmt_amount(x: float) -> str:
    """Normalize a float to the timeseries' plain-number string (no ``$`` / commas).

    Integral values render without a decimal (``70000``); fractional values keep up to two
    decimals with trailing zeros trimmed (``3287.7``, ``20023.98``) — matching the mixed
    plain-number style already in the "Budget Timeseries" ``amount`` column.
    """
    rounded = round(x, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _norm_name(s: str) -> str:
    """Normalized form of a raw_category/item for MATCHING: whitespace-collapsed + casefolded.

    Used ONLY to compare a tab line to a DB row (and to detect duplicates/renames); the original
    text is always what gets written to a cell, so display casing/spacing is preserved verbatim.
    """
    return " ".join(str(s).split()).casefold()


def _match_key(type_: str, raw: str) -> tuple[str, str]:
    """The (normalized type, normalized raw_category) key used identically on both sides."""
    return (type_.strip().casefold(), _norm_name(raw))


@dataclass(frozen=True)
class BudgetLine:
    """One parsed data row from the editable budget tab."""

    type: str  # "income" | "expense" (from the enclosing section header)
    category_group: str  # inherited from the sub-section header ("" if none seen yet)
    item: str  # raw_category — the match key within a type
    amount: float
    notes: str


@dataclass
class ParsedTab:
    """The result of parsing the editable budget tab (PURE).

    ``lines`` are the reconcilable data rows. ``skipped`` and ``orphaned`` are rows that LOOK
    like data but could not be safely reconciled — surfaced by the CLI so a dropped line is never
    silent: ``skipped`` = a row with a non-numeric/placeholder amount (e.g. "TBD", "5%") or a
    blank amount but a non-empty note; ``orphaned`` = a numeric row seen before any INCOME/EXPENSE
    section. ``section_count`` is how many section banners were found (0 => a broken layout).
    """

    lines: list[BudgetLine] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (item, raw_amount)
    orphaned: list[tuple[str, str]] = field(default_factory=list)  # (item, raw_amount)
    section_count: int = 0


def parse_budget_tab(grid: Sequence[Sequence[str]]) -> ParsedTab:
    """Parse the editable budget tab's raw grid into a :class:`ParsedTab` (PURE, no I/O).

    Scans top-to-bottom tracking the current ``type`` (set by an exact all-caps ``INCOME`` /
    ``EXPENSE`` banner in column A) and the current ``category_group`` (a BLANK-amount +
    BLANK-notes row inside a section). Classification per row (column layout A=item, B=amount,
    C=notes):

    * exact ``INCOME`` / ``EXPENSE`` -> section banner (sets type; counts toward ``section_count``).
    * anchored rollup shape (:func:`_is_rollup`) -> skipped as a subtotal/total/net row.
    * numeric amount, inside a section -> a data :class:`BudgetLine`.
    * numeric amount, before any section -> ``orphaned`` (reported, never silently dropped).
    * blank amount + blank notes, inside a section -> category_group sub-header.
    * non-numeric/placeholder amount (or blank amount with a note), inside a section -> ``skipped``
      (reported; does NOT change the current group, so following lines stay correctly grouped).
    * before the first section with no amount (title, instructions, column header) -> ignored.
    """
    result = ParsedTab()
    current_type: str | None = None
    current_group = ""
    for raw in grid:
        col_a = str(raw[0]).strip() if len(raw) > 0 else ""
        col_b = str(raw[1]).strip() if len(raw) > 1 else ""
        col_c = str(raw[2]).strip() if len(raw) > 2 else ""
        if col_a == "":
            continue
        if col_a in _SECTION_TITLES:  # case-SENSITIVE exact banner
            current_type = _SECTION_TITLES[col_a]
            current_group = ""
            result.section_count += 1
            continue
        if _is_rollup(col_a.upper()):
            continue
        amount = _num(col_b)
        if amount is not None:
            if current_type is None:
                result.orphaned.append((col_a, col_b))  # numeric row with no enclosing section
            else:
                result.lines.append(BudgetLine(current_type, current_group, col_a, amount, col_c))
            continue
        # amount is None: either a sub-header (blank amount + blank notes) or an unparseable row.
        if col_b == "" and col_c == "":
            if current_type is not None:
                current_group = col_a  # category_group sub-header
            # else: preamble (title / instructions / column header) — ignore
        elif current_type is not None:
            # a data-shaped row with a non-numeric/placeholder amount, or a blank amount + a note:
            # report it (do NOT silently drop it or overwrite current_group).
            result.skipped.append((col_a, col_b))
    return result


@dataclass
class SyncPlan:
    """The exact writes needed to reconcile the tab into the "Budget Timeseries" tab.

    ``cell_updates`` maps an A1 cell (on the timeseries tab) to its new value — only changed
    ``amount`` / ``notes`` cells of existing FY-proposed rows. ``append_rows`` are full-width
    rows (live-header order) for tab lines with no DB match. The remaining lists drive the
    human-readable diff + warnings; ``removed`` are DB proposed rows absent from the tab
    (FLAGGED, never written), ``duplicates`` are tab lines whose (type, raw_category) repeats
    (first kept, rest dropped — no ambiguous write), and ``suspected_renames`` pair a removed
    with a similar added line so enrichment loss is surfaced. Nothing here mutates a row outside
    ``(fy, proposed)`` or any enrichment column.
    """

    cell_updates: dict[str, str] = field(default_factory=dict)
    append_rows: list[list[str]] = field(default_factory=list)
    changed: list[tuple[str, str, float, float]] = field(default_factory=list)  # type,item,old,new
    notes_changed: list[tuple[str, str]] = field(default_factory=list)  # type,item
    added: list[tuple[str, str, str, float]] = field(default_factory=list)  # type,group,item,amt
    removed: list[tuple[str, str, float]] = field(default_factory=list)  # type,item,db_amt
    duplicates: list[tuple[str, str]] = field(default_factory=list)  # type,item (repeated on tab)
    suspected_renames: list[tuple[str, str, str]] = field(default_factory=list)  # type,old,new
    unchanged: int = 0

    def has_writes(self) -> bool:
        """True when applying this plan would issue at least one write."""
        return bool(self.cell_updates or self.append_rows)


def plan_budget_sync(
    timeseries_grid: Sequence[Sequence[str]],
    lines: Iterable[BudgetLine],
    *,
    fy: int,
) -> SyncPlan:
    """Diff parsed budget ``lines`` against the live timeseries; return the exact writes (PURE).

    Indexes the timeseries' ``(fy, proposed)`` rows by the normalized ``(type, raw_category)``
    key (first occurrence wins). For each budget line (a repeat of an already-seen key is recorded
    in ``duplicates`` and skipped, so two tab rows can never fight over one cell):

    * MATCHED + amount differs -> a targeted ``amount``-cell update (only that cell).
    * MATCHED + notes differ -> a targeted ``notes``-cell update.
    * MATCHED + identical -> counted as unchanged.
    * UNMATCHED -> an appended full-width row (``fiscal_year`` / ``measure=proposed`` / ``type`` /
      ``amount`` / ``raw_category`` / ``category_group`` / ``notes`` filled; enrichment blank).

    A ``(fy, proposed)`` row not present among the tab lines is recorded in ``removed`` (flagged,
    never written). Any removed line whose name is very similar (same type) to an added line is
    also recorded in ``suspected_renames``. Raises :class:`ValueError` if the grid is empty or
    lacks a required column.
    """
    if not timeseries_grid:
        raise ValueError("Budget Timeseries is empty — nothing to reconcile against")
    header = [str(cell).strip() for cell in timeseries_grid[0]]
    col = {name: index for index, name in enumerate(header)}
    missing = [name for name in _REQUIRED_COLS if name not in col]
    if missing:
        raise ValueError(f"Budget Timeseries missing required column(s): {', '.join(missing)}")

    ci_fy = col[report_source.FISCAL_YEAR]
    ci_type = col[report_source.TYPE]
    ci_measure = col[report_source.MEASURE]
    ci_amount = col[report_source.AMOUNT]
    ci_raw = col[report_source.RAW_CATEGORY]
    ci_group = col[report_source.CATEGORY_GROUP]
    ci_notes = col[report_source.NOTES]
    fy_str = str(fy)
    ncols = len(header)

    def cell(row: Sequence[str], index: int) -> str:
        return str(row[index]).strip() if 0 <= index < len(row) else ""

    # Index existing FY-proposed rows by the NORMALIZED key ->
    # (sheet_row, amount_str, notes_str, type_display, raw_display).
    existing: dict[tuple[str, str], tuple[int, str, str, str, str]] = {}
    for offset, row in enumerate(timeseries_grid[1:]):
        sheet_row = offset + 2  # 1-based; row 1 is the header
        if cell(row, ci_fy) != fy_str:
            continue
        if cell(row, ci_measure).casefold() != report_source.MEASURE_PROPOSED:
            continue
        key = _match_key(cell(row, ci_type), cell(row, ci_raw))
        existing.setdefault(
            key,
            (
                sheet_row,
                cell(row, ci_amount),
                cell(row, ci_notes),
                cell(row, ci_type),
                cell(row, ci_raw),
            ),
        )

    plan = SyncPlan()
    seen: set[tuple[str, str]] = set()

    for line in lines:
        key = _match_key(line.type, line.item)
        if key in seen:
            plan.duplicates.append((line.type, line.item))
            continue
        seen.add(key)
        match = existing.get(key)
        if match is not None:
            sheet_row, amount_str, notes_str, _type_disp, _raw_disp = match
            db_amount = _num(amount_str)
            amount_changed = db_amount is None or round(db_amount, 2) != round(line.amount, 2)
            notes_changed = line.notes != notes_str
            if amount_changed:
                plan.cell_updates[rowcol_to_a1(sheet_row, ci_amount + 1)] = _fmt_amount(line.amount)
                plan.changed.append(
                    (line.type, line.item, db_amount if db_amount is not None else 0.0, line.amount)
                )
            if notes_changed:
                plan.cell_updates[rowcol_to_a1(sheet_row, ci_notes + 1)] = line.notes
                plan.notes_changed.append((line.type, line.item))
            if not amount_changed and not notes_changed:
                plan.unchanged += 1
        else:
            new_row = [""] * ncols
            new_row[ci_fy] = fy_str
            new_row[ci_measure] = report_source.MEASURE_PROPOSED
            new_row[ci_type] = line.type
            new_row[ci_amount] = _fmt_amount(line.amount)
            new_row[ci_raw] = line.item
            new_row[ci_group] = line.category_group
            new_row[ci_notes] = line.notes
            plan.append_rows.append(new_row)
            plan.added.append((line.type, line.category_group, line.item, line.amount))

    for key, (_sheet_row, amount_str, _notes_str, type_disp, raw_disp) in existing.items():
        if key not in seen:
            db_amount = _num(amount_str)
            plan.removed.append(
                (type_disp or key[0], raw_disp, db_amount if db_amount is not None else 0.0)
            )

    # Suspected renames: a removed line whose name closely matches an added line (same type).
    for rtype, ritem, _ramt in plan.removed:
        best: tuple[float, str] | None = None
        for atype, _agroup, aitem, _aamt in plan.added:
            if atype.casefold() != rtype.casefold():
                continue
            ratio = difflib.SequenceMatcher(None, _norm_name(ritem), _norm_name(aitem)).ratio()
            if ratio >= _RENAME_RATIO and (best is None or ratio > best[0]):
                best = (ratio, aitem)
        if best is not None:
            plan.suspected_renames.append((rtype, ritem, best[1]))

    return plan
