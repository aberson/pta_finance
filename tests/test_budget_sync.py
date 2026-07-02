"""Tests for budget-sync: the PURE parse/plan pair + the CLI reconcile through the caller.

No live Google: the CLI integration test monkeypatches ``cli.SheetsClient`` with a fake that
serves both grids (the editable budget tab + the "Budget Timeseries" DB) and records the
schema-independent writes (``update_cells`` / ``append_raw_rows``) so we assert the reconcile
reaches the DB end-to-end (workspace code-quality rule: a new component gets an integration
test through its production caller). Identity is fake placeholders only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from pta_finance import budget_sync, cli, ids, report_source
from pta_finance.config import Config

# --- a 14-column live "Budget Timeseries" grid (the real shape: 9 required + 5 enrichment) ---
_TS_HEADER = [
    "fiscal_year",
    "category_group",
    "type",
    "measure",
    "amount",
    "is_fundraiser",
    "grade",
    "raw_category",
    "source_tab",
    "standard_category",
    "strategic_group",
    "release_criteria",
    "strategic_goal",
    "notes",
]


def _ts_row(
    *,
    fy: str,
    group: str,
    typ: str,
    measure: str,
    amount: str,
    raw: str,
    strat: str = "",
    goal: str = "",
    notes: str = "",
) -> list[str]:
    """Build one Budget Timeseries row in the 14-column live order."""
    return [fy, group, typ, measure, amount, "", "", raw, "budget", "", strat, "", goal, notes]


# FY2027 proposed: one income line + two expense lines (one carrying enrichment we must NOT
# clobber). Plus a FY2026 row and a FY2027 ACTUAL row that sync must never touch.
_TS_GRID: list[list[str]] = [
    list(_TS_HEADER),
    _ts_row(
        fy="2027",
        group="Fundraising Events",
        typ="income",
        measure="proposed",
        amount="25000",
        raw="Walk-A-Thon Income",
    ),
    _ts_row(
        fy="2027",
        group="Core Commitments",
        typ="expense",
        measure="proposed",
        amount="146414",
        raw="Specialist Teacher Contracts Expenses",
        strat="Core Commitments",
        goal="G2, G5",
        notes="keep me",
    ),
    _ts_row(
        fy="2027",
        group="Core Commitments",
        typ="expense",
        measure="proposed",
        amount="200",
        raw="PayPal Fees",
        strat="Core Commitments",
        goal="G2, G5",
    ),
    _ts_row(
        fy="2026",
        group="Core Commitments",
        typ="expense",
        measure="proposed",
        amount="9999",
        raw="Specialist Teacher Contracts Expenses",
    ),
    _ts_row(
        fy="2027",
        group="Core Commitments",
        typ="expense",
        measure="actual",
        amount="1",
        raw="Specialist Teacher Contracts Expenses",
    ),
]

# An editable "FY2027 Budget" tab grid (as _cmd_sync_budget builds it): title, instructions,
# a column header, INCOME/EXPENSE sections with category_group sub-headers, data rows,
# subtotals, and a total. This version encodes edits:
#   - Specialist Teacher Contracts amount changed 146414 -> 150000
#   - PayPal Fees unchanged (200)
#   - Walk-A-Thon Income unchanged (25000) but a note added
#   - a NEW expense line "Art Club Expenses" 500 under "Enrichment & Student Programs"
_BUDGET_TAB_GRID: list[list[str]] = [
    ["FY2027 Budget (editable)", "", ""],
    ["Edit amounts/notes; add lines under a section; then run pta-finance sync-budget", "", ""],
    ["Item", "Proposed $", "Notes"],
    ["INCOME", "", ""],
    ["Fundraising Events", "", ""],
    ["Walk-A-Thon Income", "$25,000.00", "spring walkathon"],
    ["Subtotal — Fundraising Events", "$25,000.00", ""],
    ["EXPENSE", "", ""],
    ["Core Commitments", "", ""],
    ["Specialist Teacher Contracts Expenses", "$150,000.00", ""],
    ["PayPal Fees", "$200.00", ""],
    ["Subtotal — Core Commitments", "$150,200.00", ""],
    ["Enrichment & Student Programs", "", ""],
    ["Art Club Expenses", "$500.00", "new for FY27"],
    ["Subtotal — Enrichment & Student Programs", "$500.00", ""],
    ["TOTAL EXPENSE", "$150,700.00", ""],
    ["NET (income − expense)", "-$125,700.00", ""],
]


# --------------------------------------------------------------------------- parse


def test_parse_budget_tab_extracts_only_data_rows() -> None:
    lines = budget_sync.parse_budget_tab(_BUDGET_TAB_GRID).lines
    # 4 data rows: Walk-A-Thon (income) + Specialist + PayPal + Art Club (expense).
    assert [(x.type, x.item, x.amount) for x in lines] == [
        ("income", "Walk-A-Thon Income", 25000.0),
        ("expense", "Specialist Teacher Contracts Expenses", 150000.0),
        ("expense", "PayPal Fees", 200.0),
        ("expense", "Art Club Expenses", 500.0),
    ]
    # category_group is inherited from the sub-section header.
    by_item = {x.item: x for x in lines}
    assert by_item["Walk-A-Thon Income"].category_group == "Fundraising Events"
    assert by_item["Art Club Expenses"].category_group == "Enrichment & Student Programs"
    assert by_item["Walk-A-Thon Income"].notes == "spring walkathon"


def test_parse_skips_headers_rollups_and_preamble() -> None:
    # Title, instructions, the "Item | Proposed $ | Notes" column header, and every
    # Subtotal/Total/Net rollup must NOT become budget lines.
    items = {x.item for x in budget_sync.parse_budget_tab(_BUDGET_TAB_GRID).lines}
    assert "Item" not in items
    assert not any(i.upper().startswith(("SUBTOTAL", "TOTAL", "NET")) for i in items)
    assert "FY2027 Budget (editable)" not in items


# --------------------------------------------------------------------------- plan


def test_plan_detects_change_add_and_flagged_removal() -> None:
    lines = budget_sync.parse_budget_tab(_BUDGET_TAB_GRID).lines
    plan = budget_sync.plan_budget_sync(_TS_GRID, lines, fy=2027)

    # amount change: Specialist Teacher Contracts 146414 -> 150000 (one targeted cell).
    assert len(plan.changed) == 1
    typ, item, old, new = plan.changed[0]
    assert typ == "expense"
    assert item == "Specialist Teacher Contracts Expenses"
    assert (old, new) == (146414.0, 150000.0)

    # note change on Walk-A-Thon Income.
    assert ("income", "Walk-A-Thon Income") in plan.notes_changed

    # one new line (Art Club) becomes an appended row.
    assert len(plan.append_rows) == 1
    assert len(plan.added) == 1
    assert plan.added[0][0:3] == ("expense", "Enrichment & Student Programs", "Art Club Expenses")

    # PayPal Fees unchanged.
    assert plan.unchanged == 1

    # nothing flagged-removed (every FY2027-proposed DB line is on the tab).
    assert plan.removed == []


def test_plan_flags_db_line_missing_from_tab() -> None:
    # Drop PayPal Fees from the tab -> it must be FLAGGED removed, never written/deleted.
    grid = [r for r in _BUDGET_TAB_GRID if r[0] != "PayPal Fees"]
    plan = budget_sync.plan_budget_sync(_TS_GRID, budget_sync.parse_budget_tab(grid).lines, fy=2027)
    # removed reports the DB's display name verbatim.
    assert ("expense", "PayPal Fees", 200.0) in plan.removed
    # removal produces NO write.
    assert plan.cell_updates == {} or "200" not in " ".join(plan.cell_updates.values())


def test_plan_targets_only_the_amount_cell_and_preserves_enrichment() -> None:
    lines = budget_sync.parse_budget_tab(_BUDGET_TAB_GRID).lines
    plan = budget_sync.plan_budget_sync(_TS_GRID, lines, fy=2027)
    # amount is column E (5th) on the Specialist row, which is sheet row 3 in _TS_GRID.
    assert plan.cell_updates["E3"] == "150000"
    # The note change targets column N (14th) on the Walk-A-Thon row (sheet row 2).
    assert plan.cell_updates["N2"] == "spring walkathon"
    # NO update touches any enrichment column (K/L/M) or the FY2026 / actual rows.
    assert not any(a1[0] in {"K", "L", "M"} for a1 in plan.cell_updates)


def test_plan_never_touches_other_year_or_actuals() -> None:
    # A tab line whose (type, item) also exists in FY2026 / as an actual must only match the
    # FY2027 proposed row (sheet row 3), never rows 5 (FY2026) or 6 (actual).
    lines = budget_sync.parse_budget_tab(_BUDGET_TAB_GRID).lines
    plan = budget_sync.plan_budget_sync(_TS_GRID, lines, fy=2027)
    touched_rows = {int("".join(ch for ch in a1 if ch.isdigit())) for a1 in plan.cell_updates}
    assert 5 not in touched_rows  # FY2026 row
    assert 6 not in touched_rows  # FY2027 actual row


def test_plan_seeded_tab_is_all_unchanged() -> None:
    # A budget tab that mirrors the DB exactly (round-trip) yields zero writes.
    seeded = [
        ["Item", "Proposed $", "Notes"],
        ["INCOME", "", ""],
        ["Fundraising Events", "", ""],
        ["Walk-A-Thon Income", "25000", ""],
        ["EXPENSE", "", ""],
        ["Core Commitments", "", ""],
        ["Specialist Teacher Contracts Expenses", "146414", "keep me"],
        ["PayPal Fees", "200", ""],
    ]
    plan = budget_sync.plan_budget_sync(
        _TS_GRID, budget_sync.parse_budget_tab(seeded).lines, fy=2027
    )
    assert not plan.has_writes()
    assert plan.unchanged == 3
    assert plan.changed == [] and plan.added == [] and plan.notes_changed == []


def test_plan_raises_on_missing_required_column() -> None:
    bad = [["fiscal_year", "type", "measure", "amount", "raw_category"]]  # no category_group/notes
    with pytest.raises(ValueError, match="missing required column"):
        budget_sync.plan_budget_sync(bad, [], fy=2027)


def test_fmt_amount_matches_plain_number_style() -> None:
    assert budget_sync._fmt_amount(70000.0) == "70000"
    assert budget_sync._fmt_amount(3287.70) == "3287.7"
    assert budget_sync._fmt_amount(20023.98) == "20023.98"


# --------------------------------------------------------------------- parser robustness (review)


def test_real_items_starting_with_total_net_subtotal_survive() -> None:
    # A legitimate line whose NAME begins with a rollup word must NOT be swallowed as a rollup
    # (only anchored "Subtotal — "/"TOTAL INCOME|EXPENSE"/"NET (...)" shapes are rollups).
    grid = [
        ["INCOME", "", ""],
        ["Fundraising Events", "", ""],
        ["Net Store Sales", "800", "wash account"],
        ["Total Rewards Program Income", "1200", "gift cards"],
        ["Subtotals Software Rebate", "50", ""],
        ["Subtotal — Fundraising Events", "=SUM(B3:B5)", ""],  # this IS a rollup
        ["TOTAL INCOME", "2050", ""],  # this IS a rollup
    ]
    items = [x.item for x in budget_sync.parse_budget_tab(grid).lines]
    assert items == ["Net Store Sales", "Total Rewards Program Income", "Subtotals Software Rebate"]


def test_placeholder_amount_is_reported_not_mis_grouped() -> None:
    # A data row with a non-numeric amount ("TBD") must be SKIPPED (reported), and must NOT
    # become a sub-header — the line after it stays in the correct category_group.
    grid = [
        ["EXPENSE", "", ""],
        ["Programs", "", ""],
        ["Art supplies", "100", ""],
        ["Music program", "TBD", ""],
        ["Field trips", "250", ""],
    ]
    parsed = budget_sync.parse_budget_tab(grid)
    assert ("Music program", "TBD") in parsed.skipped
    by_item = {x.item: x for x in parsed.lines}
    assert "Music program" not in by_item  # dropped from data, but reported in skipped
    assert by_item["Field trips"].category_group == "Programs"  # NOT "Music program"


def test_group_named_income_does_not_flip_type() -> None:
    # A category_group literally titled "Income" (mixed case) must not be read as a section banner.
    grid = [
        ["EXPENSE", "", ""],
        ["Core", "", ""],
        ["Rent", "500", ""],
        ["Income", "", ""],  # a group named "Income" — NOT the INCOME section
        ["Bank fees", "40", ""],
    ]
    by_item = {x.item: x for x in budget_sync.parse_budget_tab(grid).lines}
    assert by_item["Bank fees"].type == "expense"  # type NOT flipped to income
    assert by_item["Bank fees"].category_group == "Income"


def test_orphan_row_before_any_section_is_reported() -> None:
    grid = [
        ["Book Fair Income", "4000", ""],  # before any section banner
        ["INCOME", "", ""],
        ["Grp", "", ""],
        ["Dues", "10", ""],
    ]
    parsed = budget_sync.parse_budget_tab(grid)
    assert ("Book Fair Income", "4000") in parsed.orphaned
    assert [x.item for x in parsed.lines] == ["Dues"]
    assert parsed.section_count == 1


# --------------------------------------------------------------------- planner robustness (review)


def test_case_or_whitespace_only_edit_is_not_a_rename() -> None:
    # Retyping "Walk-A-Thon Income" as "Walk-a-thon  Income" (case + double space) with the SAME
    # amount must match the existing DB row (unchanged), NOT fork into remove+add.
    tab = [
        ["INCOME", "", ""],
        ["Fundraising Events", "", ""],
        ["Walk-a-thon  Income", "25000", ""],
    ]
    plan = budget_sync.plan_budget_sync(_TS_GRID, budget_sync.parse_budget_tab(tab).lines, fy=2027)
    assert plan.added == []
    assert not any("walk" in item.lower() for _t, item, _a in plan.removed)
    assert plan.unchanged == 1


def test_duplicate_tab_lines_are_flagged_and_collapse_to_one() -> None:
    # The same (type, item) twice on the tab -> flagged as a duplicate, first kept, no double write.
    tab = [
        ["EXPENSE", "", ""],
        ["Core Commitments", "", ""],
        ["PayPal Fees", "250", ""],
        ["PayPal Fees", "300", ""],  # duplicate
    ]
    plan = budget_sync.plan_budget_sync(_TS_GRID, budget_sync.parse_budget_tab(tab).lines, fy=2027)
    assert ("expense", "PayPal Fees") in plan.duplicates
    assert list(plan.cell_updates.keys()) == ["E4"]  # exactly one cell, first occurrence (250)
    assert plan.cell_updates["E4"] == "250"


def test_suspected_rename_is_surfaced() -> None:
    # A word-level rename (Specialist Teacher Contracts Expenses -> ... Contract Expense) shows as
    # remove+add AND is flagged as a suspected rename so the operator won't delete the tagged row.
    tab = [
        ["EXPENSE", "", ""],
        ["Core Commitments", "", ""],
        ["Specialist Teacher Contract Expense", "146414", ""],  # renamed (dropped an 's' x2)
        ["PayPal Fees", "200", ""],
    ]
    plan = budget_sync.plan_budget_sync(_TS_GRID, budget_sync.parse_budget_tab(tab).lines, fy=2027)
    olds = {old for _t, old, _new in plan.suspected_renames}
    assert "Specialist Teacher Contracts Expenses" in olds


# --------------------------------------------------------------------- code-quality-rule guards


def test_required_cols_reference_report_source_constants_by_identity() -> None:
    # `is`, not `==`: guards against a future dev re-duplicating column-name literals in
    # budget_sync (workspace code-quality rule: one source of truth for data-shape constants).
    expected = (
        report_source.FISCAL_YEAR,
        report_source.TYPE,
        report_source.MEASURE,
        report_source.AMOUNT,
        report_source.RAW_CATEGORY,
        report_source.CATEGORY_GROUP,
        report_source.NOTES,
    )
    assert len(budget_sync._REQUIRED_COLS) == len(expected)
    for actual, src in zip(budget_sync._REQUIRED_COLS, expected, strict=True):
        assert actual is src  # same string object, not merely equal


def test_appended_row_round_trips_through_report_source() -> None:
    # A row sync-budget APPENDS must be readable by report_source.to_inputs (the consumer
    # report/analyze use) — the producer->consumer round trip mocks can't see (code-quality rule:
    # audit wire shape when storage representation changes).
    lines = budget_sync.parse_budget_tab(_BUDGET_TAB_GRID).lines
    plan = budget_sync.plan_budget_sync(_TS_GRID, lines, fy=2027)
    applied_grid = [list(r) for r in _TS_GRID] + [list(r) for r in plan.append_rows]
    header = applied_grid[0]
    rows = [dict(zip(header, r, strict=False)) for r in applied_grid[1:]]
    budget_rows, _ = report_source.to_inputs(rows, start_month=7, fy=2027)
    art_id = ids.budget_id(2027, "Art Club Expenses")
    by_id = {r["id"]: r for r in budget_rows}
    assert art_id in by_id, "a sync-appended proposed line must be visible to report/analyze"
    assert by_id[art_id]["category"] == "Art Club Expenses"
    assert by_id[art_id]["budgeted_amount"] == "500"


# --------------------------------------------------------------------------- CLI integration

_CONFIG_TEXT = """\
[organization]
name = "Example PTA"
school_name = "Example Elementary"
school_email = "office@example.org"

