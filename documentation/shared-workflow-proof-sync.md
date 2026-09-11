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
