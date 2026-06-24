"""Tests for pta_finance.models — scalar parsers + row (de)serialization round-trips."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from pta_finance import schema
from pta_finance.models import (
    BudgetLine,
    Event,
    Receipt,
    Transaction,
    format_amount,
    parse_amount,
    parse_bool,
    parse_date,
    parse_optional_amount,
    parse_optional_date,
)

# --- parse_amount ----------------------------------------------------------


def test_parse_amount_plain_string() -> None:
    assert parse_amount("1234.56") == Decimal("1234.56")


def test_parse_amount_thousands_separator() -> None:
    assert parse_amount("1,234.56") == Decimal("1234.56")


def test_parse_amount_currency_symbol() -> None:
    assert parse_amount("$1,234.56") == Decimal("1234.56")


def test_parse_amount_int() -> None:
    assert parse_amount(1234) == Decimal("1234")


def test_parse_amount_float_no_binary_artifact() -> None:
    # Routed through str() so we get Decimal("0.1"), not the binary tail.
    assert parse_amount(0.1) == Decimal("0.1")


def test_parse_amount_decimal_passthrough() -> None:
    d = Decimal("99.99")
    assert parse_amount(d) == d


def test_parse_amount_parenthesised_negative() -> None:
    assert parse_amount("(1,234.56)") == Decimal("-1234.56")


def test_parse_amount_garbage_raises() -> None:
    with pytest.raises(ValueError):
        parse_amount("not money")


def test_parse_amount_empty_raises() -> None:
    with pytest.raises(ValueError):
        parse_amount("")


def test_parse_amount_bool_raises() -> None:
    # bool is an int subclass; must not silently coerce to 0/1.
    with pytest.raises(ValueError):
        parse_amount(True)


def test_parse_optional_amount_empty_is_none() -> None:
    assert parse_optional_amount("") is None
    assert parse_optional_amount("   ") is None
    assert parse_optional_amount(None) is None


def test_parse_optional_amount_value() -> None:
    assert parse_optional_amount("$50") == Decimal("50")


# --- parse_date ------------------------------------------------------------


def test_parse_date_iso() -> None:
    assert parse_date("2026-06-23") == date(2026, 6, 23)


def test_parse_date_passthrough() -> None:
    d = date(2026, 1, 1)
    assert parse_date(d) is d


def test_parse_date_garbage_raises() -> None:
    with pytest.raises(ValueError):
        parse_date("23/06/2026")


def test_parse_date_empty_raises() -> None:
    with pytest.raises(ValueError):
        parse_date("")


def test_parse_optional_date_empty_is_none() -> None:
    assert parse_optional_date("") is None
    assert parse_optional_date(None) is None


def test_parse_optional_date_value() -> None:
    assert parse_optional_date("2026-06-23") == date(2026, 6, 23)


# --- parse_bool ------------------------------------------------------------


def test_parse_bool_truthy() -> None:
    assert parse_bool("TRUE") is True
    assert parse_bool("true") is True
    assert parse_bool("1") is True
    assert parse_bool("yes") is True
    assert parse_bool(True) is True


def test_parse_bool_falsy() -> None:
    assert parse_bool("FALSE") is False
    assert parse_bool("") is False
    assert parse_bool(None) is False
    assert parse_bool("no") is False
    assert parse_bool("anything-else") is False


# --- format_amount ---------------------------------------------------------


def test_format_amount_plain() -> None:
    assert format_amount(Decimal("1234.56")) == "1234.56"
    # No scientific notation for large or small magnitudes.
    assert "E" not in format_amount(Decimal("1000000"))
    assert format_amount(Decimal("-50")) == "-50"


# --- Transaction round-trip ------------------------------------------------


def _txn_row() -> dict[str, str]:
    return {
        "id": "TXN-FY26-0001",
        "date": "2026-06-15",
        "fiscal_year": "2026",
        "type": "expense",
        "amount": "1,234.56",
        "category": "Supplies",
        "grade": "3",
        "payee": "Example Vendor",
        "memo": "classroom supplies",
        "budget_id": "BUD-FY26-supplies-g3",
        "receipt_id": "RCP-FY26-0001",
        "source": "manual",
        "entered_by": "treasurer@example.org",
        "created_at": "2026-06-15T10:00:00Z",
        "needs_review": "FALSE",
    }


def test_transaction_from_row_types() -> None:
    txn = Transaction.from_row(_txn_row())
    assert txn.id == "TXN-FY26-0001"
    assert txn.date == date(2026, 6, 15)
    assert txn.amount == Decimal("1234.56")
    assert txn.grade == "3"
    assert txn.needs_review is False


def test_transaction_round_trip() -> None:
    row = _txn_row()
    txn = Transaction.from_row(row)
    out = txn.to_row()
    # to_row emits in schema order and normalizes the amount (no thousands sep).
    assert list(out.keys()) == list(schema.TRANSACTIONS_COLUMNS)
    assert out["amount"] == "1234.56"
    assert out["date"] == "2026-06-15"
    assert out["needs_review"] == "FALSE"
    # Re-parse round-trips to an equal entity.
    assert Transaction.from_row(out) == txn


def test_transaction_optional_fields_empty_to_none() -> None:
    row = _txn_row()
    row["grade"] = ""
    row["budget_id"] = ""
    row["receipt_id"] = ""
    row["entered_by"] = ""
    txn = Transaction.from_row(row)
    assert txn.grade is None
    assert txn.budget_id is None
    assert txn.receipt_id is None
    assert txn.entered_by is None
    # And they serialize back to empty strings.
    out = txn.to_row()
    assert out["grade"] == ""
    assert out["budget_id"] == ""
    assert out["entered_by"] == ""


def test_transaction_needs_review_true() -> None:
    row = _txn_row()
    row["needs_review"] = "TRUE"
    txn = Transaction.from_row(row)
    assert txn.needs_review is True
    assert txn.to_row()["needs_review"] == "TRUE"


# --- Receipt round-trip ----------------------------------------------------


def test_receipt_round_trip_full() -> None:
    row = {
        "id": "RCP-FY26-0001",
        "txn_id": "TXN-FY26-0001",
        "drive_url": "https://drive.example.org/file/abc",
        "description": "office store receipt",
        "amount": "$50.00",
        "date": "2026-06-15",
        "added_by": "treasurer@example.org",
        "created_at": "2026-06-15T10:00:00Z",
    }
    rcp = Receipt.from_row(row)
    assert rcp.amount == Decimal("50.00")
    assert rcp.date == date(2026, 6, 15)
    out = rcp.to_row()
    assert list(out.keys()) == list(schema.RECEIPTS_COLUMNS)
    assert Receipt.from_row(out) == rcp


def test_receipt_optional_amount_and_date_empty() -> None:
    row = {
        "id": "RCP-FY26-0002",
        "txn_id": "TXN-FY26-0002",
        "drive_url": "https://drive.example.org/file/def",
        "description": "",
        "amount": "",
        "date": "",
        "added_by": "",
        "created_at": "2026-06-16T10:00:00Z",
    }
    rcp = Receipt.from_row(row)
    assert rcp.amount is None
    assert rcp.date is None
    assert rcp.description is None
    assert rcp.added_by is None
    out = rcp.to_row()
    assert out["amount"] == ""
    assert out["date"] == ""


# --- BudgetLine round-trip -------------------------------------------------


def test_budget_round_trip() -> None:
    row = {
        "id": "BUD-FY26-supplies-g3",
        "fiscal_year": "2026",
        "category": "Supplies",
        "grade": "3",
        "budgeted_amount": "2,000.00",
        "notes": "per-grade supplies",
    }
    bud = BudgetLine.from_row(row)
    assert bud.budgeted_amount == Decimal("2000.00")
    assert bud.grade == "3"
    out = bud.to_row()
    assert list(out.keys()) == list(schema.BUDGET_COLUMNS)
    assert out["budgeted_amount"] == "2000.00"
    assert BudgetLine.from_row(out) == bud


def test_budget_no_grade_no_notes() -> None:
    row = {
        "id": "BUD-FY26-events",
        "fiscal_year": "2026",
        "category": "Events",
        "grade": "",
        "budgeted_amount": "5000",
        "notes": "",
    }
    bud = BudgetLine.from_row(row)
    assert bud.grade is None
    assert bud.notes is None


# --- Event round-trip ------------------------------------------------------


def test_event_round_trip() -> None:
    row = {
        "id": "EVT-FY26-fall-festival",
        "fiscal_year": "2026",
        "name": "Fall Festival",
        "date": "2026-10-01",
        "type": "fundraiser",
        "expected_income": "3,000.00",
        "expected_expense": "500.00",
        "nag_schedule": "weekly",
        "notes": "annual event",
    }
    evt = Event.from_row(row)
    assert evt.date == date(2026, 10, 1)
    assert evt.expected_income == Decimal("3000.00")
    assert evt.expected_expense == Decimal("500.00")
    out = evt.to_row()
    assert list(out.keys()) == list(schema.EVENTS_COLUMNS)
    assert Event.from_row(out) == evt


def test_event_optional_money_empty_to_none() -> None:
    row = {
        "id": "EVT-FY26-board-meeting",
        "fiscal_year": "2026",
        "name": "Board Meeting",
        "date": "2026-09-01",
        "type": "meeting",
        "expected_income": "",
        "expected_expense": "",
        "nag_schedule": "",
        "notes": "",
    }
    evt = Event.from_row(row)
    assert evt.expected_income is None
    assert evt.expected_expense is None
    assert evt.nag_schedule is None
    out = evt.to_row()
    assert out["expected_income"] == ""
    assert out["expected_expense"] == ""


# --- Schema alignment ------------------------------------------------------


def test_dataclass_fields_match_schema_columns() -> None:
    # Mirror the import-time guard so a drift is also a visible test failure.
    from dataclasses import fields

    assert tuple(f.name for f in fields(Transaction)) == schema.TRANSACTIONS_COLUMNS
    assert tuple(f.name for f in fields(Receipt)) == schema.RECEIPTS_COLUMNS
    assert tuple(f.name for f in fields(BudgetLine)) == schema.BUDGET_COLUMNS
    assert tuple(f.name for f in fields(Event)) == schema.EVENTS_COLUMNS
