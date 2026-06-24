"""``pta-finance`` command-line entry point (stdlib ``argparse``).

Subcommands are placeholders in Step 1 — each prints "not yet implemented (Step N)"
and returns 0. They are wired up in later build steps:

    check      Step 3 (sheets client) — validate config + sheet round-trip
    snapshot   Step 3 (backup)        — export CSV backups of all tabs
    normalize  Step 4 (etl)           — legacy ledger -> canonical schema
    analyze    Step 5 (analytics)     — run analytics
    report     Step 6 (reports)       — generate monthly report(s)
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def _cmd_check(args: argparse.Namespace) -> int:
    print("check: not yet implemented (Step 3)")
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    print("snapshot: not yet implemented (Step 3)")
    return 0


def _cmd_normalize(args: argparse.Namespace) -> int:
    print("normalize: not yet implemented (Step 4)")
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    print("analyze: not yet implemented (Step 5)")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    print("report: not yet implemented (Step 6)")
    return 0


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
    p_check.set_defaults(func=_cmd_check)

    p_snapshot = sub.add_parser("snapshot", help="export CSV backups of all tabs")
    p_snapshot.set_defaults(func=_cmd_snapshot)

    p_normalize = sub.add_parser(
        "normalize", help="normalize legacy/raw ledger -> canonical schema (assign IDs, dedup)"
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
