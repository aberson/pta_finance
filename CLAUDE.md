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
| Gmail (optional) | `google-api-python-client` + `google-auth-oauthlib` (user OAuth, `gmail.readonly`) |
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
```

No live-Sheet write. Each comment names any local file the command touches:

```bash
uv run pta-finance analyze                                # reads Budget Timeseries; no writes
uv run pta-finance snapshot                               # CSV backups under snapshots/<utc>/
uv run pta-finance sync-budget --fy 2027                  # dry run: prints the diff, no writes
uv run pta-finance fetch-mail --since <date> --dry-run    # counts; MAY mint secrets/gmail-token.json
uv run pta-finance ingest-receipts --source mail_samples --profile --originals-only --start-month 7  # no writes
uv run pta-finance map-receipts --source mail_samples --start-month 7                                # no writes
```

These write. Each comment names exactly what:

```bash
uv run pta-finance check                                  # writes+deletes a probe row (test sheet)
uv run pta-finance normalize                              # LEGACY: writes the live transactions tab
uv run pta-finance report --fy YYYY --variant both        # HTML + 1 report_log row per variant
uv run pta-finance sync-budget --fy 2027 --apply          # writes amount/notes to Budget Timeseries
uv run pta-finance fetch-mail --since <date>              # .eml into [gmail] inbox_dir; no Sheet
uv run pta-finance map-receipts --source mail_samples --start-month 7 --write-tab Reimbursements  # replaces it
```

Every writing verb above snapshots first, except `check` (it deletes its own probe row) and
`fetch-mail` (it writes no Sheet). Both receipt-pipeline rules are load-bearing and fail
silently: `--start-month` defaults to `1`, and `map-receipts` must cover `mail_samples/` in ONE
run. Both are stated in full — and owned — by [docs/loading-receipts.md](docs/loading-receipts.md).

## 4. Directory layout

```
pta_finance/        package (flat layout): config, ids, schema, models, sheets,
                    backup, etl, cli, gmail_source (the ONLY Gmail surface: pinned
                    read-only OAuth + query/list/fetch + deterministic .eml writer),
                    receipt_ingest (.eml/.mbox parser + profiler),
                    receipt_map (Submission → flat "Reimbursements" ledger rows),
                    budget_sync (editable-budget-tab → Budget Timeseries reconcile),
                    report_source (Budget Timeseries → report/analyze inputs),
                    analytics/, reports/(templates/)
tests/              fake-org fixtures + mocked gspread; test_smoke_pipeline.py is the wiring gate
.github/            last-run.txt (scheduler keepalive) + workflows/ci.yml (PR gate)
                    + workflows/monthly-report.yml (cron — reports only, no mail)
secrets/            gitignored — service-account.json, gmail-client-secret.json,
                    gmail-token.json (minted at first consent; never printed)
mail_samples/       gitignored — fetched .eml + legacy Takeout .mbox, side by side (flat)
snapshots/          gitignored — CSV backups
config.toml         gitignored private config; config.example.toml ships fake values
                    (incl. the commented-out, optional [gmail] block)
