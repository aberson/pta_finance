"""Unit tests for the reimbursement-email parser (:mod:`pta_finance.receipt_ingest`).

Fixtures are synthetic and use OBVIOUSLY-FAKE identity only (``Jane Doe``,
``jane.doe@example.org``, ``Example Garden``) per the repo identity rule. They reproduce the
STRUCTURE observed in a real Wix reimbursement-form email — label and value on separate lines,
inconsistently-spaced numbered prefixes (``"1. Date:"`` / ``"1.Amount:"`` / ``"3. Amount :"``),
and a later line item that omits Date + Category — without any real data.

These validate parser behavior against that structure. The end-to-end confirmation that a REAL
``.eml``'s HTML renders to this same shape is a separate operator step (real samples live only
in the gitignored ``mail_samples/``).
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import mailbox
from dataclasses import fields
from datetime import date
from email.message import EmailMessage
from email.mime.message import MIMEMessage
from pathlib import Path

import pytest

from pta_finance import receipt_ingest

# --- Fixtures --------------------------------------------------------------

# A Wix-style HTML body: each label is its own element, the value the next (bold) element.
# Item 3 intentionally omits Date + Category (only Description + Amount), and the numbered
# prefixes vary in spacing exactly as the real email does.
_HTML_BODY = """\
<html><body>
<h1>Main Reimbursement Form got a new submission</h1>
<p>Submission summary:</p>
<p>Requestor First and Last Name:</p><p><strong>Jane Doe</strong></p>
<p>Email:</p><p><strong>jane.doe@example.org</strong></p>
<p>Phone:</p><p><strong>5551234567</strong></p>
<p>Company Name:</p><p><strong>Example Garden</strong></p>
<p>1. Date:</p><p><strong>2026-06-25</strong></p>
<p>1. Event or Budget Category:</p><p><strong>Garden Club</strong></p>
<p>1. Description:</p><p><strong>Boxes - Organization Items for Shed</strong></p>
<p>1.Amount:</p><p><strong>718.60</strong></p>
<p>2. Date:</p><p><strong>2026-06-25</strong></p>
<p>2. Event or Budget Category:</p><p><strong>Garden Club</strong></p>
<p>2. Description:</p><p><strong>Solar generator to charge tools</strong></p>
<p>2. Amount:</p><p><strong>279.40</strong></p>
<p>3. Description :</p><p><strong>Misc garden needs: tool kit, gloves, lights</strong></p>
<p>3. Amount :</p><p><strong>417.13</strong></p>
<p>Total Amount $:</p><p><strong>1415.13</strong></p>
<p>Choose Payment Type:</p><p><strong>Zelle</strong></p>
<p>PDF:</p><p><strong>https://example.com/ugd/receipt-a.pdf</strong></p>
<p>PDF 1:</p><p><strong>https://example.com/ugd/receipt-b.pdf</strong></p>
<p>NOTES:</p><p><strong>Items purchased for the garden.</strong></p>
</body></html>
"""


def _reimbursement_email(*, with_plain_stub: bool = False) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Main Reimbursement Form got a new submission"
    msg["From"] = "forms@wix-forms.com"
    msg["To"] = "treasurer@example.org"
    msg["Date"] = "Sun, 28 Jun 2026 09:09:00 -0700"
    msg["Message-ID"] = "<sample-1@example.org>"
    if with_plain_stub:
        # HTML-primary email whose text/plain alternative is a useless stub — the parser must
        # still recognize the submission from the HTML body.
        msg.set_content("Can't see this message? View in browser.")
        msg.add_alternative(_HTML_BODY, subtype="html")
    else:
        msg.set_content(_HTML_BODY, subtype="html")
    return msg


def _non_reimbursement_email() -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Your Zelle payment was sent"
    msg["From"] = "no-reply@bank.example.com"
    msg["Date"] = "Mon, 29 Jun 2026 10:00:00 -0700"
    msg["Message-ID"] = "<other-1@example.org>"
    msg.set_content("You sent $50.00 to a recipient. No line items, no total here.")
    return msg


# --- html_to_text ----------------------------------------------------------


def test_html_to_text_breaks_blocks_onto_separate_lines() -> None:
    text = receipt_ingest.html_to_text("<p>Label:</p><p><strong>Value</strong></p>")
    lines = [line for line in text.splitlines() if line]
    assert lines == ["Label:", "Value"]


def test_html_to_text_skips_script_and_style() -> None:
    text = receipt_ingest.html_to_text(
        "<style>.x{color:red}</style><p>Keep</p><script>alert(1)</script>"
    )
    assert "color" not in text and "alert" not in text
    assert "Keep" in text


# --- recognition -----------------------------------------------------------


def test_parses_reimbursement_email() -> None:
    sub = receipt_ingest.parse_submission(_reimbursement_email())
    assert sub is not None
    assert sub.requestor_name == "Jane Doe"
    assert sub.requestor_email == "jane.doe@example.org"
    assert sub.company == "Example Garden"
    assert sub.total == "1415.13"
    assert sub.payment_type == "Zelle"


def test_non_reimbursement_email_returns_none() -> None:
    assert receipt_ingest.parse_submission(_non_reimbursement_email()) is None


def test_recognized_through_plain_text_stub() -> None:
    # The text/plain part is a stub; recognition must come from the HTML alternative.
    sub = receipt_ingest.parse_submission(_reimbursement_email(with_plain_stub=True))
    assert sub is not None
    assert len(sub.line_items) == 3


def test_subject_filter_narrows_recognition() -> None:
    email_msg = _reimbursement_email()
    assert receipt_ingest.parse_submission(email_msg, subject_filter="Reimbursement") is not None
    assert receipt_ingest.parse_submission(email_msg, subject_filter="Field Trip") is None


def test_parse_received_date_preserves_header_local_calendar_date() -> None:
    # In UTC this instant is still the prior day. Ledger membership follows the RFC-822
    # header's own calendar date, so no timezone conversion is allowed here.
    assert receipt_ingest.parse_received_date("Wed, 01 Jul 2030 00:30:00 +1400") == date(2030, 7, 1)


@pytest.mark.parametrize("raw", ["", "not a date"])
def test_parse_received_date_returns_none_for_missing_or_malformed(raw: str) -> None:
    assert receipt_ingest.parse_received_date(raw) is None


# --- line items ------------------------------------------------------------


def test_line_items_extracted_in_order() -> None:
    sub = receipt_ingest.parse_submission(_reimbursement_email())
    assert sub is not None
    assert [item.index for item in sub.line_items] == [1, 2, 3]

    first = sub.line_items[0]
    assert first.date == "2026-06-25"
    assert first.category == "Garden Club"
    assert first.amount == "718.60"
    assert first.description.startswith("Boxes")


def test_line_item_with_missing_date_and_category() -> None:
    sub = receipt_ingest.parse_submission(_reimbursement_email())
    assert sub is not None
    third = sub.line_items[2]
    # Item 3 omitted Date + Category in the source; those must be blank, not the next label.
    assert third.date == ""
    assert third.category == ""
    assert third.amount == "417.13"
    assert third.description.startswith("Misc garden needs")


# --- receipts + reconciliation ---------------------------------------------


def test_receipt_urls_collected_and_deduped() -> None:
    sub = receipt_ingest.parse_submission(_reimbursement_email())
    assert sub is not None
    assert sub.receipt_urls == (
        "https://example.com/ugd/receipt-a.pdf",
        "https://example.com/ugd/receipt-b.pdf",
    )
    assert sub.source_receipt_urls_v1 == sub.receipt_urls


def test_submission_existing_constructor_fields_remain_compatible() -> None:
    parsed = receipt_ingest.parse_submission(_reimbursement_email())
    assert parsed is not None
    existing_values = {
        item.name: getattr(parsed, item.name)
        for item in fields(receipt_ingest.Submission)
        if item.name != "source_receipt_urls_v1"
    }

    reconstructed = receipt_ingest.Submission(**existing_values)

    assert reconstructed.source_receipt_urls_v1 == ()


def _email_with_upload_labels(*labels: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Example Reimbursement Form got a new submission"
    msg["From"] = "forms@example.invalid"
    msg["Date"] = "Sun, 01 Sep 2030 09:00:00 -0700"
    msg["Message-ID"] = "<upload-labels@example.invalid>"
    uploads = "\n".join(
        f"{label}:\nhttps://files.example.invalid/{index}.bin"
        for index, label in enumerate(labels, start=1)
    )
    msg.set_content(
        f"""\
