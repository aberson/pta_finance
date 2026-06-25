"""Tests for pta_finance.budget_import — pure parsing of a messy budget worksheet.

The fixture grid mirrors the SHAPE of a real treasurer's budget tab (header not in row 1,
section dividers, subtotals, totals, carry-over / CD-account rollups, currency formatting,
a junk cell) but every line item is an obvious FAKE placeholder — no real org identity.
"""

from __future__ import annotations

from datetime import date

import pytest

from pta_finance import ids, schema
from pta_finance.budget_import import BudgetImportPlan, plan_budget_import

_FY = 2026
_ACTUAL_DATE = date(2026, 12, 31)


def _grid() -> list[list[str]]:
    """A raw budget grid: leading junk rows, a header below row 1, then messy data."""
    return [
        # Two rows above the header (a title + a blank spacer) — must be skipped.
        ["Example PTA Budget FY2026", "", "", "", "", ""],
        ["", "", "", "", "", ""],
        # Header row — note the TRAILING SPACE on "Actual " (must be stripped).
        ["Type", "Line Item", "Proposed", "Actual ", "Adjustment", "Difference"],
        # --- Income section ---
        ["Income", "Membership Dues", "1500", "1450", "", ""],
        # --- Expense section (explicit type sets the running section) ---
        ["Expense", "Classroom Supplies", "$2,000.00", "-$1,234.56", "", ""],
        # Blank-Type REAL expense line: inherits type=expense, flagged needs_review.
        # Zero actual -> no summary txn (but still a budget line).
        ["", "Field Trips", "800", "0.00", "", ""],
        # Blank-Type REAL expense line with a real actual: inherits expense, flagged,
        # and its summary txn is flagged needs_review too.
        ["", "Library Books", "300", "275", "", ""],
        # Subtotal divider: blank Type + blank Line Item -> skipped (blank category).
        ["", "", "3100", "", "", ""],
        # Total rollup -> skipped (summary filter: contains "total").
        ["", "Total Expense", "3100", "1959.56", "", ""],
        # Carry-over rollup -> skipped (summary filter).
        ["", "Carry over from prior year", "500", "500", "", ""],
        # CD-account rollup -> skipped (summary filter).
        ["", "CD Account Interest", "25", "25", "", ""],
        # Junk budget cell: a stray "." that is not a parseable amount -> skipped (blank).
        ["Expense", "Mystery Row", ".", "", "", ""],
    ]


def _plan(*, with_actuals: bool = False) -> BudgetImportPlan:
    return plan_budget_import(_grid(), fy=_FY, with_actuals=with_actuals, actual_date=_ACTUAL_DATE)


def test_budget_rows_ids_amounts_and_notes() -> None:
    plan = _plan()
    rows = plan.budget_rows

    # Exactly the four REAL line items survive (income + 3 expenses).
    assert set(rows) == {
        ids.budget_id(_FY, "Membership Dues"),
        ids.budget_id(_FY, "Classroom Supplies"),
        ids.budget_id(_FY, "Field Trips"),
        ids.budget_id(_FY, "Library Books"),
    }

    # Every row is in canonical BUDGET_COLUMNS shape.
    for row in rows.values():
        assert set(row) == set(schema.BUDGET_COLUMNS)
        assert row["fiscal_year"] == str(_FY)
        assert row["grade"] == ""

    membership = rows[ids.budget_id(_FY, "Membership Dues")]
    assert membership["category"] == "Membership Dues"
    assert membership["budgeted_amount"] == "1500"
    # Explicit "Income" type -> notes is the plain section, NOT flagged.
    assert membership["notes"] == "income"

    supplies = rows[ids.budget_id(_FY, "Classroom Supplies")]
    # "$2,000.00" parses through the currency-tolerant parser.
    assert supplies["budgeted_amount"] == "2000.00"
    assert supplies["notes"] == "expense"

    # The blank-Type line inherits the running expense section AND is flagged for review.
    field_trips = rows[ids.budget_id(_FY, "Field Trips")]
    assert "expense" in field_trips["notes"]
    assert "inferred" in field_trips["notes"]


def test_skip_and_review_counts() -> None:
    plan = _plan()
    # Subtotal (blank category) + junk "." budget cell -> 2 blanks.
    assert plan.skipped_blank == 2
    # Total + Carry over + CD Account -> 3 summary rollups.
    assert plan.skipped_summary == 3
    # Field Trips + Library Books inherited their type -> 2 budget lines need review.
    assert plan.needs_review == 2
    assert plan.duplicate_ids == 0


def test_with_actuals_builds_summary_transactions() -> None:
    plan = _plan(with_actuals=True)
    txns = plan.txn_rows

    # Membership (1450), Classroom Supplies (-1234.56), Library Books (275) get a txn.
    # Field Trips has a 0.00 actual -> NO txn.
    assert set(txns) == {
        ids.summary_txn_id(_FY, "Membership Dues"),
        ids.summary_txn_id(_FY, "Classroom Supplies"),
        ids.summary_txn_id(_FY, "Library Books"),
    }
    assert ids.summary_txn_id(_FY, "Field Trips") not in txns

    for row in txns.values():
        assert set(row) == set(schema.TRANSACTIONS_COLUMNS)
        assert row["source"] == "import"
        assert row["date"] == "2026-12-31"
        assert row["created_at"] == ""  # deterministic — never datetime.now

    membership = txns[ids.summary_txn_id(_FY, "Membership Dues")]
    assert membership["type"] == "income"
    assert membership["amount"] == "1450"
    # payee == category keeps the dedup key unique per line item.
    assert membership["payee"] == "Membership Dues"
    # The txn links back to its budget line.
    assert membership["budget_id"] == ids.budget_id(_FY, "Membership Dues")
    assert membership["needs_review"] == "FALSE"  # explicit type, not inferred

    supplies = txns[ids.summary_txn_id(_FY, "Classroom Supplies")]
    assert supplies["type"] == "expense"
    assert supplies["amount"] == "-1234.56"  # negative actual preserved

    # The inherited-type line's summary txn is flagged needs_review.
    library = txns[ids.summary_txn_id(_FY, "Library Books")]
    assert library["type"] == "expense"
    assert library["needs_review"] == "TRUE"


