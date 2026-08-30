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

The fetch window and ledger window are separate controls. `fetch-mail --since` limits which Gmail
messages are acquired; it does **not** make older `.eml` or `.mbox` files in `mail_samples/`
ineligible for the ledger. The optional private `[receipt_mapping] received_since` setting is the
authoritative inclusive ledger cutoff.

---

> ### ⚠️ Read these three rules before you run anything
>
> **1. Map in ONE run.** Run `map-receipts` **once**, pointed at the whole `mail_samples/`
> directory, so the fetched `.eml` files and any `.mbox` archives are deduped **against each
> other**. Dedup happens *within a single run*: two separate runs — one over the `.eml` files, one
> over the `.mbox` archives — each look perfectly clean on their own while **together
> double-counting every message the two sources share**. There is no error message for this; the
> ledger is just silently wrong.
>
> **2. Set the ledger cutoff in private config.** If this load represents a bounded term, set
> `[receipt_mapping] received_since = "YYYY-MM-DD"` before mapping. It uses the outer RFC-822
> `Date` header's local calendar date and is inclusive. Do not assume `fetch-mail --since` provides
> this protection: legacy archives in the source directory are still read in full. Omit the
> section only when an all-history ledger is intentional.
>
> **3. Keep `[fiscal_year] start_month` correct in `config.toml`.** Standard
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
  commands read its fiscal-year setting and optional ledger cutoff, while `fetch-mail` and
  `--write-tab` use the other private settings.
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
$receivedSince = "YYYY-MM-DD" # replace with the private acquisition-window start
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; uv run pta-finance fetch-mail --since $receivedSince --dry-run
```

Then fetch for real:

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; uv run pta-finance fetch-mail --since $receivedSince
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
- These flags control Gmail acquisition only. They never filter existing files during
  `map-receipts`; `[receipt_mapping] received_since` owns ledger membership.
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

`ingest-receipts --profile` deliberately remains a **full-source completeness view**. It does not
apply `[receipt_mapping] received_since`; seeing older archive mail in its date span is expected.

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

**Set the authoritative ledger cutoff in your private `config.toml`** when the ledger should cover
only a bounded term:

```toml
[receipt_mapping]
received_since = "YYYY-MM-DD" # replace privately; inclusive
```

With no section, mapping keeps all recognized original submissions for backward compatibility.
For a one-run override, `--received-since YYYY-MM-DD` wins over config; `--all-received` disables
the configured cutoff. Those two flags are mutually exclusive. Filtering happens **before**
Message-ID/content dedup, so an older archived twin cannot suppress an in-scope fetched message.
Mapping makes one config decision before it loads the category map or parses any source message.
When `--start-month` is omitted, the config is required immediately and that exact snapshot supplies
both fiscal-year and cutoff policy. A config file that is absent then cannot appear later and affect
only one of them. A credential-free preview still works with an explicit `--start-month` when the
config path is absent; a config present at that initial decision is loaded and validated once.

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

Also check `received cutoff : ...; excluded N submission(s)` on every run. The line states both
the effective cutoff and whether it came from config or a CLI override. With an active cutoff,
any recognized original missing a valid outer `Date` header aborts before CSV or Sheet output;
the error reports aggregate counts only. Any `--write-tab` mapping that produces zero ledger rows
also refuses before constructing the Sheet client—whether the source is empty/unrecognized, the
cutoff excludes everything, or in-scope submissions have no mappable amount rows.

Only amounts that parse as finite money are sent through the Sheet's numeric write behavior;
rejected amount text is forced to remain inert for review. Every receipt CSV (the category seed,
ordinary ingest export, and mapped-ledger review) uses one serialization boundary that prefixes
formula-like text after leading whitespace (`=`, `+`, `-`, or `@`) while leaving validated counts
and positive/negative money numeric. In the category seed, the prefix is a reversible writer layer:
the category-map loader removes exactly that one layer, so raw categories with any existing leading
apostrophes remain distinct and still map correctly.

Formula-like text is also neutralized before the RAW Sheet grid write. Before replacing an existing
tab, the mandatory backup now writes two adjacent private artifacts: `<tab>.csv` is safe to open in
spreadsheet software even when the old/external grid predates this protection, while
`<tab>.raw.json` is a versioned, tagged `userEnteredValue` grid. It distinguishes a formula from
the same literal text and preserves string, native number, boolean, and empty cell types. Neither
artifact captures formatting or comments. Use the CSV for inspection/import convenience; use
Sheets version history first for recovery, with the JSON grid as the exact manual entered-value
recovery source. There is no automated JSON restore command. Both artifacts finish before
replacement, and a timestamp collision claims a suffixed directory instead of overwriting an
earlier backup.

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

1. **Email date span.** The full-source `--profile` output prints a line reading
   `email date span     : <oldest> -> <newest>  (when forms were SUBMITTED — check for gaps)`.
   Those are the dates the forms were **submitted**. Compare
   `<oldest>` to the `--since` date you fetched from. If the span **starts later** than your real
   history — e.g. emails only from the spring even though the form was used all year — fetch from
   an earlier `--since`. **Watch the line-item `month` vs the email date:** a form submitted in
   April can reimburse a July expense, so old *line-item* dates do **not** prove you captured the
   old *emails*. The **email date span** is the honest signal.
2. **Cutoff + count match.** The `map-receipts` output first states the effective inclusive cutoff
   and how many recognized originals it excluded, then prints `N submission(s) (originals; M
   Re:/Fwd: skipped) -> R ledger row(s)`. Compare `N` to the submissions expected **on or after the
   ledger cutoff**. `fetch-mail`'s own `N message(s) matched` line is only an acquisition upper
   bound — it counts all mail in that fetch window, form or not, and says nothing about older
   archive membership.

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

## Step 4 — Refresh the private review report

The report workflow deliberately has three separate entry points:

```text
fetch-mail              update the local email archive only
report-reimbursements   rebuild HTML from the current private bundle only
update-reimbursements   optionally run the first, refresh evidence, then run the second
```

All report data and HTML stay under gitignored `reports/output/`. The report command is fully
offline: it does not load `config.toml`, Google credentials, Gmail, or Sheets.

**Rebuild the report without checking Gmail:**

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; uv run pta-finance report-reimbursements
```

