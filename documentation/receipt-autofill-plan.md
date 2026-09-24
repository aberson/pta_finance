# Automatic source-receipt filling in the reimbursement refresh

Phase 9 — Steps 38–49. Planned 2026-09-16 against `main` at `7eb0c4f`.

**Goal:** Make `pta-finance update-reimbursements` produce receipt evidence instead of only
consuming it, so newly arrived review items stop rendering "Receipt not linked" until someone
hand-builds a sidecar.
**Status:** STEP 38 DONE (2026-09-23); Steps 39–49 planned.

Master plan: [`plan.md`](../plan.md). Steps 1–37 are reserved by earlier phases; this feature
continues at **Step 38**.

Related: [receipt viewer and source backfill](../plan.md) (shipped 2026-09-14, the consumer this
feature feeds), [reimbursement refresh plan](reimbursement-refresh-plan.md) (the bundle producer),
[treasurer summary Wave 1](treasurer-summary-wave-1-plan.md) (owner of the LPAC native worker this
feature extends).

---

## 1. What This Feature Does

`pta-finance update-reimbursements` refreshes the private reimbursement bundle and re-renders the
queue HTML, but it has never produced the receipt sidecar the queue reads. The sidecar is an
*input*: `build_report` looks for `<bundle>.receipts.json` beside the bundle and embeds it if
present. That file was hand-built once, by a bespoke offline backfill. Every review item that
arrives after that build therefore renders **"Receipt not linked"** forever, no matter how many
times the refresh runs.

This feature adds a fourth stage to the refresh that *produces* receipt evidence instead of only
consuming it: it fetches each ticket's own uploaded receipt assets from an operator-configured
host allowlist into a private content-addressed cache, renders them to display pages (PDFs
through the existing attested Windows LPAC worker), and writes sidecar links for the cases where
page-to-item provenance is a fact rather than a guess. Everything ambiguous is staged into a
private proposals file that an operator confirms explicitly.

Red outlines are deliberately **not** automated. `receipt_viewer.py:4-5` states the rule this
feature must not break: *"Never infer a location from an amount or description: an absent box
means the exact line has not been identified."* Machine-placed boxes would make a link assert
something no human checked. This feature gets the right page in front of the reviewer; a human
still marks the line.

**Triggering event.** Operator report, 2026-09-16: the end-to-end run "doesn't seem to fill
receipts". Confirmed — the run went from 80 to 84 unlinked items with zero fingerprint drift,
purely because two tickets arrived on 2026-09-13 and nothing regenerates the sidecar.

**Autonomous-behavior trigger: does not fire.** Every stage here runs inside one operator-invoked
CLI process that completes and exits. No scheduler, daemon, watcher or background loop is added.
The monthly GitHub Actions workflow is untouched and still does reports only. Reviewers should
confirm this classification rather than assume it.

---

## 2. Existing Context

Baseline inspected at `7eb0c4f`, branch `main`, 2026-09-16. Findings below come from producing
files, not from documentation.

| Producer | Behavior verified in source |
|---|---|
| `pta_finance/reimbursement_report.py:1604` | `build_report` computes `receipts_path = data_path.with_suffix(".receipts.json")`. |
| `pta_finance/reimbursement_report.py:1606-1608` | Loads the sidecar only `if receipts_path.exists()`; otherwise `None`. |
| `pta_finance/reimbursement_report.py:1551` | `render_html` falls back to `{"pages": {}, "items": {}}`, silently dropping every item without an entry. |
| `pta_finance/reports/templates/reimbursement_queue.html.j2:74` | Emits `View source receipt` vs `Receipt not linked` from `receipts['items'].get(receipt_id(ticket, item))`. |
| `pta_finance/cli.py:1101-1187` | `update-reimbursements` is exactly three stages: Gmail acquisition, `refresh_bundle`, `build_report`. No stage writes the sidecar. |
| `pta_finance/receipt_viewer.py:20-21` | `MAX_PAGE_BYTES = 20 MiB`, `MAX_TOTAL_BYTES = 100 MiB`, counted on **raw** bytes before base64. |
| `pta_finance/receipt_viewer.py:28-43` | `item_fingerprint` folds in `ticket.source_evidence_sha256` plus the item's source and effective fields. |
| `pta_finance/reimbursement_pipeline.py:314-328` | `evidence_payload` is the digest surface behind `source_evidence_sha256`. Receipt URLs (`:323`) and attachment filenames (`:324`) are hashed into it and then **dropped** — the bundle keeps no pointer to a receipt. |
| `pta_finance/reimbursement_pipeline.py:140-142` | `review_key = "submission:v1:" + sha256(<b>raw</b> `Message-ID` header, stripped)` — `_review_key(submission.message_id.strip())` at `:266`/`:273`. **Not** the normalized form: `normalize_message_id` lowercases the domain and feeds the separate `source_message_id` at `:329`. This is the only durable one-way join from a ticket back to its `.eml`. |
| `pta_finance/receipt_ingest.py:787`, `:889`, `:892` | `_extract_receipt_urls` already parses the submission's labeled upload fields into `Submission.receipt_urls`; `source_receipt_urls_v1` (`:153`, `:707-713`) is the frozen digest-surface subset. |
| `pta_finance/treasurer_slides/native_sandbox.py:1773` | `start_native_pdf_worker` — the attested Windows LPAC boundary. One capability (`registryRead`), no network, no inherited handles, no source path. |
| `pta_finance/treasurer_slides/bank_statements.py:69-95` | Eleven public limits, each paired with a `_HARD_MAX_*` ceiling; re-validated inside the worker by exact key-set equality. |
| `pta_finance/config.py:276-301` | `_load_receipt_mapping` is the exact shape a new optional config block must follow: `None` when absent, `ConfigError` when malformed. |
| `.gitignore:10` | `reports/output/` is already fully ignored — cache and page directories placed under it need no new rule. |
| `pyproject.toml:17-27` | Extras are `pdf`, `slides` (`pypdfium2>=5.13.0`), `web`, `dev`. Core dependencies at `:7-15` must not grow. |
| `.github/workflows/ci.yml:22`, `:50-51`, `:111` | Three jobs. Only `shared-workflow` installs Chromium, and it is path-filtered away from `tests/test_receipt_viewer.py`. |

### Measured state of the gap (2026-09-16)

| | count |
|---|---|
| Review items in the live bundle | 231 |
| Items with a sidecar link | 147 |
| Items showing "Receipt not linked" | 84 |
| — of those, one legacy ticket adjudicated 2026-09-14 as having no locatable invoice | 69 |
| — structurally unlinkable (paper totals, blank form rows, duplicate/reissue markers, one absent original) | 11 |
| — **arrived 2026-09-13, never had a chance to be linked** | **4** |
| Fingerprint drift across the 2026-09-14 → 2026-09-16 runs | 0 |

All four new items carry an upload URL in their own submission email that has never been
downloaded. Corpus-wide the intake form delivers receipts as hosted URLs, never as MIME
attachments: only 4 of 319 parsed submissions carry any attachment at all, and none of the 84
unlinked items' source forms do. Exactly two upload hosts appear anywhere in the corpus, both
belonging to the intake form vendor's CDN. Their real values are private configuration and must
never appear in a committed file.

### Measured page-budget state

| metric | value |
|---|---|
| Sidecar pages / raw bytes | 164 / 71,349,077 B (68.04 MiB) |
| Cap consumption | 68.0% of `MAX_TOTAL_BYTES`; 31.96 MiB free |
| Largest single page | 1.047 MiB (5.2% of the 20 MiB per-page cap — not binding) |
| Stored mix | 108 PNG (44.64 MiB) / 56 JPEG (23.40 MiB) |
| Rendered HTML | 95,558,877 B (base64 inflates raw page bytes ~1.37x) |
| Pages produced per fetched asset | PDF 1.537, PNG 1.000, JPEG 1.000 |
| Asset mix already fetched | 67 PDF / 36 PNG / 33 JPEG of 136 |

Those ratios describe *pages produced* (172 from the 136 fetched assets), not pages *embedded*: the
sidecar holds 164 because unreferenced pages are pruned and 8 pages were staged separately. The
§ 6.5 cap projections use the produced-pages ratio, which is the conservative direction.

