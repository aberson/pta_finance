"""Tests for pta_finance.backup — CSV snapshot of the live tab set against a mocked client."""

from __future__ import annotations

import csv
from collections.abc import Mapping
from pathlib import Path

from gspread.exceptions import WorksheetNotFound

from pta_finance import backup, report_source, schema


class FakeReadClient:
    """A minimal stand-in exposing only ``read_tab`` — what snapshot_all_tabs uses.

    Returns canned per-tab records so no live Google call is made. A tab listed in
    ``missing`` raises :class:`gspread.exceptions.WorksheetNotFound`, simulating a tab the
    operator has deleted from the spreadsheet.
    """

    def __init__(
        self,
        records_by_tab: Mapping[str, list[dict[str, str]]],
        *,
        missing: set[str] | None = None,
    ) -> None:
        self._records = dict(records_by_tab)
        self._missing = set(missing or set())
        self.read_tabs: list[str] = []

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        if tab in self._missing:
            raise WorksheetNotFound(tab)
        self.read_tabs.append(tab)
        return self._records.get(tab, [])


def test_snapshot_default_set_writes_report_log_and_timeseries(tmp_path: Path) -> None:
    """The default snapshot set is the live tabs: report_log + the Budget Timeseries source."""
    log_record = {col: "" for col in schema.REPORT_LOG_COLUMNS}
    log_record["run_at"] = "2026-06-23T12:00:00Z"
    log_record["variant"] = "internal"
    client = FakeReadClient({schema.TAB_REPORT_LOG: [log_record]})

    snapshot_dir = backup.snapshot_all_tabs(client, tmp_path, timestamp="2026-06-23T120000Z")

    assert snapshot_dir == tmp_path / "snapshots" / "2026-06-23T120000Z"
    assert snapshot_dir.is_dir()

    # Exactly the live set was read + written (NOT all 5 canonical tabs).
    assert set(client.read_tabs) == set(backup.LIVE_SNAPSHOT_TABS)
    written = {p.stem for p in snapshot_dir.glob("*.csv")}
    assert written == set(backup.LIVE_SNAPSHOT_TABS)

    # report_log CSV: canonical schema header + the one data row in column order.
    with (snapshot_dir / f"{schema.TAB_REPORT_LOG}.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == list(schema.REPORT_LOG_COLUMNS)
    assert len(rows) == 2
    run_at_index = list(schema.REPORT_LOG_COLUMNS).index("run_at")
    assert rows[1][run_at_index] == "2026-06-23T12:00:00Z"

    # The Budget Timeseries CSV carries the expected header even when empty.
    ts_csv = snapshot_dir / f"{report_source.BUDGET_TIMESERIES_TAB}.csv"
    with ts_csv.open(encoding="utf-8", newline="") as fh:
        ts_rows = list(csv.reader(fh))
    assert ts_rows[0] == list(report_source.TIMESERIES_COLUMNS)


def test_snapshot_skips_missing_tab(tmp_path: Path) -> None:
    """A tab the spreadsheet doesn't have is skipped (no crash, no CSV) — the deletion case."""
    # The operator deleted the canonical tabs; report_log is present but the Budget Timeseries
    # tab has not been created yet.
    client = FakeReadClient({}, missing={report_source.BUDGET_TIMESERIES_TAB})

    snapshot_dir = backup.snapshot_all_tabs(client, tmp_path, timestamp="2026-06-23T130000Z")

    written = {p.stem for p in snapshot_dir.glob("*.csv")}
    assert written == {schema.TAB_REPORT_LOG}  # the missing tab produced no CSV
    assert report_source.BUDGET_TIMESERIES_TAB not in client.read_tabs


def test_snapshot_legacy_tabs_arg_writes_all_canonical(tmp_path: Path) -> None:
    """Legacy callers pass tabs=schema.TABS to back up every canonical tab before mutating."""
    txn_record = {col: "" for col in schema.TRANSACTIONS_COLUMNS}
    txn_record["id"] = "TXN-FY26-0001"
    txn_record["payee"] = "Example Vendor"
    client = FakeReadClient({schema.TAB_TRANSACTIONS: [txn_record]})

    snapshot_dir = backup.snapshot_all_tabs(
        client, tmp_path, timestamp="2026-06-23T140000Z", tabs=schema.TABS
    )

    # Every canonical tab got read + a CSV with the schema header.
    assert set(client.read_tabs) == set(schema.TABS)
    for tab, columns in schema.TABS.items():
        with (snapshot_dir / f"{tab}.csv").open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        assert rows[0] == list(columns)

    # The transactions CSV carries the one data row in column order.
    with (snapshot_dir / f"{schema.TAB_TRANSACTIONS}.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert len(rows) == 2  # header + one record
    id_index = list(schema.TRANSACTIONS_COLUMNS).index("id")
    payee_index = list(schema.TRANSACTIONS_COLUMNS).index("payee")
    assert rows[1][id_index] == "TXN-FY26-0001"
    assert rows[1][payee_index] == "Example Vendor"


def test_snapshot_empty_tabs_writes_header_only(tmp_path: Path) -> None:
    client = FakeReadClient({})  # all tabs empty
    snapshot_dir = backup.snapshot_all_tabs(
        client, tmp_path, timestamp="2026-06-23T150000Z", tabs=schema.TABS
    )

    for tab, columns in schema.TABS.items():
        with (snapshot_dir / f"{tab}.csv").open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        assert rows == [list(columns)]  # header only, no data rows
