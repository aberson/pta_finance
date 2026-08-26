# Setup guide — connecting pta_finance to your Google Sheet

This walks a new operator from an empty checkout to a working toolkit talking to a live
Google Sheet. It is **generic** — it never names a specific organization. All of *your*
identity (org name, emails, spreadsheet IDs) lives only in `config.toml`, which is
gitignored. This repo is **public**; your data stays **private**.

The whole path is §0–§4 below (§5 is the analyze/report loop you run every month), plus the
optional §6, Gmail access:

```
0. Install            1. Google Cloud (M1)      2. config.toml (M2)
   uv sync               service account +          fill in your values
                         share the sheet
                                          3. init-sheet            4. check + run
                                             create report_log        verify + report
```

§6 (**Gmail access**) is a separate, **optional** setup — do it only when you want to pull
reimbursement-form emails with `fetch-mail`. Everything above works without it.

The live toolkit needs only the `report_log` tab plus a user-maintained **Budget Timeseries**
tab (the tidy long dataset `analyze` / `report` read). The older canonical tabs
(`transactions` / `receipts` / `budget` / `events`) are **optional/legacy** — you may leave
them out or delete them.

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

`config.example.toml` also carries a commented-out **`[gmail]`** block. It is genuinely optional —
leave it commented out unless you are setting up mail fetching, which is **§6 "Gmail access"**
below. An org that never wires up Gmail is unaffected.

---

## 3. Create the tab — `init-sheet`

The live toolkit provisions only the `report_log` tab (with its **exact** header). The
`init-sheet` command creates it if missing and writes its header row for you. It is
**idempotent** and **corruption-safe** — it never overwrites a tab that already has a
*different* non-empty header (it raises instead, so it can't clobber real data).

You also need a user-maintained **Budget Timeseries** tab (the tidy long dataset that
`analyze` / `report` read); create that tab yourself with your data — `init-sheet` does not
manage it. The older canonical tabs (`transactions` / `receipts` / `budget` / `events`) are
optional/legacy and are no longer created.

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

## 4. Verify the link, then run

**Verify** the live-required schema + that the Budget Timeseries source is readable + a real
read/write round-trip:

```bash
uv run pta-finance check
```

Expected: `schema OK for 1 required tab(s) [<your org>]`, a `Budget Timeseries source OK` line,
and (if `test_spreadsheet_id` is set) a round-trip `OK` line — it wrote, read back, and deleted a
probe row in `report_log` on the test sheet.

### Legacy data loads (optional)

The `normalize` and `import-budget` commands fill the older canonical tabs and are **legacy** —
superseded by the Budget Timeseries flow. Skip this section unless you are maintaining the
canonical-tab data.

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
  column) into the canonical tabs. Omit it to load only the budget.
- A line whose `Type` cell is blank is **kept but flagged `needs_review`** until you fill in the
  type and re-run — the import is idempotent, so re-running is safe.
- Per-transaction detail (beyond these summary actuals) is a separate, later load.

## 5. Analyze / report

```bash
uv run pta-finance analyze --fy 2026
uv run pta-finance report --fy 2026 --variant both   # omit --fy to target the current fiscal year
```

`analyze` and `report` source from the **Budget Timeseries** tab (a tidy long dataset), not the
canonical `budget` / `transactions` tabs, and `report` is FISCAL-YEAR scoped. Each `report` run
appends one row per variant to `report_log`. Reports are written to `reports/output/` (gitignored
— reports never enter the repo).

---

## 6. Gmail access (optional — only for `fetch-mail`)

Skip this stage entirely unless you want the toolkit to pull reimbursement-form emails out of a
mailbox for you. Everything above works without it.

**Why this needs its own credential.** The service account from M1 cannot read a personal Gmail
mailbox — impersonating a mailbox requires Google Workspace domain-wide delegation. So mail access
authenticates **as the human account** through OAuth. The grant is pinned to `gmail.readonly` in
code and guarded by a test: the toolkit can read mail and nothing else — it never sends, replies,
labels, archives, or deletes. You can revoke the grant at any time from your Google Account's
*Third-party apps & services* page.