Re-encode measurements over all 164 live pages (Pillow 12.2.0, LANCZOS, downscale only when the
page exceeds the limit):

| variant | total | vs current | median fine-print line height |
|---|---:|---:|---:|
| current | 68.04 MiB | 100% | 19.0 px |
| **long edge 2200 / JPEG q85** | **41.34 MiB** | **60.7%** | **19.0 px** |
| long edge 1800 / q82 | 30.18 MiB | 44.3% | 17.2 px |
| long edge 1600 / q80 | 24.34 MiB | 35.8% | 15.3 px |

Only 7 of 164 pages exceed a 2200 long edge, so the 2200/q85 variant is effectively a PNG→JPEG
transcode: it saves 39% with **identical** fine-print geometry and zero pages pushed under 8 px.
The tighter variants buy their extra saving by shrinking pixels on receipts that contain small
print. 2200/q85 is therefore the normalization default.

---

## 3. Scope

### Included

- A `fill-receipts` stage inside `update-reimbursements`, between the refresh summary and
  `build_report`, plus a `link-receipts` verb group for the staged half.
- Fetching a ticket's own uploaded receipt assets over HTTPS from an operator-configured host
  allowlist, into a gitignored content-addressed cache, with magic-byte sniffing, size ceilings
  and per-asset failure isolation.
- Rendering PDF assets to page images through the existing attested Windows LPAC worker,
  extended with a render response. Non-Windows hosts fail closed: PDFs are cached and counted,
  never parsed.
- Deterministic page normalization (EXIF transpose, long edge ≤ 2200, JPEG q85) shared by every
  producer, with one source of truth for the geometry constants.
- Auto-committing `box: null` sidecar regions only under § 5A's three conditions — exactly one
  distinct receipt asset, at least one page produced from it, and no item on the ticket already
  linked — then linking every item on that ticket to every page of that one asset.
- A private `<bundle>.receipt-proposals.json` for every ambiguous ticket, and
  `link-receipts confirm` to merge an operator-edited proposals file into the sidecar.
- A page-budget pre-check that refuses before crossing `MAX_TOTAL_BYTES`, naming the page and the
  remaining budget.
- Repairing the currently-red CI browser guard, which blocks every gate downstream.

### Explicitly out

- **Automatic box placement.** No OCR, no description matching, no amount matching. Forbidden by
  `receipt_viewer.py:4-5`, and the 2026-09-14 backfill is evidence that mechanical matching needs
  per-item adversarial review to be trustworthy.
- **Re-encoding the existing 164 human-verified pages.** It would roughly double headroom, but it
  rewrites evidence a human already checked. Reserved as an explicit, separately-invoked
  `link-receipts compact` verb in a later phase, never an automatic step.
- **A local HTML picker for confirming multi-asset tickets.** Reserved as Step 50 in a later
  phase; the CLI confirm path ships first so the feature is complete and testable.
- Recovering the 80 items that § 2 splits into 69 adjudicated-no-locatable-invoice and 11
  structurally unlinkable. Their originals are absent from the evidence.
- Any Sheets write, any outbound mail, any change to the monthly workflow, any change to
  adjudication, amounts, or review decisions.
- Harvesting receipt images from `.eml` MIME attachments. Measured at 0 of the 84 unlinked items
  and 4 of 319 submissions corpus-wide; it would close nothing today. Noted as a cheap follow-up
  because it shares the sidecar-merge half and skips transport entirely.

---

## 4. Impact Analysis

| File | Change Type | Reason | Verified |
|---|---|---|---|
| `pta_finance/receipt_viewer.py` | extend | Export the page-budget constants and a headroom helper so producers cannot redefine them. `load_receipts` and `item_fingerprint` are **unchanged**. | grep'd `MAX_TOTAL_BYTES` / `MAX_PAGE_BYTES` — 1 definition + 1 use each, both in this file (`:20-21`, `:114`, `:118`); no external consumer. `item_fingerprint` — 3 sites: def `:28`, use `:145`, `tests/test_receipt_viewer.py:61`. All three keep calling it; nothing re-derives it. |
| `pta_finance/reimbursement_report.py` | none | Sidecar detection and render already do the right thing once the file exists. | grep'd `render_html` — 2 production (`:1526` def, `:1611` call) + 6 test sites (`test_reimbursement_pipeline.py:1951`; `test_reimbursement_report.py:316,365,389,556,676`). Signature unchanged, so all 8 are unaffected. |
| `pta_finance/cli.py` | modify | New stage at `:1176`; new `link-receipts` subparser group; new flags on `update-reimbursements`. | grep'd `build_report` across `pta_finance/`, `tests/`, `scripts/` — **14 sites**: 1 definition (`reimbursement_report.py:1598`), 2 production callers in `cli.py` (`:1067` `report-reimbursements`, `:1181` `update-reimbursements`), **1 in `scripts/capture_readme.py`** (the README screenshot helper — it renders fictional data and must keep doing so; verify it never picks up a real sidecar), and 10 test sites (`test_receipt_viewer.py` ×5, `test_reimbursement_cli.py` ×3, `test_reimbursement_report.py` ×2). Only `:1181`'s enclosing function changes; `:1067` stays offline and untouched. |
| `pta_finance/config.py` | extend | New optional `[receipt_assets]` block following `_load_receipt_mapping` exactly. | Read `:100-126` (dataclass + `Config` optional fields) and `:276-301` (parser). Additive optional field; absent block = today's behavior. |
| `config.example.toml` | extend | Ship the block with **fake** hosts. Real allowlist stays in gitignored `config.toml`. | Identity rule, `CLAUDE.md:3-8`. |
| `pta_finance/receipt_ingest.py` | extend | Expose the already-parsed `Submission.receipt_urls` to the fill stage. **No change to `source_receipt_urls_v1`.** | grep'd — `receipt_urls` set at `:889` from `_extract_receipt_urls` (`:787`), read at `:1102`; `source_receipt_urls_v1` at `:153`, `:707-713`, `:892`. The frozen-digest-surface split already exists and is the precedent to follow. |
| `pta_finance/reimbursement_pipeline.py` | **none — hard constraint** | Nothing may enter `evidence_payload`. | Read `:314-328` (payload), `:351` (digest), and the fail-closed guards at `:1789-1812`. Adding one field rotates every `source_evidence_sha256`, which rotates every `item_fingerprint`, which invalidates all 147 live links and makes the refresh refuse. `refresh_bundle`/`plan_bundle_refresh` signatures unchanged — grep'd `refresh_bundle` across `pta_finance/`, `tests/`, `scripts/`: **59 sites** (1 production caller `cli.py:1156`, 2 in `reimbursement_pipeline.py`, 3 in `test_reimbursement_cli.py`, and **53 in `test_reimbursement_pipeline.py`**). The signature is heavily pinned; the new stage must therefore be its own call, never a new kwarg. |
| `pta_finance/treasurer_slides/bank_statements.py` | extend | Render limits, render request/response, and the page-raster path. Landing the code here means `native_sandbox.py` needs **zero** changes. | Read `_PUBLIC_WORKER_PACKAGE_FILES` (`native_sandbox.py:36-42`) — a closed 5-file allowlist that already includes `bank_statements.py`. A new module would have to be added to it. |
| `pta_finance/treasurer_slides/native_sandbox.py` | none (target) | Protocol parameters ride the existing limits envelope. | `_NATIVE_LIMIT_FIELD_CEILINGS` (`bank_statements.py:1040-1053`) is a flat name→ceiling table and `_deserialize_native_limits` (`:1065-1084`) validates by key-set equality, so new int fields are covered without touching the launcher or `native_worker._parse_arguments` (`native_worker.py:173-209`). |
| `tests/test_receipt_viewer.py` | modify | Repair the vacuous browser guard; extend the 19-case fail-closed parametrize. | Read `:90-113` (parametrize), `:159-160` (prior-HTML-preserved assertion), `:188` (`importorskip`). |
| `tests/test_reimbursement_cli.py` | modify | Stage ordering and `refresh_kwargs` equality are pinned and will fail. | Read `:200-340`. `:237-246` pins exact `refresh_kwargs`; `:310` pins `['refresh','report']`. The new stage must be its own call, never a new kwarg. |
| `.github/workflows/ci.yml` | modify | Repair the red browser gate. | Read all 3 jobs: `lint-type-test` (`:22`, `:32-33`) collects the test with no browser; `shared-workflow` (`:50-51`, `:81`) has a browser but is path-filtered; `windows-native-sandbox` (`:111`, `:115-117`) is path-filtered to the native suites. |
| `pyproject.toml` + `uv.lock` | extend | New optional extra for the imaging dependency. | `:7-15` core deps must not grow. CI installs `--locked` in all three jobs (`:22`, `:50`, `:111`) — an unrefreshed lock fails every job before a test runs. |
| `CLAUDE.md`, `docs/receipt-viewer.md`, `docs/loading-receipts.md`, `README.md` | modify | The "prepared offline" and "never downloads" wording needs a refresh-time carve-out. | `CLAUDE.md:183-184`; `docs/receipt-viewer.md:24-27`, `:81-85`; `docs/loading-receipts.md:321-380`. |

