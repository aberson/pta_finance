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

import copy
import hashlib
import json
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

from pta_finance import models, receipt_ingest, receipt_map

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


@dataclass(frozen=True)
class EvidenceTicket:
    """One selected original submission, keyed independently of display ordering."""

    review_key: str
    received: str
    requestor_name: str
    form_type: str
    payment_method: str
    stated_total: str
    mapped_total: str
    categories: tuple[str, ...]
    flags: tuple[str, ...]
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


_NEW_REF_RE = re.compile(r"NEW-(\d+)\Z")
_FLAG_SPLIT_RE = re.compile(r"\s*(?:\||;)\s*")


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
    originals: list[receipt_ingest.Submission] = []
    for _label, message in receipt_ingest.iter_source(source):
        scanned += 1
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
        evidence_payload = {
            "review_key": review_key,
            "received": received_date.isoformat(),
            "requestor_name": submission.requestor_name,
            "requestor_email": submission.requestor_email,
            "form_type": receipt_ingest.form_type(submission.subject),
            "payment_method": submission.payment_type,
            "stated_total": stated_total,
            "mapped_total": f"{mapped_total:.2f}",
            "receipt_urls": list(submission.receipt_urls),
            "attachments": list(submission.attachments),
            "notes": submission.notes,
            "items": [item.__dict__ for item in items],
            "flags": list(flags),
        }
        tickets.append(
            EvidenceTicket(
                review_key=review_key,
                received=received_date.isoformat(),
                requestor_name=submission.requestor_name,
                form_type=receipt_ingest.form_type(submission.subject),
                payment_method=submission.payment_type,
                stated_total=stated_total,
                mapped_total=f"{mapped_total:.2f}",
                categories=categories,
                flags=flags,
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
    )


def _next_refs(tickets: Sequence[Mapping[str, Any]], count: int) -> tuple[str, ...]:
    numbers = [
        int(match.group(1))
        for ticket in tickets
        if (match := _NEW_REF_RE.fullmatch(str(ticket.get("ref", "")))) is not None
    ]
    start = max(numbers, default=0) + 1
    return tuple(f"NEW-{number:02d}" for number in range(start, start + count))


def _unreviewed_ticket(evidence: EvidenceTicket, *, ref: str, display_order: int) -> dict[str, Any]:
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
        "review": {
            "status": "Q",
            "action": "Review new submission",
            "block": "Review the source evidence and record an item-level decision before payment.",
            "asks": [],
            "note": "New submission discovered by the deterministic email refresh.",
            "email_questions": [],
            "email_context": "",
        },
        "items": [
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
                "status": "Q",
                "why": "Awaiting item-level review.",
            }
            for item in evidence.items
        ],
        "messages": [{"kind": "draft", "date": "", "mode": "generated", "body": ""}],
        "archive_note": "",
    }


def merge_evidence_into_bundle(
    bundle: Mapping[str, Any], snapshot: EvidenceSnapshot, *, as_of: date
) -> tuple[dict[str, Any], BundleMergeSummary]:
    """Return a refreshed bundle, appending new tickets and refusing source drift.

    The input mapping is never mutated.  Existing reviewed tickets are preserved byte-for-byte
    apart from top-level report/provenance/source-summary values.  A source record that changed or
    disappeared is an operator event, not something this deterministic layer guesses through.
    """
    result = copy.deepcopy(dict(bundle))
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
) -> tuple[dict[str, Any], BundleMergeSummary]:
    """Build and validate a bundle refresh plan without changing either private artifact."""
    from pta_finance import reimbursement_report

    reimbursement_report.load_bundle(bundle_path)
    raw = _load_raw_bundle(bundle_path)
    snapshot = build_evidence_snapshot(
        source=source,
        category_map_path=category_map_path,
        start_month=start_month,
        received_since=received_since,
        subject_filter=subject_filter,
    )
    refreshed, summary = merge_evidence_into_bundle(raw, snapshot, as_of=as_of)
    return refreshed, summary
