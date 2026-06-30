"""Adapter: source ``report`` / ``analyze`` from the "Budget Timeseries" long dataset.

The toolkit's reporting + analytics now read from a single operator-maintained worksheet,
the **"Budget Timeseries"** tab — a tidy *long* dataset that is the operator's single
source of truth — instead of joining the canonical ``transactions`` + ``budget`` tabs. The
canonical tabs are still defined (``schema.py``) and still written by ``normalize`` /
``import-budget``; they simply are not what ``report`` / ``analyze`` read anymore.

The "Budget Timeseries" tab is one row per ``(fiscal_year, line, type, measure)``:

* ``fiscal_year`` — integer FY label (as text).
* ``category_group`` — a coarse grouping (carried through, not used for joins here).
* ``type`` — ``"income"`` or ``"expense"``.
* ``measure`` — ``"proposed"`` (the budget) or ``"actual"`` (realized spend / income).
* ``amount`` — a number-as-string.
* ``is_fundraiser`` / ``grade`` / ``raw_category`` / ``source_tab`` — line attributes.
  ``raw_category`` is the *specific* line name and is used verbatim as the canonical
  ``category`` so per-line categories (e.g. ``"Walk-A-Thon Income"`` vs
  ``"Walk-A-Thon Expenses"``) stay distinct for budget-vs-actual.

Actuals exist only for the in-progress fiscal year; other years are proposed-only.

This module is a thin reader (:func:`read_timeseries`, the only Google I/O here) plus a
PURE adapter (:func:`to_inputs`) that projects the long rows onto the EXACT
``schema.BUDGET_COLUMNS`` / ``schema.TRANSACTIONS_COLUMNS`` row-dict shapes the existing
analytics + report builders already consume. Ids come from :mod:`pta_finance.ids` (never
re-derived); columns from :mod:`pta_finance.schema`.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from datetime import date
from typing import TYPE_CHECKING

from pta_finance import ids, schema

if TYPE_CHECKING:
    from pta_finance.sheets import SheetsClient

__all__ = [
    "BUDGET_TIMESERIES_TAB",
    "FISCAL_YEAR",
    "CATEGORY_GROUP",
    "TYPE",
    "MEASURE",
    "AMOUNT",
    "IS_FUNDRAISER",
    "GRADE",
    "RAW_CATEGORY",
    "SOURCE_TAB",
    "MEASURE_PROPOSED",
    "MEASURE_ACTUAL",
    "TIMESERIES_COLUMNS",
    "read_timeseries",
    "to_inputs",
]

# The single operator-maintained worksheet the toolkit now reports/analyzes from.
BUDGET_TIMESERIES_TAB = "Budget Timeseries"

# Column names in the "Budget Timeseries" tab (exact, header-row keyed).
FISCAL_YEAR = "fiscal_year"
CATEGORY_GROUP = "category_group"
TYPE = "type"
MEASURE = "measure"
AMOUNT = "amount"
IS_FUNDRAISER = "is_fundraiser"
GRADE = "grade"
RAW_CATEGORY = "raw_category"
SOURCE_TAB = "source_tab"

# The two values the ``measure`` column carries.
MEASURE_PROPOSED = "proposed"
MEASURE_ACTUAL = "actual"

# The header columns the "Budget Timeseries" tab is expected to carry (the live data source
# the toolkit reports/analyzes from). Used by ``check`` to confirm the source is readable AND
# correctly shaped before a run; ``read_timeseries`` keys row dicts by the sheet's real header,
# so these are the names downstream projection (:func:`to_inputs`) looks up.
TIMESERIES_COLUMNS: tuple[str, ...] = (
    FISCAL_YEAR,
    CATEGORY_GROUP,
    TYPE,
    MEASURE,
    AMOUNT,
    IS_FUNDRAISER,
    GRADE,
    RAW_CATEGORY,
    SOURCE_TAB,
)


def read_timeseries(client: SheetsClient) -> list[dict[str, str]]:
    """Read the "Budget Timeseries" tab into a list of header-keyed row dicts.

    Reads the raw grid via :meth:`SheetsClient.read_values` (row 0 is the header), coerces
    every cell to ``str``, and returns one dict per data row keyed by the header. Rows that
    are entirely blank are skipped. The only Google I/O in this module.
    """
    grid = client.read_values(BUDGET_TIMESERIES_TAB)
    if not grid:
        return []
    header = [str(cell).strip() for cell in grid[0]]
    out: list[dict[str, str]] = []
    for raw_row in grid[1:]:
        cells = [str(cell) for cell in raw_row]
        if all(cell.strip() == "" for cell in cells):
            continue
        row = {col: (cells[i] if i < len(cells) else "") for i, col in enumerate(header)}
        out.append(row)
    return out


def _fy_end_iso(fy: int, start_month: int) -> str:
    """Last calendar day of fiscal year ``fy``, as an ISO ``YYYY-MM-DD`` string.

    For a calendar fiscal year (``start_month == 1``) this is December 31 of ``fy``.
    Otherwise the year ends in month ``start_month - 1`` of calendar year ``fy``; the last
    day of that month is found via :func:`calendar.monthrange`. Asserts that the computed
    date round-trips to ``fy`` under :func:`pta_finance.ids.fiscal_year_label` so an
    off-by-one in the start-month arithmetic is caught at the source.
    """
    if start_month == 1:
        end = date(fy, 12, 31)
    else:
        end_month = start_month - 1
        last_day = calendar.monthrange(fy, end_month)[1]
        end = date(fy, end_month, last_day)
    assert ids.fiscal_year_label(end, start_month) == fy
    return end.isoformat()


def _cell(row: Mapping[str, str], key: str) -> str:
    """Value at ``key`` in ``row``, stripped; missing/short -> ``""`` (robust to junk)."""
    value = row.get(key)
    if value is None:
        return ""
    return str(value).strip()


def to_inputs(
    rows: Iterable[Mapping[str, str]],
    *,
    start_month: int,
    fy: int | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Project "Budget Timeseries" long rows onto canonical budget + transaction row dicts.

    PURE — no I/O. Returns ``(budget_rows, txn_rows)`` where:

    * ``budget_rows`` is one dict per ``measure == "proposed"`` row, keyed/shaped to
      :data:`schema.BUDGET_COLUMNS`. ``raw_category`` is used verbatim as ``category`` so
      per-line categories stay distinct; ``id`` is :func:`pta_finance.ids.budget_id`
      (dedup by id, last wins).
    * ``txn_rows`` is one dict per ``measure == "actual"`` row, keyed/shaped to
      :data:`schema.TRANSACTIONS_COLUMNS`, stamped with the fiscal-year-end date
      (:func:`_fy_end_iso`) and id :func:`pta_finance.ids.summary_txn_id` (dedup, last wins).

    When ``fy`` is given, only rows whose ``fiscal_year`` equals it are projected. A row with
    a blank/non-numeric ``fiscal_year`` is skipped (it cannot be placed in a fiscal year).
    """
    budget_by_id: dict[str, dict[str, str]] = {}
    txn_by_id: dict[str, dict[str, str]] = {}

    for row in rows:
        fy_text = _cell(row, FISCAL_YEAR)
        try:
            row_fy = int(fy_text)
        except ValueError:
            continue
        if fy is not None and row_fy != fy:
            continue

        measure = _cell(row, MEASURE).casefold()
        raw_category = _cell(row, RAW_CATEGORY)
        grade = _cell(row, GRADE)
        amount = _cell(row, AMOUNT)
        row_type = _cell(row, TYPE)

        # ``grade`` is carried into the row but is INTENTIONALLY NOT part of the id / dedup
        # key here: in the timeseries source a line's ``grade`` is a SET (e.g. "TK,K,1,2,3")
        # that would make an ugly id and is not the join key — budget-vs-actual joins on
        # ``category`` (per_grade=False). Ids key on (fiscal_year, raw_category) only.
        if measure == MEASURE_PROPOSED:
            budget_identifier = ids.budget_id(row_fy, raw_category)
            budget_by_id[budget_identifier] = {
                "id": budget_identifier,
                "fiscal_year": str(row_fy),
                "category": raw_category,
                "grade": grade,
                "budgeted_amount": amount,
                "notes": row_type,
            }
        elif measure == MEASURE_ACTUAL:
            txn_identifier = ids.summary_txn_id(row_fy, raw_category)
            txn_by_id[txn_identifier] = {
                "id": txn_identifier,
                "date": _fy_end_iso(row_fy, start_month),
                "fiscal_year": str(row_fy),
                "type": row_type,
                "amount": amount,
                "category": raw_category,
                "grade": grade,
                "payee": raw_category,
                "memo": "FY summary (from Budget Timeseries)",
                "budget_id": "",
                "receipt_id": "",
                "source": "timeseries",
                "entered_by": "",
                "created_at": "",
                "needs_review": "",
            }

    return list(budget_by_id.values()), list(txn_by_id.values())


