# Shared workflow proof

The optional `web` service lets two configured Google accounts comment on one fictional
request. Modes are `identity`, `comments`, and `handoff`. In handoff mode the reviewer approves
or does not approve with a comment; only the processor can complete an approved handoff.
Completion is administrative and does not record a payment. No request import, reset, email,
Sheet write, or payment operation exists.

The local proof uses actual signed ES256 assertions, HTTP, Chromium, an installed wheel,
and Firestore emulator transactions. **M6 on Cloud Run passed on 2026-09-12.** Separate
deployed checks proved Google sign-in, cloud IAM, image inspection and revision persistence;
the operator accepted the proof. Exact identities, configuration and receipts remain private.
This is a functional proof, not an adoption claim. Handoff code has local automated coverage;
**M7 / Step 35 hosted acceptance remains pending.** Run the prepared M7 procedure below next.
The M6 procedure is retained for repeat execution.

## Local setup and verification

Use Python 3.12+, uv, the Google Cloud CLI with its `cloud-firestore-emulator` component,
and a compatible Java runtime. Java 25 is used in CI; the current emulator warns about its
older Java 21 floor. No Docker installation or Google authentication is needed locally.
Check tools with `uv --version`, `gcloud version`, and `java -version`. Install the emulator
with `gcloud components install cloud-firestore-emulator` if using a component-enabled SDK.
The runner fails if the emulator is unavailable, port 8788 is occupied, or Chromium is absent.

```powershell
uv sync --locked --extra dev --extra slides --extra web
uv run playwright install chromium
gcloud emulators firestore start --project=example-workflow-test --host-port=127.0.0.1:8787
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
```

Leave the emulator in that terminal. In a second terminal at the repository root:

```powershell
$env:FIRESTORE_EMULATOR_HOST = '127.0.0.1:8787'
uv run python scripts/shared_workflow_smoke.py --emulator-host 127.0.0.1:8787 --deadline-seconds 60
uv run pytest -q tests/test_shared_workflow_store.py tests/test_shared_workflow_http.py
uv run pytest -q
uv run mypy --strict pta_finance
uv run ruff check .
uv run ruff format --check .
uv build
uv run python scripts/check_no_identity.py
Remove-Item Env:FIRESTORE_EMULATOR_HOST
```

Stop the emulator with Ctrl+C in its original terminal. The smoke runner stops its own
server and browser on success, failure, or interruption, leaving the emulator running.
It builds a wheel into a temporary directory, installs it there, launches outside the
checkout, and uses synthetic project `example-workflow-test`, database `workflow-test`,
and a new namespace. After the comments smoke (at most 60 seconds after readiness), a separate
90-second handoff check upgrades that same namespace, proves approve/complete, and proves
not-approve in a fresh namespace; app restarts preserve both. It rejects non-loopback emulator targets and any supplied private
`PTA_WORKFLOW_CONFIG`; its anonymous gRPC transport never discovers ADC.

The full suite needs Windows for the native statement-parser checks. Existing Linux CI
keeps `dev` + `slides`, excludes the native parser file except its fail-closed case, and
retains the separate Windows native job. A new isolated web CI job supplies the emulator,
Chromium, strict full-package mypy, and all web tests. Missing optional web dependencies
skip web modules in the base job; installed web dependencies with no emulator fail collection.

