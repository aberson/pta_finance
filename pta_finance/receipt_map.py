"""Map parsed reimbursement :class:`~pta_finance.receipt_ingest.Submission` objects onto flat
"Reimbursements" ledger rows (one row per line item).

The rows are **denormalized** so the Sheet-side "Receipts Explorer" can ``QUERY`` them directly
— exactly how the "Group Explorer" tab reads the flat "Budget Timeseries" tab. Submission-level
fields (form type, requestor, payment type, receipt link, reconciliation) repeat on each of that
submission's line-item rows; per-line fields (date, category, amount) vary.

PURE + credential-free: this takes already-parsed ``Submission`` objects plus a category map and
returns row dicts. Writing the rows to the Sheet is a SEPARATE step. Design rules (from the
meta-load findings + operator decisions):

* **Originals only.** Callers pass submissions with ``Re:``/``Fwd:`` thread duplicates already
  dropped (:func:`pta_finance.receipt_ingest.is_reply_or_forward`). This module additionally drops
  a repeated ``Message-ID`` and an accidental-resubmit **content hash** (requestor + total + first
  date) as a backstop.
* **Blank category -> carry-forward, then needs_review.** A blank line-item category inherits the
  last non-blank category within the same submission; if still blank (or unmapped), the row is
  flagged ``needs_review`` — never a guessed category.
* **Skip blank-amount lines.** A line item with no amount is not a real expense; it is dropped.
* **needs_review** collects reasons: ``unmapped-category`` / ``bad-amount`` / ``total-mismatch``.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping
from decimal import Decimal
from pathlib import Path

from pta_finance import backup, ids, models, receipt_ingest
from pta_finance.receipt_ingest import Submission

__all__ = [
    "FIELDNAMES",
    "deduplicate_submissions",
    "load_category_map",
    "load_form_defaults",
    "map_submissions",
    "parse_finite_amount",
]

# Rows in the category-map CSV whose raw_category starts with this marker declare a per-form
# DEFAULT canonical category (e.g. the Teacher Reimbursement Form collects no category, so its
# blank-category lines default to one budget line instead of all landing in needs_review).
_FORM_DEFAULT_PREFIX = "FORM_DEFAULT:"

# Flat "Reimbursements" tab columns (denormalized, one row per line item).
FIELDNAMES: tuple[str, ...] = (
    "message_id",
    "received",
    "form_type",
    "requestor_name",
    "requestor_email",
    "date",
    "month",
    "fiscal_year",
    "raw_category",
    "canonical_category",
    "amount",
    "payment_type",
    "reconciles",
    "needs_review",
    "receipt_url",
)


def load_category_map(path: Path) -> dict[str, str]:
    """Load ``raw_category -> canonical_category`` from the category-map CSV (casefolded keys).

    Rows with a blank canonical (the ``(blank)`` sentinel, or a not-yet-filled mapping) are
    skipped — those raw categories resolve to ``""`` and get flagged ``needs_review`` downstream.
    """
    mapping: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw = backup.decode_formula_safe_text(row.get("raw_category") or "").strip()
            canonical = (row.get("canonical_category") or "").strip()
            if raw.startswith(_FORM_DEFAULT_PREFIX):
                continue  # a per-form default, not a category mapping (see load_form_defaults)
            if raw and raw != "(blank)" and canonical:
                mapping[raw.casefold()] = canonical
    return mapping


def load_form_defaults(path: Path) -> dict[str, str]:
    """Load per-form default categories from ``FORM_DEFAULT: <form>`` rows in the category map.

    Returns ``{form_type -> canonical_category}`` (e.g. ``{"Teacher Reimbursement Form":
    "Classroom Enhancements TK to 5th - Teacher Budget"}``). Applied only to blank-category lines
    of that form (see :func:`map_submissions`).
    """
    defaults: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw = (row.get("raw_category") or "").strip()
            canonical = (row.get("canonical_category") or "").strip()
            if raw.startswith(_FORM_DEFAULT_PREFIX) and canonical:
                form = raw[len(_FORM_DEFAULT_PREFIX) :].strip()
                if form:
                    defaults[form] = canonical
    return defaults


def _fy_of(date_str: str, start_month: int) -> str:
    """``FY<label>`` for a line-item date string, or ``""`` if it does not parse."""
    try:
        parsed = models.parse_date(date_str)
    except ValueError:
        return ""
    return f"FY{ids.fiscal_year_label(parsed, start_month)}"


def _month_of(date_str: str) -> str:
    """``YYYY-MM`` for a line-item date string, or ``""`` if it does not parse."""
    try:
        parsed = models.parse_date(date_str)
    except ValueError:
        return ""
    return f"{parsed.year:04d}-{parsed.month:02d}"


def parse_finite_amount(raw: str) -> Decimal:
    """Delegate to the receipt pipeline's shared finite-money validator."""
    return receipt_ingest.parse_finite_amount(raw)


