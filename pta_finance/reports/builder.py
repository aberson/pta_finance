"""Compute the monthly report data model from the analytics layer — two variants.

This module owns the **field-level contract** between the two report variants and the
**runtime PII guard** that enforces it. Both variants are computed from the same analytics
primitives (:mod:`pta_finance.analytics`); the variants differ ONLY in which fields they
carry, never in how a figure is computed.

Internal vs external field lists (pinned at build Step 6)
---------------------------------------------------------
``InternalReport`` (full detail — treasurer / board eyes only):

* ``organization`` / ``school_name`` — identity, from config.
* ``month`` (``YYYY-MM``) + ``fiscal_year`` (int label) — the reporting period.
* ``totals`` — income / expense / net for the month.
* ``by_category`` — per-category income/expense/net **with budget variance** for the FY.
* ``by_grade`` — per-grade allocation.
* ``fundraising`` — fundraising income raised vs the fundraiser budget target (progress).
* ``budget_headline`` — total budgeted / total spent / remaining for the FY.
* ``transactions`` — the month's ledger lines INCLUDING ``payee``, ``memo``,
  ``receipt_id`` / ``receipt_url`` (Drive link), ``grade``, and ``entered_by``.

``ExternalReport`` (public-safe — PUBLIC, no identifying detail):

* ``organization`` / ``school_name`` — the org's own name is public.
* ``month`` + ``fiscal_year`` — the reporting period.
* ``totals`` — income / expense / net for the month.
* ``by_grade`` — per-grade allocation (aggregate dollars only).
* ``fundraising`` — fundraising raised vs target (aggregate progress).
* ``budget_headline`` — total budgeted / spent / remaining (aggregate headline numbers).

The external variant deliberately omits, at the type level, EVERY one of:
``payee``, ``memo``, ``receipt_id``, ``receipt_url`` / ``drive_url``, ``entered_by`` /
``added_by``, and any individual transaction row. ``by_category`` is omitted too (a
single-line category can de-anonymize a vendor); only the by-grade and headline
aggregates are public.

Runtime PII guard (the security invariant)
------------------------------------------
A type-level omission is not enough — a future refactor could attach a PII field to a
nested object. :func:`_assert_external_safe` recursively walks the assembled
``ExternalReport`` (dataclass fields, mappings, and sequences) and raises
:class:`ExternalReportPIIError` if ANY field name on the denylist
(:data:`EXTERNAL_PII_DENYLIST`) — or a person-``name`` field — appears with a value.
:func:`build_external_report` calls it before returning, so a contaminated external model
fails loudly instead of leaking. This is the guard the workspace ``security`` rule
("pair unsafe configs with startup safety checks") requires: documentation is not a
control.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pta_finance import analytics, ids
from pta_finance.analytics import aggregate

if TYPE_CHECKING:
    from pta_finance.config import Config

__all__ = [
    "EXTERNAL_PII_DENYLIST",
    "ExternalReportPIIError",
    "ReportTotals",
    "CategoryLine",
    "GradeLine",
    "FundraisingProgress",
    "BudgetHeadline",
    "ReportTransaction",
    "InternalReport",
    "ExternalReport",
    "build_internal_report",
    "build_external_report",
    "build_reports",
]


# --- PII denylist + guard --------------------------------------------------
#
# Field names that must NEVER appear anywhere in an external report data model. A person's
# bare ``name`` is handled separately (the org's own name is fine; a person's is not), so it
# is not in this set — see ``_PERSON_NAME_FIELDS``.
EXTERNAL_PII_DENYLIST: frozenset[str] = frozenset(
    {
        "payee",
        "memo",
        "receipt_id",
        "receipt_url",
        "drive_url",
        "entered_by",
        "added_by",
    }
)

# Field names that name a *person* (vs the organization's own name, which is public).
# Walked only on nested objects; the top-level ExternalReport intentionally has no such field.
_PERSON_NAME_FIELDS: frozenset[str] = frozenset({"person_name", "member_name", "payee_name"})


class ExternalReportPIIError(Exception):
    """Raised when an external report data model carries a PII-shaped field.

    ``ExternalReportPIIError.field`` names the offending field (the denylisted attribute /
    key that was found populated) so the failure points straight at the leak. This is a
    SECURITY INVARIANT: the external variant is public, so any payee / receipt / memo /
    member-PII field reaching it is a hard error, never a warning.
    """

    def __init__(self, field: str, message: str | None = None) -> None:
        self.field = field
        super().__init__(
            message or f"external report data model contains a forbidden PII field: {field!r}"
        )


def _is_present(value: Any) -> bool:
    """Whether a value counts as 'populated' for PII purposes.

    ``None`` and the empty string are absent; everything else (including ``0`` / ``False``)
    is present. A denylisted field that exists but is ``None``/empty is not a leak.
    """
    if value is None:
        return False
    if isinstance(value, str) and value == "":
        return False
    return True


def _assert_external_safe(model: Any) -> None:
    """Recursively assert ``model`` carries no populated PII field; else raise.

    Walks dataclass instances (by field name), mappings (by key), and sequences (element by
    element), checking every encountered field/key name against
    :data:`EXTERNAL_PII_DENYLIST` and :data:`_PERSON_NAME_FIELDS`. The FIRST populated
    forbidden field raises :class:`ExternalReportPIIError` naming it. Scalars and strings are
    leaves. ``Decimal`` and ``bytes`` are treated as opaque leaves (never walked as
    sequences).
    """
    _walk_external(model)


def _walk_external(node: Any) -> None:
    if node is None or isinstance(node, (str, bytes, Decimal, int, float, bool)):
        return
    if is_dataclass(node) and not isinstance(node, type):
        for f in fields(node):
            value = getattr(node, f.name)
            _check_name(f.name, value)
            _walk_external(value)
        return
    if isinstance(node, Mapping):
        for key, value in node.items():
            _check_name(str(key), value)
            _walk_external(value)
        return
    if isinstance(node, (list, tuple, set, frozenset)):
        for item in node:
            _walk_external(item)
        return
    # Any other object type: no field/key names to inspect; treat as an opaque leaf.
    return


def _check_name(name: str, value: Any) -> None:
    """Raise if ``name`` is a denylisted/person field AND ``value`` is populated."""
    if name in EXTERNAL_PII_DENYLIST and _is_present(value):
        raise ExternalReportPIIError(name)
    if name in _PERSON_NAME_FIELDS and _is_present(value):
        raise ExternalReportPIIError(name)


# --- Shared (variant-agnostic) sub-models ----------------------------------


@dataclass(frozen=True)
class ReportTotals:
    """Income / expense (positive magnitude) / net for the reporting period."""

    income: Decimal
    expense: Decimal
    net: Decimal


@dataclass(frozen=True)
class GradeLine:
    """Per-grade allocation: income / expense / net for one grade bucket (aggregate only)."""

    grade: str
    income: Decimal
    expense: Decimal
    net: Decimal


@dataclass(frozen=True)
class FundraisingProgress:
    """Fundraising income raised vs the fundraiser budget target for the FY.

    ``raised`` is fundraising income realized so far this FY; ``target`` is the budgeted
    fundraiser income (``0`` when none is budgeted); ``pct`` is ``raised / target * 100``
    rounded to 0.1%, or ``None`` when there is no target. All aggregate — no PII.
    """

    raised: Decimal
    target: Decimal
    pct: Decimal | None


@dataclass(frozen=True)
class BudgetHeadline:
    """FY budget headline: total budgeted / total spent / remaining (aggregate only)."""

    fiscal_year: int
    total_budgeted: Decimal
    total_spent: Decimal
    remaining: Decimal


# --- Internal-only sub-models ----------------------------------------------


@dataclass(frozen=True)
class CategoryLine:
    """Per-category figures WITH budget variance — internal only.

    Carries no PII itself, but a single-line category can de-anonymize a vendor, so the
    per-category breakdown is internal-only (the external variant exposes by-grade +
    headline aggregates instead).
    """

    category: str
    income: Decimal
    expense: Decimal
    net: Decimal
    budgeted: Decimal
    variance: Decimal


@dataclass(frozen=True)
class ReportTransaction:
    """One ledger line as the INTERNAL report shows it — full identifying detail.

    Carries ``payee``, ``memo``, ``receipt_id`` / ``receipt_url`` (Drive link), and
    ``entered_by``. These field NAMES are on :data:`EXTERNAL_PII_DENYLIST`, so this type can
    only ever appear in :class:`InternalReport`; placing it on an external model would trip
    :func:`_assert_external_safe`.
    """

    id: str
    date: str
    type: str
    amount: Decimal
    category: str
    grade: str
    payee: str
    memo: str
    receipt_id: str
    receipt_url: str
    entered_by: str


# --- The two report variants -----------------------------------------------


@dataclass(frozen=True)
class InternalReport:
    """Full-detail monthly report (treasurer / board only). See module docstring."""

    organization: str
    school_name: str
    month: str
    fiscal_year: int
    totals: ReportTotals
    by_category: tuple[CategoryLine, ...]
    by_grade: tuple[GradeLine, ...]
    fundraising: FundraisingProgress
    budget_headline: BudgetHeadline
    transactions: tuple[ReportTransaction, ...]


@dataclass(frozen=True)
class ExternalReport:
    """Public-safe monthly report. Carries NO payee / receipt / memo / member PII.

    Enforced at runtime by :func:`_assert_external_safe` (called from
    :func:`build_external_report`). See module docstring for the exact field list.
    """

    organization: str
    school_name: str
    month: str
    fiscal_year: int
    totals: ReportTotals
    by_grade: tuple[GradeLine, ...]
    fundraising: FundraisingProgress
    budget_headline: BudgetHeadline


# --- Builders --------------------------------------------------------------

# A category counts as "fundraising" income when its slug matches one of these. Kept generic
# (no org-specific category names) so the toolkit stays reusable.
_FUNDRAISER_SLUGS: frozenset[str] = frozenset({"fundraiser", "fundraising", "fundraisers"})


def _month_bounds(month: str) -> tuple[str, str]:
    """Validate ``YYYY-MM`` and return its ``(YYYY-MM-01, last-day)`` ISO date strings."""
    try:
        year_s, mon_s = month.split("-")
        year, mon = int(year_s), int(mon_s)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"month must be 'YYYY-MM', got {month!r}") from exc
    if not 1 <= mon <= 12:
        raise ValueError(f"month must be 'YYYY-MM' with month in 01..12, got {month!r}")
    from calendar import monthrange
    from datetime import date

    last = monthrange(year, mon)[1]
    return date(year, mon, 1).isoformat(), date(year, mon, last).isoformat()


def _month_rows(
    txn_rows: Iterable[Mapping[str, str]], first_iso: str, last_iso: str
) -> list[dict[str, str]]:
    """Filter ``transactions`` row dicts to those whose ``date`` falls within the month.

    Compares ISO date strings (lexicographic order == chronological order for ``YYYY-MM-DD``).
    Rows with a blank/unparseable date are excluded from the month (they cannot be placed).
    """
    out: list[dict[str, str]] = []
    for row in txn_rows:
        d = str(row.get("date", "")).strip()
        if first_iso <= d <= last_iso:
            out.append(dict(row))
    return out


def _grade_lines(frame: Any) -> tuple[GradeLine, ...]:
    return tuple(
        GradeLine(grade=g.grade, income=g.income, expense=g.expense, net=g.net)
        for g in analytics.by_grade(frame)
    )


def _totals(frame: Any) -> ReportTotals:
    t = analytics.totals(frame)
    return ReportTotals(income=t.income, expense=t.expense, net=t.net)


def _fundraising_progress(
    fy_frame: Any, budget_rows: Sequence[Mapping[str, str]], fiscal_year: int
) -> FundraisingProgress:
    """Fundraising income raised this FY vs the budgeted fundraiser income target."""
    raised = Decimal("0.00")
    for cat in analytics.by_category(fy_frame):
        if ids.slugify(cat.category) in _FUNDRAISER_SLUGS:
            raised += cat.income

    target = Decimal("0.00")
    for brow in budget_rows:
        if aggregate._to_int(brow.get("fiscal_year")) != fiscal_year:
            continue
        if ids.slugify(str(brow.get("category", ""))) in _FUNDRAISER_SLUGS:
            amount = analytics_parse_amount(brow.get("budgeted_amount"))
            if amount is not None:
                target += amount

    pct: Decimal | None = None
    if target > 0:
        pct = (raised / target * Decimal(100)).quantize(Decimal("0.1"))
    return FundraisingProgress(raised=raised, target=target, pct=pct)


def analytics_parse_amount(value: Any) -> Decimal | None:
    """Parse an optional budget amount via the shared models parser (never re-derived)."""
    from pta_finance import models

    return models.parse_optional_amount(value)


def _budget_headline(
    variances: Sequence[aggregate.BudgetVariance], fiscal_year: int
) -> BudgetHeadline:
    """Roll per-category budget variance into FY headline totals."""
    total_budgeted = sum((bv.budgeted for bv in variances), Decimal("0.00"))
    total_spent = sum((bv.actual for bv in variances), Decimal("0.00"))
    return BudgetHeadline(
        fiscal_year=fiscal_year,
        total_budgeted=total_budgeted,
        total_spent=total_spent,
        remaining=total_budgeted - total_spent,
    )


def _category_lines(
    fy_frame: Any, budget_rows: Sequence[Mapping[str, str]], fiscal_year: int
) -> tuple[tuple[CategoryLine, ...], tuple[aggregate.BudgetVariance, ...]]:
    """Per-category lines (month frame) joined to FY budget variance, plus the variances.

    The income/expense/net figures are for the *reporting frame* passed in; budget variance
    is the FY-to-date figure from :func:`analytics.budget_vs_actual` (budgets are annual).
    """
    variances = tuple(analytics.budget_vs_actual(fy_frame, budget_rows, fiscal_year))
    variance_by_cat: dict[str, aggregate.BudgetVariance] = {}
    for bv in variances:
        # per_grade defaults False, so grade is None and category is the sole key.
        variance_by_cat[bv.category] = bv

    lines: list[CategoryLine] = []
    for cat in analytics.by_category(fy_frame):
        matched = variance_by_cat.get(cat.category)
        budgeted = matched.budgeted if matched is not None else Decimal("0.00")
        variance = matched.variance if matched is not None else (Decimal("0.00") - cat.expense)
        lines.append(
            CategoryLine(
                category=cat.category,
                income=cat.income,
                expense=cat.expense,
                net=cat.net,
                budgeted=budgeted,
                variance=variance,
            )
        )
    return tuple(lines), variances


def _report_transactions(month_rows: Sequence[Mapping[str, str]]) -> tuple[ReportTransaction, ...]:
    """Build the internal transaction rows (full detail), date-then-id ordered.

    ``receipt_url`` is left blank in v1 (the ``transactions`` tab stores only a
    ``receipt_id`` FK; resolving it to a Drive URL needs the ``receipts`` tab, a Phase-2
    join). The field exists so the internal template can render a link when the join lands.
    """
    from pta_finance import models

    out: list[ReportTransaction] = []
    for row in month_rows:
        if models.parse_bool(row.get("needs_review")):
            continue
        out.append(
            ReportTransaction(
                id=str(row.get("id", "")),
                date=str(row.get("date", "")),
                type=str(row.get("type", "")),
                amount=models.parse_amount(row["amount"]),
                category=str(row.get("category", "")),
                grade=str(row.get("grade", "")),
                payee=str(row.get("payee", "")),
                memo=str(row.get("memo", "")),
                receipt_id=str(row.get("receipt_id", "")),
                receipt_url="",
                entered_by=str(row.get("entered_by", "")),
            )
        )
    out.sort(key=lambda t: (t.date, t.id))
    return tuple(out)


def build_internal_report(
    config: Config,
    month: str,
    txn_rows: Iterable[Mapping[str, str]],
    budget_rows: Iterable[Mapping[str, str]],
) -> InternalReport:
    """Compute the full-detail :class:`InternalReport` for ``month`` (``YYYY-MM``).

    Reads-only over the supplied ``transactions`` / ``budget`` row dicts (no Google I/O).
    Monthly figures (totals, by-category income/expense, by-grade, the ledger lines) cover
    the report month; budget variance, fundraising progress, and the headline cover the
    fiscal year the month falls in (budgets are annual).
    """
    txn_rows = list(txn_rows)
    budget_rows = list(budget_rows)
    first_iso, last_iso = _month_bounds(month)
    month_rows = _month_rows(txn_rows, first_iso, last_iso)

    start_month = config.fiscal_year.start_month
    from datetime import date

    fiscal_year = ids.fiscal_year_label(date.fromisoformat(first_iso), start_month)

    month_frame = analytics.build_frame(month_rows, start_month=start_month).frame
    fy_built = analytics.build_frame(txn_rows, start_month=start_month).frame
    fy_frame = fy_built[fy_built[aggregate.FISCAL_YEAR_INT] == fiscal_year]

    category_lines, variances = _category_lines(month_frame, budget_rows, fiscal_year)
    return InternalReport(
        organization=config.organization.name,
        school_name=config.organization.school_name,
        month=month,
        fiscal_year=fiscal_year,
        totals=_totals(month_frame),
        by_category=category_lines,
        by_grade=_grade_lines(month_frame),
        fundraising=_fundraising_progress(fy_frame, budget_rows, fiscal_year),
        budget_headline=_budget_headline(
            tuple(analytics.budget_vs_actual(fy_frame, budget_rows, fiscal_year)),
            fiscal_year,
        ),
        transactions=_report_transactions(month_rows),
    )


def build_external_report(
    config: Config,
    month: str,
    txn_rows: Iterable[Mapping[str, str]],
    budget_rows: Iterable[Mapping[str, str]],
) -> ExternalReport:
    """Compute the public-safe :class:`ExternalReport` for ``month`` (``YYYY-MM``).

    Carries only aggregate, non-identifying figures (see module docstring). Before returning,
    it calls :func:`_assert_external_safe`, which recursively scans the assembled model for
    any denylisted PII field and raises :class:`ExternalReportPIIError` if one is present.
    The guard runs unconditionally — a contaminated external model never escapes this
    function.
    """
    txn_rows = list(txn_rows)
    budget_rows = list(budget_rows)
    first_iso, last_iso = _month_bounds(month)
    month_rows = _month_rows(txn_rows, first_iso, last_iso)

    start_month = config.fiscal_year.start_month
    from datetime import date

    fiscal_year = ids.fiscal_year_label(date.fromisoformat(first_iso), start_month)

    month_frame = analytics.build_frame(month_rows, start_month=start_month).frame
    fy_built = analytics.build_frame(txn_rows, start_month=start_month).frame
    fy_frame = fy_built[fy_built[aggregate.FISCAL_YEAR_INT] == fiscal_year]

    model = ExternalReport(
        organization=config.organization.name,
        school_name=config.organization.school_name,
        month=month,
        fiscal_year=fiscal_year,
        totals=_totals(month_frame),
        by_grade=_grade_lines(month_frame),
        fundraising=_fundraising_progress(fy_frame, budget_rows, fiscal_year),
        budget_headline=_budget_headline(
            tuple(analytics.budget_vs_actual(fy_frame, budget_rows, fiscal_year)),
            fiscal_year,
        ),
    )
    # SECURITY INVARIANT: never return an external model that carries PII.
    _assert_external_safe(model)
    return model


def build_reports(
    config: Config,
    month: str,
    txn_rows: Iterable[Mapping[str, str]],
    budget_rows: Iterable[Mapping[str, str]],
) -> tuple[InternalReport, ExternalReport]:
    """Build both variants from the same inputs (convenience for ``--variant both``)."""
    txn_rows = list(txn_rows)
    budget_rows = list(budget_rows)
    return (
        build_internal_report(config, month, txn_rows, budget_rows),
        build_external_report(config, month, txn_rows, budget_rows),
    )
