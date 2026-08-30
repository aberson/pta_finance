"""Deterministic local evidence refresh for the private reimbursement report bundle.

This module is deliberately credential-free.  It reads the same top-level ``.eml``/``.mbox``
archive and category map as :mod:`pta_finance.receipt_map`, applies the same original-message,
received-cutoff, and deduplication rules, then merges only *new* immutable source records into an
existing private report bundle.  Reviewed records are never silently rewritten: changed or
missing source evidence fails closed and leaves both the bundle and HTML untouched.

The bundle is private and contains names plus reimbursement details.  CLI callers keep it under
the gitignored ``reports/output`` tree and print aggregate counts only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, NoReturn

from pta_finance import models, receipt_ingest, receipt_map, reimbursement_events

__all__ = [
    "BundleMergeSummary",
    "EvidenceItem",
    "EvidenceSnapshot",
    "EvidenceTicket",
    "ReimbursementPipelineError",
    "build_evidence_snapshot",
    "merge_evidence_into_bundle",
    "plan_bundle_refresh",
    "refresh_bundle",
]


class ReimbursementPipelineError(ValueError):
    """The local evidence cannot be safely merged into the private report bundle."""


@dataclass(frozen=True)
class EvidenceItem:
    """One mapped line plus the lossless description retained by the email parser."""

    item_key: str
    source_index: int
    source_date: str
    source_description: str
    source_amount: str
    canonical_category: str
    flags: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceTicket:
    """One selected original submission, keyed independently of display ordering."""

    review_key: str
    source_message_id: str
    received: str
    requestor_name: str
    form_type: str
    payment_method: str
    stated_total: str
    mapped_total: str
    categories: tuple[str, ...]
    flags: tuple[str, ...]
    receipt_asset_count: int
    direct_email: bool
    items: tuple[EvidenceItem, ...]
    source_evidence_sha256: str


@dataclass(frozen=True)
class EvidenceSnapshot:
    """Canonical evidence plus privacy-safe aggregates and provenance digests."""

    tickets: tuple[EvidenceTicket, ...]
    mapped_rows: int
    mapped_total: Decimal
    first_received: str
    last_received: str
    mapped_sha256: str
    source_snapshot_sha256: str
    scanned_messages: int
    recognized_originals: int
    replies_skipped: int
    excluded_by_cutoff: int
    mail_excluded_by_cutoff: int
    mail_evidence: tuple[receipt_ingest.MailEvidence, ...]


@dataclass(frozen=True)
class BundleMergeSummary:
    """Aggregate-only outcome from refreshing one private report bundle."""

    new_tickets: int
    unchanged_tickets: int
    total_source_tickets: int
    mapped_rows: int
    mapped_total: Decimal
    first_received: str
    last_received: str
    supplemental_evidence: int
    supplemental_events: int
    unmatched_evidence: int
    supplemental_excluded_by_cutoff: int


_NEW_REF_RE = re.compile(r"NEW-(\d+)\Z")
_FLAG_SPLIT_RE = re.compile(r"\s*(?:\||;)\s*")
_HIGH_VALUE_THRESHOLD = Decimal("200.00")
_MEAL_SIGNAL_RE = re.compile(
    r"(?=.*\b(?:staff|adult|teacher|volunteer)\b)(?=.*\b(?:meal|food|lunch|dinner|breakfast)\b)",
    re.IGNORECASE,
)
_GIVEAWAY_SIGNAL_RE = re.compile(r"\b(?:giveaway|swag|prize|gift)\b", re.IGNORECASE)


def _fail(message: str) -> NoReturn:
    raise ReimbursementPipelineError(message)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _review_key(message_id: str) -> str:
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
    return f"submission:v1:{digest}"


def _line_key(review_key: str, index: int) -> str:
    digest = hashlib.sha256(f"{review_key}\0{index}".encode()).hexdigest()
    return f"line:v1:{digest}"


def _iso_item_date(raw: str) -> str:
    if not raw.strip():
        return ""
    try:
        return models.parse_date(raw).isoformat()
    except ValueError:
        return ""


def _finite_money(raw: str, *, label: str, allow_blank: bool = False) -> str:
    if not raw.strip() and allow_blank:
        return ""
    try:
        return f"{receipt_map.parse_finite_amount(raw):.2f}"
    except ValueError as exc:
        raise ReimbursementPipelineError(f"{label} is not finite money") from exc


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = value.strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return tuple(result)


def build_evidence_snapshot(
    *,
    source: Path,
    category_map_path: Path,
    start_month: int,
    received_since: date | None,
    subject_filter: str | None = None,
) -> EvidenceSnapshot:
    """Parse and map the complete local archive once, retaining report-grade item evidence.

    The cutoff is applied before mapper deduplication, matching ``map-receipts``.  Any recognized
    original with a missing/malformed outer Date under an active cutoff aborts the whole refresh.
    """
    if not source.exists():
        _fail("reimbursement source does not exist")
    if not category_map_path.is_file():
        _fail("reimbursement category map does not exist")
    if not 1 <= start_month <= 12:
        _fail("fiscal-year start month must be between 1 and 12")

    category_map = receipt_map.load_category_map(category_map_path)
    form_defaults = receipt_map.load_form_defaults(category_map_path)
    scanned = 0
    replies = 0
    excluded = 0
    mail_excluded = 0
    originals: list[receipt_ingest.Submission] = []
    mail_by_key: dict[str, receipt_ingest.MailEvidence] = {}
    for _label, message in receipt_ingest.iter_source(source):
        scanned += 1
        raw_received = str(message.get("Date", ""))
        mail_received = receipt_ingest.parse_received_date(raw_received)
        mail_in_scope = received_since is None or (
            mail_received is not None and mail_received >= received_since
        )
        if mail_in_scope:
            try:
                mail = receipt_ingest.parse_mail_evidence(message)
            except ValueError as exc:
                raise ReimbursementPipelineError(
                    "one archived message has malformed supplemental evidence headers or MIME data"
                ) from exc
            prior_mail = mail_by_key.get(mail.message_key)
            if prior_mail is not None and prior_mail.evidence_sha256 != mail.evidence_sha256:
                _fail("the mail archive contains conflicting evidence for one stable message key")
            mail_by_key[mail.message_key] = mail
        else:
            mail_excluded += 1
        submission = receipt_ingest.parse_submission(message, subject_filter=subject_filter)
        if submission is None:
            continue
        if receipt_ingest.is_reply_or_forward(submission.subject):
            replies += 1
            continue
        received = receipt_ingest.parse_received_date(submission.received)
        if received_since is not None:
            if received is None:
                _fail(
                    "a recognized original has a missing or malformed Date header under the "
                    "active received cutoff"
                )
            if received < received_since:
                excluded += 1
                continue
        originals.append(submission)

    selected = receipt_map.deduplicate_submissions(originals)
    if any(not submission.message_id.strip() for submission in selected):
        _fail("a selected reimbursement original has no Message-ID")
    mapped_rows = receipt_map.map_submissions(
        selected,
        category_map=category_map,
        form_defaults=form_defaults,
        start_month=start_month,
    )
    rows_by_message: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in mapped_rows:
        rows_by_message[row["message_id"]].append(row)

    tickets: list[EvidenceTicket] = []
    for submission in selected:
        message_id = submission.message_id.strip()
        rows = rows_by_message.get(message_id, [])
        if not rows:
            continue
        source_items = tuple(item for item in submission.line_items if item.amount.strip())
        if len(source_items) != len(rows):
            _fail("parsed and mapped line counts disagree for one selected submission")
        review_key = _review_key(message_id)
        items: list[EvidenceItem] = []
        for item, row in zip(source_items, rows, strict=True):
            amount = _finite_money(row["amount"], label="mapped line amount")
            source_date = _iso_item_date(row["date"])
            items.append(
                EvidenceItem(
                    item_key=_line_key(review_key, item.index),
                    source_index=item.index,
                    source_date=source_date,
                    source_description=item.description.strip() or "Item description unavailable",
                    source_amount=amount,
                    canonical_category=row["canonical_category"].strip(),
                    flags=_unique(tuple(_FLAG_SPLIT_RE.split(row["needs_review"]))),
                )
            )

        received_date = receipt_ingest.parse_received_date(submission.received)
        if received_date is None:
            _fail("a selected reimbursement original has a missing or malformed Date header")
        mapped_total = sum((Decimal(item.source_amount) for item in items), Decimal("0"))
        stated_total = _finite_money(
            submission.total, label="stated submission total", allow_blank=True
        )
        categories = _unique(tuple(item.canonical_category for item in items))
        flags = _unique(
            tuple(
                flag for row in rows for flag in _FLAG_SPLIT_RE.split(row["needs_review"]) if flag
            )
        )
        legacy_items_payload = [
            {
                "item_key": item.item_key,
                "source_index": item.source_index,
                "source_date": item.source_date,
                "source_description": item.source_description,
                "source_amount": item.source_amount,
                "canonical_category": item.canonical_category,
            }
            for item in items
        ]
        evidence_payload = {
            "review_key": review_key,
            "received": received_date.isoformat(),
            "requestor_name": submission.requestor_name,
            "requestor_email": submission.requestor_email,
            "form_type": receipt_ingest.form_type(submission.subject),
            "payment_method": submission.payment_type,
            "stated_total": stated_total,
            "mapped_total": f"{mapped_total:.2f}",
            "receipt_urls": list(submission.source_receipt_urls_v1),
            "attachments": list(submission.attachments),
            "notes": submission.notes,
            "items": legacy_items_payload,
            "flags": list(flags),
        }
        source_message_id = receipt_ingest.normalize_message_id(message_id)
        original_mail = next(
            (mail for mail in mail_by_key.values() if mail.message_id == source_message_id), None
        )
        tickets.append(
            EvidenceTicket(
                review_key=review_key,
                source_message_id=source_message_id,
                received=received_date.isoformat(),
                requestor_name=submission.requestor_name,
                form_type=receipt_ingest.form_type(submission.subject),
                payment_method=submission.payment_type,
                stated_total=stated_total,
                mapped_total=f"{mapped_total:.2f}",
                categories=categories,
                flags=flags,
                receipt_asset_count=(
                    len(submission.receipt_urls)
                    + (len(original_mail.attachments) if original_mail is not None else 0)
                ),
                direct_email="got a new submission" not in submission.subject.casefold(),
                items=tuple(items),
                source_evidence_sha256=_sha256_json(evidence_payload),
            )
        )

    tickets.sort(key=lambda ticket: (ticket.received, ticket.review_key))
    received_values = [ticket.received for ticket in tickets]
    mapped_payload = [
        {field: row[field] for field in receipt_map.FIELDNAMES}
        for row in sorted(
            mapped_rows,
            key=lambda row: (
                row["received"],
                row["message_id"],
                row["date"],
                row["amount"],
                row["canonical_category"],
            ),
        )
    ]
    snapshot_payload = [
        {"review_key": ticket.review_key, "source_evidence_sha256": ticket.source_evidence_sha256}
        for ticket in tickets
    ]
    return EvidenceSnapshot(
        tickets=tuple(tickets),
        mapped_rows=len(mapped_rows),
        mapped_total=sum(
            (Decimal(item.source_amount) for ticket in tickets for item in ticket.items),
            Decimal("0"),
        ),
        first_received=min(received_values) if received_values else "",
        last_received=max(received_values) if received_values else "",
        mapped_sha256=_sha256_json(mapped_payload),
        source_snapshot_sha256=_sha256_json(snapshot_payload),
        scanned_messages=scanned,
        recognized_originals=len(originals),
        replies_skipped=replies,
        excluded_by_cutoff=excluded,
        mail_excluded_by_cutoff=mail_excluded,
        mail_evidence=tuple(sorted(mail_by_key.values(), key=lambda mail: mail.message_key)),
    )


def _next_refs(tickets: Sequence[Mapping[str, Any]], count: int) -> tuple[str, ...]:
    numbers = [
        int(match.group(1))
        for ticket in tickets
        if (match := _NEW_REF_RE.fullmatch(str(ticket.get("ref", "")))) is not None
    ]
    start = max(numbers, default=0) + 1
    return tuple(f"NEW-{number:02d}" for number in range(start, start + count))


def _item_recommendation(
    evidence: EvidenceTicket, item: EvidenceItem
) -> tuple[str, str, tuple[str, ...]]:
    """Return conservative, evidence-limited advice without authorizing a decision."""

    reasons: list[str] = []
    asks: list[str] = []
    if "unmapped-category" in item.flags or not item.canonical_category:
        reasons.append("The budget category is not deterministically mapped.")
        asks.append(f"Item {item.source_index}: confirm the budget category.")
    if "total-mismatch" in item.flags:
        reasons.append("The claimed line total does not reconcile to the stated form total.")
        asks.append("Confirm the intended reimbursement total and any corrected item amounts.")
    if evidence.receipt_asset_count == 0:
        reasons.append("No receipt asset is present in the available email evidence.")
        asks.append(f"Item {item.source_index}: provide the vendor receipt.")
    if evidence.direct_email:
        reasons.append("The source is direct email rather than a structured form notification.")
        asks.append(f"Item {item.source_index}: confirm the required form provenance.")
    amount = Decimal(item.source_amount)
    if amount >= _HIGH_VALUE_THRESHOLD:
        reasons.append("The amount meets the conservative high-value review threshold.")
        asks.append(f"Item {item.source_index}: perform operator receipt and policy review.")
    if _MEAL_SIGNAL_RE.search(item.source_description):
        reasons.append("The description signals a possible staff/adult meal.")
        asks.append(f"Item {item.source_index}: confirm the covered participants and policy basis.")
    if _GIVEAWAY_SIGNAL_RE.search(item.source_description):
        reasons.append("The description signals a possible giveaway, gift, prize, or swag item.")
        asks.append(f"Item {item.source_index}: confirm the program purpose and policy basis.")
    if reasons:
        return "C", " ".join(reasons), _unique(tuple(asks))
    return (
        "A",
        "Mapped category, reconciled total, and a receipt asset are present; no conservative "
        "scrutiny signal was detected. Receipt contents were not read.",
        (),
    )


def _recommended_review(
    evidence: EvidenceTicket,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    asks: list[str] = []
    for item in evidence.items:
        status, why, item_asks = _item_recommendation(evidence, item)
        asks.extend(item_asks)
        items.append(
            {
                "item_key": item.item_key,
                "source_index": item.source_index,
                "source_date": item.source_date,
                "source_description": item.source_description,
                "source_amount": item.source_amount,
                "canonical_category": item.canonical_category,
                "display_date": "",
                "display_item": "",
                "reviewed_amount": "",
                "status": status,
                "why": why,
            }
        )
    status = "C" if any(item["status"] == "C" for item in items) else "A"
    unique_asks = list(_unique(tuple(asks)))
    if status == "A":
        action = "Consider approval after operator review"
        block = "Verify receipt contents and policy applicability, then record the decision."
    else:
        action = "Resolve specific evidence questions"
        block = "Review the listed evidence limits before recording an item-level decision."
    review = {
        "status": status,
        "action": action,
        "block": block,
        "asks": unique_asks,
        "note": (
            "Non-authoritative deterministic advice from available metadata only; no OCR, "
            "visual receipt inspection, or policy adjudication was performed."
        ),
        "email_questions": unique_asks,
        "email_context": "",
    }
    return items, review


def _unreviewed_ticket(evidence: EvidenceTicket, *, ref: str, display_order: int) -> dict[str, Any]:
    items, review = _recommended_review(evidence)
    return {
        "review_key": evidence.review_key,
        "ref": ref,
        "form_label": "",
        "origin": "submission",
        "display_order": display_order,
        "requestor_name": evidence.requestor_name,
        "form_type": evidence.form_type,
        "submitted": evidence.received,
        "submitted_label": "",
        "payment_method": evidence.payment_method,
        "source_evidence_sha256": evidence.source_evidence_sha256,
        "source": {
            "stated_total": evidence.stated_total,
            "mapped_total": evidence.mapped_total,
            "categories": list(evidence.categories),
            "flags": list(evidence.flags),
        },
        "live": {
            "workflow_state": "ACTIVE",
            "decision": "UNREVIEWED",
            "payment_status": "NOT_PAID",
            "payment_date": "",
            "confirmations": [],
        },
        "review": review,
        "items": items,
        "messages": [],
        "archive_note": "",
    }


def _is_known_placeholder(ticket: Mapping[str, Any]) -> bool:
    """Recognize only the exact placeholder emitted by schema-v1 refreshes."""

    review = ticket.get("review")
    live = ticket.get("live")
    items = ticket.get("items")
    messages = ticket.get("messages")
    if not isinstance(review, dict) or not isinstance(live, dict):
        return False
    if not isinstance(items, list) or not items or not isinstance(messages, list):
        return False
    return (
        ticket.get("origin") == "submission"
        and live.get("decision") == "UNREVIEWED"
        and review
        == {
            "status": "Q",
            "action": "Review new submission",
            "block": "Review the source evidence and record an item-level decision before payment.",
            "asks": [],
            "note": "New submission discovered by the deterministic email refresh.",
            "email_questions": [],
            "email_context": "",
        }
        and all(
            isinstance(item, dict)
            and item.get("status") == "Q"
            and item.get("why") == "Awaiting item-level review."
            for item in items
        )
        and messages == [{"kind": "draft", "date": "", "mode": "generated", "body": ""}]
    )


def _upgrade_known_placeholder(ticket: dict[str, Any], evidence: EvidenceTicket) -> None:
    items, review = _recommended_review(evidence)
    ticket["items"] = items
    ticket["review"] = review
    ticket["messages"] = []


_RECEIPT_MIME_TYPES = frozenset({"application/pdf", "image/jpeg", "image/png"})


def _ticket_selector_key(ticket: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(ticket.get("review_key", "")),
        str(ticket.get("ref", "")),
        str(ticket.get("form_label", "")),
    )


def _resolve_selector(
    tickets: Sequence[dict[str, Any]], selector: reimbursement_events.TicketSelector
) -> dict[str, Any]:
    expected = (selector.review_key, selector.ref, selector.form_label)
    matches = [ticket for ticket in tickets if _ticket_selector_key(ticket) == expected]
    if len(matches) != 1:
        _fail("a private reimbursement anchor does not select exactly one current ticket")
    return matches[0]


def _mail_date(mail: receipt_ingest.MailEvidence) -> str:
    parsed = receipt_ingest.parse_received_date(mail.date)
    return parsed.isoformat() if parsed is not None else ""


def _mail_timestamp(mail: receipt_ingest.MailEvidence) -> str:
    """Normalize RFC Date to UTC for deterministic lifecycle reduction and event order."""

    try:
        parsed = parsedate_to_datetime(mail.date)
    except (TypeError, ValueError, OverflowError):
        return ""
    if parsed is None:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _attachment_payloads(
    mail: receipt_ingest.MailEvidence,
) -> list[dict[str, Any]]:
    return [
        {
            "mime_type": attachment.mime_type,
            "filename": attachment.filename,
            "decoded_size": attachment.decoded_size,
            "content_sha256": attachment.content_sha256,
        }
        for attachment in mail.attachments
    ]


def _mail_evidence_record(mail: receipt_ingest.MailEvidence) -> dict[str, Any]:
    record = {
        "evidence_key": mail.message_key,
        "source_type": "MAIL",
        "message_id": mail.message_id,
        "in_reply_to": list(mail.in_reply_to),
        "references": list(mail.references),
        "occurred_on": _mail_date(mail),
        "occurred_at": _mail_timestamp(mail),
        "top_authored_sha256": mail.top_authored_sha256,
        "evidence_sha256": mail.evidence_sha256,
        "attachments": _attachment_payloads(mail),
    }
    record["record_sha256"] = _sha256_json(record)
    return record


def _operator_evidence_record(review: reimbursement_events.OperatorReview) -> dict[str, Any]:
    key = f"operator-review:v1:{review.evidence_sha256}"
    record = {
        "evidence_key": key,
        "source_type": "OPERATOR_REVIEW",
        "message_id": "",
        "in_reply_to": [],
        "references": [],
        "occurred_on": "",
        "occurred_at": "",
        "top_authored_sha256": review.evidence_sha256,
        "evidence_sha256": review.evidence_sha256,
        "attachments": [],
    }
    record["record_sha256"] = _sha256_json(record)
    return record


def _event(
    *,
    evidence_key: str,
    ticket_review_key: str,
    kind: str,
    occurred_on: str,
    occurred_at: str,
    evidence_sha256: str,
    summary: str,
    amount: str = "",
    reference: str = "",
    discrepancy: str = "",
) -> dict[str, Any]:
    event_key = (
        "event:v1:"
        + hashlib.sha256(f"{evidence_key}\0{ticket_review_key}\0{kind}".encode()).hexdigest()
    )
    record = {
        "event_key": event_key,
        "evidence_key": evidence_key,
        "ticket_review_key": ticket_review_key,
        "kind": kind,
        "occurred_on": occurred_on,
        "occurred_at": occurred_at,
        "evidence_sha256": evidence_sha256,
        "summary": summary,
        "amount": amount,
        "reference": reference,
        "discrepancy": discrepancy,
    }
    record["record_sha256"] = _sha256_json(record)
    return record


def _ticket_total(ticket: Mapping[str, Any]) -> Decimal:
    raw_items = ticket.get("items")
    if not isinstance(raw_items, list):
        _fail("a linked ticket has invalid item evidence")
    total = Decimal("0")
    for item in raw_items:
        if not isinstance(item, dict):
            _fail("a linked ticket has invalid item evidence")
        raw = item.get("source_amount")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            total += Decimal(raw)
        except InvalidOperation as exc:
            raise ReimbursementPipelineError("a linked ticket has invalid item evidence") from exc
    return total


def _rollup_raw_items(items: Sequence[Mapping[str, Any]]) -> str:
    statuses = {str(item.get("status", "")) for item in items}
    if statuses == {"A"}:
        return "A"
    if "C" in statuses:
        return "C"
    if "Q" in statuses:
        return "Q"
    return "D"


def _remove_drafts(ticket: dict[str, Any]) -> None:
    raw_messages = ticket.get("messages")
    if not isinstance(raw_messages, list):
        _fail("a linked ticket has invalid message history")
    ticket["messages"] = [
        message
        for message in raw_messages
        if not isinstance(message, dict) or message.get("kind") != "draft"
    ]


def _apply_response_received(ticket: dict[str, Any], *, receipts: bool) -> None:
    live = ticket.get("live")
    review = ticket.get("review")
    items = ticket.get("items")
    if not isinstance(live, dict) or not isinstance(review, dict) or not isinstance(items, list):
        _fail("a linked ticket has invalid review state")
    if live.get("workflow_state") == "SETTLED":
        return
    if receipts:
        if live.get("decision") == "UNREVIEWED":
            for item in items:
                if not isinstance(item, dict):
                    _fail("a linked ticket has invalid item review")
                item["status"] = "Q"
                item["why"] = (
                    "Supplemental receipt evidence was received; operator visual review is pending."
                )
        review.update(
            {
                "status": _rollup_raw_items(items),
                "action": "Review supplemental receipt evidence",
                "block": "Inspect the received receipt assets and record an item-level decision.",
                "asks": [],
                "note": (
                    "Replacement receipt evidence is accounted for; no OCR, visual inspection, "
                    "or policy adjudication was performed."
                ),
                "email_questions": [],
                "email_context": "",
            }
        )
    else:
        review.update(
            {
                "status": _rollup_raw_items(items),
                "action": "Review linked clarification response",
                "block": "Review the received response before recording the next decision.",
                "asks": [],
                "note": "A linked clarification response was received and is awaiting review.",
                "email_questions": [],
                "email_context": "",
            }
        )
    _remove_drafts(ticket)


def _apply_payment(
    ticket: dict[str, Any], *, occurred_on: str, amount: str, reference: str
) -> None:
    live = ticket.get("live")
    review = ticket.get("review")
    items = ticket.get("items")
    if not isinstance(live, dict) or not isinstance(review, dict) or not isinstance(items, list):
        _fail("a linked ticket has invalid payment state")
    confirmations = live.get("confirmations")
    if not isinstance(confirmations, list):
        _fail("a linked ticket has invalid payment confirmations")
    confirmation = f"Reference {reference}; amount ${amount}"
    if (
        live.get("workflow_state") == "SETTLED"
        and live.get("decision") == "APPROVED"
        and live.get("payment_status") == "PAID"
        and live.get("payment_date") == occurred_on
        and confirmation in confirmations
    ):
        return
    for item in items:
        if not isinstance(item, dict):
            _fail("a linked ticket has invalid item review")
        prior_why = str(item.get("why", "")).strip()
        item["status"] = "A"
        payment_why = "Payment was confirmed by a configured operator on the exact linked case."
        item["why"] = f"{prior_why} {payment_why}".strip()
    prior_note = str(review.get("note", "")).strip()
    prior_asks = review.get("asks")
    prior_context = ""
    if isinstance(prior_asks, list) and prior_asks:
        prior_context = " Prior clarification asks: " + " | ".join(str(item) for item in prior_asks)
    review.update(
        {
            "status": "A",
            "action": "No further action",
            "block": "Configured operator payment confirmation settled this exact ticket.",
            "asks": [],
            "note": (
                f"{prior_note} Payment evidence is recorded in the supplemental event history."
                f"{prior_context}"
            ).strip(),
            "email_questions": [],
            "email_context": "",
        }
    )
    if confirmation not in confirmations:
        confirmations.append(confirmation)
    live.update(
        {
            "workflow_state": "SETTLED",
            "decision": "APPROVED",
            "payment_status": "PAID",
            "payment_date": occurred_on,
        }
    )
    _remove_drafts(ticket)


def _apply_payment_discrepancy(ticket: dict[str, Any]) -> None:
    """Hold a linked payment mismatch without erasing the prior item review rationale."""

    live = ticket.get("live")
    review = ticket.get("review")
    items = ticket.get("items")
    if not isinstance(live, dict) or not isinstance(review, dict) or not isinstance(items, list):
        _fail("a linked ticket has invalid payment state")
    if live.get("workflow_state") == "SETTLED":
        return
    live["decision"] = "UNREVIEWED"
    asks = review.get("asks")
    if not isinstance(asks, list):
        _fail("a linked ticket has invalid review questions")
    amount_ask = "Confirm the recorded payment amount before settling this ticket."
    if amount_ask not in asks:
        asks.append(amount_ask)
    discrepancy_note = (
        "A linked configured-operator payment confirmation was held because its amount differs."
    )
    prior_note = str(review.get("note", "")).strip()
    if discrepancy_note not in prior_note:
        prior_note = f"{prior_note} {discrepancy_note}".strip()
    review.update(
        {
            "status": _rollup_raw_items(items),
            "action": "Resolve payment amount discrepancy",
            "block": "Review the linked payment amount against the ticket total.",
            "note": prior_note,
            "email_questions": list(asks),
            "email_context": "",
        }
    )
    _remove_drafts(ticket)


def _expand_proposal_statuses(
    ticket: Mapping[str, Any],
    recommendation: reimbursement_events.ProposalRecommendation,
) -> tuple[str, ...] | None:
    """Expand a strict proposal section against the ticket's current recommendation.

    A grouped ``Approve as is`` section explicitly makes every item A.  An ordinary section may
    enumerate either every item or exactly the currently non-A positions; the latter preserves
    existing A recommendations.  Any other count is ambiguous and is quarantined by the caller.
    """

    items = ticket.get("items")
    if not isinstance(items, list) or not items:
        return None
    current: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            return None
        status = item.get("status")
        if status not in {"A", "C", "D", "Q"}:
            return None
        current.append(str(status))
    if recommendation.all_items:
        if recommendation.statuses != ("A",):
            return None
        return tuple("A" for _item in items)
    proposed = recommendation.statuses
    if len(proposed) == len(items):
        return proposed
    held_positions = [index for index, status in enumerate(current) if status != "A"]
    if not held_positions or len(proposed) != len(held_positions):
        return None
    expanded = list(current)
    for index, status in zip(held_positions, proposed, strict=True):
        expanded[index] = status
    return tuple(expanded)


def _apply_proposal_approval(ticket: dict[str, Any], statuses: Sequence[str]) -> None:
    items = ticket.get("items")
    live = ticket.get("live")
    review = ticket.get("review")
    if not isinstance(items, list) or not isinstance(live, dict) or not isinstance(review, dict):
        _fail("an approval proposal targets an invalid ticket")
    if len(items) != len(statuses):
        _fail("an approval proposal item count became inconsistent")
    proposed_rollup = "C" if "C" in statuses else "A"
    existing_statuses = [str(item.get("status", "")) for item in items if isinstance(item, dict)]
    already_applied = existing_statuses == list(statuses) and live.get("decision") == (
        "APPROVED" if proposed_rollup == "A" else "CLARIFICATION"
    )
    if already_applied:
        return
    if live.get("decision") != "UNREVIEWED":
        return
    for item, status in zip(items, statuses, strict=True):
        if not isinstance(item, dict):
            _fail("an approval proposal targets an invalid item")
        item["status"] = status
        item["why"] = "A configured secondary approver authorized this exact scoped recommendation."
    review["status"] = proposed_rollup
    review["action"] = "Pay approved items" if proposed_rollup == "A" else "Resolve held items"
    review["block"] = (
        "The scoped recommendation is authorized; payment confirmation remains separate."
    )
    review["note"] = "Authorization is recorded in the supplemental event history."
    live["decision"] = "APPROVED" if proposed_rollup == "A" else "CLARIFICATION"


def _apply_operator_review(
    ticket: dict[str, Any], review_override: reimbursement_events.OperatorReview
) -> None:
    items = ticket.get("items")
    live = ticket.get("live")
    review = ticket.get("review")
    if not isinstance(items, list) or not isinstance(live, dict) or not isinstance(review, dict):
        _fail("an operator review targets an invalid ticket")
    by_index = {int(item.get("source_index", 0)): item for item in items if isinstance(item, dict)}
    override_indexes = {item.source_index for item in review_override.items}
    if set(by_index) != override_indexes:
        _fail("an operator review must cover every current ticket item exactly once")
    for override in review_override.items:
        target = by_index[override.source_index]
        target["status"] = override.status
        target["why"] = override.why
        target["reviewed_amount"] = override.reviewed_amount
    rollup = _rollup_raw_items(list(by_index.values()))
    review.update(
        {
            "status": rollup,
            "action": review_override.action,
            "block": review_override.block,
            "asks": list(review_override.asks),
            "note": review_override.note,
            "email_questions": list(review_override.email_questions),
            "email_context": review_override.email_context,
        }
    )
    if review_override.record_decision:
        decision = {"A": "APPROVED", "C": "CLARIFICATION", "D": "DECLINED"}.get(
            rollup, "UNREVIEWED"
        )
        if live.get("payment_status") in {"PAID", "PAID_PRIOR"} and decision != "APPROVED":
            _fail("an operator review cannot contradict an already recorded payment")
        live["decision"] = decision


def _candidate_mail(mail: receipt_ingest.MailEvidence) -> bool:
    text = mail.top_authored_text
    has_receipt_signal = bool(re.search(r"\b(?:receipt|reimburse|clarif)\w*\b", text, re.I))
    return (
        bool(_receipt_attachments(mail))
        or has_receipt_signal
        or (reimbursement_events.parse_payment_evidence(text) is not None)
    )


def _receipt_attachments(mail: receipt_ingest.MailEvidence) -> tuple[str, ...]:
    return tuple(
        attachment.content_sha256
        for attachment in mail.attachments
        if attachment.mime_type.casefold() in _RECEIPT_MIME_TYPES
        and not re.search(r"(?:signature|logo)", attachment.filename, re.IGNORECASE)
    )


def _build_supplemental_and_reduce(
    *,
    tickets: list[dict[str, Any]],
    snapshot: EvidenceSnapshot,
    anchors: reimbursement_events.AnchorConfig,
    previous: Mapping[str, Any],
) -> dict[str, Any]:
    """Link current mail evidence, reduce safe events, and enforce append-only freshness."""

    ticket_by_key = {str(ticket.get("review_key", "")): ticket for ticket in tickets}
    original_anchors: dict[str, str] = {}
    for evidence_ticket in snapshot.tickets:
        if evidence_ticket.source_message_id and evidence_ticket.review_key in ticket_by_key:
            prior = original_anchors.get(evidence_ticket.source_message_id)
            if prior is not None and prior != evidence_ticket.review_key:
                _fail("original submission ancestry resolves to multiple tickets")
            original_anchors[evidence_ticket.source_message_id] = evidence_ticket.review_key

    thread_by_id: dict[
        str, tuple[reimbursement_events.ThreadAnchor, tuple[dict[str, Any], ...]]
    ] = {}
    for anchor in anchors.thread_anchors:
        selected = tuple(_resolve_selector(tickets, selector) for selector in anchor.tickets)
        thread_by_id[anchor.message_id] = (anchor, selected)
    direct_by_id: dict[str, tuple[reimbursement_events.DirectLink, dict[str, Any]]] = {}
    for link in anchors.direct_links:
        direct_by_id[link.message_id] = (link, _resolve_selector(tickets, link.ticket))

    mail_by_id = {mail.message_id: mail for mail in snapshot.mail_evidence if mail.message_id}
    previous_events = previous.get("events", [])
    if not isinstance(previous_events, list) or not all(
        isinstance(event, Mapping) for event in previous_events
    ):
        _fail("private report bundle supplemental event inventory is invalid")
    evidence_records: dict[str, dict[str, Any]] = {}
    events: dict[str, dict[str, Any]] = {}
    unmatched: dict[str, dict[str, Any]] = {}

    def account_mail(mail: receipt_ingest.MailEvidence) -> None:
        evidence_records[mail.message_key] = _mail_evidence_record(mail)

    def add_event(value: dict[str, Any]) -> None:
        key = str(value["event_key"])
        prior = events.get(key)
        if prior is not None and prior != value:
            _fail("current supplemental evidence produces conflicting stable events")
        events[key] = value

    def has_prior_grant(mail: receipt_ingest.MailEvidence, ticket: Mapping[str, Any]) -> bool:
        ticket_key = str(ticket.get("review_key", ""))
        matches = [
            event
            for event in previous_events
            if event.get("evidence_key") == mail.message_key
            and event.get("ticket_review_key") == ticket_key
            and event.get("kind") == "APPROVAL_GRANTED"
            and event.get("evidence_sha256") == mail.evidence_sha256
        ]
        return len(matches) == 1

    def prior_grant_statuses(
        mail: receipt_ingest.MailEvidence, ticket: Mapping[str, Any]
    ) -> tuple[str, ...] | None:
        """Return the still-current authorized statuses for an exact prior grant replay."""

        if not has_prior_grant(mail, ticket):
            return None
        raw_items = ticket.get("items")
        live = ticket.get("live")
        if not isinstance(raw_items, list) or not isinstance(live, Mapping):
            return None
        statuses = tuple(
            str(item.get("status", "")) for item in raw_items if isinstance(item, Mapping)
        )
        if (
            len(statuses) != len(raw_items)
            or not statuses
            or any(status not in {"A", "C"} for status in statuses)
        ):
            return None
        expected_decision = "CLARIFICATION" if "C" in statuses else "APPROVED"
        if live.get("decision") != expected_decision:
            return None
        return statuses

    def has_current_operator_override(ticket: Mapping[str, Any]) -> bool:
        return any(
            (review.ticket.review_key, review.ticket.ref, review.ticket.form_label)
            == _ticket_selector_key(ticket)
            for review in anchors.operator_reviews
        )

    def ancestry_resolutions(
        mail: receipt_ingest.MailEvidence,
    ) -> set[tuple[str, tuple[str, ...], str]]:
        queue = list(mail.in_reply_to) + list(mail.references)
        seen: set[str] = set()
        found: set[tuple[str, tuple[str, ...], str]] = set()
        while queue:
            message_id = queue.pop()
            if message_id in seen:
                continue
            seen.add(message_id)
            original_ticket = original_anchors.get(message_id)
            if original_ticket is not None:
                found.add(("CASE", (original_ticket,), message_id))
            anchored = thread_by_id.get(message_id)
            if anchored is not None:
                anchor, selected = anchored
                found.add(
                    (
                        anchor.purpose,
                        tuple(str(ticket["review_key"]) for ticket in selected),
                        message_id,
                    )
                )
            ancestor = mail_by_id.get(message_id)
            if ancestor is not None:
                queue.extend(ancestor.in_reply_to)
                queue.extend(ancestor.references)
        return found

    ordered_mail = sorted(
        snapshot.mail_evidence, key=lambda mail: (_mail_timestamp(mail), mail.message_key)
    )
    for mail in ordered_mail:
        if mail.message_id in original_anchors or mail.message_id in thread_by_id:
            continue
        direct = direct_by_id.get(mail.message_id)
        resolutions = ancestry_resolutions(mail)
        if direct is not None:
            link, ticket = direct
            direct_resolution = (link.purpose, (str(ticket["review_key"]),), mail.message_id)
            ancestry_ticket_sets = {resolution[1] for resolution in resolutions}
            if ancestry_ticket_sets and ancestry_ticket_sets != {direct_resolution[1]}:
                resolutions.add(direct_resolution)
            else:
                resolutions = {direct_resolution}

        normalized = {(purpose, ticket_keys) for purpose, ticket_keys, _anchor_id in resolutions}
        if len({ticket_keys for _purpose, ticket_keys in normalized}) > 1 or (
            len(normalized) > 1
            and any(purpose == "APPROVAL_PROPOSAL" for purpose, _keys in normalized)
        ):
            if _candidate_mail(mail) or resolutions:
                account_mail(mail)
                unmatched[mail.message_key] = {
                    "evidence_key": mail.message_key,
                    "reason": "AMBIGUOUS_LINK",
                }
            continue
        if not normalized:
            if _candidate_mail(mail):
                account_mail(mail)
                reason = "MISSING_MESSAGE_ID" if not mail.message_id else "NO_EXACT_LINK"
                unmatched[mail.message_key] = {
                    "evidence_key": mail.message_key,
                    "reason": reason,
                }
            continue

        purpose, ticket_keys = next(iter(normalized))
        account_mail(mail)
        occurred_on = _mail_date(mail)
        if purpose == "APPROVAL_PROPOSAL":
            anchor_ids = {
                anchor_id
                for resolution_purpose, resolution_keys, anchor_id in resolutions
                if resolution_purpose == purpose and resolution_keys == ticket_keys
            }
            proposal_mail = mail_by_id.get(next(iter(anchor_ids))) if len(anchor_ids) == 1 else None
            selected_tickets = [ticket_by_key[key] for key in ticket_keys]
            expected_refs = [str(ticket["ref"]) for ticket in selected_tickets]
            recommendations = (
                reimbursement_events.parse_proposal_recommendations(
                    proposal_mail.top_authored_text, expected_refs=expected_refs
                )
                if proposal_mail is not None
                else None
            )
            classification = reimbursement_events.classify_approval_reply(mail.top_authored_text)
            authorized_actor = mail.sender_address.casefold() in anchors.secondary_approvers
            recommendations_by_ref: dict[str, tuple[str, ...]] = {}
            replayed_before_override: set[str] = set()
            if recommendations is not None:
                raw_by_ref = {
                    recommendation.ref: recommendation for recommendation in recommendations
                }
                for ticket in selected_tickets:
                    ref = str(ticket["ref"])
                    recommendation = raw_by_ref.get(ref)
                    if recommendation is None:
                        break
                    expanded = _expand_proposal_statuses(ticket, recommendation)
                    if expanded is None:
                        expanded = prior_grant_statuses(mail, ticket)
                    if (
                        expanded is None
                        and has_prior_grant(mail, ticket)
                        and has_current_operator_override(ticket)
                    ):
                        replayed_before_override.add(ref)
                        expanded = ()
                    if expanded is None:
                        break
                    recommendations_by_ref[ref] = expanded
            proposal_valid = recommendations is not None and len(recommendations_by_ref) == len(
                selected_tickets
            )
            if not authorized_actor or classification is None or not proposal_valid:
                kind = "APPROVAL_QUARANTINED"
                summary = "Scoped approval evidence was quarantined without changing a decision."
            elif classification == "NEGATIVE":
                kind = "APPROVAL_DECLINED"
                summary = "The configured approver declined the exact scoped proposal."
            else:
                kind = "APPROVAL_GRANTED"
                summary = (
                    "The configured approver authorized only the exact scoped proposal; "
                    "additional reply prose was not interpreted."
                )
            for ticket in selected_tickets:
                event_value = _event(
                    evidence_key=mail.message_key,
                    ticket_review_key=str(ticket["review_key"]),
                    kind=kind,
                    occurred_on=occurred_on,
                    occurred_at=_mail_timestamp(mail),
                    evidence_sha256=mail.evidence_sha256,
                    summary=summary,
                )
                add_event(event_value)
                if (
                    kind == "APPROVAL_GRANTED"
                    and str(ticket["ref"]) not in replayed_before_override
                ):
                    _apply_proposal_approval(ticket, recommendations_by_ref[str(ticket["ref"])])
            continue

        ticket = ticket_by_key[ticket_keys[0]]
        receipt_hashes = _receipt_attachments(mail)
        payment = reimbursement_events.parse_payment_evidence(mail.top_authored_text)
        authorized_payment = (
            payment is not None
            and bool(occurred_on)
            and mail.sender_address.casefold() in anchors.payment_operators
        )
        if receipt_hashes:
            event_value = _event(
                evidence_key=mail.message_key,
                ticket_review_key=str(ticket["review_key"]),
                kind="RECEIPT_RECEIVED",
                occurred_on=occurred_on,
                occurred_at=_mail_timestamp(mail),
                evidence_sha256=mail.evidence_sha256,
                summary=f"{len(receipt_hashes)} supplemental receipt asset(s) received.",
            )
            add_event(event_value)
            if not authorized_payment:
                _apply_response_received(ticket, receipts=True)
        if mail.top_authored_text.strip():
            event_value = _event(
                evidence_key=mail.message_key,
                ticket_review_key=str(ticket["review_key"]),
                kind="CLARIFICATION_RECEIVED",
                occurred_on=occurred_on,
                occurred_at=_mail_timestamp(mail),
                evidence_sha256=mail.evidence_sha256,
                summary="A linked top-authored response was received for operator review.",
            )
            add_event(event_value)
            if not receipt_hashes and not authorized_payment:
                _apply_response_received(ticket, receipts=False)

        if not receipt_hashes and not mail.top_authored_text.strip():
            unmatched[mail.message_key] = {
                "evidence_key": mail.message_key,
                "reason": "NO_ACTIONABLE_CONTENT",
            }

        if authorized_payment:
            assert payment is not None
            amount = f"{payment.amount:.2f}"
            ticket_total = _ticket_total(ticket)
            discrepancy = ""
            if payment.amount != ticket_total:
                discrepancy = (
                    f"Payment amount ${amount} differs from ticket total ${ticket_total:.2f}."
                )
            payment_summary = (
                "Configured operator payment evidence was held because its amount differs."
                if discrepancy
                else "Configured operator payment confirmation recorded for this ticket."
            )
            payment_kind = "PAYMENT_DISCREPANCY" if discrepancy else "PAYMENT_RECORDED"
            event_value = _event(
                evidence_key=mail.message_key,
                ticket_review_key=str(ticket["review_key"]),
                kind=payment_kind,
                occurred_on=occurred_on,
                occurred_at=_mail_timestamp(mail),
                evidence_sha256=mail.evidence_sha256,
                summary=payment_summary,
                amount=amount,
                reference=payment.reference,
                discrepancy=discrepancy,
            )
            add_event(event_value)
            if discrepancy:
                _apply_payment_discrepancy(ticket)
            else:
                _apply_payment(
                    ticket,
                    occurred_on=occurred_on,
                    amount=amount,
                    reference=payment.reference,
                )

    for review_override in anchors.operator_reviews:
        ticket = _resolve_selector(tickets, review_override.ticket)
        record = _operator_evidence_record(review_override)
        evidence_records[str(record["evidence_key"])] = record
        event_value = _event(
            evidence_key=str(record["evidence_key"]),
            ticket_review_key=str(ticket["review_key"]),
            kind="OPERATOR_REVIEW",
            occurred_on="",
            occurred_at="",
            evidence_sha256=review_override.evidence_sha256,
            summary="Explicit private operator item review applied; payment remains separate.",
        )
        add_event(event_value)
        _apply_operator_review(ticket, review_override)

    current: dict[str, Any] = {
        "anchors_sha256": anchors.sha256,
        "evidence": sorted(evidence_records.values(), key=lambda item: str(item["evidence_key"])),
        "events": sorted(
            events.values(), key=lambda item: (str(item["occurred_at"]), str(item["event_key"]))
        ),
        "unmatched": sorted(unmatched.values(), key=lambda item: str(item["evidence_key"])),
    }
    previous_evidence = previous.get("evidence", [])
    if not isinstance(previous_evidence, list):
        _fail("private report bundle supplemental inventory is invalid")
    current_evidence_by_key = {str(item["evidence_key"]): item for item in current["evidence"]}
    for old in previous_evidence:
        if not isinstance(old, dict):
            _fail("private report bundle supplemental inventory is invalid")
        key = str(old.get("evidence_key", ""))
        replacement = current_evidence_by_key.get(key)
        if replacement is None:
            _fail(
                "previously accounted supplemental evidence is absent; "
                "the private bundle was not changed"
            )
        if replacement.get("evidence_sha256") != old.get("evidence_sha256"):
            _fail(
                "previously accounted supplemental evidence has changed; "
                "the private bundle was not changed"
            )
    current_events_by_key = {str(item["event_key"]): item for item in current["events"]}
    for old in previous_events:
        if not isinstance(old, dict):
            _fail("private report bundle supplemental event inventory is invalid")
        replacement = current_events_by_key.get(str(old.get("event_key", "")))
        if replacement != old:
            _fail(
                "a previously recorded supplemental event no longer resolves exactly; "
                "the private bundle was not changed"
            )
    return current


def merge_evidence_into_bundle(
    bundle: Mapping[str, Any],
    snapshot: EvidenceSnapshot,
    *,
    as_of: date,
    anchors: reimbursement_events.AnchorConfig | None = None,
) -> tuple[dict[str, Any], BundleMergeSummary]:
    """Return a refreshed bundle, appending new tickets and refusing source drift.

    The input mapping is never mutated.  Existing reviewed tickets are preserved byte-for-byte
    apart from top-level report/provenance/source-summary values.  A source record that changed or
    disappeared is an operator event, not something this deterministic layer guesses through.
    """
    from pta_finance import reimbursement_report

    result = reimbursement_report.migrate_bundle(bundle)
    anchor_config = anchors or reimbursement_events.empty_anchor_config()
    raw_tickets = result.get("tickets")
    if not isinstance(raw_tickets, list) or not all(
        isinstance(ticket, dict) for ticket in raw_tickets
    ):
        _fail("private report bundle tickets are not a list of objects")
    tickets: list[dict[str, Any]] = raw_tickets
    existing_submission = {
        str(ticket.get("review_key", "")): ticket
        for ticket in tickets
        if ticket.get("origin") == "submission"
    }
    evidence_by_key = {ticket.review_key: ticket for ticket in snapshot.tickets}
    if len(evidence_by_key) != len(snapshot.tickets):
        _fail("current source evidence contains duplicate stable review keys")

    provenance = result.get("provenance")
    if not isinstance(provenance, dict):
        _fail("private report bundle provenance block is invalid")
    raw_accounted = provenance.get("accounted_review_keys")
    if not isinstance(raw_accounted, list) or not all(
        isinstance(key, str) and key for key in raw_accounted
    ):
        _fail("private report bundle accounted-review inventory is invalid")
    accounted = set(raw_accounted)
    if not set(existing_submission).issubset(accounted):
        _fail("private report bundle omits a rendered submission from its source inventory")

    missing = sorted(accounted - set(evidence_by_key))
    if missing:
        _fail(
            f"{len(missing)} previously recorded submission(s) are absent from the current source; "
            "the private bundle was not changed"
        )
    if accounted:
        accounted_snapshot_payload = [
            {
                "review_key": ticket.review_key,
                "source_evidence_sha256": ticket.source_evidence_sha256,
            }
            for ticket in snapshot.tickets
            if ticket.review_key in accounted
        ]
        if provenance.get("source_snapshot_sha256") != _sha256_json(accounted_snapshot_payload):
            _fail(
                "previously recorded source evidence has changed; "
                "the private bundle was not changed"
            )
    stale = sorted(
        key
        for key, ticket in existing_submission.items()
        if ticket.get("source_evidence_sha256") != evidence_by_key[key].source_evidence_sha256
    )
    if stale:
        _fail(
            f"{len(stale)} previously reviewed submission(s) have changed source evidence; "
            "the private bundle was not changed"
        )

    new_evidence = [ticket for ticket in snapshot.tickets if ticket.review_key not in accounted]
    refs = _next_refs(tickets, len(new_evidence))
    next_order = max((int(ticket.get("display_order", 0)) for ticket in tickets), default=0) + 1
    for offset, (evidence, ref) in enumerate(zip(new_evidence, refs, strict=True)):
        tickets.append(_unreviewed_ticket(evidence, ref=ref, display_order=next_order + offset))
    tickets.sort(key=lambda ticket: (int(ticket["display_order"]), str(ticket["ref"])))
    for review_key, ticket in existing_submission.items():
        if _is_known_placeholder(ticket):
            _upgrade_known_placeholder(ticket, evidence_by_key[review_key])

    previous_supplemental = result.get("supplemental")
    if not isinstance(previous_supplemental, dict):
        _fail("private report bundle supplemental inventory is invalid")
    result["supplemental"] = _build_supplemental_and_reduce(
        tickets=tickets,
        snapshot=snapshot,
        anchors=anchor_config,
        previous=previous_supplemental,
    )

    report = result.get("report")
    source_summary = result.get("source_summary")
    if (
        not isinstance(report, dict)
        or not isinstance(provenance, dict)
        or not isinstance(source_summary, dict)
    ):
        _fail("private report bundle metadata blocks are invalid")
    report["as_of_date"] = as_of.isoformat()
    provenance["mapped_sha256"] = snapshot.mapped_sha256
    provenance["source_snapshot_sha256"] = snapshot.source_snapshot_sha256
    provenance["accounted_review_keys"] = sorted(evidence_by_key)
    source_summary.update(
        {
            "mapped_rows": snapshot.mapped_rows,
            "mapped_submissions": len(snapshot.tickets),
            "mapped_total": f"{snapshot.mapped_total:.2f}",
            "first_received": snapshot.first_received,
            "last_received": snapshot.last_received,
        }
    )
    return result, BundleMergeSummary(
        new_tickets=len(new_evidence),
        unchanged_tickets=len(existing_submission),
        total_source_tickets=len(snapshot.tickets),
        mapped_rows=snapshot.mapped_rows,
        mapped_total=snapshot.mapped_total,
        first_received=snapshot.first_received,
        last_received=snapshot.last_received,
        supplemental_evidence=len(result["supplemental"]["evidence"]),
        supplemental_events=len(result["supplemental"]["events"]),
        unmatched_evidence=len(result["supplemental"]["unmatched"]),
        supplemental_excluded_by_cutoff=snapshot.mail_excluded_by_cutoff,
    )


def _load_raw_bundle(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReimbursementPipelineError(
            "private reimbursement report bundle does not exist"
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReimbursementPipelineError(
            "private reimbursement report bundle is not valid JSON"
        ) from exc
    if not isinstance(value, dict):
        _fail("private reimbursement report bundle root must be an object")
    return value


def _write_bundle_atomic(path: Path, value: Mapping[str, Any]) -> None:
    from pta_finance import reimbursement_report

    path = path.resolve()
    if not path.parent.is_dir():
        _fail("private reimbursement report bundle parent directory does not exist")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        reimbursement_report.load_bundle(temp_path)
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def refresh_bundle(
    *,
    bundle_path: Path,
    source: Path,
    category_map_path: Path,
    start_month: int,
    received_since: date | None,
    as_of: date,
    subject_filter: str | None = None,
    anchors_path: Path | None = None,
) -> BundleMergeSummary:
    """Validate, refresh, revalidate, and atomically replace one private report bundle."""
    refreshed, summary = plan_bundle_refresh(
        bundle_path=bundle_path,
        source=source,
        category_map_path=category_map_path,
        start_month=start_month,
        received_since=received_since,
        as_of=as_of,
        subject_filter=subject_filter,
        anchors_path=anchors_path,
    )
    _write_bundle_atomic(bundle_path, refreshed)
    return summary


def plan_bundle_refresh(
    *,
    bundle_path: Path,
    source: Path,
    category_map_path: Path,
    start_month: int,
    received_since: date | None,
    as_of: date,
    subject_filter: str | None = None,
    anchors_path: Path | None = None,
) -> tuple[dict[str, Any], BundleMergeSummary]:
    """Build and validate a bundle refresh plan without changing either private artifact."""
    from pta_finance import reimbursement_report

    reimbursement_report.load_bundle(bundle_path)
    raw = _load_raw_bundle(bundle_path)
    effective_anchors_path = anchors_path or bundle_path.with_name("reimbursement-anchors.json")
    if anchors_path is not None and not anchors_path.is_file():
        _fail("the explicitly configured private reimbursement anchor file does not exist")
    try:
        anchors = reimbursement_events.load_anchor_config(effective_anchors_path)
    except reimbursement_events.ReimbursementEventError as exc:
        raise ReimbursementPipelineError(str(exc)) from exc
    snapshot = build_evidence_snapshot(
        source=source,
        category_map_path=category_map_path,
        start_month=start_month,
        received_since=received_since,
        subject_filter=subject_filter,
    )
    refreshed, summary = merge_evidence_into_bundle(raw, snapshot, as_of=as_of, anchors=anchors)
    return refreshed, summary
