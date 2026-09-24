# Non-reimbursement action queue — Phase 10 feature plan

**Baseline:** `main` at `a421c94` on 2026-09-23. **Planning issue:** #85. **Umbrella:** #86. **Status:** IN PROGRESS (paused during Step 51). Plan review and plan wrap both returned READY on 2026-09-23; build issues #87–#92 are synced. Phase 9 still owns Steps 38–49, and its deferred receipt picker owns Step 50; this plan uses Steps 51–56. **Build entry gate:** Phase 9 Step 38 / issue #72 completed on 2026-09-23; all three CI jobs passed on PR #93. The earlier CI run for `a421c94` failed in `lint-type-test` because Chromium was absent. **Build pause:** Step 51 has an unfinished implementation in a preserved worktree; its full suite and independent review did not complete. See the [resume handoff](phase10-build-handoff.md).

## 1. What This Feature Does

An operator-invoked, private local queue turns selected archived-mail requests into reviewable non-reimbursement actions. It captures pre-purchase approval requests, adopted budget decisions that require follow-up, and planning estimates; groups exact thread replies; and retains an operator's owner, next action, status, and completion evidence across repeated refreshes. The 2026-09-23 reimbursement refresh found these requests in mail but had no durable action lane for them. The first milestone shows narrow suggestions, an uncategorized inventory, and a private report; it never treats message text as authority to pay, change a budget, or send a reply.

**Autonomous-behavior trigger:** does not fire. The queue runs only inside a user-invoked CLI process, with no scheduler, daemon, or watcher. The monthly GitHub Actions workflow and its credentials remain unchanged. **Data-pipeline trigger:** does fire: archive parser, classifier, private decision store, CLI, and HTML must agree on message and action keys. Step 55 is the real-component smoke gate before attended use.

## 2. Existing Context

- `pta_finance/gmail_source.py` writes date-window Gmail results as idempotent `.eml` files under the configured inbox; `pta_finance/receipt_ingest.py:931` reads top-level `.eml` and `.mbox` sources together. The Gmail connector is read-only and prints aggregate counts.
- `pta_finance/receipt_ingest.py:627` returns stable `MailEvidence` keys, normalized RFC ancestry, top-authored text, and decoded attachment metadata/hashes. Its evidence digest is already consumed by `pta_finance/reimbursement_pipeline.py:619` and must not change for this feature. `MailEvidence` does not carry the subject or archive label; the new scanner obtains those from the same parsed message and `iter_source` label.
- `pta_finance/receipt_ingest.py:861` recognizes actual reimbursement forms from structure, and `pta_finance/reimbursement_pipeline.py:179` builds reimbursement evidence from the full archive. `pta_finance/reimbursement_pipeline.py:1049` then links supplemental mail only through exact ancestry or explicit private anchors. A non-reimbursement queue must not loosen that ticket-link rule or revise the strict schema-v2 reimbursement bundle.
- `pta_finance/cli.py:1079` runs optional Gmail acquisition, reimbursement bundle refresh, and private HTML rendering. Its `--dry-run` writes no bundle or report. `pta_finance/reimbursement_report.py:1587` has an atomic private HTML-write pattern. The new action stage can use the same archive after the reimbursement stages without changing their signatures or report fields.
- `pta_finance/budget_sync.py:175` parses the editable fiscal-year budget tab and `pta_finance/cli.py:1196` applies a separately requested, snapshot-first budget sync. This feature records budget follow-up work but invokes neither function nor any Sheets write. `pyproject.toml` already supplies Jinja2 and the test/lint/type tools; no dependency is needed.
- `plan.md` reserves Steps 38–49 for automatic receipt filling; `documentation/receipt-autofill-plan.md:160` reserves Step 50 for a later receipt picker. Phase 9 Step 38 repaired the CI browser gate, verified by all three jobs on PR #93; receipt filling remains planned from Step 39.

Here CLI means command-line interface; CI means GitHub Actions continuous integration; RFC ancestry means the email `Message-ID`, `References`, and `In-Reply-To` headers defined for mail threading. JSON is the local JavaScript Object Notation file format, HTML is the generated browser report, and SHA-256 is the 64-hex digest used for source integrity. OAuth is the existing optional Gmail consent flow, not a new requirement for the local command.

