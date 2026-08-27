"""Unit tests for the reimbursement -> flat-ledger mapper (:mod:`pta_finance.receipt_map`).

Submissions are built directly with OBVIOUSLY-FAKE identity (``Jane Doe`` / ``jane@example.org``)
per the repo identity rule — no real data.
"""

from __future__ import annotations

import mailbox
from email.message import EmailMessage
from pathlib import Path

import pytest

from pta_finance import receipt_ingest, receipt_map
from pta_finance.receipt_ingest import LineItem, Submission

_MAP = {"garden club": "Garden Club Expenses"}


def _sub(
    *,
    message_id: str = "<m1@example.org>",
    subject: str = "Main Reimbursement Form got a new submission",
    received: str = "Sun, 28 Jun 2026 09:09:00 -0700",
    requestor_email: str = "jane@example.org",
    line_items: tuple[LineItem, ...] = (),
    total: str = "",
    payment_type: str = "Zelle",
    receipt_urls: tuple[str, ...] = (),
) -> Submission:
    return Submission(
        message_id=message_id,
        subject=subject,
        received=received,
        requestor_name="Jane Doe",
        requestor_email=requestor_email,
        phone="",
        company="",
        line_items=line_items,
        total=total,
        payment_type=payment_type,
        receipt_urls=receipt_urls,
        attachments=(),
        notes="",
    )


def test_map_basic_carry_forward_and_canonical() -> None:
    sub = _sub(
        line_items=(
            LineItem(1, "2026-06-25", "Garden Club", "boxes", "718.60"),
            LineItem(2, "", "", "generator", "279.40"),  # blank date+category -> carry-forward
        ),
        total="998.00",
    )
    rows = receipt_map.map_submissions([sub], category_map=_MAP, start_month=7)

    assert len(rows) == 2
    assert rows[0]["canonical_category"] == "Garden Club Expenses"
    assert rows[0]["fiscal_year"] == "FY2026"
    assert rows[0]["month"] == "2026-06"
    # row 2 inherited category + date from row 1
    assert rows[1]["raw_category"] == "Garden Club"
    assert rows[1]["date"] == "2026-06-25"
    assert rows[1]["canonical_category"] == "Garden Club Expenses"
    # 718.60 + 279.40 == 998.00 -> reconciles, nothing flagged
    assert all(row["reconciles"] == "yes" for row in rows)
    assert all(row["needs_review"] == "" for row in rows)


def test_map_skips_blank_amount_lines() -> None:
    sub = _sub(
        line_items=(
            LineItem(1, "2026-05-01", "Garden Club", "food", "570.00"),
            LineItem(2, "2026-05-01", "Garden Club", "", ""),  # blank amount -> dropped
        ),
        total="570.00",
    )
    rows = receipt_map.map_submissions([sub], category_map=_MAP, start_month=7)
    assert len(rows) == 1


def test_map_unmapped_category_flags_needs_review() -> None:
    sub = _sub(line_items=(LineItem(1, "2026-05-01", "Mystery", "x", "10.00"),), total="10.00")
    rows = receipt_map.map_submissions([sub], category_map={}, start_month=7)
    assert rows[0]["canonical_category"] == ""
    assert "unmapped-category" in rows[0]["needs_review"]


def test_map_total_mismatch_flags_needs_review() -> None:
    sub = _sub(line_items=(LineItem(1, "2026-05-01", "Garden Club", "x", "10.00"),), total="99.99")
    rows = receipt_map.map_submissions([sub], category_map=_MAP, start_month=7)
    assert "total-mismatch" in rows[0]["needs_review"]


def test_map_nonfinite_amounts_stay_text_and_are_flagged() -> None:
    spellings = (
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
    )
    for index, amount in enumerate(spellings, start=1):
        sub = _sub(
            message_id=f"<nonfinite-{index}@example.org>",
            requestor_email=f"nonfinite-{index}@example.org",
            line_items=(LineItem(1, "2026-05-01", "Garden Club", "x", amount),),
            total=amount,
        )

        (row,) = receipt_map.map_submissions([sub], category_map=_MAP, start_month=7)

        assert row["amount"] == amount
        assert "bad-amount" in row["needs_review"]
        assert row["reconciles"] == "n/a"


def test_map_opposite_infinities_returns_reviewable_rows_instead_of_raising() -> None:
    sub = _sub(
        line_items=(
            LineItem(1, "2030-09-01", "Garden Club", "first", "Infinity"),
            LineItem(2, "2030-09-01", "Garden Club", "second", "-Infinity"),
        ),
        total="0.00",
    )

    rows = receipt_map.map_submissions([sub], category_map=_MAP, start_month=7)

    assert [row["amount"] for row in rows] == ["Infinity", "-Infinity"]
    assert all(row["reconciles"] == "n/a" for row in rows)
    assert all("bad-amount" in row["needs_review"] for row in rows)


