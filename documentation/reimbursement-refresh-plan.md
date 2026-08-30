# Reimbursement refresh pipeline plan

## 1. What This Feature Does

Add a deterministic reimbursement-report workflow with three explicit layers: the existing
`fetch-mail` command updates the private local mail archive, a new offline report command renders
HTML from a validated private data bundle, and a new orchestration command runs acquisition,
evidence preparation, and rendering in order. The workflow keeps Gmail access, local report
generation, and Google Sheet writes as separate permission boundaries.

## 2. Existing Context

- `pta_finance.gmail_source` and `fetch-mail` already provide idempotent, read-only Gmail
  acquisition into a flat private `.eml` archive.
- `pta_finance.receipt_ingest` parses lossless submission and line-item evidence; the existing
  `receipt_map` projection intentionally keeps a smaller dashboard-oriented schema.
- `map-receipts` owns the machine-managed `Reimbursements` replacement. It remains a separate,
  explicit write rather than being hidden inside report generation.
- Existing financial reports already use typed Python models followed by autoescaped Jinja HTML.
  The reimbursement report will follow the same pattern.
- Real emails, review decisions, report inputs, and generated output remain below gitignored
  private paths. Committed code, tests, and documentation use fictional identities only.

## 3. Scope

In scope:

- A strict, versioned private reimbursement-report bundle.
- A deterministic, credential-free Jinja renderer and atomic HTML writer.
- Local evidence preparation from the existing `.eml`/`.mbox` parser.
- Stable submission and line keys, evidence hashes, and review-staleness detection.
- Separate report and end-to-end refresh CLI commands.
- Aggregate-only console output and fictional test fixtures.

Out of scope:

- Sending email, initiating payment, or making unscoped policy decisions automatically.
- A third Google Sheet tab.
- Hidden or unattended Sheet writes.
- Gmail credentials in CI or scheduled mailbox ingestion.
- Automatic model calls. Any future semantic suggestion layer must be optional and operator-gated.

### 2026-08-30 supplemental-evidence amendment

Schema v2 adds an append-only supplemental mail lane without weakening the immutable original-form
lane. It stores normalized RFC ancestry, top-authored text digests, decoded attachment metadata and
content hashes, normalized event timestamps, canonical evidence/event record digests, linked
lifecycle events, and a visible unmatched queue. The configured received cutoff scopes both source
submissions and supplemental mail. Linkage is exact RFC
ancestry or a strict private operator anchor only. Existing schema-v1 reviews and original evidence
hashes migrate unchanged; disappearance or mutation of accounted supplemental evidence fails
closed.

New submissions receive deterministic, non-authoritative A/C advice from metadata rather than a
generic Q placeholder, but their recorded decision remains UNREVIEWED. Explicit private operator
reviews may update every item on one exact ticket. A configured payment operator's exact linked
confirmation may settle only that ticket, and a configured secondary approver's short top-authored
reply may authorize only a fully parsed, explicitly anchored proposal. Payment remains a separate
event; ambiguous proposals, spoofed senders, quoted approvals, and unrelated affirmatives do not
mutate decisions.

## 4. Impact Analysis

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `pta_finance/reimbursement_report.py` | add | Strict bundle model, validation, totals, emails, rendering entry point, atomic output | New module; existing report model/render pattern inspected in `pta_finance/reports/` |
| `pta_finance/reimbursement_pipeline.py` | add | Parse/filter/deduplicate private evidence, preserve reviewed records, identify new or stale tickets, orchestrate stages | Parser and mapper producers inspected; Message-ID consumers grep-checked in `receipt_map.py`, `cli.py`, and private Review overlay |
| `pta_finance/reports/templates/reimbursement_queue.html.j2` | add | Complete data-driven reimbursement HTML without source-code literals or marker surgery | Existing Jinja loader and templates inspected |
| `pta_finance/cli.py` | extend | Register offline report and refresh commands | All `build_parser`, `main`, `_cmd_fetch_mail`, and `_cmd_map_receipts` call sites grep-checked; CLI functions are local to this module/tests |
| `tests/test_reimbursement_report.py` | add | Bundle, rendering, privacy, email, determinism, and atomic-write gates | Standard pytest discovers `tests/` only |
| `tests/test_reimbursement_pipeline.py` | add | Evidence-key, cutoff, new/stale review, stage ordering, and failure-stop gates | Existing synthetic receipt fixtures inspected |
| `tests/test_reimbursement_cli.py` | add | Parser and orchestration wiring tests | All CLI calls go through `cli.main` or direct test seams |
| `docs/loading-receipts.md` | extend | Operator commands and permission/write boundaries | Existing acquisition and mapping workflow inspected |
| `README.md`, `CLAUDE.md`, `plan.md` | extend | Current-state and command inventory | Command and architecture sections located with repository-wide search |

No existing public schema or function signature is changed. The new private bundle is additive,
so the dashboard-oriented 15-column `Reimbursements` contract remains backward compatible.

## 5. New Components

### Private report bundle

A versioned JSON document under `reports/output/` holds report metadata, provenance, stable ticket
and item keys, source evidence, adjudications, message drafts/history, and appendix material. New
or changed source evidence is visibly unreviewed or stale; unchanged reviewed records retain their
operator-authored decisions.

### Offline report renderer

The renderer validates the complete bundle before producing any output. It computes all counts and
money splits in Python, composes routine emails deterministically, supplies a presentation-only
model to Jinja, and atomically replaces the requested private HTML file.