## 3. Scope

**Included:** A private `update-actions` CLI over the existing local archive; deterministic, narrow proposals for purchase approval, budget follow-up, and planning estimates; a reviewable uncategorized inventory; exact source label, message key, RFC ancestry, and attachment metadata/hash provenance; exact-thread grouping and duplicate-source detection; operator-edited decisions with action type, owner, next action, status, optional existing-ticket reference, and completion evidence; a self-contained private HTML report; aggregate new/open/done/uncategorized counts in `update-reimbursements`; and a repeat-refresh proof using fictional mail.

**Excluded:** Creating reimbursement claims from free-text mail; changing ticket approvals or payments; extracting or interpreting spreadsheet attachment cells; editing either budget Sheet, sending email, scheduling background runs, fetching extra mail beyond the existing optional Gmail stage, hosting a new web UI, and committing private mail or decisions. The paper-claim and reimbursement-approval messages in the private triage stay in their existing reimbursement lane. An operator performs actual budget edits and replies through separately authorized tools.

**Effort:** Five sequential code steps and one attended operator step; no calendar estimate is assumed. **Kill criterion:** Stop before private-archive acceptance if a repeated refresh changes an operator decision, loses accounted evidence, or cannot keep reimbursement behavior intact. Retain the existing reimbursement workflow and the private triage as the fallback.

## 4. Impact Analysis

No existing function signature, reimbursement schema field, or shared evidence constant changes. The new module calls `receipt_ingest.iter_source`, `parse_mail_evidence`, and `parse_submission` as they stand. Their call sites were checked with `rg -n 'iter_source|parse_mail_evidence|parse_submission' pta_finance tests`; the scanner is additive, so the existing CLI mapping/ingest and reimbursement callers keep their contracts. The `cli.py` change adds command dispatch and a stage after the current refresh, not a new `refresh_kwargs` parameter.

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `pta_finance/cli.py` | extend | Add `update-actions` and report aggregate action counts after `update-reimbursements`; preserve dry-run semantics. | Read `_cmd_update_reimbursements` at 1079 and `build_parser` at 1324; `rg -n '_cmd_update_reimbursements|refresh_bundle' pta_finance tests` found production dispatch and reimbursement CLI tests. No signature changes. |
| `tests/test_reimbursement_cli.py` | extend | Prove the new stage, aggregate-only output, dry-run, and partial-failure message through the current production command. | `rg -n 'refresh_bundle|update-reimbursements' tests/test_reimbursement_cli.py` found the current stage-order and failure tests. |
| `README.md` | modify | Add the private action command and its manual decision boundary. | Read the current command overview and `pyproject.toml` entry point; no deployed UI is claimed. |
| `CLAUDE.md` | modify | Record the shipped boundary and current-state limits once built. | Read its reimbursement, Gmail, and budget-sync producer summaries; these are the sections this feature changes. |

`pta_finance/receipt_ingest.py`, `pta_finance/reimbursement_pipeline.py`, `pta_finance/reimbursement_report.py`, `pta_finance/budget_sync.py`, and `pta_finance/gmail_source.py` are read/reuse surfaces, not edit targets. Recheck this constraint during plan review if an implementation step proposes changing their shared fields or signatures; at that point enumerate every downstream call site and the evidence-digest blast radius before editing.

## 5. New Components

- `pta_finance/mail_actions.py`: archive scan, narrow classifier, exact thread grouping, strict private JSON load/merge, and aggregate summary. It consumes existing `MailEvidence` without widening it.
- `pta_finance/mail_action_report.py` and `pta_finance/reports/templates/mail_action_queue.html.j2`: validated, autoescaped, self-contained private report.
- `reports/output/mail-actions.json`: generated, gitignored proposal/evidence inventory, including source labels and uncategorized messages. `reports/output/mail-action-decisions.json`: separate, gitignored operator-edited decisions. The generated inventory never overwrites operator decisions. Both use a versioned strict schema; no real example goes into Git.
- `tests/test_mail_actions.py` and `tests/test_mail_actions_smoke.py`: fictional `.eml`/`.mbox` archive and real CLI-cycle coverage. `docs/mail-actions.md`: operator instructions using fictional identifiers and the exact source/decision/report paths.

