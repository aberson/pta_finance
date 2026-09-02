# Phase 5.1, Wave 1 - Treasurer summary Google Slide - feature plan

Master roadmap: [`treasurer-slides-plan.md`](treasurer-slides-plan.md)

Global build numbering continues after the shipped project work at Step 14. This plan owns Steps
14-25 only. Steps 14-24 are code steps; Step 25 is an attended private operator acceptance gate.

> **Identity rule.** This repository is public and the finance data is private. No real
> organization, school, person, email address, account identifier, financial value, Google file
> identifier, OAuth credential, bank statement, extracted transaction, source path, template,
> candidate, screenshot, or detailed quality-assurance evidence may appear in tracked code,
> documentation, tests, fixtures, examples, issues, or logs. Private artifacts stay under configured
> gitignored paths.

## 1. What This Feature Does

Wave 1 turns a reviewed set of private Wells Fargo PDF statements plus the adopted annual goals in
the private `Budget Timeseries` worksheet into one editable private Google Slide. It reproduces the
approved one-slide cash-basis summary: opening and latest supplied account status, current-board
fundraising and spending, spending and collection-channel composition, progress against annual
goals, an explicit prior-period exclusion, source/as-of caveats, and a terse deterministic
narrative. It is being built first because it is independently useful and proves the bank-to-Slide
pipeline before the project invests in a multi-slide graphic catalog or a large workflow engine.

The workflow has one human financial checkpoint. `prepare-treasurer-summary` extracts and
reconciles facts into a private review artifact and prints its SHA-256 digest. Only
`create-treasurer-summary` with that exact digest may copy and populate the private Google Slides
template. The tool never approves its own facts, modifies the finance workbook or template, changes
Drive permissions, publishes, or sends anything.

## 2. Existing Context

- The installed command is `pta-finance = pta_finance.cli:main` in `pyproject.toml`; the single
  argparse registry is `build_parser()` in `pta_finance/cli.py:1320`, and `main()` dispatches at
  `pta_finance/cli.py:1734`.
- `pta_finance/report_source.py:62,101` defines and reads the operator-maintained
  `Budget Timeseries` worksheet. Proposed income and proposed expense rows can supply the two
  annual goals. Current live FY data has no actual rows, so bank-derived actuals cannot come from
  this worksheet.
- `report_source.read_timeseries()` currently has exactly five call sites:
  `pta_finance/cli.py:165,313,396` and `tests/test_report_source.py:229,243`. Its dictionary
  projection cannot detect duplicate raw headers, so Wave 1 leaves it and all five callers
  unchanged and adds a narrow raw-grid budget-goal reader instead.
- The current report path already demonstrates exact amount parsing, immutable report models,
  build/render separation, deterministic output, and a runtime public-data guard in
  `pta_finance/models.py`, `pta_finance/analytics/aggregate.py`, and
  `pta_finance/reports/{builder,charts,render}.py`.
- `pta_finance/reimbursement_report.py:1385,1474,1506` is the closest precedent for strict private
  input validation, deterministic rendering, hashes, and atomic output replacement.
- `pta_finance/gmail_source.py:118,402,586,621,661` is the precedent for an exact-scope,
  separately stored desktop OAuth token. Slides must use a distinct token and must not import or
  widen Gmail authorization.
- Read-only inspection found four statement PDFs with embedded text and two image-only PDFs that
  require rasterization plus local optical character recognition (OCR). Step 14 established the
  tracked `treasurer_slides` package and strict models; this plan adds the bank-statement parser,
  OCR adapter, Slides client, and slide-specific tests.
- The approved v0 is a private, read-only, checksum-pinned reference under the gitignored prototype
  output tree. Its code hardcodes real reconciled values and uses `python-pptx`; it is a visual and
  calculation oracle, not production source code or a committed fixture.
- `reports/output/`, `doc/`, `config.toml`, `secrets/`, and local task state are gitignored. The
  public repository remains generic; all Wave 1 real runs and Google identifiers stay ignored.

## 3. Scope

### In scope

- Wells Fargo PDF statements and current-activity reports only, behind a source-adapter boundary
  that can accept other formats in later waves.
- The three fixed account roles `checking`, `savings`, and `time`, displayed as Checking, Savings,
  and Time Account (Buffer).
- Coordinate-bearing embedded-text extraction with local Tesseract TSV OCR fallback for image-only
  or insufficient-text pages.
- A strict private input manifest that declares statement paths, account roles, document kinds,
  reporting start date, as-of date, budget fiscal year, and the private rules file.
- Individually dated balance observations plus normalized transactions with source digest,
  coverage/as-of dates, posted/pending status, direction, exact positive `Decimal` magnitude,
  sanitized description, semantic identity, and source-row identity.
- Deterministic overlap selection, unique transfer and reversal matching, pending-activity handling,
  transaction classification, and exact-match adjustments/exclusions.
- A signed cash-basis fact model that separates net raised funds, bank interest, net current-board
  spending, excluded prior-period outflows, paired transfers/reversals, boundary-crossing contra
  activity, and pending items.
- Adopted fundraising and expense goals from FY proposed `Budget Timeseries` rows.
- The operator-requested board/reporting-year clock kept distinct from the budget fiscal year.
- Exact reconciliation, fixed category limits for the approved layout, progress/pacing, source
  notes, and deterministic narrative generation.
- One immutable private run snapshot and one digest-bound fact approval before any Slides write.
- One tokenized private 16:9 template, uploaded/converted by the app, copied per approved run, and
  populated as editable native text and shapes.
- A dedicated desktop OAuth token requesting exactly `drive.file`, private workspace/run manifests,
  minimal duplicate prevention, and no permissions API calls.
- Fictional unit/integration fixtures, an OCR-enabled Windows real-component local smoke gate,
  packaging, operator documentation, and one attended real-data/real-Google acceptance run.

### Out of scope

- The Wave 2 multi-slide deck, projected-spending pie/donut, or any other new graphic.
- The former fourteen-graphic catalog, generic presentation engine, appendix system, or multiple
  themes.
- Banks or inputs other than the specified Wells Fargo PDFs; CSV, QFX, and OFX are later adapters.
- Writing actuals, classifications, or any other value back to the finance workbook.
- Automatic categorization by a model, fuzzy financial inference, or transmitting private data to
  an external model/API.
- A comprehensive multi-stage approval state machine, source-refresh workflow, or remote candidate
  promotion system; those are Wave 3 concerns.
- Public/audience-safe deck variants. Wave 1 is internal/board-facing and remains private.
- Automated sharing, permission changes, publication, email, insertion into another presentation,
  scheduling, background execution, or unattended OAuth.
- PowerPoint as the production output. The private PPTX is a one-time template import source; the
  delivered Wave 1 artifact is a native Google Slides presentation.
- Reimbursement commitments, reserves policy, sustainability, forecasts, historical comparisons,
  or claims not supported by the Wave 1 bank and budget inputs.

## 4. Impact Analysis

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `pta_finance/treasurer_slides/{__init__,models}.py` | extend | Keep Step 14's package boundary and strict private models compatible with the parser contracts | Step 14 added both tracked files; their public types are the Wave 1 producer/consumer boundary |
| `pta_finance/treasurer_slides/{bank_statements,native_sandbox,native_worker}.py` | add | Strict positioned token/page, statement, account, balance, transaction, OCR, and Windows-native-parser sandbox contracts | Step 14 intentionally did not add a bank/PDF/OCR producer; private inspection confirmed four text PDFs and two image-only PDFs |
| `pta_finance/treasurer_slides/{rules,reconciliation,facts,budget_goals}.py` | add | Source authority, dated intervals, classification, exact adjustments, transfer/reversal handling, strict raw-grid budget-goal read, signed reconciliation, pace, and narrative | Current approved prototype hardcodes these results; `report_source.py:62,88-119` identifies the live tab and proves dictionary projection loses duplicate-header evidence |
| `pta_finance/treasurer_slides/{summary,template}.py` | add | Approved layout roles, text formatting, category folding, bar geometry, template inspection, and pure Slides request plan | The target render modules are absent; the existing package currently contains only contracts and must remain compatible |
| `pta_finance/treasurer_slides/google_client.py` | add | Dedicated exact-scope OAuth, template import/copy, Slides merge, app-owned candidate lookup, and redacted API boundary | `gmail_source.py:118,402,586,621,661` confirms the separate OAuth precedent; no Slides/Drive presentation client exists |
| `pta_finance/treasurer_slides/pipeline.py` | add | Atomic private run creation, fact review, digest approval, CLI orchestration, and minimal duplicate-safe resume | `reimbursement_report.py:1385,1474,1506` confirms strict-load/render/atomic-write precedent; no Treasurer run pipeline exists |
| `pta_finance/treasurer_slides/templates/fact_review.html.j2` | add | Autoescaped private financial review before Google creation | Existing Jinja templates are under `pta_finance/reports/templates/`; no Treasurer template exists |
| `pta_finance/config.py` | extend | Add optional `TreasurerSlides` paths/settings with `None` preserving existing configs | `rg '\bConfig\('` finds the sole constructor at `config.py:242`; no caller directly constructs `Config` elsewhere |
| `config.example.toml` | extend | Add fictional optional Treasurer Slides/OCR path examples | File read in full; optional `[gmail]` and `[receipt_mapping]` blocks are the existing compatibility pattern |
| `pta_finance/cli.py` | extend | Register init, prepare, and create commands without changing existing flags | `build_parser` is at `cli.py:1320`; all 13 current `set_defaults(func=...)` registrations are local to that function and remain intact |
| `pyproject.toml`, `uv.lock` | extend | Add a `slides` extra with permissively licensed `pypdfium2`; document Tesseract 5 as a system dependency | Dependency table read in full; no PDF-input/OCR package or Slides extra is present; Google API/auth clients already exist; PyMuPDF was rejected because its official distribution is AGPL/commercial |
| `.github/workflows/ci.yml` | extend | Keep portable/static and fail-closed coverage on Linux; on Windows, install the Slides extra and Tesseract, then exercise the LPAC native-parser/OCR boundaries and real-component smoke | Workflow currently runs `uv sync --extra dev` and has no system-package step |
| `scripts/check_no_identity.py` | extend | Reject tracked Treasurer statements, templates, run artifacts, tokens, and real-resource manifests while preserving fictional tests | Script currently checks service-account markers plus the optional identity denylist only; its sole CI invocation is `.github/workflows/ci.yml` |
| `tests/test_treasurer_slides_models.py` | extend | Keep Step 14 manifest/model contracts compatible with parser facts | Step 14 added the tracked fictional model suite |
| `tests/test_treasurer_slides_{bank_statements_native,native_sandbox,bank_statements_ocr,source_authority,rules,reconciliation,budget_goals,facts,summary,template,pipeline,auth,workspace,google_candidate,cli,smoke}.py`, `tests/fixtures/treasurer_slides/**` | add | Fictional positioned PDF/OCR, parser-boundary, source authority, reconciliation, budget, review, template, Google fake, CLI, privacy, packaging, and smoke coverage | Step 14 deliberately added only model tests; no parser/OCR/Slides test or fixture exists yet |
| `tests/test_config.py` | extend | Prove the optional Treasurer Slides block preserves legacy configs and validates every supplied path | Existing optional-section coverage is at `tests/test_config.py:113-175`; no Treasurer Slides block exists |
| `docs/generating-treasurer-summary.md` | add | Exact operator setup, inputs, review, OAuth, creation, and recovery procedure | Docs glob confirms no Treasurer summary guide exists |
| `README.md`, `SETUP.md`, `CLAUDE.md`, `plan.md` | extend | Document the new optional command surface, dependency, privacy boundary, status, and staged roadmap | Each file exists and was read during discovery; current project status does not mention the staged Wave 1 workflow |

