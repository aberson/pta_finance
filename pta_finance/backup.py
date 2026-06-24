"""CSV snapshot export of all tabs — the corruption-protection safety net.

:func:`snapshot_all_tabs` reads every canonical tab through a
:class:`~pta_finance.sheets.SheetsClient` and writes one ``<tab>.csv`` per tab under
``snapshots/<utc-timestamp>/``. The toolkit
runs this *before* any mutating operation so a bad write can be reconstructed from the
last snapshot (Sheets version history is the automatic primary net; these CSVs are the
belt-and-suspenders copy). See plan.md §3 "Corruption protection".

The snapshot is read-only with respect to Google: it issues only reads via the client.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from pta_finance import schema
from pta_finance.sheets import SheetsClient

__all__ = [
    "snapshot_all_tabs",
]


def _utc_stamp() -> str:
    """A filesystem-safe UTC timestamp, e.g. ``2026-06-23T144501Z``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")


def snapshot_all_tabs(
    client: SheetsClient,
    dest_dir: Path,
    *,
    timestamp: str | None = None,
) -> Path:
    """Export every canonical tab to ``dest_dir/snapshots/<timestamp>/<tab>.csv``.

    Each CSV's header is the tab's canonical column list (``schema.TABS[tab]``); each
    data row is the tab's records in column order (missing keys -> empty cells). Returns
    the created snapshot directory.

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
    """
    stamp = timestamp or _utc_stamp()
    snapshot_dir = Path(dest_dir) / "snapshots" / stamp
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    for tab, columns in schema.TABS.items():
        records = client.read_tab(tab)
        out_path = snapshot_dir / f"{tab}.csv"
        with out_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            for record in records:
                writer.writerow([record.get(col, "") for col in columns])

    return snapshot_dir