---

## 5. New Components

| Module | Responsibility |
|---|---|
| `pta_finance/receipt_geometry.py` | The single definition of the **Python-side** page-normalization limits (long edge 2200, JPEG q85, the PNG→JPEG threshold) and the box padding, imported by every producer rather than restated. It deliberately does **not** own the viewer's ellipse inflation: that geometry is JavaScript at `pta_finance/reports/templates/receipt_viewer.js.j2:46-47` (`rx = min(w*.60 + .006, …)`, `ry = min(h*.72 + .003, …)`) and is rendered in the browser, so a Python constant cannot be its source of truth. Instead this module records the same numbers as the documented mirror of that template, and a test asserts the two agree by parsing the template — so a change to either side fails CI. (The private backfill scripts that also restate the padding are untracked and out of scope; no step edits them.) |
| `pta_finance/receipt_assets.py` | Transport only; full signature in § 5A. Exact-or-suffix hostname allowlist, HTTPS only, `read(cap + 1)` so oversize is detected rather than truncated, magic-byte sniffing that decides the type (`Content-Type` never trusted), content-addressed cache keyed by the digest of the bytes, per-asset failure recorded and isolated. Bounded by config's `max_asset_mib`; the page-budget constants belong to `receipt_link`. Signature in § 5A. |
| `pta_finance/receipt_pages.py` | Asset → display pages. PNG/JPEG pass through normalization; PDF is delegated to the LPAC worker. Deterministic: EXIF transpose, downscale only when over the limit, alpha-flatten, re-encode. Records raw/display size, orientation, scale, digest. Writes progress **per page**, never once at the end. Carries the invariant that uniform scaling preserves fractional box geometry while rotation destroys it. |
| `pta_finance/receipt_link.py` | Provenance and merge; full signatures in § 5A, including the `mail_root` that carries the archive for the ticket->.eml join. `propose` builds the proposals document and `apply` merges. Owns the unambiguity gate, the refusal rules, the budget pre-check, and self-validation through the production `load_receipts` + `build_report` before any replacement. |
| `<bundle>.receipt-proposals.json` | Private, gitignored, per-ticket staging — the only surface an operator hand-edits. Specified inline below. |
| `<cache>/fill-ledger.json` | Private fetch provenance (URL, digest, timestamp, outcome) and the per-asset triage surface behind the run's aggregate counts. Kept out of the sidecar, whose `pages[]` objects require the exact key set `{id, path, sha256, label}` and cannot carry a URL. |

### The proposals document, specified

Read with `object_pairs_hook` so a duplicated JSON key is a refusal, and validate every object
against an **exact** key set. All values below are fictional.

```json
{
  "document_type": "receipt-proposals",
  "schema_version": 1,
  "generated_at": "2026-01-02T03:04:05Z",
  "bundle_path": "reports/output/reimbursement-report.json",
  "bundle_sha256": "<64 lowercase hex of the bundle file at propose time>",
  "page_root": "receipt-pages/proposed",
  "tickets": [
    {
      "review_key": "submission:v1:<64 hex>",
      "ref": "EX-01",
      "display_order": 1,
      "asset_count": 2,
      "auto_eligible": false,
      "assets": [
        {"asset_id": "asset:v1:<64 hex A>", "ordinal": 1, "origin": "form-upload",
         "canonical_key": "cdn.example-forms.invalid/u/abc"},
        {"asset_id": "asset:v1:<64 hex B>", "ordinal": 2, "origin": "form-upload",
         "canonical_key": "cdn.example-forms.invalid/u/def"}
      ],
      "candidates": [
        {"page_id": "EX-01-a1-p1", "asset_id": "asset:v1:<64 hex A>", "asset_page": 1,
         "path": "receipt-pages/proposed/EX-01-a1-p1.jpg", "sha256": "<64 hex>",
         "label": "Ticket EX-01 · upload 1 · page 1"},
        {"page_id": "EX-01-a2-p1", "asset_id": "asset:v1:<64 hex B>", "asset_page": 1,
         "path": "receipt-pages/proposed/EX-01-a2-p1.jpg", "sha256": "<64 hex>",
         "label": "Ticket EX-01 · upload 2 · page 1"}
      ],
      "items": [
        {"item_key": "<exact item key from the bundle>", "source_index": 1,
         "item_sha256": "<receipt_viewer.item_fingerprint(ticket, item)>",
         "confirmed_page_ids": []}
      ]
    }
  ]
}
```

An item is **confirmed** when `confirmed_page_ids` is a non-empty list of page ids drawn from that
same ticket's `candidates`. `link-receipts confirm` merges exactly those, as regions with
`box: null`; every other item is left alone. Labels are ordinal-only — never a source filename,
URL, vendor, requestor or line-item description, because `pages[].label` reaches the rendered HTML
and the viewer's `img alt`.

**It can never be loaded as a sidecar, in either direction.** `receipt_viewer._object` requires the
top-level key set to *equal* `{schema_version, pages, items}`; this document's key set is different
(it shares `schema_version` but carries no top-level `pages` or `items`), so `load_receipts` raises
on its first check —
asserted by a regression test that feeds the committed fictional example to the production loader.
Symmetrically, `link-receipts confirm` refuses a document carrying the sidecar's key set with a
named error, so a swapped `--proposals`/`--sidecar` pair fails loudly rather than half-merging.

---

## 5A. Interfaces, identifiers, and configuration

Everything a zero-context implementer needs that the prose above assumes. All example values are
fictional; the real allowlist and archive paths live only in gitignored `config.toml`.

### Dependencies

| Choice | Value | Why |
|---|---|---|
| Imaging | new extra `receipts = ["pillow>=11"]` in `pyproject.toml` | Pillow performed every measurement in § 2; no other dependency is added. Never a core dependency. |
| HTTP | stdlib `urllib.request` | No new dependency, and this lane needs low-level control the convenience clients hide: redirects disabled per-hop, a capped `read(n+1)`, and no implicit retry. |
| PDF raster | reuse `pypdfium2>=5.13.0` from `[slides]` | Already present; adding a second raster library would duplicate the shape it decodes. |

### `[receipt_assets]` config block

Optional. Absent ⇒ the fill stage is a no-op and behavior is byte-identical to today. Present but
malformed ⇒ `ConfigError`, never a silent skip. Parsed by `_load_receipt_assets(raw)` following
`_load_receipt_mapping` (`config.py:276-301`) exactly: `None` for absent, `ConfigError` for
non-dict, paths through `_resolve_path`. Paths are resolved, never read, at config time.

```toml
# config.example.toml — FAKE values only; the real hosts are private
[receipt_assets]
enabled        = true                                    # bool, required
allowed_hosts  = ["cdn.example-forms.invalid",           # exact host
                  ".uploads.example-forms.invalid"]      # leading dot = label-boundary suffix
cache_dir      = "reports/output/.receipt-cache"         # path, required
pages_dir      = "reports/output/receipt-pages/auto"     # path, required
mail_root      = "mail_samples"                          # path, required when enabled = true
max_asset_mib  = 25                                      # int 1..25, default 25
timeout_s      = 30                                      # int 1..120, default 30
max_redirects  = 3                                       # int 0..5,   default 3
max_parallel   = 4                                       # int 1..8,   default 4
max_attempts   = 3                                       # int 1..5,   default 3
```

