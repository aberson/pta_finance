"""Tests for pta_finance.backup — CSV snapshot of all tabs against a mocked client."""

from __future__ import annotations

import csv
from collections.abc import Mapping
from pathlib import Path

from pta_finance import backup, schema


class FakeReadClient:
    """A minimal stand-in exposing only ``read_tab`` — what snapshot_all_tabs uses.

    Returns canned per-tab records so no live Google call is made.
    """

    def __init__(self, records_by_tab: Mapping[str, list[dict[str, str]]]) -> None:
        self._records = dict(records_by_tab)
        self.read_tabs: list[str] = []

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        self.read_tabs.append(tab)
        return self._records.get(tab, [])


def test_snapshot_all_tabs_writes_one_csv_per_tab(tmp_path: Path) -> None:
    txn_record = {col: "" for col in schema.TRANSACTIONS_COLUMNS}
    txn_record["id"] = "TXN-FY26-0001"
    txn_record["payee"] = "Example Vendor"
    client = FakeReadClient({schema.TAB_TRANSACTIONS: [txn_record]})

    snapshot_dir = backup.snapshot_all_tabs(client, tmp_path, timestamp="2026-06-23T120000Z")

    # The returned dir is under tmp_path/snapshots/<timestamp>/ and exists.
    assert snapshot_dir == tmp_path / "snapshots" / "2026-06-23T120000Z"
    assert snapshot_dir.is_dir()

    # Every tab got read, and a CSV per tab was written with the schema header.
    assert set(client.read_tabs) == set(schema.TABS)
    for tab, columns in schema.TABS.items():
        csv_path = snapshot_dir / f"{tab}.csv"
        assert csv_path.is_file()
        with csv_path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        assert rows[0] == list(columns)  # header == schema columns

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
    snapshot_dir = backup.snapshot_all_tabs(client, tmp_path, timestamp="2026-06-23T130000Z")

    for tab, columns in schema.TABS.items():
        with (snapshot_dir / f"{tab}.csv").open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        assert rows == [list(columns)]  # header only, no data rows
