"""``pta-finance`` command-line entry point (stdlib ``argparse``).

Wired subcommands:

    check      Step 3 — validate report_log schema + Budget Timeseries source; round-trip a row
    init-sheet bootstrap the spreadsheet with the live-required tab(s) + their schema headers
    snapshot   Step 3 — export CSV backups of the live tab set under ``snapshots/<utc>/``
    normalize  Step 4 — (legacy) normalize legacy ledger -> canonical schema (snapshot first)
    analyze    Step 5 — run analytics over the "Budget Timeseries" tab; print a summary
    report     Step 6 — generate fiscal-year report(s); write HTML to reports/output/, log a run
    import-budget  (legacy) load a messy "budget" worksheet into the canonical budget tab

The LIVE data flow sources ``report`` / ``analyze`` from the operator-maintained "Budget
Timeseries" tab and writes only ``report_log``; ``check`` / ``init-sheet`` / ``snapshot``
provision/validate only :data:`schema.REQUIRED_TABS`. The canonical ``transactions`` /
``receipts`` / ``budget`` / ``events`` tabs (and the ``normalize`` / ``import-budget`` commands
that fill them) are LEGACY — superseded by the Budget Timeseries flow and safe to delete.
"""

from __future__ import annotations

import argparse
import calendar
import csv
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from pta_finance import (
    analytics,
    backup,
    budget_import,
    etl,
    ids,
    models,
    receipt_ingest,
    report_source,
    reports,
    schema,
)
from pta_finance.config import Config, load_config
from pta_finance.sheets import SheetsClient


def _load(args: argparse.Namespace) -> Config:
    """Load the typed config from ``--config`` (default ``config.toml`` in cwd)."""
    return load_config(Path(args.config))


def _cmd_check(args: argparse.Namespace) -> int:
    """Validate the live-required schema + the Budget Timeseries source, then round-trip a row.

    Three checks (the live deployment surface — the unused canonical tabs may be deleted):

    1. **Schema** of every tab in :data:`schema.REQUIRED_TABS` (now just ``report_log``).
    2. **Source readable.** :func:`report_source.read_timeseries` returns a non-empty list and
       its header carries every :data:`report_source.TIMESERIES_COLUMNS` name — the data
       ``report`` / ``analyze`` actually consume. A missing/empty/mis-shaped source returns 1.
    3. **Write round-trip** on the TEST sheet's ``report_log`` (write -> read-back -> delete),
       keyed by a unique ``run_at`` marker (``report_log``'s first column, the upsert/delete
       key — ``SheetsClient`` keys by column 1). Runs only when ``test_spreadsheet_id`` is set;
       it targets the throwaway test sheet, never production. Live only with real creds (M2);
       here it is unit tested against a mocked client.
    """
    config = _load(args)
    client = SheetsClient(config)
    for tab in schema.REQUIRED_TABS:
        client.validate_schema(tab)
    print(
        f"check: schema OK for {len(schema.REQUIRED_TABS)} required tab(s) "
        f"[{config.organization.name}]"
    )

    rows = report_source.read_timeseries(client)
    if not rows:
        print(
            f"check: Budget Timeseries source ({report_source.BUDGET_TIMESERIES_TAB!r}) is "
            "missing or empty — report/analyze have no data to read"
        )
        return 1
    header = set(rows[0])
    missing = [col for col in report_source.TIMESERIES_COLUMNS if col not in header]
    if missing:
        print(
            f"check: Budget Timeseries source ({report_source.BUDGET_TIMESERIES_TAB!r}) is "
            f"missing expected column(s): {', '.join(missing)}"
        )
        return 1
    print(
        f"check: Budget Timeseries source OK ({len(rows)} row(s) in "
        f"{report_source.BUDGET_TIMESERIES_TAB!r})"
    )

    test_id = config.sheets.test_spreadsheet_id
    if not test_id:
        print("check: no test_spreadsheet_id configured — skipping round-trip")
        return 0

    test_client = SheetsClient(config, spreadsheet_id=test_id)
    tab = schema.TAB_REPORT_LOG
    columns = schema.TABS[tab]
    # report_log's first column is ``run_at``; SheetsClient keys upsert/delete by column 1, so
    # the marker is the run_at value.
    marker = f"CHECK-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    probe = {col: "" for col in columns}
    probe["run_at"] = marker

    test_client.upsert_rows(tab, {marker: probe})
    log_rows = test_client.read_tab(tab)
    found = any(row.get("run_at") == marker for row in log_rows)
    test_client.delete_rows_by_id(tab, [marker])
    if not found:
        print(f"check: round-trip FAILED — wrote {marker} but did not read it back")
        return 1
    print(f"check: round-trip OK on test sheet (wrote/read/deleted {marker})")
    return 0


