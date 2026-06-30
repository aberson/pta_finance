"""Tests for pta_finance.reports — both variants render, external is PII-free, autoescape.

No live Google calls and no real WeasyPrint: the report data model is built from a small
hand-built ledger fixture via the real ``build_internal_report`` / ``build_external_report``
(now FISCAL-YEAR scoped), and the CLI integration test monkeypatches ``cli.SheetsClient`` to
a fake whose ``read_values("Budget Timeseries")`` serves a long-dataset grid.

The two PII tests are both required by the security invariant:

* the REAL external model + rendered HTML carry no denylisted field and no fixture payee, and
* the runtime guard ``_assert_external_safe`` RAISES ``ExternalReportPIIError`` when handed an
  external model artificially contaminated with a ``payee`` / ``receipt_url`` field.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from pta_finance import cli, report_source, reports, schema
from pta_finance.config import Config, load_config
from pta_finance.reports import builder

_TXN_COLS = schema.TRANSACTIONS_COLUMNS
_BUD_COLS = schema.BUDGET_COLUMNS

_PAYEE = "Acme Supply Co"  # a recognizable fixture payee that must NEVER reach external output
_FY = 2026

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


def _config(tmp_path: Path) -> Config:
    p = tmp_path / "config.toml"
    p.write_text(_CONFIG_TEXT, encoding="utf-8")
    return load_config(p)


def _txn(**ov: str) -> dict[str, str]:
    row = {c: "" for c in _TXN_COLS}
    row.update(ov)
    return row


def _bud(**ov: str) -> dict[str, str]:
    row = {c: "" for c in _BUD_COLS}
    row.update(ov)
    return row


def _ledger() -> list[dict[str, str]]:
    """An FY2026 ledger with income, expense, two grades, payee/memo/receipt PII.

    The last row is in FY2025 and must be EXCLUDED from the FY2026 report (fiscal-year scope).
    """
    return [
        _txn(
            id="TXN-FY26-0001",
            date="2026-06-05",
            fiscal_year="2026",
            type="income",
            amount="500.00",
            category="fundraiser",
            payee=_PAYEE,
            memo="bake sale deposit",
            entered_by="treasurer@example.org",
        ),
        _txn(
            id="TXN-FY26-0002",
            date="2026-02-12",
            fiscal_year="2026",
            type="expense",
            amount="120.00",
            category="supplies",
            grade="3",
            payee=_PAYEE,
            memo="classroom supplies",
            receipt_id="RCP-FY26-0001",
            entered_by="treasurer@example.org",
        ),
        # A different fiscal year — must be excluded from the FY2026 report.
        _txn(
            id="TXN-FY25-0003",
            date="2025-05-20",
            fiscal_year="2025",
            type="expense",
            amount="999.99",
            category="supplies",
            payee="Prior Year Vendor",
        ),
    ]


def _budget() -> list[dict[str, str]]:
    # Budget rows tag their income/expense kind in `notes` (as the report source does). The
    # fundraiser line is an INCOME target (feeds fundraising progress); supplies is an EXPENSE
    # target (feeds the budget headline / per-category variance).
    return [
        _bud(
            id="BUD-FY26-fundraiser",
            fiscal_year="2026",
            category="fundraiser",
            budgeted_amount="1000.00",
            notes="income",
        ),
        _bud(
            id="BUD-FY26-supplies",
            fiscal_year="2026",
            category="supplies",
            budgeted_amount="200.00",
            notes="expense",
        ),
    ]


# --- both variants render --------------------------------------------------


def test_internal_report_renders_with_expected_sections(tmp_path: Path) -> None:
    config = _config(tmp_path)
    model = reports.build_internal_report(config, _FY, _ledger(), _budget())
    html = reports.render_internal(model)

    # Section markers present.
    for marker in (
        'data-section="totals"',
        'data-section="by-grade"',
        'data-section="by-category"',
        'data-section="transactions"',
        'data-section="fundraising"',
        'data-section="budget-headline"',
    ):
        assert marker in html
    # Identity from config + the period.
    assert "Example PTA" in html
    assert f"FY{_FY}" in html
    assert f"Fiscal Year {_FY}" in html
    # Full detail: the payee appears in the INTERNAL report (escaped or plain — plain here).
    assert _PAYEE in html
    # FY2026 rows only — the FY2025 row is excluded.
    assert "TXN-FY26-0001" in html
    assert "TXN-FY25-0003" not in html


def test_external_report_renders_with_expected_sections(tmp_path: Path) -> None:
    config = _config(tmp_path)
    model = reports.build_external_report(config, _FY, _ledger(), _budget())
    html = reports.render_external(model)

    for marker in (
        'data-section="totals"',
        'data-section="by-grade"',
        'data-section="fundraising"',
        'data-section="budget-headline"',
    ):
        assert marker in html
    assert "Example PTA" in html
    assert f"Fiscal Year {_FY}" in html
    # External never carries the per-category breakdown or any transaction table.
    assert 'data-section="by-category"' not in html
    assert 'data-section="transactions"' not in html


def test_report_totals_match_the_fiscal_year(tmp_path: Path) -> None:
    """The FY2026 frame: income 500.00, expense 120.00 (the FY2025 999.99 row is excluded)."""
    config = _config(tmp_path)
    model = reports.build_internal_report(config, _FY, _ledger(), _budget())
    assert model.totals.income == Decimal("500.00")
    assert model.totals.expense == Decimal("120.00")
    assert model.totals.net == Decimal("380.00")
    # Fundraising: 500 raised of 1000 target -> 50.0%.
    assert model.fundraising.raised == Decimal("500.00")
    assert model.fundraising.target == Decimal("1000.00")
    assert model.fundraising.pct == Decimal("50.0")


# --- proposed-only fiscal year (budget, zero actuals) ----------------------


def test_proposed_only_fiscal_year_builds_both_variants(tmp_path: Path) -> None:
    """A proposed-only FY (budget rows, NO actuals) builds both variants without raising.

    The likely real-world request: a future/planned FY that has a budget but no spend yet. The
    headline must show the budgeted total with zero spent, and the empty external model must
    still pass the PII guard.
    """
    config = _config(tmp_path)
    # FY2025 budget lines, but NO FY2025 transactions at all (proposed-only year). The headline
    # reflects the EXPENSE budget only (the $150 supplies line); the income line is a target.
    budget_rows = [
        _bud(
            id="BUD-FY25-fundraiser",
            fiscal_year="2025",
            category="fundraiser",
            budgeted_amount="800.00",
            notes="income",
        ),
        _bud(
            id="BUD-FY25-supplies",
            fiscal_year="2025",
            category="supplies",
            budgeted_amount="150.00",
            notes="expense",
        ),
    ]
    txn_rows: list[dict[str, str]] = []

    internal = reports.build_internal_report(config, 2025, txn_rows, budget_rows)
    external = reports.build_external_report(config, 2025, txn_rows, budget_rows)

    # Budgeted total = the EXPENSE budget ($150); the $800 income target is NOT in the headline.
    assert internal.budget_headline.total_budgeted == Decimal("150.00")
    assert internal.budget_headline.total_spent == Decimal("0")
    assert external.budget_headline.total_budgeted == Decimal("150.00")
    assert external.budget_headline.total_spent == Decimal("0")
    # The income target still surfaces as the fundraising target (no actuals -> raised 0).
    assert internal.fundraising.target == Decimal("800.00")
    assert internal.fundraising.raised == Decimal("0.00")
    # No transactions, but rendering still produces both documents (external passed the guard).
    assert internal.transactions == ()
    assert reports.render_internal(internal).startswith("<!DOCTYPE html>")
    assert reports.render_external(external).startswith("<!DOCTYPE html>")


# --- budget headline is EXPENSE-only ---------------------------------------


def test_income_budget_row_does_not_inflate_budget_headline(tmp_path: Path) -> None:
    """An INCOME proposed line (notes=='income') must NOT add to budget_headline.total_budgeted.

    Regression for the income-bearing source: only the expense budget is "budgeted spend"; a
    fundraising-income target would otherwise inflate Total budgeted / Remaining.
    """
    config = _config(tmp_path)
    budget_rows = [
        _bud(
            id="BUD-FY26-walkathon",
            fiscal_year="2026",
            category="Walk-A-Thon Income",
            budgeted_amount="327000.00",  # a large income target
            notes="income",
        ),
        _bud(
            id="BUD-FY26-supplies",
            fiscal_year="2026",
            category="supplies",
            budgeted_amount="200.00",
            notes="expense",
        ),
    ]
    model = reports.build_internal_report(config, _FY, [], budget_rows)
    # Only the $200 expense line is in the headline; the $327k income target is excluded.
    assert model.budget_headline.total_budgeted == Decimal("200.00")


# --- fundraising uses income (all income = fundraising) --------------------


def test_fundraising_progress_uses_income_not_slug_match(tmp_path: Path) -> None:
    """A realistically-named income line ("Walk-A-Thon Income") yields raised>0 AND target>0.

    The old slug heuristic only matched {"fundraiser","fundraising","fundraisers"} and read ~$0
    for real names. Fundraising now = total income actual vs total income proposed.
    """
    config = _config(tmp_path)
    budget_rows = [
        _bud(
            id="BUD-FY26-walkathon",
            fiscal_year="2026",
            category="Walk-A-Thon Income",
            budgeted_amount="1000.00",
            notes="income",
        ),
    ]
    txn_rows = [
        _txn(
            id="TXN-FY26-SUM-walk",
            date="2026-12-31",
            fiscal_year="2026",
            type="income",
            amount="750.00",
            category="Walk-A-Thon Income",
        ),
    ]
    model = reports.build_internal_report(config, _FY, txn_rows, budget_rows)
    assert model.fundraising.raised == Decimal("750.00")
    assert model.fundraising.target == Decimal("1000.00")
    assert model.fundraising.pct == Decimal("75.0")


# --- external is PII-free (a): real output is clean ------------------------


def test_external_model_and_html_carry_no_pii(tmp_path: Path) -> None:
    """The REAL external model has no denylisted field, and its HTML omits the payee."""
    config = _config(tmp_path)
    model = reports.build_external_report(config, _FY, _ledger(), _budget())

    # The assembled external model has NO populated denylisted field anywhere.
    found = _collect_field_names(model)
    leaked = found & reports.EXTERNAL_PII_DENYLIST
    assert leaked == set(), f"external model exposes PII fields: {sorted(leaked)}"

    # The rendered HTML does not contain the fixture payee or the memo text.
    html = reports.render_external(model)
    assert _PAYEE not in html
    assert "bake sale deposit" not in html
    assert "classroom supplies" not in html
    assert "RCP-FY26-0001" not in html


# --- external is PII-free (b): the runtime guard RAISES --------------------


@dataclass(frozen=True)
class _ContaminatedLeaf:
    """A nested object carrying a forbidden field name (simulates a future refactor leak)."""

    grade: str
    payee: str  # <-- denylisted


@dataclass(frozen=True)
class _ContaminatedExternal:
    """An ExternalReport-shaped model whose by_grade leaf smuggles a populated ``payee``."""

    organization: str
    fiscal_year: int
    by_grade: tuple[_ContaminatedLeaf, ...]


def test_runtime_guard_raises_on_contaminated_payee() -> None:
    """``_assert_external_safe`` raises ExternalReportPIIError naming ``payee``."""
    contaminated = _ContaminatedExternal(
        organization="Example PTA",
        fiscal_year=_FY,
        by_grade=(_ContaminatedLeaf(grade="3", payee="Smuggled Vendor"),),
    )
    with pytest.raises(builder.ExternalReportPIIError) as exc:
        builder._assert_external_safe(contaminated)
    assert exc.value.field == "payee"


def test_runtime_guard_raises_on_contaminated_receipt_url() -> None:
    """The guard also catches a populated ``receipt_url`` (second denylist member)."""

    @dataclass(frozen=True)
    class _LeafWithReceipt:
        grade: str
        receipt_url: str

    @dataclass(frozen=True)
    class _Ext:
        fiscal_year: int
        by_grade: tuple[_LeafWithReceipt, ...]

    contaminated = _Ext(
        fiscal_year=_FY, by_grade=(_LeafWithReceipt(grade="K", receipt_url="http://x"),)
    )
    with pytest.raises(builder.ExternalReportPIIError) as exc:
        builder._assert_external_safe(contaminated)
    assert exc.value.field == "receipt_url"


def test_runtime_guard_ignores_empty_pii_field() -> None:
    """A denylisted field that is present but EMPTY is not a leak (no false positive)."""

    @dataclass(frozen=True)
    class _Leaf:
        grade: str
        payee: str

    @dataclass(frozen=True)
    class _Ext:
        by_grade: tuple[_Leaf, ...]

    # payee="" is absent for PII purposes -> no raise.
    builder._assert_external_safe(_Ext(by_grade=(_Leaf(grade="3", payee=""),)))


def test_build_external_report_calls_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_external_report invokes _assert_external_safe before returning (wired guard)."""
    config = _config(tmp_path)
    called: list[object] = []
    real = builder._assert_external_safe

    def _spy(model: object) -> None:
        called.append(model)
        real(model)

    monkeypatch.setattr(builder, "_assert_external_safe", _spy)
    model = reports.build_external_report(config, _FY, _ledger(), _budget())
    assert called == [model]


