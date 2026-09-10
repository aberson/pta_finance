# Phase 6 — Monthly reimbursement board summary — Python workflow

## 1. What This Is

**Objective:** Turn the accepted one-page reimbursement summary into a repeatable local
Python workflow: prepare the figures and suggested wording, review the page, then preserve
an approved HTML/PDF edition for the board, principal, and teacher representatives.

**Status:** PLANNED — no production summary commands exist yet. The private reference
edition was accepted and finalized on 2026-09-10. This document plans the implementation;
it does not approve any reimbursement or report future steps as shipped.

**Planning approval:** The operator accepted this plan for plan-expedite on 2026-09-10.
The selected defaults below stand for implementation; their D labels retain their origin
as agent recommendations. GitHub uses Phase 6 with global Steps 26–31, following the
separate Phase 5 slide track without depending on its unfinished work.

Proposal: documentation/reimbursement-board-summary-proposal.html

### What This Feature Does

The treasurer chooses the reporting dates and runs a Python helper similar to the existing
`end-to-end-run` launcher. It refreshes the reimbursement queue when requested, prepares a
summary from a frozen copy of the reviewed data, and suggests short discussion items and
follow-ups. The treasurer can edit the wording, preview a single-page report, then finalize
that exact edition. Each final retains its own source snapshot and checksums so a later
monthly run cannot change a report already presented.

This is an operator-invoked workflow used monthly. It finishes and exits; it adds no
scheduler, background service, or autonomous observation loop. The autonomous-behavior
observation trigger does not apply. A real pipeline smoke and an attended Windows
acceptance run are still required.

## 2. Existing Context

Baseline inspected: `cc93c2d` on `main`, 2026-09-10. The project uses Python 3.12+, `uv`
(environment and dependency manager), standard-library `argparse` commands, immutable
dataclasses, `Decimal` money, and Jinja2 HTML templates with automatic text escaping.

| Producer | Relevant behavior verified in source |
|---|---|
| `pta_finance/reimbursement_report.py:1437` | `load_bundle(Path) -> ReimbursementReport` validates the private schema-v2 bundle and accepts v1 through its existing migration. |
| `pta_finance/reimbursement_report.py:220` | A ticket has a stable `review_key`, a display reference, live decision/payment state, reviewed line items, and source context. Display references alone are not unique across legacy forms. |
| `pta_finance/reimbursement_report.py:340` | `active_tickets`, `closed_tickets`, and `summary` describe the current bundle. Closed means workflow state `SETTLED`; it is not merely a recommendation to approve. |
| `pta_finance/reimbursement_report.py:1526` | `render_html(report)` produces the full offline queue. `build_report(data_path, output_path)` validates and atomically writes it. |
| `pta_finance/reimbursement_pipeline.py:1947` | `plan_bundle_refresh(...)` validates a full local archive refresh. `refresh_bundle(...)` calls it and atomically replaces the bundle. Both preserve reviewed identities and account for exact evidence. |
| `pta_finance/cli.py:1075` | `update-reimbursements` optionally fetches Gmail, refreshes evidence, then renders the queue. Acquisition failure stops the run; HTML failure can occur after the bundle has refreshed. |
| `pta_finance/cli.py:1629` | The existing command distinguishes inclusive `--fetch-since`, exclusive `--fetch-until`, intake cutoff `--received-since`, and report `--as-of`. Fetch dates do not define report membership. |
| `scripts/capture_readme.py:1` | A local Playwright/Chromium helper already captures fictional reimbursement HTML. Playwright 1.58.0 is currently a documentation-only dependency. |
| `pyproject.toml` | Jinja2 is installed; optional PDF support uses WeasyPrint. Optional statement parsing uses `pypdfium2`. Neither currently supplies this summary workflow. |

The workspace launcher named `end-to-end-run` currently invokes
`uv run pta-finance update-reimbursements --fetch-since` with an operator-entered date.
Its registry lives outside this repository at `../.claude/observatory/registry.toml`.
The new helper must work from the repository without that control plane.

The separate [treasurer slide plan](treasurer-summary-wave-1-plan.md) reserves Steps 14–25
for bank statements and Google Slides. This feature reserves **Steps 26–31** in the
master plan and does not depend on those unfinished slide steps. Shared CLI/dependency/CI
files require ordinary rebase and regression checks if both efforts advance.

The [reimbursement refresh plan](reimbursement-refresh-plan.md) documents existing payment
replay limitations. This feature consumes validated results; it does not broaden mail
recognition or resolve quarantined evidence. A successful refresh is not proof that every
email was understood, every receipt reviewed, or every payment discovered.

## 3. Scope

### Included

- The accepted visual structure: branding, explicit reporting range and as-of date, four
  compact figures, one closed/open pie, payment update, board discussion, other follow-ups,
  and a short closing paragraph. Plain language; no payment account details on the page.
- Cumulative queue counts from the frozen source bundle, with earlier carryovers retained.
  Payment activity has its own explicit date window. Defaults are recorded in the Appendix;
  they are workflow choices, not approvals of any underlying reimbursement.
- Deterministic suggested wording, editable private notes, and source links by stable case
  identity. Rendering never changes source decisions or payment status.
- Offline HTML and a one-page US Letter PDF, a PNG preview, validation evidence, and a
  read-only final archive with a SHA-256 checksum manifest.
- Three production commands plus a Python convenience runner; a copyable workspace
  launcher snippet and a short monthly operator guide.
