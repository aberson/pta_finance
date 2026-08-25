# Gmail read-only ingest connector — feature plan

> **Identity rule.** This document is committed to a **public** repo. No organization, school,
> person, or email address appears here. The target mailbox, OAuth client, and token paths are
> all values in the private, gitignored `config.toml`.

**Umbrella issue:** [#22](https://github.com/aberson/pta_finance/issues/22) · step issues #15–#21

## 1. What This Feature Does

Replaces the manual *Gmail search → Google Takeout → `.mbox`* export with a
`pta-finance fetch-mail --since YYYY-MM-DD` command that pulls messages straight from the
treasurer mailbox over the Gmail API and writes them as `.eml` files into a gitignored inbox
directory. The existing `ingest-receipts` / `map-receipts` pipeline then runs over that directory
unchanged.

The first real use is a **backfill from the board cut-over date (2026-06-01)** rather than a
short incremental window, so the ledger has complete data for the whole of the current board's
term in one authoritative pass.

It is being built because the current loop is entirely manual: every triage round begins with a
human running a Gmail search, requesting a Takeout archive, waiting for it, and downloading a
multi-hundred-megabyte `.mbox`. That is slow, it is easy to get the date window wrong, and the
export timestamp becomes a silent evidence horizon — the last round's export was taken mid-afternoon,
so any submission that arrived later the same day was invisible to the review. Closing this gap is
already named as remaining Phase-4 work in [`plan.md`](../plan.md) §"Phase 4 → Not yet built":
*"Monthly automation — Gmail OAuth so the cron ingests new reimbursements without a manual Takeout."*

Access is **read-only by design and by operator requirement**. The toolkit never sends, modifies,
labels, or deletes mail; correspondence stays a human action.

## 2. Existing Context

| Thing | Where | Why it matters here |
|---|---|---|
| Email parser | `pta_finance/receipt_ingest.py` | `iter_source()` (~L436) already dispatches: a `.mbox` file → `iter_mbox`; a **directory** → every `*.eml` then every `*.mbox` inside it; anything else → a single `.eml`. A fetcher that drops `.eml` files into a directory needs **zero** downstream change. |
| Ledger mapper | `pta_finance/receipt_map.py` | Dedups on `Message-ID` + content hash (L187-199), so re-fetching an overlapping window cannot double-count — **but the `seen_ids`/`seen_hashes` sets live inside a single `map_submissions()` call**, so this only holds when every source is mapped in one run. See Design Decision 10. |
| Config loader | `pta_finance/config.py` | Frozen dataclasses. `Config` and `Google` are constructed in exactly **one** place (L190/L195); every other reference is a `load_config()` call. `_section()` (L95) **raises** on a missing section. |
| CLI | `pta_finance/cli.py` | `argparse` subparsers; each command is `sub.add_parser(...)` + `set_defaults(func=_cmd_x)`. `ingest-receipts` (L1047) is the closest sibling and the shape to match. |
| Secret posture | `.gitignore`, `.github/workflows/monthly-report.yml` | `secrets/` and `mail_samples/` are gitignored, and `*.eml` / `*.mbox` are gitignored globally — two independent rules cover both new paths. CI decodes base64 secrets to a file without echoing (L49). |
| Auth today | `secrets/service-account.json` | A **service account**, used for Sheets/Drive. It cannot read the mailbox (see Design Decision 1). |
| Existing archives | `mail_samples/*.mbox` (gitignored) | One archive covers submissions from early in the prior fiscal year; the other was exported with an `after:` search covering **2026-04-30 onward**. A backfill from 2026-06-01 therefore **overlaps the existing archives by roughly eleven weeks** — dedup is mandatory, not optional. |
| Board handoff window | Domain fact, 2026-06-01 → 2026-06-30 | The board changed over on **2026-06-01**, but the **outgoing treasurer (now CFO) continued accepting reimbursement submissions until 2026-06-30**. June is a dual-intake month: the same request may have been sent to either role, or to both. |

## 3. Scope

**In**

- A `gmail_source.py` module: OAuth credential load/refresh, a pinned read-only scope, message
  listing by date query, and raw-message download.
- A `fetch-mail` CLI command writing `.eml` files to a gitignored directory, idempotently.
- An optional `[gmail]` config section.
- Unit tests against a faked Gmail service, including a scope-pinning regression test.
- An operator step for the one-time Google Cloud / OAuth-consent setup.
- An end-to-end smoke gate over a real, small date window.
- A **full backfill from 2026-06-01** with reconciliation against the existing ledger.
- Cross-source dedup so the fetched `.eml` files and the existing `.mbox` archives cannot
  double-count the eleven weeks they share.
- Docs: `SETUP.md`, `docs/loading-receipts.md`, `CLAUDE.md` §6.

**Out**

- **Sending, replying, labelling, archiving, or deleting mail.** Read-only, permanently.
- **CI / cron integration.** Operator chose local-only; no OAuth token is stored in GitHub
  secrets in this phase. Revisit as a separate feature if the cadence ever justifies it.
- **Attachment/Drive download.** Linked receipt PDFs remain deferred Phase-4 work.
- **Budget Timeseries roll-up.** Separate remaining Phase-4 item.
- **Retiring the `.mbox` path.** `iter_source()` keeps reading Takeout archives; the historical
  export stays valid and this is purely additive.

## 4. Impact Analysis

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `pta_finance/gmail_source.py` | create | New OAuth client + fetch logic | glob confirmed absent; `grep -rniE "gmail\|oauth\|imap\|refresh_token" --include=*.py pta_finance tests` → 0 hits outside `.venv` |
| `pta_finance/config.py` | modify | Add optional `Gmail` dataclass + wire into `Config` | grep'd all `Config(`/`Google(`/`load_config(` sites: **10 total** — 2 constructions, both in `config.py` (L190, L195); 8 are `load_config()` calls (`cli.py:50`, `tests/conftest.py:56`, `tests/test_config.py:49,82,91,98,108`, `tests/test_reports.py:66`). Optional section ⇒ **no caller or fixture changes** |
| `pta_finance/cli.py` | extend | New `fetch-mail` subparser + `_cmd_fetch_mail` | grep'd `add_parser` → 10 existing commands; additive, no existing parser touched |
| `pyproject.toml` | modify | Add `google-api-python-client`, `google-auth-oauthlib` | read deps (L8-14); `[[tool.mypy.overrides]]` already lists `google.*` under `ignore_missing_imports`, so **no new mypy override needed** |
| `config.example.toml` | modify | Document the `[gmail]` block with fake placeholders | read in full; commented-out `[llm]` block is the precedent for an optional section |
| `tests/test_gmail_source.py` | create | Scope guard, query building, idempotent write, dead-token error | glob confirmed absent; suite is 18 files / 218 test functions |
| `SETUP.md`, `docs/loading-receipts.md` | modify | Replace the Takeout procedure with the fetch procedure | `loading-receipts.md` is the operator load guide referenced from `plan.md` |
| `CLAUDE.md` | modify | §3 command list, §4 layout, §6 current state | §6 currently says Gmail-OAuth cron is remaining |
| `plan.md` | modify | Tick the Phase-4 "Monthly automation" bullet | grep'd: L666 (`- **Monthly automation** ...`) |

**No function signature, schema field, or shared constant changes.** The feature is additive: no
existing call site changes behaviour. The only shared-shape edit is `Config`, and making `[gmail]`
optional keeps every existing construction and fixture valid.

## 5. New Components

- **`pta_finance/gmail_source.py`** — the whole Gmail surface, isolated so nothing else imports
  `googleapiclient`:
  - `SCOPES: Final[tuple[str, ...]]` — the single source of truth for OAuth scope.
  - `GmailAuthError(Exception)` — actionable failures (missing client secrets, dead refresh token,
    consent not granted).
  - `load_credentials(cfg) -> Credentials` — loads the token file, refreshes if stale, raises
    `GmailAuthError` with a remediation sentence if the refresh token is dead.
  - `build_query(since, until=None, extra=None) -> str` — composes the Gmail search string.
  - `list_message_ids(service, query) -> Iterator[str]` — paginates `users.messages.list`.
  - `fetch_raw(service, message_id) -> bytes` — `format="raw"`, base64url-decoded to RFC-822 bytes.
  - `write_eml(raw, out_dir, message_id) -> Path` — deterministic filename, skip-if-identical.

  **Filename rule (this is the on-disk idempotency key — pin it once, never vary it):**
  strip a surrounding `<`/`>` from the Message-ID; replace every character outside
  `[A-Za-z0-9._-]` with `_`; truncate to 80 characters; then append `-` plus the first 8 lowercase
  hex characters of `sha256(<original Message-ID>)`, and the `.eml` suffix. The hash suffix is what
  makes the rule collision-safe after sanitisation and truncation, and on case-insensitive
  filesystems where two Message-IDs differing only in case would otherwise collide. A message with
  **no** Message-ID header uses `nomsgid-` plus the first 16 hex characters of `sha256(<raw bytes>)`.
  Changing this rule later re-downloads every message under new names and breaks
  skip-if-identical — treat it as a shared key shape, not an implementation detail.
- **`Gmail` config dataclass** (in `config.py`) — `client_secrets_file`, `token_file`,
  `inbox_dir`, plus resolved `Path` properties, mirroring how `Google` exposes
  `service_account_path`. The `config.example.toml` block it parses (fake placeholders only,
  per the identity rule):

  ```toml
  # [gmail]                                    # optional - omit entirely if unused
  # client_secrets_file = "secrets/gmail-client-secret.json"   # OAuth Desktop-app client
  # token_file          = "secrets/gmail-token.json"           # written at first consent
  # inbox_dir           = "mail_samples"                       # fetched .eml land here
  ```

  `Gmail` shape:

  | field | type | note |
  |---|---|---|
  | `client_secrets_file` | `str` | repo-relative path to the OAuth client JSON; gitignored |
  | `token_file` | `str` | repo-relative path to the minted token; gitignored, never printed |
  | `inbox_dir` | `str` | where `.eml` files are written; **must** sit beside the `.mbox` archives (Design Decision 10) |
  | `client_secrets_path` | `Path` | resolved property, mirrors `Google.service_account_path` |
  | `token_path` | `Path` | resolved property |
  | `inbox_path` | `Path` | resolved property |
- **`fetch-mail` CLI command** — `--since` (required), `--until`, `--query`, `--out`, `--limit`,
  `--dry-run`. `--out` defaults to the existing `mail_samples/` directory rather than a
  subdirectory, for the reason given in Design Decision 10.

## 6. Design Decisions

**1. User OAuth, not the existing service account.** The target mailbox is a personal Google
account, not Workspace. A service account can only impersonate a mailbox through domain-wide
delegation, which requires a Workspace domain. So the existing `secrets/service-account.json`
is unusable for mail no matter how it is configured, and the connector must authenticate *as the
user*. *Alternative considered:* IMAP with an app password — fewer moving parts and no consent
screen, but it needs a long-lived static credential with full mailbox reach and Google continues to
tighten app-password availability. Rejected in favour of a revocable, explicitly-scoped OAuth grant.

**2. Scope is pinned to `gmail.readonly` and guarded by a test.** `SCOPES` is a module-level
`Final` tuple containing exactly one entry, and `tests/test_gmail_source.py` asserts **exact
equality** against a literal — not a substring or subset check. Adding `gmail.send` or
`gmail.modify` later fails CI rather than silently widening what the token can do. This applies
the workspace `security.md` rule, "Pair unsafe configs with startup safety checks"
inverted: instead of guarding a dangerous config, make the dangerous config impossible to reach
without a visible failure. `load_credentials()` additionally re-checks the granted scopes at
runtime and refuses to proceed if the stored token carries anything beyond `SCOPES`.

**3. Date query is the primary sync path; `historyId` is only an opportunistic fast path.**
Gmail expires history records, so a `historyId` stored at the end of one run is frequently invalid
by the next — especially at a monthly cadence. Building `historyId` as the primary mechanism would
produce a connector that works in testing and fails intermittently in production, which is the worst
failure shape. The date query is always correct and always available; `historyId` may be layered on
later purely as an optimisation, and must always fall back.

**4. The fetcher writes files; it does not parse.** `fetch-mail` produces `.eml` files and stops.
It does not import `receipt_ingest`, and `receipt_ingest` does not import it. This keeps the parser
credential-free and unit-testable exactly as it is today, keeps the fetched mail inspectable by hand
before anything consumes it, and means a Gmail-side change can never break parsing. *Alternative
considered:* a single `fetch-and-ingest` command — rejected because it couples a network+auth
boundary to a pure function and removes the operator's chance to eyeball what came down.

**5. `[gmail]` is an optional config section.** `_section()` raises on a missing section, so a
required block would break `tests/test_config.py::_FULL_CONFIG`, `tests/conftest.py` and
`tests/test_reports.py`. `load_config()` will use `data.get("gmail")` and set `Config.gmail = None`
when absent. `fetch-mail` then fails with a clear "add a `[gmail]` section" message rather than a
`KeyError`. This also keeps the toolkit genuinely reusable — an org that never wires up Gmail is
unaffected.

**6. Idempotency comes from deterministic filenames.** Each message is written as
`<sanitised-message-id>.eml`; re-running an overlapping window rewrites identical bytes rather than
accumulating duplicates. `receipt_map.py`'s Message-ID + content-hash dedup is the second line of
defence. Overlapping fetches are therefore safe by design, which matters because the correct
operating procedure is to *overlap* the window rather than risk a gap.

**7. Local-only; no OAuth token in CI.** Operator decision. The monthly workflow continues to do
reports only. A refresh token for a personal mailbox stored in a public repo's Actions secrets would
mean a compromised workflow run could read the entire inbox, and the triage rounds are hands-on
anyway. Revisit only as a deliberate, separate decision.

**8. The fetch is date-scoped, not label- or sender-scoped.** Operator decision, and the evidence
supports it: in the last triage round **6 of 13 cases did not arrive through the submission form** —
two were vendor threads, one was receipts emailed directly, one a forwarded purchase confirmation,
one a lost-check report, one a paper submission announced by email. Any subject or sender filter
would have silently dropped all six. The parser already ignores non-form mail structurally, so a
broad fetch costs disk, not correctness. ⚠️ The consequence is that unrelated personal mail in the
date window lands on disk — see Risks.

**10. Fetched `.eml` files land beside the `.mbox` archives, not in a subdirectory — because
dedup is per-run.** `map_submissions()` accumulates `seen_ids` and `seen_hashes` **within a single
call** (`receipt_map.py` L187-199). Two separate `map-receipts` runs — one over the `.eml` inbox,
one over the `.mbox` archives — would each look internally clean while together double-counting
every message in the overlapping eleven weeks. The sources must therefore be mapped in **one**
run. `iter_source()` globs a directory **non-recursively** (`glob("*.eml")`, `glob("*.mbox")`), so
a `mail_samples/inbox/` subdirectory would be invisible to `--source mail_samples` and would force
exactly the broken two-run pattern. Writing the `.eml` files directly into `mail_samples/` makes
`map-receipts --source mail_samples` pick up both kinds in a single pass, and cross-source dedup
then works for free with **no change to `iter_source()`**. *Alternatives considered:* making
`iter_source()` recursive — rejected because it silently changes behaviour for every existing
caller of a shared function; or merging the archives into the fetch — rejected because the older
archive predates the fetch window and is the only record of pre-cut-over submissions.

**11. The June handoff window is treated as expected-duplicate territory, not an anomaly.** With
two roles accepting submissions for the same month, a requestor may reasonably have sent the same
form twice. The existing content-hash key is `requestor_email | stated_total | first_line_item_date`
(`receipt_map.py` L146-151), which collapses exactly that case — it is the same mechanism that
already caught a real double submission in the last round. Duplicate drops in June are therefore
**expected and correct**, but the backfill step reports the drop count so the operator can eyeball
it rather than have it silently swallowed. ⚠️ The same key would also collapse two *genuinely
distinct* submissions from one person sharing a stated total and first line-item date. That is
pre-existing behaviour, not introduced here, but June is when it is most likely to bite.

**9. Autonomous-behaviour trigger does not fire.** `fetch-mail` is a one-shot, operator-invoked CLI
command that completes and returns. It is not a daemon, scheduled job, soak loop, or watcher, and
the CI-cron option was explicitly declined. No long-running observation step is required. *(Stated
explicitly so reviewers can confirm the classification rather than infer it.)*

## 7. Build Steps

<!-- autofix-applied: 2026-08-25 -->
### Step 9: Gmail OAuth client + pinned read-only scope
- **Problem:** Create `pta_finance/gmail_source.py` with a `Final` `SCOPES` tuple containing exactly `gmail.readonly`, a `GmailAuthError` with actionable messages, and `load_credentials()` that loads/refreshes the token, re-checks granted scopes at runtime, and raises a remediation-bearing error when the refresh token is dead. Add an **optional** `Gmail` dataclass to `config.py` via `data.get("gmail")` so `Config.gmail` is `None` when the section is absent, plus a `[gmail]` block in `config.example.toml` using fake placeholders only. Add `google-api-python-client` and `google-auth-oauthlib` to `pyproject.toml`.
- **Type:** code
- **Issue:** #15
- **Flags:** --reviewers deep
- **Produces:** `pta_finance/gmail_source.py`, modified `config.py` / `config.example.toml` / `pyproject.toml`, `tests/test_gmail_source.py`
- **Done when:** `uv sync --extra dev` is run first (this step adds two dependencies and a fresh worktree does not inherit `.venv` — per the workspace `worktree-hygiene.md` rule: fresh worktrees do not inherit `.venv`); then `uv run pytest -q` passes with the full suite at ≥218 tests plus new ones; a test asserts `gmail_source.SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)` by **exact equality**; a test asserts a token carrying an extra scope is rejected; a test asserts `load_config()` on a config with **no** `[gmail]` section yields `cfg.gmail is None` and does not raise; `mypy --strict pta_finance` and `ruff check .` clean.
- **Depends on:** none
- **Status:** DONE (2026-08-25)

<!-- autofix-applied: 2026-08-25 -->
### Step 10: `fetch-mail` command + idempotent `.eml` writer
- **Problem:** Add `build_query`, `list_message_ids` (paginating `users.messages.list`), `fetch_raw` (`format="raw"`, base64url-decoded), and `write_eml` (deterministic `<sanitised-message-id>.eml`, skip-if-identical) to `gmail_source.py`. Wire a `fetch-mail` subparser in `cli.py` following the `ingest-receipts` shape, with `--since` (required), `--until`, `--query`, `--out`, `--limit`, `--dry-run`. Print a count summary only — **never** subjects, senders, or body text to stdout.
- **Type:** code
- **Issue:** #16
- **Flags:** --reviewers deep
- **Produces:** extended `gmail_source.py`, `fetch-mail` in `cli.py`, extended `tests/test_gmail_source.py`
- **Done when:** tests using a **faked** Gmail service (no network, no credentials) prove: pagination walks multiple `nextPageToken` pages; `build_query` renders `after:`/`before:` correctly from dates; running the same window twice leaves byte-identical files and reports zero new; `--dry-run` writes nothing; a missing `[gmail]` section produces `GmailAuthError`, not a traceback. Full suite green, `mypy --strict`, `ruff` clean.
- **Depends on:** 9

<!-- autofix-applied: 2026-08-25 -->
### Step M4: Google Cloud OAuth client + one-time consent (operator)
- **Problem:** In the existing Cloud project, enable the Gmail API and create an OAuth **Desktop app** client. Download the client-secrets JSON to the gitignored `secrets/` directory and add the `[gmail]` block to the private `config.toml`. Run `pta-finance fetch-mail --since <recent-date> --dry-run` once to trigger the browser consent flow and mint the token. **Then set the OAuth consent screen to Production (or add the account as a test user):** while it is in *Testing*, refresh tokens silently expire after 7 days and every run will demand re-authorisation.
- **Type:** operator
- **Issue:** #17
- **Produces:** nothing in the repo — a gitignored client-secrets file, a gitignored token file, and a `config.toml` edit
- **Done when:** `pta-finance fetch-mail --since <recent-date> --dry-run` exits 0 and reports a message count. Verified by exit code and count only — no token or secret file contents are ever printed (per the workspace `security.md` rule, "Never dump secret file contents" -- metadata and exit codes only, never file contents). The consent screen shows **Production** (or the account is listed as a test user).
- **Depends on:** 10 — the `Done when:` invokes `fetch-mail`, which Step 10 creates; depending on Step 9 alone would dispatch this step before the command exists.

<!-- autofix-applied: 2026-08-25 -->
### Step M5: Refresh-token longevity check (wait)
- **Problem:** Confirm the OAuth refresh token still works once the Testing-mode 7-day expiry window has passed. This is the only way to prove Step M4's Production setting actually took — a same-day check cannot detect the failure, because the token is valid for its first 7 days either way.
- **Type:** wait
- **Issue:** #18
- **Produces:** nothing — an observation only
- **Done when:** `pta-finance fetch-mail --since <recent-date> --dry-run` exits 0 in a **fresh shell ≥8 days after consent**, without opening a browser or prompting for re-authorisation. If it demands re-consent, the consent screen is still in Testing — fix it and restart the 8-day clock.
- **Depends on:** M4
- **Blocks:** nothing — this is a background confirmation, deliberately OFF the critical path so the backfill is not stalled for over a week.

### Step 11: End-to-end smoke gate — fetch → ingest → map (operator)
- **Problem:** Run one real cycle over a small date window with real components wired together and no mocks: `fetch-mail --since <date>` into the gitignored inbox, then `ingest-receipts --source <inbox> --profile`, then `map-receipts --source <inbox>` to a CSV (**not** `--write-tab`). Confirm the pipeline completes without exception and that the profile's recognised-submission count and email-date span are consistent with what the mailbox actually holds. Then re-run `fetch-mail` over an **overlapping** window and confirm the mapped row count is unchanged.
- **Type:** operator
- **Issue:** #19
- **Produces:** nothing in the repo — gitignored `.eml` files and a gitignored CSV
- **Done when:** all three commands exit 0; the mapped CSV row count is stable across the overlapping re-fetch (proving end-to-end idempotency, not just per-file idempotency); no unhandled exception at any stage. Business-logic correctness of individual submissions is explicitly **out of scope** for this gate.
- **Depends on:** 10, M4

### Step 12: Full backfill from the board cut-over + reconciliation (operator)
- **Problem:** Run the real backfill: `fetch-mail --since 2026-06-01` into `mail_samples/`, then a **single** `map-receipts --source mail_samples` run so the fetched `.eml` files and the existing `.mbox` archives are deduped against each other in one pass. Compare the resulting ledger against the current 554-row ledger: row count, per-month distribution, and total. Confirm the June 2026 submission count is consistent with a dual-intake month, and explicitly check whether any June submission appears **only** in the fetched mail or **only** in the archives.
- **Type:** operator
- **Issue:** #20
- **Produces:** nothing in the repo — gitignored `.eml` files and a gitignored reconciliation CSV
- **Done when:** the combined run reports a duplicate-drop count the operator has reviewed; the new ledger is a **superset** of the existing 554 rows (no pre-cut-over row lost); no month between 2026-06 and the fetch date is empty; and any June-only-in-one-source submission is listed for follow-up rather than silently merged. If June looks thin against the archives, that is the signal that a second mailbox export is needed — see Risks.
- **Depends on:** 11

### Step 13: Docs + plan reconciliation
- **Problem:** Replace the Takeout procedure in `SETUP.md` and `docs/loading-receipts.md` with the `fetch-mail` procedure, including the Production-consent step, the overlap-don't-gap operating rule, and the
  **single-run** requirement from Design Decision 10 (splitting sources across two `map-receipts`
  runs silently double-counts). Update `CLAUDE.md` §3 (commands), §4 (layout), §7 (environment requirements) and §6 (current state). Tick the "Monthly automation" bullet in `plan.md` §"Phase 4 → Not yet built", noting that the *cron* half remains deliberately out of scope.
- **Type:** code
- **Issue:** #21
- **Flags:** --reviewers code
- **Produces:** updated `SETUP.md`, `docs/loading-receipts.md`, `CLAUDE.md`, `plan.md`
- **Done when:** `grep -i takeout SETUP.md docs/loading-receipts.md` returns only historical/backfill references, never the primary procedure; the docs contain no organization, school, person, or email address; `uv run pytest -q` still green.
- **Depends on:** 12

## 8. Risks and Open Questions

| Item | Risk | Mitigation |
|---|---|---|
| OAuth consent in *Testing* mode | Refresh tokens expire after 7 days; every run demands re-auth and the connector looks broken | Step M4 makes publishing to Production an explicit operator action, and its `Done when:` requires proving the token survives **≥8 days** — a same-day check cannot detect this |
| Broad date fetch downloads unrelated personal mail | Mail unrelated to the organization sits on local disk in the inbox directory | Accepted deliberately (Design Decision 8) — a filter would have missed 6 of 13 cases last round. Two independent gitignore rules cover the directory; the parser ignores non-form mail structurally; the directory can be cleared after each round |
| `gmail.readonly` grants whole-mailbox read | The token can read everything, not just reimbursements | Read-only is the narrowest scope Gmail offers for this job — there is no per-label OAuth scope. Scope pinned by exact-equality test; the grant is revocable at any time from Google Account settings |
| New transitive dependency surface | `google-api-python-client` pulls a sizable tree into a `mypy --strict` project | Existing `[[tool.mypy.overrides]]` already covers `google.*`; Step 9 confirms `mypy --strict` stays clean before anything else is built |
| Fetch could stall on a very large first window | A multi-year `--since` pulls a lot of mail | `--limit` and `--dry-run` let the operator size a window before committing; the historical `.mbox` remains the backfill path and is not being retired |
| Message-ID as filename | Exotic Message-IDs may contain path-hostile characters | `write_eml` sanitises and length-caps; a test covers a hostile Message-ID |
| **Handoff-window completeness** | The outgoing treasurer (now CFO) accepted submissions until 2026-06-30. A June request sent only to that role's mailbox is **not** in the mailbox being fetched, and no amount of re-fetching will surface it | Step 12 explicitly checks June for submissions present in only one source, and treats a thin June as the trigger for a second mailbox export. This is a **known gap, not a solved problem** — flagged rather than assumed away |
| Cross-source double-count | Mapping the `.eml` inbox and the `.mbox` archives in two separate runs would double every message in the ~11 overlapping weeks, and each run would look clean in isolation | Design Decision 10: fetch into `mail_samples/` so one `--source mail_samples` run covers both; Step 10 adds a regression test; Step 13 documents the single-run rule |
| Content-hash false positive | Two genuinely distinct submissions sharing requestor + stated total + first line-item date collapse into one | Pre-existing behaviour (`receipt_map.py` L146-151), not introduced here. Step 12 requires the operator to review the duplicate-drop count rather than trust it blindly |
| **Open question** | Should the inbox directory be cleared automatically after a successful `map-receipts`? | Deferred — not decided. Manual clearing for now; revisit once the operator has run a few real rounds |

## 9. Testing Strategy

**New unit tests** (`tests/test_gmail_source.py`, faked Gmail service — no network, no credentials):

- `SCOPES` exact-equality assertion (the security regression test).
- A stored token carrying any scope beyond `SCOPES` is rejected.
- `build_query` renders `after:` / `before:` correctly, including when `--until` is omitted.
- `list_message_ids` walks multiple `nextPageToken` pages and yields every id once.
- `write_eml` is idempotent — same input, byte-identical output, reported as "unchanged".
- `write_eml` sanitises a path-hostile Message-ID.
- Missing `[gmail]` section → `GmailAuthError` with a remediation sentence, not a `KeyError`.
- Dead refresh token → `GmailAuthError` naming the fix, not a raw `google.auth` exception.
- **Cross-source dedup:** a fixture directory holding one `.eml` and one `.mbox` that contain the
  **same** message yields exactly one mapped ledger row from a single `map_submissions()` call —
  the regression guard for Design Decision 10.

**Existing tests that could break:** only `tests/test_config.py`, `tests/conftest.py`, and
`tests/test_reports.py`, and only if `[gmail]` were made **required**. Making it optional is
precisely what keeps all 218 existing tests untouched — Step 9's `Done when:` asserts a
config with no `[gmail]` section still loads.

**End-to-end verification:** Step 11 is the data-pipeline smoke gate — real Gmail, real parser, real
mapper, no mocks. It exists because unit tests with a faked service mock the exact boundary they
would otherwise assert on, so producer→consumer drift between the fetcher's output and the parser's
expectations is invisible to them. The overlapping re-fetch check is what proves idempotency
end-to-end rather than merely per-file.

**Not tested automatically:** the OAuth consent flow itself (needs a browser and a human) and
token longevity past the Testing-mode cliff (needs wall-clock time). Both are covered by Step M4's
operator checks.

---

## Next step

This plan has **not** yet been through review. Run, in order:

```
/plan-expedite --plan documentation/gmail-ingest-plan.md
```

then, once it returns READY:

```
/build-phase --plan documentation/gmail-ingest-plan.md
```

`/plan-expedite` chains plan-review-autofix → plan-wrap-autofix → repo-sync → session-wrap, so
`/build-phase` walks a validated, issue-linked plan. Do **not** run `/repo-sync` first — issues
minted before review turn one plan fix into N issue-body edits.
