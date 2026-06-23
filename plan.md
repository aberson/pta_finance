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
| Language / runtime | Python `>=3.12` | Matches workspace convention; `tomllib` in stdlib (no TOML dep) |
| Dependency / build | `uv` + `hatchling` | Workspace standard (`switchboard/pyproject.toml`); reproducible, fast |
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

1. **Snapshot before every write:** `snapshot` exports each tab to CSV under `snapshots/<utc>/`
   (and optionally a private Drive backup folder) before any mutating run.
2. **Atomic writes:** all writes go through `gspread` `batch_update` — all-or-nothing; a failed
   subrequest rolls back the whole batch.
3. **Sheets version history** is the automatic secondary safety net.
4. **Rate-limit safety:** writes batch 10–50 rows per request and retry on HTTP 429 with
   exponential backoff + jitter (project quota: 300 req/min; per-user: 60 req/min).
5. **Restore:** roll back via Sheets version history (primary), or re-import the latest
   `snapshots/<utc>/` CSVs (belt-and-suspenders). A dedicated `restore` CLI command is a
   candidate Phase-2 add; v1 relies on version history + the CSV snapshots.

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

> **Open question pinned at build Step 6:** the exact field list for each variant. The split
> above is the working default; the precise columns are confirmed during the report step.

Charts are matplotlib (Agg) PNGs embedded in the HTML. **Reports are never committed to the public
repo.** They are written to `reports/output/` locally, uploaded to a **private** Drive folder, and
(in CI) attached as an ephemeral workflow artifact for the operator. A row is appended to
`report_log`.

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
- **`backup.py`** — CSV snapshot export of all tabs (corruption protection).
- **`etl.py`** — normalize legacy/raw rows → canonical schema; assign missing IDs; dedup;
  `needs_review` flagging; snapshot-before-write.
- **`analytics/`** — `aggregate.py`, `trends.py` (pandas).
- **`reports/`** — `builder.py` (compute report data model), `render.py` (Jinja2 → HTML, optional
  WeasyPrint PDF), `charts.py` (matplotlib Agg), `templates/` (`internal.html.j2`, `external.html.j2`).
- **`cli.py`** — `argparse` entry point (`main`) exposing the subcommands below; wired as the
  `pta-finance` console script.

### CLI subcommands

| Command | Action |
|---|---|
| `pta-finance check` | Validate config + sheet schema; real round-trip read/write/delete of a test row (smoke) |
| `pta-finance snapshot` | Export CSV backups of all tabs |
| `pta-finance normalize` | Normalize legacy/raw ledger → canonical schema, assign IDs, dedup (snapshot first) |
| `pta-finance analyze [--fy YYYY]` | Run analytics; print summary / write analytics artifacts |
| `pta-finance report --month YYYY-MM [--variant internal\|external\|both]` | Generate monthly report(s) |

## 6. API Route Contract

**Not applicable in v1** — there is no backend HTTP API. (A web app + endpoints arrive in Phase 3.)

## 7. Project Structure