Firestore's emulator can hold simplified concurrent-write locks for up to 30 seconds.
The application still caps each database call at 5 seconds, a mutation at 20 seconds,
and contention at three attempts. Tests require safe bounded race results and exact-operation
retries to resolve to one durable event and one stale operation. Real cloud contention remains
a separate acceptance observation. [Emulator limitations](https://docs.cloud.google.com/firestore/native/docs/emulator#transactions)

## Configuration and behavior

Copy `deployment/shared-workflow/runtime.example.json` to ignored
`secrets/shared-workflow.runtime.json`, then edit actual values privately. The example's
project, users, and region are fictional examples, not deployment defaults.
Production reads only the required `PTA_WORKFLOW_CONFIG` JSON and optional numeric `PORT`.
It accepts no config path, source path, local login, or trust switch. It rejects emulator
variables, test overrides, and `GOOGLE_APPLICATION_CREDENTIALS`. Runtime access uses the
attached service account without downloaded keys.

`identity` mode requires exactly one reviewer and one processor email, distinct after
`strip().casefold()`. It verifies IAP signatures, exposes only the caller's subject/email,
and never connects to Firestore. A subject already present in identity mode is informational;
the configured signed email remains the discovery authorization. The operator privately
binds both distinct observed subjects before enabling `comments`. There is no first-visitor
enrollment. Disabled entries remain structurally present and are denied.

For `comments` or `handoff`, supply both pinned subjects, the exact HTTPS service origin from deployment
metadata (no trailing slash, explicit default port, path, query, or fragment), a named database,
and namespace `proof_` followed by a lowercase UUIDv4 generated once. Do not reuse a namespace
to reset a proof. Restarting with the same source/database/namespace preserves history.
Startup refuses schema/source mismatch, missing resources, unsafe settings, and unavailable
storage; it never falls back to a local file or anonymous production identity.

Optional production-entry startup check, without printing private configuration:

```powershell
$env:PTA_WORKFLOW_CONFIG = [IO.File]::ReadAllText((Resolve-Path -LiteralPath 'secrets/shared-workflow.runtime.json'))
uv run python -m pta_finance.shared_workflow
```

Stop with Ctrl+C, then `Remove-Item Env:PTA_WORKFLOW_CONFIG`. Protected routes still require
real signed IAP assertions; this command is not an alternate login.

| Route | Contract |
|---|---|
| `GET /healthz` | Only application auth exemption; `{status: "ok"}`. IAP remains enabled at the platform. |
| `GET /` and `/static/request.js`, `/static/request.css` | Verified user; same-origin page/resources with no-store and CSP. |
| `GET /api/me` | `{mode, actor: {subject, email, label, role}, request_id}`; request ID is null in identity mode. |
| `GET /api/requests/{request_id}` | `{request, events, event_cap: 100}`; complete ordered history at request.version. |
| `POST /api/requests/{request_id}/comments` | Both roles, any state; exactly `{operation_id, expected_version, body}`; success `{receipt: Event}`. |
| `POST /api/requests/{request_id}/decision` | Handoff mode, reviewer only; exactly `{operation_id, expected_version, decision, body}`. Decision is `approve` or `not_approve`; a nonblank comment is required. |
| `POST /api/requests/{request_id}/complete` | Handoff mode, processor only; exactly `{operation_id, expected_version, body}`. Body may be blank; success `{receipt: Event}`. |

All data routes authenticate before accessing the request. Only the configured request ID
(SHA-256 of its review key) is accepted; clients cannot choose a namespace. Unknown IDs return
404. Query parameters are rejected. Decision/completion routes are absent in identity/comments modes.

Mutations require exact configured `Origin`, `Content-Type: application/json`,
`X-PTA-CSRF: 1`, and any supplied `Sec-Fetch-Site` must be `same-origin`. No CORS permissions
are supplied. JSON rejects extra/duplicate keys, malformed Unicode and non-finite numbers;
bodies are at most 8 KiB. Comments contain 1–2,000 characters and cannot be whitespace only.
Text is stored exactly and rendered through autoescaping/textContent.

Every event records server-owned subject/role/label, the immutable source digest, operation ID,
canonical payload hash, previous/result state, version, next owner, and server timestamp.
Money is a two-decimal string; API timestamps are UTC RFC3339 with six fractional digits and Z.
Comments preserve the current state and owner in every state. This workflow state does not use
private reimbursement recommendations or payment status.

| State | Allowed transition | Result / next owner |
|---|---|---|
| `AWAITING_REVIEW` | Reviewer approves with comment | `APPROVED` / processor |
| `AWAITING_REVIEW` | Reviewer does not approve with reason | `NOT_APPROVED` / null |
| `APPROVED` | Processor completes the handoff | `COMPLETED` / null |

Not-approved and completed outcomes are final. New invalid transitions return
`409 INVALID_TRANSITION`; direct wrong-role requests return `403 FORBIDDEN`. Role controls
never substitute for server authorization. Checkbox-shaped native radios form one accessible
review outcome group, initially unselected. Both participants can still comment after a final
outcome. The initial schema/version remains 1/0; existing M6 comments need no migration.

The client creates a UUIDv4 operation ID and expected version. An identical retry by the same
authorized subject returns its original receipt, including timestamp, before cap/state/version checks.
Reusing the ID with different data/subject returns `409 OPERATION_CONFLICT`. A new operation
at version 100 returns `409 EVENT_CAP_REACHED`; identical retries still work. History is
append-only through this API; database administrators can still alter it.

After a save, reload current state. On a lost response or `503 TEMPORARILY_UNAVAILABLE` /
`409 RETRY_CONFLICT`, keep the same operation ID and exact payload for Retry. A changed draft
gets a new ID. `409 STALE_VERSION` reloads and requires deliberate resubmission. A login HTML
response offers top-level sign-in refresh and never reports a successful save. Drafts are held
only in memory; a full navigation can discard them, so reload history before recreating a comment.

Errors use `{error: {code, message, correlation_id}}`, fixed safe prose, and a new UUIDv4.
Invalid/missing assertions return 401; disallowed identities/CSRF return 403; invalid inputs 400;
oversize bodies 413; unexpected failures 500; unavailable/inconsistent storage 503. Logs contain
only generic route, result code, duration and correlation ID. They do not contain tokens,
cookies, comments, identities, configuration, or raw database exceptions.
Startup rejects `GOOGLE_SDK_PYTHON_LOGGING_SCOPE`, `GRPC_TRACE`, and `GRPC_VERBOSITY`
with `UNSAFE_RUNTIME_ENV` before constructing the database client, including empty values.
These SDK/native logging overrides can bypass the application's log-level policy.

## M6: ordered cloud procedure

This procedure performs cloud mutations only when the operator runs the explicit commands.
Keep every actual project/account identifier, policy receipt, browser screenshot, and acceptance
record under ignored `secrets/` or `reports/output/shared-workflow/`. Do not paste them into
issues or public documentation. Never use a raw checkout as a Cloud Build source.

### 1. Read-only readiness

Privately set `$PtaProject`, `$PtaRegion` (Cloud Run), `$PtaBuildRegion`, `$PtaDatabaseLocation`,
`$PtaRegistryLocation`, `$PtaService`, `$PtaDatabase` (suggested dedicated name `workflow-proof`),
`$PtaRepository`, `$PtaSourceBucket`, `$PtaRuntimeAccount`, `$PtaBuildAccount`,
`$PtaReviewerEmail`, and `$PtaProcessorEmail`. Select dedicated proof resource names, not
unrelated existing resources. Supply locations explicitly; make no billing/free-tier assumption.
Create the private evidence directory if needed, then run:

```powershell
New-Item -ItemType Directory -Force -Path reports/output/shared-workflow | Out-Null
gcloud auth list --filter='status:ACTIVE' --format='value(account)' > reports/output/shared-workflow/operator-account.txt
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud projects describe $PtaProject --format=json > reports/output/shared-workflow/project.json
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud projects get-ancestors $PtaProject --format=json > reports/output/shared-workflow/ancestry.json
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud billing projects describe $PtaProject --format=json > reports/output/shared-workflow/billing.json
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud services list --enabled --project=$PtaProject --format=json > reports/output/shared-workflow/apis.json
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud projects get-iam-policy $PtaProject --format=json > reports/output/shared-workflow/project-iam.json
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud resource-manager org-policies describe iam.allowedPolicyMemberDomains --project=$PtaProject --effective --format=json > reports/output/shared-workflow/external-principal-policy.json
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
```

Inspect these privately and record the actual project number as `$PtaProjectNumber`.
Check the candidate Cloud Run service before any upsert deployment:

```powershell
gcloud run services list --project=$PtaProject --region=$PtaRegion --filter="metadata.name=$PtaService" --format='json(metadata.name,metadata.labels,status.latestReadyRevisionName,spec.template.spec.containers.image)' > reports/output/shared-workflow/candidate-service.json
if ($LASTEXITCODE -ne 0) { throw 'Service ownership could not be checked; stop before deployment.' }
```

An empty successful list permits creation of that new service. If a service is present,
privately verify that it is this proof's intended resource and record its current image and
revision before proceeding. An inaccessible, unknown, or unrelated service is a stop condition;
never overwrite it with `gcloud run deploy`.

Check applicable organization/folder policies and inherited IAM, not just project bindings.
Authentication alone does not prove permission to provision resources, enable APIs, attach
runtime/build service accounts, submit builds, deploy Cloud Run, or configure IAP/OAuth.
A denied metadata read means unknown; stop the affected setup action until access is resolved.
Do not broaden grants or substitute public invocation to work around a denial.

### 2. Explicit dedicated resource setup

After confirming intended names, ownership and permissions:

```powershell
gcloud services enable run.googleapis.com iap.googleapis.com firestore.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com iam.googleapis.com logging.googleapis.com storage.googleapis.com --project=$PtaProject
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud iam service-accounts create $PtaRuntimeAccount --project=$PtaProject --display-name='Shared workflow runtime'
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud iam service-accounts create $PtaBuildAccount --project=$PtaProject --display-name='Shared workflow builder'
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
$PtaRuntimeIdentity = "$PtaRuntimeAccount@$PtaProject.iam.gserviceaccount.com"
$PtaBuildEmail = "$PtaBuildAccount@$PtaProject.iam.gserviceaccount.com"
$PtaBuildIdentity = "projects/$PtaProject/serviceAccounts/$PtaBuildEmail"
gcloud firestore databases create --project=$PtaProject --database=$PtaDatabase --location=$PtaDatabaseLocation --type=firestore-native
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud artifacts repositories create $PtaRepository --project=$PtaProject --location=$PtaRegistryLocation --repository-format=docker
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud storage buckets create "gs://$PtaSourceBucket" --project=$PtaProject --location=$PtaBuildRegion --uniform-bucket-level-access
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
$PtaDatabaseCondition = "expression=resource.name=='projects/$PtaProject/databases/$PtaDatabase',title=workflow-proof-database"
gcloud projects add-iam-policy-binding $PtaProject --member="serviceAccount:$PtaRuntimeIdentity" --role=roles/datastore.user --condition=$PtaDatabaseCondition
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud artifacts repositories add-iam-policy-binding $PtaRepository --project=$PtaProject --location=$PtaRegistryLocation --member="serviceAccount:$PtaBuildEmail" --role=roles/artifactregistry.writer
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud projects add-iam-policy-binding $PtaProject --member="serviceAccount:$PtaBuildEmail" --role=roles/logging.logWriter --condition=None
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud storage buckets add-iam-policy-binding "gs://$PtaSourceBucket" --member="serviceAccount:$PtaBuildEmail" --role=roles/storage.objectViewer
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud beta services identity create --project=$PtaProject --service=iap.googleapis.com
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
```

Ensure the deployment identity already has `iam.serviceAccounts.actAs` on these two dedicated
accounts and the required deployment/build permissions. These commands do not grant them.
Inspect effective inherited access before accepting database isolation. The database condition
is enforced for SDK/API access; Console browsing does not demonstrate that boundary.
Neither browser user receives database IAM. Keep mobile/web client rules denied; verify the
new database's rules in its Google/Firebase Console before acceptance and publish deny-all
rules there if necessary (`allow read, write: if false;`). The server uses IAM, not client rules.
[Per-database IAM](https://docs.cloud.google.com/firestore/native/docs/manage-databases#configure_per-database_access_permissions)

The dedicated builder has repository write, source-bucket read and log write; runtime has
only the database-conditioned data grant. Do not create service-account keys. The recipe's
`CLOUD_LOGGING_ONLY` setting is required with the explicit build service account.
[Custom Cloud Build identities](https://docs.cloud.google.com/build/docs/securing-builds/configure-user-specified-service-accounts)

### 3. Stage, build and inspect the actual image

Choose a new `$PtaStage` under ignored `reports/output/shared-workflow/`, and unique tag
`$PtaImageTag` such as `LOCATION-docker.pkg.dev/PROJECT/REPOSITORY/proof:UNIQUE_TAG`, using
the actual private selections. Do not print private variable values in a public transcript.

```powershell
uv run python scripts/stage_shared_workflow.py --output $PtaStage
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud builds submit $PtaStage --project=$PtaProject --region=$PtaBuildRegion --service-account=$PtaBuildIdentity --gcs-source-staging-dir="gs://$PtaSourceBucket/source" --config="$PtaStage/cloudbuild.yaml" --substitutions="_IMAGE=$PtaImageTag" --format=json > reports/output/shared-workflow/build-submit.json
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
```

The staging command refuses an existing output, path traversal, symlinks/reparse points,
and source/hash/inventory drift. Its `source-content-manifest.json` is the upload receipt.
The image is built from only those files, installs locked dependencies/resources, runs as
UID 10001, and stores no durable data locally. Cloud Build then runs the actual image with
network disabled and a read-only filesystem, checks installed application paths/hashes,
environment/credential/canary exclusions, and loads the strict packaged request. Publication
occurs only after that inspection passes. **Local source/wheel tests do not satisfy this gate.**

Privately set `$PtaBuildId` from the successful receipt, retain build logs, and read the digest:

```powershell
gcloud builds describe $PtaBuildId --project=$PtaProject --region=$PtaBuildRegion --format=json > reports/output/shared-workflow/build-receipt.json
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
$PtaBuilt = [IO.File]::ReadAllText((Resolve-Path reports/output/shared-workflow/build-receipt.json)) | ConvertFrom-Json
if ($PtaBuilt.status -ne 'SUCCESS') { throw 'Build or image inspection did not pass.' }
if ($PtaBuilt.results.images.Count -ne 1 -or $PtaBuilt.results.images[0].name -ne $PtaImageTag) { throw 'The build image does not match the requested tag.' }
$PtaDigest = $PtaBuilt.results.images[0].digest
if ($PtaDigest -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Missing immutable image digest.' }
$PtaImageDigest = ($PtaImageTag -replace ':[^/:]+$', '') + '@' + $PtaDigest
```

Inspect the build's `inspect-actual-image` log for `IMAGE INSPECTION PASS` and preserve its
result before deploying. Record the build ID, source receipt, actual image digest and result.

### 4. Identity-mode deployment and IAP admission

Fill the private runtime JSON initially with `mode: identity`, actual project/number/region,
service and two email addresses; leave origin, subjects, database and namespace null.
Generate the private environment file without printing it:

```powershell
$PtaRuntimeJson = [IO.File]::ReadAllText((Resolve-Path -LiteralPath 'secrets/shared-workflow.runtime.json'))
$PtaEnvJson = @{ PTA_WORKFLOW_CONFIG = $PtaRuntimeJson } | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path (Resolve-Path -LiteralPath 'secrets') 'shared-workflow.env.json'), $PtaEnvJson, [Text.UTF8Encoding]::new($false))
$PtaRevision = 'proof-' + [Guid]::NewGuid().ToString('N').Substring(0,12)
gcloud run deploy $PtaService --project=$PtaProject --region=$PtaRegion --image=$PtaImageDigest --service-account=$PtaRuntimeIdentity --env-vars-file=secrets/shared-workflow.env.json --no-allow-unauthenticated --iap --revision-suffix=$PtaRevision --cpu=1 --memory=512Mi --min-instances=0 --max-instances=2 --concurrency=20 --timeout=30s
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
$PtaIapAgent = "service-$PtaProjectNumber@gcp-sa-iap.iam.gserviceaccount.com"
gcloud run services add-iam-policy-binding $PtaService --project=$PtaProject --region=$PtaRegion --member="serviceAccount:$PtaIapAgent" --role=roles/run.invoker
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud iap web add-iam-policy-binding --project=$PtaProject --region=$PtaRegion --resource-type=cloud-run --service=$PtaService --member="user:$PtaReviewerEmail" --role=roles/iap.httpsResourceAccessor
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud iap web add-iam-policy-binding --project=$PtaProject --region=$PtaRegion --resource-type=cloud-run --service=$PtaService --member="user:$PtaProcessorEmail" --role=roles/iap.httpsResourceAccessor
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
```

External users or projects without an organization need suitable custom OAuth configuration.
Complete the one-time IAP OAuth setup in the Cloud Run Security/IAP Console and configure its
audience/client for these accounts. Keep client secrets exclusively in IAP configuration;
never reuse the toolkit's Gmail OAuth client. If organization policy or OAuth setup blocks an
account, keep M6 pending. Do not disable IAP or grant `allUsers`, `allAuthenticatedUsers`, or
direct browser Cloud Run invoker access. No load balancer is needed.
[Direct IAP setup](https://docs.cloud.google.com/run/docs/securing/identity-aware-proxy-cloud-run)

Collect only filtered service metadata, plus private policy receipts:

```powershell
gcloud run services describe $PtaService --project=$PtaProject --region=$PtaRegion --format='json(status.url,status.latestReadyRevisionName,spec.template.spec.serviceAccountName,spec.template.spec.containers.image)' > reports/output/shared-workflow/service-receipt.json
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud run services describe $PtaService --project=$PtaProject --region=$PtaRegion --format='value(metadata.annotations[run.googleapis.com/iap-enabled])' > reports/output/shared-workflow/iap-enabled.txt
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud run services get-iam-policy $PtaService --project=$PtaProject --region=$PtaRegion --format=json > reports/output/shared-workflow/run-iam.json
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud iap web get-iam-policy --project=$PtaProject --region=$PtaRegion --resource-type=cloud-run --service=$PtaService --format=json > reports/output/shared-workflow/iap-iam.json
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud firestore databases describe --project=$PtaProject --database=$PtaDatabase --format=json > reports/output/shared-workflow/database-receipt.json
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
```

Verify IAP enabled, private invocation, intended runtime identity/image and exact two-user
service admission. Inspect inherited grants separately; a service policy alone is insufficient.
Open the service in **two isolated browser profiles signed into distinct Google subjects**.
Both `/api/me` results and identity pages must reveal only the caller. Privately record each
subject and role. Two account aliases or a UI role switch do not qualify.

### 5. Pin identities, prove comments, and force a fresh revision

Privately edit the runtime JSON: `mode: comments`, distinct observed subjects, canonical
`status.url` as origin, dedicated database name, and one new namespace:
`'proof_' + [Guid]::NewGuid().ToString()`. Record it once. Regenerate the environment file,
generate a fresh revision suffix, and repeat the exact deploy/filtered-verification commands
from step 4 using the same inspected image digest. No source edit is needed.

Within 60 seconds after data-mode readiness: reviewer loads the sample and posts one unique
fictional comment; processor reloads and reads it. Processor then comments and reviewer reloads.
Check server labels, distinct subjects, native timestamp encodings and sequential versions.
Save private observations. Deploy the same image/config again with another fresh suffix,
then prove both profiles still read the same two events from the same database/namespace.
Stop the local development service and demonstrate the hosted flow still works.

Use the browser's developer console for direct API denial/retry checks while signed in.
This snippet derives only the authenticated request ID and keeps the operation for replay:

```javascript
const me = await (await fetch('/api/me')).json();
const path = `/api/requests/${me.request_id}`;
const current = await (await fetch(path)).json();
const operation = {operation_id: crypto.randomUUID(), expected_version: current.request.version,
  body: 'Fictional acceptance comment'};
const send = async (data, headers = {'Content-Type':'application/json','X-PTA-CSRF':'1'}) => {
  const response = await fetch(path + '/comments', {method:'POST', headers, body:JSON.stringify(data)});
  return {status: response.status, data: await response.json()};
};
await send({...operation, actor_role:'processor'}); // 400, no event
await send({...operation, actor_sub:'example-processor'}); // 400, no event
await send(operation, {'Content-Type':'application/json'}); // 403, no event
await send(operation, {'Content-Type':'application/json','X-PTA-CSRF':'0'}); // 403, no event
const saved = await send(operation); // 200, exactly one event
const replay = await send(operation); // 200, identical original receipt
await send({...operation, body:'Changed payload'}); // 409 OPERATION_CONFLICT
await send({...operation, operation_id:crypto.randomUUID()}); // 409 STALE_VERSION
await (await fetch(path + '?limit=1')).json(); // INVALID_INPUT
```

Browsers control `Origin`; ordinary `fetch` cannot override it. In an authenticated Firefox
profile, open Developer Tools → Network, submit a fictional comment, right-click that POST,
and choose **Edit and Resend**. Retain its authentication cookies and `X-PTA-CSRF: 1`, remove
the `Origin` header and send; require 403. Edit the same captured POST again, set
`Origin: https://other.example.org`, and require 403. Never copy cookies/assertions into a
terminal, document, issue, or public transcript. Inspect each sent request's Headers panel
to verify that Origin was actually removed or replaced as intended.
[Firefox Edit and Resend](https://firefox-source-docs.mozilla.org/devtools-user/network_monitor/request_list/)

Replaying the captured operation is safe:
CSRF checks precede its existing-operation lookup. Read the request before and after all
denial cases and require identical version/history. If the selected browser cannot actually
send both edited requests with the intended headers, **M6 remains pending** until those
mandatory checks are executed; local coverage does not substitute for them.
For signed-assertion injection use Google's documented IAP testing facility where available;
IAP strips supplied client identity headers, so ordinary forged headers are not a platform
token-injection test. Record unsupported cloud cases as local coverage, not cloud passes.
[Signed-header verification/testing](https://docs.cloud.google.com/iap/docs/signed-headers-howto)

Open a signed-out profile: Google sign-in/access denial must expose no request data. Then
temporarily set processor `enabled: false` in private runtime JSON while preserving IAP
admission, regenerate env and redeploy with a fresh suffix. That signed-in processor must
get 403 from `/api/me`, page and request/comment routes. Restore the entry and redeploy again.
Rejected operations must add no events. Do not invite a third account for this test.

### 6. Acceptance record and access shutdown

Create ignored `reports/output/shared-workflow/m6-acceptance.md` from this template. Fill the
operational values privately, and mark each row PASS, FAIL or PENDING with observation/time.
Do not mark Step 33 DONE until all required observations are verified. Image inspection and
cloud observations cannot be replaced by local test receipts.

```markdown
# M6 private acceptance
Date/operator:
Project / Cloud Run region / build region / database location:
Service / database / namespace / runtime identity / build identity:
Staged source-content-manifest receipt:
Cloud Build ID / actual image-inspection log / immutable image digest:
Identity revision / comments revision / durability revision / disabled and restored revisions:
Reviewer verified subject and email (private):
Processor verified subject and email (private):
Distinct subjects confirmed:

| Observation | PASS / FAIL / PENDING | Evidence and timestamp |
|---|---|---|
| Actual image inspection before deployment | PENDING | |
| Two independent Google sessions; identity mode contains no workflow data | PENDING | |
| First shared comment read within 60 seconds; reciprocal second comment | PENDING | |
| Correct server actors, timestamps and sequential versions | PENDING | |
| Fresh cloud revision retains the same comments/database/namespace | PENDING | |
| Signed-out and disabled-roster denial; roster restored | PENDING | |
| Actor/role spoofing, CSRF, malformed/invalid requests create no events | PENDING | |
| IAP/private invocation/two-user policies/runtime and database boundary | PENDING | |
| Exact retry receipt, changed-ID payload rejection, stale-version rejection | PENDING | |
| Safe transient/session errors; supported signed-token injection observations | PENDING | |
| Hosted operation works with local service stopped | PENDING | |
| Both users identify the service/store/role configuration/repeat procedure | PENDING | |

Unsupported cloud injection checks and corresponding local-only evidence:
Functional proof verdict (not an adoption claim):
Retention owner and intended evidence retention:
Next: verified M6 / Step 33 DONE before separately invoking Phase B.
```

To close access while retaining evidence, remove only this proof's service-scoped admissions
and IAP invocation grant. Confirm no inherited admission remains. Keep the service private:

```powershell
gcloud iap web remove-iam-policy-binding --project=$PtaProject --region=$PtaRegion --resource-type=cloud-run --service=$PtaService --member="user:$PtaReviewerEmail" --role=roles/iap.httpsResourceAccessor
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud iap web remove-iam-policy-binding --project=$PtaProject --region=$PtaRegion --resource-type=cloud-run --service=$PtaService --member="user:$PtaProcessorEmail" --role=roles/iap.httpsResourceAccessor
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
gcloud run services remove-iam-policy-binding $PtaService --project=$PtaProject --region=$PtaRegion --member="serviceAccount:$PtaIapAgent" --role=roles/run.invoker
if ($LASTEXITCODE -ne 0) { throw 'The previous command failed; stop this procedure.' }
```

Database/image/source retention and any resource deletion are separate operator decisions.
Minimum-zero scaling does not guarantee zero cost. M7 below preserves existing evidence and
uses fresh namespaces for alternative outcomes. Never reset or delete a workflow document.


## M7: accept the deployed handoff (Step 35)

Run this attended procedure only after Step 34's code gate passes. M6 is already accepted;
its namespace, pinned subjects, dedicated identities/database, and IAP boundary remain the
starting point. Use the same two isolated real Google profiles. Local test identities and
emulator receipts cannot satisfy these rows. Use fictional text only and retain all actual
metadata, browser observations, and receipts under ignored `reports/output/shared-workflow/`.
No source edits or database document edits are needed.

### 1. Build the handoff image and preserve the original configuration

Keep the M6 receipt files intact. Create a new private M7 evidence directory, stage path and
unique image tag. Repeat **M6 section 3: Stage, build and inspect the actual image**, directing
its `build-submit.json` and `build-receipt.json` redirects into the new M7 evidence directory.
The source allowlist, fixed Cloud Build recipe, source manifest, actual image-inspection log,
and immutable digest checks are mandatory again. Use that section's checked `$PtaImageDigest`;
never use an unchecked tag or M6's old comments-only image.

Before changing runtime values, privately preserve the existing file with an exclusive copy
(the destination must not exist). No secrets are printed:

```powershell
[IO.File]::Copy((Resolve-Path -LiteralPath 'secrets/shared-workflow.runtime.json'), (Join-Path (Resolve-Path -LiteralPath 'secrets') 'shared-workflow.m7-original.runtime.json'), $false)
```

Privately change only `mode` to `handoff`. Keep the M6 namespace, both pinned subjects, database,
origin, service, project and region. Verify those against the private M6 record. Before deployment,
each profile saves the current bounded request/history JSON privately as the upgrade baseline.

### 2. Deploy and verify every M7 revision

Use this block for the initial handoff, durability, alternative-branch and restore revisions.
For each run choose a new private `$PtaM7Receipt` path for filtered metadata, regenerate the
environment file from the current private runtime JSON, and use a fresh suffix:

```powershell
$PtaRuntimeJson = [IO.File]::ReadAllText((Resolve-Path -LiteralPath 'secrets/shared-workflow.runtime.json'))
$PtaEnvJson = @{ PTA_WORKFLOW_CONFIG = $PtaRuntimeJson } | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path (Resolve-Path -LiteralPath 'secrets') 'shared-workflow.env.json'), $PtaEnvJson, [Text.UTF8Encoding]::new($false))
$PtaRevision = 'proof-' + [Guid]::NewGuid().ToString('N').Substring(0,12)
gcloud run deploy $PtaService --project=$PtaProject --region=$PtaRegion --image=$PtaImageDigest --service-account=$PtaRuntimeIdentity --env-vars-file=secrets/shared-workflow.env.json --no-allow-unauthenticated --iap --revision-suffix=$PtaRevision --cpu=1 --memory=512Mi --min-instances=0 --max-instances=2 --concurrency=20 --timeout=30s
if ($LASTEXITCODE -ne 0) { throw 'Deployment failed; stop M7.' }
gcloud run services describe $PtaService --project=$PtaProject --region=$PtaRegion --format='json(status.url,status.latestReadyRevisionName,spec.template.spec.serviceAccountName,spec.template.spec.containers.image)' > $PtaM7Receipt
if ($LASTEXITCODE -ne 0) { throw 'Revision verification failed; stop M7.' }
gcloud run services describe $PtaService --project=$PtaProject --region=$PtaRegion --format='value(metadata.annotations[run.googleapis.com/iap-enabled])'
if ($LASTEXITCODE -ne 0) { throw 'IAP verification failed; stop M7.' }
gcloud run services get-iam-policy $PtaService --project=$PtaProject --region=$PtaRegion
if ($LASTEXITCODE -ne 0) { throw 'Invocation policy verification failed; stop M7.' }
gcloud iap web get-iam-policy --project=$PtaProject --region=$PtaRegion --resource-type=cloud-run --service=$PtaService
if ($LASTEXITCODE -ne 0) { throw 'IAP policy verification failed; stop M7.' }
```

Privately verify each changed ready revision, checked image digest, intended runtime identity,
IAP enabled and private invocation. Recheck the exact two-user admission and inherited grants
as in M6. Both profiles fully refresh the page after a mode/namespace deployment, then inspect
`/api/me`: mode is `handoff`, their subjects remain distinct and their roles remain correct.
The first handoff revision must return the exact baseline comments, timestamps and versions.

### 3. Authenticated browser helpers and approved handoff

Run these helpers separately in each signed-in profile's developer console. They use the
browser's current session and same-origin headers; never copy authentication cookies or IAP
assertions into another tool. Do not publish console results containing subjects or cloud IDs.
Rerun the helpers after each namespace change. `wf.check` throws on a failed observation;
stop that row and investigate instead of continuing through a failed assertion.

```javascript
var wf = {};
wf.check = (condition, message) => { if (!condition) throw new Error(message); };
wf.sort = value => Array.isArray(value) ? value.map(wf.sort) :
  value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(k => [k, wf.sort(value[k])])) : value;
wf.same = (a, b) => JSON.stringify(wf.sort(a)) === JSON.stringify(wf.sort(b));
wf.get = async path => {
  const response = await fetch(path, {cache:'no-store', credentials:'same-origin'});
  wf.check(!response.redirected && (response.headers.get('content-type') || '').includes('application/json'), 'Refresh sign-in and rerun this read');
  const data = await response.json();
  wf.check(response.ok, 'Read failed; record status and safe error');
  return data;
};
wf.me = await wf.get('/api/me');
wf.check(wf.me.mode === 'handoff', 'Wrong deployed mode');
wf.path = `/api/requests/${wf.me.request_id}`;
wf.read = async () => {
  const data = await wf.get(wf.path);
  wf.check(data.event_cap === 100 && data.events.length === data.request.version, 'Incomplete bounded history');
  wf.check(data.events.every((e,i) => e.result_version === i+1 && e.expected_version === i), 'History version ordering failed');
  return data;
};
wf.post = async (route, operation) => {
  const response = await fetch(wf.path + '/' + route, {method:'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json','X-PTA-CSRF':'1'}, body:JSON.stringify(operation)});
  wf.check(!response.redirected && (response.headers.get('content-type') || '').includes('application/json'), 'Uncertain save: preserve operation, refresh sign-in, then retry the same operation');
  return {status:response.status, data:await response.json()};
};
wf.operation = (version, body = 'Fictional M7 observation') =>
  ({operation_id:crypto.randomUUID(), expected_version:version, body});
wf.fromReceipt = receipt => ({operation_id:receipt.operation_id, expected_version:receipt.expected_version,
  body:receipt.body, ...(['approve','not_approve'].includes(receipt.action) ? {decision:receipt.action} : {})});
wf.denied = async (route, operation, status, code) => {
  const before = await wf.read();
  const result = await wf.post(route, operation);
  wf.check(result.status === status && result.data.error.code === code, 'Unexpected denial');
  wf.check(wf.same(before, await wf.read()), 'Rejected operation changed history');
  return result;
};
wf.baseline = await wf.read();
```

Before any decision, reviewer runs:

```javascript
wf.check(wf.me.actor.role === 'reviewer', 'Use the reviewer profile');
await wf.denied('complete', wf.operation(wf.baseline.request.version), 403, 'FORBIDDEN');
```

Processor independently runs:

```javascript
wf.check(wf.me.actor.role === 'processor', 'Use the processor profile');
await wf.denied('decision', {...wf.operation(wf.baseline.request.version), decision:'approve'}, 403, 'FORBIDDEN');
await wf.denied('complete', wf.operation(wf.baseline.request.version), 409, 'INVALID_TRANSITION');
```

Reviewer uses the page: neither outcome is initially selected; keyboard focus reaches the
single-choice outcome group, comment label, and Save decision. Select **Approve**, enter a
fictional comment, and save once. Processor clicks **Reload**, sees `APPROVED`, processor
ownership, the reviewer's exact comment and actor/time, and the completion control. Reviewer
reloads and sees that the processor owns the next action. Record both views privately.

In the reviewer console, preserve the actual UI receipt and exercise duplicate/stale/conflict
checks. These new rejected operations must add no events:

```javascript
wf.afterDecision = await wf.read();
wf.approval = wf.afterDecision.events.find(e => e.action === 'approve');
wf.check(wf.approval && wf.approval.actor_sub === wf.me.actor.subject, 'Missing attributed approval');
wf.approvalOperation = wf.fromReceipt(wf.approval);
wf.replay = await wf.post('decision', wf.approvalOperation);
wf.check(wf.replay.status === 200 && wf.same(wf.replay.data.receipt, wf.approval), 'Retry changed original receipt');
wf.check(wf.same(wf.afterDecision, await wf.read()), 'Retry added an event');
await wf.denied('decision', {...wf.approvalOperation, body:'Changed fictional reason'}, 409, 'OPERATION_CONFLICT');
await wf.denied('decision', {...wf.approvalOperation, decision:'not_approve'}, 409, 'OPERATION_CONFLICT');
await wf.denied('decision', {...wf.approvalOperation, operation_id:crypto.randomUUID()}, 409, 'STALE_VERSION');
await wf.denied('decision', {...wf.operation(wf.afterDecision.request.version), decision:'not_approve'}, 409, 'INVALID_TRANSITION');
```

Processor uses **Mark workflow handoff complete**, optionally adding a fictional comment.
Both profiles reload and see `COMPLETED`, no next owner, and text explicitly stating that
completion does not record a payment. In the processor console:

```javascript
wf.completed = await wf.read();
wf.completion = wf.completed.events.find(e => e.action === 'complete');
wf.check(wf.completed.request.state === 'COMPLETED' && wf.completed.request.next_owner_role === null, 'Completion state/owner failed');
wf.check(wf.completion.actor_sub === wf.me.actor.subject, 'Wrong completion actor');
wf.completionOperation = wf.fromReceipt(wf.completion);
wf.replay = await wf.post('complete', wf.completionOperation);
wf.check(wf.replay.status === 200 && wf.same(wf.replay.data.receipt, wf.completion), 'Completion retry changed receipt');
await wf.denied('complete', wf.operation(wf.completed.request.version, ''), 409, 'INVALID_TRANSITION');
```

Each role adds one fictional comment after completion, taking turns and reloading first.
Both must read those attributed comments without any state/owner change. Save the complete
JSON privately, redeploy the same image/config through M7 section 2 with a new suffix, then
prove the exact final history, original M6 comments and completion remain. Privately preserve
this handoff runtime file as `secrets/shared-workflow.m7-approved.runtime.json` using the same
exclusive-copy pattern as section 1. Keep the original and approved namespace receipts.

### 4. Not-approved branch in a fresh namespace

Privately change only `namespace` to `'proof_' + [Guid]::NewGuid().ToString()` in the handoff
runtime JSON, record it, regenerate env and deploy with M7 section 2. Do not alter the earlier
request or delete its events. Same image, same two accounts, same packaged fictional request.
Both profiles fully refresh and rerun the browser helpers; require version 0 and empty history.

Reviewer selects **Not approve** with a fictional reason in the page. Processor reloads and
sees `NOT_APPROVED`, no next owner, and no completion control. Reviewer sees a final outcome
with no decision control. In the reviewer console:

```javascript
wf.rejected = await wf.read();
wf.check(wf.rejected.request.state === 'NOT_APPROVED' && wf.rejected.request.next_owner_role === null, 'Not-approved state/owner failed');
wf.rejection = wf.rejected.events.find(e => e.action === 'not_approve');
wf.replay = await wf.post('decision', wf.fromReceipt(wf.rejection));
wf.check(wf.replay.status === 200 && wf.same(wf.replay.data.receipt, wf.rejection), 'Not-approve retry changed receipt');
await wf.denied('decision', {...wf.operation(wf.rejected.request.version), decision:'approve'}, 409, 'INVALID_TRANSITION');
```

Processor runs `await wf.denied('complete', wf.operation((await wf.read()).request.version, ''),
409, 'INVALID_TRANSITION');`. Both roles may still comment; state and owner remain unchanged.
Save the final JSON and privately preserve this runtime file as
`secrets/shared-workflow.m7-not-approved.runtime.json`. Redeploy once with the same config and
new suffix; compare exact history in both profiles. Nothing has reset the original namespace.

### 5. Real concurrent opposing decisions, read boundaries and recovery

Use one additional fresh namespace for this small race so neither accepted terminal branch
can be changed by a nondeterministic winner. Preserve its runtime configuration and evidence
too. It still contains only the same single packaged fictional request. Deploy with section 2,
then open two tabs of the reviewer's real signed-in profile. Rerun the helper block in each tab
and verify empty history. Keep the processor's independent profile for the resulting read.

Choose the same future UTC epoch time in milliseconds for both tabs, at least 30 seconds away,
and place it in `wf.startAt`. In the first tab use decision `approve`; in the second use
`not_approve`. Each tab creates a distinct operation ID against version 0. Execute both snippets
before that time; each fires exactly once (no polling or repeated automatic decision):

```javascript
wf.raceOperation = {...wf.operation(0, 'Fictional competing review'), decision:'approve'}; // second tab: not_approve
wf.startAt = Date.parse('YYYY-MM-DDTHH:MM:SSZ'); // same future instant in BOTH tabs
wf.check(Number.isFinite(wf.startAt) && wf.startAt > Date.now(), 'Set a shared future instant');
await new Promise(resolve => setTimeout(resolve, wf.startAt - Date.now()));
wf.raceResult = await wf.post('decision', wf.raceOperation);
```

Preserve each result and operation. At most one competing transition commits. The other returns
`STALE_VERSION` or a bounded retryable conflict/unavailable result. On `RETRY_CONFLICT`, network
loss or unavailable response, deliberately replay that tab's **same** `wf.raceOperation` through
`wf.post`. Do not change its ID, expected version, or body. Resolve both: winner returns its
original receipt, loser returns `409 STALE_VERSION`, and the shared history has exactly one
decision event at version 1. Processor reads the winning decision/owner. Record request timings,
both distinct operation IDs, both results, final JSON and matching receipt from the winning
retry. This is cloud contention evidence; emulator contention alone is insufficient.

In both profiles check complete bounded reads and negative routes using the current namespace:

```javascript
wf.boundary = await wf.read();
wf.check(wf.boundary.events.length === wf.boundary.request.version, 'Incomplete history');
for (const [path, status, code] of [[wf.path + '?limit=1', 400, 'INVALID_INPUT'],
  ['/api/requests/unknown', 404, 'NOT_FOUND']]) {
  const response = await fetch(path, {cache:'no-store'});
  const data = await response.json();
  wf.check(response.status === status && data.error.code === code, 'Unsafe read boundary');
}
```

One role retains its existing view while the other adds a single fictional comment. Each GET
must contain exactly versions 1 through its own returned request.version; a later reload shows
the additional comment, with unchanged decision/owner. The 100-event cap and exact retry at
that cap are mandatory emulator gates, not a reason to manufacture 100 cloud comments.

If a save response is lost or a login/error page appears, retain the original operation/draft,
reload current history and use **Retry same operation**. A successful durable receipt is required
before claiming a save. `STALE_VERSION` reloads history and requires deliberate resubmission;
it must never silently approve a newer version. Local browser tests cover injected loss/login
responses; record unsupported cloud injection as local-only coverage, not a fabricated cloud pass.

Finally restore the privately saved approved runtime configuration to the active runtime file,
regenerate env and redeploy the same checked digest with another new suffix. Both profiles
refresh and verify the original M6 comments and full completed history match the saved approved
JSON exactly. The not-approved and race namespace configurations/evidence remain preserved.
Record this final served namespace/revision. If acceptance fails, keep M7 pending, retain the
operation IDs and evidence, and repair the fault without resetting any workflow.

### 6. Private M7 acceptance record

Create `reports/output/shared-workflow/m7-acceptance.md` privately. Mark all required rows with
actual observation/time; leave unsupported or unexecuted checks explicit. Use M6's access
shutdown commands only when the operator is ready to close this proof, retaining all evidence.

```markdown
# M7 private acceptance
Date/operator:
M6 acceptance reference and original database/namespace:
Step 34 code revision / source manifest / Cloud Build ID:
Actual image-inspection log / immutable image digest:
Pinned reviewer and processor subjects (private; distinct confirmed):
Approved namespace / initial handoff revision / durability revision / final restored revision:
Not-approved namespace / revision / durability revision:
Race namespace / revision / two operation IDs / request timestamps:
Runtime/build identities, region, IAP/private-invocation and inherited-policy checks:

| Observation | PASS / FAIL / PENDING | Evidence and timestamp |
|---|---|---|
| New actual image inspected before admission; preserved M6 configuration | PENDING | |
| Original comments and exact receipts survive upgrade to handoff | PENDING | |
| Reviewer approves with comment; processor sees ownership and may complete | PENDING | |
| Direct reviewer-completion and processor-decision requests are 403 with no event | PENDING | |
| Completion is administrative, terminal, null owner, correctly attributed | PENDING | |
| Fresh revision preserves completion, earlier comments and exact final history | PENDING | |
| Fresh namespace: not-approve is final; completion/change denied, no event added | PENDING | |
| Both roles comment after each final outcome without altering decision/owner | PENDING | |
| Original decision/completion retries return identical receipts without new events | PENDING | |
| Changed ID payload and stale new operations rejected without overwriting | PENDING | |
| Opposing real reviewer-tab race resolves to one event and original winner receipt | PENDING | |
| Both users read complete bounded history; later comments appear on reload | PENDING | |
| Query parameters and unknown IDs fail safely | PENDING | |
| Original approval namespace restored; both branch histories retained | PENDING | |
| Both users identify decision, next owner and comment authors; no payment claim | PENDING | |

Local-only loss/session/cap-injection coverage and any unsupported cloud observations:
Functional proof verdict (separate from adoption):
Retention owner / namespace configuration backups / intended retention:
Next: mark Step 35 DONE only after all required M7 observations pass.
```
