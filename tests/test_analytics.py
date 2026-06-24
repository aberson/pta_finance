"""Tests for pta_finance.analytics — known-fixture -> exact-number assertions, no I/O.

The fixture is a small hand-built ``transactions`` ledger that exercises every case the
analytics engine must handle:

* mixed income + expense,
* two categories (``supplies``, ``fundraiser``),
* two grades (``"3"`` and an EMPTY grade -> the ``unassigned`` school-wide bucket),
* two fiscal years (2025 and 2026),
* a ``needs_review="TRUE"`` row that MUST be excluded from every aggregation, and
* a money-exactness pair (``0.10`` + ``0.20``) that must sum to exactly ``0.30``.

Every assertion is an exact expected number — no "approximately". The budget fixture and
the YoY deltas are likewise checked against hand-computed values.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from pta_finance import analytics, schema
from pta_finance.analytics import aggregate

_TXN_COLS = schema.TRANSACTIONS_COLUMNS
_BUD_COLS = schema.BUDGET_COLUMNS
_START_MONTH = 1  # calendar-year FY => FY label == date.year


def _txn(**overrides: str) -> dict[str, str]:
    """A transactions row dict with every schema column present (blank unless overridden)."""
    row = {col: "" for col in _TXN_COLS}
    row.update(overrides)
    return row


def _bud(**overrides: str) -> dict[str, str]:
    """A budget row dict with every schema column present (blank unless overridden)."""
    row = {col: "" for col in _BUD_COLS}
    row.update(overrides)
    return row


def _ledger() -> list[dict[str, str]]:
    """Hand-built ledger. Expected exact figures (excluding the needs_review row):

    FY2026
      income   fundraiser  unassigned   $500.00   (id TXN-FY26-0001)
      expense  supplies    grade 3      $120.00   (id TXN-FY26-0002)
      expense  supplies    unassigned   $ 30.00   (id TXN-FY26-0003)
      expense  fundraiser  unassigned   $  0.10   (money-exact pair, part 1)
      expense  fundraiser  unassigned   $  0.20   (money-exact pair, part 2)
      <EXCLUDED needs_review=TRUE>      $999.99   (must NOT be summed anywhere)

    FY2025
      income   fundraiser  unassigned   $300.00   (id TXN-FY25-0001)
      expense  supplies    grade 3      $ 80.00   (id TXN-FY25-0002)

    Derived FY2026: income 500.00, expense 150.30, net 349.70.
    Derived FY2025: income 300.00, expense  80.00, net 220.00.
    """
    return [
        # --- FY2026 ---
        _txn(
            id="TXN-FY26-0001",
            date="2026-01-15",
            fiscal_year="2026",
            type="income",
            amount="500.00",
            category="fundraiser",
            grade="",
            payee="Bake Sale",
        ),
        _txn(
            id="TXN-FY26-0002",
            date="2026-02-10",
            fiscal_year="2026",
            type="expense",
            amount="120.00",
            category="supplies",
            grade="3",
            payee="Supply Co",
        ),
        _txn(
            id="TXN-FY26-0003",
            date="2026-02-20",
            fiscal_year="2026",
            type="expense",
            amount="30.00",
            category="supplies",
            grade="",
            payee="Supply Co",
        ),
        # Money-exactness pair: 0.10 + 0.20 must aggregate to exactly 0.30.
        _txn(
            id="TXN-FY26-0004",
            date="2026-03-01",
            fiscal_year="2026",
            type="expense",
            amount="0.10",
            category="fundraiser",
            grade="",
            payee="Misc",
        ),
        _txn(
            id="TXN-FY26-0005",
            date="2026-03-02",
            fiscal_year="2026",
            type="expense",
            amount="0.20",
            category="fundraiser",
            grade="",
            payee="Misc",
        ),
        # EXCLUDED: needs_review row must never be summed (a flagged duplicate/malformed row).
        _txn(
            id="TXN-FY26-0006",
            date="2026-03-03",
            fiscal_year="2026",
            type="expense",
            amount="999.99",
            category="supplies",
            grade="3",
            payee="DO NOT COUNT",
            needs_review="TRUE",
        ),
        # --- FY2025 ---
        _txn(
            id="TXN-FY25-0001",
            date="2025-05-15",
            fiscal_year="2025",
            type="income",
            amount="300.00",
            category="fundraiser",
            grade="",
            payee="Auction",
        ),
        _txn(
            id="TXN-FY25-0002",
            date="2025-06-10",
            fiscal_year="2025",
            type="expense",
            amount="80.00",
            category="supplies",
            grade="3",
            payee="Supply Co",
        ),
    ]


def _budget() -> list[dict[str, str]]:
    """Budget fixture: FY2026 supplies budgeted $200.00 (actual $150.00 -> +$50 under);
    FY2026 fundraiser budgeted $0.25 (actual $0.30 -> -$0.05 over). A FY2025 line that must
    be ignored when reporting FY2026."""
    return [
        _bud(
            id="BUD-FY26-supplies",
            fiscal_year="2026",
            category="supplies",
            grade="",
            budgeted_amount="200.00",
        ),
        _bud(
            id="BUD-FY26-fundraiser",
            fiscal_year="2026",
            category="fundraiser",
            grade="",
            budgeted_amount="0.25",
        ),
        _bud(
            id="BUD-FY25-supplies",
            fiscal_year="2025",
            category="supplies",
            grade="",
            budgeted_amount="100.00",
        ),
    ]


def _built() -> aggregate.BuiltFrame:
    return analytics.build_frame(_ledger(), start_month=_START_MONTH)


def _fy(frame, fiscal_year: int):  # type: ignore[no-untyped-def]
    """Filter the built frame to one fiscal year (mirrors the CLI's --fy filter)."""
    return frame[frame[aggregate.FISCAL_YEAR_INT] == fiscal_year]


# --- needs_review exclusion -------------------------------------------------


def test_build_frame_excludes_needs_review_rows() -> None:
    """The needs_review=TRUE row is dropped from the frame and counted, never summed."""
    built = _built()
    # 8 input rows, 1 flagged needs_review -> 7 remain, 1 excluded.
    assert built.excluded_needs_review == 1
    assert len(built.frame) == 7
    # The flagged $999.99 expense must not appear anywhere: total expense excludes it.
    tot = analytics.totals(built.frame)
    assert tot.expense == Decimal("230.30")  # 150.30 (FY26) + 80.00 (FY25); NOT +999.99


# --- money exactness --------------------------------------------------------


def test_money_exactness_no_float_drift() -> None:
    """0.10 + 0.20 aggregates to exactly 0.30 (integer-cents math, no binary-float tail)."""
    built = _built()
    fy26 = _fy(built.frame, 2026)
    # The two fundraiser expenses (0.10 + 0.20) net against $0 fundraiser income in FY2026.
    fundraiser = next(c for c in analytics.by_category(fy26) if c.category == "fundraiser")
    # Exactly 0.30 expense — the canonical float-drift sentinel value.
    assert fundraiser.expense == Decimal("0.30")
    assert str(fundraiser.expense) == "0.30"  # not "0.30000000000000004"
    # Income on fundraiser in FY2026 is the $500 bake sale; net = 500.00 - 0.30.
    assert fundraiser.income == Decimal("500.00")
    assert fundraiser.net == Decimal("499.70")


# --- totals -----------------------------------------------------------------


def test_totals_whole_ledger() -> None:
    """Totals across both fiscal years (excluding the needs_review row)."""
    tot = analytics.totals(_built().frame)
    assert tot.income == Decimal("800.00")  # 500 (FY26) + 300 (FY25)
    assert tot.expense == Decimal("230.30")  # 150.30 (FY26) + 80.00 (FY25)
    assert tot.net == Decimal("569.70")  # 800.00 - 230.30


def test_totals_filtered_to_one_fiscal_year() -> None:
    """The FY2026 slice totals match the hand-computed FY2026 figures."""
    fy26 = _fy(_built().frame, 2026)
    tot = analytics.totals(fy26)
    assert tot.income == Decimal("500.00")
    assert tot.expense == Decimal("150.30")  # 120 + 30 + 0.10 + 0.20
    assert tot.net == Decimal("349.70")


def test_totals_empty_frame() -> None:
    """An empty frame totals to zero, never errors."""
    empty = analytics.build_frame([], start_month=_START_MONTH).frame
    tot = analytics.totals(empty)
    assert tot.income == Decimal("0.00")
    assert tot.expense == Decimal("0.00")
    assert tot.net == Decimal("0.00")


# --- by category ------------------------------------------------------------


def test_by_category_exact() -> None:
    """Per-category income/expense/net across the whole ledger (sorted by category)."""
    cats = {c.category: c for c in analytics.by_category(_built().frame)}
    assert set(cats) == {"fundraiser", "supplies"}

    # fundraiser: income 500 (FY26) + 300 (FY25) = 800; expense 0.10 + 0.20 = 0.30.
    assert cats["fundraiser"].income == Decimal("800.00")
    assert cats["fundraiser"].expense == Decimal("0.30")
    assert cats["fundraiser"].net == Decimal("799.70")

    # supplies: all expense — 120 + 30 (FY26) + 80 (FY25) = 230; no income.
    assert cats["supplies"].income == Decimal("0.00")
    assert cats["supplies"].expense == Decimal("230.00")
    assert cats["supplies"].net == Decimal("-230.00")


# --- by grade (incl. the unassigned bucket) ---------------------------------


def test_by_grade_unassigned_bucket() -> None:
    """Empty-grade rows land in an explicit 'unassigned' bucket, never dropped."""
    grades = {g.grade: g for g in analytics.by_grade(_built().frame)}
    # Exactly two buckets: grade "3" and the school-wide "unassigned" bucket.
    assert set(grades) == {"3", aggregate.UNASSIGNED_GRADE}
    assert aggregate.UNASSIGNED_GRADE == "unassigned"

    # grade 3: supplies expense 120 (FY26) + 80 (FY25) = 200; no income.
    assert grades["3"].expense == Decimal("200.00")
    assert grades["3"].income == Decimal("0.00")
    assert grades["3"].net == Decimal("-200.00")

    # unassigned: income 500 + 300 = 800; expense 30 + 0.10 + 0.20 = 30.30.
    assert grades["unassigned"].income == Decimal("800.00")
    assert grades["unassigned"].expense == Decimal("30.30")
    assert grades["unassigned"].net == Decimal("769.70")


# --- by month (pd.Grouper MS) -----------------------------------------------


def test_by_month_buckets() -> None:
    """Monthly buckets via pd.Grouper(freq='MS'); empty months omitted, time-ordered."""
    months = {m.month: m for m in analytics.by_month(_built().frame)}
    # Populated months only: 2025-05, 2025-06, 2026-01, 2026-02, 2026-03.
    assert set(months) == {
        date(2025, 5, 1),
        date(2025, 6, 1),
        date(2026, 1, 1),
        date(2026, 2, 1),
        date(2026, 3, 1),
    }
    # 2026-02: two supplies expenses (120 + 30), no income.
    assert months[date(2026, 2, 1)].expense == Decimal("150.00")
    assert months[date(2026, 2, 1)].income == Decimal("0.00")
    # 2026-03: the money-exact pair only (0.10 + 0.20 = 0.30 expense).
    assert months[date(2026, 3, 1)].expense == Decimal("0.30")
    # 2026-01: the $500 income bake sale.
    assert months[date(2026, 1, 1)].income == Decimal("500.00")
    # Returned list is sorted ascending by month.
    ordered = [m.month for m in analytics.by_month(_built().frame)]
    assert ordered == sorted(ordered)


# --- budget vs actual -------------------------------------------------------


def test_budget_vs_actual_signs() -> None:
    """Variance = budgeted - actual; positive => under budget, negative => over budget."""
    fy26 = _fy(_built().frame, 2026)
    rows = {b.category: b for b in analytics.budget_vs_actual(fy26, _budget(), 2026)}
    assert set(rows) == {"supplies", "fundraiser"}

    # supplies: budgeted 200.00, actual 150.00 (120 + 30) -> +50.00 (UNDER budget).
    assert rows["supplies"].budgeted == Decimal("200.00")
    assert rows["supplies"].actual == Decimal("150.00")
    assert rows["supplies"].variance == Decimal("50.00")
    assert rows["supplies"].variance > 0  # under budget => positive

    # fundraiser: budgeted 0.25, actual 0.30 -> -0.05 (OVER budget) — exact-cents check too.
    assert rows["fundraiser"].budgeted == Decimal("0.25")
    assert rows["fundraiser"].actual == Decimal("0.30")
    assert rows["fundraiser"].variance == Decimal("-0.05")
    assert rows["fundraiser"].variance < 0  # over budget => negative


def test_budget_vs_actual_ignores_other_fiscal_year() -> None:
    """A FY2025 budget line is not reported when asked for FY2026."""
    fy26 = _fy(_built().frame, 2026)
    result = analytics.budget_vs_actual(fy26, _budget(), 2026)
    # No row should reflect the FY2025 supplies budget (100.00); supplies budgeted is 200.00.
    supplies = next(b for b in result if b.category == "supplies")
    assert supplies.budgeted == Decimal("200.00")


def test_budget_vs_actual_unbudgeted_category_surfaces_overspend() -> None:
    """Spend on an un-budgeted category surfaces with budgeted=0 and a negative variance."""
    fy26 = _fy(_built().frame, 2026)
    # Budget only the fundraiser; supplies spend ($150) must still appear as overspend.
    only_fundraiser = [b for b in _budget() if b["category"] == "fundraiser"]
    rows = {b.category: b for b in analytics.budget_vs_actual(fy26, only_fundraiser, 2026)}
    assert "supplies" in rows
    assert rows["supplies"].budgeted == Decimal("0.00")
    assert rows["supplies"].actual == Decimal("150.00")
    assert rows["supplies"].variance == Decimal("-150.00")


def test_budget_vs_actual_per_grade() -> None:
    """per_grade=True keeps grade-specific actuals distinct from the unassigned bucket."""
    fy26 = _fy(_built().frame, 2026)
    rows = analytics.budget_vs_actual(fy26, _budget(), 2026, per_grade=True)
    # supplies appears twice: grade '3' ($120) and 'unassigned' ($30).
    supplies = {b.grade: b for b in rows if b.category == "supplies"}
    assert supplies["3"].actual == Decimal("120.00")
    assert supplies[aggregate.UNASSIGNED_GRADE].actual == Decimal("30.00")


# --- trends: fundraising + spend by year ------------------------------------


def test_fundraising_and_spend_by_year() -> None:
    """Per-year income (fundraising) and expense, oldest first."""
    series = analytics.fundraising_and_spend_by_year(_built().frame)
    assert [y.fiscal_year for y in series] == [2025, 2026]  # oldest first

    fy2025, fy2026 = series
    assert fy2025.income == Decimal("300.00")
    assert fy2025.expense == Decimal("80.00")
    assert fy2026.income == Decimal("500.00")
    assert fy2026.expense == Decimal("150.30")


# --- trends: year over year -------------------------------------------------


def test_year_over_year_deltas() -> None:
    """Absolute + percent YoY change between FY2025 and FY2026."""
    changes = analytics.year_over_year(_built().frame)
    assert len(changes) == 1
    yoy = changes[0]
    assert (yoy.prior_year, yoy.year) == (2025, 2026)

    # income: 500 - 300 = +200; pct = 200/300*100 = 66.67% (rounded to 0.01).
    assert yoy.income_change == Decimal("200.00")
    assert yoy.income_pct == Decimal("66.67")

    # expense: 150.30 - 80.00 = +70.30; pct = 70.30/80.00*100 = 87.875 -> 87.88.
    assert yoy.expense_change == Decimal("70.30")
    assert yoy.expense_pct == Decimal("87.88")


def test_year_over_year_zero_base_pct_is_none() -> None:
    """Percent change is None when the prior-year base is zero (undefined)."""
    rows = [
        _txn(
            id="TXN-FY25-0001",
            date="2025-06-10",
            fiscal_year="2025",
            type="expense",
            amount="50.00",
            category="supplies",
        ),
        _txn(
            id="TXN-FY26-0001",
            date="2026-01-10",
            fiscal_year="2026",
            type="income",
            amount="100.00",
            category="fundraiser",
        ),
    ]
    changes = analytics.year_over_year(analytics.build_frame(rows, start_month=_START_MONTH).frame)
    (yoy,) = changes
    # FY2025 had zero income -> income_pct undefined (None); FY2025 had $50 expense,
    # FY2026 had zero expense -> expense_pct = -100.00%.
    assert yoy.income_pct is None
    assert yoy.income_change == Decimal("100.00")
    assert yoy.expense_pct == Decimal("-100.00")
    assert yoy.expense_change == Decimal("-50.00")


# --- fiscal-year derivation when the cell is blank --------------------------


def test_fiscal_year_derived_when_cell_blank() -> None:
    """A blank fiscal_year cell falls back to ids.fiscal_year_label(date, start_month)."""
    rows = [
        _txn(
            id="TXN-FY26-0001",
            date="2026-07-15",
            fiscal_year="",  # blank -> derive from date
            type="income",
            amount="10.00",
            category="fundraiser",
        ),
    ]
    series = analytics.fundraising_and_spend_by_year(
        analytics.build_frame(rows, start_month=_START_MONTH).frame
    )
    # start_month=1 (calendar year) => label is the date's year, 2026.
    assert [y.fiscal_year for y in series] == [2026]


def test_malformed_amount_in_unflagged_row_raises() -> None:
    """An un-flagged row with a bad amount raises (ETL is responsible for flagging first).

    build_frame trusts that the canonical ledger has already been normalized: a row that is
    NOT needs_review but carries an unparseable amount is a contract violation and surfaces
    loudly rather than being silently summed as zero.
    """
    rows = [
        _txn(
            id="TXN-FY26-0001",
            date="2026-01-10",
            fiscal_year="2026",
            type="expense",
            amount="not-a-number",
            category="supplies",
        ),
    ]
    with pytest.raises(ValueError):
        analytics.build_frame(rows, start_month=_START_MONTH)
