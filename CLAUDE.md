# pta_finance — project context

> **Identity rule (load-bearing).** This is a **generic, reusable finance toolkit** for a
> PTA / booster club / small nonprofit. **Never hard-code any organization, school, person,
> or email** in committed code, docs, tests, fixtures, or examples. All identity lives only in
> the private, gitignored `config.toml`. Tests/examples use fake placeholders (`Example PTA`,
> `treasurer@example.org`). The repo is **public**; the data is **private**.

## 1. Overview

A command-line Python toolkit that treats a **Google Sheet as the system-of-record database**
for a small org's finances: it normalizes a messy multi-year ledger into a clean schema, runs an
analytics engine (spend by category/grade, budget-vs-actual, multi-year trends), and generates
**pure-template monthly reports** in an internal (full) and external (public-safe) variant. v1 has
**no web UI, no LLM, no Apps Script** — a local CLI plus a GitHub Actions monthly cron.

## 2. Stack

| Layer | Tool |
|---|---|
| Language | Python `>=3.12` |
| Build / deps | `uv` + `hatchling` |
| Sheets/Drive | `gspread` 6.x + `google-auth` (service account) |
| Analytics | `pandas` |
| Charts | `matplotlib` (Agg backend) |
| Templating | `Jinja2`; optional `[pdf]` → `WeasyPrint` |
| CLI / config | stdlib `argparse` / `tomllib` |
| Scheduler | GitHub Actions cron (`0 9 1 * *`) + `workflow_dispatch` |
| Lint / type / test | `ruff`, `mypy --strict`, `pytest` |

## 3. Key commands

```bash
uv sync --extra dev                 # install (add [pdf] for WeasyPrint)
uv run pytest -q                    # test
uv run ruff check .                 # lint
uv run ruff format --check .        # format check
uv run mypy --strict pta_finance    # typecheck

uv run pta-finance check                                  # validate config + sheet round-trip
uv run pta-finance normalize                              # legacy → canonical schema
uv run pta-finance analyze                                # run analytics
uv run pta-finance report --month YYYY-MM --variant both  # generate reports
```

## 4. Directory layout

```
pta_finance/        package (flat layout): config, ids, schema, models, sheets,
                    backup, etl, cli, analytics/, reports/(templates/)
tests/              fake-org fixtures + mocked gspread; test_smoke_pipeline.py is the wiring gate
.github/            last-run.txt (scheduler keepalive) + workflows/ci.yml (PR gate)
                    + workflows/monthly-report.yml (cron)
secrets/            gitignored — service-account.json
snapshots/          gitignored — CSV backups
config.toml         gitignored private config; config.example.toml ships fake values
```

## 5. Architecture

- **Data layer** (`sheets.py`, `schema.py`, `models.py`, `ids.py`): one Google Spreadsheet, one
  tab per entity (`transactions`, `receipts`, `budget`, `events`, `report_log`). `schema.py`
  (column lists) and `ids.py` (ID formats) are **single sources of truth** — tests assert column
  identity with `is`. IDs are stable, human-readable, fiscal-year-scoped (`TXN-FY26-0001`); Python
  assigns missing IDs and never rewrites existing ones.
- **ETL** (`etl.py`): normalize legacy rows, assign IDs, dedup via `(date|amount|payee)` hash,
  flag ambiguous rows `needs_review`, snapshot-before-write. Idempotent.
- **Analytics** (`analytics/`): pandas aggregations + multi-year trends.
- **Reports** (`reports/`): builder computes a data model → Jinja2 renders internal + external
  variants (matplotlib charts; optional WeasyPrint PDF). **Reports are never committed to the
  repo** — they go to `reports/output/` + a private Drive folder + an ephemeral CI artifact.
- **Access:** a Google **service account** (Sheet + Drive folder shared with its email). Its JSON
  key is the only secret — gitignored locally, base64 GitHub Actions secret in CI, decoded to a
  file without echoing.

## 6. Current state

**Plan written, no code yet.** Build via `/build-phase` against `plan.md` (8 automated steps +
M1–M3 manual). Update this file at the end of each phase via `/repo-update`.

## 7. Environment requirements

- Windows 11 + Python `>=3.12`; `uv` on PATH. No `pip` (uv-managed).
- A Google account with a Cloud project (Sheets API + Drive API enabled) and a service account
  whose JSON key sits at `secrets/service-account.json`.
- The target spreadsheet + Drive folders shared with the service-account email (Editor).
- Optional `[pdf]` extra needs WeasyPrint's Pango/Cairo native libs (heavy on Windows — PDF is
  optional; Markdown + HTML are the primary outputs).
- GitHub repo secrets for CI: `GOOGLE_SA_KEY_B64`, `PTA_CONFIG_B64`.
- **Scheduled-workflow keepalive.** GitHub disables scheduled workflows in **public** repos after
  60 days of no repository activity (the monthly cron firing does **not** count). `monthly-report.yml`
  pushes a `.github/last-run.txt` timestamp each run to reset that timer. If the workflow ever shows
  as disabled, re-enable it one-click under the repo's **Actions** tab → the workflow → "Enable
  workflow", or push any commit.
