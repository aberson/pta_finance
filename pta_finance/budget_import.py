"""Parse a messy human "budget" worksheet into canonical budget + summary-actual rows.

The module separates **pure parsing** (this file — no I/O, deterministic, unit-testable)
from the **I/O orchestration** (the ``import-budget`` CLI command in :mod:`pta_finance.cli`,
which reads the source grid, snapshots every tab, then upserts the plan's rows).

A treasurer's "budget" tab is free-form: a header somewhere below row 1, section dividers
("INCOME" / "EXPENSE"), subtotal + carry-over + total rows interleaved with real line
items, currency-formatted amounts, and stray junk cells. :func:`plan_budget_import` turns
that into:

* one canonical ``budget`` row per real line item (``schema.BUDGET_COLUMNS`` shape), keyed
  by :func:`pta_finance.ids.budget_id` so a re-import upserts in place (idempotent), and
* optionally one *summary* ``transactions`` row per line item carrying its "actual" spend,
  keyed by :func:`pta_finance.ids.summary_txn_id` (a shape ``etl.normalize`` ignores).

Counts of what was skipped / flagged ride back on :class:`BudgetImportPlan` so the caller
can print an honest summary. Columns come from :mod:`pta_finance.schema`; parsing +
row-shaping from :mod:`pta_finance.models`; id formats from :mod:`pta_finance.ids`. None
is re-implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from pta_finance import ids, models

__all__ = [
    "BudgetImportPlan",
    "plan_budget_import",
]

# Header-cell aliases (each compared stripped + casefolded). A header row is the first row
# carrying BOTH a category-like and a budget-like cell.
CATEGORY_ALIASES = frozenset({"line item", "category", "item"})
BUDGET_ALIASES = frozenset({"proposed", "budget", "budgeted", "budgeted_amount", "budget amount"})
TYPE_ALIASES = frozenset({"type"})
ACTUAL_ALIASES = frozenset({"actual", "spent", "actual amount"})

# Section keywords (casefolded) that flip the running income/expense context.
_SECTIONS = frozenset({"income", "expense"})

# A category cell matching any of these is a summary/rollup row, not a budget line.
_SUMMARY_CONTAINS = ("total", "carry over", "cd account")
_SUMMARY_STARTSWITH = ("subtotal",)


@dataclass(frozen=True)
class BudgetImportPlan:
    """The pure-parse result: rows to upsert plus what was skipped / flagged.

    ``budget_rows`` maps a :func:`pta_finance.ids.budget_id` to a row dict in
    :data:`schema.BUDGET_COLUMNS` shape; ``txn_rows`` maps a
    :func:`pta_finance.ids.summary_txn_id` to a row dict in
    :data:`schema.TRANSACTIONS_COLUMNS` shape (empty unless actuals were requested). Both
    are keyed by id so the caller's :meth:`SheetsClient.upsert_rows` is idempotent.

    Counts:

    * ``skipped_blank`` — rows with no category, or no parseable budgeted amount.
    * ``skipped_summary`` — rows whose category is a subtotal / total / carry-over / CD line.
    * ``needs_review`` — budget lines whose type was inferred from the running section (or is
      still unknown), plus actuals that could not be signed (no section seen yet).
    * ``duplicate_ids`` — later rows whose category slug collides with an already-kept line
      (the first is kept; the duplicate is dropped from output but counted here).
    """

    budget_rows: dict[str, dict[str, str]]
    txn_rows: dict[str, dict[str, str]]
    skipped_blank: int
    skipped_summary: int
    needs_review: int
    duplicate_ids: int


def _norm(cell: str) -> str:
    """Strip + casefold a header/section cell for alias comparison."""
    return cell.strip().casefold()


def _cell(row: list[str], idx: int | None) -> str:
    """Value at ``idx`` in ``row``, or ``""`` when the column is absent / row is short."""
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return row[idx]


def _is_summary_category(category_cf: str) -> bool:
    """True when a (casefolded) category cell is a rollup row, not a real budget line."""
    if any(token in category_cf for token in _SUMMARY_CONTAINS):
        return True
    return any(category_cf.startswith(token) for token in _SUMMARY_STARTSWITH)


def _find_header(values: list[list[str]]) -> tuple[int, dict[str, int | None]]:
    """Locate the header row and build its column-index map.

    Returns ``(header_row_index, columns)`` where ``columns`` carries
    ``category``/``budget`` (required, guaranteed present) and ``type``/``actual``
    (optional, ``None`` when absent). Raises :class:`ValueError` if no row contains both a
    category-alias cell AND a budget-alias cell.
    """
    for row_index, row in enumerate(values):
        normalized = [_norm(cell) for cell in row]
        cells = set(normalized)
        if not (cells & CATEGORY_ALIASES and cells & BUDGET_ALIASES):
            continue
        # First matching cell wins for each role (a header rarely repeats a column).
        columns: dict[str, int | None] = {
            "category": None,
            "budget": None,
            "type": None,
            "actual": None,
        }
        for col_index, name in enumerate(normalized):
            if columns["category"] is None and name in CATEGORY_ALIASES:
                columns["category"] = col_index
            if columns["budget"] is None and name in BUDGET_ALIASES:
                columns["budget"] = col_index
            if columns["type"] is None and name in TYPE_ALIASES:
                columns["type"] = col_index
            if columns["actual"] is None and name in ACTUAL_ALIASES:
                columns["actual"] = col_index
        return row_index, columns
    raise ValueError(
        "no header row found: need a row containing a category column "
        f"(one of {sorted(CATEGORY_ALIASES)}) and a budget column "
        f"(one of {sorted(BUDGET_ALIASES)})"
    )


def plan_budget_import(
    values: list[list[str]],
    *,
    fy: int,
    with_actuals: bool,
    actual_date: date,
) -> BudgetImportPlan:
    """Parse a raw budget grid into canonical budget (+ optional summary-actual) rows.

    Parameters
    ----------
    values:
        The raw worksheet grid (list of rows of cell strings), as
        :meth:`SheetsClient.read_values` returns it. The header is detected (it is rarely
        row 1); rows above it are ignored.
    fy:
        The integer fiscal-year LABEL (e.g. ``2026``). Used to mint
        :func:`pta_finance.ids.budget_id` / :func:`pta_finance.ids.summary_txn_id` and to
        stamp each row's ``fiscal_year``.
    with_actuals:
        When ``True`` (and the header has an actual/spent column), emit one summary
        ``transactions`` row per line item whose actual is a non-zero amount.
    actual_date:
        The date stamped on every summary transaction (the caller passes the fiscal year's
        last day). Kept as a parameter — never derived here — so the parse is deterministic.

    Returns
    -------
    BudgetImportPlan
        The rows to upsert plus per-bucket skip/flag counts. See :class:`BudgetImportPlan`.

    Raises
    ------
    ValueError
        When no header row can be found (see :func:`_find_header`).
    """
    header_row_index, columns = _find_header(values)
    category_col = columns["category"]
    budget_col = columns["budget"]
    type_col = columns["type"]
    actual_col = columns["actual"]

    budget_rows: dict[str, dict[str, str]] = {}
    txn_rows: dict[str, dict[str, str]] = {}
    skipped_blank = 0
    skipped_summary = 0
    needs_review = 0
    duplicate_ids = 0

    # The running income/expense section, flipped by a row whose type cell names one.
    current_type: str | None = None

    for row in values[header_row_index + 1 :]:
        # A row's type cell can BOTH set the running section and type this very line.
        type_cf = _norm(_cell(row, type_col)) if type_col is not None else ""
        if type_cf in _SECTIONS:
            current_type = type_cf

        category = _cell(row, category_col).strip()
        if category == "":
            skipped_blank += 1
            continue

        category_cf = category.casefold()
        if _is_summary_category(category_cf):
            skipped_summary += 1
            continue

        try:
            budgeted = models.parse_optional_amount(_cell(row, budget_col))
        except ValueError:
            budgeted = None
        if budgeted is None:
            # No budget amount means this is not a budget line (a stray label, a junk cell).
            skipped_blank += 1
            continue

        # --- Duplicate id check FIRST: a dropped duplicate must not perturb any stat
        # (e.g. an inferred-type duplicate must not inflate needs_review). Keep the FIRST
        # row; drop this one from output, but record the collision on the kept row's notes
        # so the operator sees it. The duplicate is counted, never silently lost. ---
        budget_identifier = ids.budget_id(fy, category)
        if budget_identifier in budget_rows:
            duplicate_ids += 1
            kept = budget_rows[budget_identifier]
            if "(DUPLICATE category seen — review)" not in kept["notes"]:
                suffix = " (DUPLICATE category seen — review)"
                kept["notes"] = (kept["notes"] + suffix).strip()
            continue

        explicit_type = type_cf if type_cf in _SECTIONS else None
        row_type = explicit_type or current_type
        type_inferred = explicit_type is None

        # --- Build the canonical budget row. ---
        if type_inferred:
            if row_type is None:
                note = "(type unknown — review)"
                needs_review += 1
            else:
                note = f"{row_type} (type inferred from section — review)"
                needs_review += 1
        else:
            note = row_type or ""

        budget_rows[budget_identifier] = models.BudgetLine(
            id=budget_identifier,
            fiscal_year=str(fy),
            category=category,
            grade=None,
            budgeted_amount=budgeted,
            notes=note,
        ).to_row()

        # --- Optionally build the summary "actual" transaction. ---
        if with_actuals and actual_col is not None:
            try:
                actual = models.parse_optional_amount(_cell(row, actual_col))
            except ValueError:
                actual = None
            if actual is None or actual == 0:
                # A zero / absent actual is not a transaction.
                continue
            if row_type is None:
                # Can't sign an actual with no section context — flag and skip the txn.
                needs_review += 1
                continue

            summary_identifier = ids.summary_txn_id(fy, category)
            if summary_identifier in txn_rows:
                # Mirrors the budget dup case: keep first, duplicate already counted above.
                continue
            txn_rows[summary_identifier] = models.Transaction(
                id=summary_identifier,
                date=actual_date,
                fiscal_year=str(fy),
                type=row_type,
                amount=actual,
                category=category,
                grade=None,
                # payee = category keeps the (date|amount|payee) dedup key unique per line.
                payee=category,
                memo=f"summary actual imported from budget worksheet (aggregate, FY{fy})",
                budget_id=budget_identifier,
                receipt_id=None,
                source="import",
                entered_by=None,
                created_at="",
                needs_review=type_inferred,
            ).to_row()

    return BudgetImportPlan(
        budget_rows=budget_rows,
        txn_rows=txn_rows,
        skipped_blank=skipped_blank,
        skipped_summary=skipped_summary,
        needs_review=needs_review,
        duplicate_ids=duplicate_ids,
    )