def test_without_actuals_emits_no_transactions() -> None:
    plan = _plan(with_actuals=False)
    assert plan.txn_rows == {}


def test_duplicate_category_slug_keeps_first_and_counts() -> None:
    grid = [
        ["Type", "Line Item", "Proposed", "Actual "],
        ["Expense", "Field Trips", "100", "90"],
        # Slugifies to the same id ("field-trips") -> dropped from output, counted once.
        ["Expense", "field trips!", "200", "180"],
    ]
    plan = plan_budget_import(grid, fy=_FY, with_actuals=True, actual_date=_ACTUAL_DATE)

    dup_id = ids.budget_id(_FY, "Field Trips")
    # Only ONE budget row kept (the first), one summary txn (the first).
    assert set(plan.budget_rows) == {dup_id}
    assert plan.budget_rows[dup_id]["budgeted_amount"] == "100"
    assert "DUPLICATE" in plan.budget_rows[dup_id]["notes"]
    assert plan.duplicate_ids == 1
    assert set(plan.txn_rows) == {ids.summary_txn_id(_FY, "Field Trips")}
    assert plan.txn_rows[ids.summary_txn_id(_FY, "Field Trips")]["amount"] == "90"


def test_inferred_type_duplicate_does_not_inflate_needs_review() -> None:
    """A dropped duplicate whose type would be INFERRED must NOT bump needs_review — the
    duplicate detection runs before the type-inference/note block (stats reflect kept rows)."""
    grid = [
        ["Type", "Line Item", "Proposed", "Actual "],
        # Explicit-type first occurrence: kept, NOT flagged.
        ["Expense", "Field Trips", "100", "90"],
        # Blank-Type duplicate (same slug): its type WOULD be inferred from the section, but
        # it is dropped as a duplicate BEFORE any needs_review increment.
        ["", "field trips!", "200", "180"],
    ]
    plan = plan_budget_import(grid, fy=_FY, with_actuals=True, actual_date=_ACTUAL_DATE)

    assert plan.duplicate_ids == 1
    # Only the kept (explicit-type) row exists; nothing was flagged for review.
    assert plan.needs_review == 0
    kept = plan.budget_rows[ids.budget_id(_FY, "Field Trips")]
    assert kept["notes"].startswith("expense")  # explicit type, not "inferred"


def test_actual_before_any_section_cannot_be_signed() -> None:
    """An actual with no preceding income/expense section can't be signed -> no txn, flagged."""
    grid = [
        ["Type", "Line Item", "Proposed", "Actual "],
        # No section row yet; this line's type is unknown.
        ["", "Orphan Line", "100", "90"],
    ]
    plan = plan_budget_import(grid, fy=_FY, with_actuals=True, actual_date=_ACTUAL_DATE)

    bid = ids.budget_id(_FY, "Orphan Line")
    # Budget line still created, with an "unknown" note, and flagged.
    assert bid in plan.budget_rows
    assert "unknown" in plan.budget_rows[bid]["notes"]
    # No txn (can't sign it); needs_review counts the budget line AND the un-signable actual.
    assert plan.txn_rows == {}
    assert plan.needs_review == 2


def test_alternate_header_aliases_parse_and_find_actual_via_spent() -> None:
    """A header using ["Category","Budget","Spent"] aliases still parses, and the actual
    column is found via the "spent" alias (no explicit Type column -> type stays unknown)."""
    grid = [
        ["Category", "Budget", "Spent"],
        ["Field Trips", "100", "90"],
    ]
    plan = plan_budget_import(grid, fy=_FY, with_actuals=True, actual_date=_ACTUAL_DATE)

    bid = ids.budget_id(_FY, "Field Trips")
    assert set(plan.budget_rows) == {bid}
    assert plan.budget_rows[bid]["budgeted_amount"] == "100"
    # No Type column at all -> the line's type is unknown, so the actual can't be signed.
    assert plan.txn_rows == {}
    assert "unknown" in plan.budget_rows[bid]["notes"]


def test_with_actuals_but_no_actual_column_emits_no_transactions() -> None:
    """with_actuals=True but the header has NO actual/spent column -> txn_rows == {} while
    budget rows are still produced."""
    grid = [
        ["Type", "Line Item", "Proposed"],
        ["Income", "Membership Dues", "1500"],
        ["Expense", "Classroom Supplies", "2000"],
    ]
    plan = plan_budget_import(grid, fy=_FY, with_actuals=True, actual_date=_ACTUAL_DATE)

    assert set(plan.budget_rows) == {
        ids.budget_id(_FY, "Membership Dues"),
        ids.budget_id(_FY, "Classroom Supplies"),
    }
    assert plan.txn_rows == {}


def test_missing_header_raises() -> None:
    grid = [
        ["just", "some", "rows"],
        ["with", "no", "header"],
    ]
    with pytest.raises(ValueError, match="no header row found"):
        plan_budget_import(grid, fy=_FY, with_actuals=False, actual_date=_ACTUAL_DATE)
