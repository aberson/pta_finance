# Ask an AI assistant about your finance sheet

You don't have to read the manuals. Paste one of the prompts below into **ChatGPT** or **Claude**
and just ask. The prompts point the AI at this project's **public** code and docs on GitHub
(`https://github.com/aberson/pta_finance`), which it can read on its own. Your **Google Sheet is
private**, so for questions about your actual numbers you paste or upload the tab (see below).

> This page is **generic** — it never names a real organization or person. Fill in the
> `[bracketed]` bits with your own details when you use a prompt.

---

## Two ways to use these

**1. Quick — no setup.** Open ChatGPT or Claude, paste any prompt below, send. Done.

**2. Best — set it up once as a mini "app"** so nobody has to paste the background every time.
In **ChatGPT** choose *Explore GPTs → Create*, or in **Claude** create a *Project*, and paste the
block below into its instructions. (Optional but great: also upload this repo's `README.md`,
`docs/using-the-spreadsheet.md`, `docs/loading-receipts.md`, and `SETUP.md` as knowledge files.)
After that, anyone can just ask their question in plain English.

```
You are a friendly finance helper for a NON-technical PTA / booster club / nonprofit treasurer.
Our finances run on the open-source toolkit at https://github.com/aberson/pta_finance — always
base your answers on that repo's README.md, docs/using-the-spreadsheet.md, docs/loading-receipts.md,
and SETUP.md. Follow these rules:
1. Always answer in plain, non-technical language, with exact click-by-click steps.
2. Enforce the #1 rule: the budget is changed ONLY in the "FY<year> Budget" tab — never in an old
   copy or a duplicate. If I mention editing any other budget sheet, warn me and point me back.
3. Our real numbers live in a private Google Sheet you cannot open. When you need data, ask me to
   paste or upload the relevant tab.
4. Before any command or action that writes/changes data (like "sync-budget --apply"), first
   explain in plain language what it will and won't change, so I can decide.
5. If you're not sure, say so and point me to the right doc or tell me to ask a technical helper.
```

---

## How to give the AI your numbers (for the data prompts)

The AI can read the public GitHub, but **not** your private Sheet. When a prompt needs your data:

- **Paste it:** open the tab in Google Sheets, click any cell, press **Ctrl+A** then **Ctrl+C**,
  and paste into the chat.
- **Or upload it:** in Google Sheets go to **File → Download → Comma-separated values (.csv)**,
  then attach that file to the chat.
- **Shortcut:** if your ChatGPT or Claude has a **Google Drive connector** turned on, you can
  connect the Sheet directly and skip the paste.

---

## Get oriented

**Explain the whole thing like I'm brand new**

```
I just took over the finances for our PTA / booster club and inherited a Google Sheet plus the
open-source toolkit at https://github.com/aberson/pta_finance. Please read its README.md and
docs/using-the-spreadsheet.md, then explain — in plain, non-technical language — what this system
is, what the Google Sheet is for, and the single most important rule I should never break. Keep it
short and friendly.
```

**Give me a menu of what I can do**

```
Using the toolkit documented at https://github.com/aberson/pta_finance (read
docs/using-the-spreadsheet.md and README.md), give me a simple menu of everything I can do with
this system — like "change the budget", "make the monthly report", "load receipts". One plain
sentence each, and mark which ones I can do myself versus which need a technical helper.
```

**Tour the tabs and tell me what I can touch**

```
I'm looking at the Google Sheet behind the toolkit at https://github.com/aberson/pta_finance.
Based on docs/using-the-spreadsheet.md, give me a tab-by-tab tour: what each tab is for and — most
importantly — whether I'm allowed to edit it by hand or should leave it alone. Put it in a simple
table.
```

---

## Do a task, step by step

**Change the budget (exact click-by-click steps)**

```
I need to change our proposed budget in the Google Sheet. Using docs/using-the-spreadsheet.md at
https://github.com/aberson/pta_finance, walk me through it click-by-click: exactly which tab to
open, which column to type the new amount in, how to add a brand-new line, and what I must NOT
touch. I'm not technical, so be very specific.
```

**Add a new budget line**