Flat JSON is appropriate because one operator invokes a local refresh and edits a small decision file; it avoids a database and remains inspectable offline. A separate generated inventory prevents refreshes from overwriting operator edits. Jinja2 is already installed and autoescapes the HTML report, so no new UI service or runtime dependency is needed.

**Private file contracts.** The implementation defines these version-1 shapes once in `mail_actions.py`; the CLI and HTML renderer import and validate them. Unknown fields, duplicate JSON object keys, unknown enum values, and malformed identifiers fail validation. These tables are summaries of required fields, not permission to write real mail into the repo.

| Type | Required fields and shape | Role |
|---|---|---|
| `ActionInventory` | `schema_version: 1`, `scan_scope: {source_path: string, received_since: ISO date or null}`, `sources: SourceRecord[]`, `actions: ActionRecord[]`, `uncategorized_source_keys: string[]`, `quarantined_source_keys: string[]` | Generated `mail-actions.json`; unique sorted keys; retained across refreshes for drift checks. A different source/cutoff requires a new `--data`/`--output` pair. |
| `SourceRecord` | `message_key: string`, `evidence_sha256: 64-hex`, `source_labels: string[]`, `message_id: string`, `in_reply_to: string[]`, `references: string[]`, `subject: string`, `sender_address: string`, `date: string`, `top_authored_excerpt: string`, `top_authored_sha256: 64-hex`, `attachments: AttachmentRecord[]` | Private provenance for each distinct mail source. Excerpt has a fixed documented maximum length; attachment bytes are never stored. |
| `AttachmentRecord` | `filename: string`, `mime_type: string`, `decoded_size: integer`, `content_sha256: 64-hex` | Reuses decoded attachment evidence from `receipt_ingest`; does not parse spreadsheet cells. |
| `ActionRecord` | `action_key: string`, `proposed_type: enum or null`, `reason: string`, `source_keys: string[]`, `state: PROPOSED/OPEN/DONE/DISMISSED`, `decision: ActionDecision or null` | Generated proposal plus validated operator decision. `proposed_type` is null for a manual promotion; its reason is `operator promotion`. `source_keys` is unique and sorted. |
| `ActionDecisionFile` | `schema_version: 1`, `decisions: ActionDecision[]`, `related_reimbursement_sources: {message_key, review_key}[]` | Operator-owned `mail-action-decisions.json`; scanner reads but never edits it. An exact reimbursement relation excludes that source from action proposals. |
| `ActionDecision` | `action_key: string`, `type: PURCHASE_APPROVAL/BUDGET_FOLLOWUP/PLANNING_ESTIMATE`, `owner: string`, `next_action: string`, `status: OPEN/DONE/DISMISSED`, `source_keys: string[]`, `required_outcomes: {description: string, evidence: string or null}[]` | Operator confirmation or promotion of an uncategorized source. `DONE` requires nonempty evidence for every outcome. |

**Identifier contracts.** `message_key` is `mail:v1:<64 lowercase hex SHA-256>` generated by `receipt_ingest.parse_mail_evidence` from normalized Message-ID, or its documented evidence fallback when the ID is absent; it identifies one source across `.eml` and `.mbox` copies. `action_key` is `action:v1:<64 lowercase hex SHA-256 of the UTF-8 message_key>` generated by `mail_actions` from the earliest proposed or manually promoted source in a thread, ordered by parsed received date and then `message_key`; an existing persisted action key remains stable when older or later replies arrive. `review_key` is `submission:v1:<64 lowercase hex SHA-256 of the normalized reimbursement Message-ID>`, generated by `reimbursement_pipeline._review_key`; the action module validates an entered key against the private reimbursement bundle and never guesses one. RFC `Message-ID`, `References`, and `In-Reply-To` are normalized by `receipt_ingest`; only those exact identifiers can join a reply to an action. The generated source list is the single key/provenance source of truth for the decision validator and report.

