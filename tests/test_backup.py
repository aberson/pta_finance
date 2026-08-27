"""Tests for safe-CSV/exact-JSON snapshots of the live tab set against a mocked client."""

from __future__ import annotations

import csv
import hashlib
import json
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from gspread.exceptions import WorksheetNotFound

from pta_finance import backup, report_source, schema


def _entered(kind: str, value: object) -> dict[str, object]:
    return {"userEnteredValue": {kind: value}}


def _empty() -> dict[str, object]:
    return {"userEnteredValue": None}


def _tag_cell(value: object) -> dict[str, object]:
    if isinstance(value, dict) and "userEnteredValue" in value:
        return value
    if value is None:
        return _empty()
    if isinstance(value, bool):
        return _entered("boolValue", value)
    if isinstance(value, (int, float)):
        return _entered("numberValue", value)
    if isinstance(value, str) and value.startswith("="):
        return _entered("formulaValue", value)
    return _entered("stringValue", str(value))


def _tag_grid(grid: list[list[object]]) -> list[list[dict[str, object]]]:
    return [[_tag_cell(cell) for cell in row] for row in grid]


def _exact_artifact(grid: list[list[dict[str, object]]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "value_model": "google-sheets-userEnteredValue",
        "grid": grid,
    }


class FakeReadClient:
    """A minimal stand-in exposing the exact-grid read that snapshot_all_tabs uses.

    Builds canned exact per-tab grids so no live Google call is made. A tab listed in
    ``missing`` raises :class:`gspread.exceptions.WorksheetNotFound`, simulating a tab the
    operator has deleted from the spreadsheet.
    """

    def __init__(
        self,
        records_by_tab: Mapping[str, list[dict[str, object]]],
        *,
        missing: set[str] | None = None,
    ) -> None:
        self._records = dict(records_by_tab)
        self._missing = set(missing or set())
        self.read_tabs: list[str] = []

    def read_tab(self, tab: str) -> list[dict[str, object]]:
        if tab in self._missing:
            raise WorksheetNotFound(tab)
        self.read_tabs.append(tab)
        return self._records.get(tab, [])

    def read_snapshot_values(self, tab: str) -> list[list[dict[str, object]]]:
        if tab in self._missing:
            raise WorksheetNotFound(tab)
        self.read_tabs.append(tab)
        records = self._records.get(tab, [])
        if tab in schema.TABS:
            columns = list(schema.TABS[tab])
        elif tab == report_source.BUDGET_TIMESERIES_TAB:
            columns = list(report_source.TIMESERIES_COLUMNS)
        else:
            columns = list(records[0]) if records else []
        rows = [[record.get(column, "") for column in columns] for record in records]
        return _tag_grid([columns, *rows])


class FakeRawClient:
    """A raw-grid reader for the lossless + spreadsheet-safe snapshot boundary."""

    def __init__(
        self,
        grid: list[list[object]],
        *,
        formatted_grid: list[list[str]] | None = None,
    ) -> None:
        self._grid = _tag_grid(grid)
        self._formatted_grid = formatted_grid or [[str(cell) for cell in row] for row in grid]
        self.read_tabs: list[str] = []

    def read_values(self, tab: str) -> list[list[str]]:
        self.read_tabs.append(tab)
        return [list(row) for row in self._formatted_grid]

    def read_snapshot_values(self, tab: str) -> list[list[dict[str, object]]]:
        self.read_tabs.append(tab)
        return [list(row) for row in self._grid]


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


def test_snapshot_all_tabs_keeps_signed_numbers_safe_and_writes_exact_sidecars(
    tmp_path: Path,
) -> None:
    txn_record: dict[str, object] = {column: "" for column in schema.TRANSACTIONS_COLUMNS}
    txn_record.update({"id": "TXN-EXAMPLE-1", "amount": -12.5, "payee": "=FORMULA"})
    timeseries_record: dict[str, object] = {
        column: "" for column in report_source.TIMESERIES_COLUMNS
    }
    timeseries_record.update(
        {
            report_source.FISCAL_YEAR: 2030,
            report_source.AMOUNT: 20.5,
            report_source.RAW_CATEGORY: "  @SUM(A1:A2)",
        }
    )
    client = FakeReadClient(
        {
            schema.TAB_TRANSACTIONS: [txn_record],
            report_source.BUDGET_TIMESERIES_TAB: [timeseries_record],
        }
    )

    snapshot_dir = backup.snapshot_all_tabs(
        client,
        tmp_path,
        timestamp="2030-09-01T160000Z",
        tabs=[schema.TAB_TRANSACTIONS, report_source.BUDGET_TIMESERIES_TAB],
    )

    txn_csv = snapshot_dir / f"{schema.TAB_TRANSACTIONS}.csv"
    with txn_csv.open(encoding="utf-8", newline="") as handle:
        txn_rows = list(csv.DictReader(handle))
    assert txn_rows[0]["amount"] == "-12.5"
    assert txn_rows[0]["payee"] == "'=FORMULA"
    txn_grid: list[list[object]] = [
        list(schema.TRANSACTIONS_COLUMNS),
        [txn_record[column] for column in schema.TRANSACTIONS_COLUMNS],
    ]
    assert json.loads(txn_csv.with_suffix(".raw.json").read_text(encoding="utf-8")) == (
        _exact_artifact(_tag_grid(txn_grid))
    )

    timeseries_csv = snapshot_dir / f"{report_source.BUDGET_TIMESERIES_TAB}.csv"
    with timeseries_csv.open(encoding="utf-8", newline="") as handle:
        timeseries_rows = list(csv.DictReader(handle))
    assert timeseries_rows[0][report_source.FISCAL_YEAR] == "2030"
    assert timeseries_rows[0][report_source.AMOUNT] == "20.5"
    assert timeseries_rows[0][report_source.RAW_CATEGORY] == "'  @SUM(A1:A2)"
    timeseries_grid: list[list[object]] = [
        list(report_source.TIMESERIES_COLUMNS),
        [timeseries_record[column] for column in report_source.TIMESERIES_COLUMNS],
    ]
    assert json.loads(timeseries_csv.with_suffix(".raw.json").read_text(encoding="utf-8")) == (
        _exact_artifact(_tag_grid(timeseries_grid))
    )


def test_snapshot_raw_tab_writes_safe_csv_and_exact_lossless_json(tmp_path: Path) -> None:
    grid = [
        ["plain", "equals", "plus", "minus", "at", "signed", "boolean", "date"],
        [
            "unchanged",
            "=1+1",
            "  +1+1",
            "\t-command",
            "  @SUM(A1:A2)",
            -12.5,
            True,
            47485,
        ],
    ]
    formatted_grid = [
        list(grid[0]),
        [
            "unchanged",
            "2",
            "  +1+1",
            "\t-command",
            "  @SUM(A1:A2)",
            "$12.50",
            "TRUE",
            "1/2/2030",
        ],
    ]
    client = FakeRawClient(grid, formatted_grid=formatted_grid)

    csv_path = backup.snapshot_raw_tab(
        client,
        "Example Raw",
        tmp_path,
        timestamp="2030-09-01T120000Z",
    )

    expected_path = tmp_path / "snapshots" / "2030-09-01T120000Z" / "Example Raw.csv"
    assert csv_path == expected_path
    assert client.read_tabs == ["Example Raw"]
    with csv_path.open(encoding="utf-8", newline="") as handle:
        safe_grid = list(csv.reader(handle))
    assert safe_grid == [
        grid[0],
        [
            "unchanged",
            "'=1+1",
            "'  +1+1",
            "'\t-command",
            "'  @SUM(A1:A2)",
            "-12.5",
            "True",
            "47485",
        ],
    ]
    lossless_path = csv_path.with_suffix(".raw.json")
    assert json.loads(lossless_path.read_text(encoding="utf-8")) == _exact_artifact(_tag_grid(grid))


def test_snapshot_raw_tab_keeps_artifacts_inside_snapshot_directory(tmp_path: Path) -> None:
    client = FakeRawClient([["header"], ["value"]])

    csv_path = backup.snapshot_raw_tab(
        client,
        "../escape",
        tmp_path,
        timestamp="2030-09-01T170000Z",
    )

    snapshot_dir = (tmp_path / "snapshots" / "2030-09-01T170000Z").resolve()
    assert csv_path.resolve().parent == snapshot_dir
    assert csv_path.with_suffix(".raw.json").resolve().parent == snapshot_dir


def test_snapshot_all_tabs_sanitized_name_cannot_overwrite_literal_safe_name(
    tmp_path: Path,
) -> None:
    unsafe_tab = ".hidden"
    old_sanitized_name = f"hidden-{hashlib.sha256(unsafe_tab.encode('utf-8')).hexdigest()[:12]}"
    client = FakeReadClient(
        {
            unsafe_tab: [{"header": "unsafe value"}],
            old_sanitized_name: [{"header": "safe value"}],
        }
    )

    snapshot_dir = backup.snapshot_all_tabs(
        client,
        tmp_path,
        timestamp="2030-09-01T180000Z",
        tabs=[unsafe_tab, old_sanitized_name],
    )

    csv_paths = list(snapshot_dir.glob("*.csv"))
    exact_grids = {
        json.dumps(json.loads(path.read_text(encoding="utf-8"))["grid"], sort_keys=True)
        for path in snapshot_dir.glob("*.raw.json")
    }
    assert len(csv_paths) == 2
    assert exact_grids == {
        json.dumps(_tag_grid([["header"], ["unsafe value"]]), sort_keys=True),
        json.dumps(_tag_grid([["header"], ["safe value"]]), sort_keys=True),
    }


def test_formula_safe_encoding_is_injective_and_reversible_for_apostrophe_prefixes() -> None:
    raw_values = ["'" * count + "=1+1" for count in range(9)]

    encoded = [backup.encode_formula_safe_text(value) for value in raw_values]

    assert len(set(encoded)) == len(raw_values)
    assert encoded == ["'" + value for value in raw_values]
    assert [backup.decode_formula_safe_text(value) for value in encoded] == raw_values


def test_snapshot_raw_tab_writes_versioned_tagged_exact_artifact(tmp_path: Path) -> None:
    tagged_grid = [
        [_entered("stringValue", "kind"), _entered("stringValue", "value")],
        [_entered("formulaValue", "=1+1"), _entered("stringValue", "=1+1")],
        [_entered("numberValue", -12.5), _entered("boolValue", True)],
        [_entered("stringValue", "-12.5"), _entered("numberValue", -12.5)],
        [_empty(), _entered("stringValue", "")],
    ]
    client = FakeRawClient(tagged_grid)

    csv_path = backup.snapshot_raw_tab(
        client,
        "Example Raw",
        tmp_path,
        timestamp="2030-09-01T190000Z",
    )

    exact = json.loads(csv_path.with_suffix(".raw.json").read_text(encoding="utf-8"))
    assert exact == {
        "schema_version": 1,
        "value_model": "google-sheets-userEnteredValue",
        "grid": tagged_grid,
    }
    with csv_path.open(encoding="utf-8", newline="") as handle:
        assert list(csv.reader(handle)) == [
            ["kind", "value"],
            ["'=1+1", "'=1+1"],
            ["-12.5", "True"],
            ["'-12.5", "-12.5"],
            ["", ""],
        ]


def test_snapshot_directory_claim_is_unique_for_same_timestamp(tmp_path: Path) -> None:
    first = backup.snapshot_raw_tab(
        FakeRawClient([["header"], ["first"]]),
        "Example Raw",
        tmp_path,
        timestamp="2030-09-01T200000Z",
    )
    second = backup.snapshot_raw_tab(
        FakeRawClient([["header"], ["second"]]),
        "Example Raw",
        tmp_path,
        timestamp="2030-09-01T200000Z",
    )

    assert first.parent.name == "2030-09-01T200000Z"
    assert second.parent.name == "2030-09-01T200000Z-2"
    assert "first" in first.read_text(encoding="utf-8")
    assert "second" in second.read_text(encoding="utf-8")
    first_exact = json.loads(first.with_suffix(".raw.json").read_text(encoding="utf-8"))
    second_exact = json.loads(second.with_suffix(".raw.json").read_text(encoding="utf-8"))
    assert first_exact["grid"][1][0]["userEnteredValue"] == {"stringValue": "first"}
    assert second_exact["grid"][1][0]["userEnteredValue"] == {"stringValue": "second"}


def test_snapshot_directory_claim_is_atomic_for_concurrent_runs(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)

    class ConcurrentClient(FakeRawClient):
        def read_snapshot_values(self, tab: str) -> list[list[dict[str, object]]]:
            barrier.wait()
            return super().read_snapshot_values(tab)

    def _snapshot(value: str) -> Path:
        return backup.snapshot_raw_tab(
            ConcurrentClient([["header"], [value]]),
            "Example Raw",
            tmp_path,
            timestamp="2030-09-01T210000Z",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(_snapshot, ["first", "second"]))

    assert {path.parent.name for path in paths} == {
        "2030-09-01T210000Z",
        "2030-09-01T210000Z-2",
    }
    assert {path.read_text(encoding="utf-8") for path in paths} == {
        "header\nfirst\n",
        "header\nsecond\n",
    }
    exact_values = {
        json.loads(path.with_suffix(".raw.json").read_text(encoding="utf-8"))["grid"][1][0][
            "userEnteredValue"
        ]["stringValue"]
        for path in paths
    }
    assert exact_values == {"first", "second"}


def test_snapshot_all_tabs_rejects_filesystem_equivalent_artifact_names(tmp_path: Path) -> None:
    client = FakeReadClient({"Example": [{"header": "first"}], "example": [{"header": "second"}]})

    with pytest.raises(ValueError, match="filesystem-equivalent snapshot artifact"):
        backup.snapshot_all_tabs(
            client,
            tmp_path,
            timestamp="2030-09-01T220000Z",
            tabs=["Example", "example"],
        )

    assert client.read_tabs == []
    assert not (tmp_path / "snapshots").exists()


def test_snapshot_sanitizes_windows_reserved_stem_with_trailing_space(tmp_path: Path) -> None:
    csv_path = backup.snapshot_raw_tab(
        FakeRawClient([["header"], ["value"]]),
        "CON .txt",
        tmp_path,
        timestamp="2030-09-01T230000Z",
    )

    assert csv_path.name.startswith("~")
    assert csv_path.is_file()
