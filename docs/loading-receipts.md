# How-to: loading reimbursement receipts

Turns the **reimbursement-form emails** in a treasurer mailbox into a flat **Reimbursements**
ledger tab (one row per line item), which powers the **Receipts Explorer** dashboard. This guide
is **generic** — no organization, person, or email is named. Real emails stay in the gitignored
`mail_samples/`; the repo is public, your data stays private.

Pipeline:

```
Gmail  ->  fetch-mail (.eml into mail_samples/)  ->  map-receipts --write-tab  ->  Reimbursements tab
                                                                                   (Receipts Explorer auto-updates)
```

`fetch-mail` is the only step that touches Gmail, and its grant is **read-only** — the toolkit
never sends, replies, labels, archives, or deletes mail. Parsing is **credential-free**; the only
step that touches the Sheet is `--write-tab`. Re-fetching and re-loading are both idempotent: an
overlapping fetch rewrites nothing, and `--write-tab` **replaces** the tab, with `Message-ID` +
content-hash dedup preventing double-counting. The one thing a re-run can destroy is your
hand-filled `category_map.csv` — Step 2's copy command is guarded against that, so do not replace
it with a bare `Copy-Item`.

---

> ### ⚠️ Read these two rules before you run anything
>
> **1. Map in ONE run.** Run `map-receipts` **once**, pointed at the whole `mail_samples/`
> directory, so the fetched `.eml` files and any `.mbox` archives are deduped **against each
> other**. Dedup happens *within a single run*: two separate runs — one over the `.eml` files, one
> over the `.mbox` archives — each look perfectly clean on their own while **together
> double-counting every message the two sources share**. There is no error message for this; the
> ledger is just silently wrong.
>
> **2. Keep `[fiscal_year] start_month` correct in `config.toml`.** Standard
> `ingest-receipts` / `map-receipts` runs read that configured month, so the commands below do not
> repeat it. Use `--start-month N` only as an intentional override for one run; an explicit value
> wins over config. With neither a usable config nor an explicit override, the command fails rather
> than silently assuming a calendar year.
>
> **Do not rely on the CLI to remind you of rule 1.** `fetch-mail` prints a one-run reminder,
> but only after a **real** fetch — a `--dry-run` (the first command this guide gives you) prints
> no reminder at all.

---

## Prerequisites

- Toolkit installed and `config.toml` set up (see [SETUP.md](../SETUP.md) §0–2) — standard receipt
  commands read its fiscal-year setting, while `fetch-mail` and `--write-tab` use the other private
  settings.
- A Gmail OAuth client + a `[gmail]` config section, set up once — see [SETUP.md](../SETUP.md)
  **§6 "Gmail access"**. The first `fetch-mail` run opens a browser for a one-time read-only
  consent, and Testing-mode consent expires after 7 days, so expect an occasional re-approval.
- A category map at `reports/output/category_map.csv` (raw form category → Budget Timeseries
  line). **It does not ship** — `reports/output/` is gitignored, so a fresh clone does not have
  one. You build it once, from the seed that Step 2's preview writes; Step 2 walks you through it.
  A new/unmapped category is flagged `needs_review` until you add a row.

---

## Step 1 — Fetch the mail with `fetch-mail`

`fetch-mail` pulls a **date window** of messages straight out of the mailbox and writes each one
as a `.eml` file into the gitignored `mail_samples/` directory. There is no Gmail search, no
label, and no export to wait for.

**Size the window first** (`--dry-run` counts the matches and writes no `.eml` files):

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; uv run pta-finance fetch-mail --since 2026-06-01 --dry-run
```

Then fetch for real:

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; uv run pta-finance fetch-mail --since 2026-06-01
```

You get the search query, the destination directory, and a count summary — `N message(s) matched
-> X new, Y unchanged, Z rewritten` — and **no message content**. No subject, sender, body, or
message id is ever printed. (A `--dry-run` stops after its own count line, `N message(s) match`.)

Each message is written under a stable, automatically-derived filename of the form
`<readable-stem>-<hash>.eml`, so the same message always lands on the same file: a re-fetch writes
identical bytes and reports it as `unchanged` rather than making a duplicate. **Do not rename
these files by hand.** (The exact rule is in `CLAUDE.md` §6 if you need it.)

**Choosing dates:**

- `--since` is **inclusive**; `--until` is **exclusive** (it names the first day that is *not*
  fetched). Omit `--until` for an open-ended window up to now.
- **Overlap successive windows; never tile them.** An overlap is free — a re-fetch rewrites
  nothing. A gap is silent and permanent.
- The fetch is **date-scoped only**: no sender or subject filter, so unrelated mail in the window
  is fetched too and then ignored by the parser. That is deliberate — a narrower filter silently
  misses the submissions that never went through the form (emailed receipts, forwarded purchase
  confirmations, vendor threads, paper submissions announced by email). See
  `documentation/gmail-ingest-plan.md` § Design Decision 8 for the evidence.
