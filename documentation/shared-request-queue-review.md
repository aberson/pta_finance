Reviewing as: feature plan. Sections 17–21 apply.

# Live shared request queue plan review

Reviewed and rechecked against repository baseline `5287796cf5be8f8ca88e262987858ddca2baf72d`. Target: [shared-request-queue-plan.md](shared-request-queue-plan.md). This was an in-session, source-grounded planning review; it was not an independent code review. No populated Issue fields were detected. Implementation and hosted acceptance have not started.

The final plan has no remaining technical defects identified by this review. Step 35 / M7 is an explicit, still-open execution prerequisite. Plan readiness does not complete that prerequisite.

**Ticket-lifecycle amendment recheck:** At planning HEAD `240679a6e3801fbea0f4839b8d183a1764f8d40e`, the operator requested ticket creation/CRUD and selected Processor-only creation. All numbered checks below were reconsidered against the amended queue plan. New P5/P6 and D8 record the [product rules](shared-ticket-lifecycle-design.md) as a separate follow-up after M8. Steps 36–37, their fixed catalog, API shapes and gates are unchanged. The follow-up is explicitly not a build-ready engineering plan: new durable discovery, source revisions, ID/API contracts and bounds must be specified against the implemented queue before its own build. The READY finding here applies only to the fixed-queue plan, not to CRUD implementation readiness.

## Blockers

None.

## Significant gaps

None.

## Missing items

None.

## Nice-to-haves

- **Expected issue-sync reminder:** Steps 36 and 37 have the correct pre-sync `Issue: #` fields. Synchronize them before the build runner starts. Keep the required project browser evidence and the documented deep-review route when forming issue bodies.

## Clarifications verified on recheck

- M8 creates new actions only on the five new requests. Reading or exactly replaying an existing receipt on the original must not add events. This makes the original-history preservation criterion and the acceptance procedure agree.
- Unexpected existing events stop the first-run baseline check. A resumed acceptance run uses preserved operation evidence instead of resetting a fixture or pretending it is new.
- The plan explains the tooling and the build/review/integration sequence inline. The summary read loop has a shared incoming deadline and runs outside the asynchronous web event loop.
- The follow-up's role policy is explicit: Processor creates shared drafts; Reviewer cannot edit them. Draft correction, submission locking, a competing withdrawal/decision, archive eligibility and restore semantics are stated without changing current source checks or claiming those behaviors exist. `rg` of `load_source`, `Store`, `create_app`, source hashes and transition consumers corroborates that this needs a subsequent lifecycle/storage design.

These were author clarifications during review, not the skill's mechanical step-autofix classes. Rechecking the revised plan found no further defect.

## Complete numbered check record

