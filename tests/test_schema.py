"""Tests for pta_finance.schema — exact column lists + `is`-identity registry guard.

The identity assertions (``is``, not ``==``) are load-bearing: they ensure the
:data:`schema.TABS` registry references the SAME tuple objects as the named column
constants. If someone later re-lists a tab's columns inline in the registry, the
tuples would still be equal but would no longer be identical — and these tests fail.
That is the workspace ``code-quality`` rule ("one source of truth for data-shape
constants; assert ``is`` not ``==``") encoded as CI.
"""

from __future__ import annotations

from pta_finance import schema

# Expected column lists, copied verbatim from plan.md §12 Appendix.
_EXPECTED_TRANSACTIONS = (
    "id",
    "date",
    "fiscal_year",
    "type",
    "amount",
    "category",
    "grade",
    "payee",
    "memo",
    "budget_id",
    "receipt_id",
    "source",
    "entered_by",
    "created_at",
    "needs_review",
)
_EXPECTED_RECEIPTS = (
    "id",
    "txn_id",
    "drive_url",
    "description",
    "amount",
    "date",
    "added_by",
    "created_at",
)
_EXPECTED_BUDGET = (
    "id",
    "fiscal_year",
    "category",
    "grade",
    "budgeted_amount",
    "notes",
)
_EXPECTED_EVENTS = (
    "id",
    "fiscal_year",
    "name",
    "date",
    "type",
    "expected_income",
    "expected_expense",
    "nag_schedule",
    "notes",
)
_EXPECTED_REPORT_LOG = (
    "run_at",
    "variant",
    "month",
    "output_url",
    "generated_by",
)


def test_tab_names() -> None:
    assert schema.TAB_TRANSACTIONS == "transactions"
    assert schema.TAB_RECEIPTS == "receipts"
    assert schema.TAB_BUDGET == "budget"
    assert schema.TAB_EVENTS == "events"
    assert schema.TAB_REPORT_LOG == "report_log"


def test_transactions_columns_exact() -> None:
    assert schema.TRANSACTIONS_COLUMNS == _EXPECTED_TRANSACTIONS


def test_receipts_columns_exact() -> None:
    assert schema.RECEIPTS_COLUMNS == _EXPECTED_RECEIPTS


def test_budget_columns_exact() -> None:
    assert schema.BUDGET_COLUMNS == _EXPECTED_BUDGET


def test_events_columns_exact() -> None:
    assert schema.EVENTS_COLUMNS == _EXPECTED_EVENTS


def test_report_log_columns_exact() -> None:
    assert schema.REPORT_LOG_COLUMNS == _EXPECTED_REPORT_LOG


def test_registry_keys_are_all_tabs() -> None:
    assert set(schema.TABS) == {
        schema.TAB_TRANSACTIONS,
        schema.TAB_RECEIPTS,
        schema.TAB_BUDGET,
        schema.TAB_EVENTS,
        schema.TAB_REPORT_LOG,
    }


def test_registry_references_same_tuple_objects_by_identity() -> None:
    # `is`, not `==`: guards against a future inline re-duplication of columns.
    assert schema.TABS[schema.TAB_TRANSACTIONS] is schema.TRANSACTIONS_COLUMNS
    assert schema.TABS[schema.TAB_RECEIPTS] is schema.RECEIPTS_COLUMNS
    assert schema.TABS[schema.TAB_BUDGET] is schema.BUDGET_COLUMNS
    assert schema.TABS[schema.TAB_EVENTS] is schema.EVENTS_COLUMNS
    assert schema.TABS[schema.TAB_REPORT_LOG] is schema.REPORT_LOG_COLUMNS


def test_required_tabs_is_a_subset_of_the_registry() -> None:
    # REQUIRED_TABS is the live-provisioned subset; every entry must still be a known tab in
    # the full TABS registry (the column-shape source of truth).
    assert set(schema.REQUIRED_TABS) <= set(schema.TABS)


def test_report_log_is_required() -> None:
    # report_log is the one tab the live toolkit always writes (one row per report run).
    assert schema.TAB_REPORT_LOG in schema.REQUIRED_TABS
