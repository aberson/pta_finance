# FY2027 budget adoption — folding the members' "Pending Approval" expense budget in

> **Status:** Phases 1–3 DONE — Board-approved and APPLIED 2026-07-20 (see Execution log below).
> Phases 4–5 (archive old copies; process close-out) remain. Plus the tagging follow-up.
>
> **Companion (private, gitignored):** `reports/output/fy27-budget-reconciliation.md` holds the real
> amounts, the full line-by-line delta table, and the board's rationale notes. This plan stays
> generic (no org name, no dollar figures) so it is safe on the public repo.

## 1. What this is

PTA members worked a **copy** of the sheet into an expense-only "Enrichment Emphasis Plan" — now the
`FY27 Pending Approval Expense Budget` tab — with two scenarios (*Initial Deficit* / *Zero Deficit*)
and a written board rationale per line. The **Zero-Deficit column is the proposed budget**: it
applies 18 expense cuts + 2 new lines and turns the projected deficit into a surplus. This plan
adopts that column as the FY2027 proposed budget in the live system, **preserving the rationale
notes** (needed to justify choices to external groups) and **archiving the superseded copies**.

The reconciliation is done and **verified**: the members' *Initial* column equals our current budget
on every mapped line to the penny, so the name-mapping (their informal names → our canonical line
names) is confirmed. See the companion file.

## 2. Inputs and the mechanism this reuses

- **The one live budget tab** is `FY2027 Budget` (editable). It syncs into the `Budget Timeseries`
  database — the single source `report`/`analyze` read — via `uv run pta-finance sync-budget --fy 2027`
  (preview) / `--apply`. That tool already: snapshots first, writes only changed amount/notes cells,
  appends new lines, **carries notes through to the database**, never touches actuals/other-years/tags,
  and flags (never deletes) removed lines. This build **reuses that path** — no new tooling required.
- **The name gap is handled by transcribing into canonical names**, not by syncing the members' tab
  directly (their informal names would create duplicate rows). The mapping is in the companion file.
- **The operator guide** `docs/using-the-spreadsheet.md` already documents this exact situation in its
  *"I already edited an old sheet — now what?"* section. This plan is that section, executed.

## 3. Decisions to confirm before Phase 2 (operator/board)

These gate the build; they are enumerated with specifics in the companion file §8:

1. Board has **approved** the Zero-Deficit column as the FY2027 budget.
2. **Fundraising bundle:** the 11 self-funded event lines stay at current amounts (the members
   summarized them as one rounded line marginally low; they proposed no cut to them).
3. **New lines** (two) are confirmed for inclusion.
4. **Home for the adopted budget:** overwrite `FY2027 Budget` in place (Step 2a) **or** stand up a
   distinct approved-budget tab (Step 2b). Recommendation: in place — `FY2027 Budget` *is* the
   proposed-budget tab by design; a second tab reintroduces the copy-drift this system exists to avoid.

---

## Phase 1 — Sign-off + decision capture

### Step 1: Confirm the four decisions in §3

- **Type:** operator
- **Problem:** The tab is *Pending Approval*; adopting it changes what every report shows. Approval +
  the three data confirmations must be explicit before any write.
- **Produces:** the four decisions recorded (a note on the companion file is enough).
- **Status:** PENDING

---

## Phase 2 — Land Zero-Deficit as the FY2027 proposed budget

### Step 2: Transcribe the Zero-Deficit amounts + rationale into `FY2027 Budget`

- **Type:** data (operator-run, or agent-assisted with the verified mapping)
- **Problem:** The live `FY2027 Budget` tab still holds the current amounts and blank expense notes.
  It must reflect the approved Zero-Deficit amounts, the two new lines, and the board rationale — all
  under **canonical line names** so `sync-budget` matches existing rows instead of duplicating them.
- **Do:**
  1. For each of the 18 changed lines: set column B to the Zero-Deficit amount and column C to the
     board rationale (mapping + text in the companion file §3).
  2. Add the two new lines (companion §5) under the correct sections, with their notes.
  3. Leave the 11 self-funded event lines and all 21 maintained lines unchanged.
  4. Leave the INCOME section untouched.
- **Guard:** do **not** rename any existing line (renames lose the hidden `strategic_group` /
  `strategic_goal` tags). Transcribe into the names already on the tab.
- **Produces:** an updated `FY2027 Budget` tab, not yet synced.
- **Status:** PENDING

### Step 3: Preview the sync (writes nothing)