def _norm_amount(raw: str) -> str:
    """Amount normalized to two decimals, or the raw text kept verbatim if unparseable."""
    try:
        return f"{parse_finite_amount(raw):.2f}"
    except ValueError:
        return raw


def _submission_fy(sub: Submission, start_month: int) -> str:
    """Fiscal year for a submission: first dated line item, else the email ``Date`` header."""
    for item in sub.line_items:
        fy = _fy_of(item.date, start_month)
        if fy:
            return fy
    received = receipt_ingest.parse_received_date(sub.received)
    if received is None:
        return ""
    return f"FY{ids.fiscal_year_label(received, start_month)}"


def _content_hash(sub: Submission) -> str:
    """Accidental-resubmit key: requestor + stated total + first line-item date."""
    who = sub.requestor_email.strip().casefold() or sub.requestor_name.strip().casefold()
    total = sub.total.strip()
    first_date = sub.line_items[0].date.strip() if sub.line_items else ""
    return f"{who}|{total}|{first_date}"


def _needs_review(amount: str, canonical: str, reconciles: bool | None) -> str:
    """Pipe-joined review reasons for a line-item row (``""`` when the row is clean)."""
    reasons: list[str] = []
    if canonical == "":
        reasons.append("unmapped-category")
    try:
        parse_finite_amount(amount)
    except ValueError:
        reasons.append("bad-amount")
    if reconciles is False:
        reasons.append("total-mismatch")
    return " | ".join(reasons)


def deduplicate_submissions(subs: Iterable[Submission]) -> tuple[Submission, ...]:
    """Return the submissions selected by the mapper's two stable deduplication rules.

    Keeping this selection step public lets other local consumers, such as the private
    reimbursement-report evidence builder, retain the full parsed submission while agreeing
    exactly with the flat ledger about which originals survived.  Input order is preserved.
    """
    selected: list[Submission] = []
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for sub in subs:
        message_id = sub.message_id.strip()
        if message_id and message_id in seen_ids:
            continue
        content_key = _content_hash(sub)
        if content_key in seen_hashes:
            continue
        if message_id:
            seen_ids.add(message_id)
        seen_hashes.add(content_key)
        selected.append(sub)
    return tuple(selected)


def map_submissions(
    subs: Iterable[Submission],
    *,
    category_map: Mapping[str, str],
    form_defaults: Mapping[str, str] | None = None,
    start_month: int,
) -> list[dict[str, str]]:
    """Project submissions onto flat ledger rows (one per non-blank-amount line item).

    Drops repeated ``Message-ID`` and accidental-resubmit content-hash duplicates, carries a blank
    line-item date/category forward within a submission, skips blank-amount lines, resolves the
    canonical category from ``category_map`` (casefolded), and flags ``needs_review``.

    ``form_defaults`` (``{form_type -> canonical}``) is applied ONLY when a line's category is
    still blank after carry-forward (the systematic no-category case, e.g. the Teacher form) — a
    non-blank but unmapped category is left ``needs_review``, never silently defaulted.
    """
    defaults = form_defaults or {}
    rows: list[dict[str, str]] = []
    for sub in deduplicate_submissions(subs):
        message_id = sub.message_id.strip()
        reconciles = receipt_ingest.total_reconciles(sub)
        reconciles_text = {True: "yes", False: "no", None: "n/a"}[reconciles]
        submission_fy = _submission_fy(sub, start_month)
        receipt_url = " | ".join(sub.receipt_urls)
        this_form = receipt_ingest.form_type(sub.subject)

        last_date = ""
        last_raw = ""
        for item in sub.line_items:
            if item.amount.strip() == "":
                continue  # not a real expense
            date = item.date.strip() or last_date
            raw_category = item.category.strip() or last_raw
            if item.date.strip():
                last_date = item.date.strip()
            if item.category.strip():
                last_raw = item.category.strip()

            canonical = category_map.get(raw_category.casefold(), "")
            if not canonical and not raw_category:
                canonical = defaults.get(this_form, "")  # per-form fallback for no-category lines
            fiscal_year = _fy_of(date, start_month) or submission_fy
            rows.append(
                {
                    "message_id": message_id,
                    "received": sub.received,
                    "form_type": this_form,
                    "requestor_name": sub.requestor_name,
                    "requestor_email": sub.requestor_email,
                    "date": date,
                    "month": _month_of(date),
                    "fiscal_year": fiscal_year,
                    "raw_category": raw_category,
                    "canonical_category": canonical,
                    "amount": _norm_amount(item.amount),
                    "payment_type": sub.payment_type,
                    "reconciles": reconciles_text,
                    "needs_review": _needs_review(item.amount, canonical, reconciles),
                    "receipt_url": receipt_url,
                }
            )
    return rows