- Synthetic examples and automated checks; one private acceptance run against the accepted
  reference, with real data retained only in ignored directories.

### Excluded

Sending messages, paying people, uploading proof of checks, writing Sheets, fetching linked
Drive receipts, changing reimbursement policy, repairing payment parsers, bank reconciliation,
Google Slides, hosting a web app, an in-app editor, an LLM service, automatic monthly sending,
and scheduling. There is no new authentication or cloud service. The source queue remains
the place to fix approvals, amounts, missing payments, and clarifications.

## 4. Impact Analysis

No existing public function signature, bundle field, or shared constant is changed by this
plan. New commands consume existing interfaces. The scan was
`rg -n '\b(load_bundle|render_html|build_report|plan_bundle_refresh|refresh_bundle|_cmd_update_reimbursements)\(' pta_finance tests scripts`.
The existing consumers are listed below to make the compatibility boundary explicit.

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `pta_finance/cli.py` | extend | Register prepare, render, and finalize commands with aggregate-only receipts. | Existing handlers at 1059/1075; parser entries at 1613/1629; `main(argv)` at 1734. Existing handlers, arguments, and return codes retain their contracts. |
| `pta_finance/reimbursement_report.py` | reuse unchanged | Strict source loader and full-queue renderer. | `load_bundle` callers: this module's `build_report`, pipeline `_write_bundle_atomic` and `plan_bundle_refresh`, tests `test_reimbursement_report.py` and `test_reimbursement_pipeline.py`. `render_html` callers: this module's `build_report` and those same two test modules. `build_report` callers: CLI report/update handlers, `scripts/capture_readme.py`, and report tests. No caller edits required. |
| `pta_finance/reimbursement_pipeline.py` | reuse unchanged | Convenience runner uses the established refresh command. | `plan_bundle_refresh` callers: `refresh_bundle`, CLI update handler, pipeline tests. `refresh_bundle` callers: CLI update handler and pipeline tests. No signature or reducer changes. |
| `pta_finance/reports/templates/reimbursement_queue.html.j2` | reuse unchanged | Generate the adjacent detailed source report from the same snapshot. | Line 61 uses lowercased `ref` plus optional `form_label`, spaces replaced by hyphens. Summary links must include the form label and resolve to exactly one generated anchor. |
| `pyproject.toml`, `uv.lock` | extend | Add a dedicated optional summary-export extra and required test tooling. | Existing extras `pdf`, `slides`, `dev`; entry point `pta-finance = pta_finance.cli:main`. Preserve the existing extras and Python floor. |
| `.github/workflows/ci.yml` | extend | Exercise the new browser export on Windows and Linux using fictional input. | Existing Linux lint/type/test job and Windows native-sandbox job inspected; preserve their tests and add a separate summary-export job. No mailbox credentials. |
| `tests/test_reimbursement_cli.py` | extend | Verify new registrations without weakening existing offline/refresh contracts. | Existing tests cover forbidden Google access, dry-run preservation, update sequencing, invalid fetch options, and failed rendering. |
| `README.md`, `CLAUDE.md`, `SETUP.md`, `docs/loading-receipts.md` | extend | Link the new monthly guide and distinguish summary preparation from evidence review. | All exist; README's reimbursement flow and CLAUDE's command table describe the current three-stage refresh. |
| `plan.md` | extend | Reserve Steps 26–31 and link this scoped plan. | Existing root steps 1–8, Gmail steps 9–13, slide steps 14–25 verified with `rg -n '^### Step '`. Earlier legacy subplans retain their historical numbering. |

New files are intentionally absent at planning time and are described in the next section.
The workspace registry is a separate repository's file, not an implementation dependency
or an automatic write target of this plan.

## 5. New Components and Contracts

| New component | Responsibility |
|---|---|
| `pta_finance/reimbursement_summary.py` | Summary-only input validation, normalized payment facts, queue figures, stable suggestions, and editable notes. Owns the new schemas and calculation rules. |
| `pta_finance/reimbursement_summary_render.py` | Render the summary with Jinja2; validate content/links; produce Chromium PDF/PNG; publish final archives. Imports summary models, not the CLI. |
| `pta_finance/reports/templates/reimbursement_board_summary.html.j2` | Generic, self-contained version of the approved design; every count, label, date, and chart description comes from the model. |
| `scripts/run_reimbursement_summary.py` | One-shot convenience caller of `pta_finance.cli.main(argv)`: existing refresh, then prepare and render the summary. Uses argument lists, never a shell-built command. |
| `tests/test_reimbursement_summary.py`, `tests/test_reimbursement_summary_render.py`, `tests/test_reimbursement_summary_pipeline.py` | Arithmetic/state boundaries, document/export/finalization behavior, and a real-component CLI smoke. Synthetic inputs only. |
| `tests/fixtures/reimbursement_summary/` | Small fictional examples covering the accepted layout and edge cases; no copy of private source data. |
| `docs/reimbursement-board-summary.md` | Install, monthly run, editorial review, dates, recovery, finalization, and attended acceptance instructions. |
| `docs/examples/reimbursement-summary-launchers.toml` | Copyable runner/preview/finalize launcher examples for the existing workspace registry. No machine-specific paths or identities. |

### Source contract used by this feature

