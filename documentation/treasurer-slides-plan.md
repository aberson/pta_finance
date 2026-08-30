# Phase 5 — Treasurer update Google Slides generator — feature plan

Repository issue-sync phase is `5`; global build-step numbering continues after shipped Phase 4 at
Step 14.

> **Identity rule.** This repository is public and the finance data is private. No real
> organization, school, person, email address, Google file ID, chart ID, source value, or OAuth
> credential may appear in committed code, documentation, tests, fixtures, or examples. Private
> request text, source snapshots, run bundles, thumbnails, candidate/final IDs, and quality-assurance
> (QA) evidence stay
> under gitignored paths.

## 1. What This Feature Does

Add an operator-driven workflow that turns a rough Treasurer-update request plus private Google
Sheet data into an editable, source-grounded Google Slides deck. The default output includes an
overview, terse main slides, appendix detail, appropriate graphics, source/as-of notes, and a clear
separation between cash held, reserves, allocations, commitments, received fundraising, pending
fundraising, and projections.

The workflow deliberately stops for four digest-bound approvals:

1. **Brief** — “Does this summary and goal/scope look right?”
2. **Facts** — “Do these facts, definitions, conflicts, gaps, and exclusions look right?”
3. **Story** — “Does this outline, graphic selection, and appendix split look right?”
4. **Candidate** — “Does this QA-passing Google Slides candidate look right?”

An end-to-end convenience command may resume a run to its next missing approval, but it never
answers an approval question or promotes a deck automatically. Promotion is a separate explicit
command that copies an approved private candidate to a private final folder. The tool never changes
sharing permissions or publishes a presentation to the web.

## 2. Existing Context

- The package is Python 3.12 with a flat command-line interface (CLI) in `pta_finance/cli.py`, frozen configuration
  dataclasses in `pta_finance/config.py`, and Google Sheet access in `pta_finance/sheets.py`.
- `Budget Timeseries` is the primary report dataset. Its current contract covers fiscal year,
  category grouping, income/expense type, budget/actual measure, amount, fundraiser flag, grade,
  raw category, and source tab. It does **not** establish bank balance, reserve policy, commitments,
  outstanding obligations, pending employer matches, sustainability scenarios, or financial-control
  prose.
- `pta_finance/report_source.py` already converts `Budget Timeseries` rows into typed report inputs.
  The deck source layer should reuse that parser through a minimal reader-protocol type widening,
  rather than duplicate or behaviorally change it.
- `pta_finance/reimbursement_pipeline.py` and `reimbursement_report.py` provide the closest workflow
  precedent: strict versioned private bundles, stable hashes, fail-closed refresh, atomic writes,
  offline rendering, aggregate-only console output, and separate subcomponent/end-to-end commands.
- `pta_finance/reports/builder.py` provides the runtime public-output personally identifiable
  information (PII) guard precedent. Public
  deck safety must likewise be enforced in production code, not only in tests or documentation.
- `pta_finance/reports/charts.py` provides deterministic Matplotlib with its noninteractive Agg
  raster backend, but its current report-specific chart functions do not reproduce the workbook
  graphics and should remain backward compatible.
- `reports/output/`, `config.toml`, `secrets/`, and the current finance/mail sample directories are
  gitignored. Deck runs belong under `reports/output/treasurer-slides/`.
- The existing Google service account is appropriate for scope-pinned read-only Sheet access, but
  Google service accounts cannot own ordinary My Drive files. Slides creation therefore needs a
  separate human OAuth 2.0 token unless a Shared Drive is introduced later.
- The live workbook was inventoried read-only. It currently contains 11 native charts and three
  over-grid PNGs. The native set consists of five pie charts, four column charts, one stacked bar,
  and one line chart. Two PNGs are custom finance visuals (an expense donut and a program-to-goal
  Sankey). The third is an internal-only legacy participation table containing names.
- Native chart IDs are not durable: rebuilding a Sheet can delete and recreate them. Runtime
  matching must use logical identity plus semantic fingerprints, never a committed live chart ID.
- Several graphics are driven by dropdowns or formula spill ranges. A run must declare selector
  state and rebuild from its fact snapshot; it must not inherit the last state left in the workbook
  UI.
- Google Slides can store a static, unlinked copy of a Sheets chart. Locally generated Portable
  Network Graphics (PNG) insertion
  requires a fetchable URL, so the two custom aggregate images need short-lived controlled asset
  staging. Over-grid Sheet images are not exposed by the normal Sheets metadata surface and cannot
  be the production source of truth.

## 3. Scope

### In scope

- A strict, versioned, private Treasurer deck run bundle and explicit state machine.
- Deterministic rough-text cleanup with every ignored fragment recorded and reviewable.
- A provider-neutral optional import for an assistant-generated brief proposal. The proposal is a
  suggestion only; it cannot supply approved facts or bypass operator review. There is no embedded
  model call in v1.
- Read-only source adapters for `Budget Timeseries`, an optional operator-maintained
  `Treasurer Briefing Inputs` tab, the existing reimbursement summary/bundle where selected, and a
  private per-run override file.
- Typed facts with dates, financial basis, audience classification, provenance, calculations,
  conflicts, missing status, and source hashes.
- Reusable content modules for overview, current position, history, prior-vs-current comparison,
  current budget, fundraising, reserves/sustainability, spending breakdown, spending controls, and
  appendix detail.
- One versioned visual design system in v1, shared by slides and graphics.
- Capability to reproduce all 14 currently registered workbook graphics when requested. A graphic
  can be supported while still being excluded from a particular deck by audience or module choice.
- An offline 16:9 storyboard and Playwright layout checks before any run-specific candidate,
  staging-Sheet, or Storage write. One-time workspace bootstrap is the explicit exception.
- A copied private Google Slides template, private candidate creation, Google-rendered thumbnail
  QA, explicit candidate approval, and separate final promotion.
- Subcomponent commands plus a reimbursement-style resume/orchestration command.
- Fictional fixtures, fake Google services, a real-component local smoke gate, and a private live
  operator acceptance sequence covering minimal smoke, internal parity, and public safety.

### Out of scope

- Writing to the source finance workbook or silently creating the briefing-input tab.
- Inventing, estimating, or silently filling a missing financial fact.
- Autonomous scheduling, background agents, unattended publishing, or automatic approvals.
- Sending email, approving reimbursements, authorizing spending, or making policy decisions.
- Changing Drive permissions, publishing to the web, or inserting a final into a larger
  human-maintained Town Hall deck.
- Implementing or changing the separately deferred monthly-report Drive-upload path; Treasurer
  Slides owns only its app-created workspace artifacts.
- Linked/refreshable charts in a public deck. V1 graphics are static in Slides; text and native
  tables remain editable.
- Multiple visual themes, a generic non-finance presentation engine, or automatic support for an
  unregistered future graphic.
- A built-in large language model (LLM) provider or transmitting private requests/facts to a model
  application programming interface (API).
- A PowerPoint (`.pptx`) export/import backend, Apps Script bridge, or service-account-only Shared
  Drive backend.
  These are deferred alternatives, not hidden fallbacks.
- Merging the private launcher proposal into the external coding-root observatory registry. That is a
  separate repository change with its own review and is not an operator action in this plan.

## 4. Impact Analysis

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `pta_finance/treasurer_deck/{__init__,models,intake,sources}.py` | add | Private bundle, cleanup, read-only source protocol/client, fact snapshot, schema-v2 reimbursement adapter | `rg --files` confirmed the package absent; current reimbursement producers are `reimbursement_report.load_bundle` and `ReimbursementReport.{active_tickets,events_for,summary}` in `reimbursement_report.py` |
| `pta_finance/treasurer_deck/{modules,theme,storyboard}.py` | add | Module catalog, design tokens, claim/appendix assembly, local slide plan | Package glob confirmed absent; existing report builder/render modules remain separate |
| `pta_finance/treasurer_deck/assets/fonts/*` | add | Pinned open-licensed v1 font bytes and license for deterministic network-free preview | Deck package is absent; no current report font asset provides browser-load proof |
| `pta_finance/treasurer_deck/{graphics_catalog,graphics}.py` | add | Fourteen logical capabilities, private bindings, native/custom/table renderers | Package glob confirmed absent; live read-only inventory found 11 native charts + 3 over-grid PNGs |
| `pta_finance/treasurer_deck/{google_client,asset_staging}.py` | add | Dedicated OAuth/bootstrap, staging Sheet, candidate, redacted Google boundary, temporary image transport | Package glob confirmed absent; `pyproject.toml` already has Google API/auth clients but no Storage client |
| `pta_finance/treasurer_deck/{qa,pipeline}.py` | add | Local/candidate QA, locks/atomic state, approvals, promotion, resume workflow | Package glob confirmed absent; reimbursement pipeline inspected as the atomic/fail-closed precedent |
| `pta_finance/treasurer_deck/templates/*.html.j2` | add | Autoescaped brief review, storyboard, and candidate gallery | Existing Jinja templates live under `pta_finance/reports/templates/`; deck template glob confirmed absent |
| `pta_finance/report_source.py` | extend | Type `read_timeseries` against a minimal `ValuesReader` protocol so the new non-writing client can reuse the parser | All direct calls grep'd: `cli.py:165,313,396` and `tests/test_report_source.py:229,243`; existing `SheetsClient` remains structurally compatible |
| `pta_finance/config.py` | extend | Add optional strict `Slides` paths/settings | `rg '\bConfig\('` found the sole construction at `config.py:242`; no test/caller constructs `Config` directly, so `slides=None` is backward compatible |
| `config.example.toml` | extend | Fake optional Slides/OAuth/storage/binding examples | File read in full; optional `[gmail]`/`[receipt_mapping]` blocks are the precedent |
| `pta_finance/cli.py` | extend | Register setup/check/bind/prepare/approve/preview/create/update/promote commands | `build_parser` is at `cli.py:1320`; all 13 existing `set_defaults(func=...)` registrations are local to that function and remain unchanged |
| `pyproject.toml`, `uv.lock` | extend | Add optional `slides` extra with Playwright and Cloud Storage client | Dependency table read in full; neither package is currently declared |
| `.github/workflows/ci.yml` | extend | Install the `slides` extra + Chromium and run real local Playwright tests in continuous integration (CI) | Current install is only `uv sync --extra dev`; current pytest step has no browser install |
| `scripts/check_no_identity.py` | extend | Detect private Google resource IDs/URLs and deck artifacts with precise allowlists | Script currently checks service-account JSON markers and optional identity substrings only; sole workflow invocation is `.github/workflows/ci.yml:37` |
| `tests/test_treasurer_deck_*.py` | add | Contracts, source, module, graphic, auth, Google, QA, CLI, and smoke coverage | `rg --files tests` confirmed no existing Treasurer-deck tests |
| `docs/generating-treasurer-updates.md` | add | Operator procedure and exact read/write/approval boundaries | Docs glob confirmed the file absent |
| `README.md`, `SETUP.md`, `CLAUDE.md`, `plan.md` | extend | Command, layout, dependency, and current-state inventory | Each file read/located during discovery; no public identity values will be added |
| External coding-root `.claude/observatory/registry.toml` | proposal only | Specify five private launchers without replacing unrelated registry entries | Existing reimbursement launchers were inspected outside this repo; Step 23 produces a merge-preserving proposal, while applying it remains a separate repository change outside this plan |