def _cmd_init_sheet(args: argparse.Namespace) -> int:
    """Bootstrap the spreadsheet with the live-required tab(s) + their exact schema headers.

    Iterates :data:`schema.REQUIRED_TABS` (now just ``report_log``) and calls
    :meth:`SheetsClient.ensure_tab` on each, which creates a missing worksheet (sized to the
    schema) and writes its header row, writes the header into an existing tab whose row 1 is
    empty, or no-ops when the header already matches. A pre-existing tab with a non-empty
    mismatched header raises (never clobbered). The unused canonical tabs are NOT created — the
    toolkit sources ``report`` / ``analyze`` from the operator-maintained "Budget Timeseries"
    tab instead.

    ``--target test`` bootstraps ``test_spreadsheet_id`` instead of the production sheet (and
    fails fast when that id is blank). ``--dry-run`` reports the action each tab WOULD take —
    computed from :meth:`SheetsClient.list_worksheet_titles` plus a header read for existing
    tabs — and issues no writes.
    """
    config = _load(args)

    if args.target == "test":
        spreadsheet_id = config.sheets.test_spreadsheet_id
        if not spreadsheet_id:
            print("init-sheet: no test_spreadsheet_id configured — nothing to do")
            return 1
        client = SheetsClient(config, spreadsheet_id=spreadsheet_id)
    else:
        client = SheetsClient(config)

    if args.dry_run:
        existing = set(client.list_worksheet_titles())
        for tab in schema.REQUIRED_TABS:
            columns = schema.TABS[tab]
            if tab not in existing:
                action = "would create"
            elif tuple(client.read_header(tab)) == columns:
                action = "ok (no change)"
            else:
                action = "would write headers / mismatch"
            print(f"init-sheet [dry-run]: {tab} -> {action}")
        print(f"init-sheet [dry-run]: {len(schema.REQUIRED_TABS)} tab(s) inspected, no writes made")
        return 0

    counts = {"created": 0, "headers-written": 0, "ok": 0}
    for tab in schema.REQUIRED_TABS:
        status = client.ensure_tab(tab)
        counts[status] += 1
        print(f"init-sheet: {tab} -> {status}")
    print(
        "init-sheet: "
        f"{counts['created']} created, "
        f"{counts['headers-written']} header(s) written, "
        f"{counts['ok']} already ok"
    )
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    """Export a CSV snapshot of the live tab set under ``snapshots/<utc>/``.

    Backs up :data:`backup.LIVE_SNAPSHOT_TABS` — the live-required tab(s) plus the operator-
    maintained "Budget Timeseries" source — and skips any of those tabs the spreadsheet does
    not have (so it keeps working once the unused canonical tabs are deleted).
    """
    config = _load(args)
    client = SheetsClient(config)
    dest = Path(args.dest)
    snapshot_dir = backup.snapshot_all_tabs(client, dest)
    written = sorted(p.name for p in snapshot_dir.glob("*.csv"))
    print(f"snapshot: wrote {len(written)} tab(s) to {snapshot_dir}")
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
    """Source from the "Budget Timeseries" tab, build the analytics frame, print a summary.

    Reads the "Budget Timeseries" long dataset via
    :func:`pta_finance.report_source.read_timeseries` + :func:`~.to_inputs` (which projects
    it onto the budget/transaction row shapes the analytics engine consumes) — the canonical
    ``transactions`` / ``budget`` tabs are no longer read here. ``--fy YYYY`` filters every
    aggregation to that fiscal year; absent, all years are included. Rows flagged
    ``needs_review`` are excluded by :func:`analytics.build_frame` (the excluded count is
    printed). Reads only — never writes the sheet.
    """
    config = _load(args)
    client = SheetsClient(config)
    rows = report_source.read_timeseries(client)
    budget_rows, txn_rows = report_source.to_inputs(
        rows, start_month=config.fiscal_year.start_month, fy=None
    )

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
    """Generate fiscal-year report(s): read the timeseries, build, render to HTML, log the run.

    Sources from the "Budget Timeseries" tab (read-only) via
    :func:`pta_finance.report_source.read_timeseries` + :func:`~.to_inputs`, which projects
    that long dataset onto the budget/transaction row shapes the report builder consumes —
    the canonical ``transactions`` / ``budget`` tabs are no longer read here. Builds the
    requested variant(s) via :mod:`pta_finance.reports`, renders each to a single
    self-contained HTML file under ``reports/output/FY<fy>-<variant>.html`` (a gitignored
    dir — reports never enter the repo), and appends one row to the ``report_log`` tab per
    variant (run_at, variant, ``month``=``FY<fy>``, output_url=the local path, generated_by).
    ``--variant both`` emits both files + both log rows. The external builder runs its PII
    guard before rendering.

    ``--fy`` is OPTIONAL: when omitted it defaults to the CURRENT fiscal year
    (:func:`pta_finance.ids.fiscal_year_label` of today's UTC date under the configured
    ``fiscal_year.start_month``), so the unattended monthly cron can run
    ``report --variant both`` with no target argument.
    """
    config = _load(args)
    fy: int = (
        args.fy
        if args.fy is not None
        else ids.fiscal_year_label(datetime.now(UTC).date(), config.fiscal_year.start_month)
    )

    client = SheetsClient(config)
    rows = report_source.read_timeseries(client)
    budget_rows, txn_rows = report_source.to_inputs(
        rows, start_month=config.fiscal_year.start_month, fy=fy
    )

    variants = ("internal", "external") if args.variant == "both" else (args.variant,)

    out_dir = Path(args.output_dir) / "reports" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    run_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    generated_by = config.contacts.treasurer
    fy_label = f"FY{fy}"

    log_rows: list[dict[str, str]] = []
    for variant in variants:
        if variant == "internal":
            model = reports.build_internal_report(config, fy, txn_rows, budget_rows)
            html = reports.render_internal(model)
        else:
            ext_model = reports.build_external_report(config, fy, txn_rows, budget_rows)
            html = reports.render_external(ext_model)

        out_path = out_dir / f"{fy_label}-{variant}.html"
        out_path.write_text(html, encoding="utf-8")
        print(f"report: wrote {variant} report to {out_path}")

        log_rows.append(
            {
                "run_at": run_at,
                "variant": variant,
                "month": fy_label,
                "output_url": str(out_path),
                "generated_by": generated_by,
            }
        )

    client.append_rows(schema.TAB_REPORT_LOG, log_rows)
    print(f"report: logged {len(log_rows)} run(s) to {schema.TAB_REPORT_LOG}")
    return 0


