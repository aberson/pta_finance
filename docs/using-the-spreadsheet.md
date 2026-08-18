# Working with your finance spreadsheet — a plain-language guide

This guide is for the **non-technical person** who keeps the books — a treasurer, a board
member, or whoever inherits the spreadsheet next. You do **not** need to know any code to use
it. It explains what the spreadsheet is, which tabs you can safely touch, the one correct way to
change a budget, and how the charts and dashboards get made.

> This guide is **generic** — it never names a real organization, school, or person. Your own
> details live only in the private setup, not in this public repository.

> **Prefer to just ask?** You can paste ready-made prompts into ChatGPT or Claude instead of
> reading this — see **[ask-an-ai-assistant.md](ask-an-ai-assistant.md)**.

---

## The one rule that saves everyone time

**The spreadsheet _is_ the database.** It is not a set of loose worksheets you copy and rework —
it is one living system, and the tools that make the reports read straight out of it.

So there is exactly **one** place to change a year's budget: the tab named **"FY&lt;year&gt;
Budget"** (for example `FY2027 Budget`). Make your changes there.

> ### ⛔ Never copy, duplicate, or edit an old budget sheet
> An old "Budget 2026-2027" sheet — or any spare copy — **is not connected to anything.** Numbers
> you change on a copy don't reach the reports, the dashboards, or the database. To count, they
> have to be **re-typed by hand** into the real tab first — which is slow, error-prone, and the
> exact chore this system exists to avoid. If you're not sure a tab is the live one, it almost
> certainly isn't: the live one is named **`FY<year> Budget`** and nothing else.

A picture that helps: think of the spreadsheet as a building. The **`FY<year> Budget`** tab is the
**front desk** where you drop off changes. The other tabs are back rooms — some store the official
records, some run the reports, and many are wired together with hidden formulas. Working in a
photocopy of the front desk that's sitting out in the parking lot changes nothing inside the
building.

> ### 🔒 Make the rule impossible to break by accident (recommended)
> A rule you have to *remember* isn't as good as one the sheet enforces for you. To stop anyone
> from editing the wrong tab in the first place, an owner can **lock every tab except
> `FY<year> Budget`**: in Google Sheets, go to **Data → Protect sheets and ranges**, add each of
> the other tabs, and set it to *warn* or *restrict* editing. Then the only tab that's freely
> editable is the one you're *supposed* to edit — and any leftover old budget copies should be
> renamed to `ARCHIVE — DO NOT EDIT` or deleted so they can't be mistaken for the live tab.

---

## The three kinds of tabs

Every tab is one of three kinds, and the **"Can you edit it?"** column below tells you which:

1. ✅ **The one you fill in** — `FY<year> Budget`.
2. ⚠️/❌ **The ones that run themselves** — the database and the dashboards; look and filter, but
   don't hand-edit.
3. 📖 **Reference tabs** — settings and recipes to read, not change.

Your sheet won't have every tab listed here, and it may have a few more — but each one falls into
one of those three kinds.

| Tab | What it's for | Can you edit it by hand? |
|---|---|---|
| **`FY<year> Budget`** (e.g. `FY2027 Budget`) | The **one** tab you hand-edit to set or adjust a year's proposed budget. | ✅ **Yes — this is the point.** Edit freely, then sync it in (below). |
| **Budget Timeseries** | The actual **database**: every year's numbers in one long list. Every report and dashboard reads from here. | ⚠️ **Normally no.** The sync tool updates it *for* you from your `FY<year> Budget` tab. Hand-editing works but is easy to get wrong. |
| **`FY<year> - Public Budget`** | The **member-facing, publishable** budget: last year's actuals next to this year's proposed amounts, with deltas, category subtotals, and totals. Per-fundraiser costs are combined into one "Fundraising Expenses" line. | 📖 **Share it; don't budget in it.** It's a generated snapshot of `FY<year> Budget` + the database — to change a number, edit `FY<year> Budget` and ask for a regeneration. |
| **Reimbursements** | An **auto-built** list of receipt line items, rebuilt from the treasurer's email. | ❌ **No.** It's machine-owned — any hand edit is wiped the next time it's rebuilt. |
| **Dashboards** — e.g. *Group Explorer, Receipts Explorer, Spending by Goal, Income & Expense Breakdown, Pivot / Explore, Year-over-Year, Forecast* | Interactive views. You pick a value from a **dropdown** and the chart and table redraw. | 🔁 **Use the dropdowns; don't hand-edit the cells or formulas.** They rebuild themselves from the database. |
| **Assumptions** | The tunable constants (the "settings") plus a written record of how the analytics were built. | ⚠️ **Mostly leave alone.** There's a clearly-marked "deletable" area for scratch notes. |
| **Rebuild Playbook** | A step-by-step recipe for recreating all the analytics from scratch. | 📖 **Reference only.** Don't edit — read it if you ever need to rebuild. |