1. Description:
Synthetic purchase
1. Amount:
1.00
Total Amount $:
1.00
{uploads}
"""
    )
    return msg


@pytest.mark.parametrize(
    "label",
    ["PDF", "pdf 12", "Jpeg", "jPeG 2", "JPG", "jpg3", "JPEG", "JPEG 4", "PNG", "pNg 5"],
)
def test_exact_form_upload_labels_are_recognized(label: str) -> None:
    sub = receipt_ingest.parse_submission(_email_with_upload_labels(label))
    assert sub is not None
    expected_url = "https://files.example.invalid/1.bin"
    assert sub.receipt_urls == (expected_url,)
    expected_v1 = (expected_url,) if label.casefold().startswith("pdf") else ()
    assert sub.source_receipt_urls_v1 == expected_v1


def test_signature_image_and_non_receipt_labels_are_excluded() -> None:
    sub = receipt_ingest.parse_submission(
        _email_with_upload_labels(
            "Signature Image",
            "GIF",
            "JPEG Preview",
            "PNG Upload",
            "PDF Document",
        )
    )
    assert sub is not None
    assert sub.receipt_urls == ()
    assert sub.source_receipt_urls_v1 == ()


def test_total_reconciles_when_items_sum_to_total() -> None:
    sub = receipt_ingest.parse_submission(_reimbursement_email())
    assert sub is not None
    assert receipt_ingest.total_reconciles(sub) is True


def test_total_mismatch_is_detected() -> None:
    sub = receipt_ingest.parse_submission(_reimbursement_email())
    assert sub is not None
    tampered = receipt_ingest.Submission(**{**sub.__dict__, "total": "999.99"})
    assert receipt_ingest.total_reconciles(tampered) is False


def test_reconcile_none_when_an_amount_is_unparseable() -> None:
    sub = receipt_ingest.parse_submission(_reimbursement_email())
    assert sub is not None
    bad_item = receipt_ingest.LineItem(index=4, date="", category="", description="x", amount="N/A")
    tampered = receipt_ingest.Submission(
        **{**sub.__dict__, "line_items": (*sub.line_items, bad_item)}
    )
    assert receipt_ingest.total_reconciles(tampered) is None


@pytest.mark.parametrize(
    "amount",
    [
        "NaN",
        "+NaN",
        "-NaN",
        "sNaN",
        "+sNaN",
        "-sNaN",
        "Infinity",
        "+Infinity",
        "-Infinity",
        "Inf",
        "+Inf",
        "-Inf",
    ],
)
def test_reconciliation_treats_every_nonfinite_spelling_as_unavailable(amount: str) -> None:
    sub = receipt_ingest.parse_submission(_reimbursement_email())
    assert sub is not None
    item = receipt_ingest.LineItem(1, "2030-09-01", "Supplies", "Example item", amount)
    tampered = receipt_ingest.Submission(**{**sub.__dict__, "line_items": (item,), "total": amount})

    assert receipt_ingest.line_item_total(tampered) is None
    assert receipt_ingest.stated_total(tampered) is None
    assert receipt_ingest.total_reconciles(tampered) is None


def test_reconciliation_treats_opposite_infinities_as_unavailable() -> None:
    sub = receipt_ingest.parse_submission(_reimbursement_email())
    assert sub is not None
    items = (
        receipt_ingest.LineItem(1, "2030-09-01", "Supplies", "First item", "Infinity"),
        receipt_ingest.LineItem(2, "2030-09-01", "Supplies", "Second item", "-Infinity"),
    )
    tampered = receipt_ingest.Submission(**{**sub.__dict__, "line_items": items, "total": "0.00"})

    assert receipt_ingest.line_item_total(tampered) is None
    assert receipt_ingest.total_reconciles(tampered) is None


@pytest.mark.parametrize(
    "amount",
    ["1e100000", "1e-100000", "-1e100000", "-1e-100000", "9" * 129],
)
def test_receipt_amount_bounds_reject_compact_exponents_and_oversized_text(amount: str) -> None:
    with pytest.raises(ValueError, match="receipt monetary amount"):
        receipt_ingest.parse_finite_amount(amount)


# --- iter_eml (disk round-trip) --------------------------------------------


def test_iter_eml_reads_directory(tmp_path: Path) -> None:
    (tmp_path / "a.eml").write_bytes(bytes(_reimbursement_email()))
    (tmp_path / "b.eml").write_bytes(bytes(_non_reimbursement_email()))

    parsed = [receipt_ingest.parse_submission(msg) for _, msg in receipt_ingest.iter_eml(tmp_path)]
    recognized = [sub for sub in parsed if sub is not None]
    assert len(parsed) == 2
    assert len(recognized) == 1
    assert recognized[0].requestor_name == "Jane Doe"


def test_attachments_are_listed() -> None:
    msg = _reimbursement_email()
    msg.add_attachment(
        b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="receipt-a.pdf"
    )
    sub = receipt_ingest.parse_submission(msg)
    assert sub is not None
    assert sub.attachments == ("receipt-a.pdf",)


# --- stable mail evidence --------------------------------------------------


_JPEG_BYTES = b"\xff\xd8\xff\xe0synthetic-jpeg-payload\xff\xd9"


def _mail_evidence_email() -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Re: Synthetic reimbursement question"
    msg["From"] = "Example Reviewer <REVIEWER@Example.Invalid>"
    msg["To"] = "treasurer@example.invalid"
    msg["Date"] = "Mon, 02 Sep 2030 10:15:00 -0700"
    msg["Message-ID"] = "<Reply.Case@Example.Invalid>"
    msg["In-Reply-To"] = "<Root.Case@Example.Invalid>"
    msg["References"] = (
        "<Ancestor@Example.Invalid> <Root.Case@EXAMPLE.INVALID> <Root.Case@example.invalid>"
    )
    msg.set_content(
        "Approved for the synthetic amount.\n"
        "Please use the attached image.\n\n"
        "On Sun, Sep 1, 2030 at 9:00 AM Example Sender wrote:\n"
        "> Prior private thread content.\n"
    )
    msg.add_attachment(
        _JPEG_BYTES,
        maintype="image",
        subtype="jpeg",
        filename="synthetic-receipt.JPG",
    )
    return msg


def test_normalize_message_id_is_strict_and_stable() -> None:
    assert receipt_ingest.normalize_message_id("  <Left.Case@EXAMPLE.Invalid>  ") == (
        "<Left.Case@example.invalid>"
    )
    assert receipt_ingest.normalize_message_id("Left.Case@example.invalid") == ""
    assert receipt_ingest.normalize_message_id("<one@example.invalid> extra") == ""
    assert receipt_ingest.normalize_message_id("<one@example.invalid> <two@example.invalid>") == ""
    assert receipt_ingest.normalize_message_id("<folded@exam\r\n ple.invalid>") == ""


@pytest.mark.parametrize(
    "message_id",
    [
        "<comma,value@example.invalid>",
        "<comment(value)@example.invalid>",
        "<nönascii@example.invalid>",
        "<leading.dot.@example.invalid>",
        "<valid@example..invalid>",
    ],
)
def test_normalize_message_id_rejects_non_dot_atom_syntax(message_id: str) -> None:
    assert receipt_ingest.normalize_message_id(message_id) == ""


def test_parse_mail_evidence_captures_ancestry_authored_text_and_decoded_jpeg() -> None:
    msg = _mail_evidence_email()
    evidence = receipt_ingest.parse_mail_evidence(msg)

    assert evidence.message_id == "<Reply.Case@example.invalid>"
    assert (
        evidence.message_key
        == "mail:v1:" + hashlib.sha256(evidence.message_id.encode("utf-8")).hexdigest()
    )
    assert evidence.in_reply_to == ("<Root.Case@example.invalid>",)
    assert evidence.references == (
        "<Ancestor@example.invalid>",
        "<Root.Case@example.invalid>",
    )
    assert evidence.date == "Mon, 02 Sep 2030 10:15:00 -0700"
    assert evidence.sender_address == "reviewer@example.invalid"
    assert evidence.top_authored_text == (
        "Approved for the synthetic amount.\nPlease use the attached image."
    )
    assert (
        evidence.top_authored_sha256
        == hashlib.sha256(evidence.top_authored_text.encode("utf-8")).hexdigest()
    )
    assert evidence.attachments == (
        receipt_ingest.AttachmentEvidence(
            mime_type="image/jpeg",
            filename="synthetic-receipt.JPG",
            decoded_size=len(_JPEG_BYTES),
            content_sha256=hashlib.sha256(_JPEG_BYTES).hexdigest(),
        ),
    )
    assert len(evidence.evidence_sha256) == 64
    assert receipt_ingest.parse_mail_evidence(msg) == evidence

    # Reprs are safe for incidental diagnostics and omit mailbox content/identifiers/filenames.
    assert "reviewer@example.invalid" not in repr(evidence)
    assert "Approved for" not in repr(evidence)
    assert "synthetic-receipt.JPG" not in repr(evidence.attachments[0])


@pytest.mark.parametrize(
    "quoted_html",
    [
        "<blockquote><p>Prior private thread content.</p></blockquote>",
        '<div class="gmail_quote"><p>Prior private thread content.</p></div>',
    ],
)
def test_parse_mail_evidence_strips_structural_html_quotes(quoted_html: str) -> None:
    msg = EmailMessage()
    msg["From"] = "reviewer@example.invalid"
    msg["Date"] = "Mon, 02 Sep 2030 10:15:00 -0700"
    msg["Message-ID"] = "<html-reply@example.invalid>"
    msg.set_content("Plain fallback")
    msg.add_alternative(
        f"<html><body><p>HTML-authored answer.</p>{quoted_html}</body></html>",
        subtype="html",
    )

    evidence = receipt_ingest.parse_mail_evidence(msg)

    assert evidence.top_authored_text == "HTML-authored answer."


def test_missing_message_id_uses_deterministic_privacy_safe_fallback_key() -> None:
    msg = _mail_evidence_email()
    del msg["Message-ID"]

    first = receipt_ingest.parse_mail_evidence(msg)
    second = receipt_ingest.parse_mail_evidence(msg)

    assert first.message_id == ""
    assert first.message_key.startswith("mail:v1:")
    assert len(first.message_key) == len("mail:v1:") + 64
    assert first.message_key == second.message_key
    assert "reviewer" not in first.message_key


def test_missing_message_id_fallback_includes_rfc_ancestry() -> None:
    first_msg = _mail_evidence_email()
    second_msg = _mail_evidence_email()
    del first_msg["Message-ID"]
    del second_msg["Message-ID"]
    del second_msg["References"]
    second_msg["References"] = "<Different.Root@example.invalid>"

    first = receipt_ingest.parse_mail_evidence(first_msg)
    second = receipt_ingest.parse_mail_evidence(second_msg)

    assert first.top_authored_sha256 == second.top_authored_sha256
    assert first.message_key != second.message_key


def test_inline_forwarded_message_is_not_top_authored_text() -> None:
    forwarded = EmailMessage()
    forwarded["From"] = "forwarded@example.invalid"
    forwarded.set_content("Private forwarded reimbursement approval text.")
    outer = EmailMessage()
    outer["From"] = "reviewer@example.invalid"
    outer["Date"] = "Mon, 02 Sep 2030 10:15:00 -0700"
    outer["Message-ID"] = "<inline-forward@example.invalid>"
    outer.set_content("Top-authored synthetic response.")
    outer.make_mixed()
    outer.attach(MIMEMessage(forwarded))

    evidence = receipt_ingest.parse_mail_evidence(outer)

    assert evidence.top_authored_text == "Top-authored synthetic response."
    assert "forwarded reimbursement" not in evidence.top_authored_text


def test_malformed_base64_attachment_fails_closed() -> None:
    msg = EmailMessage()
    msg["From"] = "reviewer@example.invalid"
    msg["Date"] = "Mon, 02 Sep 2030 10:15:00 -0700"
    msg["Message-ID"] = "<bad-base64@example.invalid>"
    msg.set_content("Synthetic receipt attached.")
    msg.make_mixed()
    attachment = EmailMessage()
    attachment["Content-Type"] = "image/jpeg"
    attachment["Content-Disposition"] = 'attachment; filename="synthetic.jpg"'
    attachment["Content-Transfer-Encoding"] = "base64"
    attachment.set_payload("%%%not-valid-base64%%")
    msg.attach(attachment)

    with pytest.raises(ValueError, match="base64 MIME payload is malformed"):
        receipt_ingest.parse_mail_evidence(msg)


@pytest.mark.parametrize("invalid_byte", [b"\xff", b"\xfe"])
def test_invalid_text_charset_bytes_fail_closed_without_digest_collision(
    invalid_byte: bytes,
) -> None:
    raw = (
        b"From: reviewer@example.invalid\r\n"
        b"Date: Mon, 02 Sep 2030 10:15:00 -0700\r\n"
        b"Message-ID: <invalid-text@example.invalid>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"Synthetic response " + invalid_byte + b".\r\n"
    )
    message = email.message_from_bytes(raw, policy=email.policy.default)

    with pytest.raises(ValueError, match="declared charset"):
        receipt_ingest.parse_mail_evidence(message)


@pytest.mark.parametrize(
    ("transfer_encoding", "payload", "message"),
    [
        ("x-private-transform", "opaque-wire-text", "unsupported transfer encoding"),
        ("quoted-printable", "=ZZ-not-hex", "quoted-printable MIME payload is malformed"),
    ],
)
def test_unsupported_or_malformed_attachment_encoding_fails_closed(
    transfer_encoding: str, payload: str, message: str
) -> None:
    msg = EmailMessage()
    msg["From"] = "reviewer@example.invalid"
    msg["Date"] = "Mon, 02 Sep 2030 10:15:00 -0700"
    msg["Message-ID"] = "<bad-transfer-encoding@example.invalid>"
    msg.set_content("Synthetic receipt attached.")
    msg.make_mixed()
    attachment = EmailMessage()
    attachment["Content-Type"] = "image/jpeg"
    attachment["Content-Disposition"] = 'attachment; filename="synthetic.jpg"'
    attachment["Content-Transfer-Encoding"] = transfer_encoding
    attachment.set_payload(payload)
    msg.attach(attachment)

    with pytest.raises(ValueError, match=message):
        receipt_ingest.parse_mail_evidence(msg)


@pytest.mark.parametrize(
    ("transfer_headers", "message"),
    [
        (["x-private-transform"], "unsupported transfer encoding"),
        (["7bit", "base64"], "ambiguous transfer encoding"),
        (["base64"], "message attachment uses an unsupported transfer encoding"),
    ],
)
@pytest.mark.parametrize("disposition", [None, "inline", "attachment"])
def test_message_attachment_encoding_is_validated_before_nested_payload(
    transfer_headers: list[str], message: str, disposition: str | None
) -> None:
    forwarded = EmailMessage()
    forwarded["From"] = "forwarded@example.invalid"
    forwarded.set_content("Synthetic forwarded content.")
    attachment = MIMEMessage(forwarded)
    if disposition is not None:
        attachment["Content-Disposition"] = (
            'attachment; filename="forwarded.eml"' if disposition == "attachment" else disposition
        )
    for transfer_header in transfer_headers:
        attachment["Content-Transfer-Encoding"] = transfer_header
    outer = EmailMessage()
    outer["From"] = "reviewer@example.invalid"
    outer["Date"] = "Mon, 02 Sep 2030 10:15:00 -0700"
    outer["Message-ID"] = "<bad-message-encoding@example.invalid>"
    outer.set_content("Top-authored synthetic response.")
    outer.make_mixed()
    outer.attach(attachment)

    with pytest.raises(ValueError, match=message):
        receipt_ingest.parse_mail_evidence(outer)


def test_identity_encoded_message_attachment_fails_closed_without_canonicalized_hash() -> None:
    forwarded = EmailMessage()
    forwarded["From"] = "forwarded@example.invalid"
    forwarded.set_content("Synthetic forwarded content.")
    attachment = MIMEMessage(forwarded)
    attachment["Content-Disposition"] = 'attachment; filename="forwarded.eml"'
    outer = EmailMessage()
    outer["From"] = "reviewer@example.invalid"
    outer["Date"] = "Mon, 02 Sep 2030 10:15:00 -0700"
    outer["Message-ID"] = "<message-attachment@example.invalid>"
    outer.set_content("Top-authored synthetic response.")
    outer.make_mixed()
    outer.attach(attachment)

    with pytest.raises(ValueError, match="cannot provide byte-exact evidence"):
        receipt_ingest.parse_mail_evidence(outer)


@pytest.mark.parametrize(
    ("transfer_headers", "message"),
    [
        (["x-private-transform"], "unsupported transfer encoding"),
        (["7bit", "base64"], "ambiguous transfer encoding"),
        (["base64"], "multipart MIME payload uses an unsupported transfer encoding"),
    ],
)
def test_multipart_container_encoding_fails_closed_before_recursive_parse(
    transfer_headers: list[str], message: str
) -> None:
    alternative = EmailMessage()
    alternative.set_content("Synthetic plain response.")
    alternative.add_alternative("<p>Synthetic HTML response.</p>", subtype="html")
    for index, transfer_header in enumerate(transfer_headers):
        if index == 0:
            alternative["Content-Transfer-Encoding"] = transfer_header
        else:
            alternative._headers.append(("Content-Transfer-Encoding", transfer_header))
    outer = EmailMessage()
    outer["From"] = "reviewer@example.invalid"
    outer["Date"] = "Mon, 02 Sep 2030 10:15:00 -0700"
    outer["Message-ID"] = "<bad-multipart-encoding@example.invalid>"
    outer.make_mixed()
    outer.attach(alternative)
    reparsed = email.message_from_bytes(outer.as_bytes(), policy=email.policy.default)

    with pytest.raises(ValueError, match=message):
        receipt_ingest.parse_mail_evidence(reparsed)


def test_v1_pdf_url_extraction_keeps_legacy_blank_pdf_fallback() -> None:
    msg = EmailMessage()
    msg["Subject"] = "Example Reimbursement Form got a new submission"
    msg["Date"] = "Mon, 02 Sep 2030 10:15:00 -0700"
    msg["Message-ID"] = "<v1-pdf-fallback@example.invalid>"
    msg.set_content(
        """\