The supplemental input tab is intentionally **not** added to `schema.REQUIRED_TABS`. Doing so would
make `init-sheet` or `check` a potential writer. Existing reimbursement models/pipeline and monthly
report builders/renderers remain unchanged; the only shared-source edit is the structurally
backward-compatible `report_source.read_timeseries` reader protocol described above.

## 5. New Components and Contracts

### 5.1 Private run directory and state machine

Each run atomically claims a unique directory:

```text
reports/output/treasurer-slides/runs/<YYYYMMDDTHHMMSSZ>-<24-lowercase-hex>/
  request.txt
  brief.draft.json
  facts.snapshot.json
  deck.bundle.json
  ignored-choices.json
  review.html
  preview/storyboard.html
  preview/slides/*.png
  qa/local-report.json
  qa/candidate-gallery.html
  qa/candidate-report.json
  manifest.json
```

`run_id` is exactly `<YYYYMMDDTHHMMSSZ>-<24 lowercase hex>`, using Coordinated Universal Time (UTC)
plus 96 bits from
`secrets.token_hex(12)`. The atomic directory claim remains the collision arbiter. CLI `--run` and
`--supersedes-run` values are IDs, never paths: they must match that grammar, name an immediate
child of the configured run root, resolve beneath that root, and traverse no symlink/reparse-point
component. `--as-of` is a strict ISO `YYYY-MM-DD` calendar date.

Every run JSON artifact rejects unknown root fields and unsupported schema versions. Every v1
`schema_version` is the integer `1`; timestamps are RFC 3339 UTC values with a trailing `Z`.
Canonical JSON hashes use UTF-8, Unicode NFC strings, lexicographically sorted object keys, compact
separators, preserved array order, JSON `true`/`false`/`null` literals, and no insignificant
whitespace. Integers use base-10 with no leading zero; finite `Decimal` values are JSON strings in
plain decimal notation with trailing fractional zeroes/dot removed and negative zero normalized to
`"0"`; binary floating-point values are rejected. An artifact digest covers the complete canonical
object with its own digest field omitted. A content-derived ID covers the named record with its ID
and digest fields omitted. `request_sha256` alone covers the exact validated UTF-8 file bytes so
source offsets and the reviewed input remain bound. The compact v1 root contracts are:

- `brief.draft.json`: `schema_version`, `run_id`, `request_sha256`, ordered `tasks`
  (`task_id`, `source_spans`, `module_key`, `question`, `required`), ordered
  `workflow_guidance`, and ordered `ignored` (`source_span`, `reason`, `confidence`);
- `facts.snapshot.json`: `schema_version`, `run_id`, `captured_at`, `as_of_date`, `audience`,
  ordered `source_snapshots`, `facts`, `graphic_datasets`, `conflicts`, `missing_required`, and
  `missing_optional`;
- `deck.bundle.json`: `schema_version`, `run_id`, approved upstream digests, `theme_version`,
  ordered `modules`, `slides` (`slide_id`, `layout_key`, `claim_fact_ids`, `elements`,
  `source_note`), `graphics`, and `appendix_claims`;
- `ignored-choices.json`: `schema_version`, `run_id`, `request_sha256`, ordered ignored fragments,
  and explicit run choices (`audience`, `skip_overview`, excluded modules, and requested graphics);
- `qa/local-report.json`: `schema_version`, `run_id`, `storyboard_sha256`, `theme_version`, browser
  and font proof, ordered per-slide rule results, `passed`, ordered failure codes, and `created_at`;
- `qa/candidate-report.json`: `schema_version`, `run_id`, approved upstream digest, allowlisted
  candidate ID/version/revision, canonical structure hash, ordered slide/image/thumbnail digests,
  cleanup receipts, `passed`, ordered failure codes, and `created_at`; and
- `manifest.json`: `schema_version`, `run_id`, optional `supersedes_run_id`, `created_at`, `state`,
  monotonic `generation`, `artifact_sha256`, ordered approvals, allowlisted remote IDs/versions,
  cleanup receipts, and immutable promotion attempts.

Within `deck.bundle.json`, a module record is `module_key`, audience, ordered required/optional fact
IDs, selected graphic keys, and ordered slide IDs. An element record is `object_id`, semantic role,
kind (`text`, `image`, `table`, or `shape`), normalized bounds, and exactly one content/graphic/table
reference. A graphic placement is `graphic_key`, `dataset_id`, render mode, alt text, normalized
bounds, and output digest. An appendix claim is claim text, ordered supporting fact IDs, definition or
calculation note, and originating main-slide ID.

An approval record is `stage`, `approved_sha256`, `approved_at`, and `upstream_sha256`; a
supersession record is only the predecessor `run_id` plus a private reason digest. A source-snapshot
record is `source_alias`, `captured_at`, contract version, private locator, source revision when
available, canonical content hash, and ordered captured ranges/bundle keys. Conflict and missing-item
records name the `fact_id`, affected module keys, source candidates or absence reason, and blocking
status. `artifact_sha256` is an ordered map from canonical run-relative path to lowercase SHA-256.
A cleanup receipt is resource role, deletion attempt time, verified-absent time, and fixed result
code. A promotion attempt is monotonic attempt number, start time, reconciliation result, allowlisted
private final ID/version, pre/post canonical hashes, fixed failure code when applicable, and finish
time.

Identifiers are closed contracts:

- `fact_id` is a canonical dotted snake-case registry key such as `position.bank_balance`; repeated
  observations append `@<period-slug>`, where the slug is lowercase ASCII letters/digits joined by
  hyphens, such as `history.year_end_balance@fy-2025-26`. Source adapters and calculation functions
  generate it; claims, conflicts, calculations, and provenance consume it.
- `dataset_id` is `<graphic-key>-<12 lowercase hex>`, where the suffix is the first 12 characters of
  the canonical `GraphicDataset` SHA-256. The dataset builder generates it; `GraphicSpec` and both
  render paths consume it.
- `source_alias` is one of the code-owned snake-case values `budget_timeseries`,
  `treasurer_briefing_inputs`, `reimbursement_bundle`, or `run_override`; bindings and provenance use
  the same values and private inputs cannot add an alias.
- `task_id` is `task-<module-slug>-<12 lowercase hex>`; `slide_id` is
  `slide-<layout-slug>-<12 lowercase hex>`; and table `row_id` is
  `row-<table-key-slug>-<12 lowercase hex>`. The readable slug comes only from a code-owned module,
  layout, or table key—never private text—and is lowercased, maps underscores/non-alphanumerics to
  one hyphen, trims hyphens, and truncates to 20 characters. The suffix hashes the complete canonical
  task/slide/row record without its ID. `models.py` generates and namespace-collision-checks these
  IDs; brief/deck/claim/table consumers never regenerate them.
- Generated Slides element object IDs are `tdr_<role-slug>_<12 lowercase hex>` using the same
  code-owned slug normalization (hyphens mapped to underscores for this grammar) and a hash of the
  canonical element without its ID. `google_client.py` generates them; Slides requests/readback use
  them. They must match `[A-Za-z0-9_][A-Za-z0-9_-]{4,49}` and collide with neither template nor other
  run objects.
- Workspace `workspace_key` is a random version-4 universally unique identifier (UUIDv4). Google
  file/chart/revision IDs are opaque API-returned private strings and are stored only in their
  allowlisted manifest fields.
- `app_property_namespace` is the fixed code-owned identifier `pta_finance_treasurer_slides_v1`;
  bootstrap generates no variant, and every Drive reconciliation query uses exact equality.
- Every displayed or supplied approval digest is exactly 64 lowercase hexadecimal SHA-256
  characters and is compared to the stage artifact named by the command.

Every file is private and ignored. The durable state sequence is:

```text
PREPARED
  -> BRIEF_APPROVED
  -> FACTS_APPROVED
  -> PREVIEW_PASSED
  -> STORY_APPROVED
  -> CANDIDATE_CREATED
     -> QA_PASSED -> CANDIDATE_APPROVED -> PROMOTED
     -> QA_FAILED (terminal for that candidate)
```

An approval stores the stage name, approved SHA-256 digest, timestamp, and upstream digests. A
changed request, fact snapshot, module selection, slide plan, graphic, candidate revision, or QA
report invalidates every downstream approval. The tool never infers an approval from a previous run.

`prepare-treasurer-deck` captures one immutable fact snapshot for the run. Brief approval does not
refresh it: the operator next reviews the same captured facts. If the source changes before
candidate creation, the run is marked stale and a new prepare/run is required; facts are never
silently refreshed in place beneath an approval.

`update-treasurer-deck` has one deterministic action per current state:

| Current state | Update behavior | Required next operator action |
|---|---|---|
| `PREPARED` | Validate and point to the brief review; no progression | Approve the brief digest |
| `BRIEF_APPROVED` | Validate and point to the captured fact review; no source refresh | Approve the fact digest |
| `FACTS_APPROVED` | Render and locally QA the storyboard | Approve the story digest |
| `PREVIEW_PASSED` | Validate and point to the locally QA-passing story review; no progression | Approve the story digest |
| `STORY_APPROVED` | Recheck source hashes, create and QA one private candidate | Review actual thumbnails and approve the candidate digest |
| `CANDIDATE_CREATED` | Reconcile the exact candidate, verify staging cleanup, and resume automated QA; never create a second candidate | Review thumbnails after QA passes, or start a linked run if QA fails |
| `QA_PASSED` | Report the candidate and QA evidence; no remote change | Approve the candidate digest |
| `CANDIDATE_APPROVED` | Report that the run is promotable, including any recorded failed copy attempt | Invoke or safely retry the separate promotion command |
| `QA_FAILED` | Report a terminal failed candidate | Correct inputs/layout and create a new linked run/version |
| `PROMOTED` | Report the existing final; no remote change | Start a new run for a revision |

`--dry-run` performs transition planning and validation only: it changes no run state, writes no
local artifact, does not mint or refresh OAuth credentials, and performs no Google or Storage
mutation.

`--preview-only` permits the same validation and local transitions through `PREVIEW_PASSED`, then
hard-stops. At `STORY_APPROVED` or any later state it reports what the remote action would be but
does not construct OAuth/Google/Storage clients or mutate the run. A failed post-copy comparison
leaves the run `CANDIDATE_APPROVED`, appends an immutable failed-attempt record and private copy ID,
and never accepts that copy; an explicit retry first reconciles prior attempts by `appProperties`.

