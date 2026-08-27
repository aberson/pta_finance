"""Tests for the wired CLI subcommands (check, init-sheet, snapshot) against a mocked SheetsClient.

No live Google calls: ``cli.SheetsClient`` is monkeypatched to a fake, and ``snapshot``
runs through the real ``backup.snapshot_all_tabs`` with a fake read client.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path

import pytest

from pta_finance import backup, cli, receipt_ingest, receipt_map, schema
from pta_finance.config import Config, ConfigError
from tests.conftest import tagged_user_entered_grid

_CONFIG_TEXT = """\
[organization]
name = "Example PTA"
school_name = "Example Elementary"
school_email = "office@example.org"

[contacts]
president = ["president@example.org"]
treasurer = "treasurer@example.org"
cfo = "cfo@example.org"
account_holders = ["president@example.org", "treasurer@example.org"]

[fiscal_year]
start_month = 1

[grades]
labels = ["K", "1", "2", "3", "4", "5"]

[sheets]
spreadsheet_id = "fake-spreadsheet-id"
test_spreadsheet_id = "fake-test-sheet-id"
drive_receipts_folder_id = "fake-receipts-folder-id"
drive_reports_folder_id = "fake-reports-folder-id"

[google]
service_account_file = "secrets/service-account.json"
"""


def _write_config(tmp_path: Path, text: str = _CONFIG_TEXT) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


class FakeCheckClient:
    """A fake SheetsClient for the check round-trip: validate + read source + upsert/read/delete.

    ``read_values`` serves the "Budget Timeseries" grid (so the source-readable check passes);
    the report_log round-trip is keyed by the row's ``run_at`` cell (column 1), mirroring how
    ``SheetsClient`` keys upsert/delete.
    """

    instances: list[FakeCheckClient] = []

    def __init__(
        self,
        config: Config,
        *,
        spreadsheet_id: str | None = None,
        **_: object,
    ) -> None:
        self.config = config
        self.spreadsheet_id = spreadsheet_id
        self.validated: list[str] = []
        self.upserts: list[tuple[str, Mapping[str, Mapping[str, str]]]] = []
        self.deletes: list[tuple[str, list[str]]] = []
        self.read_values_calls: list[str] = []
        self._store: dict[str, dict[str, str]] = {}
        FakeCheckClient.instances.append(self)

    def validate_schema(self, tab: str) -> None:
        self.validated.append(tab)

    def read_values(self, tab: str) -> list[list[str]]:
        from pta_finance import report_source

        self.read_values_calls.append(tab)
        if tab == report_source.BUDGET_TIMESERIES_TAB:
            return [list(r) for r in _TIMESERIES_GRID]
        return []

    def upsert_rows(self, tab: str, rows_by_id: Mapping[str, Mapping[str, str]]) -> None:
        self.upserts.append((tab, rows_by_id))
        for row_id, row in rows_by_id.items():
            self._store[row_id] = dict(row)

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        return list(self._store.values())

    def delete_rows_by_id(self, tab: str, ids: list[str]) -> None:
        self.deletes.append((tab, list(ids)))
        for row_id in ids:
            self._store.pop(row_id, None)


def test_check_validates_required_tabs_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pta_finance import report_source

    FakeCheckClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeCheckClient)
    config_path = _write_config(tmp_path)

    rc = cli.main(["check", "--config", str(config_path)])

    assert rc == 0
    # Two clients are built: one for prod schema validation + source read, one for the round-trip.
    assert len(FakeCheckClient.instances) == 2
    prod, test = FakeCheckClient.instances
    # Only the LIVE-required tabs are validated (not all 5 canonical tabs).
    assert prod.validated == list(schema.REQUIRED_TABS)
    assert schema.TAB_TRANSACTIONS not in prod.validated
    assert schema.TAB_BUDGET not in prod.validated
    # The Budget Timeseries source was read + confirmed readable.
    assert prod.read_values_calls == [report_source.BUDGET_TIMESERIES_TAB]
    # Round-trip on the test client targets report_log, keyed by the run_at marker.
    assert test.spreadsheet_id == "fake-test-sheet-id"
    assert len(test.upserts) == 1
    upsert_tab, rows_by_id = test.upserts[0]
    assert upsert_tab == schema.TAB_REPORT_LOG
    (marker,) = rows_by_id.keys()
    assert rows_by_id[marker]["run_at"] == marker
    assert test.deletes == [(schema.TAB_REPORT_LOG, [marker])]
    out = capsys.readouterr().out
    assert f"schema OK for {len(schema.REQUIRED_TABS)} required tab(s)" in out
    assert "Budget Timeseries source OK" in out
    assert "round-trip OK" in out


def test_check_fails_when_timeseries_source_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An empty Budget Timeseries source returns 1 and never builds the round-trip client."""

    class _EmptySourceCheckClient(FakeCheckClient):
        def read_values(self, tab: str) -> list[list[str]]:
            self.read_values_calls.append(tab)
            return []  # source missing/empty

    FakeCheckClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", _EmptySourceCheckClient)
    config_path = _write_config(tmp_path)

    rc = cli.main(["check", "--config", str(config_path)])

    assert rc == 1
    # Only the prod client was built — we never reached the round-trip step.
    assert len(FakeCheckClient.instances) == 1
    assert "missing or empty" in capsys.readouterr().out


def test_check_skips_round_trip_without_test_sheet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeCheckClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeCheckClient)
    text = _CONFIG_TEXT.replace(
        'test_spreadsheet_id = "fake-test-sheet-id"',
        'test_spreadsheet_id = "x"',
    )
    # Make it empty-ish: an empty string is rejected by config validation, so use a
    # config whose test id is whitespace-only is also rejected. Instead, monkeypatch the
    # loaded config to blank the test id after load.
    config_path = _write_config(tmp_path, text)

    real_load = cli.load_config

    def _load_blank(path: Path) -> Config:
        cfg = real_load(path)
        object.__setattr__(cfg.sheets, "test_spreadsheet_id", "")
        return cfg

    monkeypatch.setattr(cli, "load_config", _load_blank)

    rc = cli.main(["check", "--config", str(config_path)])

    assert rc == 0
    # Only the prod client was built (no round-trip client).
    assert len(FakeCheckClient.instances) == 1
    out = capsys.readouterr().out
    assert "skipping round-trip" in out


class FakeInitSheetClient:
    """A fake SheetsClient capturing the init-sheet bootstrap: list/ensure/header reads.

    ``existing`` maps tab name -> its current header row (a missing key = absent tab,
    an empty list = present-but-empty). ``ensure_tab`` records the tab and returns a
    status derived from that state; ``read_header`` serves the dry-run path.
    """

    instances: list[FakeInitSheetClient] = []
    existing: dict[str, list[str]] = {}

    def __init__(
        self,
        config: Config,
        *,
        spreadsheet_id: str | None = None,
        **_: object,
    ) -> None:
        self.config = config
        self.spreadsheet_id = spreadsheet_id
        self.ensured: list[str] = []
        self._state = {tab: list(hdr) for tab, hdr in FakeInitSheetClient.existing.items()}
        FakeInitSheetClient.instances.append(self)

    def list_worksheet_titles(self) -> list[str]:
        return list(self._state)

    def read_header(self, tab: str) -> list[str]:
        return list(self._state.get(tab, []))

    def ensure_tab(self, tab: str) -> str:
        self.ensured.append(tab)
        if tab not in self._state:
            self._state[tab] = list(schema.TABS[tab])
            return "created"
        if not self._state[tab]:
            self._state[tab] = list(schema.TABS[tab])
            return "headers-written"
        return "ok"


