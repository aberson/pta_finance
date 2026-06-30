"""CSV snapshot export of tabs — the corruption-protection safety net.

:func:`snapshot_all_tabs` reads a set of tabs through a
:class:`~pta_finance.sheets.SheetsClient` and writes one ``<tab>.csv`` per tab under
``snapshots/<utc-timestamp>/``. The toolkit
runs this *before* any mutating operation so a bad write can be reconstructed from the
last snapshot (Sheets version history is the automatic primary net; these CSVs are the
belt-and-suspenders copy). See plan.md §3 "Corruption protection".

By default it snapshots the **live** set — :data:`schema.REQUIRED_TABS` plus the operator-
maintained "Budget Timeseries" tab — and silently SKIPS any tab absent from the spreadsheet,
so the snapshot keeps working once the unused canonical tabs are deleted. Legacy callers
(``etl.normalize`` / ``import-budget``) pass ``tabs=schema.TABS`` to snapshot every canonical
tab before mutating them.

The snapshot is read-only with respect to Google: it issues only reads via the client.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from gspread.exceptions import WorksheetNotFound

from pta_finance import report_source, schema
from pta_finance.sheets import SheetsClient

__all__ = [
    "snapshot_all_tabs",
    "LIVE_SNAPSHOT_TABS",
]

# The default snapshot set: the live-required tab(s) plus the operator-maintained
# "Budget Timeseries" source. A tab in this set that the spreadsheet doesn't have is skipped.
LIVE_SNAPSHOT_TABS: tuple[str, ...] = (
    *schema.REQUIRED_TABS,
    report_source.BUDGET_TIMESERIES_TAB,
)


def _utc_stamp() -> str:
    """A filesystem-safe UTC timestamp, e.g. ``2026-06-23T144501Z``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _columns_for(tab: str, sample: list[dict[str, str]]) -> list[str]:
    """The CSV header columns for ``tab``.

    Prefers the canonical column list (``schema.TABS[tab]``) for a canonical tab; falls back
    to :data:`report_source.TIMESERIES_COLUMNS` for the "Budget Timeseries" tab; otherwise
    derives the columns from the first read record's keys (a tab outside both registries).
    """
    canonical = schema.TABS.get(tab)
    if canonical is not None:
        return list(canonical)
    if tab == report_source.BUDGET_TIMESERIES_TAB:
        return list(report_source.TIMESERIES_COLUMNS)
    return list(sample[0]) if sample else []


def snapshot_all_tabs(
    client: SheetsClient,
    dest_dir: Path,
    *,
    timestamp: str | None = None,
    tabs: Iterable[str] | None = None,
) -> Path:
    """Export each requested tab to ``dest_dir/snapshots/<timestamp>/<tab>.csv``.

    Each CSV's header is the tab's column list (canonical via ``schema.TABS[tab]``, or
    :data:`report_source.TIMESERIES_COLUMNS` for the "Budget Timeseries" tab); each data row
    is the tab's records in column order (missing keys -> empty cells). A tab that does not
    exist on the spreadsheet is **skipped** (a :class:`gspread.exceptions.WorksheetNotFound`
    from the read is caught and noted) rather than aborting the snapshot — so the backup keeps
    working once the unused canonical tabs are deleted. Returns the created snapshot directory.

    Parameters
    ----------
    client:
        The :class:`~pta_finance.sheets.SheetsClient` to read through. Tests inject a
        mock with a ``read_tab`` that returns canned records (no live calls).
    dest_dir:
        The base directory; ``snapshots/<timestamp>/`` is created beneath it.
    timestamp:
        Override the snapshot folder name (default: current UTC time). Useful for
        deterministic tests and for a caller that wants one stamp across artifacts.
    tabs:
        The tabs to snapshot (default: :data:`LIVE_SNAPSHOT_TABS` — the live-required tab(s)
        plus the "Budget Timeseries" source). Legacy callers (``etl.normalize`` /
        ``import-budget``) pass ``schema.TABS`` to back up every canonical tab before mutating.
    """
    stamp = timestamp or _utc_stamp()
    snapshot_dir = Path(dest_dir) / "snapshots" / stamp
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    target_tabs = list(tabs) if tabs is not None else list(LIVE_SNAPSHOT_TABS)

    for tab in target_tabs:
        try:
            records = client.read_tab(tab)
        except WorksheetNotFound:
            print(f"snapshot: skipping {tab!r} (tab not present on the spreadsheet)")
            continue
        columns = _columns_for(tab, records)
        out_path = snapshot_dir / f"{tab}.csv"
        with out_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            for record in records:
                writer.writerow([record.get(col, "") for col in columns])

    return snapshot_dir