The existing `ReimbursementReport` carries `settings` (`organization`, embedded logo,
`cutoff_date`, `as_of_date`), `tickets`, `supplemental.events`, and a summary property.
Each `Ticket` supplies `review_key`, `ref`, `form_label`, `origin`, `display_order`,
`submitted`, `submitted_label`, `payment_method`, `total`, `live` (workflow, decision,
payment status/date, confirmations), `review` (status, action, asks, note), and `items`.
Each item has an `item_key`, effective amount, status, and description. Status `A` is an
item recommendation/decision value, `C` needs clarification, `D` is declined, `Q` is a
question, and `-` is informational. Live `UNREVIEWED` must remain distinguishable from an
actual recorded approval. A `TicketEvent` has an event/evidence key, ticket key, kind,
date, amount, and reference. Only validated `PAYMENT_RECORDED` events count as payment
events; proposals, discrepancies, and quarantined records do not.

### New private files, schema version 1

All runtime files live under ignored `reports/output/`. Reject unknown keys, invalid
types, duplicate keys, non-finite money, malformed dates, invalid stable references, and
unsupported schema versions with a named field error. Money on disk is a two-decimal
string; calculations use `Decimal`. Dates are ISO `YYYY-MM-DD`. All new JSON is UTF-8.

| File / entity | Fields and constraints |
|---|---|
| `request.json` / `SummaryRequest` | `schema_version: 1`; `period_start`, `period_end` inclusive; `payment_start`, `payment_end` inclusive, default to reporting dates; `source_sha256` and optional `payment_notes_sha256` (64 lowercase hex); `prepared_at` UTC timestamp; `generator_version`. Require reporting start <= end <= bundle as-of, payment window contained in reporting window. |
| `payment-notes.json` / `PaymentNotes` | `schema_version: 1`; `records[]` of `ticket_review_key`, positive `amount`, `method` (`Check`, `Zelle`, `Other`), `reference` (nonempty text), `payment_date` (date or null), `recorded_on` (date), `source_note` (plain text), `proof_path` (private relative path or null). Describes an already-recorded settlement; never authorizes one. Proof may be absent. |
| `facts.json` / `SummaryFacts` | `schema_version`, source digest, reporting/payment windows, source cutoff/as-of, queue counts, active clarification amount, normalized payment records/totals/counts by method/date basis, open-case records, and diagnostics. Open cases include stable identity, display reference/form label, source anchor, submitted date/label, live decision, amounts and source wording. All values are derived from the frozen inputs. |
| `notes.json` / `SummaryNotes` | `schema_version: 1`; `source_sha256`; `discussion[]` and `followups[]` each contain `ticket_review_key`, `title`, `body`, `amount_kind` (`none`, `request`, `clarification`, `recorded_approved_unpaid`); `omitted[]` contains stable ticket key and reason; `narrative` is plain text; `reviewed: false` initially. Maximum 3 discussion rows, 4 follow-ups, and a 70-word narrative. There are no editable headline numbers. |
| `render-receipt.json` | `schema_version`, source/request/notes/template digests, generated file digests, page/overflow/link/offline checks, dependency versions, `status` (`preview` or `ready`). A ready receipt requires reviewed notes and all export checks. It is not a reimbursement approval. |
| `current-preview.json` | `schema_version: 1`; `preview_id` (lowercase UUID4 hex); relative path to that preview's receipt. This atomically replaced pointer selects a fully published preview directory; it never points at staging. |
| Final `manifest.json` | `schema_version`, `status: final`, reporting/payment dates, as-of, `run_id`, `edition_id`, finalization UTC time, generator/browser versions, source/request/notes/template digests, and relative file paths with SHA-256 and byte size. The finalize command records the operator's explicit act, not a fabricated board approval or signature. |

Identifiers are defined once in `reimbursement_summary.py`:

- `ticket_review_key`: existing opaque bundle key, e.g. `legacy:v1:example-case` or
  `submission:v1:` plus a SHA-256 digest. Copy and validate against the loaded bundle;
  never derive identity from names, display references, list order, or amounts.
- `run_id`: lowercase UUID4 hex, generated at prepare time. Used for
  `reports/output/reimbursement-summary/runs/{run_id}/`; no operator-supplied path fragments.
- `edition_id`: SHA-256 of canonical JSON containing schema/generator versions, source,
  request, reviewed notes, and template digests. Canonical JSON uses sorted keys, compact
  separators, UTF-8, no NaN. Finalization timestamps and generated PDF bytes are excluded
  from this identity; actual PDF/PNG checksums are recorded separately.
- `payment_key`: the existing stable ticket key, reflecting the current model's maximum
  one recorded settlement event per ticket. Payment figures count settled reimbursement
  records, not bank transfers. Never claim a count of distinct transfers from these inputs.
- `preview_id`: lowercase UUID4 hex generated by the renderer; identifies an immutable
  `previews/{preview_id}/` directory inside a run. Printed preview links include this ID,
  so a later render cannot change a document already open for review.

## 6. Design Decisions

### 6.1 Freeze inputs before generating a summary

Preparation creates a new private run directory containing `source.json`, `request.json`,
an optional payment-notes copy, `facts.json`, and `notes.json`. It snapshots file bytes once,
validates that copy with `load_bundle`, and generates the adjacent
`reimbursement-queue-breakdown.html` from the same report. Later stages use only that run.
Original bundle/anchor files are never changed by summary-only commands. Re-running prepare
creates a new run and never overwrites edited notes. Changing source data means preparing
a new run; changing wording only means re-rendering the current run.

This avoids scraping presentation HTML or reading live Sheets for numbers already present
in the strict source bundle. It also avoids silently importing a later queue update into
an edition the treasurer already reviewed.

