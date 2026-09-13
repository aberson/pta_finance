# Shared workflow proof plan

**Status:** STEPS 32–34 DONE — M6 cloud acceptance and the reviewer-to-processor handoff code delivery pass. Next: M7 / Step 35, using the two pinned accounts and preserved fictional proof. See [the Step 34 delivery](shared-workflow-proof-sync.md#step-34-delivery--2026-09-13-utc).

**Reserved steps:** 32–35. A scan of `plan.md` and every `documentation/*plan.md` found the latest global span 26–31 in the monthly reimbursement board-summary plan; the slide plan owns 14–25. Older feature plans contain historical local numbering. No sibling currently claims 32–35. Recheck before issue synchronization; this feature neither completes nor renumbers existing work. The master-plan pointer is Phase 7 in `plan.md`.

**Decision provenance:** The operator selected a separate workflow, an early authentication/persistence proof, one fictional request, two independent Google sign-ins, comments, server attribution, shared state, then a two-role approve/not-approve handoff and completion. Implementation defaults D1–D10 below are agent-selected. Prior scope approval stands; actual cloud permissions, resources, region, and account identities remain external prerequisites.

**Public repository boundary:** Only fictional examples, such as Example PTA and `reviewer@example.org`, belong in this plan, code, fixtures, screenshots, issues, or committed evidence. Real identities, project identifiers, cloud settings, and browser acceptance records stay in `secrets/` or `reports/output/`, already ignored. Never copy existing private ledger, mailbox, financial report, payment, or credential data into the proof.

## 1. What This Is

**Objective:** Prove authenticated shared comments across two Google accounts before building one administrative approval handoff.

Proposal: documentation/shared-workflow-proof-proposal.html

**What this feature does:**

Two invited Google users open the same hosted fictional reimbursement request, leave comments attributed by the server, and see each other's saved work after reload. The first delivery proves those basics on the intended Google deployment before approval controls are implemented. After that proof passes, a reviewer can approve or not approve the request; approval hands the request to a processor, who can mark the demonstration complete. Completion records an administrative handoff, with no assertion that money moved. This is a small collaboration proof for the toolkit, not a replacement for the private reimbursement process.

## 2. Existing Context

Producer files were opened before making the following claims:

- `pyproject.toml` declares Python `>=3.12`, `uv`/Hatch packaging, Jinja2, Google client libraries, optional `pdf`/`slides` extras, and `pta-finance = "pta_finance.cli:main"`. There is currently no web framework, hosted entry point, or Firestore dependency.
- `pta_finance/reimbursement_report.py` defines immutable `Ticket`/`LiveState` models, strict schema-v2 input with v1 migration through `load_bundle(Path)`, and autoescaped Jinja rendering. `Ticket.review_key` and `source_evidence_sha256` provide identity and revision anchors. `Ticket.pay_now` checks payment state before summing A items; it does not itself require a recorded live approval and must not authorize web transitions.
- `pta_finance/reimbursement_pipeline.py` implements `refresh_bundle(...)` through validated local JSON and atomic replacement. This is a private file workflow, without browser principals, shared comments, or multi-user version checks. Its payment/review reducers remain outside this feature.
- `pta_finance/cli.py` keeps `report-reimbursements` offline and `update-reimbursements` limited to optional mail acquisition and local evidence/report writes. The existing static `reimbursement_queue.html.j2` does not supply editable workflow controls.
- `scripts/capture_readme.py` contains a validated fictional fixture. Its first request, `NEW-01`, has two classroom-supply items totaling `184.50` and live decision `UNREVIEWED`, despite item-A recommendations. Its screenshot module imports Playwright; the hosted app must not import that script.

The current tracked `.gitignore` modification predates this plan and must be preserved. Existing worktrees and the pending summary/slide tracks can touch dependencies, CI, and documentation; implementation starts in an isolated worktree after checking the current branch, HEAD, and status. No existing public function signature, bundle schema field, or shared domain constant needs changing.

The root roadmap's future Apps Script/sign-in/admin UI entries describe unbuilt possibilities. This scoped Cloud Run proof is an explicit new deployment choice; it does not implement the broader roadmap, slide workflow, or board summary.

## 3. Scope

Included:

- One immutable fictional request per proof namespace, loaded through the existing strict bundle validator; one minimal same-origin page and JSON API.
- Direct Identity-Aware Proxy (IAP) protection on one Cloud Run service; exactly two privately configured user identities with reviewer and processor roles.
- Server verification of the signed IAP assertion, role enforcement, bounded plain-text comments, explicit reload, durable event history, transactional versions, and retry idempotency.
- A deployment package with an explicit source allowlist, keyless runtime identity, Firestore storage, local real-component smoke, negative API tests, and copyable operator instructions.
- Two separate attended cloud gates: initial access/comments/persistence, then approval/handoff acceptance.

Excluded:

- Private request import, live Sheets/Gmail/Drive access, writes to existing bundles or anchors, payments, settlement, email sending, reimbursement-policy changes, parser repairs, or review supersession.
- Multi-organization tenancy, arbitrary request creation, file attachments, comment edits/deletes, rich text, reassignment, rework/reopening, analytics, visual redesign, public onboarding, local-role switching, and application-managed Google OAuth.
- Scheduling, background processing, polling, notifications, automatic deploy-on-push, and production adoption claims. A successful proof is not evidence that an independent participant adopted the tool for real work.

The service handles user-invoked HTTP requests that finish and return; it adds no unattended business job or observation loop. The autonomous-behavior soak trigger does not apply. Service lifecycle persistence is still verified across a fresh revision, and a real-component smoke gate precedes the longer attended workflow checks.

## 4. Impact Analysis

The impact scan used `rg` over `pta_finance`, `tests`, and `scripts` for `load_bundle`, `build_report`, `render_html`, `refresh_bundle`, `plan_bundle_refresh`, and `example_bundle`. No existing signature, shared constant, or schema changes are proposed. New workflow entities have their own version and namespace.

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `pyproject.toml`, `uv.lock` | extend | Add optional `web` dependencies and web test tooling; retain all existing extras, package selection, Python floor, and CLI entry point. | Opened dependency/script/build/tool tables. Existing consumers are package installation, Hatch build, both jobs in `.github/workflows/ci.yml`, and `.github/workflows/monthly-report.yml`; no CLI signature changes. |
| `.github/workflows/ci.yml` | extend | Exercise web dependencies and the Firestore emulator in an additional isolated job while retaining all existing gates. | Opened both jobs: Linux lint/type/test/identity and Windows native-parser tests. Existing Linux split for native parser tests must remain. No workflow path filter is present. |
| `README.md`, `CLAUDE.md` | extend | Document the optional proof, new commands, private configuration boundary, and distinction between implemented local behavior and cloud acceptance. | Read current stack/workflow/command statements against producers; both currently describe no hosted application. Update status only to the evidence actually achieved. |
| `pta_finance/reimbursement_report.py` | reuse unchanged | Validate the packaged fictional bundle through public `load_bundle(Path)`. Do not render payment-email prose into the web view. | `load_bundle` callers: same module `build_report`; pipeline `_write_bundle_atomic` and `plan_bundle_refresh`; `tests/test_reimbursement_report.py` and `tests/test_reimbursement_pipeline.py`. `render_html` callers: same module `build_report` and those two test modules. `build_report` callers: CLI report/update handlers, `scripts/capture_readme.py:render_examples`, and report tests. No caller edits required. |
| `pta_finance/reimbursement_pipeline.py`, `reimbursement_events.py`, `cli.py`, `sheets.py`, `gmail_source.py` | preserve unchanged | Protect local evidence, payment, ledger, and credential boundaries. The web entry point does not invoke these side effects. | Opened refresh writer, report/update handlers, anchor/review/payment dataclasses. `refresh_bundle` callers are the CLI update handler and pipeline tests; `plan_bundle_refresh` callers are `refresh_bundle`, CLI update handler, and pipeline tests. No changed call sites. |
| `scripts/capture_readme.py` | reuse as fixture reference only | Derive a static one-request package fixture from its first fictional ticket; do not import its Playwright dependency. | Read `digest`, `ticket`, `example_bundle`, and `render_examples`. `example_bundle` has one production caller, `render_examples`; both retain their contracts and the five-ticket screenshot fixture. |
| `.gitignore`, `.github/workflows/monthly-report.yml` | preserve unchanged | Existing ignored directories suffice; scheduling and real credential handling are unrelated. | Opened `.gitignore` and CI/workflow consumers. The pre-existing `.gitignore` diff is not part of this feature. `tests/test_workflows.py` guards the monthly workflow's report-only behavior. |
| `scripts/check_no_identity.py` | reuse unchanged | Retain the repository identity/secret gate. Add separate package-context checks because this guard scans tracked files only. | Opened implementation: `git ls-files`, service-account JSON fingerprints, optional `PTA_IDENTITY_DENYLIST`; it does not certify untracked staging files. |

New files use the components in §5. If implementation discovers an existing shared contract must change, stop that expansion, enumerate every consumer, and revise this plan before changing it.

## 5. New Components

| Component | Responsibility |
|---|---|
| `pta_finance/shared_workflow/__init__.py`, `__main__.py` | Separate optional web entry point, launched with `python -m pta_finance.shared_workflow`; import neither private CLI configuration nor mailbox/Sheet clients. |
| `pta_finance/shared_workflow/config.py`, `auth.py` | Strict runtime settings, verified IAP principal, fixed role roster, startup guards, bounded key retrieval. |
| `pta_finance/shared_workflow/models.py`, `store.py` | Separate workflow/event schema, pure transition rules, Firestore transaction implementation, immutable fictional source projection. |
| `pta_finance/shared_workflow/app.py`, `templates/request.html.j2`, `static/request.js`, `static/request.css` | FastAPI routes, same-origin protections, small autoescaped page, accessible feedback, explicit reload and comment form. |
| `pta_finance/shared_workflow/example-request.json` | Exactly one strict schema-v2 fictional bundle, based on `NEW-01`; correct provenance inventory, two mapped rows, one submission, `184.50` mapped total, zero confirmed outstanding. |
| `deployment/shared-workflow/Dockerfile`, `.dockerignore`, `cloudbuild.yaml`, `inspect_image.py`, `source-manifest.txt`, `runtime.example.json` | Fixed build/image-inspection recipe, explicit source inclusion, fictional runtime configuration, resource defaults. No actual project/user identifiers. |
| `scripts/stage_shared_workflow.py` | Local-only allowlisted source staging and content manifest. No cloud calls, runtime-config reads, deployment, or IAM reconciliation. |
| `scripts/shared_workflow_smoke.py` | Bounded local integration runner using the real HTTP server, actual token verification with ephemeral test signing keys, and Firestore emulator. The test key transport is injected in this runner only. |
| `tests/test_shared_workflow_*.py` | Auth, routes, transactions, client behavior, startup invariants, packaging/privacy, and regression boundaries. |
| `docs/shared-workflow-proof.md` | New command contracts, cloud setup sequence, identity binding, two attended acceptance scripts, retry/recovery behavior, resource shutdown instructions. Written by code steps before operator execution. |

Steps 32 and 34 implement these components for identity, comments and handoff modes; hosted handoff acceptance remains M7 / Step 35. Keep module count small; avoid a generic workflow framework or dependency on the screenshot tool.

## 6. Design Decisions

### D1 — Separate optional Python service

Use FastAPI, Uvicorn, `google-cloud-firestore`, and `cryptography` in a new `web` extra, retaining Jinja2 and existing `google-auth`. The verifier is `google.oauth2.id_token.verify_token` using `google.auth.transport.requests.Request` with the fixed IAP certificate URL; enforce the additional D2 claims explicitly. Add HTTPX and Playwright to `dev`; install Playwright Chromium explicitly for browser checks. Lock resolved versions during implementation. The web entry point uses no private CLI config discovery and accepts no arbitrary bundle path. It validates only the packaged fictional bundle through `load_bundle`, projects request title/items/amount/source identity into a new workflow model, and omits payment instructions and generated email drafts. This avoids changing private schema-v2 semantics or accidentally turning an A recommendation into approval. [Google verifier API](https://google-auth.readthedocs.io/en/latest/reference/google.oauth2.id_token.html).

### D2 — Direct IAP and verified, pinned subjects

Enable IAP directly on Cloud Run, retain authenticated invocation, grant the IAP service agent `roles/run.invoker` only on this service, and grant two named users service-scoped IAP access. No `allUsers`/`allAuthenticatedUsers` grants, direct browser Cloud Run invoker grant, or load balancer is part of this design. External/no-organization users require a suitable custom OAuth setup; project ancestry and applicable policy must be checked, not inferred from an email domain. [Google direct-IAP setup](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run).

Every application data route verifies `X-Goog-IAP-JWT-Assertion`: ES256 signature against Google's IAP keys, key ID, exact issuer `https://cloud.google.com/iap`, nonempty string `sub` and `email`, exact string audience `/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME`, expiry, issued-at, and `0 < exp - iat <= 660` seconds with at most 30 seconds clock skew. Do not trust plain authenticated-user headers or any client actor/role field. Use `verify_token(..., audience=expected_audience, certs_url="https://www.gstatic.com/iap/verify/public_key", clock_skew_in_seconds=30)` and explicit checks for constraints that helper does not enforce. Cache keys for at most 300 seconds, refresh at most once for an unknown key, and cap total key retrieval at 5 seconds. Never use stale cached keys after that bound; unavailable verification fails closed. These are fixed limits, not a new configuration subsystem. [Signed assertion contract and Python verification](https://docs.cloud.google.com/iap/docs/signed-headers-howto).

Private configuration contains exactly two entries, each with `email`, `subject`, `role`, and `enabled`; roles are exactly `reviewer` and `processor`. Subjects, once supplied, must be distinct, as must emails compared with `strip().casefold()` on both configuration and verified claim. Do not strip dots, plus suffixes, or otherwise infer account aliases. The server matches both verified subject and signed email. It uses subject as the actor key; actor labels are fixed from role: `Reviewer` and `Processor`, never client names or request-body values.

Subject discovery is a bounded setup operation: `identity` mode accepts only a fully verified assertion whose signed email matches one of the two configured entries and exposes that caller's subject/email at `GET /api/me`. It exposes no request, comments, database contents, or workflow mutations and does not initialize Firestore. Each user privately records their subject; the operator populates both subject fields and switches to `comments` mode. `comments` and later `handoff` modes refuse startup without two complete, distinct subject bindings. No automatic first-visitor enrollment, browser role selector, or unsigned local-login mode exists. Disabling one configured entry denies that user immediately for that deployed revision and supports the cloud application-allowlist negative check without inviting a third user.

### D3 — Keyless runtime and dedicated workflow storage

Attach a dedicated runtime service account to Cloud Run and use Application Default Credentials from that identity. No downloaded key, Gmail OAuth client/token, existing service-account JSON, or real config enters the image. Default storage is a named Firestore Native-mode database dedicated to this proof. Grant database access to the runtime identity with a database-scoped IAM condition; inspect inherited grants before accepting the boundary. Browser users receive no database IAM grants and no client SDK. Deny mobile/web direct access; server-side enforcement is IAM plus the API's authorization. [Firestore server security](https://docs.cloud.google.com/firestore/native/docs/security/iam), [Firestore client rules](https://docs.cloud.google.com/firestore/native/docs/security/get-started).

Firestore namespace is a private deployment parameter, `proof_<UUID>`, where UUID is a lowercase hyphenated UUIDv4 generated once by the operator. One request document lives under `workflow_proofs/{namespace}/requests/{request_id}`, with events in its `events` subcollection. The request ID is lowercase SHA-256 hex over UTF-8 `Ticket.review_key`; `source_sha256` is exactly `Ticket.source_evidence_sha256`. Display reference `NEW-01` is not the authorization key. The application can access only its configured namespace and request; clients cannot supply a namespace. Wrong/unknown request IDs return a generic 404 after authentication.

`models.py` is the single owner for all new state/action/schema shapes; API, store, browser, and tests consume this contract. Initially `schema_version=1`, `version=0`, `state=AWAITING_REVIEW`, and `next_owner_role=reviewer`. Store money as exact two-decimal strings. Firestore stores native server timestamps; the API encodes them as UTC RFC3339 strings with six fractional digits and `Z`. Nullable owner is JSON `null`, never an empty string. History is append-only through this API; administrators with database permissions remain capable of altering data, so do not describe it as tamper-proof storage.

| Shape | Exact fields / meaning |
|---|---|
| Request | `schema_version`, `request_id`, `review_key`, `source_sha256`, `display`, `state`, `version`, `next_owner_role`, `created_at`, `updated_at`. `display = {ref, title, submitted_on, total, items}`: ref is Ticket.ref, title is fixed `Classroom supply reimbursement`, date is Ticket.submitted as ISO `YYYY-MM-DD`, and each item is `{item_key, description, amount, category}` from `item_key`, `source_description`, `source_amount`, `canonical_category`. Total is the exact sum of the two source amounts; no reviewed/payment fields. |
| Event / receipt | `operation_id`, `payload_sha256`, `actor_sub`, `actor_label`, `actor_role`, `request_id`, `source_sha256`, `action`, `body`, `expected_version`, `previous_state`, `result_state`, `result_version`, `next_owner_role`, `created_at`. Action is `comment`, `approve`, `not_approve`, or `complete`. `created_at` is the committed server timestamp. |
| `GET /api/me` success | HTTP 200 `{mode, actor: {subject, email, label, role}, request_id}`. Email comes from the normalized signed claim; request_id is null in identity mode, otherwise the one request ID. No JWT or full roster. |
| Request GET success | HTTP 200 `{request: Request, events: [Event], event_cap: 100}`; events are the complete bounded sequence at request.version, ascending by result_version. |
| POST success, including identical retry | HTTP 200 `{receipt: Event}`. Same committed event produces the same receipt, including timestamp; fetch current request afterward rather than treating an old receipt as current state. |
| API error | `{error: {code, message, correlation_id}}` with the HTTP status from D6. Message is fixed safe prose for that code; correlation_id is a new UUIDv4. No submitted text, assertion, identity, or database exception. |

Seed the single request during data-mode startup with a create-if-absent transaction. An existing document must match schema, source digest, and request identity; mismatch fails closed with `SOURCE_MISMATCH`. Identity mode never connects to the database. Restart/redeploy never resets state or history. No reset/delete API is shipped. Repeating an alternative branch uses a new namespace with the same one fictional request, preserving the earlier proof.

### D4 — Transactions, retries, and a bounded read model

Every comment/transition POST supplies a lowercase hyphenated UUIDv4 `operation_id` (browser `crypto.randomUUID()`) and nonnegative integer `expected_version`; booleans do not qualify as integers. In one Firestore transaction, read the request and event keyed by operation ID before any writes. Check current allowlist authorization first. If that operation already exists, return its original receipt only when verified subject and canonical payload agree; otherwise return `409 OPERATION_CONFLICT` without exposing the other actor's payload. Perform this check before cap/state/version validation so a genuine retry after a committed transition succeeds. New operations must match expected version and valid action/role/state, then atomically increment version once and create exactly one immutable event with a server timestamp. Transaction callbacks have no network side effects beyond their database operations. [Firestore transaction semantics](https://docs.cloud.google.com/firestore/native/docs/manage-data/transactions).

Canonical payload is exactly `{request_id, source_sha256, action, body, expected_version, operation_id}` serialized by Python `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)` then UTF-8 encoded and SHA-256 hashed to lowercase hex. Preserve submitted body text exactly after validation; do not trim or normalize it before hashing/storage. Source fields and action are server-derived, not accepted as extra input keys. Resolve commit timestamps before returning a successful receipt; a read-after-commit failure returns a retryable error, never rolls back or invents a timestamp. The browser retains the same operation ID and exact payload for an uncertain retry. A genuine edit or conflict resolution creates a new ID. A `409 STALE_VERSION` triggers reload and deliberate resubmission, never automatic approval against newer state.

Each namespace has a fixed cap of **100 events**, counted by request.version because every event advances it once. Check an identical retry before the cap; reject any new operation at version 100 with `409 EVENT_CAP_REACHED`, without changing state. The API exposes no limit/cursor/page parameters and rejects unexpected query parameters. Request GET reads the request at version V, then at most 100 immutable events with result_version <= V ordered ascending; require exactly versions 1 through V or return `503 STORE_INCONSISTENT`. Concurrent later writes cannot enter that view. This is one bounded full read, not pagination. To continue beyond the cap, the operator selects a new fictional namespace through runtime config and redeploys; existing evidence remains intact.

### D5 — Small same-origin API and browser behavior

| Route | Access and behavior |
|---|---|
| `GET /` | IAP-authenticated page; identity mode shows only the caller's setup status. Comments mode shows the one fictional request, actor, state/owner, history, comment box, and Reload. |
| `GET /api/me` | Verified enabled roster identity; returns caller subject/label/role and mode. Identity mode permits subject discovery by exact signed email only. No JWT is returned. |
| `GET /api/requests/{request_id}` | Both pinned participants may read in comments/handoff modes; bounded history per D4. |
| `POST /api/requests/{request_id}/comments` | Both pinned participants; strict body `{operation_id, expected_version, body}`. Blank comments rejected; state/owner unchanged. |
| `POST /api/requests/{request_id}/decision` | Added in Step 34 only; reviewer submits `{operation_id, expected_version, decision, body}`, where decision is `approve` or `not_approve`. A comment is required. |
| `POST /api/requests/{request_id}/complete` | Added in Step 34 only; processor submits `{operation_id, expected_version, body}`; comment may be blank. |
| `GET /healthz` | Non-sensitive process health only, no identity/config/database details; only application auth exemption. Platform IAP remains enabled. |

Serve static resources from the same origin. Every mutation requires an exact configured HTTPS `Origin`, `Content-Type: application/json`, and custom header `X-PTA-CSRF: 1`. Reject missing/null/mismatched Origin and any supplied `Sec-Fetch-Site` value other than `same-origin`. Do not enable CORS; cross-origin preflights receive no permission, and GET never mutates workflow state. The custom header and strict JSON contract prevent simple cross-site form submission; signed-in requests still require full authentication/authorization. Browser tests exercise the actual protection, not only header helpers.

Use a canonical origin from validated private deployment configuration; do not derive it from arbitrary forwarded Host headers. Data modes enforce the configured Host, accepting only documented platform probe handling at `/healthz`. Identity mode may start before the service URL is known because it has no mutation/data routes and emits only relative links; obtain the actual URL from deployment metadata and pin it before enabling comments. Request bodies are limited to 8 KiB before parsing; comments are plain text, 1–2,000 characters when required, with a defined whitespace-only rejection. Reject extra JSON fields, duplicate JSON keys, invalid Unicode and non-finite numeric tokens. Render comments through autoescaping/textContent, never Markdown or innerHTML. Add `Cache-Control: no-store` for page/API data, `X-Content-Type-Options: nosniff`, and a CSP permitting only self-hosted scripts/styles/connects with `frame-ancestors 'none'`, `base-uri 'none'`, and `form-action 'self'`.

On success, reload current server state. Show a pending indicator and disable repeat-clicks while saving, without using disabled buttons as authorization. Preserve comment drafts in memory on a retryable error, with an explicit Retry action. A login redirect/HTML response during a JSON request is shown as a session-refresh action using top-level navigation to the canonical service; it never looks like a successful save. No localStorage authentication or fake identity controls.

### D6 — Startup invariants and errors

`__main__` reads one required environment variable, `PTA_WORKFLOW_CONFIG`, as strict JSON through `config.py`; no file search, default private configuration, OAuth secret, or application CLI configuration flag exists. Reject unknown/duplicate keys and invalid types. `PORT` is the only other application setting: validated integer 1–65535, default 8080; bind `0.0.0.0` with Uvicorn. The fictional `deployment/shared-workflow/runtime.example.json` defines this complete initial configuration shape:

```json
{
  "schema_version": 1,
  "mode": "identity",
  "project_id": "example-project",
  "project_number": "123456789012",
  "region": "us-central1",
  "service_name": "pta-workflow-proof",
  "origin": null,
  "database": null,
  "namespace": null,
  "users": [
    {"email": "reviewer@example.org", "subject": null, "role": "reviewer", "enabled": true},
    {"email": "processor@example.org", "subject": null, "role": "processor", "enabled": true}
  ]
}
```

Example values are synthetic, not deployment defaults. Copy this to ignored `secrets/shared-workflow.runtime.json` and fill actual project/region/two-email values privately. For `comments` and later `handoff`, both subjects become distinct nonempty strings, origin becomes the exact HTTPS service origin without a trailing slash, database becomes the selected named database (default selection `workflow-proof`), and namespace becomes `proof_` plus a lowercase hyphenated UUIDv4. Identity mode needs none of those four data-mode values and never touches Firestore. A disabled roster entry remains structurally present but cannot read/write.

For local production-entry-point startup, read the private JSON into the environment without printing it:

```powershell
$env:PTA_WORKFLOW_CONFIG = [IO.File]::ReadAllText((Resolve-Path -LiteralPath 'secrets/shared-workflow.runtime.json'))
uv run python -m pta_finance.shared_workflow
```

Stop with Ctrl+C, then `Remove-Item Env:PTA_WORKFLOW_CONFIG`. This launch still requires valid IAP assertions for protected routes; it is useful for startup/health checks, not an alternate login. The separate smoke runner below supplies test identities only through its test factory.

Cloud Run receives the same JSON through a private environment file, created without printing configuration values:

```powershell
$PtaRuntimeJson = [IO.File]::ReadAllText((Resolve-Path -LiteralPath 'secrets/shared-workflow.runtime.json'))
$PtaEnvJson = @{ PTA_WORKFLOW_CONFIG = $PtaRuntimeJson } | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path (Resolve-Path -LiteralPath 'secrets') 'shared-workflow.env.json'), $PtaEnvJson, [Text.UTF8Encoding]::new($false))
```

Pass `--env-vars-file=secrets/shared-workflow.env.json` to the explicit `gcloud run deploy` command in M6. Neither private file enters staging or the image. OAuth client secrets remain exclusively in IAP configuration, not this roster.

Startup failure codes are `CONFIG_MISSING`, `CONFIG_INVALID`, `IDENTITY_BINDING_REQUIRED`, `DUPLICATE_IDENTITY`, `ORIGIN_INVALID`, `UNSAFE_RUNTIME_ENV`, `FIXTURE_INVALID`, and `SOURCE_MISMATCH`. Emit only the code and safe explanation, exit nonzero, and do not start serving. Production rejects emulator variables and any test-key/trust override (`UNSAFE_RUNTIME_ENV`); non-HTTPS configured origins fail even in identity mode. Missing packaged resources fail before readiness. Data-store startup unavailability is `STORE_UNAVAILABLE`, with no local/anonymous fallback. Test-only construction injects a loopback origin, ephemeral key transport, and emulator client into the same app factory; the shipped entry point always uses the strict environment loader and production verifier. No request, environment variable, CLI auth switch, or packaged test launcher can activate test trust.

Use a 5-second per-database-call deadline, at most 3 transaction attempts, and a 20-second total mutation deadline including verification/retries; pass remaining time into nested calls rather than multiplying budgets. No retries of validation/permission failures. On exhausted contention return `409 RETRY_CONFLICT`; on key service/database unavailability return `503 TEMPORARILY_UNAVAILABLE` without a speculative success. Missing/invalid JWT is `401 UNAUTHENTICATED`; valid but unlisted/disabled/mismatched identity or wrong role is `403 FORBIDDEN`; unknown request is `404 NOT_FOUND`; invalid input is `400 INVALID_INPUT`; excessive body is `413 BODY_TOO_LARGE`; other 409 codes are `STALE_VERSION`, `INVALID_TRANSITION`, `OPERATION_CONFLICT`, and `EVENT_CAP_REACHED`. Inconsistent history is `503 STORE_INCONSISTENT`; unexpected failure is `500 INTERNAL_ERROR`. All application API errors use the D3 envelope. Logs retain route, result code, duration, and correlation ID, never assertions, cookies, comments, emails, private config, or raw database exception content. UI errors expose safe codes and an actionable retry/reload instruction.

### D7 — Two-role lifecycle, separate from payment

| Current state | Actor/action | Next state | Next owner |
|---|---|---|---|
| `AWAITING_REVIEW` | Reviewer: approve with comment | `APPROVED` | processor |
| `AWAITING_REVIEW` | Reviewer: not approve with comment | `NOT_APPROVED` | none |
| `APPROVED` | Processor: complete | `COMPLETED` | none |
| Any state | Either allowed participant: comment | unchanged | unchanged |

Initial next owner is reviewer. The reviewer sees Approve and Not approve as mutually exclusive checkbox-style choices, initially unselected, beside a free-text comment field and an explicit save action. Exactly one outcome plus a nonblank comment is required; accessible radio-group semantics preserve the single-choice behavior. All unlisted transitions fail, including processor decisions, reviewer completion, completion before approval, completion after not-approval, and changed decisions after a terminal result. Not approve is terminal for this proof. Completion means “workflow handoff complete”; the page states that it does not record a payment. Never map it to existing `SETTLED`, `PAID`, anchors, or operator payment records. Comments cannot change a decision. After Step 32, no decision/complete handlers or controls exist; Step 34 has a hard dependency on the real Step 33 gate.

### D8 — Deployment packaging and operational defaults

Use Cloud Build to build a staged allowlisted context and Artifact Registry to store the image; deploy an immutable image digest. `source-manifest.txt` names required metadata plus individual package source/template/static files and the single synthetic JSON. The local-only staging command creates a new output directory, copies only those declared paths, and writes a path/hash manifest; fail if the output already exists, rather than deleting it. Reject symlinks/reparse points, paths outside the repo, and unexpected staged files. Never run `gcloud ... --source .` or upload the working tree. `.dockerignore` is an additional safeguard, not the privacy boundary. Step 32 inspects staged source and the installed wheel for absence of `config.toml`, `secrets/`, mail archives, snapshots, private outputs/task state, credentials, and planted test canaries. Include untracked canaries because the existing identity guard scans tracked files only.

The Docker recipe installs the locked web extra, includes packaged resources, runs as a non-root user, binds `0.0.0.0` to Cloud Run's `PORT`, and uses no writable local persistence. Default service settings are one CPU, 512 MiB, minimum zero instances, maximum two, concurrency 20, and timeout 30 seconds. These are proof defaults, not a cost guarantee. Cloud Run local files are ephemeral, so only Firestore counts as persistent state. [Cloud Run container contract](https://docs.cloud.google.com/run/docs/container-contract).

There is no cloud planner/applier/verifier framework. Step 32 writes a short runbook of fixed explicit gcloud commands for the named proof resources: read-only readiness/access checks, necessary API/resource setup, database-conditioned runtime access, service-scoped IAP/invoker grants, build, image receipt, deploy, and read-only verification. Use explicit project/region and runtime/build identity arguments, not changed gcloud defaults. The operator checks resource names/access before running mutations; never reuse unrelated resources or broaden access to work around a failure. Initial external-user OAuth setup remains a documented Console action, with no secret retrieval/printing.

The fixed `cloudbuild.yaml` builds the image, inspects that actual image's application file inventory and environment for forbidden private paths/credential files, exercises packaged-resource loading inside it, then publishes it only if inspection passes. That image inspection executes in **M6 after the build and before deployment/admission**, not in the local code gate. Record build ID, inspection result, and resulting image digest privately. Step 32 checks the recipe/source/wheel but does not claim an image was inspected. No local Docker installation is required.

The code step authors the staging tool, fixed recipe, and runbook before the operator gate. Every deploy command uses a fresh revision suffix so the persistence check starts a new revision even with the same image digest. Private configuration changes, receipts, and screenshots are runtime evidence, not new shipped code. Shutdown instructions remove proof access and preserve database evidence; deletion remains a separate explicit operation.

### D9 — Local and cloud evidence are different gates

Local tests use real cryptographic verification with ephemeral ES256 keys and a controlled certificate transport. The HTTP-to-Firestore smoke starts the actual Uvicorn server at fixed loopback `127.0.0.1:8788`, loads the installed package's strict source fixture, and uses the Firestore SDK/emulator, transaction code, and two signed fixture principals; no mocked repository/store. It launches headless Playwright Chromium by default and injects each signed assertion through a separate browser context. The test factory supplies the exact loopback origin and certificate transport, never the production environment loader. It completes one save/read cycle within 60 seconds after readiness, asserting the D3 response shapes through the actual page/route boundary, then restarts only the app process and rereads the event. The runner owns and stops its server/browser on success, error, deadline, or interruption; it leaves the separately started emulator running. No test identity mechanism appears in the deployed entry point.

Run the fuller conflict/browser checks after that smoke. Local emulator concurrency and index behavior are not identical to production; the cloud acceptance must exercise real writes and bounded history too. Emulator setup requires a compatible Java runtime (default Java 21+) and the gcloud Firestore emulator. [Emulator setup and limitations](https://docs.cloud.google.com/firestore/native/docs/emulator).

Cloud proof requires two isolated browser profiles signing into two independent Google account subjects. Account aliases or a UI role toggle fail that condition. Record actual subject/config/revision evidence privately. The same operator may control both independent accounts for functional proof; independent adoption is a different claim.

### D10 — Operational prerequisites remain visible

Before Step 33, the operator must supply or verify: accessible project ID/number; actual organization ancestry and external-principal policy; billing and required API readiness; service/Firestore/Artifact Registry locations; distinct account-role assignments; IAP OAuth audience/client readiness; deployment identity permissions for build, service creation/update, IAP policy, runtime service-account attachment, and necessary resource provisioning; and the dedicated runtime database access boundary. Authentication in gcloud does not prove these permissions. A denied metadata read means unknown, not absent. Keep all results private and stop the affected cloud action on unmet prerequisites; do not silently substitute Firebase, public invocation, local JSON persistence, or the private toolkit's credentials.

## 7. Build Steps

This plan has two separately invoked phases. `build-phase` defers pure operator steps to its phase-end UAT bundle, so a single invocation over all four steps would skip past the early proof. **Never invoke this entire plan as one unattended span.** Phase A contains only Steps 32–33; its code execution ends at Step 32 and emits M6 for attended completion. Phase B is ineligible until the orchestrator has read the private M6 record, verified the required observations, and marked Step 33 DONE. `Depends on` alone is not the enforcement mechanism.

Use `--reviewers deep --isolation worktree` for both code steps because authentication and persistent authorized transitions require deeper code review. This is a code-review lane; the deployed UI remains auth-gated, so cloud browser proof is operator acceptance. Local browser coverage is explicit in the code gates. Leave issue fields blank until plan-review → plan-redline → plan-wrap have passed and repo-sync supplies issue IDs.

First invocation, from the repository root:

```text
/build-phase --plan documentation/shared-workflow-proof-plan.md --phase A
```

Do not issue the Phase B command until the M6 check described above is complete. Afterward, invoke it separately:

```text
/build-phase --plan documentation/shared-workflow-proof-plan.md --phase B
```

## Phase A — Shared-comment proof (Steps 32–33)

#### Automated delivery A

<!-- autofix-applied: 2026-09-11 -->
### Step 32: Make the fictional request available for authenticated shared comments

- **Problem:** Deliver the smallest deployable shared-comment slice through the real HTTP entry point.
- **Type:** code
- **Issue:** #63
- **Flags:** --reviewers deep --isolation worktree
- **Status:** DONE
- **Files:** New `pta_finance/shared_workflow/` source/resources listed in §5; new `deployment/shared-workflow/` files listed in §5; new `scripts/stage_shared_workflow.py`, `scripts/shared_workflow_smoke.py`, `tests/test_shared_workflow_*.py`, and `docs/shared-workflow-proof.md`; existing `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml`, `README.md`, and `CLAUDE.md`. Reuse `pta_finance/reimbursement_report.py` unchanged; preserve `.gitignore` and private inputs.
- **Produces:** The optional web package, packaged one-request fixture, strict environment configuration/IAP verifier, identity probe, GET/me/request and comment POST routes, Firestore transactions, capped full history, minimal page, local source-staging tool, fixed Cloud Build recipe and gcloud runbook, tests, web CI job, and accurate optional-feature documentation. No decision/completion handlers or controls.
- **Done when:** Both fixture principals save/read attributed comments through real HTTP and the Firestore emulator; the <=60-second smoke asserts D3's request/receipt/error contracts before longer checks, and app-process restart retains both events. Signed-token validation, direct actor/role spoofing, invalid/unlisted identities, CSRF, schema/body bounds, event cap, stale version, duplicate/changed operation, contention, timeout, startup, and source-context/wheel privacy checks pass. The installed wheel includes and loads fixture/static/template resources from outside the checkout. All local repository gates in §9 pass. The staging command and prepared recipes/runbook are checked without cloud writes; actual image inspection and M6 cloud acceptance remain pending.
- **Depends on:** none; review and issue synchronization prerequisites apply.

#### Manual acceptance A — M6

<!-- autofix-applied: 2026-09-11 -->
### Step 33: Prove two independent Google users share durable state on Cloud Run

- **Problem:** Establish real browser identity and cross-session persistence on the intended deployment before business controls are built.
- **Type:** operator
- **Issue:** #64
- **Status:** DONE
- **Files:** Execute prepared `scripts/stage_shared_workflow.py`, the fixed `deployment/shared-workflow/cloudbuild.yaml`, and `docs/shared-workflow-proof.md` commands without source edits; configure private `secrets/shared-workflow.runtime.json` / `shared-workflow.env.json`; record runtime evidence under `reports/output/shared-workflow/`.
- **Produces:** Private deployed-resource/revision/access receipts and an attended pass/fail record for M6; no code artifacts or application-source changes.
- **Done when:** The actual Cloud Build image passes inspection before deployment, and all M6 observations below pass on one deployed comments-only revision and the subsequent fresh revision. The two observed subjects are distinct and pinned; A reads B's saved comment and B reads A's; both survive redeploy to the same database/namespace. Signed-out and disabled-roster access reveal no request data, direct actor/role spoofing cannot alter attribution, malformed signed assertions fail closed where the IAP test mechanism permits injection, and deployed IAM/source-context checks match the prepared boundaries. Unavailable cloud checks remain failed/pending rather than being replaced with local evidence. Record the exact image digest/revisions and checked outcome in private `reports/output/shared-workflow/m6-acceptance.md` before marking Step 33 DONE.
- **Depends on:** 32
- **Acceptance:** M6 passed on 2026-09-12; the checked private record includes all deployed observations and the operator pass verdict. Public summary: [M6 closeout](shared-workflow-proof-sync.md#m6-closeout--2026-09-12-utc).

Commands produced by Step 32, run from the repository root after filling private runtime configuration and completing the runbook's explicit Google setup commands. The following shell variables are supplied privately: `$PtaProject`, `$PtaRegion`, `$PtaBuildRegion`, `$PtaService`, `$PtaRuntimeIdentity` (email), `$PtaBuildIdentity` (full service-account resource name), `$PtaSourceBucket` (dedicated source bucket), `$PtaImageTag` (unique Artifact Registry tag), and `$PtaStage` (new staging path under `reports/output/shared-workflow/`). None are hard-coded into source. An unavailable prerequisite blocks the cloud action; no automatic fallback/reconciler is introduced.

```powershell
uv sync --locked --extra dev --extra web
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed; stop this procedure.' }
uv run python scripts/stage_shared_workflow.py --output $PtaStage
if ($LASTEXITCODE -ne 0) { throw 'Source staging failed; stop this procedure.' }
gcloud builds submit $PtaStage --project=$PtaProject --region=$PtaBuildRegion --service-account=$PtaBuildIdentity --gcs-source-staging-dir="gs://$PtaSourceBucket/source" --config="$PtaStage/cloudbuild.yaml" --substitutions="_IMAGE=$PtaImageTag"
if ($LASTEXITCODE -ne 0) { throw 'Build or image inspection failed; stop this procedure.' }
```

After the build's actual image-inspection/resource-load steps pass, use its receipt to set `$PtaImageDigest` to the immutable full image URI including `@sha256:...`. Do not deploy an unchecked tag. Create the private environment file using D6, then deploy:

```powershell
$PtaRevision = 'proof-' + [Guid]::NewGuid().ToString('N').Substring(0,12)
gcloud run deploy $PtaService --project=$PtaProject --region=$PtaRegion --image=$PtaImageDigest --service-account=$PtaRuntimeIdentity --env-vars-file=secrets/shared-workflow.env.json --no-allow-unauthenticated --iap --revision-suffix=$PtaRevision --cpu=1 --memory=512Mi --min-instances=0 --max-instances=2 --concurrency=20 --timeout=30s
if ($LASTEXITCODE -ne 0) { throw 'Deployment failed; stop this procedure.' }
gcloud run services describe $PtaService --project=$PtaProject --region=$PtaRegion --format='value(status.url,status.latestReadyRevisionName)'
if ($LASTEXITCODE -ne 0) { throw 'Service verification failed; stop this procedure.' }
gcloud run services get-iam-policy $PtaService --project=$PtaProject --region=$PtaRegion
if ($LASTEXITCODE -ne 0) { throw 'Invocation policy verification failed; stop this procedure.' }
gcloud iap web get-iam-policy --project=$PtaProject --region=$PtaRegion --resource-type=cloud-run --service=$PtaService
if ($LASTEXITCODE -ne 0) { throw 'IAP policy verification failed; stop this procedure.' }
```

These are fixed CLI surfaces, verified in the [build](https://docs.cloud.google.com/sdk/gcloud/reference/builds/submit) and [deploy](https://docs.cloud.google.com/sdk/gcloud/reference/run/deploy) references. The prepared runbook includes the separate exact commands for APIs, registry/build identity, runtime identity/database condition, IAP invoker/user access, image digest receipt, and filtered IAP-enabled verification. Use existing-resource metadata only after checking ownership; no unrelated resource modification is implied.

Start with `mode: identity` in the private runtime JSON. Complete one-time IAP external-user setup, open the service in two independent profiles, and bind both verified subjects privately. Pin the service URL as origin, set database/namespace and `mode: comments`, regenerate the environment file, and repeat only deploy/verify commands with a new suffix and the same checked image digest. The runbook must keep the original Gmail OAuth client separate. OAuth setup failure never justifies public invocation.

| M6 observation | Required evidence |
|---|---|
| Actual image, before admission | Cloud Build builds the image and then inspects its application file inventory/environment for private-input paths, credentials, and synthetic canaries; its resource-load check passes. Record the build ID, inspection result, and immutable digest before any deployment. Source/wheel checks alone do not satisfy this row. |
| Identity | Separate Google sign-ins; each `/api/me` returns its own subject and assigned role. Identity mode exposes no request/history. Distinct signed subjects are recorded privately. |
| Shared data | Within the first 60 seconds after data-mode readiness, A loads the request, posts a unique fictional comment, and B reloads it. Then B comments and A reloads. Each event has the correct server actor, timestamp, and sequential version. |
| Cloud durability | Redeploy the same image/config to force a new revision with the same namespace. Both profiles reload both comments. Record the changed revision and unchanged saved events. A local process or browser cache cannot satisfy this check. |
| Denial | Signed-out profile receives Google sign-in/access denial with no request data. Temporarily disable B in the private application roster while retaining its IAP admission, redeploy, and confirm B receives 403 from data routes; restore and reverify. Include direct POST actor/role extra fields and missing/wrong Origin/custom header checks from an authenticated browser. |
| Platform and storage | IAP enabled, private invocation, exact two-user IAP policy, attached dedicated runtime identity, intended database/namespace, image digest and allowlisted upload receipt. Verify broader inherited access has not defeated the boundary. |
| Error handling | Denied/invalid requests create no event. On a transient service error, UI never reports a save that lacks a durable receipt. Use Google's documented signed-token testing facility when available; record unsupported cloud injection cases as local verifier coverage, not cloud passes. |
| Portability | Stop the local development service. Both users can still perform the hosted reload/comment flow. The operator identifies the service, durable store, role configuration, and repeat procedure. |

Phase A ends after Step 32's code delivery with **run M6 / Step 33 next**. Any M6 failure is repaired within Phase A and that gate rerun. The orchestrator checks the private M6 record and marks Step 33 DONE before invoking Phase B; a deferred-UAT bundle or future promise is not sufficient.

## Phase B — Approval handoff (Steps 34–35)

**Entry gate:** Step 33 must already be DONE with its verified private M6 record. A session asked to invoke Phase B before that evidence exists returns to M6; it does not dispatch Step 34. This separate invocation boundary enforces the early proof despite build-phase's operator-step deferral.

#### Automated delivery B

<!-- autofix-applied: 2026-09-11 -->
### Step 34: Enforce the reviewer-to-processor handoff

- **Problem:** Make one server-authorized approval outcome determine the next participant's permitted action.
- **Type:** code
- **Issue:** #65
- **Flags:** --reviewers deep --isolation worktree
- **Status:** DONE
- **Files:** `pta_finance/shared_workflow/config.py`, `models.py`, `store.py`, `app.py`, `templates/request.html.j2`, and `static/request.js`; `tests/test_shared_workflow_*.py`, `scripts/shared_workflow_smoke.py`, and `docs/shared-workflow-proof.md`; update `README.md` and `CLAUDE.md` only for the delivered optional behavior. Keep private reimbursement modules unchanged.
- **Produces:** Decision/completion endpoints and minimal controls, D7 transition enforcement using the existing transaction lane, handoff mode, role-specific next-action wording, full lifecycle/denial/retry/concurrency tests, and prepared M7 runbook/browser checks.
- **Done when:** Through the production HTTP routes with actual Firestore emulator transactions, reviewer approval transfers ownership and only processor completion closes the workflow; not-approval is terminal and cannot be completed; comments never change decisions. Same-operation retries produce one event, conflicting/stale decisions do not overwrite each other, and direct wrong-role requests fail even when browser controls are bypassed. Local browser tests show escaped comments, accessible outcomes, refresh after version conflict, and safe session/network errors. All §9 repository gates pass. M6 is already recorded DONE; no human cloud acceptance is hidden in this code gate.
- **Depends on:** 33
- **Delivery:** Complete candidate and main repository gates and six fresh independent review lenses passed on 2026-09-13 UTC. Actual cloud image inspection and the two-account handoff observations remain Step 35/M7. See [delivery evidence](shared-workflow-proof-sync.md#step-34-delivery--2026-09-13-utc).

#### Manual acceptance B — M7

<!-- autofix-applied: 2026-09-11 -->
### Step 35: Accept the two-role handoff on the deployed service

- **Problem:** Demonstrate both terminal outcomes with the two real Google sessions using the shared cloud store.
- **Type:** operator
- **Issue:** #66
- **Status:** PENDING
- **Files:** Execute prepared staging/build/deploy commands from `docs/shared-workflow-proof.md` without source edits; configure private `secrets/shared-workflow.runtime.json` / `shared-workflow.env.json`; record runtime evidence under `reports/output/shared-workflow/`.
- **Produces:** Private M7 acceptance observations, final image/revision receipt, and a concise functional-proof verdict; no code artifacts.
- **Done when:** Every M7 row passes with the two pinned real subjects. The existing namespace retains the earlier comments across deployment, approval transfers ownership, only processor completion succeeds, and terminal history remains after another fresh revision. A separate fresh namespace with the same single fictional request proves not-approval and denial of completion without resetting/deleting the first proof. Real-cloud duplicate/stale/concurrent API checks and bounded history checks create exactly the expected events. The operator confirms administrative completion wording and records functional proof separately from any adoption claim.
- **Depends on:** 34

Stage/build Step 34's source with the same fixed commands, inspect the actual image, and record its new digest. Set private runtime JSON mode to `handoff`, retain the M6 namespace, regenerate the environment file, and deploy that checked image digest using M6's explicit commands with a new revision suffix. After preserving the approval branch, select a fresh `proof_<UUID>` namespace for the not-approve branch and redeploy the same digest. No source code or existing workflow document is edited to reset state.

| M7 observation | Required evidence |
|---|---|
| Approved handoff | Reviewer submits approve with a fictional comment. Processor reloads, sees `APPROVED` and processor ownership, and can complete. Reviewer completion and processor decision requests return 403 when called directly. |
| Completion | Processor completes once; both profiles read `COMPLETED`, no next owner, correct actor/event ordering, and wording that does not assert payment. Redeploy preserves the result and earlier comments. |
| Not-approved branch | Fresh namespace, same one sample request: reviewer selects not approve with a reason. Processor sees `NOT_APPROVED` and cannot complete; reviewer cannot reopen/change it. Original namespace evidence remains intact. |
| Races and retries | Use the prepared authenticated-browser snippets: replay an identical operation, reuse an ID with changed data, send stale expected versions, and race two different reviewer decisions from two tabs of the reviewer's real session. At most one competing transition commits, retries return their original receipt, and new rejected operations add no events. |
| Read boundaries | Both users read the complete event sequence matching the returned request version; concurrent later comments appear on reload. Unexpected query parameters and unknown request IDs fail safely. The 100-event cap/retry boundary is mandatory emulator coverage; there is no need to manufacture 100 cloud comments for acceptance. |
| Final explanation | Both users can identify the current decision, next owner, and who wrote each comment. Save exact cloud proof metadata privately; public closeout contains only generic pass/fail and scope. |

Handoff at the end of Step 34: **run M7 / Step 35 next**. These steps authorize no real reimbursement, money movement, or message sending.

## 8. Risks and Open Questions

| Item | Risk | Mitigation / owner |
|---|---|---|
| Actual Google project and region | Existing credentials or an account domain do not establish project access, ancestry, billing, or deployment permissions. | Operator supplies private values and performs the runbook's read-only prerequisite checks before setup. Readiness discovery never enables APIs. Region remains an explicit private selection. |
| External-user IAP setup | Organization policy/OAuth configuration may block one user. | Operator verifies effective policy and completes custom OAuth setup when needed. Preserve IAP/private invocation; an architectural change requires a plan revision. |
| Two account subjects | Two email addresses can refer to one principal; roles could be misbound. | Identity-only probe, explicit private binding, duplicate-subject startup rejection, and real two-profile evidence. Roles already chosen: reviewer and processor. |
| Runtime/build permissions | Overbroad inherited access or build defaults can breach isolation. | Dedicated runtime identity, database condition, service-scoped IAP/invoker grants, explicit keyless build identity, and operator inspection of the fixed commands and resulting policies before acceptance. |
| Readiness of local tools | Java/emulator/container build tooling may be missing. | Code step documents/test-drives prerequisite detection. CI adds an emulator job; absent tooling is not a mocked-smoke pass. Cloud Build supplies the image build, avoiding a mandatory local Docker dependency. |
| Local-vs-cloud semantics | Emulator does not prove IAP, production contention, indexes, or real IAM. | M6/M7 remain separate required cloud gates; record failures and exact revisions privately. |
| Approval semantics | Existing A recommendations or administrative completion could be mistaken for payment authorization. | Separate states/store, explicit initial `AWAITING_REVIEW`, no payment API, no reuse of `pay_now` as permission, and M7 wording check. |
| Private build context | Ignored data can still enter an upload or image. | Positive source manifest, staged-context and image inspection, synthetic leak-canary tests, no raw-root upload. Existing tracked-only identity guard remains an additional gate. |
| Sibling work and dirty file | Dependency/CI/doc conflicts or accidental inclusion of private/local edits. | Recheck HEAD/status and all plan numbers, isolated worktree, scoped commits later, preserve existing `.gitignore` diff. |
| Evidence retention / costs | Hosted resources persist beyond the demonstration. | Private owner records selected database/location and intended retention; runbook shows conservative scaling defaults and access shutdown preserving evidence. No price/free-tier assumption. |

No product-behavior decision remains open: comments are plain text, not-approve is terminal, completion is administrative, and both participants may comment in any state. Outstanding inputs are operational values and actual permission/account observations, not hidden code assumptions.

## 9. Testing Strategy

Run narrow tests during development; a code-step DONE gate runs the complete applicable suite. Retain `tests/test_reimbursement_report.py`, `test_reimbursement_pipeline.py`, `test_reimbursement_events.py`, `test_reimbursement_cli.py`, receipt/Sheet/Gmail tests, and the existing monthly workflow guards. Test fixture validation through `load_bundle`, including source inventory/total consistency; never weaken its validator to accept the web fixture.

New tests must cover:

1. Real JWT signature verification with ephemeral keys: valid ES256, wrong signature/algorithm/key/issuer/audience, missing or malformed required claims, expired/future/overlong tokens, unsigned identity headers, missing assertion, key-fetch failure, and role/subject/email mismatch. Only test code controls the certificate transport.
2. HTTP authorization and CSRF through the actual app: all data routes, identity-only confinement, wrong-role direct API calls, rejected actor/role extras, content-type/origin/custom-header requirements, extra/duplicate JSON keys, limits, safe errors, and no sensitive response caching/logging.
3. Firestore emulator transactions: immutable seed, schema/source mismatch, persist/reload, actor attribution, exact one-event retries, changed-operation collision, stale expected version, concurrent operations, timeout/retry exhaustion, server timestamps, and bounded complete reads at request.version. Exercise the 100-event cap, rejection of a new operation at the cap, and successful identical retry at the cap. Integration tests are marked explicitly and fail if the requested emulator is absent, rather than silently skip.
4. Real local HTTP/browser smoke using the installed package, emulator, and ephemeral signed test identities: one end-to-end cycle within 60 seconds after readiness, exact D3 response-shape assertions through the actual page/routes, app-process restart, XSS rendering, lost-response retry, stale conflict, and non-JSON login/error responses. Run the longer matrix only after the smoke passes.
5. Local distribution/privacy: staged source and installed wheel contain required resources, work outside the repo, cannot select private inputs, reject production test/emulator settings, and exclude synthetic private canaries. Actual built-image inspection belongs to M6 after Cloud Build, before deployment, and repeats for the Step 34 image in M7. Generic identity tests enforce only fictional example domains and sample organization names in new fixtures/config/examples; use the existing private denylist when supplied without printing its values.

Required Windows gate commands after the new extra exists:

```powershell
uv sync --locked --extra dev --extra slides --extra web
uv run playwright install chromium
uv run pytest -q
uv run mypy --strict pta_finance
uv run ruff check .
uv run ruff format --check .
uv build
uv run python scripts/check_no_identity.py
```

The full web-enabled suite assumes the Firestore emulator is running. Register an `integration` marker for HTTP/store tests, require `FIRESTORE_EMULATOR_HOST`, and fail collection of those tests when it is missing; no missing-emulator skip may masquerade as evidence. Start the emulator separately at fixed loopback `127.0.0.1:8787`, then the new smoke runner. The runner owns server port 8788; fail clearly if it is occupied. Commands below are the planned runner contract; the emulator command is documented by Google:

```powershell
gcloud emulators firestore start --host-port=127.0.0.1:8787
```

In another PowerShell session:

```powershell
$env:FIRESTORE_EMULATOR_HOST = '127.0.0.1:8787'
uv run python scripts/shared_workflow_smoke.py --emulator-host 127.0.0.1:8787 --deadline-seconds 60
uv run pytest -q tests/test_shared_workflow_store.py tests/test_shared_workflow_http.py
```

The runner uses an isolated synthetic project/database/namespace with an explicitly anonymous emulator client; it never discovers ADC or reads real Google credentials. Refuse non-loopback emulator targets and a supplied private `PTA_WORKFLOW_CONFIG`. Headless Chromium is the fixed browser mode; runner exit 0 means smoke passed, nonzero means failed, with deterministic server/browser shutdown on exit or Ctrl+C. Stop the separately started emulator using Ctrl+C in its terminal; then remove the local variable with `Remove-Item Env:FIRESTORE_EMULATOR_HOST`. The private cloud environment file contains only `PTA_WORKFLOW_CONFIG` and cannot carry emulator/test settings. Base installations without `web` use optional-dependency `importorskip` guards; CI's new web job installs the extra and treats integration omissions as failures.

Preserve the existing Linux base-regression job's `dev` + `slides` installation without `web`: its optional web-test modules skip, while all existing tests except `test_treasurer_slides_bank_statements_native.py` run, followed by that file's `extractor_never_reads_a_pdf_if_the_sandbox_cannot_start` case. Keep the separate Windows native-sandbox job. Move the full-package `uv run mypy --strict pta_finance` gate into the new web-enabled job, which installs `dev` + `slides` + `web`, the emulator, and Chromium; it then runs strict mypy across the entire package plus the web HTTP/store/browser tests. Do not suppress or exclude new modules from typechecking, and do not install web into the old job without also provisioning its required emulator. Retain all Ruff/identity and existing regression gates; do not touch monthly report automation. Report exact commands and platform exclusions, not a fixed advance test count.

Planning verification performed: producing-code reads, complete project/workspace/step-authoring contracts, git/status/worktree checks, sibling-number scan, targeted caller searches, and official Google documentation checks. Technical review, redline publication, and plan-wrap are recorded complete. Step 32 implementation tests, independent reviews, integration, and feature CI now pass. M6/Step 33 subsequently passed actual cloud deployment and operator acceptance on 2026-09-12. The checked private record satisfies the Phase B entry gate. Step 34 implementation and M7/Step 35 acceptance remain pending; each phase emits its own attended UAT.

## Appendix — Decision record

### Decision Inventory

Publication 1, 2026-09-11. P records explicit operator choices; D records agent-selected implementation defaults, which stand under the accepted direction. D1–D10 retain their existing §6 identities. IDs are append-only: future changes update the row/status and never renumber or delete it. The proposal is a view; this plan remains authoritative. No additional approval is requested for these accepted choices.

| ID | P/D | choice | status |
|---|---|---|---|
| P1 | P | Keep the hosted proof separate from existing private ledger, bundle, and payment behavior. | Accepted in conversation. |
| P2 | P | Use one fictional request as the initial demonstration. | Accepted in conversation. |
| P3 | P | Use two independent Google sign-ins with shared comments and server-owned attribution. | Accepted in conversation. |
| P4 | P | Prove real authentication and persistence before implementing approval controls. | Accepted in conversation; enforced by separate Phase A and Phase B invocations. |
| P5 | P | Reviewer chooses approve/not approve with a free-text comment; processor receives the approved handoff and completes it. | Accepted in conversation; checkbox-style single choice is explicit in D7. |
| D1 | D | Add an optional Python web service beside the existing CLI, reusing strict fictional-bundle validation. | Stands under accepted direction. |
| D2 | D | Use direct IAP on Cloud Run with verified assertions and two privately pinned Google subjects. | Stands under accepted direction; real account/OAuth setup remains M6. |
| D3 | D | Use keyless runtime identity and a separate Firestore database/namespace with immutable source projection and events. | Stands under accepted direction. |
| D4 | D | Use transaction versions and idempotent receipts; cap a namespace at 100 events with a bounded full read. | Changed 2026-09-11: pagination removed during accepted review correction. |
| D5 | D | Serve one same-origin page and strict JSON routes with plain-text comments and explicit reload. | Stands under accepted direction. |
| D6 | D | Load one private JSON environment configuration; fail closed on unsafe startup or runtime inputs. | Stands under accepted direction; exact loading/errors are specified in §6. |
| D7 | D | Not approve is terminal; completion is administrative and does not record a payment. | Stands under accepted direction; no reopening or reassignment in this proof. |
| D8 | D | Stage an explicit source allowlist, use fixed Cloud Build/gcloud instructions, and inspect the actual image in M6. | Changed 2026-09-11: generic deployment reconciler removed; image gate moved after Cloud Build. |
| D9 | D | Separate local emulator/browser checks from real M6/M7 proof, with M6 required before Phase B. | Stands under accepted direction; local tests cannot satisfy cloud acceptance. |
| D10 | D | Keep project, account, billing, permissions, regions, and OAuth readiness as explicit private operator prerequisites. | Stands under accepted direction; no cloud readiness is asserted by plan approval. |