# --- autoescape ------------------------------------------------------------


def test_internal_html_escapes_script_payee(tmp_path: Path) -> None:
    """A payee of ``<script>x</script>`` renders ESCAPED in the internal HTML, not as a tag."""
    config = _config(tmp_path)
    ledger = [
        _txn(
            id="TXN-FY26-0009",
            date="2026-06-15",
            fiscal_year="2026",
            type="expense",
            amount="10.00",
            category="supplies",
            payee="<script>x</script>",
        )
    ]
    model = reports.build_internal_report(config, _FY, ledger, _budget())
    html = reports.render_internal(model)
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert "<script>x</script>" not in html


# --- report CLI integration (through the production timeseries source) ------

# A recognizable vendor-ish raw_category. In the actual->txn projection this lands as the
# transaction's `payee`, so it must appear in the INTERNAL HTML (full detail) and NEVER in the
# EXTERNAL HTML — the on-disk PII boundary the integration test re-closes.
_VENDOR_CATEGORY = "Acme Supply Co Classroom Spend"

# A "Budget Timeseries" long-dataset grid (header row 0, then data) with obviously-fake
# line items: FY2026 fundraiser income (proposed + actual carrying the payee-as-category) and
# a graded expense whose raw_category is a recognizable vendor name.
_TIMESERIES_GRID: list[list[str]] = [
    [
        report_source.FISCAL_YEAR,
        report_source.CATEGORY_GROUP,
        report_source.TYPE,
        report_source.MEASURE,
        report_source.AMOUNT,
        report_source.IS_FUNDRAISER,
        report_source.GRADE,
        report_source.RAW_CATEGORY,
        report_source.SOURCE_TAB,
    ],
    ["2026", "fundraising", "income", "proposed", "1000.00", "TRUE", "", "fundraiser", "budget"],
    ["2026", "fundraising", "income", "actual", "500.00", "TRUE", "", "fundraiser", "actuals"],
    ["2026", "operations", "expense", "proposed", "200.00", "FALSE", "3", _VENDOR_CATEGORY, "bud"],
    ["2026", "operations", "expense", "actual", "120.00", "FALSE", "3", _VENDOR_CATEGORY, "act"],
]