```
Using https://github.com/aberson/pta_finance (docs/using-the-spreadsheet.md), give me step-by-step
instructions to add a new line item to this year's budget tab — where to put the name, the amount,
and a note — and explain what has to happen next for it to count in the official numbers.
```

**Load reimbursement receipts from email**

```
I want to turn our reimbursement-request emails into the receipts data in the Sheet. Read
docs/loading-receipts.md at https://github.com/aberson/pta_finance and give me the steps in plain
language, including how to make sure I don't miss any emails. Flag any step that needs a technical
helper.
```

**Explain the monthly report**

```
Using README.md and SETUP.md at https://github.com/aberson/pta_finance, explain in plain language
how the monthly financial report gets made, whether it happens automatically, and what I would do
if I wanted to generate one right now.
```

---

## Understand your own numbers

*(These need your data — paste or upload the tab first; see the section above.)*

**Summarize my budget tab**

```
Below is our current-year budget tab, copied from our Google Sheet. In plain language: total the
income and the expenses, tell me the net, list the biggest items on each side, and flag anything
that looks like a typo or a duplicate.

[paste the tab here]
```

**What changed year over year**

```
Attached is our "Budget Timeseries" data from the Sheet (the toolkit at
https://github.com/aberson/pta_finance uses it as its database). Compare this year to last year:
what went up, what went down, and what's new or gone. Give me a short plain-language summary and a
simple table.

[upload the CSV, or paste the data below]
```

**I edited an OLD copy by mistake — find exactly what I changed**

```
I accidentally made my budget edits in an OLD copy of the sheet instead of the live "FY<year>
Budget" tab. Below are BOTH: first the old copy I edited, then the live tab. Tell me exactly which
line items and amounts I changed on the old copy, as a simple list ("change X from A to B", "add
new line Y = Z"), so I can re-enter only those changes into the live tab. Ignore differences that
look like old leftover numbers rather than my edits.

--- OLD COPY I EDITED ---
[paste it here]

--- LIVE "FY<year> Budget" TAB ---
[paste it here]
```

---

## Do it safely

**Explain `sync-budget` before I run it**

```
Before I run the toolkit's "sync-budget" command, explain in plain language what it does, the
difference between the safe preview and the "--apply" version, and exactly what it will and won't
change in our database. Base it on docs/using-the-spreadsheet.md and the code at
https://github.com/aberson/pta_finance. I want to be sure it's safe.
```

**Is this preview safe to apply?**

```
Here's the preview (dry-run) output from the toolkit's "sync-budget" command at
https://github.com/aberson/pta_finance. Go through it in plain language: what will change, and
explain any line it flagged (a rename, a duplicate, a removed line). Then tell me whether it looks
safe to apply, or if I should fix something on the tab first.

[paste the preview output here]
```

---

## Get something new built

**Can I get a dashboard that shows ___ ?**

```
Using the toolkit at https://github.com/aberson/pta_finance (see how the dashboards are described
in docs/using-the-spreadsheet.md), I'd like a new view in the Sheet that shows
[DESCRIBE IT — e.g. "spending by grade over the last 3 years", or "a dashboard I can filter by
event"]. Is that possible with this system? If so, write a clear request I can hand to a developer
or an AI coding assistant to build it.
```

**Draft a help request for a technical helper**

```
I'm stuck on [DESCRIBE THE PROBLEM] with our finance sheet and the toolkit at
https://github.com/aberson/pta_finance. Read the relevant docs there and write a clear, complete
help request I can send to a technical helper — what I'm trying to do, what I've tried, and the
exact tab or command involved — so they can help me quickly.
```

---

## A few tips

- **Fill in the `[brackets]`** with your own details before sending.
- **The AI can read the public GitHub, not your private Sheet.** For anything about your real
  numbers, paste or upload the tab.
- **Double-check before you act.** These assistants are helpful but can be wrong — for any step
  that changes data, confirm it against [docs/using-the-spreadsheet.md](using-the-spreadsheet.md),
  and always run the **preview** of a command before the real one.
- **Never paste private data into a shared or public AI you don't trust.** Stick to an assistant
  your organization is comfortable using for financial information.
