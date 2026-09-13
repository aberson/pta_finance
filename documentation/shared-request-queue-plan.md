# Phase 8 — Live shared request queue

**Status:** PLANNED — implementation has not started. Steps 36–37 are reserved here.
**Source baseline:** `5287796cf5be8f8ca88e262987858ddca2baf72d` (Step 34 delivered).
**Prerequisite:** Step 35 / M7 in [the proof plan](shared-workflow-proof-plan.md#step-35-accept-the-two-role-handoff-on-the-deployed-service) remains pending. Finish its real-account conflict/read checks, final browser readback, and operator wording acknowledgment before building Step 36. Planning and review can proceed independently.
**Decision provenance:** The operator accepted the fictional queue preview and asked to start making it a live shared system. This authorizes the bounded queue milestone described below; preview approval is not M7 acceptance.

## 1. What This Is

**Objective:** Let the same two signed-in participants find and work through six fictional requests in one shared queue, with each row reflecting its durable decision, next owner, and latest activity.

This feature connects the accepted queue layout to the existing Google-hosted workflow. Opening a row shows that request's items and complete attributed history; comments and decisions persist in the shared store and appear in the queue after a refresh or return from the detail page. The existing request and every accepted receipt remain intact. This is a multi-request functional demonstration, not real reimbursement intake or a payment system.

**Design reference:** [interactive fictional queue preview](../docs/examples/shared-request-queue-preview.html). The preview's sample outcomes illustrate the interface; they are not seed authority for decisions in the live application.

**Proposal:** documentation/shared-request-queue-proposal.html
**Track:** Optional shared workflow, independent of the local finance report, Gmail, Sheets, and Slides work.
**Effort:** One bounded application slice followed by one hosted acceptance step. Full repository gates and actual image inspection remain required.
**Stop criterion:** Do not declare this phase complete if the original request changes during upgrade, identities gain unintended access, requests affect one another, the queue misreports durable state, or M8 lacks real-account evidence. Retain the candidate and histories; repair the observed fault without resetting the demonstration.

## 2. Existing Context

The optional Python `[web]` package uses FastAPI for HTTP, Jinja for HTML, plain JavaScript for the browser, and Google Firestore for persistence. Cloud Run hosts it behind Google's Identity-Aware Proxy (IAP), which verifies Google sign-in before the application verifies the signed assertion again. Exactly one reviewer and one processor are pinned to distinct subjects. These choices already work and do not change.

Reusing that stack keeps the tested sign-in, transaction, packaging and rendering behavior together; the queue needs neither another service nor a JavaScript build system. Uvicorn runs the Python web server. uv manages the locked Python environment; Hatch builds its wheel; pytest runs tests; Playwright drives Chromium; Ruff checks style; mypy checks types. CI means continuous integration on GitHub Actions. M7 and M8 are the seventh and eighth hosted manual acceptance milestones. JSON is the structured HTTP/config format; UUIDv4 is a random operation identifier; SHA-256 is the existing content/key digest. CSRF means cross-site request forgery; the existing required header and exact Origin check protect writes.

- `pta_finance/shared_workflow/models.py:235` implements `load_source()`. It loads the packaged `example-request.json` through `reimbursement_report.load_bundle()`, checks the one-request fictional inventory, and returns the stable source projection. The original file, source projection, ID derivation, and payload hash are compatibility boundaries.
- `store.py:75` already binds one `Store` to one request path. Its transaction lane atomically advances the request and creates an immutable operation receipt. Reads validate the complete bounded history against that request's version. Multiple request documents require composition of these stores, not a replacement transaction engine.
- `app.py:42` exposes `create_app(config, verifier, store)`. Its request lookup accepts only the original source ID. `GET /` renders one detail page; there is no list route. Middleware authenticates protected routes, rejects query parameters, applies deadlines/security headers, and protects mutations with same-origin checks.
- `config.py` has strict `identity`, `comments`, and `handoff` modes. `auth.py` verifies signatures, audience, issuer, expiry, enabled roster membership, email, and pinned subject. `identity` mode does not initialize storage.
- `scripts/shared_workflow_smoke.py:137` is a nonpackaged test server factory. It runs actual HTTP, an installed wheel, synthetic signed identities, Chromium, and the local Firestore emulator. Production never enables this test trust. `deployment/shared-workflow/inspect_image.py` checks the actual built image against the allowlisted source inventory before deployment.

Steps 32–34 are delivered. M6 is accepted. M7 has passed both terminal branches and their revision persistence, but remains incomplete. Its actual accounts, namespaces, operations, browser state, and configuration are only in ignored `reports/output/shared-workflow/` and `secrets/`. Do not infer completion from screenshots of the queue preview. The operator's unrelated `.gitignore` edit must remain byte-identical and unstaged.

## 3. Scope

### Included

- An opt-in `queue` mode using the same accounts, service, named database, and original accepted namespace.
- Six fixed packaged fictional requests: the original plus five new requests, each independently durable.
- An authenticated summary endpoint, a queue at `/`, and direct detail links at `/requests/{request_id}`.
- Search by title/reference, status and next-owner filters, four status counts, latest-activity ordering, a visible Reload control, and clear loading/error/no-match states.
- Existing comment, approval, not-approval, completion, exact retry, and per-request event-cap behavior on every admitted request.
- Compatibility checks, local browser/emulator evidence, inspected cloud deployment, and attended two-account acceptance M8.

### Deferred

Request creation/import, private reimbursements, uploads, receipt files, Gmail/Drive/Sheets integration, payments, email sending, notifications, more users, per-request assignees, multiple organizations, reopening/reassignment, deletion, exports, analytics, arbitrary catalog configuration, and pagination beyond this fixed six-request demonstration. No private data discovery or credential changes accompany queue setup.

There is no automatic refresh interval, subscription, background job, or unattended workflow. Reads occur on page entry/return and explicit Reload. Filtering is local to the most recently fetched authorized list. The autonomous-behavior observation trigger does not apply; actual hosted acceptance still does.

## 4. Impact Analysis

All paths below are relative to the repository root. Existing files were read or found with `rg`; new paths are deliverables, not claims that they already exist. Repository-wide searches excluded ignored private output. Before implementation, repeat these searches against the current commit and include newly introduced callers.

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `pta_finance/shared_workflow/config.py` | extend | Accept `queue` with the same complete subject/origin/database requirements as handoff; no additional config fields | `rg 'mode' pta_finance/shared_workflow` found enum/startup checks at config:79,148; auth:179 uses only the identity distinction; main:25, app:45,47,89,144,161,201, store:307–308, template:8,15,23,36,46,57, request.js:84 consume mode. Tests: config:43,53,169–170; auth:134; store:64–75; handoff:50,133; browser:273; helper setup/new_store; smoke fixture_config, serve, Server and handoff checks. |
| `pta_finance/shared_workflow/models.py` | refactor | Share the source-projection construction with the new catalog while retaining the no-argument original loader and output | `load_source` consumers: main:14,23; app:28,44; smoke:141,150; image inspector:53,55; tests config:14,158, helpers:16,40, HTTP embedded subprocess:140,156, packaging embedded subprocess:150,153. `payload_hash`, `REQUEST_FIELDS`, `EVENT_FIELDS`, schema version, transition tables, and event cap retain their current shape/meaning. |
| `pta_finance/shared_workflow/store.py` | extend | Admit existing handoff actions in queue mode; reuse a separate Store for each fixed source | Constructor calls: main:27; smoke:150; tests helpers:40, store:41,60, handoff:50,244. Constructor/read/mutate signatures and per-request storage paths remain unchanged. Source/mode call sites listed above; no global operation-ID deduplication is introduced. |
| `pta_finance/shared_workflow/app.py` | extend | Add catalog dependency, protected list/detail routes, summary projection, and queue template/assets | `create_app` callers: main:30; smoke:152; tests HTTP:37,355 and handoff:37. Existing three positional arguments stay valid; add keyword-only `catalog_stores`. Root, `/api/me`, lookup and static dispatch were read in full. |
| `pta_finance/shared_workflow/__main__.py` | modify | Construct the catalog stores only in queue mode using the existing runtime identity | Sole production launcher; config → source → FirestoreClient → Store → create_app at lines 21–30. Identity still constructs no client/store. |
| `pta_finance/shared_workflow/templates/request.html.j2` and `static/request.js` | extend | Enable handoff controls in queue mode; add a queue return link while preserving existing retry/draft behavior | Template provides data-request-id/mode/role and exact form IDs; request.js consumes those and targets `/api/requests/` plus the page's selected ID. All mode comparisons enumerated above. |
| `pta_finance/shared_workflow/static/request.css` | extend | Reuse the existing visual tokens where useful for consistent queue/detail presentation | Existing request stylesheet and accepted standalone preview read; no frontend framework/bundler exists. |
| `scripts/shared_workflow_smoke.py` | extend | Build queue-mode stores in the existing local factory and exercise installed resources/HTTP/browser/storage together | `serve`, `fixture_config`, `Server`, existing comments/handoff phases and its `--emulator-host`/`--deadline-seconds` arguments read. Preserve old invocations; add an automatically executed bounded queue phase. |
| `tests/test_shared_workflow_config.py`, `test_shared_workflow_helpers.py`, `test_shared_workflow_store.py`, `test_shared_workflow_http.py`, `test_shared_workflow_handoff.py`, `test_shared_workflow_browser.py`, `test_shared_workflow_packaging.py` | extend | Cover new mode while retaining old contract assertions; add multi-request cases in dedicated files | `rg` enumerated the callers above. Current old-mode tests remain regression evidence; do not replace their expected wire shapes with queue shapes. |
| `deployment/shared-workflow/source-manifest.txt` and `inspect_image.py` | extend | Explicitly admit all new package resources and load/validate the actual six-request catalog inside the built image | Manifest currently declares 25 individual staged files; inspector hashes installed package files and loads the original source. No directory wildcard or runtime/private file enters the manifest. |
| `docs/shared-workflow-proof.md`, `README.md`, `CLAUDE.md` | extend | Document queue mode, behavior, local smoke and prepared M8 execution; distinguish built from accepted | Existing local commands, strict config, image admission and M6/M7 runbooks read. M8 is authored in Step 36, executed in Step 37. |
| `plan.md` and this feature plan | extend | Reserve Phase 8 / Steps 36–37 and record gates | Root reserves 32–35 for Phase 7, 26–31 for board summary, and 14–25 for Slides. No existing Phase 8 / Step 36 or 37 found in root/shared-workflow plans. Older independently numbered operational plans remain untouched. |

`auth.py`, `reimbursement_report.py`, `scripts/stage_shared_workflow.py`, the original `example-request.json`, Cloud Build recipe/Dockerfile, `pyproject.toml`, `uv.lock`, and CI workflow need no behavior changes. `load_bundle(path: Path) -> ReimbursementReport` stays unchanged; it remains the strict offline validator. The existing package wheel includes resources, and existing CI web discovery includes new `test_shared_workflow_*.py` files. If implementation uncovers a necessary change, identify its producer/consumers and retain the corresponding regression gate.

## 5. New Components

| New file | Responsibility |
|---|---|
| `pta_finance/shared_workflow/catalog.py` | Fixed six-source inventory, expected fictional metadata, `load_catalog()` returning a request-ID → source mapping; complete validation before any seeding. |
| `pta_finance/shared_workflow/example-request-02.json` through `example-request-06.json` | Five static fictional bundles, each accepted by the unchanged offline validator. Every filename is individually listed in the source manifest. |
| `pta_finance/shared_workflow/templates/queue.html.j2` | Authenticated queue shell, role label, counts, filters, table, refresh/error/live-status regions. |
| `pta_finance/shared_workflow/static/queue.js` and `static/queue.css` | Fetch/render the real summary response; local filters, stable sorting, accessible links, existing-origin navigation, responsive layout. |
| `tests/test_shared_workflow_queue.py` and `tests/test_shared_workflow_queue_browser.py` | Actual HTTP/emulator contracts, multi-request isolation, compatibility and browser behavior. Reuse the existing signer/server fixtures. |

The immutable inventory uses these operator-reviewable defaults. The original projection stays exactly as returned by `load_source()`; new refs do not borrow unrelated README fixture identities.

| Source | Ref | Title | Total | Starting workflow |
|---|---|---|---|---|
| Original `example-request.json` | NEW-01 | Classroom supply reimbursement | 184.50 | Preserve existing document and all events; awaiting review only in a fresh namespace |
| New resource 02 | DEMO-02 | Family reading night materials | 96.00 | AWAITING_REVIEW, reviewer, version 0 |
| New resource 03 | DEMO-03 | Garden club seed kits | 142.75 | AWAITING_REVIEW, reviewer, version 0 |
| New resource 04 | DEMO-04 | Volunteer appreciation refreshments | 78.20 | AWAITING_REVIEW, reviewer, version 0 |
| New resource 05 | DEMO-05 | Field day activity supplies | 225.00 | AWAITING_REVIEW, reviewer, version 0 |
| New resource 06 | DEMO-06 | Art display mounting materials | 63.40 | AWAITING_REVIEW, reviewer, version 0 |

New bundles contain 1–3 fictional line items with exact two-decimal amounts summing to their stated total. Use generic Example PTA / Example Volunteer labels and example.org for any required address. Their live decision is UNREVIEWED, workflow ACTIVE, payment NOT_PAID, with no confirmation/payment evidence. Validate source summary counts/totals and accounted review keys. Assign stable `submission:v1:` review keys from a SHA-256 of a fixed `shared-queue-v1:DEMO-NN` fictional label; source evidence hashes come from deterministic fictional labels, as in the existing example generator. Do not import the screenshot/Playwright helper into production.

## 6. Design Decisions

### D1 — Six fixed requests and explicit queue mode

Add only `queue` to the existing mode enum. The strict runtime JSON keeps its current fields: schema_version, mode, project_id, project_number, region, service_name, origin, database, namespace, users. Each of the exactly two users has email, subject, role, enabled. Queue mode requires both distinct subject bindings and all data-mode configuration. Old modes retain their current behavior and expose no catalog/list route. `GET /api/me` keeps the same keys; `request_id` is null in queue mode because no single request is selected globally. Its actor is always the verified caller.

The catalog is package-owned and fixed at six sources; users cannot select a file, namespace, source digest, catalog, or extra request through HTTP/config. Validate all sources and uniqueness before seeding. Fail with `FIXTURE_INVALID` on bad inventory. A new read-only map of stores is constructed by the launcher, using one existing Firestore client and the selected config. `create_app(..., *, catalog_stores=None)` requires exactly the catalog's six sources for queue mode, with keys matching sources, matching runtime config/database/namespace, and the original positional store identical to its catalog entry. Reject missing/mismatched dependencies as `CONFIG_INVALID` before storage writes; reject a catalog dependency in old modes. The map is copied before routes close over it.

### D2 — Preserve each request's storage contract

Keep schema version 1, the original source bytes/projection, payload hash function and original path. Paths remain `workflow_proofs/{namespace}/requests/{request_id}` with child `events/{operation_id}`. No legacy migration or reset is necessary. Seed each missing admitted request with the existing create-if-absent transaction; never write a decision during seeding. A partially completed startup is safe to retry because existing matching documents are validated and retained. Source mismatch stops startup; it is never repaired by overwriting documents. New IDs coexist with the original, and returning to handoff mode simply serves the original again without deleting the five additions.

Identifiers and wire shapes:

| Entity | Fields / contract |
|---|---|
| Request ID | 64 lowercase hex characters: SHA-256 of UTF-8 review_key, computed from the fixed packaged source. Membership in the catalog is also required. |
| Namespace | `proof_` plus lowercase hyphenated UUIDv4, chosen once privately; HTTP clients never supply it. |
| Operation ID | Lowercase hyphenated UUIDv4 from browser `crypto.randomUUID()`. Scoped to one request's events; the same ID on another request is a different operation, not a replay. |
| Source | schema_version=1, request_id, review_key, source_sha256, display. Display: ref, title, submitted_on ISO date, total two-decimal string, items[] with item_key, description, amount, category. |
| Request | Source fields plus state, next_owner_role, version (integer 0–100), created_at, updated_at. |
| Event | operation_id, payload_sha256, actor_sub, actor_label, actor_role, request_id, source_sha256, action, body, expected_version, previous_state, result_state, result_version, next_owner_role, created_at. |
| Actor | subject, email, label, role; verified per request, never accepted from request bodies. Public actor at `/api/me` is only the caller. |
| Timestamp | UTC RFC3339 string with six fractional digits and `Z`, serialized by existing `wire()`. |
| Mutation | Exactly operation_id, expected_version, body; decision adds exactly decision=`approve` or `not_approve`. Existing 8 KiB body/2,000-character comment limits apply. |

Every request retains its independent 100-event cap, version, full-history validation, same-operation retry and role checks. Queue mode permits the existing handoff actions; it introduces no new transition. Approval moves AWAITING_REVIEW → APPROVED with processor ownership; not-approval moves AWAITING_REVIEW → NOT_APPROVED with null owner; processor completion moves APPROVED → COMPLETED with null owner. Both terminal states permit comments without changing state/owner. Administrative completion never records a payment.

### D3 — A bounded summary over verified shared history

`GET /api/requests` returns exactly `{requests: RequestSummary[], request_count: 6, request_cap: 6}` in queue mode. RequestSummary has exactly request_id, display (ref/title/submitted_on/total only), state, next_owner_role, version, updated_at, latest_event. latest_event is null at version zero; otherwise it has exactly action, actor_label, actor_role, created_at from the last validated event. Do not return email, subject, raw comment body, source digest or full history in the list.

Build each summary from that store's existing validated `read(Deadline)` result. This deliberately reuses history integrity checking for six bounded requests, instead of introducing a second summary document or cross-request collection query. Execute the finite read loop outside the asynchronous web event loop through `run_in_threadpool`, sharing the incoming 20-second deadline; each database remote procedure call has an existing five-second maximum and mutation retries remain bounded. Stop and return the existing safe error envelope on any failure; no misleading partial-success count. Each row is consistent with its own returned version. The list is not an atomic snapshot across all requests; refresh resolves activity that happens while it is read. At most six histories of at most 100 events are read; output contains six summaries, never those histories.

Server ordering is updated_at descending, then request_id ascending for ties. Use timestamp values before serialization, not locale text. Browser filters preserve this order. Show latest event label/time, or “Awaiting first activity” for a new request. Counts derive from all six returned states; the visible row count separately reports matches after filters. Counts describe workflow, never paid/unpaid money.

### D4 — Routes and authorization

Both enabled pinned roles can see all six fictional requests. The server authenticates before catalog membership or storage access for list, HTML, static assets, detail and mutation routes. Membership lookup prevents access to any other document even if it exists in the same database. This is a two-person shared catalog, not request-specific user assignment.

| Method / route | Queue-mode behavior | Old-mode behavior |
|---|---|---|
| GET `/healthz` | Existing `{status:"ok"}`; no data | Unchanged |
| GET `/` | Queue HTML, role label, no embedded private roster | Existing identity/single-detail page |
| GET `/requests/{request_id}` | Selected request detail HTML and “All requests” link; unknown ID 404 | 404 |
| GET `/static/{name}` | Allowlisted queue and existing detail assets only | Existing assets; queue-only names 404 |
| GET `/api/me` | Existing actor shape, mode=`queue`, request_id=null | Existing mode-specific contract |
| GET `/api/requests` | D3 list response | 404 |
| GET `/api/requests/{request_id}` | `{request: Request, events: Event[], event_cap:100}` for an admitted source | Existing one-ID contract |
| POST `/api/requests/{request_id}/comments` | Both roles; `{receipt: Event}` | Existing comments/handoff behavior |
| POST `/api/requests/{request_id}/decision` | Reviewer only; `{receipt: Event}` | Existing handoff-only behavior |
| POST `/api/requests/{request_id}/complete` | Processor only; `{receipt: Event}` | Existing handoff-only behavior |

No query parameters on any protected route, including the list; use local filters. Unknown/malformed IDs are 404 after authentication. Invalid body/query is 400 INVALID_INPUT; unauthenticated is 401; wrong/disabled role or identity is 403. Keep conflict codes STALE_VERSION, OPERATION_CONFLICT, INVALID_TRANSITION, EVENT_CAP_REACHED and RETRY_CONFLICT; storage failure remains 503, source mismatch retains its existing startup error. Error shape remains `{error:{code,message,correlation_id}}`. Keep no-store, CSP (Content Security Policy), no-referrer, exact Origin/JSON/CSRF header rules, no CORS (cross-origin permission), safe logging, and text escaping. There is no role-switch UI or authentication fallback in production.

### D5 — Refresh and navigation that users can understand

Adopt the accepted preview's hierarchy and colors with semantic links, labeled search/select controls, keyboard focus indication, mobile scrolling and live feedback. Package CSS/JS separately to honor the existing CSP; do not serve the preview's inline script. A row opens the actual `/requests/{request_id}` page, not a modal with canned history. The selected page's data-request-id supplies every mutation path. Its existing forms keep operation IDs/drafts during ambiguous responses and never redirect a pending save to another request.

Fetch on initial queue entry, explicit Reload and browser `pageshow` restoration. Do not depend on browser back-cache returning fresh data. Disable Reload while pending; use a load sequence guard so an older response cannot replace a later one. Keep the old valid list visibly marked “Refresh failed; showing previously loaded requests” on a failed refresh; show no fake empty list/counts on initial failure. Session HTML/non-JSON or 401 prompts sign-in refresh. On success show when the queue was loaded. An empty filter result says “No requests match these filters” and offers a filter reset.

Keep filter state in the current page only for this first version: returning to `/` shows all requests and loads current data. This avoids introducing local storage or query-string exceptions. Status cards select that status; owner choices are Anyone, Reviewer, Processor, No further action (null). Search uses case-insensitive title/ref substring matching with trimmed input. It never searches comment bodies or private identity fields.

### D6 — Verify compatibility before cloud admission

Keep the Step 34 regression suite. Add real-component tests that capture the original source/request/events/receipts, enable queue mode in the same namespace, then assert the original projection and receipt replay are identical. Mutate two different requests and prove their versions/histories remain independent. Exercise two opposing same-request decisions with bounded conflict resolution; equal operation IDs across two different requests remain separate. Disabled users cannot list, navigate, mutate or replay; valid unknown IDs expose nothing. An inconsistent request makes the list fail safely. Test list ordering/counts/wire fields, zero/latest-event cases, failures, and one request reaching its cap without blocking another.

Use the existing local signer + loopback emulator + installed-wheel browser factory for repeatable UI evidence. No fresh Google sign-in is needed for code gates, and no IAP bypass enters the production package. Reviewer flag is `--reviewers deep --isolation worktree`: independent code/security/plan lenses gate the auth/storage changes. The generic `--ui` lifecycle cannot authenticate to the hosted URL; capture required Playwright evidence through the existing project smoke/browser tests instead. Do not invent a public debug route to satisfy a generic reviewer. M8 separately uses the actual signed-in Google accounts.

### D7 — Safe hosted progression

Step 36 authors the M8 runbook. Step 37 executes it only after Step 35 and the new code gate pass. Preserve the completed M7 runtime/env, immutable image/revision, full original JSON and receipts before changing mode to queue. Keep every field except mode, especially namespace and the two subjects. Stage only declared source files, build a unique image tag with the fixed Cloud Build recipe, require successful actual catalog/image inspection, then deploy the checked digest using the existing Cloud Run/IAP/runtime identity/resource limits. Retain filtered revision/IAM evidence privately.

M8 loads the same six IDs in both accounts and compares the original M7 history before any new action. New fixtures initially have zero events. Use only the five new requests for new M8 mutations; the original is read-only except for exact receipt replays that add no event. Using the actual page controls, reviewer approves DEMO-03 and leaves it for the processor; reviewer does not approve DEMO-04; reviewer approves DEMO-06 and processor completes it. Both roles comment on different new requests and refresh the queue/detail views. After this exercise, the preserved original and DEMO-06 are completed, DEMO-03 is approved, DEMO-04 is not approved, and DEMO-02/05 await review. The expected status counts are 2/1/2/1; compute them from observed states, not fixture flags. Verify exact actor/time/version and all untouched histories, unknown-ID/disabled-user denial, no request interference, browser direct links, refresh/back navigation, and same-image fresh-revision persistence of all six histories. Unexpected existing events stop the initial-baseline check; resume from preserved operations/evidence without resetting a request. Restore any temporarily disabled roster entry with verified readback even if a negative test fails.

Retain the accepted queue namespace on success. If a queue deployment fails, redeploy the previously inspected handoff image with the preserved M7 runtime/env, verify the original history, and retain all five extra documents and receipts. Never reset/delete them. Later retry reuses the intended stored state; a deliberately fresh rehearsal namespace must be named and recorded separately. No unattended browser focus takeover: use the already-authorized sessions only when desktop time is coordinated; never export cookies/assertions or retry Google's rejected automated-login method.

## 7. Build Steps

### Automated build

### Step 36: Work through a fixed shared request queue

- **Problem:** Make the six-request queue reflect each authenticated request's durable workflow.
- **Type:** code
- **Status:** PENDING
- **Issue:** #
- **Flags:** --reviewers deep --isolation worktree
- **Files:** Existing files in §4; new catalog, five example bundles, queue template/assets and two test files in §5; extend `docs/shared-workflow-proof.md` with the M8 procedure.
- **Existing context:** `Store` already isolates a source's transaction/history path; `create_app` admits one ID, and `load_source()` is the immutable original source. Extend their composition while preserving old modes, schema and receipts.
- **Produces:** Queue mode, authenticated list/detail UI and APIs, five additional validated fictional sources, complete local regression/browser/emulator evidence, expanded source/image inspection and a prepared M8 runbook.
- **Done when:** The production HTTP routes and installed-wheel Chromium views load six independently stored requests with exact D3/D4 contracts; a real comment/decision cycle updates the matching queue row after refresh; the original handoff namespace and exact receipts survive queue startup/restart; wrong identities and unadmitted IDs fail closed; list failures/counts/order and per-request caps/retries behave as §6 specifies. The new real-component queue smoke finishes within 60 seconds after readiness before longer suites, full Windows/CI/packaging/privacy gates pass, and independent deep reviews pass. Save Playwright queue/detail/error evidence from the authenticated local fixture harness. No operator judgment or cloud deployment is required for this code gate.
- **Depends on:** 35 (external prerequisite in the proof plan; all M7 rows and operator acceptance must be recorded DONE).
- **Parallel-safe with:** none — the following step admits and observes this exact built image; it cannot precede code verification.

### Hosted acceptance

### Step 37: Accept the live shared queue with both accounts

- **Problem:** Verify the deployed six-request queue matches independent Google users' durable actions.
- **Type:** operator
- **Status:** PENDING
- **Issue:** #
- **Files:** Execute the Step 36 M8 runbook; use existing private runtime/env and new ignored `reports/output/shared-workflow/` acceptance records.
- **Existing context:** Step 35 establishes the original completed request/history, Step 36 supplies the new catalog/UI and immutable image inspection, and the same two real subjects remain configured.
- **Produces:** Private M8 observations, source/build/image/revision and policy receipts, preservation comparisons, and an operator functional-proof judgment. No shipped code artifacts.
- **Done when:** Every D7/M8 observation passes with the two pinned real subjects, all six histories survive a fresh revision, the original M7 history remains unchanged, and the operator can locate work, identify next ownership/history, and distinguish administrative completion from payment. Actual image inspection and cloud/IAP observations are required; local tests and preview approval do not satisfy this gate.
- **Depends on:** 36.
- **Parallel-safe with:** none — acceptance runs against Step 36's verified build after Step 35 acceptance.

M8 execution is delegated to the agent wherever mechanical checks and available tools permit; “operator” reserves genuine browser availability/sign-in and product judgment, not a requirement that the user type every command. Do not run Step 36 while the external Step 35 dependency is still open.

## 8. Risks and Open Questions

| Item | Risk | Mitigation |
|---|---|---|
| M7 still pending | Confusing preview approval with acceptance of the deployed handoff | Keep Step 35 dependency explicit and its private evidence intact. |
| Existing source identity | Rebuilding the original fictional bundle could invalidate old receipts | Original file/projection/hash/path unchanged; compare full old-mode wire fixtures and installed-wheel upgrade/replay. |
| Cross-request access/write routing | A guessed ID or selected row could route to another document | Fixed catalog membership after authentication; server selects the Store; independent versions/receipts and unknown-ID tests. |
| List consistency and failure | Mixed read times or one corrupt request could be mistaken for a clean whole-system snapshot | Per-row version consistency, bounded shared deadline, no partial-success result, visible refresh time/failure state. |
| Demonstration states | Copying preview states would invent decisions/actors | Seed only awaiting-review sources; actual authenticated page actions establish varied states in M8. |
| Browser/host boundary | Local fixtures alone cannot establish Google/IAP behavior; desktop focus can interrupt tests | Separate M8, reuse real sessions, coordinate desktop time, retain operations and interruptions. |
| Finite demonstration | A six-request summary scan does not prove arbitrary-scale intake | Keep count fixed, no pagination/scale claim, no private import. Revisit query/index design when expanding scope. |
| Parallel work / user files | Shared docs or staging could include another task's changes | Recheck Git/worktrees; isolated code worktree; scope all staging/commits; preserve `.gitignore` and all private state. |

No unresolved product choice blocks this bounded plan. Six requests, refresh behavior, catalog mode and implementation seams are defaults D1–D7, not statements that the operator personally selected those details. Actual IDs/account/billing values stay in the already configured private runtime. Further intake/team/notification scope needs a later plan.

## 9. Testing Strategy and Quickstart

Run from the repository root using Windows 11, Python 3.12+, uv, a Google Cloud CLI with the Firestore emulator component, a compatible Java runtime, and installed Playwright Chromium. Existing local commands were verified against `docs/shared-workflow-proof.md` and the smoke parser. No Docker or Google login is required for local gates. The real emulator uses loopback port 8787; the fixture server uses 8788. Check availability first; do not kill unrelated listeners.

```powershell
uv sync --locked --extra dev --extra slides --extra web
uv run playwright install chromium
gcloud emulators firestore start --project=example-workflow-test --host-port=127.0.0.1:8787
```

Keep the emulator in its terminal. In a second terminal at the repository root:

```powershell
$env:FIRESTORE_EMULATOR_HOST = '127.0.0.1:8787'
uv run python scripts/shared_workflow_smoke.py --emulator-host 127.0.0.1:8787 --deadline-seconds 60
uv run pytest -q tests/test_shared_workflow_queue.py tests/test_shared_workflow_queue_browser.py
uv run pytest -q
uv run mypy --strict pta_finance
uv run ruff check .
uv run ruff format --check .
uv build
uv run python scripts/check_no_identity.py
Remove-Item Env:FIRESTORE_EMULATOR_HOST
```

Each command is sequential and must return zero before continuing; command failure stops the dependent gate. The two new test paths are produced in Step 36 and do not exist at plan time. The smoke command exists now; Step 36 extends it to execute the queue phase automatically after its existing comments/handoff phases. Its test factory supplies synthetic project/database/subjects/public test keys through stdin, never private runtime config or ADC (Application Default Credentials). It starts/stops its own server and browser on success, failure and interruption. Ctrl+C stops only the emulator started in the first terminal.

**Development first run:** the extended smoke command is the supported local real-component runner; use the queue browser tests for interactive-control evidence and failure diagnosis. Production first run remains `python -m pta_finance.shared_workflow` with strict private `PTA_WORKFLOW_CONFIG` and the attached keyless service identity. Never point the local harness at a remote emulator or set production config for it. M8's build/deploy/restore commands are authored in the code step, reusing the fixed staged-image/runbook contract rather than uploading the checkout.

**Required evidence:** separate old-mode wire compatibility from the new list contract; state/version/event comparisons across two request IDs; auth/CSRF/query/body negatives; independent event caps and duplicate IDs; stale/competing same-request writes; keyboard search/filter/link traversal; actual queue-to-detail-to-queue navigation; reload/back-cache handling; error/non-JSON/session recovery; safe text; layout at desktop and narrow widths; actual installed-wheel resource loading; image catalog validation. The code gate runs the full applicable suite on candidate and integrated main, existing independent deep reviews, feature/main CI and privacy/identity gates. Current baseline has 1,173 collected tests, 1,170 passing and three recorded existing skips with dev/slides/web; counts will grow and must be reported from actual results. Do not replace full-gate evidence with a subset count.

**Build process:** After M7 is recorded complete and plan issues are synchronized, the build runner creates an isolated candidate worktree, implements Step 36, runs the gates, and obtains fresh independent deep code reviews. Fix findings within the runner's bounded iteration allowance; preserve the candidate and report any remaining findings on a halt. Integrate only a passing candidate, rerun the required main gates, push and verify CI, then synchronize delivery status. M8 executes the prepared cloud procedure against that exact delivered source. A deferred operator step remains pending until its actual observations and judgment are recorded.

## Appendix

### Decision Inventory

| ID | P/D | Choice | Status |
|---|---|---|---|
| P1 | P | Connect the accepted shared queue concept to live shared storage | Operator requested |
| P2 | P | Retain the same two-account workflow with individual request histories | Operator accepted milestone |
| P3 | P | Use several fictional requests before real reimbursements | Operator accepted milestone |
| P4 | P | Use the accepted preview as the interface direction | Operator accepted preview |
| D1 | D | Six fixed packaged sources, opt-in queue mode, old-mode compatibility | Proposed default |
| D2 | D | Compose existing per-request stores; preserve original schema/receipts; seed no decisions | Proposed default |
| D3 | D | Six bounded validated reads, summary-only output, per-row consistency | Proposed default |
| D4 | D | Both existing roles see the whole fixed catalog; authenticate every route | Proposed default |
| D5 | D | Refresh on entry/return/Reload; local filters reset when returning to the queue | Proposed default |
| D6 | D | Existing authenticated local harness plus independent deep code reviews | Proposed default |
| D7 | D | Finish M7, build one queue slice, then execute separate M8 with real decisions | Proposed default |

Plan preparation may proceed while M7 waits for browser availability and wording judgment. This plan does not grant unattended access to an actively used desktop or mark any remaining M7 observation as passed.
