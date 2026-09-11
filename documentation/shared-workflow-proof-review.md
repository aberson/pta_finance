Reviewing as: feature plan. Sections 17–21 apply.

# Shared workflow proof plan review

Reviewed and rechecked 2026-09-11 against repository baseline `dc259ee3d19e3b95f1f6f9d68388a6a0dfda4f9f`. Target: [shared-workflow-proof-plan.md](shared-workflow-proof-plan.md). The reviewer was independent of the author, and edits were serialized. Autofix ON; no populated Issue fields were detected.

**Verdict: READY for redline and plan-wrap.** Remaining defects: **0 blockers, 0 significant gaps, 0 missing items**. The first review applied six mechanical fixes. The rerun applied zero and verified the author's substantive corrections. The canonical roadmap pointer and blank Issue fields remain normal planning-closeout reminders.

This certifies the plan's readiness, not implementation or deployment. No application tests, cloud acceptance, Git writes, or cloud mutations were performed by this review. Real project/account/configuration observations remain private. Code preparation can proceed through the planning sequence while deployment prerequisites are gathered; Phase B requires the actual M6 proof.

## Blockers

None.

## Significant gaps

None.

## Missing items

None.

## Nice-to-haves

- **Canonical roadmap pointer:** The feature has parseable Phase A/B and step-status units. The plan assigns the canonical `plan.md` pointer to a separate planning closeout edit. Add a labeled objective and progress summary there so the observer can discover the proof from the entry plan. This is not an implementation blocker.
- **Issue synchronization:** All four Issue fields are correctly blank before redline → wrap → repo-sync. No existing issue body needs reconciliation yet. Run only Phase A after issue synchronization; complete M6 before a separate Phase B invocation.

## Findings closed on recheck

| Finding | Original defect | Verified correction |
|---|---|---|
| B1 | A single build span could defer M6 past the approval implementation. | §7 now explains actual operator-step deferral, gives separate `--phase A` and `--phase B` invocations, and prohibits an unattended whole-plan run. Phase A contains exactly 32–33; Phase B exactly 34–35. Phase B requires the orchestrator to inspect the private M6 record and mark Step 33 DONE before dispatching Step 34. |
| S1 | The first comment slice also built a cloud resource reconciler and pagination product surface. | §5/D8 replace the helper with local-only `stage_shared_workflow.py`, a fixed Cloud Build recipe, and explicit gcloud runbook. D4 caps a namespace at 100 events, has no cursor/page API, and permits identical retries before checking the cap. The server returns one complete bounded history. |
| S2 | Private configuration had no specified route into the container; start and guard contracts were incomplete. | D6 defines strict `PTA_WORKFLOW_CONFIG` JSON, its complete fictional schema, private environment-file generation, production module startup, PORT handling, stop/cleanup commands, and stable startup codes. D1/D2 name the Google verifier, crypto support and explicit lifetime/cache/fetch limits. D9 specifies the local browser runner. |
| S3 | The store, browser and API would independently invent response/receipt fields and actor labels. | D3 defines Request/Event/me/request/POST/error shapes, initial version/state, timestamp and nullable-owner formats, and `models.py` as schema owner. D2 fixes labels by role; D3/D4 fix UUIDv4, source-digest mapping, canonical JSON hashing, and retry receipts. D9/§9 require production-route response-shape checks. |
| S4 | Step 32 required inspected containers while relying on Cloud Build after its own completion. | Step 32 now gates local staged-source/installed-wheel behavior only. D8/M6 inspect the actual Cloud Build image and load its packaged resources before deployment/admission; M7 repeats this for the later image. No Docker prerequisite or live build is hidden in the code gate. |
| S5 | Step 32 had plain code review for its auth/persistence/deployment blast surface. | Initial autofix changed only the reviewer tokens to `--reviewers deep`, preserving `--isolation worktree`. The author retained this and aligned §7 prose. |
| S6 | Step 34 had plain code review for authorized persistent transitions. | Initial autofix changed only the reviewer tokens to deep. The rerun confirms the same Flags; no model override was introduced. |
| M1 | All four steps lacked Files fields. | Four Files fields and one marker per step were added initially. The author updated their renamed staging/runtime targets. Operator Files remain prepared artifacts plus private runtime configuration/evidence, with no shipped-code output. |
| R1 | Existing base CI installs dev+slides but typechecks the entire package; new optional web imports require installed web dependencies. | Final §9 explicitly retains base tests without web, moves the full-package strict mypy gate into the new web-enabled job with emulator/Chromium, and forbids module exclusions/suppressions. Existing regression/Ruff/identity/Windows gates remain. This was a small clarification, not another product feature. |