### 6.2 Dates and headline figures

The page keeps a clear `Reporting period: start–end` line and a separate `As of` date from
the source bundle. The latter is the current queue snapshot date, not an assertion that
the pipeline reconstructs historical state. The source intake cutoff remains unchanged.
Queue counts include all tickets in the source, including older carryovers and any changes
through the displayed as-of. A small note identifies the counts as cumulative current-queue
figures; they are not cases received or closed during the selected month.

| Figure | Rule |
|---|---|
| Cases tracked | `len(report.tickets)`. |
| Closed | `len(report.closed_tickets)`. |
| Still open | `len(report.active_tickets)`; tracked = closed + open. |
| Needs clarification | Sum known effective amounts of status-C items in active tickets. Unknown amounts are not zero-valued resolved items; note their existence in the page when present. |
| Pie | Closed versus open, by the same case counts; rounded whole percent. Handle all-open/all-closed without invalid SVG arcs. The current source loader rejects an empty ticket array; propagate that error rather than fabricating a zero-case report. |

Use exact amounts internally and half-up rounding only at whole-dollar display boundaries.
Amounts in case follow-ups can retain cents. Never relabel source `summary.approved` or
`outstanding` as ready-to-pay money: those fields include item advice on unreviewed cases.
`recorded_approved_unpaid` is available only for a reviewed live decision with approved
items and `NOT_PAID`; mixed reviewed cases can have an approved portion even when their
overall decision is declined. Unreviewed cases never receive an imperative to pay.

### 6.3 Payment activity and paper checks

Default the payment window to the reporting window. Permit an explicitly narrower window
to reproduce a latest payment update; print that window in the payment card. This preserves
the accepted edition's batch meaning without pretending its payment amount was total
spending across the full reporting range.

Normalize one payment record per settled ticket. Prefer its validated `PAYMENT_RECORDED`
event for date/amount/reference. If no such event exists, a settled `PAID` or `PAID_PRIOR`
ticket with a known payment date supplies the reviewed ticket total and that date as a
legacy settlement record. Never parse free-form email prose or confirmation strings to
invent a transfer reference. Method aliases are a small explicit mapping; unknown strings
map to `Other`. Count labels describe reimbursements, not distinct bank transfers.

An optional `PaymentNotes` record supplies structured metadata for an already-settled case,
including paper checks with no issue date. It must match the ticket key, total, paid state,
and any known date/event reference. Validate notes before filtering by date, reject conflicts
and duplicate ticket entries, and merge with the existing payment record rather than adding
a second payment. A recognized source payment method must agree with the note; a note may
clarify an otherwise `Other` method. Require payment date <= recorded-on date <= source
as-of whenever those dates are present. A missing check scan does not prevent recording
the known facts; `proof_path` is metadata only and the renderer never opens it.

Dated payments enter the window by payment date. Undated checks enter a separate
`recorded_on` bucket only when supported by PaymentNotes. A combined card is labeled
**Recorded payment update**, with separate dated-payment and undated-check amounts and
the statement that check issue dates are unspecified. Without recorded-on evidence,
exclude an undated settlement from time-window totals and disclose its unknown-date count
in the payment note. Future-dated or inconsistent metadata is a validation error. No
unsupported subtotal is presented as all spending or a bank reconciliation.

The initial private payment register is an accepted source for a one-time conversion to
this metadata format. It is not an existing production schema. Preserve its original bytes
in the private migration evidence and validate the resulting records against the frozen
queue. Future payment-state entry continues through the existing reimbursement workflow.

### 6.4 Suggest wording, keep editorial control

Use deterministic templates rather than a new LLM dependency. Seed concise wording from
the structured live status and existing review action/asks; preserve the source qualifiers
such as requested, uncertain, reported conflict, and pending receipt review. Default title
is the first nonblank canonical category, falling back to `Reimbursement review`; include
the display reference and form label. Source prose is data and is always escaped.

Candidates for discussion are active clarification/unreviewed/question cases ranked by
known clarification amount descending, then ticket total descending, then display order.
These are suggestions for agenda selection, not a claim that every such case needs board
approval. Other active cases seed follow-ups in display order. Seed text must not invent
policy conflicts, vendor deadlines, or a requirement to void a check from keywords alone.
If the source wording cannot fit cleanly, leave it in the private notes with an edit-needed
diagnostic instead of cutting off a qualification. An operator can select a policy case
manually from the full active list.

The private notes can place a ticket in both sections for distinct issues, as the accepted
layout does. Every active key must occur in discussion, follow-ups, or explicit omissions.
No duplicate key within one section. Omitted cases remain in headline totals and the full
queue; when any are omitted, the page states that further open items are in the source.
Preparation lists all overflow candidates in omissions with reasons and sets `reviewed`
false. The operator can adjust wording/order/selection and set reviewed true. This is a
content-review step; it cannot change a source decision or certify a payment.

The default closing paragraph is limited to factual count/status statements derived from
the snapshot, with deterministic singular/plural handling. The operator can replace it
with a short narrative such as the approved reference. Do not offer unsupported trend or
turnaround claims without earlier snapshots and a separately designed metric.

### 6.5 Rendering and the one-page limit

Use the accepted private template as a visual reference, then author a generic Jinja2
template with `StrictUndefined` and autoescaping. Its inline CSS, embedded logo and inline
SVG must render without network access or JavaScript. No remote fonts, tracking pixels,
payment handles, phone numbers, bank references, or raw email excerpts belong on the board
page. The detailed report stays private; distribute just the summary HTML/PDF.

