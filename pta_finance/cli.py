"""``pta-finance`` command-line entry point (stdlib ``argparse``).

Wired subcommands:

    check      Step 3 — validate config + sheet schema; round-trip a test row (test sheet)
    snapshot   Step 3 — export CSV backups of all tabs under ``snapshots/<utc>/``
    normalize  Step 4 — normalize legacy ledger -> canonical schema (snapshot first)

Placeholder subcommands (later steps):

    analyze    Step 5 (analytics)   — run analytics
    report     Step 6 (reports)     — generate monthly report(s)
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pta_finance import backup, etl, schema
from pta_finance.config import Config, load_config
from pta_finance.sheets import SheetsClient


def _load(args: argparse.Namespace) -> Config:
    """Load the typed config from ``--config`` (default ``config.toml`` in cwd)."""
    return load_config(Path(args.config))


def _cmd_check(args: argparse.Namespace) -> int:
    """Validate config + every tab's schema, then round-trip one row on the TEST sheet.

    The round-trip (write -> read-back -> delete) runs only when a non-empty
    ``test_spreadsheet_id`` is configured; it targets the throwaway test sheet, never
    the production spreadsheet. Runs live only with real creds (M2); here it is unit
    tested against a mocked client.
    """
    config = _load(args)
    client = SheetsClient(config)
    for tab in schema.TABS:
        client.validate_schema(tab)
    print(f"check: schema OK for {len(schema.TABS)} tab(s) [{config.organization.name}]")

    test_id = config.sheets.test_spreadsheet_id
    if not test_id:
        print("check: no test_spreadsheet_id configured — skipping round-trip")
        return 0

    test_client = SheetsClient(config, spreadsheet_id=test_id)
    tab = schema.TAB_TRANSACTIONS
    columns = schema.TABS[tab]
    probe_id = f"TXN-CHECK-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    probe = {col: "" for col in columns}
    probe["id"] = probe_id

    test_client.upsert_rows(tab, {probe_id: probe})
    rows = test_client.read_tab(tab)
    found = any(row.get("id") == probe_id for row in rows)
    test_client.delete_rows_by_id(tab, [probe_id])
    if not found:
        print(f"check: round-trip FAILED — wrote {probe_id} but did not read it back")
        return 1
    print(f"check: round-trip OK on test sheet (wrote/read/deleted {probe_id})")
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    """Export a CSV snapshot of every tab under ``snapshots/<utc>/``."""
    config = _load(args)
    client = SheetsClient(config)
    dest = Path(args.dest)
    snapshot_dir = backup.snapshot_all_tabs(client, dest)
    print(f"snapshot: wrote {len(schema.TABS)} tab(s) to {snapshot_dir}")
    return 0


def _cmd_normalize(args: argparse.Namespace) -> int:
    """Normalize the ``transactions`` ledger: snapshot first, assign ids, dedup, flag.

    Delegates to :func:`pta_finance.etl.normalize`, which snapshots every tab BEFORE any
    write, runs the pure normalization, then writes only changed rows back row-targeted.
    """
    config = _load(args)
    client = SheetsClient(config)
    result = etl.normalize(client, config, dest_dir=Path(args.dest))
    print(
        "normalize: "
        f"{result.ids_assigned} id(s) assigned, "
        f"{result.duplicates_flagged} duplicate(s) flagged, "
        f"{result.malformed_flagged} malformed row(s) flagged, "
        f"{result.unchanged} unchanged"
    )
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    print("analyze: not yet implemented (Step 5)")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    print("report: not yet implemented (Step 6)")
    return 0


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default="config.toml",
        help="path to the private config.toml (default: ./config.toml)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="pta-finance",
        description=(
            "Finance toolkit for a PTA / booster club / small nonprofit "
            "(Google Sheet as system-of-record)."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    p_check = sub.add_parser("check", help="validate config + sheet schema (round-trip smoke)")
    _add_config_arg(p_check)
    p_check.set_defaults(func=_cmd_check)

    p_snapshot = sub.add_parser("snapshot", help="export CSV backups of all tabs")
    _add_config_arg(p_snapshot)
    p_snapshot.add_argument(
        "--dest",
        default=".",
        help="base directory for snapshots/<utc>/ output (default: .)",
    )
    p_snapshot.set_defaults(func=_cmd_snapshot)

    p_normalize = sub.add_parser(
        "normalize", help="normalize legacy/raw ledger -> canonical schema (assign IDs, dedup)"
    )
    _add_config_arg(p_normalize)
    p_normalize.add_argument(
        "--dest",
        default=".",
        help="base directory for the pre-write snapshots/<utc>/ backup (default: .)",
    )
    p_normalize.set_defaults(func=_cmd_normalize)

    p_analyze = sub.add_parser("analyze", help="run analytics over the ledger")
    p_analyze.add_argument("--fy", type=int, default=None, help="fiscal-year label, e.g. 2026")
    p_analyze.set_defaults(func=_cmd_analyze)

    p_report = sub.add_parser("report", help="generate monthly report(s)")
    p_report.add_argument("--month", default=None, help="report month, format YYYY-MM")
    p_report.add_argument(
        "--variant",
        choices=("internal", "external", "both"),
        default="both",
        help="report variant to generate (default: both)",
    )
    p_report.set_defaults(func=_cmd_report)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code (0 = ok, 1 = error)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 1
    func = args.func
    result: int = func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
