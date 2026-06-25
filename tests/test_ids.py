"""Tests for pta_finance.ids — exact ID formats + fiscal-year logic."""

from __future__ import annotations

from datetime import date

from pta_finance.etl import _TXN_ID_RE
from pta_finance.ids import (
    budget_id,
    event_id,
    fiscal_year_label,
    receipt_id,
    slugify,
    summary_txn_id,
    txn_id,
)


def test_txn_id_exact() -> None:
    assert txn_id(2026, 1) == "TXN-FY26-0001"
    assert txn_id(2026, 42) == "TXN-FY26-0042"
    assert txn_id(2025, 1234) == "TXN-FY25-1234"


def test_summary_txn_id_exact() -> None:
    assert summary_txn_id(2026, "Fall Festival") == "TXN-FY26-SUM-fall-festival"
    assert summary_txn_id(2025, "Supplies") == "TXN-FY25-SUM-supplies"
    # Grade suffix mirrors budget_id's -g{slug} shape.
    assert summary_txn_id(2026, "Field Trips", grade="3") == "TXN-FY26-SUM-field-trips-g3"
    assert summary_txn_id(2026, "Field Trips", grade="K") == "TXN-FY26-SUM-field-trips-gk"
    # Empty/None grade collapses to the plain form.
    assert summary_txn_id(2026, "Supplies", grade=None) == "TXN-FY26-SUM-supplies"
    assert summary_txn_id(2026, "Supplies", grade="") == "TXN-FY26-SUM-supplies"


def test_summary_txn_id_not_matched_by_etl_txn_id_regex() -> None:
    """Cross-consumer safety: a summary id must NOT match etl's canonical-id regex, so it
    can never seed/perturb normalize's per-FY sequence counter (code-quality rule: grep
    all downstream consumers of an id shape)."""
    # The canonical-id regex DOES match a real txn id...
    assert _TXN_ID_RE.match(txn_id(2026, 1)) is not None
    # ...but never a summary id (its body is -SUM-{slug}, not a \d{4,} sequence).
    assert _TXN_ID_RE.match(summary_txn_id(2026, "Fall Festival")) is None
    assert _TXN_ID_RE.match(summary_txn_id(2026, "Supplies", grade="3")) is None


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