def test_init_sheet_creates_required_tabs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """init-sheet drives ensure_tab through the LIVE-required tab(s) via the production caller."""
    FakeInitSheetClient.instances = []
    FakeInitSheetClient.existing = {}  # empty spreadsheet — every required tab is created
    monkeypatch.setattr(cli, "SheetsClient", FakeInitSheetClient)
    config_path = _write_config(tmp_path)

    rc = cli.main(["init-sheet", "--config", str(config_path)])

    assert rc == 0
    (client,) = FakeInitSheetClient.instances
    assert client.spreadsheet_id is None  # default target = main
    # Only the live-required tab(s) were reached — the 4 unused canonical tabs are NOT created.
    assert client.ensured == list(schema.REQUIRED_TABS)
    assert schema.TAB_TRANSACTIONS not in client.ensured
    assert schema.TAB_BUDGET not in client.ensured
    out = capsys.readouterr().out
    for tab in schema.REQUIRED_TABS:
        assert f"init-sheet: {tab} -> created" in out
    assert f"{len(schema.REQUIRED_TABS)} created" in out


def test_init_sheet_dry_run_makes_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run reports the 'ok (no change)' action and never calls ensure_tab (no writes)."""
    FakeInitSheetClient.instances = []
    # report_log already correct -> "ok (no change)".
    FakeInitSheetClient.existing = {schema.TAB_REPORT_LOG: list(schema.REPORT_LOG_COLUMNS)}
    monkeypatch.setattr(cli, "SheetsClient", FakeInitSheetClient)
    config_path = _write_config(tmp_path)

    rc = cli.main(["init-sheet", "--config", str(config_path), "--dry-run"])

    assert rc == 0
    (client,) = FakeInitSheetClient.instances
    assert client.ensured == []  # NO writes
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert f"{schema.TAB_REPORT_LOG} -> ok (no change)" in out
    assert "no writes made" in out


def test_init_sheet_dry_run_would_create_absent_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run on an empty spreadsheet reports required tab(s) as 'would create', no writes."""
    FakeInitSheetClient.instances = []
    FakeInitSheetClient.existing = {}  # required tab absent
    monkeypatch.setattr(cli, "SheetsClient", FakeInitSheetClient)
    config_path = _write_config(tmp_path)

    rc = cli.main(["init-sheet", "--config", str(config_path), "--dry-run"])

    assert rc == 0
    (client,) = FakeInitSheetClient.instances
    assert client.ensured == []  # NO writes
    out = capsys.readouterr().out
    assert f"{schema.TAB_REPORT_LOG} -> would create" in out
    assert "no writes made" in out


def test_init_sheet_dry_run_reports_mismatch_without_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run on an existing tab with a non-empty mismatched header reports the mismatch
    branch and makes no writes (the previously-uncovered 'would write headers / mismatch' case)."""
    FakeInitSheetClient.instances = []
    # report_log exists with a non-empty WRONG header (not equal to the schema columns).
    bad_header = ["run_at", "WRONG", *list(schema.REPORT_LOG_COLUMNS[2:])]
    FakeInitSheetClient.existing = {schema.TAB_REPORT_LOG: bad_header}
    monkeypatch.setattr(cli, "SheetsClient", FakeInitSheetClient)
    config_path = _write_config(tmp_path)

    rc = cli.main(["init-sheet", "--config", str(config_path), "--dry-run"])

    assert rc == 0
    (client,) = FakeInitSheetClient.instances
    assert client.ensured == []  # NO writes
    out = capsys.readouterr().out
    assert f"{schema.TAB_REPORT_LOG} -> would write headers / mismatch" in out
    assert "no writes made" in out


def test_init_sheet_target_test_without_test_sheet_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--target test with an empty test_spreadsheet_id returns 1 before building a client."""
    FakeInitSheetClient.instances = []
    FakeInitSheetClient.existing = {}
    monkeypatch.setattr(cli, "SheetsClient", FakeInitSheetClient)
    config_path = _write_config(tmp_path)

    real_load = cli.load_config

    def _load_blank(path: Path) -> Config:
        cfg = real_load(path)
        object.__setattr__(cfg.sheets, "test_spreadsheet_id", "")
        return cfg

    monkeypatch.setattr(cli, "load_config", _load_blank)

    rc = cli.main(["init-sheet", "--config", str(config_path), "--target", "test"])

    assert rc == 1
    # No client was constructed for the missing test sheet.
    assert FakeInitSheetClient.instances == []
    assert "no test_spreadsheet_id configured" in capsys.readouterr().out


class FakeSnapshotClient:
    """A fake SheetsClient serving the live snapshot set: report_log + Budget Timeseries."""

    def __init__(self, config: Config, **_: object) -> None:
        self.config = config
        self.read_tabs: list[str] = []

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        self.read_tabs.append(tab)
        return []  # both live tabs present (no WorksheetNotFound), simply empty

    def read_snapshot_values(self, tab: str) -> list[list[dict[str, object]]]:
        self.read_tabs.append(tab)
        return []  # both live tabs present (no WorksheetNotFound), simply empty


