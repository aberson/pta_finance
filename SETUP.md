# Setup guide — connecting pta_finance to your Google Sheet

This walks a new operator from an empty checkout to a working toolkit talking to a live
Google Sheet. It is **generic** — it never names a specific organization. All of *your*
identity (org name, emails, spreadsheet IDs) lives only in `config.toml`, which is
gitignored. This repo is **public**; your data stays **private**.

The whole path is five stages:

```
0. Install            1. Google Cloud (M1)      2. config.toml (M2)
   uv sync               service account +          fill in your values
                         share the sheet
                                          3. init-sheet            4. check + load data
                                             create the 5 tabs        verify + ingest
```

---

## 0. Install

Requirements: Windows/macOS/Linux, **Python ≥ 3.12**, and [`uv`](https://docs.astral.sh/uv/)
on your PATH. From the repo root:

```bash
uv sync --extra dev
```

(Add `--extra pdf` later only if you want WeasyPrint PDF output — its native libs are heavy
on Windows and optional; HTML is the primary format.)

---

## 1. Google Cloud + service account (Manual Step M1)

The toolkit reads/writes your Sheet as a **service account** — a robot Google identity with
its own email. You create it once in the browser, download its key, and share your Sheet with
its email. No human login is ever needed after that (this is what lets the monthly GitHub
Actions cron run unattended).

In the [Google Cloud Console](https://console.cloud.google.com/):

1. **Create or select a project** (top bar → project picker → *New Project*).
2. **Enable two APIs.** APIs & Services → *Library* → enable **Google Sheets API** and
   **Google Drive API** (Drive is needed even in v1 because `gspread` opens the file by key
   through Drive).
3. **Create the service account.** APIs & Services → *Credentials* → *Create credentials* →
   *Service account*. Give it any name (e.g. `pta-finance-bot`); no roles/grants are needed.
4. **Make a key.** Open the new service account → *Keys* → *Add key* → *Create new key* →
   **JSON** → *Create*. A `.json` file downloads.
5. **Save the key** to `secrets/service-account.json` in this repo (the `secrets/` folder is
   gitignored — the key never gets committed). Create the folder if it doesn't exist.
6. **Copy the robot's email.** Open the JSON and copy the `"client_email"` value — it looks
   like `pta-finance-bot@your-project.iam.gserviceaccount.com`.
7. **Share your Sheet with that email.** Open your Google Spreadsheet → *Share* → paste the
   `client_email` → role **Editor** → *Send*. (Uncheck "notify" — it's a robot.)
8. *(Optional but recommended)* Make a throwaway **test sheet**, share it with the same email
   as Editor, and note its ID — the `check` command round-trips a probe row there instead of
   on your production file. See "Sheet IDs" below for how to read an ID from the URL.

**M1 done-checks:**

| Check | Expected |
|---|---|
| Sheets API + Drive API | both show *Enabled* in the console |
| `secrets/service-account.json` | the file exists; `git status` does **not** list it |
| Sharing | your spreadsheet (and the test sheet) list the `client_email` as **Editor** |

---

## 2. config.toml (Manual Step M2)

Your private values live here. Start from the template:

```bash
cp config.example.toml config.toml
```

Then edit `config.toml`. **Sheet IDs** come from the URL — in
`https://docs.google.com/spreadsheets/d/`**`THIS_IS_THE_ID`**`/edit#gid=0`, the long token
between `/d/` and `/edit` is the `spreadsheet_id`.

| Field | What to put |
|---|---|
| `[organization] name` | your PTA / booster name |
| `[organization] school_name` | the school name |
| `[organization] school_email` | the school's front-office email |
| `[contacts] president` / `treasurer` / `cfo` | the role emails (president & account_holders are lists) |
| `[contacts] account_holders` | everyone who should later be allowed in (Phase-3 allowlist) |
| `[fiscal_year] start_month` | `1` for a calendar-year fiscal period (Jan–Dec) |
| `[grades] labels` | your grade range, e.g. `["K","1","2","3","4","5"]` |
| `[sheets] spreadsheet_id` | the production Sheet's ID (from its URL) |
| `[sheets] test_spreadsheet_id` | the throwaway test sheet's ID (or reuse `spreadsheet_id` to skip making one) |
| `[sheets] drive_receipts_folder_id` / `drive_reports_folder_id` | **Phase 2** — any non-empty placeholder is fine for v1 (unused) |
| `[google] service_account_file` | leave as `secrets/service-account.json` |

Every field must be non-empty or `pta-finance` will fail fast naming the missing field. The
two Drive folder IDs are not used in v1 (live Drive upload is Phase 2) — a placeholder string
satisfies validation.

---

## 3. Create the tabs — `init-sheet`

The toolkit expects five worksheet tabs with **exact** headers: `transactions`, `receipts`,
`budget`, `events`, `report_log`. The `init-sheet` command creates any that are missing and
writes their header rows for you. It is **idempotent** and **corruption-safe** — it never
overwrites a tab that already has a *different* non-empty header (it raises instead, so it
can't clobber real data).

Preview first (no writes), then apply:

```bash
uv run pta-finance init-sheet --dry-run
```

```bash
uv run pta-finance init-sheet
```

If you made a separate test sheet, bootstrap it too:

```bash
uv run pta-finance init-sheet --target test
```

You'll see one line per tab (`created` / `headers-written` / `ok`) and a summary.

---

## 4. Verify the link, then load data

**Verify** config + schema + a real read/write round-trip:

```bash
uv run pta-finance check
```

Expected: `schema OK for 5 tab(s) [<your org>]` and (if `test_spreadsheet_id` is set) a
round-trip `OK` line — it wrote, read back, and deleted a probe row on the test sheet.

**Load your ledger** (normalizes legacy rows, assigns IDs, dedups; snapshots first):

```bash
uv run pta-finance normalize
```

**Load your budget** with `import-budget`, pointed at the worksheet that holds it. It tolerates a
messy human layout (`Type` / `Line Item` / `Proposed` / `Actual` columns, with subtotal/total rows
mixed in). Preview first — this reads only and writes nothing:

```bash
uv run pta-finance import-budget --from-tab "<your budget tab>" --fy <YYYY> --with-actuals --dry-run
```

Then the real load (snapshots every tab first, then idempotent upsert by ID):

```bash
uv run pta-finance import-budget --from-tab "<your budget tab>" --fy <YYYY> --with-actuals
```

- `--fy` is the fiscal-year **label** (e.g. `2026` = the 2025–2026 year when `start_month` is a
  school-year month).
- `--with-actuals` also writes one summary "actual" transaction per line item (from the `Actual`
  column) so `analyze`/`report` show real spend, not just the budget. Omit it to load only the budget.
- A line whose `Type` cell is blank is **kept but flagged `needs_review`** (and excluded from analytics)
  until you fill in the type and re-run — the import is idempotent, so re-running is safe.
- Per-transaction detail (beyond these summary actuals) is a separate, later load.

**Then analyze / report:**

```bash
uv run pta-finance analyze --fy 2026
uv run pta-finance report --month 2026-06 --variant both
```

Reports are written to `reports/output/` (gitignored — reports never enter the repo).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ConfigError: missing or invalid required config field: X` | fill field `X` in `config.toml` (every field must be non-empty) |
| `FileNotFoundError: secrets/service-account.json` | M1 step 5 — the key isn't where `service_account_file` points |
| `gspread ... PermissionError` / 403 | the `client_email` isn't shared on that Sheet as Editor (M1 step 7) |
| `SpreadsheetNotFound` | wrong `spreadsheet_id`, or the sheet isn't shared with the service account |
| `SchemaError: schema mismatch on tab 'X'` | that tab's header row doesn't match the canonical columns — fix the header, or (for an empty/new tab) run `init-sheet` |
| HTTP 429 during a big import | normal under load — the client retries with backoff automatically |

For the full architecture and command reference, see `README.md` and `plan.md`.