- `--limit N` stops after N messages — a cheap first look at a big window.
- `--out DIR` overrides the destination, but **leave it alone**: the default is the configured
  `[gmail] inbox_dir` *itself*, never a subdirectory, because the parser globs a directory
  **non-recursively**. Files in a subdirectory would be invisible to `--source mail_samples` and
  would force exactly the broken two-run pattern warned about above.

**Know what `mail_samples/` now holds.** Under the old export-a-label procedure it held one
label's worth of reimbursement forms. It now holds a plaintext copy of *a whole mailbox window* —
personal mail included. `.gitignore` keeps it out of the repo, but that is all it does: treat the
directory like the mailbox itself. Do not zip and share it, do not put it in a cloud-synced
folder, never attach a `.eml` to an issue, and delete the window once the ledger tab is written
and verified.

---

## Step 2 — Preview, build the category map, then load

PowerShell on Windows (the `PYTHONUTF8` vars prevent a cp1252 crash on non-ASCII names). One
command per line — `&&` is a parser error in PowerShell 5.1.

**Preview + data-spread profile.** Writes the seed CSV named below; makes no Sheet write:

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; uv run pta-finance ingest-receipts --source "mail_samples" --profile --originals-only --csv reports\output\category_seed.csv
```

Read the output: form types, the full category list, reconcile pass/fail, and — key for
completeness — the `email date span` line (see Step 3). **Keep this output on screen**: the
`form types:` list is what you copy form names from, two steps below.

**First time only — turn the seed into the category map.** The command above wrote
`reports/output/category_seed.csv`: one row per raw form category, with an empty column for you to
fill in. Copy it to the name the loader expects. This command is written so it will **not**
clobber a map you have already filled in:

```powershell
if (Test-Path reports\output\category_map.csv) { "category_map.csv exists - keeping your filled-in copy" } else { Copy-Item reports\output\category_seed.csv reports\output\category_map.csv }
```

Now open `reports/output/category_map.csv` **in a plain UTF-8 text editor** (VS Code, Notepad) or a
spreadsheet editor that can save UTF-8 CSV. Two cautions, both of which fail *silently*:

- **Not in Google Sheets.** `--category-map` reads a local file path only — it never reads a Sheet
  tab. Editing an imported copy in Drive leaves the file on disk untouched, and the load then maps
  nothing.
- **Not in Excel's default CSV save.** The file is UTF-8 without a BOM; Excel opens it as ANSI and
  saves it back as cp1252, which makes the load die on any non-ASCII category name. If you use
  Excel, save as **CSV UTF-8**.

The file has **three** columns, in this order:

```
raw_category,line_item_count,canonical_category (fill in)
```

Do two things to it:

1. **Rename the third column header to exactly `canonical_category`.** The loader looks for that
   bare name — leave the "(fill in)" on and every mapping is ignored, flagging the whole ledger
   `needs_review`.
2. **Fill in that third column** with the Budget Timeseries line each raw category maps to. Leave a
   row's canonical blank to have those line items flagged `needs_review` instead.

Two details about that file that are easy to get wrong:

- **The `(blank)` row does nothing.** The seed lists `(blank)` for line items that carried no
  category at all. The loader skips that row by name, so filling it in has no effect — use a
  `FORM_DEFAULT:` row instead (next bullet). This is usually the largest count in the seed, so it
  is the row you are most likely to reach for first.
- **Per-form default.** To give a form that collects no category its own budget line, add a row
  whose `raw_category` is `FORM_DEFAULT: <form name>` and whose `canonical_category` is the budget
  line. `<form name>` must match **exactly** — copy it from the `form types:` list in the profile
  output above; an approximate name is a silent no-op.

Keep the finished `category_map.csv` — later loads reuse it, and you only add rows when a new
category appears.

**Preview the ledger first** (no Sheet write). Write the CSV under `reports\output\` and nowhere
else — it carries `requestor_name`, `requestor_email` and `receipt_url` for every line item, and
`reports/output/` is the gitignored directory. A CSV dropped anywhere else in the repo is **not**
ignored, and one `git add -A` would put those names in the public history:

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; uv run pta-finance map-receipts --source "mail_samples" --category-map reports\output\category_map.csv --csv reports\output\ledger.csv
```

**Check the summary's `category map : N mapping(s), M form default(s)` line before you trust the
run.** `0 mapping(s)` means the header rename did not take, and every row will be flagged
`unmapped-category`.