@pytest.mark.parametrize("amount", ["1e100000", "1e-100000", "-1e100000", "-1e-100000"])
def test_map_extreme_exponents_stay_compact_and_reviewable(amount: str) -> None:
    sub = _sub(
        line_items=(LineItem(1, "2030-09-01", "Garden Club", "Example item", amount),),
        total=amount,
    )

    rows = receipt_map.map_submissions([sub], category_map=_MAP, start_month=7)

    assert len(rows) == 1
    assert rows[0]["amount"] == amount
    assert "bad-amount" in rows[0]["needs_review"]
    assert rows[0]["reconciles"] == "n/a"


def test_map_dedup_by_message_id() -> None:
    s1 = _sub(
        message_id="<dup@x>",
        line_items=(LineItem(1, "2026-05-01", "Garden Club", "x", "10.00"),),
        total="10.00",
    )
    s2 = _sub(
        message_id="<dup@x>",
        requestor_email="other@example.org",
        line_items=(LineItem(1, "2026-05-02", "Garden Club", "y", "20.00"),),
        total="20.00",
    )
    rows = receipt_map.map_submissions([s1, s2], category_map=_MAP, start_month=7)
    assert len(rows) == 1  # second dropped: repeat Message-ID


def test_map_dedup_by_content_hash() -> None:
    # Different Message-ID, same requestor + total + first date -> accidental resubmit.
    s1 = _sub(
        message_id="<a@x>",
        requestor_email="p@example.org",
        line_items=(LineItem(1, "2026-05-01", "Garden Club", "x", "10.00"),),
        total="10.00",
    )
    s2 = _sub(
        message_id="<b@x>",
        requestor_email="p@example.org",
        line_items=(LineItem(1, "2026-05-01", "Garden Club", "x", "10.00"),),
        total="10.00",
    )
    rows = receipt_map.map_submissions([s1, s2], category_map=_MAP, start_month=7)
    assert len(rows) == 1


def test_map_fiscal_year_falls_back_to_received_date() -> None:
    sub = _sub(
        received="Thu, 23 Apr 2026 15:26:43 +0000",
        line_items=(LineItem(1, "", "Garden Club", "x", "10.00"),),  # no line date
        total="10.00",
    )
    rows = receipt_map.map_submissions([sub], category_map=_MAP, start_month=7)
    # no line date -> submission FY from the received header (Apr 2026 -> FY2026 under July start)
    assert rows[0]["fiscal_year"] == "FY2026"
    assert rows[0]["date"] == ""


def test_map_received_fallback_preserves_header_local_calendar_date() -> None:
    sub = _sub(
        received="Mon, 01 Jul 2030 00:30:00 +1400",
        line_items=(LineItem(1, "", "Garden Club", "x", "10.00"),),
        total="10.00",
    )

    rows = receipt_map.map_submissions([sub], category_map=_MAP, start_month=7)

    # UTC conversion would move this to June 30 / FY2030. The header-local July 1 date is FY2031.
    assert rows[0]["fiscal_year"] == "FY2031"


def test_map_form_default_fills_blank_category() -> None:
    sub = _sub(
        subject="Teacher Reimbursement Form got a new submission",
        line_items=(LineItem(1, "2026-05-01", "", "supplies", "50.00"),),  # no category
        total="50.00",
    )
    rows = receipt_map.map_submissions(
        [sub],
        category_map={},
        form_defaults={
            "Teacher Reimbursement Form": "Classroom Enhancements TK to 5th - Teacher Budget"
        },
        start_month=7,
    )
    assert rows[0]["canonical_category"] == "Classroom Enhancements TK to 5th - Teacher Budget"
    assert rows[0]["needs_review"] == ""  # default resolved it — not flagged


def test_map_form_default_only_applies_to_blank_category() -> None:
    sub = _sub(
        subject="Teacher Reimbursement Form got a new submission",
        line_items=(LineItem(1, "2026-05-01", "Weird Thing", "x", "50.00"),),  # non-blank, unmapped
        total="50.00",
    )
    rows = receipt_map.map_submissions(
        [sub],
        category_map={},
        form_defaults={
            "Teacher Reimbursement Form": "Classroom Enhancements TK to 5th - Teacher Budget"
        },
        start_month=7,
    )
    # A non-blank but unmapped category is NOT silently defaulted -> it stays flagged for review.
    assert rows[0]["canonical_category"] == ""
    assert "unmapped-category" in rows[0]["needs_review"]


def test_load_category_map_skips_blank_sentinel_and_form_default(tmp_path: Path) -> None:
    path = tmp_path / "map.csv"
    path.write_text(
        "raw_category,canonical_category,confidence,notes\n"
        "Garden Club,Garden Club Expenses,high,\n"
        "Science,,new-line,tbd\n"
        "(blank),,n/a,carry-forward\n"
        "FORM_DEFAULT: Teacher Reimbursement Form,"
        "Classroom Enhancements TK to 5th - Teacher Budget,default,\n",
        encoding="utf-8",
    )
    assert receipt_map.load_category_map(path) == {"garden club": "Garden Club Expenses"}
    assert receipt_map.load_form_defaults(path) == {
        "Teacher Reimbursement Form": "Classroom Enhancements TK to 5th - Teacher Budget"
    }


