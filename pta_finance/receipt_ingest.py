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

import email
import email.policy
import re
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path

from pta_finance import models

__all__ = [
    "LineItem",
    "Submission",
    "html_to_text",
    "body_candidates",
    "message_text",
    "attachment_names",
    "looks_like_reimbursement",
    "parse_submission",
    "iter_eml",
    "line_item_total",
    "stated_total",
    "total_reconciles",
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


# --- HTML -> text ----------------------------------------------------------

# Block-level tags after which rendered text starts a new line. Form emails lay out each
# "Label:" and its value in separate table cells / paragraphs, so preserving these breaks is
# what lets the line-oriented parser below pair a label with the value beneath it.
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "tr", "td", "th", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
)
_SKIP_CONTENT_TAGS = frozenset({"style", "script", "head"})


class _TextExtractor(HTMLParser):
    """Collect visible text from HTML, inserting newlines at block boundaries (stdlib only)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    """Render HTML to newline-separated visible text (stdlib ``html.parser``; no deps).

    Block tags become line breaks so a ``"Label:"`` element and its value element land on
    separate lines. Runs of blank lines collapse; each line is stripped.
    """
    extractor = _TextExtractor()
    extractor.feed(html)
    raw = extractor.text()
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


# --- Email body extraction -------------------------------------------------


def _decode_part(part: Message) -> str:
    """Decode one MIME part to text using its declared charset (utf-8 fallback)."""
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def body_candidates(msg: Message) -> list[str]:
    """Rendered-text candidate bodies, richest first: HTML(->text), then ``text/plain``.

    Form emails are HTML-primary; a co-present ``text/plain`` part is often just a
    "view in browser" stub. Returning BOTH (non-empty) lets the parser pick whichever body
    actually carries the reimbursement structure, rather than committing to one MIME type.
    Attachment parts are skipped.
    """
    plain: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if (part.get_content_disposition() or "") == "attachment":
            continue
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

# A "PDF:" / "PDF 1:" style label whose value is a receipt URL.
_PDF_LABEL = re.compile(r"^\s*pdf\s*\d*\s*:", re.IGNORECASE)

# A bare URL line (fallback receipt-link capture).
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _is_any_label(line: str) -> bool:
    """True when a line is itself a known label (so it is never mistaken for a value)."""
    if _ITEM_LABEL.search(line) or _PDF_LABEL.search(line):
        return True
    return any(pattern.search(line) for pattern in _TOP_LABELS.values())


def _value_for(lines: list[str], idx: int, match_end: int) -> str:
    """Value for a label found on ``lines[idx]``: same-line tail, else next non-label line."""
    tail = lines[idx][match_end:].strip()
    if tail:
        return tail
    for candidate in lines[idx + 1 :]:
        stripped = candidate.strip()
        if not stripped:
            continue
        # A blank value: the next non-empty line is the following label, not this value.
        return "" if _is_any_label(stripped) else stripped
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


def _extract_receipt_urls(lines: list[str]) -> tuple[str, ...]:
    """URLs following any ``PDF:`` label, plus any bare receipt-looking URL lines."""
    urls: list[str] = []
    for idx, line in enumerate(lines):
        label = _PDF_LABEL.search(line)
        if label:
            value = _value_for(lines, idx, label.end())
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


# --- Reconciliation helpers (preview-time sanity, not yet a write gate) -----


def line_item_total(sub: Submission) -> Decimal | None:
    """Sum of parseable line-item amounts, or ``None`` if any present amount is unparseable."""
    total = Decimal("0")
    saw_any = False
    for item in sub.line_items:
        if item.amount.strip() == "":
            continue
        try:
            total += models.parse_amount(item.amount)
            saw_any = True
        except ValueError:
            return None
    return total if saw_any else None


def stated_total(sub: Submission) -> Decimal | None:
    """The email's stated grand Total as a :class:`~decimal.Decimal`, or ``None`` if absent/bad."""
    if sub.total.strip() == "":
        return None
    try:
        return models.parse_amount(sub.total)
    except ValueError:
        return None


def total_reconciles(sub: Submission) -> bool | None:
    """Do the line items sum to the stated total? ``None`` when either side is unavailable."""
    items = line_item_total(sub)
    stated = stated_total(sub)
    if items is None or stated is None:
        return None
    return items == stated
