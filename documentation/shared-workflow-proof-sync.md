# Shared workflow proof issue synchronization

Completed 2026-09-11 against `aberson/pta_finance`, default branch `main`, after the recorded technical review, redline publication and plan-wrap. The operator authorized planning closeout, issue synchronization and Phase A through Step 32; M6 remains attended.

Source: [shared-workflow-proof-plan.md](shared-workflow-proof-plan.md), published at planning commit `2b74e25`. [Umbrella #62](https://github.com/aberson/pta_finance/issues/62) links all four steps.

| Execution phase | Step | Issue | Dependency |
|---|---|---|---|
| A | 32: Authenticated shared comments | [#63](https://github.com/aberson/pta_finance/issues/63) | Planning preparation |
| A | 33: Deployed identity/comments/persistence proof, M6 | [#64](https://github.com/aberson/pta_finance/issues/64) | Step 32 |
| B | 34: Reviewer-to-processor handoff | [#65](https://github.com/aberson/pta_finance/issues/65) | Checked M6 record and Step 33 DONE |
| B | 35: Deployed handoff acceptance, M7 | [#66](https://github.com/aberson/pta_finance/issues/66) | Step 34 |

## Verification

- Resolved the repository and default branch before mutations; checked all 61 existing issues for matching Phase 7 titles and source-plan footers. No matching issues or cross-plan collisions existed.
- Recognized both footer forms: `Synced from [<plan>](...)` and `Enriched by /repo-sync from <build-doc-path> @ <sha>`.
- All four plan steps contain rich problem, files, acceptance, output and dependency fields. Code steps preserve `--reviewers deep --isolation worktree`; the required local HTTP/Playwright/emulator smoke supplies browser evidence.
- Created one umbrella and four step issues, backfilled all four plan Issue fields, and updated the umbrella with the actual step links.
- Read back and compared all five complete issue bodies after newline normalization. All 61 pre-existing issue titles, states and bodies remained unchanged.
- No code, cloud acceptance or deployment is implied by issue synchronization.

## Execution arrangement

Step 32 remains one formal code step and one integrated completion gate. Internal work packets are: contracts/configuration/fictional fixture; verified identity; durable comment transactions; browser/API integration; packaging/CI/runbook; integrated verification. Identity and store work may proceed independently once their shared contracts are established. Packaging preparation can overlap but must be verified against the integrated application.

Use one build/integration owner for shared dependencies, CI, documentation and plan state. A separate operator window can gather cloud prerequisites read-only and later owns M6 deployments. The emulator uses 8787 and the smoke server uses 8788; do not run competing smoke servers. Sibling summary/Slides work requires separate worktrees and coordinated shared-file integration.

The serial path is preparation -> Step 32 -> inspected image -> identity deployment and subject binding -> comments deployment -> M6 -> checked Step 33 DONE -> Step 34 -> inspected new image -> M7. Do not invoke this plan as one unattended span. Preserve the approval namespace; test not-approval in a fresh namespace.

Plan pipeline: /plan-review + /plan-wrap -> /repo-sync (step 4 of 5) -> /build-phase.

## Step 32 review boundary — 2026-09-11

> Historical checkpoint, superseded by the bounded delivery below.

The isolated candidate contains the optional authenticated comments service, packaged fictional fixture, Firestore transactions, browser interface, staging/image recipes, tests, CI changes and attended runbook. Feature code remains unmerged and unpushed. Step 32/#63 stays open and PENDING; Steps 33–35 remain pending.

The final frozen-source Windows run collected 1,090 cases: 1,087 passed, three unchanged existing skips, and zero failures/errors. All 140 web cases ran without skips; the temporary-PyYAML supplement passed all nine workflow tests. Strict package mypy, Ruff lint/format, installed-wheel browser/emulator smoke (6.23 seconds after readiness), build/distribution privacy and identity checks passed. Post-merge and new-feature remote CI gates have not run because integration has not occurred.

The final six independent reviews retained two Nits: reject noncanonical configured origins such as trailing `?` or `#` before readiness, and make the duplicate-key HTTP negative case otherwise valid so it isolates the strict JSON parser. Correctness and test quality returned NEEDS-WORK; bugs, security, style and plan conformance passed. The build-step run reached its three-iteration limit and its parent-authenticated result is BLOCKED. The candidate and all three rounds of evidence are preserved for a bounded follow-up; no fourth iteration was started.

M6's ordered runbook is prepared in the candidate, and its blank private acceptance record and local-tool notes are under ignored `reports/output/shared-workflow/`. Every M6 observation remains PENDING. No cloud authentication, cloud resource mutation or attended acceptance occurred. The local emulator and app are stopped; the pre-existing `.gitignore` change was restored exactly. Step 32 must pass its remaining review/integration gates before M6 execution; Phase B still requires checked M6 and Step 33 DONE.

## Step 32 delivery — 2026-09-12 UTC

The operator authorized one additional bounded iteration. Delivery `9652b159df877b373bbe55bfb0676dc518869784` preserves the original candidate and fixes only the remaining origin validation and HTTP test findings. The origin guard now rejects unsupported serialized forms at startup; a complete duplicate-key operation proves the real HTTP parser rejects the request without changing events or version. A temporary in-memory ordinary-JSON parser substitution made that corrected test fail; the real parser was restored before gates.

The complete Windows suite passed in both the candidate and main checkout: 1,110 cases, 1,107 passed, three unchanged existing skips; all 160 web cases passed without skips. The optional PyYAML supplement passed all nine workflow tests. Strict package mypy, Ruff lint/format, installed-wheel HTTP/Chromium/restart smoke, packaging/privacy and identity checks passed. All six independent review lenses passed with zero findings. All three [feature CI jobs](https://github.com/aberson/pta_finance/actions/runs/34674326030) passed on `9652b15`.

Integration used a three-way Git merge so the earlier main documentation updates survived. The pre-existing `.gitignore` change remains local, unstaged and byte-identical. The R3 candidate, R4 patch, tests and independent review receipts remain preserved in the original worktree. Step 32/#63 is complete; umbrella #62 and Steps 33–35/#64–66 remain open.

Stop boundary: the [M6 runbook](../docs/shared-workflow-proof.md) and root Manual UAT block are ready, but no attended M6 observation or cloud operation was performed. Phase B cannot begin until the private M6 record is checked and Step 33 is marked DONE.


## M6 closeout — 2026-09-12 UTC

Step 33/M6 passed the actual hosted comments proof and operator acceptance. The inspected immutable image, independent Google subjects, deployed access boundaries, exact revisions and detailed observations are recorded only in ignored `reports/output/shared-workflow/m6-acceptance.md` and its private receipts.

Both roles read the same seven server-attributed comments at Version 7 after a fresh deployment to the same store and namespace. The measured repeat reached the second account within 10.05 seconds of revision readiness. Signed-out and disabled-roster requests disclosed no workflow data; the disabled roster entry was restored. All 18 authenticated request/retry cases, two actual Origin cases, five supported signed-token injections and four UI recovery cases passed. A lost successful response followed by retry produced one durable event. The operator's final screenshot and pass verdict corroborated both browser views; another manual reproduction was unnecessary.

The source remains the Step 32 delivery; this closeout changes status documentation only. Existing full-suite, independent-review and CI evidence applies to that unchanged implementation. Cloud observations establish M6 separately from those local checks. Private receipts preserve earlier interrupted timing attempts without changing their outcomes. No real reimbursement, payment, mailbox or Sheet operation was performed.

Step 33/#64 is complete; umbrella #62 and Steps 34–35/#65–66 remain open. The operator requested the next planned stage: implement reviewer approval/not-approval and processor administrative completion in Step 34, then run M7/Step 35. The same fictional request remains the planned fixture. Existing proof data and the user's unstaged `.gitignore` change are preserved.

## Step 34 delivery — 2026-09-13 UTC

Delivery `fd4b45a1e38df4d82041fac0cb473bf9611d29a6` adds optional handoff mode. Reviewer approval transfers the next action to the processor; only the processor can complete an approved workflow. Not-approval is terminal. Comments preserve the decision, and completion explicitly remains administrative. The existing shared transaction lane enforces roles, versions, exact retry receipts and event ordering across comments, decisions and completion. Identity and comments modes retain their earlier behavior.

The complete Windows suite passed in both the isolated candidate and main checkout: 1,173 collected, 1,170 passed, three unchanged existing skips; all 223 web cases passed without skips. Strict package mypy, Ruff lint/format, build and identity gates passed. Installed-wheel HTTP/Chromium/emulator smoke passed the comments/restart proof and both durable handoff branches. The unchanged workflow files retain their nine-case optional PyYAML supplement pass. All six fresh independent review lenses passed after the bounded second iteration; the original review findings and earlier candidate remain archived. All three [feature CI jobs](https://github.com/aberson/pta_finance/actions/runs/34728978038) passed on `fd4b45a`.

Integration used Git's three-way merge. The pre-existing `.gitignore` change remains local, unstaged and byte-identical. Step 34/#65 is complete; umbrella #62 and Step 35/#66 remain open. The root Manual UAT section and the expanded runbook now point to M7.

No M7 cloud build, deployment or acceptance occurred during this code delivery. The accepted seven-comment M6 proof remains preserved. M7 must inspect the new actual image and verify both outcomes, wrong-role denials, exact retries, real competing decisions, history and fresh-revision durability using the pinned accounts. Preserve the approval namespace and use fresh namespaces for not-approval and the race before restoring the approval example. Functional acceptance remains separate from adoption of a real reimbursement workflow.


## M7 closeout — 2026-09-13 UTC

All 15 required observations pass: the inspected deployed handoff image, original receipt preservation, both terminal outcomes, role denials, exact retries, stale/changed-operation rejection, an actual opposing two-tab decision race, bounded history reads, refresh behavior and final restoration. The operator confirmed role labels, authorship and administrative completion wording. Exact cloud, identity and observation receipts remain private in `reports/output/shared-workflow/m7-acceptance.md`.

The restored original example remains completed. Its original eleven-event history is unchanged and an additional authenticated comment is preserved; both accounts read the same twelve events. The not-approved and race namespaces remain retained. No reset or deletion occurred. The initial CSP-blocked race helper was replaced by native form interactions without weakening application policy.

Step 35/#66 and the proof umbrella #62 are complete. Step 36 is the approved next build: the live six-request queue, followed by hosted M8. Functional proof does not establish adoption of a real reimbursement workflow. The pre-existing unstaged `.gitignore` change remains byte-identical. This closeout changes documentation only; the application is the already verified Step 34 source with its existing full-suite and CI receipts.
