"""Tests for pta_finance.reports — both variants render, external is PII-free, autoescape.

No live Google calls and no real WeasyPrint: the report data model is built from a small
hand-built ledger fixture via the real ``build_internal_report`` / ``build_external_report``,
and the CLI integration test monkeypatches ``cli.SheetsClient`` to a fake that records the
``report_log`` append.

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

from pta_finance import cli, reports, schema
from pta_finance.config import Config, load_config
from pta_finance.reports import builder

_TXN_COLS = schema.TRANSACTIONS_COLUMNS
_BUD_COLS = schema.BUDGET_COLUMNS

_PAYEE = "Acme Supply Co"  # a recognizable fixture payee that must NEVER reach external output
_MONTH = "2026-06"

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
    """A June-2026 ledger with income, expense, two grades, payee/memo/receipt PII."""
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
            date="2026-06-12",
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
        # A different month — must be excluded from the June report.
        _txn(
            id="TXN-FY26-0003",
            date="2026-05-20",
            fiscal_year="2026",
            type="expense",
            amount="999.99",
            category="supplies",
            payee="May Vendor",
        ),
    ]


def _budget() -> list[dict[str, str]]:
    return [
        _bud(
            id="BUD-FY26-fundraiser",
            fiscal_year="2026",
            category="fundraiser",
            budgeted_amount="1000.00",
        ),
        _bud(
            id="BUD-FY26-supplies",
            fiscal_year="2026",
            category="supplies",
            budgeted_amount="200.00",
        ),
    ]


# --- both variants render --------------------------------------------------


def test_internal_report_renders_with_expected_sections(tmp_path: Path) -> None:
    config = _config(tmp_path)
    model = reports.build_internal_report(config, _MONTH, _ledger(), _budget())
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
    assert _MONTH in html
    # Full detail: the payee appears in the INTERNAL report (escaped or plain — plain here).
    assert _PAYEE in html
    # June rows only — the May row is excluded.
    assert "TXN-FY26-0001" in html
    assert "TXN-FY26-0003" not in html


def test_external_report_renders_with_expected_sections(tmp_path: Path) -> None:
    config = _config(tmp_path)
    model = reports.build_external_report(config, _MONTH, _ledger(), _budget())
    html = reports.render_external(model)

    for marker in (
        'data-section="totals"',
        'data-section="by-grade"',
        'data-section="fundraising"',
        'data-section="budget-headline"',
    ):
        assert marker in html
    assert "Example PTA" in html
    assert _MONTH in html
    # External never carries the per-category breakdown or any transaction table.
    assert 'data-section="by-category"' not in html
    assert 'data-section="transactions"' not in html


def test_report_totals_match_the_month(tmp_path: Path) -> None:
    """The June frame: income 500.00, expense 120.00 (the May 999.99 row is excluded)."""
    config = _config(tmp_path)
    model = reports.build_internal_report(config, _MONTH, _ledger(), _budget())
    assert model.totals.income == Decimal("500.00")
    assert model.totals.expense == Decimal("120.00")
    assert model.totals.net == Decimal("380.00")
    # Fundraising: 500 raised of 1000 target -> 50.0%.
    assert model.fundraising.raised == Decimal("500.00")
    assert model.fundraising.target == Decimal("1000.00")
    assert model.fundraising.pct == Decimal("50.0")


# --- external is PII-free (a): real output is clean ------------------------


def test_external_model_and_html_carry_no_pii(tmp_path: Path) -> None:
    """The REAL external model has no denylisted field, and its HTML omits the payee."""
    config = _config(tmp_path)
    model = reports.build_external_report(config, _MONTH, _ledger(), _budget())

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
    month: str
    by_grade: tuple[_ContaminatedLeaf, ...]


def test_runtime_guard_raises_on_contaminated_payee() -> None:
    """``_assert_external_safe`` raises ExternalReportPIIError naming ``payee``."""
    contaminated = _ContaminatedExternal(
        organization="Example PTA",
        month=_MONTH,
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
        month: str
        by_grade: tuple[_LeafWithReceipt, ...]

    contaminated = _Ext(
        month=_MONTH, by_grade=(_LeafWithReceipt(grade="K", receipt_url="http://x"),)
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
    model = reports.build_external_report(config, _MONTH, _ledger(), _budget())
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
    model = reports.build_internal_report(config, _MONTH, ledger, _budget())
    html = reports.render_internal(model)
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert "<script>x</script>" not in html


# --- report CLI integration ------------------------------------------------


class FakeReportClient:
    """A fake SheetsClient: serves the ledger/budget reads, records report_log appends."""

    instances: list[FakeReportClient] = []

    def __init__(self, config: Config, **_: object) -> None:
        self.config = config
        self.appends: list[tuple[str, list[dict[str, str]]]] = []
        FakeReportClient.instances.append(self)

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        if tab == schema.TAB_TRANSACTIONS:
            return [dict(r) for r in _ledger()]
        if tab == schema.TAB_BUDGET:
            return [dict(r) for r in _budget()]
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
            "--month",
            _MONTH,
            "--variant",
            "both",
            "--output-dir",
            str(out_base),
        ]
    )

    assert rc == 0
    out_dir = out_base / "reports" / "output"
    internal_path = out_dir / f"{_MONTH}-internal.html"
    external_path = out_dir / f"{_MONTH}-external.html"
    assert internal_path.is_file()
    assert external_path.is_file()

    # Internal contains the payee; external does not (the PII boundary holds end-to-end).
    assert _PAYEE in internal_path.read_text(encoding="utf-8")
    assert _PAYEE not in external_path.read_text(encoding="utf-8")

    # One report_log append with one row per variant.
    (client,) = FakeReportClient.instances
    assert len(client.appends) == 1
    tab, rows = client.appends[0]
    assert tab == schema.TAB_REPORT_LOG
    assert len(rows) == 2
    variants = {row["variant"] for row in rows}
    assert variants == {"internal", "external"}
    for row in rows:
        assert row["month"] == _MONTH
        assert row["output_url"].endswith(f"{_MONTH}-{row['variant']}.html")
        assert set(row) == set(schema.REPORT_LOG_COLUMNS)
    assert "logged 2 run(s)" in capsys.readouterr().out


def test_report_cli_requires_month(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "SheetsClient", FakeReportClient)
    config_path = tmp_path / "config.toml"
    config_path.write_text(_CONFIG_TEXT, encoding="utf-8")

    rc = cli.main(["report", "--config", str(config_path)])

    assert rc == 1
    assert "--month YYYY-MM is required" in capsys.readouterr().out


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