No existing function signature, schema field, or shared constant changes in Wave 1.
`report_source.read_timeseries()` and its five existing callers remain unchanged. The new
`budget_goals.py` imports only `BUDGET_TIMESERIES_TAB` and consumes a read-only `read_values`
protocol so duplicate raw headers remain observable. `SheetsClient`, analytics, existing HTML
reports, reimbursement code, and existing CLI semantics remain behaviorally unchanged.

Private run data, source PDFs, input/rules manifests, OAuth files, workspace IDs, converted
template, candidate IDs, screenshots, and acceptance evidence are not tracked impact rows. They
remain under `reports/output/`, `doc/`, `secrets/`, or Google Drive.

## 5. New Components

### 5.1 Optional configuration

`Config` gains `treasurer_slides: TreasurerSlides | None = None`. The private optional block uses
paths only; it contains no Google resource ID:

```toml
[treasurer_slides]
client_secrets_file = "secrets/treasurer-slides-client.json"
token_file = "secrets/treasurer-slides-token.json"
workspace_manifest = "reports/output/treasurer-slides/workspace.json"
runs_root = "reports/output/treasurer-slides/runs"
tesseract_command = "tesseract"
```

All paths resolve relative to `config.toml`. Before any private source is read or output is written,
a shared privacy gate locates the containing Git worktree, if any. A repo-local input/output path
must be untracked and must match `git check-ignore --no-index`; a tracked or non-ignored path blocks
before filesystem or remote mutation. Paths outside a Git worktree are allowed after the ordinary
containment/type checks. Git is invoked with an argument array and no shell. The gate covers client
secret/token, workspace/bootstrap, runs/diagnostics, input manifest, rules, every PDF, and the private
PPTX. Omitting the config block preserves every current command and test. The module reads paths but
never prints secret contents, tokens, statement text, or Google IDs.

### 5.1.1 Development and supported-host setup

The native PDF boundary is supported only on Windows. A developer installs the repository's locked
development and Slides dependencies with `uv sync --extra dev --extra slides`; Step 16 additionally
requires a local Tesseract major version 5 executable for its OCR smoke. Linux and other hosts may
run the portable/model/test suite, but the native statement path must fail before a source-file read.
The operator workflow remains one-shot, but the Step 15 boundary is not limited by host-native thread
count: `CreateProcessW` uses `bInheritHandles=False`, and the broker transfers only the two anonymous
PDF-channel endpoints directly into the independently attested child after startup.

### 5.2 Private input and rule manifests

`prepare-treasurer-summary --inputs <private-json>` accepts a strict version-1 manifest:

```text
schema_version
reporting_start_date
as_of_date
budget_fiscal_year
cash_basis
documents[]: account_role, document_kind, relative_path
rules_relative_path
```

`cash_basis` is exactly `available_including_pending` in Wave 1. Account role is exactly
`checking`, `savings`, or `time`. Document kind is exactly `monthly_statement` or
`current_activity`. Dates are strict ISO dates, the as-of date cannot precede the reporting start,
and the budget fiscal year is explicit rather than inferred from either date.

Document/rule paths are relative to the manifest directory, contain no `..`, resolve beneath that
directory, traverse no symlink or Windows reparse point, and name regular files. The manifest and
rules file are each capped at 1 MiB; PDF limits are fixed in the extraction contract. Each
account role must have enough coverage to establish its start and latest supplied balance. The CLI
never infers account role or authority from a private filename, and the shared privacy gate also
checks the input manifest plus every resolved document/rule path.

The strict private rules file contains:

- `classification_rules[]` with a unique `rule_id`, optional account role, direction, a closed
  normalized-description matcher (`exact`, `prefix`, or `contains`), `cash_role`, and display
  category, plus a required non-empty `pair_key` for `transfer` or `reversal`;
- `overlap_resolutions[]` naming one exact selected source row and one exact rejected source row when
  overlapping documents cannot prove semantic equality;
- `pair_resolutions[]` naming exactly two selected transaction selectors and the action
  `pair_as_transfer` or `pair_as_reversal`; and
- `transaction_adjustments[]` with one exact selected transaction selector, action, and non-empty
  private reason.

An exact selector contains account role, effective date, status, direction, canonical amount,
normalized description, occurrence ordinal, source SHA-256, page number, and source-row ordinal.
All fields must agree after authoritative-source selection; zero or multiple matches block. This
prevents an adjustment from drifting to a legitimate same-amount duplicate after an input changes.

`cash_role` is one of `fundraising`, `spending`, `interest`, `transfer`, or `reversal`; Wave 1 has no
catch-all `other` role. Every affecting transaction must match exactly one classification rule and
every transfer/reversal role must become exactly one valid pair. The Wave 1 adjustment
`exclude_from_current_board_spend` is valid only for an unpaired `spending` debit: the debit remains
in the bank cash bridge and excluded-outflow total but is absent from current-board spending,
category shares, and pace. It never changes opening/latest balances, cash change, or the adopted
expense-goal denominator.

### 5.3 Statement extraction and normalization

`bank_statements.py` exposes a `StatementExtractor` protocol and one Wells Fargo PDF implementation
using permissively licensed `pypdfium2` for both embedded text and rasterization. The account role
comes only from the input manifest. Masked/full account identifiers encountered in a document are
discarded and never become model or log fields.

Both native and OCR paths emit the same `PositionedToken` records: page number, normalized
page-relative bounding box, text, extraction method, and confidence (`100` for native text).
Native characters are grouped into words by line and x-gap. A page with fewer than 80 non-whitespace
native characters or without a known page fingerprint is rasterized at 300 DPI and streamed through
the Step 16 LPAC Tesseract boundary to Tesseract major version 5 as `eng`, OEM 1, PSM 6, TSV output,
with a 45-second per-page timeout. Tesseract is invoked with an argument array and no shell. Any
token used for a date, money, direction, status, balance, or table header with confidence below 75
blocks parsing; low-confidence
description-only tokens remain visible as review evidence.

The hard v1 limits are 25 MiB per PDF, 25 pages, 2,000,000 extracted characters, 20,000,000 rendered
pixels per page, and 2,500 transaction rows per document. Raster input and TSV output cross the
separate OCR boundary only through bounded pipes and are never copied into a run or an ambient
temporary directory. If a future OCR dependency cannot stream, it must use a per-run directory inside
its AppContainer local data that is ACLed only for that worker, and cleanup must retain ownership until
the worker has exited; otherwise the operation fails closed. Errors identify only a safe document
ordinal and page number, not its path or text. The full PDF backend, parser-contract, and Tesseract
versions and the pinned OCR arguments are recorded in private lineage.

Before `pypdfium2` receives PDF bytes, the broker must start a one-shot Windows Low Privilege
AppContainer (LPAC) worker through the narrow `native_sandbox.py` launcher. The LPAC has exactly one
enabled capability, `registryRead`, which CPython needs for runtime initialization on the supported
host; it has no network capability and opts out of `ALL_APPLICATION_PACKAGES`. It receives a sanitized
environment and safe working directory, and cannot access the caller profile or worktree. A per-run
public-only runtime is ACLed only for the LPAC SID; it contains the selected interpreter, package code,
and native-library dependencies, never a private PDF, manifest, rule, or user configuration. The
launcher creates the child suspended with `bInheritHandles=False`, assigns it to a kill-on-close Job
Object with CPU, memory, and one-active-process limits, and resumes it with no PDF-channel endpoint.
The worker can initially open only generated Low-IL named control objects containing public names and a
nonce. It self-attests its token/Job state and signals the broker; the broker independently re-queries
that exact process for its AppContainer SID, sole `registryRead` capability, `WIN://NOALLAPPPKG`
attribute, and Job membership before directly duplicating the request reader and response writer into
that child with non-inheritable handles. The control mapping carries only those child-local handle
numbers and the nonce. The worker then emits the versioned `READY` frame before either side reads or
writes source bytes. No sandbox/profile/runtime/control cleanup failure may fall back to an ordinary
child. Because no PDF channel is inherited through `CreateProcessW`, concurrent unrelated host child
launches cannot receive a PDF-channel handle.

