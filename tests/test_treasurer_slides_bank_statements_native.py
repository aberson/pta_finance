"""Fictional native-text PDF coverage for the strict Wells Fargo v1 adapter."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import date
from decimal import Decimal
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest

from pta_finance.treasurer_slides import bank_statements
from pta_finance.treasurer_slides.bank_statements import (
    SlidesDependencyError,
    StatementExtractionError,
    WellsFargoStatementExtractor,
)
from pta_finance.treasurer_slides.models import (
    AccountRole,
    ActivityColumn,
    BalanceBoundary,
    BalanceKind,
    BoundingBox,
    Direction,
    DocumentKind,
    DocumentSpec,
    EvidenceField,
    PageKind,
    PrivateInputError,
    TransactionStatus,
    build_source_row_id,
)


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _fictional_pdf_bytes(
    pages: list[list[tuple[int, int, str]]], *, media_box: tuple[int, int] = (612, 792)
) -> bytes:
    """Create a tiny letter-sized embedded-text PDF without a PDF-writing dependency."""

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    page_object_ids = tuple(4 + index * 2 for index in range(len(pages)))
    width, height = media_box
    objects[2] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{number} 0 R' for number in page_object_ids)}] "
        f"/Count {len(page_object_ids)} >>"
    ).encode("ascii")
    for index, page_lines in enumerate(pages):
        page_object = page_object_ids[index]
        content_object = page_object + 1
        stream = "\n".join(
            f"BT /F1 10 Tf 1 0 0 1 {left} {bottom} Tm ({_escape_pdf_text(text)}) Tj ET"
            for left, bottom, text in page_lines
        ).encode("cp1252")
        objects[page_object] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            "/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {content_object} 0 R >>"
        ).encode("ascii")
        objects[content_object] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number in range(1, max(objects) + 1):
        offsets.append(len(body))
        body.extend(f"{object_number} 0 obj\n".encode("ascii"))
        body.extend(objects[object_number])
        body.extend(b"\nendobj\n")
    xref_offset = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    trailer = f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
    body.extend(f"{trailer}startxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
    return bytes(body)


def _write_pdf(
    tmp_path: Path,
    pages: list[list[tuple[int, int, str]]],
    *,
    name: str = "fictional-native.pdf",
    media_box: tuple[int, int] = (612, 792),
) -> Path:
    path = tmp_path / name
    path.write_bytes(_fictional_pdf_bytes(pages, media_box=media_box))
    return path


def _monthly_pages(*, two_amounts: bool = False) -> list[list[tuple[int, int, str]]]:
    second_row = [
        (72, 440, "01/29/2026"),
        (150, 440, "Fictional Supplies"),
        (390, 440, "50.00"),
    ]
    if two_amounts:
        second_row.append((490, 440, "50.00"))
    return [
        [
            (72, 720, "Wells Fargo"),
            (72, 690, "Monthly Account Statement"),
            (72, 660, "Statement Period 01/01/2026 01/31/2026"),
            (72, 630, "Account Number 1234567890"),
            (72, 570, "Opening Balance Start of Day Excludes Pending 01/01/2026 1,000.00"),
            (72, 530, "Closing Balance End of Day Excludes Pending 01/31/2026 1,250.00"),
            (72, 480, "This entirely fictional statement tests positioned native text only."),
        ],
        [
            (72, 720, "Wells Fargo"),
            (72, 690, "Monthly Account Activity"),
            (72, 660, "Statement Period 01/01/2026 01/31/2026"),
            (72, 610, "Date"),
            (150, 610, "Description"),
            (390, 610, "Debits"),
            (490, 610, "Credits"),
            (72, 570, "Posted"),
            (72, 540, "01/03/2026"),
            (150, 540, "Fictional Contribution"),
            (490, 540, "250.00"),
            (72, 480, "Pending"),
            *second_row,
            (72, 380, "End of Activity"),
            (72, 350, "Fictional footer text gives this page a realistic public-safe shape."),
        ],
        [
            (72, 720, "Wells Fargo"),
            (72, 680, "Important Account Information"),
            (72, 640, "Member FDIC"),
            (72, 600, "Equal Housing Lender"),
            (72, 560, "This fictional boilerplate intentionally supplies no financial fact."),
            (72, 520, "It verifies safe ignored-page recognition in the native parser."),
        ],
    ]


def _monthly_document() -> DocumentSpec:
    return DocumentSpec(
        account_role=AccountRole.CHECKING,
        document_kind=DocumentKind.MONTHLY_STATEMENT,
        relative_path="fictional-native.pdf",
    )


def test_monthly_native_pdf_yields_positioned_balances_and_rows(tmp_path: Path) -> None:
    source = _write_pdf(tmp_path, _monthly_pages())
    observation = WellsFargoStatementExtractor().extract(
        source,
        document_ordinal=1,
        document=_monthly_document(),
    )

    assert [page.page_kind for page in observation.page_evidence] == [
        PageKind.MONTHLY_SUMMARY,
        PageKind.MONTHLY_ACTIVITY,
        PageKind.BOILERPLATE,
    ]
    assert [
        (balance.kind, balance.boundary, balance.observed_on, balance.includes_pending)
        for balance in observation.balances
    ] == [
        (BalanceKind.OPENING, BalanceBoundary.START_OF_DAY, date(2026, 1, 1), False),
        (BalanceKind.CLOSING, BalanceBoundary.END_OF_DAY, date(2026, 1, 31), False),
    ]
    assert [(row.direction, row.status) for row in observation.transactions] == [
        (Direction.CREDIT, TransactionStatus.POSTED),
        (Direction.DEBIT, TransactionStatus.PENDING),
    ]
    table = observation.page_evidence[1].activity_tables[0]
    debit = table.band(ActivityColumn.DEBIT)
    credit = table.band(ActivityColumn.CREDIT)
    assert debit.right < credit.left
    assert all(
        "1234567890" not in token.text
        for page in observation.page_evidence
        for token in page.tokens
    )
    assert observation.source_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert observation.parser_version == bank_statements.NATIVE_PARSER_VERSION
    opening = observation.balances[0]
    assert (
        opening.locator.document_ordinal,
        opening.locator.page_number,
        opening.locator.table_ordinal,
        opening.locator.row_ordinal,
    ) == (1, 1, 1, 1)
    assert [evidence.field for evidence in opening.parse_evidence] == [
        EvidenceField.DATE,
        EvidenceField.BALANCE,
        EvidenceField.KIND,
        EvidenceField.BOUNDARY,
        EvidenceField.INCLUDES_PENDING,
    ]
    first_transaction = observation.transactions[0]
    assert (
        first_transaction.locator.document_ordinal,
        first_transaction.locator.page_number,
        first_transaction.locator.table_ordinal,
        first_transaction.locator.row_ordinal,
    ) == (1, 2, 1, 1)
    assert first_transaction.source_row_id == build_source_row_id(
        bank_statements.NATIVE_PARSER_VERSION,
        observation.source_sha256,
        first_transaction.locator,
    )
    assert [evidence.field for evidence in first_transaction.parse_evidence] == [
        EvidenceField.DATE,
        EvidenceField.DESCRIPTION,
        EvidenceField.DIRECTION,
        EvidenceField.STATUS,
        EvidenceField.MAGNITUDE,
    ]
    assert (
        first_transaction.parse_evidence[2].context_token_ordinals
        == table.band(ActivityColumn.CREDIT).header_token_ordinals
    )


def test_corrupt_pdf_fails_closed_without_reflecting_private_source_text(tmp_path: Path) -> None:
    source = tmp_path / "synthetic-corrupt.pdf"
    canary = "SYNTHETIC_PRIVATE_LEAK_CANARY"
    source.write_bytes(f"not a PDF: {canary}".encode("ascii"))

    with pytest.raises(StatementExtractionError) as failure:
        WellsFargoStatementExtractor().extract(
            source,
            document_ordinal=1,
            document=_monthly_document(),
        )

    message = str(failure.value)
    assert message == "Treasurer Slides statement extraction failed for document 1"
    assert canary not in message
    assert source.name not in message
    assert str(source) not in message


def test_pdf_backend_page_count_failure_is_normalized_without_canary() -> None:
    canary = "SYNTHETIC_BACKEND_CANARY"

    class LengthFailure:
        def __len__(self) -> int:
            raise RuntimeError(canary)

    backend = SimpleNamespace(PdfDocument=lambda payload: LengthFailure())

    with pytest.raises(StatementExtractionError) as failure:
        bank_statements._extract_native_pages(backend, b"synthetic", document_ordinal=1)

    assert str(failure.value) == "Treasurer Slides statement extraction failed for document 1"
    assert canary not in str(failure.value)


@pytest.mark.parametrize(
    "description",
    (
        "Opening Day Supplies",
        "Beginning Day Supplies",
        "Closing Day Supplies",
        "Ending Day Supplies",
        "Collected Day Supplies",
        "Available Day Supplies",
    ),
)
def test_activity_descriptions_with_balance_kind_words_are_not_balance_rows(
    tmp_path: Path, description: str
) -> None:
    pages = _monthly_pages()
    pages[1][13] = (150, 440, description)

    observation = WellsFargoStatementExtractor().extract(
        _write_pdf(tmp_path, pages, name="fictional-activity-balance-kind-description.pdf"),
        document_ordinal=1,
        document=_monthly_document(),
    )

    assert observation.transactions[1].normalized_description == description.casefold()
    assert observation.transactions[1].status is TransactionStatus.PENDING


def test_balance_kind_text_remains_strict_outside_a_transaction_table(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[0][4] = (72, 570, "Opening Day Supplies 01/01/2026 1,000.00")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, pages, name="fictional-malformed-summary-balance.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_balance_rows_require_a_literal_balance_marker(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[0][4] = (
        72,
        570,
        "Opening Start of Day Excludes Pending 01/01/2026 1,000.00",
    )

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, pages, name="fictional-missing-balance-marker.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_balance_rows_reject_unknown_qualifiers(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[0][4] = (
        72,
        570,
        "Projected Opening Balance Start of Day Excludes Pending 01/01/2026 1,000.00",
    )

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, pages, name="fictional-unsupported-balance-qualifier.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_balance_rows_must_match_their_page_fingerprint_contract(tmp_path: Path) -> None:
    monthly = _monthly_pages()
    monthly[0].insert(
        6,
        (72, 500, "Available Balance Capture Includes Pending 01/31/2026 1,100.00"),
    )
    current = _write_pdf(
        tmp_path,
        [
            [
                (72, 720, "Wells Fargo"),
                (72, 690, "Current Account Balance"),
                (72, 640, "Available Balance Capture Includes Pending 02/10/2026 1,450.00"),
                (72, 590, "Opening Balance Start of Day Excludes Pending 02/10/2026 1,200.00"),
                (
                    72,
                    540,
                    "Fictional current page with a monthly balance grammar must fail closed.",
                ),
            ]
        ],
        name="fictional-current-with-monthly-balance.pdf",
    )
    current_document = DocumentSpec(
        account_role=AccountRole.SAVINGS,
        document_kind=DocumentKind.CURRENT_ACTIVITY,
        relative_path="fictional-current-with-monthly-balance.pdf",
    )

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, monthly, name="fictional-monthly-with-current-balance.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )
    with pytest.raises(StatementExtractionError, match=r"document 2 page 1"):
        WellsFargoStatementExtractor().extract(
            current,
            document_ordinal=2,
            document=current_document,
        )


def test_activity_headers_reject_unknown_columns(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[1].insert(6, (275, 610, "Memo"))
    pages[1].insert(11, (275, 540, "ZX"))

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, pages, name="fictional-unknown-activity-column.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_activity_description_status_words_do_not_become_status_controls(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[1][9] = (150, 540, "Pending Supplies")
    pages[1][13] = (150, 440, "Supplies Posted Today")

    observation = WellsFargoStatementExtractor().extract(
        _write_pdf(tmp_path, pages, name="fictional-status-word-descriptions.pdf"),
        document_ordinal=1,
        document=_monthly_document(),
    )

    assert [transaction.normalized_description for transaction in observation.transactions] == [
        "pending supplies",
        "supplies posted today",
    ]
    assert [transaction.status for transaction in observation.transactions] == [
        TransactionStatus.POSTED,
        TransactionStatus.PENDING,
    ]
    assert [
        control.status
        for control in observation.page_evidence[1].activity_tables[0].status_controls
    ] == [TransactionStatus.POSTED, TransactionStatus.PENDING]


def test_canonical_dollar_balance_amounts_remain_valid(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[0][4] = (
        72,
        570,
        "Opening Balance Start of Day Excludes Pending 01/01/2026 $1,000.00",
    )
    pages[0][5] = (
        72,
        530,
        "Closing Balance End of Day Excludes Pending 01/31/2026 $1,250.00",
    )

    observation = WellsFargoStatementExtractor().extract(
        _write_pdf(tmp_path, pages, name="canonical-dollar-balances.pdf"),
        document_ordinal=1,
        document=_monthly_document(),
    )

    assert [balance.amount for balance in observation.balances] == [
        Decimal("1000.00"),
        Decimal("1250.00"),
    ]


def test_wrong_media_box_dimensions_fail_closed(tmp_path: Path) -> None:
    source = _write_pdf(
        tmp_path,
        _monthly_pages(),
        name="wrong-media-box.pdf",
        media_box=(600, 792),
    )

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        WellsFargoStatementExtractor().extract(
            source,
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_activity_direction_uses_geometry_not_content_stream_order(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[1] = list(reversed(pages[1]))

    observation = WellsFargoStatementExtractor().extract(
        _write_pdf(tmp_path, pages, name="scrambled-stream-order.pdf"),
        document_ordinal=1,
        document=_monthly_document(),
    )

    assert [(row.direction, row.magnitude) for row in observation.transactions] == [
        (Direction.CREDIT, Decimal("250.00")),
        (Direction.DEBIT, Decimal("50.00")),
    ]


def test_current_pages_retain_capture_date_and_pending_available_basis(tmp_path: Path) -> None:
    current_pdf = _write_pdf(
        tmp_path,
        [
            [
                (72, 720, "Wells Fargo"),
                (72, 690, "Current Account Balance"),
                (72, 660, "As of 02/10/2026"),
                (72, 610, "Available Balance Capture Includes Pending 02/10/2026 1,450.00"),
                (72, 560, "Entirely fictional native source text for testing only."),
            ],
            [
                (72, 720, "Wells Fargo"),
                (72, 690, "Current Activity"),
                (72, 610, "Date"),
                (150, 610, "Details"),
                (390, 610, "Withdrawals"),
                (490, 610, "Deposits"),
                (72, 570, "Posted"),
                (72, 540, "02/09/2026"),
                (150, 540, "Fictional Deposit"),
                (490, 540, "200.00"),
                (72, 480, "End of Activity"),
                (72, 440, "Fictional footer for the fixed current-activity contract."),
            ],
            [
                (72, 720, "Wells Fargo"),
                (72, 680, "Important Account Information"),
                (72, 640, "Member FDIC"),
                (72, 600, "Equal Housing Lender"),
                (72, 550, "Fictional boilerplate stays ignored even in a current document."),
                (72, 510, "It contains no account ID, balance, or transaction source fact."),
            ],
        ],
    )
    current_document = DocumentSpec(
        account_role=AccountRole.SAVINGS,
        document_kind=DocumentKind.CURRENT_ACTIVITY,
        relative_path="fictional-current.pdf",
    )

    observation = WellsFargoStatementExtractor().extract(
        current_pdf,
        document_ordinal=2,
        document=current_document,
    )

    assert observation.capture_date == date(2026, 2, 10)
    assert observation.coverage_start == date(2026, 2, 9)
    assert observation.balances[0].kind is BalanceKind.AVAILABLE
    assert observation.balances[0].boundary is BalanceBoundary.CAPTURE
    assert observation.balances[0].includes_pending is True
    assert observation.transactions[0].status is TransactionStatus.POSTED
    assert observation.page_evidence[-1].page_kind is PageKind.BOILERPLATE


def test_current_balance_rows_keep_distinct_capture_dates(tmp_path: Path) -> None:
    current_pdf = _write_pdf(
        tmp_path,
        [
            [
                (72, 720, "Wells Fargo"),
                (72, 690, "Current Account Balance"),
                (72, 640, "Available Balance As of Includes Pending 02/10/2026 1,450.00"),
                (72, 600, "Collected Balance As of Excludes Pending 02/08/2026 1,200.00"),
                (72, 550, "Entirely fictional native source text for separate balance dates."),
            ]
        ],
        name="fictional-distinct-captures.pdf",
    )
    current_document = DocumentSpec(
        account_role=AccountRole.SAVINGS,
        document_kind=DocumentKind.CURRENT_ACTIVITY,
        relative_path="fictional-distinct-captures.pdf",
    )

    observation = WellsFargoStatementExtractor().extract(
        current_pdf,
        document_ordinal=2,
        document=current_document,
    )

    assert observation.capture_date == date(2026, 2, 10)
    assert observation.coverage_start == date(2026, 2, 8)
    assert observation.coverage_end == date(2026, 2, 10)
    balance_facts = [
        (balance.kind, balance.boundary, balance.observed_on, balance.includes_pending)
        for balance in observation.balances
    ]
    assert balance_facts == [
        (BalanceKind.AVAILABLE, BalanceBoundary.CAPTURE, date(2026, 2, 10), True),
        (BalanceKind.COLLECTED, BalanceBoundary.CAPTURE, date(2026, 2, 8), False),
    ]


def test_current_capture_requires_accepted_balance_capture_control(tmp_path: Path) -> None:
    current_pdf = _write_pdf(
        tmp_path,
        [
            [
                (72, 720, "Wells Fargo"),
                (72, 690, "Current Account Balance"),
                (72, 610, "Available Balance Capture Includes Pending 02/10/2026 1,450.00"),
                (72, 560, "Entirely fictional native source text for capture-control testing."),
            ],
            [
                (72, 720, "Wells Fargo"),
                (72, 690, "Current Activity"),
                (72, 610, "Date"),
                (150, 610, "Details"),
                (390, 610, "Withdrawals"),
                (490, 610, "Deposits"),
                (72, 570, "Posted"),
                (72, 540, "02/09/2026"),
                (150, 540, "Fictional Detail"),
                (490, 540, "200.00"),
                (72, 480, "End of Activity"),
                (72, 440, "Fictional footer for a capture-control-only current report."),
            ],
        ],
        name="current-capture-control.pdf",
    )
    current_document = DocumentSpec(
        account_role=AccountRole.SAVINGS,
        document_kind=DocumentKind.CURRENT_ACTIVITY,
        relative_path="current-capture-control.pdf",
    )

    observation = WellsFargoStatementExtractor().extract(
        current_pdf,
        document_ordinal=2,
        document=current_document,
    )

    assert observation.capture_date == date(2026, 2, 10)
    assert observation.coverage_end == date(2026, 2, 10)
    assert observation.balances[0].boundary is BalanceBoundary.CAPTURE


def test_current_activity_as_of_text_cannot_advance_capture_date(tmp_path: Path) -> None:
    current_pdf = _write_pdf(
        tmp_path,
        [
            [
                (72, 720, "Wells Fargo"),
                (72, 690, "Current Account Balance"),
                (72, 660, "As of 02/10/2026"),
                (72, 610, "Available Balance Capture Includes Pending 02/10/2026 1,450.00"),
                (72, 560, "Entirely fictional native source text for capture-date testing."),
            ],
            [
                (72, 720, "Wells Fargo"),
                (72, 690, "Current Activity"),
                (72, 610, "Date"),
                (150, 610, "Details"),
                (390, 610, "Withdrawals"),
                (490, 610, "Deposits"),
                (72, 570, "Posted"),
                (72, 540, "02/11/2026"),
                (150, 540, "Fictional As of Detail"),
                (490, 540, "200.00"),
                (72, 480, "End of Activity"),
                (72, 440, "Fictional footer cannot redefine the balance capture date."),
            ],
        ],
        name="activity-as-of-is-not-capture.pdf",
    )
    current_document = DocumentSpec(
        account_role=AccountRole.SAVINGS,
        document_kind=DocumentKind.CURRENT_ACTIVITY,
        relative_path="activity-as-of-is-not-capture.pdf",
    )

    with pytest.raises(StatementExtractionError, match=r"document 2 page 2"):
        WellsFargoStatementExtractor().extract(
            current_pdf,
            document_ordinal=2,
            document=current_document,
        )


def test_current_standalone_as_of_metadata_cannot_supply_capture_date(tmp_path: Path) -> None:
    current_pdf = _write_pdf(
        tmp_path,
        [
            [
                (72, 720, "Wells Fargo"),
                (72, 690, "Current Account Balance"),
                (72, 660, "As of 02/10/2026"),
                (72, 610, "Available Balance Start of Day Includes Pending 02/10/2026 1,450.00"),
                (72, 560, "Fictional standalone metadata is not a capture control."),
            ]
        ],
        name="standalone-as-of-is-not-capture.pdf",
    )
    current_document = DocumentSpec(
        account_role=AccountRole.SAVINGS,
        document_kind=DocumentKind.CURRENT_ACTIVITY,
        relative_path="standalone-as-of-is-not-capture.pdf",
    )

    with pytest.raises(StatementExtractionError, match=r"document 2"):
        WellsFargoStatementExtractor().extract(
            current_pdf,
            document_ordinal=2,
            document=current_document,
        )


def test_unknown_or_ambiguous_activity_fails_closed(tmp_path: Path) -> None:
    source = _write_pdf(tmp_path, _monthly_pages(two_amounts=True))

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        WellsFargoStatementExtractor().extract(
            source,
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_unrecognized_financial_lines_outside_recognized_rows_fail_closed(tmp_path: Path) -> None:
    activity_preamble = _monthly_pages()
    activity_preamble[1].extend(
        [
            (72, 630, "Unrecognized 01/02/2026"),
            (390, 630, "10.00"),
        ]
    )
    balance_page = _monthly_pages()
    balance_page[0].append((72, 500, "Unrecognized 01/15/2026 10.00"))

    for name, pages, page_number in (
        ("financial-activity-preamble.pdf", activity_preamble, 2),
        ("financial-balance-page.pdf", balance_page, 1),
    ):
        with pytest.raises(StatementExtractionError, match=rf"document 1 page {page_number}"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name=name),
                document_ordinal=1,
                document=_monthly_document(),
            )


def test_account_identifier_continuations_are_discarded_and_zero_balances_are_valid(
    tmp_path: Path,
) -> None:
    pages = _monthly_pages()
    pages[0][3] = (72, 630, "Account")
    pages[0].insert(4, (72, 610, "Number"))
    pages[0].insert(5, (72, 590, "123 456"))
    pages[0].insert(6, (72, 580, "7890 2468"))
    pages[0] = [
        (left, bottom, text.replace("1,000.00", "0.00").replace("1,250.00", "0.00"))
        for left, bottom, text in pages[0]
    ]

    observation = WellsFargoStatementExtractor().extract(
        _write_pdf(tmp_path, pages, name="zero-and-split-account-id.pdf"),
        document_ordinal=1,
        document=_monthly_document(),
    )

    assert [balance.amount for balance in observation.balances] == [Decimal("0.00")] * 2
    token_texts = {token.text for page in observation.page_evidence for token in page.tokens}
    assert {"123", "456", "7890", "2468"}.isdisjoint(token_texts)


def test_account_label_lines_and_identifier_continuations_are_discarded(tmp_path: Path) -> None:
    for name, label, identifier in (
        ("compact-account-hash.pdf", "Account #1234567890", "1234567890"),
        ("attached-account-number.pdf", "Account no.1234567890", "1234567890"),
        ("compact-acct-hash.pdf", "Acct #1234567890", "1234567890"),
        ("period-acct-number.pdf", "Acct. No. 1234567890", "1234567890"),
        ("abbreviated-ac-no.pdf", "A/C No. 1234", "1234"),
        ("spaced-abbreviated-ac-number.pdf", "A / C Number 1234", "1234"),
        ("bare-account-identifier.pdf", "Account 3456789012", "3456789012"),
        ("bare-acct-identifier.pdf", "Acct. 4567890123", "4567890123"),
        ("short-account-number.pdf", "Account Number 1234", "1234"),
        ("short-account-num.pdf", "Account Num 1234", "1234"),
        ("short-account-num-period.pdf", "Account Num. 1234", "1234"),
        ("short-account-no.pdf", "Account No. 1234", "1234"),
        ("masked-account-number.pdf", "Account Number ••••1234", "1234"),
        ("short-account-id.pdf", "Account ID: 1234", "1234"),
        ("short-acct-num.pdf", "Acct Num: 1234", "1234"),
        ("masked-account-identifier.pdf", "Acct. Identifier: ****1234", "1234"),
        ("wrapped-account-id.pdf", "Account (ID) 1234", "1234"),
        ("ending-in-account-number.pdf", "Account ending in ••••1234", "1234"),
        ("punctuated-account-number.pdf", "Account: Number: 8901234567", "8901234567"),
    ):
        pages = _monthly_pages()
        pages[0][3] = (72, 630, label)

        observation = WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, pages, name=name),
            document_ordinal=1,
            document=_monthly_document(),
        )

        assert all(
            identifier not in token.text
            for page in observation.page_evidence
            for token in page.tokens
        )


def test_unicode_slash_abbreviated_account_labels_are_scrubbed_before_evidence() -> None:
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    for lines in (
        [[("A\u2215C No. 1234", box)], [("Fictional public heading", box)]],
        [
            [("A\u2044C:", box)],
            [("No:", box)],
            [("1234", box)],
            [("Fictional public heading", box)],
        ],
    ):
        scrubbed = bank_statements._discard_account_identifier_tokens(
            lines, document_ordinal=1, page_number=1
        )

        assert all("1234" not in text for line in scrubbed for text, _ in line)


def test_noncanonical_account_abbreviation_fragments_fail_before_evidence() -> None:
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    for start, separator, end in (
        ("A", "\u2215", "C"),
        ("A", "\u2571", "C"),
        ("4", "/", "C"),
        ("@", "/", "C"),
        ("\u2200", "/", "C"),
        ("A", "_", "C"),
        ("A", "/", "("),
        ("A", "/", "<"),
        ("A", "/", "["),
        ("A", "/", "{"),
        ("A", "/", "\u2282"),
        ("A", "/", "\u228a"),
        ("A", "/", "\u228f"),
        ("A", "/", "\u2291"),
        ("A", "/", "\u2329"),
        ("A", "/", "\u3008"),
        ("A", "/", "\u27c3"),
        ("A", "/", "\u27e8"),
    ):
        for lines in (
            [[(f"{start}{separator}{end} Ref 1234", box)], [("Fictional public heading", box)]],
            [
                [(start, box)],
                [(separator, box)],
                [("Header", box)],
                [(f"{end} Ref", box)],
                [("12", box)],
                [("34", box)],
                [("Fictional public heading", box)],
            ],
        ):
            with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
                bank_statements._materialize_scrubbed_native_layout(
                    lines,
                    document_ordinal=1,
                    page_number=1,
                    limits=limits,
                )


def test_armed_account_abbreviation_rejects_financial_shaped_or_preceding_short_identifiers() -> (
    None
):
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    for lines in (
        [[("A/C Ref", box)], [("12/34", box)], [("Fictional public heading", box)]],
        [[("A/C Ref", box)], [("10.00", box)], [("Fictional public heading", box)]],
        [[("A/C Ref", box)], [("1O23", box)], [("Fictional public heading", box)]],
        [[("A/C Ref", box)], [("1I23", box)], [("Fictional public heading", box)]],
        [[("A/C Ref", box)], [("12S4", box)], [("Fictional public heading", box)]],
        [[("A/C Ref", box)], [("l234", box)], [("Fictional public heading", box)]],
        [[("A/C Ref", box)], [("12B4", box)], [("Fictional public heading", box)]],
        [[("A/C Ref", box)], [("1Z34", box)], [("Fictional public heading", box)]],
        [[("A/C Ref", box)], [("1\u039f23", box)], [("Fictional public heading", box)]],
        [[("A/C Ref", box)], [("12\u04054", box)], [("Fictional public heading", box)]],
        [[("1234", box)], [("Header", box)], [("A/C Ref", box)], [("Fictional", box)]],
        [[("12/34", box)], [("Header", box)], [("A/C Ref", box)], [("Fictional", box)]],
    ):
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            bank_statements._materialize_scrubbed_native_layout(
                lines,
                document_ordinal=1,
                page_number=1,
                limits=limits,
            )


def test_armed_account_abbreviation_counts_whitespace_hidden_short_identifiers() -> None:
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    limits = bank_statements._native_extraction_limits(document_ordinal=1)

    for identifier in ("12 34", "12\u00a034", "ID 12 34"):
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            bank_statements._materialize_scrubbed_native_layout(
                [[("A/C Ref", box)], [(identifier, box)]],
                document_ordinal=1,
                page_number=1,
                limits=limits,
            )


def test_bare_a_does_not_cross_arbitrary_prose_before_an_account_separator() -> None:
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    lines = [
        [("A", box)],
        [("Header", box)],
        [("/", box)],
        [("C Ref", box)],
        [("2026", box)],
    ]

    assert not bank_statements._lines_have_unhandled_identifier_candidate(
        lines,
        provisional_phone_token_indexes=(frozenset(),) * len(lines),
    )


@pytest.mark.parametrize(
    "page_header",
    (
        "Page \u2014 2",
        "Page \u00b7 2",
        "Page \u2013 2",
        "Page \u2020 2",
        "Page 2\u20443",
        "Page 2\u22153",
        "Page \u00b6 2",
        "Page \u00a7 2",
        "PAGE | 2",
        "\u2014 2 \u2014",
        "\u2013 2 \u2013",
        "\u00b7 2 \u00b7",
        "2\u20443",
        "\u3010 2 \u3011",
        "\u2014 Page 2 \u2014",
    ),
)
def test_decorated_page_headers_cannot_reset_a_split_account_label(page_header: str) -> None:
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    limits = bank_statements._native_extraction_limits(document_ordinal=1)

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._materialize_scrubbed_native_layout(
            [
                [("Foo Acc", box)],
                [(page_header, box)],
                [("ount Ref 1234", box)],
            ],
            document_ordinal=1,
            page_number=1,
            limits=limits,
        )


def test_public_account_heading_suffixes_must_end_at_a_source_word_boundary() -> None:
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    limits = bank_statements._native_extraction_limits(document_ordinal=1)

    for lines in (
        [[("Account SummaryRef 1234", box)]],
        [[("Account", box)], [("SummaryRef", box)], [("1234", box)]],
        [[("Account InformationRef 1234", box)]],
        [[("Account", box)], [("InformationRef", box)], [("1234", box)]],
        [[("Account", box)], [("Summary", box)], [("Ref", box)], [("1234", box)]],
        [[("Account", box)], [("Summary", box)], [("Number", box)], [("1234", box)]],
        [[("Account", box)], [("Summary", box)], [("ending in", box)], [("1234", box)]],
        [[("Account", box)], [("Summary", box)], [("ending", box)], [("in", box)], [("1234", box)]],
        [[("Account", box)], [("Summary", box)], [("Reference", box)], [("1234", box)]],
        [[("Account Summary", box)], [("Header", box)], [("Ref", box)], [("1234", box)]],
        [
            [("Account Summary", box)],
            [("Header", box)],
            [("Header", box)],
            [("Ref", box)],
            [("1234", box)],
        ],
        [
            [("Account Summary", box)],
            [("Alpha", box)],
            [("Beta", box)],
            [("Gamma", box)],
            [("Delta", box)],
            [("Ref", box)],
            [("1234", box)],
        ],
        [[("Account Summary", box)], [("Page 2", box)], [("Ref", box)], [("1234", box)]],
        [[("Account Summary", box)], [("---", box)], [("Ref", box)], [("1234", box)]],
    ):
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            bank_statements._materialize_scrubbed_native_layout(
                lines,
                document_ordinal=1,
                page_number=1,
                limits=limits,
            )


def test_inline_account_identifier_at_page_end_is_complete(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[0][3] = (72, 430, "Acct. 5678901234")

    observation = WellsFargoStatementExtractor().extract(
        _write_pdf(tmp_path, pages, name="inline-account-at-page-end.pdf"),
        document_ordinal=1,
        document=_monthly_document(),
    )

    assert not bank_statements._is_monetary_like_text("5678901234")
    assert all(
        "5678901234" not in token.text
        for page in observation.page_evidence
        for token in page.tokens
    )


def test_account_label_continuation_cannot_reset_on_next_page(tmp_path: Path) -> None:
    pages = _monthly_pages()[:2]
    pages[0][3] = (72, 430, "Account Number")
    pages[1].insert(3, (72, 630, "6789012345"))

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, pages, name="cross-page-account-continuation.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_account_label_awaiting_identifier_cannot_reset_with_decorated_id() -> None:
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    lines = [
        [("Account Number", box)],
        [("ID: 7890123456", box)],
        [("Fictional Information", box)],
    ]

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._discard_account_identifier_tokens(lines, document_ordinal=1, page_number=1)


def test_wrapped_account_label_with_attached_masked_identifier_is_discarded(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[0][3] = (72, 630, "Account")
    pages[0].insert(4, (72, 610, "Number ••••1234"))

    observation = WellsFargoStatementExtractor().extract(
        _write_pdf(tmp_path, pages, name="wrapped-masked-account-identifier.pdf"),
        document_ordinal=1,
        document=_monthly_document(),
    )

    assert all(
        "1234" not in token.text for page in observation.page_evidence for token in page.tokens
    )


def test_wrapped_account_label_punctuation_variants_are_discarded(tmp_path: Path) -> None:
    for name, prefix, suffix, identifier, forbidden in (
        ("wrapped-number-colon.pdf", "Account", "Number:", "1234567890", "1234567890"),
        ("wrapped-no-colon.pdf", "Account", "No:", "2345678901", "2345678901"),
        ("wrapped-hash.pdf", "Account", "#", "3456789012", "3456789012"),
        ("wrapped-id.pdf", "Account", "ID:", "4567", "4567"),
        ("wrapped-ending-in.pdf", "Account", "ending in", "••••4567", "4567"),
        (
            "wrapped-number-ending-in.pdf",
            "Account",
            "Number: ending in",
            "••••5678",
            "5678",
        ),
        ("wrapped-account-colon.pdf", "Account:", "Number:", "6789012345", "6789012345"),
        ("wrapped-acct-colon.pdf", "Acct.:", "No:", "7890123456", "7890123456"),
        ("wrapped-abbreviated-ac-colon.pdf", "A/C:", "No:", "8901234567", "8901234567"),
    ):
        pages = _monthly_pages()
        pages[0][3] = (72, 630, prefix)
        pages[0].insert(4, (72, 610, suffix))
        pages[0].insert(5, (72, 590, identifier))

        observation = WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, pages, name=name),
            document_ordinal=1,
            document=_monthly_document(),
        )

        assert all(
            forbidden not in token.text
            for page in observation.page_evidence
            for token in page.tokens
        )


def test_account_label_context_with_financial_content_fails_closed(tmp_path: Path) -> None:
    label_line = _monthly_pages()
    label_line[0][3] = (72, 630, "Account Number 1234567890 01/15/2026 10.00")
    continuation_line = _monthly_pages()
    continuation_line[0][3] = (72, 630, "Account Number")
    continuation_line[0].insert(4, (72, 610, "1234567890 10.00"))
    wrapped_label_line = _monthly_pages()
    wrapped_label_line[0][3] = (72, 630, "Account")
    wrapped_label_line[0].insert(4, (72, 610, "Number 1234567890 10.00"))
    activity_row = _monthly_pages()
    activity_row[1][9] = (150, 540, "Fictional Account Number Fee")

    for name, pages, page_number in (
        ("financial-account-label.pdf", label_line, 1),
        ("financial-account-continuation.pdf", continuation_line, 1),
        ("financial-wrapped-account-label.pdf", wrapped_label_line, 1),
        ("financial-account-description.pdf", activity_row, 2),
    ):
        with pytest.raises(StatementExtractionError, match=rf"document 1 page {page_number}"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name=name),
                document_ordinal=1,
                document=_monthly_document(),
            )


def test_confusable_account_qualifiers_fail_before_evidence() -> None:
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    qualifiers = (
        "Numb\u0435r",
        "Nu\u043c",
        "N\u043e",
        "\u0406D",
        "Id\u0435ntifier",
        "\u0435nding in",
        "ending \u0456n",
    )

    for qualifier in qualifiers:
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            bank_statements._discard_account_identifier_tokens(
                [[(f"Account {qualifier} 1234", box)]],
                document_ordinal=1,
                page_number=1,
            )
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            bank_statements._discard_account_identifier_tokens(
                [[(f"A/C {qualifier} 1234", box)]],
                document_ordinal=1,
                page_number=1,
            )
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            bank_statements._discard_account_identifier_tokens(
                [[("Account", box)], [(f"{qualifier} 1234", box)]],
                document_ordinal=1,
                page_number=1,
            )

    ordinary_public_prose = [
        [("Account Information", box)],
        [("No ID is required for this fictional public notice.", box)],
        [("Account Numbering Notes", box)],
    ]
    assert (
        bank_statements._discard_account_identifier_tokens(
            ordinary_public_prose,
            document_ordinal=1,
            page_number=1,
        )
        == ordinary_public_prose
    )

    identifier = "Account Numb\u0435r 1234"
    characters = tuple(
        bank_statements._RawCharacter(
            index=index,
            text=character,
            box=BoundingBox(
                Decimal("0.1") + Decimal(index) / Decimal("100"),
                Decimal("0.2"),
                Decimal("0.105") + Decimal(index) / Decimal("100"),
                Decimal("0.21"),
            ),
        )
        for index, character in enumerate(identifier)
    )
    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._group_native_characters(characters, document_ordinal=1, page_number=1)


def test_ascii_obfuscated_account_labels_fail_before_evidence(tmp_path: Path) -> None:
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    for label in (
        "Acc0unt Number 1234",
        "Account Numb3r 1234",
        "A-c-c-o-u-n-t Number 1234",
        "Account N-u-m-b-e-r 1234",
        "Account_Number_1234",
        "Account-Number-1234",
        "Account/Number/1234",
        "Acc0unt ending in 1234",
        "Acct_ending_in_1234",
        "Acc\u00b7ount 1234",
        "A/C N0 1234",
        "A/C Numb3r 1234",
        "A/C 3nding in 1234",
        "A/CNo.1234",
    ):
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            bank_statements._discard_account_identifier_tokens(
                [[(label, box)]], document_ordinal=1, page_number=1
            )

        pages = _monthly_pages()
        pages[1][9] = (150, 540, label)
        with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(
                    tmp_path,
                    pages,
                    name=f"fictional-{label[:7].casefold().replace('/', '-')}-account-label.pdf",
                ),
                document_ordinal=1,
                document=_monthly_document(),
            )

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._discard_account_identifier_tokens(
            [[("Acc0unt", box)]], document_ordinal=1, page_number=1
        )
    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._discard_account_identifier_tokens(
            [[("A-c-c-o-u-n-t", box)]], document_ordinal=1, page_number=1
        )
    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._discard_account_identifier_tokens(
            [[("Account", box)], [("N-u-m-b-e-r 1234", box)]],
            document_ordinal=1,
            page_number=1,
        )
    for lines in (
        [[("Account", box)], [("Numb3r", box)], [("1234", box)]],
        [[("Acc0unt Number", box)], [("1234", box)]],
    ):
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            bank_statements._discard_account_identifier_tokens(
                lines,
                document_ordinal=1,
                page_number=1,
            )

    widely_spaced = tuple(
        bank_statements._RawCharacter(
            index=index,
            text=character,
            box=BoundingBox(
                Decimal(index) / Decimal("100"),
                Decimal("0.1"),
                Decimal(index) / Decimal("100") + Decimal("0.001"),
                Decimal("0.11"),
            ),
        )
        for index, character in enumerate("AccountNumber1234")
    )
    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._group_native_characters(
            widely_spaced,
            document_ordinal=1,
            page_number=1,
        )


def test_unconfirmed_account_text_without_an_identifier_suffix_is_retained(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[2].extend(
        [
            (72, 480, "Account"),
            (72, 440, "Information"),
            (72, 400, "Account Numbering Notes"),
            (72, 360, "Account Information Guide"),
            (72, 320, "Account Numbering Reference"),
            (72, 280, "This public boilerplate has no account ID, balance data, or identifier."),
        ]
    )

    observation = WellsFargoStatementExtractor().extract(
        _write_pdf(tmp_path, pages, name="unconfirmed-account-text.pdf"),
        document_ordinal=1,
        document=_monthly_document(),
    )

    token_texts = {token.text for page in observation.page_evidence for token in page.tokens}
    assert {"Account", "Information", "Numbering", "Reference", "ID,"}.issubset(token_texts)


def test_unknown_ascii_account_label_with_a_short_identifier_fails_before_evidence() -> None:
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    limits = bank_statements._native_extraction_limits(document_ordinal=1)

    for lines in (
        [[("Account Ref 1234", box)]],
        [[("Account-Ref-1234", box)]],
        [[("Account.Ref.1234", box)]],
        [[("Account/Ref/1234", box)]],
        [[(value, box) for value in ("A", "c", "c", "o", "u", "n", "t", "Ref", "1234")]],
        [
            [(value, box) for value in ("A", "c", "c")],
            [(value, box) for value in ("o", "u", "n", "t", "Ref")],
            [("1234", box)],
        ],
        [[("Account Ref", box)], [("1234", box)]],
        [[("Account", box)], [("Ref 1234", box)]],
        [[("Account Ref", box)], [("Header", box)], [("1234", box)]],
        [[("Account", box)], [("Ref", box)], [("Header", box)], [("1234", box)]],
        [[("Acc\u03bfunt Ref", box)], [("Header", box)], [("1234", box)]],
        [[("Acc\u0660unt Ref", box)], [("Header", box)], [("1234", box)]],
        [[("Account Ref", box)], [("12", box)], [("Header", box)], [("34", box)]],
        [
            [("Acc", box)],
            [("Headerount", box), ("Ref", box)],
            [("Header", box)],
            [("1234", box)],
        ],
        [
            [("A\u0441\u0441", box)],
            [("Header\u03bfunt", box), ("Ref", box)],
            [("Header", box)],
            [("1234", box)],
        ],
        [
            [("4cc", box)],
            [("Header0unt", box), ("Ref", box)],
            [("Header", box)],
            [("1234", box)],
        ],
        [
            [(value, box)]
            for value in ("A", "c", "c", "\u03bf", "u", "n", "t", "Ref", "Header", "1234")
        ],
    ):
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            bank_statements._materialize_scrubbed_native_layout(
                lines,
                document_ordinal=1,
                page_number=1,
                limits=limits,
            )


@pytest.mark.parametrize(
    "fragments",
    (
        ("Ac.count Ref 2266",),
        ("Acc_ount Ref 2266",),
        ("Ac/count Ref 2266",),
        ("Acc", "ountRef", "2266"),
        ("AccountNo1234",),
        ("AccountNumber1234",),
        ("AccountID1234",),
        ("AccountRef1234",),
        ("Acc0untNo1234",),
        ("Primary Account Ref 2266",),
        ("Details: Acct Ref 2266",),
        ("Acc", "-", "ount Ref", "1234"),
        ("Acc", "0", "unt Ref", "1234"),
        ("Acc", "\u200b", "ount Ref", "1234"),
        ("A", "c", "c", "0", "u", "n", "t", "Ref", "1234"),
        ("Account Summary 1234",),
        ("Account Summary", "1234"),
        ("Account Summary", "R\u0435f", "2266"),
        ("Account Summary", "R3f", "2266"),
        ("Account Summary", "Numb\u0435r", "2266"),
        ("2266", "Acc", "ount", "Ref"),
        ("Acc", "Page 2", "ount", "Ref", "2266"),
        ("A/C Ref", "1", "O", "2", "3"),
    ),
)
def test_residual_account_label_variants_fail_before_evidence(
    fragments: tuple[str, ...],
) -> None:
    """Reject synthetic label evasions before any private text can cross the boundary."""

    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    lines = [[(fragment, box)] for fragment in fragments]

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._materialize_scrubbed_native_layout(
            lines,
            document_ordinal=1,
            page_number=1,
            limits=limits,
        )


@pytest.mark.parametrize(
    "line_fragments",
    (
        (("Primary Account Activity 1234",),),
        (("Primary Account ActivityRef1234",),),
        (("Primary Account Activity Ref 10.00",),),
        (("Fictional Account Activity Fee Ref 10.00",),),
        (("Fictional Account Activity Fee",), ("12",), ("34",)),
        (("Primary Account Acti",), ("vity Ref 1234",)),
        (("Primary Account Acti",), ("-",), ("vity Ref",), ("1234",)),
        (("Foo Account Numbering Ref 10.00",),),
        (("Foo Account Activity",), ("No",), ("10.00",)),
        (("Account Summary",), ("N",), ("o",), ("10.00",)),
        (("Account Summary",), ("N",), ("P\u0430ge 2",), ("o",), ("10.00",)),
        (("Account Summary",), ("N", "o"), ("10.00",)),
        (("Account Summary",), ("N\u200bo",), ("10.00",)),
        (("Account Summary",), ("N0",), ("10.00",)),
        (("Account Summary",), ("N",), ("\u043e",), ("10.00",)),
        (("Account Activity",), ("No=",), ("10.00",)),
        (("Account Activity",), ("No-",), ("10.00",)),
        (("Account Activity",), ("No/",), ("10.00",)),
        (("Account Activity",), ("No_",), ("10.00",)),
        (("Account Activity",), ("Ref=",), ("10.00",)),
        (("Account Activity",), ("Number=",), ("10.00",)),
        (("Account Activity",), ("ID=",), ("10.00",)),
        (("Account Activity",), ("#",), ("10.00",)),
        (("Account Activity",), ("#.",), ("10.00",)),
        (("Account Activity",), ("#=",), ("10.00",)),
        (("Account Activity",), ("\u266f",), ("10.00",)),
        (("Account Activity",), ("(#)",), ("$10.00",)),
        (("Account Activity",), ("*",), ("10.00",)),
        (("Account Activity",), ("x",), ("10.00",)),
        (("Account Activity",), ("R\u212ef 10.00",)),
        (("Account Activity",), ("R\u20acf 10.00",)),
        (("Account Activity",), ("Ending !n 10.00",)),
        (("Account Activity",), ("lD 10.00",)),
        (("Account Activity",), ("!D 10.00",)),
        (("Account Activity",), ("Nurnber 10.00",)),
        (("Account Activity",), ("R", "\u212e", "f", "10.00")),
        (("Account Activity",), ("R",), ("P\u0430ge 2",), ("\u212ef 10.00",)),
        (("Acc\u25cbunt Number 10.00",),),
        (("Foo Acc\u2022ount Ref 1234",),),
        (("A",), ("P\u212ege 2",), ("/C Ref",), ("10.00",)),
        (("Foo Acc",), ("P\u212ege 2",), ("ount Ref 1234",)),
        (("Foo A",), ("P\u212ege 2",), ("cct Ref 1234",)),
        (("Foo Account Acti",), ("P\u212ege 2",), ("vity Ref 1234",)),
        (("Account Summary",), ("R",), ("P\u212ege 2",), ("\u212ef 10.00",)),
        (("Account end",), ("Page 2",), ("ing in 1234",)),
        (("1234",), ("Acc",), ("ount Ref", "Account Summary")),
        (("Foo Account R\u212ef 1234",),),
        (("Foo Account R\u20acf 1234",),),
        (("Foo Account Nurnber 1234",),),
        (("Foo Acc\u2116ount Ref 1234",),),
        (("Foo A0/C Ref 1234",),),
        (("Foo A3/C Ref 1234",),),
    ),
)
def test_public_account_phrase_identifier_variants_fail_before_evidence(
    line_fragments: tuple[tuple[str, ...], ...],
) -> None:
    """A public phrase never clears an adjacent residual identifier context."""

    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    lines = [[(fragment, box) for fragment in fragments] for fragments in line_fragments]

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._materialize_scrubbed_native_layout(
            lines,
            document_ordinal=1,
            page_number=1,
            limits=limits,
        )


@pytest.mark.parametrize(
    "line_fragments",
    (
        (("Monthly Account",), ("Statement",), ("01/01/2026",)),
        (("Current Account",), ("Balance",), ("02/10/2026",)),
        (("Fictional Account Activity Fee 01/01/2026 10.00",),),
        (("Account Summary",), ("June 1, 2026",)),
        (("Account Summary",), ("January 15, 2026",), ("10.00",)),
        (
            ("Account Information",),
            ("No ID is required for this fictional public notice.",),
            ("01/15/2026",),
            ("10.00",),
        ),
        (("Account Summary",), ("x axis label",), ("01/15/2026",), ("10.00",)),
        (("Account Balance",), ("Ending Balance",), ("$1,000.00",)),
        (("Important Account Information",), ("Questions? Call", "800-555-1234")),
        (("Account Summary",), ("Statement Period 01/01/2026\u201301/31/2026",)),
        (("Account Balance",), ("Balance ($1,000.00)",)),
        (("Account Balance",), ("Balance -$1,000.00",)),
        (
            ("Important Account Information",),
            ("This public boilerplate supplies no financial fact.",),
            ("Opening Balance 01/01/2026 $1,000.00",),
        ),
        (
            ("Monthly Account Activity",),
            ("01/01/2026",),
            ("Refund from vendor",),
            ("10.00",),
        ),
    ),
)
def test_public_account_heading_controls_are_safe_at_the_privacy_boundary(
    line_fragments: tuple[tuple[str, ...], ...],
) -> None:
    """These controls test screening only, not parser support for every date/balance form."""

    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    lines = [[(fragment, box) for fragment in fragments] for fragments in line_fragments]

    bank_statements._materialize_scrubbed_native_layout(
        lines,
        document_ordinal=1,
        page_number=1,
        limits=limits,
    )


def test_designated_public_phone_completes_an_unresolved_account_context() -> None:
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    lines = [[("Account", box)], [("Call", box), ("800-555-1234", box)]]
    phone_indexes = (frozenset(), frozenset({1}))

    assert bank_statements._lines_have_unhandled_identifier_candidate(
        lines,
        provisional_phone_token_indexes=phone_indexes,
    )


def test_public_account_heading_rejects_a_short_identifier_suffix() -> None:
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))

    assert bank_statements._lines_have_unhandled_identifier_candidate(
        [[("Account Summary", box)], [("1234", box)]],
        provisional_phone_token_indexes=(frozenset(), frozenset()),
    )
    assert bank_statements._lines_have_unhandled_identifier_candidate(
        [[("a", box)], [("Account Summary", box)], [("1234", box)]],
        provisional_phone_token_indexes=(frozenset(), frozenset(), frozenset()),
    )
    assert bank_statements._lines_have_unhandled_identifier_candidate(
        [
            [("Acc", box)],
            [("Account Summary", box)],
            [("Headerount", box), ("Ref", box)],
            [("1234", box)],
        ],
        provisional_phone_token_indexes=(frozenset(),) * 4,
    )


def test_label_looking_account_near_misses_fail_before_identifier_evidence(tmp_path: Path) -> None:
    direct = _monthly_pages()
    direct[0][3] = (72, 630, "Account (No) 1234567890")
    bracketed = _monthly_pages()
    bracketed[0][3] = (72, 630, "Account [No] 4567890123")
    wrapped = _monthly_pages()
    wrapped[0][3] = (72, 630, "Account")
    wrapped[0].insert(4, (72, 610, "Number (ending in)"))
    wrapped[0].insert(5, (72, 590, "2345678901"))
    immediate_identifier = _monthly_pages()
    immediate_identifier[0][3] = (72, 630, "Account")
    immediate_identifier[0].insert(4, (72, 610, "3456789012"))

    for name, pages in (
        ("direct-account-near-miss.pdf", direct),
        ("bracketed-account-near-miss.pdf", bracketed),
        ("wrapped-account-near-miss.pdf", wrapped),
        ("account-followed-by-identifier.pdf", immediate_identifier),
    ):
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name=name),
                document_ordinal=1,
                document=_monthly_document(),
            )


def test_decorated_inline_account_identifier_candidates_fail_closed(tmp_path: Path) -> None:
    for name, label in (
        ("parenthesized-account-identifier.pdf", "Account (8901234567)"),
        ("angled-acct-identifier.pdf", "Acct. <9012345678>"),
    ):
        pages = _monthly_pages()
        pages[0][3] = (72, 630, label)

        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name=name),
                document_ordinal=1,
                document=_monthly_document(),
            )


def test_qualified_account_label_identifier_candidates_fail_closed(tmp_path: Path) -> None:
    for name, label in (
        ("checking-account-identifier.pdf", "Checking Account: 1234567890"),
        ("primary-acct-mask.pdf", "Primary Acct: ****1234"),
        ("attached-checking-account-identifier.pdf", "Checking Account:1234567890"),
        ("attached-primary-acct-mask.pdf", "Primary Acct:****1234"),
        ("equals-checking-account-identifier.pdf", "Checking Account=1234567890"),
        ("trailing-checking-account-identifier.pdf", "Checking Account: 1234567890/"),
    ):
        pages = _monthly_pages()
        pages[0][3] = (72, 630, label)

        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name=name),
                document_ordinal=1,
                document=_monthly_document(),
            )


def test_bare_identifier_candidates_fail_before_evidence(tmp_path: Path) -> None:
    balance = _monthly_pages()
    balance[0][4] = (
        72,
        570,
        "Opening Balance Start of Day Excludes Pending 01/01/2026 1,000.00 1234567890",
    )
    activity = _monthly_pages()
    activity[1].insert(10, (275, 540, "1234567890"))
    split_activity = _monthly_pages()
    split_activity[1].extend(
        [
            (275, 540, "123"),
            (305, 540, "456"),
            (335, 540, "7890"),
        ]
    )
    six_digit_activity = _monthly_pages()
    six_digit_activity[1].insert(10, (275, 540, "123456"))
    masked_activity = _monthly_pages()
    masked_activity[1].insert(10, (275, 540, "*1"))
    header = _monthly_pages()
    header[1].append((300, 610, "1234567890"))
    slash_grouped_activity = _monthly_pages()
    slash_grouped_activity[1].insert(10, (275, 540, "234/567/8901"))
    unicode_dash_grouped_activity = _monthly_pages()
    unicode_dash_grouped_activity[1].insert(10, (275, 540, "234\u2013567\u20138901"))
    split_boilerplate = _monthly_pages()
    split_boilerplate[2].extend(
        [
            (72, 480, "1234"),
            (72, 440, "5678"),
            (72, 400, "9012"),
        ]
    )

    for name, pages, page_number in (
        ("bare-balance-identifier.pdf", balance, 1),
        ("bare-activity-identifier.pdf", activity, 2),
        ("split-bare-activity-identifier.pdf", split_activity, 2),
        ("six-digit-activity-identifier.pdf", six_digit_activity, 2),
        ("short-masked-activity-identifier.pdf", masked_activity, 2),
        ("bare-header-identifier.pdf", header, 2),
        ("slash-grouped-activity-identifier.pdf", slash_grouped_activity, 2),
        ("unicode-dash-grouped-activity-identifier.pdf", unicode_dash_grouped_activity, 2),
        ("split-boilerplate-identifier.pdf", split_boilerplate, 3),
    ):
        with pytest.raises(StatementExtractionError, match=rf"document 1 page {page_number}"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name=name),
                document_ordinal=1,
                document=_monthly_document(),
            )


def test_middle_dot_grouped_account_and_phone_forms_fail_before_evidence(tmp_path: Path) -> None:
    account = _monthly_pages()
    account[1][9] = (150, 540, "Fictional account 123\u00b7456\u00b7890")
    phone = _monthly_pages()
    phone[2].append((72, 480, "Questions? Call 234\u00b7567\u00b78901"))

    for name, pages, page_number in (
        ("middle-dot-account-identifier.pdf", account, 2),
        ("middle-dot-phone-identifier.pdf", phone, 3),
    ):
        with pytest.raises(StatementExtractionError, match=rf"document 1 page {page_number}"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name=name),
                document_ordinal=1,
                document=_monthly_document(),
            )


def test_grouped_identifier_separators_fail_before_evidence() -> None:
    for identifier in (
        "234/567/8901",
        "234\u2010567\u20108901",
        "123\u00b7456\u00b7890",
        "234\u00b7567\u00b78901",
        "123:456:7890",
        "800:555:0199",
        "123;456;7890",
        "123=456=7890",
        "123|456|7890",
        "123_456_7890",
    ):
        characters = tuple(
            bank_statements._RawCharacter(
                index=index,
                text=character,
                box=BoundingBox(
                    Decimal("0.1") + Decimal(index) / Decimal("100"),
                    Decimal("0.2"),
                    Decimal("0.105") + Decimal(index) / Decimal("100"),
                    Decimal("0.21"),
                ),
            )
            for index, character in enumerate(identifier)
        )

        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            bank_statements._group_native_characters(
                characters,
                document_ordinal=1,
                page_number=1,
            )


def test_alphabetic_digit_homographs_fail_before_evidence(tmp_path: Path) -> None:
    for identifier in ("1234O6789", "234-567-89O1", "1234\u039f6789"):
        assert bank_statements._text_has_unhandled_identifier_candidate(identifier)

    for identifier in ("1234O6789", "234-567-89O1"):
        pages = _monthly_pages()
        pages[1][9] = (150, 540, f"Fictional reference {identifier}")
        with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name="fictional-alpha-obstructed-identifier.pdf"),
                document_ordinal=1,
                document=_monthly_document(),
            )

    for safe_text in (
        "Fictional supplies arrive today.",
        "Order Q1 has no private identifier.",
        "x axis label",
        "01/15/2026",
        "1,250.00",
    ):
        assert not bank_statements._text_has_unhandled_identifier_candidate(safe_text)
    assert bank_statements._text_has_unhandled_identifier_candidate("xx1234")


def test_generic_identifier_delimiter_keeps_date_money_and_prose_exemptions() -> None:
    assert bank_statements._is_generic_identifier_syntax_character("\u00b7")
    assert not bank_statements._is_identifier_mask("\u00b7")
    assert not bank_statements._is_identifier_syntax_character("\u00b7")

    for identifier in ("123\u00b7456\u00b7890", "234\u00b7567\u00b78901"):
        assert bank_statements._text_has_unhandled_identifier_candidate(identifier)

    for safe_text in (
        "01/15/2026",
        "1,250.00",
        "$1,250.00",
        "Fictional prose with /, \u00b7, :, ;, =, |, and _ punctuation",
        "Fictional prose uses middle\u00b7dot punctuation.",
    ):
        assert not bank_statements._text_has_unhandled_identifier_candidate(safe_text)


def test_nfkc_identifier_screening_fails_or_redacts_before_evidence() -> None:
    box = BoundingBox(Decimal("0.1"), Decimal("0.1"), Decimal("0.11"), Decimal("0.11"))
    full_width_identifier = "\uff11\uff12\uff13\uff14\uff15\uff16\uff17\uff18\uff19\uff10"
    arabic_indic_identifier = "\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u0660"
    account_label = [[(f"Account ID: {full_width_identifier[:4]}", box)]]
    account_mask_label = [[("Account ID: \uff0a\uff0a\uff0a\uff0a\uff11\uff12\uff13\uff14", box)]]

    assert bank_statements._discard_account_identifier_tokens(
        account_label,
        document_ordinal=1,
        page_number=1,
    ) == [[]]
    assert bank_statements._discard_account_identifier_tokens(
        account_mask_label,
        document_ordinal=1,
        page_number=1,
    ) == [[]]
    assert account_label[0][0][0].endswith(full_width_identifier[:4])

    characters = tuple(
        bank_statements._RawCharacter(
            index=index,
            text=character,
            box=BoundingBox(
                Decimal("0.1") + Decimal(index) / Decimal("100"),
                Decimal("0.2"),
                Decimal("0.105") + Decimal(index) / Decimal("100"),
                Decimal("0.21"),
            ),
        )
        for index, character in enumerate(full_width_identifier)
    )

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._group_native_characters(characters, document_ordinal=1, page_number=1)

    masked_characters = tuple(
        bank_statements._RawCharacter(
            index=index,
            text=character,
            box=BoundingBox(
                Decimal("0.1") + Decimal(index) / Decimal("100"),
                Decimal("0.3"),
                Decimal("0.105") + Decimal(index) / Decimal("100"),
                Decimal("0.31"),
            ),
        )
        for index, character in enumerate("\uff0a\uff11")
    )

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._group_native_characters(
            masked_characters,
            document_ordinal=1,
            page_number=1,
        )

    for account_label_candidate in (
        f"Account ID: {arabic_indic_identifier[:4]}",
        "Account ID: \u25cf\u25cf\u25cf\u25cf1234",
        "\u0410ccount:1234",
        "\u0410ccount=1234",
        "Acco\u0338unt 1234",
    ):
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            bank_statements._discard_account_identifier_tokens(
                [[(account_label_candidate, box)]],
                document_ordinal=1,
                page_number=1,
            )

    for identifier in (arabic_indic_identifier, "\u25cf\u25cf\u25cf\u25cf1234"):
        candidate_characters = tuple(
            bank_statements._RawCharacter(
                index=index,
                text=character,
                box=BoundingBox(
                    Decimal("0.1") + Decimal(index) / Decimal("100"),
                    Decimal("0.4"),
                    Decimal("0.105") + Decimal(index) / Decimal("100"),
                    Decimal("0.41"),
                ),
            )
            for index, character in enumerate(identifier)
        )
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            bank_statements._group_native_characters(
                candidate_characters,
                document_ordinal=1,
                page_number=1,
            )


def test_public_boilerplate_phone_exemption_is_narrow(tmp_path: Path) -> None:
    public_phone = _monthly_pages()
    public_phone[2].append((72, 480, "Questions? Call 1-800-555-0199"))

    observation = WellsFargoStatementExtractor().extract(
        _write_pdf(tmp_path, public_phone, name="fictional-public-contact-phone.pdf"),
        document_ordinal=1,
        document=_monthly_document(),
    )

    assert observation.page_evidence[2].page_kind is PageKind.BOILERPLATE
    public_phone_tokens = {token.text for token in observation.page_evidence[2].tokens}
    assert "Questions?" in public_phone_tokens
    assert "Call" in public_phone_tokens
    assert "1-800-555-0199" not in public_phone_tokens

    hostile_phone = _monthly_pages()
    hostile_phone[2].append((72, 480, "Questions? Call 234-567-8901"))
    hostile_observation = WellsFargoStatementExtractor().extract(
        _write_pdf(tmp_path, hostile_phone, name="fictional-hostile-phone-shape.pdf"),
        document_ordinal=1,
        document=_monthly_document(),
    )
    assert all(
        token.text != "234-567-8901" for token in hostile_observation.page_evidence[2].tokens
    )

    no_contact_context = _monthly_pages()
    no_contact_context[2].append((72, 480, "Fictional number 1-800-555-0199"))
    invalid_phone_shape = _monthly_pages()
    invalid_phone_shape[2].append((72, 480, "Questions? Call 1-100-555-0199"))
    phone_with_identifier = _monthly_pages()
    phone_with_identifier[2].append((72, 480, "Questions? Call 1-800-555-0199 reference 123456"))
    non_boilerplate_phone = _monthly_pages()
    non_boilerplate_phone[1].extend(
        [
            (72, 320, "Important Account Information"),
            (72, 300, "Questions? Call 1-800-555-0199"),
        ]
    )

    for name, pages, page_number in (
        ("unlabelled-public-phone.pdf", no_contact_context, 3),
        ("invalid-public-phone-shape.pdf", invalid_phone_shape, 3),
        ("public-phone-with-identifier.pdf", phone_with_identifier, 3),
        ("non-boilerplate-public-phone.pdf", non_boilerplate_phone, 2),
    ):
        with pytest.raises(StatementExtractionError, match=rf"document 1 page {page_number}"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name=name),
                document_ordinal=1,
                document=_monthly_document(),
            )


def test_visual_money_fragments_are_coalesced_or_fail_closed(tmp_path: Path) -> None:
    canonical = _monthly_pages()
    canonical[0][4] = (
        72,
        570,
        "Opening Balance Start of Day Excludes Pending 01/01/2026",
    )
    canonical[0].extend(
        [
            (390, 570, "1"),
            (396, 570, ","),
            (399, 570, "000"),
            (417, 570, "."),
            (421, 570, "00"),
        ]
    )

    observation = WellsFargoStatementExtractor().extract(
        _write_pdf(tmp_path, canonical, name="fragmented-canonical-money.pdf"),
        document_ordinal=1,
        document=_monthly_document(),
    )

    assert observation.balances[0].amount == Decimal("1000.00")
    assert any(token.text == "1,000.00" for token in observation.page_evidence[0].tokens)

    malformed = _monthly_pages()
    malformed[2].extend(
        [
            (72, 480, "Unsupported"),
            (145, 480, "1"),
            (151, 480, ","),
            (154, 480, "000"),
            (172, 480, "."),
            (175, 480, "000"),
        ]
    )
    compact_iso = _monthly_pages()
    compact_iso[2].extend(
        [
            (72, 480, "Unsupported"),
            (145, 480, "USD"),
            (165, 480, "1.00"),
        ]
    )
    lowercase_compact_iso_balance = _monthly_pages()
    lowercase_compact_iso_balance[0][4] = (
        72,
        570,
        "Opening Balance Start of Day Excludes Pending 01/01/2026",
    )
    lowercase_compact_iso_balance[0].extend([(390, 570, "usd"), (410, 570, "1.00")])

    for name, pages, page_number in (
        ("fragmented-malformed-money.pdf", malformed, 3),
        ("fragmented-compact-iso-money.pdf", compact_iso, 3),
        ("fragmented-lowercase-iso-balance.pdf", lowercase_compact_iso_balance, 1),
    ):
        with pytest.raises(StatementExtractionError, match=rf"document 1 page {page_number}"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name=name),
                document_ordinal=1,
                document=_monthly_document(),
            )


def test_marker_lines_cannot_hide_unrecognized_financial_data(tmp_path: Path) -> None:
    boilerplate = _monthly_pages()
    boilerplate[2].append((72, 480, "Unrecognized 01/03/2026 10.00"))
    bare_balance = _monthly_pages()
    bare_balance[2].append((72, 480, "Balance 1100"))
    grouped_bare_balance = _monthly_pages()
    grouped_bare_balance[2].append((72, 480, "Balance 1_100"))
    status = _monthly_pages()
    status[1][11] = (72, 480, "Pending 01/29/2026")
    status[1].insert(12, (390, 480, "60.00"))
    footer = _monthly_pages()
    footer[1][15] = (72, 380, "End of Activity 01/28/2026")
    footer[1].insert(16, (390, 380, "60.00"))

    for name, pages, page_number in (
        ("financial-boilerplate.pdf", boilerplate, 3),
        ("bare-balance-boilerplate.pdf", bare_balance, 3),
        ("grouped-bare-balance-boilerplate.pdf", grouped_bare_balance, 3),
        ("financial-status-marker.pdf", status, 2),
        ("financial-footer-marker.pdf", footer, 2),
    ):
        with pytest.raises(StatementExtractionError, match=rf"document 1 page {page_number}"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name=name),
                document_ordinal=1,
                document=_monthly_document(),
            )


def test_malformed_monetary_like_tokens_fail_closed(tmp_path: Path) -> None:
    balance_page = _monthly_pages()
    balance_page[0].append((72, 500, "Unrecognized $10.0"))
    exponential_balance = _monthly_pages()
    exponential_balance[0].append((72, 500, "Unrecognized 1E+5"))
    recognized_balance = _monthly_pages()
    recognized_balance[0][4] = (
        72,
        570,
        "Opening Balance Start of Day Excludes Pending 01/01/2026 1,000.00 $10.0",
    )
    statement_metadata = _monthly_pages()
    statement_metadata[0][2] = (
        72,
        660,
        "Statement Period 01/01/2026 01/31/2026 $1,000.000",
    )
    boilerplate = _monthly_pages()
    boilerplate[2].append((72, 480, "Unsupported $1,000.000"))
    activity_preamble = _monthly_pages()
    activity_preamble[1].insert(3, (72, 630, "Unsupported -10.00"))
    activity_row = _monthly_pages()
    activity_row[1].insert(10, (275, 540, "Malformed $10.0"))
    status = _monthly_pages()
    status[1][11] = (72, 480, "Pending $1,000.000")
    footer = _monthly_pages()
    footer[1][15] = (72, 380, "End of Activity -10.00")
    post_footer = _monthly_pages()
    post_footer[1].append((72, 320, "Unsupported $10.0"))

    assert all(
        bank_statements._is_monetary_like_text(value)
        for value in ("$10.0", "$1,000.000", "-10.00", "1E+5")
    )

    for name, pages, page_number in (
        ("malformed-balance-money.pdf", balance_page, 1),
        ("exponential-balance-money.pdf", exponential_balance, 1),
        ("malformed-recognized-balance-money.pdf", recognized_balance, 1),
        ("malformed-statement-metadata-money.pdf", statement_metadata, 1),
        ("malformed-boilerplate-money.pdf", boilerplate, 3),
        ("malformed-preamble-money.pdf", activity_preamble, 2),
        ("malformed-row-money.pdf", activity_row, 2),
        ("malformed-status-money.pdf", status, 2),
        ("malformed-footer-money.pdf", footer, 2),
        ("malformed-post-footer-money.pdf", post_footer, 2),
    ):
        with pytest.raises(StatementExtractionError, match=rf"document 1 page {page_number}"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name=name),
                document_ordinal=1,
                document=_monthly_document(),
            )


def test_compact_iso_currency_candidates_fail_closed(tmp_path: Path) -> None:
    activity_description = _monthly_pages()
    activity_description[1].insert(10, (275, 540, "USD1.00"))
    lowercase_activity_description = _monthly_pages()
    lowercase_activity_description[1].insert(10, (275, 540, "usd1.00"))
    footer = _monthly_pages()
    footer[1][15] = (72, 380, "End of Activity USD$1.00")
    mixed_footer = _monthly_pages()
    mixed_footer[1][15] = (72, 380, "End of Activity uSd$1.00")
    boilerplate = _monthly_pages()
    boilerplate[2].append((72, 480, "Unsupported USD$1.00"))

    assert all(
        bank_statements._is_monetary_like_text(value)
        for value in ("USD1.00", "USD$1.00", "usd1.00", "uSd$1.00", "€1.00")
    )

    assert not any(bank_statements._is_monetary_like_text(value) for value in ("APR2026", "ABC123"))

    for name, pages, page_number in (
        ("compact-iso-activity-description.pdf", activity_description, 2),
        ("lowercase-iso-activity-description.pdf", lowercase_activity_description, 2),
        ("compact-iso-footer.pdf", footer, 2),
        ("mixed-iso-footer.pdf", mixed_footer, 2),
        ("compact-iso-boilerplate.pdf", boilerplate, 3),
    ):
        with pytest.raises(StatementExtractionError, match=rf"document 1 page {page_number}"):
            WellsFargoStatementExtractor().extract(
                _write_pdf(tmp_path, pages, name=name),
                document_ordinal=1,
                document=_monthly_document(),
            )


def test_activity_header_financial_extras_fail_closed(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[1].extend(
        [
            (280, 610, "01/15/2026"),
            (340, 610, "99.99"),
        ]
    )

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, pages, name="financial-activity-header.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_empty_activity_marker_cannot_coexist_with_transaction_rows(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[1].insert(-2, (72, 420, "No Activity"))

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, pages, name="contradictory-empty-activity.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_activity_page_requires_rows_or_an_exact_empty_activity_marker(tmp_path: Path) -> None:
    empty = _monthly_pages()
    empty[1] = [*empty[1][:7], *empty[1][15:]]
    supported_empty = _monthly_pages()
    supported_empty[1] = [
        *supported_empty[1][:7],
        (72, 570, "No Activity"),
        *supported_empty[1][15:],
    ]

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, empty, name="truncated-empty-activity-table.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )

    observation = WellsFargoStatementExtractor().extract(
        _write_pdf(tmp_path, supported_empty, name="supported-empty-activity-table.pdf"),
        document_ordinal=1,
        document=_monthly_document(),
    )
    assert observation.transactions == ()


def test_monthly_document_requires_its_activity_page(tmp_path: Path) -> None:
    pages = _monthly_pages()
    summary_only = [pages[0], pages[2]]

    with pytest.raises(StatementExtractionError, match=r"document 1"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, summary_only, name="truncated-monthly-summary-only.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_page_fingerprint_controls_cannot_come_from_activity_descriptions(tmp_path: Path) -> None:
    pages = _monthly_pages()
    pages[1][0] = (72, 720, "Fictional Ledger")
    pages[1][1] = (72, 690, "Generic Activity Report")
    pages[1][9] = (150, 540, "Wells Fargo Monthly Account Activity")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, pages, name="description-supplied-page-fingerprint.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )


@pytest.mark.parametrize(
    ("line_index", "replacement"),
    (
        (
            4,
            "Opening Balance Start of Day Excludes Pending 01/02/2026 1,000.00",
        ),
        (
            5,
            "Closing Balance End of Day Excludes Pending 01/30/2026 1,250.00",
        ),
    ),
)
def test_monthly_boundary_balances_must_match_the_statement_period(
    tmp_path: Path, line_index: int, replacement: str
) -> None:
    pages = _monthly_pages()
    left, bottom, _ = pages[0][line_index]
    pages[0][line_index] = (left, bottom, replacement)

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, pages, name=f"wrong-monthly-boundary-{line_index}.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_native_worker_replays_privacy_scrub_before_serializing_a_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker must reject a forged raw layout before bytes cross its IPC boundary."""

    class _Request:
        def __init__(self) -> None:
            self.closed = False

        def recv_bytes(self, maxlength: int | None = None) -> bytes:
            assert maxlength == bank_statements.MAX_PDF_BYTES
            return b"fictional"

        def send_bytes(self, value: bytes) -> None:
            del value
            raise AssertionError("the worker request endpoint must not send")

        def close(self) -> None:
            self.closed = True

    class _Response:
        def __init__(self) -> None:
            self.closed = False
            self.messages: list[bytes] = []

        def recv_bytes(self, maxlength: int | None = None) -> bytes:
            del maxlength
            raise AssertionError("the worker response endpoint must not receive")

        def send_bytes(self, value: bytes) -> None:
            self.messages.append(value)

        def close(self) -> None:
            self.closed = True

    request = _Request()
    response = _Response()
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    box = BoundingBox(Decimal("0"), Decimal("0"), Decimal("1"), Decimal("1"))
    forged_raw_page = bank_statements._NativePage(
        page_number=1,
        tokens=(bank_statements._LayoutToken(1, "Account Ref 1234", box, 1),),
        lines=(bank_statements._NativeLine(1, (1,)),),
    )

    monkeypatch.setattr(bank_statements, "_require_pdfium", lambda: object())
    monkeypatch.setattr(
        bank_statements,
        "_extract_native_pages",
        lambda *_args, **_kwargs: (forged_raw_page,),
    )

    bank_statements._native_page_extraction_after_limits(
        request,
        response,
        document_ordinal=1,
        limits=limits,
    )

    assert response.messages == [b'{"status":"rejected","page_number":1}']
    assert b"1234" not in response.messages[0]
    assert request.closed
    assert response.closed