Keep the approved teal/gold palette, four figures, two-column chart/payment block, three-row
discussion table, up to four follow-ups, and small closing narrative. Empty sections show
a truthful short message. Long organization names, large figures, fewer/more cases, date
ranges crossing years, and all-open/all-closed charts must remain legible. The footer keeps
the source link, rounding note, date range, and page count.

Add optional `[summary]` dependencies `playwright==1.58.0` and `pypdfium2>=5.13.0`, locked in
`uv.lock`. Reuse Chromium, which produced the accepted reference, rather than the existing
WeasyPrint extra. Install browser binaries explicitly during setup; never download them
during an ordinary report run. `pypdfium2` checks only PDFs generated by this process; it
does not open external receipt or bank PDFs or change the statement parser's sandbox.
HTML preparation/rendering remains available without the export extra; finalization needs
it and reports a concrete setup command when missing.

Render a final-shaped preview without draft numbers; preview/final state is shown in the
CLI receipt and private run metadata. This makes finalization a copy of the reviewed
document rather than a new layout. Screen validation includes 390px mobile overflow;
print validation requires one 612×792-point Letter page, expected text, visible footer,
no clipped elements, no horizontal overflow, and zero network requests. Minimum sizes
remain those of the accepted design (approximately 11px body and 9px footnotes). If it
does not fit, preserve the prior preview/final and name the section to shorten. Never
silently omit content, shrink the whole page, truncate prose, or publish a second page.

Case links use the existing full-report anchor transformation of `ref` plus `form_label`.
Validate that each link resolves once in the generated detailed report. On an anchor
collision, link to the full report without a fragment and retain the display reference;
do not introduce a summary-only change to existing queue anchor semantics.

### 6.6 Commands, output, and errors

These are **planned commands**, unavailable until their build steps ship. All run from the
repository root. Existing commands and the current `end-to-end-run` launcher stay intact.

| Command | Inputs | Observable result |
|---|---|---|
| `prepare-reimbursement-summary` | `--data` (default current bundle); required `--period-start`, `--period-end`; optional paired `--payment-start`, `--payment-end`; optional `--payment-notes`; `--output-root` (default `reports/output/reimbursement-summary`). | Validated frozen run, facts and suggested editable notes; print run path, dates, aggregate counts, and diagnostics. No Google access. |
| `report-reimbursement-summary` | Required `--run` path; optional `--html-only`. | Reread/validate frozen source and edited notes, rebuild derived facts, publish a complete new HTML/PDF/PNG preview directory with its receipt, then atomically advance the preview pointer. `--html-only` writes HTML and marks PDF checks pending; cannot finalize. No source reads outside the run and no Google access. |
| `finalize-reimbursement-summary` | Required `--run` path. | Require reviewed notes and a successful unchanged export receipt; validate all recorded hashes; publish or return the same immutable final edition. Never invoke Gmail, recompute different content, or infer board approval. |
| `uv run python scripts/run_reimbursement_summary.py` | Summary date/payment options above; `--no-refresh`; `--dry-run`; existing refresh arguments after `--`. | Default: refresh locally through existing CLI, then prepare and render; explicit `--fetch-since` in forwarded args enables Gmail. `--no-refresh` uses current reviewed data. It prints the run path to resume after editing notes. It never finalizes. |

Runner `--dry-run` invokes the existing refresh dry-run and validates summary inputs without
creating a run. It identifies any summary estimate as based on the current local bundle;
Gmail match counts are not a preview of newly downloaded data. Existing OAuth consent may
still occur when fetching; document that inherited behavior. `--no-refresh` rejects forwarded
refresh arguments; dry-run never reaches prepare or render. Forwarding is a Python argument
list to `cli.main`, and the runner returns the first nonzero status. The runner owns `--data`
and `--dry-run`: reject either after the `--` separator, pass the runner's chosen bundle path
to both refresh and prepare, and insert the dry-run flag itself. This prevents a forwarded
dry-run from accidentally being followed by writes or a different bundle from being summarized.

Exit statuses: 0 success; 1 validation/render/export failure; 2 argparse usage error.
Use concise stage-specific messages and diagnostic codes (`INVALID_INPUT`, `EDIT_REQUIRED`,
`EXPORT_SETUP`, `PAGE_OVERFLOW`, `STALE_RENDER`, `RUN_BUSY`, `FINAL_CONFLICT`). Never log names,
email bodies, payment references, private notes, or secrets. A failure after refresh explicitly
says the queue was updated and the summary was not published; it does not pretend to roll
back the existing refresh command.

### 6.7 Final archives and recovery

Use `reports/output/reimbursement-summary/final/{period_start}-to-{period_end}/{edition_id}/`.
Only finalize writes here. The final contains the exact reviewed HTML/PDF/PNG, adjacent
detailed HTML for links, frozen inputs, notes, template copy, receipt, and manifest. Finalize
resolves `current-preview.json` once while holding the run lock, checks its receipt against
the current notes and frozen input hashes, then copies only that preview's validated bytes.
Publish by staging on
the same volume and moving a fully validated directory into its unused final name; never
merge files into an existing edition. If the same edition exists with valid hashes, return
it unchanged. Different reviewed notes produce a new edition instead of replacing history.
Set archived files read-only and record hashes; this prevents ordinary workflow overwrites,
not intentional filesystem-administrator changes.