`max_asset_mib` is its **own** bound, not `receipt_viewer.MAX_PAGE_BYTES`: those two constants cap
rendered *pages*, and a 24 MiB source PDF can legitimately yield pages well under the page cap. The
25 MiB ceiling matches the worker's own `max_pdf_bytes` so a fetched asset can never exceed what the
renderer will accept. Backoff is `2**attempt` seconds, capped at 30.

Accepted asset types are exactly three magic-byte signatures — `%PDF-`, `\x89PNG\r\n\x1a\n`,
`\xff\xd8\xff`. Everything else is refused; `Content-Type` is never consulted.

### Module signatures

```python
# receipt_assets.py
@dataclass(frozen=True)
class AssetResult:
    url: str                 # the requested URL (never printed, never enters the sidecar)
    canonical_key: str       # normalized identity; see below
    asset_id: str            # "asset:v1:" + sha256 of the fetched BYTES
    media_type: str          # "pdf" | "png" | "jpeg", decided by magic bytes
    path: Path | None        # cache file, or None when the fetch failed
    byte_count: int
    outcome: str             # "fetched" | "cached" | "refused" | "unreachable" | "oversize" | "gone"
    detail: str              # short reason; a path or status class, never a URL or vendor string

def fetch_assets(urls, *, allowlist, cache_dir, max_bytes, timeout,
                 max_redirects, max_parallel, max_attempts) -> list[AssetResult]: ...

# receipt_pages.py
@dataclass(frozen=True)
class PageImage:
    page_id: str; asset_id: str; asset_page: int
    path: Path; sha256: str; label: str
    width: int; height: int; scale: float

def to_pages(asset: AssetResult, *, pages_dir: Path) -> list[PageImage]: ...

# receipt_link.py
def propose(report, assets, pages, *, mail_root: Path) -> Proposals: ...
def apply(sidecar_path: Path, report, confirmed: Proposals) -> MergeResult: ...
```

`propose` takes `mail_root` explicitly — that is the parameter carrying the mail archive the
`review_key` → `.eml` join reads. It is not discovered and never defaults silently.

### Identifiers

| Identifier | Format | Generated by | Used by |
|---|---|---|---|
| `review_key` | `submission:v1:<64 lowercase hex>` — sha256 of the **raw** `Message-ID` header after `.strip()`, **never** the normalized form (`normalize_message_id` lowercases the domain and feeds a different field); or `legacy:v1:<ref>[:<form>]` | `_review_key` at `reimbursement_pipeline.py:140-142`, called at `:266`/`:273` | the ticket↔`.eml` join; sidecar item entries |
| `item_key` | opaque stable string minted by the pipeline per review line; treated as an exact token, never parsed | `reimbursement_pipeline` | refusal keys, proposals items, sidecar entries |
| `source_index` | 1-based index of the line **as submitted on the form**, not its list offset | the bundle | proposals ordering; never used to guess an asset |
| `asset_id` | `asset:v1:<64 lowercase hex>` — sha256 of the **fetched bytes** | `receipt_assets` | cache filename stem, page provenance |
| `canonical_key` | `<lowercased host><path>`, query string and fragment discarded | `receipt_assets` | the one-asset gate (so two CDN size-variants of one file count once), and the ledger's per-asset row |
| `page_id` | `<ticket ref>-a<asset ordinal>-p<page number>`, unique within the sidecar | `receipt_pages` | the proposals↔sidecar join key |
| `item_sha256` | 64 lowercase hex from `receipt_viewer.item_fingerprint(ticket, item)` — **called, never re-derived** | `receipt_viewer.py:28` | staleness detection |
| cache filename | `<asset_id hex>.pdf` / `.png` / `.jpg` — extension from magic bytes | `receipt_assets` | idempotent re-fetch |

`asset_id` and `canonical_key` answer different questions and both exist deliberately: identity of
*bytes* (dedup the cache, prove provenance) versus identity of *the upload* (count a ticket's
distinct receipts). Neither substitutes for the other.

### The auto-commit rule, stated exactly

A ticket is auto-eligible when **all** of these hold; otherwise it goes to proposals:

1. Its receipt assets — the submission's upload URLs; MIME attachments are out of scope for this
   phase per § 3 — have exactly **one** distinct `canonical_key`.
2. That asset produced **at least one** page. A 404, an oversize refusal, or a PDF on a non-Windows
   host yields zero pages, so the ticket is reported unlinkable rather than auto-linked.
3. **No item on the ticket already has a sidecar entry.** Existing links are never re-linked,
   re-hashed, or touched — this is what protects the 147 human-verified entries.

When it holds, every item on that ticket gains one region **per page of that one asset**, each with
`box: null`. A three-page PDF therefore links all three pages and the viewer offers page navigation.

`box: null` from this stage means "this is the ticket's only uploaded receipt". A human-placed box
means "this exact line was identified". The two are distinguishable because only a human writes a
non-null box, and the merge refuses to overwrite one.

### The sidecar entry this stage emits

```json
{"review_key": "submission:v1:<64 hex>", "item_key": "<exact item key>",
 "item_sha256": "<64 hex>", "regions": [{"page_id": "EX-01-a1-p1", "box": null}]}
```

`pages[].path` is relative to the **sidecar's own directory** — that is what `receipt_viewer`
enforces — so a page under `receipt-pages/auto/` has `path: "receipt-pages/auto/EX-01-a1-p1.jpg"`.
The proposals document's `page_root` is advisory context for the operator and is never joined to
`path`. Only pages referenced by a merged region may enter `pages[]`; the merge prunes the rest,
because `load_receipts` refuses an unreferenced page.

### The fetch ledger

`<cache_dir>/fill-ledger.json` — private, gitignored, the per-asset triage surface behind the run's
aggregate counts, and where an unjoinable ticket is recorded.

```json
{"schema_version": 1, "updated_at": "2026-01-02T03:04:05Z",
 "assets": [{"url": "https://cdn.example-forms.invalid/u/abc?v=2", "canonical_key": "cdn.example-forms.invalid/u/abc",
             "asset_id": "asset:v1:<64 hex>",
             "outcome": "fetched", "byte_count": 128904, "attempts": 1,
             "first_seen": "2026-01-02T03:04:05Z", "last_seen": "2026-01-02T03:04:05Z",
             "detail": ""}],
 "unjoinable_tickets": [{"review_key": "legacy:v1:P-900", "reason": "no archived message for this key"}]}
```

`outcome` is exactly the `AssetResult.outcome` vocabulary. The ledger holds URLs because it is
private; nothing from it reaches the sidecar or stdout.

**Ownership:** `receipt_assets` writes `assets[]`; `receipt_link` writes `unjoinable_tickets[]`. Both
read-modify-write the whole file under the exact key set above, so each must emit the other's key
(as an empty list) when it creates the file. Rows are keyed by `(canonical_key, asset_id)` — a
canonical key can legitimately carry more than one byte-digest across CDN variants — and a re-run
updates `last_seen`/`attempts` in place rather than appending a duplicate.

### Errors and the raise-vs-record boundary

Two exception types: `ReceiptAssetError` (transport and validation) and `ReceiptLinkError` (merge
refusals), each following the message discipline of `gmail_source` — paths and remediation only,
never a URL, requestor, vendor or subject.

**Recorded, never raised:** any per-asset outcome — unreachable, oversize, wrong magic bytes,
disallowed redirect target, 404. One bad asset never aborts the batch.
**Raised, aborting the stage before `build_report`:** malformed config, an unwritable cache or pages
directory, and every merge refusal (unknown key, stale fingerprint, attempted override of a
human-verified box, duplicate entry, budget crossing). A merge refusal aborts the **whole merge** —
never a partial sidecar.

### The LPAC render request and response