| Check | Result and evidence |
|---|---|
| 1 — Persistence | D2 preserves request/event schema 1, original identity, per-request paths, immutable receipts and create-if-absent seeding. Existing producer: [Store.seed](../pta_finance/shared_workflow/store.py#L217), [read](../pta_finance/shared_workflow/store.py#L242), [mutate](../pta_finance/shared_workflow/store.py#L295). No new materialized summary or migration is required. |
| 2 — External dependencies | Existing Cloud Run/IAP/Firestore and staged Cloud Build/image inspection remain the only hosted dependencies. D7 retains exact private runtime settings, checked-image admission, failure recovery and the rejected automated-login boundary. No scraping or new external API is added. |
| 3 — Authentication and secrets | D1/D4 retain two pinned subjects, enabled membership, private environment configuration, all protected-route checks and safe session-expiry feedback. [config.py](../pta_finance/shared_workflow/config.py#L41) rejects unsafe trust settings; [app.py](../pta_finance/shared_workflow/app.py#L79) applies the signed-identity boundary. No actual account/config values enter the plan or preview. |
| 4 — Async and concurrency | D3 uses one bounded six-read loop off the event loop, sharing the existing deadline; D2 retains independent transactions and version checks. No background scheduler or polling lifecycle is introduced. [Deadline](../pta_finance/shared_workflow/models.py#L102) and [Store](../pta_finance/shared_workflow/store.py#L75) are the existing owners. |
| 5 — Error handling | D3/D5 specify whole-list failure, retained stale data on refresh failure, no fake initial zero counts, non-JSON/session handling, pending controls and response-order protection. Existing detail drafts/retry IDs remain tied to their selected request. |
| 6 — Build and toolchain | §9 supplies install (`uv sync`), dev (real installed-wheel smoke factory), build (`uv build`), test (`pytest`), lint/format (Ruff) and typecheck (strict mypy). [pyproject.toml](../pyproject.toml#L17) defines web/dev dependencies and Hatch packaging; no new frontend build is necessary. |
| 7 — Unresolved decisions | A targeted `rg` for TBD, undecided alternatives, optional tool selections and bare ID placeholders returned no matches. `Issue: #` is the expected pre-sync reminder. D1–D7 are explicit defaults; private runtime values are already configured and are not unresolved architecture. |
| 8 — Setup | §9 specifies Windows, Python, uv, the gcloud Firestore emulator, Java, Chromium, ports and environment transfer. Commands match the existing [smoke parser](../scripts/shared_workflow_smoke.py#L545); the new test paths and extended queue phase are expressly future deliverables. |
| 9 — Idempotency | D2 preserves the [payload hash](../pta_finance/shared_workflow/models.py#L222), UUIDv4 receipts, seed retries and per-request operation scope. Tests distinguish identical IDs on different requests from same-request replay and conflict. |
| 10 — Integration seams | §4 enumerates `load_source`, Store, mode and `create_app` consumers. D1 defines the additional keyword-only catalog dependency, map validation, startup error and old-call compatibility. D3 gives exact list fields; no circular catalog/store ownership is needed. |
| 11 — Scope | §3 excludes intake, private data, payments, notification systems, more users and arbitrary catalog configuration. One queue mode composes existing stores; one code slice plus one acceptance step meets the stated six-request goal. |
| 12 — Security | D1 requires complete catalog validation before seeding. D4 authenticates before request membership/storage, retains exact write-origin/header rules and safe errors, and exposes no arbitrary path/query input or role-switch bypass. No external content is passed to an LLM. |
| 13 — Testing | D6/§9 require actual HTTP, signed local identities, emulator transactions, installed-wheel Chromium, old-mode wire/receipt preservation, per-request isolation/caps, failures and concurrency. [CI](../.github/workflows/ci.yml#L76) already discovers the new web test filenames. |
| 14 — Operations | §9 describes local startup and cleanup. D7 describes admission, normal retained queue mode and rollback to the inspected handoff image without deleting documents. Runtime/private evidence remains outside the source image. |
| 15 — End-to-end observation | §3 explicitly excludes unattended time-based behavior; no time soak is implied. Cross-component hosted behavior is nevertheless observed in Step 37/M8 through real accounts, real page actions and fresh-revision persistence. |
| 15.5 — Data-shape smoke | Step 36 explicitly requires a new real-component queue cycle within 60 seconds after readiness, before longer gates. It uses the actual installed package, browser, HTTP routes and emulator; upgrade checks open existing handoff state. |
| 16 — Clean context | §§2/5/6/9 explain existing architecture, source inventory, schemas, identifiers, API contracts, tools, development process and first run. The missing proposal is a planned publication created before wrap; all future implementation files are labeled new. |
| 17 — Existing-code validation | The plan says the current app accepts only one source ID; [data_store](../pta_finance/shared_workflow/app.py#L131) checks exactly that. It says `load_source()` must remain stable; [the loader](../pta_finance/shared_workflow/models.py#L235) owns the original fixture projection. Existing paths in §4 were found and read; the missing list route is new work, not a claim about current behavior. |
| 18 — Impact completeness | Config, launcher, store action-mode gate, root/detail/static/API routes, templates/scripts, tests, smoke, package manifest/image inspector and documentation are all in §4. D6 requires exact existing wire/receipt comparison rather than rewriting reference assertions to match a regression. |
| 19 — Conflicts and conventions | `git log -4` confirms the recent Step 34 baseline. `git worktree list --porcelain` confirms retained older candidates, including Step 32. Root plan reserves Phase 8 / Steps 36–37; Step 35 remains pending. Public-identity and user-ignore rules in [CLAUDE.md](../CLAUDE.md) are explicit constraints. No other worktree is edited. |
| 20 — Context sufficiency | §§2/4 describe source validation, per-request Store ownership, middleware, local test factory and image inspection. The plan specifies the new seam without requiring conversational context or a reconstruction of private cloud setup. |
| 21 — Feature sizing | Step 36 is the smallest independently useful live queue slice across source, storage, HTTP and UI. Step 37 admits and observes that delivered build. Existing finance/Slides/Gmail tracks are not replanned. |
| 22 — Step shape | Step 36 produces all source/tests and the M8 procedure, and its Done-when uses code gates. Step 37 executes the prepared procedure and produces private observations/judgment only. Neither masks an operator/code hybrid. |
| 23 — Conditional predicates | No conditional steps. The external Step 35 dependency is explicit; it cannot be inferred complete from preview approval or skipped by an unattended runner. |
| 24 — Reviewer flags | Step 36 declares `--reviewers deep --isolation worktree`, without a generic unauthenticated UI lifecycle. D6 and its Done-when explicitly require browser evidence from the existing signed-identity harness. No missing start command/URL is hidden behind a runtime reviewer flag. |
| 25 — Build format | Two `### Step N:` headings each contain Problem, Type, Issue, Files, Produces, Done-when and Depends-on. Blank Issue numbers are expected before synchronization. |
| 26 — Deployment seam | Step 37 is an explicit operator step that requires inspected-image deployment, Google/IAP observations, both roles' real decisions and persistence. Local tests cannot satisfy its Done-when. |
| 27 — Stakes-aware routing | Step 36 already declares deep review. This is the check's idempotent no-op case; no flags or model tier were rewritten. |

## Additional verification

The root roadmap links this canonical feature plan and carries a labeled objective and planned phase. The feature contains scrapable numbered steps and statuses. It reuses the existing smoke ports 8787/8788; the registry check found no conflicting declared entry, and the quickstart requires checking listeners rather than killing unrelated processes.

No application behavior, runtime configuration, cloud state, or M7 acceptance status was changed by this review. No application test pass is claimed for these planning edits. The final documentation checks and standalone proposal verification are recorded in [the wrap report](shared-request-queue-wrap.md).

Auto-applied 0 fixes. Plan is ready for `/plan-wrap` and `/repo-sync`.