def test_native_worker_timeout_terminates_the_disposable_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_unblocked = threading.Event()

    class _RequestSender:
        def __init__(self) -> None:
            self.closed = False

        def send_bytes(self, value: bytes) -> None:
            assert value == b"fictional"

        def recv_bytes(self, maxlength: int | None = None) -> bytes:
            del maxlength
            raise AssertionError("the parent request endpoint must not be read")

        def close(self) -> None:
            self.closed = True

    class _Receiver:
        def __init__(self) -> None:
            self.closed = False

        def recv_bytes(self, maxlength: int | None = None) -> bytes:
            del maxlength
            assert response_unblocked.wait(timeout=5)
            raise EOFError("synthetic partial worker frame interrupted by termination")

        def send_bytes(self, value: bytes) -> None:
            del value
            raise AssertionError("the parent response endpoint must not write")

        def close(self) -> None:
            self.closed = True

    class _Worker:
        def __init__(self) -> None:
            self.alive = True
            self.terminated = False
            self.closed = False

        def join(self, timeout: float | None = None) -> None:
            assert timeout is None or timeout >= 0

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False
            response_unblocked.set()

        def kill(self) -> None:
            raise AssertionError("terminate should be sufficient for this synthetic worker")

        def close(self) -> None:
            self.closed = True

    request_sender = _RequestSender()
    receiver = _Receiver()
    worker = _Worker()
    monkeypatch.setattr(bank_statements, "MAX_NATIVE_EXTRACTION_SECONDS", 1)
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    monkeypatch.setattr(
        bank_statements,
        "_start_native_page_worker",
        lambda *_: (request_sender, receiver, worker),
    )

    with pytest.raises(StatementExtractionError, match=r"document 1"):
        bank_statements._extract_native_pages_in_worker(
            b"fictional",
            document_ordinal=1,
            limits=limits,
        )

    assert request_sender.closed
    assert receiver.closed
    assert worker.terminated
    assert worker.closed