def _fiscal_year_end_date(fy: int, start_month: int) -> date:
    """The LAST calendar day of fiscal year ``fy`` for a given start month.

    For a calendar fiscal year (``start_month == 1``) this is December 31 of ``fy``.
    Otherwise the year spans into ``fy``'s calendar year and ENDS in ``start_month - 1``
    of that year; the last day of that month is found via :func:`calendar.monthrange`.
    """
    if start_month == 1:
        return date(fy, 12, 31)
    end_month = start_month - 1
    last_day = calendar.monthrange(fy, end_month)[1]
    return date(fy, end_month, last_day)


def _cmd_import_budget(args: argparse.Namespace) -> int:
    """(Legacy) Load a messy human "budget" worksheet into the canonical ``budget`` tab.

    Superseded by the LIVE flow, which sources ``report`` / ``analyze`` from the operator-
    maintained "Budget Timeseries" tab; this command (and the canonical tabs it writes) is
    retained for the older budget-import path and is not part of the live deployment surface.

    Reads the source worksheet named by ``--from-tab`` as a raw grid
    (:meth:`SheetsClient.read_values`), parses it with the pure
    :func:`pta_finance.budget_import.plan_budget_import`, then (unless ``--dry-run``)
    snapshots every tab BEFORE any write and upserts the planned ``budget`` rows
    (idempotent by :func:`pta_finance.ids.budget_id`). With ``--with-actuals`` it also
    upserts one summary ``transactions`` row per line item carrying its actual spend
    (keyed by :func:`pta_finance.ids.summary_txn_id`, a shape ``etl.normalize`` ignores).

    The summary transactions are stamped with the fiscal year's last day. ``--actual-date``
    overrides that; absent, it is derived from ``--fy`` + ``fiscal_year.start_month`` and a
    sanity check (a real :class:`ValueError`, not an ``assert``) confirms the derived date
    falls in ``--fy``. ``--dry-run`` prints the plan's counts + a sample and makes NO writes
    and NO snapshot.
    """
    config = _load(args)
    start_month = config.fiscal_year.start_month

    if args.actual_date:
        actual_date = models.parse_date(args.actual_date)
    else:
        actual_date = _fiscal_year_end_date(args.fy, start_month)
        # The derived date must fall in the requested fiscal year — a real guard (NOT an
        # assert, which `python -O` strips) against an off-by-one in the start-month
        # arithmetic (workspace security rule: invariants get real guards).
        derived_fy = ids.fiscal_year_label(actual_date, start_month)
        if derived_fy != args.fy:
            raise ValueError(
                f"computed fiscal-year-end date {actual_date.isoformat()} falls in "
                f"FY{derived_fy}, not the requested FY{args.fy} "
                f"(start_month={start_month}) — internal arithmetic error"
            )

    client = SheetsClient(config)
    values = client.read_values(args.from_tab)
    plan = budget_import.plan_budget_import(
        values,
        fy=args.fy,
        with_actuals=args.with_actuals,
        actual_date=actual_date,
    )

    if args.dry_run:
        print(
            "import-budget [dry-run]: "
            f"{len(plan.budget_rows)} budget row(s), "
            f"{len(plan.txn_rows)} summary txn(s), "
            f"{plan.skipped_blank} skipped (blank), "
            f"{plan.skipped_summary} skipped (summary), "
            f"{plan.needs_review} need review, "
            f"{plan.duplicate_ids} duplicate(s)"
        )
        for budget_id_ in list(plan.budget_rows)[:5]:
            row = plan.budget_rows[budget_id_]
            print(f"  {budget_id_}: {row['budgeted_amount']}")
        print("import-budget [dry-run]: no writes made")
        return 0

    # Snapshot BEFORE any mutation (corruption protection). This legacy path writes the
    # canonical budget/transactions tabs, so snapshot the full canonical registry.
    backup.snapshot_all_tabs(client, Path("."), tabs=schema.TABS)
    client.upsert_rows(schema.TAB_BUDGET, plan.budget_rows)
    if args.with_actuals and plan.txn_rows:
        client.upsert_rows(schema.TAB_TRANSACTIONS, plan.txn_rows)

    skipped = plan.skipped_blank + plan.skipped_summary
    print(
        "import-budget: "
        f"{len(plan.budget_rows)} budget row(s), "
        f"{len(plan.txn_rows)} summary txn(s), "
        f"{skipped} skipped, "
        f"{plan.needs_review} need review"
    )
    return 0


