"""Tests for pta_finance.report_source — the "Budget Timeseries" -> canonical-row adapter.

No live Google calls: :func:`to_inputs` and :func:`_fy_end_iso` are pure, and the
:func:`read_timeseries` reader is exercised against a tiny fake client whose ``read_values``
returns a hand-built grid. The fixtures cover income + expense, proposed + actual, an
in-progress FY2026 with actuals plus a proposed-only FY2025, and :func:`_fy_end_iso` across
fiscal-year start months.
"""

from __future__ import annotations

import pytest

from pta_finance import ids, report_source, schema

_FY = report_source.FISCAL_YEAR
_TYPE = report_source.TYPE
_MEASURE = report_source.MEASURE
_AMOUNT = report_source.AMOUNT
_GRADE = report_source.GRADE
_RAW = report_source.RAW_CATEGORY


def _row(**ov: str) -> dict[str, str]:
    """A "Budget Timeseries" row with every column present (blank unless overridden)."""
    base = {
        report_source.FISCAL_YEAR: "",
        report_source.CATEGORY_GROUP: "",
        report_source.TYPE: "",
        report_source.MEASURE: "",
        report_source.AMOUNT: "",
        report_source.IS_FUNDRAISER: "",
        report_source.GRADE: "",
        report_source.RAW_CATEGORY: "",
        report_source.SOURCE_TAB: "",
    }
    base.update(ov)
    return base


def _timeseries() -> list[dict[str, str]]:
    """A tidy long fixture: FY2026 has proposed + actual; FY2025 is proposed-only."""
    return [
        # FY2026 — income line, proposed + actual.
        _row(
            **{
                _FY: "2026",
                _TYPE: "income",
                _MEASURE: "proposed",
                _AMOUNT: "1000.00",
                _RAW: "Walk-A-Thon Income",
            }
        ),
        _row(
            **{
                _FY: "2026",
                _TYPE: "income",
                _MEASURE: "actual",
                _AMOUNT: "950.00",
                _RAW: "Walk-A-Thon Income",
            }
        ),
        # FY2026 — expense line, proposed + actual, with a grade.
        _row(
            **{
                _FY: "2026",
                _TYPE: "expense",
                _MEASURE: "proposed",
                _AMOUNT: "200.00",
                _GRADE: "3",
                _RAW: "Classroom Supplies",
            }
        ),
        _row(
            **{
                _FY: "2026",
                _TYPE: "expense",
                _MEASURE: "actual",
                _AMOUNT: "120.00",
                _GRADE: "3",
                _RAW: "Classroom Supplies",
            }
        ),
        # FY2025 — proposed only (no actuals for a closed/future year).
        _row(
            **{
                _FY: "2025",
                _TYPE: "income",
                _MEASURE: "proposed",
                _AMOUNT: "800.00",
                _RAW: "Walk-A-Thon Income",
            }
        ),
    ]


def test_to_inputs_projects_budget_rows_with_raw_category_and_id() -> None:
    """Proposed rows -> budget rows shaped to BUDGET_COLUMNS, raw_category as category."""
    budget_rows, _ = report_source.to_inputs(_timeseries(), start_month=1, fy=None)

    # Every projected row uses EXACTLY the canonical budget columns.
    for row in budget_rows:
        assert set(row) == set(schema.BUDGET_COLUMNS)

    by_id = {row["id"]: row for row in budget_rows}
    income_id = ids.budget_id(2026, "Walk-A-Thon Income")
    supplies_id = ids.budget_id(2026, "Classroom Supplies")
    fy25_id = ids.budget_id(2025, "Walk-A-Thon Income")
    assert {income_id, supplies_id, fy25_id} <= set(by_id)

    # raw_category lands verbatim in `category`; type lands in `notes`.
    assert by_id[income_id]["category"] == "Walk-A-Thon Income"
    assert by_id[income_id]["budgeted_amount"] == "1000.00"
    assert by_id[income_id]["notes"] == "income"
    assert by_id[supplies_id]["category"] == "Classroom Supplies"
    assert by_id[supplies_id]["grade"] == "3"


def test_to_inputs_projects_txn_rows_with_summary_id_and_fy_end_date() -> None:
    """Actual rows -> transaction rows shaped to TRANSACTIONS_COLUMNS, FY-end date + summary id."""
    _, txn_rows = report_source.to_inputs(_timeseries(), start_month=1, fy=None)

    for row in txn_rows:
        assert set(row) == set(schema.TRANSACTIONS_COLUMNS)

    by_id = {row["id"]: row for row in txn_rows}
    income_id = ids.summary_txn_id(2026, "Walk-A-Thon Income")
    supplies_id = ids.summary_txn_id(2026, "Classroom Supplies")
    assert {income_id, supplies_id} == set(by_id)  # only FY2026 has actuals

    inc = by_id[income_id]
    assert inc["category"] == "Walk-A-Thon Income"
    assert inc["payee"] == "Walk-A-Thon Income"  # raw_category, keeps dedup key distinct
    assert inc["type"] == "income"
    assert inc["amount"] == "950.00"
    assert inc["date"] == "2026-12-31"  # calendar FY end
    assert inc["fiscal_year"] == "2026"
    assert inc["source"] == "timeseries"
    assert inc["memo"] == "FY summary (from Budget Timeseries)"