class FakeReportClient:
    """A fake SheetsClient: serves the "Budget Timeseries" grid, records report_log appends.

    ``read_tabs`` / ``read_values_tabs`` record which tabs were touched so a test can assert
    the canonical transactions/budget tabs are NOT read by ``report``.
    """

    instances: list[FakeReportClient] = []

    def __init__(self, config: Config, **_: object) -> None:
        self.config = config
        self.appends: list[tuple[str, list[dict[str, str]]]] = []
        self.read_tabs: list[str] = []
        self.read_values_tabs: list[str] = []
        FakeReportClient.instances.append(self)

    def read_values(self, tab: str) -> list[list[str]]:
        self.read_values_tabs.append(tab)
        if tab == report_source.BUDGET_TIMESERIES_TAB:
            return [list(r) for r in _TIMESERIES_GRID]
        return []

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        self.read_tabs.append(tab)
        return []

    def append_rows(self, tab: str, rows: list[Mapping[str, str]]) -> None:
        self.appends.append((tab, [dict(r) for r in rows]))


def test_report_cli_writes_output_and_logs_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    FakeReportClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeReportClient)
    config_path = tmp_path / "config.toml"
    config_path.write_text(_CONFIG_TEXT, encoding="utf-8")
    out_base = tmp_path / "work"

    rc = cli.main(
        [
            "report",
            "--config",
            str(config_path),
            "--fy",
            str(_FY),
            "--variant",
            "both",
            "--output-dir",
            str(out_base),
        ]
    )

    assert rc == 0
    out_dir = out_base / "reports" / "output"
    internal_path = out_dir / f"FY{_FY}-internal.html"
    external_path = out_dir / f"FY{_FY}-external.html"
    assert internal_path.is_file()
    assert external_path.is_file()

    (client,) = FakeReportClient.instances
    # Sourced from the "Budget Timeseries" tab; the canonical tabs are NOT read.
    assert client.read_values_tabs == [report_source.BUDGET_TIMESERIES_TAB]
    assert schema.TAB_TRANSACTIONS not in client.read_tabs
    assert schema.TAB_BUDGET not in client.read_tabs

    # FY2026 totals from the timeseries actuals: income 500, expense 120.
    internal_html = internal_path.read_text(encoding="utf-8")
    external_html = external_path.read_text(encoding="utf-8")
    assert "$500.00" in internal_html
    assert "$120.00" in internal_html

    # PII boundary holds through the whole producer -> file path: the vendor-ish payee
    # (raw_category -> txn.payee) appears in the INTERNAL file but NEVER in the EXTERNAL file.
    assert _VENDOR_CATEGORY in internal_html
    assert _VENDOR_CATEGORY not in external_html

    # One report_log append with one row per variant; the `month` column carries FY<fy>.
    assert len(client.appends) == 1
    tab, rows = client.appends[0]
    assert tab == schema.TAB_REPORT_LOG
    assert len(rows) == 2
    variants = {row["variant"] for row in rows}
    assert variants == {"internal", "external"}
    for row in rows:
        assert row["month"] == f"FY{_FY}"
        assert row["output_url"].endswith(f"FY{_FY}-{row['variant']}.html")
        assert set(row) == set(schema.REPORT_LOG_COLUMNS)
    assert "logged 2 run(s)" in capsys.readouterr().out


