# Treasurer Slides staged roadmap

This roadmap supersedes the earlier monolithic Phase 5 plan as of 2026-08-31. The prior plan was
reviewed and issue-synced, but implementation stopped before any Treasurer Slides production code
landed. Its design remains available in git history; its old Step 14-26 issue bodies are stale and
must not drive a build.

The current executable plan is
[`treasurer-summary-wave-1-plan.md`](treasurer-summary-wave-1-plan.md).

> **Identity rule.** This repository is public and the finance data is private. No real
> organization, school, person, email address, account identifier, financial value, Google file
> identifier, OAuth credential, statement, extracted transaction, run bundle, template, candidate,
> screenshot, or quality-assurance evidence may enter tracked files. Private artifacts stay under
> configured gitignored paths, principally `reports/output/` and `secrets/`.

## Why the build is staged

The first plan attempted to specify a general presentation system, fourteen graphic capabilities,
multiple audiences, a large approval state machine, and extensive Google hardening before proving
the smallest useful result. The private design prototype established a better sequence: first make
one valuable summary slide repeatable from real inputs, then add a small financial story, then add
operational rigor, and only then grow the catalog in independently useful slices.

Each wave must:

- produce an operator-usable result on its own;
- preserve every previously accepted output unless a later plan explicitly versions it;
- receive its own feature plan, plan review, fresh-context wrap, issue synchronization, build, and
  attended acceptance;
- keep the finance workbook read-only unless a separately approved plan explicitly changes that
  boundary; and
- stop before the next wave so lessons from real use can change later scope.

## Global step ranges

The project completed Steps 1-13 before Treasurer Slides. Global numbering remains collision-free:

| Wave | Reserved steps | Outcome | Status |
|---|---:|---|---|
| 1 - Summary slide | 14-25 | Repeat the approved one-slide cash snapshot from private bank statements and budget goals, then create one editable private Google Slide | Prepared in `treasurer-summary-wave-1-plan.md`; umbrella #40, steps #41-#52 |
| 2 - Core financial story | 26-30 | Expand to a small deck with a few high-value graphics | Outline only; plan after Wave 1 acceptance |
| 3 - Operational rigor | 31-35 | Harden provenance, revisions, Google behavior, visual QA, and audience policy | Outline only; plan after Wave 2 acceptance |
| 4+ - Capability slices | 36 onward | Add one bounded topic or operating capability per plan | Backlog only |

Reserved ranges are planning boundaries, not authorization to build an unplanned wave.

## Wave 1 - Summary slide

Wave 1 is intentionally narrow:

- Wells Fargo PDF inputs, including local optical character recognition (OCR) fallback;
- Checking, Savings, and Time Account (Buffer) roles;
- overlap selection, transfer/reversal handling, pending activity, and exact operator exclusions;
- annual fundraising and expense goals read from `Budget Timeseries`;
- one reviewed fact snapshot with exact cash reconciliation;
- the approved one-slide visual and narrative contract; and
- one private, editable Google Slide created from an app-owned private template.

Minimum correctness and privacy controls ship now. A failed extraction, ambiguous classification,
unmatched exclusion, broken reconciliation, missing template role, wrong approval digest, or wider
OAuth grant blocks output.

See the detailed Wave 1 plan for exact contracts, affected files, Steps 14-25, and acceptance.

## Wave 2 - Core financial story

Wave 2 will start only after the operator accepts the real Wave 1 Google Slide. Its provisional
goal is a concise three-slide deck:

1. the unchanged Wave 1 financial snapshot;
2. **What we plan to spend money on** - projected expense composition from FY proposed expense
   rows, summarized by `category_group` with the detailed `raw_category` lines visible beside the
   graphic; and
3. **What has happened so far** - actual spending mix, fundraising collection channels, and
   progress toward annual goals, with unclassified coverage disclosed rather than inferred.

The initial graphic set is deliberately small:

- one projected-spending donut or pie with a detailed legend/table;
- one actual-spending composition graphic; and
- one fundraising-source/progress graphic.

Wave 2 will decide chart family, density, category hierarchy, and multi-slide template changes from
Wave 1 evidence. It will not revive the old fourteen-graphic catalog wholesale.

## Wave 3 - Operational rigor

Wave 3 adds controls whose value becomes clearer after the useful workflow exists:

- richer immutable run manifests and source lineage;
- multi-stage digest-bound approvals and linked revisions;
- cross-run/source-refresh drift detection and rendered template-drift diagnosis beyond Wave 1's
  digest/projection gates;
- bounded Google retry, timeout, revision reconciliation, and duplicate recovery beyond Wave 1's
  create-once checks;
- presentation readback, rendered-thumbnail comparison, and stronger layout QA;
- explicit internal/public audience policies and runtime private-data guards;
- packaging and recovery procedures for a successor operator; and
- retention/archive policy for local runs and remote candidates.

Wave 3 may strengthen Wave 1 controls, but it may not weaken the Wave 1 reconciliation, approval,
read-only source, template immutability, or privacy boundaries.

## Wave 4+ - Capability slices

Each item below becomes its own scoped plan only when prioritized:

- historical year comparisons;
- reserve and sustainability projections;
- reimbursement commitments and outstanding obligations;
- richer budget-versus-actual views;
- appendix/detail slides;
- additional registered graphics;
- alternate themes or reusable organization profiles;
- additional bank formats such as CSV, QFX, or OFX;
- insertion into a larger human-maintained presentation;
- scheduling and unattended generation;
- sharing/publishing workflows; and
- optional assisted narrative drafting.

No item is implicitly part of Wave 1-3.

## Cross-wave invariants

1. **Private inputs and outputs remain untracked.** Tests and documentation use only fictional
   organizations, values, account roles, and resource identifiers.
2. **The finance workbook is a read-only source for Slides.** Existing budget-edit and sync
   workflows remain separate.
3. **Bank facts and budget facts remain distinct.** Bank activity supplies cash actuals; the
   workbook supplies adopted targets until a later plan establishes another authoritative source.
4. **Unknown data is visible or blocking.** No parser, classifier, renderer, or narrative silently
   invents a value or category.
5. **The approved summary is versioned, not casually edited.** Later waves reuse it or deliberately
   create a new visual contract.
6. **Google output is private by construction.** The tool never changes permissions, publishes,
   emails, or inserts a final into another deck without separately planned operator authority.
7. **Every command is one-shot and operator-triggered.** No autonomous, scheduled, or always-on
   behavior is authorized by this roadmap.

## Plan and issue transition

The previous umbrella #26 and step issues #27-#39 were created from the superseded monolithic plan
and are closed with a generic supersession note. The reviewed and fresh-context-wrapped Wave 1 plan
is synchronized as umbrella #40 and steps #41-#52; its `Phase 5.1` namespace keeps those issue bodies
distinct from the stopped `Phase 5` set.

Next command:

```text
/build-phase --plan documentation/treasurer-summary-wave-1-plan.md
```

`/build-phase` runs code through Step 24 and defers operator Step 25 into its phase-end Manual UAT
bundle for attended private acceptance.
