"""Pure pandas aggregations over the ``transactions`` ledger — exact-money, no I/O.

Every function here takes a DataFrame built by :func:`build_frame` (or, for
:func:`budget_vs_actual`, that frame plus the ``budget`` tab's row dicts) and returns
plain Python objects (``Decimal`` dollar values, lists of dataclasses). No function touches
Google or disk, so the analytics are unit-testable from a hand-built fixture.

Money exactness (the central invariant)
----------------------------------------
:func:`build_frame` stores each amount as **integer cents** in the :data:`AMOUNT_CENTS`
column (``int((Decimal_dollars * 100)).to_integral_value()``), signed by transaction
``type`` so income is positive and expense is negative for net math. All ``groupby`` /
``Grouper`` sums therefore run over Python/NumPy integers — pandas never sums dollar floats,
so ``0.10 + 0.20`` aggregates to exactly ``30`` cents -> ``Decimal("0.30")``. A dollar
:class:`~decimal.Decimal` is reconstructed only at the boundary by :func:`cents_to_dollars`.

``needs_review`` exclusion
--------------------------
:func:`build_frame` drops every row whose ``needs_review`` cell parses truthy
(``models.parse_bool``). Malformed and dedup-flagged rows are pending operator review;
summing them would double-count a duplicate. The dropped count is reported on
:attr:`BuiltFrame.excluded_needs_review`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

import pandas as pd

from pta_finance import ids, models, schema

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "AMOUNT_CENTS",
    "BuiltFrame",
    "build_frame",
    "cents_to_dollars",
    "Totals",
    "totals",
    "CategoryAmount",
    "by_category",
    "GradeAmount",
    "by_grade",
    "MonthAmounts",
    "by_month",
    "BudgetVariance",
    "budget_vs_actual",
]

# --- Frame column names ----------------------------------------------------
#
# The built frame keeps the raw schema string columns the aggregations need PLUS three
# derived columns. The amount lives ONLY as integer cents — never as a dollar float — so
# every sum below is exact.
AMOUNT_CENTS = "amount_cents"  # signed integer cents (income +, expense -)
DATE_DT = "date_dt"  # parsed datetime64 (for pd.Grouper monthly buckets)
FISCAL_YEAR_INT = "fiscal_year_int"  # int fiscal-year label

# The explicit bucket for rows with no grade (school-wide spend), never dropped.
UNASSIGNED_GRADE = "unassigned"

_EXPENSE = "expense"


def cents_to_dollars(cents: int) -> Decimal:
    """Convert signed integer ``cents`` back to a dollar :class:`~decimal.Decimal`.

    Exact: ``Decimal(cents) / 100`` quantized to two places. This is the ONLY place dollars
    re-enter the pipeline, and it never goes through ``float``.
    """
    return (Decimal(int(cents)) / Decimal(100)).quantize(Decimal("0.01"))


def _amount_cents(amount: Decimal) -> int:
    """Integer cents for a Decimal dollar amount (rounds to the nearest cent).

    Mirrors :func:`pta_finance.etl._amount_cents` so the analytics and the dedup key agree
    on the cents representation.
    """
    return int((amount * 100).to_integral_value())


@dataclass(frozen=True)
class BuiltFrame:
    """A parsed analytics frame plus the count of rows excluded for ``needs_review``.

    ``frame`` columns: the raw ``type``/``category``/``grade`` strings, plus the derived
    :data:`AMOUNT_CENTS` (signed int cents), :data:`DATE_DT` (datetime64), and
    :data:`FISCAL_YEAR_INT` (int). ``excluded_needs_review`` is how many input rows were
    dropped because their ``needs_review`` flag was truthy.
    """

    frame: pd.DataFrame
    excluded_needs_review: int


def build_frame(
    rows: Iterable[Mapping[str, str]],
    *,
    start_month: int = 1,
) -> BuiltFrame:
    """Build a typed analytics DataFrame from ``transactions`` row dicts.

    Parameters
    ----------
    rows:
        ``transactions`` records keyed by :data:`schema.TRANSACTIONS_COLUMNS` (cell strings).
    start_month:
        Fiscal-year start month, forwarded to :func:`pta_finance.ids.fiscal_year_label` when
        a row's ``fiscal_year`` cell is blank or non-numeric (so the label is derived from the
        date rather than re-hard-coded here).

    Returns
    -------
    BuiltFrame
        The frame (rows flagged ``needs_review`` already removed) and the excluded count.

    Notes
    -----
    * **needs_review exclusion** — any row whose ``needs_review`` cell parses truthy via
      :func:`pta_finance.models.parse_bool` is dropped before the frame is built; it is never
      summed. The drop count is returned on :attr:`BuiltFrame.excluded_needs_review`.
    * **Exact money** — ``amount`` is parsed with :func:`pta_finance.models.parse_amount`
      (Decimal) then stored as signed integer cents (income +, expense -). No dollar float is
      ever materialized.
    """
    amount_cents: list[int] = []
    dates: list[date] = []
    fiscal_years: list[int] = []
    types: list[str] = []
    categories: list[str] = []
    grades: list[str] = []

    excluded = 0
    for row in rows:
        if models.parse_bool(row.get("needs_review")):
            excluded += 1
            continue

        d = models.parse_date(row["date"])
        amount = models.parse_amount(row["amount"])
        txn_type = str(row.get("type", "")).strip().casefold()
        # Sign cents by type so a single integer column carries income (+) and expense (-).
        cents = _amount_cents(amount)
        signed = -cents if txn_type == _EXPENSE else cents

        amount_cents.append(signed)
        dates.append(d)
        fiscal_years.append(_resolve_fiscal_year(row, d, start_month))
        types.append(txn_type)
        categories.append(str(row.get("category", "")).strip())
        grade = str(row.get("grade", "")).strip()
        grades.append(grade or UNASSIGNED_GRADE)

    frame = pd.DataFrame(
        {
            "type": pd.Series(types, dtype="object"),
            "category": pd.Series(categories, dtype="object"),
            "grade": pd.Series(grades, dtype="object"),
            AMOUNT_CENTS: pd.Series(amount_cents, dtype="int64"),
            DATE_DT: pd.Series(pd.to_datetime(dates), dtype="datetime64[ns]"),
            FISCAL_YEAR_INT: pd.Series(fiscal_years, dtype="int64"),
        }
    )
    return BuiltFrame(frame=frame, excluded_needs_review=excluded)


def _resolve_fiscal_year(row: Mapping[str, str], d: date, start_month: int) -> int:
    """The row's fiscal-year label: trust a numeric ``fiscal_year`` cell, else derive it.

    A blank or non-numeric ``fiscal_year`` cell falls back to
    :func:`pta_finance.ids.fiscal_year_label` (never re-derived inline).
    """
    raw = str(row.get("fiscal_year", "")).strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return ids.fiscal_year_label(d, start_month)


def _income_expense_net_cents(frame: pd.DataFrame) -> tuple[int, int, int]:
    """Return ``(income_cents, expense_cents, net_cents)`` for a frame.

    ``income_cents`` is the sum of positive (income) entries, ``expense_cents`` the absolute
    sum of the negative (expense) entries (reported as a positive magnitude), and
    ``net_cents`` is income minus expense.
    """
    if frame.empty:
        return 0, 0, 0
    cents = frame[AMOUNT_CENTS]
    income = int(cents[cents > 0].sum())
    expense = int(-cents[cents < 0].sum())
    return income, expense, income - expense


# --- totals ----------------------------------------------------------------


@dataclass(frozen=True)
class Totals:
    """Total income, total expense (positive magnitude), and net (income - expense)."""

    income: Decimal
    expense: Decimal
    net: Decimal


def totals(frame: pd.DataFrame) -> Totals:
    """Total income, total expense, and net across the whole frame (exact dollars)."""
    income, expense, net = _income_expense_net_cents(frame)
    return Totals(
        income=cents_to_dollars(income),
        expense=cents_to_dollars(expense),
        net=cents_to_dollars(net),
    )


# --- by category -----------------------------------------------------------


@dataclass(frozen=True)
class CategoryAmount:
    """Income/expense/net dollar totals for one ``category``."""

    category: str
    income: Decimal
    expense: Decimal
    net: Decimal


def by_category(frame: pd.DataFrame) -> list[CategoryAmount]:
    """Income/expense/net per ``category``, sorted by category name.

    Sums run over the signed integer-cents column, so the per-category figures are exact.
    """
    return [
        CategoryAmount(
            category=str(category),
            income=cents_to_dollars(income),
            expense=cents_to_dollars(expense),
            net=cents_to_dollars(net),
        )
        for category, (income, expense, net) in _grouped_income_expense_net(frame, "category")
    ]


# --- by grade --------------------------------------------------------------


@dataclass(frozen=True)
class GradeAmount:
    """Income/expense/net dollar totals for one ``grade`` bucket.

    Rows with an empty grade are bucketed under :data:`UNASSIGNED_GRADE` (school-wide
    spend), never dropped.
    """

    grade: str
    income: Decimal
    expense: Decimal
    net: Decimal


def by_grade(frame: pd.DataFrame) -> list[GradeAmount]:
    """Income/expense/net per ``grade``, sorted by grade label.

    Rows whose ``grade`` was empty are collected under the explicit
    :data:`UNASSIGNED_GRADE` bucket (set in :func:`build_frame`), never dropped.
    """
    return [
        GradeAmount(
            grade=str(grade),
            income=cents_to_dollars(income),
            expense=cents_to_dollars(expense),
            net=cents_to_dollars(net),
        )
        for grade, (income, expense, net) in _grouped_income_expense_net(frame, "grade")
    ]


# --- by month --------------------------------------------------------------


@dataclass(frozen=True)
class MonthAmounts:
    """Income/expense/net dollar totals for one calendar month (its first day)."""

    month: date
    income: Decimal
    expense: Decimal
    net: Decimal


def by_month(frame: pd.DataFrame) -> list[MonthAmounts]:
    """Monthly income/expense/net via ``pd.Grouper(freq="MS")`` (month start), time-ordered.

    Buckets every transaction into the first day of its month and sums the signed cents, so
    each month's figures are exact. Months with no transactions are omitted.
    """
    if frame.empty:
        return []
    grouped = frame.groupby(pd.Grouper(key=DATE_DT, freq="MS"))[AMOUNT_CENTS]
    out: list[MonthAmounts] = []
    for period, cents in grouped:
        # Drop empty MS buckets that Grouper can interpolate between populated months.
        if len(cents) == 0:
            continue
        income = int(cents[cents > 0].sum())
        expense = int(-cents[cents < 0].sum())
        month_ts = pd.Timestamp(period)
        out.append(
            MonthAmounts(
                month=month_ts.date().replace(day=1),
                income=cents_to_dollars(income),
                expense=cents_to_dollars(expense),
                net=cents_to_dollars(income - expense),
            )
        )
    out.sort(key=lambda m: m.month)
    return out


# --- budget vs actual ------------------------------------------------------


@dataclass(frozen=True)
class BudgetVariance:
    """Budget vs actual spend for one category (optionally per grade), one fiscal year.

    ``budgeted`` is the ``budget`` tab's figure; ``actual`` is realized expense (a positive
    magnitude); ``variance = budgeted - actual`` (positive => under budget, negative => over).
    """

    category: str
    grade: str | None
    budgeted: Decimal
    actual: Decimal
    variance: Decimal


def budget_vs_actual(
    frame: pd.DataFrame,
    budget_rows: Iterable[Mapping[str, str]],
    fiscal_year: int,
    *,
    per_grade: bool = False,
) -> list[BudgetVariance]:
    """Join actual expense against the ``budget`` tab per category for ``fiscal_year``.

    Parameters
    ----------
    frame:
        The analytics frame from :func:`build_frame`.
    budget_rows:
        ``budget`` tab records keyed by :data:`schema.BUDGET_COLUMNS`. Only rows whose
        ``fiscal_year`` matches ``fiscal_year`` are considered.
    fiscal_year:
        The integer fiscal-year label to report on.
    per_grade:
        When ``True`` the join key is ``(category, grade)`` so grade-specific budget lines are
        kept distinct; when ``False`` (default) the key is ``category`` alone and actuals are
        summed across grades.

    Returns
    -------
    list[BudgetVariance]
        One row per budgeted (and/or actually-spent) key, sorted by ``(category, grade)``.
        Categories that were budgeted but had no spend show ``actual == 0``; categories with
        spend but no budget line show ``budgeted == 0`` (so over-spend on an un-budgeted
        category still surfaces, with a negative variance).
    """
    # --- Budgeted cents per key (exact: parse Decimal -> int cents). ---
    budgeted_cents: dict[tuple[str, str | None], int] = {}
    for brow in budget_rows:
        if _to_int(brow.get("fiscal_year")) != fiscal_year:
            continue
        category = str(brow.get("category", "")).strip()
        grade = _grade_key(brow.get("grade"), per_grade)
        amount = models.parse_optional_amount(brow.get("budgeted_amount"))
        if amount is None:
            continue
        key = (category, grade)
        budgeted_cents[key] = budgeted_cents.get(key, 0) + _amount_cents(amount)

    # --- Actual expense cents per key for this fiscal year (exact integer sums). ---
    actual_cents: dict[tuple[str, str | None], int] = {}
    if not frame.empty:
        fy_frame = frame[(frame[FISCAL_YEAR_INT] == fiscal_year) & (frame[AMOUNT_CENTS] < 0)]
        for _, fr in fy_frame.iterrows():
            category = str(fr["category"]).strip()
            grade = _grade_from_frame(str(fr["grade"]), per_grade)
            key = (category, grade)
            actual_cents[key] = actual_cents.get(key, 0) + int(-fr[AMOUNT_CENTS])

    keys = sorted(
        set(budgeted_cents) | set(actual_cents),
        key=lambda k: (k[0], k[1] or ""),
    )
    out: list[BudgetVariance] = []
    for category, grade in keys:
        budgeted = budgeted_cents.get((category, grade), 0)
        actual = actual_cents.get((category, grade), 0)
        out.append(
            BudgetVariance(
                category=category,
                grade=grade,
                budgeted=cents_to_dollars(budgeted),
                actual=cents_to_dollars(actual),
                variance=cents_to_dollars(budgeted - actual),
            )
        )
    return out


# --- shared grouping helper ------------------------------------------------


def _grouped_income_expense_net(
    frame: pd.DataFrame, key: str
) -> list[tuple[str, tuple[int, int, int]]]:
    """Group ``frame`` by ``key`` and return ``[(value, (income, expense, net)), ...]`` cents.

    Sorted by the group value. Each tuple is exact integer cents; the caller converts to
    dollars at the boundary.
    """
    if frame.empty:
        return []
    out: list[tuple[str, tuple[int, int, int]]] = []
    for value, group in frame.groupby(key, sort=True):
        cents = group[AMOUNT_CENTS]
        income = int(cents[cents > 0].sum())
        expense = int(-cents[cents < 0].sum())
        out.append((str(value), (income, expense, income - expense)))
    return out


def _grade_key(raw: str | None, per_grade: bool) -> str | None:
    """Budget-row grade join key: ``None`` when not per-grade or the cell is blank."""
    if not per_grade:
        return None
    grade = str(raw or "").strip()
    return grade or UNASSIGNED_GRADE


def _grade_from_frame(grade: str, per_grade: bool) -> str | None:
    """Frame grade join key (frame already maps blank -> UNASSIGNED_GRADE)."""
    if not per_grade:
        return None
    return grade


def _to_int(raw: str | None) -> int | None:
    """Parse a fiscal-year cell to int, or ``None`` if blank/non-numeric."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


# Keep schema import meaningfully referenced: the frame's input columns are the canonical
# transactions columns. Asserting the columns the analytics reads are a subset guards against
# a schema rename silently producing all-empty aggregations.
_REQUIRED_INPUT_COLUMNS = ("date", "amount", "type", "category", "grade", "needs_review")
assert set(_REQUIRED_INPUT_COLUMNS) <= set(schema.TRANSACTIONS_COLUMNS)
