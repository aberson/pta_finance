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

from email.message import EmailMessage
from pathlib import Path

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
