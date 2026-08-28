"""``pta-finance`` command-line entry point (stdlib ``argparse``).

Wired subcommands:

    check      Step 3 — validate report_log schema + Budget Timeseries source; round-trip a row
    init-sheet bootstrap the spreadsheet with the live-required tab(s) + their schema headers
    snapshot   Step 3 — export safe CSV + exact tagged entered-value JSON under ``snapshots/<utc>/``
    normalize  Step 4 — (legacy) normalize legacy ledger -> canonical schema (snapshot first)
    analyze    Step 5 — run analytics over the "Budget Timeseries" tab; print a summary
    report     Step 6 — generate fiscal-year report(s); write HTML to reports/output/, log a run
    sync-budget    reconcile the editable "FY<fy> Budget" tab back into the Budget Timeseries DB
    import-budget  (legacy) load a messy "budget" worksheet into the canonical budget tab
    fetch-mail     Phase 4 - fetch a date window of Gmail into .eml files (counts only)
    ingest-receipts / map-receipts  Phase 4 - parse those .eml/.mbox files into ledger rows
    report-reimbursements  render the private queue from its validated local bundle
    update-reimbursements  optional mail fetch -> local evidence refresh -> private report

The LIVE data flow sources ``report`` / ``analyze`` from the operator-maintained "Budget
Timeseries" tab and writes only ``report_log``; ``check`` / ``init-sheet`` / ``snapshot``
provision/validate only :data:`schema.REQUIRED_TABS`. The canonical ``transactions`` /
``receipts`` / ``budget`` / ``events`` tabs (and the ``normalize`` / ``import-budget`` commands
that fill them) are LEGACY — superseded by the Budget Timeseries flow and safe to delete.
"""

from __future__ import annotations

import argparse
import calendar
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pta_finance import (
    analytics,
    backup,
    budget_import,
    budget_sync,
    etl,
    gmail_source,
    ids,
    models,
    receipt_ingest,
    receipt_map,
    reimbursement_pipeline,
    reimbursement_report,
    report_source,
    reports,
    schema,
)
from pta_finance.config import Config, load_config
from pta_finance.sheets import SheetsClient


def _load(args: argparse.Namespace) -> Config:
    """Load the typed config from ``--config`` (default ``config.toml`` in cwd)."""
    return load_config(Path(args.config))


def _receipt_start_month(args: argparse.Namespace, *, config: Config | None) -> int:
    """Resolve receipt FY month from an explicit override or the caller's config snapshot."""
    if args.start_month is not None:
        return int(args.start_month)
    if config is None:
        raise RuntimeError("receipt fiscal year requires an initial config snapshot")
    return config.fiscal_year.start_month


def _received_since_arg(raw: str) -> date:
    """Argparse converter for a strict ISO ``YYYY-MM-DD`` calendar date."""
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO date (YYYY-MM-DD)") from exc
    if parsed.isoformat() != raw:
        raise argparse.ArgumentTypeError("must be an ISO date (YYYY-MM-DD)")
    return parsed


def _nonempty_tab_name_arg(raw: str) -> str:
    """Argparse converter that rejects an explicitly empty Sheet tab name."""
    tab_name = raw.strip()
    if not tab_name:
        raise argparse.ArgumentTypeError("must contain at least one non-whitespace character")
    return tab_name


def _receipt_received_cutoff(
    args: argparse.Namespace, *, config: Config | None
) -> tuple[date | None, str]:
    """Resolve map-receipts' inclusive cutoff and its operator-visible source."""
    received_since = args.received_since
    if received_since is not None:
        if not isinstance(received_since, date):
            raise TypeError("map-receipts received_since must be a date")
        return received_since, "--received-since"
    if bool(args.all_received):
        return None, "--all-received"
    if config is not None and config.receipt_mapping is not None:
        return config.receipt_mapping.received_since, "config: receipt_mapping.received_since"
    return None, "not configured"


def _normalized_finite_amount(raw: str) -> str | None:
    """Return normalized finite money, or ``None`` for a rejected amount cell."""
    try:
        return f"{receipt_map.parse_finite_amount(raw):.2f}"
    except ValueError:
        return None


def _sheet_amount_cell(raw: str) -> str:
    """Keep valid money numeric; force rejected USER_ENTERED values to durable safe text."""
    normalized = _normalized_finite_amount(raw)
    if normalized is not None:
        return normalized
    neutralized = backup.encode_formula_safe_text(raw)
    # USER_ENTERED consumes this transport apostrophe. Any safety apostrophe added above—or any
    # literal apostrophes already present in the rejected source text—then remains in the cell.
    return f"'{neutralized}"


def _csv_receipt_row(row: dict[str, str]) -> list[object]:
    """Order one review-CSV row and normalize its finite monetary cell."""
    ordered: list[object] = []
    for field in receipt_map.FIELDNAMES:
        raw = row[field]
        if field == "amount":
            normalized = _normalized_finite_amount(raw)
            ordered.append(
                backup.validated_csv_number(normalized)
                if normalized is not None
                else backup.force_csv_text(raw)
            )
        else:
            ordered.append(raw)
    return ordered