# --- Cross-source dedup (the guard for the fetch connector's Design Decision 10) --------

# A minimal Wix-style form body, same STRUCTURE as the real notification email (label element,
# then a bold value element) with obviously-fake identity only. Deliberately built here rather
# than imported from tests/test_receipt_ingest.py: this test is about the mapper's dedup, and it
# must keep working if that module's fixture changes shape.
_FORM_HTML = """\
<html><body>
<h1>Main Reimbursement Form got a new submission</h1>
<p>Requestor First and Last Name:</p><p><strong>Jane Doe</strong></p>
<p>Email:</p><p><strong>jane.doe@example.org</strong></p>
<p>1. Date:</p><p><strong>2026-06-25</strong></p>
<p>1. Event or Budget Category:</p><p><strong>Garden Club</strong></p>
<p>1. Description:</p><p><strong>Boxes for the shed</strong></p>
<p>1.Amount:</p><p><strong>718.60</strong></p>
<p>Total Amount $:</p><p><strong>718.60</strong></p>
<p>Choose Payment Type:</p><p><strong>Zelle</strong></p>
</body></html>
"""


def _form_email_bytes() -> bytes:
    """The raw RFC-822 bytes of one reimbursement-form submission (fake identity)."""
    msg = EmailMessage()
    msg["Subject"] = "Main Reimbursement Form got a new submission"
    msg["From"] = "forms@example.com"
    msg["To"] = "treasurer@example.org"
    msg["Date"] = "Sun, 28 Jun 2026 09:09:00 -0700"
    msg["Message-ID"] = "<cross-source-1@example.org>"
    msg.set_content(_FORM_HTML, subtype="html")
    return bytes(msg)


def _parsed_from(source: Path) -> list[Submission]:
    """Every recognized submission under ``source`` — the exact loop ``map-receipts`` runs."""
    return [
        sub
        for _label, msg in receipt_ingest.iter_source(source)
        if (sub := receipt_ingest.parse_submission(msg)) is not None
    ]


def test_same_message_in_eml_and_mbox_maps_to_one_row(tmp_path: Path) -> None:
    """One message present as BOTH a fetched ``.eml`` and an archived ``.mbox`` member.

    This is the regression guard for the Gmail connector's Design Decision 10. ``fetch-mail``
    writes its ``.eml`` files directly into the directory that already holds the ``.mbox``
    archives — never a subdirectory — precisely so that ONE ``map-receipts --source <dir>`` run
    covers both. The eleven-week overlap between the archive and the fetch window means the same
    submission genuinely appears in both sources, and only a single ``map_submissions`` call can
    collapse it: the dedup sets live inside that call.
    """
    raw = _form_email_bytes()
    (tmp_path / "fetched.eml").write_bytes(raw)
    box = mailbox.mbox(str(tmp_path / "archive.mbox"))
    box.add(raw)
    box.flush()
    box.close()

    subs = _parsed_from(tmp_path)
    # The message really is read twice — otherwise the assertion below would prove nothing.
    assert len(subs) == 2
    assert {sub.message_id for sub in subs} == {"<cross-source-1@example.org>"}

    rows = receipt_map.map_submissions(subs, category_map=_MAP, start_month=7)
    assert len(rows) == 1
    assert rows[0]["canonical_category"] == "Garden Club Expenses"


def test_mapping_the_two_sources_in_separate_runs_double_counts(tmp_path: Path) -> None:
    """The failure Design Decision 10 exists to prevent — pinned so it cannot be forgotten.

    ``map_submissions`` accumulates ``seen_ids``/``seen_hashes`` WITHIN A SINGLE CALL. Two runs
    therefore each look internally clean while together counting the shared message twice. If a
    future change ever made ``iter_source`` recursive (so fetched mail could sit in a
    subdirectory), this is the silent double-count that would follow.
    """
    raw = _form_email_bytes()
    eml_dir = tmp_path / "inbox"
    eml_dir.mkdir()
    (eml_dir / "fetched.eml").write_bytes(raw)
    box = mailbox.mbox(str(tmp_path / "archive.mbox"))
    box.add(raw)
    box.flush()
    box.close()

    from_eml = receipt_map.map_submissions(_parsed_from(eml_dir), category_map=_MAP, start_month=7)
    from_mbox = receipt_map.map_submissions(
        _parsed_from(tmp_path / "archive.mbox"), category_map=_MAP, start_month=7
    )
    assert len(from_eml) == 1
    assert len(from_mbox) == 1
    # Each run is clean on its own; the duplicate only exists across them.
    assert len(from_eml + from_mbox) == 2


def test_iter_source_does_not_see_a_subdirectory(tmp_path: Path) -> None:
    """The mechanical fact Design Decision 10 rests on: the glob is NON-recursive.

    If this ever starts failing, ``iter_source`` became recursive and every existing caller's
    behaviour changed silently — including the two-run double-count above.
    """
    nested = tmp_path / "inbox"
    nested.mkdir()
    (nested / "fetched.eml").write_bytes(_form_email_bytes())
    assert _parsed_from(tmp_path) == []
