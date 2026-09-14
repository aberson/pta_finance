# Inspecting a source receipt

In **Reimbursement Review — Current Queue**, click an item description to open its source
receipt. The red outline identifies the receipt line or lines behind that claim. Use **+**
and **−** to zoom, **Focus item** to return to the marked lines, **Fit page** to see the whole
page, and **Previous / Next** when a claim
has more than one source page. **Escape** or **Close** returns focus to the selected item.
This also works for tickets expanded in the closed-case appendix.

**Receipt not linked** means no source page has been associated with that item yet. A linked
page without a marked location opens with an explicit explanation and no red outline.
These are evidence-reading aids; they do not change reimbursement amounts or decisions.

The HTML embeds the receipt images and works offline without a server or third-party scripts.
It contains private receipt details as well as the queue. Keep it with the other private reports.

## Preparing source locations

Locations live in an optional private JSON file beside the report bundle. Replace the bundle's
`.json` suffix with `.receipts.json`. For the default bundle this is
`reports/output/reimbursement-report.receipts.json`. Both `report-reimbursements` and the render
stage of `update-reimbursements` pick it up automatically. Existing bundles need no migration.

This version uses **verified saved locations**. It does not perform OCR, download receipts during
rendering, or automatically match newly submitted items. An operator or an assistant inspecting the
original receipt prepares the locations. For a PDF, first export each relevant page as a PNG or
JPEG; retain the original PDF privately. Do not use a reconstructed receipt or crop away its context.

The file has this structure (the hashes below must be replaced with real computed digests):

```json
{
  "schema_version": 1,
  "pages": [
    {
      "id": "receipt-1-page-1",
      "path": "receipt-pages/receipt-1-page-1.png",
      "sha256": "SHA256_OF_THE_IMAGE_BYTES",
      "label": "Example store receipt · page 1"
    }
  ],
  "items": [
    {
      "review_key": "EXACT_REVIEW_KEY_FROM_BUNDLE",
      "item_key": "EXACT_ITEM_KEY_FROM_BUNDLE",
      "item_sha256": "COMPUTED_ITEM_FINGERPRINT",
      "regions": [
        {"page_id": "receipt-1-page-1", "box": [0.12, 0.30, 0.72, 0.04]}
      ]
    }
  ]
}
```

Each box is `[left, top, width, height]`, as fractions of the full image measured from the
top-left corner. Divide horizontal pixel coordinates by image width, and vertical coordinates
by image height. Multiple regions can mark several purchases within one claim, across one or
more pages. Use `"box": null` only when the source page is verified but the location isn't.
Keep each page once in `pages` and reference it from every relevant item.

Compute an image hash with `hashlib.sha256(path.read_bytes()).hexdigest()`. Compute an item's
fingerprint from the validated report rather than duplicating the fingerprint algorithm:

```python
from pathlib import Path
from pta_finance import receipt_viewer, reimbursement_report

report = reimbursement_report.load_bundle(Path("reports/output/reimbursement-report.json"))
ticket = next(t for t in report.tickets if t.ref == "NEW-01")
item = ticket.items[0]  # Select the exact item that was visually checked.
fingerprint = receipt_viewer.item_fingerprint(ticket, item)
```

Paths must stay below the sidecar's directory, including resolved symlinks. Only PNG and JPEG
bytes are accepted (20 MiB per page; 100 MiB total). Remote URLs, active image formats, duplicate
keys, unknown items/pages, unused pages, and invalid coordinates are rejected. An image digest
mismatch or a changed source/claim invalidates the location and stops HTML replacement, preserving
the previous report. Reinspect the receipt before updating that entry; never just refresh hashes
to suppress an error. Changes only to review status or reviewer explanations retain the match.

Rebuild after saving the sidecar:

```powershell
uv run pta-finance report-reimbursements
```