Requestor First and Last Name:
Morgan Example
1. Event or Budget Category:
Supplies
1. Description:
Synthetic supplies
1. Amount:
10.00
Total Amount $:
10.00
PDF:
JPEG: https://receipts.example.invalid/synthetic.jpg
"""
    )

    submission = receipt_ingest.parse_submission(msg)

    assert submission is not None
    expected = ("https://receipts.example.invalid/synthetic.jpg",)
    assert submission.receipt_urls == expected
    assert submission.source_receipt_urls_v1 == expected


# --- reply / forward detection (thread-duplicate guard) --------------------


def test_is_reply_or_forward() -> None:
    assert receipt_ingest.is_reply_or_forward("Re: Main Reimbursement Form got a new submission")
    assert receipt_ingest.is_reply_or_forward("Fwd: Teacher Reimbursement Form")
    assert receipt_ingest.is_reply_or_forward("  fw : quoted")
    assert not receipt_ingest.is_reply_or_forward("Main Reimbursement Form got a new submission")
    assert not receipt_ingest.is_reply_or_forward("Reimbursement Form got a new submission")


# --- iter_mbox / iter_source (Takeout backfill path) -----------------------


def test_iter_mbox_reads_and_reparses_messages(tmp_path: Path) -> None:
    box = mailbox.mbox(str(tmp_path / "export.mbox"))
    box.add(bytes(_reimbursement_email()))
    box.add(bytes(_non_reimbursement_email()))
    box.flush()
    box.close()

    parsed = [
        receipt_ingest.parse_submission(msg)
        for _label, msg in receipt_ingest.iter_mbox(tmp_path / "export.mbox")
    ]
    recognized = [sub for sub in parsed if sub is not None]
    assert len(parsed) == 2
    assert len(recognized) == 1
    assert recognized[0].total == "1415.13"


def test_iter_source_mixes_eml_and_mbox(tmp_path: Path) -> None:
    (tmp_path / "one.eml").write_bytes(bytes(_reimbursement_email()))
    box = mailbox.mbox(str(tmp_path / "more.mbox"))
    box.add(bytes(_reimbursement_email()))
    box.flush()
    box.close()

    recognized = [
        sub
        for _label, msg in receipt_ingest.iter_source(tmp_path)
        if (sub := receipt_ingest.parse_submission(msg)) is not None
    ]
    assert len(recognized) == 2


# --- profile (the "meta load") ---------------------------------------------


def _email_with(subject: str, *, requestor_email: str) -> EmailMessage:
    """A reimbursement-form email with a chosen subject + requestor email (fake identity)."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "forms@wix-forms.com"
    msg["Date"] = "Sun, 28 Jun 2026 09:09:00 -0700"
    msg["Message-ID"] = f"<{requestor_email}@example.org>"
    msg.set_content(_HTML_BODY.replace("jane.doe@example.org", requestor_email), subtype="html")
    return msg


