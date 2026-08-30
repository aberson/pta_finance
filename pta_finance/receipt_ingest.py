"""Parse reimbursement-form emails into structured submissions (Phase-4 receipt ingestion).

The treasurer inbox receives auto-generated **form-submission emails** (e.g. a Wix
"reimbursement form" notification): a header block naming the requestor, then one or more
NUMBERED line items — each a *Date / (Budget) Category / Description / Amount* group — a
grand Total, a payment method, and links (or attachments) to the underlying vendor receipts.

This module is the *reader + parser* for those emails. It is intentionally **credential-free
and write-free**: it reads raw ``.eml`` files off disk and returns pure
:class:`Submission` dataclasses. Mapping a submission onto the canonical ``transactions`` /
``receipts`` row shapes (:mod:`pta_finance.schema`) and writing them to the Sheet is a
SEPARATE, later step — this prototype only lets an operator *see what gets extracted*.

Identity rule (this is a PUBLIC repo): no organization/person/email is hard-coded here. A
submission is recognized **structurally** (a "submission summary"-style body carrying labeled
Total + numbered line items), with an OPTIONAL operator-supplied subject substring as an extra
filter. Real names/addresses live only in the private ``.eml`` samples (gitignored), never in
code, tests, or fixtures.

Robustness posture: form emails render each label and its value on separate lines (the value
often bolded), and the numbered prefixes are inconsistently spaced (``"1. Date:"`` vs
``"1.Amount:"`` vs ``"3. Amount :"``). Some line items omit fields (an item may carry only a
Description + Amount). The parser tolerates all of that and leaves missing fields blank rather
than guessing; downstream mapping is where a blank amount / total mismatch becomes
``needs_review``.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.policy
import hashlib
import json
import mailbox
import quopri
import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, DecimalException
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from pta_finance import ids, models

__all__ = [
    "AttachmentEvidence",
    "LineItem",
    "MailEvidence",
    "Submission",
    "html_to_text",
    "body_candidates",
    "message_text",
    "attachment_names",
    "normalize_message_id",
    "parse_mail_evidence",
    "looks_like_reimbursement",
    "is_reply_or_forward",
    "form_type",
    "parse_received_date",
    "parse_submission",
    "iter_eml",
    "iter_mbox",
    "iter_source",
    "parse_finite_amount",
    "line_item_total",
    "stated_total",
    "total_reconciles",
    "Profile",
    "profile",
]


# --- Data model ------------------------------------------------------------


@dataclass(frozen=True)
class LineItem:
    """One numbered reimbursement line. Fields are RAW strings as they appear in the email.

    A missing field is ``""`` (some submissions omit Date/Category on later items). Typed
    conversion (``amount`` -> :class:`~decimal.Decimal`, ``date`` -> :class:`datetime.date`)
    and validation happen at the later mapping step, not here.
    """

    index: int
    date: str
    category: str
    description: str
    amount: str


@dataclass(frozen=True)
class AttachmentEvidence:
    """Stable evidence for one decoded MIME attachment.

    ``filename`` is private source material and is therefore omitted from the dataclass repr.
    The digest is always over the decoded payload bytes, never over base64/quoted-printable wire
    text.
    """

    mime_type: str
    filename: str = field(repr=False)
    decoded_size: int
    content_sha256: str


@dataclass(frozen=True)
class MailEvidence:
    """Normalized, immutable evidence for one email without implicit terminal output.

    Fields carrying mailbox content or identifiers are omitted from the dataclass repr so an
    accidental log statement cannot expose them. ``message_key`` is privacy-safe: it contains
    only a version marker and SHA-256 digest.
    """

    message_key: str
    message_id: str = field(repr=False)
    in_reply_to: tuple[str, ...] = field(repr=False)
    references: tuple[str, ...] = field(repr=False)
    date: str = field(repr=False)
    sender_address: str = field(repr=False)
    top_authored_text: str = field(repr=False)
    top_authored_sha256: str
    attachments: tuple[AttachmentEvidence, ...] = field(repr=False)
    evidence_sha256: str


@dataclass(frozen=True)
class Submission:
    """A parsed reimbursement-form email. All values are RAW strings/tuples (no typing yet)."""

    message_id: str
    subject: str
    received: str
    requestor_name: str
    requestor_email: str
    phone: str
    company: str
    line_items: tuple[LineItem, ...]
    total: str
    payment_type: str
    receipt_urls: tuple[str, ...]
    attachments: tuple[str, ...]
    notes: str
    # Compatibility surface for the reimbursement bundle's v1 source-evidence digest. Keep this
    # at the end with a default so every existing Submission constructor remains valid.
    source_receipt_urls_v1: tuple[str, ...] = ()


# --- HTML -> text ----------------------------------------------------------

# Block-level tags after which rendered text starts a new line. Form emails lay out each
# "Label:" and its value in separate table cells / paragraphs, so preserving these breaks is
# what lets the line-oriented parser below pair a label with the value beneath it.
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "tr", "td", "th", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
)
_SKIP_CONTENT_TAGS = frozenset({"style", "script", "head"})
_QUOTED_CONTAINER_CLASSES = frozenset(
    {"gmail_quote", "moz-cite-prefix", "protonmail_quote", "yahoo_quoted"}
)
_QUOTED_CONTAINER_IDS = frozenset({"divrplyfwdmsg"})


def _is_quoted_container(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    if tag.casefold() == "blockquote":
        return True
    normalized = {key.casefold(): (value or "") for key, value in attrs}
    classes = {item.casefold() for item in normalized.get("class", "").split()}
    return bool(classes & _QUOTED_CONTAINER_CLASSES) or (
        normalized.get("id", "").casefold() in _QUOTED_CONTAINER_IDS
    )


class _TextExtractor(HTMLParser):
    """Collect visible text from HTML, inserting newlines at block boundaries (stdlib only)."""

    def __init__(self, *, stop_at_quoted_history: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._stop_at_quoted_history = stop_at_quoted_history
        self._stopped = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._stopped:
            return
        if self._stop_at_quoted_history and _is_quoted_container(tag, attrs):
            self._chunks.append("\n")
            self._stopped = True
            return
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._stopped:
            return
        if tag in _SKIP_CONTENT_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._stopped and self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def _normalize_rendered_text(raw: str) -> str:
    lines = [line.strip() for line in raw.splitlines()]
    # Collapse consecutive blanks to a single blank; drop leading/trailing blanks.
    out: list[str] = []
    for line in lines:
        if line == "" and (not out or out[-1] == ""):
            continue
        out.append(line)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def html_to_text(html: str) -> str:
    """Render HTML to newline-separated visible text (stdlib ``html.parser``; no deps).

    Block tags become line breaks so a ``"Label:"`` element and its value element land on
    separate lines. Runs of blank lines collapse; each line is stripped.
    """
    extractor = _TextExtractor()
    extractor.feed(html)
    return _normalize_rendered_text(extractor.text())


def _top_authored_html_to_text(html: str) -> str:
    extractor = _TextExtractor(stop_at_quoted_history=True)
    extractor.feed(html)
    return _normalize_rendered_text(extractor.text())


# --- Email body extraction -------------------------------------------------


def _validated_transfer_encoding(part: Message) -> str:
    """Return one supported CTE, including the stricter message/* identity-only rule."""

    transfer_headers = part.get_all("Content-Transfer-Encoding", [])
    if len(transfer_headers) > 1:
        raise ValueError("MIME payload has ambiguous transfer encoding")
    transfer_encoding = str(transfer_headers[0]).strip().casefold() if transfer_headers else ""
    allowed_encodings = {"", "7bit", "8bit", "binary", "base64", "quoted-printable"}
    if transfer_encoding not in allowed_encodings:
        raise ValueError("MIME payload uses an unsupported transfer encoding")
    if part.get_content_maintype() == "message":
        # ``email`` eagerly parses message/rfc822 payloads into Message objects, so the
        # original encoded octets are no longer available for a strict base64 or
        # quoted-printable validation pass.  Accept only identity encodings here.
        if transfer_encoding not in {"", "7bit", "8bit", "binary"}:
            raise ValueError("message attachment uses an unsupported transfer encoding")
    elif part.is_multipart() and transfer_encoding not in {"", "7bit", "8bit", "binary"}:
        raise ValueError("multipart MIME payload uses an unsupported transfer encoding")
    return transfer_encoding


def _decoded_payload(part: Message) -> bytes:
    """Decode one MIME payload, failing closed on unsupported or malformed wire encoding."""

    transfer_encoding = _validated_transfer_encoding(part)
    if part.get_content_maintype() == "message":
        # The stdlib eagerly parses message/rfc822 and cannot recover the exact decoded wire
        # octets afterward.  Canonical reserialization would hide byte-level mutations, so this
        # evidence lane rejects attached messages rather than claiming a byte-exact digest.
        raise ValueError("message attachments cannot provide byte-exact evidence")
    if transfer_encoding in {"base64", "quoted-printable"}:
        wire = part.get_payload(decode=False)
        if isinstance(wire, str):
            try:
                encoded = wire.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("encoded MIME payload is malformed") from exc
        elif isinstance(wire, bytes):
            encoded = wire
        else:
            raise ValueError("encoded MIME payload is malformed")
        if transfer_encoding == "base64":
            compact = b"".join(encoded.split())
            try:
                return base64.b64decode(compact, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("base64 MIME payload is malformed") from exc
        if re.search(rb"=(?![0-9A-Fa-f]{2}|\r?\n)", encoded):
            raise ValueError("quoted-printable MIME payload is malformed")
        return quopri.decodestring(encoded)
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        raise ValueError("MIME payload could not be decoded")
    if transfer_encoding in {"", "7bit"} and any(byte > 127 for byte in payload):
        raise ValueError("7bit MIME payload contains non-ASCII bytes")
    return payload


def _decode_part(part: Message) -> str:
    """Decode one MIME text part strictly using its declared charset (UTF-8 default)."""
    payload = _decoded_payload(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="strict")
    except LookupError as exc:
        raise ValueError("text MIME payload declares an unsupported charset") from exc
    except UnicodeDecodeError as exc:
        raise ValueError("text MIME payload does not match its declared charset") from exc


def _iter_body_parts(msg: Message) -> Iterator[Message]:
    """Yield top-message body leaves without descending into attached/inline RFC messages."""

    def visit(part: Message, *, root: bool = False) -> Iterator[Message]:
        if not root and part.get_content_maintype() == "message":
            _validated_transfer_encoding(part)
            return
        if not root and (part.get_content_disposition() or "") == "attachment":
            return
        if part.is_multipart():
            _validated_transfer_encoding(part)
            payload = part.get_payload()
            if isinstance(payload, list):
                for child in payload:
                    if isinstance(child, Message):
                        yield from visit(child)
            return
        yield part

    yield from visit(msg, root=True)


def _iter_attachment_parts(msg: Message) -> Iterator[Message]:
    """Yield top-message attachments without recursively treating forwarded mail as authored."""

    def visit(part: Message, *, root: bool = False) -> Iterator[Message]:
        if not root and part.get_content_maintype() == "message":
            _validated_transfer_encoding(part)
            if (part.get_content_disposition() or "") == "attachment":
                yield part
            return
        if not root and (part.get_content_disposition() or "") == "attachment":
            yield part
            return
        if part.is_multipart():
            _validated_transfer_encoding(part)
            payload = part.get_payload()
            if isinstance(payload, list):
                for child in payload:
                    if isinstance(child, Message):
                        yield from visit(child)

    yield from visit(msg, root=True)


def body_candidates(msg: Message) -> list[str]:
    """Rendered-text candidate bodies, richest first: HTML(->text), then ``text/plain``.

    Form emails are HTML-primary; a co-present ``text/plain`` part is often just a
    "view in browser" stub. Returning BOTH (non-empty) lets the parser pick whichever body
    actually carries the reimbursement structure, rather than committing to one MIME type.
    Attachment parts are skipped.
    """
    plain: list[str] = []
    html_parts: list[str] = []
    for part in _iter_body_parts(msg):
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain.append(_decode_part(part))
        elif ctype == "text/html":
            html_parts.append(_decode_part(part))

    candidates: list[str] = []
    joined_html = "\n".join(h for h in html_parts if h).strip()
    if joined_html:
        rendered = html_to_text(joined_html)
        if rendered.strip():
            candidates.append(rendered)
    joined_plain = "\n".join(p for p in plain if p).strip()
    if joined_plain:
        candidates.append(joined_plain)
    return candidates


def message_text(msg: Message) -> str:
    """The single best-effort rendered body (richest candidate), or ``""``."""
    candidates = body_candidates(msg)
    return candidates[0] if candidates else ""


def attachment_names(msg: Message) -> tuple[str, ...]:
    """Filenames of attachment parts (the underlying vendor-receipt PDFs, when attached)."""
    names: list[str] = []
    for part in msg.walk():
        if (part.get_content_disposition() or "") == "attachment":
            filename = part.get_filename()
            if filename:
                names.append(str(filename))
    return tuple(names)


# --- Stable mail evidence --------------------------------------------------


_ID_ATOM = r"[A-Za-z0-9!#$%&'*+\-/=?^_`{|}~]+"
_ID_LEFT = rf"{_ID_ATOM}(?:\.{_ID_ATOM})*"
_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_ID_RIGHT = rf"{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})*"
_MESSAGE_ID = re.compile(rf"\A<(?P<left>{_ID_LEFT})@(?P<right>{_ID_RIGHT})>\Z", re.ASCII)
_MESSAGE_ID_TOKEN = re.compile(r"<[^<>]*>")
_SENDER_ADDRESS = re.compile(r"\A[^@\s<>,;:]+@[^@\s<>,;:]+\Z")
_QUOTED_LINE = re.compile(r"^\s*>")
_ON_WROTE = re.compile(r"\Aon\s+.+\b(?:wrote|writes)\s*:\s*\Z", re.IGNORECASE)
_QUOTED_SEPARATOR = re.compile(
    r"\A\s*(?:(?:-{2,}\s*)?(?:original message|forwarded message)"
    r"(?:\s*-{2,})?|begin forwarded message:)\s*\Z",
    re.IGNORECASE,
)
_REPLY_HEADER = re.compile(r"^\s*(from|sent|date|to|cc|subject)\s*:", re.IGNORECASE)


def _unfold_header(value: str) -> str:
    """Collapse RFC header folding and outer whitespace to a stable single-line spelling."""
    return " ".join(value.split())


def _raw_header_values(msg: Message, name: str) -> tuple[str, ...]:
    """All raw values for ``name`` without headerregistry's Message-ID truncation."""
    wanted = name.casefold()
    return tuple(str(value) for key, value in msg.raw_items() if key.casefold() == wanted)


def normalize_message_id(value: str) -> str:
    """Return one strict Message-ID in canonical angle-bracket form, else ``""``.

    The input must contain exactly one ``<id-left@id-right>`` and no comments, extra IDs,
    internal whitespace, or control characters. Message-ID left sides are case-sensitive and are
    preserved; the domain-like right side is case-insensitive and normalized to lowercase.
    """
    clean = _unfold_header(value)
    match = _MESSAGE_ID.fullmatch(clean)
    if match is None:
        return ""
    left = match.group("left")
    right = match.group("right")
    if any(ord(char) < 33 or ord(char) == 127 for char in f"{left}@{right}"):
        return ""
    return f"<{left}@{right.lower()}>"


def _normalized_message_id_list(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    """Normalize a whitespace-delimited Message-ID ancestry header, rejecting partial parses."""
    raw = " ".join(_unfold_header(value) for value in values if _unfold_header(value))
    if not raw:
        return ()

    normalized: list[str] = []
    seen: set[str] = set()
    end = 0
    for match in _MESSAGE_ID_TOKEN.finditer(raw):
        if raw[end : match.start()].strip():
            raise ValueError(f"{label} header is malformed")
        message_id = normalize_message_id(match.group(0))
        if not message_id:
            raise ValueError(f"{label} header is malformed")
        if message_id not in seen:
            seen.add(message_id)
            normalized.append(message_id)
        end = match.end()
    if raw[end:].strip() or not normalized:
        raise ValueError(f"{label} header is malformed")
    return tuple(normalized)


def _normalized_message_id(msg: Message) -> str:
    values = _raw_header_values(msg, "Message-ID")
    if not values:
        return ""
    if len(values) != 1:
        raise ValueError("Message-ID header is ambiguous")
    raw = _unfold_header(values[0])
    if not raw:
        return ""
    normalized = normalize_message_id(raw)
    if not normalized:
        raise ValueError("Message-ID header is malformed")
    return normalized


def _single_header(msg: Message, name: str) -> str:
    values = _raw_header_values(msg, name)
    if not values:
        return ""
    if len(values) != 1:
        raise ValueError(f"{name} header is ambiguous")
    return _unfold_header(values[0])


def _sender_address(msg: Message) -> str:
    """One normalized addr-spec for fail-closed runtime authorization, or ``""``."""
    values = _raw_header_values(msg, "From")
    if len(values) != 1:
        return ""
    parsed = getaddresses([_unfold_header(values[0])])
    if len(parsed) != 1:
        return ""
    address = parsed[0][1].strip()
    if _SENDER_ADDRESS.fullmatch(address) is None:
        return ""
    return address.casefold()


def _starts_on_wrote(lines: list[str], index: int) -> bool:
    """Recognize common one-to-three-line ``On ... wrote:`` quote introductions."""
    if not lines[index].strip().casefold().startswith("on "):
        return False
    chunks: list[str] = []
    for line in lines[index : index + 3]:
        clean = line.strip()
        if not clean:
            break
        chunks.append(clean)
        if clean.casefold().endswith(("wrote:", "writes:")):
            break
    return _ON_WROTE.fullmatch(" ".join(chunks)) is not None


def _starts_reply_header_block(lines: list[str], index: int) -> bool:
    """Recognize an Outlook-style quoted ``From/Sent/To/Subject`` header block."""
    first = _REPLY_HEADER.match(lines[index])
    if first is None or first.group(1).casefold() != "from":
        return False
    labels: set[str] = set()
    for line in lines[index : index + 8]:
        match = _REPLY_HEADER.match(line)
        if match is not None:
            labels.add(match.group(1).casefold())
    return "subject" in labels and "to" in labels and bool({"sent", "date"} & labels)


def _top_authored_text(text: str) -> str:
    """Normalize newlines and retain only the content before quoted message history."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    authored: list[str] = []
    for index, line in enumerate(lines):
        clean = line.rstrip()
        stripped = clean.strip()
        if (
            _QUOTED_LINE.match(clean)
            or _QUOTED_SEPARATOR.fullmatch(stripped)
            or (len(stripped) >= 5 and set(stripped) == {"_"})
            or _starts_on_wrote(lines, index)
            or _starts_reply_header_block(lines, index)
        ):
            break
        authored.append(clean)
    while authored and not authored[0].strip():
        authored.pop(0)
    while authored and not authored[-1].strip():
        authored.pop()
    return "\n".join(authored)


def _message_top_authored_text(msg: Message) -> str:
    """Top-authored body, using structural HTML quote containers before text markers."""
    plain: list[str] = []
    html_parts: list[str] = []
    for part in _iter_body_parts(msg):
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain.append(_decode_part(part))
        elif content_type == "text/html":
            html_parts.append(_decode_part(part))

    joined_html = "\n".join(value for value in html_parts if value).strip()
    if joined_html and html_to_text(joined_html).strip():
        return _top_authored_text(_top_authored_html_to_text(joined_html))
    joined_plain = "\n".join(value for value in plain if value).strip()
    return _top_authored_text(joined_plain) if joined_plain else ""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(canonical)


def _attachment_evidence(msg: Message) -> tuple[AttachmentEvidence, ...]:
    attachments: list[AttachmentEvidence] = []
    for part in _iter_attachment_parts(msg):
        decoded = _decoded_payload(part)
        filename = part.get_filename()
        attachments.append(
            AttachmentEvidence(
                mime_type=part.get_content_type().casefold(),
                filename=str(filename).strip() if filename is not None else "",
                decoded_size=len(decoded),
                content_sha256=hashlib.sha256(decoded).hexdigest(),
            )
        )
    return tuple(attachments)


def _attachment_payload(attachment: AttachmentEvidence) -> dict[str, object]:
    return {
        "mime_type": attachment.mime_type,
        "filename": attachment.filename,
        "decoded_size": attachment.decoded_size,
        "content_sha256": attachment.content_sha256,
    }


def parse_mail_evidence(msg: Message) -> MailEvidence:
    """Build deterministic evidence for one parsed RFC-822 message.

    Malformed/ambiguous identifier ancestry and undecodable attachments raise a generic
    :class:`ValueError` that never repeats private header or body values. A missing Message-ID is
    allowed and receives a deterministic fallback key derived from the other captured evidence.
    This function performs no I/O and never prints source fields.
    """
    message_id = _normalized_message_id(msg)
    in_reply_to = _normalized_message_id_list(
        _raw_header_values(msg, "In-Reply-To"), label="In-Reply-To"
    )
    references = _normalized_message_id_list(
        _raw_header_values(msg, "References"), label="References"
    )
    message_date = _single_header(msg, "Date")
    sender_address = _sender_address(msg)
    top_authored_text = _message_top_authored_text(msg)
    top_authored_sha256 = _sha256_text(top_authored_text)
    attachments = _attachment_evidence(msg)
    attachment_payload = [_attachment_payload(item) for item in attachments]

    if message_id:
        message_key = f"mail:v1:{_sha256_text(message_id)}"
    else:
        fallback_payload = {
            "date": message_date,
            "sender_address": sender_address,
            "top_authored_sha256": top_authored_sha256,
            "in_reply_to": list(in_reply_to),
            "references": list(references),
            "attachments": attachment_payload,
        }
        message_key = f"mail:v1:{_sha256_json(fallback_payload)}"

    evidence_payload = {
        "message_key": message_key,
        "message_id": message_id,
        "in_reply_to": list(in_reply_to),
        "references": list(references),
        "date": message_date,
        "sender_address": sender_address,
        "top_authored_sha256": top_authored_sha256,
        "attachments": attachment_payload,
    }
    return MailEvidence(
        message_key=message_key,
        message_id=message_id,
        in_reply_to=in_reply_to,
        references=references,
        date=message_date,
        sender_address=sender_address,
        top_authored_text=top_authored_text,
        top_authored_sha256=top_authored_sha256,
        attachments=attachments,
        evidence_sha256=_sha256_json(evidence_payload),
    )


# --- Label / value extraction ----------------------------------------------

# Numbered line-item label, e.g. "1. Date:", "1.Amount:", "3. Amount :". Tolerates the
# inconsistent spacing real form emails emit around the "." and the ":".
_ITEM_LABEL = re.compile(
    r"^\s*(\d+)\s*\.\s*(date|event or budget category|category|description|amount)\s*:",
    re.IGNORECASE,
)

# Top-level (non-numbered) labels we read. Order-independent; matched per line.
_TOP_LABELS: dict[str, re.Pattern[str]] = {
    "requestor_name": re.compile(r"^\s*requestor.*name\s*:", re.IGNORECASE),
    "requestor_email": re.compile(r"^\s*email\s*:", re.IGNORECASE),
    "phone": re.compile(r"^\s*phone\s*:", re.IGNORECASE),
    "company": re.compile(r"^\s*company\s*(name)?\s*:", re.IGNORECASE),
    "total": re.compile(r"^\s*total\s*amount\b.*:", re.IGNORECASE),
    "payment_type": re.compile(r"^\s*(choose\s*)?payment\s*type\s*:", re.IGNORECASE),
    "notes": re.compile(r"^\s*notes?\s*:", re.IGNORECASE),
}

# Exact form-upload labels whose values are receipt URLs. ``_PDF_LABEL`` remains the legacy-v1
# subset used by ``Submission.source_receipt_urls_v1`` so widening the public receipt inventory
# does not mutate already-reviewed source-evidence digests.
_PDF_LABEL = re.compile(r"^\s*pdf\s*\d*\s*:", re.IGNORECASE)
_RECEIPT_URL_LABEL = re.compile(r"^\s*(?:pdf|jpe?g|png)\s*\d*\s*:", re.IGNORECASE)

# A bare URL line (fallback receipt-link capture).
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _is_any_label(line: str) -> bool:
    """Frozen v1 label boundary used by all original form field extraction."""
    if _ITEM_LABEL.search(line) or _PDF_LABEL.search(line):
        return True
    return any(pattern.search(line) for pattern in _TOP_LABELS.values())


def _is_receipt_value_label(line: str) -> bool:
    """Expanded boundary used only while extracting the additive receipt URL inventory."""

    if _ITEM_LABEL.search(line) or _RECEIPT_URL_LABEL.search(line):
        return True
    return any(pattern.search(line) for pattern in _TOP_LABELS.values())


def _value_for(
    lines: list[str],
    idx: int,
    match_end: int,
    *,
    label_check: Callable[[str], bool] = _is_any_label,
) -> str:
    """Value for a label found on ``lines[idx]``: same-line tail, else next non-label line."""
    tail = lines[idx][match_end:].strip()
    if tail:
        return tail
    for candidate in lines[idx + 1 :]:
        stripped = candidate.strip()
        if not stripped:
            continue
        # A blank value: the next non-empty line is the following label, not this value.
        return "" if label_check(stripped) else stripped
    return ""


def _extract_top(lines: list[str], pattern: re.Pattern[str]) -> str:
    for idx, line in enumerate(lines):
        match = pattern.search(line)
        if match:
            return _value_for(lines, idx, match.end())
    return ""


def _extract_line_items(lines: list[str]) -> tuple[LineItem, ...]:
    """Collect numbered line items; per index gather whatever sub-fields are present."""
    fields: dict[int, dict[str, str]] = {}
    for idx, line in enumerate(lines):
        match = _ITEM_LABEL.search(line)
        if not match:
            continue
        number = int(match.group(1))
        label = match.group(2).lower()
        key = "category" if label in ("event or budget category", "category") else label
        value = _value_for(lines, idx, match.end())
        fields.setdefault(number, {})[key] = value

    items: list[LineItem] = []
    for number in sorted(fields):
        row = fields[number]
        items.append(
            LineItem(
                index=number,
                date=row.get("date", ""),
                category=row.get("category", ""),
                description=row.get("description", ""),
                amount=row.get("amount", ""),
            )
        )
    return tuple(items)


def _extract_receipt_urls(
    lines: list[str],
    *,
    label_pattern: re.Pattern[str] = _RECEIPT_URL_LABEL,
    label_check: Callable[[str], bool] = _is_receipt_value_label,
) -> tuple[str, ...]:
    """URLs following an exact recognized form-upload label, de-duplicated in source order."""
    urls: list[str] = []
    for idx, line in enumerate(lines):
        label = label_pattern.search(line)
        if label:
            value = _value_for(lines, idx, label.end(), label_check=label_check)
            found = _URL_RE.search(value)
            if found:
                urls.append(found.group(0))
    # De-duplicate, preserve order.
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return tuple(unique)


# --- Recognition + top-level parse -----------------------------------------


def looks_like_reimbursement(subject: str, text: str, *, subject_filter: str | None = None) -> bool:
    """Heuristic: is this email a reimbursement-form submission?

    Structural signal (no org identity): the body carries a labeled grand Total AND at least
    one numbered line item. When ``subject_filter`` is given, the subject must also contain it
    (case-insensitive) — an optional operator-supplied narrowing (e.g. the form's name).
    """
    if subject_filter and subject_filter.casefold() not in subject.casefold():
        return False
    lines = text.splitlines()
    has_total = any(_TOP_LABELS["total"].search(line) for line in lines)
    has_item = any(_ITEM_LABEL.search(line) for line in lines)
    return has_total and has_item


# A reply/forward subject prefix (``Re:`` / ``Fwd:`` / ``Fw:``), case-insensitive.
_REPLY_PREFIX = re.compile(r"^\s*(re|fwd|fw)\s*:", re.IGNORECASE)


def is_reply_or_forward(subject: str) -> bool:
    """True when ``subject`` carries a reply/forward prefix (``Re:`` / ``Fwd:`` / ``Fw:``).

    Reply/forward notifications re-quote the original form body, so the parser recognizes them as
    submissions too — but they are THREAD DUPLICATES of an original submission: a different
    ``Message-ID`` for the *same* reimbursement, so ``message_id`` idempotency will NOT catch them.
    Callers ingesting submissions should skip these; the profile counts them separately so the
    duplicate volume stays visible.
    """
    return _REPLY_PREFIX.match(subject) is not None


def parse_received_date(raw: str) -> date | None:
    """Parse an RFC-822 ``Date`` header to its header-local calendar date.

    Returns ``None`` for a missing or malformed header. Deliberately does not convert an aware
    datetime to UTC: receipt-ledger membership follows the calendar date written in the header.
    """
    if raw.strip() == "":
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed.date() if parsed is not None else None


def parse_submission(msg: Message, *, subject_filter: str | None = None) -> Submission | None:
    """Parse an email into a :class:`Submission`, or ``None`` if it is not a reimbursement form.

    Tries each rendered body candidate (:func:`body_candidates`) and parses from the FIRST one
    that reads as a reimbursement form, so an HTML-primary email with a plain-text stub (or vice
    versa) is still recognized.
    """
    subject = str(msg.get("Subject", "")).strip()
    text = ""
    for candidate in body_candidates(msg):
        if looks_like_reimbursement(subject, candidate, subject_filter=subject_filter):
            text = candidate
            break
    if not text:
        return None

    lines = text.splitlines()
    return Submission(
        message_id=str(msg.get("Message-ID", "")).strip(),
        subject=subject,
        received=str(msg.get("Date", "")).strip(),
        requestor_name=_extract_top(lines, _TOP_LABELS["requestor_name"]),
        requestor_email=_extract_top(lines, _TOP_LABELS["requestor_email"]),
        phone=_extract_top(lines, _TOP_LABELS["phone"]),
        company=_extract_top(lines, _TOP_LABELS["company"]),
        line_items=_extract_line_items(lines),
        total=_extract_top(lines, _TOP_LABELS["total"]),
        payment_type=_extract_top(lines, _TOP_LABELS["payment_type"]),
        receipt_urls=_extract_receipt_urls(lines),
        attachments=attachment_names(msg),
        notes=_extract_top(lines, _TOP_LABELS["notes"]),
        source_receipt_urls_v1=_extract_receipt_urls(
            lines, label_pattern=_PDF_LABEL, label_check=_is_any_label
        ),
    )


def iter_eml(source: Path) -> Iterator[tuple[Path, Message]]:
    """Yield ``(path, message)`` for every ``.eml`` file under ``source`` (sorted by name).

    ``source`` may be a single ``.eml`` file or a directory. Uses the modern email policy so
    parts are convenient :class:`~email.message.EmailMessage` objects.
    """
    if source.is_file():
        paths = [source]
    else:
        paths = sorted(source.glob("*.eml"))
    for path in paths:
        with path.open("rb") as handle:
            msg = email.message_from_binary_file(handle, policy=email.policy.default)
        yield path, msg


def iter_mbox(path: Path) -> Iterator[tuple[str, Message]]:
    """Yield ``(label, message)`` for every message in an ``mbox`` file (a Google Takeout export).

    Reads each member's raw bytes and re-parses it under the modern email policy (so the messages
    are the same shape :func:`iter_eml` produces), rather than relying on ``mailbox``'s legacy
    compat32 message factory. ``label`` is ``<mbox-filename>#<n>`` for display/provenance.
    """
    box = mailbox.mbox(str(path))
    try:
        for key in box.iterkeys():
            raw = box.get_bytes(key)
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            yield f"{path.name}#{key}", msg
    finally:
        box.close()


def iter_source(source: Path) -> Iterator[tuple[str, Message]]:
    """Yield ``(label, message)`` for every email under ``source`` — ``.eml`` files OR an ``.mbox``.

    Dispatches on ``source``: a single ``.mbox`` file (e.g. a Google Takeout export) is read via
    :func:`iter_mbox`; a directory yields every ``*.eml`` then every ``*.mbox`` inside it; anything
    else is treated as a single ``.eml`` file. ``label`` is a display string (filename, or
    ``<mbox>#<n>`` for mbox members) — the general entry point the CLI iterates, so one batch can
    mix hand-downloaded ``.eml`` samples and a full ``.mbox`` backfill transparently.
    """
    if source.is_file() and source.suffix.lower() == ".mbox":
        yield from iter_mbox(source)
        return
    if source.is_dir():
        for path, msg in iter_eml(source):
            yield path.name, msg
        for mbox_path in sorted(source.glob("*.mbox")):
            yield from iter_mbox(mbox_path)
        return
    for path, msg in iter_eml(source):
        yield path.name, msg


# --- Reconciliation helpers (preview-time sanity, not yet a write gate) -----


_RECEIPT_AMOUNT_MAX_TEXT_CHARS = 128
_RECEIPT_AMOUNT_MAX_DIGITS = 64
_RECEIPT_AMOUNT_MAX_ADJUSTED_EXPONENT = 18


def parse_finite_amount(raw: str) -> Decimal:
    """Parse one bounded monetary value and reject unsafe/non-finite Decimal spellings."""
    if len(raw) > _RECEIPT_AMOUNT_MAX_TEXT_CHARS:
        raise ValueError("receipt monetary amount is too long")
    try:
        amount = models.parse_amount(raw)
        finite = amount.is_finite()
    except DecimalException as exc:
        raise ValueError("receipt monetary amount must be finite") from exc
    if not finite:
        raise ValueError("receipt monetary amount must be finite")
    if (
        len(amount.as_tuple().digits) > _RECEIPT_AMOUNT_MAX_DIGITS
        or abs(amount.adjusted()) > _RECEIPT_AMOUNT_MAX_ADJUSTED_EXPONENT
    ):
        raise ValueError("receipt monetary amount is outside supported bounds")
    return amount


def line_item_total(sub: Submission) -> Decimal | None:
    """Sum of finite line-item amounts, or ``None`` if any present amount is unavailable."""
    total = Decimal("0")
    saw_any = False
    for item in sub.line_items:
        if item.amount.strip() == "":
            continue
        try:
            total += parse_finite_amount(item.amount)
            saw_any = True
        except (ValueError, DecimalException):
            return None
        if not total.is_finite():
            return None
    return total if saw_any else None


def stated_total(sub: Submission) -> Decimal | None:
    """The email's finite stated grand total, or ``None`` if absent/unavailable."""
    if sub.total.strip() == "":
        return None
    try:
        return parse_finite_amount(sub.total)
    except (ValueError, DecimalException):
        return None


def total_reconciles(sub: Submission) -> bool | None:
    """Do the line items sum to the stated total? ``None`` when either side is unavailable."""
    items = line_item_total(sub)
    stated = stated_total(sub)
    if items is None or stated is None:
        return None
    try:
        return items == stated
    except DecimalException:
        return None


# --- Profiling (the "meta load": aggregate, PII-free) ----------------------


_SUBMISSION_MARKER = "got a new submission"


def form_type(subject: str) -> str:
    """Normalize a submission subject to its form name, e.g. ``"Main Reimbursement Form"``.

    Form-notification subjects look like ``"<Form Name> got a new submission"``; strip that
    trailing boilerplate so submissions group by form. Falls back to the whole (stripped) subject
    when the marker is absent, or ``"(no subject)"`` when empty.
    """
    lowered = subject.casefold()
    cut = lowered.find(_SUBMISSION_MARKER)
    name = (subject[:cut] if cut != -1 else subject).strip()
    return name or "(no subject)"


@dataclass(frozen=True)
class Profile:
    """Aggregate, PII-free profile of a batch of parsed submissions (the "meta load").

    Counts and distributions only — NO requestor names/emails/phones are retained (only the COUNT
    of distinct requestors). Built by :func:`profile`, rendered by ``ingest-receipts --profile``.
    Its purpose is to reveal the full data spread — every form type, the complete category
    vocabulary, field-blank rates, reconciliation failures, fiscal-year span — so the canonical
    schema + category map can be designed ONCE against the true distribution instead of a handful
    of samples. Each ``*_counts``/distribution tuple is ``(key, count)`` sorted by count desc.
    """

    recognized: int
    received_span: tuple[str, str]
    form_types: tuple[tuple[str, int], ...]
    reconcile_yes: int
    reconcile_no: int
    reconcile_na: int
    line_items: int
    blank_category_items: int
    blank_amount_items: int
    no_date_items: int
    zero_receipt_submissions: int
    categories: tuple[tuple[str, int], ...]
    payment_types: tuple[tuple[str, int], ...]
    fiscal_years: tuple[tuple[str, int], ...]
    unparseable_amounts: int
    unparseable_dates: int
    distinct_requestors: int


def _sorted_counts(counter: Counter[str]) -> tuple[tuple[str, int], ...]:
    """A ``Counter`` as ``(key, count)`` sorted by count desc then key asc (deterministic)."""
    return tuple(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def profile(subs: Iterable[Submission], *, start_month: int = 1) -> Profile:
    """Aggregate parsed submissions into a PII-free :class:`Profile` (PURE — no I/O).

    ``start_month`` buckets line-item dates into fiscal years (a July-start org passes ``7``).
    Requestors are tallied by email (falling back to name), casefolded, but only the DISTINCT
    COUNT is kept — no identity is stored on the returned :class:`Profile`.
    """
    recognized = 0
    reconcile_yes = reconcile_no = reconcile_na = 0
    line_items = blank_category = blank_amount = no_date = 0
    zero_receipts = bad_amounts = bad_dates = 0
    form_types: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    payment_types: Counter[str] = Counter()
    fiscal_years: Counter[str] = Counter()
    requestors: set[str] = set()
    received_dates: list[date] = []

    for sub in subs:
        recognized += 1
        form_types[form_type(sub.subject)] += 1
        received_date = parse_received_date(sub.received)
        if received_date is not None:
            received_dates.append(received_date)
        payment_types[sub.payment_type.strip() or "(blank)"] += 1
        who = sub.requestor_email.strip().casefold() or sub.requestor_name.strip().casefold()
        if who:
            requestors.add(who)
        if not sub.receipt_urls and not sub.attachments:
            zero_receipts += 1

        recon = total_reconciles(sub)
        if recon is True:
            reconcile_yes += 1
        elif recon is False:
            reconcile_no += 1
        else:
            reconcile_na += 1

        for item in sub.line_items:
            line_items += 1
            categories[item.category.strip() or "(blank)"] += 1
            if item.category.strip() == "":
                blank_category += 1
            if item.amount.strip() == "":
                blank_amount += 1
            else:
                try:
                    parse_finite_amount(item.amount)
                except ValueError:
                    bad_amounts += 1
            if item.date.strip() == "":
                no_date += 1
            else:
                try:
                    parsed = models.parse_date(item.date)
                except ValueError:
                    bad_dates += 1
                else:
                    fiscal_years[f"FY{ids.fiscal_year_label(parsed, start_month)}"] += 1

    received_span = (
        (min(received_dates).isoformat(), max(received_dates).isoformat())
        if received_dates
        else ("", "")
    )
    return Profile(
        recognized=recognized,
        received_span=received_span,
        form_types=_sorted_counts(form_types),
        reconcile_yes=reconcile_yes,
        reconcile_no=reconcile_no,
        reconcile_na=reconcile_na,
        line_items=line_items,
        blank_category_items=blank_category,
        blank_amount_items=blank_amount,
        no_date_items=no_date,
        zero_receipt_submissions=zero_receipts,
        categories=_sorted_counts(categories),
        payment_types=_sorted_counts(payment_types),
        fiscal_years=_sorted_counts(fiscal_years),
        unparseable_amounts=bad_amounts,
        unparseable_dates=bad_dates,
        distinct_requestors=len(requestors),
    )
