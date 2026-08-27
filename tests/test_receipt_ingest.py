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

import mailbox
from dataclasses import fields
from datetime import date
from email.message import EmailMessage
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