**Development and first-run procedure.** From this repo root, use Python 3.12+ and `uv sync --locked --extra dev --extra web --extra slides`; `pyproject.toml` supplies the CLI entry point, Jinja2, pytest, Ruff, and mypy. For a local fictional archive run after Step 53, materialize the tracked fictional `tests/fixtures/mail_actions/*.txt` content as `.eml`/`.mbox` into a gitignored test directory, then use `uv run pta-finance update-actions --source reports/input/mail-actions-demo --all-received --dry-run`; omit `--dry-run` to write private JSON and HTML under `reports/output/`. The `.txt` suffix allows fictional fixtures to be tracked despite the global `.eml`/`.mbox` ignore rules. The standalone local command requires no Gmail or Sheets credentials; the optional Gmail fetch in `update-reimbursements` still uses the existing `config.toml`/OAuth setup in `SETUP.md`. To review a real archive, inspect `reports/output/mail-action-queue.html`, copy the fictional decision example from `docs/mail-actions.md` into the gitignored decision path, replace its keys with exact report keys, and rerun `uv run pta-finance update-actions --source reports/input/mail-archive --all-received` after placing the private archive there. Start/stop is one CLI process; rerunning is the refresh. Run `uv build`, `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict pta_finance`, and `uv run python scripts/check_no_identity.py` as build gates; CI additionally provides emulator and native jobs. Build work reads the current CLI and tests before changing them because Phase 9 also modifies `cli.py`.

## 6. Design Decisions

**Suggestions and review.** Deterministic rules may *propose* only the three scoped action types. A proposal includes its matching reason and source keys; it cannot set owner, completion, or authorization. Weak or unrelated mail remains uncategorized in the private inventory for manual promotion or dismissal. The rules must cover generic reply subjects when the top-authored body names an explicit budget-update instruction, and they must not classify a simple acknowledgment, meeting link, board vote by itself, or reimbursement-like mail as a new action. This trades some automatic coverage for auditable false-negative review rather than guessing a task from every message.

| Proposed type | Required positive evidence in subject or top-authored text | Rejection boundary |
|---|---|---|
| `PURCHASE_APPROVAL` | A request for approval **before** buying, paired with a purchase/wish-list description or attached equipment list. | A paid charge, receipt, reimbursement request, or approval already granted is not a new pre-purchase request. |
| `BUDGET_FOLLOWUP` | An explicit instruction to record an adopted allocation and update one or more budget surfaces; a generic `Re:` subject is allowed. | A vote, allocation notice, or acknowledgment without an outstanding update instruction is evidence, not a new task. |
| `PLANNING_ESTIMATE` | A budget-planning or tracker request paired with estimate wording and spreadsheet attachment metadata. | An expense form, invoice for payment, or unattached meeting note is not a planning estimate. |

These are rule families, not keyword-only authority: tests must cover negation, quoted prior text, and a reply that reports a task already completed. The operator can promote an uncategorized source if the rules miss a valid action; no classifier decision is final.

**Queue states and coverage.** `PROPOSED` is machine-generated and has no owner; `OPEN`, `DONE`, and `DISMISSED` require an operator decision. An uncategorized source is visible for manual promotion by an exact source key but is not an action. `new` counts action keys absent from the previous generated inventory, not every email added to an existing thread. The embedded stage uses the same resolved archive and received-date cutoff as `update-reimbursements`; the standalone command defaults to all received dates unless `--received-since` is given. Persist the normalized source/cutoff scope and reject a changed scope for an existing inventory, with instructions to use a new private `--data`/`--output` pair. It reports malformed in-scope evidence as a counted review/error state, never a silently dropped source.

**Stable identity and provenance.** Deduplicate `.eml`/`.mbox` copies by `MailEvidence.message_key`, refusing a conflicting digest for the same key. Create an action key from the first proposed source message key using the deterministic order defined above; later replies attach by exact `References`/`In-Reply-To` ancestry without rotating that key. If ancestry could attach a message to more than one action, quarantine it for operator review and leave the existing actions unchanged. A missing Message-ID can still be a stable standalone source, but cannot establish a thread link. Store original archive label, normalized ID, subject, sender, received date, a bounded top-authored excerpt and its digest, and attachment name/type/size/hash only in the private inventory. Never emit those fields on stdout or into tracked fixtures.