documentation/      committed feature plans (e.g. gmail-ingest-plan.md)
```

## 5. Architecture

- **Data layer** (`sheets.py`, `schema.py`, `models.py`, `ids.py`): one Google Spreadsheet.
  `schema.py` (column lists) and `ids.py` (ID formats) are **single sources of truth** — tests
  assert column identity with `is`. The full `schema.TABS` registry (`transactions`, `receipts`,
  `budget`, `events`, `report_log`) remains the column-shape source of truth, but the LIVE toolkit
  provisions/validates only `schema.REQUIRED_TABS` (just `report_log`) via `check` / `init-sheet`
  / `snapshot`. `report` / `analyze` source from the operator-maintained **Budget Timeseries** tab
  (`report_source.py`) and `report` writes one row per run to `report_log`; the other canonical
  tabs (filled by the legacy `normalize` / `import-budget`) are optional and may be deleted. IDs
  are stable, human-readable, fiscal-year-scoped (`TXN-FY26-0001`); Python assigns missing IDs and
  never rewrites existing ones.
- **Editable budget ↔ DB** (`budget_sync.py`): the operator hand-edits a readable **"FY&lt;fy&gt; Budget"**
  tab (styled after the hidden "Budget Share" tab); `sync-budget` reconciles those edits back into
  the Budget Timeseries. PURE `parse_budget_tab` + `plan_budget_sync` (matches `(type, raw_category)`
  within `(fy, proposed)`), CLI orchestrates. Default dry-run diff; `--apply` snapshots first
  (`backup.snapshot_raw_tab`, faithful full grid) then writes ONLY changed amount/notes cells +
  appends new lines via schema-independent `SheetsClient.update_cells` / `append_raw_rows`. Never
  touches actuals, other years, or enrichment columns; removed lines are flagged, never deleted.
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

**v1 automated build COMPLETE (Steps 1–8, issues #1–#8 closed).** The full pipeline works end-to-end
under test: Sheets client, ETL/normalize, analytics, internal/external reports (runtime PII guard),
smoke gate, and the monthly GitHub Actions workflow. 332 tests + 1 skipped; `mypy --strict` + ruff
clean. **Phase-4 receipt ingestion has shipped end-to-end:** `receipt_ingest.py` (`.eml`/`.mbox`
parser + PII-free batch `Profile` + `Re:`/`Fwd:` dedup) + `receipt_map.py` (`Submission` → flat ledger
rows) drive the `ingest-receipts` (preview / `--profile`) and `map-receipts` (`--write-tab`) CLIs,
which land receipts in a flat **Reimbursements** Sheet tab (via `SheetsClient.replace_tab_grid`) that a
dropdown-driven **Receipts Explorer** dashboard reads; operator load guide in `docs/loading-receipts.md`.
**The Gmail read-only ingest connector has also shipped** (`documentation/gmail-ingest-plan.md`,
issues #15–#22): `gmail_source.py` + the `fetch-mail` CLI replace the manual Google Takeout export —
user OAuth pinned to `gmail.readonly` (exact-equality-tested, re-checked at runtime), a date-scoped
`after:`/`before:` query (no sender/subject filter, by design), and an idempotent `.eml` writer whose
filename is `<sanitised Message-ID stem>-<8 hex of sha256(full raw message bytes)>.eml` — the hash is
over the **whole message**, never over the Message-ID (amended 2026-08-25 after five extraction/
truncation collision vectors; the Message-ID supplies the readable stem only). Files land **beside**
the `.mbox` archives in `mail_samples/` so ONE `map-receipts` run dedups both
(`documentation/gmail-ingest-plan.md` § Design Decision 10). `fetch-mail` prints counts only — never
a subject, sender, or message id. **The cron half is deliberately NOT built:** no OAuth token goes
into CI (a personal-mailbox refresh token in a public repo's Actions secrets would expose the whole
inbox), so fetching is local-only and hands-on and the monthly workflow still does reports only
(§ Design Decision 7). OAuth consent stays in **Testing** mode with a test user — publishing to
Production would need a homepage + privacy-policy URL this project has no reason to host — so the
refresh token expires ~7 days after consent and a browser re-approval is **expected behaviour**, not
a bug (issue #18 tracks the longevity check). Known gap, still open with no tracker of its own:
submissions sent only to a *second* mailbox during a role handover are not in the fetched mail and
cannot be recovered by re-fetching — a thin month is the signal that a second, label-scoped export
is needed (found during the backfill, closed issue #20; `docs/loading-receipts.md` carries the
operator handling). Remaining: Budget Timeseries roll-up; live Drive fetch of linked receipt PDFs.
A **`sync-budget` command** (`budget_sync.py`) also landed: it reconciles an editable, operator-
maintained **"FY&lt;fy&gt; Budget"** Sheet tab back into the Budget Timeseries (dry-run diff by default;
`--apply` snapshots first, then writes only changed amount/notes cells + appends new lines; never
touches actuals/other-years/enrichment; removed lines flagged not deleted). **Google credentials are configured + working** — `secrets/service-account.json` (gitignored) +
`config.toml`; `pta-finance check` round-trips read+write against the live Sheet (M1 service-account
setup + M2 real-sheet smoke are DONE). **Next = operator-gated observation:** M3 monthly-report run
(plan §11 Manual Steps). Live Drive upload is deferred to Phase 2 (`google-api-python-client`).

## 7. Environment requirements

- Windows 11 + Python `>=3.12`; `uv` on PATH. No `pip` (uv-managed).
- A Google account with a Cloud project (Sheets API + Drive API enabled) and a service account
  whose JSON key sits at `secrets/service-account.json`.
- The target spreadsheet + Drive folders shared with the service-account email (Editor).
- **For `fetch-mail` only (optional):** the Gmail API enabled in that same Cloud project, plus an
  OAuth **Desktop app** client whose JSON sits at `secrets/gmail-client-secret.json`, and a
  `[gmail]` block in `config.toml`. The service account is unusable here — reading a personal
  mailbox needs user OAuth (Workspace domain-wide delegation is the only alternative). First run
  opens a browser for a one-time read-only consent; `--dry-run` still mints the token, by contract.
  Consent shows an "unverified app" warning (`gmail.readonly` is a restricted scope) — clear it via
  Advanced → "go to … (unsafe)", but ONLY for the Desktop-app client the operator created themselves
  in their own Cloud project (SETUP.md §6 step 5); this is never general advice for a consent screen
  they did not initiate. Console labels drift: prefer the deep links
  (`console.cloud.google.com/auth/overview`, `/auth/clients`, `/auth/scopes`, `/auth/audience`), and
  do not upload a logo (it triggers app verification).
- **Never print, `cat`, or `Get-Content`** `secrets/gmail-token.json` or
  `secrets/gmail-client-secret.json`.
  Metadata checks (`Test-Path`, size) and effect-based verification (run `fetch-mail --dry-run`,
  check the exit code) only — the token file holds a live refresh token.
- **Gmail consent stays in Testing mode with a test user, by decision.** Production would require a
  homepage + privacy-policy URL this project has no reason to host, so the refresh token expires
  ~7 days after consent and hands-on runs need a browser re-approval roughly weekly — expected, not
  a fault. Mail fetching is **local-only by design**: there is **no Gmail credential in CI** and the
  monthly workflow does reports only.
- Optional `[pdf]` extra needs WeasyPrint's Pango/Cairo native libs (heavy on Windows — PDF is
  optional; Markdown + HTML are the primary outputs).
- GitHub repo secrets for CI: `GOOGLE_SA_KEY_B64`, `PTA_CONFIG_B64`.
- **Scheduled-workflow keepalive.** GitHub disables scheduled workflows in **public** repos after
  60 days of no repository activity (the monthly cron firing does **not** count). `monthly-report.yml`
  pushes a `.github/last-run.txt` timestamp each run to reset that timer. If the workflow ever shows
  as disabled, re-enable it one-click under the repo's **Actions** tab → the workflow → "Enable
  workflow", or push any commit.