def test_extractor_arms_the_ready_sandbox_before_the_broker_reads_a_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The private source reader must be downstream of the ready sandbox gate."""

    events: list[str] = []

    class _Connection:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class _Worker:
        def __init__(self) -> None:
            self.alive = True
            self.terminated = False
            self.closed = False

        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False

        def kill(self) -> None:
            raise AssertionError("terminate should be sufficient for the prepared worker")

        def close(self) -> None:
            self.closed = True

    request_sender = _Connection()
    response_receiver = _Connection()
    worker = _Worker()
    monkeypatch.setattr(bank_statements, "_require_pdfium_distribution", lambda: None)

    def start_worker(
        document_ordinal: int, limits: bank_statements._NativeExtractionLimits
    ) -> tuple[_Connection, _Connection, _Worker]:
        assert document_ordinal == 1
        assert limits.max_pdf_bytes == bank_statements.MAX_PDF_BYTES
        events.append("sandbox-ready")
        return request_sender, response_receiver, worker

    def read_private_pdf(source: Path, *, maximum_bytes: int) -> bytes:
        assert source == tmp_path / "not-read.pdf"
        assert maximum_bytes == bank_statements.MAX_PDF_BYTES
        assert events == ["sandbox-ready"]
        events.append("private-read")
        raise PrivateInputError

    monkeypatch.setattr(bank_statements, "_start_native_page_worker", start_worker)
    monkeypatch.setattr(bank_statements, "_read_bounded_pdf", read_private_pdf)

    with pytest.raises(PrivateInputError):
        WellsFargoStatementExtractor().extract(
            tmp_path / "not-read.pdf",
            document_ordinal=1,
            document=_monthly_document(),
        )

    assert events == ["sandbox-ready", "private-read"]
    assert request_sender.closed
    assert response_receiver.closed
    assert worker.terminated
    assert worker.closed


def test_extractor_never_reads_a_pdf_if_the_sandbox_cannot_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sandbox startup failures are a strict gate, not a best-effort optimization."""

    read_attempted = False
    monkeypatch.setattr(bank_statements, "_require_pdfium_distribution", lambda: None)

    def start_failure(*_args: object, **_kwargs: object) -> None:
        raise bank_statements._page_error(1)

    def unexpected_private_read(*_args: object, **_kwargs: object) -> bytes:
        nonlocal read_attempted
        read_attempted = True
        raise AssertionError("the broker must not open a statement before sandbox readiness")

    monkeypatch.setattr(bank_statements, "_start_native_page_worker", start_failure)
    monkeypatch.setattr(bank_statements, "_read_bounded_pdf", unexpected_private_read)

    with pytest.raises(StatementExtractionError, match=r"document 1"):
        WellsFargoStatementExtractor().extract(
            tmp_path / "never-opened.pdf",
            document_ordinal=1,
            document=_monthly_document(),
        )

    assert not read_attempted