The Step 15 PDF worker's one-active-process Job deliberately prevents it from launching Tesseract.
Step 16 therefore has the broker launch `tesseract.exe` directly as the one active process in a
separate LPAC Job. Before streaming rendered raster bytes, the broker verifies that Tesseract's token,
capability policy, staged public program/data access, and Job limits match the OCR contract. It is not
an ambient subprocess, a helper-wrapper child, or a child of the PDF worker. Non-Windows hosts fail
closed before reading a statement. Windows CI proves the true-LPAC positive/negative attestation,
pre-read startup ordering, cleanup behavior, and real-parser regression with fictional fixtures;
Linux runs only portable logic and the pre-read fail-closed assertion. The dedicated AppContainer
profile is isolated application state, not access to the caller's ordinary user profile.

Page parsing is versioned. A `wf-v1` format fingerprint combines normalized page dimensions,
required marker sets, and ordered table headers; it never uses a filename or account identifier.
Known page kinds are monthly summary, monthly activity, current balance, current activity, and
bank boilerplate. A recognized boilerplate page produces no facts and is recorded as ignored.
An unknown or contradictory page blocks. For activity tables, detected header boxes define
non-overlapping normalized date/description/debit/credit bands; token centroids assign cells to a
band. A debit/credit ambiguity or a row spanning both amount bands blocks, so plain OCR order can
never determine transaction direction.

Each `StatementObservation` contains source SHA-256/safe ordinal, account role/document kind,
coverage dates, capture date, versioned page evidence, `BalanceObservation[]`, and transactions.
Each balance has kind (`opening`, `closing`, `collected`, or `available`), exact amount, effective
date, boundary (`start_of_day`, `end_of_day`, or `capture`), whether it includes pending activity,
and safe source locator. This permits collected, available, and captured values in one document to
retain different dates and bases.

A transaction stores effective date, positive finite cent-precision `Decimal` magnitude, direction
(`credit` or `debit`), status (`posted` or `pending`), normalized private description, occurrence
ordinal, parse evidence, and safe source locator. Its signed cash movement is positive for a credit
and negative for a debit. Description normalization is exactly Unicode NFKC, Unicode casefold,
Unicode dash-to-ASCII-hyphen, whitespace collapse, and outer-whitespace trim; internal punctuation
and digits remain private and unchanged. A source-row ID hashes parser version, source SHA-256,
page/table/row ordinals, and normalized row coordinates. A separate cross-document semantic key
hashes account role, effective date, status, direction, canonical amount, normalized description,
and occurrence ordinal. Occurrence ordinals are assigned in source-row order within the preceding
semantic fields before the ordinal. Money is never parsed through binary float, and summary text
such as a period-interest label is never treated as activity unless it occurs in a recognized
transaction row.

### 5.4 Overlap, transfers, reversals, and reconciliation

Source authority is fixed before any financial classification. For each account, closed monthly
statements own posted activity through their coverage end. A later-captured `current_activity`
document may supply posted rows only after the last closed cutoff, current pending rows, and the
latest balance; its posted rows inside a closed period are cross-check evidence, never authority.
The overlapping posted multisets must agree by semantic key, or every difference needs one exact
private overlap resolution. Among documents of the same kind, a newer capture takes precedence only
after the overlap agrees. This prevents OCR from silently overriding a closed embedded-text statement.

The engine then executes one explicit order: source authority and overlap resolution; source-row
and semantic identity; balance-boundary and interval selection; classification; transfer/reversal
pairing; one-off adjustment; signed aggregation; account reconciliation; combined reconciliation.
No later stage can change an earlier identity, interval, or source-authority decision.

An automatic transfer requires two `cash_role=transfer` rows with the same non-empty rule-supplied
`pair_key`, equal magnitudes, opposite directions, different account roles, dates within three
calendar days, and no competing row. A reversal requires two `cash_role=reversal` rows with the same
non-empty `pair_key`, equal magnitudes, opposite directions, the same account role, dates within five
calendar days, and no competitor. Otherwise an exact two-row private resolution is required. An
amount alone is never pairing evidence. Both legs must lie inside their accounts' selected balance
intervals; a mixed-cutoff one-leg internal transfer blocks until source coverage is corrected.

Signed contributions after pairing and adjustment are exhaustive; the explicit exclusion row
overrides the general spending row:

| Cash role | Credit | Debit |
|---|---|---|
| `fundraising` | increases net funds raised | decreases net funds raised as a refund/chargeback |
| `spending` without exclusion | decreases net current-board spending as a refund | increases net current-board spending |
| `spending` with `exclude_from_current_board_spend` | invalid | increases excluded prior-period outflows; no current-board-spend contribution |
| `interest` | increases interest | invalid in Wave 1 |
| paired `transfer` | no combined raised/spent/interest contribution | no combined raised/spent/interest contribution |
| paired `reversal` | no contribution | no contribution |

An unpaired return is never discarded: it must be classified as a fundraising debit or spending
credit and contributes as boundary-crossing contra activity. Net fundraising, net spending,
interest, and every displayed category must remain non-negative; a negative result blocks rather
than producing a misleading graphic.

The reporting interval begins immediately before `reporting_start_date` and includes activity from
that date through each account's selected latest boundary. No selected transaction or balance
boundary may be later than manifest `as_of_date`; later source content is ignored only when a known
document contract proves the row lies outside the requested interval, otherwise it blocks. An
explicit opening balance at the reporting-start boundary takes precedence. Otherwise the engine may
back-solve opening from the earliest authoritative dated end-of-day/capture balance with complete
coverage:

```text
back-solved opening = anchor balance - signed selected activity from reporting_start_date
                      through the anchor boundary
```

Every later independent balance anchor must reproduce the same opening to the cent. The latest
balance per account is the available-at-capture observation when it includes the selected pending
rows; otherwise it is a closing/collected balance plus the explicitly enumerated pending movements.
Account-specific dates and bases remain visible. Posted and pending subtotals are separate, and a
pending row contributes only when the selected latest balance includes it.

The engine reconciles every account over its own declared boundaries. A paired transfer remains a
signed movement inside each account even though it cancels in the combined view:

```text
opening account cash
+ net fundraising in the account
+ net bank interest in the account
- net current-board spending in the account
- excluded prior-period outflows in the account
+ signed paired-transfer movement in the account
= latest account cash
```

Paired reversal legs are in one account and net to zero. For the combined bridge:

```text
opening cash
+ net fundraising (credits less refunds/chargebacks)
+ net bank interest
- net current-board spending (debits less refunds)
- explicitly excluded prior-period outflows
= latest supplied cash
```

Transfers and paired reversals are absent from combined activity but must reconcile inside the
account bridges. Any unmatched role, interval ambiguity, or cent of unexplained difference blocks
fact approval and Google output.

`operating_surplus_before_interest` is exactly net fundraising minus net current-board spending; it
excludes bank interest and excluded prior-period outflows. Consequently, `cash_change` is exactly
operating surplus before interest plus net interest minus excluded prior-period outflows. Both
equations are stored and tested, not inferred by the narrative.

### 5.5 Budget goals, fact model, pace, and narrative

`budget_goals.py` defines a `BudgetTimeseriesGridReader` protocol exposing only
`read_values(tab)`. Its concrete adapter may wrap the existing `SheetsClient`, but neither
`facts.py` nor any downstream component receives that write-capable client. Tests use a reader with
no write surface and sentinels prove that no append/update/replace/delete method is called.

The adapter reads the raw `Budget Timeseries` grid so it can reject a blank or duplicate header
before converting any row to a mapping. Every `report_source.TIMESERIES_COLUMNS` name must occur
exactly once; extra unique headers are retained only in the private source hash. Relevant proposed
rows require strict fiscal year, type, measure, raw category, and finite cent-precision `Decimal`
amounts. The logical key
`(fiscal_year, normalized_raw_category, normalized_type, normalized_measure)` must be unique.
Normalization matches `budget_sync._norm_name`: split/collapse all Unicode whitespace and
Unicode-casefold; type and measure also trim before casefolding. Original display text is retained
privately, but casing/spacing variants collide and block rather than double-count. For the declared
budget fiscal year the adapter sums:

- proposed fundraising goal: `measure=proposed`, `type=income`; and
- proposed expense budget: `measure=proposed`, `type=expense`.

Malformed/short relevant rows, duplicate logical rows, invalid amounts, no matching rows, or
non-positive totals block. Wave 1 does not require actual rows and calls neither
`report_source.read_timeseries()` nor `report_source.to_inputs()`.

`SummaryFacts` contains:

- reporting start/as-of dates and the distinct budget fiscal year;
- opening and latest supplied cash plus account-specific balances, dates, and balance bases;
- net raised funds, interest, net current-board spend, excluded outflows, cash change, and operating
  surplus before interest;
- posted/pending subtotals and reconciliation equation;
- up to four spending display groups and five fundraising collection-channel groups, folding a
  fully classified residual into explicit `Other` when needed;
- annual goals, exact progress ratios, board-year elapsed ratio, and pace verdicts;
- the deterministic story text and complete source/caveat footer; and
- source/rules/workbook content hashes and classification coverage.

Display groups aggregate the signed contributions by the rule-supplied label, drop exact zeroes,
and require positive total. If the number exceeds the layout capacity, spending retains the three
largest groups and fundraising the four largest; ties sort by Unicode-casefolded label and then
original UTF-8 label, and every remainder (including a literal `Other`) is merged into one final
`Other` group. Otherwise groups sort by descending amount with the same tie-break. Group amounts
sum exactly to the corresponding headline before rounding. Percentages are allocated in integer
tenths by largest remainder, breaking equal remainders in display order, so visible shares total
100.0%. Fundraising groups are explicitly collection channels unless a later source proves
program-level meaning.

