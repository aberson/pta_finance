# pta_finance — Plan (v1)

> **Identity rule (load-bearing).** This is a **generic, reusable finance toolkit** for a
> PTA / booster club / small nonprofit. **No organization, school, person, or email may be
> hard-coded anywhere in this repository** — code, docs, tests, fixtures, or examples. All
> identity (org name, school name, school email, board emails, spreadsheet/Drive IDs,
> fiscal-year setting, grade labels, category lists) lives **only** in a private, gitignored
> config file. Tests and examples use obviously-fake placeholders (`Example PTA`,
> `treasurer@example.org`). A CI guard fails the build if a service-account key or an
> identity string is staged. The repo is public; the data is private.

## 1. What This Is

A command-line Python toolkit that treats a **Google Sheet as the system-of-record database**
for a small organization's finances. It normalizes a messy multi-year ledger into a clean
schema, runs an analytics engine over it (spend by category, spend by grade, budget-vs-actual,
multi-year fundraising/spend trends), and generates **pure-template monthly reports** in two
variants — an **internal** version (full detail) and an **external** public-safe version.

v1 is deliberately small: **no web UI, no LLM, no Google Apps Script.** It is a local Python
toolkit a technically-comfortable operator runs from a terminal, plus a GitHub Actions monthly
cron that runs the report unattended. The design is chosen so that the operational core never
depends on a server anyone must pay for or keep alive — everything recurring runs in a cloud
(GitHub Actions now; Google Apps Script in Phase 2) — which is what lets a non-technical
successor operate it later with only Chrome.

**Primary users (v1):** the treasurer / CFO (the "admins"), running the CLI and reading reports.
**Data access:** a Google **service account** the Sheet and a Drive folder are shared with; its
JSON key is the only secret.

See **§ Roadmap** for Phases 2–4 (Apps Script automation, admin web UI, wiki/forecasting/ingestion).

## 2. Stack

| Layer | Tool | Why |
|---|---|---|
| Language / runtime | Python `>=3.12` | Maintained runtime baseline; `tomllib` in stdlib (no TOML dep) |
| Dependency / build | `uv` + `hatchling` | Reproducible dependency resolution and a small standards-based build backend |
| Sheets/Drive access | `gspread` 6.x + `google-auth` | Clean service-account API (`service_account()`, `batch_update()`, `get_all_records()`); atomic batch writes. Low-level `google-api-python-client` deferred to Phase 2 (formatting/web) |
| Data / analytics | `pandas` | C-optimized `groupby` + `pd.Grouper(freq="MS")` for by-category/grade/month aggregation; fine for PTA volumes |
| Charts | `matplotlib` (Agg backend) | Deterministic, headless, zero-browser rendering in CI. **Not** Plotly/Kaleido (needs Chrome) |
| Templating | `Jinja2` | Two report variants from two templates; HTML autoescape for payee/memo fields |
| PDF (optional extra `[pdf]`) | `WeasyPrint` | HTML→PDF. **Optional** because Pango/Cairo native deps are heavy on Windows; the primary output is HTML |
| CLI | stdlib `argparse` | No unjustified dependency for subcommands |
| Config | stdlib `tomllib` (read) | Private `config.toml`; no parser dependency |
| Scheduler (unattended) | GitHub Actions cron | Free, cloud-hosted; `0 9 1 * *` monthly + `workflow_dispatch` |
| Lint / type / test | `ruff`, `mypy --strict`, `pytest` | Workspace standard, identical config to siblings |

**Core deps:** `gspread`, `google-auth`, `pandas`, `matplotlib`, `jinja2`.
**Optional extras:** `[pdf]` → `weasyprint`; `[dev]` → `pytest`, `ruff`, `mypy`.

## 3. Data Store

