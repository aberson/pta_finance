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

The isolated candidate contains the optional authenticated comments service, packaged fictional fixture, Firestore transactions, browser interface, staging/image recipes, tests, CI changes and attended runbook. Feature code remains unmerged and unpushed. Step 32/#63 stays open and PENDING; Steps 33–35 remain pending.

The final frozen-source Windows run collected 1,090 cases: 1,087 passed, three unchanged existing skips, and zero failures/errors. All 140 web cases ran without skips; the temporary-PyYAML supplement passed all nine workflow tests. Strict package mypy, Ruff lint/format, installed-wheel browser/emulator smoke (6.23 seconds after readiness), build/distribution privacy and identity checks passed. Post-merge and new-feature remote CI gates have not run because integration has not occurred.

The final six independent reviews retained two Nits: reject noncanonical configured origins such as trailing `?` or `#` before readiness, and make the duplicate-key HTTP negative case otherwise valid so it isolates the strict JSON parser. Correctness and test quality returned NEEDS-WORK; bugs, security, style and plan conformance passed. The build-step run reached its three-iteration limit and its parent-authenticated result is BLOCKED. The candidate and all three rounds of evidence are preserved for a bounded follow-up; no fourth iteration was started.

M6's ordered runbook is prepared in the candidate, and its blank private acceptance record and local-tool notes are under ignored `reports/output/shared-workflow/`. Every M6 observation remains PENDING. No cloud authentication, cloud resource mutation or attended acceptance occurred. The local emulator and app are stopped; the pre-existing `.gitignore` change was restored exactly. Step 32 must pass its remaining review/integration gates before M6 execution; Phase B still requires checked M6 and Step 33 DONE.