All output must resolve beneath the invocation working directory's `reports/output/`, which
is ignored when invoked from the repository root as documented, including a custom output root;
reject paths escaping that boundary, symlink/junction escapes, and output/input overlap.
Record manifest paths relative to the edition, not arbitrary filesystem targets. Acquire a
per-run/edition exclusive lock before mutations so concurrent render/finalize calls fail
with `RUN_BUSY`. On a crash, leave a clearly named staging directory and lock with PID/time;
the guide explains inspecting the process and removing only that abandoned run's lock.
Never auto-delete a live lock or an earlier final. Failed render/export keeps the previous
complete outputs and receipt. For previews, stage the complete file set, move it to a new
`previews/{preview_id}/` directory, then atomically replace `current-preview.json`. A crash
before pointer replacement leaves the old preview selected and at most an unselected complete
directory. Readers never have to guess which of several partly replaced files belong together.
A final manifest is written before the final directory is published as a complete set.

The prototype finalized during planning lives under
`reports/output/approved/reimbursement-summary-2026-06-01-to-2026-09-10/` with read-only
HTML/PDF/PNG, its detailed report, private source/reference files, and `manifest.json`.
It remains the accepted reference and is not overwritten or silently migrated by the
production workflow. A fresh checkout can build against fictional fixtures using the
design contract above; private acceptance requires the operator's local reference.

## 7. Build Steps

The first five steps are code work; Step 31 is attended acceptance. Run them sequentially.
No conditional placeholders, credentials in tests, or cross-repository writes are needed.
Code steps must exercise their real production caller. Deep review is used for new
schema/financial derivation, rendering, workflow wiring, and final-publication boundaries;
ordinary code review covers the smoke harness. Browser evidence is generated by tests over local files,
so no server URL or runtime reviewer is required.

### Step 26: Prepare a dated summary review packet

- **Problem:** Expose a preparation command that derives an editable monthly summary packet from validated frozen reimbursement inputs.
- **Type:** code
- **Status:** PENDING
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `pta_finance/reimbursement_summary.py`, `pta_finance/cli.py`, `tests/test_reimbursement_summary.py`, `tests/test_reimbursement_cli.py`, `tests/fixtures/reimbursement_summary/`.
- **Produces:** `prepare-reimbursement-summary`, strict new private schemas, deterministic facts/suggestions, input snapshots, and synthetic examples.
- **Done when:** The actual CLI prepares a packet with correct date windows, cumulative counts, exact active-C totals, payment deduplication and unknown-check-date handling; conflicting/malformed inputs fail before publication. Suggestions cannot turn unreviewed advice into approval. Repeated preparation preserves earlier edited notes. Existing reimbursement CLI tests pass.
- **Depends on:** none.

<!-- autofix-applied: 2026-09-10 -->
### Step 27: Render the accepted one-page HTML design

- **Problem:** Expose a render command that turns a reviewed packet into the approved style of offline board summary.
- **Type:** code
- **Status:** PENDING
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `pta_finance/reimbursement_summary_render.py`, `pta_finance/reports/templates/reimbursement_board_summary.html.j2`, `pta_finance/cli.py`, `tests/test_reimbursement_summary_render.py`, `tests/fixtures/reimbursement_summary/`.
- **Produces:** `report-reimbursement-summary --html-only`, the generic template, adjacent detailed report, and a receipt with export checks pending.
- **Done when:** The real command reproduces all approved sections with fictional data; every displayed count/date/chart description is derived, text is escaped, case/form links resolve, empty/all-open/all-closed examples render correctly, and stale notes or invalid selections fail without replacing the previous complete HTML. Markup and source-link checks pass.
- **Depends on:** Step 26.

### Step 28: Preserve an approved one-page export

- **Problem:** Finalize an unchanged reviewed preview into a validated immutable HTML/PDF edition.
- **Type:** code
- **Status:** PENDING
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `pta_finance/reimbursement_summary_render.py`, `pta_finance/cli.py`, `pyproject.toml`, `uv.lock`, `tests/test_reimbursement_summary_render.py`, `.github/workflows/ci.yml`.
- **Produces:** Default render with PDF/PNG, summary export extra, `finalize-reimbursement-summary`, hash manifests, read-only editions, concurrency/error handling, and browser-export CI coverage.
- **Done when:** Chromium produces one Letter page offline on Windows and Linux; the final command copies precisely the reviewed content, rejects edited/stale inputs and missing/failed export checks, returns an unchanged valid existing edition on retry, preserves old finals on all failures, and creates a separate edition after a legitimate wording change. Overflow and concurrent-write probes fail visibly without corrupting a prior output set. The built wheel contains the template.
- **Depends on:** Step 27.

<!-- autofix-applied: 2026-09-10 -->
### Step 29: Provide the monthly Python runner

- **Problem:** Let the treasurer prepare the monthly summary through one local command with the existing refresh behavior.
- **Type:** code
- **Status:** PENDING
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** `scripts/run_reimbursement_summary.py`, `docs/reimbursement-board-summary.md`, `docs/examples/reimbursement-summary-launchers.toml`, `tests/test_reimbursement_summary_pipeline.py`, `README.md`, `CLAUDE.md`, `SETUP.md`, `docs/loading-receipts.md`.
- **Produces:** A runner calling the existing refresh CLI followed by summary prepare/render, concrete setup/recovery/review instructions, copyable launchers, and a private-reference conversion procedure for the accepted payment register.
- **Done when:** The runner accepts the documented dates and forwarded arguments, honors no-refresh/dry-run, stops on the first failure, preserves finals, and prints a usable resume command. The guide explains editing/reviewing notes, separate intake/payment/reporting dates, receipt-proof optionality, one-page failures, and source-review limits. Launcher commands parse and preserve exit status without modifying the workspace registry.
- **Depends on:** Step 28.