**Preview a combined local refresh** (Gmail is counted, but no `.eml`, bundle, or HTML is written):

```powershell
$receivedSince = "YYYY-MM-DD"
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; uv run pta-finance update-reimbursements --fetch-since $receivedSince --dry-run
```

**Run the combined local refresh:**

```powershell
$receivedSince = "YYYY-MM-DD"
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; uv run pta-finance update-reimbursements --fetch-since $receivedSince
```

The combined command performs these stages in order:

1. Fetch the overlapping Gmail window into the configured top-level archive.
2. Parse the `.eml` + `.mbox` archive once, apply the configured received cutoff to both original
   submissions and supplemental mail before deduplication/linking, and compute stable keys plus
   evidence hashes. The aggregate CLI summary reports supplemental evidence, events, unmatched
   candidates, and cutoff-excluded messages without printing mail identifiers or content.
3. Preserve every unchanged operator review. Append new source records with specific,
   evidence-limited per-item **Approve** or **Clarification** recommendations while keeping the
   recorded decision **UNREVIEWED**. These recommendations use only deterministic metadata
   (mapping, reconciliation, available receipt assets, amount threshold, and conservative text
   signals); they do not perform OCR, visual inspection, or policy adjudication.
4. Process candidate supplemental mail in a separate append-only lane. Exact RFC ancestry or an
   explicit private anchor may link it to a ticket; sender, name, subject, and prose similarity
   never do. Unlinked or ambiguous candidates remain visible in the report's unmatched queue.
5. Validate the complete schema-v2 bundle and atomically replace
   `reports/output/reimbursement-queue-breakdown.html`.

If an already-accounted email disappears or its evidence changes, the command stops before changing
the bundle or HTML. Stored evidence and event metadata carry canonical record digests, including
decoded attachment hashes and normalized RFC timestamps. That requires deliberate review; the tool
never carries an approval forward by guessing. A late-arriving in-scope email is still detected
because the bundle persists every accounted source key rather than relying on archive order.