The pace clock is the operator-declared reporting/board year, from `reporting_start_date` through
the day before its next anniversary. The `as_of_date` must lie inside that interval. Elapsed days
are inclusive: `(as_of_date - reporting_start_date).days + 1`; total days are
`(next_anniversary - reporting_start_date).days`. It is not silently replaced by the budget fiscal
year start. The footer labels the comparison as board-year bank actuals against the declared FY
annual target.

Fundraising is `ON PACE` when its exact progress ratio is no more than one tenth of a percentage
point below the exact elapsed ratio; it is otherwise `BEHIND PACE`. Spending is `WITHIN PACE` when
its exact budget-used ratio does not exceed the elapsed ratio; it is otherwise `AHEAD OF PACE`.
These are straight-line current-rate screens, not forecasts: timing and seasonality are not modeled,
and `WITHIN PACE` means the budget is not being consumed too quickly, not that every planned program
will be delivered. The approved slide and narrative disclose that limitation rather than asserting
goal attainment.

The narrative is a deterministic format over approved facts. It may state cash change, the effect
of explicitly excluded outflows, current-board raised/spent amounts, operating surplus before
interest, and pace verdicts. It cannot introduce a fact, causal claim, projection, or category not
present in `SummaryFacts`.

### 5.6 Private run and fact approval

`prepare-treasurer-summary` atomically claims:

```text
reports/output/treasurer-slides/runs/<YYYYMMDDTHHMMSSZ>-<12-lowercase-hex>/
  input.manifest.snapshot.json
  sources.snapshot.json
  transactions.normalized.json
  facts.snapshot.json
  summary.plan.snapshot.json
  review.html
  manifest.json
  candidate.receipt.json  # created once, only after a candidate exists
```

The run ID uses UTC plus 48 random bits. The run root is configured and path-contained. Files are
created privately, validated, and committed with the manifest last; those prepared artifacts are
never rewritten. A successful `create` may add the candidate receipt exactly once, but cannot alter
the approved snapshot or manifest. JSON rejects unknown root fields and binary floats. `Decimal`
values serialize as plain decimal strings, dates as ISO strings, and canonical hashes use UTF-8,
sorted object keys, compact separators, and preserved array order.

Rule authoring is not a blind loop. If extraction succeeds but overlap, balance, classification,
pairing, selector, adjustment, or reconciliation validation fails, `prepare` atomically writes a
separate non-approvable private diagnostic under
`reports/output/treasurer-slides/diagnostics/<run-id>/`. Its autoescaped inventory includes all
extracted source rows, exact selectors, parse evidence, tentative rule matches, and stable error
codes needed to correct the private rules. It contains no `facts_sha256`, cannot be passed to
`create`, and is never promoted into a run; the operator changes inputs/rules and prepares a new
run. Parse failures that cannot produce trustworthy rows remain status-only errors.

`facts_sha256` covers the complete canonical `facts.snapshot.json` payload with its digest field
omitted. That payload includes the hashes of the input, source, normalized-transaction, private-rule,
and raw-budget-grid snapshots. `summary_plan_sha256` covers the canonical `SummarySlidePlan` defined
in Section 5.7, including every rendered string and geometry value. The displayed
`approval_sha256` covers a versioned canonical envelope containing both hashes, so approval binds
the complete producer and presentation plan.

The autoescaped private review shows every headline and account balance/basis, plus one row for every
selected normalized transaction: date, status, direction, amount, sanitized description, safe
source ordinal/page/row, native/OCR method and confidence/ambiguity evidence, classification rule,
cash role, category, pair/adjustment, and signed headline/category contribution. It also shows
rejected overlap rows, posted/pending subtotals, all transfer/reversal pairs, exact adjustment and
reason, per-account and combined reconciliation, goal rows/source, calendar/pace basis, every slide
string, and the digest. It never renders an account identifier or filesystem path.

`create-treasurer-summary --run <run-id> --approval-sha256 <digest>` accepts exactly the displayed
64-character lowercase digest. It revalidates all local artifact hashes and builds only from that
immutable facts/summary-plan snapshot; it never silently refreshes PDFs/Sheet or regenerates approved
strings after approval. A rejected review is corrected by changing the private input/rules and
preparing a new run.

### 5.7 Approved summary and template contract

`summary.py` is a pure builder from `SummaryFacts` to a `SummarySlidePlan`. It contains generic
theme tokens, formatting rules, role names, text, and dynamic geometry derived from the approved
private v0. It contains no real organization identity, logo, value, source path, or screenshot.

The private template is one exact 16:9 slide. Its organization name/header, logo, and branding are
static private template content and never enter `SummaryFacts` or a replacement request. The
operator prepares it from the approved visual by replacing only variable content with unique tags
of the form `{{TS:<role>}}`. The code-owned role registry covers:

- period and current-snapshot label;
- opening/latest/change values and labels;
- three account names, values, and as-of basis;
- raised/spent/interest/exclusion values and labels;
- four spending labels/amounts/shares plus stacked-bar markers;
- five fundraising labels/amounts/shares plus bar markers;
- both goal/progress rows, elapsed benchmark, and pace labels;
- story narrative and source/caveat footer.

Every required tag must occur exactly once and every dynamic marker must belong to one shape. No
unknown `{{TS:` tag may remain. The pure request planner maps tags to discovered object IDs, replaces
text, and emits absolute transforms for stacked/progress bar widths and positions. It clamps ratios
to the declared geometry and rejects negative, non-finite, out-of-bounds, overlapping, or
zero-total bar plans. The template itself supplies all private identity and branding.

### 5.8 Google authorization and rendering

Slides uses a dedicated desktop OAuth client/token requesting exactly
`https://www.googleapis.com/auth/drive.file`. Google documents this as the recommended per-file
scope and documents the template-copy plus `replaceAllText`/`batchUpdate` pattern:

- <https://developers.google.com/workspace/slides/api/scopes>
- <https://developers.google.com/workspace/slides/api/guides/merge>
- <https://developers.google.com/workspace/slides/api/reference/rest/v1/presentations/batchUpdate>
- <https://developers.google.com/workspace/drive/api/guides/manage-uploads>
- <https://developers.google.com/workspace/drive/api/reference/rest/v3/files/create>
- <https://developers.google.com/workspace/drive/api/reference/rest/v3/files/copy>
- <https://developers.google.com/workspace/drive/api/guides/user-info>
- <https://developers.google.com/identity/protocols/oauth2>

Operator setup enables both Drive and Slides APIs and creates a dedicated Desktop OAuth client. A
Workspace operator may choose an Internal consent app; otherwise the app remains External/Testing
with the operator account listed as a test user. Documentation calls out Google's seven-day refresh
token lifetime for an External app left in Testing and treats `invalid_grant` as an actionable
reauthorization of only the dedicated Slides token. The implementation never imports, widens,
deletes, or overwrites the Gmail token.

`init-treasurer-summary-template --pptx <private-template>`:

1. loads or mints the separate token and rejects the credential unless its granted-scope set equals
   the one required scope;
2. reads the opaque Drive principal from `about.user.permissionId`, checks
   `about.importFormats` for PowerPoint-to-Google-Slides conversion, and never persists the user's
   email/display name;
3. acquires an atomic-create workspace-init lock held through manifest commit; a concurrent init
   blocks, and a crash-stale lock requires documented operator inspection/removal rather than
   automatic age-based reclamation;
4. hashes the OAuth client ID and PPTX, then atomically creates private `workspace.bootstrap.json`
   with schema/workspace version, random bootstrap key, OAuth-client hash, Drive principal, and
   expected source PPTX SHA-256 before any remote mutation; an existing bootstrap/workspace client
   or principal mismatch blocks before search or creation;
5. fully paginates a broad app-owned query anchored only by workspace/bootstrap keys, then partitions
   results by resource role. Zero anchored resources permits creation; otherwise the exact expected
   folder/template cardinality, source hash, role, MIME type, parent, and visibility must validate.
   Any anchored provenance mismatch or extra resource blocks rather than looking like a zero;
6. creates the folder and uploads/converts the tokenized PPTX when absent, always with
   `ignoreDefaultVisibility=true`; the original folder-create and template-upload request carries
   the complete workspace/bootstrap/source-hash and `role=folder|template` app properties, so a lost
   response remains discoverable; the template has exactly that app-owned folder as parent, and
   readback requires the folder and template both report `shared=false`;
7. validates one 16:9 slide and the complete role/tag contract; and
8. atomically writes the private workspace manifest with folder/template IDs, OAuth-client hash,
   Drive principal, source PPTX SHA-256, bootstrap key, and canonical converted-template projection
   hash, then releases the workspace-init lock in `finally`.

A valid workspace rerun with the same source hash validates and reuses it; a different PPTX requires
a different workspace/bootstrap rather than rebinding an orphan. A crash after folder or template
creation resumes from app properties and does not upload a duplicate. The canonical projection includes page size
and every element's type, normalized geometry, static/dynamic text, relevant shape/text styles, and
image geometry/title/description, sorted by page and geometry; it excludes API object IDs,
revision metadata, and expiring content URLs. This provides a stable Wave 1 drift gate while Wave 3
retains stronger rendered-image comparison. The command never accepts or calls a permissions
endpoint.

`create-treasurer-summary` requires the approved envelope digest before constructing a Google client.
It first acquires one atomic-create run lock; another creator blocks, and a crash-stale lock requires
the documented operator check/removal rather than time-based automatic reclamation. It re-fetches
the source template, recomputes the canonical projection hash, and blocks on any drift before copy.
It first confirms the current OAuth-client hash and Drive `permissionId` equal the private workspace
values. It then
fully paginates a broad app-owned query anchored only by workspace key and run ID. Zero anchored
resources permits one template copy with
`ignoreDefaultVisibility=true` and exactly the app-owned private folder as parent. The original copy
request explicitly overwrites inherited template properties with candidate
workspace/run/approval-digest/`role=candidate` properties, making a lost response discoverable as a
candidate rather than a template. Otherwise exactly one anchored result must have the expected role,
approval digest, MIME type, parent, and unshared state; any mismatch or extra result blocks. An ambiguous
copy result repeats that broad paginated reconciliation search rather than another mutation.