### Step 30: Exercise one complete real-component cycle

- **Problem:** Prove the packaged commands can complete one summary cycle through their actual interfaces.
- **Type:** code
- **Status:** PENDING
- **Issue:** #
- **Flags:** --reviewers code --isolation worktree
- **Files:** `tests/test_reimbursement_summary_pipeline.py`, `tests/fixtures/reimbursement_summary/`, `.github/workflows/ci.yml`, `docs/reimbursement-board-summary.md`.
- **Produces:** A bounded smoke gate wiring an actual synthetic email archive, anchor/category inputs, refresh CLI, snapshot preparation, note edit, render, finalize, and final manifest verification; no mocks at the producer/consumer boundaries.
- **Done when:** With dependencies/browser already installed, one fixture cycle completes within a 60-second smoke budget, outputs one validated page, and resolves detailed links from the archived copy. Repeating finalization changes no bytes; a second period creates a separate edition; a deliberately changed note requires re-render; a render failure preserves the previous final. Google construction/network access is forbidden in this smoke. Run full appropriate repository quality gates and build/install the wheel before the step is marked done.
- **Depends on:** Step 29.

### Step 31: Accept the workflow on the treasurer's Windows machine

- **Problem:** Confirm that the installed monthly workflow produces a usable private board packet from the actual reviewed queue.
- **Type:** operator
- **Status:** PENDING
- **Issue:** #
- **Files:** No code changes; run the acceptance procedure already written in `docs/reimbursement-board-summary.md`.
- **Produces:** Private run evidence, a checked comparison with the accepted reference, and an operator acceptance result or a concrete defects list.
- **Done when:** The operator runs the real helper with the chosen date windows, reviews the suggested wording, verifies a known paid case and a received clarification against the queue, confirms paper checks remain undated where evidence is absent, previews/prints one legible page, and finalizes a new edition. A retry returns the same final and a later run leaves it unchanged. Record source refresh failures as findings rather than weakening evidence rules. A private no-refresh replay reproduces the accepted reference's figures and editorial choices with its narrower payment window. Acceptance may not be claimed solely from unit tests.
- **Depends on:** Step 30.

## 8. Risks and Open Questions

No unresolved architecture choice blocks implementation. Defaults D1–D7 in the Appendix
stand under the operator's plan-expedite go-ahead; later scope changes must be reflected in
both the plan and its synchronized issues.

| Item | Risk | Mitigation |
|---|---|---|
| Mail coverage / strict parser gaps | A summary may accurately reflect an incomplete queue. | Surface unmatched/unreviewed diagnostics in the private packet; require source review for content correctness. No “all email reconciled” claim. |
| Meaning of reporting period | Cumulative case figures mistaken for monthly throughput. | Label current-queue counts and as-of separately from reporting/payment windows; no retroactive state reconstruction. |
| Missing check issue dates | Recorded date mistaken for actual payment date. | Separate date basis and disclose unknown issue dates; missing scans do not invent dates. |
| Recommendations versus approval | Item-A advice on an unreviewed request becomes a payment instruction. | Use live decision plus item state; test mixed reviewed cases and unreviewed recommendations explicitly. |
| Agenda suggestions | Source facts do not encode every board-policy nuance. | Deterministic suggestions plus editable wording/selection; no invented policy judgment. |
| A busier month | Open items outgrow one page. | Explicit omissions with a source-report note; require edit/re-render on overflow, never hidden truncation. |
| Private reference not in a checkout | CI cannot reproduce the actual organization’s report. | Synthetic fixture tests plus the inline design contract; private acceptance is a distinct operator step. |
| Export/browser setup | Missing binary or platform font differences break printing. | Locked package/browser versions, explicit setup, Windows/Linux export checks, clear failure before finalization. |
| Concurrent or interrupted run | Mismatched files or overwritten approved edition. | Per-run lock, staged publication, manifest commit marker, no-overwrite finals, fault-injection tests. |
| Shared CLI/CI files | Slide work touches the same files later. | Independent numbering, no dependency on slide modules, retain existing jobs/extras, rerun affected gates after rebase. |

## 9. Testing Strategy and Quickstart

### Required coverage

Use parameterized synthetic cases for month/year boundaries, paid-date versus recorded-date
selection, source as-of later than reporting end, older carryovers, active-only clarification,
unknown amounts, already-recorded payments appearing in notes, duplicate display references
with distinct form labels, unreviewed item-A advice, mixed reviewed approvals/declines,
lost-check informational lines, zero recent payments, and all-open/all-closed pies.

Test the production CLI entry point for ordinary success and critical failures. Verify all
generated output remains private, markup escapes text, custom paths cannot escape the
output boundary, unknown-date payments are not silently attributed, and no Google client
is constructed by the offline commands. Existing `test_reimbursement_report.py`,
`test_reimbursement_pipeline.py`, and `test_reimbursement_cli.py` retain their assertions.
Do not rewrite the source report schema merely to make a summary fixture pass.