def test_snapshot_writes_csvs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pta_finance import backup, report_source

    monkeypatch.setattr(cli, "SheetsClient", FakeSnapshotClient)
    config_path = _write_config(tmp_path)
    dest = tmp_path / "out"

    rc = cli.main(["snapshot", "--config", str(config_path), "--dest", str(dest)])

    assert rc == 0
    snapshot_root = dest / "snapshots"
    assert snapshot_root.is_dir()
    (run_dir,) = list(snapshot_root.iterdir())
    # The live snapshot set is report_log + the Budget Timeseries source.
    written = {p.stem for p in run_dir.glob("*.csv")}
    assert written == set(backup.LIVE_SNAPSHOT_TABS)
    # report_log's CSV carries its canonical schema header.
    with (run_dir / f"{schema.TAB_REPORT_LOG}.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == list(schema.REPORT_LOG_COLUMNS)
    # The Budget Timeseries CSV carries the expected timeseries header.
    ts_csv = run_dir / f"{report_source.BUDGET_TIMESERIES_TAB}.csv"
    with ts_csv.open(encoding="utf-8", newline="") as fh:
        ts_rows = list(csv.reader(fh))
    assert ts_rows[0] == list(report_source.TIMESERIES_COLUMNS)
    assert "snapshot: wrote 2 tab(s)" in capsys.readouterr().out


# A "Budget Timeseries" long-dataset grid (header row 0, then data): FY2026 fundraiser
# income (proposed + actual) and a graded supplies expense (proposed + actual).
_TIMESERIES_GRID = [
    [
        "fiscal_year",
        "category_group",
        "type",
        "measure",
        "amount",
        "is_fundraiser",
        "grade",
        "raw_category",
        "source_tab",
    ],
    ["2026", "fundraising", "income", "proposed", "1000.00", "TRUE", "", "fundraiser", "budget"],
    ["2026", "fundraising", "income", "actual", "500.00", "TRUE", "", "fundraiser", "actuals"],
    ["2026", "operations", "expense", "proposed", "200.00", "FALSE", "3", "supplies", "budget"],
    ["2026", "operations", "expense", "actual", "120.00", "FALSE", "3", "supplies", "actuals"],
]


class FakeAnalyzeClient:
    """A fake SheetsClient serving the "Budget Timeseries" grid for ``analyze``.

    Records which tabs were read so a test can assert the canonical transactions/budget
    tabs are NOT read by ``analyze`` (it sources only from the timeseries).
    """

    instances: list[FakeAnalyzeClient] = []

    def __init__(self, config: Config, **_: object) -> None:
        self.config = config
        self.read_values_calls: list[str] = []
        self.read_tab_calls: list[str] = []
        FakeAnalyzeClient.instances.append(self)

    def read_values(self, tab: str) -> list[list[str]]:
        self.read_values_calls.append(tab)
        from pta_finance import report_source

        if tab == report_source.BUDGET_TIMESERIES_TAB:
            return [list(r) for r in _TIMESERIES_GRID]
        return []

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        self.read_tab_calls.append(tab)
        return []


def test_analyze_prints_summary_all_years(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The analyze subcommand runs the real analytics through the production caller."""
    from pta_finance import report_source

    FakeAnalyzeClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeAnalyzeClient)
    config_path = _write_config(tmp_path)

    rc = cli.main(["analyze", "--config", str(config_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "all fiscal years" in out
    # income 500.00, expense 120.00 (the timeseries actuals).
    assert "income:  500.00" in out
    assert "expense: 120.00" in out
    # Sourced from the "Budget Timeseries" tab; the canonical tabs are NOT read.
    (client,) = FakeAnalyzeClient.instances
    assert client.read_values_calls == [report_source.BUDGET_TIMESERIES_TAB]
    assert schema.TAB_TRANSACTIONS not in client.read_tab_calls
    assert schema.TAB_BUDGET not in client.read_tab_calls


def test_analyze_filtered_to_fiscal_year_shows_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--fy filters the frame and triggers the budget-vs-actual section (from the timeseries)."""
    FakeAnalyzeClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeAnalyzeClient)
    config_path = _write_config(tmp_path)

    rc = cli.main(["analyze", "--config", str(config_path), "--fy", "2026"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "FY2026" in out
    assert "budget vs actual (FY2026)" in out
    # supplies budgeted 200.00, actual 120.00, variance 80.00 (under budget).
    assert "budgeted 200.00, actual 120.00, variance 80.00" in out


# A raw budget grid (header below row 1, a section, currency cells, a total rollup) with
# obviously-fake line items — exercises import-budget through the production CLI caller.
_BUDGET_GRID = [
    ["Example PTA Budget", "", "", ""],
    ["Type", "Line Item", "Proposed", "Actual "],
    ["Income", "Membership Dues", "1500", "1450"],
    ["Expense", "Classroom Supplies", "$2,000.00", "1200"],
    ["", "Total Expense", "2000", "1200"],
]


class FakeImportBudgetClient:
    """A fake SheetsClient for import-budget: serves a raw grid + records upserts/snapshots.

    ``read_values`` returns the canned budget grid; ``read_snapshot_values`` returns a header
    grid for every tab (so the real ``backup.snapshot_all_tabs`` runs and we can detect a
    snapshot was taken);
    ``upsert_rows`` records its (tab, rows) so the test asserts which tabs were written.
    """

    instances: list[FakeImportBudgetClient] = []

    def __init__(self, config: Config, **_: object) -> None:
        self.config = config
        self.upserts: list[tuple[str, Mapping[str, Mapping[str, str]]]] = []
        self.read_tab_calls: list[str] = []
        self.read_values_calls: list[str] = []
        FakeImportBudgetClient.instances.append(self)

    def read_values(self, tab: str) -> list[list[str]]:
        self.read_values_calls.append(tab)
        return [list(row) for row in _BUDGET_GRID]

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        self.read_tab_calls.append(tab)
        return []

    def read_snapshot_values(self, tab: str) -> list[list[dict[str, object]]]:
        self.read_tab_calls.append(tab)
        return tagged_user_entered_grid([list(schema.TABS[tab])])

    def upsert_rows(self, tab: str, rows_by_id: Mapping[str, Mapping[str, str]]) -> None:
        self.upserts.append((tab, rows_by_id))


def test_import_budget_upserts_budget_and_transactions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """import-budget --with-actuals upserts BOTH tabs and snapshots first, end-to-end."""
    from pta_finance import ids

    FakeImportBudgetClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeImportBudgetClient)
    config_path = _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)  # snapshot writes under cwd

    rc = cli.main(
        [
            "import-budget",
            "--from-tab",
            "Budget Source",
            "--fy",
            "2026",
            "--with-actuals",
            "--config",
            str(config_path),
        ]
    )

    assert rc == 0
    (client,) = FakeImportBudgetClient.instances
    assert client.read_values_calls == ["Budget Source"]
    # A snapshot was taken BEFORE writing (the exact-grid read fired for every canonical tab).
    assert set(client.read_tab_calls) == set(schema.TABS)
    assert (tmp_path / "snapshots").is_dir()

    upsert_tabs = {tab for tab, _ in client.upserts}
    assert upsert_tabs == {schema.TAB_BUDGET, schema.TAB_TRANSACTIONS}

    budget_upsert = next(rows for tab, rows in client.upserts if tab == schema.TAB_BUDGET)
    assert ids.budget_id(2026, "Membership Dues") in budget_upsert
    assert ids.budget_id(2026, "Classroom Supplies") in budget_upsert
    # "Total Expense" rollup was skipped.
    assert ids.budget_id(2026, "Total Expense") not in budget_upsert

    txn_upsert = next(rows for tab, rows in client.upserts if tab == schema.TAB_TRANSACTIONS)
    assert ids.summary_txn_id(2026, "Membership Dues") in txn_upsert
    assert ids.summary_txn_id(2026, "Classroom Supplies") in txn_upsert

    assert "import-budget:" in capsys.readouterr().out


def test_import_budget_dry_run_makes_no_writes_or_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run prints the plan but never upserts and never snapshots."""
    FakeImportBudgetClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeImportBudgetClient)
    config_path = _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(
        [
            "import-budget",
            "--from-tab",
            "Budget Source",
            "--fy",
            "2026",
            "--with-actuals",
            "--dry-run",
            "--config",
            str(config_path),
        ]
    )

    assert rc == 0
    (client,) = FakeImportBudgetClient.instances
    assert client.upserts == []  # NO writes
    assert client.read_tab_calls == []  # NO snapshot
    assert not (tmp_path / "snapshots").exists()
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "no writes made" in out


def test_import_budget_without_actuals_upserts_only_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --with-actuals, only the budget tab is upserted (no transactions)."""
    FakeImportBudgetClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeImportBudgetClient)
    config_path = _write_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(
        [
            "import-budget",
            "--from-tab",
            "Budget Source",
            "--fy",
            "2026",
            "--config",
            str(config_path),
        ]
    )

    assert rc == 0
    (client,) = FakeImportBudgetClient.instances
    upsert_tabs = [tab for tab, _ in client.upserts]
    assert upsert_tabs == [schema.TAB_BUDGET]  # transactions NOT written


def _config_with_start_month(start_month: int) -> str:
    """The fake config text with a substituted fiscal_year.start_month."""
    return _CONFIG_TEXT.replace("start_month = 1", f"start_month = {start_month}")


def _one_summary_txn_date(client: FakeImportBudgetClient) -> str:
    """The ``date`` cell shared by every upserted summary transaction row."""
    txn_rows = next(rows for tab, rows in client.upserts if tab == schema.TAB_TRANSACTIONS)
    dates = {row["date"] for row in txn_rows.values()}
    assert len(dates) == 1  # all summary txns share the FY-end date
    return dates.pop()


def _run_import_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_text: str,
    extra_args: list[str],
) -> FakeImportBudgetClient:
    """Run import-budget --with-actuals against the fake client and return that client."""
    FakeImportBudgetClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeImportBudgetClient)
    config_path = _write_config(tmp_path, config_text)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(
        [
            "import-budget",
            "--from-tab",
            "Budget Source",
            "--fy",
            "2026",
            "--with-actuals",
            "--config",
            str(config_path),
            *extra_args,
        ]
    )
    assert rc == 0
    (client,) = FakeImportBudgetClient.instances
    return client


def test_import_budget_july_start_stamps_fy_end_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start_month=7, FY2026: summary txns are stamped with the FY-end day (2026-06-30)."""
    client = _run_import_budget(
        tmp_path, monkeypatch, config_text=_config_with_start_month(7), extra_args=[]
    )
    assert _one_summary_txn_date(client) == "2026-06-30"


def test_import_budget_august_start_stamps_fy_end_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start_month=8, FY2026 (the LIVE deployment path): FY ends 2026-07-31."""
    client = _run_import_budget(
        tmp_path, monkeypatch, config_text=_config_with_start_month(8), extra_args=[]
    )
    assert _one_summary_txn_date(client) == "2026-07-31"


