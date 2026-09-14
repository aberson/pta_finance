"""Offline, content-checked receipt pages and item locations for review reports.

The optional sidecar is private evidence, separate from adjudication. Coordinates are
fractions of the displayed page, measured from its top left. Never infer a location from
an amount or description: an absent box means the exact line has not been identified.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pta_finance.reimbursement_report import ReimbursementReport, ReviewItem, Ticket

MAX_PAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024


class ReceiptViewerError(ValueError):
    """A receipt sidecar cannot be safely associated with this report."""


def item_fingerprint(ticket: Ticket, item: ReviewItem) -> str:
    """Invalidate saved locations if the evidence or displayed claim changes."""

    values = [
        ticket.review_key,
        ticket.source_evidence_sha256,
        item.item_key,
        item.source_index,
        str(item.source_date),
        item.source_description,
        str(item.source_amount),
        str(item.effective_date),
        item.effective_item,
        str(item.effective_amount),
    ]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True).encode("utf-8")).hexdigest()


def _object(value: Any, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ReceiptViewerError("receipt sidecar has invalid object fields")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReceiptViewerError("receipt sidecar requires nonempty strings")
    return value


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ReceiptViewerError("receipt sidecar requires arrays")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ReceiptViewerError("receipt sidecar contains a duplicate JSON key")
        value[key] = item
    return value


def _box(value: Any) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise ReceiptViewerError("receipt box must be [left, top, width, height] or null")
    if any(type(n) not in (int, float) or not math.isfinite(n) for n in value):
        raise ReceiptViewerError("receipt box coordinates must be finite numbers")
    x, y, width, height = value
    if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
        raise ReceiptViewerError("receipt box must fit within the page's 0–1 coordinates")
    return [float(n) for n in value]


def load_receipts(path: Path, report: ReimbursementReport) -> dict[str, Any]:
    """Validate a private sidecar and embed only its referenced local PNG/JPEG pages.

    All paths must resolve below the sidecar directory, including symlink resolution.
    Invalid/stale evidence fails before the caller replaces the existing report.
    """

    try:
        document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, OSError) as exc:
        raise ReceiptViewerError("cannot read a valid receipt sidecar") from exc
    raw = _object(document, {"schema_version", "pages", "items"})
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ReceiptViewerError("unsupported receipt sidecar schema_version")
    pages: dict[str, Any] = {}
    total_bytes = 0
    root = path.resolve().parent
    for value in _array(raw["pages"]):
        page = _object(value, {"id", "path", "sha256", "label"})
        page_id = _text(page["id"])
        if page_id in pages:
            raise ReceiptViewerError("receipt page IDs must be unique")
        relative = Path(_text(page["path"]))
        image_path = (root / relative).resolve()
        if relative.is_absolute() or not image_path.is_relative_to(root):
            raise ReceiptViewerError("receipt page must be inside the sidecar directory")
        try:
            with image_path.open("rb") as handle:
                payload = handle.read(MAX_PAGE_BYTES + 1)
        except OSError as exc:
            raise ReceiptViewerError("receipt page is missing or unreadable") from exc
        total_bytes += len(payload)
        if len(payload) > MAX_PAGE_BYTES or total_bytes > MAX_TOTAL_BYTES:
            raise ReceiptViewerError("receipt images exceed the offline report size limit")
        if hashlib.sha256(payload).hexdigest() != _text(page["sha256"]):
            raise ReceiptViewerError("receipt page content no longer matches its sha256")
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif payload.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        else:
            raise ReceiptViewerError("receipt pages must be PNG or JPEG images")
        pages[page_id] = {
            "label": _text(page["label"]),
            "src": f"data:{mime};base64," + base64.b64encode(payload).decode("ascii"),
        }
    known = {
        (ticket.review_key, item.item_key): (ticket, item)
        for ticket in report.tickets
        for item in ticket.items
    }
    items: dict[str, dict[str, Any]] = {}
    used_pages: set[str] = set()
    for value in _array(raw["items"]):
        entry = _object(value, {"review_key", "item_key", "item_sha256", "regions"})
        review_key, item_key = _text(entry["review_key"]), _text(entry["item_key"])
        if (review_key, item_key) not in known:
            raise ReceiptViewerError("receipt location references an unknown review item")
        ticket, item = known[review_key, item_key]
        if _text(entry["item_sha256"]) != item_fingerprint(ticket, item):
            raise ReceiptViewerError("receipt location is stale; recheck the changed review item")
        ticket_items = items.setdefault(review_key, {})
        if item_key in ticket_items:
            raise ReceiptViewerError("receipt locations contain a duplicate review item")
        regions = []
        for value in _array(entry["regions"]):
            region = _object(value, {"page_id", "box"})
            page_id = _text(region["page_id"])
            if page_id not in pages:
                raise ReceiptViewerError("receipt location references an unknown page")
            regions.append({"page_id": page_id, "box": _box(region["box"])})
            used_pages.add(page_id)
        if not regions:
            raise ReceiptViewerError("receipt item needs at least one source page")
        ticket_items[item_key] = {"regions": regions}
    if used_pages != set(pages):
        raise ReceiptViewerError("receipt sidecar contains unreferenced pages")
    return {"pages": pages, "items": items}