**Rule of thumb:** if a tab is full of dropdowns, charts, or long formula-driven lists, it's a
back room that runs itself — look, filter, but don't retype. The only tab you *fill in* is
**`FY<year> Budget`**.

---

## How to change the budget (the normal path)

### Step 1 — Edit the `FY<year> Budget` tab

Open the tab named for the year you're working on — e.g. **`FY2027 Budget`**.

> **What does "FY2027" mean?** It's the label for the fiscal year, not a random number. For an
> org whose fiscal year starts in a school-year month (say July), **FY2027 is the 2026–2027
> school year**. For an org on the calendar year, FY2027 is simply Jan–Dec 2027.

The tab is laid out as plain rows under two big all-caps banners — **INCOME** and **EXPENSE** —
with grouped line items beneath each. Three columns matter:

| Column | Holds | Example |
|---|---|---|
| **A** | The line-item name | `Fall Carnival` |
| **B** | The **proposed dollar amount** | `3500` |
| **C** | A note (optional) | `up from last year — bigger event` |

- **To change an amount:** type the new number in **column B** next to the line. That's it.
- **To add a line:** insert a new row in the correct section (under INCOME or EXPENSE), put the
  name in **column A**, the amount in **column B**, and any note in **column C**.
- **Leave the structure alone:** don't rename or delete the **INCOME** / **EXPENSE** banners
  (they tell the tool which section a line belongs to), and don't retype the computed rows like
  `Subtotal — …`, `TOTAL`, or `NET (…)` (those add themselves up).
- A **group sub-header** is just a row with a name in column A and columns B and C left blank —
  that's how the tool knows the lines under it belong to that group.

Save your work. Your edits now live in that tab, waiting to be pulled into the database.

### Step 2 — Pull your edits into the database (the `sync-budget` command)

One command reconciles your `FY<year> Budget` tab into the **Budget Timeseries** database. This is
normally run by whoever set up the toolkit, on the computer where it's installed — they open a
terminal (Command Prompt or PowerShell on Windows) in the project folder and type the commands
below. If you set the toolkit up yourself, you can run them too (see [SETUP.md](../SETUP.md)).

**First, preview** — this is completely safe: it shows you exactly what would change and writes
nothing.

```bash
uv run pta-finance sync-budget --fy 2027
```

Read the summary it prints (so many amount changes, so many new lines, anything flagged). When it
looks right, **apply** it:

```bash
uv run pta-finance sync-budget --fy 2027 --apply
```

(Omit `--fy 2027` and it uses the **current** fiscal year automatically.)

**What the command promises — this is why it's safe to run:**

- It **backs up** the whole database first (a timestamped copy), then writes **only** the amounts
  and notes you actually changed, and adds any new lines.
- It **never touches** actual (spent) numbers, any other year, or the extra tag columns.
- A line you **removed** from the tab is **flagged, not deleted** — so you can never lose data by
  forgetting a row.
- If you **renamed** a line, the preview flags it as a *suspected rename* (it shows up as one line
  removed and one added). This does **not** stop the sync — so read the preview before you apply.
  If it really is a rename, don't rename it on the tab; ask a maintainer to change the name
  directly in the Budget Timeseries, so the line keeps its dashboard tags (the hidden category and
  goal labels that make it show up correctly in the charts).