Both sides validate by exact key-set equality, so the field names are part of the contract. Render
parameters ride the existing limits envelope as plain ints: `render_page_first`,
`render_page_count`, `render_scale_permille`, `max_render_raw_bytes_per_page`,
`max_render_wire_bytes`. The response is ASCII JSON:

```json
{"status": "rendered",
 "pages": [{"page_number": 1, "width": 1700, "height": 2200, "format": "gray8",
            "raw_sha256": "<64 hex>", "raw_length": 3740000, "zlib": "<base64>"}]}
```

Grayscale is deliberate: receipts are read for their *text*, the viewer draws its own red outline
over the page, and gray8 is a third of BGRA on the wire — which matters against a 16 MiB frame cap
and a 10-second worker CPU limit. Color is not evidence here; legibility is.

### Development commands

```bash
uv sync --extra dev --extra slides --extra receipts   # add --extra web for the full gate
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict pta_finance
uv run python scripts/check_no_identity.py            # the identity guard referenced in § 6.8
```

The full-suite DONE gate additionally needs the Firestore emulator for the `web` suites — start it
per [docs/shared-workflow-proof.md](shared-workflow-proof.md) and export `FIRESTORE_EMULATOR_HOST`;
those test modules fail loudly rather than skipping when `web` is installed without it.

---

## 6. Design Decisions

**6.1 The stage produces pages, never locations.** The policy sentence in `CLAUDE.md:183-184`
has two clauses. *"the toolkit never OCRs, downloads, or auto-matches at render time"* is
time-scoped and survives intact — rendering still touches no socket. *"Locations are prepared
offline by an operator or assistant"* is not time-scoped, and this feature must amend it in the
same change to say that **pages** may be acquired during a refresh while **locations** remain
operator-prepared. Amending it silently, or ignoring it, would leave the repo asserting something
false about itself.

**6.2 The auto-commit gate is one asset per ticket.** Stated exactly, with its two guard
conditions, in § 5A; the reasoning is here. When a ticket has exactly one uploaded receipt asset,
"this asset is this ticket's receipt" is a provenance fact taken from the requestor's own upload
field, not an inference from an amount or a description. Every item on that ticket links to every
page of that one asset with `box: null`, which the viewer already renders as a source page with an
explicit no-outline explanation. The two guards exist because the fact fails quietly otherwise: a
ticket whose one asset produced zero pages has no provenance to assert, and a ticket carrying an
existing link must never have human-verified evidence overwritten by a machine. When a ticket has two or more
assets, the binding genuinely is a guess: the 2026-09-14 backfill measured upload order diverging
from form row order (one six-item ticket mapped to uploads 4, 5, 3, 2, 1, 6), and aggregate rows
fan out across several receipts. Those tickets go to proposals. Asset identity is normalized on
host plus path with query strings discarded, because the form CDN serves size variants of one
file under different query strings.

