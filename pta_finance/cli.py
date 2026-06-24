"""``pta-finance`` command-line entry point (stdlib ``argparse``).

Wired subcommands:

    check      Step 3 — validate config + sheet schema; round-trip a test row (test sheet)
    snapshot   Step 3 — export CSV backups of all tabs under ``snapshots/<utc>/``
    normalize  Step 4 — normalize legacy ledger -> canonical schema (snapshot first)
    analyze    Step 5 — run analytics over the ledger; print a readable summary
    report     Step 6 — generate monthly report(s); write HTML to reports/output/, log a run
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from pta_finance import analytics, backup, etl, reports, schema
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
    """Read the ledger (+ budget), build the analytics frame, print a readable summary.

    ``--fy YYYY`` filters every aggregation to that fiscal year; absent, all years are
    included. Rows flagged ``needs_review`` are excluded by :func:`analytics.build_frame`
    (the excluded count is printed). Reads only — never writes the sheet.
    """
    config = _load(args)
    client = SheetsClient(config)
    txn_rows = client.read_tab(schema.TAB_TRANSACTIONS)
    budget_rows = client.read_tab(schema.TAB_BUDGET)

    built = analytics.build_frame(txn_rows, start_month=config.fiscal_year.start_month)
    frame = built.frame
    fy: int | None = args.fy
    if fy is not None:
        frame = frame[frame[analytics.aggregate.FISCAL_YEAR_INT] == fy]

    scope = f"FY{fy}" if fy is not None else "all fiscal years"
    print(f"analyze: {config.organization.name} — {scope}")
    print(f"  rows analyzed: {len(frame)}; excluded (needs_review): {built.excluded_needs_review}")

    tot = analytics.totals(frame)
    print(f"  income:  {tot.income}")
    print(f"  expense: {tot.expense}")
    print(f"  net:     {tot.net}")

    print("  by category:")
    for cat in analytics.by_category(frame):
        print(f"    {cat.category or '(uncategorized)'}: net {cat.net}")

    print("  by grade:")
    for grade in analytics.by_grade(frame):
        print(f"    {grade.grade}: net {grade.net}")

    print("  by month:")
    for month in analytics.by_month(frame):
        print(f"    {month.month.isoformat()}: net {month.net}")

    if fy is not None:
        print(f"  budget vs actual (FY{fy}):")
        for bv in analytics.budget_vs_actual(frame, budget_rows, fy):
            print(
                f"    {bv.category or '(uncategorized)'}: "
                f"budgeted {bv.budgeted}, actual {bv.actual}, variance {bv.variance}"
            )

    print("  fundraising + spend by year:")
    for year in analytics.fundraising_and_spend_by_year(built.frame):
        print(f"    FY{year.fiscal_year}: income {year.income}, expense {year.expense}")

    print("  year-over-year:")
    for yoy in analytics.year_over_year(built.frame):
        inc_pct = "n/a" if yoy.income_pct is None else f"{yoy.income_pct}%"
        exp_pct = "n/a" if yoy.expense_pct is None else f"{yoy.expense_pct}%"
        print(
            f"    FY{yoy.prior_year}->FY{yoy.year}: "
            f"income {yoy.income_change} ({inc_pct}), expense {yoy.expense_change} ({exp_pct})"
        )

    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    """Generate monthly report(s): read the ledger, build, render to HTML, log the run.

    Reads ``transactions`` + ``budget`` (read-only), builds the requested variant(s) via
    :mod:`pta_finance.reports`, renders each to a single self-contained HTML file under
    ``reports/output/<month>-<variant>.html`` (a gitignored dir — reports never enter the
    repo), and appends one row to the ``report_log`` tab per variant (run_at, variant, month,
    output_url=the local path, generated_by). ``--variant both`` emits both files + both log
    rows. The external builder runs its PII guard before rendering.
    """
    config = _load(args)
    month = args.month
    if not month:
        print("report: --month YYYY-MM is required")
        return 1

    client = SheetsClient(config)
    txn_rows = client.read_tab(schema.TAB_TRANSACTIONS)
    budget_rows = client.read_tab(schema.TAB_BUDGET)

    variants = ("internal", "external") if args.variant == "both" else (args.variant,)

    out_dir = Path(args.output_dir) / "reports" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    generated_by = config.contacts.treasurer

    log_rows: list[dict[str, str]] = []
    for variant in variants:
        if variant == "internal":
            model = reports.build_internal_report(config, month, txn_rows, budget_rows)
            html = reports.render_internal(model)
        else:
            ext_model = reports.build_external_report(config, month, txn_rows, budget_rows)
            html = reports.render_external(ext_model)

        out_path = out_dir / f"{month}-{variant}.html"
        out_path.write_text(html, encoding="utf-8")
        print(f"report: wrote {variant} report to {out_path}")

        log_rows.append(
            {
                "run_at": run_at,
                "variant": variant,
                "month": month,
                "output_url": str(out_path),
                "generated_by": generated_by,
            }
        )

    client.append_rows(schema.TAB_REPORT_LOG, log_rows)
    print(f"report: logged {len(log_rows)} run(s) to {schema.TAB_REPORT_LOG}")
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
    _add_config_arg(p_analyze)
    p_analyze.add_argument("--fy", type=int, default=None, help="fiscal-year label, e.g. 2026")
    p_analyze.set_defaults(func=_cmd_analyze)

    p_report = sub.add_parser("report", help="generate monthly report(s)")
    _add_config_arg(p_report)
    p_report.add_argument("--month", default=None, help="report month, format YYYY-MM")
    p_report.add_argument(
        "--variant",
        choices=("internal", "external", "both"),
        default="both",
        help="report variant to generate (default: both)",
    )
    p_report.add_argument(
        "--output-dir",
        default=".",
        help="base dir for the gitignored reports/output/ HTML files (default: .)",
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
