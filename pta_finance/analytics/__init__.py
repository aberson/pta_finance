"""Analytics engine over the canonical ``transactions`` ledger (pandas, no Google I/O).

The package separates **pure aggregation** (unit-testable from a list of row dicts or a
DataFrame, with NO network/disk I/O) from the CLI wiring in :mod:`pta_finance.cli`:

* :func:`build_frame` turns a list of ``transactions`` row dicts into a typed
  :class:`pandas.DataFrame`. It parses ``date`` (datetime), ``amount`` (exact integer
  **cents** — never a binary float), and ``fiscal_year`` (int), and it **excludes every
  row flagged** ``needs_review == "TRUE"`` from the frame — malformed and dedup-flagged
  rows are pending operator review and must never be summed (double-counting a duplicate is
  a correctness bug). The count of excluded rows is reported on
  :attr:`BuiltFrame.excluded_needs_review`.
* :mod:`~pta_finance.analytics.aggregate` and :mod:`~pta_finance.analytics.trends` are pure
  functions over the frame built above.

Money discipline (load-bearing): all monetary aggregation happens in **integer cents** so
``0.10 + 0.20`` sums to exactly ``0.30`` — pandas never sees a dollar ``float``. Dollar
:class:`~decimal.Decimal` values appear only at the boundary, when a result is returned.

Columns come from :mod:`pta_finance.schema`; parsing from :mod:`pta_finance.models`. Neither
is re-implemented here.
"""

from __future__ import annotations

from pta_finance.analytics.aggregate import (
    AMOUNT_CENTS,
    BuiltFrame,
    budget_vs_actual,
    build_frame,
    by_category,
    by_grade,
    by_month,
    totals,
)
from pta_finance.analytics.trends import (
    fundraising_and_spend_by_year,
    year_over_year,
)

__all__ = [
    "AMOUNT_CENTS",
    "BuiltFrame",
    "build_frame",
    "totals",
    "by_category",
    "by_grade",
    "by_month",
    "budget_vs_actual",
    "fundraising_and_spend_by_year",
    "year_over_year",
]