def _cmd_check(args: argparse.Namespace) -> int:
    """Validate the live-required schema + the Budget Timeseries source, then round-trip a row.

    Three checks (the live deployment surface — the unused canonical tabs may be deleted):

    1. **Schema** of every tab in :data:`schema.REQUIRED_TABS` (now just ``report_log``).
    2. **Source readable.** :func:`report_source.read_timeseries` returns a non-empty list and
       its header carries every :data:`report_source.TIMESERIES_COLUMNS` name — the data
       ``report`` / ``analyze`` actually consume. A missing/empty/mis-shaped source returns 1.
    3. **Write round-trip** on the ``test_spreadsheet_id`` sheet's ``report_log`` (write ->
       read-back -> delete), keyed by a unique ``run_at`` marker (``report_log``'s first
       column, the upsert/delete key — ``SheetsClient`` keys by column 1). Runs only when
       ``test_spreadsheet_id`` is set; that is a throwaway test sheet by default, though the
       config permits pointing it at the production sheet (the probe row is deleted either
       way). Live only with real creds (M2); here it is unit tested against a mocked client.
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
    """Export safe CSV + exact tagged entered-value JSON under ``snapshots/<utc>/``.

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
        value = receipt_ingest.parse_finite_amount(raw)
    except ValueError:
        return f"{raw}?"
    return f"${value:,.2f}"


def _print_receipt_profile(
    prof: receipt_ingest.Profile, scanned: int, replies_skipped: int = 0
) -> None:
    """Render a PII-free aggregate profile of a batch of parsed submissions to stdout.

    Names/emails/phones are never printed — only the COUNT of distinct requestors. The category
    list is the seed for the assumptions-tab category map (raw form categories -> canonical lines).
    ``replies_skipped`` (Re:/Fwd: thread duplicates dropped by ``--originals-only``) is reported
    separately so it is never conflated with genuinely non-matching email.
    """
    non_matching = scanned - replies_skipped - prof.recognized
    reply_note = f", {replies_skipped} Re:/Fwd: duplicate(s) skipped" if replies_skipped else ""
    print(
        f"profile: scanned {scanned} email(s), recognized {prof.recognized} "
        f"reimbursement form(s), {non_matching} non-matching{reply_note}"
    )
    print(f"  distinct requestors : {prof.distinct_requestors} (names not listed — PII)")
    print(f"  line items          : {prof.line_items}")
    lo, hi = prof.received_span
    if lo:
        print(f"  email date span     : {lo} -> {hi}  (when forms were SUBMITTED — check for gaps)")

    print("  form types:")
    for name, count in prof.form_types:
        print(f"    {count:>6}  {name}")

    print(
        f"  reconciliation      : {prof.reconcile_yes} reconcile, "
        f"{prof.reconcile_no} MISMATCH (-> needs_review), {prof.reconcile_na} n/a"
    )
    print(
        f"  blank line fields   : category {prof.blank_category_items}, "
        f"amount {prof.blank_amount_items}, no-date {prof.no_date_items} "
        f"(of {prof.line_items} line items)"
    )
    print(
        f"  parse anomalies     : {prof.unparseable_amounts} bad amount(s), "
        f"{prof.unparseable_dates} bad date(s)"
    )
    print(f"  zero-receipt subs   : {prof.zero_receipt_submissions}")

    if prof.payment_types:
        print("  payment types:")
        for name, count in prof.payment_types:
            print(f"    {count:>6}  {name}")

    if prof.fiscal_years:
        print("  fiscal years (by line-item date):")
        for name, count in prof.fiscal_years:
            print(f"    {count:>6}  {name}")

    print(f"  distinct categories : {len(prof.categories)} (seed for the assumptions category map)")
    for name, count in prof.categories:
        print(f"    {count:>6}  {name}")


