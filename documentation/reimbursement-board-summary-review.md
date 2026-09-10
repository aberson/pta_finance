Reviewing as: feature plan. Sections 17–21 apply.

# Reimbursement board summary — plan review

Reviewed 2026-09-10 against `cc93c2d` and the planning changes in the working tree.
Target: [reimbursement-board-summary-plan.md](reimbursement-board-summary-plan.md).
All Issue fields are blank; no issue synchronization has occurred.

## Blockers

None.

## Significant gaps

Resolved during this review:

1. **Preview publication needed a concrete all-files boundary.** The original §6.7 promised
   that failed rendering preserved the prior output set, but described replacement of several
   files. The corrected §5/§6.7 defines immutable preview directories and an atomically
   replaced `current-preview.json` pointer. Finalization locks the run and validates exactly
   that selected receipt. A failed replacement cannot mix a prior PDF with new HTML.
2. **Runner argument forwarding could undermine dry-run or summarize the wrong bundle.**
   §6.6 now reserves `--data` and `--dry-run` to the runner and rejects them after the
   argument separator. Both prepare and refresh use the same selected data path. This is
   grounded in `pta_finance/cli.py:1075` and `:1154–1178`, where the existing update handler
   owns its data and dry-run branches.
3. **Steps 27 and 29 needed deeper review at producer/consumer seams.** Their original plain
   code-review flags covered the new report-model consumer and refresh-to-summary wiring.
   Per the trigger-list owner, `../.claude/skills/review-deep/core.md`, opening
   “Review Deep” section, those are producer/consumer changes with a costly silent-miss risk.
   Flags now use `--reviewers deep`, preserving isolation flags. Consider the provider's
   high-stakes review tier for these steps; no model override is set by this plan.

## Missing items

None remaining. The review added the preview-pointer shape and identifier, template-copy
retention, and explicit payment-note method/date consistency rules. The initial plan already
includes install, development, build, test, lint, typecheck, wheel verification, and a real
Windows acceptance procedure as planned work.

## Nice-to-haves

None required. Blank Issue fields are the expected pre-sync state; populate them through
repo-sync before build-phase. A hosted application, scheduler, and new AI service are outside
the requested feature.

## Checks and evidence

| Checks | Result / evidence |
|---|---|
| 1–5: storage, dependencies, secrets, concurrency, errors | Pass. Plan §5/§6 defines private files, frozen inputs, staged publication, locks, failure receipts, and setup errors. Existing OAuth is reused solely through the existing command. |
| 6–10: tooling, decisions, setup, idempotency, seams | Pass. §9 covers all six toolchain verbs; §6 defines command inputs/results and final retries. Proposed defaults are selected choices with stable D IDs, not unresolved implementation alternatives. |
| 11–14: scope, security, tests, operations | Pass. Summary-only commands are offline, output is constrained, text is escaped, and no payment/Sheet authority is added. §9 plus Steps 30–31 cover automated and private operation. |
| 15: autonomous observation | N/A. Explicitly one-shot and operator-invoked; monthly describes use, not a scheduler. |
| 15.5: data-pipeline smoke | Pass. Step 30 wires actual local archive/refresh/summary/render/finalize components with a 60-second installed-environment budget. |
| 16–18: context, existing code, impact | Pass. Producing files and all existing callers were read/searched before writing. `load_bundle` at report:1437, `render_html` at :1526, `build_report` at :1580, refresh functions at pipeline:1921/1947, and CLI update at :1075 match the plan. Existing signatures are unchanged. |
| 19: conflicts and conventions | Pass. `CLAUDE.md` identity/privacy rule retained. Sibling slide plan reserves 14–25; new 26–31 do not collide. Source examples are fictional; accepted real artifacts are ignored. |
| 20–21: context and step sizing | Pass. Six steps each have one observable behavior; schemas, identifiers, dates, source semantics, and recovery are inline. |
| 22–25: parseable steps and reviewer routing | Pass after fixes. Six `### Step N:` units have Problem/Type/Issue/Files/Done-when fields. Operator acceptance produces no code. No conditional steps or server-based reviewer flags. |
| 26: live environment seam | Pass. Step 31 requires the actual Windows machine, command invocation, PDF review, and immutable-final retry evidence. |
| 27: stakes-aware review | Pass after escalating Steps 27 and 29. Steps 26/28 already deep; Step 30 is a synthetic smoke harness and Step 31 is operator work. |
| Control-plane hooks | Pass. Root plan links the scoped plan; objective and pending step statuses are explicit; no port is added. |

Search used for caller verification:
`rg -n '\b(load_bundle|render_html|build_report|plan_bundle_refresh|refresh_bundle|_cmd_update_reimbursements)\(' pta_finance tests scripts`.
Only “optionally” in the unresolved-choice scan describes existing Gmail behavior;
it is not a deferred architecture decision. A structural scan found six sequential steps,
complete required fields, and no populated Issue fields. Referenced existing producer paths
were checked on disk; new components are explicitly marked planned.

Auto-applied 2 reviewer-routing fixes:

- Stakes-aware reviewer escalation: Step 27.
- Stakes-aware reviewer escalation: Step 29.

The publication and forwarding clarifications above were folded into the authored design.
The corrected plan was rechecked against all applicable sections; no remaining findings
need operator input. Next in the workspace sequence: plan-redline, then plan-wrap.

## Plan-expedite recheck — 2026-09-10

The operator accepted the plan for issue preparation. Re-ran all applicable review checks
against the current source and six PENDING steps. Phase 6 now names the issue namespace;
Steps 26–31 and the implementation scope are unchanged. D labels retain their provenance,
with the plan go-ahead recorded rather than inventing individually selected choices.
The prior fixes remain present. No source signatures or schemas changed since the inspected
baseline, no conditional steps are present, and all six steps satisfy the rich-body threshold.

Blockers: None. Significant gaps: None. Missing items: None. Nice-to-haves: None required;
blank Issue fields are expected until the following repo-sync step.

Auto-applied 0 fixes. Plan is ready for `/plan-wrap` and `/repo-sync`.