def test_import_budget_actual_date_override_flows_into_txn_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--actual-date overrides the derived FY-end date and lands on every summary txn."""
    client = _run_import_budget(
        tmp_path,
        monkeypatch,
        config_text=_config_with_start_month(8),  # override must win over the derived date
        extra_args=["--actual-date", "2026-03-15"],
    )
    assert _one_summary_txn_date(client) == "2026-03-15"


def test_fiscal_year_end_date_guard_holds_for_each_start_month(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The derived FY-end date always falls in the requested FY for every start month — the
    guard (a real ValueError, not a stripped assert) never trips on the correct arithmetic."""
    for start_month in range(1, 13):
        client = _run_import_budget(
            tmp_path,
            monkeypatch,
            config_text=_config_with_start_month(start_month),
            extra_args=[],
        )
        derived = _one_summary_txn_date(client)
        assert cli.ids.fiscal_year_label(cli.date.fromisoformat(derived), start_month) == 2026


@pytest.mark.parametrize(
    ("command", "start_month_args", "expected_fiscal_year"),
    [
        ("ingest-receipts", [], "FY2027"),
        ("ingest-receipts", ["--start-month", "8"], "FY2026"),
        ("map-receipts", [], "FY2027"),
        ("map-receipts", ["--start-month", "8"], "FY2026"),
    ],
)
def test_receipt_commands_use_config_start_month_unless_overridden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    start_month_args: list[str],
    expected_fiscal_year: str,
) -> None:
    """Both receipt entry points use config month 7 unless explicit month 8 wins."""
    config_path = _write_config(tmp_path, _config_with_start_month(7))
    source_path = tmp_path / "fake-submission.eml"
    source_path.touch()
    category_map_path = tmp_path / "category_map.csv"
    category_map_path.write_text(
        "raw_category,canonical_category\nSupplies,Program Supplies\n",
        encoding="utf-8",
    )
    csv_path = tmp_path / f"{command}.csv"
    submission = receipt_ingest.Submission(
        message_id="fake-message@example.invalid",
        subject="Reimbursement Request",
        received="Thu, 16 Jul 2026 12:00:00 +0000",
        requestor_name="Example Requestor",
        requestor_email="requestor@example.invalid",
        phone="",
        company="Example Vendor",
        line_items=(
            receipt_ingest.LineItem(
                index=1,
                date="2026-07-16",
                category="Supplies",
                description="Example supplies",
                amount="10.00",
            ),
        ),
        total="10.00",
        payment_type="Check",
        receipt_urls=(),
        attachments=(),
        notes="",
    )

    monkeypatch.setattr(
        receipt_ingest,
        "iter_source",
        lambda _source: iter([("fake-submission.eml", object())]),
    )
    monkeypatch.setattr(
        receipt_ingest,
        "parse_submission",
        lambda _message, *, subject_filter=None: submission,
    )

    args = [
        command,
        "--source",
        str(source_path),
        "--csv",
        str(csv_path),
        "--config",
        str(config_path),
        *start_month_args,
    ]
    if command == "map-receipts":
        args.extend(["--category-map", str(category_map_path)])

    assert cli.main(args) == 0
    with csv_path.open(encoding="utf-8", newline="") as handle:
        (row,) = csv.DictReader(handle)
    assert row["fiscal_year"] == expected_fiscal_year


# --- map-receipts received-date cutoff (real CLI + .eml wiring) -----------------------

_CUTOFF_FORM_BODY = """\
Requestor First and Last Name:
Example Requestor
Email:
requestor@example.invalid
1. Date:
{item_date}
1. Event or Budget Category:
Supplies
1. Description:
Example purchase
1. Amount:
{amount}
Total Amount $:
{amount}
Choose Payment Type:
Check
"""


def _write_cutoff_email(
    path: Path,
    *,
    message_id: str,
    received: str | None,
    item_date: str,
    amount: str,
) -> None:
    """Write one obviously-fake reimbursement submission as real RFC-822 bytes."""
    msg = EmailMessage()
    msg["Subject"] = "Example Reimbursement Form got a new submission"
    msg["From"] = "forms@example.invalid"
    msg["To"] = "treasurer@example.invalid"
    if received is not None:
        msg["Date"] = received
    msg["Message-ID"] = message_id
    msg.set_content(_CUTOFF_FORM_BODY.format(item_date=item_date, amount=amount))
    path.write_bytes(bytes(msg))


def _write_cutoff_category_map(tmp_path: Path) -> Path:
    path = tmp_path / "category-map.csv"
    path.write_text(
        "raw_category,canonical_category\nSupplies,Program Supplies\n",
        encoding="utf-8",
    )
    return path


def _config_with_received_since(received_since: str) -> str:
    return _CONFIG_TEXT + "\n[receipt_mapping]\n" + f'received_since = "{received_since}"\n'


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _forbid_sheets_construction(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    constructed: list[bool] = []

    class _UnexpectedSheetsClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            constructed.append(True)

    monkeypatch.setattr(cli, "SheetsClient", _UnexpectedSheetsClient)
    return constructed


def test_map_receipts_uses_config_cutoff_inclusively_through_real_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "a-older.eml",
        message_id="<older@example.invalid>",
        received="Sat, 31 Aug 2030 23:45:00 -0700",
        item_date="2030-08-31",
        amount="10.00",
    )
    _write_cutoff_email(
        source / "b-boundary.eml",
        message_id="<boundary@example.invalid>",
        received="Sun, 01 Sep 2030 00:15:00 -0700",
        item_date="2030-09-01",
        amount="20.00",
    )
    config_path = _write_config(tmp_path, _config_with_received_since("2030-09-01"))
    map_path = _write_cutoff_category_map(tmp_path)
    csv_path = tmp_path / "ledger.csv"

    rc = cli.main(
        [
            "map-receipts",
            "--source",
            str(source),
            "--category-map",
            str(map_path),
            "--csv",
            str(csv_path),
            "--config",
            str(config_path),
        ]
    )

    assert rc == 0
    assert [row["message_id"] for row in _read_csv_rows(csv_path)] == ["<boundary@example.invalid>"]
    out = capsys.readouterr().out
    assert "received cutoff : 2030-09-01 inclusive (config: receipt_mapping.received_since)" in out
    assert "excluded 1 submission(s)" in out