def test_profile_aggregates_pii_free() -> None:
    subs = []
    for subject, who in [
        ("Main Reimbursement Form got a new submission", "a@example.org"),
        ("Main Reimbursement Form got a new submission", "b@example.org"),
        ("Teacher Reimbursement Form got a new submission", "a@example.org"),
    ]:
        sub = receipt_ingest.parse_submission(_email_with(subject, requestor_email=who))
        assert sub is not None
        subs.append(sub)

    prof = receipt_ingest.profile(subs, start_month=7)

    assert prof.recognized == 3
    # all three fixtures carry the same Date header -> a single-day received span
    assert prof.received_span == ("2026-06-28", "2026-06-28")
    assert {name for name, _ in prof.form_types} == {
        "Main Reimbursement Form",
        "Teacher Reimbursement Form",
    }
    assert dict(prof.form_types)["Main Reimbursement Form"] == 2
    # a@ appears twice -> 2 distinct requestors, and only the COUNT is retained
    assert prof.distinct_requestors == 2
    # each fixture has 3 line items; item 3 omits the category
    assert prof.line_items == 9
    assert prof.blank_category_items == 3
    assert prof.reconcile_yes == 3
    # dated items (2 per email, 2026-06-25) fall in FY2026 under a July start
    assert dict(prof.fiscal_years) == {"FY2026": 6}
    # the Profile must carry NO requestor identity — only the distinct count
    banned = {"requestor_name", "requestor_email", "requestors", "phone"}
    assert not {f.name for f in fields(receipt_ingest.Profile)} & banned


def test_profile_counts_blank_amount_and_category() -> None:
    # Item 2 has a category but no amount; item 3 has neither category nor date.
    sub = receipt_ingest.parse_submission(_reimbursement_email())
    assert sub is not None
    blanked = receipt_ingest.Submission(
        **{
            **sub.__dict__,
            "line_items": (
                sub.line_items[0],
                receipt_ingest.LineItem(
                    index=2, date="2026-06-25", category="Garden Club", description="", amount=""
                ),
                sub.line_items[2],
            ),
        }
    )
    prof = receipt_ingest.profile([blanked], start_month=7)
    assert prof.blank_amount_items == 1
    assert prof.no_date_items == 1
    assert prof.blank_category_items == 1
