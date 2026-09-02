# pta_finance

A generic, reusable **finance toolkit for a PTA / booster club / small nonprofit**. It treats a
**Google Sheet as the system-of-record database** for the organization's finances: it normalizes a
messy multi-year ledger into a clean schema, runs an analytics engine over it (spend by category,
spend by grade, budget-vs-actual, multi-year fundraising/spend trends), and generates **monthly
reports** in an **internal** (full-detail) and an **external** (public-safe) variant.

> **The repo is public; your data is private.** No organization, school, person, or email is
> hard-coded anywhere in this repository. All identity — org name, school name, board emails,
> spreadsheet/Drive IDs, fiscal-year setting, grade labels — lives only in a private, gitignored
> `config.toml`. Examples and tests use fake placeholders (`Example PTA`, `treasurer@example.org`).

v1 is deliberately small: a local Python CLI plus a GitHub Actions monthly cron — **no web UI, no
LLM, no Google Apps Script**. The design keeps recurring work in a cloud (GitHub Actions now, Google
Apps Script in a later phase) so the operational core never depends on a server anyone must pay for
or keep alive — which is what lets a non-technical successor operate it later with only a browser.

## How to use this documentation

New here? Read only the guide that matches what you need — you do **not** have to read them all.

| If you want to… | Read | Who it's for |
|---|---|---|
| Understand what this is | this README | everyone |
| **Work with the spreadsheet day-to-day** — change the budget, read the dashboards | **[docs/using-the-spreadsheet.md](docs/using-the-spreadsheet.md)** | **non-technical operators** |
| **Just ask an AI** (ChatGPT/Claude) instead of reading — ready-made prompts | **[docs/ask-an-ai-assistant.md](docs/ask-an-ai-assistant.md)** | **anyone** |
| Connect the toolkit to your Google Sheet (one-time) | [SETUP.md](SETUP.md) | whoever runs the tools |
| Load reimbursement receipts from email | [docs/loading-receipts.md](docs/loading-receipts.md) | whoever runs the tools |
| Refresh or rebuild the private reimbursement queue | [docs/loading-receipts.md#step-4--refresh-the-private-review-report](docs/loading-receipts.md#step-4--refresh-the-private-review-report) | treasurer/reviewer |

**The one rule that saves everyone time:** the spreadsheet _is_ the database, and there is exactly
**one** place to change a year's budget — the tab named **"FY&lt;year&gt; Budget"** (e.g.
`FY2027 Budget`). Make your changes there. **Never copy, duplicate, or edit an old budget sheet** —
those aren't connected to anything, so changes made on them have to be re-typed by hand before they
count. The full plain-language tour — every tab, how to change the budget, and how to hand over a
sheet you already edited — is in **[docs/using-the-spreadsheet.md](docs/using-the-spreadsheet.md)**.

## Stack

| Layer | Tool | Why |
|---|---|---|
| Language / runtime | Python `>=3.12` | `tomllib` in stdlib (no TOML dependency) |
| Dependency / build | `uv` + `hatchling` | Reproducible, fast |
| Sheets / Drive access | `gspread` + `google-auth` (service account) | Clean API, atomic batch writes |
| Gmail access (optional) | `google-api-python-client` + `google-auth-oauthlib` | User OAuth pinned to `gmail.readonly` |
| Analytics | `pandas` | By-category / grade / month aggregation, trends |
| Charts | `matplotlib` (Agg backend) | Deterministic, headless, zero-browser in CI |
| Templating | `Jinja2` (+ optional `WeasyPrint` for PDF) | Two report variants; HTML output, PDF optional |
| Native statement parsing (foundation) | optional `pypdfium2` `slides` extra in a Windows LPAC worker | Parse private PDF bytes only after a fail-closed sandbox attestation |
| CLI / config | stdlib `argparse` / `tomllib` | No extra dependency |
| Scheduler | GitHub Actions cron | Free, cloud-hosted monthly run |
| Lint / type / test | `ruff`, `mypy --strict`, `pytest` | — |

