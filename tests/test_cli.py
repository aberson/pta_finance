"""Tests for the wired CLI subcommands (check, init-sheet, snapshot) against a mocked SheetsClient.

No live Google calls: ``cli.SheetsClient`` is monkeypatched to a fake, and ``snapshot``
runs through the real ``backup.snapshot_all_tabs`` with a fake read client.
"""

from __future__ import annotations

import csv
from collections.abc import Mapping
from pathlib import Path

import pytest

from pta_finance import cli, schema
from pta_finance.config import Config

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

    ``read_values`` returns the canned budget grid; ``read_tab`` returns [] for every tab
    (so the real ``backup.snapshot_all_tabs`` runs and we can detect a snapshot was taken);
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
    # A snapshot was taken BEFORE writing (read_tab fired for every canonical tab).
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