- **Type:** code (CLI)
- **Do:** `uv run pta-finance sync-budget --fy 2027`
- **Expect:** ~18 amount changes, ~2 new lines, ~18 note changes; **zero suspected renames** and
  **zero duplicates**. Any rename/duplicate flag means a Step-2 name didn't match a canonical line —
  fix the tab and re-preview before applying.
- **Status:** PENDING

### Step 4: Apply the sync

- **Type:** code (CLI)
- **Do:** `uv run pta-finance sync-budget --fy 2027 --apply` (snapshots the database first).
- **Verify:** `uv run pta-finance report --fy 2027 --variant internal` — the proposed expense total
  drops to the Zero-Deficit figure and net flips from deficit to surplus (companion §1).
- **Status:** PENDING

---

## Phase 3 — Preserve the rationale durably

### Step 5: Confirm notes reached the database

- **Type:** code (verification)
- **Problem:** The board rationale is the external-justification record; it must live *with* the
  budget, not only in the private reconciliation file.
- **Do:** confirm the `notes` column on `Budget Timeseries` now carries each cut's rationale (they
  ride through `sync-budget` from column C). Spot-check the highest-scrutiny lines.
- **Status:** PENDING

---

## Phase 4 — Archive the superseded copies

### Step 6: Retire the disconnected budget tabs

- **Type:** operator
- **Problem:** Several old, disconnected full-budget tabs (prior-year copies, the members' pending
  tab, "suggestions" copies) invite exactly the copy-then-rework drift this build is closing. Per the
  operator guide's archive step, they should be unmistakably retired.
- **Do:** once the Zero-Deficit budget is live and verified, rename each superseded tab to
  `ARCHIVE — DO NOT EDIT (replaced by FY2027 Budget)` (or delete). Keep the members' `FY27 Pending
  Approval Expense Budget` tab as a dated archive of the board's decision if useful for the record.
- **Guard:** never delete a tab other live tabs pull cell references from — check first. Snapshot the
  spreadsheet before any deletion.
- **Status:** PENDING

---

## Phase 5 — Close the loop on process + docs

### Step 7: Point future edits at the one live tab

- **Type:** operator + docs
- **Problem:** The root cause was members working in a copy. The durable fix is process, not tooling.
- **Do:** ensure `docs/using-the-spreadsheet.md` is shared with the people who touch the budget; going
  forward, budget changes are made **directly in `FY2027 Budget`** and synced — which removes the
  transcription step this whole plan exists to perform.
- **Status:** PENDING

---

## Execution log (2026-07-20)

_(Generic — all real amounts and line names live in the private companion file.)_

- **Phase 1 — decisions:** Board approved the Zero-Deficit column. Confirmed: keep the self-funded
  event lines at current amounts; the two new lines filed under their strategic sections; rationale
  notes carried on every line.
- **Phase 2 — transcribe + sync:** snapshotted `FY2027 Budget` + `Budget Timeseries`; transcribed the
  Zero-Deficit amounts + board notes into the existing canonical line names; inserted the two new
  lines *inside* their section `SUM` ranges (the tab total recomputed to the Zero-Deficit figure).
  `sync-budget --fy 2027` preview showed only the expected amount + note changes and the two new
  lines — **zero suspected renames, zero duplicates** — then `--apply`.
- **Phase 3 — notes durable:** verified every FY2027 proposed-expense row in `Budget Timeseries`
  carries its rationale note; net flipped from deficit to surplus (figures in the companion file).
- **Post-adoption dashboard refresh:** tagged the two new lines' `strategic_group` / `strategic_goal`
  (removing a phantom untagged group); recomputed the derived breakdown tables from live data — the
  Spending-by-Goal even-split matrix, the Income & Expense Breakdown color-key + item table, and the
  Year-over-Year + Forecast FY2027 figures — and regenerated the two manual-insert PNGs. QUERY/pivot
  dashboards auto-update.

**Still open:** (a) the `FY27 Strategic Budget` tab still reflects the pre-adoption scenario — refresh
or archive it separately; (b) Phase 4 archive of the old disconnected copies + the members' source tab.

## Not doing (and why)

- **No importer tool for the members' tab.** A one-time transcription through the existing, safe
  `sync-budget` path is cheaper and lower-risk than new code; the long-term fix is editing the live
  tab directly (Step 7), which makes any importer moot.
- **No income changes.** The members' work is expense-only.
- **No direct `Budget Timeseries` hand-edits.** All writes go through `sync-budget` so the snapshot +
  cell-targeted-write safety applies.