The tool never updates the template. Before Slides mutation it classifies candidate readback as one
of three states: exact pristine template, exact expected post-state for this approved plan, or other.
Exact post-state writes/resumes the private receipt without another mutation. Exact pristine state
permits one atomic Slides `batchUpdate` containing the complete text/geometry request list and a
`writeControl.requiredRevisionId` from that read. Any other state blocks. If the batch response is
lost, the handler performs readback only: expected post-state is success, pristine is left for an
explicit rerun, and partial/unexpected state blocks; it never blindly retries. Readback must show
one 16:9 slide, the expected text/object count, no unresolved tag, and no out-of-bounds planned
element before a create-once candidate receipt records the Google ID/URL, applied revision ID, and
readback hash inside the private run. The run lock is released in `finally`.

Wave 1 retries only safe reads and idempotent validation. It does not automatically retry an
ambiguous remote mutation beyond the app-property reconciliation above; richer bounded retry and
revision handling belong to Wave 3. No method changes sharing, publishes, emails, exports a final,
or touches the source spreadsheet.

### 5.9 CLI surface

The new argparse commands are:

```text
uv run pta-finance init-treasurer-summary-template \
  --pptx <private-tokenized-template> [--config config.toml]

uv run pta-finance prepare-treasurer-summary \
  --inputs <private-input-manifest> [--config config.toml]

uv run pta-finance create-treasurer-summary \
  --run <run-id> --approval-sha256 <approval-digest> [--config config.toml]
```

`prepare` constructs the existing Sheets service-account client only inside the narrow read-only
budget adapter plus local statement/OCR components; the fact engine cannot reach write methods. It
performs no Slides OAuth or Drive mutation. `init` performs only one-time app-owned
workspace/template setup and reads no financial sources. `create` reads only the approved private
run plus the workspace manifest and performs no source refresh.

Console output is intentionally small: command status, safe run ID, review/manifest/candidate-receipt
path, and approval digest. The private receipt, rather than stdout/stderr, holds the candidate Google
location needed by the operator. Console output never echoes a statement path, OCR text,
transaction description, account identifier, financial value, token, raw Google response, or raw
exception body.

## 6. Design Decisions

1. **Ship in waves.** Wave 1 proves one valuable summary. Wave 2 adds a few slides/graphics; Wave 3
   deepens operational rigor; later plans add one capability slice at a time. The superseded broad
   plan is not a hidden requirement list.
2. **Use a Wells Fargo adapter behind a protocol.** A source-specific parser is honest about the
   current evidence and smaller than a premature universal bank parser. CSV/QFX/OFX can implement
   the same normalized contract later.
3. **Manifest-declared roles beat filename inference.** Private filenames are neither stable nor
   authoritative. The operator explicitly maps every document to account role and document kind.
4. **OCR is local, positional, and ephemeral.** Two current documents require OCR, and their debit
   and credit columns cannot be reconstructed safely from plain reading-order text. Native and OCR
   extraction therefore share one positioned-token contract. Page images and raw OCR text are
   temporary; only normalized private facts and safe OCR-use evidence persist.
5. **Use a permissive PDF backend.** `pypdfium2` supplies text positions and rasterization under
   Apache-2.0/BSD-3-Clause terms. PyMuPDF was rejected for this MIT repository because its official
   distribution requires AGPL or a commercial license:
   <https://github.com/pypdfium2-team/pypdfium2#licensing> and
   <https://github.com/pymupdf/PyMuPDF#licensing>.
6. **Closed statements outrank current OCR.** A later current-activity capture is valuable for the
   latest balance, pending items, and post-close activity, but it never silently overrides posted
   rows in an authoritative closed monthly statement. Overlap disagreement blocks or requires an
   exact private resolution.
7. **Classification and exclusions are explicit data.** The tool can deterministically recognize
   structure, but it does not guess board-period ownership or fundraiser meaning. Private rules are
   reviewed, exact adjustments match one transaction, and ambiguity blocks.
8. **Cash actuals and budget goals retain separate provenance.** Statements establish cash facts;
   a raw-grid, read-only adapter establishes adopted targets and catches duplicate headers/rows
   before mapping. No FY actual row is fabricated or written back.
9. **Board-year pace is explicit.** The reporting start date and budget fiscal year can differ.
   Wave 1 preserves the operator-requested board-year comparison and labels it; it never silently
   treats the two calendars as identical or calls a straight-line pace screen a forecast.
10. **Minimum rigor precedes rendering.** Exact `Decimal`, full classification, cent-exact
   reconciliation, private storage, source hashes, one human review, and exact digest approval are
   Wave 1 requirements. More elaborate revisions, approvals, retry, and visual automation remain
   Wave 3 work.
11. **A private template preserves the locked design.** Google also recommends separating design
   from data through a copied tagged template. Rebuilding the full visual in API calls would spend
   the wave on design parity; importing a tokenized private PPTX preserves native editability and
   keeps identity/branding static and private.
12. **The app creates and reconciles the template it later copies.** Upload/conversion makes the
     file app-owned so the narrow `drive.file` grant can manage it without broad Drive access or a
     Picker UI. Bootstrap app properties recover safely from a partial initialization.
13. **OAuth surfaces stay separate.** The existing service account reads the budget workbook;
     dedicated user OAuth owns only app-used Slides/Drive files; Gmail authorization is never reused.
14. **One-shot operation.** Every command starts, completes, and exits under operator control. No
     autonomous, scheduled, background, or always-on behavior is added, so the autonomous-observation
     trigger does not fire. The explicit local smoke and attended Step 25 are sufficient for this
     one-shot data pipeline.
15. **Native parsing is sandboxed before bytes cross the boundary.** Resource limits alone do not
    restrict filesystem, network, or inherited-handle authority. Wave 1 therefore launches the
    native PDF parser only in a Windows LPAC worker with exactly the CPython-required `registryRead`
    capability, no network capability, public-only staged runtime, Job Object containment, and a
    `READY` attestation. The child begins with no PDF-channel handle: public control objects carry
    only generated names, a nonce, and child-local handle numbers; after independent token/Job
    verification, the broker uses non-inheritable direct handle duplication into that exact process.
    No `CreateProcess` inheritance window exists, so an unrelated host child launch cannot receive a
    PDF-channel handle. If the boundary cannot be created and verified, parsing fails before the
    statement is read or sent.
16. **Each native executable earns its own boundary.** The PDF worker's one-active-process Job
    intentionally prevents a child Tesseract process. Step 16 must therefore have the broker launch
    staged `tesseract.exe` directly as the one process in a separate LPAC Job and verify its token and
    Job before streaming raster bytes. A need for network access, caller-profile/worktree access, an
    ambient private-data directory, a helper wrapper, or an unenumerated capability blocks OCR rather
    than widening the Step 15 PDF worker.

## 7. Build Steps

<!-- autofix-applied: 2026-08-31 -->
### Step 14: Define private manifests and normalized finance contracts

- **Problem:** Establish strict, identity-safe v1 schemas for private inputs, rules, positioned
  tokens, dated balances, transactions, selectors, and canonical serialization before any parser
  or financial logic depends on them.
- **Type:** code
- **Issue:** #41
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_slides/__init__.py`,
  `pta_finance/treasurer_slides/models.py`, `tests/test_treasurer_slides_models.py`, and fictional
  model/manifest/rule fixtures
- **Produces:** `pta_finance/treasurer_slides/{__init__,models}.py`, fictional manifest/rule
  fixtures, and `tests/test_treasurer_slides_models.py`
- **Done when:** unknown/missing fields, unsafe paths, symlinks/reparse points, invalid role/kind/date,
  binary float, signed magnitude, malformed selector, duplicate rule ID, invalid cash role, and
  noncanonical JSON all fail; exact Decimal/date/token/balance/transaction/source-row/semantic-key
  round trips are stable; repo-local private paths that are tracked or not ignored block while
  ignored/outside-worktree paths pass; description normalization and duplicate occurrence ordering
  are pinned; and exceptions/logs leak none of the fixture's canary paths, identifiers, descriptions,
  or values
- **Depends on:** none
- **Status:** DONE (2026-09-01)

<!-- autofix-applied: 2026-08-31 -->
### Step 15: Parse supported native-text statement pages inside the pre-read LPAC boundary

- **Problem:** Turn known Wells Fargo embedded-text pages into positioned tokens, dated balance
  observations, and transaction rows using a versioned fail-closed document contract, without ever
  letting a normal-process native engine receive private statement bytes.
- **Type:** code
- **Issue:** #42
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_slides/bank_statements.py`,
  `pta_finance/treasurer_slides/native_sandbox.py`,
  `pta_finance/treasurer_slides/native_worker.py`, `pyproject.toml`, `uv.lock`,
  `.github/workflows/ci.yml`, `tests/test_treasurer_slides_bank_statements_native.py`,
  `tests/test_treasurer_slides_native_sandbox.py`, and fictional native PDF fixtures
- **Produces:** `pta_finance/treasurer_slides/bank_statements.py`, a narrow Windows LPAC/AppContainer
  launcher and one-shot PDF-worker entry point, public-only staged runtime/profile lifecycle,
  public control-object attestation plus directly broker-duplicated request/response pipes with a
  `READY` frame, the `pypdfium2` Slides extra in `pyproject.toml`/`uv.lock`, Windows CI coverage, and
  fictional native-text/parser-boundary tests
