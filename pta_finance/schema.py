"""Canonical tab names + ordered column lists — the single source of truth for data shape.

Every producer (ETL writer) and consumer (analytics, reports, sheets I/O) imports the
column lists from here. Re-listing a tab's columns anywhere else risks silent shape drift;
don't. Regression tests in ``tests/test_schema.py`` assert that the :data:`TABS` registry
references these exact tuple objects with ``is`` (not ``==``), so a future re-duplication
fails CI (workspace ``code-quality`` rule: one source of truth for data-shape constants).

Column lists are verbatim from plan.md §12 Appendix.
"""

from __future__ import annotations

__all__ = [
    "TAB_TRANSACTIONS",
    "TAB_RECEIPTS",
    "TAB_BUDGET",
    "TAB_EVENTS",
    "TAB_REPORT_LOG",
    "TRANSACTIONS_COLUMNS",
    "RECEIPTS_COLUMNS",
    "BUDGET_COLUMNS",
    "EVENTS_COLUMNS",
    "REPORT_LOG_COLUMNS",
    "TABS",
]

# --- Tab (worksheet) names -------------------------------------------------

TAB_TRANSACTIONS = "transactions"
TAB_RECEIPTS = "receipts"
TAB_BUDGET = "budget"
TAB_EVENTS = "events"
TAB_REPORT_LOG = "report_log"

# --- Ordered column lists (single source of truth) -------------------------

TRANSACTIONS_COLUMNS: tuple[str, ...] = (
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

RECEIPTS_COLUMNS: tuple[str, ...] = (
    "id",
    "txn_id",
    "drive_url",
    "description",
    "amount",
    "date",
    "added_by",
    "created_at",
)

BUDGET_COLUMNS: tuple[str, ...] = (
    "id",
    "fiscal_year",
    "category",
    "grade",
    "budgeted_amount",
    "notes",
)

EVENTS_COLUMNS: tuple[str, ...] = (
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

REPORT_LOG_COLUMNS: tuple[str, ...] = (
    "run_at",
    "variant",
    "month",
    "output_url",
    "generated_by",
)

# --- Tab registry ----------------------------------------------------------
#
# Maps each tab name to its column tuple. The values MUST be the SAME tuple
# objects defined above (by identity) — never re-list columns inline here.
# Tests assert ``TABS[TAB_X] is X_COLUMNS`` for every tab so a stray inline
# duplicate fails CI.
TABS: dict[str, tuple[str, ...]] = {
    TAB_TRANSACTIONS: TRANSACTIONS_COLUMNS,
    TAB_RECEIPTS: RECEIPTS_COLUMNS,
    TAB_BUDGET: BUDGET_COLUMNS,
    TAB_EVENTS: EVENTS_COLUMNS,
    TAB_REPORT_LOG: REPORT_LOG_COLUMNS,
}