**Operator decisions.** Keep decisions in a separate file keyed by action key. The operator can confirm or correct type, set owner and next action, mark `OPEN`, `DONE`, or `DISMISSED`, add source references, promote an uncategorized source by its exact key, and optionally mark a message as related to an existing reimbursement ticket instead of creating an action. A ticket relation uses an exact `review_key` checked against the private reimbursement bundle when supplied; no name/subject match establishes it. Each confirmed action has one or more required outcomes with a description and completion evidence; `DONE` is invalid until every required outcome has evidence. A budget-follow-up proposal seeds separate internal and public update outcomes, which the operator checks against the actual decision before confirming. On repeat refresh, preserve reviewed fields and status byte-for-byte; append only new, valid source references. If a previously accounted source is missing or its evidence digest changes, fail before replacing the private report. Do not derive a completion state from incoming mail.

**Local interface and boundaries.** `pta-finance update-actions` accepts `--source`, `--received-since`/`--all-received`, `--data`, `--decisions`, `--output`, optional `--reimbursements-data`, and `--dry-run`; default private paths are `reports/output/mail-actions.json`, `reports/output/mail-action-decisions.json`, `reports/output/mail-action-queue.html`, and `reports/output/reimbursement-report.json`. It writes a proposal inventory and HTML report under gitignored `reports/output/`, validates the separate decision file, and prints counts only. Validate and stage both generated artifacts before replacement; keep same-directory backups of prior generated files, atomically replace each target, and restore backups if either replacement fails. Include the inventory digest in the HTML and verify it at the next refresh so an interrupted two-file replacement is detected and repaired before normal processing. Never write the operator-owned decision file. A single-process lock covering read/validate/replace refuses concurrent refreshes, and a failed validation or render leaves both prior action artifacts untouched. The report gives the operator source references and attachment metadata plus proposed, open, done, dismissed, and uncategorized views; untrusted text is escaped. `update-reimbursements` passes its resolved archive/cutoff into the same action stage after its existing report step and prints the new counts. A failure must say whether the reimbursement artifacts already refreshed and preserve the prior action report; no success message may imply the whole run completed. `--dry-run` validates and counts with no local artifact write and no extra network request. The default is active for a valid local archive; the standalone command supports reruns without Gmail or Sheets.

**Parallel phase boundary.** Phase 9 also plans to modify `cli.py`. Its Step 38 browser repair is the build entry gate; later receipt-fill work need not land before this feature. Whichever phase lands second rechecks current stage order and dry-run behavior, then keeps action processing after reimbursement rendering (and after receipt filling if that stage exists). Neither phase changes the other's private schema or uses Step 50.

## 7. Build Steps

<!-- autofix-applied: 2026-09-23 -->
### Step 51: Propose actions from archived mail
- **Problem:** Make `pta-finance update-actions` scan the local archive and produce a strict private proposal/uncategorized inventory with stable source keys, narrow type reasons, attachment provenance, and exact-thread deduplication.
- **Type:** code
- **Issue:** #87
- **Flags:** `--reviewers deep`
- **Files:** `pta_finance/mail_actions.py`, `pta_finance/cli.py`, `tests/test_mail_actions.py`, `tests/fixtures/mail_actions/*.txt`
- **Produces:** `pta_finance/mail_actions.py`, the `update-actions` entry point in `pta_finance/cli.py`, `tests/test_mail_actions.py`, fictional source fixtures with `.txt` suffix
- **Done when:** running the CLI on a fictional mixed `.eml`/`.mbox` archive yields one proposal for each of the three scoped types, groups an exact reply without duplicating an action, and leaves unrelated/reimbursement-like mail uncategorized; a second identical run reports zero new proposals; a duplicate key with changed evidence fails before output replacement; ambiguous ancestry is quarantined without changing existing actions; stdout contains counts and paths only.
- **Depends on:** Phase 9 Step 38 / #72