> **Console labels drift.** Google renames and reshuffles this area of the Cloud Console often, and
> it is currently mid-rename (the OAuth/consent area is now presented as the "Google Auth
> Platform"). Treat every button name below as a description, not gospel — look for the equivalent
> control. The deep links are more stable than the menu paths, so prefer them.

1. **Enable the Gmail API.** In the **same** Cloud project you used for M1: APIs & Services →
   *Library* → search "Gmail API" → *Enable*.
2. **Configure the consent screen — do this first.** The clients page will just bounce you into
   this wizard otherwise. Go to `console.cloud.google.com/auth/overview` and work through it:
   - **App name:** anything generic — it is only shown to you on the consent screen. Do not put
     your organization's name here if you would rather it stayed out of a Google-side record.
   - **Do NOT upload a logo.** A logo triggers Google's app-verification requirement, which is a
     review process you do not need for a single-user tool.
   - **Audience:** *External* (the audience page is `console.cloud.google.com/auth/audience`).
   - **Leave the publishing status at *Testing*, and add your own Google account as a test user.**
     See "Testing mode" below for what that costs you — it is the right trade for this project.
   - **Scopes** (`console.cloud.google.com/auth/scopes`): nothing to pre-add. The toolkit requests
     exactly one scope at consent time, `.../auth/gmail.readonly`.
3. **Create the OAuth client.** Go to `console.cloud.google.com/auth/clients` → create a client.
   **Application type must be `Desktop app`** — the consent flow runs a loopback listener on your
   own machine, so a "Web application" client fails with a redirect-URI mismatch. Download the
   client-secrets JSON when it offers it (you can re-download it later from the same page).
4. **Save the client secret and fill in `[gmail]`.** Save the downloaded JSON to
   `secrets/gmail-client-secret.json` — that exact filename is what `config.example.toml`
   documents, and `secrets/` is gitignored so it never gets committed. Then uncomment and fill in
   the `[gmail]` block in your `config.toml`:

   ```toml
   [gmail]
   client_secrets_file = "secrets/gmail-client-secret.json"   # the JSON you just downloaded
   token_file          = "secrets/gmail-token.json"           # written for you at first consent
   inbox_dir           = "mail_samples"                       # fetched .eml files land here
   ```

   Leave `inbox_dir` pointing at the **same** directory as any `.mbox` archives you already have —
   that is load-bearing, and `docs/loading-receipts.md` explains why.
5. **First run — mint the token.** Run this once; it opens a browser:

   ```bash
   uv run pta-finance fetch-mail --since <date> --dry-run
   ```

   - Any recent date works — this run is only here to trigger consent.
   - Sign in with the account whose mailbox you want to read (the test user from step 2).
   - You will see an **"unverified app"** warning. That is expected here: `gmail.readonly` is a
     *restricted* scope and this app is deliberately unverified. Click through via the **Advanced**
     link, then the "go to `<your app name>` (unsafe)" link beneath it. **This click-through is
     safe only because it is the Desktop-app client you created yourself, minutes ago, in your own
     Cloud project** — check that the app name on screen is the one you typed in step 2. Never
     click past this warning on a consent screen you did not personally initiate: that is exactly
     what an OAuth phishing page looks like. It is not general advice.
   - Approve the **read-only** request.

**`--dry-run` still mints the token.** It writes no `.eml` files — it counts the matching messages
and stops — but the consent flow is *how the token file gets created*, so this is a stated contract,
not a bug. The later "run it for real" step depends on the token already existing.

The token lands at your configured `token_file`. **Never print it.** Check it with metadata only —
`Test-Path secrets\gmail-token.json` (PowerShell) or `ls -l secrets/gmail-token.json` (macOS/Linux)
— never `type` / `cat` / `Get-Content`.

### Testing mode: expect to re-approve about weekly

While the consent screen is in **Testing**, Google expires the refresh token **7 days** after
consent. A `fetch-mail` run after that opens the browser again for a fresh approval. **This is
expected behaviour, not a broken install** — the error message tells you to re-run the consent
command, and re-approving takes a few seconds.

Publishing to *Production* would remove the 7-day expiry, but Google requires a public homepage URL
and a hosted privacy-policy URL to do it — pages this project has no reason to host. Testing mode
is therefore the deliberate choice, and it costs little because mail fetching is **local-only by
design**: no OAuth token is ever placed in CI, and the runs are hands-on anyway. (Issue #18 tracks
the longevity check; the setup decision is recorded in the closed issue #17.)

**§6 done-checks:**

| Check | Expected |
|---|---|
| Gmail API | shows *Enabled* in the console for that project |
| OAuth client | exists, application type **Desktop app** |
| `secrets/gmail-client-secret.json` | the file exists; `git status` does **not** list it |
| `[gmail]` block | present in `config.toml` (not in `config.example.toml`) |
| First run | `fetch-mail --since <date> --dry-run` exits 0 and prints a message count |
| Token | the token file exists (metadata check above) — never print its contents |

---

## Loading reimbursement receipts

To turn the reimbursement-form emails in a treasurer mailbox into a **Reimbursements** ledger tab
(and the interactive **Receipts Explorer** dashboard), follow
**[docs/loading-receipts.md](docs/loading-receipts.md)** — `fetch-mail` → `map-receipts --write-tab`,
with a built-in **completeness check** so you don't miss any submissions.

That guide owns the load procedure and its two rules; both are repeated here only because getting
either wrong corrupts the ledger with **no error message at all**, and the full statement of each
is at the top of the guide:

- **Map in ONE run.** Point `map-receipts --source` at the whole `mail_samples/` *directory* once,
  so fetched `.eml` files and any `.mbox` archives dedup against each other. Two separate runs each
  look clean while together double-counting every message the sources share.
- **Use the configured fiscal year.** Standard `ingest-receipts` / `map-receipts` commands read
  `[fiscal_year] start_month` from `config.toml`. Use `--start-month N` only as an intentional
  one-run override; an explicit value wins over config.

*Historical:* before `fetch-mail`, loads were done with a manual Gmail label → **Google Takeout**
`.mbox` export. Existing Takeout archives remain valid input for backfill — keep them in
`mail_samples/` — but they are no longer the procedure for a new load.

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
| `fetch-mail: config.toml has no [gmail] section` | §6 step 4 — uncomment and fill in the `[gmail]` block |
| `fetch-mail: no Gmail OAuth client-secrets file at …` | §6 step 4 — the downloaded client JSON isn't where `client_secrets_file` points |
| `fetch-mail` opens the browser again a week later | expected in Testing mode (7-day refresh-token expiry) — just re-approve |
| Consent shows "Google hasn't verified this app" | expected **for the client you created yourself in §6** — read §6 step 5 before clicking through |
| Consent fails with a redirect-URI mismatch | the OAuth client isn't type **Desktop app** (§6 step 3) — create a Desktop-app client |

For the full architecture and command reference, see `README.md` and `plan.md`.