The review routing follows the trigger owner in `review-deep/core.md:33–41`. Considering the provider's high-stakes review tier remains an operator-policy note; this review did not set a tier.

## Complete numbered check record

| Check | Final result and evidence |
|---|---|
| 1 — Persistence | D3/D4 choose Firestore request/events, create-if-absent seeding, source/schema mismatch failure, transactional versions, and immutable history. The feature introduces a separate store; no existing private bundle migration is required. |
| 2 — External dependencies | Direct IAP, Cloud Run, Firestore, Cloud Build, Artifact Registry, and identities are explicit. D10/M6 preserve actual readiness/access gates and failure handling. Two invited users create no scraping/ToS concern. |
| 3 — Authentication/secrets | D2 verifies signed IAP assertions and pins two distinct subjects plus exact normalized emails. D3 uses keyless runtime ADC; D6 supplies configuration without placing it in the image. Missing/invalid/disabled identities fail closed. |
| 4 — Async/concurrency | No background job is introduced. D4's Firestore transactions read before writing, compare expected versions, and replay identical operations. D6 bounds call/transaction/total deadlines; no queue or polling system is warranted. |
| 5 — Error handling | D5/D6 cover uncertain saves, draft preservation, pending state, explicit retry, stale reload, session-refresh HTML, safe errors and correlation IDs. D3 defines the error envelope. |
| 6 — Build/toolchain | Install: uv sync with extras. Dev: configured module launch and actual-browser smoke. Build: uv build locally, fixed Cloud Build recipe in M6. Test: pytest/emulator/browser smoke. Lint: Ruff. Typecheck: full-package strict mypy. §9 explicitly separates base CI from installed-web/emulator CI. |
| 7 — Decisions/placeholders | No unresolved product or architecture alternative. Defined patterns and private operational inputs are inventoried below; no bare undefined id or TBD marker remains. |
| 8 — Setup | D6/§9 specify Python >=3.12, uv, gcloud/Java emulator, Chromium, runtime JSON, environment transfer and first run. D10/M6 leave actual cloud readiness as an operator gate without blocking code preparation. |
| 9 — Idempotency | UUIDv4 operation documents, same-subject/canonical-payload retry receipts, changed-operation conflicts, transactional increments, immutable seeding, and retry-before-cap are explicit. |
| 10 — Integration seams | Separate optional package, typed request/event shapes in models.py, actual HTTP-to-Firestore caller, and no private CLI/Sheets/Gmail invocation. Browser tests assert the same published response shapes. |
| 11 — Scope | The approved cut is implemented: one request, two identities, comments, bounded history and later handoff. No resource reconciler, pagination, arbitrary import, payment, notification, or workflow framework is added. |
| 12 — Security | D2/D5/D6/D8 cover signed assertions, role checks, same-origin protections, strict bounded inputs, escaping/CSP, startup invariants, source allowlist, keyless runtime and private-config exclusion. No fetched content enters an LLM, so that injection-specific trigger is inapplicable. |
| 13 — Tests | Real local cryptographic/HTTP/emulator/browser/restart checks, distribution privacy, failures and old-regression gates are planned. M6/M7 cover live substrate separately. The plan does not claim any of those future tests have passed. |
| 14 — Operations | Exact local start/stop and emulator cleanup, explicit reload, private evidence path, fresh namespace instead of reset/delete, and access shutdown preserving evidence. No new scheduler is necessary. |
| 15 — End-to-end observation | The multi-stage integration trigger applies. M6/M7 observe real browser-to-service-to-store paths and forced fresh-revision persistence. No hours-long scheduled-job soak is needed for this request-driven feature. Separate phases place M6 before business controls. |
| 15.5 — Data-shape smoke | Step 32/D9 require <=60-second actual HTTP/Firestore-emulator/browser wiring, D3 shape assertions, then app-process restart before the larger matrix. The single event cap replaces pagination coupling. |
| 16 — Clean context | Existing architecture, rationale, schemas, IDs, APIs, setup and execution order are now concrete. Plan-wrap remains the next independent clean-context gate after redline. |
| 17 — Existing-code validation | All fifteen existing §4 targets were verified. New web/deployment files are clearly designated as new. Existing producer behavior/callers were checked against actual code; evidence below. |
| 18 — Impact completeness | Dependency/lock, CI, resources, tests, package/distribution, runbook and README/context docs are covered. Existing signatures/private schemas remain unchanged. Final CI partition closes the optional-import seam without weakening strict typechecks. |
| 19 — Conflicts/conventions | Actual build-phase deferral is handled through separate invocation boundaries. Sibling steps own 14–31; no sibling claims 32–35. Shared dependency/CI/docs overlap is acknowledged. Public identity rules and the existing dirty .gitignore are preserved. |
| 20 — Architecture context | §2/§4 plus D1–D10 sufficiently orient a fresh builder without assuming hosted code exists. Config/response gaps are closed. |
| 21 — Step sizing | After removing the reconciler/pagination, Step 32 is one observable save/read comment slice with essential packaging/security/verification. Keep that production caller intact. Step 34 is one two-role handoff slice. |
| 22 — Operator/code split | Steps 33/35 produce private runtime observations only; code artifacts/runbooks are prepared by code steps. Code gates no longer require cloud image construction or operator review. |
| 23 — Conditional predicates | No conditional steps; not applicable. |
| 24 — Reviewer flags | Both code steps use deep, a code-only lane. No full/runtime profile requires a Start-cmd/URL. Explicit local browser evidence remains independently required. |
| 25 — Step shape | Four valid Step headings with Problem/Type/Issue/Files/Done-when. Phase A parser result is 32,33; Phase B is 34,35. Blank Issue values are expected. |
| 26 — Live substrate | M6/M7 require actual IAP/Cloud Run/Firestore/browser evidence, image inspection and revision receipts. Unavailable checks remain pending/failed; local tests cannot substitute. |
| 27 — Stakes routing | Deep on both matched code steps; no remaining plain-code flag. Existing markers are not treated as exemptions. |
| Control-plane hook | Canonical pointer reminder above. Phase/step/status units are scrapable. `observatory ports` returned an empty map and no collisions, covering proposed loopback 8787/8788. |