### Refresh orchestrator

The orchestrator runs the existing Gmail acquisition stage, refreshes local report evidence, then
renders the report. It stops after a failed stage and prints aggregate counts only. It does not send
mail, decide policy, or write Sheets.

## 6. Design Decisions

**Three permission boundaries.** Gmail acquisition, local rendering, and Sheet writes remain
separate commands. The convenience orchestrator combines only acquisition and local report work;
an operator must still invoke an explicit Sheet-writing command.

**Preserve two tabs.** Item-level evidence and adjudication live in the private bundle initially,
so no `Reimbursement Review Items` worksheet is introduced.

**Stable persisted identities.** Tickets and items use hash-derived source keys while human-facing
references are persisted in the private bundle. A late-arriving older message cannot renumber an
already reviewed ticket.

**Per-record freshness.** Each reviewed ticket records a canonical evidence hash and policy
version. Only new or changed evidence needs semantic review; unchanged tickets require no model or
human re-analysis.

**Full-template rendering.** One complete Jinja template replaces legacy HTML string surgery.
Autoescape applies by default; any intentionally formatted legacy content must use a narrowly
validated representation rather than arbitrary trusted HTML.

**One-shot operation.** These commands run on operator request and exit. They add no autonomous,
scheduled, background, or always-on behavior, so the long-running observation trigger does not
apply.

## 7. Build Steps

### Step 1: Add the validated reimbursement report model and renderer

- **Status:** DONE (2026-08-27)
- **Problem:** Replace snapshot-specific report construction with a strict private bundle and one
  full Jinja template.
- **Issue:** #24 (posterity closeout)
- **Flags:** `--reviewers code --isolation worktree`
- **Produces:** report model/loader, template, deterministic email composition, atomic writer,
  fictional unit tests
- **Done when:** the standard suite proves validation, totals, privacy, escaping, determinism,
  email coverage, and atomic output
- **Depends on:** none

### Step 2: Add local evidence preparation and the three-stage command surface

- **Status:** DONE (2026-08-27)
- **Problem:** Connect existing mail acquisition to evidence refresh and offline rendering without
  coupling in Sheet writes.
- **Issue:** #24 (posterity closeout)
- **Flags:** `--reviewers code --isolation worktree`
- **Produces:** evidence preparation module, report CLI, refresh orchestrator, aggregate-only output
- **Done when:** synthetic new, unchanged, and stale evidence flows exercise all stages and stop on
  the first failure
- **Depends on:** Step 1

### Step 3: Migrate and compare the current private report bundle

- **Status:** DONE (2026-08-27)
- **Problem:** Convert the current ignored snapshot into the new schema without committing private
  values and prove semantic parity.
- **Issue:** #24 (posterity closeout)
- **Flags:** `--reviewers code --isolation worktree`
- **Produces:** private ignored bundle and parity/readback artifacts
- **Done when:** one real local cycle parses the current archive, validates every reviewed ticket,
  renders HTML, and matches the current ticket/item/action/email aggregates without touching Gmail
  or Sheets
- **Depends on:** Steps 1 and 2

### Step 4: Document and run the operator smoke gate

- **Status:** DONE (2026-08-27)
- **Problem:** Make each command's reads, writes, and required confirmations unambiguous.
- **Type:** operator
- **Issue:** #24 (posterity closeout)
- **Produces:** none
- **Done when:** the operator can run email-only, report-only, and combined flows independently and
  verify the combined command completes one real local cycle without Sheet mutation
- **Depends on:** Step 3

## 8. Risks and Open Questions

| Item | Risk | Mitigation |
|---|---|---|
| Private-data leakage | Real identities enter tracked fixtures or logs | Fictional fixtures, gitignore path guard, aggregate-only console output, repository search gate |
| Evidence changes after review | Old decisions attach to changed items | Canonical per-ticket evidence hash; mark stale and fail publication or render a visible stale state |
| Late-arriving mail | Positional `NEW-##` identifiers shift | Persist display references; never derive existing refs from current sort order |
| Partial pipeline failure | Report appears current after acquisition/preparation failure | Stop immediately; atomic output; print completed stage and unchanged output status |
| Hidden remote mutation | Convenience command unexpectedly changes Sheets | Orchestrator performs no Sheet write; keep existing explicit `map-receipts --write-tab` boundary |
| Legacy formatted text | Unsafe HTML bypasses autoescape | Convert to plain text or validate a tiny allowlist before rendering |

## 9. Testing Strategy

- Unit-test strict bundle schemas, duplicate IDs, finite money, status rollups, evidence freshness,
  stable references, deterministic templates, all payment placeholders, and injection escaping.
- Unit-test evidence selection across `.eml` plus `.mbox`, reply filtering, cutoff inclusivity,
  content deduplication, stable source/item hashes, and preservation of reviewed fields.
- CLI-test report-only and combined stage ordering, aggregate-only output, failure short-circuiting,
  and absence of Sheet construction.
- Run `pytest`, Ruff check/format, and strict mypy over tracked code.
- Run a real-component local smoke gate over the current private mail archive and bundle. The smoke
  gate may write only gitignored evidence/report artifacts and must not contact Gmail or Sheets.

Completion evidence (2026-08-27): 436 tests passed with one optional skip; strict mypy, Ruff lint,
and Ruff format checks passed. The private report was regenerated from the migrated bundle and its
desktop/mobile layouts were independently reviewed from browser screenshots plus structured
read-back evidence.
