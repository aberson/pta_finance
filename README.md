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

## Stack

| Layer | Tool | Why |
|---|---|---|
| Language / runtime | Python `>=3.12` | `tomllib` in stdlib (no TOML dependency) |
| Dependency / build | `uv` + `hatchling` | Reproducible, fast |
| Sheets / Drive access | `gspread` + `google-auth` (service account) | Clean API, atomic batch writes |
| Analytics | `pandas` | By-category / grade / month aggregation, trends |
| Charts | `matplotlib` (Agg backend) | Deterministic, headless, zero-browser in CI |
| Templating | `Jinja2` (+ optional `WeasyPrint` for PDF) | Two report variants; HTML output, PDF optional |
| CLI / config | stdlib `argparse` / `tomllib` | No extra dependency |
| Scheduler | GitHub Actions cron | Free, cloud-hosted monthly run |
| Lint / type / test | `ruff`, `mypy --strict`, `pytest` | — |

## Prerequisites

- Python `>=3.12` and [`uv`](https://docs.astral.sh/uv/) on your PATH.
- A Google account with a Cloud project (Sheets API + Drive API enabled) and a **service account**
  whose JSON key you can download.
- The target spreadsheet and a Drive folder shared with the service-account email (Editor role).

## Setup

```bash
# 1. Install
uv sync --extra dev            # add the [pdf] extra if you want WeasyPrint PDF output

# 2. Configure (private, gitignored)
cp config.example.toml config.toml
#    fill in: org/school name + email, board emails, spreadsheet_id,
#    drive folder ids, grade labels, fiscal_year.start_month (1 = calendar year)

# 3. Google service account (one-time)
#    download the service-account JSON to secrets/service-account.json
#    share the spreadsheet + Drive folder with the service-account email (Editor)

# 4. Verify, then run
uv run pta-finance check                                  # validate config + sheet round-trip
uv run pta-finance normalize                              # legacy ledger -> clean schema
uv run pta-finance analyze                                # run analytics
uv run pta-finance report --month YYYY-MM --variant both  # generate reports
```

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
GitHub Actions report workflow. 113 tests passing (+1 skipped), 0 type errors (`mypy --strict`),
0 lint violations. First-run setup needs the Google service account (see Setup) — then
`uv run pta-finance check`.

Roadmap beyond v1: Apps Script automation (nag emails, calendar, sign-in), an admin web UI, then
forecasting / receipt ingestion / bank imports / wiki / live Drive upload (`google-api-python-client`).

## License

TBD.