def test_report_cli_defaults_to_current_fiscal_year_without_fy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --fy: the unattended cron path. report --variant both targets the CURRENT FY.

    With the fake config's calendar fiscal year (start_month=1), the current FY label is just
    today's UTC year — so the written files + the report_log `month` carry that FY.
    """
    from datetime import UTC, datetime

    from pta_finance import ids

    FakeReportClient.instances = []
    monkeypatch.setattr(cli, "SheetsClient", FakeReportClient)
    config_path = tmp_path / "config.toml"
    config_path.write_text(_CONFIG_TEXT, encoding="utf-8")
    out_base = tmp_path / "work"

    rc = cli.main(
        [
            "report",
            "--config",
            str(config_path),
            "--variant",
            "both",
            "--output-dir",
            str(out_base),
        ]
    )

    assert rc == 0
    # start_month=1 in the fake config -> current FY label == today's UTC year.
    current_fy = ids.fiscal_year_label(datetime.now(UTC).date(), 1)
    out_dir = out_base / "reports" / "output"
    assert (out_dir / f"FY{current_fy}-internal.html").is_file()
    assert (out_dir / f"FY{current_fy}-external.html").is_file()

    (client,) = FakeReportClient.instances
    _, rows = client.appends[0]
    assert {row["month"] for row in rows} == {f"FY{current_fy}"}


# --- helpers ---------------------------------------------------------------


def _collect_field_names(model: object) -> set[str]:
    """Recursively collect every dataclass field / mapping key name reachable from ``model``."""
    from dataclasses import fields, is_dataclass

    names: set[str] = set()

    def walk(node: object) -> None:
        if is_dataclass(node) and not isinstance(node, type):
            for f in fields(node):
                names.add(f.name)
                walk(getattr(node, f.name))
        elif isinstance(node, Mapping):
            for k, v in node.items():
                names.add(str(k))
                walk(v)
        elif isinstance(node, (list, tuple, set, frozenset)):
            for item in node:
                walk(item)

    walk(model)
    return names