- **Done when:** every supported monthly/current page kind and known boilerplate is recognized by
  dimensions/markers/headers; char positions reconstruct non-overlapping debit/credit bands;
  balance bases and boundaries remain individually dated; transaction-table rows alone become
  activity; account identifiers are discarded; hard file/page/row/character/pixel limits and
  unknown/contradictory pages fail closed; and a base install without the optional extra still loads
  every existing command. A native parser cannot read or receive source bytes unless its LPAC child
  was created suspended, assigned to a kill-on-close Job Object, resumed, and returned a versioned
  `READY` frame after self-attesting token and Job state. Before its first `recv_bytes()`, the worker
  proves it is an AppContainer with exactly one enabled `registryRead` capability, the exact
  `WIN://NOALLAPPPKG` LPAC-policy token attribute, and the required one-active-process, CPU, and
  memory limits. The child uses an explicit AppContainer SID ACL only for its public staged runtime,
  has a sanitized environment/safe working directory, cannot access caller profile/worktree paths or
  the network, and begins without PDF-channel handles. It can reach only public control objects until
  both worker and broker token/Job attestations pass; then the broker directly duplicates only the
  intended request-reader and response-writer handles into that PID with inheritance disabled.
  Malformed frames, output floods, timeout, crash, cleanup failure, and sandbox-start failure block
  without a normal-process fallback or source-byte read/write. Per-run profile/runtime cleanup retains
  ownership until the child is known to have exited; non-Windows hosts fail closed before source-file
  reads. Windows CI proves real-parser regression, the true-LPAC positive/negative attestation,
  pre-read ordering, and cleanup with fictional fixtures; Linux proves portable logic and pre-read
  fail-closed behavior.
- **Release constraint:** native parsing and its LPAC enforcement ship as one atomic Step 15 diff;
  no direct-parser-only commit or private-source run is permitted. `CreateProcessW` must retain
  `bInheritHandles=False`; no temporary-inherit, handle-list, or foreign-process launch exception is
  permitted. The only control-plane contents may be generated names, nonce, and child-local handle
  numbers; private bytes cross only the two direct-duplicated anonymous endpoints after both
  attestations.
- **Depends on:** Step 14
- **Status:** PENDING

#### 15a: LPAC pre-read security gate (part of Step 15)

The parent starts and validates the child before opening the source PDF. The child receives no private
path, bytes, or PDF-channel handle at `CreateProcessW`; it can initially open only public control
objects. It self-attests, the broker independently validates its exact token/Job, then the broker
directly duplicates the two anonymous endpoints into that PID. The child emits `READY` only after
wrapping those endpoints. The broker then reads bounded bytes and passes them through the request pipe.
This is an acceptance subsection of Step 15, not an independently dispatchable or issue-bearing build
step.

<!-- autofix-applied: 2026-08-31 -->
### Step 16: Add bounded positional Tesseract fallback

- **Problem:** Parse image-only supported pages without using plain OCR reading order, persisting raw
  OCR material, weakening the native statement contract, or letting the one-process PDF worker spawn
  Tesseract.
- **Type:** code
- **Issue:** #43
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_slides/bank_statements.py`,
  `pta_finance/treasurer_slides/{native_sandbox,native_worker}.py`, `.github/workflows/ci.yml`,
  `tests/test_treasurer_slides_bank_statements_ocr.py`,
  `tests/test_treasurer_slides_native_sandbox.py`, and fictional raster PDF fixtures
- **Produces:** the OCR adapter plus a separately broker-launched `tesseract.exe` one-process LPAC
  Job; staged public Tesseract program/data assets and an explicitly tested minimum capability policy;
  Windows Tesseract 5 CI setup; fictional image-only/low-confidence/timeout fixtures; and OCR/
  sandbox regression tests
- **Done when:** real Tesseract 5 TSV at 300 DPI emits the same semantic observation as the paired
  native fixture; x-position—not token order—sets debit/credit direction; financial tokens below the
  threshold, timeouts, process failures, row ambiguity, or unsupported fingerprints block with safe
  errors; parser/OCR versions and evidence persist; raster input and TSV output use bounded pipes,
  while any allowed AppContainer-local fallback directory is removed only after Tesseract exits. The
  PDF worker never launches Tesseract because its Job is limited to one active process. Instead, the
  broker launches staged `tesseract.exe` directly as the one active process in a distinct LPAC Job and
  verifies its token, Job limits, and explicitly enumerated minimum capabilities before it writes
  raster bytes to the input pipe. The OCR process has no network or caller-profile/worktree access and
  never writes private raster/OCR data to ambient disk. Windows CI installs Tesseract and executes the
  real OCR fixture; Linux runs only portable OCR-contract and fail-closed coverage.
- **Depends on:** Step 15
- **Status:** PENDING

<!-- autofix-applied: 2026-08-31 -->
### Step 17: Select authoritative activity and balance intervals

- **Problem:** Combine overlapping closed statements and current captures without allowing later OCR
  to override closed posted activity, while deriving complete per-account opening/latest intervals.
- **Type:** code
- **Issue:** #44
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_slides/reconciliation.py`,
  `tests/test_treasurer_slides_source_authority.py`, and fictional overlap/balance fixtures
- **Produces:** `pta_finance/treasurer_slides/reconciliation.py`, overlap/balance fixtures, and
  `tests/test_treasurer_slides_source_authority.py`
- **Done when:** closed-monthly precedence, current post-cutoff/pending/latest supplementation,
  semantic overlap equality, exact disagreement resolution, legitimate duplicate occurrences,
  explicit and back-solved openings, multiple-anchor validation, pending-inclusive latest balances,
  mixed account dates, interval filtering, and inclusive boundaries are tested; any source gap,
  contradictory anchor, or unexplained overlap blocks before classification
- **Depends on:** Step 16
- **Status:** PENDING

<!-- autofix-applied: 2026-08-31 -->
### Step 18: Classify, pair, adjust, and reconcile signed cash

- **Problem:** Apply reviewed rules to every selected transaction and produce exhaustive signed
  contributions without silently dropping refunds, returns, transfers, or prior-period outflows.
- **Type:** code
- **Issue:** #45
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_slides/rules.py`,
  `pta_finance/treasurer_slides/reconciliation.py`,
  `tests/test_treasurer_slides_rules.py`, `tests/test_treasurer_slides_reconciliation.py`, and
  fictional rule/cash-bridge fixtures
- **Produces:** `pta_finance/treasurer_slides/{rules,reconciliation}.py`, fictional rule/cash-bridge
  fixtures, and `tests/test_treasurer_slides_{rules,reconciliation}.py`
- **Done when:** zero/multiple classification matches block; automatic pairs require matching
  pair-key/amount/direction/account/window and uniqueness; ambiguous pairs require exact two-row
  resolution; fundraising chargebacks, spending refunds, paired reversals, boundary-crossing returns,
  posted/pending contributions, one-leg mixed-cutoff transfer rejection, and the exact prior-board
  exclusion follow the contribution table; the exclusion changes only its declared
  numerator/categories/pace; operating-surplus/cash-change equations are exact; negative aggregates
  block; and every account plus the combined bridge reconciles exactly to the cent
- **Depends on:** Step 17
- **Status:** PENDING

<!-- autofix-applied: 2026-08-31 -->
### Step 19: Read adopted goals and build deterministic summary facts

- **Problem:** Read strict proposed goals through a narrow raw-grid interface and combine them with
  reconciled bank facts into the approved summary, pace, grouping, and narrative contract.
- **Type:** code
- **Issue:** #46
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_slides/budget_goals.py`,
  `pta_finance/treasurer_slides/facts.py`, `tests/test_treasurer_slides_budget_goals.py`,
  `tests/test_treasurer_slides_facts.py`, and fictional grid/fact fixtures
- **Produces:** `pta_finance/treasurer_slides/{budget_goals,facts}.py`, fictional raw-grid/fact
  fixtures, and `tests/test_treasurer_slides_{budget_goals,facts}.py`
- **Done when:** duplicate/blank/missing headers, malformed relevant rows, duplicate logical rows,
  including whitespace/case variants, invalid/non-positive goals, or attempted source writes block;
  proposed income/expense totals use exact Decimal; board/budget calendars and inclusive elapsed-day
  math remain distinct; category fold/tie/Other and largest-remainder rules are exact; tolerance boundaries and deterministic story
  strings are pinned; pace is labeled as a straight-line screen, not a forecast; and all five
  existing `read_timeseries()` callers retain their behavior
- **Depends on:** Step 18
- **Status:** PENDING

<!-- autofix-applied: 2026-08-31 -->
### Step 20: Encode the approved one-slide and template contract

- **Problem:** Turn approved facts into the exact generic text and geometry required by the locked
  design while keeping organization identity and branding static in a private tagged template.
- **Type:** code
- **Issue:** #47
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_slides/summary.py`,
  `pta_finance/treasurer_slides/template.py`, `tests/test_treasurer_slides_summary.py`, and
  `tests/test_treasurer_slides_template.py`
- **Produces:** `pta_finance/treasurer_slides/{summary,template}.py`, fictional presentation
  structures, and `tests/test_treasurer_slides_{summary,template}.py`
- **Done when:** every approved fact maps to one declared variable role; static identity is not a
  role or request; money/percent/date/story text is deterministic; four spending and five fundraising
  slots preserve totals; stacked/progress geometry stays inside the exact 16:9 contract; and
  missing/duplicate/unknown tags, stale literals, unresolved markers, invalid ratios, overflow,
  overlap, zero totals, or source-facts-hash mismatch block without Google access
- **Depends on:** Step 19
- **Status:** PENDING

<!-- autofix-applied: 2026-08-31 -->
### Step 21: Create immutable runs and transaction-level fact/slide review

- **Problem:** Persist the complete producer and canonical slide-plan snapshots, then require a
  human-readable digest-bound transaction and slide-string review before any Google client can be
  constructed.
- **Type:** code
- **Issue:** #48
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_slides/pipeline.py`,
  `pta_finance/treasurer_slides/templates/fact_review.html.j2`, and
  `tests/test_treasurer_slides_pipeline.py`
- **Produces:** `pta_finance/treasurer_slides/pipeline.py`,
  `pta_finance/treasurer_slides/templates/fact_review.html.j2`, and
  `tests/test_treasurer_slides_pipeline.py`
