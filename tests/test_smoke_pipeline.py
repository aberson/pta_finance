"""End-to-end WIRING gate — the full pipeline, REAL components, in-memory Sheet.

This is a producer -> consumer **drift gate**, distinct from the per-module unit tests (which
mock their own boundaries) and from the M3 operator observation run (which uses a real Sheet).
It runs the whole pipeline once, through the **real** :class:`~pta_finance.sheets.SheetsClient`
wired to the conftest in-memory ``gspread`` fake (``FakeClient`` / ``FakeSpreadsheet`` /
``FakeWorksheet``), and asserts every hop fits together — NOT what any number comes out to.

Hops exercised end-to-end (no module under test is mocked, no live Google call is made):

1. ``etl.normalize(client, config)`` — snapshots ALL five tabs (``backup.snapshot_all_tabs``
   reads every tab in ``schema.TABS``), reads + normalizes ``transactions``, and writes the
   changed rows back through the real ``SheetsClient`` (``upsert_rows`` +
   ``update_rows_by_index``).
2. ``client.read_tab(transactions)`` -> ``analytics.build_frame`` -> the five aggregations
   (``totals``, ``by_category``, ``by_grade``, ``by_month``, ``budget_vs_actual``) + trends
   (``fundraising_and_spend_by_year``, ``year_over_year``).
3. ``reports.build_internal_report`` / ``build_external_report`` -> ``render_internal`` /
   ``render_external``.

Wiring assertions only: the pipeline raises nothing; both HTML strings are non-empty and carry
the expected structural section markers; and the external HTML does NOT contain the fixture
payee (the PII boundary holds through the whole chain). No dollar totals are asserted — that is
``test_analytics.py`` / ``test_reports.py``'s job.

Identity is obviously-fake placeholders only (``Example PTA``, ``treasurer@example.org``); the
recognizable fixture payee ``Acme Supply Co`` is a sentinel that must never reach external
output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gspread.utils import a1_range_to_grid_range

from pta_finance import analytics, etl, reports, schema
from pta_finance.config import Config
from pta_finance.sheets import SheetsClient
from tests.conftest import FakeClient, FakeSpreadsheet, FakeWorksheet

_TXN_COLS = schema.TRANSACTIONS_COLUMNS
_BUD_COLS = schema.BUDGET_COLUMNS

_PAYEE = "Acme Supply Co"  # sentinel: appears internally, must NEVER reach external output
_FY = 2026


class _ApplyingWorksheet(FakeWorksheet):
    """A ``FakeWorksheet`` whose ``batch_update`` actually APPLIES the range writes to its grid.

    The conftest ``FakeWorksheet`` records ``batch_update`` calls without mutating the grid —
    perfect for the sheets unit tests that assert on the recorded payload, but it means a write
    is invisible to a later read. For an honest END-TO-END gate the in-memory sheet must behave
    like a real one: a row ``etl.normalize`` writes back (e.g. a malformed row's ``needs_review``
    flag, written by sheet position) must be visible when the pipeline reads the tab again. This
    subclass parses each request's A1 ``range`` and writes its values into the grid (growing it
    for appended rows), so ``read_tab`` after ``normalize`` returns the NORMALIZED ledger — which
    is what the analytics + reports hops must consume. The conftest call-recording is preserved
    (``super().batch_update`` still runs), so this never changes conftest behavior.
    """

    def batch_update(self, data: Any) -> dict[str, Any]:
        requests = [dict(req) for req in data]
        result = super().batch_update(requests)  # record the call exactly as conftest does
        for req in requests:
            grid_range = a1_range_to_grid_range(req["range"])
            start_row = grid_range["startRowIndex"]
            start_col = grid_range["startColumnIndex"]
            for r_offset, values_row in enumerate(req["values"]):
                self._set_row(start_row + r_offset, start_col, [str(v) for v in values_row])
        return result

    def _set_row(self, row_index: int, start_col: int, values: list[str]) -> None:
        """Write ``values`` into ``grid[row_index]`` starting at ``start_col`` (0-based), growing
        the grid / row with empty cells as needed so an appended row lands cleanly."""
        ncols = len(self.grid[0]) if self.grid else (start_col + len(values))
        while len(self.grid) <= row_index:
            self.grid.append([""] * ncols)
        row = self.grid[row_index]
        if len(row) < ncols:
            row.extend([""] * (ncols - len(row)))
        for c_offset, value in enumerate(values):
            row[start_col + c_offset] = value


def _grid(columns: tuple[str, ...], rows: list[dict[str, str]]) -> list[list[str]]:
    """Build a fake-worksheet grid: header row (schema columns) then each row in column order."""
    grid: list[list[str]] = [list(columns)]
    grid.extend([row.get(col, "") for col in columns] for row in rows)
    return grid


def _txn(**overrides: str) -> dict[str, str]:
    row = {col: "" for col in _TXN_COLS}
    row.update(overrides)
    return row


def _bud(**overrides: str) -> dict[str, str]:
    row = {col: "" for col in _BUD_COLS}
    row.update(overrides)
    return row


def _legacy_transactions() -> list[dict[str, str]]:
    """A small, realistic legacy ``transactions`` grid mixing every shape the ETL must handle.

    * a row that ALREADY has a canonical id (must be left untouched),
    * an id-less but valid row (gets a fresh id),
    * an EXACT duplicate of it (same date/amount/payee -> flagged ``needs_review``),
    * ONE malformed row (un-parseable amount -> flagged, never crashes the run).

    Two grades (``3`` and unassigned) and an income + expense mix exercise the by-grade /
    by-category / fundraising paths downstream.
    """
    return [
        # 1. Existing-id income row (fundraiser) — id must survive normalize unchanged.
        _txn(
            id="TXN-FY26-0001",
            date="2026-06-03",
            fiscal_year="2026",
            type="income",
            amount="500.00",
            category="fundraiser",
            payee=_PAYEE,
            memo="bake sale deposit",
            entered_by="treasurer@example.org",
        ),
        # 2. Id-less valid expense (grade 3) — ETL assigns the next FY26 id.
        _txn(
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
        # 3. Exact duplicate of (2) — same date/amount/payee -> flagged needs_review, not dropped.
        _txn(
            date="2026-06-12",
            fiscal_year="2026",
            type="expense",
            amount="120.00",
            category="supplies",
            grade="3",
            payee=_PAYEE,
            memo="classroom supplies",
        ),
        # 4. Malformed amount — flagged needs_review, skipped for id/dedup, must NOT crash.
        _txn(
            date="2026-06-20",
            fiscal_year="2026",
            type="expense",
            amount="not-a-number",
            category="supplies",
            payee="Bad Amount LLC",
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


def _build_client(config: Config) -> SheetsClient:
    """A REAL :class:`SheetsClient` wired to an in-memory five-tab spreadsheet.

    Every tab gets its canonical header row from ``schema.TABS`` so ``validate_schema`` and the
    pre-write snapshot (which reads every tab) both succeed; ``transactions`` and ``budget`` are
    seeded with the fixture grids. The client is constructed with ``gspread_client=`` so
    ``sheets.py`` is genuinely in the loop — no live ``gspread.service_account`` call.
    """
    seeded = {
        schema.TAB_TRANSACTIONS: _legacy_transactions(),
        schema.TAB_BUDGET: _budget(),
    }
    worksheets = {
        tab: _ApplyingWorksheet(_grid(columns, seeded.get(tab, [])))
        for tab, columns in schema.TABS.items()
    }
    spreadsheet = FakeSpreadsheet(worksheets)
    fake_client = FakeClient(spreadsheet)
    return SheetsClient(config, gspread_client=fake_client)  # type: ignore[arg-type]


def test_full_pipeline_wires_end_to_end(fake_config: Config, tmp_path: Path) -> None:
    """The whole Sheet -> ETL -> analytics -> reports chain runs once with no exception, and
    the rendered HTML carries the expected structural sections with the PII boundary intact."""
    client = _build_client(fake_config)

    # All five tabs validate against the canonical schema before anything mutates (proves the
    # in-memory spreadsheet is shaped exactly like a real one the rest of the chain expects).
    for tab in schema.TABS:
        client.validate_schema(tab)

    # --- Hop 1: real ETL through the real SheetsClient (snapshots, normalizes, writes back). ---
    result = etl.normalize(client, fake_config, dest_dir=tmp_path)
    # The snapshot of every tab landed on disk BEFORE any write (corruption-protection wiring).
    snap_root = tmp_path / "snapshots"
    assert snap_root.is_dir()
    snap_dirs = list(snap_root.iterdir())
    assert snap_dirs, "normalize must snapshot before writing"
    snapshot_files = {p.name for p in snap_dirs[0].iterdir()}
    assert snapshot_files == {f"{tab}.csv" for tab in schema.TABS}
    # ETL touched the fixture as expected (wiring sanity, not business logic): the id-less valid
    # row got an id, its duplicate + the malformed row were flagged.
    assert result.ids_assigned >= 1
    assert result.malformed_flagged == 1
    assert result.duplicates_flagged == 1

    # --- Hop 2: read the normalized ledger back THROUGH the client, then run analytics. ---
    txn_rows = client.read_tab(schema.TAB_TRANSACTIONS)
    budget_rows = client.read_tab(schema.TAB_BUDGET)
    assert txn_rows, "normalized transactions read back empty"

    start_month = fake_config.fiscal_year.start_month
    built = analytics.build_frame(txn_rows, start_month=start_month)
    frame = built.frame
    # The needs_review rows (duplicate + malformed) are excluded from the analytics frame.
    assert built.excluded_needs_review >= 2

    # Every aggregation + trend runs over the real frame without raising (the drift gate).
    _ = analytics.totals(frame)
    _ = analytics.by_category(frame)
    _ = analytics.by_grade(frame)
    _ = analytics.by_month(frame)
    _ = analytics.budget_vs_actual(frame, budget_rows, 2026)
    _ = analytics.fundraising_and_spend_by_year(frame)
    _ = analytics.year_over_year(frame)

    # --- Hop 3: build both report variants from the SAME normalized rows, then render. ---
    internal = reports.build_internal_report(fake_config, _FY, txn_rows, budget_rows)
    external = reports.build_external_report(fake_config, _FY, txn_rows, budget_rows)

    internal_html = reports.render_internal(internal)
    external_html = reports.render_external(external)

    # Both rendered to non-empty HTML documents.
    assert internal_html.strip().startswith("<!DOCTYPE html>")
    assert external_html.strip().startswith("<!DOCTYPE html>")

    # Structural section markers present (the producer/consumer shape the templates expect).
    for marker in (
        'data-section="totals"',
        'data-section="by-grade"',
        'data-section="by-category"',
        'data-section="transactions"',
        'data-section="fundraising"',
        'data-section="budget-headline"',
    ):
        assert marker in internal_html, f"internal report missing section: {marker}"
    for marker in (
        'data-section="totals"',
        'data-section="by-grade"',
        'data-section="fundraising"',
        'data-section="budget-headline"',
    ):
        assert marker in external_html, f"external report missing section: {marker}"

    # Org identity from config (not hard-coded) flows through to both renders.
    assert fake_config.organization.name in internal_html
    assert fake_config.organization.name in external_html

    # PII boundary holds end-to-end: the payee appears internally but NEVER in the public HTML.
    assert _PAYEE in internal_html, "internal report should carry the fixture payee (full detail)"
    assert _PAYEE not in external_html, "PII LEAK: fixture payee reached the external report"
    # The external variant also omits the per-category breakdown + the transaction table.
    assert 'data-section="by-category"' not in external_html
    assert 'data-section="transactions"' not in external_html