Remote file IDs, revision IDs, OAuth metadata, and signed/thumbnail URLs never enter a committed
file. URLs used for image insertion or thumbnail download are never persisted in the private bundle
either; only resulting content hashes and cleanup receipts are retained. Run mutations use a
per-run single-writer lock and manifest-generation compare-and-swap. Each multi-file transition
writes immutable artifacts first and atomically replaces the manifest last; resume validates every
referenced digest and treats partial/corrupt state as blocked rather than guessing.

### 5.2 Brief and request cleanup

`--request` always names a private UTF-8 text file; literal request text is never accepted on the
command line, where it could leak through shell history or process listings. The request is limited
to 1 MiB. Brief proposals, fact overrides, and story directives are UTF-8 JSON regular files limited
to 5 MiB. Inputs are opened read-only, read once, and are neither echoed nor passed to a shell.
Symlinks/reparse points and non-regular files are rejected. Autoescaped HTML plus the network-free
preview boundary treat all input text as data.

The deterministic cleaner:

- repairs/flags invalid Unicode and replacement characters;
- normalizes whitespace and repeated headings;
- identifies obvious standalone recording/export filenames and transcript metadata;
- separates task-like questions from conversational workflow guidance;
- retains source spans for every proposed task;
- records every discarded fragment as `{source_span, reason, confidence}`; and
- never converts an ambiguous sentence into an approved financial fact.

A `source_span` is `{start_codepoint, end_codepoint, start_line, start_column, end_line,
end_column, fragment_sha256}`. Code-point offsets are zero-based and half-open into the exact decoded
request before cleanup; line/column coordinates are one-based and point to the same bounds; the
fragment digest covers the exact UTF-8 substring. Every task, workflow-guidance item, and ignored
fragment has one or more non-overlapping spans whose request digest matches the run. Cleanup may
normalize its copied display text but never changes the coordinates or fragment digest.

For the motivating request shape, opening/closing prose such as “get facts before making slides” or
“challenge the narrative before presenting” becomes workflow guidance, while the requested finance
questions become module/fact requirements. A stray recording filename and malformed character are
logged as ignored noise. No content is silently dropped.

An optional `--brief-proposal <private-json>` accepts a provider-neutral suggestion document using
the `brief.draft.json` task/guidance/ignored subset plus `schema_version` and `request_sha256`; it
cannot contain facts or approvals. The same source-span and ignore-log validators apply. A private
story directive is separately limited to `schema_version`, ordered `module_order`,
`excluded_modules`, graphic keys/selectors, appendix preferences, and `reason`. This preserves a
future model-assisted cleanup workflow without adding an LLM dependency or giving a model authority
over facts.

### 5.3 Fact records and source precedence

Each fact records at least:

- logical `fact_id`, label, typed value, and unit;
- period and/or `as_of_date`;
- basis such as `cash`, `reserve`, `allocated`, `committed`, `spent`, `received`, `pending`,
  `projected`, `definition`, or `calculated`;
- origin such as `observed`, `operator_supplied`, `derived`, or `projected`;
- maximum audience eligibility `public_aggregate` or `internal`;
- source alias, tab/range or bundle-key locator, source revision/hash, and capture timestamp;
- definition/source note where needed; and
- status `available`, `missing`, `conflicting`, `stale`, or `not_applicable`.

An available fact value is exactly one of UTF-8 string, boolean, integer, finite `Decimal`, or ISO
date. Unit is one code-owned value: `text`, `boolean`, `count`, `percent`, `date`, or
`currency:<ISO-4217-code>`; missing/non-applicable facts carry `value=null` but retain the expected
unit. Series live as repeated uniquely period-qualified facts or as `GraphicDataset`, never as an
untyped list inside one fact.

Money uses `Decimal`; non-finite and malformed values fail validation. Derived facts retain their
input fact IDs and a versioned calculation identifier. No displayed total is retyped into a title or
subtitle; it is formatted from the approved fact on every run.

Audience eligibility is monotonic. Code-owned source-field policy and the catalog each set a
maximum; combining inputs takes the most restrictive value. A briefing row, override, binding, or
story directive may restrict eligibility but can never promote `internal` data to
`public_aggregate`. `Budget Timeseries` fiscal year, type, measure, category group, strategic group,
strategic goal, and numeric aggregate values may feed public aggregates. Raw category, source tab,
grade/class, requestor, vendor/payee, receipt/evidence references, and free text are internal.
Reimbursement public output is limited to overall approved aggregate totals; dimension and
requestor graphics are internal in v1. Unknown fields default to internal. The production public
guard checks the resulting field lineage as well as rendered text, so a literal
`audience="public"` cell is never an authority grant.

The feature constructs a dedicated `ReadOnlySheetsReader` from service-account credentials scoped
exactly to `spreadsheets.readonly`; it does not wrap or subclass the existing write-capable
`SheetsClient`. `report_source.read_timeseries` is widened only at the type boundary to accept a
small structural `ValuesReader` protocol (`read_values(tab)`). Existing `SheetsClient` callers still
conform, while the deck path can reuse the real parser through a client that has no write methods.
The dedicated client additionally exposes `read_grid(tab, a1_range) -> GridSnapshot` through the
Sheets `spreadsheets.get` grid-data surface. `GridSnapshot` is source alias, worksheet title,
requested A1 range, returned rectangle, source revision when available, and row-major cells. Each
`CellSnapshot` is zero-based row/column within that rectangle, A1 coordinate, typed
`effective_value`, typed `user_entered_value` or formula, and number-format kind. `read_values` is only the evaluated
projection needed by the existing parser; it is not used as provenance. The source snapshot hashes
the canonical grid cells, so formatted currency text is never the canonical money value.

A versioned `GraphicDataset` is stored alongside scalar facts. It contains `schema_version=1`,
`dataset_id`, ordered column descriptors (`key`, scalar kind, unit, sensitivity), ordered row
objects, `source_fact_ids`, `source_grid_hashes`, `calculation_id`, selector/echo state, declared
totals, and a provenance hash.
Column scalar kind is exactly `string`, `integer`, `decimal`, `money`, `percent`, `date`, or `period`;
unit is required for `money`/`percent` and otherwise `null`; sensitivity uses the same monotonic
`public_aggregate`/`internal` lattice as facts.
Rows preserve source ordering unless the named calculation declares a sort. Numeric cells use
finite `Decimal`; missing, numeric-text, ambiguous headers, duplicate keys, selector mismatch, and
declared-total disagreement block the dataset. This is the sole producer shape consumed by
`GraphicSpec`; renderers never query the live workbook.

When reimbursement information is selected, `sources.py` calls the current strict schema-v2
`reimbursement_report.load_bundle` on the configured private bundle. It derives outstanding facts
from the validated `ReimbursementReport.summary`, `active_tickets`, settled workflow state, and
supplemental payment events; unmatched supplemental evidence is never counted as an obligation.
Person-level detail remains internal. Public mode can consume only the resulting approved aggregate
facts, never raw tickets, messages, evidence, or requestor labels.

Source precedence is explicit, not silent:

1. validated per-run private override with a stated reason and expected superseded source hash;
2. operator-owned `Treasurer Briefing Inputs` record;
3. canonical structured dataset or validated reimbursement summary;
4. derived fact from approved inputs.

If two sources disagree and no override explicitly resolves the conflict, the fact is
`conflicting` and required modules cannot advance. Optional unavailable facts are omitted and
logged. Required unavailable facts block story approval and publication.

The read-only `Treasurer Briefing Inputs` contract is one header row with:

```text
fact_key, period, label, value, unit, basis, as_of_date,
audience, definition, source_note
```

Rows are uniquely identified by `(fact_key, period, label, basis)`. Supported units and basis values
are enumerated in code. `audience` is an optional restriction only; blank or unrecognized values
become internal. The operator creates and maintains the tab; this feature only reads it. Private
override input is a strict envelope with `schema_version=1`, ordered `facts`, and ordered
`table_graphics`; unknown root or entry fields reject the file. Each fact entry uses the same fact
shape plus `reason` and `replaces_source_hash`, and inherits the same no-promotion audience rule.
Each table-graphic entry is exactly `graphic_key="participation_table"`, `input` containing the
`TableGraphicInput` shape from section 5.5, `reason`, and `replaces_source_hash`. That hash is required
when replacing an existing structured source; it may be `null` only for the first structured input
with `reason="initial_structured_input"`. The briefing Sheet
tab remains fact-only in v1; structured participation data has this one explicit ingress and cannot
be inferred from the legacy image or free text.

Slides show only a human-safe source alias and as-of date. Exact spreadsheet IDs, tab/range locators,
formulas, bundle keys, revisions, and hashes remain in the private fact snapshot and never appear in
a public presentation.

### 5.4 Content-module catalog

The v1 semantic palette starts from the workbook's established roles: income/core blue `#3C78D8`,
expense red `#CC0000`, net yellow `#F1C232`, student/classroom green `#6AA84F`, fundraiser/event
magenta `#A64D79`, enrichment orange `#E69138`, community/volunteer teal `#45818E`, and grade-support
purple `#674EA7`. Proposed/actual/forecast variants are generated by versioned token rules with
contrast tests rather than chart-specific ad hoc colors. Neutral, warning, missing-data, type,
spacing, and grid tokens live beside the palette in `theme.py`. The type token pins Noto Sans with
its SIL Open Font License (OFL): the Web Open Font Format 2 (WOFF2) file used by local preview is
vendored, while setup verifies the same family
in the private Google Slides template. The initial `theme_version` is the exact constant
`treasurer-slides-theme-v1`.

The v1 registry contains the following canonical requirements. `[*]` means ordered observations for
at least three requested periods; period/as-of metadata lives on each record. Fiscal year is
abbreviated `FY`. “MAD” is treated as a
private configured fundraising-stream label—the code never guesses or expands the acronym.