## Placeholder and decision inventory

- `proof_<UUID>` is explicitly defined as `proof_` followed by lowercase hyphenated UUIDv4; operation IDs use the same canonical UUID form. The specific namespace remains a private runtime value.
- `/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME` is the defined IAP audience formula, populated from the strict runtime configuration; it is not an unresolved architecture placeholder.
- `{request_id}`, `{namespace}`, Request, Event and the JSON object notation are defined route/schema substitutions in D3–D5, not unknown entities.
- `approve` or `not_approve` is the exact decision enum. `identity`, `comments`, and later `handoff` are exhaustive modes with defined gates, not alternatives awaiting selection.
- `$PtaProject`, `$PtaRegion`, `$PtaBuildRegion`, `$PtaService`, `$PtaRuntimeIdentity`, `$PtaBuildIdentity`, `$PtaImageTag`, `$PtaStage`, `$PtaImageDigest`, and `$PtaRevision` are explained private shell inputs or generated receipts. They never authorize changing unrelated resources.
- “where the IAP test mechanism permits injection” and “when available” remain deliberate platform-test limitations; unsupported cloud injection is explicitly not counted as cloud success, while local verifier negatives remain mandatory.
- Actual project access/ancestry, billing, APIs, region, account-role bindings, OAuth setup and IAM permissions remain explicit private operational prerequisites. These are separate from plan readiness.
- No TBD sentinel, plain code-review flag, old deploy-helper reference, cursor parameter, or unresolved verifier/label/configuration choice remains. The surviving word “pagination” says the API does not implement it.

## Code and contract evidence