[contacts]
president = ["president@example.org"]
treasurer = "treasurer@example.org"
cfo = "cfo@example.org"
account_holders = ["president@example.org"]

[fiscal_year]
start_month = 7

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


def _write_config(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(_CONFIG_TEXT, encoding="utf-8")
    return p


class FakeSyncClient:
    """A fake SheetsClient serving both grids + recording the schema-independent writes."""

    instances: list[FakeSyncClient] = []

    def __init__(self, config: Config, **_: object) -> None:
        self.config = config
        self.update_cells_calls: list[tuple[str, Mapping[str, str]]] = []
        self.append_calls: list[tuple[str, Sequence[Sequence[str]]]] = []
        self.read_values_calls: list[str] = []
        FakeSyncClient.instances.append(self)

    def read_values(self, tab: str) -> list[list[str]]:
        self.read_values_calls.append(tab)
        if tab == budget_sync.budget_tab_name(2027):
            return [list(r) for r in _BUDGET_TAB_GRID]
        if tab == report_source.BUDGET_TIMESERIES_TAB:
            return [list(r) for r in _TS_GRID]
        return []

    def update_cells(self, tab: str, cell_values: Mapping[str, str]) -> None:
        self.update_cells_calls.append((tab, dict(cell_values)))

    def append_raw_rows(self, tab: str, rows: Sequence[Sequence[str]]) -> None:
        self.append_calls.append((tab, [list(r) for r in rows]))


def test_cli_sync_budget_dry_run_makes_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeSyncClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeSyncClient)
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)

    rc = cli.main(["sync-budget", "--fy", "2027", "--config", str(config_path)])

    assert rc == 0
    (client,) = FakeSyncClient.instances
    assert client.update_cells_calls == []  # NO writes
    assert client.append_calls == []
    assert not (tmp_path / "snapshots").exists()  # NO snapshot on a dry run
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "1 amount change(s)" in out
    assert "1 new line(s)" in out