| Module | Required facts/datasets | Optional facts | Default behavior |
|---|---|---|---|
| `overview` | `position.bank_balance`, `position.reserve_amount`, `position.committed_amount`, `position.available_amount`, `fundraising.goal_total`, `fundraising.received_total` | `position.outstanding_obligations`, `budget.spent`, `fundraising.pending_total` | Included unless explicitly skipped |
| `current_position` | `position.bank_balance`, `position.reserve_amount`, `position.reserve_definition`, `position.committed_amount`, `position.available_amount` | `position.outstanding_obligations`, `position.unreflected_checks`, `position.other_obligations` | Selected by request |
| `history` | `history.year_end_balance[*]`, `history.revenue[*]`, `history.expense[*]` | `history.reserve[*]`, `history.reserve_peak`, `history.change_explanations` | Selected by request/data availability |
| `year_comparison` | `comparison.prior_actual_revenue`, `comparison.prior_actual_expense`, `comparison.current_budget_revenue`, `comparison.current_budget_expense` | `comparison.category_deltas`, `comparison.reserve_funded_nonrecurring` | Selected by request |
| `budget_status` | `budget.expense_total`, `budget.spent`, `budget.committed`, `budget.remaining` | `budget.contingent_items`, `budget.remaining_projection` | Selected by request |
| `fundraising` | `fundraising.goal_total`, `fundraising.received_total`, `fundraising.remaining_cash_gap` | `fundraising.mad_goal`, `fundraising.mad_received`, `fundraising.match_received`, `fundraising.match_pending`, `fundraising.sponsorship_received`, `fundraising.sponsorship_committed`, `fundraising.other_received`, `fundraising.other_projected` | Selected by request |
| `reserve_sustainability` | `reserve.current`, `reserve.target_definition`, `reserve.projected_use`, `reserve.year_end_goal_met`, `reserve.year_end_shortfall` | `reserve.recurring_revenue`, `reserve.recurring_expense`, `reserve.recurring_gap` | Selected by request |
| `spending_breakdown` | one selected approved expense `GraphicDataset` | alternate goal/program/category datasets | Selected by request |
| `spending_controls` | `controls.approval_process`, `controls.receipt_requirements`, `controls.budget_check` | `controls.board_review_cadence`, `controls.parent_facing_controls` | Selected by request |
| `appendix` | none beyond selected-module facts | definitions, calculations, caveats, extended detail, source notes | Automatically assembled |

Four calculation IDs are built in and may run only when all inputs share compatible period/as-of
and basis: `available_cash_v1 = bank_balance - reserve - committed - other_obligations`,
`budget_remaining_v1 = expense_total - spent - committed`,
`fundraising_cash_gap_v1 = max(goal_total - received_total, 0)`, and
`recurring_gap_v1 = recurring_expense - recurring_revenue`. Otherwise the corresponding fact is
missing/conflicting and requires an operator-supplied value with provenance; pending or projected
fundraising never silently reduces the cash gap.

Each module declares required and optional facts, audience eligibility, layout choices, supported
graphics, main-slide density limits, and appendix spill rules. Main-slide claims are short and cite
fact IDs; nuance, calculation notes, definitions, and extended prose move to appendix slides with a
cross-reference. An unresolved required fact, unsupported claim, or public-safety violation blocks
rendering rather than producing a placeholder.

The template contract is `treasurer-slides-template-v1` on a 10 × 5.625 inch page (720 × 405
points). Its layout keys are `title`, `overview_4up`, `headline_chart`, `comparison_2col`,
`dense_table`, `process_flow`, and `appendix_text`. Each prototype uses fixed object IDs of the form
`td_<layout>_<role>` for applicable roles from `title`, `subtitle`, `body`, `metric_1`–`metric_4`,
`visual`, `table`, `source`, and `page`; text placeholders also contain the unique fallback token
`{{TD:<role>}}`. `check-treasurer-slides` requires exactly one expected object/token per role and
rejects missing, duplicate, or unexpected contract objects.

Layouts are single-source normalized rectangles `(x, y, width, height)` in `[0,1]`. Local preview
maps them to 1600 × 900 Cascading Style Sheets (CSS) pixels; Slides maps them to 720 × 405 points and
rounds once to 0.001 pt.
No renderer maintains independent pixel/point coordinates. Generated Slides object IDs use the
run-scoped deterministic ID rule from section 5.1, while template object IDs remain fixed.

### 5.5 Graphics capability registry

“Supports all graphics” means the tool can reproduce the same data, categories, series, ordering,
annotations, chart family, and audience meaning using the shared design tokens. Pixel-for-pixel
identity with the workbook is not required. A capability need not appear unless requested by a
selected module.

The v1 logical catalog contains exactly these keys and dataset/calculation contracts:

| Key / capability | Family | Required ordered dataset columns and rule | Maximum eligibility |
|---|---|---|---|
| `fy_fundraising_expense_net` — fundraising / expense / net by FY | grouped column | `fiscal_year, fundraising, expense, net`; `net = fundraising - expense` | `public_aggregate` |
| `budget_vs_actual` — budget versus actual | grouped column | `category, budget, actual`; source order; category lineage applies | `public_aggregate` |
| `income_by_item` — income by item | pie | `item, amount, item_order`; positive amounts; raw-item lineage | `internal` |
| `expense_by_item_group` — expense items grouped by strategic group | custom gradient donut | `item, strategic_group, amount, item_order, group_order`; one slice per raw item, contiguous groups, light-to-dark gradient within each group, one legend entry per group | `internal` |
| `spend_by_goal_program` — spend by strategic goal/program | stacked horizontal bar | `goal, program_area, amount, goal_order, program_order`; explicit zero-fill pivot | `public_aggregate` |
| `program_to_goal_flow` — program area to strategic goal | custom Sankey | `program_area, goal, amount, program_order, goal_order`; positive flows only | `public_aggregate` |
| `group_comparison_a` — left comparison slot | pie | `raw_category, amount, item_order`; explicit `category_group` + `fiscal_year` selectors and title echo | `internal` |
| `group_comparison_b` — right comparison slot | pie | same contract as slot A with an independently supplied selector pair | `internal` |
| `reimbursements_by_dimension` — reimbursements by selected dimension | pie | `dimension, label, amount`; explicit dimension enum; descending top-20 rule | `internal` |
| `selected_category_by_requestor` — selected category by requestor | pie | `requestor, amount`; explicit canonical-category selector; descending top-15 rule | `internal` |
| `income_expense_forecast` — historical and forecast income/expense | line | `period, actual_income, actual_expense, forecast_income, forecast_expense`; nullable actual/forecast boundary is explicit | `public_aggregate` |
| `budget_comparison_by_category` — budget comparison | grouped column | `category, series_key, amount, category_order`; binding declares the exact series-key set | `public_aggregate` |
| `revenue_scenarios` — scenario totals | grouped column | `scenario, revenue_total, scenario_order`; parameter-set digest and formula calculation ID required | `public_aggregate` |
| `participation_table` — legacy participation view | native Slides table | strict `TableGraphicInput` below | `internal` |

Selector contracts are closed and never inherit workbook UI state:

- `group_comparison_a` and `_b` are stable presentation slots, not hard-coded groups. Each selector
  is `{category_group, fiscal_year}` from the approved snapshot. One requested comparison uses slot A;
  two use the story directive's order for A then B. More than two blocks. The source echo must equal
  `Showing: <category_group> | FY<fiscal_year>` after whitespace normalization, and the captured pie
  rows must be the selected group's proposed amounts by `raw_category`. Because those labels are raw
  categories, both capabilities are internal even if a particular workbook value looks harmless.
- `reimbursements_by_dimension.dimension` is exactly one of `category`, `form_type`, `payment_type`,
  `month`, or `requestor`, mapped respectively to the validated canonical category, form type,
  payment type, `YYYY-MM` month, or requestor field. Rows sort by descending `amount`, then normalized
  `label`, and retain at most 20. The required echo is `Reimbursement $ by <display-label>`, where the
  display-label map is code-owned and one-to-one with the enum.
- `selected_category_by_requestor.canonical_category` must exactly match one category present in the
  approved reimbursement snapshot. Rows aggregate amount by requestor, sort by descending `amount`
  then normalized requestor, and retain at most 15. The selector echo must exactly match the selected
  category; the renderer generates its title from that value rather than trusting a static chart
  title. Requestor labels and the category lineage keep the capability internal.

For each native chart, the approved binding's domain/series/selector ranges are captured as a
`GraphicDataset` with both evaluated cells and entered/formula provenance. The source range is the
parity oracle; no uncommitted scratch formula is re-created from memory. The two custom graphics use
the explicit long-table contracts above from a bound helper range or private override. This avoids
assuming optional `strategic_group`/`strategic_goal` columns are part of the required
`Budget Timeseries` contract. Forecast and scenario datasets also preserve their input/parameter
digests, so the renderer never extrapolates or invents a projection.

Native adapters pin the workbook edge cases discovered during inventory: a blank-header `QUERY`
result begins on its first returned row; formula spills are captured as explicit rectangles; hidden
source columns remain readable data rather than silently disappearing; numeric text is rejected or
explicitly parsed before staging; and each staging chart is created from one complete chart spec
rather than a partial `updateChartSpec`. Source `styleOverrides` are evidence only because v1 owns
its theme, including the readback case where index zero is omitted.

Each `GraphicSpec` records logical key, dataset query/calculation ID, chart/table family, series semantics, selector
state, source fingerprint, theme roles, audience class, alt text, aspect ratio, and layout
constraints. Live discovery matches a native chart by worksheet plus unique title/type/range and a
semantic fingerprint covering domains, series, order, stacking/axis meaning, selectors, and
data-bearing annotations. Source colors and cosmetic style are recorded as comparison evidence but
do not block theme-normalized rendering unless the style encodes a declared semantic role. Zero or
multiple matches, unexpected semantic drift, stale hard-coded data annotations, or an unknown
discovered graphic blocks candidate creation until deliberately reconciled.

Live identities are resolved through a private, versioned graphics-binding manifest, never through
tracked chart IDs. Its root is `schema_version=1`, `catalog_version="treasurer-graphics-v1"`,
`source_alias="budget_timeseries"`, source snapshot hash, `status` (`draft` or `approved`), ordered
entries, and manifest digest. For each native logical key, an entry records the worksheet title,
expected chart title/type, domain and series ranges, selector/echo cells, expected semantic
fingerprint, and last resolved private chart ID. `init-treasurer-slides` performs read-only
discovery and writes a draft binding manifest plus review. The operator seals that exact artifact
with `approve-treasurer-bindings --digest <displayed-digest>`; `check-treasurer-slides` thereafter
re-resolves and validates it without modifying either the source or binding. Ambiguity or drift
produces a new draft and blocks candidates until a replacement binding is explicitly approved.

The participation capability consumes a strict private `TableGraphicInput` rather than trying to
scrape the legacy PNG. Its schema is `title`, ordered `columns` (`key`, `label`, value kind, and
alignment), ordered `rows` (`row_id` plus keyed cells), optional totals, `as_of_date`, source note,
and fixed `audience="internal"`. It may come only from the override envelope's `table_graphics[]` in
v1; missing structured input leaves that capability available but not renderable for a request that
selects it: an unselected run records `available_not_selected`, while selecting it without valid
input records `blocked`.

Column `key` is unique snake case; `kind` is one of `text`, `integer`, `decimal`, `currency`,
`percent`, or `date`; and alignment is `left`, `center`, or `right`. Every row has exactly one cell
per declared key, with a value matching the column kind; blank text is allowed, but missing keys,
extra keys, binary floats, formulas, URLs, and non-finite values block. Optional totals use the same
keyed-cell shape and only numeric column kinds. The row-ID rule from section 5.1 binds each canonical
row without exposing cell text in its readable prefix.