- If the same line appears **twice**, it **stops and makes you fix it** before writing anything —
  that's the one thing it refuses to guess.

---

## "I already edited an old sheet — now what?"

This is the exact situation this guide exists to prevent, so if it already happened: **nothing is
broken — your changes just aren't connected yet.** Here's the clean way to bring them in:

1. **Stop editing the old sheet.** Every further change on it widens the gap you'll have to close.
2. **Write down what you changed** — ideally as a short list: *"I changed these line items to
   these amounts, and added these new ones."* In the old sheet, it helps to **highlight the exact
   cells you changed** so only your intended changes get carried over — not any stale numbers that
   were already sitting in that copy. Can't remember exactly what you touched? Google Sheets keeps
   a full history — **File → Version history → See version history** shows every change and when it
   was made, so your edits can be reconstructed after the fact.
3. **Hand it to whoever maintains the toolkit** (or an AI coding assistant, if your organization
   uses one for this): *"Here's the old sheet, here are my changes."* They transcribe your changes
   into the live **`FY<year> Budget`** tab and run `sync-budget` to push them into the database.
4. **Archive the old copy so it can't be reused.** Once your changes are safely in, rename the old
   sheet to something unmistakable — e.g. **`ARCHIVE — DO NOT EDIT (replaced by FY2027 Budget)`** —
   or delete it. A leftover copy that's still named like a real budget is exactly the trap that
   started this.
5. **From now on, edit the `FY<year> Budget` tab directly** — then this transcription step
   disappears entirely, which is the whole point.

---

## How the dashboards and charts were built (and how to get a new one)

You never have to build a dashboard by hand — **you ask for what you want to see, and it's
generated for you.**

- Every analytics and dashboard tab in this sheet was created by a small program run through the
  toolkit's connection to Google Sheets — not typed by hand.
- The **Rebuild Playbook** tab is the written recipe for all of it: a step-by-step sequence that
  recreates the database view, the year-over-year charts, the forecast, and the interactive
  explorers from scratch, along with the exact lookup tables they depend on. If the analytics ever
  get scrambled, that tab is how they get rebuilt cleanly.
- **Want a new view?** Just describe it in plain English — *"show me spending by grade over the
  last three years,"* or *"a dashboard I can filter by event."* A new tab gets built the same way,
  and because it reads from the **Budget Timeseries** database, it stays up to date on its own.
- **Why the dashboards use dropdowns:** Google Sheets' built-in pivot tables can't be driven by a
  dropdown, so these dashboards are built a different way (dropdown menus + live query formulas +
  charts). That's why you steer them by changing the **dropdown cells**, not by editing the table
  underneath.

---

## The safety rules the tools always follow

So you're never nervous about running a command, here's what the tools will and won't do:

- **They back up before they write.** Every change is preceded by a timestamped snapshot, so the
  previous state can always be recovered.
- **On the tabs you edit, they write only the specific cells they own.** `sync-budget` changes
  just the amount and note cells you actually touched in the database — it never wipes the whole
  tab and retypes it, so your hand-typed notes and any hand-maintained rows are safe. (The one
  tab that _is_ fully rebuilt each time is the machine-owned **Reimbursements** list — by design,
  which is why you never hand-edit that one.)
- **They never move rows around,** because other tabs point formulas at exact cells — moving a row
  would silently break a chart or a total elsewhere.
- **They write real numbers,** so Google's own `SUM` and `QUERY` formulas count them (a number
  stored as text gets silently skipped from totals).
- **They surface anything unclear** — a rename, a duplicate, a removed line — instead of quietly
  guessing.

---

## Where each guide lives

| Guide | What it covers | Who it's for |
|---|---|---|
| [README](../README.md) | What this toolkit is, overall | everyone |
| **This guide** | Day-to-day: which tabs to touch, how to change the budget | **you — the non-technical operator** |
| [SETUP.md](../SETUP.md) | One-time: connecting the toolkit to your Google Sheet | whoever runs the tools |
| [docs/loading-receipts.md](loading-receipts.md) | Turning reimbursement emails into the Receipts data | whoever runs the tools |