### Private supplemental anchors and operator reviews

By default, `update-reimbursements` looks for
`reports/output/reimbursement-anchors.json` beside the private bundle. Use `--anchors PATH` to
select another gitignored file. The file is optional, strict, digestible, and must never be
committed. It can hold exact outbound-thread anchors, exact non-threaded direct links, configured
payment/approval actors, and explicit item-complete operator reviews. An anchor alone cannot mark
payment; payment requires a linked, affirmative, top-authored confirmation from a configured
payment operator with one unambiguous amount and reference (or the strict Zelle confirmation-block
shape). Negations, questions, attribution, cancellation language, and ambiguous values fail closed.
A secondary approval remains scoped to an exact
proposal thread and does not record payment. If the confirmed amount differs from the ticket
total, the discrepancy and reference remain visible but the ticket is not settled.

Public fake-shape example (replace every value only in your private file):

```json
{
  "schema_version": 1,
  "actors": {
    "payment_operators": ["payments@example.org"],
    "secondary_approvers": ["reviewer@example.org"]
  },
  "thread_anchors": [
    {
      "message_id": "<outbound-case@example.org>",
      "purpose": "CASE",
      "tickets": [
        {
          "review_key": "submission:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "ref": "NEW-01",
          "form_label": ""
        }
      ]
    },
    {
      "message_id": "<outbound-proposal@example.org>",
      "purpose": "APPROVAL_PROPOSAL",
      "tickets": [
        {
          "review_key": "submission:v1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "ref": "NEW-02",
          "form_label": ""
        }
      ]
    }
  ],
  "direct_links": [
    {
      "message_id": "<direct-receipt@example.org>",
      "purpose": "RECEIPT",
      "ticket": {
        "review_key": "legacy:v1:example-form-a",
        "ref": "P-001",
        "form_label": "Form A"
      }
    }
  ],
  "operator_reviews": [
    {
      "ticket": {
        "review_key": "legacy:v1:example-form-a",
        "ref": "P-001",
        "form_label": "Form A"
      },
      "record_decision": true,
      "items": [
        {
          "source_index": 1,
          "status": "A",
          "why": "Operator verified the synthetic receipt and claimed amount.",
          "reviewed_amount": ""
        },
        {
          "source_index": 2,
          "status": "C",
          "why": "The synthetic claimed amount differs from the receipt amount.",
          "reviewed_amount": ""
        }
      ],
      "action": "Resolve the remaining amount question",
      "block": "Confirm the intended claim amount.",
      "asks": ["Which synthetic amount is intended for item 2?"],
      "note": "Explicit operator visual review; payment remains separate.",
      "email_questions": ["Which synthetic amount is intended for item 2?"],
      "email_context": ""
    }
  ]
}
```

Approval proposals use a deliberately small protocol: a line containing only the exact ticket
reference, followed by `Approve...` or `Clarification...` action lines. The lines may cover every
item or exactly the currently non-A/held positions, preserving existing A positions. A
comma-separated group of exact refs is allowed only with one optionally bulleted `Approve as is`,
which expands to all A for each grouped ticket. Every anchored ref must occur exactly once and every
count must resolve unambiguously; otherwise the reply is quarantined without changing a decision.
Only a short positive/negative top-authored reply from the configured approver is classified;
quoted history and unrelated affirmative mail do nothing.

This command does **not** update either worksheet. Keeping that permission boundary visible is
intentional. When you also want the machine-owned `Reimbursements` tab replaced, run Step 2's
explicit `map-receipts --write-tab Reimbursements` command separately. The workflow creates no third
tab; item-level report decisions remain in the private structured bundle.

---

## How the ledger cleans the data (so the numbers make sense)

- **`Re:`/`Fwd:` form copies** remain excluded from the original-submission lane so quoted forms do
  not create duplicate tickets. The separate supplemental lane still accounts for exact-linked
  receipt, clarification, payment, and approval evidence carried by those messages.
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