**6.3 PDFs go through the LPAC worker.** A requestor-uploaded PDF is attacker-influenced input,
and this project already built capability-level isolation for exactly that class of risk. The
alternative considered was pypdfium2 in a capped subprocess — cross-platform, roughly a third of
the work, and it would run in CI — but it grants a pdfium exploit an ordinary user-level process
holding the bundle and Sheets credentials, which is a clear step down from the bar set for bank
statements. The LPAC route costs more and is Windows-only; on other hosts PDFs are cached,
counted, and reported as needing manual export. The existing `[summary]` carve-out
(`documentation/reimbursement-board-summary-plan.md:303-308`, *"does not open external receipt or
bank PDFs"*) is amended deliberately in Step 48, which owns every doc amendment, not quietly. If
Step 41's spike records a blocked verdict, Step 42 supersedes this decision and
`documentation/findings/step-42-pdf-boundary.md` becomes authoritative for Steps 43–44.

**6.4 The render protocol rides the existing limits envelope.** `_NATIVE_LIMIT_FIELD_CEILINGS` is
a flat name→ceiling table and `_deserialize_native_limits` validates by exact key-set equality, so
new integer fields are covered without touching the launcher or the worker's argument parser. The
worker emits **raw grayscale pixels plus zlib**, not PNG: the broker then validates with a
bounded `decompressobj().decompress(data, max_length=W*H)` and an empty-leftover assertion, which
is decompression-bomb-proof, and re-derives the PNG itself. Emitting PNG from inside the boundary
would instead force the broker to parse untrusted PNG chunks and allowlist ancillary chunks to
close the `tEXt`/`zTXt`/`iTXt`/`eXIf` covert channel — more attack surface, not less, and it
would break the boundary's central invariant that the broker re-derives everything the worker
sends. Note two traps found during verification: pdfium pads bitmap rows, so the buffer must be
copied row by row rather than wholesale; and `_page_dimensions` computes rendered pixels at a
hardcoded 300 DPI and hard-rejects anything that is not US Letter, so the render path needs its
own dimension gate and cannot reuse the statement extraction path.

**6.5 The budget check happens before the write, not inside the loader.** Today, crossing
`MAX_TOTAL_BYTES` raises an opaque error inside `load_receipts` — *after* `refresh_bundle` has
already rewritten the bundle, leaving the run at exit 1 with a stale HTML and no clear cause. The
fill stage computes projected totals up front and refuses with the crossing page and the
remaining budget named. Measured: fetching every currently-unfetched asset lands at 94–96% of the
cap with the current encoding, and at roughly 79 MiB with the new pages normalized — so
normalization of new pages alone is sufficient, and the existing 164 pages stay untouched.

**6.6 Normalization defaults to long edge 2200 / JPEG q85.** Measured across all 164 live pages
rather than a sample: it saves 39% of bytes with *identical* fine-print line geometry, because
only 7 pages exceed a 2200 long edge — it is a transcode, not a downscale. The tighter variants
save more but push the worst 5% of pages under 8 px of text height, which is the practical floor
for reading a receipt line without zooming.

**6.7 Proposals cannot be loaded as a sidecar, in either direction.** `receipt_viewer._object`
compares exact top-level key sets, so a proposals document raises on its first check. The confirm
verb refuses a document carrying the sidecar's key set with a named error, so a swapped
`--proposals`/`--sidecar` pair fails loudly rather than half-merging. A regression test feeds the
committed fictional proposals example to `load_receipts` and asserts it raises.

**6.8 Printed output stays aggregate-only.** No URL, requestor, vendor, subject or filename may
reach stdout; existing CLI tests already assert this shape and the identity guard cannot see
inside a PNG or a base64 `data:` URI. Cache files are named from the digest of their bytes, never
from the URL or the original filename, so no vendor filename lands on disk or in a page `path`.

---

## 7. Build Steps

<!-- autofix-applied: 2026-09-16 -->
### Step 38: Repair the red CI browser gate
- **Problem:** `tests/test_receipt_viewer.py:188` guards on `importorskip("playwright.sync_api")`, but Playwright ships in the `dev` extra so the import always succeeds. The only job that collects the test (`lint-type-test`) never runs `playwright install`, so `chromium.launch()` raises and `main` CI has been red since `7eb0c4f`. Make the guard check for a browser executable, and give the browser test a job that actually has one.
- **Type:** code
- **Issue:** #72
- **Flags:** `--reviewers code`
- **Produces:** guard fix in `tests/test_receipt_viewer.py`; `.github/workflows/ci.yml` change so the receipt-viewer browser test runs in a browser-equipped job
- **Done when:** all three CI jobs are green on a PR branch; the browser test is observed **executing** (not skipped) in exactly one job, and observed **skipping cleanly** when no browser binary is present
- **Depends on:** none
- **Status:** DONE (2026-09-23)
- **Evidence:** PR #93 run 35932478654 passed all three jobs; lint asserted two skips and shared-workflow asserted the same two cases executed. The full post-merge local suite passed 1,253 tests with 3 expected skips.

<!-- autofix-applied: 2026-09-16 -->
### Step 39: Shared page geometry and deterministic normalization
- **Problem:** Create `receipt_geometry.py` as the one definition of the **Python-side** normalization limits and box padding — it does *not* own the viewer's ellipse inflation, which is JavaScript in a template (§ 5); it mirrors those numbers and a test asserts the two agree by parsing the template. Then build `receipt_pages.py`'s image half: EXIF transpose, downscale only above a 2200 long edge, alpha-flatten, JPEG q85 re-encode, per-page progress. PDF handling is out of scope for this step.
- **Type:** code
- **Issue:** #73
- **Flags:** `--reviewers code`
- **Produces:** `pta_finance/receipt_geometry.py`, `pta_finance/receipt_pages.py` (image path), `tests/test_receipt_pages.py`; **`pta_finance/receipt_viewer.py`** — export the page-budget constants and a `headroom_bytes(sidecar_path)` helper so producers import rather than redefine them (`load_receipts` and `item_fingerprint` are unchanged); the `receipts` extra in `pyproject.toml` with `uv.lock` refreshed; `.github/workflows/ci.yml` — every job installs a **fixed** `--extra` list, so a new extra that no job names leaves the new tests running without their dependency
- **Done when:** byte-identical output across two runs on the same input; a rotated-EXIF fixture normalizes to displayed orientation; the geometry constants are asserted with `is`, not `==`, so re-duplication fails CI; the `lint-type-test` job installs the new extra and `tests/test_receipt_pages.py` is observed **executing, not skipped**, in that job (a module-level `importorskip` that leaves the module uncovered in every CI job is not an acceptable resolution — mirror the existing zero-skip assertion at `ci.yml:85-91`); full suite green
- **Depends on:** 38

<!-- autofix-applied: 2026-09-16 -->
### Step 40: Allowlisted asset fetcher with a content-addressed cache
- **Problem:** Create `receipt_assets.py`: HTTPS-only fetch restricted to an exact-or-suffix hostname allowlist, `read(cap + 1)` oversize detection, magic-byte type sniffing with `Content-Type` ignored, digest-named cache, per-asset failure isolation, and a private fetch ledger. The asset fetch cap is `max_asset_mib` from config (§ 5A), **not** `receipt_viewer.MAX_PAGE_BYTES` — those cap rendered pages, and a 24 MiB source PDF can yield pages well under the page cap. The page caps are imported (never redefined) by `receipt_link`, which owns the budget pre-check. The allowlist is the whole safety property of this lane, so close its four documented escapes explicitly: (a) **redirects** — an allowed host that 30x-redirects elsewhere defeats the check, so disable automatic redirect following and re-run the full allowlist check against every hop, capped at a small hop count; (b) **suffix matching must respect label boundaries** — a `.example.net` suffix rule must not match `evil-example.net`; (c) **port and scheme** — HTTPS on the default port only, and reject userinfo-bearing URLs (`https://allowed@evil/`); (d) **IP-literal hosts** rejected outright rather than resolved. Add bounded politeness: a small concurrency limit, a retry with backoff on 429/5xx with a hard attempt ceiling, and a documented note that upload URLs on a third-party CDN may expire — a 404 is a link-rot finding, not a bug.
- **Type:** code
- **Issue:** #74
- **Flags:** `--reviewers deep`
- **Produces:** `pta_finance/receipt_assets.py`, `tests/test_receipt_assets.py` (local HTTP fixture — no real network)
- **Done when:** tests cover allowlist rejection, non-HTTPS rejection, oversize at cap+1, an SVG and a text body served as `image/png` both rejected, cache hit on re-fetch, and one failing asset not aborting the batch; plus one test per escape above — a redirect from an allowed host to an unlisted one is refused, `evil-example.net` does not match the `.example.net` rule, a userinfo-bearing URL is refused, an IP-literal host is refused, and a 429 retries with backoff then gives up at the ceiling; no test opens a socket to a remote host. **The transport seam is a known trap:** the fetcher is HTTPS-only on the default port, so a loopback `http.server` is unreachable by construction and `cryptography` (for a self-signed cert) is not installed in the job that runs these tests. Test through a module-private opener seam the tests substitute — **never by allowlisting `http://` on loopback "for testability", which guts the step's entire safety property while leaving every test green.** The `--reviewers deep` pass must check this specifically.
- **Depends on:** 38

<!-- autofix-applied: 2026-09-16 -->
### Step 41: LPAC render spike — prove glyphs actually rasterize
- **Problem:** pdfium rasterizes on CPU with no GPU or Skia, and the LPAC can read `C:\Windows\Fonts`, but pdfium's Win32 font mapper reaches fonts through GDI/win32k and the worker is launched with no window-station grant. Text *extraction* needs no glyph outlines; *rendering* does. A degraded mapper yields blank or boxed text — silently wrong output that no fake-backend unit test can see. Prove a real LPAC render of a fixture PDF with non-embedded fonts produces legible glyphs before funding the protocol work.
- **Type:** code
- **Issue:** #75
- **Flags:** `--reviewers code`
- **Produces:** a fictional non-embedded-font fixture PDF; `tests/test_treasurer_slides_lpac_render_spike.py` added to the `windows-native-sandbox` job's pytest file list in `.github/workflows/ci.yml` (that job runs a hardcoded two-file list, so a new test file it does not name never executes); and `documentation/findings/step-41-lpac-render.md` recording the measured verdict in every case — plus `documentation/findings/step-41-lpac-render-blocked.md` on the failure verdict only
- **Done when:** the `windows-native-sandbox` CI job renders the fixture inside the **real** LPAC — never a mocked call — and asserts the *measured* ink coverage and glyph-shape similarity against a reference. **The step is DONE on either verdict with the full suite green:** above threshold, the test asserts the pass; below threshold (blank or box glyphs), the same measurement is asserted as the blocked verdict rather than left as a red test, and the step also writes the blocked findings file on the branch that merges, so Step 42's predicate can observe it. A spike that cannot produce a measurement at all is still a failure.
- **Depends on:** 38

<!-- autofix-applied: 2026-09-16 -->
### Step 42: Record the PDF-boundary decision when the LPAC render path is blocked
- **Problem:** If Step 41 recorded a blocked verdict, decide and record the fallback PDF rasterization boundary — a capped, short-lived subprocess — as a findings file that Steps 43–44 read at build time. This step must NOT try to amend this plan's later step briefs: `/build-phase` parses every step once at Step 0 and does not re-read the plan mid-run, so an in-place plan edit would never reach Step 43's dispatch. The decision travels as a file instead.
- **Type:** conditional
- **Condition:** `test -s documentation/findings/step-41-lpac-render-blocked.md`
- **Issue:** #76
- **Flags:** `--reviewers code`
- **Produces:** `documentation/findings/step-42-pdf-boundary.md` — the authoritative boundary decision read by Steps 43 and 44
- **Done when:** the findings file names the exact isolation mechanism, its wall-clock / memory / page-count caps, precisely what an exploit of the PDF parser would reach under it, and how the non-Windows path differs; § 6.3 of this plan is annotated with a pointer to the file (a documentation edit, not a step-brief edit)
- **Depends on:** 41

<!-- autofix-applied: 2026-09-16 -->
### Step 43: Render protocol across the attested boundary
- **Problem:** Implement the PDF rasterization boundary recorded in `documentation/findings/step-42-pdf-boundary.md`; **when that file is absent, the boundary is the LPAC worker per § 6.3** and this brief applies as written. Extend the worker's limits envelope with render parameters, add the render request branch and the raw-grayscale-plus-zlib response, and add broker-side validation: bounded decompression with an empty-leftover assertion, exact length check, digest check, then broker-side PNG encoding. The render path needs its own page-dimension gate — it cannot reuse the statement path, which rejects anything that is not US Letter and requires a minimum non-whitespace character count. **Coordinate with [treasurer-summary Wave 1](treasurer-summary-wave-1-plan.md) Step 16 (issue #43, still PENDING)**, which claims the same three worker files and `ci.yml` for its own bounded-Tesseract path — rebase and re-run both suites if it has landed; if it has not, land this first and leave its brief a note.
- **Type:** code
- **Issue:** #77
- **Flags:** `--reviewers deep`
- **Produces:** render limits, request/response, and validation in `pta_finance/treasurer_slides/bank_statements.py`; tests in `tests/test_treasurer_slides_bank_statements_native.py`
- **Done when:** the Windows CI job renders a multi-page fictional fixture end-to-end through the real LPAC; a malformed, bomb-shaped, wrong-length, or digest-mismatched response is refused with a named error; the existing statement-extraction tests are unchanged and green; the row-stride copy is covered by a test using a width that forces padding
- **Depends on:** 41, 42

<!-- autofix-applied: 2026-09-16 -->
### Step 44: PDF assets become display pages
- **Problem:** Wire the rasterization boundary from Step 43 — the LPAC worker, or whatever `documentation/findings/step-42-pdf-boundary.md` records if it exists — into `receipt_pages.py` so a cached PDF asset yields normalized page images, with a page-count ceiling and per-page progress. On non-Windows hosts, fail closed before any PDF byte is read and report the assets as needing manual export.
- **Type:** code
- **Issue:** #78
- **Flags:** `--reviewers code`
- **Produces:** PDF path in `pta_finance/receipt_pages.py`; tests covering multi-page output, the page ceiling, and the non-Windows fail-closed path
- **Done when:** a fictional 3-page PDF fixture produces 3 normalized pages on Windows and a clean, counted refusal elsewhere; output is byte-identical across two runs
- **Depends on:** 39, 43

<!-- autofix-applied: 2026-09-16 -->
### Step 45: Provenance, the unambiguity gate, and the sidecar merge
- **Problem:** Create `receipt_link.py`. **It owns the ticket→receipt-URL join, which nothing else does today:** the bundle keeps no receipt pointer (§ 2), so for each ticket this module re-derives the submission's upload URLs by matching `review_key` back to its `.eml` in the configured mail archive — `review_key` is the only durable join, and it hashes the **raw** stripped `Message-ID` header (§ 5A) — and reads `Submission.receipt_urls`, which `receipt_ingest._extract_receipt_urls` already parses. A legacy ticket with no message-derived key has no automatic join and is reported as such, never guessed. Then: build the proposals document for every ticket, auto-commit `box: null` regions only for single-asset tickets, and merge confirmed entries into the sidecar. Hard refusals with no silent skip: unknown `(review_key, item_key)`, overriding an entry that already carries a human-verified box, duplicates, stale fingerprints, and any merge that would cross the page budget. Call `receipt_viewer.item_fingerprint`; never re-derive it. Self-validate through the production `load_receipts` and `build_report` before replacing anything, and back up the prior sidecar. **Regenerating proposals must preserve operator edits:** a rewrite merges into the existing file by `(review_key, item_key)` rather than truncating it, and refuses rather than discarding an edit it cannot reconcile.
- **Type:** code
- **Issue:** #79
- **Flags:** `--reviewers deep`
- **Produces:** `pta_finance/receipt_link.py`, `tests/test_receipt_link.py`, a committed fictional proposals example, and the additive accessor in `pta_finance/receipt_ingest.py` that exposes an already-parsed `Submission` by its archived message without widening the frozen `source_receipt_urls_v1` digest surface
- **Done when:** § 5A's three auto-commit conditions each have a test — a one-asset ticket auto-links, a two-asset ticket does not, a one-asset ticket whose asset produced zero pages does not, and a ticket with any already-linked item does not; a ticket whose `review_key` resolves to no archived message is reported unjoinable rather than skipped silently; regenerating proposals over a hand-edited file preserves every operator edit, and an irreconcilable edit refuses instead of overwriting; each refusal class has a test asserting the sidecar is byte-unchanged afterward; the fictional proposals example fed to `load_receipts` raises; the budget refusal names the crossing page and the remaining bytes
- **Depends on:** 40, 44

<!-- autofix-applied: 2026-09-16 -->
### Step 46: The refresh stage, config block, and `link-receipts` verbs
- **Problem:** Insert the fill stage between the refresh summary and `build_report`, add the `[receipt_assets]` config block per § 5A, and add these exact CLI surfaces: on `update-reimbursements`, the mutually-exclusive pair `--fill-receipts` / `--no-fill-receipts` (**default: on when `[receipt_assets]` exists with `enabled = true`, off otherwise**) plus `--fill-receipts-offline` (serve from cache only; never opens a socket); and a `link-receipts` verb group with exactly `audit`, `propose`, and `confirm`. Keep exit codes in {0, 1}; keep printed output aggregate-only; keep `--dry-run` writing nothing and opening no socket. The new stage is its own call — never a new `refresh_kwargs` key, which is pinned by test.
- **Type:** code
- **Issue:** #80
- **Flags:** `--reviewers code`
- **Produces:** `pta_finance/cli.py` stage and subparsers, `pta_finance/config.py` block, `config.example.toml` with fake hosts, updated `tests/test_reimbursement_cli.py`
- **Done when:** absent config is a no-op with byte-identical output to today; a malformed block raises `ConfigError` rather than skipping; `--dry-run` writes nothing and opens no socket; stage ordering and `refresh_kwargs` equality tests pass in their updated form; no URL, filename, vendor or requestor appears in any printed line; a partial fetch (some assets fail) prints an aggregate failure count **and names the private ledger path as the triage surface**, so aggregate-only output never leaves the operator with no way to diagnose which asset failed and why; `link-receipts audit` reports per-asset outcomes from that ledger
- **Depends on:** 45

<!-- autofix-applied: 2026-09-16 -->
### Step 47: Integration through the production caller
- **Problem:** Prove the wiring, not just the parts. Drive `cli.main(["update-reimbursements", ...])` end-to-end against a local fixture source and assert the new link reaches the rendered HTML. Add the producer→consumer round trip on the `page_id` join key, extend the fail-closed parametrize with the classes the fill stage can produce, and add the fingerprint blast-radius test.
- **Type:** code
- **Issue:** #81
- **Flags:** `--reviewers code`
- **Produces:** integration tests in `tests/test_reimbursement_cli.py` and `tests/test_receipt_viewer.py`
- **Done when:** the end-to-end test asserts a sidecar entry was created, the rendered HTML shows `View source receipt` for that item, and the embedded item map grew by exactly one; the blast-radius test mutates one ticket-level evidence field and asserts **every** item on that ticket loses its link; the prior-HTML-preserved assertion still holds for each new failure class
- **Depends on:** 46

<!-- autofix-applied: 2026-09-16 -->
### Step 48: Documentation, policy amendments, and the full-suite gate
- **Problem:** Amend every sentence this feature makes false, in lockstep: the "prepared offline" clause, the "never downloads during rendering" wording plus its new refresh-time carve-out, the external-receipt-PDF carve-out in the board-summary plan, the operator guide's rebuild instructions, and the command lists. Refresh `uv.lock` and run the full suite.
- **Type:** code
- **Issue:** #82
- **Flags:** `--reviewers code`
- **Produces:** updates to `CLAUDE.md`, `docs/receipt-viewer.md`, `docs/loading-receipts.md`, `README.md`, `documentation/reimbursement-board-summary-plan.md`, refreshed `uv.lock`
- **Done when:** the **full** suite runs — every declared test root with dev, slides, web and the new imaging extra installed and the Firestore emulator running, not a subset — with collected count at or above **the baseline measured on this phase's merge-base at phase start and recorded in the step checkpoint**, not a number copied from an older phase (CLAUDE.md's 1,231 is stale: the non-web suites alone collect 1,018 as of 2026-09-16, so a hard-coded 1,231 would let a change that deletes tests pass); strict mypy, Ruff lint and format, the identity guard, and all three CI jobs pass; no committed file contains a real host, organization, person or email
- **Depends on:** 47