# Sanity check at import time: the projected dicts must use EXACTLY the canonical column
# sets, so a schema rename surfaces as an ImportError here rather than a silent all-empty
# frame downstream. Build one of each shape from a probe row and assert key identity.
def _check_projection_shapes() -> None:
    probe = {
        FISCAL_YEAR: "2026",
        TYPE: "income",
        MEASURE: MEASURE_PROPOSED,
        AMOUNT: "1",
        RAW_CATEGORY: "probe",
    }
    budget_rows, _ = to_inputs([probe], start_month=1, fy=2026)
    actual_probe = {**probe, MEASURE: MEASURE_ACTUAL}
    _, txn_rows = to_inputs([actual_probe], start_month=1, fy=2026)
    if set(budget_rows[0]) != set(schema.BUDGET_COLUMNS):
        raise AssertionError(
            f"budget projection keys {sorted(budget_rows[0])} != "
            f"schema.BUDGET_COLUMNS {sorted(schema.BUDGET_COLUMNS)}"
        )
    if set(txn_rows[0]) != set(schema.TRANSACTIONS_COLUMNS):
        raise AssertionError(
            f"txn projection keys {sorted(txn_rows[0])} != "
            f"schema.TRANSACTIONS_COLUMNS {sorted(schema.TRANSACTIONS_COLUMNS)}"
        )


_check_projection_shapes()