def test_to_inputs_fy_filter_keeps_only_requested_year() -> None:
    """fy=2026 drops the FY2025 proposed line entirely."""
    budget_rows, txn_rows = report_source.to_inputs(_timeseries(), start_month=1, fy=2026)
    budget_ids = {row["id"] for row in budget_rows}
    assert ids.budget_id(2025, "Walk-A-Thon Income") not in budget_ids
    assert ids.budget_id(2026, "Walk-A-Thon Income") in budget_ids
    # All txn rows are FY2026 (FY2025 had no actuals anyway).
    assert all(row["fiscal_year"] == "2026" for row in txn_rows)


def test_to_inputs_dedup_last_wins_by_id() -> None:
    """Two proposed rows with the same (fy, raw_category) collapse to one (last wins)."""
    rows = [
        _row(**{_FY: "2026", _TYPE: "income", _MEASURE: "proposed", _AMOUNT: "10", _RAW: "Dues"}),
        _row(**{_FY: "2026", _TYPE: "income", _MEASURE: "proposed", _AMOUNT: "99", _RAW: "Dues"}),
    ]
    budget_rows, _ = report_source.to_inputs(rows, start_month=1, fy=None)
    assert len(budget_rows) == 1
    assert budget_rows[0]["budgeted_amount"] == "99"


def test_to_inputs_skips_blank_and_non_numeric_fiscal_year() -> None:
    """A row with a blank/non-numeric fiscal_year is unplaceable and is dropped."""
    rows = [
        _row(**{_FY: "", _TYPE: "income", _MEASURE: "proposed", _AMOUNT: "5", _RAW: "X"}),
        _row(**{_FY: "n/a", _TYPE: "income", _MEASURE: "proposed", _AMOUNT: "5", _RAW: "Y"}),
        _row(**{_FY: "2026", _TYPE: "income", _MEASURE: "proposed", _AMOUNT: "5", _RAW: "Z"}),
    ]
    budget_rows, _ = report_source.to_inputs(rows, start_month=1, fy=None)
    assert {row["category"] for row in budget_rows} == {"Z"}


def test_to_inputs_robust_to_missing_cells() -> None:
    """A short/partial row dict (missing keys) does not raise; absent cells read as blank."""
    rows = [{_FY: "2026", _MEASURE: "proposed", _AMOUNT: "5", _RAW: "Partial"}]
    budget_rows, _ = report_source.to_inputs(rows, start_month=1, fy=None)
    assert len(budget_rows) == 1
    assert budget_rows[0]["grade"] == ""
    assert budget_rows[0]["notes"] == ""  # type cell absent -> blank


@pytest.mark.parametrize(
    ("fy", "start_month", "expected"),
    [
        (2026, 8, "2026-07-31"),  # the live deployment path (Aug start)
        (2026, 1, "2026-12-31"),  # calendar fiscal year
        (2026, 7, "2026-06-30"),  # July start -> ends June 30
    ],
)
def test_fy_end_iso(fy: int, start_month: int, expected: str) -> None:
    """The FY-end date is the last calendar day of the fiscal year for the start month."""
    assert report_source._fy_end_iso(fy, start_month) == expected


# --- read_timeseries (the thin reader) -------------------------------------


class _FakeReadClient:
    """Minimal client whose ``read_values`` returns a canned grid for the timeseries tab."""

    def __init__(self, grid: list[list[str]]) -> None:
        self._grid = grid
        self.read_values_calls: list[str] = []

    def read_values(self, tab: str) -> list[list[str]]:
        self.read_values_calls.append(tab)
        if tab == report_source.BUDGET_TIMESERIES_TAB:
            return [list(row) for row in self._grid]
        return []


def test_read_timeseries_skips_blank_rows_and_pads_short_rows() -> None:
    """A fully-blank row is skipped; a short row's missing trailing cells read as ""."""
    grid = [
        [
            report_source.FISCAL_YEAR,
            report_source.TYPE,
            report_source.MEASURE,
            report_source.AMOUNT,
            report_source.RAW_CATEGORY,
            report_source.SOURCE_TAB,
        ],
        ["2026", "income", "proposed", "100", "Dues", "budget"],
        ["", "", "", "", "", ""],  # fully blank -> skipped
        ["2026", "expense", "actual", "50", "Supplies"],  # short: source_tab cell missing
    ]
    client = _FakeReadClient(grid)
    rows = report_source.read_timeseries(client)  # type: ignore[arg-type]

    assert client.read_values_calls == [report_source.BUDGET_TIMESERIES_TAB]
    # The blank row is gone; only the two real rows remain.
    assert len(rows) == 2
    # The short row's missing trailing cell reads as "".
    short = rows[1]
    assert short[report_source.RAW_CATEGORY] == "Supplies"
    assert short[report_source.SOURCE_TAB] == ""


def test_read_timeseries_empty_grid_returns_empty() -> None:
    """An empty worksheet (no rows at all) yields no records."""
    client = _FakeReadClient([])
    assert report_source.read_timeseries(client) == []  # type: ignore[arg-type]