<!-- autofix-applied: 2026-09-23 -->
### Step 52: Preserve operator decisions across refreshes
- **Problem:** Validate a separate operator decision file and merge its confirmed type, owner, next action, status, ticket relation, and required outcomes with completion evidence into the action inventory without rewriting those fields on a repeat scan.
- **Type:** code
- **Issue:** #88
- **Flags:** `--reviewers deep`
- **Files:** `pta_finance/mail_actions.py`, `docs/mail-actions.md`, `tests/test_mail_actions.py`
- **Produces:** strict decision loader/merge in `pta_finance/mail_actions.py`, fictional decision example in `docs/mail-actions.md`, decision-cycle tests in `tests/test_mail_actions.py`
- **Done when:** the production `update-actions` command retains an `OPEN` decision after new thread replies, retains a `DONE` decision and its evidence after a repeat refresh, refuses `DONE` until every required outcome has evidence (including both seeded budget surfaces), refuses unknown/duplicate source or action keys, and refuses changed or missing previously accounted evidence before replacing private output.
- **Depends on:** 51

<!-- autofix-applied: 2026-09-23 -->
### Step 53: Render the private action queue
- **Problem:** Render the validated action inventory into a self-contained, autoescaped HTML report that lets an operator inspect proposals, active work, completed work, and uncategorized sources.
- **Type:** code
- **Issue:** #89
- **Flags:** `--reviewers deep`
- **Files:** `pta_finance/mail_action_report.py`, `pta_finance/reports/templates/mail_action_queue.html.j2`, `pta_finance/mail_actions.py`, `tests/test_mail_actions.py`
- **Produces:** `pta_finance/mail_action_report.py`, `pta_finance/reports/templates/mail_action_queue.html.j2`, report tests in `tests/test_mail_actions.py`
- **Done when:** `update-actions` writes the private report with source/thread/attachment references and the operator's next action/status; hostile subject/body text renders inert; a validation or render failure leaves the prior report intact; no private value enters public stdout or a tracked artifact.
- **Depends on:** 52

<!-- autofix-applied: 2026-09-23 -->
### Step 54: Surface actions in the reimbursement end-to-end run
- **Problem:** Add the local action stage and aggregate new/open/done/uncategorized counts to `update-reimbursements` while keeping its current reimbursement, receipt, no-Sheets, no-send, and dry-run contracts.
- **Type:** code
- **Issue:** #90
- **Flags:** `--reviewers deep`
- **Files:** `pta_finance/cli.py`, `tests/test_reimbursement_cli.py`, `docs/mail-actions.md`, `README.md`, `CLAUDE.md`
- **Produces:** action-stage dispatch in `pta_finance/cli.py`, regression tests in `tests/test_reimbursement_cli.py`, operator guide in `docs/mail-actions.md`, updated `README.md` and `CLAUDE.md`
- **Done when:** the production CLI prints action counts after a successful local refresh; `--dry-run` writes neither action nor reimbursement artifacts; a failed action stage reports the already-refreshed reimbursement boundary and preserves the prior action report; the standalone command and current reimbursement output stay usable; no action stage calls a Sheets writer, payment function, or Gmail sender.
- **Depends on:** 53

<!-- autofix-applied: 2026-09-23 -->
### Step 55: Real-component archive-to-report smoke gate
- **Problem:** Exercise one full offline cycle through `pta-finance update-reimbursements` using fictional mail, a fictional reimbursement bundle, and the actual classifier, decision loader, renderer, and CLI to catch producer/consumer key drift.
- **Type:** code
- **Issue:** #91
- **Flags:** `--reviewers deep`
- **Files:** `tests/test_mail_actions_smoke.py`, `tests/fixtures/mail_actions/*.txt`
- **Produces:** `tests/test_mail_actions_smoke.py` with a complete fictional fixture and repeat-run proof
- **Done when:** a no-mock CLI cycle creates both private reports and the expected three action proposals, a confirmed budget-follow-up action stays open until both completion references are recorded, an exact reply joins its existing action, a repeat cycle has zero new actions and preserves the decision bytes, and unrelated mail remains uncategorized. All three CI jobs pass after the Step 38 browser repair, including the emulator and native jobs; Ruff, strict mypy, and identity guard pass. Record local-suite scope explicitly if the emulator is unavailable there.
- **Depends on:** 54