def test_successful_worker_reply_is_rejected_if_sandbox_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A private parse is not accepted when the disposable sandbox cannot be reaped."""

    class _RequestSender:
        def __init__(self) -> None:
            self.closed = False

        def send_bytes(self, value: bytes) -> None:
            assert value == b"fictional"

        def close(self) -> None:
            self.closed = True

    class _ResponseReceiver:
        def __init__(self) -> None:
            self.closed = False

        def recv_bytes(self, maxlength: int | None = None) -> bytes:
            assert maxlength == bank_statements.MAX_NATIVE_PAGE_WIRE_BYTES
            return b'{"status":"ok"}'

        def close(self) -> None:
            self.closed = True

    class _Worker:
        def join(self, timeout: float | None = None) -> None:
            del timeout

        def is_alive(self) -> bool:
            return False

        def terminate(self) -> None:
            raise AssertionError("an exited worker should not be terminated")

        def kill(self) -> None:
            raise AssertionError("an exited worker should not be killed")

        def close(self) -> None:
            raise OSError("synthetic teardown fault")

    request_sender = _RequestSender()
    response_receiver = _ResponseReceiver()
    worker = _Worker()
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    monkeypatch.setattr(bank_statements, "_deserialize_native_pages", lambda *_args: ())

    with pytest.raises(StatementExtractionError, match=r"document 1"):
        bank_statements._extract_native_pages_in_worker(
            b"fictional",
            document_ordinal=1,
            limits=limits,
            prepared_worker=(request_sender, response_receiver, worker),
        )

    assert request_sender.closed
    assert response_receiver.closed


def test_parent_rejects_a_worker_layout_that_contains_an_account_identifier() -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [[1, "1234567890", "0.1", "0.1", "0.2", "0.2", 1]],
                    "lines": [[1, [1]]],
                    "provisional_contact_phone_token_ordinals": [],
                }
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


def test_parent_scrubs_a_unicode_slash_abbreviated_account_identifier() -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [
                        [1, "A\u2215C", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "No.", "0.3", "0.1", "0.4", "0.2", 1],
                        [3, "1234", "0.5", "0.1", "0.6", "0.2", 1],
                        [4, "Fictional", "0.1", "0.3", "0.2", "0.4", 2],
                    ],
                    "lines": [[1, [1, 2, 3]], [2, [4]]],
                    "provisional_contact_phone_token_ordinals": [],
                }
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    pages = bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)

    assert all(token.text != "1234" for page in pages for token in page.tokens)


@pytest.mark.parametrize(
    "fragments",
    (
        ("Acc", "ountRef", "2266"),
        ("Account Summary", "R\u0435f", "2266"),
        ("2266", "Acc", "ount", "Ref"),
        ("Acc", "Page 2", "ount", "Ref", "2266"),
        ("A/C Ref", "1", "O", "2", "3"),
    ),
)
def test_parent_rejects_forged_residual_account_label_variants(
    fragments: tuple[str, ...],
) -> None:
    """The parent repeats residual-label screening on a bounded forged worker reply."""

    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    tokens = [
        [
            ordinal,
            fragment,
            "0.1",
            str(Decimal(ordinal) / Decimal("10")),
            "0.2",
            str(Decimal(ordinal) / Decimal("10") + Decimal("0.05")),
            ordinal,
        ]
        for ordinal, fragment in enumerate(fragments, start=1)
    ]
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": tokens,
                    "lines": [[ordinal, [ordinal]] for ordinal in range(1, len(tokens) + 1)],
                    "provisional_contact_phone_token_ordinals": [],
                }
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


@pytest.mark.parametrize(
    "line_fragments",
    (
        (("Primary Account Activity Ref 10.00",),),
        (("Fictional Account Activity Fee Ref 10.00",),),
        (("Primary Account Acti",), ("vity Ref 1234",)),
        (("Foo Account Numbering Ref 10.00",),),
        (("Foo Account Activity",), ("No",), ("10.00",)),
        (("Account Summary",), ("N",), ("o",), ("10.00",)),
        (("Account Summary",), ("N0",), ("10.00",)),
        (("Account Activity",), ("No=",), ("10.00",)),
        (("Account Activity",), ("Ref=",), ("10.00",)),
        (("Account Activity",), ("[#]",), ("10.00",)),
        (("Account Activity",), ("#.",), ("10.00",)),
        (("Account Activity",), ("\u266f",), ("10.00",)),
        (("Account Activity",), ("R\u212ef 10.00",)),
        (("Account Activity",), ("Nurnber 10.00",)),
        (("Acc\u25cbunt Number 10.00",),),
        (("Foo Account R\u212ef 1234",),),
        (("Foo Account R\u20acf 1234",),),
        (("Foo Account Nurnber 1234",),),
        (("Foo Acc\u2116ount Ref 1234",),),
        (("Foo A0/C Ref 1234",),),
        (("Foo A3/C Ref 1234",),),
        (("A",), ("Page 2",), ("/C Ref",), ("10.00",)),
        (("Foo Acc",), ("P\u212ege 2",), ("ount Ref 1234",)),
        (("Foo Acc",), ("\u2014 2 \u2014",), ("ount Ref 1234",)),
        (("Foo Acc\u2022ount Ref 1234",),),
        (("Account end",), ("Page 2",), ("ing in 1234",)),
    ),
)
def test_parent_rejects_forged_public_account_phrase_variants(
    line_fragments: tuple[tuple[str, ...], ...],
) -> None:
    """The parent repeats public-phrase state screening on a forged worker reply."""

    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    tokens: list[list[object]] = []
    lines: list[list[object]] = []
    ordinal = 1
    for line_ordinal, fragments in enumerate(line_fragments, start=1):
        line_token_ordinals: list[int] = []
        for fragment in fragments:
            tokens.append(
                [
                    ordinal,
                    fragment,
                    "0.1",
                    str(Decimal(ordinal) / Decimal("10")),
                    "0.2",
                    str(Decimal(ordinal) / Decimal("10") + Decimal("0.05")),
                    line_ordinal,
                ]
            )
            line_token_ordinals.append(ordinal)
            ordinal += 1
        lines.append([line_ordinal, line_token_ordinals])
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": tokens,
                    "lines": lines,
                    "provisional_contact_phone_token_ordinals": [],
                }
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


def test_parent_rejects_a_forged_public_account_phrase_split_across_pages() -> None:
    """Page replay preserves a public-suffix prefix until a later short value is screened."""

    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [[1, "Foo Account Acti", "0.1", "0.1", "0.2", "0.2", 1]],
                    "lines": [[1, [1]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
                {
                    "page_number": 2,
                    "tokens": [
                        [1, "Page 2", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "vity Ref", "0.1", "0.3", "0.2", "0.4", 2],
                        [3, "1234", "0.1", "0.5", "0.2", "0.6", 3],
                    ],
                    "lines": [[1, [1]], [2, [2]], [3, [3]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


def test_parent_rejects_a_forged_public_qualifier_visual_split_across_pages() -> None:
    """A visual qualifier split and intervening page header cannot retain an identifier."""

    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [
                        [1, "Account Activity", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "R", "0.1", "0.3", "0.2", "0.4", 2],
                    ],
                    "lines": [[1, [1]], [2, [2]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
                {
                    "page_number": 2,
                    "tokens": [
                        [1, "P\u0430ge 2", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "\u212e", "0.1", "0.3", "0.2", "0.4", 2],
                        [3, "f", "0.3", "0.3", "0.4", "0.4", 2],
                        [4, "10.00", "0.1", "0.5", "0.2", "0.6", 3],
                    ],
                    "lines": [[1, [1]], [2, [2, 3]], [3, [4]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


def test_parent_rejects_a_forged_split_no_qualifier_across_pages() -> None:
    """A split/leet ``No`` qualifier remains unsafe across a page header."""

    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [
                        [1, "Account Summary", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "N", "0.1", "0.3", "0.2", "0.4", 2],
                    ],
                    "lines": [[1, [1]], [2, [2]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
                {
                    "page_number": 2,
                    "tokens": [
                        [1, "Page \u2014 2", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "\u043e", "0.1", "0.3", "0.2", "0.4", 2],
                        [3, "10.00", "0.1", "0.5", "0.2", "0.6", 3],
                    ],
                    "lines": [[1, [1]], [2, [2]], [3, [3]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


def test_parent_rejects_a_forged_abbreviated_account_split_across_page_header() -> None:
    """A page header cannot break an ``A/C`` label before its short value is screened."""

    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [[1, "A", "0.1", "0.1", "0.2", "0.2", 1]],
                    "lines": [[1, [1]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
                {
                    "page_number": 2,
                    "tokens": [
                        [1, "P\u212ege 2", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "/C Ref", "0.1", "0.3", "0.2", "0.4", 2],
                        [3, "10.00", "0.1", "0.5", "0.2", "0.6", 3],
                    ],
                    "lines": [[1, [1]], [2, [2]], [3, [3]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


@pytest.mark.parametrize(
    ("start", "separator", "end"),
    (
        ("A", "\u2215", "C"),
        ("A", "\u2571", "C"),
        ("4", "/", "C"),
        ("@", "/", "C"),
        ("\u2200", "/", "C"),
        ("A", "_", "C"),
        ("A", "/", "("),
        ("A", "/", "\u2282"),
        ("A", "/", "\u228a"),
        ("A", "/", "\u228f"),
        ("A", "/", "\u2291"),
        ("A", "/", "\u2329"),
        ("A", "/", "\u3008"),
        ("A", "/", "\u27c3"),
        ("A", "/", "\u27e8"),
    ),
)
def test_parent_rejects_a_noncanonical_account_abbreviation_split_across_pages(
    start: str, separator: str, end: str
) -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [
                        [1, start, "0.1", "0.1", "0.2", "0.2", 1],
                        [2, separator, "0.3", "0.1", "0.4", "0.2", 2],
                    ],
                    "lines": [[1, [1]], [2, [2]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
                {
                    "page_number": 2,
                    "tokens": [
                        [1, "Header", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, end, "0.1", "0.3", "0.2", "0.4", 2],
                        [3, "Ref", "0.3", "0.3", "0.4", "0.4", 2],
                        [4, "1234", "0.1", "0.5", "0.2", "0.6", 3],
                    ],
                    "lines": [[1, [1]], [2, [2, 3]], [3, [4]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


def test_parent_rejects_a_short_identifier_before_an_account_abbreviation() -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [[1, "12/34", "0.1", "0.1", "0.2", "0.2", 1]],
                    "lines": [[1, [1]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
                {
                    "page_number": 2,
                    "tokens": [
                        [1, "A/C", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "Ref", "0.3", "0.1", "0.4", "0.2", 1],
                    ],
                    "lines": [[1, [1, 2]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


@pytest.mark.parametrize(
    "identifier",
    (
        "12 34",
        "12\u00a034",
        "ID 12 34",
        "1O23",
        "1I23",
        "12S4",
        "l234",
        "12B4",
        "1Z34",
        "1\u039f23",
        "12\u04054",
    ),
)
def test_parent_rejects_short_identifier_hidden_in_account_abbreviation_context(
    identifier: str,
) -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [
                        [1, "A/C", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "Ref", "0.3", "0.1", "0.4", "0.2", 1],
                        [3, identifier, "0.1", "0.3", "0.2", "0.4", 2],
                    ],
                    "lines": [[1, [1, 2]], [2, [3]]],
                    "provisional_contact_phone_token_ordinals": [],
                }
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


@pytest.mark.parametrize("phone_before_label", (False, True))
def test_parent_rejects_provisional_public_phone_in_account_abbreviation_context(
    phone_before_label: bool,
) -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    if phone_before_label:
        tokens = [
            [1, "Member FDIC", "0.1", "0.1", "0.3", "0.2", 1],
            [2, "Call", "0.1", "0.3", "0.2", "0.4", 2],
            [3, "800-555-1234", "0.3", "0.3", "0.5", "0.4", 2],
            [4, "A/C", "0.1", "0.5", "0.2", "0.6", 3],
            [5, "Ref", "0.3", "0.5", "0.4", "0.6", 3],
        ]
        lines = [[1, [1]], [2, [2, 3]], [3, [4, 5]]]
        phone_ordinal = 3
    else:
        tokens = [
            [1, "Member FDIC", "0.1", "0.1", "0.3", "0.2", 1],
            [2, "A/C", "0.1", "0.3", "0.2", "0.4", 2],
            [3, "Ref", "0.3", "0.3", "0.4", "0.4", 2],
            [4, "Call", "0.1", "0.5", "0.2", "0.6", 3],
            [5, "800-555-1234", "0.3", "0.5", "0.5", "0.6", 3],
        ]
        lines = [[1, [1]], [2, [2, 3]], [3, [4, 5]]]
        phone_ordinal = 5
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": tokens,
                    "lines": lines,
                    "provisional_contact_phone_token_ordinals": [phone_ordinal],
                }
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


def test_parent_rejects_cross_page_account_identifier_fragments() -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [
                        [1, "Account", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "Ref", "0.3", "0.1", "0.4", "0.2", 1],
                    ],
                    "lines": [[1, [1, 2]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
                {
                    "page_number": 2,
                    "tokens": [
                        [1, "Header", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "1234", "0.1", "0.3", "0.2", "0.4", 2],
                    ],
                    "lines": [[1, [1]], [2, [2]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


def test_parent_rejects_an_ascii_account_word_split_across_a_page_break() -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [
                        [1, "A", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "c", "0.3", "0.1", "0.4", "0.2", 1],
                        [3, "c", "0.5", "0.1", "0.6", "0.2", 1],
                    ],
                    "lines": [[1, [1, 2, 3]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
                {
                    "page_number": 2,
                    "tokens": [
                        [1, "Headerount", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "Ref", "0.3", "0.1", "0.4", "0.2", 1],
                        [3, "Header", "0.1", "0.3", "0.2", "0.4", 2],
                        [4, "1234", "0.1", "0.5", "0.2", "0.6", 3],
                    ],
                    "lines": [[1, [1, 2]], [2, [3]], [3, [4]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


def test_parent_rejects_a_short_identifier_split_after_a_page_break() -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [
                        [1, "Account", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "Ref", "0.3", "0.1", "0.4", "0.2", 1],
                    ],
                    "lines": [[1, [1, 2]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
                {
                    "page_number": 2,
                    "tokens": [
                        [1, "Header", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "12", "0.1", "0.3", "0.2", "0.4", 2],
                        [3, "34", "0.1", "0.5", "0.2", "0.6", 3],
                    ],
                    "lines": [[1, [1]], [2, [2]], [3, [3]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


def test_parent_rejects_a_confusable_account_word_across_a_page_break() -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [
                        [1, "A", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "\u0441", "0.3", "0.1", "0.4", "0.2", 1],
                        [3, "\u0441", "0.5", "0.1", "0.6", "0.2", 1],
                    ],
                    "lines": [[1, [1, 2, 3]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
                {
                    "page_number": 2,
                    "tokens": [
                        [1, "Header", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "\u03bfunt", "0.1", "0.3", "0.2", "0.4", 2],
                        [3, "Ref", "0.3", "0.3", "0.4", "0.4", 2],
                        [4, "Header", "0.1", "0.5", "0.2", "0.6", 3],
                        [5, "1234", "0.1", "0.7", "0.2", "0.8", 4],
                    ],
                    "lines": [[1, [1]], [2, [2, 3]], [3, [4]], [4, [5]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


def test_parent_rejects_a_non_ascii_digit_like_account_label_across_a_page_break() -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    forged_worker_payload = json.dumps(
        {
            "status": "ok",
            "pages": [
                {
                    "page_number": 1,
                    "tokens": [
                        [1, "Acc\u0660unt", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "Ref", "0.3", "0.1", "0.4", "0.2", 1],
                    ],
                    "lines": [[1, [1, 2]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
                {
                    "page_number": 2,
                    "tokens": [
                        [1, "Header", "0.1", "0.1", "0.2", "0.2", 1],
                        [2, "1234", "0.1", "0.3", "0.2", "0.4", 2],
                    ],
                    "lines": [[1, [1]], [2, [2]]],
                    "provisional_contact_phone_token_ordinals": [],
                },
            ],
        },
        separators=(",", ":"),
    ).encode("ascii")

    with pytest.raises(StatementExtractionError, match=r"document 1"):
        bank_statements._deserialize_native_pages(forged_worker_payload, 1, limits)


def test_limits_and_missing_extra_fail_without_disclosing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_pdf(tmp_path, _monthly_pages())
    monkeypatch.setattr(bank_statements, "MAX_PDF_BYTES", 1)
    with pytest.raises(PrivateInputError):
        WellsFargoStatementExtractor().extract(
            source,
            document_ordinal=1,
            document=_monthly_document(),
        )

    def _missing_pdfium(_: str) -> str:
        raise metadata.PackageNotFoundError("pypdfium2")

    monkeypatch.setattr(bank_statements.metadata, "version", _missing_pdfium)
    with pytest.raises(SlidesDependencyError):
        WellsFargoStatementExtractor().extract(
            source,
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_reader_rechecks_link_segments_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_pdf(tmp_path, _monthly_pages(), name="fictional-post-open-reparse.pdf")
    recheck_paths: list[Path] = []

    def _changed_to_link(path: Path) -> None:
        recheck_paths.append(path)
        raise PrivateInputError("synthetic changed link segment")

    monkeypatch.setattr(bank_statements, "_assert_no_link_segments", _changed_to_link)
    with pytest.raises(PrivateInputError, match=r"private input does not satisfy"):
        bank_statements._read_bounded_pdf(source)

    assert recheck_paths


def test_native_character_limit_matches_the_wave_1_contract() -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)

    assert bank_statements.MAX_NATIVE_CHARACTERS == 2_000_000
    assert bank_statements._HARD_MAX_NATIVE_CHARACTERS == 2_000_000
    assert limits.max_characters == 2_000_000


def test_transaction_row_limit_cannot_exceed_its_hard_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as limited:
        limited.setattr(
            bank_statements,
            "MAX_TRANSACTION_ROWS",
            bank_statements._HARD_MAX_TRANSACTION_ROWS + 1,
        )

        with pytest.raises(StatementExtractionError, match=r"document 1"):
            bank_statements._native_extraction_limits(document_ordinal=1)


def test_hard_page_character_pixel_and_row_limits_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extractor = WellsFargoStatementExtractor()
    normal = _write_pdf(tmp_path, _monthly_pages(), name="normal.pdf")
    oversized_page_count = _write_pdf(
        tmp_path,
        [[(72, 720, "too short to matter because page count is checked first")]]
        * (bank_statements.MAX_PDF_PAGES + 1),
        name="too-many-pages.pdf",
    )
    undersized_native = _write_pdf(
        tmp_path,
        [[(72, 720, "tiny")]],
        name="too-few-native-characters.pdf",
    )

    with pytest.raises(StatementExtractionError, match=r"document 1"):
        extractor.extract(
            oversized_page_count,
            document_ordinal=1,
            document=_monthly_document(),
        )
    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        extractor.extract(
            undersized_native,
            document_ordinal=1,
            document=_monthly_document(),
        )
    with monkeypatch.context() as limited:
        limited.setattr(bank_statements, "MAX_NATIVE_CHARACTERS", 1)
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            extractor.extract(normal, document_ordinal=1, document=_monthly_document())
    with monkeypatch.context() as limited:
        limited.setattr(bank_statements, "MAX_RENDERED_PIXELS_PER_PAGE", 1)
        with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
            extractor.extract(normal, document_ordinal=1, document=_monthly_document())
    with monkeypatch.context() as limited:
        original_draft = bank_statements._activity_draft_for_line
        draft_calls = 0

        def _counting_draft(*args: object, **kwargs: object) -> object:
            nonlocal draft_calls
            draft_calls += 1
            return original_draft(*args, **kwargs)

        limited.setattr(bank_statements, "MAX_TRANSACTION_ROWS", 1)
        limited.setattr(bank_statements, "_activity_draft_for_line", _counting_draft)
        with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
            extractor.extract(normal, document_ordinal=1, document=_monthly_document())
        assert draft_calls == 1


def test_document_row_budget_preflights_before_over_cap_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = _monthly_pages()
    pages.insert(2, _monthly_pages()[1])
    source = _write_pdf(tmp_path, pages, name="row-budget-preflight.pdf")
    original_draft = bank_statements._activity_draft_for_line
    draft_calls = 0

    def _counting_draft(*args: object, **kwargs: object) -> object:
        nonlocal draft_calls
        draft_calls += 1
        return original_draft(*args, **kwargs)

    with monkeypatch.context() as limited:
        limited.setattr(bank_statements, "MAX_TRANSACTION_ROWS", 3)
        limited.setattr(bank_statements, "_activity_draft_for_line", _counting_draft)
        with pytest.raises(StatementExtractionError, match=r"document 1 page 3"):
            WellsFargoStatementExtractor().extract(
                source,
                document_ordinal=1,
                document=_monthly_document(),
            )

    assert draft_calls == 3


def test_aggregate_character_limit_preflights_before_second_page_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _TextPage:
        def __init__(self, text: str, *, reject_decode: bool) -> None:
            self.text = text
            self.reject_decode = reject_decode
            self.decoded = False

        def count_chars(self) -> int:
            return len(self.text)

        def get_text_range(self, index: int = 0, count: int = -1) -> str:
            del index, count
            self.decoded = True
            if self.reject_decode:
                raise AssertionError("the over-budget page must not be decoded")
            return self.text

        def get_charbox(self, index: int, loose: bool = False) -> tuple[float, float, float, float]:
            del index, loose
            return (0.0, 0.0, 10.0, 10.0)

        def close(self) -> None:
            return None

    class _Page:
        def __init__(self, text_page: _TextPage) -> None:
            self.text_page = text_page

        def get_size(self) -> tuple[float, float]:
            return (612.0, 792.0)

        def get_textpage(self) -> _TextPage:
            return self.text_page

        def close(self) -> None:
            return None

    class _Document:
        def __init__(self, pages: tuple[_Page, ...]) -> None:
            self.pages = pages

        def __len__(self) -> int:
            return len(self.pages)

        def get_page(self, index: int) -> _Page:
            return self.pages[index]

        def close(self) -> None:
            return None

    first_text_page = _TextPage("a" * 100, reject_decode=False)
    second_text_page = _TextPage("b" * 100, reject_decode=True)
    document = _Document((_Page(first_text_page), _Page(second_text_page)))
    monkeypatch.setattr(bank_statements, "MAX_NATIVE_CHARACTERS", 160)
    monkeypatch.setattr(bank_statements, "_open_pdf_document", lambda *_: document)

    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        bank_statements._extract_native_pages(object(), b"fictional", document_ordinal=1)

    assert first_text_page.decoded is True
    assert second_text_page.decoded is False


def test_conflicting_marker_and_partial_header_fail_closed(tmp_path: Path) -> None:
    conflicting = _write_pdf(
        tmp_path,
        [
            [
                (72, 720, "Wells Fargo"),
                (72, 690, "Monthly Account Statement"),
                (72, 660, "Current Account Balance"),
                (72, 630, "Statement Period 01/01/2026 01/31/2026"),
                (72, 590, "Opening Balance Start of Day Excludes Pending 01/01/2026 1,000.00"),
                (72, 550, "Fictional contradictory marker text must fail the versioned contract."),
            ]
        ],
        name="conflicting.pdf",
    )
    partial_header = _write_pdf(
        tmp_path,
        [
            _monthly_pages()[0],
            [
                (72, 720, "Wells Fargo"),
                (72, 690, "Monthly Account Activity"),
                (72, 660, "Statement Period 01/01/2026 01/31/2026"),
                (72, 610, "Date"),
                (150, 610, "Description"),
                (390, 610, "Debits"),
                (72, 560, "Fictional partial headers cannot create a transaction table."),
                (72, 520, "Additional fictional text ensures the native-character gate is passed."),
            ],
        ],
        name="partial-header.pdf",
    )
    unrelated_boilerplate = _write_pdf(
        tmp_path,
        [
            [
                (72, 720, "Member FDIC"),
                (72, 680, "Equal Housing Lender"),
                (72, 640, "This generic fictional page lacks the required issuer marker."),
                (72, 600, "It must never become a recognized ignored page in the v1 contract."),
            ]
        ],
        name="unrelated-boilerplate.pdf",
    )
    near_miss = _monthly_pages()
    near_miss[0][1] = (72, 690, "Monthly Account Notice")
    duplicate_balance = _monthly_pages()
    duplicate_balance[0].insert(
        6,
        (72, 500, "Closing Balance End of Day Excludes Pending 01/31/2026 1,100.00"),
    )

    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        WellsFargoStatementExtractor().extract(
            conflicting,
            document_ordinal=1,
            document=_monthly_document(),
        )
    with pytest.raises(StatementExtractionError, match=r"document 1 page 2"):
        WellsFargoStatementExtractor().extract(
            partial_header,
            document_ordinal=1,
            document=_monthly_document(),
        )
    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        WellsFargoStatementExtractor().extract(
            unrelated_boilerplate,
            document_ordinal=1,
            document=_monthly_document(),
        )
    with pytest.raises(StatementExtractionError, match=r"document 1 page 1"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, near_miss, name="near-miss.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )
    with pytest.raises(StatementExtractionError, match=r"document 1"):
        WellsFargoStatementExtractor().extract(
            _write_pdf(tmp_path, duplicate_balance, name="duplicate-balance.pdf"),
            document_ordinal=1,
            document=_monthly_document(),
        )


def test_built_base_wheel_loads_cli_without_the_slides_extra(tmp_path: Path) -> None:
    """A clean wheel install proves PDFium is not imported by any existing command."""

    dist = tmp_path / "dist"
    environment = tmp_path / "base-install"
    subprocess.run(
        ["uv", "build", "--out-dir", str(dist)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["uv", "venv", str(environment), "--python", sys.executable],
        check=True,
        capture_output=True,
        text=True,
    )
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    wheel = next(dist.glob("*.whl"))
    subprocess.run(
        ["uv", "pip", "install", "--python", str(interpreter), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [
            str(interpreter),
            "-c",
            "import importlib.util; import pta_finance.cli; "
            "import pta_finance.treasurer_slides.bank_statements; "
            "assert importlib.util.find_spec('pypdfium2') is None; "
            "assert pta_finance.cli.build_parser() is not None",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
