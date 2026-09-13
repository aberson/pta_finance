# Ticket creation and lifecycle rules

**Status:** Product design recorded; not implemented and not a build-ready engineering plan.
**Operator decision:** Only the Treasurer/Processor account creates tickets in the first version. The existing Reviewer account independently reviews them.
**Proposed sequence:** Add this milestone after the [live shared queue](shared-request-queue-plan.md) is accepted in M8. This sequence and the rules below are agent defaults; the creator-role restriction is the operator's explicit choice. Initial use remains fictional. This document does not authorize real reimbursement intake or payment processing.

## 1. Create, read, update and delete

CRUD means create, read, update and delete. For this workflow, deletion is a reversible archive action.

| Operation | First-version rule |
|---|---|
| Create | The enabled Processor can choose **New ticket**, enter request details and save a shared draft. The server assigns a stable ticket identity and records the verified creator. Saving or retrying the same creation must not duplicate the ticket. |
| Read | Both enabled accounts see drafts, submitted requests and terminal requests in the shared queue. Archived tickets remain accessible through an **Archived** view and their original link. Drafts are shared, not private to the creator. |
| Update | The Processor can edit request details while the ticket is an active draft. Submission locks those details. Both roles may append comments to active tickets; existing comments, decisions and recorded authors are never edited in place. |
| Delete | The Processor can archive a draft, a not-approved ticket or a completed ticket, with a reason. Archive hides it from the default queue and disables writes. The Processor can restore it to its previous state. No permanent-delete action or history erasure is exposed. |

Reviewer accounts have no create, edit-details, submit, withdraw, archive or restore permission. Hiding a button is only presentation; the server enforces every rule on direct API requests and retries as well.

## 2. State and permission rules

| Current state | Allowed next action | Actor | Result |
|---|---|---|---|
| Draft | Save changes | Processor | Draft with a new recorded revision |
| Draft | Submit for review | Processor | Awaiting review; exact submitted details preserved; next owner Reviewer |
| Awaiting review | Withdraw for correction, with a reason | Processor | Draft; prior submission and withdrawal remain in history |
| Awaiting review | Approve, with a comment | Reviewer | Approved; next owner Processor |
| Awaiting review | Not approve, with a comment | Reviewer | Not approved; no next owner |
| Approved | Complete | Processor | Completed; no next owner; no payment recorded |
| Active Draft / Awaiting review / Approved / Not approved / Completed | Add comment | Either role | State unchanged; new attributed event |
| Draft / Not approved / Completed | Archive, with a reason | Processor | Archived; original state retained; no current action |
| Archived | Restore | Processor | Prior state restored, with its corresponding next owner |

An active draft's next owner is Processor. Archived tickets are read-only except for Restore and exact receipt replays that make no new change. Awaiting-review and approved work cannot be hidden with Archive. Withdraw an awaiting-review ticket first. Cancellation after approval, reopening terminal decisions and editing approved/completed details are outside the first version.

After a withdrawal, saving edits and resubmitting creates a new review round. Approval must refer to the exact submitted revision. A simultaneous withdrawal and review decision cannot both succeed; the losing action shows the latest state and requires a deliberate new action. A not-approved or completed request that needs materially different details is handled as a new ticket, preserving the old one.

## 3. New ticket form

The first form contains a title, requester display name, expense date, purpose, and line items with description, category and amount. Currency is USD, matching the existing fictional display. The server computes the total from the line items; the browser cannot supply an authoritative total, state, owner or actor.

Drafts may be incomplete. Submission requires a nonblank title, requester, purpose and valid expense date, plus at least one complete positive-amount line item. Amounts use exact cents. Field lengths, item-count limits, the allowed category list and the error contract must be specified in the engineering plan using the implemented queue's producers before build dispatch. There is no file upload, email import or payment evidence field in this first form.

The page makes **Save draft** and **Submit for review** distinct actions. It shows the signed-in role, current state, next owner, current details and attributed history. Unsaved changes remain visible on validation or network errors. After a successful save, the ticket can be reopened from the queue in either account; no browser-only draft storage is treated as durable.

## 4. History and concurrency

Creation, saved revisions, submission, withdrawal, comments, decisions, completion, archive and restore each record the verified actor, server timestamp and operation receipt. Detail changes retain sufficient before/after evidence to reconstruct every submitted version. Archive is a visibility change, not a replacement workflow outcome.

Updates require the version the user actually loaded. Concurrent edits never silently overwrite each other. Ambiguous-response retries reuse the same operation identity; an exact retry returns the existing receipt, while changed content with the same operation is a conflict. Restore, archive and submit obey the same rule. Authorization is checked before serving any replay to a disabled account.

Every original packaged request, source identity and receipt remains intact. Created tickets need an explicit persisted source/revision model. Do not loosen the existing source-integrity check to make editing appear to work.

## 5. Implementation boundary

The current implementation supports a fixed immutable source and four handoff states. [models.py](../pta_finance/shared_workflow/models.py#L21) owns those states and transitions; [load_source](../pta_finance/shared_workflow/models.py#L235) constructs the packaged source; [Store._request](../pta_finance/shared_workflow/store.py#L164) compares stored details against it. [app.py](../pta_finance/shared_workflow/app.py#L130) admits only the configured request and exposes comment/decision/completion routes. These producer reads establish that creation, draft updates, archive and restore do not exist today.

The pending queue plan deliberately admits exactly six packaged sources. Ticket creation is a subsequent scope change: it needs a durable catalog of user-created tickets, new lifecycle/revision storage, list/count behavior for a growing catalog, corresponding authenticated forms/routes, and installed-image compatibility checks. It must not merely append an in-memory row or edit a packaged fixture.

After the queue is implemented, author its engineering plan against that actual source: enumerate all schema/function consumers; define IDs, request/response schemas, catalog capacity/pagination, transaction boundaries, source-version compatibility and rollback; then split code delivery from real-account acceptance. Run technical review, proposal publication and readiness checks before issue synchronization and build dispatch. No new numbered code steps or issues are minted by this product-rule document.

## 6. Acceptance scenarios for that plan

1. Processor creates a fictional draft, changes its title and line items, reloads and finds the same ticket from both accounts.
2. Reviewer cannot create or edit a ticket through either the page or a direct API request. Both accounts can read the shared draft.
3. Submission validates the details, freezes the exact revision and gives the Reviewer the next action. Direct detail editing after submission fails.
4. Processor withdraws, corrects and resubmits; the older submitted details remain inspectable. A competing approval/withdrawal race produces exactly one successful action.
5. Reviewer approves and Processor completes a newly created ticket. A separate created ticket follows the not-approved branch. Author, time, version and next owner are correct throughout.
6. Processor archives and restores a draft and a terminal request. Archived requests disappear from the default view, remain readable in Archived, and retain all history. Awaiting-review/approved archive attempts fail.
7. Repeated creation and mutation requests after lost responses add no duplicates. A stale edit does not overwrite newer data. Disabled-account, unknown-ID and request-isolation checks pass.
8. All created requests, submitted revisions, archived flags and old packaged histories survive an inspected new service revision. Local component/browser tests are required before real-account acceptance; neither substitutes for the other.

The next engineering plan must turn these scenarios into measurable code gates and an actual hosted walkthrough. This product design establishes the behavior to build; it does not claim that its storage and API implementation has been specified or reviewed yet.