Every catalog entry has exactly one per-run state: `rendered`, `available_not_selected`,
`excluded_by_audience`, or `blocked`. Discovery evidence is recorded separately. A blocked entry
prevents candidate creation; an available-but-unselected entry proves capability coverage without
forcing the graphic into the deck.

Production rendering uses two paths:

- The 11 native-chart capabilities are rebuilt from the approved snapshot datasets in a temporary,
  app-owned staging Sheet using the shared theme, then imported into Slides as static unlinked
  images. The source workbook is never edited, no source link remains, and the staging file is
  removed after verified insertion. Sanitized presentation readback must contain an image element,
  not a `sheetsChart`, and must contain no staging spreadsheet ID or URL before deletion proceeds.
- The custom donut and Sankey are committed deterministic Matplotlib renderers. Their approved
  aggregate PNGs are inserted through short-lived private asset staging. The participation image is
  not copied; its structured private data is rebuilt as an editable native Slides table and is
  rejected in public mode.

Local preview renderers consume the same `GraphicSpec` and immutable datasets. Candidate QA validates series,
labels, totals, ordering, aspect ratio, and actual thumbnail rendering rather than assuming the
local and Google renderers are pixel-identical.

### 5.6 Authentication, template, and asset boundaries

V1 uses three deliberately separate authorization surfaces:

1. **Source Sheet reader:** existing service-account key loaded with exactly
   `https://www.googleapis.com/auth/spreadsheets.readonly` for this feature. No write-capable Sheet
   client is constructed.
2. **Slides workspace:** a dedicated desktop OAuth token, separate from the Gmail token and pinned
   by exact equality to `https://www.googleapis.com/auth/drive.file`. This scope supports the
   app-owned template, private folders, staging Sheets, Slides requests, and thumbnails without
   broad Drive access. Credential loading follows the proven Gmail pattern—load, exact-scope
   recheck, refresh, atomic token persistence, and actionable malformed/dead/Testing-mode
   re-consent errors—but uses a distinct desktop client/token file and never imports Gmail scopes.
   V1 uses the existing private Cloud project and its External/Testing consent screen, with the
   Treasurer account listed as a test user. The resulting approximately seven-day refresh-token
   lifetime and recurring browser re-consent are explicit accepted operating costs; the tool does
   not claim long-lived unattended access or reuse the Gmail token.
3. **Custom image staging:** the service account may create/read/delete opaque objects in one
   private Cloud Storage bucket. The same key may back a separately constructed Storage credential
   with exactly `https://www.googleapis.com/auth/devstorage.read_write` and bucket-level Identity and
   Access Management (IAM) limited
   to the required object operations; the `spreadsheets.readonly` credential object is never passed
   to Storage. Signed GET URLs last no more than 15 minutes, contain no semantic filename, are never
   logged, and are deleted in `finally`. A lifecycle rule is only a fallback. Cleanup failure marks
   QA failed and blocks promotion; an acceptance check proves the object/URL is inaccessible
   afterward.

A V4 signed URL is a temporary bearer URL, not a private authenticated URL. Its query normally
contains a signer credential identifier, and Slides can retain the expired source URL in image
metadata. V1 accepts that residual only for audience-approved aggregate custom graphics. The bucket,
object name, Cloud project, and signing principal must be generic/non-identifying; the expired URL is
stripped from every local log, exception, readback projection, and content hash. If that residual is
not acceptable for a future graphic, the direct-image path must block and a later PPTX/Apps Script
design decision must be reopened rather than silently weakening this boundary.

The optional private `[slides]` config contains only fake equivalents in the example file:

```toml
[slides]
client_secrets_file = "secrets/slides-client-secret.json"
token_file = "secrets/slides-token.json"
workspace_manifest = "reports/output/treasurer-slides/workspace.json"
graphics_bindings_file = "reports/output/treasurer-slides/graphics-bindings.approved.json"
image_staging_bucket = "<private-bucket-name>"
briefing_inputs_tab = "Treasurer Briefing Inputs"
reimbursement_bundle_file = "reports/output/reimbursement-report.json"
```

`Config.slides` is `None` when the block is absent, preserving every current config/test. The
config is valid before any Google file exists: `init-treasurer-slides` creates the app-owned folders
and initial template, then atomically writes their IDs and initial revisions to the configured,
gitignored workspace manifest. Later commands require that manifest and validate its contract. The
operator customizes the app-created template after bootstrap. The template uses generic tracked
placeholder tokens and prototype layouts. Branding and Google IDs remain private.

The workspace manifest root is `schema_version=1`, random version-4 universally unique identifier
(UUIDv4) `workspace_key`,
`app_property_namespace`, `created_at`, `template` (`file_id`, `revision_id`, `content_sha256`,
`contract_version`), and `folders` with exact `candidate`, `final`, and `staging` IDs. Unknown fields
or roles block. `app_property_namespace` is the fixed generic value
`pta_finance_treasurer_slides_v1`. Remote artifacts carry only that namespace, workspace key, run ID, artifact
role, and attempt number in `appProperties`; those values are the reconciliation key and contain no
organization identity.

Bootstrap is boundedly idempotent. If the workspace manifest is valid, `init-treasurer-slides`
validates it without creating or mutating a Google workspace resource. It may atomically refresh
the dedicated local token and replace the gitignored binding draft/review produced by read-only
discovery. If setup crashed before the manifest commit, stable generic `appProperties` identify
each expected folder/template role: zero matches may be created, exactly one is reconciled, and
multiple or contradictory matches block for operator repair. Init never guesses among matches,
replaces a customized template, or deletes an artifact.

External calls use a single bounded retry contract: 30-second request deadlines, at most five
attempts and 60 seconds total, full-jitter exponential backoff, and a bounded `Retry-After` when
present. HTTP 429/500/502/503/504 and transport timeouts are retryable. GETs and idempotent deletes
may retry directly; create/copy/batch mutations first reconcile deterministic object IDs or
`appProperties` after an ambiguous timeout and retry only when readback proves the mutation absent.
One credential refresh may follow a 401; other 4xx responses are permanent redacted failures. A
depleted retry budget leaves the run resumable and never triggers a broader-scope or linked-chart
fallback.

Every Google, Storage, and HTTP call sits behind a translation boundary that raises fixed,
redacted feature errors. Raw client exceptions and raw API responses are never serialized or
printed: they can contain file IDs, facts, source URLs, signer identities, or thumbnail URLs. The
private manifests store only allowlisted fields: resource IDs and revisions required for resume may
appear only in their designated gitignored workspace/run fields, and approved finance facts only in
the designated fact snapshot. Signed URLs, thumbnail URLs, account identities, unprojected response
fields, and raw API responses are never persisted.
Tests inject canary values and prove no private value reaches committed artifacts, stdout, stderr,
exception messages, or any non-allowlisted private-manifest field.