def test_cli_sync_budget_apply_writes_through_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--apply reconciles end-to-end: snapshot first, then targeted cell updates + append."""
    FakeSyncClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeSyncClient)
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)

    rc = cli.main(["sync-budget", "--fy", "2027", "--apply", "--config", str(config_path)])

    assert rc == 0
    (client,) = FakeSyncClient.instances
    # both grids were read.
    assert budget_sync.budget_tab_name(2027) in client.read_values_calls
    assert report_source.BUDGET_TIMESERIES_TAB in client.read_values_calls

    # a faithful snapshot of the Budget Timeseries was written BEFORE the write.
    snap_root = tmp_path / "snapshots"
    assert snap_root.is_dir()
    (run_dir,) = list(snap_root.iterdir())
    snap_csv = run_dir / f"{report_source.BUDGET_TIMESERIES_TAB}.csv"
    assert snap_csv.is_file()
    # the snapshot keeps ALL 14 columns (enrichment not dropped).
    assert snap_csv.read_text(encoding="utf-8").splitlines()[0].count(",") == len(_TS_HEADER) - 1

    # targeted cell update to the amount cell E3, on the Budget Timeseries tab.
    (upd_tab, updates) = client.update_cells_calls[0]
    assert upd_tab == report_source.BUDGET_TIMESERIES_TAB
    assert updates["E3"] == "150000"

    # the new Art Club line was appended as a full-width (14-col) row.
    (app_tab, rows) = client.append_calls[0]
    assert app_tab == report_source.BUDGET_TIMESERIES_TAB
    (new_row,) = rows
    assert len(new_row) == len(_TS_HEADER)
    assert new_row[_TS_HEADER.index("raw_category")] == "Art Club Expenses"
    assert new_row[_TS_HEADER.index("amount")] == "500"
    assert new_row[_TS_HEADER.index("measure")] == "proposed"
    assert new_row[_TS_HEADER.index("category_group")] == "Enrichment & Student Programs"
    # enrichment columns on the new row are blank (flagged for tagging).
    assert new_row[_TS_HEADER.index("strategic_group")] == ""
    assert "applied" in capsys.readouterr().out


def test_cli_sync_budget_empty_tab_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _EmptyTabClient(FakeSyncClient):
        def read_values(self, tab: str) -> list[list[str]]:
            self.read_values_calls.append(tab)
            return []

    FakeSyncClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", _EmptyTabClient)
    monkeypatch.chdir(tmp_path)
    config_path = _write_config(tmp_path)

    rc = cli.main(["sync-budget", "--fy", "2027", "--config", str(config_path)])

    assert rc == 1
    assert "empty or missing" in capsys.readouterr().out
