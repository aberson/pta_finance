from __future__ import annotations

import copy
import hashlib
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest
from test_reimbursement_report import _bundle, _write_bundle

from pta_finance import receipt_viewer, reimbursement_report


def _page_png() -> bytes:
    """A fictional tall receipt page with a visible gray line at the marked location."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    rows = b"".join(b"\0" + bytes([180 if 300 <= y < 330 else 255]) * 400 for y in range(1000))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 400, 1000, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _inputs(root: Path) -> tuple[Path, Path, dict[str, Any]]:
    bundle = root / "bundle.json"
    _write_bundle(bundle, _bundle())
    report = reimbursement_report.load_bundle(bundle)
    ticket = report.tickets[0]
    picture = _page_png()
    (root / "receipt.png").write_bytes(picture)
    raw = {
        "schema_version": 1,
        "pages": [
            {
                "id": "page-1",
                "path": "receipt.png",
                "sha256": hashlib.sha256(picture).hexdigest(),
                "label": "Example receipt · page 1 </script><script>window.injected=true</script>",
            },
            {
                "id": "page-2",
                "path": "receipt.png",
                "sha256": hashlib.sha256(picture).hexdigest(),
                "label": "Example receipt · page 2",
            },
        ],
        "items": [
            {
                "review_key": ticket.review_key,
                "item_key": ticket.items[0].item_key,
                "item_sha256": receipt_viewer.item_fingerprint(ticket, ticket.items[0]),
                "regions": [
                    {"page_id": "page-1", "box": [0.1, 0.3, 0.8, 0.03]},
                    {"page_id": "page-1", "box": [0.1, 0.5, 0.8, 0.03]},
                    {"page_id": "page-2", "box": None},
                ],
            }
        ],
    }
    sidecar = bundle.with_suffix(".receipts.json")
    sidecar.write_text(json.dumps(raw), encoding="utf-8")
    return bundle, sidecar, raw


def test_build_report_embeds_checked_receipts_offline_and_deterministically(tmp_path: Path) -> None:
    bundle, _, _ = _inputs(tmp_path)
    output = tmp_path / "report.html"
    result = reimbursement_report.build_report(bundle, output)
    rendered = output.read_text(encoding="utf-8")
    assert "data:image/png;base64," in rendered
    assert "View source receipt" in rendered
    assert "Receipt not linked" in rendered
    assert "<script>window.injected=true</script>" not in rendered
    assert "submission:v1:" not in rendered
    assert str(tmp_path) not in rendered
    assert reimbursement_report.build_report(bundle, output).sha256 == result.sha256
    assert result.summary == reimbursement_report.load_bundle(bundle).summary
    assert result.receipt_linked_items == 1


@pytest.mark.parametrize(
    "problem",
    [
        "stale",
        "hash",
        "missing",
        "escape",
        "absolute",
        "unknown_item",
        "unknown_page",
        "duplicate_item",
        "duplicate_page",
        "empty_regions",
        "unused_page",
        "active_content",
        "negative",
        "overflow",
        "nan",
        "boolean",
        "zero_width",
        "extra_field",
        "version",
    ],
)
def test_bad_receipt_evidence_preserves_existing_report(tmp_path: Path, problem: str) -> None:
    bundle, sidecar, raw = _inputs(tmp_path)
    page, item = raw["pages"][0], raw["items"][0]
    if problem == "stale":
        item["item_sha256"] = "0" * 64
    elif problem == "hash":
        page["sha256"] = "0" * 64
    elif problem == "missing":
        page["path"] = "missing.png"
    elif problem == "escape":
        page["path"] = "../receipt.png"
    elif problem == "absolute":
        page["path"] = str(tmp_path / "receipt.png")
    elif problem == "unknown_item":
        item["item_key"] = "other"
    elif problem == "unknown_page":
        item["regions"][0]["page_id"] = "other"
    elif problem == "duplicate_item":
        raw["items"].append(copy.deepcopy(item))
    elif problem == "duplicate_page":
        raw["pages"].append(copy.deepcopy(page))
    elif problem == "empty_regions":
        item["regions"] = []
    elif problem == "unused_page":
        raw["items"] = []
    elif problem == "active_content":
        payload = b'<svg onload="alert(1)"></svg>'
        (tmp_path / "receipt.png").write_bytes(payload)
        page["sha256"] = hashlib.sha256(payload).hexdigest()
    elif problem == "extra_field":
        page["url"] = "https://example.org/receipt.png"
    elif problem == "version":
        raw["schema_version"] = True
    else:
        item["regions"][0]["box"] = {
            "negative": [-0.1, 0.3, 0.8, 0.03],
            "overflow": [0.5, 0.3, 0.8, 0.03],
            "nan": [0.1, float("nan"), 0.8, 0.03],
            "boolean": [False, 0.3, 0.8, 0.03],
            "zero_width": [0.1, 0.3, 0, 0.03],
        }[problem]
    sidecar.write_text(json.dumps(raw), encoding="utf-8")
    output = tmp_path / "report.html"
    output.write_text("previous report", encoding="utf-8")
    with pytest.raises(reimbursement_report.ReimbursementReportError):
        reimbursement_report.build_report(bundle, output)
    assert output.read_text() == "previous report"


def test_changed_claim_invalidates_location_but_decision_does_not(tmp_path: Path) -> None:
    bundle, sidecar, _ = _inputs(tmp_path)
    raw = json.loads(bundle.read_text(encoding="utf-8"))
    raw["tickets"][0]["items"][0]["why"] = "Updated reviewer explanation"
    _write_bundle(bundle, raw)
    receipt_viewer.load_receipts(sidecar, reimbursement_report.load_bundle(bundle))
    raw["tickets"][0]["items"][0]["display_item"] = "A different claimed item"
    _write_bundle(bundle, raw)
    with pytest.raises(receipt_viewer.ReceiptViewerError, match="stale"):
        receipt_viewer.load_receipts(sidecar, reimbursement_report.load_bundle(bundle))


def test_duplicate_json_keys_rejected(tmp_path: Path) -> None:
    bundle, sidecar, _ = _inputs(tmp_path)
    sidecar.write_text('{"schema_version":1,"schema_version":1,"pages":[],"items":[]}')
    with pytest.raises(receipt_viewer.ReceiptViewerError):
        receipt_viewer.load_receipts(sidecar, reimbursement_report.load_bundle(bundle))


@pytest.mark.parametrize(
    "viewport", [{"width": 1280, "height": 900}, {"width": 390, "height": 844}]
)
def test_browser_receipt_navigation_zoom_focus_and_missing_source(
    tmp_path: Path, viewport: dict[str, int]
) -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    bundle, _, _ = _inputs(tmp_path)
    output = tmp_path / "report.html"
    reimbursement_report.build_report(bundle, output)
    with playwright.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch()
        except playwright.Error as exc:
            if "Executable doesn't exist at" in str(exc):
                pytest.skip("Playwright Chromium executable is not installed")
            raise
        page = browser.new_page(viewport=viewport)
        errors: list[str] = []
        requests: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        page.goto(output.as_uri())
        button = page.locator(".receipt-item").first
        button.focus()
        page.keyboard.press("Enter")
        page.wait_for_function("document.getElementById('receipt-image').naturalWidth === 400")
        assert page.locator("#receipt-viewer").is_visible()
        assert page.locator("#receipt-close").evaluate("el => el === document.activeElement")
        assert page.locator("#receipt-marks ellipse").count() == 2
        assert page.locator("#receipt-prev").is_disabled()
        page.locator("#receipt-fit").click()
        before = page.locator("#receipt-image").bounding_box()
        assert before is not None
        page.locator("#receipt-zoom-in").click()
        after = page.locator("#receipt-image").bounding_box()
        assert after is not None and after["width"] == pytest.approx(
            before["width"] * 1.5, abs=0.02
        )
        overlay = page.locator("#receipt-marks").bounding_box()
        assert overlay is not None
        assert overlay == pytest.approx(after)
        page.locator("#receipt-fit").click()
        assert page.locator("#receipt-image").bounding_box() == pytest.approx(before)
        page.locator("#receipt-next").click()
        assert page.locator("#receipt-marks ellipse").count() == 0
        assert "has not been marked" in page.locator("#receipt-status").inner_text()
        assert page.locator("#receipt-next").is_disabled()
        page.keyboard.press("Escape")
        assert not page.locator("#receipt-viewer").is_visible()
        assert button.evaluate("el => el === document.activeElement")
        page.locator(".receipt-item").nth(1).click()
        assert page.locator("#receipt-empty").is_visible()
        assert not page.locator("#receipt-page").is_visible()
        assert page.locator("#receipt-marks ellipse").count() == 0
        page.locator("#receipt-close").click()
        assert not page.evaluate("Boolean(window.injected)")
        assert not errors
        assert all(url.startswith(("file:", "data:")) for url in requests)
        browser.close()