<!-- autofix-applied: 2026-09-23 -->
### Step 56: Attended private-archive acceptance
- **Problem:** Run the new action stage on the current private archive and check that its proposals correspond to the three non-reimbursement cases in the September 23 triage, with no reimbursement request, approval email, acknowledgment, or meeting-only mail incorrectly promoted.
- **Type:** operator
- **Issue:** #92
- **Files:** `reports/output/mail-actions.json`, `reports/output/mail-action-decisions.json`, `reports/output/mail-action-queue.html`, `reports/output/mail-action-acceptance.md`
- **Produces:** a private acceptance record under `reports/output/` containing checked source keys, any manual classifications, counts, and the operator's judgment
- **Done when:** the operator inspects the private action report, confirms source/thread/attachment references for the three triage cases, records the correct owner and next action for each, checks that the budget follow-up remains open until both budget surfaces are evidenced, and verifies a second run preserves the decisions. No actual budget update or outgoing email is performed by this step.
- **Depends on:** 55

## 8. Risks and Open Questions

| Item | Risk | Mitigation |
|---|---|---|
| False negatives and false positives | Narrow rules miss wording variants; broad rules turn ordinary mail into tasks. | Show uncategorized sources privately, retain rule reason on every proposal, allow manual promotion, and test the three fictional cases plus acknowledgments and meeting mail. |
| Public privacy | The archive contains real identities, text, and attachments. | All generated action/decision/report files live under gitignored `reports/output/`; stdout is aggregate-only; tracked fixtures are fictional; run `scripts/check_no_identity.py`. |
| Source and thread drift | Reissued messages, `.eml`/`.mbox` overlap, and partial ancestry could duplicate or rotate action IDs. | Stable message keys, exact RFC ancestry only, conflict detection, explicit ambiguity quarantine, and repeat-refresh smoke coverage. |
| Manual decision loss | Regeneration could overwrite an operator's edits. | Decisions live in a separate validated file; merge preserves them; missing/changed accounted evidence aborts before replacement. |
| Two budget follow-ups | One thread requires both internal and public updates, so a single generic DONE marker could hide one unfinished output. | Decision records contain required outcomes; the budget proposal seeds both, and validation forbids DONE until each has evidence. |
| Partial pipeline writes | The action stage follows an existing reimbursement write. | Stage and validate action JSON/HTML before replacement; on failure, say exactly which prior artifacts refreshed and keep the old action report. |
| Phase 9 overlap | Its receipt-fill stage will also touch `cli.py`. | Re-read current CLI at Step 54 and preserve stage order/dry-run contracts; no shared schema change. |
| Existing test gates | Step 38 repaired the CI browser gate; a full local suite needs Firestore emulator and native extras. | Step 38 / #72 is complete. Record exact gate evidence during Step 55 without mislabeling a subset as full. |

The planning decisions are fixed for this phase: deterministic narrow suggestions, a private JSON decision file, a self-contained HTML queue, and operator acceptance after the automated smoke gate. The classifier is evidence-bound by the three positive and rejection rules above; implementation tests add concrete wording variants without widening these action types.

## 9. Testing Strategy

Use fictional `.eml` plus `.mbox` duplicates through the real `receipt_ingest.iter_source` and `parse_mail_evidence` path. Cover three scoped proposal types, exact RFC reply grouping, malformed/absent IDs, conflicting digests, missing previously accounted mail, attachment hash provenance, non-form reimbursement references, unknown mail, hostile HTML text, duplicate/unknown decision keys, completion-evidence gates, and repeated refreshes. Drive CLI tests through `cli.main`, with aggregate-only stdout assertions. Step 55 runs the full offline producer-to-consumer cycle without mocking the archive parser or report renderer; Step 56 checks the real private archive and operator judgment. All three CI jobs, Ruff, strict mypy, `git diff --check`, and the public identity guard are build gates; any local environment-dependent collection failure is reported explicitly.

**Build handoff:** Phase 9 Step 38 / #72 and its PR CI gate are complete. Resume build-phase at Step 51 against the preserved worktree using the instructions in documentation/phase10-build-handoff.md. Steps 52-55 remain pending; Step 56 is attended private-archive acceptance.