The database is **one Google Spreadsheet** (the `spreadsheet_id` from config), with one worksheet
("tab") per entity. The Python toolkit is the only writer in v1; humans may also edit the Sheet
directly (it's a spreadsheet), so the toolkit is **idempotent** and **never reassigns existing IDs**.

### Tabs (worksheets)

| Tab | Purpose | Key |
|---|---|---|
| `transactions` | The ledger — every income/expense line | `id` = `TXN-FY{yy}-{seq}` |
| `receipts` | Receipt records, each linked to a transaction (Drive URL) | `id` = `RCP-FY{yy}-{seq}` |
| `budget` | Budgeted amount per category (optionally per grade) per fiscal year | `id` = `BUD-FY{yy}-{slug}` |
| `events` | Calendar events (fundraisers/meetings) — **defined now, used in Phase 2** | `id` = `EVT-FY{yy}-{slug}` |
| `report_log` | One row per generated report run (timestamp, variant, output links) | append-only |

Column definitions are the **single source of truth** in `pta_finance/schema.py` (see § Modules
and § Appendix). Both the writer (ETL) and readers (analytics, reports) import the same column
lists; regression tests assert column-list identity with `is`, not `==`, so future re-duplication
fails CI (workspace `code-quality` rule: one source of truth for data-shape constants).

**Live-required subset.** The LIVE toolkit provisions/validates only `schema.REQUIRED_TABS`
(currently just `report_log`) via `check` / `init-sheet` / `snapshot`. `report` / `analyze`
source from the operator-maintained **Budget Timeseries** tab (a tidy long dataset, read via
`report_source.py`), and `report` appends one row per run to `report_log`. The full `schema.TABS`
registry above remains the column-shape source of truth (used by `report_source`'s canonical-shape
projection), but the `transactions` / `receipts` / `budget` / `events` tabs — filled by the legacy
`normalize` / `import-budget` commands — are optional and may be deleted from the spreadsheet.

### Identifiers

IDs are human-readable, fiscal-year-scoped, and **stable**. Defined once in `pta_finance/ids.py`:

- `TXN-FY{yy}-{seq:04d}` — e.g. `TXN-FY26-0001`. `yy` = last two digits of the transaction's
  fiscal-year label; `seq` is a zero-padded per-fiscal-year sequence.
- `RCP-FY{yy}-{seq:04d}`, `EVT-FY{yy}-{slug}`.
- `BUD-FY{yy}-{category-slug}` (grade-specific: `BUD-FY26-supplies-g3`).
- **Fiscal-year label** = `fiscal_year_label(date, start_month)`. For `start_month == 1`
  (calendar year, this deployment) the label is `date.year`. For a non-January start month the
  span is labeled by its **ending** calendar year (configurable convention). The `FY{yy}` token
  uses the last two digits of that label.

Python assigns an ID to any row missing one on each `normalize` run; it **never** rewrites an
existing ID (receipts/budget reference transaction IDs — changing a key shape silently breaks
consumers; workspace `code-quality` rule: grep all downstream before changing a key).

### Deduplication

Transaction natural key = `sha1(f"{iso_date}|{amount_cents}|{normalized_payee}")`. On import /
normalize, an exact duplicate (same hash) is **flagged**, not silently dropped or double-inserted.
Ambiguous legacy rows get a `needs_review` flag column rather than being discarded.

### Corruption protection

1. **Snapshot before every write:** `snapshot` exports each tab through the spreadsheet-safe CSV
   boundary under `snapshots/<utc>/` before any mutating run. Every safe CSV has an adjacent
   versioned `.raw.json` tagged `userEnteredValue` grid that distinguishes formulas from identical
   literal text and preserves native scalar types; formatting and comments are outside these
   artifacts. Snapshot directories are claimed atomically and timestamp collisions use suffixes.
2. **Atomic writes:** all writes go through `gspread` `batch_update` — all-or-nothing; a failed
   subrequest rolls back the whole batch.
3. **Sheets version history** is the automatic primary recovery path.
4. **Rate-limit safety:** writes batch 10–50 rows per request and retry on HTTP 429 with
   exponential backoff + jitter (project quota: 300 req/min; per-user: 60 req/min).
5. **Restore:** roll back via Sheets version history. The safe CSV is for inspection/import
   convenience; the adjacent `.raw.json` is the exact entered-value/formula recovery source.
   Recovery from JSON is currently manual; there is no automated `restore` command.

Writes **target specific rows/ranges by ID**, never a full-tab overwrite — this bounds the
blast radius of a write and reduces the chance of clobbering a concurrent human edit.

## 4. Domain Model

### Entities (dataclasses in `pta_finance/models.py`)

- **Transaction** — `id`, `date`, `fiscal_year`, `type` (`income`|`expense`), `amount`,
  `category`, `grade` (optional), `payee`, `memo`, `budget_id?`, `receipt_id?`,
  `source` (`manual`|`import`|`legacy`), `entered_by?`, `created_at`.
- **Receipt** — `id`, `txn_id` (FK), `drive_url`, `description?`, `amount?`, `date?`,
  `added_by?`, `created_at`. (v1 stores Drive URLs only; ingestion is Phase 4.)
- **BudgetLine** — `id`, `fiscal_year`, `category`, `grade?`, `budgeted_amount`, `notes?`.
- **Event** — `id`, `fiscal_year`, `name`, `date`, `type`, `expected_income?`,
  `expected_expense?`, `nag_schedule?`, `notes?`. (Phase 2.)

`grade` is an **optional** dimension (some spend is school-wide). Grade labels come from config —
the toolkit never hard-codes a grade range.

### Analytics (`pta_finance/analytics/`)

- **Aggregations** (`aggregate.py`): totals; income vs expense; by category; by grade; by month
  (`pd.Grouper(freq="MS")`); budget-vs-actual variance per category/grade.
- **Trends** (`trends.py`): multi-year series for fundraising income and spend; year-over-year
  comparison. (Forecasting — one year ahead — is **Phase 4**; the trend series is the input it
  will consume.)

### Reports (`pta_finance/reports/`)

Pure-template (no LLM). The **report data model** is computed once by `builder.py` from the
analytics layer, then rendered by `render.py` into two variants from two Jinja2 templates:

- **Internal** (`internal.html.j2`): full ledger detail, payee/vendor names, receipt
  links, per-line budget variance.
- **External** (`external.html.j2`): public-safe — income-vs-expense totals, by-grade
  allocation, fundraising progress, budget headline numbers, **no payee names, no receipt links,
  no member PII**.

Templates are authored in HTML (Jinja2 autoescape on); outputs are **HTML** always and **PDF**
optionally (the `[pdf]` extra runs WeasyPrint over the rendered HTML). A Markdown/plain-text
variant is a Phase-2 nicety, not v1.

> **Field split resolved at build Step 6:** the internal model includes identity, totals,
> by-category variance, by-grade allocation, fundraising progress, budget headlines, and full
> transaction detail. The external model includes only organization identity plus aggregate totals,
> by-grade allocation, fundraising progress, and budget headlines. A recursive runtime guard rejects
> payee, memo, receipt, entered-by, and other person-name fields from the external model.

Charts are matplotlib (Agg) PNGs embedded in the HTML. **Reports are never committed to the public
repo.** They are written to `reports/output/` (gitignored) locally and (in CI) attached as an
ephemeral workflow artifact. Both variants in that artifact are downloadable by signed-in readers
of this public repo; it is not private storage. A row is appended to `report_log`. **Live upload to a
private Drive folder is deferred to Phase 2** — it needs `google-api-python-client`, which §8 defers
to Phase 2; v1 ships local output + the CI artifact, and `report_log.output_url` records the local
path.

## 5. Modules

`pta_finance/` (flat package, mirroring `switchboard/`):

- **`config.py`** — load + validate the private `config.toml` (stdlib `tomllib`); resolve the
  service-account key path; expose typed config objects. Fails fast with a clear error if a
  required field is missing. Never logs secret values.
- **`ids.py`** — ID grammar + `fiscal_year_label()`. Single source of truth for ID formats.
- **`schema.py`** — canonical tab names + ordered column lists per tab. Single source of truth
  for data shape; importable `is`-comparable constants.
- **`models.py`** — entity dataclasses + (de)serialization to/from row dicts.
- **`sheets.py`** — `gspread` service-account client wrapper: open spreadsheet, read a tab to
  records, atomic `batch_update` writes with 429 backoff + jitter, schema validation. The only
  module that talks to Google.
- **`backup.py`** — spreadsheet-safe CSV plus exact tagged entered-value JSON snapshots (corruption
  protection); defaults to the live tab set (`report_log` + Budget Timeseries, skipping any absent
  tab), with `tabs=` for legacy callers.
- **`etl.py`** — normalize legacy/raw rows → canonical schema; assign missing IDs; dedup;
  `needs_review` flagging; snapshot-before-write.
- **`budget_import.py` / `budget_sync.py`** — legacy budget import plus dry-run-first reconciliation
  from an editable `FY<fy> Budget` tab back into Budget Timeseries.
- **`report_source.py`** — project Budget Timeseries rows into the canonical shapes consumed by
  `analyze` and `report`.
- **`receipt_ingest.py` / `receipt_map.py`** — parse `.eml`/`.mbox` form submissions and map them
  into the flat, machine-owned Reimbursements ledger.
- **`gmail_source.py`** — read-only Gmail OAuth, bounded query/list/fetch, and deterministic
  idempotent `.eml` acquisition shared by `fetch-mail` and the reimbursement refresh command.
- **`reimbursement_events.py` / `reimbursement_pipeline.py` / `reimbursement_report.py`** — strict
  private anchor/review configuration, stable-keyed original and supplemental evidence, schema-v2
  validation with explicit v1 migration, scoped lifecycle reduction, deterministic email
  composition, and atomic Jinja rendering for the reimbursement review queue.
- **`analytics/`** — `aggregate.py`, `trends.py` (pandas).
- **`reports/`** — `builder.py` (compute report data model), `render.py` (Jinja2 → HTML, optional
  WeasyPrint PDF), `charts.py` (matplotlib Agg), `templates/` (`internal.html.j2`, `external.html.j2`).
- **`cli.py`** — `argparse` entry point (`main`) exposing the subcommands below; wired as the
  `pta-finance` console script.

### CLI subcommands

| Command | Action |
|---|---|
| `pta-finance check` | Validate `report_log` schema + Budget Timeseries source readability; real round-trip read/write/delete of a test row in `report_log` (smoke) |
| `pta-finance init-sheet [--target production\|test]` | Create the live-required tab and headers in the selected spreadsheet |
| `pta-finance snapshot` | Export safe CSV + exact tagged entered-value JSON backups of the live tab set (`report_log` + Budget Timeseries; skips absent tabs) |
| `pta-finance normalize` | (legacy) Normalize legacy/raw ledger → canonical schema, assign IDs, dedup (snapshot first) |
| `pta-finance analyze [--fy YYYY]` | Run analytics over the Budget Timeseries tab; print summary |
| `pta-finance report [--fy YYYY] [--variant internal\|external\|both]` | Generate fiscal-year report(s) from the Budget Timeseries tab (default: current FY) |
| `pta-finance import-budget` | (legacy) Load a budget worksheet into the canonical budget tab |
| `pta-finance sync-budget --fy YYYY [--apply]` | Preview or apply editable-budget changes to Budget Timeseries; snapshots before writes |
| `pta-finance ingest-receipts --source <path> [--profile]` | Parse or profile reimbursement-form `.eml`/`.mbox` exports |
| `pta-finance map-receipts --source <path> [--write-tab Reimbursements]` | Map submissions to the flat ledger and optionally replace its machine-owned Sheet tab |
| `pta-finance fetch-mail --since YYYY-MM-DD` | Acquire a bounded Gmail window into the local `.eml` archive; no Sheet write |
| `pta-finance report-reimbursements` | Validate the private structured review bundle and atomically render its HTML offline |
| `pta-finance update-reimbursements [--fetch-since YYYY-MM-DD]` | Optionally acquire mail, refresh stable-keyed local evidence, then render; never sends mail or writes Sheets |

## 6. API Route Contract

**Not applicable in v1** — there is no backend HTTP API. (A web app + endpoints arrive in Phase 3.)

## 7. Project Structure

```
pta_finance/                      # repo root (standalone public repo)
├── plan.md                       # this document
├── CLAUDE.md                     # project context for future sessions (generic)
├── README.md                     # generic toolkit readme
├── SETUP.md                      # one-time Google + local setup guide
├── docs/                         # operator guides + diagrams/, screenshots/, examples/ (fictional)
├── documentation/                # public-safe planning/playbook documents
├── pyproject.toml                # uv + hatchling + ruff + mypy(strict) + pytest
├── config.example.toml           # committed template with FAKE placeholders
├── .gitignore                    # ignores config.toml, secrets/, *.json keys, .env, caches
├── .github/
│   ├── last-run.txt              # keepalive timestamp (resets the 60-day scheduler timer)
│   └── workflows/
│       ├── ci.yml                # lint + type + test on PR
│       └── monthly-report.yml    # cron 0 9 1 * * + workflow_dispatch
├── pta_finance/                  # the package (flat layout)
│   ├── __init__.py
│   ├── config.py
│   ├── ids.py
│   ├── schema.py
│   ├── models.py
│   ├── sheets.py
│   ├── backup.py
│   ├── budget_import.py
│   ├── budget_sync.py
│   ├── etl.py
│   ├── cli.py
│   ├── gmail_source.py
│   ├── receipt_ingest.py
│   ├── receipt_map.py
│   ├── reimbursement_events.py
│   ├── reimbursement_pipeline.py
│   ├── reimbursement_report.py
│   ├── report_source.py
│   ├── analytics/
│   │   ├── __init__.py
│   │   ├── aggregate.py
│   │   └── trends.py
│   └── reports/
│       ├── __init__.py
│       ├── builder.py
│       ├── render.py
│       ├── charts.py
│       └── templates/
│           ├── internal.html.j2
│           ├── external.html.j2
│           └── reimbursement_queue.html.j2
├── scripts/
│   ├── capture_readme.py         # real reimbursement renderer + fictional fixtures → screenshots
│   ├── export_example_snapshot.ps1 # desktop PowerPoint → fictional snapshot PNG
│   └── check_no_identity.py      # CI guard: blocks staged credentials / identity strings
├── tests/
│   ├── conftest.py               # fake-org fixtures + mocked gspread client
│   ├── test_ids.py
│   ├── test_config.py            # config validation fails fast on missing fields
│   ├── test_schema.py            # asserts column-list identity with `is`
│   ├── test_models.py
│   ├── test_sheets.py
│   ├── test_backup.py
│   ├── test_budget_import.py
│   ├── test_budget_sync.py
│   ├── test_etl.py
│   ├── test_analytics.py
│   ├── test_receipt_ingest.py
│   ├── test_receipt_map.py
│   ├── test_reimbursement_cli.py
│   ├── test_reimbursement_pipeline.py
│   ├── test_reimbursement_report.py
│   ├── test_report_source.py
│   ├── test_reports.py
│   ├── test_cli.py
│   ├── test_workflows.py         # static safety guards on monthly-report.yml
│   └── test_smoke_pipeline.py    # end-to-end wiring gate (mock sheet)
├── secrets/                      # gitignored; holds service-account.json locally
└── snapshots/                    # gitignored; safe CSV + exact tagged entered-value JSON backups
```

## 8. Key Design Decisions

- **Sheet-as-DB + service account.** Zero-server, transparent, and survives a non-technical
  handoff; a service account gives unattended CI access to a private Sheet without a human login.
- **gspread over google-api-python-client (v1).** Clean `service_account()` / `batch_update()` API
  and atomic batches; the lower-level library is reserved for Phase 2 (cell formatting, sharing
  management, the web app).
- **One source of truth for schema + IDs.** `schema.py` and `ids.py` are imported by every
  producer and consumer; identity-asserting tests prevent silent re-duplication and key drift.
- **Stable, human-readable, fiscal-year-scoped IDs;** Python assigns missing IDs and never
  rewrites existing ones.
- **Pure-template reports, deterministic charts.** matplotlib Agg + Jinja2; PDF is an optional
  extra so the core toolkit installs without Pango/Cairo native deps on Windows.
- **Real reports never enter Git.** Local reports stay in gitignored `reports/output/`.
  The monthly workflow uploads both variants as Actions artifacts accessible to signed-in readers
  of the public repo. Private Drive upload remains deferred; README examples contain fictional data.
- **Config-driven identity, fiscal year, and grades.** Makes the toolkit generic and reusable;
  this deployment binds calendar-year fiscal periods via config.
- **Snapshot-before-write + atomic batch + Sheets history** for corruption protection;
  the toolkit is idempotent so re-runs are safe.
- **Secrets posture.** Service-account JSON is gitignored locally and stored as a **base64** GitHub
  Actions secret, decoded to a file at runtime **without echoing**; a CI guard blocks staged keys
  or identity strings.

## 9. Open Questions / Risks

| Item | Risk | Mitigation |
|---|---|---|
| Future report-model changes | A new field could leak PII or omit needed detail | Step 6 pinned both field lists; the external builder recursively rejects payee/receipt/member fields at runtime |
| Legacy sheet structure unknown until inspected | ETL mis-maps messy multi-year data | M1 shares the legacy sheet; ETL is inspection-driven and flags ambiguous rows `needs_review` (never silently drops) |
| WeasyPrint native deps on Windows | PDF generation friction blocks the operator | PDF is an optional `[pdf]` extra; the primary output is HTML |
| Sheets API quota (300/min project, 60/min user) on large legacy import | HTTP 429 mid-import | Batch 10–50 rows; exponential backoff + jitter; atomic batches |
| Accidental secret/identity leak in a **public** repo | Credentials or org identity exposed | `.gitignore` SA key + `config.toml`; `config.example.toml` only; CI guard greps staged diff for `*.json` creds + identity patterns; tests use fake org |
| Scheduled workflow auto-disables after 60 days of no **repo activity** (public repo) | Monthly report silently stops — the cron firing does **not** count as activity | The workflow pushes a `.github/last-run.txt` timestamp each run (a liveness marker, not a report) so repo activity resets the 60-day timer; CLAUDE.md documents the one-click re-enable in the Actions tab |
| Concurrent human edit during a tool read→modify→write | Lost update — the tool overwrites a row a human just changed | Writes target specific rows/ranges by ID (not full-tab overwrite); snapshot-before-write preserves prior state; v1 contention is low (single treasurer, monthly cadence). A concurrent-modification check is a Phase-2 add |
| `gspread` v6 reordered `update()` args | Silent wrong-cell writes | Always call with named args (`range_name=`, `values=`) |
| Nested git repo (project lives inside the `dev` workspace tree) | Confusion publishing the standalone public repo | Resolved at `/repo-init`: pta_finance is its own repo; keep the dev workspace's tracking out of it |
| Forecasting deferred | Stakeholders expect it in v1 | Explicitly Phase 4; v1 ships the trend series it will consume |

## 10. How to Run

```bash
# 1. Install (from repo root)
uv sync --extra dev

# 2. Configure (private, gitignored)
cp config.example.toml config.toml
#    fill in: org/school name + email, board emails, spreadsheet_id,
#    drive receipts folder id, grade labels, fiscal_year.start_month (1 = Jan)

# 3. Google setup (one-time, operator — see Step M1)
#    create a service account, download its JSON to secrets/service-account.json,
#    share the spreadsheet + Drive folder with the service-account email (Editor)

# 4. Smoke-check the wiring
uv run pta-finance check

# 5. Analyze + report from the operator-maintained Budget Timeseries tab
uv run pta-finance analyze
uv run pta-finance report --fy 2027 --variant both
# normalize is a legacy write command; it is not part of the current reporting setup.
```

```bash
# CI (unattended monthly report)
# In the GitHub repo settings, add secrets:
#   GOOGLE_SA_KEY_B64  = base64 of the service-account JSON
#   PTA_CONFIG_B64     = base64 of config.toml (identity stays out of the repo)
# The monthly-report.yml workflow runs 0 9 1 * * and on workflow_dispatch.
```

## Roadmap (post-v1)

| Phase | Adds | Notes |
|---|---|---|
| **2 — Apps Script cloud layer** | Nag emails + calendar reminders (time-driven triggers), Chrome-editable config (Sheet tab / Script Properties), Google Sign-In allowlist plumbing; flexible/config-driven charts (Vega-Lite) | All recurring compute stays in a cloud (Google) — the handoff-safety layer |
| **3 — Admin web UI** | React (or Apps Script HtmlService) admin surface; Google Sign-In gated to the config allowlist | Front-end deferred so the schema settles first |
| **4 — Power features** | One-year-ahead forecasting; Gmail-OAuth receipt automation + linked-file fetch; bank-CSV / QuickBooks import; LLM report narrative + people-friendly wiki rendering; board-ramp wiki (LLM-friendly + people-friendly) | Credential-free receipt parsing/mapping and Gmail **fetching** (`fetch-mail`) are shipped; unattended/cron ingestion and Drive fetch remain |

## 11. Development Process

Build with `/build-phase` walking `/build-step` per step. Default flags: `--reviewers code`
(backend/library/JSON/YAML — no runtime UI surface in v1), `--isolation worktree`. The plan mixes
`code` and `operator` steps, so Build Steps split into **Automated** (walked unattended) and
**Manual** (operator-driven, after the automated run).

Pipeline ordering for the data flow (Sheet → ETL → analytics → report) includes a **code-level
smoke gate** (Step 7) before the operator **observation run** (M3), per the workspace plan-init
quality bar for producer→consumer pipelines and scheduled jobs.

### Automated Steps
*(These run unattended via `/build-phase`.)*

### Step 1: Scaffold + tooling + config + IDs
- **Problem:** Create the `uv`/hatchling project (pyproject with ruff, mypy-strict, pytest mirroring `switchboard`), flat `pta_finance/` package skeleton, `.gitignore` (config.toml, secrets/, *.json, .env, caches), `config.example.toml` with FAKE placeholders, generic `README.md`, `config.py` (load+validate TOML, resolve SA key path), and `ids.py` (ID grammar + `fiscal_year_label`) with tests. Add a CI guard script that fails if a `*.json` credential or an identity pattern is staged.
- **Type:** code
- **Issue:** #1
- **Flags:** --reviewers code --isolation worktree
- **Produces:** project skeleton, `pta_finance/config.py`, `pta_finance/ids.py`, `tests/test_ids.py`, `tests/test_config.py`, `config.example.toml`, `.gitignore`, `.github/workflows/ci.yml`
- **Done when:** `uv run pytest -q`, `uv run ruff check .`, `uv run mypy --strict pta_finance` all pass; ID tests assert exact formats; `config.py` fails fast on a missing required field (tested)
- **Depends on:** none
- **Status:** DONE (2026-06-23)

### Step 2: Sheet schema + entity models
- **Problem:** Define `schema.py` (canonical tab names + ordered column lists as single-source-of-truth constants) and `models.py` (entity dataclasses + row (de)serialization). Add tests asserting column-list **identity** with `is` (not `==`).
- **Type:** code
- **Issue:** #2
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `pta_finance/schema.py`, `pta_finance/models.py`, `tests/test_schema.py`
- **Done when:** tests pass incl. an `is`-identity assertion on a shared column list; mypy strict clean
- **Depends on:** 1
- **Status:** DONE (2026-06-23)

### Step 3: Sheets client + backup
- **Problem:** Implement `sheets.py` (`gspread` service-account wrapper: open spreadsheet, read tab→records, atomic `batch_update` with 429 exponential-backoff+jitter, schema validation, named `update()` args) and `backup.py` (CSV snapshot of all tabs). Mock `gspread` in tests; include an integration test that exercises the production write path and asserts the batch+backoff code is reached (workspace `code-quality` rule: integration test through the production caller).
- **Type:** code
- **Issue:** #3
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `pta_finance/sheets.py`, `pta_finance/backup.py`, `tests/` additions
- **Done when:** unit + integration tests pass against a mocked client; mypy strict clean (add `[[tool.mypy.overrides]]` for `gspread`/`google.*` if untyped)
- **Depends on:** 2
- **Status:** DONE (2026-06-23)

### Step 4: ETL / normalize
- **Problem:** Implement `etl.py` — normalize legacy/raw rows to canonical schema, assign missing IDs via `ids.py`, dedup via `(date|amount|payee)` hash, flag ambiguous rows `needs_review`, snapshot-before-write. Rows with an unparseable date/amount are flagged `needs_review` and skipped — a single bad legacy row must never crash the whole run. Wire the `normalize` CLI subcommand. Integration test: legacy fixture → normalized round trip (assert IDs assigned, dups flagged, existing IDs untouched).
- **Type:** code
- **Issue:** #4
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `pta_finance/etl.py`, `normalize` in `cli.py`, `tests/test_etl.py`
- **Done when:** round-trip test passes; re-running normalize is idempotent (no dup IDs, no reassigned IDs); a malformed-legacy-row fixture is flagged `needs_review`, not fatal
- **Depends on:** 3
- **Status:** DONE (2026-06-23)

### Step 5: Analytics engine
- **Problem:** Implement `analytics/aggregate.py` (totals, income/expense, by category, by grade, by month via `pd.Grouper`, budget-vs-actual variance) and `analytics/trends.py` (multi-year fundraising/spend series, YoY). Wire the `analyze` CLI subcommand. Fixture-based numeric assertions.
- **Type:** code
- **Issue:** #5
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `pta_finance/analytics/`, `analyze` in `cli.py`, `tests/test_analytics.py`
- **Done when:** known-fixture → expected-number assertions pass; mypy strict clean
- **Depends on:** 4
- **Status:** DONE (2026-06-23)

### Step 6: Report generation (internal + external)
- **Problem:** Implement `reports/builder.py` (compute the report data model from analytics), `reports/charts.py` (matplotlib Agg PNGs), `reports/render.py` (Jinja2 → HTML with autoescape on payee/memo; optional WeasyPrint PDF behind the `[pdf]` extra), and `templates/internal.html.j2` + `templates/external.html.j2`. **Pin the exact internal vs external field lists here.** The external variant must exclude payee names, receipt links, and member PII — enforce this as a **runtime invariant**, not just a test: the external builder raises a stable `ExternalReportPIIError` if any payee/receipt/PII field appears in the external data model. A public-facing safety control must be a runtime guard, not documentation alone. Wire the `report` CLI subcommand; append to `report_log`; write to `reports/output/` + (configured) private Drive folder — never to the repo.
- **Type:** code
- **Issue:** #6
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `pta_finance/reports/`, `report` in `cli.py`, `tests/test_reports.py`
- **Done when:** both variants render from a fixture without error; a unit test AND the runtime `ExternalReportPIIError` guard both reject an external data model containing payee/receipt/PII fields; mypy strict clean
- **Depends on:** 5
- **Status:** DONE (2026-06-23) — Drive upload deferred to Phase 2 (needs google-api-python-client per §8); v1 = local output + CI artifact

### Step 7: End-to-end smoke gate (code)
- **Problem:** Add `tests/test_smoke_pipeline.py` — a 60-second end-to-end wiring test with REAL components (config → schema → etl → analytics → reports) against an in-memory / mocked Sheet, asserting the full pipeline completes once without exception and the rendered report contains the expected sections. No business-logic assertions — this is a producer/consumer drift gate, distinct from the M3 observation run.
- **Type:** code
- **Issue:** #7
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `tests/test_smoke_pipeline.py`
- **Done when:** the smoke test passes in CI with no live Google calls
- **Depends on:** 6
- **Status:** DONE (2026-06-23)

### Step 8: GitHub Actions monthly report workflow
- **Problem:** Add `.github/workflows/monthly-report.yml` — `schedule: cron "0 9 1 * *"` + `workflow_dispatch`; `actions/checkout@v4`; `astral-sh/setup-uv` (cache on); restore `GOOGLE_SA_KEY_B64` + `PTA_CONFIG_B64` secrets by base64-decoding to files **without echoing**; `uv run pta-finance report --variant both` writes local reports and appends `report_log` rows; the workflow uploads `reports/output/` as an ephemeral artifact accessible to signed-in readers of this public repo; **never commit real reports to the repo**. Private Drive upload remains deferred. Also append a UTC timestamp to a tracked `.github/last-run.txt` and push it (keepalive so the public-repo scheduler isn't auto-disabled after 60 days; this liveness marker is not a report). Confirm `ci.yml` (lint/type/test on PR) from Step 1 is green.
- **Type:** code
- **Issue:** #8
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `.github/workflows/monthly-report.yml`, `.github/last-run.txt`
- **Done when:** `actionlint` (or a YAML lint) passes; a test asserts the workflow restores secrets via file redirect with no `run: echo`/`cat` of a secret variable and that it invokes `pta-finance report`; the real credentialed end-to-end run is deferred to M3
- **Depends on:** 7
- **Status:** DONE (2026-06-23)

### Manual Steps
*(These run after `/build-phase` completes. Operator drives.)*

### Step M1: Google Cloud + service-account setup
- **Type:** operator
- **Source step:** prerequisite for M2/M3
- **Issue:** N/A (operator step)
- **Commands:**
  ```text
  In Google Cloud Console (browser):
  1. Create / select a project; enable the Google Sheets API and Google Drive API.
  2. APIs & Services > Credentials > Create credentials > Service account.
  3. On the service account > Keys > Add key > Create new key > JSON > download.
  4. Save the JSON to:  secrets/service-account.json   (gitignored)
  5. Copy the service account's client_email from the JSON.
  6. In Google Drive, Share the spreadsheet AND the receipts Drive folder
     AND a throwaway TEST spreadsheet with that email, role = Editor.
  7. Put the test spreadsheet id in config.toml for the M2 smoke check.
  ```
- **What to look for:**
  | Check | Expected outcome |
  |---|---|
  | Sheets API + Drive API status | Both show "Enabled" |
  | Downloaded key | `secrets/service-account.json` exists; `git status` does NOT list it |
  | Sharing | Spreadsheet, Drive folder, and test sheet each list the SA email as Editor |
- **Status:** DONE (2026-06-24)

### Step M2: Real-sheet smoke (round-trip)
- **Type:** operator
- **Source step:** Step 7 (real-credentials variant)
- **Issue:** N/A (operator step)
- **Commands:**
  ```powershell
  uv run pta-finance check
  ```
- **What to look for:**
  | Check | Expected outcome |
  |---|---|
  | Config + schema validation | Passes; reports the resolved org from config (not hard-coded) |
  | Round-trip | Writes a test row to the test sheet, reads it back, deletes it — no exception, exit 0 |
  | Quota behavior | No HTTP 429; if hit, backoff retries and still exits 0 |
- **Status:** DONE (2026-07-08; reverified 2026-08-20)

### Step M3: Monthly-report observation run (end-to-end)
- **Type:** operator
- **Source step:** Step 8 (scheduled job, exercised end-to-end); requires Step 7 smoke gate green and Step M2 passed
- **Issue:** N/A (operator step)
- **Data boundary:** the CI run uploads both report variants to an artifact accessible to signed-in
  readers of this public repo. Use only data suitable for that audience in CI; observe private
  figures locally. Private Drive upload remains unimplemented.
- **Commands:**
  ```powershell
  # Local end-to-end (use the current fiscal year — FY2027 as of 2026-09):
  uv run pta-finance report --fy 2027 --variant both
  # CI end-to-end (after adding GOOGLE_SA_KEY_B64 + PTA_CONFIG_B64 secrets):
  gh workflow run monthly-report.yml
  ```
- **What to look for:**
  | Check | Expected outcome |
  |---|---|
  | Internal report | Full-detail schema renders from Budget Timeseries; charts and transaction table are present |
  | External report | Public-safe: totals, by-grade allocation, fundraising progress; **no payee names, no receipt links, no PII** |
  | Output destination | Reports in gitignored `reports/output/` + the ephemeral CI artifact; **not** committed; `report_log` row appended (live Drive upload is deferred) |
  | CI logs | No service-account JSON or config value echoed anywhere in the run log |

**Next manual step: M3** — observe one local and one dispatched monthly-report run end-to-end.

## 12. Appendix

### Tab column lists (single source of truth → `schema.py`)

- **transactions:** `id, date, fiscal_year, type, amount, category, grade, payee, memo, budget_id, receipt_id, source, entered_by, created_at, needs_review`
- **receipts:** `id, txn_id, drive_url, description, amount, date, added_by, created_at`
- **budget:** `id, fiscal_year, category, grade, budgeted_amount, notes`
- **events:** `id, fiscal_year, name, date, type, expected_income, expected_expense, nag_schedule, notes`
- **report_log:** `run_at, variant, month, output_url, generated_by`

### ID grammar (→ `ids.py`)

```
TXN-FY{yy}-{seq:04d}     # transactions
RCP-FY{yy}-{seq:04d}     # receipts
BUD-FY{yy}-{slug}        # budget (slug = kebab category, optional -g{grade})
EVT-FY{yy}-{slug}        # events
yy   = last two digits of fiscal_year_label(date, start_month)
seq  = per-fiscal-year, per-entity zero-padded counter
```

### Dedup hash

```
key = sha1(f"{iso_date}|{amount_cents}|{normalized_payee}").hexdigest()
# normalized_payee = casefold, collapse whitespace, strip punctuation
```

### Private config schema (`config.toml`, gitignored — `config.example.toml` ships fake values)

```toml
[organization]
name        = "Example PTA"            # PTA / booster name
school_name = "Example Elementary"
school_email = "office@example.org"

[contacts]
president       = ["president@example.org"]
treasurer       = "treasurer@example.org"
cfo             = "cfo@example.org"
account_holders = ["president@example.org", "treasurer@example.org", "cfo@example.org"]  # Phase-3 allowlist

[fiscal_year]
start_month = 1                        # 1 = January (calendar year)

[grades]
labels = ["K", "1", "2", "3", "4", "5"]

[sheets]
spreadsheet_id           = "<google-spreadsheet-id>"
test_spreadsheet_id      = "<throwaway-test-sheet-id>"     # for `check`
drive_receipts_folder_id = "<google-drive-folder-id>"
drive_reports_folder_id  = "<private-drive-folder-id>"     # report outputs (never the repo)

[google]
service_account_file = "secrets/service-account.json"

# [receipt_mapping]             # optional; omit for all-history mapping
# received_since = "YYYY-MM-DD" # inclusive outer RFC-822 Date-header cutoff

# [llm]            # Phase 4
# api_key_env = "ANTHROPIC_API_KEY"
```

---

## Phase 1 — v1 Automated Build (Steps 1–8)

**All 8 issues (#1–#8) closed. The full test suite, `mypy --strict`, and Ruff gates passed. Built
directly on `main` (sequential greenfield, no
worktrees); pushed `cbeeecc..193bed2`.**

### What was built
- **Step 1** — `uv`/hatchling scaffold (ruff, `mypy --strict`, pytest), `config.py` (TOML load +
  fail-fast validation), `ids.py` (single-source ID grammar + `fiscal_year_label`), the CI
  identity guard, and `ci.yml`.
- **Step 2** — `schema.py` (single source of truth for tab column lists, `is`-identity registry) +
  `models.py` (entity dataclasses, Decimal money, tolerant parsers, import-time field/schema guard).
- **Step 3** — `sheets.py` (service-account gspread wrapper: atomic row-targeted `batch_update`,
  429/500/503 exponential backoff + jitter, schema validation) + `backup.py` (CSV snapshots).
- **Step 4** — `etl.py` (normalize legacy ledger: FY-scoped ID assignment seeded from existing ids,
  `(date|amount|payee)` dedup, malformed-row resilience, snapshot-before-write).
- **Step 5** — `analytics/` (exact integer-cents aggregation by category/grade/month, budget-vs-actual,
  multi-year trends + YoY; `needs_review` rows excluded).
- **Step 6** — `reports/` (internal + external HTML via Jinja2 with autoescape, matplotlib Agg charts,
  optional WeasyPrint PDF; the external builder's runtime `ExternalReportPIIError` guard).
- **Step 7** — `tests/test_smoke_pipeline.py` (end-to-end wiring gate, real modules, in-memory sheet).
- **Step 8** — `.github/workflows/monthly-report.yml` (cron + dispatch, secret-safe credential
  restore, ephemeral artifact, `last-run.txt` scheduler keepalive).

### Files changed

| Area | Files |
|---|---|
| Package | `pta_finance/{config,ids,schema,models,sheets,backup,etl,cli}.py`, `analytics/{aggregate,trends}.py`, `reports/{builder,charts,render}.py` + `templates/{internal,external}.html.j2` |
| Config / CI | `pyproject.toml`, `config.example.toml`, `scripts/check_no_identity.py`, `.github/workflows/{ci,monthly-report}.yml`, `.github/last-run.txt` |
| Tests | `tests/conftest.py` + 12 `test_*.py` (full suite passing) |

### Fresh-context notes

| Issue | Detail |
|---|---|
| Review-caught fixes | Step 3: header row is never a data-write target (upsert/delete). Step 4: malformed id-less rows persist `needs_review` by sheet position via `SheetsClient.update_rows_by_index`. |
| Drive deviation | Live Drive upload deferred to Phase 2 (needs `google-api-python-client` per §8); v1 = local `reports/output/` + the CI artifact. |
| Money | Aggregated as exact integer cents — never binary floats. |
| Next | Operator-gated Manual Step M3 (monthly-report observation run) — see §11 Manual Steps. |

---

## Phase 4 — Receipt ingestion (shipped: profiler + mapping engine + Reimbursements ledger + Receipts Explorer + the `fetch-mail` Gmail connector)

**Reimbursement refresh milestone complete: all six steps in
`documentation/reimbursement-refresh-plan.md` shipped. The repository gate was then 535 tests passing
(plus one optional skip), zero type errors, and zero lint/format violations. Receipt ingestion was
also shipped end-to-end against a real, gitignored mailbox and live Sheet; the live write path was
revalidated with snapshot + semantic read-back reconciliation on 2026-08-20. Private mailbox
counts and financial totals remain outside the repo. Posterity issue #24 is closed.**

### What was built
- **`receipt_ingest.py`** — a credential-free parser for reimbursement-form `.eml`/`.mbox` emails
  (e.g. Wix form-submission notifications). Recognizes a submission **structurally** (labeled Total +
  numbered *Date / Category / Description / Amount* line items); tolerant of inconsistent label
  spacing and missing fields. Adds `.mbox`/Takeout reading (`iter_mbox` / `iter_source`), a PII-free
  batch **`Profile`** (form types, category vocabulary, blank-field rates, reconciliation, FY span,
  and the **email-date span** for completeness), and `is_reply_or_forward` (drops `Re:`/`Fwd:` thread
  duplicates that `Message-ID` dedup can't catch), plus one shared RFC-822 received-date parser
  used by the profile and mapper FY fallback without converting the header-local calendar date.
- **`receipt_map.py`** — pure `Submission` → flat **Reimbursements** ledger rows: carry-forward blank
  category/date, skip blank-amount lines, canonical-category lookup + per-form default,
  `Message-ID` + content-hash dedup, `needs_review` reasons.
- **`ingest-receipts` CLI** — preview each submission, or `--profile` the whole batch (`--csv` writes
  a category-map seed). **`map-receipts` CLI** — build the ledger; `--write-tab` creates/replaces a
  machine-owned Sheet tab via `SheetsClient.replace_tab_grid` (RAW grid + USER_ENTERED amount so it
  totals in native SUM/QUERY); only validated finite amounts take that numeric path, while rejected
  amount text remains reviewable. Category-seed, ingest, mapped-ledger, and snapshot CSVs share one
  injective formula-neutralization boundary; each backup pairs the inspection/import CSV with a
  versioned tagged `userEnteredValue` JSON grid before replacement. Mapping without explicit
  `--start-month` freezes one
  required config snapshot before
  category/source parsing and reuses it for both fiscal-year and cutoff policy.
  Optional private `[receipt_mapping] received_since` is the
  authoritative inclusive ledger cutoff; explicit `--received-since` / `--all-received` overrides
  win, recognized originals are partitioned before the mapper's single dedup call, and every
  zero-ledger-row `--write-tab` refuses before Sheet-client construction.
- **Receipts Explorer** (Sheet-side, not repo code) — a dropdown-driven QUERY+pie dashboard over the
  Reimbursements tab: Panel A breaks down by dimension, Panel B drills into one category. Operator
  load procedure: [docs/loading-receipts.md](docs/loading-receipts.md).
- **`gmail_source.py` + the `fetch-mail` CLI** (shipped 2026-08-26; `documentation/gmail-ingest-plan.md`,
  issues #15–#22) — the Gmail read-only ingest connector that retires the manual Takeout export: user
  OAuth pinned to `gmail.readonly`, a date-scoped fetch, and an idempotent `.eml` writer landing files
  in `mail_samples/` beside the archives so ONE `map-receipts` run dedups both. Fetching only — the
  unattended cron half is deliberately not built (see "Not yet built" below).
- **Strict payment-confirmation lanes** (landed on `main` 2026-09-06, commits `01f7bff` + `aa9b1b1`;
  built as a build-step outside a numbered plan step, documented in
  `documentation/reimbursement-refresh-plan.md` § "2026-09-06 payment-confirmation-lanes amendment").
  Schema-v2 anchors gain `payment_links` (exact archived operator mail, two strict Zelle grammars,
  per-ticket reference-digest bindings, atomic group validation/quarantine) and `operator_payments`
  (audited no-mail payments); under v2 only those lanes authorize payment, while schema-v1 anchors
  replay their historical behavior byte-for-byte, including v1 payment mail's companion
  clarification/receipt events. Gate at landing: 950 collected tests, strict mypy, Ruff, identity
  check all green. Known fail-closed gaps from the landing review (next fix step): generated-single
  mail bound to a bundle-held `email_context` cannot replay after settlement; the v1 lane records at
  the source total while the loader validates the reviewed-effective total; v1 Check/discrepancy
  histories cannot replay under v2 although the docs promise reproducibility.

- **Data-driven reimbursement review report** (shipped 2026-08-27; supplemental email-event lane
  shipped 2026-08-30; `documentation/reimbursement-refresh-plan.md`) — `report-reimbursements`
  renders a strict, gitignored schema-v2 bundle offline; `update-reimbursements` optionally acquires
  Gmail, refreshes the complete local archive once, preserves stable reviewed identities, appends
  genuinely new submissions as unreviewed, and accounts for follow-up receipts, clarification,
  payment, and scoped approval mail in an append-only event ledger. Exact RFC ancestry or an
  explicit private anchor is required; ambiguous evidence cannot mutate a ticket. Changed or
  missing accounted evidence fails closed. Mail sending and all Sheet writes remain separate
  permission boundaries.

### Deliberate design choice
Receipts land in a **flat, denormalized "Reimbursements" tab** (Explorer-ready), NOT the canonical
`transactions`/`receipts` schema — so no schema change was needed and the dashboard reads it directly.

### Not yet built (remaining Phase-4 work)
- **Budget Timeseries roll-up** — aggregate the ledger's per-category actuals into Budget Timeseries
  so reimbursement spend appears in the monthly/FY reports (which read Budget Timeseries, not this tab).
- **Monthly automation (cron half)** — the *fetch* half shipped 2026-08-26 as `fetch-mail` (see
  "What was built" above), so no manual Takeout export is needed; the cron half stays deliberately
  out of scope: `documentation/gmail-ingest-plan.md` § Design Decision 7 keeps the OAuth token out
  of CI (a personal-mailbox refresh token in a public repo's Actions secrets would expose the whole
  inbox), so ingestion is local-only and operator-run and the monthly workflow still does reports
  only. Revisit only as a separate, deliberate decision.
- **Live Drive fetch** of the linked receipt PDFs.

### Files changed

| File | Change |
|---|---|
| `pta_finance/receipt_ingest.py` | `.eml`/`.mbox` parser + PII-free `Profile` + shared RFC-822 received-date parser + `is_reply_or_forward` (structural recognition, no identity hard-coded) |
| `pta_finance/receipt_map.py` | New — pure `Submission` → flat Reimbursements ledger rows (dedup, carry-forward, per-form default, `needs_review`) |
| `pta_finance/reimbursement_events.py` | Strict private anchor, actor, proposal, payment, and item-complete operator-review parsing |
| `pta_finance/reimbursement_pipeline.py` | Stable-keyed full-archive snapshot, exact supplemental linkage, lifecycle reduction, item recommendations, fail-closed merge, and atomic private-bundle refresh |
| `pta_finance/reimbursement_report.py`, `pta_finance/reports/templates/reimbursement_queue.html.j2` | Strict schema-v2 loader with explicit v1 migration, supplemental history/unmatched evidence, deterministic email composition, summary model, and offline atomic HTML renderer |
| `pta_finance/sheets.py` | New `replace_tab_grid` — schema-independent create/replace of a machine-owned tab (RAW grid + USER_ENTERED numeric column) |
| `pta_finance/cli.py` | Receipt ingestion/mapping plus separate `report-reimbursements` and `update-reimbursements` entry points |
| `tests/test_receipt_ingest.py`, `test_receipt_map.py`, `test_sheets.py` | Parser / profiler / mapper / writer coverage over synthetic fixtures |
| `tests/test_reimbursement_events.py`, `test_reimbursement_pipeline.py`, `test_reimbursement_report.py`, `test_reimbursement_cli.py` | Synthetic anchor/proposal/payment parsing, stable-key, fail-closed, strict-schema, renderer, email, and CLI-boundary coverage |
| `docs/loading-receipts.md`, `SETUP.md` | Operator load how-to + completeness check; acquisition half since replaced by `fetch-mail` (Gmail → `fetch-mail` → `map-receipts`), and SETUP.md §6 adds the OAuth stage |

### Fresh-context notes

| Issue | Detail |
|---|---|
| Operational scope | Parsing, mapping, and `--write-tab` are shipped; Gmail OAuth **fetching** shipped later as `fetch-mail` (see the Monthly-automation bullet above). Unattended/cron ingestion and linked Drive-file retrieval remain deferred. |
| Identity rule | Recognition is structural; no org/person/email in code or tests. Real `.eml` samples stay gitignored (default source `./mail_samples`). |
| Supplemental linkage | Follow-up mail changes a ticket only through exact RFC ancestry or a strict private anchor. Secondary approval applies only to the fully parsed anchored proposal; trailing prose does not broaden scope. |
| Payment under schema v2 | Only `payment_links` / `operator_payments` settle a ticket; the ticket must already be APPROVED with every item A and the amount must equal the reviewed-effective total, else the record is quarantined or held (and that outcome persists). Operator records are content-addressed, so a persisted operator review cannot be amended in place — retiring the prior review record and event from the bundle is the current workaround; a `supersede` mechanism is a fix-step candidate. |
| Sheet-side work | Related dashboard work (chart recolor, FY2025/27 `raw_category` canonicalization, the Group Explorer tab) lives in the Google Sheet, not this repo. |

---

## README presentation refresh — 2026-09-08

**Status: COMPLETE.** Accepted by the user; shipped in `79bed2d` and `273d0ed`.
This documentation update adds no application behavior and completes no pending build step.

### What changed

- Shorter, plain-language descriptions of budgets, financial reports, and reimbursement review.
- Light/dark workflow diagrams in the style of skill-mesh.
- Screenshots from the real reimbursement renderer using fictional data.
- A native, editable fictional treasurer snapshot and its PowerPoint-exported screenshot.
- Clear boundaries for shipped features, the unfinished slide workflow, and public Actions artifacts.

### Files and maintenance

| File | Purpose |
|---|---|
| `README.md` | Public overview, examples, setup links, and visual regeneration commands |
| `docs/diagrams/` | Editable light/dark SVG sources for both workflows |
| `docs/screenshots/` | Two reimbursement views and the treasurer snapshot |
| `docs/examples/treasurer-snapshot.pptx` | Editable one-slide example containing only fictional data |
| `scripts/capture_readme.py` | Render fictional reimbursement fixtures and capture them with Playwright |
| `scripts/export_example_snapshot.ps1` | Export the example slide through desktop PowerPoint on Windows |

Validation: 42 report/reimbursement tests passed; Ruff lint/format and strict mypy passed.
Linux and Windows CI passed for `273d0ed`. Both capture helpers ran; the README was checked
in light/dark themes and at a narrow width. The sample deck was checked for private content
and its fictional figures were reconciled. The slide is a presentation example; automatic
slide generation remains governed by `documentation/treasurer-summary-wave-1-plan.md`.

---

## Phase 6 — Monthly reimbursement board summary — planned (Steps 26–31)

**Objective:** Produce a repeatable one-page reimbursement update for the board, principal,
and teacher representatives from the reviewed private queue, with explicit reporting dates,
editable discussion items, and preserved final HTML/PDF editions.

**Status: PLANNED.** The private reference design was accepted and finalized on 2026-09-10.
Production implementation is governed by
[documentation/reimbursement-board-summary-plan.md](documentation/reimbursement-board-summary-plan.md).
Reserve Steps 26–31 for this workflow. It is independent of the bank-statement/Google Slides
work in Steps 14–25 and adds no scheduled mail fetching, Sheet writes, or message sending.

Plan preparation completed 2026-09-10: [umbrella #55](https://github.com/aberson/pta_finance/issues/55),
automated Steps 26–30 in #56–60, and attended Step 31 in #61. Implementation remains pending.

---

## Phase 7 — Shared reimbursement workflow proof — accepted (Steps 32–35)

**Objective:** Explain and prove a small Google-hosted workflow in which two authenticated
users share one fictional reimbursement request, comments, a decision, and the next owner.

**Status: STEPS 32–35 DONE.** The comments service passed actual two-account Cloud Run acceptance in M6. Reviewer approval/not-approval and processor administrative completion now pass the full candidate and main suites, independent reviews and feature CI. Hosted M7 acceptance and operator wording judgment pass. The scoped source of truth is
[documentation/shared-workflow-proof-plan.md](documentation/shared-workflow-proof-plan.md).
Reserve Steps 32–35 for this feature; it is independent of the pending Slides and monthly
board-summary work.

Execute the feature plan in two separate phases. Phase A contains automated Step 32 and
attended Step 33/M6: prove attributed shared comments and persistence across a new service
revision with two real Google identities. Phase B contains automated Step 34 and attended
Step 35/M7: reviewer approval/not-approval and processor completion. Phase B is ineligible
until Step 33/M6 is recorded DONE with deployed evidence; do not run the entire plan as
one unattended span.

The comments proof passed Cloud Run IAP and Firestore acceptance. The reviewer-to-processor handoff passed its separate deployed acceptance; see the [M7 closeout](documentation/shared-workflow-proof-sync.md#m7-closeout--2026-09-13-utc).
Administrative completion does not record payment or mutate the private reimbursement
bundle, Sheets ledger, or mailbox. Actual project, account, and billing configuration stays
private. The independent plan review is recorded in
[documentation/shared-workflow-proof-review.md](documentation/shared-workflow-proof-review.md).

---

## Phase 8 — Live shared request queue — built; M8 next (Steps 36–37)

**Objective:** Connect the accepted queue design to shared storage so the same two signed-in
participants can find six fictional requests and work through each request's own history.

**Status: STEP 36 DONE; STEP 37 / M8 NEXT.** The fixed six-request queue passes the complete
candidate/main suites, independent reviews and feature CI. The scoped plan is
[documentation/shared-request-queue-plan.md](documentation/shared-request-queue-plan.md).
Step 36 delivers the application slice; Step 37 separately verifies its hosted behavior as M8.
Step 35 / M7 is DONE. See [queue delivery evidence](documentation/shared-request-queue-sync.md).

The original request and receipts remain intact. Five additional packaged fictional requests
start awaiting review; actual signed-in actions establish their decisions. Request intake,
private imports, more users, notifications and payment tracking remain future work.

The requested follow-up is [ticket creation and lifecycle rules](documentation/shared-ticket-lifecycle-design.md):
Processor-only creation, shared drafts, review submission and reversible archive. Product rules are recorded;
its engineering plan follows acceptance of the live queue and does not expand Steps 36–37.

---

## Receipt viewer and source backfill — shipped 2026-09-14

**Status: COMPLETE.** Accepted by the user for current items ("Looks great!"), then extended to
every older and closed submission whose original receipt exists. This adds one optional,
offline evidence layer to the reimbursement queue; it changes no amounts, decisions, or Sheets.

### What was built

- `pta_finance/receipt_viewer.py`: strict loader for an optional private sidecar
  (`<bundle>.receipts.json`) of PNG/JPEG receipt pages plus per-item locations, validated on exact
  keys, image digests, item fingerprints (`item_fingerprint` ties a location to the ticket's
  source evidence and displayed claim), in-bounds boxes, sidecar-relative path containment, and
  size caps (20 MiB per page, 100 MiB total). Invalid or stale evidence stops HTML replacement.
- `build_report` auto-detects the sidecar beside the bundle and embeds pages and locations into
  the queue HTML (offline, no scripts or network). Each item becomes a button: linked items open a
  dialog with red ellipses on the source lines, page navigation, zoom, fit, focus, and Esc/Close
  focus restoration; unlinked items show "Receipt not linked".
- Private backfill (gitignored `reports/output/.work/receipt-backfill/`): display-normalized page
  copies (EXIF transpose, bounded downscale, two upside-down photos rotated), Tesseract line
  geometry as a navigation aid, proof renders using the viewer's own ellipse geometry, a sidecar
  builder, a Playwright smoke test, a promote-with-backup script, and a per-item audit. Marks were
  verified by independent vision reviewers plus adversarial refuters, with a resolver round for
  disputes. Result: 147 of 227 queue items linked across 43 of 49 tickets over 164 pages; the 80
  unlinked items have no original receipt in the evidence.

### Files changed

| File | Change |
|---|---|
| `pta_finance/receipt_viewer.py` | New sidecar loader and item fingerprint |
| `pta_finance/reimbursement_report.py` | `render_html(receipts=...)`, `build_report` sidecar detection |
| `pta_finance/reports/templates/reimbursement_queue.html.j2` | Item buttons with receipt hints; viewer include |
| `pta_finance/reports/templates/receipt_viewer.{html,css,js}.j2` | Dialog markup, styles, and inline viewer script |
| `tests/test_receipt_viewer.py` | Loader, determinism, invalid-evidence, and browser navigation tests |
| `docs/receipt-viewer.md`, `docs/loading-receipts.md` | Operator guide and cross-link |
| `README.md`, `CLAUDE.md` | Feature mention, guide link, current state |

### Fresh context notes

| Issue | Detail |
|---|---|
| Ellipse geometry | The viewer inflates each box (`rx = 0.60w + 0.006`, `ry = 0.72h + 0.003`); a box that must exclude an adjacent line needs about 22% vertical headroom. |
| Mark convention | Product lines with their price, plus one ellipse over the totals block when the total equals or explains the claim; claim money is never changed and documented claim/receipt discrepancies are preserved. |
| Missing originals | Paper-total tickets, one absent IKEA upload, and the unlocated Amazon invoice rows stay "Receipt not linked"; two attachments filed as receipts were mis-filed mailbox screenshots. |
| Size | The private HTML is about 92 MB with 164 embedded pages; lower JPEG quality would cut roughly a quarter if needed. |

Validation: 821 passed, 2 skipped (823 collected) on the selected suite that excludes the
Firestore-emulator, browser-proof, and native-PDF suites whose services were not running; Ruff
lint/format, strict mypy (39 files), `git diff --check`, and the identity guard passed. The live
report was regenerated by `report-reimbursements` and its digest matched the smoke-tested candidate.

---

## Manual UAT

*M6 passed on 2026-09-12. The procedure below is retained for repeat execution; exact deployment and account evidence stays private.*

### M6: Prove two independent Google users share durable state on Cloud Run

- **Source step:** Step 33 in [the shared workflow proof plan](documentation/shared-workflow-proof-plan.md); Phase A code ends at Step 32.
- **Issue:** #64
- **Status:** DONE — all deployed observations and operator acceptance recorded privately; see [M6 closeout](documentation/shared-workflow-proof-sync.md#m6-closeout--2026-09-12-utc).
- **Commands to run:** Complete the ordered readiness, resource setup, and identity-binding instructions in [the attended runbook](docs/shared-workflow-proof.md). Supply its private variables before these commands. Each command must succeed before continuing; an unavailable prerequisite remains pending.

  ```powershell
  uv sync --locked --extra dev --extra web
  if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
  uv run python scripts/stage_shared_workflow.py --output $PtaStage
  if ($LASTEXITCODE -ne 0) { throw 'Source staging failed' }
  gcloud builds submit $PtaStage --project=$PtaProject --region=$PtaBuildRegion --service-account=$PtaBuildIdentity --gcs-source-staging-dir="gs://$PtaSourceBucket/source" --config="$PtaStage/cloudbuild.yaml" --substitutions="_IMAGE=$PtaImageTag"
  if ($LASTEXITCODE -ne 0) { throw 'Build or image inspection failed' }
  ```

  Record the successful image inspection and immutable digest privately, then use the runbook's digest-based deployment commands. Complete identity mode, pin the two observed subjects and canonical service origin, switch to comments mode, and run every observation below. A fresh revision with the same image/database/namespace must preserve saved comments. Record results in ignored `reports/output/shared-workflow/m6-acceptance.md` using the runbook's template.

- **What you're looking for:**

  | Check | Expected outcome |
  |---|---|
  | Prerequisites and ownership | Project, ancestry/policy, billing/APIs, locations, deployment permissions, dedicated identities and intended resource ownership are verified privately; denied reads remain unknown. |
  | Actual image | The actual Cloud Build image passes inventory/environment and packaged-resource inspection before deployment; build ID and immutable digest are recorded. |
  | Identity | Two separate Google profiles show distinct signed subjects and the assigned roles; identity mode reveals no request or history. |
  | Shared comments | Within 60 seconds of comments-mode readiness, A saves and B reads a unique fictional comment, then B saves and A reads; actor, timestamps and versions are correct. |
  | Durability | A new Cloud Run revision using the same namespace retains both saved comments for both profiles. |
  | Admission and authorization | Signed-out access reveals no data; disabled-roster B is denied while still admitted by IAP, and works again after restoration. |
  | Direct request boundaries | Actor/role spoofing and missing/wrong Origin or CSRF headers fail without adding events. Unavailable cloud assertion-injection checks remain pending or identified as local-only evidence. |
  | IAM and storage | IAP is enabled, invocation is private, only the two intended users have effective admission, the dedicated runtime identity is attached, and database access has the intended boundary. |
  | Error and portability behavior | No false saved message follows an uncertain error; hosted comments still work with the local development server stopped. |
  | Evidence and phase gate | The private record names checked revisions, digest and each outcome. Step 33 remains pending until every required observation is verified; Step 34/Phase B cannot begin before that. |

### M7: Accept the two-role handoff on the deployed service

- **Source step:** Step 35 in [the shared workflow proof plan](documentation/shared-workflow-proof-plan.md); Step 34 code delivery is complete.
- **Issue:** #66
- **Status:** DONE — all 15 observations and operator judgment pass; see the [M7 closeout](documentation/shared-workflow-proof-sync.md#m7-closeout--2026-09-13-utc).
- **Commands to run:** Execute the ordered [M7 runbook](docs/shared-workflow-proof.md#m7-accept-the-deployed-handoff-step-35). Build and inspect a new immutable image, preserve the M6 configuration and namespace, and deploy in handoff mode with the existing two pinned accounts. Retain the approved proof, use fresh namespaces for not-approval and the opposing-decision race, then restore the approved namespace. Every command must pass before continuing.
- **What you're looking for:**

  | Check | Expected outcome |
  |---|---|
  | Upgrade and image | New actual image inspection passes before deployment; the original comments and receipts survive the handoff upgrade. |
  | Approval and ownership | Reviewer approval requires a comment, transfers ownership to processor, and enables processor completion. Direct wrong-role calls return 403 without adding an event. |
  | Completion and durability | Both users read administrative completion, null next owner and correct event attribution after another fresh revision. Earlier history survives. |
  | Not-approval | A fresh namespace proves terminal not-approval; completion and changed decisions fail without altering either proof. |
  | Comments after outcomes | Both roles can comment after each terminal outcome while the decision and owner remain unchanged. |
  | Retries and races | Identical operations return their original receipts. Changed payloads, stale versions and competing decisions cannot overwrite the winner or add rejected events. |
  | Reads | Both users read complete bounded history and later comments; unexpected query parameters and unknown IDs fail safely. The 100-event cap remains mandatory emulator coverage. |
  | Restoration and explanation | Restore the original approved namespace and retain both branch histories. Both users identify the decision, next owner and comment authors without interpreting completion as payment. |
  | Evidence | Record the exact image, revisions, namespace configuration backups and all 15 runbook observations privately in `reports/output/shared-workflow/m7-acceptance.md`. Mark Step 35 DONE only after every required row passes. |

### M8: Accept the live shared queue with both accounts

- **Source step:** Step 37 in [the queue plan](documentation/shared-request-queue-plan.md); Step 36 code delivery is complete.
- **Issue:** #69
- **Status:** PENDING — the delivered source was staged and its actual cloud image passed inspection on 2026-09-14 UTC. Queue deployment and two-account observations remain pending; the live service remains in accepted M7 handoff mode.
- **Commands to run:** Resume the ordered [M8 runbook](docs/shared-workflow-proof.md#m8-accept-the-deployed-shared-queue-step-37-prepared-not-executed) from the retained inspected image. Coordinate fresh desktop availability and verify both accepted M7 account baselines before any mode change. Preserve the M7 runtime and full original history; admit queue mode in the same namespace with the same two subjects. Keep every private receipt and do not repeat completed staging/build steps.
- **What you're looking for:** Both users see the same six requests and current counts, can search/filter/open each history, and correctly identify the next owner. Real controls approve DEMO-03, do not approve DEMO-04, approve and complete DEMO-06, and add comments to DEMO-02 and DEMO-05. Each action changes only its selected history; the original M7 request and exact receipts remain intact. Wrong-role, unknown/query and disabled-account reads/writes/replays fail safely. Restore the disabled user even after an interrupted test, verify both enabled identities, and prove all six full histories survive a fresh revision. The operator confirms finding work, ownership/history and administrative nonpayment wording.
- **Completion boundary:** Record every required row privately and mark Step 37 DONE only after actual observations and operator judgment pass. Ticket lifecycle engineering and build follow this accepted queue.