- **Done when:** a valid prepare uses Step 20's canonical `SummarySlidePlan`, writes facts and plan
  snapshots atomically with manifest last, and produces stable facts/plan/approval hashes; every
  selected/rejected transaction, financial equation, slide string, and geometry value appears in the
  autoescaped private review; the approval digest binds both snapshot chains; partial writes,
  existing run IDs, bad hashes, bad approval syntax, changed sources, duplicate formatting, and
  canary leakage block; a failed rule/reconciliation pass writes only a private non-approvable
  selector-rich diagnostic; a rejected review requires a new run; and candidate receipt creation
  cannot rewrite prepared files
- **Depends on:** Step 20
- **Status:** PENDING

<!-- autofix-applied: 2026-08-31 -->
### Step 22: Add exact-scope OAuth and recoverable template bootstrap

- **Problem:** Authorize one dedicated Slides surface and import/validate one private app-owned
  template without duplicate resources, wider Drive access, or permission changes.
- **Type:** code
- **Issue:** #49
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_slides/google_client.py`, `pta_finance/config.py`,
  `config.example.toml`, `tests/test_config.py`, `tests/test_treasurer_slides_auth.py`, and
  `tests/test_treasurer_slides_workspace.py`
- **Produces:** the auth/workspace portion of `pta_finance/treasurer_slides/google_client.py`,
  optional config in `pta_finance/config.py`/`config.example.toml`, fake Google services,
  `tests/test_config.py`, and `tests/test_treasurer_slides_{auth,workspace}.py`
- **Done when:** the granted set must equal `drive.file`; Gmail/missing/extra scopes fail; dedicated
  token load/mint/refresh/atomic persistence and `invalid_grant` guidance are safe; import support,
  one-slide 16:9, tags, and canonical projection hash gate initialization; zero/one/multiple
  broad-anchor bootstrap resources with client/principal/source-hash/role provenance
  create/resume/block across partial failures and pagination; a different OAuth client or
  reauthorized different principal blocks before search; an atomic workspace-init lock serializes
  bootstrap/search/create/manifest and stale-lock recovery is explicit;
  every create uses `ignoreDefaultVisibility=true` and readback proves an unshared exact-parent
  workspace; every private config/PPTX path passes the ignored-or-outside-worktree gate; config omission preserves all legacy loads; failures redact canary IDs/URLs; and no permission/sharing/publish/email method exists in or is called by the boundary
- **Depends on:** Step 21
- **Status:** PENDING

<!-- autofix-applied: 2026-08-31 -->
### Step 23: Copy and populate one approved candidate idempotently

- **Problem:** Cross the Google write boundary exactly once for an approved run, then reconcile and
  validate the native editable candidate without mutating the template or source workbook.
- **Type:** code
- **Issue:** #50
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_slides/google_client.py`,
  `pta_finance/treasurer_slides/pipeline.py`, and
  `tests/test_treasurer_slides_google_candidate.py`
- **Produces:** candidate/render support in `google_client.py` and `pipeline.py`, fake Drive/Slides
  cases, and `tests/test_treasurer_slides_google_candidate.py`
- **Done when:** no Google client exists before digest/local-hash validation; template drift blocks;
  one atomic run lock serializes creators; zero/one/multiple app-property matches create/resume/block;
  OAuth-client/principal equality and broad paginated workspace/run search turn every
  digest/role/MIME/parent mismatch into a block rather than a false zero; copy writes candidate provenance in its original
  request and ambiguous copy reconciles rather than issuing a second mutation;
  pristine/post/other candidate-state classification plus required
  revision makes a lost batch response resumable without blind retry; one atomic batch applies the
  full request plan; copy/readback enforce ignored default visibility, `shared=false`, and the exact
  private parent; readback validates slide/roles/text/geometry; one private create-once receipt records
  the candidate; rerun resolves the same candidate; and template, workbook, permissions, and console
  remain unchanged/redacted
- **Depends on:** Step 22
- **Status:** PENDING

<!-- autofix-applied: 2026-08-31 -->
### Step 24: Wire CLI, privacy gates, packaging, docs, and local smoke

- **Problem:** Expose the complete one-shot workflow through the installed CLI and prove the local
  producer-consumer chain before real authorization and Google acceptance.
