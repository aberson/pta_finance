"""Strict private anchors and deterministic reimbursement event helpers.

This module contains no mailbox or Sheet I/O.  It validates the optional, gitignored operator
anchor file used by :mod:`pta_finance.reimbursement_pipeline` and provides small pure parsers for
payment confirmations, scoped recommendation proposals, and approval replies.  Sender, subject,
message-id, and authored mail text are deliberately absent from public error messages.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NoReturn

from pta_finance import receipt_ingest

__all__ = [
    "AnchorConfig",
    "DirectLink",
    "OperatorReview",
    "OperatorReviewItem",
    "OperatorPayment",
    "PaymentBinding",
    "PaymentEvidence",
    "PaymentLink",
    "ProposalRecommendation",
    "ReimbursementEventError",
    "ThreadAnchor",
    "TicketSelector",
    "classify_approval_reply",
    "empty_anchor_config",
    "load_anchor_config",
    "parse_payment_evidence",
    "parse_payment_evidence_blocks",
    "parse_proposal_recommendations",
]


class ReimbursementEventError(ValueError):
    """A private anchor or deterministic event payload is unsafe or ambiguous."""


@dataclass(frozen=True)
class TicketSelector:
    """All stable public coordinates required to select exactly one private ticket."""

    review_key: str
    ref: str
    form_label: str


@dataclass(frozen=True)
class ThreadAnchor:
    """An outbound Message-ID that anchors a case or a scoped approval proposal."""

    message_id: str
    purpose: str
    tickets: tuple[TicketSelector, ...]


@dataclass(frozen=True)
class DirectLink:
    """An explicit link for non-threaded inbound evidence."""

    message_id: str
    purpose: str
    ticket: TicketSelector


@dataclass(frozen=True)
class PaymentBinding:
    """One private reference-digest binding from a payment block to a ticket."""

    ticket: TicketSelector
    reference_sha256: str


@dataclass(frozen=True)
class PaymentLink:
    """An exact, non-propagating Message-ID link for one atomic payment message."""

    message_id: str
    bindings: tuple[PaymentBinding, ...]


@dataclass(frozen=True)
class OperatorReviewItem:
    """One explicit operator-authored item override; never payment authorization."""

    source_index: int
    status: str
    why: str
    reviewed_amount: str


@dataclass(frozen=True)
class OperatorReview:
    """A strict item-complete operator review from the private anchor file."""

    ticket: TicketSelector
    record_decision: bool
    items: tuple[OperatorReviewItem, ...]
    action: str
    block: str
    asks: tuple[str, ...]
    note: str
    email_questions: tuple[str, ...]
    email_context: str
    evidence_sha256: str


@dataclass(frozen=True)
class OperatorPayment:
    """An explicit audited payment record for which no archived mail exists."""

    ticket: TicketSelector
    record_payment: bool
    date: str
    amount: str
    reference: str
    audit_note: str
    evidence_sha256: str


@dataclass(frozen=True)
class AnchorConfig:
    """Normalized, digestable private supplemental-evidence configuration."""

    schema_version: int
    payment_operators: tuple[str, ...]
    secondary_approvers: tuple[str, ...]
    thread_anchors: tuple[ThreadAnchor, ...]
    direct_links: tuple[DirectLink, ...]
    operator_reviews: tuple[OperatorReview, ...]
    payment_links: tuple[PaymentLink, ...]
    operator_payments: tuple[OperatorPayment, ...]
    sha256: str


@dataclass(frozen=True)
class PaymentEvidence:
    """A conservatively parsed payment confirmation."""

    amount: Decimal
    reference: str


@dataclass(frozen=True)
class ProposalRecommendation:
    """One unambiguous ticket section from a deterministic proposal message."""

    ref: str
    statuses: tuple[str, ...]
    all_items: bool = False


_MONEY_RE = re.compile(r"(?<![A-Za-z0-9])\$?\s*([0-9]+(?:,[0-9]{3})*\.[0-9]{2})(?![0-9])")
_REFERENCE_RE = re.compile(
    r"\b(?:(?:confirmation|reference|ref)(?:\s+(?:number|no\.?))?"
    r"|check\s+(?:number|no\.?)|check\s*#)\s*[:#-]?\s*"
    r"((?=[A-Za-z0-9-]{3,64}\b)(?=[A-Za-z0-9-]*[0-9])"
    r"[A-Za-z0-9][A-Za-z0-9-]{2,63})\b",
    re.IGNORECASE,
)
_PAID_SIGNAL_RE = re.compile(
    r"\b(?:paid|payment\s+(?:was\s+)?(?:made|sent)|(?:funds|money)\s+(?:were\s+)?transferred)\b",
    re.IGNORECASE,
)
_CHECK_SENT_SIGNAL_RE = re.compile(
    r"\bcheck\s+(?:number|no\.?|#)\s*[A-Za-z0-9-]{3,64}\b[^\r\n.]*\b(?:sent|mailed|issued)\b",
    re.IGNORECASE,
)
_PAYMENT_NEGATIVE_RE = re.compile(
    r"\b(?:not|never|cancelled|canceled|void(?:ed)?|disput(?:e|ed)|reversed?|failed|pending)\b"
    r"|\b(?:wrote|said|reported)\s*:|\bplease\s+confirm\b|\bwhether\b|\?"
    r"|\b(?:for\s+context|fyi|according\s+to)\b|(?:^|\n)\s*>",
    re.IGNORECASE,
)
_ZELLE_CONTEXT_RE = re.compile(r"\bconfirmation\s+info\s+is\s+below\b", re.IGNORECASE)
_ZELLE_MARKER_RE = re.compile(r"^zelle\s*-\s*[0-9]{6,20}$", re.IGNORECASE)
_BARE_CONFIRMATION_RE = re.compile(
    r"(?=[A-Z0-9]{10,20}$)(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*[0-9])[A-Z0-9]+"
)
_BARE_MONEY_RE = re.compile(r"^\$?\s*([0-9]+(?:,[0-9]{3})*\.[0-9]{2})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_VALUE_RE = re.compile(
    r"(?=[A-Za-z0-9-]{3,64}$)(?=[A-Za-z0-9-]*[0-9])"
    r"[A-Za-z0-9][A-Za-z0-9-]{2,63}"
)
_GENERATED_SINGLE_INTRO_RE = re.compile(
    r"^Your \$([0-9]+(?:,[0-9]{3})*\.[0-9]{2}) reimbursement has been approved "
    r"and sent by Zelle\.$"
)
_GENERATED_CONFIRMATION_RE = re.compile(
    r"^Zelle confirmation: "
    r"((?=[A-Za-z0-9-]{3,64}$)(?=[A-Za-z0-9-]*[0-9])"
    r"[A-Za-z0-9][A-Za-z0-9-]{2,63})$"
)
_PAYMENT_GREETING_RE = re.compile(
    r"^(?:(?:Hello|Hi|Dear)(?: [^\r\n]{1,80})?|Good (?:morning|afternoon|evening)"
    r"(?: [^\r\n]{1,80})?)[,!]$"
)
_SENT_DESTINATION_RE = re.compile(r"^Zelle - \S(?:.*\S)?$")
_SENT_THANK_YOU_RE = re.compile(r"^(?:Thank you[!,]|Thank you for supporting our classrooms!)$")
_PROPOSAL_REF_RE = re.compile(
    r"^\s*(?:\[|#{1,6}\s*)?"
    r"(?P<refs>[A-Z][A-Z0-9-]*\d(?:\s*,\s*[A-Z][A-Z0-9-]*\d)*)"
    r"(?:\])?\s*[:.-]?\s*$"
)
_PROPOSAL_ACTION_RE = re.compile(
    r"^\s*(?:[*-]\s+)?(approve(?:\s+as\s+is)?|clarification)\b", re.IGNORECASE
)
_PROPOSAL_APPROVE_ALL_RE = re.compile(
    r"^\s*(?:[*-]\s+)?approve\s+as\s+is\s*[.!]?\s*$", re.IGNORECASE
)
_APPROVAL_GREETING_RE = re.compile(
    r"^(?:(?:hi|hello|dear)(?:\s+[^.!?]{1,80})?"
    r"|good\s+(?:morning|afternoon|evening)(?:\s+[^.!?]{1,80})?)[,.!:]?$",
    re.IGNORECASE,
)
_ASSESSMENT_AGREEMENT_RE = re.compile(
    r"^i\s+agree\s+with\s+your\s+assessment(?=$|[.!](?:\s|$)|\s+and\s+\S)",
    re.IGNORECASE,
)
_APPROVAL_MODIFIER_RE = re.compile(
    r"\b(?:no|but|except|however|(?:al)?though|unless|only|provided|cannot|without"
    r"|apart\s+from|with\s+the\s+exception\s+of"
    r"|reject(?:ed|s|ing|ion)?|exclud(?:e|ed|es|ing|ion)"
    r"|oppos(?:e|ed|es|ing|ition)"
    r"|disagree(?:d|s|ing|ment)?|disput(?:e|ed|es|ing)"
    r"|declin(?:e|ed|es|ing))\b"
    r"|\b(?:do[-\s]+not|subject[-\s]+to)\b|\bnot\b|\b[a-z]+n['’]t\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+$")


def _fail(message: str) -> NoReturn:
    raise ReimbursementEventError(message)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _object(value: Any, *, label: str, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != keys:
        _fail(f"{label} must contain exactly the documented keys")
    return value


def _string(value: Any, *, label: str, blank: bool = False) -> str:
    if not isinstance(value, str) or (not blank and not value.strip()):
        qualifier = "a string" if blank else "a nonblank string"
        _fail(f"{label} must be {qualifier}")
    return value.strip() if not blank else value


def _strings(value: Any, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(f"{label} must be an array")
    result = tuple(_string(item, label=f"{label} item") for item in value)
    if len(result) != len(set(result)):
        _fail(f"{label} must not contain duplicates")
    return result


def _selector(value: Any, *, label: str) -> TicketSelector:
    raw = _object(
        value,
        label=label,
        keys=frozenset({"review_key", "ref", "form_label"}),
    )
    return TicketSelector(
        review_key=_string(raw["review_key"], label=f"{label}.review_key"),
        ref=_string(raw["ref"], label=f"{label}.ref"),
        form_label=_string(raw["form_label"], label=f"{label}.form_label", blank=True),
    )


def _message_id(value: Any, *, label: str) -> str:
    raw = _string(value, label=label)
    normalized = receipt_ingest.normalize_message_id(raw)
    if not normalized:
        _fail(f"{label} must be one canonical RFC Message-ID")
    return normalized


def _actor_addresses(value: Any, *, label: str) -> tuple[str, ...]:
    raw = _strings(value, label=label)
    normalized = tuple(address.casefold() for address in raw)
    if any(not _EMAIL_RE.fullmatch(address) for address in normalized):
        _fail(f"{label} must contain mailbox addresses")
    if tuple(sorted(normalized)) != normalized:
        _fail(f"{label} must contain unique sorted mailbox addresses")
    return normalized


def _sha256(value: Any, *, label: str) -> str:
    raw = _string(value, label=label)
    if _SHA256_RE.fullmatch(raw) is None:
        _fail(f"{label} must be a lowercase SHA-256 digest")
    return raw


def _payment_reference(value: Any, *, label: str) -> str:
    raw = _string(value, label=label)
    if _REFERENCE_VALUE_RE.fullmatch(raw) is None:
        _fail(f"{label} must be one exact confirmation reference")
    return raw


def _payment_amount(value: Any, *, label: str) -> str:
    raw = _string(value, label=label)
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ReimbursementEventError(f"{label} must be exact positive money") from exc
    if not amount.is_finite() or amount <= 0 or f"{amount:.2f}" != raw:
        _fail(f"{label} must be exact positive money")
    return raw


def _payment_date(value: Any, *, label: str) -> str:
    raw = _string(value, label=label)
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ReimbursementEventError(f"{label} must be an ISO calendar date") from exc
    if parsed.isoformat() != raw:
        _fail(f"{label} must be an ISO calendar date")
    return raw


def empty_anchor_config() -> AnchorConfig:
    """Return the canonical no-anchor configuration used when the private file is absent."""

    payload = {
        "schema_version": 1,
        "actors": {"payment_operators": [], "secondary_approvers": []},
        "thread_anchors": [],
        "direct_links": [],
        "operator_reviews": [],
    }
    return AnchorConfig(
        schema_version=1,
        payment_operators=(),
        secondary_approvers=(),
        thread_anchors=(),
        direct_links=(),
        operator_reviews=(),
        payment_links=(),
        operator_payments=(),
        sha256=_digest(payload),
    )


def load_anchor_config(path: Path | None) -> AnchorConfig:
    """Load one strict private anchor file, or an empty config when it does not exist.

    The path is caller-controlled and expected to be gitignored.  Errors intentionally identify
    only structural positions; they never echo a private address, Message-ID, ticket, or text.
    """

    if path is None or not path.exists():
        return empty_anchor_config()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReimbursementEventError("private reimbursement anchors are not valid JSON") from exc
    if not isinstance(value, dict):
        _fail("anchor root must contain exactly the documented keys")
    version = value.get("schema_version")
    if isinstance(version, bool) or version not in {1, 2}:
        _fail("anchor schema_version must equal 1 or 2")
    root_keys = {
        "schema_version",
        "actors",
        "thread_anchors",
        "direct_links",
        "operator_reviews",
    }
    if version == 2:
        root_keys.update({"payment_links", "operator_payments"})
    root = _object(value, label="anchor root", keys=frozenset(root_keys))
    actors = _object(
        root["actors"],
        label="anchor actors",
        keys=frozenset({"payment_operators", "secondary_approvers"}),
    )
    payment_operators = _actor_addresses(
        actors["payment_operators"], label="anchor payment operators"
    )
    secondary_approvers = _actor_addresses(
        actors["secondary_approvers"], label="anchor secondary approvers"
    )

    raw_threads = root["thread_anchors"]
    if not isinstance(raw_threads, list):
        _fail("anchor thread_anchors must be an array")
    threads: list[ThreadAnchor] = []
    for index, value_item in enumerate(raw_threads):
        raw = _object(
            value_item,
            label=f"thread_anchors[{index}]",
            keys=frozenset({"message_id", "purpose", "tickets"}),
        )
        purpose = _string(raw["purpose"], label=f"thread_anchors[{index}].purpose")
        if purpose not in {"CASE", "APPROVAL_PROPOSAL"}:
            _fail("thread anchor purpose must be CASE or APPROVAL_PROPOSAL")
        raw_tickets = raw["tickets"]
        if not isinstance(raw_tickets, list) or not raw_tickets:
            _fail("thread anchor tickets must be a nonempty array")
        tickets = tuple(
            _selector(item, label=f"thread_anchors[{index}].tickets item") for item in raw_tickets
        )
        if len(tickets) != len(set(tickets)):
            _fail("thread anchor tickets must not contain duplicates")
        if purpose == "CASE" and len(tickets) != 1:
            _fail("CASE thread anchors must select exactly one ticket")
        threads.append(
            ThreadAnchor(
                message_id=_message_id(
                    raw["message_id"], label=f"thread_anchors[{index}].message_id"
                ),
                purpose=purpose,
                tickets=tickets,
            )
        )

    raw_direct = root["direct_links"]
    if not isinstance(raw_direct, list):
        _fail("anchor direct_links must be an array")
    direct: list[DirectLink] = []
    for index, value_item in enumerate(raw_direct):
        raw = _object(
            value_item,
            label=f"direct_links[{index}]",
            keys=frozenset({"message_id", "purpose", "ticket"}),
        )
        purpose = _string(raw["purpose"], label=f"direct_links[{index}].purpose")
        if purpose not in {"CASE", "RECEIPT", "CLARIFICATION"}:
            _fail("direct link purpose is invalid")
        direct.append(
            DirectLink(
                message_id=_message_id(
                    raw["message_id"], label=f"direct_links[{index}].message_id"
                ),
                purpose=purpose,
                ticket=_selector(raw["ticket"], label=f"direct_links[{index}].ticket"),
            )
        )

    raw_payment_links = root.get("payment_links", [])
    if not isinstance(raw_payment_links, list):
        _fail("anchor payment_links must be an array")
    payment_links: list[PaymentLink] = []
    for index, value_item in enumerate(raw_payment_links):
        raw = _object(
            value_item,
            label=f"payment_links[{index}]",
            keys=frozenset({"message_id", "bindings"}),
        )
        raw_bindings = raw["bindings"]
        if not isinstance(raw_bindings, list) or not raw_bindings:
            _fail("payment link bindings must be a nonempty array")
        bindings: list[PaymentBinding] = []
        for binding_index, value_binding in enumerate(raw_bindings):
            binding = _object(
                value_binding,
                label=f"payment_links[{index}].bindings[{binding_index}]",
                keys=frozenset({"ticket", "reference_sha256"}),
            )
            bindings.append(
                PaymentBinding(
                    ticket=_selector(
                        binding["ticket"],
                        label=f"payment_links[{index}].bindings[{binding_index}].ticket",
                    ),
                    reference_sha256=_sha256(
                        binding["reference_sha256"],
                        label=(
                            f"payment_links[{index}].bindings[{binding_index}].reference_sha256"
                        ),
                    ),
                )
            )
        binding_tickets = [binding.ticket for binding in bindings]
        binding_references = [binding.reference_sha256 for binding in bindings]
        if len(binding_tickets) != len(set(binding_tickets)):
            _fail("payment link bindings must select unique tickets")
        if len(binding_references) != len(set(binding_references)):
            _fail("payment link bindings must contain unique reference digests")
        payment_links.append(
            PaymentLink(
                message_id=_message_id(
                    raw["message_id"], label=f"payment_links[{index}].message_id"
                ),
                bindings=tuple(sorted(bindings, key=lambda item: item.reference_sha256)),
            )
        )

    raw_reviews = root["operator_reviews"]
    if not isinstance(raw_reviews, list):
        _fail("anchor operator_reviews must be an array")
    reviews: list[OperatorReview] = []
    for index, value_item in enumerate(raw_reviews):
        raw = _object(
            value_item,
            label=f"operator_reviews[{index}]",
            keys=frozenset(
                {
                    "ticket",
                    "record_decision",
                    "items",
                    "action",
                    "block",
                    "asks",
                    "note",
                    "email_questions",
                    "email_context",
                }
            ),
        )
        if not isinstance(raw["record_decision"], bool):
            _fail("operator review record_decision must be boolean")
        raw_items = raw["items"]
        if not isinstance(raw_items, list) or not raw_items:
            _fail("operator review items must be a nonempty array")
        review_items: list[OperatorReviewItem] = []
        for raw_item in raw_items:
            item = _object(
                raw_item,
                label=f"operator_reviews[{index}].items item",
                keys=frozenset({"source_index", "status", "why", "reviewed_amount"}),
            )
            source_index = item["source_index"]
            if (
                isinstance(source_index, bool)
                or not isinstance(source_index, int)
                or source_index < 1
            ):
                _fail("operator review source_index must be a positive integer")
            status = _string(item["status"], label="operator review item status")
            if status not in {"A", "C", "D", "Q"}:
                _fail("operator review item status is invalid")
            reviewed_amount = _string(
                item["reviewed_amount"], label="operator review item amount", blank=True
            )
            if reviewed_amount:
                try:
                    amount = Decimal(reviewed_amount)
                except InvalidOperation as exc:
                    raise ReimbursementEventError(
                        "operator review item amount must be exact nonnegative money"
                    ) from exc
                if not amount.is_finite() or amount < 0 or f"{amount:.2f}" != reviewed_amount:
                    _fail("operator review item amount must be exact nonnegative money")
            review_items.append(
                OperatorReviewItem(
                    source_index=source_index,
                    status=status,
                    why=_string(item["why"], label="operator review item why"),
                    reviewed_amount=reviewed_amount,
                )
            )
        indexes = [item.source_index for item in review_items]
        if len(indexes) != len(set(indexes)):
            _fail("operator review item indexes must not contain duplicates")
        normalized_for_digest = {
            "ticket": dict(raw["ticket"]),
            "record_decision": raw["record_decision"],
            "items": [dict(item) for item in raw_items],
            "action": raw["action"],
            "block": raw["block"],
            "asks": raw["asks"],
            "note": raw["note"],
            "email_questions": raw["email_questions"],
            "email_context": raw["email_context"],
        }
        reviews.append(
            OperatorReview(
                ticket=_selector(raw["ticket"], label=f"operator_reviews[{index}].ticket"),
                record_decision=raw["record_decision"],
                items=tuple(review_items),
                action=_string(raw["action"], label="operator review action"),
                block=_string(raw["block"], label="operator review block"),
                asks=_strings(raw["asks"], label="operator review asks"),
                note=_string(raw["note"], label="operator review note"),
                email_questions=_strings(
                    raw["email_questions"], label="operator review email questions"
                ),
                email_context=_string(
                    raw["email_context"], label="operator review email context", blank=True
                ),
                evidence_sha256=_digest(normalized_for_digest),
            )
        )

    raw_operator_payments = root.get("operator_payments", [])
    if not isinstance(raw_operator_payments, list):
        _fail("anchor operator_payments must be an array")
    operator_payments: list[OperatorPayment] = []
    for index, value_item in enumerate(raw_operator_payments):
        raw = _object(
            value_item,
            label=f"operator_payments[{index}]",
            keys=frozenset(
                {"record_payment", "ticket", "date", "amount", "reference", "audit_note"}
            ),
        )
        if raw["record_payment"] is not True:
            _fail("operator payment record_payment must be true")
        ticket = _selector(raw["ticket"], label=f"operator_payments[{index}].ticket")
        payment_on = _payment_date(raw["date"], label=f"operator_payments[{index}].date")
        payment_amount = _payment_amount(raw["amount"], label=f"operator_payments[{index}].amount")
        reference = _payment_reference(
            raw["reference"], label=f"operator_payments[{index}].reference"
        )
        audit_note = _string(raw["audit_note"], label=f"operator_payments[{index}].audit_note")
        normalized_for_digest = {
            "record_payment": True,
            "ticket": ticket.__dict__,
            "date": payment_on,
            "amount": payment_amount,
            "reference": reference,
            "audit_note": audit_note,
        }
        operator_payments.append(
            OperatorPayment(
                ticket=ticket,
                record_payment=True,
                date=payment_on,
                amount=payment_amount,
                reference=reference,
                audit_note=audit_note,
                evidence_sha256=_digest(normalized_for_digest),
            )
        )

    thread_ids = [anchor.message_id for anchor in threads]
    direct_ids = [link.message_id for link in direct]
    payment_ids = [link.message_id for link in payment_links]
    if len(thread_ids) != len(set(thread_ids)):
        _fail("thread anchor Message-IDs must not contain duplicates")
    if len(direct_ids) != len(set(direct_ids)):
        _fail("direct link Message-IDs must not contain duplicates")
    if len(payment_ids) != len(set(payment_ids)):
        _fail("payment link Message-IDs must not contain duplicates")
    all_link_ids = thread_ids + direct_ids + payment_ids
    if len(all_link_ids) != len(set(all_link_ids)):
        _fail("a Message-ID cannot occur in more than one private link lane")
    review_selectors = [review.ticket for review in reviews]
    if len(review_selectors) != len(set(review_selectors)):
        _fail("operator reviews must select unique tickets")
    linked_payment_selectors = [
        binding.ticket for link in payment_links for binding in link.bindings
    ]
    linked_reference_digests = [
        binding.reference_sha256 for link in payment_links for binding in link.bindings
    ]
    operator_payment_selectors = [payment.ticket for payment in operator_payments]
    operator_payment_references = [payment.reference for payment in operator_payments]
    if len(linked_payment_selectors) != len(set(linked_payment_selectors)):
        _fail("payment links must select unique tickets")
    if len(linked_reference_digests) != len(set(linked_reference_digests)):
        _fail("payment links must contain unique reference digests")
    if len(operator_payment_selectors) != len(set(operator_payment_selectors)):
        _fail("operator payments must select unique tickets")
    if len(operator_payment_references) != len(set(operator_payment_references)):
        _fail("operator payments must contain unique references")
    if set(linked_payment_selectors) & set(operator_payment_selectors):
        _fail("a ticket cannot occur in both private payment lanes")
    if set(linked_reference_digests) & {
        hashlib.sha256(reference.encode("utf-8")).hexdigest()
        for reference in operator_payment_references
    }:
        _fail("a payment reference cannot occur in both private payment lanes")

    normalized_payload: dict[str, Any] = {
        "schema_version": version,
        "actors": {
            "payment_operators": list(payment_operators),
            "secondary_approvers": list(secondary_approvers),
        },
        "thread_anchors": [
            {
                "message_id": anchor.message_id,
                "purpose": anchor.purpose,
                "tickets": [selector.__dict__ for selector in anchor.tickets],
            }
            for anchor in threads
        ],
        "direct_links": [
            {
                "message_id": link.message_id,
                "purpose": link.purpose,
                "ticket": link.ticket.__dict__,
            }
            for link in direct
        ],
        "operator_reviews": [review.evidence_sha256 for review in reviews],
    }
    if version == 2:
        normalized_payload["payment_links"] = [
            {
                "message_id": link.message_id,
                "bindings": [
                    {
                        "ticket": binding.ticket.__dict__,
                        "reference_sha256": binding.reference_sha256,
                    }
                    for binding in link.bindings
                ],
            }
            for link in sorted(payment_links, key=lambda item: item.message_id)
        ]
        normalized_payload["operator_payments"] = [
            payment.evidence_sha256
            for payment in sorted(
                operator_payments,
                key=lambda item: (
                    item.ticket.review_key,
                    item.ticket.ref,
                    item.ticket.form_label,
                ),
            )
        ]
    return AnchorConfig(
        schema_version=version,
        payment_operators=payment_operators,
        secondary_approvers=secondary_approvers,
        thread_anchors=tuple(threads),
        direct_links=tuple(direct),
        operator_reviews=tuple(reviews),
        payment_links=tuple(sorted(payment_links, key=lambda item: item.message_id)),
        operator_payments=tuple(
            sorted(
                operator_payments,
                key=lambda item: (
                    item.ticket.review_key,
                    item.ticket.ref,
                    item.ticket.form_label,
                ),
            )
        ),
        sha256=_digest(normalized_payload),
    )


def _payment_lines(text: str) -> list[str] | None:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not normalized:
        return None
    lines = [line.strip() for line in normalized.split("\n")]
    return lines if all("\x00" not in line for line in lines) else None


def _parse_generated_single(text: str) -> tuple[PaymentEvidence, ...] | None:
    lines = _payment_lines(text)
    if lines is None:
        return None
    matches: list[tuple[int, re.Match[str], re.Match[str]]] = []
    for index in range(len(lines) - 1):
        intro = _GENERATED_SINGLE_INTRO_RE.fullmatch(lines[index])
        confirmation = _GENERATED_CONFIRMATION_RE.fullmatch(lines[index + 1])
        if intro is not None and confirmation is not None:
            matches.append((index, intro, confirmation))
    if len(matches) != 1:
        return None
    index, intro, confirmation = matches[0]
    prefix = lines[:index]
    suffix = lines[index + 2 :]
    if prefix and not (
        len(prefix) == 2
        and _PAYMENT_GREETING_RE.fullmatch(prefix[0]) is not None
        and prefix[1] == ""
    ):
        return None
    if suffix and not (suffix[0] == "" and len(suffix) >= 2 and all(suffix[1:])):
        return None
    amount_matches = [match.group(1) for match in _MONEY_RE.finditer(text)]
    reference_matches = [match.group(1) for match in _REFERENCE_RE.finditer(text)]
    if amount_matches != [intro.group(1)] or reference_matches != [confirmation.group(1)]:
        return None
    for line in prefix + suffix[1:]:
        if _BARE_MONEY_RE.fullmatch(line) is not None or _BARE_CONFIRMATION_RE.fullmatch(line):
            return None
    amount = Decimal(intro.group(1).replace(",", ""))
    if not amount.is_finite() or amount <= 0:
        return None
    return (PaymentEvidence(amount=amount, reference=confirmation.group(1)),)


def _split_payment_blocks(lines: Sequence[str]) -> list[list[str]] | None:
    if not lines or not lines[0] or not lines[-1]:
        return None
    blocks: list[list[str]] = [[]]
    for line in lines:
        if line:
            blocks[-1].append(line)
        elif not blocks[-1]:
            return None
        else:
            blocks.append([])
    if not blocks[-1]:
        return None
    return blocks


def _parse_sent_batch(text: str) -> tuple[PaymentEvidence, ...] | None:
    lines = _payment_lines(text)
    if lines is None:
        return None
    cursor = 0
    if _PAYMENT_GREETING_RE.fullmatch(lines[0]) is not None:
        if len(lines) < 2 or lines[1] != "":
            return None
        cursor = 2
    if cursor + 3 >= len(lines):
        return None
    intro = lines[cursor]
    single_intro = _GENERATED_SINGLE_INTRO_RE.fullmatch(intro)
    if single_intro is not None:
        expected_header = "Zelle confirmation:"
        expected_blocks = 1
        header = lines[cursor + 1]
        if lines[cursor + 2] != "":
            return None
        tail = lines[cursor + 3 :]
    elif intro == "Your Reimbursements have been approved and sent by Zelle.":
        expected_header = "Zelle confirmations"
        expected_blocks = 2
        header = lines[cursor + 2]
        if lines[cursor + 1] != "" or lines[cursor + 3] != "":
            return None
        tail = lines[cursor + 4 :]
    elif intro == "Your reimbursements have been approved and sent by Zelle.":
        expected_header = "Confirmation:"
        expected_blocks = 3
        header = lines[cursor + 2]
        if lines[cursor + 1] != "" or lines[cursor + 3] != "":
            return None
        tail = lines[cursor + 4 :]
    else:
        return None
    if header != expected_header:
        return None
    if len(tail) < 6 or tail[-4] != "" or any(not line for line in tail[-3:]):
        return None
    if _SENT_THANK_YOU_RE.fullmatch(tail[-3]) is None:
        return None
    blocks = _split_payment_blocks(tail[:-4])
    if blocks is None or len(blocks) != expected_blocks:
        return None

    parsed: list[PaymentEvidence] = []
    for block in blocks:
        if len(block) not in {4, 5}:
            return None
        if not block[0] or _SENT_DESTINATION_RE.fullmatch(block[1]) is None:
            return None
        reference_index = 3 if len(block) == 5 else 2
        if len(block) == 5 and block[2] != "Classroom Supplies":
            return None
        reference = block[reference_index]
        amount_match = _BARE_MONEY_RE.fullmatch(block[reference_index + 1])
        if _BARE_CONFIRMATION_RE.fullmatch(reference) is None or amount_match is None:
            return None
        if any(
            _MONEY_RE.search(label) is not None
            or _REFERENCE_RE.search(label) is not None
            or _BARE_CONFIRMATION_RE.fullmatch(label) is not None
            for label in block[:reference_index]
        ):
            return None
        amount = Decimal(amount_match.group(1).replace(",", ""))
        if not amount.is_finite() or amount <= 0:
            return None
        parsed.append(PaymentEvidence(amount=amount, reference=reference))
    references = [item.reference for item in parsed]
    if len(references) != len(set(references)):
        return None
    bare_tokens = [line for line in lines if _BARE_CONFIRMATION_RE.fullmatch(line) is not None]
    if bare_tokens != references:
        return None
    expected_amount_occurrences = len(parsed) + (1 if single_intro is not None else 0)
    if len(list(_MONEY_RE.finditer(text))) != expected_amount_occurrences:
        return None
    if _REFERENCE_RE.search(text) is not None:
        return None
    if single_intro is not None:
        intro_amount = Decimal(single_intro.group(1).replace(",", ""))
        if parsed[0].amount != intro_amount:
            return None
    return tuple(parsed)


def parse_payment_evidence_blocks(text: str) -> tuple[PaymentEvidence, ...] | None:
    """Parse one exact generated message or one exact sent Zelle block message."""

    if _PAYMENT_NEGATIVE_RE.search(text) is not None:
        return None
    generated = _parse_generated_single(text)
    if generated is not None:
        return generated
    return _parse_sent_batch(text)


def parse_payment_evidence(text: str) -> PaymentEvidence | None:
    """Return exact singular payment evidence while preserving the legacy parser contract."""

    structured = parse_payment_evidence_blocks(text)
    if structured is not None:
        return structured[0] if len(structured) == 1 else None
    if any(
        marker in text
        for marker in (
            "reimbursement has been approved and sent by Zelle.",
            "Reimbursements have been approved and sent by Zelle.",
            "reimbursements have been approved and sent by Zelle.",
            "Zelle confirmations",
        )
    ):
        return None

    positive_signal = _PAID_SIGNAL_RE.search(text) or _CHECK_SENT_SIGNAL_RE.search(text)
    if positive_signal is None or _PAYMENT_NEGATIVE_RE.search(text) is not None:
        return None
    amount_matches = [match.group(1) for match in _MONEY_RE.finditer(text)]
    if len(amount_matches) != 1:
        return None
    amount = Decimal(amount_matches[0].replace(",", ""))
    if not amount.is_finite() or amount < 0:
        return None
    reference_matches = [match.group(1) for match in _REFERENCE_RE.finditer(text)]
    if len(reference_matches) == 1:
        return PaymentEvidence(amount=amount, reference=reference_matches[0])
    if (
        reference_matches
        or _PAID_SIGNAL_RE.search(text) is None
        or len(_ZELLE_CONTEXT_RE.findall(text)) != 1
    ):
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if sum(_ZELLE_MARKER_RE.fullmatch(line) is not None for line in lines) != 1:
        return None
    bare_tokens = [line for line in lines if _BARE_CONFIRMATION_RE.fullmatch(line)]
    if len(bare_tokens) != 1:
        return None
    token = bare_tokens[0]
    token_index = lines.index(token)
    if token_index + 1 >= len(lines):
        return None
    amount_line = _BARE_MONEY_RE.fullmatch(lines[token_index + 1])
    if amount_line is None or Decimal(amount_line.group(1).replace(",", "")) != amount:
        return None
    return PaymentEvidence(amount=amount, reference=token)


def classify_approval_reply(text: str) -> str | None:
    """Classify explicit top-authored approval as POSITIVE/NEGATIVE, else fail closed.

    ``receipt_ingest.parse_mail_evidence`` removes quoted history before this function runs.  The
    remaining reply must be a short stand-alone response, or begin (after one optional greeting)
    with the exact assessment-agreement sentence. Trailing prose is never parsed as instructions,
    and adversative/negative modifiers reject the extended form.
    """

    normalized = " ".join(line.strip() for line in text.splitlines() if line.strip()).casefold()
    normalized = re.sub(r"[.!]+$", "", normalized).strip()
    normalized = normalized.replace(",", "")
    negative = {
        "no",
        "no thanks",
        "not approved",
        "decline",
        "declined",
        "i dispute this",
        "do not approve",
    }
    positive = {
        "yes",
        "yes please",
        "approved",
        "approve",
        "confirmed",
        "looks good",
        "i approve",
    }
    if normalized in negative:
        return "NEGATIVE"
    if normalized in positive:
        return "POSITIVE"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if _APPROVAL_MODIFIER_RE.search(" ".join(lines)) is not None:
        return None
    if lines and _APPROVAL_GREETING_RE.fullmatch(lines[0]) is not None:
        lines = lines[1:]
    if not lines or _ASSESSMENT_AGREEMENT_RE.match(lines[0]) is None:
        return None
    return "POSITIVE"


def parse_proposal_recommendations(
    text: str, *, expected_refs: Sequence[str]
) -> tuple[ProposalRecommendation, ...] | None:
    """Parse exact ticket headings followed by one A/C action line per item.

    The deliberately small protocol accepts a section heading containing only one ticket ref, then
    lines beginning ``Approve``/``Approve as is`` or ``Clarification``.  A comma-separated group of
    exact refs is accepted only with one exact ``Approve as is`` action, which means all items in
    each grouped ticket.  All and only expected refs must occur once.  Item-count and held-position
    validation remain the reducer's responsibility because this pure parser does not know tickets.
    """

    expected = tuple(expected_refs)
    if not expected or len(expected) != len(set(expected)):
        return None
    expected_set = set(expected)
    sections: list[tuple[tuple[str, ...], list[str], list[str]]] = []
    seen: set[str] = set()
    current: tuple[str, ...] | None = None
    for line in text.splitlines():
        ref_match = _PROPOSAL_REF_RE.fullmatch(line)
        if ref_match:
            refs = tuple(part.strip() for part in ref_match.group("refs").split(","))
            if any(ref not in expected_set or ref in seen for ref in refs):
                return None
            seen.update(refs)
            current = refs
            sections.append((refs, [], []))
            continue
        action_match = _PROPOSAL_ACTION_RE.match(line)
        if action_match:
            if current is None:
                return None
            action = action_match.group(1).casefold()
            sections[-1][1].append("C" if action.startswith("clarification") else "A")
            sections[-1][2].append(line)
    if seen != expected_set or any(not statuses for _refs, statuses, _lines in sections):
        return None
    parsed: dict[str, ProposalRecommendation] = {}
    for refs, statuses, action_lines in sections:
        grouped = len(refs) > 1
        if grouped and (
            statuses != ["A"]
            or len(action_lines) != 1
            or _PROPOSAL_APPROVE_ALL_RE.fullmatch(action_lines[0]) is None
        ):
            return None
        for ref in refs:
            parsed[ref] = ProposalRecommendation(
                ref=ref,
                statuses=tuple(statuses),
                all_items=grouped,
            )
    return tuple(parsed[ref] for ref in expected)
