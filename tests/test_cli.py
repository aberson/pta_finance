"""Tests for the wired CLI subcommands (check, snapshot) against a mocked SheetsClient.

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
    """A fake SheetsClient capturing the check round-trip: validate + upsert/read/delete."""

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
        self._store: dict[str, dict[str, str]] = {}
        FakeCheckClient.instances.append(self)

    def validate_schema(self, tab: str) -> None:
        self.validated.append(tab)

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


def test_check_validates_all_tabs_and_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeCheckClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeCheckClient)
    config_path = _write_config(tmp_path)

    rc = cli.main(["check", "--config", str(config_path)])

    assert rc == 0
    # Two clients are built: one for prod schema validation, one for the test-sheet round-trip.
    assert len(FakeCheckClient.instances) == 2
    prod, test = FakeCheckClient.instances
    # All tabs validated on the prod client.
    assert prod.validated == list(schema.TABS)
    # Round-trip on the test client: one upsert, one read-back match, one delete cleanup.
    assert test.spreadsheet_id == "fake-test-sheet-id"
    assert len(test.upserts) == 1
    upsert_tab, rows_by_id = test.upserts[0]
    assert upsert_tab == schema.TAB_TRANSACTIONS
    (probe_id,) = rows_by_id.keys()
    assert test.deletes == [(schema.TAB_TRANSACTIONS, [probe_id])]
    out = capsys.readouterr().out
    assert "round-trip OK" in out


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


class FakeSnapshotClient:
    def __init__(self, config: Config, **_: object) -> None:
        self.config = config

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        return []


def test_snapshot_writes_csvs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "SheetsClient", FakeSnapshotClient)
    config_path = _write_config(tmp_path)
    dest = tmp_path / "out"

    rc = cli.main(["snapshot", "--config", str(config_path), "--dest", str(dest)])

    assert rc == 0
    snapshot_root = dest / "snapshots"
    assert snapshot_root.is_dir()
    (run_dir,) = list(snapshot_root.iterdir())
    for tab, columns in schema.TABS.items():
        csv_path = run_dir / f"{tab}.csv"
        assert csv_path.is_file()
        with csv_path.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        assert rows[0] == list(columns)
    assert "snapshot: wrote" in capsys.readouterr().out


class FakeAnalyzeClient:
    """A fake SheetsClient returning canned transactions + budget records for ``analyze``."""

    def __init__(self, config: Config, **_: object) -> None:
        self.config = config
        cols = schema.TRANSACTIONS_COLUMNS
        bcols = schema.BUDGET_COLUMNS

        def txn(**ov: str) -> dict[str, str]:
            row = {c: "" for c in cols}
            row.update(ov)
            return row

        def bud(**ov: str) -> dict[str, str]:
            row = {c: "" for c in bcols}
            row.update(ov)
            return row

        self._txns = [
            txn(
                id="TXN-FY26-0001",
                date="2026-01-15",
                fiscal_year="2026",
                type="income",
                amount="500.00",
                category="fundraiser",
            ),
            txn(
                id="TXN-FY26-0002",
                date="2026-02-10",
                fiscal_year="2026",
                type="expense",
                amount="120.00",
                category="supplies",
                grade="3",
            ),
            # Excluded from all aggregations.
            txn(
                id="TXN-FY26-0003",
                date="2026-02-11",
                fiscal_year="2026",
                type="expense",
                amount="999.99",
                category="supplies",
                needs_review="TRUE",
            ),
        ]
        self._budget = [
            bud(
                id="BUD-FY26-supplies",
                fiscal_year="2026",
                category="supplies",
                budgeted_amount="200.00",
            ),
        ]

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        if tab == schema.TAB_TRANSACTIONS:
            return [dict(r) for r in self._txns]
        if tab == schema.TAB_BUDGET:
            return [dict(r) for r in self._budget]
        return []


def test_analyze_prints_summary_all_years(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The analyze subcommand runs the real analytics through the production caller."""
    monkeypatch.setattr(cli, "SheetsClient", FakeAnalyzeClient)
    config_path = _write_config(tmp_path)

    rc = cli.main(["analyze", "--config", str(config_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "all fiscal years" in out
    # The needs_review row is excluded and reported.
    assert "excluded (needs_review): 1" in out
    # income 500.00, expense 120.00 (NOT 1119.99 — the flagged row is excluded).
    assert "income:  500.00" in out
    assert "expense: 120.00" in out


def test_analyze_filtered_to_fiscal_year_shows_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--fy filters the frame and triggers the budget-vs-actual section."""
    monkeypatch.setattr(cli, "SheetsClient", FakeAnalyzeClient)
    config_path = _write_config(tmp_path)

    rc = cli.main(["analyze", "--config", str(config_path), "--fy", "2026"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "FY2026" in out
    assert "budget vs actual (FY2026)" in out
    # supplies budgeted 200.00, actual 120.00, variance 80.00 (under budget).
    assert "budgeted 200.00, actual 120.00, variance 80.00" in out