- **Type:** code
- **Issue:** #51
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/cli.py`, `scripts/check_no_identity.py`,
  `tests/test_treasurer_slides_cli.py`, `tests/test_treasurer_slides_smoke.py`, packaging/privacy
  tests, `docs/generating-treasurer-summary.md`, `README.md`, `SETUP.md`, `CLAUDE.md`, and `plan.md`
- **Produces:** three commands in `pta_finance/cli.py`; extended `scripts/check_no_identity.py`;
  CLI/privacy/packaging/smoke tests; `docs/generating-treasurer-summary.md`; and status/setup updates
  in `README.md`, `SETUP.md`, `CLAUDE.md`, and `plan.md`
- **Done when:** dispatch resolves all three commands and old commands are unchanged; missing
  extra/config/Tesseract/workspace/approval and stale locks fail actionably; a Windows-only
  60-second smoke uses real pypdfium2 in the Step 15 PDF worker, real Tesseract in Step 16's
  directly broker-launched LPAC Job, real statement/rule/reconciliation/budget/fact/review/
  summary/template-request components, and a fictional in-memory read-only grid to complete
  PDF-to-Slides-request without mocks inside the data pipeline; Linux proves portable/static and
  pre-read fail-closed coverage; tracked-file guard rejects canary private artifacts;
  runtime path-gate tests reject tracked/non-ignored private locations before read/write/network;
  wheel build/isolated install loads package/templates/entry point; full pytest, strict mypy, Ruff,
  existing report/reimbursement/CLI regressions, and identity guard pass; and docs cover diagnostic
  rule authoring, API/consent setup, seven-day Testing tokens, recovery, exact commands, review, and
  privacy boundaries
- **Depends on:** Step 23
- **Status:** PENDING

### Step 25: Run the private source-to-Google acceptance

- **Problem:** Run this attended acceptance against the real private statements, workbook, OCR
  executable, template, OAuth account, and Google rendering:

  ```powershell
  uv run pta-finance init-treasurer-summary-template --pptx "<private-tokenized-template>" --config config.toml
  uv run pta-finance prepare-treasurer-summary --inputs "<private-input-manifest>" --config config.toml
  Invoke-Item "<private-run-directory>\review.html"
  uv run pta-finance create-treasurer-summary --run "<run-id>" --approval-sha256 "<approval-digest>" --config config.toml
  Invoke-Item "<private-run-directory>\candidate.receipt.json"
  uv run pta-finance create-treasurer-summary --run "<run-id>" --approval-sha256 "<approval-digest>" --config config.toml
  git status --short
  uv run python scripts/check_no_identity.py
  ```

  | Check | Expected outcome |
  |---|---|
  | Template init | Dedicated grant is exactly `drive.file`; one app-owned unshared template is validated under the private folder |
  | Prepare | Real sources yield one immutable run; transaction review and every account/combined equation reconcile exactly |
  | Review | Every transaction, exclusion, headline, category, progress value, pace line, story, and source note agrees with the locked v0 |
  | First create | Exactly one editable, unshared, exact-parent one-slide candidate is recorded in the private receipt |
  | Visual check | Candidate has no clipping, wrapping, overlap, wrong geometry, or theme drift |
  | Second create | The same candidate/readback is resumed; no duplicate appears |
  | Repository check | Source workbook/template/permissions are unchanged and no private artifact is tracked |
- **Type:** operator
- **Issue:** #52
- **Files:** no tracked files; only configured gitignored private inputs/outputs and private Google
  artifacts are inspected or created
- **Produces:** only gitignored private input/rules/run/workspace artifacts, one app-owned private
  template, one private Google Slides candidate, and generic pass/fail plan/issue bookkeeping; no
  tracked code or detailed evidence
- **Done when:** the operator tokenizes a private copy of the approved v0 PPTX; init authorizes only
  the dedicated `drive.file` grant and converts/validates that template; prepare processes the real
  statement set and live budget goals; transaction-level review agrees with the locked v0 on every
  headline, category, exclusion, progress value, pace statement, narrative, and source note; every
  account and combined bridge reconciles; the operator supplies the displayed digest; create yields
  exactly one editable private one-slide candidate; visual comparison finds no clipping, wrapping,
  overlap, wrong bar geometry, or theme drift; rerun resolves the same candidate; source workbook,
  template, and Drive permissions remain unchanged; Drive readback reports the folder, template, and
  candidate unshared under the exact private parent; and git status/identity guard show no private
  artifact tracked
- **Depends on:** Step 24
- **Status:** PENDING

Steps are deliberately sequential producer/consumer slices. Step 14 establishes normalized source
contracts; Step 15 adds native parsing and the enforceable pre-read LPAC boundary as one atomic slice;
Step 16 adds bounded OCR; Steps 17-18 establish bank truth; Step 19 builds summary facts; Step 20
fixes the presentation plan; Step 21 binds both into the reviewed approval; Steps 22-23 cross the
Google boundary in two recoverable stages; Step 24 proves the installed local workflow; and Step 25 is
the only real-service visual acceptance.

## 8. Risks and Open Questions

| Item | Risk | Mitigation / decided handling |
|---|---|---|
| Native PDF/OCR engine compromise or ambient access | A malformed document or native dependency could read user data, use the network, or inherit a privileged handle | Step 15's LPAC with only CPython's `registryRead` capability, no network capability, public-only staged runtime, one-process Job limits, worker and broker token/Job attestation, direct non-inheritable duplication of only two anonymous PDF-channel endpoints, cleanup ownership, and fail-closed behavior is mandatory before PDF bytes cross the boundary. Public control objects contain no PDF path or bytes and cannot route a PDF endpoint to a foreign launch. Residual host-trust assumption: a separate same-user full-trust process that discovers a per-run public control-object name may interfere with startup ordering or cause denial of service, but cannot obtain, redirect, or read a PDF channel without access to the broker or child process; the broker duplicates those endpoints only into the independently verified child PID. Step 16 has the broker launch Tesseract directly in a distinct LPAC Job; the PDF worker may not spawn it. |
| Image-only or low-quality statements | OCR can drop punctuation, dates, digits, or debit/credit column position and create a plausible wrong transaction | Positioned TSV tokens, column bands, confidence gates, typed parsing, account/combined reconciliation, transaction-level private review, and real Step 25 comparison; any ambiguity or unexplained cent blocks |
| Wells Fargo layout changes | A future statement may parse with wrong column association | Versioned format fingerprints and explicit unsupported-layout failure; later adapter update with a new fictional regression fixture |
| Overlapping reports | Monthly plus current activity can double-count most of the window or let OCR replace stronger closed evidence | Closed monthly statements own posted closed dates; current activity supplements later/pending/latest facts; semantic disagreement requires an exact resolution |
| Legitimate duplicate transactions | Fingerprint-only dedup could erase a real repeated payment | Separate source-row and semantic identities, preserve occurrence ordinal, and never deduplicate solely by date/amount/description |
| Transfer/reversal ambiguity | A same-amount external transaction could be incorrectly removed | Auto-pair only two rule-keyed rows satisfying every role/account/direction/time constraint; otherwise require exact private resolution |
| Boundary-crossing refund/return | Dropping a one-sided return would understate or overstate actuals | Treat it as signed contra-fundraising or contra-spending unless both reversal legs are present and paired |
| Pending activity changes after review | The approved slide may differ from a later bank view | Snapshot and label the as-of/balance basis; create from the approved snapshot without silent refresh; a new view requires a new run |
| Mixed account as-of dates | Combined latest cash can look synchronized or strand one internal-transfer leg | Store/display each account boundary, label the total `latest supplied`, block one-leg transfers, and include the footer caveat |
| Board year differs from fiscal year | A pace screen could be mistaken for a forecast over the budget's calendar | Keep both dates in the fact/review, use inclusive board-year days, label the comparison, and explicitly say timing/seasonality are not modeled |
| Duplicate budget header or logical row | Dictionary projection or naïve summation can silently overwrite/double-count a goal | Inspect the raw grid through a read-only protocol and reject duplicate/blank headers and duplicate proposed-row keys |
| Misclassification or unsupported causal story | Bank descriptions do not prove program purpose or prior-board ownership | Require explicit classification and exact adjustment reason; label fundraising by collection channel; narrative uses approved facts only |
| Private template lacks, duplicates, or changes a role | A candidate can silently retain stale values | Strict one-occurrence tag contract, create-time canonical template hash, and unresolved-token readback block init/create |
| PPTX conversion changes layout | The imported Google template may wrap or move objects | Validate page/roles/geometry programmatically and require Step 25 visual comparison; automated thumbnail QA remains Wave 3 |
| Narrow OAuth cannot see an arbitrary manual template | `drive.file` is per-file and a CLI has no Picker | The app uploads/converts the private PPTX itself, making the template app-owned before it is customized/copied |
| Domain-default or parent visibility | A domain policy could make a newly created file visible more broadly than intended without an explicit share call | Set `ignoreDefaultVisibility=true` on folder/template/candidate creation, use only the verified unshared app folder as parent, and require `shared=false` readback |
| Partial, cross-account, or concurrent Google mutation | A crash, reauthorization into another account, narrow recovery query, or two creators could orphan/duplicate a workspace, candidate, or Slides update | Bind OAuth client/Drive principal privately, paginate broad stable-anchor queries before validating provenance, put recovery properties on original mutations, serialize workspace init and each run with explicit stale-lock recovery, classify Slides state, require the read revision, and never blind-retry |
| Private data enters the public repo | A mistyped source/output path could put statements, values, IDs, or screenshots in a tracked tree | Before access, require every repo-local private path to be untracked and matched by `git check-ignore --no-index`; add fictional fixtures, status-only logs, tracked-file guard, canary tests, and attended `git status` check |
| Optional Slides dependencies break base users | Existing commands could require PDF/OCR packages unexpectedly | Lazy imports/actionable missing-extra errors plus base-install subprocess coverage |

There are no blocking open choices. Explicitly deferred decisions are the Wave 2 slide/chart design,
additional bank formats, public/audience variants, full remote retry/revision/thumbnail QA, richer
approval states, scheduling, sharing, and the rest of the old graphic catalog.

## 9. Testing Strategy

### Unit and contract tests

- Strict config, manifest, rules, run, fact, and workspace schemas; unknown fields, invalid enums,
  bad dates, non-finite/binary-float money, unsafe paths, symlinks/reparse points, oversize inputs,
  malformed digests, and temporary Git worktrees proving ignored/outside paths pass while tracked or
  non-ignored private paths block before access.
- Embedded-text and raster-only fictional Wells Fargo pages; positioned-token/column reconstruction,
  page fingerprints and boilerplate, fallback threshold, actual Tesseract TSV invocation on Windows,
  date/amount/status/balance parsing, repeated transactions, low-confidence/malformed OCR, limits,
  timeout, bounded pipe transfer, and streamed OCR data never reaching an ambient temporary
  directory. Linux retains only portable OCR-contract tests and cannot execute the statement parser.
- Native-boundary contract and Windows integration coverage: `READY` precedes every source-byte
  transfer; true LPAC state, exactly one enabled `registryRead` capability, exact
  `WIN://NOALLAPPPKG` LPAC-policy token attribute, expected Job limits, sanitized environment/current
  directory, public-control-object-only pre-attestation handshake, non-inheritable direct duplication
  of the two intended handles into the independently verified PID, malformed-frame/output/timeout/
  crash handling, and per-run profile/runtime cleanup are proved with fictional canaries. The test
  suite proves that a missing LPAC policy blocks before either endpoint is duplicated and that no
  foreign `CreateProcess` inheritance window exists. Step 16 has a separate direct-Tesseract boundary
  suite, including
  broker-side token/Job verification before raster input, and cannot be spawned by the PDF worker.
  Linux and other non-Windows runs prove fail-closed behavior before a statement file is read.
- Closed-statement overlap authority, exact disagreement resolution, source versus semantic identity,
  explicit/back-solved balances, anchor validation, transfer/reversal key/uniqueness/ambiguity,
  refunds/chargebacks/boundary returns, mixed-cutoff transfers, pending basis, classification
  coverage/collisions, exact exclusions, and cent-exact account/combined signed reconciliation.
- Budget fiscal-year filtering and direct proposed income/expense totals over a fictional 14-column
  raw grid; blank/missing/duplicate headers, short/invalid rows, exact and whitespace/case-variant
  duplicate logical proposed rows, zero totals, write-method sentinels, and proof that existing
  `read_timeseries` callers retain behavior.
- Summary formatting, largest-remainder percentages, board-year elapsed math, pace threshold edges,
  deterministic narrative, footnotes, tag registry, geometry, and unresolved/private-content lint.
- Atomic run writes, non-approvable selector-rich diagnostics, transaction-level autoescaped review,
  canonical SummarySlidePlan reuse, stable fact/plan/envelope digests, wrong/stale approval rejection,
  create-once receipt, and no Google-client construction before approval.

### Google boundary tests

- Exact-equality `drive.file` scope checks, dedicated token path, token refresh/atomic save, and
  rejection of Gmail, broader, narrower, malformed, or unverified grants.
- Fake Drive/Slides services for import-format validation, PPTX conversion, bootstrap zero/one/many
  recovery with client/principal/source-hash/role binding, broad-anchor pagination/mismatch cases, ignored
  default visibility, workspace-init concurrency/stale lock, exact parents, unshared readback,
  canonical template projection/drift,
  candidate locking/copy/appProperties and ambiguous-
  copy reconciliation, pristine/post/other candidate states, required-revision atomic batches,
  lost-response readback without blind retry, sanitized receipt, and zero permissions/source-update
  calls.
- Canary file IDs, URLs, account text, amounts, paths, token content, and raw API errors proving the
  redaction boundary and private-manifest allowlist.

### Real-component smoke gate

Step 24 adds a sub-60-second Windows smoke before the live operator step. It uses real pypdfium2 in
the Step 15 PDF worker and real Tesseract in Step 16's directly broker-launched LPAC Job, extracts
one text and one raster fictional statement, passes the rows through the real
rules/reconciliation/budget/fact/review/summary/template-request pipeline, and asserts one complete
Google request plan with exact reconciliation and no unresolved role. Only the external Sheet and
Google network boundaries use controlled fictional adapters; no producer/consumer boundary inside the
feature is mocked. Linux keeps portable/static coverage and verifies that native parsing fails closed
before a statement read.

### Regression, packaging, and privacy gates

- `uv run pytest -q`
- `uv run mypy --strict pta_finance`
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run python scripts/check_no_identity.py`
- `uv build`, install the wheel in an isolated environment, load the fact-review template, and run
  `pta-finance --help` both with and without the optional Slides extra.
- Explicit regression selection for config, Sheets, report source, reports, reimbursement, Gmail,
  CLI, workflows, and the existing smoke suite.

### Operator acceptance

Step 25 is mandatory because synthetic tests cannot prove the private statement layout, local OCR
quality, live budget totals, OAuth consent, PPTX conversion, or actual Google line wrapping. Only
generic pass/fail status belongs in tracked plan/issue updates; values, screenshots, IDs, hashes,
paths, and detailed evidence remain private.

## Next Step

The Step 15a security gate is part of executable Step 15, so it updates the existing Wave 1 issue
#42 rather than creating an unexecutable fractional-step issue. Before further phase orchestration,
refresh plan review and fresh-context wrap, then synchronize the existing Wave 1 umbrella/step issues
#40-#52. The superseded Phase 5 set #26-#39 remains closed with a generic note. Then run:

```text
/build-phase --plan documentation/treasurer-summary-wave-1-plan.md
```

`/build-phase` runs code through Step 24 and defers operator Step 25 into its phase-end Manual UAT
bundle for an attended private handoff.