Use actual Chromium exports for the one-page, offline, mobile, text, and clipping checks.
Keep visual fixtures deterministic with fictional content. Do not require PDF byte equality
across machines; compare document content/layout while checking immutable bytes on retries
of an existing final. Export failures, note changes after rendering, process interruption,
and concurrent finalization must preserve a previous valid edition.

### Build and development commands

After Step 28 supplies the new extra, from the repository root:

```powershell
uv sync --locked --extra dev --extra slides --extra summary
uv run python -m playwright install chromium
uv run pta-finance --help
uv run pytest -q tests/test_reimbursement_summary.py tests/test_reimbursement_summary_render.py tests/test_reimbursement_summary_pipeline.py
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict pta_finance
uv build
uv run python scripts/check_no_identity.py
```

Development is the CLI run above; there is no dev server or port. Run the full repository
suite with the existing platform policy before marking an implementation step complete:
Windows with the slides extra runs `uv run pytest -q`; Linux retains the exclusions and
explicit non-Windows rejection test in `.github/workflows/ci.yml`. The summary-export CI job
installs Chromium (with OS dependencies on Linux) and runs its own real browser tests.
Wheel validation installs the built wheel into a temporary clean environment and exercises
prepare/render with bundled fictional input and the packaged template; it must not rely on
the source checkout supplying missing package data.

### Monthly operation after implementation

1. Install the export extra and Chromium once. Existing mail refresh additionally needs
   private `config.toml`, category mapping, anchors, and the Gmail credentials already
   described in `SETUP.md`; offline summary commands need none of those credentials.
2. Review/update reimbursement evidence using the established workflow. Supply payment
   notes only for settlement metadata that the current bundle cannot represent, such as
   a check's recorded date. Do not mark an unpaid case paid in summary notes.
3. Choose explicit reporting dates. The helper refreshes the source locally by default;
   add `--fetch-since` after `--` when new mail acquisition is wanted. For example:

   ```powershell
   uv run python scripts/run_reimbursement_summary.py --period-start 2026-09-01 --period-end 2026-09-30 -- --fetch-since 2026-09-01 --as-of 2026-09-30
   ```

   These are illustrative dates, not defaults or a claim that future source data exists.
   `--no-refresh` reuses the reviewed local bundle. Use the printed run path for all later
   commands; do not reuse a path from an older reporting period accidentally.
4. Edit that run's `notes.json`, checking discussion items, omissions, and the narrative;
   set `reviewed` true. Re-run `report-reimbursement-summary --run` with the printed path.
   Inspect the summary HTML/PDF. If the source itself needs correction, prepare a new run.
5. Run `finalize-reimbursement-summary --run` with that path. Share only its summary HTML
   or PDF; source snapshots and detailed reports contain private working material. The
   final path and manifest identify exactly what was presented.

### Development handoff

This plan follows plan-review → plan-redline → plan-wrap before issue creation. Next,
`plan-expedite --plan documentation/reimbursement-board-summary-plan.md` from this repository
rechecks the plan and synchronizes issues; `build-phase --plan
documentation/reimbursement-board-summary-plan.md` then executes numbered steps in order.
Each code step gets an isolated worktree, its declared code review, and the repository
quality gates. Step 31 is attended, with no code-authoring obligation. Blank Issue fields
are expected now and must be filled by repo-sync before build-phase starts.

## Appendix

### Decision Inventory

| ID | P/D | Choice | Status |
|---|---|---|---|
| P1 | P | One-page, plain-language board/principal/teacher-representative summary using the approved HTML style, compact figures, one pie, discussion list, and closing paragraph. | Accepted in conversation. |
| P2 | P | Show a date range as well as an as-of date. | Accepted in conversation. |
| P3 | P | Remove draft numbering and lock the final reference. | Completed for the private approved edition on 2026-09-10. |
| P4 | P | Plan a Python workflow similar to end-to-end-run. | Delivered; plan-expedite authorized 2026-09-10. |
| D1 | D | Suggest deterministic wording in editable private notes; operator reviews before finalizing. | Stands under plan go-ahead 2026-09-10; no LLM service. |
| D2 | D | Keep cumulative current-queue counts; use separate explicit reporting/payment windows. | Stands under plan go-ahead 2026-09-10; preserves the accepted count semantics. |
| D3 | D | Default payments to the reporting window; allow a narrower latest-update window and separately identify undated checks by recorded date. | Stands under plan go-ahead 2026-09-10; no invented check issue dates. |
| D4 | D | Three CLI stages plus one Python refresh/prepare/render helper; monthly invocation is manual. | Stands under plan go-ahead 2026-09-10; no scheduler. |
| D5 | D | Chromium export via a dedicated optional summary extra; fail when the page does not fit. | Stands under plan go-ahead 2026-09-10; matches the accepted export route. |
| D6 | D | Frozen runs and read-only, checksum-verified final editions; changed wording creates a new edition. | Stands under plan go-ahead 2026-09-10; implements the requested final lock. |
| D7 | D | Phase 6, Steps 26–31: prepare, HTML, final export, runner, real-component smoke, attended acceptance. | Stands under plan go-ahead 2026-09-10; phase label recorded for issue synchronization. |

### Acceptance reference

The private approved directory named in §6.7 contains the final reference and checksums.
Its manifest records the source data and source-template hashes. Its screenshot is the
visual oracle, while its editorial notes explain the figures, date bases, and selected
agenda. Nothing in that directory is a committed fixture. Use the approved source/reference
copies for the private replay; never infer hard-coded names, amounts, counts, or case IDs
from this plan's generic example data.