---

## Manual UAT

The eleven steps above are automated (`Type: code` / `Type: conditional`) and `/build-phase` walks
them unattended. The single step below is attended and is the phase's operator boundary — the
orchestrator halts before it by design.

<!-- autofix-applied: 2026-09-16 -->
### Step 49 (M9): Attended run against the real private evidence
- **Problem:** Run the real end-to-end refresh on the live private bundle with the fill stage enabled, then complete the confirm round-trip for the ambiguous tickets, and verify the four items that arrived 2026-09-13 open their source receipts.
- **Type:** operator
- **Issue:** #83
- **Done when:** all of the following hold — (a) the fill stage auto-links the single-asset ticket (NEW-31) with no operator action; (b) the multi-asset ticket (NEW-32) appears in the proposals file, the operator confirms it via `link-receipts confirm`, and its items then open their pages — **the plan's own measurement says the auto-gate closes only 1 of the 4 new items, so the confirm round-trip is part of this step, not an optional extra**; (c) no previously-linked item regresses and the 147 existing links survive; (d) the sidecar validates through `load_receipts` and the rendered HTML stays under the page budget with the remaining headroom reported; (e) the operator confirms the receipts shown are the right ones — a link that opens the wrong page passes every automated check
- **Depends on:** 48

**Before running.** Three prerequisites, none created by an earlier step: (1) a `[receipt_assets]`
block per § 5A exists in gitignored `config.toml` with the **real** allowlist — no committed file
carries it, so the stage is a no-op until the operator writes it; (2) the `receipts` extra is
installed (`uv sync --extra dev --extra slides --extra receipts`); (3) Gmail one-time setup is
already in place if `--fetch-since` is used — an OAuth Desktop-app client at
`secrets/gmail-client-secret.json`, a `[gmail]` config block, and a browser consent that expires
roughly weekly by design (SETUP.md §6). To skip mail acquisition entirely, drop `--fetch-since` and
run against the existing local archive.