## Prerequisites

- Python `>=3.12` and [`uv`](https://docs.astral.sh/uv/) on your PATH.
- A Google account with a Cloud project (Sheets API + Drive API enabled) and a **service account**
  whose JSON key you can download.
- The target spreadsheet and a Drive folder shared with the service-account email (Editor role).
- *(Optional, only for `fetch-mail`)* an OAuth **Desktop app** client for the mailbox you want to
  read — a separate credential from the service account; see SETUP.md §6.

## Setup

```bash
# 1. Install
uv sync --extra dev            # add the [pdf] extra if you want WeasyPrint PDF output

# 2. Configure (private, gitignored)
cp config.example.toml config.toml
#    fill in: org/school name + email, board emails, spreadsheet_id,
#    drive folder ids, grade labels, fiscal_year.start_month (1 = calendar year),
#    and optional receipt_mapping.received_since (inclusive ledger cutoff)

# 3. Google service account (one-time)
#    download the service-account JSON to secrets/service-account.json
#    share the spreadsheet + Drive folder with the service-account email (Editor)

# 4. Verify, then run
uv run pta-finance check                                  # validate report_log + Budget Timeseries source
uv run pta-finance analyze                                # run analytics (Budget Timeseries)
uv run pta-finance report --fy YYYY --variant both        # fiscal-year reports (default: current FY)
```

The live toolkit provisions/validates only the `report_log` tab and sources `analyze` / `report`
from the operator-maintained **Budget Timeseries** tab; the canonical `transactions` / `receipts` /
`budget` / `events` tabs (and the `normalize` / `import-budget` commands that fill them) are
**optional/legacy** and may be deleted from the spreadsheet.

For the unattended monthly report, add two GitHub Actions secrets — `GOOGLE_SA_KEY_B64` (base64 of
the service-account JSON) and `PTA_CONFIG_B64` (base64 of `config.toml`) — and the
`monthly-report.yml` workflow runs on the 1st of each month (and on demand via **Run workflow**).

## Key design decisions

- **Sheet-as-DB + service account** — zero-server, transparent, and survives a non-technical
  handoff; unattended CI access without a human login.
- **One source of truth for schema + IDs** — column lists and ID formats live in single modules
  every producer and consumer imports; tests assert column-list identity so drift fails CI.
- **Stable, human-readable, fiscal-year-scoped IDs** (`TXN-FY26-0001`) — assigned by the tool,
  never rewritten.
- **Reports never enter this public repo** — written to a private Drive folder + an ephemeral CI
  artifact; the external variant has a runtime guard that rejects payee/receipt/PII fields.
- **Config-driven identity, fiscal year, and grades** — making the toolkit generic and reusable.

## Project layout

```
pta_finance/        package: config, ids, schema, models, sheets, backup, etl, cli,
                    gmail_source, budget_sync, report_source, receipt_ingest, receipt_map,
                    reimbursement_events, reimbursement_pipeline, reimbursement_report,
                    analytics/, reports/(templates/)
tests/              fake-org fixtures + mocked gspread; an end-to-end wiring smoke gate
.github/workflows/  ci.yml (PR gate) + monthly-report.yml (cron)
config.example.toml committed template with fake values; real config.toml is gitignored
```

See [plan.md](plan.md) for the full design, data model, and build steps, and
[CLAUDE.md](CLAUDE.md) for project context.

## Status

**v1 complete** — issues #1–#8 closed. The full toolkit ships: config/IDs, a single-source-of-truth
schema, a service-account Sheets client (atomic row-targeted writes + 429 backoff), idempotent
legacy-ledger ETL (ID assignment, dedup, malformed-row resilience), an exact-cents analytics engine,
internal/external HTML reports with a runtime PII guard, an end-to-end smoke gate, and a monthly
GitHub Actions report workflow. The full test suite, `mypy --strict`, and Ruff gates passed at that
milestone. First-run setup needs the Google service account (see Setup) — then
`uv run pta-finance check`.

**Receipt ingestion (Phase 4)** — a credential-free `.eml`/`.mbox` parser (`receipt_ingest.py`) with
two CLIs: `ingest-receipts --profile` scans a whole mailbox and reports the data spread (form types,
category vocabulary, blank-field rates, reconciliation, and the **email-date span** that catches a
gappy export), and `map-receipts` projects the parsed submissions onto a flat **Reimbursements**
ledger (carry-forward blank categories, per-form defaults, `Message-ID` + content-hash dedup,
`needs_review` flags). Its optional private `receipt_mapping.received_since` cutoff is applied to
the outer email date before dedup; `fetch-mail --since` controls acquisition only. The mapper
reports its effective cutoff and excluded count, then writes to the Sheet with `--write-tab`. A
zero-row `--write-tab` is refused before any Sheet client is constructed. A dropdown-driven
**Receipts Explorer** dashboard reads that ledger. Mail now arrives through
`fetch-mail` — a read-only Gmail
connector (`gmail_source.py`, OAuth pinned to `gmail.readonly`) that fetches a date window straight
into the gitignored inbox directory, retiring the manual Takeout export. See
[docs/loading-receipts.md](docs/loading-receipts.md) for the end-to-end load (`fetch-mail` →
`map-receipts`) with a completeness check. Receipt CSV exports neutralize formula-like inbound text
while retaining validated signed money as numeric cells. A replacement backup keeps a
spreadsheet-safe CSV beside a versioned, tagged raw JSON `userEnteredValue` grid, so the first
replacement of a pre-existing tab is safe to inspect and formulas remain distinguishable from
identical literal text. Native numbers, booleans, strings, and empty cells remain typed in JSON.
Formatting/comments are outside the artifact; Sheets version history is the primary recovery path,
and there is no automated JSON restore command. The full test suite, `mypy --strict`, and Ruff gates
pass.

**Phase 4 reimbursement refresh complete** — issues #24 closed. The private reimbursement queue
is data-driven: `report-reimbursements` validates one gitignored schema-v2 bundle and renders the
complete HTML offline, while `update-reimbursements` optionally runs `fetch-mail`, refreshes
stable-keyed original submissions plus append-only supplemental email evidence, and then renders.
Exact RFC ancestry or an explicit private anchor links follow-up receipts, clarification/payment
responses, and scoped secondary approvals; ambiguous mail remains visible but cannot mutate a
ticket. New submissions receive non-authoritative item-level recommendations while their recorded
decision remains **unreviewed**. Neither command sends mail or writes Sheets. Existing reviewed
records fail closed if their source evidence changes or disappears, so a refresh cannot silently
attach an old decision to different evidence. The current repository gate has **849 collected
tests**; the final Linux and Windows CI run passed, with zero strict-mypy errors and zero Ruff
lint/format violations.

**Treasurer-summary Wave 1 foundation (Step 15) complete** — the optional `slides` extra now
contains a Windows-only, LPAC-isolated native-text PDF parser tested only with fictional fixtures.
Non-Windows hosts fail closed before a statement file is read. This is deliberately not yet an
operator-facing slide workflow: OCR, reconciliation, budget facts, review, Google Slides creation,
and private acceptance remain in the later Wave 1 steps.

The live `map-receipts --write-tab` path was revalidated on 2026-08-20 with a pre-write snapshot
and a semantic read-back reconciliation. Private mailbox counts, financial totals, and generated
reports remain outside this public repository.

Roadmap beyond v1: Apps Script automation (nag emails, calendar, sign-in), an admin web UI, then
forecasting / receipt automation / bank imports / wiki / live Drive upload (`google-api-python-client`).

## License

TBD.