- `git rev-parse HEAD` remained `dc259ee3d19e3b95f1f6f9d68388a6a0dfda4f9f`. Initial/final status contains the pre-existing `.gitignore` change and the new scoped planning artifacts; no Git mutation was performed.
- `Test-Path` verified all fifteen existing §4 files. The `rg --files` inventory contained no deployment/shared-workflow package. A targeted `FastAPI|Firestore|uvicorn|shared_workflow` search of current package/dependency declarations found no existing hosted implementation.
- `pyproject.toml:6–34` confirms Python, existing extras/dependencies, CLI and Hatch package selection. `.github/workflows/ci.yml:22,31,35–36,55–62` establishes the actual base-install/full-mypy/Linux-test/Windows split that R1 now accounts for.
- The plan says pay_now must not authorize the new transitions. `pta_finance/reimbursement_report.py:261–264` checks payment status and returns approved item totals without a recorded approval test. Ticket is frozen (`:219–220`); `load_bundle` loads/migrates/validates (`:1437–1450`); HTML autoescape is enabled (`:1526–1532`). Existing generated payment prose (`:1463–1497`) remains out of the new page.
- The plan describes validated atomic refresh. `pta_finance/reimbursement_pipeline.py:1897–1944` flushes/fsyncs a temporary file, validates with load_bundle, then replaces the destination. Call searches confirm the stated CLI/pipeline/report/test consumers. No signature change is planned.
- `scripts/capture_readme.py:111–136` contains NEW-01, UNREVIEWED, and two source amounts 124.50/60.00. It imports Playwright at `:20`; the static derived fixture avoids importing that screenshot tool at runtime.
- `scripts/check_no_identity.py:26–39` obtains only tracked files, so inspecting allowlisted staged contents and untracked canaries addresses an actual upload gap.
- Sibling-plan searches confirm slide Steps 14–25 and board-summary Steps 26–31. Root `plan.md:390–391` contains future Apps Script/admin-UI ideas; `CLAUDE.md:15` says no hosted app is shipped. The proof is an explicit narrow new direction, not a claim those roadmaps are complete.
- `build-phase/core.md:184–190,272–273` actually defers pure operator steps. Its `--phase` extraction at `:74,174` supports the corrected separate phases. A mechanical heading scan returned A = 32,33 and B = 34,35.
- Four step records had zero missing required fields. Four existing autofix markers remain; zero populated Issue fields and zero plain-code reviewer Flags were found. The initial two deep escalations preserve their isolation flag.
- The pre-existing `.gitignore` SHA-256 remained `464EA61C37D5CC66EB0228435C1018867F6ACC6EFE01F7CEBC1C7F7134FE2138`. No private runtime configuration or credential content was opened.

## Primary-source checks

Direct Cloud Run IAP, service-scoped admission, external/no-organization OAuth setup, and the IAP service-agent invoker role match the official deployment model. [Direct IAP setup](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run). The assertion algorithm/audience/issuer and maximum lifetime/skew match Google's signed-header contract; the selected verifier supports the stated cert URL/audience/skew parameters. [Signed headers](https://docs.cloud.google.com/iap/docs/signed-headers-howto), [Google verifier API](https://google-auth.readthedocs.io/en/latest/reference/google.oauth2.id_token.html).

Server Firestore access uses IAM with database conditions, while the emulator has documented environment/runtime requirements and cannot establish deployed IAM correctness. [Firestore IAM](https://docs.cloud.google.com/firestore/native/docs/security/iam), [Firestore emulator](https://docs.cloud.google.com/firestore/native/docs/emulator). The build/deploy command surfaces were checked against official CLI references. [gcloud builds submit](https://docs.cloud.google.com/sdk/gcloud/reference/builds/submit), [gcloud run deploy](https://docs.cloud.google.com/sdk/gcloud/reference/run/deploy). No price, free-tier, access, deployment, or application-success claim is inferred from those documents.

## Autofix and readiness record

Initial run: 6 fixes — Files on Steps 32/33/34/35, reviewer escalation on Steps 32/34. The author subsequently made all substantive corrections; R1 was resolved by its final CI sentence. The rerun made no plan edits and requires no new product decision. Continue redline → plan-wrap → repo-sync, then Phase A only; M6 gates Phase B.

Auto-applied 0 fixes. Plan is ready for `/plan-wrap` and `/repo-sync`.