**Load into the Sheet** (creates/replaces the `Reimbursements` tab; snapshots it first if it
already exists):

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; uv run pta-finance map-receipts --source "mail_samples" --category-map reports\output\category_map.csv --write-tab "Reimbursements"
```

Note that `--source` is the **directory**, not a single file. That is what makes this one run
cover the fetched `.eml` files *and* any `.mbox` archives sitting beside them — see the
map-in-ONE-run rule at the top. The Receipts Explorer tab reads this ledger live, so it updates
automatically.

---

## Step 3 — Verify completeness (do not skip)

Two independent checks that you actually got **everything**:

1. **Email date span.** The `--profile` output prints a line reading
   `email date span     : <oldest> -> <newest>  (when forms were SUBMITTED — check for gaps)`.
   Those are the dates the forms were **submitted**. Compare
   `<oldest>` to the `--since` date you fetched from. If the span **starts later** than your real
   history — e.g. emails only from the spring even though the form was used all year — fetch from
   an earlier `--since`. **Watch the line-item `month` vs the email date:** a form submitted in
   April can reimburse a July expense, so old *line-item* dates do **not** prove you captured the
   old *emails*. The **email date span** is the honest signal.
2. **Count match.** The `map-receipts` summary prints `N submission(s) (originals; M Re:/Fwd:
   skipped) -> R ledger row(s)`. Compare `N` to the number of submissions you expect for the
   window. `fetch-mail`'s own `N message(s) matched` line is the upper bound — it counts *all*
   mail in the window, form or not.

**If either check shows a gap:** re-run `fetch-mail` with an earlier `--since` (Step 1), then
re-run Step 2's **preview** and **load** commands. The copy command in between is guarded, so it
will keep your filled-in `category_map.csv`. Re-running the load replaces the tab cleanly — no
double-counting.

### Known coverage gap: a handover period with two intake mailboxes

`fetch-mail` reads **one** mailbox — the one whose account granted consent. If, during a role
handover, two people were both receiving submissions, any request sent **only** to the other
person's mailbox is not in the fetched mail, and **no amount of re-fetching will surface it**.

The signal is a **thin month** across the handover window: fewer submissions than the month
plausibly saw.

If you see that, the fix is a second export from the other mailbox — and the way you ask for it
matters, because you are asking for someone else's mail:

- **Prefer a label-scoped export.** Ask them to label just the submission emails and export that
  label (Google Takeout can export a single label), so you receive only those messages and not
  their whole mailbox. Tell them up front what you are collecting and why.
- A second `fetch-mail` consent from their account also works, but it is **date-scoped only** and
  would copy *all* of their mail for the window onto your machine. If you go that way, use the
  narrowest `--since` / `--until` that covers the handover, and say so before you run it.
- Either way: drop the result into `mail_samples/`, let the single `map-receipts` run dedup it,
  and then **delete the other mailbox's messages from `mail_samples/` once the tab is written**.
  You do not need them again.

This gap is real and unsolved — it was found during the backfill run (issue #20, now closed), and
there is no open tracker for it. Re-read this section whenever a month looks thin.

Duplicate drops during a handover month are the *opposite* signal and are **expected and
correct**: if a requestor sent the same form to both mailboxes, content-hash dedup collapses it.
The tool does not print a duplicate count, so the visible signal is the `N submission(s)` figure
coming in lower than the raw email count — check the two against each other rather than assuming
either.

---

## How the ledger cleans the data (so the numbers make sense)

- **`Re:`/`Fwd:` replies** are thread duplicates (same reimbursement, different email) → dropped.
  A reply's `Message-ID` differs from the original, so only skipping them by subject catches these.
- **Blank category** carries forward from a prior line in the same submission; a form that
  collects no category falls back to its `FORM_DEFAULT:` budget line; anything still blank →
  `needs_review`.
- **Blank-amount** lines are dropped (not real expenses).
- **`needs_review`** collects `unmapped-category` / `bad-amount` / `total-mismatch`. Sort the tab
  by that column and spot-check.
- **New category?** If a form category isn't in `category_map.csv`, its row shows a blank
  `canonical_category` + `needs_review`. Add a row using the file's real three-column order —
  `raw_category,line_item_count,canonical_category`, leaving `line_item_count` empty — then re-run
  Step 2's load command. A two-field line puts the budget line in the wrong column and is ignored.

---

## Historical: earlier loads used Google Takeout

Before `fetch-mail` existed, the acquisition half of this guide was a manual Gmail search → label →
**Google Takeout** `.mbox` export. Those older `.mbox` archives are still valid input and are
**not** retired: they are the only record of submissions predating the fetch window, so leave them
in `mail_samples/` and let the single `map-receipts` run dedup them against the fetched `.eml`
files. A label-scoped Takeout export also remains the better option for a mailbox that is not
yours (the second-mailbox case above). For new loads from your own mailbox, use `fetch-mail`.

---

## Later: monthly automation

`fetch-mail` removes the manual export, but the **cron half is deliberately not built**: no OAuth
token goes into CI. A refresh token for a personal mailbox stored in a public repo's Actions
secrets would let a compromised workflow run read the entire inbox, and these rounds are hands-on
anyway. Fetching stays **local-only** by design (`documentation/gmail-ingest-plan.md` § Design
Decision 7); revisit only as a deliberate, separate decision.