**Operator commands**

Choose `<YYYY-MM-DD>` to cover the arrivals under test with margin — for the 2026-09-13 tickets,
any date on or before 2026-09-01. The fetch window controls acquisition only, never report
membership.

```powershell
uv run pta-finance update-reimbursements --fetch-since <YYYY-MM-DD>
uv run pta-finance link-receipts audit
```

Then, after editing the proposals file for any ambiguous ticket:

```powershell
uv run pta-finance link-receipts confirm --proposals reports/output/reimbursement-report.receipt-proposals.json
uv run pta-finance report-reimbursements
```

**What to look for**

| Check | Expected |
|---|---|
| Fill-stage receipt lines | aggregate counts only — no URL, vendor, filename or requestor |
| Auto-linked | the single-asset ticket — `NEW-31`, one of the two tickets that arrived 2026-09-13 (see § 2) — with no operator action |
| Proposals file | one entry per multi-asset ticket, hand-editable, never auto-merged |
| Existing links | 147 preserved; zero fingerprint drift |
| Page budget | reported headroom after the merge, still under the cap |
| Opened receipt | the correct receipt for that item — verify by eye, not by count |

---

## 8. Risks and Open Questions

| Item | Risk | Mitigation |
|---|---|---|
| LPAC render effort | The single largest cost here: four of the twelve steps (41–44) with a mandatory spike — the largest single cost in this plan, for a boundary that exists but has never carried image payloads. | Steps 41–42 gate it: the spike must pass before protocol work is funded, and a conditional fallback step re-scopes to a capped subprocess if it does not. |
| Silent blank renders | pdfium's Win32 font mapper reaches fonts through GDI/win32k; the worker has no window-station grant. A degraded mapper produces blank or box glyphs — wrong output, not a crash, invisible to fake-backend tests. | Step 41's gate is a real LPAC render measured for ink coverage and glyph similarity against a reference, not a mocked call. |
| `evidence_payload` blast radius | One new field there rotates every `source_evidence_sha256` → every `item_fingerprint` → all 147 live links die and the refresh refuses outright. | Declared a hard constraint in § 4; Step 47's blast-radius test makes the coupling explicit and failing. |
| Page budget | 68% consumed before this feature adds anything; crossing it currently raises inside the loader *after* the bundle was rewritten. | Pre-check before write (§ 6.5) plus measured normalization (§ 6.6). Fetching everything currently unfetched lands near 79 MiB with new-page normalization. |
| Small measured benefit today | On current data the auto-gate closes 1 of 84 unlinked items; 69 are adjudicated unlinkable and 11 are structural. | The benefit is prospective and evidence-preserving: 4 new items appeared in 2 days, and the hosted upload URL is the only pointer to the original receipt that exists anywhere — the bundle drops it and the toolkit keeps no copy. |
| First outbound HTTP fetcher | New lane, new failure modes, new trust in a third-party CDN. | HTTPS-only, config-declared allowlist, magic-byte sniffing with `Content-Type` ignored, hard size ceilings, per-asset isolation, digest-named cache, and a cache-only offline mode. |
| Corpus-wide unfetched count is uncertain | Two mappings of tickets to forms disagree: ~50 unfetched under one, 174 under another (superseded re-submissions), and 0 among exactly-keyed tickets. | Only the 4 new items' URLs are established beyond doubt. Step 49 measures the real number on live evidence rather than committing to either estimate. |
| Confirm UX friction | A six-upload ticket is confirmed by hand-editing JSON. | Accepted for this phase; the HTML picker is reserved as Step 50 in a later phase once the CLI path has been used on a real run. |
| **Plan collision with treasurer Wave 1 Step 16** | Wave 1 Step 16 (issue #43, still PENDING) claims `bank_statements.py`, `native_sandbox.py`, `native_worker.py` and `.github/workflows/ci.yml` for its bounded-Tesseract path. Steps 43–44 touch only `bank_statements.py` of those (§ 4 keeps the launcher and worker parser unchanged), but both land in the same file and the same `windows-native-sandbox` job. Whichever lands second rebases onto a changed worker protocol. | **Resolved deterministically in Step 43's brief — the build does not halt for it.** Step 43 checks whether #43 has landed: if it has, rebase onto it and run both native suites; if it has not, land this phase first and leave a note on #43. The operator may override that order at any time, but no decision is required before dispatch. |
| Spike verdict travels as a file, not a plan edit | `/build-phase` parses steps once at Step 0 and never re-reads the plan, so Step 42 cannot amend Steps 43–44 in place. | Step 42 writes `documentation/findings/step-42-pdf-boundary.md`; Steps 43–44 read it at build time and fall back to the LPAC boundary when absent. |
| Deferred, not open | `link-receipts compact` (re-encoding the existing 164 verified pages, which roughly doubles headroom) is **decided out of scope** for this phase — no step depends on it. It rewrites human-verified evidence, so it waits until the budget actually forces it. | No action; recorded so a later phase does not re-derive the reasoning. |

---

## 9. Testing Strategy

**Pipeline smoke gate before any observation step.** This is a producer→consumer chain —
assets → pages → proposals → sidecar → rendered HTML — and every boundary is exactly where mocked
unit tests go blind. Step 47's end-to-end drive of `cli.main` with real components and no mocks is
that gate, and it precedes Step 49's attended run.

**New components are tested through their production caller.** Per the workspace rule, a unit test
of `receipt_link` alone would leave a silent-wiring failure invisible — the failure mode where a
module is built, unit-tested, and never actually invoked. Step 47 asserts the new component is
reached from `cli.main` and that its effect appears in the rendered HTML.

**Conventions to follow**, matching `tests/test_receipt_viewer.py` exactly: no pytest fixtures
(module-level helpers), no committed binary fixtures (synthesize PNG bytes in code, and emit the PDF fixtures Steps 41 and 44 need from a committed generator rather than checking in a .pdf), `tmp_path` on
every test with an assertion that the path never appears in rendered output, fake-organization
placeholders throughout, and an injection payload baked into the happy-path fixture and asserted
inert.

**Existing tests that will break, and what they become:** `test_reimbursement_cli.py:237-246`
(exact `refresh_kwargs` equality) and `:310` (stage ordering `['refresh', 'report']`) must accept
the new stage as its own call rather than a new kwarg. The 19-case fail-closed parametrize at
`test_receipt_viewer.py:90-113` gains the classes the fill stage can produce, each keeping the
assertion that the previous HTML survives untouched.

**Gate scope.** The gate that flips a step DONE runs the **full** suite — every declared test root
with dev, slides, web and the `receipts` extra installed and the Firestore emulator running — not the subset a step
iterated against. The checkpoint names which suites ran. A subset count cannot see a cross-suite
regression.

**End-to-end verification** is Step 49: a real refresh against the live private bundle, confirming
the two tickets from 2026-09-13 open their source receipts, no previously-linked item regresses,
and the rendered report stays inside the page budget.