def _fy_for(date_str: str, start_month: int) -> str:
    """Fiscal-year label for a raw line-item date string, or ``""`` if it does not parse."""
    try:
        parsed = models.parse_date(date_str)
    except ValueError:
        return ""
    return f"FY{ids.fiscal_year_label(parsed, start_month)}"


def _money(raw: str) -> str:
    """Render a raw amount for preview: ``$1,234.56`` when parseable, else ``<raw>?``."""
    if raw.strip() == "":
        return "(blank)"
    try:
        value = models.parse_amount(raw)
    except ValueError:
        return f"{raw}?"
    return f"${value:,.2f}"


def _cmd_ingest_receipts(args: argparse.Namespace) -> int:
    """(Phase-4 prototype) Parse reimbursement-form ``.eml`` files and PREVIEW what is extracted.

    Credential-free and WRITE-FREE: reads raw ``.eml`` files from ``--source`` (a directory or
    a single file), recognizes reimbursement-form submissions structurally
    (:func:`pta_finance.receipt_ingest.parse_submission`), and prints one block per submission —
    requestor, each numbered line item (date / fiscal-year / category / amount / description),
    the stated Total vs. the summed line items (reconciliation check), and the count of linked
    receipt URLs + attachments. Emails that are not reimbursement forms are counted as skipped.

    Nothing is written to the Google Sheet: this step exists so the operator can eyeball the
    extraction on a few real emails before we decide where the rows land. ``--limit`` caps the
    number of RECOGNIZED submissions shown; ``--subject-filter`` narrows recognition to emails
    whose subject contains a substring; ``--csv`` also writes a flat one-row-per-line-item CSV
    (to a gitignored path) for spreadsheet review. ``--start-month`` (default 1) drives the
    fiscal-year derivation without needing a config/credentials.
    """
    source = Path(args.source)
    if not source.exists():
        print(f"ingest-receipts: source not found: {source}")
        print("  (download a few reimbursement emails as .eml into that folder — see SETUP.md)")
        return 1

    start_month = args.start_month
    subject_filter = args.subject_filter or None

    scanned = 0
    recognized = 0
    shown = 0
    csv_rows: list[dict[str, str]] = []

    for path, msg in receipt_ingest.iter_eml(source):
        scanned += 1
        sub = receipt_ingest.parse_submission(msg, subject_filter=subject_filter)
        if sub is None:
            continue
        recognized += 1

        for item in sub.line_items:
            csv_rows.append(
                {
                    "source_file": path.name,
                    "message_id": sub.message_id,
                    "received": sub.received,
                    "requestor_name": sub.requestor_name,
                    "requestor_email": sub.requestor_email,
                    "company": sub.company,
                    "item_index": str(item.index),
                    "date": item.date,
                    "fiscal_year": _fy_for(item.date, start_month),
                    "category": item.category,
                    "description": item.description,
                    "amount": item.amount,
                    "total_stated": sub.total,
                    "receipt_urls": " | ".join(sub.receipt_urls),
                    "attachments": " | ".join(sub.attachments),
                }
            )

        if args.limit is not None and shown >= args.limit:
            continue
        shown += 1

        print(f"[{shown}] {path.name}")
        who = sub.requestor_name or "(no name)"
        if sub.requestor_email:
            who = f"{who} <{sub.requestor_email}>"
        print(f"  requestor : {who}")
        meta = [
            f"company {sub.company}" if sub.company else "",
            f"phone {sub.phone}" if sub.phone else "",
        ]
        meta_line = "  ".join(m for m in meta if m)
        if meta_line:
            print(f"  details   : {meta_line}")
        print(f"  received  : {sub.received or '(no Date header)'}")
        print(f"  line items ({len(sub.line_items)}):")
        for item in sub.line_items:
            fy = _fy_for(item.date, start_month)
            date_cell = f"{item.date or '(no date)':<12}"
            fy_cell = f"{fy:<7}"
            cat_cell = f"{(item.category or '(no category)'):<16}"
            amt_cell = f"{_money(item.amount):>12}"
            desc = item.description or "(no description)"
            if len(desc) > 60:
                desc = desc[:57] + "..."
            print(f"    #{item.index}  {date_cell} {fy_cell} {cat_cell} {amt_cell}  {desc}")

        items_sum = receipt_ingest.line_item_total(sub)
        stated = receipt_ingest.stated_total(sub)
        reconciles = receipt_ingest.total_reconciles(sub)
        recon = {True: "YES", False: "NO — MISMATCH", None: "n/a"}[reconciles]
        sum_txt = "n/a" if items_sum is None else f"${items_sum:,.2f}"
        stated_txt = "n/a" if stated is None else f"${stated:,.2f}"
        print(f"  totals    : stated {stated_txt}   line-item sum {sum_txt}   reconciles {recon}")
        print(
            f"  receipts  : {len(sub.receipt_urls)} link(s), {len(sub.attachments)} attachment(s)"
        )
        print("")

    print(
        f"ingest-receipts: scanned {scanned} email(s), "
        f"recognized {recognized} reimbursement form(s), "
        f"skipped {scanned - recognized} non-matching"
    )

    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "source_file",
            "message_id",
            "received",
            "requestor_name",
            "requestor_email",
            "company",
            "item_index",
            "date",
            "fiscal_year",
            "category",
            "description",
            "amount",
            "total_stated",
            "receipt_urls",
            "attachments",
        ]
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"ingest-receipts: wrote {len(csv_rows)} line-item row(s) to {csv_path}")

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

    p_init_sheet = sub.add_parser(
        "init-sheet", help="create the live-required tab(s) + schema headers in the spreadsheet"
    )
    _add_config_arg(p_init_sheet)
    p_init_sheet.add_argument(
        "--target",
        choices=("main", "test"),
        default="main",
        help="which spreadsheet to bootstrap: main (default) or the test sheet",
    )
    p_init_sheet.add_argument(
        "--dry-run",
        action="store_true",
        help="report the action each tab would take, make no writes",
    )
    p_init_sheet.set_defaults(func=_cmd_init_sheet)

    p_snapshot = sub.add_parser("snapshot", help="export CSV backups of the live tab set")
    _add_config_arg(p_snapshot)
    p_snapshot.add_argument(
        "--dest",
        default=".",
        help="base directory for snapshots/<utc>/ output (default: .)",
    )
    p_snapshot.set_defaults(func=_cmd_snapshot)

    p_normalize = sub.add_parser(
        "normalize",
        help="(legacy) normalize legacy/raw ledger -> canonical schema (assign IDs, dedup)",
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

    p_report = sub.add_parser("report", help="generate fiscal-year report(s)")
    _add_config_arg(p_report)
    p_report.add_argument(
        "--fy",
        type=int,
        default=None,
        help="fiscal-year label to report on, e.g. 2026 (default: current fiscal year)",
    )
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

    p_import_budget = sub.add_parser(
        "import-budget",
        help="load a messy budget worksheet into the canonical budget tab (+ summary actuals)",
    )
    _add_config_arg(p_import_budget)
    p_import_budget.add_argument(
        "--from-tab",
        required=True,
        help="name of the source worksheet to read (the messy human budget tab)",
    )
    p_import_budget.add_argument(
        "--fy",
        type=int,
        required=True,
        help="fiscal-year label the budget belongs to, e.g. 2026",
    )
    p_import_budget.add_argument(
        "--with-actuals",
        action="store_true",
        help="also import one summary 'actual' transaction per line item",
    )
    p_import_budget.add_argument(
        "--actual-date",
        default=None,
        help="ISO date YYYY-MM-DD for the summary actuals (default: last day of the FY)",
    )
    p_import_budget.add_argument(
        "--dry-run",
        action="store_true",
        help="print the parsed plan + counts, make no writes and no snapshot",
    )
    p_import_budget.set_defaults(func=_cmd_import_budget)

    p_ingest = sub.add_parser(
        "ingest-receipts",
        help="(prototype) parse reimbursement-form .eml files and preview what is extracted",
    )
    p_ingest.add_argument(
        "--source",
        default="mail_samples",
        help="a .eml file or a directory of .eml files (default: ./mail_samples)",
    )
    p_ingest.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap how many recognized submissions are printed (CSV still gets all)",
    )
    p_ingest.add_argument(
        "--subject-filter",
        default=None,
        help="only treat emails whose subject contains this substring as reimbursement forms",
    )
    p_ingest.add_argument(
        "--start-month",
        type=int,
        default=1,
        help="fiscal-year start month for FY derivation (default: 1 = calendar year)",
    )
    p_ingest.add_argument(
        "--csv",
        default=None,
        help="also write a flat one-row-per-line-item CSV to this (gitignored) path",
    )
    p_ingest.set_defaults(func=_cmd_ingest_receipts)

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