def _write_category_csv(prof: receipt_ingest.Profile, path: Path) -> None:
    """Write the category distribution to a gitignored CSV — the seed for the assumptions map.

    One row per distinct raw form category (with its line-item count) plus an empty
    ``canonical_category`` column for the operator to fill in when building the mapping.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["raw_category", "line_item_count", "canonical_category (fill in)"]
    rows = [[name, count, ""] for name, count in prof.categories]
    backup.write_formula_safe_csv(path, [header, *rows])
    print(f"ingest-receipts: wrote {len(prof.categories)} category row(s) to {path}")


def _cmd_ingest_receipts(args: argparse.Namespace) -> int:
    """(Phase-4 prototype) Parse reimbursement-form emails and PREVIEW or PROFILE what is extracted.

    Credential-free and WRITE-FREE. ``--source`` may be a single ``.eml``, a directory of
    ``.eml``/``.mbox`` files, or a Google Takeout ``.mbox``
    (:func:`pta_finance.receipt_ingest.iter_source`). Recognizes reimbursement-form submissions
    structurally (:func:`pta_finance.receipt_ingest.parse_submission`). Two modes:

    * **default (preview)** — prints one block per recognized submission (requestor, each numbered
      line item, stated-vs-summed total reconciliation, receipt-link/attachment counts). ``--limit``
      caps blocks shown; ``--csv`` also writes a flat one-row-per-line-item CSV (gitignored).
    * **``--profile`` (meta load)** — prints an AGGREGATE, PII-free profile of the whole batch
      (form types, the full category vocabulary, blank-field rates, reconciliation pass/fail, FY
      span, parse anomalies) so the canonical schema + category map can be designed once against the
      true distribution. In this mode ``--csv`` writes the category-distribution seed instead.

    Nothing is written to the Google Sheet. Emails that are not reimbursement forms are counted as
    skipped; ``--subject-filter`` narrows recognition. Fiscal-year derivation uses the configured
    ``fiscal_year.start_month`` unless ``--start-month`` intentionally overrides it.
    """
    source = Path(args.source)
    if not source.exists():
        print(f"ingest-receipts: source not found: {source}")
        print("  (download a few reimbursement emails as .eml into that folder — see SETUP.md)")
        return 1

    config = _load(args) if args.start_month is None else None
    start_month = _receipt_start_month(args, config=config)
    subject_filter = args.subject_filter or None

    # One read pass: parse every message, keep the recognized submissions (with a display label).
    # ``--originals-only`` drops Re:/Fwd: thread duplicates (same reimbursement, different
    # Message-ID) so message_id idempotency never sees them and profile numbers aren't inflated.
    scanned = 0
    replies_skipped = 0
    labeled_subs: list[tuple[str, receipt_ingest.Submission]] = []
    for label, msg in receipt_ingest.iter_source(source):
        scanned += 1
        sub = receipt_ingest.parse_submission(msg, subject_filter=subject_filter)
        if sub is None:
            continue
        if args.originals_only and receipt_ingest.is_reply_or_forward(sub.subject):
            replies_skipped += 1
            continue
        labeled_subs.append((label, sub))
    recognized = len(labeled_subs)

    if args.profile:
        prof = receipt_ingest.profile(
            [sub for _label, sub in labeled_subs], start_month=start_month
        )
        _print_receipt_profile(prof, scanned, replies_skipped)
        if args.csv:
            _write_category_csv(prof, Path(args.csv))
        return 0

    shown = 0
    csv_rows: list[dict[str, str]] = []

    for label, sub in labeled_subs:
        for item in sub.line_items:
            csv_rows.append(
                {
                    "source_file": label,
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

        print(f"[{shown}] {label}")
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

    reply_note = f", {replies_skipped} Re:/Fwd: duplicate(s) skipped" if replies_skipped else ""
    print(
        f"ingest-receipts: scanned {scanned} email(s), "
        f"recognized {recognized} reimbursement form(s), "
        f"{scanned - replies_skipped - recognized} non-matching{reply_note}"
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
        money_fields = frozenset({"amount", "total_stated"})
        ordered_rows: list[list[object]] = []
        for row in csv_rows:
            ordered_row: list[object] = []
            for field in fieldnames:
                raw = row[field]
                if field in money_fields:
                    normalized = _normalized_finite_amount(raw)
                    ordered_row.append(
                        backup.validated_csv_number(normalized)
                        if normalized is not None
                        else backup.force_csv_text(raw)
                    )
                elif field == "item_index":
                    ordered_row.append(backup.validated_csv_number(raw))
                else:
                    ordered_row.append(raw)
            ordered_rows.append(ordered_row)
        backup.write_formula_safe_csv(
            csv_path,
            [fieldnames, *ordered_rows],
        )
        print(f"ingest-receipts: wrote {len(csv_rows)} line-item row(s) to {csv_path}")

    return 0


def _cmd_map_receipts(args: argparse.Namespace) -> int:
    """(Phase-4) Map reimbursement submissions onto flat "Reimbursements" ledger rows (PREVIEW).

    Credential-free, write-free by default: parses ``--source`` (originals only — ``Re:``/``Fwd:``
    dropped), loads the category map, projects to flat one-row-per-line-item ledger rows via
    :func:`pta_finance.receipt_map.map_submissions` (carry-forward blank category/date, skip
    blank-amount lines, canonical-category lookup, dedup, ``needs_review``), and prints a summary.
    ``--csv`` writes the full flat ledger to a gitignored path for review; ``--limit`` previews the
    first N rows. An inclusive received-date cutoff is resolved from explicit CLI overrides, then
    optional private config, and applied before mapper deduplication. ``--write-tab NAME``
    additionally creates/replaces a **machine-owned** Sheet tab with the ledger (this is the only
    mode that needs credentials + touches the Sheet).
    """
    source = Path(args.source)
    if not source.exists():
        print(f"map-receipts: source not found: {source}")
        return 1
    map_path = Path(args.category_map)
    if not map_path.exists():
        print(f"map-receipts: category map not found: {map_path}")
        print("  (build it with: ingest-receipts --profile --csv <path>, then fill in")
        print("   the canonical_category column)")
        return 1

    config_path = Path(args.config)
    needs_config = args.start_month is None or args.write_tab is not None or config_path.exists()
    config = _load(args) if needs_config else None
    category_map = receipt_map.load_category_map(map_path)
    form_defaults = receipt_map.load_form_defaults(map_path)
    start_month = _receipt_start_month(args, config=config)
    received_cutoff, cutoff_source = _receipt_received_cutoff(args, config=config)
    subject_filter = args.subject_filter or None

    subs: list[receipt_ingest.Submission] = []
    scanned = 0
    replies = 0
    for _label, msg in receipt_ingest.iter_source(source):
        scanned += 1
        sub = receipt_ingest.parse_submission(msg, subject_filter=subject_filter)
        if sub is None:
            continue
        if receipt_ingest.is_reply_or_forward(sub.subject):
            replies += 1
            continue
        subs.append(sub)

    excluded = 0
    invalid_received = 0
    if received_cutoff is not None:
        in_scope: list[receipt_ingest.Submission] = []
        for sub in subs:
            received_date = receipt_ingest.parse_received_date(sub.received)
            if received_date is None:
                invalid_received += 1
            elif received_date < received_cutoff:
                excluded += 1
            else:
                in_scope.append(sub)
    else:
        in_scope = subs

    cutoff_text = f"{received_cutoff.isoformat()} inclusive" if received_cutoff else "none"
    print(f"  received cutoff : {cutoff_text} ({cutoff_source}); excluded {excluded} submission(s)")
    if invalid_received:
        print(
            "map-receipts: cannot apply received cutoff: "
            f"{invalid_received} recognized original submission(s) have a missing or malformed "
            "Date header"
        )
        return 1
    rows = receipt_map.map_submissions(
        in_scope, category_map=category_map, form_defaults=form_defaults, start_month=start_month
    )
    if args.write_tab is not None and not rows:
        print(
            "map-receipts: refusing --write-tab: mapping produced zero ledger rows from "
            f"{len(in_scope)} in-scope submission(s) "
            f"({len(subs)} recognized original submission(s)); existing tab was not changed"
        )
        return 1

    flagged = sum(1 for row in rows if row["needs_review"])
    unmapped = sum(1 for row in rows if "unmapped-category" in row["needs_review"])
    total = 0.0
    for row in rows:
        try:
            total += float(receipt_map.parse_finite_amount(row["amount"]))
        except ValueError:
            pass
    print(
        f"map-receipts: {len(in_scope)} submission(s) (originals; {replies} Re:/Fwd: skipped) "
        f"-> {len(rows)} ledger row(s)"
    )
    print(f"  category map : {len(category_map)} mapping(s), {len(form_defaults)} form default(s)")
    print(f"  needs_review : {flagged} row(s) ({unmapped} unmapped-category)")
    print(f"  total amount : ${total:,.2f}")

    if args.limit:
        print(f"  first {args.limit} row(s):")
        for row in rows[: args.limit]:
            label = (row["canonical_category"] or row["raw_category"] or "(blank)")[:34]
            form = row["form_type"].split()[0] if row["form_type"] else "?"
            print(
                f"    {row['date'] or '(no date)':<11} {form:<8} {label:<34} "
                f"${row['amount']:>10}  {row['needs_review']}"
            )

    if args.write_tab is not None:
        if config is None:
            raise RuntimeError("map-receipts write is missing its initial config snapshot")
        client = SheetsClient(config)
        if args.write_tab in client.list_worksheet_titles():
            backup.snapshot_raw_tab(
                client,
                args.write_tab,
                Path(args.dest),
            )
        ordered = [
            [
                (
                    _sheet_amount_cell(row[col])
                    if col == "amount"
                    else backup.encode_formula_safe_text(row[col])
                )
                for col in receipt_map.FIELDNAMES
            ]
            for row in rows
        ]
        status = client.replace_tab_grid(
            args.write_tab,
            list(receipt_map.FIELDNAMES),
            ordered,
            numeric_columns=["amount"],
        )
        print(f"map-receipts: {status} tab {args.write_tab!r} with {len(rows)} row(s)")

    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        header = list(receipt_map.FIELDNAMES)
        ordered_rows = [_csv_receipt_row(row) for row in rows]
        backup.write_formula_safe_csv(
            csv_path,
            [header, *ordered_rows],
        )
        print(f"map-receipts: wrote {len(rows)} row(s) to {csv_path}")
    return 0


def _cmd_fetch_mail(args: argparse.Namespace, *, service: Any = None) -> int:
    """(Phase-4) Fetch a DATE WINDOW of mail from Gmail into ``.eml`` files. Counts only.

    The read-only half of the receipt pipeline's front door: it authenticates as the user
    with a pinned ``gmail.readonly`` grant (:mod:`pta_finance.gmail_source`), walks
    ``users.messages.list`` through every page of ``--since``/``--until``, and writes each
    message's raw RFC-822 bytes to one ``.eml`` file. It does not parse, classify, or touch
    the Google Sheet — ``ingest-receipts`` and ``map-receipts`` do that, from the files.

    **Nothing about the mail is printed.** No subject, no sender, no recipient, no body, not
    even a Gmail message id: stdout carries counts, the search query the operator themself
    supplied, and the destination directory. That is a privacy requirement of the whole
    connector (the window is date-scoped, so unrelated personal mail is fetched too), not a
    display preference.

    **``--out`` defaults to the configured ``[gmail] inbox_dir`` itself, never a
    subdirectory of it.** ``receipt_ingest.iter_source`` globs a directory NON-recursively,
    so fetched mail must land BESIDE the ``.mbox`` archives for one
    ``map-receipts --source <dir>`` run to cover both. Two separate runs would each look
    internally clean while together double-counting every message the sources share, because
    ``receipt_map.map_submissions`` accumulates its dedup sets within a single call.

    Re-running an overlapping window is free: :func:`pta_finance.gmail_source.write_eml`
    names each file deterministically from its ``Message-ID`` and skips a byte-identical
    file, so the summary reports it as ``unchanged``. Overlap windows; never tile them.

    **What ``--dry-run`` guarantees, precisely.** It writes no ``.eml`` files into the
    destination — it lists and counts and stops. It MAY still mint or refresh the OAuth
    token file, and that is deliberate, not an oversight: plan Step M4 instructs the
    operator to run this exact command (``fetch-mail --since <date> --dry-run``) to trigger
    the one-time browser consent, and ``gmail_source._CONSENT_CMD`` names it in every
    remediation sentence. A ``--dry-run`` that refused to authenticate would make the
    connector's documented first-run procedure impossible.

    ``--limit`` caps how many messages are fetched (and stops paginating). ``service`` is a
    test seam: production passes nothing and the Gmail client is built from the stored
    credentials, minting them via that one-time browser consent on the very first run.
    """
    try:
        since = models.parse_date(args.since)
        until = models.parse_date(args.until) if args.until else None
    except ValueError as exc:
        print(f"fetch-mail: could not read that date ({exc}) — dates are ISO YYYY-MM-DD")
        return 1
    if until is not None and until <= since:
        print(
            f"fetch-mail: --until {until.isoformat()} is not after --since {since.isoformat()}, "
            "so the window is empty. Gmail's before: is EXCLUSIVE — --until names the first "
            "day that is NOT fetched."
        )
        return 1

    config = _load(args)
    try:
        out_dir = Path(args.out) if args.out else gmail_source.inbox_dir(config)
        if service is None:
            if gmail_source.needs_consent(config):
                print(
                    "fetch-mail: no Gmail token on this machine yet — opening a browser for "
                    "the one-time, READ-ONLY consent. Approve it and this run continues."
                )
            service = gmail_source.build_service(gmail_source.load_or_mint_credentials(config))

        summary = gmail_source.fetch_window(
            service,
            since=since,
            until=until,
            extra_query=args.query,
            out_dir=out_dir,
            limit=args.limit,
            dry_run=args.dry_run,
        )
        print(f"fetch-mail: query {summary.query!r}")
        print(f"  destination : {out_dir}")
    except gmail_source.GmailError as exc:
        print(f"fetch-mail: {exc}")
        return 1

    if args.dry_run:
        print(f"fetch-mail: {summary.matched} message(s) match — --dry-run, no .eml files written")
        return 0

    print(
        f"fetch-mail: {summary.matched} message(s) matched -> "
        f"{summary.new} new, {summary.unchanged} unchanged, {summary.rewritten} rewritten"
    )
    print(
        "  next: map the .eml files AND the .mbox archives in ONE run — "
        f"`pta-finance map-receipts --source {out_dir}` (two separate runs would "
        "double-count every message the two sources share)"
    )
    return 0


def _print_reimbursement_report_result(result: reimbursement_report.BuildResult) -> None:
    """Print one aggregate-only reimbursement report receipt."""
    summary = result.summary
    print(
        "report-reimbursements: "
        f"{summary.active} active, {summary.settled} settled, "
        f"{summary.live_unreviewed} unreviewed, {summary.item_lines} item line(s)"
    )
    print(
        f"  recommendation : ${summary.approved:,.2f} approved, "
        f"${summary.clarification:,.2f} clarification, "
        f"${summary.declined:,.2f} declined, ${summary.question:,.2f} question"
    )
    print(f"  email drafts   : {summary.emails_to_send}")
    print(f"  output         : {result.output_path}")
    print(f"  sha256         : {result.sha256}")


def _cmd_report_reimbursements(args: argparse.Namespace) -> int:
    """Render the private reimbursement report from one validated local bundle.

    This command is intentionally offline: it does not load ``config.toml``, credentials, Gmail,
    or Sheets.  The private bundle is the complete input and the HTML is replaced atomically only
    after validation and rendering succeed.
    """
    try:
        result = reimbursement_report.build_report(Path(args.data), Path(args.output))
    except (OSError, reimbursement_report.ReimbursementReportError) as exc:
        print(f"report-reimbursements: {exc}")
        return 1
    _print_reimbursement_report_result(result)
    return 0


def _cmd_update_reimbursements(args: argparse.Namespace, *, service: Any = None) -> int:
    """Run optional Gmail acquisition, local evidence refresh, then offline report rendering.

    The convenience command deliberately stops at private local artifacts.  It never sends mail
    and never writes either Google Sheet tab; ``map-receipts --write-tab`` retains that separate,
    explicit permission boundary.
    """
    if args.fetch_since is None and any(
        value is not None for value in (args.fetch_until, args.fetch_query, args.fetch_limit)
    ):
        print(
            "update-reimbursements: --fetch-until, --fetch-query, and --fetch-limit "
            "require --fetch-since"
        )
        return 1

    config = _load(args)
    start_month = _receipt_start_month(args, config=config)
    received_since, cutoff_source = _receipt_received_cutoff(args, config=config)
    source = (
        Path(args.source)
        if args.source
        else (gmail_source.inbox_dir(config) if config.gmail is not None else Path("mail_samples"))
    )
    as_of = args.as_of or date.today()

    if args.fetch_since is not None:
        if args.fetch_until is not None and args.fetch_until <= args.fetch_since:
            print(
                "update-reimbursements: --fetch-until must be after --fetch-since "
                "(the until date is exclusive)"
            )
            return 1
        if source.exists() and not source.is_dir():
            print("update-reimbursements: the combined fetch source must be a directory")
            return 1
        try:
            if service is None:
                if gmail_source.needs_consent(config):
                    print(
                        "update-reimbursements: no Gmail token on this machine yet — opening a "
                        "browser for the one-time, READ-ONLY consent"
                    )
                service = gmail_source.build_service(gmail_source.load_or_mint_credentials(config))
            fetched = gmail_source.fetch_window(
                service,
                since=args.fetch_since,
                until=args.fetch_until,
                extra_query=args.fetch_query,
                out_dir=source,
                limit=args.fetch_limit,
                dry_run=args.dry_run,
            )
        except gmail_source.GmailError as exc:
            print(f"update-reimbursements: email acquisition failed: {exc}")
            return 1
        if fetched.dry_run:
            print(
                f"update-reimbursements: {fetched.matched} message(s) match — dry run, "
                "no .eml files written"
            )
        else:
            print(
                f"update-reimbursements: email archive -> {fetched.new} new, "
                f"{fetched.unchanged} unchanged, {fetched.rewritten} rewritten"
            )

    try:
        refresh_kwargs = {
            "bundle_path": Path(args.data),
            "source": source,
            "category_map_path": Path(args.category_map),
            "start_month": start_month,
            "received_since": received_since,
            "as_of": as_of,
            "subject_filter": args.subject_filter,
        }
        if args.dry_run:
            _planned, summary = reimbursement_pipeline.plan_bundle_refresh(**refresh_kwargs)
        else:
            summary = reimbursement_pipeline.refresh_bundle(**refresh_kwargs)
    except (
        OSError,
        reimbursement_pipeline.ReimbursementPipelineError,
        reimbursement_report.ReimbursementReportError,
    ) as exc:
        print(f"update-reimbursements: local evidence refresh failed: {exc}")
        return 1

    print(
        f"update-reimbursements: {summary.total_source_tickets} source submission(s), "
        f"{summary.mapped_rows} line(s), ${summary.mapped_total:,.2f} "
        f"({cutoff_source})"
    )
    print(f"  review bundle : {summary.new_tickets} new, {summary.unchanged_tickets} unchanged")
    if args.dry_run:
        print("update-reimbursements [dry-run]: no bundle or report files written")
        return 0

    try:
        result = reimbursement_report.build_report(Path(args.data), Path(args.output))
    except (OSError, reimbursement_report.ReimbursementReportError) as exc:
        print(
            "update-reimbursements: bundle refreshed, but report rendering failed; "
            f"the prior HTML was preserved: {exc}"
        )
        return 1
    _print_reimbursement_report_result(result)
    return 0


def _cmd_sync_budget(args: argparse.Namespace) -> int:
    """Reconcile the editable "FY<fy> Budget" tab back into the "Budget Timeseries" DB.

    Reads the per-fiscal-year budget tab (:func:`budget_sync.budget_tab_name`) and the live
    "Budget Timeseries" tab, plans the diff with the PURE
    :func:`pta_finance.budget_sync.plan_budget_sync`, and PRINTS it (amount changes / note
    changes / new lines / flagged-removed / unchanged). The default is a DRY RUN — no writes.

    With ``--apply`` it snapshots the "Budget Timeseries" tab FIRST (a full entered-value
    :func:`pta_finance.backup.snapshot_raw_tab` backup of every column; formatting/comments are
    outside the artifact), then writes ONLY changed ``amount`` / ``notes`` cells and appends new
    lines. Only
    ``measure == "proposed"`` rows of ``--fy`` are touched; all enrichment columns are
    preserved, and a line present in the DB but absent from the tab is FLAGGED, never deleted.

    ``--fy`` defaults to the current fiscal year (:func:`pta_finance.ids.fiscal_year_label` of
    today's UTC date under ``fiscal_year.start_month``); the tab it reads is ``FY<fy> Budget``.
    """
    config = _load(args)
    fy: int = (
        args.fy
        if args.fy is not None
        else ids.fiscal_year_label(datetime.now(UTC).date(), config.fiscal_year.start_month)
    )
    client = SheetsClient(config)
    tab = budget_sync.budget_tab_name(fy)

    tab_grid = client.read_values(tab)
    if not tab_grid:
        print(f"sync-budget: {tab!r} is empty or missing — build/seed the FY{fy} budget tab first")
        return 1
    parsed = budget_sync.parse_budget_tab(tab_grid)
    if parsed.section_count == 0:
        print(
            f"sync-budget: {tab!r} has NO 'INCOME'/'EXPENSE' section banner — the layout looks "
            "broken (nothing can be reconciled). Fix the tab and re-run. No writes made."
        )
        return 1
    timeseries_grid = client.read_values(report_source.BUDGET_TIMESERIES_TAB)
    plan = budget_sync.plan_budget_sync(timeseries_grid, parsed.lines, fy=fy)

    rename_old = {old for _t, old, _n in plan.suspected_renames}
    print(
        f"sync-budget: {tab!r} -> {report_source.BUDGET_TIMESERIES_TAB!r} "
        f"(FY{fy} proposed) [{config.organization.name}]"
    )
    print(f"  parsed {len(parsed.lines)} budget line(s) from the tab")
    print(
        f"  {len(plan.changed)} amount change(s), {len(plan.notes_changed)} note change(s), "
        f"{len(plan.added)} new line(s), {len(plan.removed)} flagged-removed, "
        f"{plan.unchanged} unchanged"
    )
    for c_typ, c_item, c_old, c_new in plan.changed:
        print(f"    ~ [{c_typ}] {c_item}: {c_old:,.2f} -> {c_new:,.2f}")
    for a_typ, a_group, a_item, a_amt in plan.added:
        print(
            f"    + [{a_typ}] {a_item}: {a_amt:,.2f}  (group={a_group or '?'}; NEEDS tagging in DB)"
        )
    for r_typ, r_item, r_amt in plan.removed:
        note = " (looks like a RENAME — do NOT delete; see below)" if r_item in rename_old else ""
        print(f"    ! [{r_typ}] {r_item}: {r_amt:,.2f} in DB, absent from tab — NOT deleted{note}")

    # Warnings — surface anything that could silently corrupt the sync so it is never invisible.
    for sr_typ, sr_old, sr_new in plan.suspected_renames:
        print(
            f"    ~rename? [{sr_typ}] {sr_old!r} -> {sr_new!r}: appears as remove+add and will "
            "NOT carry the old row's strategic tags. If it IS a rename, edit raw_category in the "
            "Budget Timeseries directly instead of renaming on the tab."
        )
    for sk_item, sk_amt in parsed.skipped:
        print(f"    ? SKIPPED {sk_item!r}: amount {sk_amt!r} is not a number — fix it and re-run")
    for or_item, or_amt in parsed.orphaned:
        print(
            f"    ? ORPHAN {or_item!r} ({or_amt!r}): not under any INCOME/EXPENSE section — ignored"
        )
    for d_typ, d_item in plan.duplicates:
        print(f"    x DUPLICATE [{d_typ}] {d_item}: listed more than once on the tab")

    if plan.duplicates:
        print(
            f"sync-budget: {len(plan.duplicates)} duplicate line(s) on the tab — the intended "
            "value is ambiguous. Remove the duplicate rows, then re-run. No writes made."
        )
        return 1

    if not args.apply:
        print("sync-budget [dry-run]: no writes made. Re-run with --apply to write to the DB.")
        return 0
    if not plan.has_writes():
        print("sync-budget: nothing to write.")
        return 0

    # Snapshot the FULL "Budget Timeseries" grid BEFORE any mutation (corruption protection;
    # faithful — keeps every enrichment column, unlike snapshot_all_tabs).
    backup.snapshot_raw_tab(
        client,
        report_source.BUDGET_TIMESERIES_TAB,
        Path(args.dest),
    )
    client.update_cells(report_source.BUDGET_TIMESERIES_TAB, plan.cell_updates)
    client.append_raw_rows(report_source.BUDGET_TIMESERIES_TAB, plan.append_rows)
    print(
        f"sync-budget: applied {len(plan.cell_updates)} cell update(s) + "
        f"{len(plan.append_rows)} new row(s) to {report_source.BUDGET_TIMESERIES_TAB!r} "
        "(snapshot saved under snapshots/)"
    )
    if plan.added:
        print(
            f"sync-budget: {len(plan.added)} new line(s) need category_group / strategic tags "
            "filled in the Budget Timeseries tab"
        )
    if plan.removed:
        print(
            f"sync-budget: {len(plan.removed)} line(s) are in the DB but not on the tab — a "
            "flagged line may be a RENAME (its strategic tags cannot be recovered if deleted); "
            "verify before removing anything manually"
        )
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

    p_snapshot = sub.add_parser(
        "snapshot",
        help="export safe CSV + exact tagged entered-value JSON backups of the live tab set",
    )
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

    p_sync_budget = sub.add_parser(
        "sync-budget",
        help="reconcile the editable 'FY<fy> Budget' tab back into the Budget Timeseries DB",
    )
    _add_config_arg(p_sync_budget)
    p_sync_budget.add_argument(
        "--fy",
        type=int,
        default=None,
        help="fiscal year of the budget tab, e.g. 2027 (default: current fiscal year)",
    )
    p_sync_budget.add_argument(
        "--apply",
        action="store_true",
        help="write the changes to the DB (default: dry-run — print the diff, make no writes)",
    )
    p_sync_budget.add_argument(
        "--dest",
        default=".",
        help="base dir for the pre-write snapshots/<utc>/ backup (default: .)",
    )
    p_sync_budget.set_defaults(func=_cmd_sync_budget)

    p_ingest = sub.add_parser(
        "ingest-receipts",
        help="(prototype) parse reimbursement-form emails; preview each or --profile the batch",
    )
    p_ingest.add_argument(
        "--source",
        default="mail_samples",
        help="a .eml file, a directory of .eml/.mbox files, or a Takeout .mbox "
        "(default: ./mail_samples)",
    )
    p_ingest.add_argument(
        "--profile",
        action="store_true",
        help="meta load: print an aggregate PII-free profile of the whole batch (form types, "
        "category vocabulary, blank-field rates, reconciliation, FY span); --csv writes the "
        "category-map seed instead of the per-line-item CSV",
    )
    p_ingest.add_argument(
        "--originals-only",
        action="store_true",
        help="skip Re:/Fwd: thread duplicates (a reply re-quotes the form -> same reimbursement, "
        "different Message-ID); recommended for a true count and for the eventual backfill",
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
        help="override the configured fiscal-year start month for FY derivation",
    )
    p_ingest.add_argument(
        "--csv",
        default=None,
        help="also write a flat one-row-per-line-item CSV to this (gitignored) path",
    )
    _add_config_arg(p_ingest)
    p_ingest.set_defaults(func=_cmd_ingest_receipts)

    p_map = sub.add_parser(
        "map-receipts",
        help="(prototype) map reimbursement submissions to flat 'Reimbursements' ledger rows",
    )
    p_map.add_argument(
        "--source",
        default="mail_samples",
        help="a .eml file, a directory of .eml/.mbox files, or a Takeout .mbox "
        "(default: ./mail_samples)",
    )
    p_map.add_argument(
        "--category-map",
        default="reports/output/category_map.csv",
        help="category-map CSV: raw_category -> canonical_category "
        "(default: reports/output/category_map.csv)",
    )
    p_map.add_argument(
        "--start-month",
        type=int,
        help="override the configured fiscal-year start month for FY derivation",
    )
    p_map.add_argument(
        "--subject-filter",
        default=None,
        help="only treat emails whose subject contains this substring as reimbursement forms",
    )
    received_group = p_map.add_mutually_exclusive_group()
    received_group.add_argument(
        "--received-since",
        type=_received_since_arg,
        metavar="YYYY-MM-DD",
        help="inclusive RFC-822 Date-header cutoff; overrides [receipt_mapping] received_since",
    )
    received_group.add_argument(
        "--all-received",
        action="store_true",
        help="disable the configured received-date cutoff for this run",
    )
    p_map.add_argument(
        "--limit",
        type=int,
        default=None,
        help="preview the first N ledger rows",
    )
    p_map.add_argument(
        "--csv",
        default=None,
        help="write the full flat ledger to this (gitignored) path",
    )
    p_map.add_argument(
        "--write-tab",
        type=_nonempty_tab_name_arg,
        default=None,
        metavar="NAME",
        help="also create/replace a machine-owned Sheet tab NAME with the ledger "
        "(needs credentials)",
    )
    p_map.add_argument(
        "--dest",
        default=".",
        help="base dir for the pre-write snapshots/<utc>/ backup when --write-tab replaces a tab",
    )
    _add_config_arg(p_map)
    p_map.set_defaults(func=_cmd_map_receipts)

    p_fetch = sub.add_parser(
        "fetch-mail",
        help="fetch a date window of Gmail into .eml files (read-only; prints counts only)",
    )
    p_fetch.add_argument(
        "--since",
        required=True,
        metavar="YYYY-MM-DD",
        help="first day to fetch (INCLUSIVE); overlap the previous window, never tile it",
    )
    p_fetch.add_argument(
        "--until",
        default=None,
        metavar="YYYY-MM-DD",
        help="first day NOT fetched (Gmail's before: is EXCLUSIVE); omit for an open-ended window",
    )
    p_fetch.add_argument(
        "--query",
        default=None,
        help="extra raw Gmail search terms appended to the date window, e.g. 'has:attachment'",
    )
    p_fetch.add_argument(
        "--out",
        default=None,
        metavar="DIR",
        help="where the .eml files land (default: [gmail] inbox_dir, the SAME directory as "
        "the .mbox archives, so one map-receipts run covers both; never a subdirectory)",
    )
    p_fetch.add_argument(
        "--limit",
        type=int,
        default=None,
        help="stop after this many messages (a cheap first look at a big window)",
    )
    p_fetch.add_argument(
        "--dry-run",
        action="store_true",
        help="count the matching messages and write no .eml files (it MAY still mint or "
        "refresh the OAuth token: plan Step M4 uses this exact command for first consent)",
    )
    _add_config_arg(p_fetch)
    p_fetch.set_defaults(func=_cmd_fetch_mail)

    p_reimbursement_report = sub.add_parser(
        "report-reimbursements",
        help="render the private reimbursement queue from a validated local bundle",
    )
    p_reimbursement_report.add_argument(
        "--data",
        default="reports/output/reimbursement-report.json",
        help="private structured report bundle (default: reports/output/reimbursement-report.json)",
    )
    p_reimbursement_report.add_argument(
        "--output",
        default="reports/output/reimbursement-queue-breakdown.html",
        help="private HTML output (default: reports/output/reimbursement-queue-breakdown.html)",
    )
    p_reimbursement_report.set_defaults(func=_cmd_report_reimbursements)

    p_reimbursement_update = sub.add_parser(
        "update-reimbursements",
        help=(
            "optionally fetch mail, refresh private evidence, then render the reimbursement report"
        ),
    )
    p_reimbursement_update.add_argument(
        "--source",
        default=None,
        help=(
            "complete top-level .eml/.mbox archive "
            "(default: configured Gmail inbox or mail_samples)"
        ),
    )
    p_reimbursement_update.add_argument(
        "--category-map",
        default="reports/output/category_map.csv",
        help="category-map CSV (default: reports/output/category_map.csv)",
    )
    p_reimbursement_update.add_argument(
        "--data",
        default="reports/output/reimbursement-report.json",
        help="private structured report bundle (default: reports/output/reimbursement-report.json)",
    )
    p_reimbursement_update.add_argument(
        "--output",
        default="reports/output/reimbursement-queue-breakdown.html",
        help="private HTML output (default: reports/output/reimbursement-queue-breakdown.html)",
    )
    p_reimbursement_update.add_argument(
        "--start-month",
        type=int,
        help="override the configured fiscal-year start month for evidence mapping",
    )
    p_reimbursement_update.add_argument(
        "--subject-filter",
        default=None,
        help="only recognize reimbursement forms whose subject contains this substring",
    )
    update_received = p_reimbursement_update.add_mutually_exclusive_group()
    update_received.add_argument(
        "--received-since",
        type=_received_since_arg,
        metavar="YYYY-MM-DD",
        help="inclusive email Date-header cutoff; overrides private config",
    )
    update_received.add_argument(
        "--all-received",
        action="store_true",
        help="disable the configured reimbursement received-date cutoff for this run",
    )
    p_reimbursement_update.add_argument(
        "--fetch-since",
        type=_received_since_arg,
        default=None,
        metavar="YYYY-MM-DD",
        help="optionally acquire Gmail from this inclusive date before local refresh",
    )
    p_reimbursement_update.add_argument(
        "--fetch-until",
        type=_received_since_arg,
        default=None,
        metavar="YYYY-MM-DD",
        help="first Gmail date not acquired (exclusive)",
    )
    p_reimbursement_update.add_argument(
        "--fetch-query",
        default=None,
        help="optional raw Gmail search terms appended to the acquisition window",
    )
    p_reimbursement_update.add_argument(
        "--fetch-limit",
        type=int,
        default=None,
        help="stop Gmail acquisition after this many messages",
    )
    p_reimbursement_update.add_argument(
        "--as-of",
        type=_received_since_arg,
        default=None,
        metavar="YYYY-MM-DD",
        help="reproducible report date (default: today)",
    )
    p_reimbursement_update.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "list Gmail matches and validate the local refresh plan; write no email, "
            "bundle, or HTML"
        ),
    )
    _add_config_arg(p_reimbursement_update)
    p_reimbursement_update.set_defaults(func=_cmd_update_reimbursements)

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
