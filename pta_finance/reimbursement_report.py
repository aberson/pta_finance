"""Deterministic renderer for a private reimbursement-review bundle.

This module is deliberately offline.  It reads one strict JSON document, validates the
adjudication and arithmetic, renders a self-contained HTML report, and atomically replaces
the requested output.  It never opens a mailbox, reads credentials, or contacts Google.

The JSON contains private names and financial detail and therefore belongs below the
gitignored ``reports/output`` tree.  The renderer itself is organization-neutral and safe
to keep in the public package.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from pta_finance import receipt_ingest

SCHEMA_VERSION = 2
_MONEY_RE = re.compile(r"^(?:0|[1-9][0-9]*)\.[0-9]{2}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAIL_KEY_RE = re.compile(r"^mail:v1:[0-9a-f]{64}$")
_OPERATOR_KEY_RE = re.compile(r"^operator-review:v1:[0-9a-f]{64}$")
_OPERATOR_PAYMENT_KEY_RE = re.compile(r"^operator-payment:v1:[0-9a-f]{64}$")
_EVENT_KEY_RE = re.compile(r"^event:v1:[0-9a-f]{64}$")
_PNG_DATA_PREFIX = "data:image/png;base64,"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_TEMPLATE_NAME = "reimbursement_queue.html.j2"
_TEMPLATE_DIR = Path(__file__).parent / "reports" / "templates"

STATUS_META: Mapping[str, tuple[str, str]] = {
    "A": ("APPROVED", "v-a"),
    "C": ("CLARIFICATION", "v-c"),
    "D": ("DECLINE", "v-d"),
    "Q": ("UNREVIEWED", "v-q"),
    "-": ("—", "v-n"),
}


class ReimbursementReportError(ValueError):
    """The private input cannot safely produce a reimbursement report."""


def _fail(message: str) -> NoReturn:
    raise ReimbursementReportError(message)


@dataclass(frozen=True)
class ReportSettings:
    title: str
    eyebrow: str
    subtitle: str
    organization: str
    email_signoff: tuple[str, ...]
    logo_data_uri: str
    confirmed_outstanding: Decimal
    cutoff_date: date
    policy_version: str
    as_of_date: date


@dataclass(frozen=True)
class Provenance:
    mapped_sha256: str
    policy_sha256: str
    source_snapshot_sha256: str
    accounted_review_keys: tuple[str, ...]


@dataclass(frozen=True)
class SourceSummary:
    mapped_rows: int
    mapped_submissions: int
    mapped_total: Decimal
    first_received: date
    last_received: date


@dataclass(frozen=True)
class TicketSource:
    stated_total: Decimal | None
    mapped_total: Decimal | None
    categories: tuple[str, ...]
    flags: tuple[str, ...]


@dataclass(frozen=True)
class LiveState:
    workflow_state: str
    decision: str
    payment_status: str
    payment_date: date | None
    confirmations: tuple[str, ...]


@dataclass(frozen=True)
class TicketReview:
    status: str
    action: str
    block: str
    asks: tuple[str, ...]
    note: str
    email_questions: tuple[str, ...]
    email_context: str


@dataclass(frozen=True)
class ReviewItem:
    item_key: str
    source_index: int
    source_date: date | None
    source_description: str
    source_amount: Decimal | None
    canonical_category: str
    display_date: date | None
    display_item: str
    reviewed_amount: Decimal | None
    status: str
    why: str

    @property
    def effective_date(self) -> date | None:
        return self.display_date or self.source_date

    @property
    def effective_item(self) -> str:
        return self.display_item or self.source_description

    @property
    def effective_amount(self) -> Decimal | None:
        return self.reviewed_amount if self.reviewed_amount is not None else self.source_amount


@dataclass(frozen=True)
class TicketMessage:
    kind: str
    date: date | None
    mode: str
    body: str


@dataclass(frozen=True)
class SupplementalAttachment:
    """Decoded attachment metadata retained without embedding the private bytes."""

    mime_type: str
    filename: str
    decoded_size: int
    content_sha256: str


@dataclass(frozen=True)
class SupplementalEvidence:
    """One accounted mail or explicit operator-authored evidence record."""

    evidence_key: str
    source_type: str
    message_id: str
    in_reply_to: tuple[str, ...]
    references: tuple[str, ...]
    occurred_on: date | None
    occurred_at: str
    top_authored_sha256: str
    evidence_sha256: str
    attachments: tuple[SupplementalAttachment, ...]
    record_sha256: str


@dataclass(frozen=True)
class TicketEvent:
    """One append-only lifecycle event linked to exactly one ticket."""

    event_key: str
    evidence_key: str
    ticket_review_key: str
    kind: str
    occurred_on: date | None
    occurred_at: str
    evidence_sha256: str
    summary: str
    amount: Decimal | None
    reference: str
    discrepancy: str
    record_sha256: str


@dataclass(frozen=True)
class UnmatchedEvidence:
    """A candidate supplemental record deliberately left outside every ticket."""

    evidence_key: str
    reason: str


@dataclass(frozen=True)
class SupplementalLedger:
    """Strict schema-v2 supplemental evidence/event inventory."""

    anchors_sha256: str
    evidence: tuple[SupplementalEvidence, ...]
    events: tuple[TicketEvent, ...]
    unmatched: tuple[UnmatchedEvidence, ...]


@dataclass(frozen=True)
class Ticket:
    review_key: str
    ref: str
    form_label: str
    origin: str
    display_order: int
    requestor_name: str
    form_type: str
    submitted: date
    submitted_label: str
    payment_method: str
    source_evidence_sha256: str
    source: TicketSource
    live: LiveState
    review: TicketReview
    items: tuple[ReviewItem, ...]
    messages: tuple[TicketMessage, ...]
    archive_note: str

    @property
    def total(self) -> Decimal:
        return sum(
            (item.effective_amount for item in self.items if item.effective_amount is not None),
            Decimal("0.00"),
        )

    def amount_for(self, status: str) -> Decimal:
        return sum(
            (
                item.effective_amount
                for item in self.items
                if item.status == status and item.effective_amount is not None
            ),
            Decimal("0.00"),
        )

    @property
    def approved(self) -> Decimal:
        return self.amount_for("A")

    @property
    def pay_now(self) -> Decimal:
        if self.live.payment_status != "NOT_PAID":
            return Decimal("0.00")
        return self.approved

    @property
    def is_closed(self) -> bool:
        return self.live.workflow_state == "SETTLED"

    @property
    def first_name(self) -> str:
        parts = self.requestor_name.strip().split(maxsplit=1)
        return parts[0] if parts else "there"

    @property
    def submitted_display(self) -> str:
        return self.submitted_label or self.submitted.isoformat()

    @property
    def item_groups(self) -> tuple[tuple[str, tuple[ReviewItem, ...]], ...]:
        groups: list[tuple[str, list[ReviewItem]]] = []
        for item in self.items:
            category = item.canonical_category or "Category not mapped"
            if not groups or groups[-1][0] != category:
                groups.append((category, [item]))
            else:
                groups[-1][1].append(item)
        return tuple((category, tuple(items)) for category, items in groups)


@dataclass(frozen=True)
class Amendment:
    title: str
    body: str
    effect: str
    scope: str


@dataclass(frozen=True)
class CfoCheck:
    ticket: str
    question: str
    answer: str


@dataclass(frozen=True)
class ExcludedEntry:
    label: str
    detail: str


@dataclass(frozen=True)
class Appendix:
    amendments: tuple[Amendment, ...]
    cfo_checks: tuple[CfoCheck, ...]
    excluded: tuple[ExcludedEntry, ...]
    defects: tuple[str, ...]


@dataclass(frozen=True)
class ReportSummary:
    review_rows: int
    active: int
    settled: int
    live_unreviewed: int
    new_records: int
    legacy_records: int
    item_lines: int
    known_total: Decimal
    approved: Decimal
    clarification: Decimal
    declined: Decimal
    question: Decimal
    outstanding: Decimal
    legacy_outstanding: Decimal
    emails_to_send: int


@dataclass(frozen=True)
class ReimbursementReport:
    settings: ReportSettings
    provenance: Provenance
    source_summary: SourceSummary
    tickets: tuple[Ticket, ...]
    appendix: Appendix
    supplemental: SupplementalLedger

    @property
    def active_tickets(self) -> tuple[Ticket, ...]:
        return tuple(ticket for ticket in self.tickets if not ticket.is_closed)

    @property
    def active_new(self) -> tuple[Ticket, ...]:
        return tuple(ticket for ticket in self.active_tickets if ticket.origin == "submission")

    @property
    def active_legacy(self) -> tuple[Ticket, ...]:
        return tuple(ticket for ticket in self.active_tickets if ticket.origin == "legacy")

    @property
    def closed_tickets(self) -> tuple[Ticket, ...]:
        return tuple(ticket for ticket in self.tickets if ticket.is_closed)

    def events_for(self, review_key: str) -> tuple[TicketEvent, ...]:
        """Return the deterministic event history for one stable ticket key."""

        return tuple(
            event for event in self.supplemental.events if event.ticket_review_key == review_key
        )

    def evidence_for(self, evidence_key: str) -> SupplementalEvidence:
        """Return already-validated evidence for a rendered event or unmatched record."""

        return next(
            item for item in self.supplemental.evidence if item.evidence_key == evidence_key
        )

    @property
    def summary(self) -> ReportSummary:
        active = self.active_tickets
        priced_amounts = tuple(
            amount
            for ticket in self.tickets
            for item in ticket.items
            if (amount := item.effective_amount) is not None
        )
        return ReportSummary(
            review_rows=len(self.tickets),
            active=len(active),
            settled=len(self.closed_tickets),
            live_unreviewed=sum(ticket.live.decision == "UNREVIEWED" for ticket in self.tickets),
            new_records=sum(ticket.origin == "submission" for ticket in self.tickets),
            legacy_records=sum(ticket.origin == "legacy" for ticket in self.tickets),
            item_lines=sum(len(ticket.items) for ticket in self.tickets),
            known_total=sum(priced_amounts, Decimal("0.00")),
            approved=sum((ticket.amount_for("A") for ticket in self.tickets), Decimal("0.00")),
            clarification=sum((ticket.amount_for("C") for ticket in self.tickets), Decimal("0.00")),
            declined=sum((ticket.amount_for("D") for ticket in self.tickets), Decimal("0.00")),
            question=sum((ticket.amount_for("Q") for ticket in self.tickets), Decimal("0.00")),
            outstanding=sum((ticket.pay_now for ticket in active), Decimal("0.00")),
            legacy_outstanding=sum(
                (ticket.pay_now for ticket in active if ticket.origin == "legacy"),
                Decimal("0.00"),
            ),
            emails_to_send=sum(
                message.kind == "draft" for ticket in active for message in ticket.messages
            ),
        )


@dataclass(frozen=True)
class EmailBlock:
    kind: str
    date: date | None
    body: str


@dataclass(frozen=True)
class BuildResult:
    output_path: Path
    sha256: str
    bytes_written: int
    summary: ReportSummary


def _object(
    value: Any,
    *,
    label: str,
    keys: frozenset[str],
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    actual = frozenset(value)
    missing = sorted(keys - actual)
    unknown = sorted(actual - keys)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        _fail(f"{label} has invalid keys ({'; '.join(details)})")
    return value


def _string(value: Any, *, label: str, blank: bool = False) -> str:
    if not isinstance(value, str) or (not blank and not value.strip()):
        qualifier = "a string" if blank else "a nonblank string"
        _fail(f"{label} must be {qualifier}")
    return value


def _strings(value: Any, *, label: str, blank_items: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(f"{label} must be an array")
    return tuple(
        _string(item, label=f"{label}[{index}]", blank=blank_items)
        for index, item in enumerate(value)
    )


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{label} must be an integer >= {minimum}")
    return value


def _date(value: Any, *, label: str, blank: bool = False) -> date | None:
    text = _string(value, label=label, blank=blank)
    if blank and not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ReimbursementReportError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != text:
        _fail(f"{label} must be an ISO date")
    return parsed


def _money(value: Any, *, label: str, blank: bool = False) -> Decimal | None:
    text = _string(value, label=label, blank=blank)
    if blank and not text:
        return None
    if not _MONEY_RE.fullmatch(text):
        _fail(f"{label} must be nonnegative money with exactly two decimals")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - regex already excludes invalid decimals
        raise ReimbursementReportError(f"{label} is invalid money") from exc
    if not amount.is_finite():
        _fail(f"{label} must be finite money")
    return amount


def _timestamp(value: Any, *, label: str, blank: bool = False) -> str:
    text = _string(value, label=label, blank=blank)
    if not text and blank:
        return ""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReimbursementReportError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.astimezone(UTC).isoformat() != text:
        _fail(f"{label} must be a normalized UTC ISO timestamp")
    return text


def _sha256(value: Any, *, label: str) -> str:
    text = _string(value, label=label)
    if not _SHA256_RE.fullmatch(text):
        _fail(f"{label} must be 64 lowercase hexadecimal characters")
    return text


def _json_sha256(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _choice(value: Any, *, label: str, choices: frozenset[str]) -> str:
    text = _string(value, label=label)
    if text not in choices:
        _fail(f"{label} must be one of {', '.join(sorted(choices))}")
    return text


def _logo(value: Any) -> str:
    text = _string(value, label="report.logo_data_uri", blank=True)
    if not text:
        return ""
    if not text.startswith(_PNG_DATA_PREFIX):
        _fail("report.logo_data_uri must be blank or an inline PNG data URI")
    try:
        payload = base64.b64decode(text[len(_PNG_DATA_PREFIX) :], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ReimbursementReportError("report.logo_data_uri contains invalid base64") from exc
    if not payload.startswith(_PNG_SIGNATURE):
        _fail("report.logo_data_uri is not a PNG image")
    return text


def _parse_settings(value: Any) -> ReportSettings:
    raw = _object(
        value,
        label="report",
        keys=frozenset(
            {
                "title",
                "eyebrow",
                "subtitle",
                "organization",
                "email_signoff",
                "logo_data_uri",
                "confirmed_outstanding",
                "cutoff_date",
                "policy_version",
                "as_of_date",
            }
        ),
    )
    signoff = _strings(raw["email_signoff"], label="report.email_signoff")
    if not signoff:
        _fail("report.email_signoff must contain at least one line")
    cutoff = _date(raw["cutoff_date"], label="report.cutoff_date")
    as_of = _date(raw["as_of_date"], label="report.as_of_date")
    assert cutoff is not None and as_of is not None
    return ReportSettings(
        title=_string(raw["title"], label="report.title"),
        eyebrow=_string(raw["eyebrow"], label="report.eyebrow"),
        subtitle=_string(raw["subtitle"], label="report.subtitle"),
        organization=_string(raw["organization"], label="report.organization"),
        email_signoff=signoff,
        logo_data_uri=_logo(raw["logo_data_uri"]),
        confirmed_outstanding=(
            _money(raw["confirmed_outstanding"], label="report.confirmed_outstanding")
            or Decimal("0.00")
        ),
        cutoff_date=cutoff,
        policy_version=_string(raw["policy_version"], label="report.policy_version"),
        as_of_date=as_of,
    )


def _parse_provenance(value: Any) -> Provenance:
    raw = _object(
        value,
        label="provenance",
        keys=frozenset(
            {
                "mapped_sha256",
                "policy_sha256",
                "source_snapshot_sha256",
                "accounted_review_keys",
            }
        ),
    )
    accounted = _strings(raw["accounted_review_keys"], label="provenance.accounted_review_keys")
    if tuple(sorted(accounted)) != accounted or len(accounted) != len(set(accounted)):
        _fail("provenance.accounted_review_keys must contain unique sorted strings")
    return Provenance(
        mapped_sha256=_sha256(raw["mapped_sha256"], label="provenance.mapped_sha256"),
        policy_sha256=_sha256(raw["policy_sha256"], label="provenance.policy_sha256"),
        source_snapshot_sha256=_sha256(
            raw["source_snapshot_sha256"], label="provenance.source_snapshot_sha256"
        ),
        accounted_review_keys=accounted,
    )


def _parse_source_summary(value: Any, *, cutoff: date) -> SourceSummary:
    raw = _object(
        value,
        label="source_summary",
        keys=frozenset(
            {
                "mapped_rows",
                "mapped_submissions",
                "mapped_total",
                "first_received",
                "last_received",
            }
        ),
    )
    total = _money(raw["mapped_total"], label="source_summary.mapped_total")
    first = _date(raw["first_received"], label="source_summary.first_received")
    last = _date(raw["last_received"], label="source_summary.last_received")
    assert total is not None and first is not None and last is not None
    if first > last:
        _fail("source_summary first_received must not be after last_received")
    if first < cutoff:
        _fail("source_summary contains a received date before report.cutoff_date")
    return SourceSummary(
        mapped_rows=_integer(raw["mapped_rows"], label="source_summary.mapped_rows"),
        mapped_submissions=_integer(
            raw["mapped_submissions"], label="source_summary.mapped_submissions"
        ),
        mapped_total=total,
        first_received=first,
        last_received=last,
    )


def _parse_ticket_source(value: Any, *, label: str) -> TicketSource:
    raw = _object(
        value,
        label=label,
        keys=frozenset({"stated_total", "mapped_total", "categories", "flags"}),
    )
    categories = _strings(raw["categories"], label=f"{label}.categories")
    if len(categories) != len(set(categories)):
        _fail(f"{label}.categories must not contain duplicates")
    flags = _strings(raw["flags"], label=f"{label}.flags")
    if len(flags) != len(set(flags)):
        _fail(f"{label}.flags must not contain duplicates")
    return TicketSource(
        stated_total=_money(raw["stated_total"], label=f"{label}.stated_total", blank=True),
        mapped_total=_money(raw["mapped_total"], label=f"{label}.mapped_total", blank=True),
        categories=categories,
        flags=flags,
    )


def _parse_live(value: Any, *, label: str) -> LiveState:
    raw = _object(
        value,
        label=label,
        keys=frozenset(
            {
                "workflow_state",
                "decision",
                "payment_status",
                "payment_date",
                "confirmations",
            }
        ),
    )
    payment_status = _choice(
        raw["payment_status"],
        label=f"{label}.payment_status",
        choices=frozenset({"NOT_PAID", "PAID", "PAID_PRIOR"}),
    )
    payment_date = _date(raw["payment_date"], label=f"{label}.payment_date", blank=True)
    confirmations = _strings(raw["confirmations"], label=f"{label}.confirmations")
    if payment_status == "NOT_PAID" and (payment_date is not None or confirmations):
        _fail(f"{label} cannot contain payment evidence while payment_status is NOT_PAID")
    return LiveState(
        workflow_state=_choice(
            raw["workflow_state"],
            label=f"{label}.workflow_state",
            choices=frozenset({"ACTIVE", "SETTLED"}),
        ),
        decision=_choice(
            raw["decision"],
            label=f"{label}.decision",
            choices=frozenset({"UNREVIEWED", "APPROVED", "CLARIFICATION", "DECLINED"}),
        ),
        payment_status=payment_status,
        payment_date=payment_date,
        confirmations=confirmations,
    )


def _parse_review(value: Any, *, label: str) -> TicketReview:
    raw = _object(
        value,
        label=label,
        keys=frozenset(
            {"status", "action", "block", "asks", "note", "email_questions", "email_context"}
        ),
    )
    return TicketReview(
        status=_choice(
            raw["status"], label=f"{label}.status", choices=frozenset({"A", "C", "D", "Q"})
        ),
        action=_string(raw["action"], label=f"{label}.action"),
        block=_string(raw["block"], label=f"{label}.block"),
        asks=_strings(raw["asks"], label=f"{label}.asks"),
        note=_string(raw["note"], label=f"{label}.note"),
        email_questions=_strings(raw["email_questions"], label=f"{label}.email_questions"),
        email_context=_string(raw["email_context"], label=f"{label}.email_context", blank=True),
    )


def _parse_item(value: Any, *, label: str) -> ReviewItem:
    raw = _object(
        value,
        label=label,
        keys=frozenset(
            {
                "item_key",
                "source_index",
                "source_date",
                "source_description",
                "source_amount",
                "canonical_category",
                "display_date",
                "display_item",
                "reviewed_amount",
                "status",
                "why",
            }
        ),
    )
    status = _choice(raw["status"], label=f"{label}.status", choices=frozenset(STATUS_META))
    source_amount = _money(raw["source_amount"], label=f"{label}.source_amount", blank=True)
    reviewed_amount = _money(raw["reviewed_amount"], label=f"{label}.reviewed_amount", blank=True)
    if status == "-" and reviewed_amount is not None:
        _fail(f"{label} with status '-' must not carry a reviewed amount")
    return ReviewItem(
        item_key=_string(raw["item_key"], label=f"{label}.item_key"),
        source_index=_integer(raw["source_index"], label=f"{label}.source_index", minimum=1),
        source_date=_date(raw["source_date"], label=f"{label}.source_date", blank=True),
        source_description=_string(raw["source_description"], label=f"{label}.source_description"),
        source_amount=source_amount,
        canonical_category=_string(
            raw["canonical_category"], label=f"{label}.canonical_category", blank=True
        ),
        display_date=_date(raw["display_date"], label=f"{label}.display_date", blank=True),
        display_item=_string(raw["display_item"], label=f"{label}.display_item", blank=True),
        reviewed_amount=reviewed_amount,
        status=status,
        why=_string(raw["why"], label=f"{label}.why"),
    )


def _parse_message(value: Any, *, label: str) -> TicketMessage:
    raw = _object(
        value,
        label=label,
        keys=frozenset({"kind", "date", "mode", "body"}),
    )
    kind = _choice(raw["kind"], label=f"{label}.kind", choices=frozenset({"draft", "sent"}))
    mode = _choice(raw["mode"], label=f"{label}.mode", choices=frozenset({"generated", "verbatim"}))
    message_date = _date(raw["date"], label=f"{label}.date", blank=True)
    body = _string(raw["body"], label=f"{label}.body", blank=True)
    if kind == "sent" and (mode != "verbatim" or message_date is None or not body.strip()):
        _fail(f"{label} sent messages require a date and verbatim body")
    if kind == "draft" and message_date is not None:
        _fail(f"{label} draft messages must have a blank date")
    if mode == "generated" and body:
        _fail(f"{label} generated messages must have a blank body")
    if mode == "verbatim" and not body.strip():
        _fail(f"{label} verbatim messages require a body")
    return TicketMessage(kind=kind, date=message_date, mode=mode, body=body)


def _parse_supplemental_attachment(value: Any, *, label: str) -> SupplementalAttachment:
    raw = _object(
        value,
        label=label,
        keys=frozenset({"mime_type", "filename", "decoded_size", "content_sha256"}),
    )
    return SupplementalAttachment(
        mime_type=_string(raw["mime_type"], label=f"{label}.mime_type"),
        filename=_string(raw["filename"], label=f"{label}.filename", blank=True),
        decoded_size=_integer(raw["decoded_size"], label=f"{label}.decoded_size"),
        content_sha256=_sha256(raw["content_sha256"], label=f"{label}.content_sha256"),
    )


def _parse_supplemental(value: Any) -> SupplementalLedger:
    raw = _object(
        value,
        label="supplemental",
        keys=frozenset({"anchors_sha256", "evidence", "events", "unmatched"}),
    )
    raw_evidence = raw["evidence"]
    raw_events = raw["events"]
    raw_unmatched = raw["unmatched"]
    if not isinstance(raw_evidence, list):
        _fail("supplemental.evidence must be an array")
    if not isinstance(raw_events, list):
        _fail("supplemental.events must be an array")
    if not isinstance(raw_unmatched, list):
        _fail("supplemental.unmatched must be an array")

    evidence: list[SupplementalEvidence] = []
    for index, item in enumerate(raw_evidence):
        label = f"supplemental.evidence[{index}]"
        entry = _object(
            item,
            label=label,
            keys=frozenset(
                {
                    "evidence_key",
                    "source_type",
                    "message_id",
                    "in_reply_to",
                    "references",
                    "occurred_on",
                    "occurred_at",
                    "top_authored_sha256",
                    "evidence_sha256",
                    "attachments",
                    "record_sha256",
                }
            ),
        )
        record_sha256 = _sha256(entry["record_sha256"], label=f"{label}.record_sha256")
        record_payload = {key: entry[key] for key in entry if key != "record_sha256"}
        if _json_sha256(record_payload) != record_sha256:
            _fail(f"{label}.record_sha256 does not match the stored evidence metadata")
        raw_attachments = entry["attachments"]
        if not isinstance(raw_attachments, list):
            _fail(f"{label}.attachments must be an array")
        source_type = _choice(
            entry["source_type"],
            label=f"{label}.source_type",
            choices=frozenset({"MAIL", "OPERATOR_REVIEW", "OPERATOR_PAYMENT"}),
        )
        message_id = _string(entry["message_id"], label=f"{label}.message_id", blank=True)
        in_reply_to = _strings(entry["in_reply_to"], label=f"{label}.in_reply_to")
        references = _strings(entry["references"], label=f"{label}.references")
        if message_id and receipt_ingest.normalize_message_id(message_id) != message_id:
            _fail(f"{label}.message_id must be normalized")
        if any(receipt_ingest.normalize_message_id(item) != item for item in in_reply_to):
            _fail(f"{label}.in_reply_to must contain normalized Message-IDs")
        if any(receipt_ingest.normalize_message_id(item) != item for item in references):
            _fail(f"{label}.references must contain normalized Message-IDs")
        if len(in_reply_to) != len(set(in_reply_to)) or len(references) != len(set(references)):
            _fail(f"{label} ancestry must not contain duplicate Message-IDs")
        attachments = tuple(
            _parse_supplemental_attachment(
                attachment, label=f"{label}.attachments[{attachment_index}]"
            )
            for attachment_index, attachment in enumerate(raw_attachments)
        )
        if source_type == "OPERATOR_REVIEW" and (
            message_id
            or in_reply_to
            or references
            or attachments
            or entry["occurred_on"]
            or entry["occurred_at"]
        ):
            _fail(f"{label} operator reviews cannot impersonate mail evidence")
        if source_type == "OPERATOR_PAYMENT" and (
            message_id or in_reply_to or references or attachments or entry["occurred_at"]
        ):
            _fail(f"{label} operator payments cannot impersonate mail evidence")
        if source_type == "OPERATOR_PAYMENT" and not entry["occurred_on"]:
            _fail(f"{label} operator payments require a payment date")
        evidence_key = _string(entry["evidence_key"], label=f"{label}.evidence_key")
        expected_key_pattern = {
            "MAIL": _MAIL_KEY_RE,
            "OPERATOR_REVIEW": _OPERATOR_KEY_RE,
            "OPERATOR_PAYMENT": _OPERATOR_PAYMENT_KEY_RE,
        }[source_type]
        if expected_key_pattern.fullmatch(evidence_key) is None:
            _fail(f"{label}.evidence_key has the wrong stable-key shape")
        top_authored_sha256 = _sha256(
            entry["top_authored_sha256"], label=f"{label}.top_authored_sha256"
        )
        evidence_sha256 = _sha256(entry["evidence_sha256"], label=f"{label}.evidence_sha256")
        if source_type in {"OPERATOR_REVIEW", "OPERATOR_PAYMENT"} and (
            top_authored_sha256 != evidence_sha256
        ):
            _fail(f"{label} operator-authored digests must match")
        if source_type in {"OPERATOR_REVIEW", "OPERATOR_PAYMENT"} and not evidence_key.endswith(
            evidence_sha256
        ):
            _fail(f"{label} operator-authored key must match its digest")
        if source_type == "MAIL" and message_id:
            expected_mail_key = "mail:v1:" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()
            if evidence_key != expected_mail_key:
                _fail(f"{label} mail key must match its normalized Message-ID")
        evidence.append(
            SupplementalEvidence(
                evidence_key=evidence_key,
                source_type=source_type,
                message_id=message_id,
                in_reply_to=in_reply_to,
                references=references,
                occurred_on=_date(entry["occurred_on"], label=f"{label}.occurred_on", blank=True),
                occurred_at=_timestamp(
                    entry["occurred_at"], label=f"{label}.occurred_at", blank=True
                ),
                top_authored_sha256=top_authored_sha256,
                evidence_sha256=evidence_sha256,
                attachments=attachments,
                record_sha256=record_sha256,
            )
        )

    events: list[TicketEvent] = []
    event_kinds = frozenset(
        {
            "RECEIPT_RECEIVED",
            "CLARIFICATION_RECEIVED",
            "PAYMENT_RECORDED",
            "PAYMENT_DISCREPANCY",
            "PAYMENT_QUARANTINED",
            "APPROVAL_GRANTED",
            "APPROVAL_DECLINED",
            "APPROVAL_QUARANTINED",
            "OPERATOR_REVIEW",
        }
    )
    for index, item in enumerate(raw_events):
        label = f"supplemental.events[{index}]"
        entry = _object(
            item,
            label=label,
            keys=frozenset(
                {
                    "event_key",
                    "evidence_key",
                    "ticket_review_key",
                    "kind",
                    "occurred_on",
                    "occurred_at",
                    "evidence_sha256",
                    "summary",
                    "amount",
                    "reference",
                    "discrepancy",
                    "record_sha256",
                }
            ),
        )
        record_sha256 = _sha256(entry["record_sha256"], label=f"{label}.record_sha256")
        record_payload = {key: entry[key] for key in entry if key != "record_sha256"}
        if _json_sha256(record_payload) != record_sha256:
            _fail(f"{label}.record_sha256 does not match the stored event metadata")
        kind = _choice(entry["kind"], label=f"{label}.kind", choices=event_kinds)
        occurred_on = _date(entry["occurred_on"], label=f"{label}.occurred_on", blank=True)
        amount = _money(entry["amount"], label=f"{label}.amount", blank=True)
        reference = _string(entry["reference"], label=f"{label}.reference", blank=True)
        discrepancy = _string(entry["discrepancy"], label=f"{label}.discrepancy", blank=True)
        payment_kinds = {"PAYMENT_RECORDED", "PAYMENT_DISCREPANCY", "PAYMENT_QUARANTINED"}
        if kind in payment_kinds and (
            occurred_on is None or amount is None or not reference.strip()
        ):
            _fail(f"{label} payment events require date, amount, and reference")
        if kind == "PAYMENT_RECORDED" and discrepancy:
            _fail(f"{label} recorded payments cannot carry a discrepancy")
        if kind == "PAYMENT_DISCREPANCY" and not discrepancy:
            _fail(f"{label} payment discrepancies require discrepancy detail")
        if kind == "PAYMENT_QUARANTINED" and not discrepancy:
            _fail(f"{label} quarantined payments require quarantine detail")
        if kind not in payment_kinds and (amount is not None or reference or discrepancy):
            _fail(f"{label} non-payment events cannot carry payment fields")
        event_key = _string(entry["event_key"], label=f"{label}.event_key")
        if _EVENT_KEY_RE.fullmatch(event_key) is None:
            _fail(f"{label}.event_key has the wrong stable-key shape")
        events.append(
            TicketEvent(
                event_key=event_key,
                evidence_key=_string(entry["evidence_key"], label=f"{label}.evidence_key"),
                ticket_review_key=_string(
                    entry["ticket_review_key"], label=f"{label}.ticket_review_key"
                ),
                kind=kind,
                occurred_on=occurred_on,
                occurred_at=_timestamp(
                    entry["occurred_at"], label=f"{label}.occurred_at", blank=True
                ),
                evidence_sha256=_sha256(entry["evidence_sha256"], label=f"{label}.evidence_sha256"),
                summary=_string(entry["summary"], label=f"{label}.summary"),
                amount=amount,
                reference=reference,
                discrepancy=discrepancy,
                record_sha256=record_sha256,
            )
        )

    unmatched: list[UnmatchedEvidence] = []
    unmatched_reasons = frozenset(
        {
            "NO_EXACT_LINK",
            "AMBIGUOUS_LINK",
            "MISSING_MESSAGE_ID",
            "AUTHORIZATION_REJECTED",
            "PROPOSAL_AMBIGUOUS",
            "NO_ACTIONABLE_CONTENT",
            "PAYMENT_LINK_REJECTED",
            "PAYMENT_AMOUNT_MISMATCH",
        }
    )
    for index, item in enumerate(raw_unmatched):
        label = f"supplemental.unmatched[{index}]"
        entry = _object(
            item,
            label=label,
            keys=frozenset({"evidence_key", "reason"}),
        )
        unmatched.append(
            UnmatchedEvidence(
                evidence_key=_string(entry["evidence_key"], label=f"{label}.evidence_key"),
                reason=_choice(entry["reason"], label=f"{label}.reason", choices=unmatched_reasons),
            )
        )

    evidence_keys = [item.evidence_key for item in evidence]
    if evidence_keys != sorted(evidence_keys) or len(evidence_keys) != len(set(evidence_keys)):
        _fail("supplemental.evidence must have unique evidence_key values in sorted order")
    event_order = [(item.occurred_at, item.event_key) for item in events]
    if event_order != sorted(event_order) or len({item.event_key for item in events}) != len(
        events
    ):
        _fail("supplemental.events must have unique event_key values in deterministic order")
    unmatched_keys = [item.evidence_key for item in unmatched]
    if unmatched_keys != sorted(unmatched_keys) or len(unmatched_keys) != len(set(unmatched_keys)):
        _fail("supplemental.unmatched must have unique evidence_key values in sorted order")
    return SupplementalLedger(
        anchors_sha256=_sha256(raw["anchors_sha256"], label="supplemental.anchors_sha256"),
        evidence=tuple(evidence),
        events=tuple(events),
        unmatched=tuple(unmatched),
    )


def _rollup_status(items: Sequence[ReviewItem]) -> str:
    statuses = {item.status for item in items if item.status != "-"}
    if not statuses:
        _fail("a ticket must contain at least one adjudicated item")
    if statuses == {"A"}:
        return "A"
    if "C" in statuses:
        return "C"
    if "Q" in statuses:
        return "Q"
    return "D"


def _validate_ticket(ticket: Ticket, *, label: str) -> None:
    item_keys = [item.item_key for item in ticket.items]
    if len(item_keys) != len(set(item_keys)):
        _fail(f"{label}.items contains duplicate item_key values")
    item_indexes = [item.source_index for item in ticket.items]
    if len(item_indexes) != len(set(item_indexes)):
        _fail(f"{label}.items contains duplicate source_index values")
    if _rollup_status(ticket.items) != ticket.review.status:
        _fail(f"{label}.review.status does not match its item status roll-up")

    source_categories = tuple(
        dict.fromkeys(item.canonical_category for item in ticket.items if item.canonical_category)
    )
    if source_categories != ticket.source.categories:
        _fail(f"{label}.source.categories does not match the item categories in display order")
    source_total = sum(
        (item.source_amount for item in ticket.items if item.source_amount is not None),
        Decimal("0.00"),
    )
    if ticket.source.mapped_total is not None and source_total != ticket.source.mapped_total:
        _fail(f"{label}.source.mapped_total does not equal its source item sum")

    drafts = sum(message.kind == "draft" for message in ticket.messages)
    if drafts > 1:
        _fail(f"{label} tickets may contain at most one draft message")
    if ticket.live.workflow_state == "SETTLED":
        if ticket.review.status != "A" or ticket.live.decision != "APPROVED":
            _fail(f"{label} SETTLED tickets must be approved")
        if ticket.live.payment_status not in {"PAID", "PAID_PRIOR"}:
            _fail(f"{label} SETTLED tickets must carry paid status")

    expected_decision = {"A": "APPROVED", "C": "CLARIFICATION", "D": "DECLINED"}
    if ticket.live.decision != "UNREVIEWED":
        expected = expected_decision.get(ticket.review.status)
        if expected is None or ticket.live.decision != expected:
            _fail(f"{label}.live.decision does not match its review status")

    questions = ticket.review.email_questions or ticket.review.asks
    generated_draft = any(
        message.kind == "draft" and message.mode == "generated" for message in ticket.messages
    )
    if generated_draft and ticket.review.status == "C" and not questions:
        _fail(f"{label} generated clarification email requires at least one question")


def _parse_ticket(value: Any, *, index: int) -> Ticket:
    label = f"tickets[{index}]"
    raw = _object(
        value,
        label=label,
        keys=frozenset(
            {
                "review_key",
                "ref",
                "form_label",
                "origin",
                "display_order",
                "requestor_name",
                "form_type",
                "submitted",
                "submitted_label",
                "payment_method",
                "source_evidence_sha256",
                "source",
                "live",
                "review",
                "items",
                "messages",
                "archive_note",
            }
        ),
    )
    items_raw = raw["items"]
    messages_raw = raw["messages"]
    if not isinstance(items_raw, list) or not items_raw:
        _fail(f"{label}.items must be a nonempty array")
    if not isinstance(messages_raw, list):
        _fail(f"{label}.messages must be an array")
    submitted = _date(raw["submitted"], label=f"{label}.submitted")
    assert submitted is not None
    ticket = Ticket(
        review_key=_string(raw["review_key"], label=f"{label}.review_key"),
        ref=_string(raw["ref"], label=f"{label}.ref"),
        form_label=_string(raw["form_label"], label=f"{label}.form_label", blank=True),
        origin=_choice(
            raw["origin"],
            label=f"{label}.origin",
            choices=frozenset({"submission", "legacy"}),
        ),
        display_order=_integer(raw["display_order"], label=f"{label}.display_order", minimum=1),
        requestor_name=_string(raw["requestor_name"], label=f"{label}.requestor_name"),
        form_type=_string(raw["form_type"], label=f"{label}.form_type"),
        submitted=submitted,
        submitted_label=_string(
            raw["submitted_label"], label=f"{label}.submitted_label", blank=True
        ),
        payment_method=_string(raw["payment_method"], label=f"{label}.payment_method"),
        source_evidence_sha256=_sha256(
            raw["source_evidence_sha256"], label=f"{label}.source_evidence_sha256"
        ),
        source=_parse_ticket_source(raw["source"], label=f"{label}.source"),
        live=_parse_live(raw["live"], label=f"{label}.live"),
        review=_parse_review(raw["review"], label=f"{label}.review"),
        items=tuple(
            _parse_item(item, label=f"{label}.items[{item_index}]")
            for item_index, item in enumerate(items_raw)
        ),
        messages=tuple(
            _parse_message(message, label=f"{label}.messages[{message_index}]")
            for message_index, message in enumerate(messages_raw)
        ),
        archive_note=_string(raw["archive_note"], label=f"{label}.archive_note", blank=True),
    )
    _validate_ticket(ticket, label=label)
    return ticket


def _parse_appendix(value: Any) -> Appendix:
    raw = _object(
        value,
        label="appendix",
        keys=frozenset({"amendments", "cfo_checks", "excluded", "defects"}),
    )
    amendments_raw = raw["amendments"]
    checks_raw = raw["cfo_checks"]
    excluded_raw = raw["excluded"]
    if not isinstance(amendments_raw, list):
        _fail("appendix.amendments must be an array")
    if not isinstance(checks_raw, list):
        _fail("appendix.cfo_checks must be an array")
    if not isinstance(excluded_raw, list):
        _fail("appendix.excluded must be an array")

    amendments: list[Amendment] = []
    for index, item in enumerate(amendments_raw):
        label = f"appendix.amendments[{index}]"
        entry = _object(item, label=label, keys=frozenset({"title", "body", "effect", "scope"}))
        amendments.append(
            Amendment(
                title=_string(entry["title"], label=f"{label}.title"),
                body=_string(entry["body"], label=f"{label}.body"),
                effect=_string(entry["effect"], label=f"{label}.effect"),
                scope=_string(entry["scope"], label=f"{label}.scope"),
            )
        )

    checks: list[CfoCheck] = []
    for index, item in enumerate(checks_raw):
        label = f"appendix.cfo_checks[{index}]"
        entry = _object(item, label=label, keys=frozenset({"ticket", "question", "answer"}))
        checks.append(
            CfoCheck(
                ticket=_string(entry["ticket"], label=f"{label}.ticket"),
                question=_string(entry["question"], label=f"{label}.question"),
                answer=_string(entry["answer"], label=f"{label}.answer"),
            )
        )

    excluded: list[ExcludedEntry] = []
    for index, item in enumerate(excluded_raw):
        label = f"appendix.excluded[{index}]"
        entry = _object(item, label=label, keys=frozenset({"label", "detail"}))
        excluded.append(
            ExcludedEntry(
                label=_string(entry["label"], label=f"{label}.label"),
                detail=_string(entry["detail"], label=f"{label}.detail"),
            )
        )

    return Appendix(
        amendments=tuple(amendments),
        cfo_checks=tuple(checks),
        excluded=tuple(excluded),
        defects=_strings(raw["defects"], label="appendix.defects"),
    )


def _reject_json_constant(value: str) -> NoReturn:
    raise ReimbursementReportError(f"bundle contains forbidden JSON constant {value}")


def migrate_bundle(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep-copied schema-v2 bundle, explicitly migrating strict schema-v1 input."""

    if not isinstance(value, Mapping):
        _fail("bundle must be an object")
    version = value.get("schema_version")
    if isinstance(version, bool) or version not in {1, SCHEMA_VERSION}:
        _fail(f"bundle.schema_version must equal 1 or {SCHEMA_VERSION}")
    if version == SCHEMA_VERSION:
        return copy.deepcopy(dict(value))
    expected_v1 = frozenset(
        {"schema_version", "report", "provenance", "source_summary", "tickets", "appendix"}
    )
    if frozenset(value) != expected_v1:
        _fail("bundle has invalid keys")
    migrated = copy.deepcopy(dict(value))
    empty_anchor_payload = {
        "schema_version": 1,
        "actors": {"payment_operators": [], "secondary_approvers": []},
        "thread_anchors": [],
        "direct_links": [],
        "operator_reviews": [],
    }
    migrated["schema_version"] = SCHEMA_VERSION
    migrated["supplemental"] = {
        "anchors_sha256": hashlib.sha256(
            json.dumps(
                empty_anchor_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "evidence": [],
        "events": [],
        "unmatched": [],
    }
    return migrated


def _load_data(value: Any) -> ReimbursementReport:
    root = _object(
        value,
        label="bundle",
        keys=frozenset(
            {
                "schema_version",
                "report",
                "provenance",
                "source_summary",
                "tickets",
                "appendix",
                "supplemental",
            }
        ),
    )
    if root["schema_version"] != SCHEMA_VERSION or isinstance(root["schema_version"], bool):
        _fail(f"bundle.schema_version must equal {SCHEMA_VERSION}")
    settings = _parse_settings(root["report"])
    tickets_raw = root["tickets"]
    if not isinstance(tickets_raw, list) or not tickets_raw:
        _fail("bundle.tickets must be a nonempty array")
    tickets = tuple(
        sorted(
            (_parse_ticket(ticket, index=index) for index, ticket in enumerate(tickets_raw)),
            key=lambda ticket: ticket.display_order,
        )
    )
    for field, values in (
        ("review_key", [ticket.review_key for ticket in tickets]),
        ("ref/form_label", [(ticket.ref, ticket.form_label) for ticket in tickets]),
        ("display_order", [ticket.display_order for ticket in tickets]),
    ):
        if len(values) != len(set(values)):
            _fail(f"bundle.tickets contains duplicate {field} values")
    submission_refs = [ticket.ref for ticket in tickets if ticket.origin == "submission"]
    if len(submission_refs) != len(set(submission_refs)):
        _fail("bundle.tickets contains duplicate submission ref values")
    supplemental = _parse_supplemental(root["supplemental"])
    report = ReimbursementReport(
        settings=settings,
        provenance=_parse_provenance(root["provenance"]),
        source_summary=_parse_source_summary(root["source_summary"], cutoff=settings.cutoff_date),
        tickets=tickets,
        appendix=_parse_appendix(root["appendix"]),
        supplemental=supplemental,
    )
    submission_keys = {ticket.review_key for ticket in tickets if ticket.origin == "submission"}
    accounted_keys = set(report.provenance.accounted_review_keys)
    if not submission_keys.issubset(accounted_keys):
        _fail("every rendered submission ticket must be present in accounted_review_keys")
    if len(report.provenance.accounted_review_keys) != report.source_summary.mapped_submissions:
        _fail("accounted_review_keys count must match source_summary.mapped_submissions")
    ticket_keys = {ticket.review_key for ticket in tickets}
    evidence_by_key = {item.evidence_key: item for item in supplemental.evidence}
    operator_review_targets = {
        event.ticket_review_key for event in supplemental.events if event.kind == "OPERATOR_REVIEW"
    }
    event_evidence_keys = {event.evidence_key for event in supplemental.events}
    unmatched_evidence_keys = {item.evidence_key for item in supplemental.unmatched}
    if event_evidence_keys & unmatched_evidence_keys:
        _fail("supplemental evidence cannot be both linked and unmatched")
    if event_evidence_keys | unmatched_evidence_keys != set(evidence_by_key):
        _fail("every supplemental evidence record must be linked or unmatched")
    for event in supplemental.events:
        evidence = evidence_by_key.get(event.evidence_key)
        if evidence is None or evidence.evidence_sha256 != event.evidence_sha256:
            _fail("supplemental event evidence digest does not match its accounted record")
        if event.occurred_on != evidence.occurred_on:
            _fail("supplemental event date does not match its accounted evidence")
        if event.occurred_at != evidence.occurred_at:
            _fail("supplemental event timestamp does not match its accounted evidence")
        if event.ticket_review_key not in ticket_keys:
            _fail("supplemental event targets an unknown ticket review key")
        expected_event_key = (
            "event:v1:"
            + hashlib.sha256(
                f"{event.evidence_key}\0{event.ticket_review_key}\0{event.kind}".encode()
            ).hexdigest()
        )
        if event.event_key != expected_event_key:
            _fail("supplemental event key does not match its scoped event payload")
        if event.kind == "OPERATOR_REVIEW" and evidence.source_type != "OPERATOR_REVIEW":
            _fail("operator review events require operator-review evidence")
        if evidence.source_type == "OPERATOR_PAYMENT" and event.kind not in {
            "PAYMENT_RECORDED",
            "PAYMENT_DISCREPANCY",
            "PAYMENT_QUARANTINED",
        }:
            _fail("operator payment evidence can support only payment lifecycle events")
        if event.kind != "OPERATOR_REVIEW" and evidence.source_type not in {
            "MAIL",
            "OPERATOR_PAYMENT",
        }:
            _fail("mail lifecycle events require mail or operator-payment evidence")
        target = next(ticket for ticket in tickets if ticket.review_key == event.ticket_review_key)
        source_total = sum(
            (item.source_amount for item in target.items if item.source_amount is not None),
            Decimal("0.00"),
        )
        if event.kind == "PAYMENT_RECORDED" and (
            target.live.workflow_state != "SETTLED"
            or target.live.decision != "APPROVED"
            or target.live.payment_status not in {"PAID", "PAID_PRIOR"}
            or event.amount != source_total
        ):
            _fail(
                "recorded payment events require the linked ticket total to match and be approved "
                "and settled"
            )
        if event.kind == "PAYMENT_DISCREPANCY" and event.amount == source_total:
            _fail("payment discrepancy events must differ from the linked ticket total")
        if event.kind == "PAYMENT_QUARANTINED" and target.live.payment_status != "NOT_PAID":
            _fail("quarantined payment events cannot settle the linked ticket")
        if (
            event.kind == "APPROVAL_GRANTED"
            and target.live.decision == "UNREVIEWED"
            and event.ticket_review_key not in operator_review_targets
        ):
            _fail("granted approval events must update the linked recorded decision")
    operator_payment_keys = {
        evidence.evidence_key
        for evidence in supplemental.evidence
        if evidence.source_type == "OPERATOR_PAYMENT"
    }
    for evidence_key in operator_payment_keys:
        matching_events = [
            event for event in supplemental.events if event.evidence_key == evidence_key
        ]
        if len(matching_events) != 1:
            _fail("operator payment evidence must support exactly one scoped payment event")
    for unmatched_item in supplemental.unmatched:
        evidence = evidence_by_key.get(unmatched_item.evidence_key)
        if evidence is None or evidence.source_type != "MAIL":
            _fail("unmatched entries require accounted mail evidence")
    return report


def load_bundle(path: Path) -> ReimbursementReport:
    """Load schema-v2, explicitly migrating a strict schema-v1 private bundle in memory."""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReimbursementReportError("private reimbursement bundle is unavailable") from exc
    try:
        value = json.loads(raw, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise ReimbursementReportError("private reimbursement bundle is invalid JSON") from exc
    if not isinstance(value, dict):
        _fail("bundle must be an object")
    return _load_data(migrate_bundle(value))


def _format_money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}"


def _format_date(value: date | None) -> str:
    return "—" if value is None else value.isoformat()


def _payment_confirmation(payment_method: str) -> str:
    lowered = payment_method.casefold()
    if "zelle" in lowered:
        return "Zelle confirmation: [ZELLE CONFIRMATION]"
    if "check" in lowered:
        return "Check number: [CHECK NUMBER]"
    return "Check number or Zelle confirmation: [CHECK NUMBER OR ZELLE CONFIRMATION]"


def _generated_email(ticket: Ticket, signoff: Sequence[str]) -> str:
    questions = ticket.review.email_questions or ticket.review.asks
    confirmation = _payment_confirmation(ticket.payment_method)
    if ticket.review.status == "A":
        message = (
            f"Your {_format_money(ticket.total)} reimbursement has been approved and sent by "
            f"{ticket.payment_method}.\n{confirmation}"
        )
    elif ticket.review.status == "C":
        remaining = ticket.total - ticket.approved
        question_text = "\n".join(f"- {question}" for question in questions)
        if ticket.approved:
            message = (
                f"We approved {_format_money(ticket.approved)} of your "
                f"{_format_money(ticket.total)} request and sent that amount by "
                f"{ticket.payment_method}.\n{confirmation}\n\n"
                f"Before we can reimburse the remaining {_format_money(remaining)}, "
                f"please reply with:\n{question_text}"
            )
        else:
            message = (
                f"Before we can reimburse your {_format_money(ticket.total)} request, "
                f"please reply with:\n{question_text}"
            )
    elif ticket.review.status == "D":
        message = (
            f"We are unable to reimburse your {_format_money(ticket.total)} request.\n\n"
            f"{ticket.review.block}"
        )
    else:
        if questions:
            question_text = "\n".join(f"- {question}" for question in questions)
            message = (
                f"We are reviewing your {_format_money(ticket.total)} reimbursement request. "
                f"Please reply with:\n{question_text}"
            )
        else:
            message = (
                f"We are reviewing your {_format_money(ticket.total)} reimbursement request. "
                "We will follow up if any additional information is needed."
            )
    if ticket.review.email_context:
        message += f"\n\n{ticket.review.email_context}"
    return f"Hello {ticket.first_name},\n\n{message}\n\n" + "\n".join(signoff)


def _email_blocks(ticket: Ticket, signoff: Sequence[str]) -> tuple[EmailBlock, ...]:
    blocks: list[EmailBlock] = []
    for message in ticket.messages:
        body = _generated_email(ticket, signoff) if message.mode == "generated" else message.body
        blocks.append(EmailBlock(kind=message.kind, date=message.date, body=body))
    return tuple(blocks)


def render_html(report: ReimbursementReport) -> str:
    """Render a validated report to deterministic, self-contained HTML."""

    environment = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = environment.get_template(_TEMPLATE_NAME)
    email_blocks = {
        ticket.review_key: _email_blocks(ticket, report.settings.email_signoff)
        for ticket in report.tickets
    }
    ticket_events = {
        ticket.review_key: report.events_for(ticket.review_key) for ticket in report.tickets
    }
    evidence_by_key = {evidence.evidence_key: evidence for evidence in report.supplemental.evidence}
    rendered = template.render(
        report=report,
        summary=report.summary,
        status_meta=STATUS_META,
        email_blocks=email_blocks,
        ticket_events=ticket_events,
        evidence_by_key=evidence_by_key,
        money=_format_money,
        iso_date=_format_date,
    )
    return rendered.rstrip() + "\n"


def write_html_atomic(path: Path, html_text: str) -> None:
    """Atomically replace ``path`` with a complete UTF-8 HTML document."""

    if not html_text.lstrip().lower().startswith("<!doctype html>"):
        _fail("refusing to write an incomplete reimbursement report")
    output = path.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(html_text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def build_report(data_path: Path, output_path: Path) -> BuildResult:
    """Load, validate, render, and atomically write one private reimbursement report."""

    report = load_bundle(data_path)
    html_text = render_html(report)
    encoded = html_text.encode("utf-8")
    write_html_atomic(output_path, html_text)
    return BuildResult(
        output_path=output_path.resolve(),
        sha256=hashlib.sha256(encoded).hexdigest(),
        bytes_written=len(encoded),
        summary=report.summary,
    )