The authorization and transport choices above follow Google's documented contracts: service
accounts cannot own ordinary My Drive files, `drive.file` is the recommended narrow per-file scope,
Slides accepts that scope, static Sheets charts can be inserted without a source link, and raster
insertion requires a fetchable URL. External/Testing OAuth refresh tokens expire after seven days
outside the basic identity-scope exception. See Google's [Drive storage error guidance](https://developers.google.com/workspace/drive/api/guides/handle-errors#storageQuotaExceeded),
[Drive scope guidance](https://developers.google.com/workspace/drive/api/guides/api-specific-auth),
[Slides scope table](https://developers.google.com/workspace/slides/api/scopes),
[Sheets-chart insertion guide](https://developers.google.com/workspace/slides/api/guides/add-chart),
[image insertion guide](https://developers.google.com/workspace/slides/api/guides/add-image), and
[OAuth refresh-token guidance](https://developers.google.com/identity/protocols/oauth2#expiration).

No command calls the Drive permissions API. `public` means content-safe, not publicly shared.
Folder access-control lists (ACLs) remain an operator-owned Drive concern. Promotion does not make the presentation
publicly accessible; an operator separately shares the final file/folder outside this tool.

`requiredRevisionId` is used only around Slides `batchUpdate`, because a Slides revision ID is
short-lived and Drive `files.copy` has no equivalent atomic Slides-revision precondition. Candidate
QA stores a sanitized canonical presentation-content hash plus the then-current Drive version and
Slides revision. That projection includes `image_sha256` for every image: QA immediately fetches the
API's expiring image `contentUrl`, hashes the bytes, and discards the URL. Immediately before
promotion the tool re-reads and re-hashes candidate structure and image bytes; after copying it does
the same for the final and requires exact equality before marking `PROMOTED`. Any mismatch retains
and marks the private copy as a failed attempt without accepting it.

### 5.7 Storyboard and visual QA

The local storyboard uses exact 16:9, 1600×900 canvases. Every checked-in layout declares its own
safe region, minimum occupied-area ratio, intentional-whitespace exemptions, permitted overlaps,
alignment grid, and type floors. Initial floors are 16 pt body text, 10 pt chart/table labels, and
8 pt source notes; a layout may be stricter but cannot bypass the global minimum without a committed
catalog change.

Playwright waits for `document.fonts.ready` and all images, then captures element bounding boxes,
computed styles, and screenshots. Storyboards use only local/data/blob assets; the Playwright
context aborts every HTTP(S) request so neither private text nor assets can escape through a remote
font/image load. Local `@font-face` loads the pinned vendored Noto Sans WOFF2. QA requires its exact
`FontFace.status == "loaded"`, a positive `document.fonts.check`, and a pinned sentinel-string
width within tolerance; a missing face or fallback therefore fails deterministically. It blocks:

- unresolved tokens/placeholders or missing images;
- content outside safe bounds or undeclared overlaps;
- grid/alignment drift outside tolerance;
- type below the declared floor;
- a layout-specific density/whitespace failure;
- slide/graphic/source digest mismatches; or
- required facts, public-safety violations, or unaccounted graphics.

Candidate QA separately calls `presentations.get`, validates page size/count, object IDs, text,
positions, tables, image-byte digests, and revision, then downloads each `LARGE` thumbnail
immediately into the ignored run directory. Expiring image-content and thumbnail URLs are never
logged or persisted. A private gallery shows the
actual Google rendering beside the storyboard. Candidate creation requires stored
`PREVIEW_PASSED` and `STORY_APPROVED` evidence, and the pipeline validates both before constructing
an OAuth client or any remote-writer service. Automated QA plus an operator acknowledgement bound
to the compound candidate-approval digest is required for promotion. The API exposes structure and
element boxes, not actual Google line wrapping/overflow, and v1 adds no optical character
recognition (OCR) or vision grader;
therefore automated candidate QA proves structure, values, positions, asset presence, and thumbnail
availability, while actual Google clipping, wrapping, and legibility remain a mandatory operator
check in the gallery.

Candidate approval seals one compound record: canonical candidate-content hash, current Drive
version, current Slides revision, candidate QA-report digest, and operator-reviewed gallery digest.
That approval digest—not a second bare QA digest—is the sole promotion authority. `QA_FAILED` is
terminal for that remote candidate; corrected facts/content/layout start a new linked run/candidate
version and retain the failed candidate for diagnosis.

## 6. Design Decisions

1. **Themes are reusable content modules, with one visual system in v1.** Multiple skins can be
   added later; v1 first establishes consistent semantic design tokens.
2. **Overview is default-on, not mandatory.** `--skip-overview` records an explicit choice.
3. **Facts precede narrative.** Request cleanup proposes tasks; only approved typed facts can
   support a slide claim.
4. **No embedded LLM in v1.** A provider-neutral proposal import permits assisted cleanup without
   adding network behavior, provider lock-in, or factual authority.
5. **Supplemental facts are operator-owned.** The optional Sheet tab and per-run override are read
   only. The tool never silently creates or updates either.
6. **Required versus optional is fail-closed.** Required missing/conflicting/stale facts block;
   optional facts are omitted and logged. No unresolved placeholders reach a candidate.
7. **Financial states remain distinct.** Cash, reserve, allocation, commitment, spending, received,
   pending, and projection are typed bases, not prose labels that can be conflated.
8. **Audience safety is monotonic runtime policy.** `public` is the default request policy, but
   code-owned source/catalog ceilings decide eligibility. Inputs may only restrict; public decks
   reject person/vendor/raw-category/private-reimbursement lineage even in an appendix.
9. **Full graphics capability does not imply universal inclusion.** All 14 registered capabilities
   are available when requested; module selection and audience policy decide what appears.
10. **Semantic parity, shared theme.** Reproduction preserves information and chart semantics; it
    need not preserve every workbook pixel.
11. **Selector state is input.** Interactive graphics never depend on the workbook's last UI state.
12. **Candidate first.** The template is never edited. Failed candidates remain private and visibly
    failed. The end-to-end command stops at a QA'd candidate.
13. **Finals are immutable by workflow.** Promotion copies rather than moves/mutates. Later changes
    create a new run/version and declare `supersedes_run_id`.
14. **Google ownership uses narrow user OAuth.** The service account reads the source; dedicated
    `drive.file` OAuth owns app-created Slides/Drive artifacts. The Gmail token is never reused;
    External/Testing mode's approximately weekly re-consent is accepted for this hands-on v1.
15. **Custom image exposure is bounded and explicit.** Only audience-approved aggregate graphics
    use short-lived signed URLs. V1 explicitly accepts their expired-source-URL metadata residual
    under the constraints in section 5.6. Private names in the legacy table use native table
    elements instead.
16. **One-shot operation.** Every command is operator-triggered and exits. No autonomous-observation
    step is warranted beyond the real M6–M8 acceptance sequence.
17. **Retention is conservative.** Temporary staging is verified deleted, but ignored runs,
    failed candidates, and finals are never auto-deleted in v1; archive/prune is a later explicit
    operator workflow.

## 7. Build Steps

Global project numbering continues at Step 14. Steps 14–23 are agent-completable code steps.
Numeric operator Steps 24–26 retain the M6–M8 labels so `/repo-sync` and `/build-phase` can discover
them while preserving the project's manual-step sequence.

<!-- autofix-applied: 2026-08-30 -->
### Step 14: Add the briefing bundle, read-only sources, and immutable fact snapshot

- **Problem:** Convert rough request text and private finance sources into one strict, reviewable,
  provenance-bearing run without exposing a write-capable source client.
- **Type:** code
- **Issue:** #27
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_deck/{__init__,models,intake,sources}.py`,
  `pta_finance/report_source.py`, `tests/test_treasurer_deck_{models,intake,sources}.py`
- **Produces:** versioned run/brief/fact/dataset contracts, a minimal reader protocol, dedicated
  read-only Sheet adapter, schema-v2 reimbursement summary adapter, fictional source fixtures, and
  locked atomic persistence
- **Done when:** tests prove cleanup/ignore provenance, typed financial bases and graphic datasets,
  source precedence, effective/user-entered/formula hashing, current reimbursement settlement and
  payment handling, required gaps/conflicts/stale overrides/non-finite money/public PII failure,
  optional omission logging, approval invalidation, run-ID/path containment, crash/corruption
  detection, and manifest-last commits; the source test double has no write method and every existing
  `read_timeseries` caller still passes
- **Depends on:** none
- **Status:** PENDING

<!-- autofix-applied: 2026-08-30 -->
### Step 15: Add content modules, theme tokens, and the offline storyboard

- **Problem:** Turn approved facts into a deterministic, dense but readable slide plan with terse
  main slides and appendix detail without any run-specific Google operation.
- **Type:** code
- **Issue:** #28
- **Flags:** `--reviewers code --isolation worktree`
- **Files:** `pta_finance/treasurer_deck/{modules,theme,storyboard}.py`,
  `pta_finance/treasurer_deck/templates/{review,storyboard}.html.j2`,
  `tests/test_treasurer_deck_{modules,storyboard}.py`
- **Produces:** module/fact-requirement catalog, versioned theme/layout geometry, claim and appendix
  assembly, autoescaped reviews, and fictional module/layout fixtures
- **Done when:** every v1 module renders deterministically from approved fictional facts; overview is
  default-on and suppressible; every claim resolves to fact IDs; exact provenance stays private;
  safe source/as-of aliases render; long nuance spills to appendix; normalized local/Slides geometry
  agrees; unapproved stages, required gaps, or public private-data contamination prevent output
- **Depends on:** Step 14
- **Status:** PENDING

<!-- autofix-applied: 2026-08-30 -->
### Step 16: Define the 14-capability catalog, graphic datasets, and private binding contracts

- **Problem:** Give every current workbook graphic one stable semantic identity and validated input
  shape without relying on live chart IDs, source styling, or last-used selector state.
- **Type:** code
- **Issue:** #29
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_deck/graphics_catalog.py`,
  `tests/test_treasurer_deck_graphics_catalog.py`
- **Produces:** exact catalog, `GraphicDataset` schemas/calculation IDs, audience ceilings, selector
  rules, semantic fingerprints, binding draft/approval schemas, and fictional discovery fixtures
- **Done when:** the catalog has exactly 14 keys and rejects unknown/duplicate entries; every dataset
  validates its declared columns, ordering, totals, selectors, calculation version, provenance, and
  sensitivity; fake read-only discovery uniquely resolves all native charts and creates a private
  draft; zero/multiple/semantic-drift matches block; source color-only drift is review evidence, not
  a blocker; public eligibility cannot be elevated by a tab or override
- **Depends on:** Steps 14 and 15
- **Status:** PENDING

<!-- autofix-applied: 2026-08-30 -->
### Step 17: Implement native, custom, and table graphic renderers

- **Problem:** Reproduce all 14 registered graphic semantics from approved immutable datasets through
  deterministic local renders and pure staging plans.
- **Type:** code
- **Issue:** #30
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_deck/graphics.py`,
  `tests/test_treasurer_deck_graphics.py`, `tests/fixtures/treasurer_deck/graphics/`
- **Produces:** four native-family adapters, pure staging-Sheet chart plans, deterministic expense
  donut and goal-flow renderers, editable participation-table plan, reference fixtures and digests
- **Done when:** fictional fixtures exercise every catalog key; native/local plans agree on evaluated
  values, labels, series, stacking, ordering, totals, selector echoes, aspect, alt text, and theme
  roles; expense-item gradients and goal flows match their pinned contracts; structured table input
  is internal-only; malformed/numeric-text/ambiguous/unsafe datasets fail closed; no Google writer is
  imported or called
- **Depends on:** Step 16
- **Status:** PENDING

<!-- autofix-applied: 2026-08-30 -->
### Step 18: Add the mandatory network-free Playwright preview gate

- **Problem:** Prove local layout density, bounds, fonts, assets, and placeholders before code can
  construct a run-specific Google writer or create a candidate; one-time setup remains separate.
- **Type:** code
- **Issue:** #31
- **Flags:** `--reviewers code --isolation worktree`
- **Files:** `pta_finance/treasurer_deck/qa.py`,
  `pta_finance/treasurer_deck/assets/fonts/*`, `pyproject.toml`, `uv.lock`,
  `.github/workflows/ci.yml`, `tests/test_treasurer_deck_local_qa.py`
- **Produces:** local QA report/contracts, real Chromium fixtures, optional `slides` dependencies,
  pinned Noto Sans WOFF2/OFL assets, and Linux CI browser setup
- **Done when:** real headless Chromium passes the good fictional deck and each broken fixture fires
  its intended overflow/bounds/overlap/font/alignment/density/image/token rule; every HTTP(S)
  request is aborted; exact vendored-font status and sentinel metrics are verified; base installs
  still import/run existing CLI commands without Slides extras; CI runs
  `uv run playwright install --with-deps chromium`; a failing local report makes the remote-writer
  seam unreachable in request-order tests
- **Depends on:** Steps 15 and 17
- **Status:** PENDING

<!-- autofix-applied: 2026-08-30 -->
### Step 19: Add dedicated Slides OAuth, workspace bootstrap, and binding approval

- **Problem:** Establish narrow human-owned Google workspace access and private template/binding
  state before candidate rendering.
- **Type:** code
- **Issue:** #32
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_deck/google_client.py`, `pta_finance/config.py`,
  `config.example.toml`, `tests/test_treasurer_deck_{auth,bootstrap}.py`
- **Produces:** auth/bootstrap/check primitives, bounded Google retry policy, strict optional Slides
  config, private workspace/binding manifests, and fake Drive/Slides/Sheets services
- **Done when:** exact `drive.file` equality is enforced; missing/extra/Gmail scopes are rejected;
  token load/refresh/atomic persistence and expected Testing-mode re-consent use a distinct token;
  config works before remote IDs exist; bootstrap creates only app-owned folders/template and records
  IDs atomically; rerun makes no remote mutation and partial setup reconciles by `appProperties`;
  binding approval seals the displayed digest; ordinary check uses only Google-resource GETs, with
  credential refresh and ignored drift review as its only local writes; bounded 429/5xx/timeout
  behavior and redaction are tested; no permissions API or source/template update is called
- **Depends on:** Steps 14, 16, and 18
- **Status:** PENDING

<!-- autofix-applied: 2026-08-30 -->
### Step 20: Create one private candidate with temporary chart/image staging

- **Problem:** Fill a copied template from a story-approved, locally QA-passing run while preserving
  the source/template and leaving no linked or temporary asset behind.
- **Type:** code
- **Issue:** #33
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_deck/{google_client,asset_staging}.py`,
  `tests/test_treasurer_deck_candidate.py`
- **Produces:** candidate renderer, native staging-Sheet and custom-image insertion, editable
  text/table requests, cleanup receipts, mutation reconciliation, and candidate failure states
- **Done when:** local QA failure or missing story approval produces zero candidate/staging writes;
  source drift blocks; the template is copied but never updated; native charts read back as images,
  not `sheetsChart`, with no staging ID/URL; custom URLs meet TTL/name/audience/redaction rules;
  Storage credentials are separately scoped; every staging file/object is deleted and verified;
  ambiguous timeout-after-commit is reconciled before retry; cleanup failure marks the candidate
  failed; no final/permission call exists; sequential crash/retry creates no second candidate
- **Depends on:** Step 19
- **Status:** PENDING

<!-- autofix-applied: 2026-08-30 -->
### Step 21: Add candidate readback, thumbnail QA, approval, and safe promotion

- **Problem:** Bind automated evidence and mandatory operator-visible Google rendering to one exact
  candidate, then copy only that approved content to a private final.
- **Type:** code
- **Issue:** #34
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_deck/{qa,google_client}.py`,
  `tests/test_treasurer_deck_promotion.py`
- **Produces:** canonical structure/image projection, candidate gallery, compound approval contract,
  promotion-attempt ledger, and safe copy/reconciliation primitives
- **Done when:** Google fakes prove structural/value/position/asset checks, immediate thumbnail and
  image-byte consumption with no URL persistence, compound candidate approval, short-lived revision
  use, pre/post-promotion structure/image equality, final readback, and refusal on wrong/stale
  approval, source/candidate drift, QA/cleanup failure, or duplicate ambiguity; retry creates no
  second accepted final; concurrent execution is blocked/detected; no sharing changes occur; actual
  wrapping/clipping/legibility remains explicitly assigned to operator Steps 25–26
- **Depends on:** Step 20
- **Status:** PENDING

<!-- autofix-applied: 2026-08-30 -->
### Step 22: Wire the CLI, immutable revisions, privacy guard, and local smoke

- **Problem:** Expose the approved subcomponents through one reimbursement-style CLI/resume path
  without weakening immutable corrections, approvals, or privacy boundaries.
- **Type:** code
- **Issue:** #35
- **Flags:** `--reviewers deep --isolation worktree`
- **Files:** `pta_finance/treasurer_deck/pipeline.py`, `pta_finance/cli.py`,
  `scripts/check_no_identity.py`, `tests/test_treasurer_deck_{cli,smoke}.py`
- **Produces:** CLI/resume/revision workflow, precise deck leak guard/tests, and local-component smoke
- **Done when:** commands implement the state/action, rejection/new-linked-run, and digest contracts;
  `--dry-run` has no local/auth/remote side effects; missing extras/browser fail actionably; console
  output never echoes an input path, fact, identity, private resource, raw exception, URL, or secret;
  real local components complete request → facts/datasets → modules → all graphics → storyboard →
  local QA with only Google APIs faked; existing CLI behavior and base-install imports remain green;
  the extended private-resource guard rejects canary IDs/URLs/artifacts without rejecting official
  documentation links or explicit fake placeholders
- **Depends on:** Step 21
- **Status:** PENDING

<!-- autofix-applied: 2026-08-30 -->
### Step 23: Prove packaging and reconcile operator/project documentation

- **Problem:** Ship the complete workflow and runtime assets from an installable wheel, then document
  exactly how the operator invokes and maintains it.
- **Type:** code
- **Issue:** #36
- **Flags:** `--reviewers code --isolation worktree`
- **Files:** `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml`,
  `pta_finance/treasurer_deck/{templates,assets}/**`, `docs/generating-treasurer-updates.md`,
  `README.md`, `SETUP.md`, `CLAUDE.md`, `plan.md`, `tests/test_treasurer_deck_packaging.py`
- **Produces:** wheel-install asset test, Linux Chromium CI gate, operator/project-context updates, and
  a merge-preserving private observatory launcher proposal documented in the operator guide
- **Done when:** `uv build` plus isolated wheel installation loads every Treasurer template, the Noto
  Sans WOFF2/OFL files, and `pta-finance` entry point; CI installs Chromium with OS dependencies;
  the operator guide matches the exact commands/states/re-consent/privacy boundaries; docs distinguish
  Treasurer Slides writes from deferred monthly-report Drive upload; full pytest, Ruff lint/format,
  strict mypy, CI, reimbursement/report/CLI regressions, and identity/private-resource guard pass;
  the launcher appendix names the exact five generic commands and the separate external-repository
  review/apply boundary without containing private IDs or values
- **Depends on:** Step 22
- **Status:** PENDING

All names below are `pta-finance` argparse subcommands. Copyable invocations use the repository's
actual console entry point:

```text
uv run pta-finance init-treasurer-slides
uv run pta-finance approve-treasurer-bindings --digest <displayed-digest>
uv run pta-finance check-treasurer-slides
uv run pta-finance prepare-treasurer-deck --request <private-text-file> --as-of <YYYY-MM-DD>
  [--audience public|internal] [--skip-overview]
  [--brief-proposal <private-json>] [--override <private-json>]
  [--story-directive <private-json>] [--supersedes-run <run-id>]
uv run pta-finance approve-treasurer-deck --run <run-id>
  --stage brief|facts|story|candidate --digest <displayed-stage-digest>
uv run pta-finance preview-treasurer-deck --run <run-id>
uv run pta-finance create-treasurer-deck --run <run-id>
uv run pta-finance update-treasurer-deck --run <run-id> [--preview-only] [--dry-run]
uv run pta-finance promote-treasurer-deck --run <run-id>
  --approval-sha256 <candidate-approval-digest>
```

In these examples, `<private-text-file>` and `<private-json>` are private local paths subject to the
regular-file/size rules in section 5.2; `<run-id>` uses the exact section 5.1 grammar;
`<displayed-digest>`, `<displayed-stage-digest>`, and `<candidate-approval-digest>` are the exact
64-character lowercase SHA-256 values printed by the immediately preceding review/check command;
and `<private-bucket-name>` in the configuration example is an operator-created generic private
Cloud Storage bucket name, never an organization identifier.

#### One-time private setup and first request

This is the complete v1 setup order a fresh operator follows; Step 23 copies it into the operator
guide with screenshots only in private evidence:

1. In the existing private Google Cloud project, enable the Drive, Slides, Sheets, and Cloud Storage
   APIs (`drive.googleapis.com`, `slides.googleapis.com`, `sheets.googleapis.com`, and
   `storage.googleapis.com`). Keep the OAuth consent screen `External/Testing`, add the Treasurer
   Google account as a test user, create one **Desktop app** OAuth client dedicated to Treasurer
   Slides, and save its downloaded JSON only at the private `client_secrets_file` path. Do not reuse
   the Gmail desktop client or token.
2. Give the existing source-reader service account Viewer access only to the source spreadsheet.
   Create one generically named private Storage bucket with public-access prevention and uniform
   bucket-level access. Grant the signer only `storage.objects.create`, `storage.objects.get`, and
   `storage.objects.delete` on that bucket; add a one-day object lifecycle deletion fallback; create
   no public ACL, website, or CDN configuration.
3. Extend the existing private `config.toml`—never replace its current `[google]` settings—with the
   `[slides]` block from section 5.6, substituting only private paths and the generic bucket name.
   Confirm `reports/output/`, `secrets/`, the token, workspace/binding manifests, and requests remain
   gitignored.
4. From the repository root run `uv sync --extra dev --extra slides`, then
   `uv run playwright install chromium`. Run `uv run pta-finance --help` before authorizing anything
   to prove the installed entry point and optional command registration.
5. Run `uv run pta-finance init-treasurer-slides`. Complete the browser OAuth flow with the test-user
   account and consent only to exact `drive.file`; the command stores the dedicated token atomically,
   creates/reconciles the generic app-owned folders and template, writes the private workspace
   manifest, and emits the private binding review/digest. Any extra scope or ambiguous pre-existing
   app-owned resource blocks setup.
6. Customize only the app-owned template using the fixed layout/object/token contract, keeping it
   private and using Noto Sans. Review the discovered 14 bindings, run
   `uv run pta-finance approve-treasurer-bindings --digest <displayed-digest>`, then run
   `uv run pta-finance check-treasurer-slides`. Continue only when exact-scope auth, template roles,
   source/template baselines, and all 14 keys report healthy.
7. Put a small real request in a private UTF-8 file and run
   `uv run pta-finance prepare-treasurer-deck --request <private-text-file> --as-of <YYYY-MM-DD>`.
   Stop at the generated private brief review; the digest-bound brief/fact/story/candidate commands
   above are the only progression path. Steps 25–26 perform the required live candidate/final runs.

Omitting `--audience` selects the fail-closed `public` policy. Approval is intentionally accept-only.
When an operator rejects a brief, fact set, story, or candidate, they change the relevant private
request/proposal/override/story-directive input and call `prepare-treasurer-deck` with
`--supersedes-run <run-id>`. The predecessor must exist under the canonical run root; the command
creates a new immutable linked run and never edits or invalidates the predecessor.

<!-- autofix-applied: 2026-08-30 -->
### Step 24: M6 — Configure the private Slides workspace and live bindings

- **Problem:** Configure real credentials, storage, template, and bindings before any live candidate
  acceptance run.
- **Type:** operator
- **Issue:** #37
- **Files:** no feature files; only status bookkeeping in this plan/issue plus gitignored private
  config/token/manifests and app-owned Google/Storage resources
- **Produces:** no tracked feature artifact; only private config/token, workspace/binding manifests,
  app-owned template/folders, bucket lifecycle/IAM, and generic pass/fail plan/issue bookkeeping
- **Done when:** the operator runs `uv sync --extra dev --extra slides` and
  `uv run playwright install chromium`; configures the generic signer/bucket with least-privilege
  object access and lifecycle fallback; runs `uv run pta-finance init-treasurer-slides`, customizes
  the app-owned template, approves the displayed binding digest, and runs
  `uv run pta-finance check-treasurer-slides`; the check records safe source/template baseline hashes
  and reports exact-scope auth plus all 14 binding keys healthy; no private value enters tracked
  content
- **Depends on:** Step 23
- **Status:** PENDING

<!-- autofix-applied: 2026-08-30 -->
### Step 25: M7 — Run the minimal real candidate smoke

- **Problem:** Exercise one short real producer-to-consumer cycle before spending time on full parity
  review.
- **Type:** operator
- **Issue:** #38
- **Files:** no feature files; only status bookkeeping in this plan/issue plus gitignored request/run/QA
  artifacts and private Google resources
- **Produces:** one private QA-passing smoke candidate and private evidence; no final deck. A failed
  candidate is retained as evidence but leaves this step PENDING/BLOCKED and does not unlock Step 26
- **Done when:** from a small private request selecting one native chart and one custom aggregate
  graphic, the operator runs the prepare/approval/preview/create flow in the command surface above;
  every command exits as expected, the same immutable facts/datasets reach both renders, local QA and
  automated candidate QA pass, actual thumbnails have no clipping/wrapping/legibility defect, source
  and frozen-template hashes equal the Step 24 baselines, and no staging Sheet/object remains; any
  failure is recorded as evidence rather than bypassed
- **Depends on:** Step 24
- **Status:** PENDING

<!-- autofix-applied: 2026-08-30 -->
### Step 26: M8 — Run full graphic/public-safety acceptance and explicit promotion

- **Problem:** Prove all capabilities, audience policy, negative authorization paths, and immutable
  final promotion against the real services.
- **Type:** operator
- **Issue:** #39
- **Files:** no feature files; only status bookkeeping in this plan/issue plus gitignored
  inputs/runs/galleries and private Google resources
- **Produces:** private parity/public candidates, one accepted private final, and generic pass/fail
  plan/issue bookkeeping; no public Drive permission
- **Done when:** one internal run accounts for all 14 keys, including structured private table input;
  a representative public run contains no PII/source link/placeholder and excludes every internal
  dataset/capability; figures match the immutable snapshot; the operator reviews every actual
  thumbnail for clipping/wrapping/legibility/density; wrong binding/stage/promotion digests and an
  internal-to-public eligibility attempt are rejected; explicit promotion creates one content-equal
  private final; sequential retry reconciles to it; source/template baseline hashes remain unchanged;
  no staging resource remains; public sharing is a separate operator action
- **Depends on:** Step 25
- **Status:** PENDING

## 8. Risks and Decided Handling

| Risk | Consequence | Mitigation / decided handling |
|---|---|---|
| Source data lacks requested bank/reserve/commitment facts | Tool could imply completeness it does not have | Typed required facts block; briefing tab/override supplies operator-owned values and definitions |
| Cash, commitments, received, pending, and projections are conflated | Misleading public narrative | Enum-backed basis, separate facts/labels, module lint, operator fact approval |
| Operator input attempts to label private data public | PII or small-group detail reaches a public-safe deck | Code-owned monotonic sensitivity ceilings; overrides can only restrict; requestor/vendor/raw/free-text lineages remain internal |
| Rough transcript cleanup discards meaning | A real question silently disappears | Source spans, explicit ignore log/reasons, brief approval, no model authority |
| Native chart IDs/ranges drift | Wrong chart or stale data | Logical matching plus semantic fingerprint; zero/multiple/drift blocks |
| Dropdown/formula charts inherit workbook UI state | Nondeterministic deck | Explicit selectors and rebuild from captured facts in an app-owned staging Sheet |
| Over-grid graphics are unavailable through Sheets metadata | Custom visuals disappear | Committed donut/Sankey renderers; structured internal table input; no production dependence on embedded PNGs |
| Hard-coded chart subtitle becomes stale | Displayed total disagrees with data | Titles/subtitles format current approved facts; fingerprint/lint rejects literals that duplicate values |
| Template placeholders/layouts drift | Broken candidate | `check-treasurer-slides`, versioned placeholder/layout contract, revision controls, readback QA |
| Service account cannot own My Drive files | Candidate creation fails | Dedicated human `drive.file` OAuth; app-created template/folders; Shared Drive deferred |
| OAuth consent/token expires | External/Testing mode requires browser re-consent roughly every seven days | Accepted hands-on operating cost; separate documented Slides token and actionable auth error; never reuse Gmail token |
| Google quota, 5xx, or timeout interrupts a mutation | Duplicate or half-recorded remote artifact | Bounded retry budget, `Retry-After`, deterministic IDs/appProperties, read-after-timeout reconciliation, resumable failure |
| Signed image URL is bearer-accessible briefly and retained as expired source metadata | Temporary aggregate image and signer-identifier exposure | Audience gate first; aggregate custom visuals only; generic signer/project/bucket/object names; ≤15-minute time to live (TTL); strip from all projections; immediate deletion and post-delete proof; accepted residual documented |
| Cleanup fails after remote write | Private staging artifact remains | `finally`, lifecycle fallback, explicit cleanup receipt; mark QA failed and block promotion |
| Playwright checks mechanics but misses narrative problems | Technically clean but misleading slide | Fact/claim provenance lint, contradiction/challenge checks, actual thumbnail gallery, operator approvals |
| Local and Google rendering differ | Text wrapping or graphics drift | Exact geometry contract, API readback, actual thumbnails, per-layout thresholds, M7–M8 visual review |
| Candidate changes after QA | Approved evidence no longer matches | Short-lived revision control for batch updates plus canonical content/Drive-version re-read before and after promotion |
| Retry or concurrency creates duplicate candidates/finals | Confusing version history | Per-run single-writer lock, manifest generation compare-and-swap, appProperties, sequential crash reconciliation; multiple remote matches block rather than guess |
| Failed candidate is edited and reused | QA evidence no longer names one immutable artifact | `QA_FAILED` is terminal for that candidate; corrections create a new linked run/version |
| Ignored runs and retained failed/final Drive files accumulate | Local or Drive storage grows across recurring use | V1 never auto-deletes diagnostic candidates/finals; staging is always deleted, while ignored runs and remote versions remain operator-retained until a separately designed prune/archive workflow |
| Private facts or IDs enter the public repo/logs | Privacy breach | Ignored run root, fictional fixtures, redacted exception boundary, status-only console, and precise private-resource guard with public-doc/fake-placeholder allowlists |

There are no blocking open choices left. Explicitly deferred follow-ons are: linked/editable charts,
multiple visual themes, generic discovery support beyond the registered graphics, insertion into a
larger human deck, a built-in LLM adapter, autonomous scheduling, PPTX conversion, Apps Script, and
service-account-only Shared Drive operation, and automatic archive/prune of retained runs/decks.

## 9. Testing Strategy

### Unit and contract tests

- Strict schema version, unknown/missing fields, enum validation, unique IDs, finite `Decimal`
  money, deterministic serialization, locking/generation compare-and-swap, atomic manifest-last
  writes, crash/corruption recovery, state transitions, and digest invalidation.
- Cleanup of malformed Unicode, metadata/filename noise, repeated headings, ambiguous prose,
  source-span preservation, optional proposal validation, and explicit ignore reasons.
- Fact precedence, conflict detection, override source-hash guard, stale snapshot detection,
  required/optional behavior, typed effective/user-entered/formula cell provenance, current
  schema-v2 reimbursement settlement/payment reduction, and financial-basis separation.
- All module fact requirements, overview toggle, deterministic ordering, claim citations, public
  guard, main-slide budgets, and appendix spill.
- Catalog identity and exactly-14 coverage, every declared `GraphicDataset` schema/calculation,
  all four per-run capability states, private binding proposal/approval, every chart family, custom
  render determinism, structured table input, selector requirements, semantic-versus-cosmetic
  fingerprint drift, theme roles, alt text, monotonic sensitivity, and audience exclusions.

### Google boundary tests

- Exact-equality OAuth scope tests reject missing or extra scopes and prove the Gmail token path is
  never loaded.
- Source-Sheet and Storage tests require separate credential objects with exact respective scopes;
  bucket-limited Storage fakes reject a reused Sheet-reader credential or an unneeded operation.
- Fake Sheets reader exposes only GET behavior; fake source/template objects explode on update.
- Fake Drive/Slides/Sheets staging services prove template copy, request ordering, short-lived
  revision control, unlinked image readback with no staging ID/URL, image-byte hash binding,
  text/table editability, failure marking, no permissions calls, 429/5xx/timeout retry budgets,
  read-after-ambiguous-mutation reconciliation, and bounded sequential idempotency.
- Fake Storage proves randomized keys, TTL bound, no URL in logs/bundle, deletion in `finally`,
  failure handling, and post-delete inaccessibility receipt.
- Thumbnail tests prove URLs are downloaded immediately but never persisted.
- Canary tests inject private facts, file IDs, account identities, signed URLs, thumbnail URLs, and
  raw client errors. They prove required IDs/revisions persist only in designated gitignored
  manifest fields, while the redacted exception/allowlisted projection boundary exposes no private
  value through committed artifacts, output, errors, or non-allowlisted fields.

### Visual and smoke tests

- Real Playwright/Chromium against fictional storyboards at 1600×900, with all HTTP(S) requests
  aborted, expected fonts positively verified, and one deliberately broken fixture per overflow,
  bounds, overlap, fallback font, alignment, density, image, and placeholder rule.
- A real-component local smoke path using actual contracts, sources over fictional in-memory data,
  module assembly, all graphic renderers, storyboard, and QA—mocking only the external Google APIs.
- Full repository `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, and
  `uv run mypy --strict pta_finance` gates; Ruff is the Python linter/formatter and mypy is the static
  type checker. Development entry-point smoke is
  `uv run pta-finance --help`.
- CI runs `uv sync --extra dev --extra slides` and
  `uv run playwright install --with-deps chromium`, then the same network-free local visual checks
  without Google credentials; a base-install subprocess still imports existing CLIs.
- `uv build`, install the built wheel into an isolated environment, and load every Treasurer Jinja
  template, the Noto Sans WOFF2/OFL files, and the `pta-finance` entry point from that wheel.
- Explicit regression run for all reimbursement, report, Sheet, config, CLI, and existing smoke
  tests so this additive feature cannot alter their behavior.
- Repository scan proving no real identity, request text, financial value, private Google resource
  ID/URL, credential, signed URL, thumbnail URL, or candidate metadata is tracked; public
  documentation URLs and explicit fake placeholders are allowlisted.

### Operator acceptance

Steps 24–26 (M6–M8) are the mandatory real producer-to-consumer gate. Unit/fake tests cannot prove
OAuth setup, template fidelity, Google chart rendering, thumbnail behavior, or short-lived asset
cleanup against configured accounts. The operator records only generic pass/fail status in the plan
and issue; every screenshot, ID, hash, value, and detailed QA artifact remains private.

## 10. Operator Workflow After V1

```text
uv run pta-finance prepare-treasurer-deck
  -> review.html: approve brief
  -> review the same immutable fact snapshot: approve facts
  -> uv run pta-finance preview-treasurer-deck: inspect local storyboard, approve story
  -> uv run pta-finance create-treasurer-deck: private candidate + automated QA
  -> inspect actual thumbnail gallery, approve candidate
  -> uv run pta-finance promote-treasurer-deck: one immutable-by-workflow final
```

A rejected checkpoint starts a new linked run with `--supersedes-run`; no review artifact is edited
in place. `update-treasurer-deck` is the resumable convenience command; the operator guide includes
the exact copyable invocations from Step 22.

The Step 23 proposal lets the local observatory mirror this with separate setup, intake, preview,
candidate, and promotion launchers after a separately reviewed external-repository merge. The
candidate launcher specification never invokes promotion.

---

## Next Step

After review, wrap, and issue synchronization populate the step Issue fields, build the automated
phase with:

```text
/build-phase --plan documentation/treasurer-slides-plan.md
```

Stop after Step 23. Steps 24–26 are mandatory operator gates for real private Google resources and
are not part of the automated build-phase goal.