```
pta_finance/                      # repo root (standalone public repo)
├── plan.md                       # this document
├── CLAUDE.md                     # project context for future sessions (generic)
├── README.md                     # generic toolkit readme
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
│   ├── etl.py
│   ├── cli.py
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
│           └── external.html.j2
├── tests/
│   ├── conftest.py               # fake-org fixtures + mocked gspread client
│   ├── test_ids.py
│   ├── test_config.py            # config validation fails fast on missing fields
│   ├── test_schema.py            # asserts column-list identity with `is`
│   ├── test_etl.py
│   ├── test_analytics.py
│   ├── test_reports.py
│   └── test_smoke_pipeline.py    # end-to-end wiring gate (mock sheet)
├── secrets/                      # gitignored; holds service-account.json locally
└── snapshots/                    # gitignored; CSV backups
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
- **Reports never enter the public repo.** Written to a private Drive folder + ephemeral CI
  artifact only — generated reports can contain financial/identifying detail.
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
| Exact internal vs external report fields | Wrong fields leak PII or omit needed detail | Pin field lists at build Step 6; external template excludes payee/receipt/PII by default |
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

# 5. One-time legacy normalize, then analyze + report
uv run pta-finance normalize
uv run pta-finance analyze
uv run pta-finance report --month 2026-06 --variant both
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
| **4 — Power features** | One-year-ahead forecasting; receipt-email ingestion; bank-CSV / QuickBooks import; LLM report narrative + people-friendly wiki rendering; board-ramp wiki (LLM-friendly + people-friendly) | LLM token enters config here |

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
- **Issue:** #
- **Flags:** --reviewers code --isolation worktree
- **Produces:** project skeleton, `pta_finance/config.py`, `pta_finance/ids.py`, `tests/test_ids.py`, `tests/test_config.py`, `config.example.toml`, `.gitignore`, `.github/workflows/ci.yml`
- **Done when:** `uv run pytest -q`, `uv run ruff check .`, `uv run mypy --strict pta_finance` all pass; ID tests assert exact formats; `config.py` fails fast on a missing required field (tested)
- **Depends on:** none

### Step 2: Sheet schema + entity models
- **Problem:** Define `schema.py` (canonical tab names + ordered column lists as single-source-of-truth constants) and `models.py` (entity dataclasses + row (de)serialization). Add tests asserting column-list **identity** with `is` (not `==`).
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `pta_finance/schema.py`, `pta_finance/models.py`, `tests/test_schema.py`
- **Done when:** tests pass incl. an `is`-identity assertion on a shared column list; mypy strict clean
- **Depends on:** 1

### Step 3: Sheets client + backup
- **Problem:** Implement `sheets.py` (`gspread` service-account wrapper: open spreadsheet, read tab→records, atomic `batch_update` with 429 exponential-backoff+jitter, schema validation, named `update()` args) and `backup.py` (CSV snapshot of all tabs). Mock `gspread` in tests; include an integration test that exercises the production write path and asserts the batch+backoff code is reached (workspace `code-quality` rule: integration test through the production caller).
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `pta_finance/sheets.py`, `pta_finance/backup.py`, `tests/` additions
- **Done when:** unit + integration tests pass against a mocked client; mypy strict clean (add `[[tool.mypy.overrides]]` for `gspread`/`google.*` if untyped)
- **Depends on:** 2

### Step 4: ETL / normalize
- **Problem:** Implement `etl.py` — normalize legacy/raw rows to canonical schema, assign missing IDs via `ids.py`, dedup via `(date|amount|payee)` hash, flag ambiguous rows `needs_review`, snapshot-before-write. Rows with an unparseable date/amount are flagged `needs_review` and skipped — a single bad legacy row must never crash the whole run. Wire the `normalize` CLI subcommand. Integration test: legacy fixture → normalized round trip (assert IDs assigned, dups flagged, existing IDs untouched).
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `pta_finance/etl.py`, `normalize` in `cli.py`, `tests/test_etl.py`
- **Done when:** round-trip test passes; re-running normalize is idempotent (no dup IDs, no reassigned IDs); a malformed-legacy-row fixture is flagged `needs_review`, not fatal
- **Depends on:** 3

### Step 5: Analytics engine
- **Problem:** Implement `analytics/aggregate.py` (totals, income/expense, by category, by grade, by month via `pd.Grouper`, budget-vs-actual variance) and `analytics/trends.py` (multi-year fundraising/spend series, YoY). Wire the `analyze` CLI subcommand. Fixture-based numeric assertions.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `pta_finance/analytics/`, `analyze` in `cli.py`, `tests/test_analytics.py`
- **Done when:** known-fixture → expected-number assertions pass; mypy strict clean
- **Depends on:** 4

### Step 6: Report generation (internal + external)
- **Problem:** Implement `reports/builder.py` (compute the report data model from analytics), `reports/charts.py` (matplotlib Agg PNGs), `reports/render.py` (Jinja2 → HTML with autoescape on payee/memo; optional WeasyPrint PDF behind the `[pdf]` extra), and `templates/internal.html.j2` + `templates/external.html.j2`. **Pin the exact internal vs external field lists here.** The external variant must exclude payee names, receipt links, and member PII — enforce this as a **runtime invariant**, not just a test: the external builder raises a stable `ExternalReportPIIError` if any payee/receipt/PII field appears in the external data model (per `.claude/rules/security.md` § "Pair unsafe configs with startup safety checks" — a public-facing safety control must be a guard, not documentation). Wire the `report` CLI subcommand; append to `report_log`; write to `reports/output/` + (configured) private Drive folder — never to the repo.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `pta_finance/reports/`, `report` in `cli.py`, `tests/test_reports.py`
- **Done when:** both variants render from a fixture without error; a unit test AND the runtime `ExternalReportPIIError` guard both reject an external data model containing payee/receipt/PII fields; mypy strict clean
- **Depends on:** 5

### Step 7: End-to-end smoke gate (code)
- **Problem:** Add `tests/test_smoke_pipeline.py` — a 60-second end-to-end wiring test with REAL components (config → schema → etl → analytics → reports) against an in-memory / mocked Sheet, asserting the full pipeline completes once without exception and the rendered report contains the expected sections. No business-logic assertions — this is a producer/consumer drift gate, distinct from the M3 observation run.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `tests/test_smoke_pipeline.py`
- **Done when:** the smoke test passes in CI with no live Google calls
- **Depends on:** 6

### Step 8: GitHub Actions monthly report workflow
- **Problem:** Add `.github/workflows/monthly-report.yml` — `schedule: cron "0 9 1 * *"` + `workflow_dispatch`; `actions/checkout@v4`; `astral-sh/setup-uv` (cache on); restore `GOOGLE_SA_KEY_B64` + `PTA_CONFIG_B64` secrets by base64-decoding to files **without echoing**; `uv run pta-finance report --variant both` (the command writes to the private Drive folder, the canonical destination); the workflow then uploads the local `reports/output/` as an ephemeral artifact for operator download; **never commit reports to the repo**. Also append a UTC timestamp to a tracked `.github/last-run.txt` and push it (keepalive so the public-repo scheduler isn't auto-disabled after 60 days; this liveness marker is not a report). Confirm `ci.yml` (lint/type/test on PR) from Step 1 is green.
- **Type:** code
- **Issue:** #
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `.github/workflows/monthly-report.yml`, `.github/last-run.txt`
- **Done when:** `actionlint` (or a YAML lint) passes; a test asserts the workflow restores secrets via file redirect with no `run: echo`/`cat` of a secret variable and that it invokes `pta-finance report`; the real credentialed end-to-end run is deferred to M3
- **Depends on:** 7

### Manual Steps
*(These run after `/build-phase` completes. Operator drives.)*

### Step M1: Google Cloud + service-account setup
- **Source step:** prerequisite for M2/M3
- **Issue:** #
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

### Step M2: Real-sheet smoke (round-trip)
- **Source step:** Step 7 (real-credentials variant)
- **Issue:** #
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

### Step M3: Monthly-report observation run (end-to-end)
- **Source step:** Step 8 (scheduled job, exercised end-to-end); requires Step 7 smoke gate green and Step M2 passed
- **Issue:** #
- **Commands:**
  ```powershell
  # Local end-to-end:
  uv run pta-finance normalize
  uv run pta-finance report --month 2026-06 --variant both
  # CI end-to-end (after adding GOOGLE_SA_KEY_B64 + PTA_CONFIG_B64 secrets):
  gh workflow run monthly-report.yml
  ```
- **What to look for:**
  | Check | Expected outcome |
  |---|---|
  | Internal report | Full detail renders: ledger, payee names, receipt links, per-line variance; charts present |
  | External report | Public-safe: totals, by-grade allocation, fundraising progress; **no payee names, no receipt links, no PII** |
  | Output destination | Reports in `reports/output/` + private Drive folder; **not** committed to the repo; `report_log` row appended |
  | CI logs | No service-account JSON or config value echoed anywhere in the run log |

**Please run M1 next** once the Automated Steps complete.

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

# [llm]            # Phase 4
# api_key_env = "ANTHROPIC_API_KEY"
```