def test_map_receipts_cutoff_overrides_win_and_all_received_disables_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "a-first.eml",
        message_id="<first@example.invalid>",
        received="Sun, 01 Sep 2030 08:00:00 +0000",
        item_date="2030-09-01",
        amount="10.00",
    )
    _write_cutoff_email(
        source / "b-second.eml",
        message_id="<second@example.invalid>",
        received="Thu, 05 Sep 2030 08:00:00 +0000",
        item_date="2030-09-05",
        amount="20.00",
    )
    config_path = _write_config(tmp_path, _config_with_received_since("2030-09-10"))
    map_path = _write_cutoff_category_map(tmp_path)
    override_csv = tmp_path / "override.csv"
    all_csv = tmp_path / "all.csv"

    assert (
        cli.main(
            [
                "map-receipts",
                "--source",
                str(source),
                "--category-map",
                str(map_path),
                "--received-since",
                "2030-09-05",
                "--csv",
                str(override_csv),
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    assert [row["message_id"] for row in _read_csv_rows(override_csv)] == [
        "<second@example.invalid>"
    ]
    override_out = capsys.readouterr().out
    assert "received cutoff : 2030-09-05 inclusive (--received-since)" in override_out

    assert (
        cli.main(
            [
                "map-receipts",
                "--source",
                str(source),
                "--category-map",
                str(map_path),
                "--all-received",
                "--csv",
                str(all_csv),
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    assert {row["message_id"] for row in _read_csv_rows(all_csv)} == {
        "<first@example.invalid>",
        "<second@example.invalid>",
    }
    all_out = capsys.readouterr().out
    assert "received cutoff : none (--all-received)" in all_out
    assert "excluded 0 submission(s)" in all_out


def test_map_receipts_filters_cutoff_before_content_dedup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    # Same requestor + total + first line-item date => the mapper's content-dedup key matches.
    # The older file sorts first, so filtering after mapping would let it suppress the newer row.
    for filename, message_id, received in [
        ("a-older.eml", "<older-twin@example.invalid>", "Sat, 31 Aug 2030 08:00:00 +0000"),
        ("b-newer.eml", "<newer-twin@example.invalid>", "Sun, 01 Sep 2030 08:00:00 +0000"),
    ]:
        _write_cutoff_email(
            source / filename,
            message_id=message_id,
            received=received,
            item_date="2030-08-20",
            amount="30.00",
        )
    config_path = _write_config(tmp_path, _config_with_received_since("2030-09-01"))
    map_path = _write_cutoff_category_map(tmp_path)
    csv_path = tmp_path / "ledger.csv"

    assert (
        cli.main(
            [
                "map-receipts",
                "--source",
                str(source),
                "--category-map",
                str(map_path),
                "--csv",
                str(csv_path),
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    assert [row["message_id"] for row in _read_csv_rows(csv_path)] == [
        "<newer-twin@example.invalid>"
    ]
    assert "excluded 1 submission(s)" in capsys.readouterr().out


@pytest.mark.parametrize("received", [None, "not a valid RFC-822 date"])
def test_map_receipts_active_cutoff_rejects_invalid_received_without_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    received: str | None,
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "invalid.eml",
        message_id="<invalid-date@example.invalid>",
        received=received,
        item_date="2030-09-02",
        amount="10.00",
    )
    _write_cutoff_email(
        source / "valid.eml",
        message_id="<valid-date@example.invalid>",
        received="Mon, 02 Sep 2030 08:00:00 +0000",
        item_date="2030-09-02",
        amount="20.00",
    )
    config_path = _write_config(tmp_path, _config_with_received_since("2030-09-01"))
    map_path = _write_cutoff_category_map(tmp_path)
    csv_path = tmp_path / "must-not-exist.csv"
    constructed = _forbid_sheets_construction(monkeypatch)

    rc = cli.main(
        [
            "map-receipts",
            "--source",
            str(source),
            "--category-map",
            str(map_path),
            "--write-tab",
            "Example Reimbursements",
            "--csv",
            str(csv_path),
            "--config",
            str(config_path),
        ]
    )

    assert rc == 1
    assert not csv_path.exists()
    assert constructed == []
    out = capsys.readouterr().out
    assert "1 recognized original submission(s) have a missing or malformed Date header" in out
    assert "not a valid RFC-822 date" not in out


def test_map_receipts_refuses_empty_cutoff_write_before_constructing_sheets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "older.eml",
        message_id="<older-only@example.invalid>",
        received="Sat, 31 Aug 2030 08:00:00 +0000",
        item_date="2030-08-31",
        amount="10.00",
    )
    config_path = _write_config(tmp_path, _config_with_received_since("2030-09-01"))
    map_path = _write_cutoff_category_map(tmp_path)
    constructed = _forbid_sheets_construction(monkeypatch)

    rc = cli.main(
        [
            "map-receipts",
            "--source",
            str(source),
            "--category-map",
            str(map_path),
            "--write-tab",
            "Example Reimbursements",
            "--config",
            str(config_path),
        ]
    )

    assert rc == 1
    assert constructed == []
    out = capsys.readouterr().out
    assert "refusing --write-tab" in out
    assert "mapping produced zero ledger rows from 0 in-scope submission(s)" in out
    assert "1 recognized original submission(s)" in out


def test_map_receipts_refuses_zero_row_write_for_unrecognized_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    msg = EmailMessage()
    msg["Subject"] = "Example status update"
    msg["From"] = "sender@example.invalid"
    msg["To"] = "recipient@example.invalid"
    msg["Date"] = "Sun, 01 Sep 2030 08:00:00 +0000"
    msg["Message-ID"] = "<nonmatching@example.invalid>"
    msg.set_content("This message has no reimbursement-form structure.")
    (source / "nonmatching.eml").write_bytes(bytes(msg))
    config_path = _write_config(tmp_path)
    map_path = _write_cutoff_category_map(tmp_path)
    csv_path = tmp_path / "must-not-exist.csv"
    constructed = _forbid_sheets_construction(monkeypatch)

    rc = cli.main(
        [
            "map-receipts",
            "--source",
            str(source),
            "--category-map",
            str(map_path),
            "--write-tab",
            "Example Reimbursements",
            "--csv",
            str(csv_path),
            "--config",
            str(config_path),
        ]
    )

    assert rc == 1
    assert not csv_path.exists()
    assert constructed == []
    out = capsys.readouterr().out
    assert "mapping produced zero ledger rows from 0 in-scope submission(s)" in out
    assert "0 recognized original submission(s)" in out


def test_map_receipts_refuses_zero_row_write_for_in_scope_blank_amount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "blank-amount.eml",
        message_id="<blank-amount@example.invalid>",
        received="Sun, 01 Sep 2030 08:00:00 +0000",
        item_date="2030-09-01",
        amount="",
    )
    config_path = _write_config(tmp_path, _config_with_received_since("2030-09-01"))
    map_path = _write_cutoff_category_map(tmp_path)
    csv_path = tmp_path / "must-not-exist.csv"
    constructed = _forbid_sheets_construction(monkeypatch)

    rc = cli.main(
        [
            "map-receipts",
            "--source",
            str(source),
            "--category-map",
            str(map_path),
            "--write-tab",
            "Example Reimbursements",
            "--csv",
            str(csv_path),
            "--config",
            str(config_path),
        ]
    )

    assert rc == 1
    assert not csv_path.exists()
    assert constructed == []
    out = capsys.readouterr().out
    assert "mapping produced zero ledger rows from 1 in-scope submission(s)" in out
    assert "1 recognized original submission(s)" in out


def test_map_receipts_without_config_section_keeps_all_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "historical.eml",
        message_id="<historical@example.invalid>",
        received="Tue, 01 Jan 2030 08:00:00 +0000",
        item_date="2030-01-01",
        amount="10.00",
    )
    config_path = _write_config(tmp_path)
    map_path = _write_cutoff_category_map(tmp_path)
    csv_path = tmp_path / "ledger.csv"

    assert (
        cli.main(
            [
                "map-receipts",
                "--source",
                str(source),
                "--category-map",
                str(map_path),
                "--csv",
                str(csv_path),
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    assert [row["message_id"] for row in _read_csv_rows(csv_path)] == [
        "<historical@example.invalid>"
    ]
    out = capsys.readouterr().out
    assert "received cutoff : none (not configured)" in out
    assert "excluded 0 submission(s)" in out


def test_map_receipts_explicit_start_month_allows_absent_config_all_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "historical.eml",
        message_id="<config-free-history@example.invalid>",
        received="Tue, 01 Jan 2030 08:00:00 +0000",
        item_date="2030-01-01",
        amount="10.00",
    )
    map_path = _write_cutoff_category_map(tmp_path)
    csv_path = tmp_path / "ledger.csv"
    absent_config = tmp_path / "absent.toml"

    rc = cli.main(
        [
            "map-receipts",
            "--source",
            str(source),
            "--category-map",
            str(map_path),
            "--start-month",
            "1",
            "--csv",
            str(csv_path),
            "--config",
            str(absent_config),
        ]
    )

    assert rc == 0
    assert [row["message_id"] for row in _read_csv_rows(csv_path)] == [
        "<config-free-history@example.invalid>"
    ]
    assert "received cutoff : none (not configured)" in capsys.readouterr().out


def test_map_receipts_explicit_start_month_still_rejects_present_malformed_config(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "submission.eml",
        message_id="<malformed-config@example.invalid>",
        received="Sun, 01 Sep 2030 08:00:00 +0000",
        item_date="2030-09-01",
        amount="10.00",
    )
    map_path = _write_cutoff_category_map(tmp_path)
    config_path = _write_config(tmp_path, _config_with_received_since("not-an-iso-date"))

    with pytest.raises(ConfigError, match=r"receipt_mapping\.received_since"):
        cli.main(
            [
                "map-receipts",
                "--source",
                str(source),
                "--category-map",
                str(map_path),
                "--start-month",
                "1",
                "--all-received",
                "--config",
                str(config_path),
            ]
        )


@pytest.mark.parametrize("tab_name", ["", "   "])
def test_map_receipts_rejects_empty_write_tab_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tab_name: str
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "submission.eml",
        message_id="<empty-tab-name@example.invalid>",
        received="Sun, 01 Sep 2030 08:00:00 +0000",
        item_date="2030-09-01",
        amount="10.00",
    )
    map_path = _write_cutoff_category_map(tmp_path)
    constructed = _forbid_sheets_construction(monkeypatch)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            [
                "map-receipts",
                "--source",
                str(source),
                "--category-map",
                str(map_path),
                "--start-month",
                "1",
                "--all-received",
                "--write-tab",
                tab_name,
                "--config",
                str(tmp_path / "absent.toml"),
            ]
        )

    assert exc_info.value.code == 2
    assert constructed == []


@pytest.mark.parametrize(
    ("config_text", "cutoff_args"),
    [
        (_CONFIG_TEXT, []),
        (_config_with_received_since("2030-09-01"), ["--all-received"]),
    ],
    ids=["no-cutoff", "all-received-override"],
)
def test_map_receipts_inactive_cutoff_accepts_missing_and_malformed_dates(
    tmp_path: Path,
    config_text: str,
    cutoff_args: list[str],
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    for filename, message_id, received, item_date, amount in [
        ("missing.eml", "<missing-date@example.invalid>", None, "2030-09-01", "10.00"),
        (
            "malformed.eml",
            "<malformed-date@example.invalid>",
            "not a valid RFC-822 date",
            "2030-09-02",
            "20.00",
        ),
    ]:
        _write_cutoff_email(
            source / filename,
            message_id=message_id,
            received=received,
            item_date=item_date,
            amount=amount,
        )
    config_path = _write_config(tmp_path, config_text)
    map_path = _write_cutoff_category_map(tmp_path)
    csv_path = tmp_path / "ledger.csv"

    rc = cli.main(
        [
            "map-receipts",
            "--source",
            str(source),
            "--category-map",
            str(map_path),
            "--csv",
            str(csv_path),
            "--config",
            str(config_path),
            *cutoff_args,
        ]
    )

    assert rc == 0
    assert {row["message_id"] for row in _read_csv_rows(csv_path)} == {
        "<missing-date@example.invalid>",
        "<malformed-date@example.invalid>",
    }


def test_ingest_receipts_profile_ignores_configured_mapping_cutoff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    for filename, message_id, received, item_date in [
        (
            "historical.eml",
            "<profile-history@example.invalid>",
            "Tue, 01 Jan 2030 08:00:00 +0000",
            "2030-01-01",
        ),
        (
            "current.eml",
            "<profile-current@example.invalid>",
            "Sun, 01 Sep 2030 08:00:00 +0000",
            "2030-09-01",
        ),
    ]:
        _write_cutoff_email(
            source / filename,
            message_id=message_id,
            received=received,
            item_date=item_date,
            amount="10.00",
        )
    config_path = _write_config(tmp_path, _config_with_received_since("2030-09-01"))

    rc = cli.main(
        [
            "ingest-receipts",
            "--source",
            str(source),
            "--profile",
            "--originals-only",
            "--config",
            str(config_path),
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "recognized 2 reimbursement form(s)" in out
    assert "email date span     : 2030-01-01 -> 2030-09-01" in out


def test_category_seed_round_trips_formula_categories_without_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    csv_path = tmp_path / "category-seed.csv"
    dangerous = (
        "=1+1",
        "  +1+1",
        "'=1+1",
        "''=1+1",
        "'" * 8 + "=1+1",
        "'  @SUM(A1:A2)",
    )
    prof = replace(
        receipt_ingest.profile([], start_month=1),
        categories=tuple((value, index) for index, value in enumerate(dangerous, start=1)),
    )
    monkeypatch.setattr(receipt_ingest, "profile", lambda *_args, **_kwargs: prof)

    rc = cli.main(
        [
            "ingest-receipts",
            "--source",
            str(source),
            "--profile",
            "--start-month",
            "1",
            "--csv",
            str(csv_path),
            "--config",
            str(tmp_path / "absent.toml"),
        ]
    )

    assert rc == 0
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["raw_category"] for row in rows] == [f"'{value}" for value in dangerous]
    assert len({row["raw_category"] for row in rows}) == len(dangerous)
    assert [row["line_item_count"] for row in rows] == [
        str(index) for index in range(1, len(dangerous) + 1)
    ]

    filled_path = tmp_path / "category-map.csv"
    with filled_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["raw_category", "canonical_category"])
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow(
                {
                    "raw_category": row["raw_category"],
                    "canonical_category": f"Category {index}",
                }
            )

    category_map = receipt_map.load_category_map(filled_path)
    submission = receipt_ingest.Submission(
        message_id="category-round-trip@example.invalid",
        subject="Example Reimbursement Form got a new submission",
        received="Sun, 01 Sep 2030 08:00:00 +0000",
        requestor_name="Example Requestor",
        requestor_email="requestor@example.invalid",
        phone="",
        company="Example Vendor",
        line_items=tuple(
            receipt_ingest.LineItem(
                index=index,
                date="2030-09-01",
                category=value,
                description=f"Example item {index}",
                amount=f"{index}.00",
            )
            for index, value in enumerate(dangerous, start=1)
        ),
        total=f"{sum(range(1, len(dangerous) + 1))}.00",
        payment_type="Check",
        receipt_urls=(),
        attachments=(),
        notes="",
    )

    mapped = receipt_map.map_submissions([submission], category_map=category_map, start_month=1)
    assert [row["canonical_category"] for row in mapped] == [
        f"Category {index}" for index in range(1, len(dangerous) + 1)
    ]
    assert all("unmapped-category" not in row["needs_review"] for row in mapped)


def test_ingest_receipts_csv_neutralizes_text_and_preserves_finite_signed_money(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    csv_path = tmp_path / "ingest.csv"
    formula_submission = receipt_ingest.Submission(
        message_id="  +message",
        subject="Example Reimbursement Form got a new submission",
        received="\t-received",
        requestor_name="  @requestor",
        requestor_email="=mail",
        phone="",
        company=" +company",
        line_items=(
            receipt_ingest.LineItem(
                index=1,
                date="\t-date",
                category=" @category",
                description="=description",
                amount="=2+2",
            ),
        ),
        total="  @SUM(A1:A2)",
        payment_type="Check",
        receipt_urls=(" +url",),
        attachments=("\t-attachment",),
        notes="",
    )

    def _money_submission(message_id: str, amount: str) -> receipt_ingest.Submission:
        return receipt_ingest.Submission(
            message_id=message_id,
            subject="Example Reimbursement Form got a new submission",
            received="Sun, 01 Sep 2030 08:00:00 +0000",
            requestor_name="Example Requestor",
            requestor_email="requestor@example.invalid",
            phone="",
            company="Example Vendor",
            line_items=(
                receipt_ingest.LineItem(
                    index=1,
                    date="2030-09-01",
                    category="Supplies",
                    description="Example purchase",
                    amount=amount,
                ),
            ),
            total=amount,
            payment_type="Check",
            receipt_urls=(),
            attachments=(),
            notes="",
        )

    submissions = [
        ("=source", formula_submission),
        ("negative.eml", _money_submission("negative@example.invalid", "-12.50")),
        ("positive.eml", _money_submission("positive@example.invalid", "+20.50")),
        ("bounded.eml", _money_submission("bounded@example.invalid", "1e100000")),
    ]
    monkeypatch.setattr(receipt_ingest, "iter_source", lambda _path: iter(submissions))
    monkeypatch.setattr(
        receipt_ingest,
        "parse_submission",
        lambda message, *, subject_filter=None: message,
    )

    rc = cli.main(
        [
            "ingest-receipts",
            "--source",
            str(source),
            "--start-month",
            "1",
            "--limit",
            "0",
            "--csv",
            str(csv_path),
            "--config",
            str(tmp_path / "absent.toml"),
        ]
    )

    assert rc == 0
    rows = _read_csv_rows(csv_path)
    formula_row = rows[0]
    expected_formula_cells = {
        "source_file": "=source",
        "message_id": "  +message",
        "received": "\t-received",
        "requestor_name": "  @requestor",
        "requestor_email": "=mail",
        "company": " +company",
        "date": "\t-date",
        "category": " @category",
        "description": "=description",
        "amount": "=2+2",
        "total_stated": "  @SUM(A1:A2)",
        "receipt_urls": " +url",
        "attachments": "\t-attachment",
    }
    for field, raw in expected_formula_cells.items():
        assert formula_row[field] == f"'{raw}"
    assert [row["amount"] for row in rows[1:]] == ["-12.50", "20.50", "'1e100000"]
    assert [row["total_stated"] for row in rows[1:]] == [
        "-12.50",
        "20.50",
        "'1e100000",
    ]


def test_map_receipts_csv_neutralizes_formula_like_text_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "submission.eml",
        message_id="<csv-safety@example.invalid>",
        received="Sun, 01 Sep 2030 08:00:00 +0000",
        item_date="2030-09-01",
        amount="10.00",
    )
    map_path = _write_cutoff_category_map(tmp_path)
    csv_path = tmp_path / "ledger.csv"
    text_fields = [name for name in receipt_map.FIELDNAMES if name != "amount"]
    dangerous_values = ["=1+1", "  +1+1", "\t-command", "  @SUM(A1:A2)"]
    mapped_row = {
        field: dangerous_values[index % len(dangerous_values)]
        for index, field in enumerate(text_fields)
    }
    mapped_row["amount"] = "-12.50"
    mapped_rows = []
    for message_id in ("=message", "'=message", "''=message"):
        row = dict(mapped_row)
        row["message_id"] = message_id
        mapped_rows.append(row)
    monkeypatch.setattr(receipt_map, "map_submissions", lambda *_args, **_kwargs: mapped_rows)

    rc = cli.main(
        [
            "map-receipts",
            "--source",
            str(source),
            "--category-map",
            str(map_path),
            "--start-month",
            "1",
            "--all-received",
            "--csv",
            str(csv_path),
            "--config",
            str(tmp_path / "absent.toml"),
        ]
    )

    assert rc == 0
    rows = _read_csv_rows(csv_path)
    assert [row["message_id"] for row in rows] == ["'=message", "''=message", "'''=message"]
    assert [row["amount"] for row in rows] == ["-12.50"] * 3
    for field in text_fields:
        if field == "message_id":
            continue
        assert [row[field] for row in rows] == [
            backup.encode_formula_safe_text(mapped_row[field])
        ] * 3


class _CapturingReceiptSheetsClient:
    instances: list[_CapturingReceiptSheetsClient] = []

    def __init__(self, config: Config) -> None:
        self.config = config
        self.replacement: tuple[str, list[str], list[list[str]], list[str]] | None = None
        self.instances.append(self)

    def list_worksheet_titles(self) -> list[str]:
        return []

    def replace_tab_grid(
        self,
        tab: str,
        header: Sequence[str],
        rows: Sequence[Sequence[str]],
        *,
        numeric_columns: Sequence[str] = (),
    ) -> str:
        self.replacement = (
            tab,
            list(header),
            [list(row) for row in rows],
            list(numeric_columns),
        )
        return "created"


def test_map_receipts_cutoff_sheet_write_orders_schema_and_safes_amounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    for filename, message_id, received, item_date, amount in [
        (
            "a-excluded.eml",
            "<sheet-excluded@example.invalid>",
            "Sat, 31 Aug 2030 08:00:00 +0000",
            "2030-08-31",
            "1.00",
        ),
        (
            "b-negative.eml",
            "<sheet-negative@example.invalid>",
            "Sun, 01 Sep 2030 08:00:00 +0000",
            "2030-09-01",
            "-10.25",
        ),
        (
            "c-positive.eml",
            "<sheet-positive@example.invalid>",
            "Mon, 02 Sep 2030 08:00:00 +0000",
            "2030-09-02",
            "+20.50",
        ),
        (
            "d-formula.eml",
            "<sheet-formula@example.invalid>",
            "Tue, 03 Sep 2030 08:00:00 +0000",
            "2030-09-03",
            "=2+2",
        ),
        (
            "e-bounded.eml",
            "<sheet-bounded@example.invalid>",
            "Wed, 04 Sep 2030 08:00:00 +0000",
            "2030-09-04",
            "1e100000",
        ),
    ]:
        _write_cutoff_email(
            source / filename,
            message_id=message_id,
            received=received,
            item_date=item_date,
            amount=amount,
        )
    map_path = _write_cutoff_category_map(tmp_path)
    config_path = _write_config(tmp_path, _config_with_received_since("2030-09-01"))
    csv_path = tmp_path / "ledger.csv"
    load_calls: list[Path] = []
    real_load_config = cli.load_config

    def _counting_load_config(path: Path) -> Config:
        load_calls.append(path)
        return real_load_config(path)

    _CapturingReceiptSheetsClient.instances = []
    monkeypatch.setattr(cli, "load_config", _counting_load_config)
    monkeypatch.setattr(cli, "SheetsClient", _CapturingReceiptSheetsClient)

    rc = cli.main(
        [
            "map-receipts",
            "--source",
            str(source),
            "--category-map",
            str(map_path),
            "--write-tab",
            "Example Reimbursements",
            "--csv",
            str(csv_path),
            "--config",
            str(config_path),
        ]
    )

    assert rc == 0
    (client,) = _CapturingReceiptSheetsClient.instances
    assert client.replacement is not None
    tab, header, sheet_rows, numeric_columns = client.replacement
    assert tab == "Example Reimbursements"
    assert header == list(receipt_map.FIELDNAMES)
    assert numeric_columns == ["amount"]
    amount_index = header.index("amount")
    assert [row[amount_index] for row in sheet_rows] == [
        "-10.25",
        "20.50",
        "''=2+2",
        "'1e100000",
    ]
    csv_rows = _read_csv_rows(csv_path)
    assert [row["message_id"] for row in csv_rows] == [
        "<sheet-negative@example.invalid>",
        "<sheet-positive@example.invalid>",
        "<sheet-formula@example.invalid>",
        "<sheet-bounded@example.invalid>",
    ]
    assert [row["amount"] for row in csv_rows] == [
        "-10.25",
        "20.50",
        "'=2+2",
        "'1e100000",
    ]
    expected_sheet_rows = [[row[name] for name in receipt_map.FIELDNAMES] for row in csv_rows]
    expected_sheet_rows[-2][amount_index] = "''=2+2"
    expected_sheet_rows[-1][amount_index] = "'1e100000"
    assert sheet_rows == expected_sheet_rows
    assert load_calls == [config_path]


def test_map_receipts_write_requires_config_snapshot_before_source_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "historical.eml",
        message_id="<late-config@example.invalid>",
        received="Sun, 01 Sep 2030 08:00:00 +0000",
        item_date="2030-09-01",
        amount="10.00",
    )
    map_path = _write_cutoff_category_map(tmp_path)
    config_path = tmp_path / "initially-absent.toml"
    source_iteration_started: list[bool] = []
    real_iter_source = receipt_ingest.iter_source

    def _source_that_creates_config_late(path: Path):  # type: ignore[no-untyped-def]
        source_iteration_started.append(True)
        config_path.write_text(_config_with_received_since("2030-10-01"), encoding="utf-8")
        yield from real_iter_source(path)

    _CapturingReceiptSheetsClient.instances = []
    monkeypatch.setattr(receipt_ingest, "iter_source", _source_that_creates_config_late)
    monkeypatch.setattr(cli, "SheetsClient", _CapturingReceiptSheetsClient)

    with pytest.raises(FileNotFoundError):
        cli.main(
            [
                "map-receipts",
                "--source",
                str(source),
                "--category-map",
                str(map_path),
                "--start-month",
                "1",
                "--write-tab",
                "Example Reimbursements",
                "--config",
                str(config_path),
            ]
        )

    assert source_iteration_started == []
    assert not config_path.exists()
    assert _CapturingReceiptSheetsClient.instances == []


def test_map_receipts_preview_without_start_month_requires_initial_config_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "historical.eml",
        message_id="<late-preview-config@example.invalid>",
        received="Sun, 01 Sep 2030 08:00:00 +0000",
        item_date="2030-09-01",
        amount="10.00",
    )
    map_path = _write_cutoff_category_map(tmp_path)
    config_path = tmp_path / "initially-absent.toml"
    category_load_started: list[bool] = []
    real_load_category_map = receipt_map.load_category_map

    def _category_load_that_creates_config_late(path: Path) -> dict[str, str]:
        category_load_started.append(True)
        config_path.write_text(_config_with_received_since("2030-10-01"), encoding="utf-8")
        return real_load_category_map(path)

    monkeypatch.setattr(receipt_map, "load_category_map", _category_load_that_creates_config_late)

    with pytest.raises(FileNotFoundError):
        cli.main(
            [
                "map-receipts",
                "--source",
                str(source),
                "--category-map",
                str(map_path),
                "--config",
                str(config_path),
            ]
        )

    assert category_load_started == []
    assert not config_path.exists()


class _PersistingReceiptSheetsClient:
    """Fake the RAW grid plus USER_ENTERED apostrophe handling across two CLI runs."""

    def __init__(self) -> None:
        self.grid: list[list[object]] = []
        self.numeric_columns_calls: list[list[str]] = []
        self.events: list[str] = []

    def list_worksheet_titles(self) -> list[str]:
        return ["Example Reimbursements"] if self.grid else []

    def read_values(self, _tab: str) -> list[list[str]]:
        self.events.append("snapshot-read")
        return [[str(cell) for cell in row] for row in self.grid]

    def read_snapshot_values(self, _tab: str) -> list[list[dict[str, object]]]:
        self.events.append("snapshot-read")
        return tagged_user_entered_grid(self.grid)

    def replace_tab_grid(
        self,
        _tab: str,
        header: Sequence[str],
        rows: Sequence[Sequence[str]],
        *,
        numeric_columns: Sequence[str] = (),
    ) -> str:
        self.events.append("replace")
        existed = bool(self.grid)
        header_row: list[object] = list(header)
        data: list[list[object]] = [list(row) for row in rows]
        self.numeric_columns_calls.append(list(numeric_columns))
        amount_index = header_row.index("amount")
        for row in data:
            amount = str(row[amount_index])
            if amount.startswith("'"):
                row[amount_index] = amount[1:]
            else:
                row[amount_index] = float(receipt_map.parse_finite_amount(amount))
        self.grid = [header_row, *data]
        return "replaced" if existed else "created"


def test_map_receipts_first_backup_safes_existing_grid_and_keeps_lossless_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "submission.eml",
        message_id="<first-safe-backup@example.invalid>",
        received="Sun, 01 Sep 2030 08:00:00 +0000",
        item_date="2030-09-01",
        amount="10.00",
    )
    map_path = _write_cutoff_category_map(tmp_path)
    config_path = _write_config(tmp_path)
    existing_grid = [
        ["equals", "plus", "minus", "at", "plain"],
        ["=1+1", "  +1+1", "\t-command", "  @SUM(A1:A2)", "unchanged"],
    ]
    client = _PersistingReceiptSheetsClient()
    client.grid = [list(row) for row in existing_grid]
    monkeypatch.setattr(cli, "SheetsClient", lambda _config: client)

    rc = cli.main(
        [
            "map-receipts",
            "--source",
            str(source),
            "--category-map",
            str(map_path),
            "--write-tab",
            "Example Reimbursements",
            "--dest",
            str(tmp_path),
            "--config",
            str(config_path),
        ]
    )

    assert rc == 0
    assert client.events == ["snapshot-read", "replace"]
    (csv_path,) = list((tmp_path / "snapshots").glob("*/Example Reimbursements.csv"))
    with csv_path.open(encoding="utf-8", newline="") as handle:
        safe_grid = list(csv.reader(handle))
    assert safe_grid == [
        existing_grid[0],
        ["'=1+1", "'  +1+1", "'\t-command", "'  @SUM(A1:A2)", "unchanged"],
    ]
    lossless_path = csv_path.with_suffix(".raw.json")
    assert json.loads(lossless_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "value_model": "google-sheets-userEnteredValue",
        "grid": tagged_user_entered_grid(existing_grid),
    }


def test_map_receipts_sheet_values_remain_formula_safe_in_next_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    _write_cutoff_email(
        source / "submission.eml",
        message_id="<snapshot-safety@example.invalid>",
        received="Sun, 01 Sep 2030 08:00:00 +0000",
        item_date="2030-09-01",
        amount="10.00",
    )
    map_path = _write_cutoff_category_map(tmp_path)
    config_path = _write_config(tmp_path)
    text_fields = [name for name in receipt_map.FIELDNAMES if name != "amount"]
    dangerous_values = ["=1+1", "  +1+1", "\t-command", "  @SUM(A1:A2)"]
    formula_row = {
        field: dangerous_values[index % len(dangerous_values)]
        for index, field in enumerate(text_fields)
    }
    formula_row["amount"] = "=2+2"

    def _safe_row(message_id: str, amount: str) -> dict[str, str]:
        row = {field: "safe" for field in receipt_map.FIELDNAMES}
        row["message_id"] = message_id
        row["amount"] = amount
        row["needs_review"] = ""
        return row

    mapped_rows = [
        formula_row,
        _safe_row("<negative-snapshot@example.invalid>", "-12.50"),
        _safe_row("<positive-snapshot@example.invalid>", "+20.50"),
        _safe_row("<apostrophe-snapshot@example.invalid>", "'oops"),
        _safe_row("<double-apostrophe-snapshot@example.invalid>", "''=2+2"),
        _safe_row("=message", "1.00"),
        _safe_row("'=message", "1.00"),
        _safe_row("''=message", "1.00"),
    ]
    client = _PersistingReceiptSheetsClient()
    monkeypatch.setattr(
        receipt_map,
        "map_submissions",
        lambda *_args, **_kwargs: [dict(row) for row in mapped_rows],
    )
    monkeypatch.setattr(cli, "SheetsClient", lambda _config: client)
    args = [
        "map-receipts",
        "--source",
        str(source),
        "--category-map",
        str(map_path),
        "--write-tab",
        "Example Reimbursements",
        "--dest",
        str(tmp_path),
        "--config",
        str(config_path),
    ]

    assert cli.main(args) == 0
    assert cli.main(args) == 0

    snapshot_paths = list((tmp_path / "snapshots").glob("*/Example Reimbursements.csv"))
    assert len(snapshot_paths) == 1
    snapshot_rows = _read_csv_rows(snapshot_paths[0])
    formula_snapshot = snapshot_rows[0]
    for field in text_fields:
        assert formula_snapshot[field] == "''" + formula_row[field]
    assert [row["amount"] for row in snapshot_rows] == [
        "''=2+2",
        "-12.5",
        "20.5",
        "'oops",
        "''''=2+2",
        "1.0",
        "1.0",
        "1.0",
    ]
    assert [row["message_id"] for row in snapshot_rows[-3:]] == [
        "''=message",
        "'''=message",
        "''''=message",
    ]
    exact = json.loads(snapshot_paths[0].with_suffix(".raw.json").read_text(encoding="utf-8"))
    message_index = list(receipt_map.FIELDNAMES).index("message_id")
    assert [
        row[message_index]["userEnteredValue"]["stringValue"] for row in exact["grid"][-3:]
    ] == ["'=message", "''=message", "'''=message"]
    assert client.numeric_columns_calls == [["amount"], ["amount"]]
