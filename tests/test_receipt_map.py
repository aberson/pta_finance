"""Unit tests for the reimbursement -> flat-ledger mapper (:mod:`pta_finance.receipt_map`).

Submissions are built directly with OBVIOUSLY-FAKE identity (``Jane Doe`` / ``jane@example.org``)
per the repo identity rule — no real data.
"""

from __future__ import annotations

from pathlib import Path

from pta_finance import receipt_map
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
