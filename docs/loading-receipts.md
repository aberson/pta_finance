# How-to: loading reimbursement receipts

Turns the **reimbursement-form emails** in the treasurer inbox into a flat **Reimbursements**
ledger tab (one row per line item), which powers the **Receipts Explorer** dashboard. This guide
is **generic** — no organization, person, or email is named. Real emails stay in the gitignored
`mail_samples/`; the repo is public, your data stays private.

Pipeline:

```
Gmail label  ->  Google Takeout (.mbox)  ->  map-receipts --write-tab  ->  Reimbursements tab
                                                                            (Receipts Explorer auto-updates)
```

Parsing is **credential-free**; the only step that touches the Sheet is `--write-tab`. Re-running
is safe — it **replaces** the tab, and `Message-ID` + content-hash dedup prevent double-counting.

---

## Prerequisites

- Toolkit installed and `config.toml` set up (see [SETUP.md](../SETUP.md) §0–2) — needed only for
  the `--write-tab` step.
- The category map at `reports/output/category_map.csv` (raw form category → Budget Timeseries
  line). It ships filled for the known categories; a new/unmapped category is flagged
  `needs_review` until you add a row.

---

## Step 1 — Label the reimbursement emails in Gmail (catch **all** of them)

Completeness lives or dies here. To avoid missing any:

1. In Gmail, search **broadly, across the whole mailbox**:

   ```
   from:(@wix-forms.com) in:anywhere
   ```

   - `in:anywhere` includes **Spam + Trash** (a normal search skips them).
   - Filtering by the **sender**, not the subject, catches every form variant (the parser
     recognizes reimbursement forms by their **structure**, not their subject line — so a
     differently-named form is still picked up).
2. **Note the date of the OLDEST email** in the results — that is how far back your history goes.
   You will confirm the load reaches this far in Step 4.
3. Select every match: tick the top checkbox, then click **"Select all conversations that match
   this search"** (otherwise Gmail only selects the visible ~50).
4. Apply a label: **Label** icon → **Create new** → e.g. `Reimbursements`.

---

## Step 2 — Export the label with Google Takeout

1. Go to **takeout.google.com** → **Deselect all** → check **Mail**.
2. Click **"All Mail data included"** → select **only** your `Reimbursements` label → **OK**.
3. **Next step** → create export with **File type = `.zip`** → download the zip when it's ready.
4. Unzip it and find `Takeout\Mail\Reimbursements.mbox`.
5. Move that `.mbox` into the repo's gitignored **`mail_samples\`** folder.

---

## Step 3 — Preview, then load

PowerShell on Windows (the `PYTHONUTF8` vars prevent a cp1252 crash on non-ASCII names):

**Preview + data-spread profile** (no Sheet writes):

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; uv run pta-finance ingest-receipts --source "mail_samples\Reimbursements.mbox" --profile --originals-only --start-month 7 --csv reports\output\category_seed.csv
```

Read the output: form types, the full category list, reconcile pass/fail, and — key for
completeness — the **`email date span`** line (see Step 4).

**Load into the Sheet** (creates/replaces the `Reimbursements` tab; snapshots it first if it
already exists):

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING="utf-8"; uv run pta-finance map-receipts --source "mail_samples\Reimbursements.mbox" --category-map reports\output\category_map.csv --start-month 7 --write-tab "Reimbursements"
```

The Receipts Explorer tab reads this ledger live, so it updates automatically.

---

## Step 4 — Verify completeness (do not skip)

Two independent checks that you actually got **everything**:

1. **Email date span.** The `--profile` output prints
   `email date span: <oldest> -> <newest>` — the dates the forms were **submitted**. Compare
   `<oldest>` to the oldest email you saw in Gmail (Step 1.2). If the span **starts later** than
   your real history — e.g. emails only from the spring even though the form was used all year —
   the export missed the earlier ones. **Watch the line-item `month` vs the email date:** a form
   submitted in April can reimburse a July expense, so old *line-item* dates do **not** prove you
   captured the old *emails*. The **email date span** is the honest signal.
2. **Count match.** The `map-receipts` summary prints `N submission(s)`. Compare `N` to Gmail's
   result count for your Step-1 search, **minus** any `Re:`/`Fwd:` replies (the tool drops those
   as thread duplicates).

**If either check shows a gap:** widen the Gmail search / re-select all matches (Step 1),
re-export (Step 2), and re-run Step 3. Re-running replaces the tab cleanly — no double-counting.

---

## How the ledger cleans the data (so the numbers make sense)

- **`Re:`/`Fwd:` replies** are thread duplicates (same reimbursement, different email) → dropped.
  A reply's `Message-ID` differs from the original, so only skipping them by subject catches these.
- **Blank category** carries forward from a prior line in the same submission; the Teacher form
  (which collects no category) defaults to its budget line; anything still blank → `needs_review`.
- **Blank-amount** lines are dropped (not real expenses).
- **`needs_review`** collects `unmapped-category` / `bad-amount` / `total-mismatch`. Sort the tab
  by that column and spot-check.
- **New category?** If a form category isn't in `category_map.csv`, its row shows a blank
  `canonical_category` + `needs_review`. Add a line to the CSV (`raw_category,canonical_category,…`)
  mapping it to a Budget Timeseries line, then re-run Step 3.

---

## Later: monthly automation

The manual Takeout export is fine for a one-time (or occasional) load. A monthly unattended cron
needs **Gmail API access (OAuth)** instead of Takeout — deferred until you want it.
