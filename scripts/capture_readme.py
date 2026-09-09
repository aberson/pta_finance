"""Capture the real reimbursement report with fictional data and no live I/O.

From the repository root:
    uv run --with playwright==1.58.0 python -m playwright install chromium
    uv run --with playwright==1.58.0 python scripts/capture_readme.py

Playwright is a documentation-only dependency. HTML and the sample bundle live in a
temporary directory; only the two reimbursement PNGs are written into the repository.
The separate PowerPoint example is exported by export_example_snapshot.ps1.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from playwright.sync_api import Page, sync_playwright

from pta_finance import reimbursement_report

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "screenshots"


def digest(label: str) -> str:
    return hashlib.sha256(f"fictional-readme-example:{label}".encode()).hexdigest()


def ticket(
    order: int,
    ref: str,
    status: str,
    decision: str,
    action: str,
    items: list[tuple[str, str, str, str, str]],
    *,
    legacy: bool = False,
    settled: bool = False,
) -> dict[str, Any]:
    review_key = f"legacy:v1:{ref.lower()}" if legacy else f"submission:v1:{digest(ref)}"
    total = sum((Decimal(item[1]) for item in items), Decimal("0.00"))
    questions = ["Will the reusable display stand stay at the school for future events?"]
    asks = questions if status == "C" else []
    return {
        "review_key": review_key,
        "ref": ref,
        "form_label": "",
        "origin": "legacy" if legacy else "submission",
        "display_order": order,
        "requestor_name": f"Example Volunteer {order}",
        "form_type": "Example Reimbursement Form",
        "submitted": "2026-08-12",
        "submitted_label": "",
        "payment_method": "Zelle",
        "source_evidence_sha256": digest(f"source-{ref}"),
        "source": {
            "stated_total": str(total),
            "mapped_total": str(total),
            "categories": list(dict.fromkeys(item[2] for item in items)),
            "flags": [],
        },
        "live": {
            "workflow_state": "SETTLED" if settled else "ACTIVE",
            "decision": decision,
            "payment_status": "PAID_PRIOR" if settled else "NOT_PAID",
            "payment_date": "2026-08-14" if settled else "",
            "confirmations": ["EXAMPLE-PRIOR-PAYMENT"] if settled else [],
        },
        "review": {
            "status": status,
            "action": action,
            "block": (
                "Confirm where the equipment will be kept before closing this review."
                if status == "C"
                else action + "."
            ),
            "asks": asks,
            "note": "Fictional example for documentation. Decisions shown are sample records.",
            "email_questions": asks,
            "email_context": "",
        },
        "items": [
            {
                "item_key": f"{ref}:line:{index}",
                "source_index": index,
                "source_date": "2026-08-10",
                "source_description": description,
                "source_amount": amount,
                "canonical_category": category,
                "display_date": "",
                "display_item": "",
                "reviewed_amount": "",
                "status": item_status,
                "why": why,
            }
            for index, (description, amount, category, item_status, why) in enumerate(items, 1)
        ],
        "messages": (
            [] if settled else [{"kind": "draft", "date": "", "mode": "generated", "body": ""}]
        ),
        "archive_note": "Prior payment recorded in the fictional review history."
        if settled
        else "",
    }


def example_bundle() -> dict[str, Any]:
    tickets = [
        ticket(
            1,
            "NEW-01",
            "A",
            "UNREVIEWED",
            "Review classroom supply receipts",
            [
                (
                    "Art paper packs",
                    "124.50",
                    "Classroom Supplies",
                    "A",
                    "Itemized form matches the stated total; reviewer confirmation remains.",
                ),
                (
                    "Washable paints",
                    "60.00",
                    "Classroom Supplies",
                    "A",
                    "Classroom materials; recommendation awaits a recorded decision.",
                ),
            ],
        ),
        ticket(
            2,
            "NEW-02",
            "C",
            "CLARIFICATION",
            "Confirm equipment stays at school",
            [
                (
                    "Family night activity materials",
                    "36.75",
                    "Family Events",
                    "A",
                    "Receipt and event purpose were confirmed in the sample review.",
                ),
                (
                    "Reusable display stand",
                    "90.00",
                    "Equipment",
                    "C",
                    "Confirm school ownership and storage for future events.",
                ),
            ],
        ),
        ticket(
            3,
            "NEW-03",
            "Q",
            "UNREVIEWED",
            "Inspect the receipt and assign a category",
            [
                (
                    "Event supplies",
                    "68.00",
                    "Pending Review",
                    "Q",
                    "Original submission is present; item review has not been recorded.",
                ),
            ],
        ),
        ticket(
            4,
            "P-001",
            "A",
            "APPROVED",
            "Pay the approved amount and record confirmation",
            [
                (
                    "Grade 3 field trip materials",
                    "240.00",
                    "Field Trips",
                    "A",
                    "Reviewed receipt and program purpose support the recorded approval.",
                ),
            ],
            legacy=True,
        ),
        ticket(
            5,
            "P-002",
            "A",
            "APPROVED",
            "Settled; no further action",
            [
                (
                    "Library reading celebration",
                    "96.00",
                    "Library",
                    "A",
                    "Approved and paid in the prior example review.",
                ),
            ],
            legacy=True,
            settled=True,
        ),
    ]
    return {
        "schema_version": 2,
        "report": {
            "title": "Reimbursement Review",
            "eyebrow": "Example PTA / Treasurer",
            "subtitle": "Receipts, decisions, and next steps / Fictional example data",
            "organization": "Example PTA",
            "email_signoff": ["Thank you!", "Example PTA Treasurer"],
            "logo_data_uri": "",
            "confirmed_outstanding": "240.00",
            "cutoff_date": "2026-07-01",
            "policy_version": "example-v1",
            "as_of_date": "2026-08-15",
        },
        "provenance": {
            "mapped_sha256": digest("mapped"),
            "policy_sha256": digest("policy"),
            "source_snapshot_sha256": digest("snapshot"),
            "accounted_review_keys": sorted(t["review_key"] for t in tickets[:3]),
        },
        "source_summary": {
            "mapped_rows": 5,
            "mapped_submissions": 3,
            "mapped_total": "379.25",
            "first_received": "2026-08-12",
            "last_received": "2026-08-12",
        },
        "tickets": tickets,
        "supplemental": {
            "anchors_sha256": digest("anchors"),
            "evidence": [],
            "events": [],
            "unmatched": [],
        },
        "appendix": {"amendments": [], "cfo_checks": [], "excluded": [], "defects": []},
    }


def render_examples(directory: Path) -> None:
    bundle_path = directory / "reimbursements.json"
    bundle_path.write_text(json.dumps(example_bundle(), indent=2), encoding="utf-8")
    reimbursement_report.build_report(bundle_path, directory / "reimbursements.html")


def open_report(page: Page, path: Path) -> None:
    page.goto(path.as_uri(), wait_until="load")
    page.evaluate("document.fonts.ready")
    page.locator("img").evaluate_all("imgs => Promise.all(imgs.map(img => img.decode()))")


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="pta-readme-") as temporary, sync_playwright() as playwright:
        directory = Path(temporary)
        render_examples(directory)
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1050}, device_scale_factor=1)
        # Self-contained reports must not load external resources during documentation capture.
        page.route("https://**/*", lambda route: route.abort())
        page.route("http://**/*", lambda route: route.abort())
        open_report(page, directory / "reimbursements.html")
        index = page.locator("#action-index").bounding_box()
        assert index is not None
        page.screenshot(
            path=OUTPUT / "reimbursement-queue.png",
            clip={
                "x": 0,
                "y": 0,
                "width": 1440,
                "height": index["y"] + index["height"] + 24,
            },
        )
        page.locator("#new-02").screenshot(path=OUTPUT / "reimbursement-detail.png")
        browser.close()
    print(f"Captured two reimbursement screenshots with fictional data in {OUTPUT}")


if __name__ == "__main__":
    main()
