"""Tests for pta_finance.ids — exact ID formats + fiscal-year logic."""

from __future__ import annotations

from datetime import date

from pta_finance.ids import (
    budget_id,
    event_id,
    fiscal_year_label,
    receipt_id,
    slugify,
    txn_id,
)


def test_txn_id_exact() -> None:
    assert txn_id(2026, 1) == "TXN-FY26-0001"
    assert txn_id(2026, 42) == "TXN-FY26-0042"
    assert txn_id(2025, 1234) == "TXN-FY25-1234"


def test_receipt_id_exact() -> None:
    assert receipt_id(2026, 1) == "RCP-FY26-0001"
    assert receipt_id(2024, 7) == "RCP-FY24-0007"


def test_event_id_exact() -> None:
    assert event_id(2026, "Fall Festival") == "EVT-FY26-fall-festival"


def test_fiscal_year_label_calendar() -> None:
    # start_month == 1 -> the label is just the calendar year.
    assert fiscal_year_label(date(2026, 1, 1), start_month=1) == 2026
    assert fiscal_year_label(date(2026, 12, 31), start_month=1) == 2026
    assert fiscal_year_label(date(2026, 6, 15)) == 2026  # default start_month=1


def test_fiscal_year_label_july_start() -> None:
    # July-start FY is labeled by its ENDING calendar year.
    # Jul 2025 .. Jun 2026 => FY 2026.
    assert fiscal_year_label(date(2025, 7, 1), start_month=7) == 2026
    assert fiscal_year_label(date(2025, 12, 31), start_month=7) == 2026
    assert fiscal_year_label(date(2026, 1, 1), start_month=7) == 2026
    assert fiscal_year_label(date(2026, 6, 30), start_month=7) == 2026
    # The next span starts Jul 2026 => FY 2027.
    assert fiscal_year_label(date(2026, 7, 1), start_month=7) == 2027


def test_slugify() -> None:
    assert slugify("Fall Festival") == "fall-festival"
    assert slugify("Supplies & Materials!") == "supplies-materials"
    assert slugify("  Trim  Me  ") == "trim-me"
    assert slugify("Already-kebab") == "already-kebab"


def test_budget_id_plain_and_grade() -> None:
    assert budget_id(2026, "Supplies") == "BUD-FY26-supplies"
    assert budget_id(2026, "Supplies", grade="3") == "BUD-FY26-supplies-g3"
    assert budget_id(2026, "Field Trips", grade="K") == "BUD-FY26-field-trips-gk"
    # Empty/None grade collapses to the plain form.
    assert budget_id(2026, "Supplies", grade=None) == "BUD-FY26-supplies"
    assert budget_id(2026, "Supplies", grade="") == "BUD-FY26-supplies"
