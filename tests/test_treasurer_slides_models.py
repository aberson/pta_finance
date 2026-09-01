from __future__ import annotations

import logging
import os
import subprocess
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum
from pathlib import Path

import pytest

from pta_finance.treasurer_slides import models as treasurer_models
from pta_finance.treasurer_slides.models import (
    PRIVATE_JSON_MAX_BYTES,
    AccountRole,
    ActivityColumn,
    ActivityColumnBand,
    ActivityRowEvidence,
    ActivityStatusControl,
    ActivityTableEvidence,
    AdjustmentAction,
    BalanceBoundary,
    BalanceControlEvidence,
    BalanceKind,
    BalanceObservation,
    BalanceRowEvidence,
    BoundingBox,
    CashBasis,
    CashRole,
    ClassificationRule,
    ContractError,
    Direction,
    DocumentKind,
    DocumentSpec,
    EvidenceField,
    ExtractionMethod,
    InputManifest,
    MatcherKind,
    NormalizedTransaction,
    PageEvidence,
    PageFingerprint,
    PageKind,
    PairAction,
    PairResolution,
    ParseEvidence,
    PositionedToken,
    PrivateInputError,
    SafeSourceLocator,
    StatementObservation,
    TransactionAdjustment,
    TransactionSelector,
    TransactionStatus,
    TreasurerRules,
    assert_private_path_allowed,
    assign_occurrence_ordinals,
    build_semantic_key,
    build_source_row_id,
    canonical_json_bytes,
    canonical_json_text,
    canonical_sha256,
    load_input_manifest,
    load_rules,
    normalize_description,
    parse_iso_date,
    parse_nonnegative_money,
    parse_positive_money,
    resolve_private_relative_path,
)

SHA = "a" * 64
_PAGE_FINGERPRINTS = {
    PageKind.MONTHLY_SUMMARY: PageFingerprint.WELLS_FARGO_V1_MONTHLY_SUMMARY,
    PageKind.MONTHLY_ACTIVITY: PageFingerprint.WELLS_FARGO_V1_MONTHLY_ACTIVITY,
    PageKind.CURRENT_BALANCE: PageFingerprint.WELLS_FARGO_V1_CURRENT_BALANCE,
    PageKind.CURRENT_ACTIVITY: PageFingerprint.WELLS_FARGO_V1_CURRENT_ACTIVITY,
    PageKind.BOILERPLATE: PageFingerprint.WELLS_FARGO_V1_BOILERPLATE,
}


def _locator(
    row: int = 1,
    *,
    left: Decimal = Decimal("0.10"),
    top: Decimal = Decimal("0.20"),
) -> SafeSourceLocator:
    return SafeSourceLocator(
        document_ordinal=1,
        page_number=1,
        table_ordinal=1,
        row_ordinal=row,
        row_box=BoundingBox(left, top, Decimal("0.80"), top + Decimal("0.10")),
    )


def _status_control(row_ordinals: tuple[int, ...] = (1,)) -> ActivityStatusControl:
    return ActivityStatusControl(
        TransactionStatus.POSTED,
        BoundingBox(Decimal("0.20"), Decimal("0.16"), Decimal("0.40"), Decimal("0.19")),
        (10,),
        row_ordinals,
    )


def _balance_locator(
    *,
    right: Decimal = Decimal("0.39"),
) -> SafeSourceLocator:
    return SafeSourceLocator(
        document_ordinal=1,
        page_number=1,
        table_ordinal=1,
        row_ordinal=1,
        row_box=BoundingBox(Decimal("0.20"), Decimal("0.20"), right, Decimal("0.30")),
    )


def _balance_row(locator: SafeSourceLocator | None = None) -> BalanceRowEvidence:
    return BalanceRowEvidence(
        locator=_balance_locator() if locator is None else locator,
        date_token_ordinals=(9,),
        balance_token_ordinals=(9,),
    )


def _balance_control(
    *,
    locator: SafeSourceLocator | None = None,
    kind: BalanceKind = BalanceKind.CLOSING,
    boundary: BalanceBoundary = BalanceBoundary.END_OF_DAY,
    includes_pending: bool = True,
) -> BalanceControlEvidence:
    source_locator = _balance_locator() if locator is None else locator
    return BalanceControlEvidence(
        locator=source_locator,
        kind=kind,
        boundary=boundary,
        includes_pending=includes_pending,
        control_box=source_locator.row_box,
        kind_token_ordinals=(9,),
        boundary_token_ordinals=(9,),
        includes_pending_token_ordinals=(9,),
    )


def _activity_table(
    rows: tuple[ActivityRowEvidence, ...] | None = None,
    status_controls: tuple[ActivityStatusControl, ...] | None = None,
) -> ActivityTableEvidence:
    table_rows = rows if rows is not None else (ActivityRowEvidence(1, _locator().row_box),)
    table_status_controls = (
        (_status_control(tuple(row.row_ordinal for row in table_rows)),)
        if status_controls is None and table_rows
        else (() if status_controls is None else status_controls)
    )
    return ActivityTableEvidence(
        table_ordinal=1,
        header_box=BoundingBox(Decimal("0.10"), Decimal("0.10"), Decimal("0.80"), Decimal("0.15")),
        columns=(
            ActivityColumnBand(
                ActivityColumn.DATE,
                Decimal("0.10"),
                Decimal("0.19"),
                BoundingBox(Decimal("0.10"), Decimal("0.10"), Decimal("0.19"), Decimal("0.15")),
                (1,),
            ),
            ActivityColumnBand(
                ActivityColumn.DESCRIPTION,
                Decimal("0.20"),
                Decimal("0.49"),
                BoundingBox(Decimal("0.20"), Decimal("0.10"), Decimal("0.49"), Decimal("0.15")),
                (2,),
            ),
            ActivityColumnBand(
                ActivityColumn.DEBIT,
                Decimal("0.50"),
                Decimal("0.64"),
                BoundingBox(Decimal("0.50"), Decimal("0.10"), Decimal("0.64"), Decimal("0.15")),
                (3,),
            ),
            ActivityColumnBand(
                ActivityColumn.CREDIT,
                Decimal("0.65"),
                Decimal("0.79"),
                BoundingBox(Decimal("0.65"), Decimal("0.10"), Decimal("0.79"), Decimal("0.15")),
                (4,),
            ),
        ),
        rows=table_rows,
        status_controls=table_status_controls,
    )


def _page_evidence(
    *,
    text: str = "fictional-item",
    page_kind: PageKind = PageKind.MONTHLY_ACTIVITY,
    extraction_method: ExtractionMethod = ExtractionMethod.NATIVE,
    confidence: int = 100,
    box: BoundingBox | None = None,
    activity_tables: tuple[ActivityTableEvidence, ...] | None = None,
    balance_rows: tuple[BalanceRowEvidence, ...] | None = None,
    balance_controls: tuple[BalanceControlEvidence, ...] = (),
    tokens: tuple[PositionedToken, ...] | None = None,
) -> PageEvidence:
    return PageEvidence(
        page_number=1,
        page_kind=page_kind,
        fingerprint_version=_PAGE_FINGERPRINTS[page_kind],
        extraction_method=extraction_method,
        ignored=False,
        activity_tables=activity_tables
        if activity_tables is not None
        else (_activity_table(),)
        if page_kind in {PageKind.MONTHLY_ACTIVITY, PageKind.CURRENT_ACTIVITY}
        else (),
        balance_rows=balance_rows
        if balance_rows is not None
        else tuple(_balance_row(control.locator) for control in balance_controls),
        balance_controls=balance_controls,
        tokens=tokens
        if tokens is not None
        else (
            PositionedToken(
                page_number=1,
                box=BoundingBox(Decimal("0.10"), Decimal("0.10"), Decimal("0.19"), Decimal("0.15")),
                text="date",
                extraction_method=extraction_method,
                confidence=confidence,
            ),
            PositionedToken(
                page_number=1,
                box=BoundingBox(Decimal("0.20"), Decimal("0.10"), Decimal("0.49"), Decimal("0.15")),
                text="description",
                extraction_method=extraction_method,
                confidence=confidence,
            ),
            PositionedToken(
                page_number=1,
                box=BoundingBox(Decimal("0.50"), Decimal("0.10"), Decimal("0.63"), Decimal("0.15")),
                text="debit",
                extraction_method=extraction_method,
                confidence=confidence,
            ),
            PositionedToken(
                page_number=1,
                box=BoundingBox(Decimal("0.65"), Decimal("0.10"), Decimal("0.79"), Decimal("0.15")),
                text="credit",
                extraction_method=extraction_method,
                confidence=confidence,
            ),
            PositionedToken(
                page_number=1,
                box=box
                or BoundingBox(Decimal("0.10"), Decimal("0.20"), Decimal("0.19"), Decimal("0.30")),
                text="06/01",
                extraction_method=extraction_method,
                confidence=confidence,
            ),
            PositionedToken(
                page_number=1,
                box=BoundingBox(Decimal("0.20"), Decimal("0.20"), Decimal("0.39"), Decimal("0.30")),
                text=text,
                extraction_method=extraction_method,
                confidence=confidence,
            ),
            PositionedToken(
                page_number=1,
                box=BoundingBox(Decimal("0.40"), Decimal("0.20"), Decimal("0.48"), Decimal("0.30")),
                text="posted",
                extraction_method=extraction_method,
                confidence=confidence,
            ),
            PositionedToken(
                page_number=1,
                box=BoundingBox(Decimal("0.51"), Decimal("0.20"), Decimal("0.62"), Decimal("0.30")),
                text="12.34",
                extraction_method=extraction_method,
                confidence=confidence,
            ),
            PositionedToken(
                page_number=1,
                box=BoundingBox(Decimal("0.20"), Decimal("0.20"), Decimal("0.39"), Decimal("0.30")),
                text="06/30 99.00 closing end-of-day includes-pending",
                extraction_method=extraction_method,
                confidence=confidence,
            ),
            PositionedToken(
                page_number=1,
                box=BoundingBox(Decimal("0.20"), Decimal("0.16"), Decimal("0.40"), Decimal("0.19")),
                text="posted",
                extraction_method=extraction_method,
                confidence=confidence,
            ),
        ),
    )


def _transaction_parse_evidence() -> tuple[ParseEvidence, ...]:
    return (
        ParseEvidence(EvidenceField.DATE, (5,)),
        ParseEvidence(EvidenceField.DESCRIPTION, (6,)),
        ParseEvidence(EvidenceField.DIRECTION, (8,), (3,)),
        ParseEvidence(EvidenceField.STATUS, (7,), (10,)),
        ParseEvidence(EvidenceField.MAGNITUDE, (8,)),
    )


def _balance_parse_evidence() -> tuple[ParseEvidence, ...]:
    return (
        ParseEvidence(EvidenceField.DATE, (9,)),
        ParseEvidence(EvidenceField.BALANCE, (9,)),
        ParseEvidence(EvidenceField.KIND, (9,), (9,)),
        ParseEvidence(EvidenceField.BOUNDARY, (9,), (9,)),
        ParseEvidence(EvidenceField.INCLUDES_PENDING, (9,), (9,)),
    )


def _transaction(
    row: int = 1,
    occurrence: int = 1,
    *,
    extraction_method: ExtractionMethod = ExtractionMethod.NATIVE,
    parser_version: str = "wf-v1",
) -> NormalizedTransaction:
    return NormalizedTransaction(
        account_role=AccountRole.CHECKING,
        effective_date=date(2026, 6, 1),
        status=TransactionStatus.POSTED,
        direction=Direction.DEBIT,
        magnitude=Decimal("12.34"),
        normalized_description="fictional-item",
        occurrence_ordinal=occurrence,
        source_sha256=SHA,
        locator=_locator(row),
        extraction_method=extraction_method,
        parser_version=parser_version,
        parse_evidence=_transaction_parse_evidence(),
    )


def _balance() -> BalanceObservation:
    return BalanceObservation(
        account_role=AccountRole.CHECKING,
        amount=Decimal("99.00"),
        observed_on=date(2026, 6, 30),
        boundary=BalanceBoundary.END_OF_DAY,
        kind=BalanceKind.CLOSING,
        includes_pending=True,
        source_sha256=SHA,
        locator=_balance_locator(),
        extraction_method=ExtractionMethod.NATIVE,
        parse_evidence=_balance_parse_evidence(),
    )


def _selector() -> TransactionSelector:
    return TransactionSelector(
        account_role=AccountRole.CHECKING,
        effective_date=date(2026, 6, 1),
        status=TransactionStatus.POSTED,
        direction=Direction.DEBIT,
        magnitude=Decimal("12.34"),
        normalized_description="fictional-item",
        occurrence_ordinal=1,
        source_sha256=SHA,
        page_number=1,
        source_row_ordinal=1,
    )


def _manifest_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "reporting_start_date": "2026-06-01",
        "as_of_date": "2026-06-30",
        "budget_fiscal_year": "FY2027",
        "cash_basis": "available_including_pending",
        "documents": [
            {
                "account_role": "checking",
                "document_kind": "monthly_statement",
                "relative_path": "statements/fictional.pdf",
            }
        ],
        "rules_relative_path": "rules.json",
    }


def _rules_payload(rule_id: str = "example-rule") -> dict[str, object]:
    return {
        "schema_version": 1,
        "classification_rules": [
            {
                "rule_id": rule_id,
                "account_role": "checking",
                "direction": "debit",
                "matcher_kind": "exact",
                "matcher_value": "fictional-item",
                "cash_role": "spending",
                "category": "Example category",
                "pair_key": None,
            }
        ],
        "overlap_resolutions": [],
        "pair_resolutions": [],
        "transaction_adjustments": [],
    }


def _write_canonical(path: Path, payload: object) -> None:
    path.write_text(canonical_json_text(payload), encoding="utf-8")


def test_money_rejects_float_signed_zero_nonfinite_and_overprecision() -> None:
    assert parse_positive_money("12.34") == Decimal("12.34")
    assert parse_nonnegative_money("0.00") == Decimal("0.00")
    for value in (
        12.34,
        "-12.34",
        "0.00",
        "1.234",
        Decimal("-0.00"),
        Decimal("NaN"),
        Decimal("Infinity"),
    ):
        with pytest.raises(ContractError):
            parse_positive_money(value)
    with pytest.raises(ContractError):
        parse_nonnegative_money(Decimal("-0.00"))


def test_decimal_contracts_ignore_ambient_context_and_bound_unusable_magnitudes() -> None:
    with localcontext() as context:
        context.prec = 2
        context.Emax = 2
        context.Emin = -2
        context.traps[InvalidOperation] = False
        assert parse_positive_money("12.34", private=True) == Decimal("12.34")
        assert parse_positive_money(Decimal("1000.00")) == Decimal("1000.00")
        assert canonical_json_text({"amount": Decimal("12.34")}) == '{"amount":"12.34"}'
        assert BoundingBox(
            Decimal("0.1234"), Decimal("0.20"), Decimal("0.30"), Decimal("0.40")
        ).left == Decimal("0.1234")

    assert parse_nonnegative_money(Decimal("0E+999")) == Decimal("0.00")
    assert parse_nonnegative_money(Decimal("0E-999")) == Decimal("0.00")
    assert canonical_json_text({"amount": Decimal("0E+999")}) == '{"amount":"0.00"}'
    assert BoundingBox(
        Decimal("0.1234567890120"), Decimal("0.20"), Decimal("0.30"), Decimal("0.40")
    ).left == Decimal("0.123456789012")
    with pytest.raises(PrivateInputError):
        parse_positive_money(("9" * 65) + ".00", private=True)
    with pytest.raises(ContractError):
        canonical_json_text({"amount": Decimal("1E+64")})
    with pytest.raises(ContractError):
        canonical_json_text({"invalid": chr(0xD800)})
    with pytest.raises(ContractError):
        canonical_sha256({chr(0xD800): "invalid-key"})
    with pytest.raises(ContractError):
        BoundingBox(Decimal("0.1234567890123"), Decimal("0.20"), Decimal("0.30"), Decimal("0.40"))


def test_date_and_description_normalization_are_exact() -> None:
    assert parse_iso_date("2026-06-01") == date(2026, 6, 1)
    assert normalize_description("  Caf\u00e9\u2014Event\t") == "caf\u00e9-event"
    for value in ("06/01/2026", "2026-6-1", "2026-13-01"):
        with pytest.raises(ContractError):
            parse_iso_date(value)


def test_canonical_json_and_hash_reject_float_and_are_stable() -> None:
    payload = {"amount": Decimal("12.34"), "when": date(2026, 6, 1), "items": ["a", 1]}
    assert canonical_json_text(payload) == (
        '{"amount":"12.34","items":["a",1],"when":"2026-06-01"}'
    )
    assert canonical_json_bytes(payload) == canonical_json_text(payload).encode("utf-8")
    assert canonical_sha256(payload) == canonical_sha256(payload)
    with pytest.raises(ContractError):
        canonical_json_text({"bad": 1.5})

    class FloatBacked(Enum):
        BAD = 1.5

    with pytest.raises(ContractError):
        canonical_json_text({"bad": FloatBacked.BAD})


def test_normalized_transaction_round_trips_stable_source_and_semantic_keys() -> None:
    transaction = _transaction()
    assert (
        transaction.source_row_id
        == "220ca1e7a30c2536428ca497febfd3854128925e666f130b4521592d2bd3acd4"
    )
    assert (
        transaction.semantic_key
        == "f919e24cff6f76398e9e39433f92e7dad7f47aa497a7cd1886ba42b78a421ce9"
    )
    assert transaction.source_row_id == build_source_row_id("wf-v1", SHA, _locator())
    assert transaction.semantic_key == build_semantic_key(
        AccountRole.CHECKING,
        date(2026, 6, 1),
        TransactionStatus.POSTED,
        Direction.DEBIT,
        Decimal("12.34"),
        "fictional-item",
        1,
    )
    assert NormalizedTransaction.from_dict(transaction.to_dict()) == transaction
    assert SafeSourceLocator.from_dict(_locator().to_dict()) == _locator()
    assert _selector().matches(transaction)


def test_source_row_identity_excludes_manifest_only_document_ordinal() -> None:
    relocated = SafeSourceLocator(**{**_locator().__dict__, "document_ordinal": 2})
    assert build_source_row_id("wf-v1", SHA, _locator()) == build_source_row_id(
        "wf-v1", SHA, relocated
    )


def test_transaction_selector_uses_every_exact_discriminator() -> None:
    transaction = _transaction()
    selector = _selector()
    for field, value in (
        ("account_role", AccountRole.SAVINGS),
        ("effective_date", date(2026, 6, 2)),
        ("status", TransactionStatus.PENDING),
        ("direction", Direction.CREDIT),
        ("magnitude", Decimal("12.35")),
        ("normalized_description", "different-fictional-item"),
        ("occurrence_ordinal", 2),
        ("source_sha256", "b" * 64),
        ("page_number", 2),
        ("source_row_ordinal", 2),
    ):
        near_collision = TransactionSelector(**{**selector.__dict__, field: value})
        assert not near_collision.matches(transaction)


def test_duplicate_occurrences_are_ordered_by_safe_source_locator() -> None:
    second = _transaction(row=2, occurrence=99)
    first = _transaction(row=1, occurrence=99)
    result = assign_occurrence_ordinals((second, first))
    assert [transaction.locator.row_ordinal for transaction in result] == [1, 2]
    assert [transaction.occurrence_ordinal for transaction in result] == [1, 2]
    assert result[0].semantic_key != result[1].semantic_key

    right = _transaction(row=1, occurrence=99)
    left = _transaction(row=1, occurrence=99)
    left = NormalizedTransaction(
        **{
            **left.__dict__,
            "locator": _locator(1, left=Decimal("0.05")),
            "source_row_id": None,
        }
    )
    right = NormalizedTransaction(
        **{
            **right.__dict__,
            "locator": _locator(1, left=Decimal("0.15")),
            "source_row_id": None,
        }
    )
    forward = assign_occurrence_ordinals((right, left))
    reversed_result = assign_occurrence_ordinals((left, right))
    assert [(item.source_row_id, item.occurrence_ordinal) for item in forward] == [
        (item.source_row_id, item.occurrence_ordinal) for item in reversed_result
    ]
    assert [item.locator.row_box.left for item in forward] == [Decimal("0.05"), Decimal("0.15")]

    first_source = NormalizedTransaction(
        **{
            **_transaction(row=1, occurrence=99).__dict__,
            "locator": SafeSourceLocator(**{**_locator().__dict__, "document_ordinal": 2}),
            "source_row_id": None,
        }
    )
    second_source = NormalizedTransaction(
        **{
            **first_source.__dict__,
            "source_sha256": "b" * 64,
            "locator": SafeSourceLocator(**{**_locator().__dict__, "document_ordinal": 1}),
            "source_row_id": None,
        }
    )
    source_forward = assign_occurrence_ordinals((second_source, first_source))
    source_reversed = assign_occurrence_ordinals((first_source, second_source))
    assert [
        (item.source_sha256, item.source_row_id, item.occurrence_ordinal, item.semantic_key)
        for item in source_forward
    ] == [
        (item.source_sha256, item.source_row_id, item.occurrence_ordinal, item.semantic_key)
        for item in source_reversed
    ]
    assert [(item.source_sha256, item.occurrence_ordinal) for item in source_forward] == [
        (SHA, 1),
        ("b" * 64, 2),
    ]


def test_statement_rejects_duplicate_semantic_keys_before_authority_selection() -> None:
    first = _transaction(row=1, occurrence=1)
    duplicate = _transaction(row=2, occurrence=1)
    with pytest.raises(ContractError):
        StatementObservation(
            document_ordinal=1,
            document=DocumentSpec(
                AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, "statement.pdf"
            ),
            source_sha256=SHA,
            parser_version="wf-v1",
            coverage_start=date(2026, 6, 1),
            coverage_end=date(2026, 6, 30),
            capture_date=date(2026, 6, 30),
            source_page_count=1,
            page_evidence=(_page_evidence(),),
            transactions=(first, duplicate),
            balances=(),
        )
    noncanonical_first = _transaction(row=1, occurrence=2)
    noncanonical_second = _transaction(row=2, occurrence=1)
    with pytest.raises(ContractError):
        StatementObservation(
            document_ordinal=1,
            document=DocumentSpec(
                AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, "statement.pdf"
            ),
            source_sha256=SHA,
            parser_version="wf-v1",
            coverage_start=date(2026, 6, 1),
            coverage_end=date(2026, 6, 30),
            capture_date=date(2026, 6, 30),
            source_page_count=1,
            page_evidence=(_page_evidence(),),
            transactions=(noncanonical_first, noncanonical_second),
            balances=(),
        )


def test_position_balance_and_statement_contracts_round_trip() -> None:
    box = BoundingBox(Decimal("0.1234"), Decimal("0.20"), Decimal("0.30"), Decimal("0.40"))
    transaction = _transaction()
    balance = _balance()
    observation = StatementObservation(
        document_ordinal=1,
        document=DocumentSpec(
            AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, "statement.pdf"
        ),
        source_sha256=SHA,
        parser_version="wf-v1",
        coverage_start=date(2026, 6, 1),
        coverage_end=date(2026, 6, 30),
        capture_date=date(2026, 6, 30),
        source_page_count=1,
        page_evidence=(_page_evidence(),),
        transactions=(transaction,),
        balances=(),
    )
    assert box.to_dict()["left"] == "0.1234"
    assert BoundingBox.from_dict(box.to_dict()) == box
    token = _page_evidence().tokens[0]
    assert token.to_dict() == {
        "page_number": 1,
        "box": {"left": "0.1", "top": "0.1", "right": "0.19", "bottom": "0.15"},
        "text": "date",
        "extraction_method": "native",
        "confidence": 100,
    }
    assert PositionedToken.from_dict(token.to_dict()) == token
    evidence = ParseEvidence(EvidenceField.DIRECTION, (8,), (3,))
    assert evidence.to_dict() == {
        "field": "direction",
        "token_ordinals": [8],
        "context_token_ordinals": [3],
    }
    assert ParseEvidence.from_dict(evidence.to_dict()) == evidence
    with pytest.raises(ContractError):
        ParseEvidence(EvidenceField.DATE, (2, 1))
    assert balance.to_dict() == {
        "account_role": "checking",
        "amount": "99.00",
        "observed_on": "2026-06-30",
        "boundary": "end_of_day",
        "kind": "closing",
        "includes_pending": True,
        "source_sha256": SHA,
        "locator": {
            "document_ordinal": 1,
            "page_number": 1,
            "table_ordinal": 1,
            "row_ordinal": 1,
            "row_box": {"left": "0.2", "top": "0.2", "right": "0.39", "bottom": "0.3"},
        },
        "extraction_method": "native",
        "parse_evidence": [
            {"field": "date", "token_ordinals": [9], "context_token_ordinals": []},
            {"field": "balance", "token_ordinals": [9], "context_token_ordinals": []},
            {"field": "kind", "token_ordinals": [9], "context_token_ordinals": [9]},
            {"field": "boundary", "token_ordinals": [9], "context_token_ordinals": [9]},
            {
                "field": "includes_pending",
                "token_ordinals": [9],
                "context_token_ordinals": [9],
            },
        ],
    }
    assert BalanceObservation.from_dict(balance.to_dict()) == balance
    page = _page_evidence()
    assert PageEvidence.from_dict(page.to_dict()) == page
    assert StatementObservation.from_dict(observation.to_dict()) == observation
    assert observation.transactions[0].source_row_id == transaction.source_row_id
    with pytest.raises(ContractError):
        BoundingBox(Decimal("-0"), Decimal("0.20"), Decimal("0.30"), Decimal("0.40"))


def test_statement_requires_nonignored_matching_page_evidence_and_parser_version() -> None:
    document = DocumentSpec(AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, "statement.pdf")
    transaction = _transaction()
    with pytest.raises(ContractError):
        PageEvidence(
            page_number=1,
            page_kind=PageKind.MONTHLY_ACTIVITY,
            fingerprint_version=PageFingerprint.WELLS_FARGO_V1_MONTHLY_ACTIVITY,
            extraction_method=ExtractionMethod.NATIVE,
            ignored=False,
            activity_tables=(_activity_table(),),
            balance_rows=(),
            balance_controls=(),
            tokens=(),
        )
    boilerplate = PageEvidence(
        page_number=1,
        page_kind=PageKind.BOILERPLATE,
        fingerprint_version=PageFingerprint.WELLS_FARGO_V1_BOILERPLATE,
        extraction_method=ExtractionMethod.NATIVE,
        ignored=True,
        activity_tables=(),
        balance_rows=(),
        balance_controls=(),
        tokens=(),
    )

    def observation(
        page_evidence: tuple[PageEvidence, ...],
        transactions: tuple[NormalizedTransaction, ...],
        balances: tuple[BalanceObservation, ...] = (),
    ) -> StatementObservation:
        return StatementObservation(
            document_ordinal=1,
            document=document,
            source_sha256=SHA,
            parser_version="wf-v1",
            coverage_start=date(2026, 6, 1),
            coverage_end=date(2026, 6, 30),
            capture_date=date(2026, 6, 30),
            source_page_count=1,
            page_evidence=page_evidence,
            transactions=transactions,
            balances=balances,
        )

    with pytest.raises(ContractError):
        observation((boilerplate,), (transaction,))
    with pytest.raises(ContractError):
        observation((_page_evidence(),), (_transaction(extraction_method=ExtractionMethod.OCR),))
    with pytest.raises(ContractError):
        observation((_page_evidence(),), (_transaction(parser_version="wf-v2"),))
    summary = PageEvidence(
        page_number=1,
        page_kind=PageKind.MONTHLY_SUMMARY,
        fingerprint_version=PageFingerprint.WELLS_FARGO_V1_MONTHLY_SUMMARY,
        extraction_method=ExtractionMethod.NATIVE,
        ignored=False,
        activity_tables=(),
        balance_rows=(),
        balance_controls=(),
        tokens=_page_evidence().tokens,
    )
    with pytest.raises(ContractError):
        observation((summary,), (transaction,))
    unsupported = _page_evidence(
        text="unrelated-fictional-token",
        box=BoundingBox(Decimal("0.81"), Decimal("0.20"), Decimal("0.90"), Decimal("0.30")),
    )
    with pytest.raises(ContractError):
        observation((unsupported,), (transaction,))
    with pytest.raises(ContractError):
        observation(
            (
                _page_evidence(
                    box=BoundingBox(
                        Decimal("0.81"), Decimal("0.20"), Decimal("0.90"), Decimal("0.30")
                    )
                ),
            ),
            (transaction,),
        )

    def page_with_token(ordinal: int, text: str) -> PageEvidence:
        page = _page_evidence()
        tokens = list(page.tokens)
        token = tokens[ordinal - 1]
        tokens[ordinal - 1] = PositionedToken(**{**token.__dict__, "text": text})
        return _page_evidence(tokens=tuple(tokens))

    for ordinal, contradicted_text in (
        (3, "credit"),
        (10, "pending"),
        (5, "06/10"),
        (5, "06/01/2025"),
        (8, "1,2.34"),
    ):
        with pytest.raises(ContractError):
            observation((page_with_token(ordinal, contradicted_text),), (transaction,))
    with pytest.raises(ContractError):
        observation(
            (_page_evidence(),),
            (
                NormalizedTransaction(
                    **{
                        **transaction.__dict__,
                        "magnitude": Decimal("98.76"),
                        "semantic_key": None,
                    }
                ),
            ),
        )
    out_of_period = NormalizedTransaction(
        **{
            **transaction.__dict__,
            "effective_date": date(2026, 7, 1),
            "semantic_key": None,
        }
    )
    with pytest.raises(ContractError):
        observation(
            (page_with_token(5, "07/01"),),
            (out_of_period,),
        )

    balance = _balance()
    with pytest.raises(ContractError):
        observation((boilerplate,), (), (balance,))
    for changes in (
        {"source_sha256": "b" * 64},
        {"account_role": AccountRole.SAVINGS},
        {
            "locator": SafeSourceLocator(
                document_ordinal=2,
                page_number=1,
                table_ordinal=1,
                row_ordinal=1,
                row_box=_locator().row_box,
            )
        },
        {"extraction_method": ExtractionMethod.OCR},
        {
            "parse_evidence": (
                ParseEvidence(EvidenceField.DATE, (2,)),
                ParseEvidence(EvidenceField.BALANCE, (9,)),
                ParseEvidence(EvidenceField.KIND, (9,)),
                ParseEvidence(EvidenceField.BOUNDARY, (9,)),
                ParseEvidence(EvidenceField.INCLUDES_PENDING, (9,)),
            )
        },
        {"observed_on": date(2026, 6, 29)},
        {"amount": Decimal("98.76")},
        {"kind": BalanceKind.OPENING},
        {"boundary": BalanceBoundary.START_OF_DAY},
        {"includes_pending": False},
        {
            "locator": SafeSourceLocator(
                document_ordinal=1,
                page_number=2,
                table_ordinal=1,
                row_ordinal=1,
                row_box=_locator().row_box,
            )
        },
        {
            "locator": SafeSourceLocator(
                document_ordinal=1,
                page_number=1,
                table_ordinal=1,
                row_ordinal=1,
                row_box=BoundingBox(
                    Decimal("0.81"), Decimal("0.20"), Decimal("0.90"), Decimal("0.30")
                ),
            )
        },
    ):
        with pytest.raises(ContractError):
            observation(
                (_page_evidence(),),
                (),
                (BalanceObservation(**{**balance.__dict__, **changes}),),
            )


def test_statement_is_closed_over_declared_pages_rows_and_balance_controls() -> None:
    document = DocumentSpec(AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, "statement.pdf")
    transaction = _transaction()
    balance = _balance()

    def observation(
        page: PageEvidence,
        transactions: tuple[NormalizedTransaction, ...] = (),
        balances: tuple[BalanceObservation, ...] = (),
        *,
        source_page_count: int = 1,
    ) -> StatementObservation:
        return StatementObservation(
            document_ordinal=1,
            document=document,
            source_sha256=SHA,
            parser_version="wf-v1",
            coverage_start=date(2026, 6, 1),
            coverage_end=date(2026, 6, 30),
            capture_date=date(2026, 6, 30),
            source_page_count=source_page_count,
            page_evidence=(page,),
            transactions=transactions,
            balances=balances,
        )

    with pytest.raises(ContractError):
        observation(_page_evidence())
    with pytest.raises(ContractError):
        observation(_page_evidence(), (transaction,), source_page_count=2)

    second_locator = _locator(2, top=Decimal("0.31"))
    two_rows = _page_evidence(
        activity_tables=(
            _activity_table(
                rows=(
                    ActivityRowEvidence(1, _locator().row_box),
                    ActivityRowEvidence(2, second_locator.row_box),
                ),
                status_controls=(_status_control((1, 2)),),
            ),
        )
    )
    with pytest.raises(ContractError):
        observation(two_rows, (transaction,))

    untyped_status = NormalizedTransaction(
        **{
            **transaction.__dict__,
            "parse_evidence": (
                ParseEvidence(EvidenceField.DATE, (5,)),
                ParseEvidence(EvidenceField.DESCRIPTION, (6,)),
                ParseEvidence(EvidenceField.DIRECTION, (8,), (3,)),
                ParseEvidence(EvidenceField.STATUS, (7,)),
                ParseEvidence(EvidenceField.MAGNITUDE, (8,)),
            ),
        }
    )
    with pytest.raises(ContractError):
        observation(_page_evidence(), (untyped_status,))

    balance_page = _page_evidence(
        page_kind=PageKind.MONTHLY_SUMMARY,
        balance_controls=(_balance_control(),),
    )
    with pytest.raises(ContractError):
        observation(balance_page)
    with pytest.raises(ContractError):
        observation(balance_page, balances=(balance, balance))
    with pytest.raises(ContractError):
        observation(_page_evidence(page_kind=PageKind.MONTHLY_SUMMARY), balances=(balance,))

    first_table = _activity_table(status_controls=(_status_control(),))
    duplicate_table = ActivityTableEvidence(**{**first_table.__dict__, "table_ordinal": 2})
    with pytest.raises(ContractError):
        _page_evidence(activity_tables=(first_table, duplicate_table))

    aliased_balance_locator = SafeSourceLocator(
        document_ordinal=1,
        page_number=1,
        table_ordinal=2,
        row_ordinal=2,
        row_box=_locator().row_box,
    )
    aliased_balance_control = _balance_control(locator=aliased_balance_locator)
    with pytest.raises(ContractError):
        _page_evidence(
            page_kind=PageKind.MONTHLY_SUMMARY,
            balance_controls=(_balance_control(), aliased_balance_control),
        )

    empty_table_page = _page_evidence(activity_tables=(_activity_table(rows=()),))
    assert observation(empty_table_page).transactions == ()


def test_positioned_status_and_balance_evidence_are_local_and_unambiguous() -> None:
    second_locator = _locator(2, top=Decimal("0.31"))
    third_locator = _locator(3, top=Decimal("0.42"))
    posted_control = ActivityStatusControl(
        TransactionStatus.POSTED,
        BoundingBox(Decimal("0.20"), Decimal("0.16"), Decimal("0.40"), Decimal("0.19")),
        (10,),
        (1, 2),
    )
    pending_control = ActivityStatusControl(
        TransactionStatus.PENDING,
        BoundingBox(Decimal("0.20"), Decimal("0.301"), Decimal("0.40"), Decimal("0.309")),
        (11,),
        (3,),
    )
    table = _activity_table(
        rows=(
            ActivityRowEvidence(1, _locator().row_box),
            ActivityRowEvidence(2, second_locator.row_box),
            ActivityRowEvidence(3, third_locator.row_box),
        ),
        status_controls=(posted_control, pending_control),
    )
    with pytest.raises(ContractError):
        table.status_control_for_row(2)

    loose_locator = SafeSourceLocator(
        document_ordinal=1,
        page_number=1,
        table_ordinal=1,
        row_ordinal=1,
        row_box=BoundingBox(Decimal("0.10"), Decimal("0.20"), Decimal("0.80"), Decimal("0.30")),
    )
    loose_control = BalanceControlEvidence(
        locator=loose_locator,
        kind=BalanceKind.CLOSING,
        boundary=BalanceBoundary.END_OF_DAY,
        includes_pending=True,
        control_box=BoundingBox(Decimal("0.20"), Decimal("0.20"), Decimal("0.39"), Decimal("0.30")),
        kind_token_ordinals=(9,),
        boundary_token_ordinals=(9,),
        includes_pending_token_ordinals=(9,),
    )
    with pytest.raises(ContractError):
        _page_evidence(
            page_kind=PageKind.MONTHLY_SUMMARY,
            balance_rows=(_balance_row(loose_locator),),
            balance_controls=(loose_control,),
        )

    contradictory_page = _page_evidence(page_kind=PageKind.MONTHLY_SUMMARY)
    contradictory_tokens = list(contradictory_page.tokens)
    balance_token = contradictory_tokens[8]
    contradictory_tokens[8] = PositionedToken(
        **{
            **balance_token.__dict__,
            "text": "06/30 99.00 available as-of excludes-pending",
        }
    )
    with pytest.raises(ContractError):
        _page_evidence(
            page_kind=PageKind.MONTHLY_SUMMARY,
            balance_controls=(
                _balance_control(
                    kind=BalanceKind.AVAILABLE,
                    boundary=BalanceBoundary.CAPTURE,
                    includes_pending=True,
                ),
            ),
            tokens=tuple(contradictory_tokens),
        )

    ambiguous_page = _page_evidence(page_kind=PageKind.MONTHLY_SUMMARY)
    ambiguous_tokens = list(ambiguous_page.tokens)
    balance_token = ambiguous_tokens[8]
    ambiguous_tokens[8] = PositionedToken(
        **{
            **balance_token.__dict__,
            "text": ("06/30 99.00 closing end-of-day includes-pending 06/29 100.00"),
        }
    )
    with pytest.raises(ContractError):
        _page_evidence(
            page_kind=PageKind.MONTHLY_SUMMARY,
            balance_controls=(_balance_control(),),
            tokens=tuple(ambiguous_tokens),
        )


def test_page_fingerprint_and_activity_geometry_fail_closed() -> None:
    page = _page_evidence()
    with pytest.raises(ContractError):
        PageEvidence(
            **{
                **page.__dict__,
                "fingerprint_version": PageFingerprint.WELLS_FARGO_V1_MONTHLY_SUMMARY,
            }
        )
    with pytest.raises(ContractError):
        PageEvidence(
            **{**page.__dict__, "fingerprint_version": "unsupported-fingerprint"}  # type: ignore[arg-type]
        )

    current_document = DocumentSpec(
        AccountRole.CHECKING, DocumentKind.CURRENT_ACTIVITY, "current.pdf"
    )
    with pytest.raises(ContractError):
        StatementObservation(
            document_ordinal=1,
            document=current_document,
            source_sha256=SHA,
            parser_version="wf-v1",
            coverage_start=date(2026, 6, 1),
            coverage_end=date(2026, 6, 30),
            capture_date=date(2026, 6, 30),
            source_page_count=1,
            page_evidence=(page,),
            transactions=(),
            balances=(),
        )

    def observation(
        page_evidence: PageEvidence,
        transaction: NormalizedTransaction,
    ) -> StatementObservation:
        return StatementObservation(
            document_ordinal=1,
            document=DocumentSpec(
                AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, "statement.pdf"
            ),
            source_sha256=SHA,
            parser_version="wf-v1",
            coverage_start=date(2026, 6, 1),
            coverage_end=date(2026, 6, 30),
            capture_date=date(2026, 6, 30),
            source_page_count=1,
            page_evidence=(page_evidence,),
            transactions=(transaction,),
            balances=(),
        )

    debit = _transaction()
    assert observation(page, debit).transactions == (debit,)
    truncated_description = NormalizedTransaction(
        **{
            **debit.__dict__,
            "normalized_description": "fictional",
            "semantic_key": None,
        }
    )
    with pytest.raises(ContractError):
        observation(page, truncated_description)
    credit_evidence = (
        ParseEvidence(EvidenceField.DATE, (5,)),
        ParseEvidence(EvidenceField.DESCRIPTION, (6,)),
        ParseEvidence(EvidenceField.DIRECTION, (8,), (4,)),
        ParseEvidence(EvidenceField.STATUS, (7,)),
        ParseEvidence(EvidenceField.MAGNITUDE, (8,)),
    )
    credit = NormalizedTransaction(
        **{
            **debit.__dict__,
            "direction": Direction.CREDIT,
            "parse_evidence": credit_evidence,
            "semantic_key": None,
        }
    )
    with pytest.raises(ContractError):
        observation(page, credit)

    arbitrary_header = NormalizedTransaction(
        **{
            **debit.__dict__,
            "parse_evidence": credit_evidence,
            "semantic_key": None,
        }
    )
    with pytest.raises(ContractError):
        observation(page, arbitrary_header)

    tokens = list(page.tokens)
    tokens.append(
        PositionedToken(
            page_number=1,
            box=BoundingBox(Decimal("0.66"), Decimal("0.20"), Decimal("0.77"), Decimal("0.30")),
            text="12.34",
            extraction_method=ExtractionMethod.NATIVE,
            confidence=100,
        )
    )
    with pytest.raises(ContractError):
        observation(_page_evidence(tokens=tuple(tokens)), debit)

    malformed_tokens = list(page.tokens)
    amount_token = malformed_tokens[7]
    malformed_tokens[7] = PositionedToken(**{**amount_token.__dict__, "text": "1,2.34"})
    lower_amount = NormalizedTransaction(
        **{**debit.__dict__, "magnitude": Decimal("2.34"), "semantic_key": None}
    )
    with pytest.raises(ContractError):
        observation(_page_evidence(tokens=tuple(malformed_tokens)), lower_amount)

    for ordinal in (1, 2):
        contradictory_headers = list(page.tokens)
        header = contradictory_headers[ordinal - 1]
        contradictory_headers[ordinal - 1] = PositionedToken(
            **{**header.__dict__, "text": "unrecognized-header"}
        )
        with pytest.raises(ContractError):
            _page_evidence(tokens=tuple(contradictory_headers))

    widened_locator = SafeSourceLocator(
        document_ordinal=1,
        page_number=1,
        table_ordinal=1,
        row_ordinal=1,
        row_box=BoundingBox(Decimal("0.10"), Decimal("0.20"), Decimal("0.80"), Decimal("0.40")),
    )
    widened = NormalizedTransaction(
        **{**debit.__dict__, "locator": widened_locator, "source_row_id": None}
    )
    with pytest.raises(ContractError):
        observation(page, widened)


def test_statement_requires_canonical_source_order_and_document_fact_cutoffs() -> None:
    first = _transaction(occurrence=1)
    second_locator = _locator(2, top=Decimal("0.31"))
    second_evidence = (
        ParseEvidence(EvidenceField.DATE, (11,)),
        ParseEvidence(EvidenceField.DESCRIPTION, (12,)),
        ParseEvidence(EvidenceField.DIRECTION, (14,), (3,)),
        ParseEvidence(EvidenceField.STATUS, (13,), (10,)),
        ParseEvidence(EvidenceField.MAGNITUDE, (14,)),
    )
    second = NormalizedTransaction(
        **{
            **first.__dict__,
            "locator": second_locator,
            "occurrence_ordinal": 2,
            "parse_evidence": second_evidence,
            "source_row_id": None,
            "semantic_key": None,
        }
    )
    page = _page_evidence()
    tokens = list(page.tokens)
    for box, text in (
        (BoundingBox(Decimal("0.10"), Decimal("0.31"), Decimal("0.19"), Decimal("0.41")), "06/01"),
        (
            BoundingBox(Decimal("0.20"), Decimal("0.31"), Decimal("0.39"), Decimal("0.41")),
            "fictional-item",
        ),
        (BoundingBox(Decimal("0.40"), Decimal("0.31"), Decimal("0.48"), Decimal("0.41")), "posted"),
        (BoundingBox(Decimal("0.51"), Decimal("0.31"), Decimal("0.62"), Decimal("0.41")), "12.34"),
    ):
        tokens.append(
            PositionedToken(
                page_number=1,
                box=box,
                text=text,
                extraction_method=ExtractionMethod.NATIVE,
                confidence=100,
            )
        )
    page = _page_evidence(
        activity_tables=(
            _activity_table(
                rows=(
                    ActivityRowEvidence(1, _locator().row_box),
                    ActivityRowEvidence(2, second_locator.row_box),
                ),
                status_controls=(_status_control((1, 2)),),
            ),
        ),
        tokens=tuple(tokens),
    )
    document = DocumentSpec(AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, "statement.pdf")
    kwargs = {
        "document_ordinal": 1,
        "document": document,
        "source_sha256": SHA,
        "parser_version": "wf-v1",
        "coverage_start": date(2026, 6, 1),
        "coverage_end": date(2026, 6, 30),
        "capture_date": date(2026, 7, 31),
        "source_page_count": 1,
        "page_evidence": (page,),
        "balances": (),
    }
    canonical = StatementObservation(**{**kwargs, "transactions": (first, second)})
    assert StatementObservation.from_dict(canonical.to_dict()) == canonical
    with pytest.raises(ContractError):
        StatementObservation(**{**kwargs, "transactions": (second, first)})

    late_transaction = NormalizedTransaction(
        **{
            **first.__dict__,
            "effective_date": date(2026, 7, 1),
            "semantic_key": None,
        }
    )
    with pytest.raises(ContractError):
        StatementObservation(**{**kwargs, "transactions": (late_transaction,)})

    current_tokens = list(_page_evidence(page_kind=PageKind.CURRENT_ACTIVITY).tokens)
    date_token = current_tokens[4]
    current_tokens[4] = PositionedToken(**{**date_token.__dict__, "text": "07/01"})
    current_page = _page_evidence(
        page_kind=PageKind.CURRENT_ACTIVITY,
        tokens=tuple(current_tokens),
    )
    current = StatementObservation(
        **{
            **kwargs,
            "document": DocumentSpec(
                AccountRole.CHECKING, DocumentKind.CURRENT_ACTIVITY, "current.pdf"
            ),
            "page_evidence": (current_page,),
            "transactions": (late_transaction,),
        }
    )
    assert current.transactions == (late_transaction,)

    late_balance = BalanceObservation(**{**_balance().__dict__, "observed_on": date(2026, 7, 1)})
    balance_tokens = list(_page_evidence().tokens)
    balance_token = balance_tokens[8]
    balance_tokens[8] = PositionedToken(
        **{
            **balance_token.__dict__,
            "text": "07/01 99.00 closing end-of-day includes-pending",
        }
    )
    late_balance_page = _page_evidence(tokens=tuple(balance_tokens))
    with pytest.raises(ContractError):
        StatementObservation(
            **{
                **kwargs,
                "page_evidence": (late_balance_page,),
                "transactions": (),
                "balances": (late_balance,),
            }
        )

    current_balance_page = _page_evidence(
        page_kind=PageKind.CURRENT_BALANCE,
        balance_controls=(_balance_control(),),
        tokens=tuple(balance_tokens),
    )
    current_balance = StatementObservation(
        **{
            **kwargs,
            "document": DocumentSpec(
                AccountRole.CHECKING, DocumentKind.CURRENT_ACTIVITY, "current.pdf"
            ),
            "page_evidence": (current_balance_page,),
            "transactions": (),
            "balances": (late_balance,),
        }
    )
    assert current_balance.balances == (late_balance,)


def test_balance_evidence_accepts_declared_boundary_and_pending_variants() -> None:
    document = DocumentSpec(AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, "statement.pdf")
    for observed_on, boundary, kind, includes_pending, text in (
        (
            date(2026, 6, 1),
            BalanceBoundary.START_OF_DAY,
            BalanceKind.OPENING,
            False,
            "06/01 99.00 opening start-of-day excludes-pending",
        ),
        (
            date(2026, 6, 30),
            BalanceBoundary.CAPTURE,
            BalanceKind.COLLECTED,
            False,
            "06/30 99.00 collected as-of excludes-pending",
        ),
        (
            date(2026, 6, 30),
            BalanceBoundary.CAPTURE,
            BalanceKind.AVAILABLE,
            True,
            "06/30 99.00 available as-of includes-pending",
        ),
    ):
        balance = BalanceObservation(
            **{
                **_balance().__dict__,
                "observed_on": observed_on,
                "boundary": boundary,
                "kind": kind,
                "includes_pending": includes_pending,
            }
        )
        page = _page_evidence(page_kind=PageKind.MONTHLY_SUMMARY)
        tokens = list(page.tokens)
        balance_token = tokens[8]
        tokens[8] = PositionedToken(**{**balance_token.__dict__, "text": text})
        observation = StatementObservation(
            document_ordinal=1,
            document=document,
            source_sha256=SHA,
            parser_version="wf-v1",
            coverage_start=date(2026, 6, 1),
            coverage_end=date(2026, 6, 30),
            capture_date=date(2026, 6, 30),
            source_page_count=1,
            page_evidence=(
                _page_evidence(
                    page_kind=PageKind.MONTHLY_SUMMARY,
                    balance_controls=(
                        _balance_control(
                            kind=kind,
                            boundary=boundary,
                            includes_pending=includes_pending,
                        ),
                    ),
                    tokens=tuple(tokens),
                ),
            ),
            transactions=(),
            balances=(balance,),
        )
        assert observation.balances == (balance,)


def test_balance_context_requires_a_local_typed_control_and_round_trips() -> None:
    local_balance_locator = _balance_locator(right=Decimal("0.48"))
    parse_evidence = (
        ParseEvidence(EvidenceField.DATE, (9,)),
        ParseEvidence(EvidenceField.BALANCE, (9,)),
        ParseEvidence(EvidenceField.KIND, (11,), (11,)),
        ParseEvidence(EvidenceField.BOUNDARY, (11,), (11,)),
        ParseEvidence(EvidenceField.INCLUDES_PENDING, (11,), (11,)),
    )
    balance = BalanceObservation(
        **{
            **_balance().__dict__,
            "locator": local_balance_locator,
            "parse_evidence": parse_evidence,
        }
    )
    base = _page_evidence()
    tokens = list(base.tokens)
    balance_anchor = tokens[8]
    tokens[8] = PositionedToken(**{**balance_anchor.__dict__, "text": "06/30 99.00 balance-anchor"})
    remote_token = PositionedToken(
        page_number=1,
        box=BoundingBox(Decimal("0.81"), Decimal("0.80"), Decimal("0.95"), Decimal("0.85")),
        text="closing end-of-day includes-pending",
        extraction_method=ExtractionMethod.NATIVE,
        confidence=100,
    )
    document = DocumentSpec(AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, "statement.pdf")
    kwargs = {
        "document_ordinal": 1,
        "document": document,
        "source_sha256": SHA,
        "parser_version": "wf-v1",
        "coverage_start": date(2026, 6, 1),
        "coverage_end": date(2026, 6, 30),
        "capture_date": date(2026, 6, 30),
        "source_page_count": 1,
        "transactions": (),
        "balances": (balance,),
    }
    with pytest.raises(ContractError):
        StatementObservation(
            **{
                **kwargs,
                "page_evidence": (_page_evidence(tokens=tuple(tokens) + (remote_token,)),),
            }
        )

    control_box = BoundingBox(Decimal("0.40"), Decimal("0.20"), Decimal("0.48"), Decimal("0.30"))
    local_token = PositionedToken(
        page_number=1,
        box=control_box,
        text="closing end-of-day includes-pending",
        extraction_method=ExtractionMethod.NATIVE,
        confidence=100,
    )
    control = BalanceControlEvidence(
        locator=local_balance_locator,
        kind=BalanceKind.CLOSING,
        boundary=BalanceBoundary.END_OF_DAY,
        includes_pending=True,
        control_box=control_box,
        kind_token_ordinals=(11,),
        boundary_token_ordinals=(11,),
        includes_pending_token_ordinals=(11,),
    )
    page = _page_evidence(
        page_kind=PageKind.MONTHLY_SUMMARY,
        balance_controls=(control,),
        tokens=tuple(tokens) + (local_token,),
    )
    observation = StatementObservation(**{**kwargs, "page_evidence": (page,)})
    assert StatementObservation.from_dict(observation.to_dict()) == observation

    contradictory_control = BalanceControlEvidence(
        **{**control.__dict__, "kind": BalanceKind.OPENING}
    )
    with pytest.raises(ContractError):
        StatementObservation(
            **{
                **kwargs,
                "page_evidence": (
                    _page_evidence(
                        balance_controls=(contradictory_control,),
                        tokens=tuple(tokens) + (local_token,),
                    ),
                ),
            }
        )


def test_ocr_parse_evidence_enforces_control_confidence_and_keeps_description_evidence() -> None:
    def statement(
        page: PageEvidence,
        transactions: tuple[NormalizedTransaction, ...] = (),
        balances: tuple[BalanceObservation, ...] = (),
    ) -> StatementObservation:
        return StatementObservation(
            document_ordinal=1,
            document=DocumentSpec(
                AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, "statement.pdf"
            ),
            source_sha256=SHA,
            parser_version="wf-v1",
            coverage_start=date(2026, 6, 1),
            coverage_end=date(2026, 6, 30),
            capture_date=date(2026, 6, 30),
            source_page_count=1,
            page_evidence=(page,),
            transactions=transactions,
            balances=balances,
        )

    def ocr_page(
        changes: dict[int, int] | None = None,
        *,
        activity_tables: tuple[ActivityTableEvidence, ...] | None = None,
        extra_tokens: tuple[PositionedToken, ...] = (),
    ) -> PageEvidence:
        base = _page_evidence(extraction_method=ExtractionMethod.OCR)
        tokens = list(base.tokens)
        for ordinal, confidence in (changes or {}).items():
            token = tokens[ordinal - 1]
            tokens[ordinal - 1] = PositionedToken(**{**token.__dict__, "confidence": confidence})
        return _page_evidence(
            extraction_method=ExtractionMethod.OCR,
            activity_tables=activity_tables,
            tokens=tuple(tokens) + extra_tokens,
        )

    transaction = NormalizedTransaction(
        **{
            **_transaction().__dict__,
            "extraction_method": ExtractionMethod.OCR,
        }
    )
    description_low = ocr_page({6: 74})
    assert statement(description_low, (transaction,)).transactions == (transaction,)
    with pytest.raises(ContractError):
        ocr_page({3: 74})
    date_low = ocr_page({5: 74})
    with pytest.raises(ContractError):
        statement(date_low, (transaction,))

    balance = BalanceObservation(
        **{
            **_balance().__dict__,
            "extraction_method": ExtractionMethod.OCR,
        }
    )
    balance_low = ocr_page({9: 74})
    with pytest.raises(ContractError):
        statement(balance_low, balances=(balance,))

    status_box = BoundingBox(Decimal("0.20"), Decimal("0.16"), Decimal("0.40"), Decimal("0.19"))
    status_table = _activity_table(
        status_controls=(ActivityStatusControl(TransactionStatus.POSTED, status_box, (10,), (1,)),)
    )
    header_status_evidence = (
        ParseEvidence(EvidenceField.DATE, (5,)),
        ParseEvidence(EvidenceField.DESCRIPTION, (6,)),
        ParseEvidence(EvidenceField.DIRECTION, (8,), (3,)),
        ParseEvidence(EvidenceField.STATUS, (7,), (10,)),
        ParseEvidence(EvidenceField.MAGNITUDE, (8,)),
    )
    header_status_transaction = NormalizedTransaction(
        **{**transaction.__dict__, "parse_evidence": header_status_evidence}
    )
    header_status_page = ocr_page(activity_tables=(status_table,))
    status_tokens = list(header_status_page.tokens)
    row_status_token = status_tokens[6]
    status_tokens[6] = PositionedToken(**{**row_status_token.__dict__, "text": "status-anchor"})
    header_status_page = _page_evidence(
        extraction_method=ExtractionMethod.OCR,
        activity_tables=(status_table,),
        tokens=tuple(status_tokens),
    )
    assert statement(header_status_page, (header_status_transaction,)).transactions == (
        header_status_transaction,
    )
    valid_status_observation = statement(header_status_page, (header_status_transaction,))
    assert (
        StatementObservation.from_dict(valid_status_observation.to_dict())
        == valid_status_observation
    )

    wrong_status_context = NormalizedTransaction(
        **{
            **header_status_transaction.__dict__,
            "parse_evidence": (
                ParseEvidence(EvidenceField.DATE, (5,)),
                ParseEvidence(EvidenceField.DESCRIPTION, (6,)),
                ParseEvidence(EvidenceField.DIRECTION, (8,), (3,)),
                ParseEvidence(EvidenceField.STATUS, (7,), (3,)),
                ParseEvidence(EvidenceField.MAGNITUDE, (8,)),
            ),
        }
    )
    with pytest.raises(ContractError):
        statement(header_status_page, (wrong_status_context,))

    with pytest.raises(ContractError):
        ocr_page({10: 74}, activity_tables=(status_table,))

    pending_box = BoundingBox(Decimal("0.20"), Decimal("0.191"), Decimal("0.40"), Decimal("0.199"))
    with pytest.raises(ContractError):
        _activity_table(
            status_controls=(
                ActivityStatusControl(TransactionStatus.POSTED, status_box, (10,), (1,)),
                ActivityStatusControl(TransactionStatus.PENDING, pending_box, (11,), (1,)),
            )
        )

    below_box = BoundingBox(Decimal("0.20"), Decimal("0.31"), Decimal("0.40"), Decimal("0.34"))
    with pytest.raises(ContractError):
        _activity_table(
            status_controls=(
                ActivityStatusControl(TransactionStatus.POSTED, below_box, (11,), (1,)),
            )
        )

    with pytest.raises(ContractError):
        _activity_table(
            status_controls=(
                ActivityStatusControl(
                    TransactionStatus.POSTED,
                    BoundingBox(Decimal("0.20"), Decimal("0.20"), Decimal("0.40"), Decimal("0.25")),
                    (10,),
                    (1,),
                ),
            )
        )


def test_manifest_contract_rejects_invalid_dates_cash_basis_and_paths() -> None:
    document = DocumentSpec(AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, "statement.pdf")
    with pytest.raises(ContractError):
        InputManifest(
            1,
            date(2026, 6, 2),
            date(2026, 6, 1),
            "FY2027",
            CashBasis.AVAILABLE_INCLUDING_PENDING,
            (document,),
            "rules.json",
        )
    for unsafe_path in (
        "../outside.pdf",
        r"..\outside.pdf",
        r"nested\..\outside.pdf",
        "C:/outside.pdf",
        "C:drive-relative.pdf",
        "/posix-absolute.pdf",
        "//server/share/statement.pdf",
        r"\rooted-current-drive.pdf",
    ):
        with pytest.raises(PrivateInputError):
            DocumentSpec(AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, unsafe_path)


def test_in_memory_contracts_reject_untyped_enums_and_datetime_dates() -> None:
    with pytest.raises(ContractError):
        DocumentSpec("checking", DocumentKind.MONTHLY_STATEMENT, "statement.pdf")  # type: ignore[arg-type]
    with pytest.raises(ContractError):
        ClassificationRule(
            rule_id="bad-role",
            account_role=None,
            direction=Direction.DEBIT,
            matcher_kind=MatcherKind.EXACT,
            matcher_value="fictional-item",
            cash_role="not-a-cash-role",  # type: ignore[arg-type]
            category="Example category",
            pair_key=None,
        )
    with pytest.raises(ContractError):
        BalanceObservation(
            account_role=AccountRole.CHECKING,
            amount=Decimal("1.00"),
            observed_on=datetime(2026, 6, 1, 12, 0),
            boundary=BalanceBoundary.END_OF_DAY,
            kind=BalanceKind.CLOSING,
            includes_pending=False,
            source_sha256=SHA,
            locator=_locator(),
            extraction_method=ExtractionMethod.NATIVE,
            parse_evidence=_balance_parse_evidence(),
        )


def test_manifest_loader_requires_canonical_private_files_and_redacts_canaries(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    statements = tmp_path / "statements"
    statements.mkdir()
    (statements / "fictional.pdf").write_bytes(b"fake")
    _write_canonical(tmp_path / "rules.json", _rules_payload())
    manifest_path = tmp_path / "input.json"
    _write_canonical(manifest_path, _manifest_payload())
    manifest = load_input_manifest(manifest_path)
    assert manifest.documents[0].account_role is AccountRole.CHECKING

    canary_path = tmp_path / "canary-private-path.json"
    canary = _manifest_payload()
    canary["canary_identifier"] = "canary-private-description-17.23"
    _write_canonical(canary_path, canary)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(PrivateInputError) as captured:
        load_input_manifest(canary_path)
    visible = f"{captured.value} {caplog.text}"
    for private_fragment in (
        "canary-private",
        "canary_identifier",
        "fictional.pdf",
        "17.23",
    ):
        assert private_fragment not in visible


def test_manifest_loader_rejects_noncanonical_and_canonical_schema_errors(tmp_path: Path) -> None:
    statements = tmp_path / "statements"
    statements.mkdir()
    (statements / "fictional.pdf").write_bytes(b"fake")
    _write_canonical(tmp_path / "rules.json", _rules_payload())
    (tmp_path / "input.json").write_text('{\n  "schema_version":1\n}', encoding="utf-8")
    with pytest.raises(PrivateInputError):
        load_input_manifest(tmp_path / "input.json")

    payload = _manifest_payload()
    payload["unexpected"] = "fictional"
    _write_canonical(tmp_path / "input.json", payload)
    with pytest.raises(PrivateInputError):
        load_input_manifest(tmp_path / "input.json")

    payload = _manifest_payload()
    del payload["as_of_date"]
    _write_canonical(tmp_path / "input.json", payload)
    with pytest.raises(PrivateInputError):
        load_input_manifest(tmp_path / "input.json")

    for field, value in (
        ("reporting_start_date", "2026-6-1"),
        ("cash_basis", "collected"),
    ):
        payload = _manifest_payload()
        payload[field] = value
        _write_canonical(tmp_path / "input.json", payload)
        with pytest.raises(PrivateInputError):
            load_input_manifest(tmp_path / "input.json")

    for field, value in (
        ("account_role", "other"),
        ("document_kind", "unsupported"),
    ):
        payload = _manifest_payload()
        document = payload["documents"]
        assert isinstance(document, list)
        assert isinstance(document[0], dict)
        document[0][field] = value
        _write_canonical(tmp_path / "input.json", payload)
        with pytest.raises(PrivateInputError):
            load_input_manifest(tmp_path / "input.json")


def test_private_json_loaders_reject_directories_and_oversize_payloads(tmp_path: Path) -> None:
    manifest_directory = tmp_path / "manifest-directory"
    rules_directory = tmp_path / "rules-directory"
    manifest_directory.mkdir()
    rules_directory.mkdir()
    with pytest.raises(PrivateInputError):
        load_input_manifest(manifest_directory)
    with pytest.raises(PrivateInputError):
        load_rules(rules_directory)

    oversized_manifest = tmp_path / "oversized-manifest.json"
    oversized_rules = tmp_path / "oversized-rules.json"
    payload = b"x" * (PRIVATE_JSON_MAX_BYTES + 1)
    oversized_manifest.write_bytes(payload)
    oversized_rules.write_bytes(payload)
    with pytest.raises(PrivateInputError):
        load_input_manifest(oversized_manifest)
    with pytest.raises(PrivateInputError):
        load_rules(oversized_rules)


def test_rules_are_strict_and_duplicate_ids_fail(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    _write_canonical(rules_path, _rules_payload())
    rules = load_rules(rules_path)
    assert rules.classification_rules[0].cash_role is CashRole.SPENDING

    duplicate_rule: dict[str, object] = {
        "rule_id": "repeat",
        "account_role": "checking",
        "direction": "debit",
        "matcher_kind": "exact",
        "matcher_value": "fictional-item",
        "cash_role": "spending",
        "category": "Example category",
        "pair_key": None,
    }
    duplicate = _rules_payload()
    duplicate["classification_rules"] = [duplicate_rule, duplicate_rule.copy()]
    _write_canonical(rules_path, duplicate)
    with pytest.raises(PrivateInputError):
        load_rules(rules_path)

    invalid_role = _rules_payload()
    classification_values = invalid_role["classification_rules"]
    assert isinstance(classification_values, list)
    assert isinstance(classification_values[0], dict)
    classification_values[0]["cash_role"] = "unknown-role"
    _write_canonical(rules_path, invalid_role)
    with pytest.raises(PrivateInputError):
        load_rules(rules_path)

    unknown_top_level = _rules_payload()
    unknown_top_level["unexpected"] = "fictional"
    _write_canonical(rules_path, unknown_top_level)
    with pytest.raises(PrivateInputError):
        load_rules(rules_path)

    missing_top_level = _rules_payload()
    del missing_top_level["pair_resolutions"]
    _write_canonical(rules_path, missing_top_level)
    with pytest.raises(PrivateInputError):
        load_rules(rules_path)

    unknown_rule_field = _rules_payload()
    classification_values = unknown_rule_field["classification_rules"]
    assert isinstance(classification_values, list)
    assert isinstance(classification_values[0], dict)
    classification_values[0]["unexpected"] = "fictional"
    _write_canonical(rules_path, unknown_rule_field)
    with pytest.raises(PrivateInputError):
        load_rules(rules_path)

    missing_rule_field = _rules_payload()
    classification_values = missing_rule_field["classification_rules"]
    assert isinstance(classification_values, list)
    assert isinstance(classification_values[0], dict)
    del classification_values[0]["category"]
    _write_canonical(rules_path, missing_rule_field)
    with pytest.raises(PrivateInputError):
        load_rules(rules_path)

    invalid_pair_key = _rules_payload()
    classification_values = invalid_pair_key["classification_rules"]
    assert isinstance(classification_values, list)
    assert isinstance(classification_values[0], dict)
    classification_values[0]["cash_role"] = "transfer"
    classification_values[0]["pair_key"] = None
    _write_canonical(rules_path, invalid_pair_key)
    with pytest.raises(PrivateInputError):
        load_rules(rules_path)

    forbidden_pair_key = _rules_payload()
    classification_values = forbidden_pair_key["classification_rules"]
    assert isinstance(classification_values, list)
    assert isinstance(classification_values[0], dict)
    classification_values[0]["pair_key"] = "cash-move"
    _write_canonical(rules_path, forbidden_pair_key)
    with pytest.raises(PrivateInputError):
        load_rules(rules_path)

    malformed_selector = _rules_payload()
    malformed_selector["overlap_resolutions"] = [{"selected": {}, "rejected": {}}]
    _write_canonical(rules_path, malformed_selector)
    with pytest.raises(PrivateInputError):
        load_rules(rules_path)


def test_rules_loader_round_trips_valid_resolution_blocks(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    first = _selector().to_dict()
    second = {**first, "page_number": 2}
    payload = _rules_payload()
    payload["overlap_resolutions"] = [{"selected": first, "rejected": second}]
    payload["pair_resolutions"] = [{"first": first, "second": second, "action": "pair_as_transfer"}]
    payload["transaction_adjustments"] = [
        {
            "selector": first,
            "action": "exclude_from_current_board_spend",
            "reason": "previous period",
        }
    ]
    _write_canonical(rules_path, payload)
    rules = load_rules(rules_path)
    assert rules.to_dict() == payload
    assert canonical_json_text(rules.to_dict()) == canonical_json_text(payload)


def test_rules_loader_rejects_each_missing_exact_selector_discriminator(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    selector = _selector().to_dict()
    rejected = {**selector, "page_number": 2}
    for field in selector:
        incomplete = selector.copy()
        del incomplete[field]
        payload = _rules_payload()
        payload["overlap_resolutions"] = [{"selected": incomplete, "rejected": rejected}]
        _write_canonical(rules_path, payload)
        with pytest.raises(PrivateInputError):
            load_rules(rules_path)


def test_rules_loader_redacts_canary_selector_reason_path_and_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rules_path = tmp_path / "canary-rules-path.json"
    payload = _rules_payload()
    classifications = payload["classification_rules"]
    assert isinstance(classifications, list)
    assert isinstance(classifications[0], dict)
    classifications[0]["matcher_value"] = "canary-rule-matcher"
    selector = _selector().to_dict()
    selector["normalized_description"] = "canary-selector-description"
    selector["magnitude"] = "18.76"
    payload["transaction_adjustments"] = [
        {
            "selector": selector,
            "action": "not-an-adjustment",
            "reason": "canary-adjustment-reason",
        }
    ]
    _write_canonical(rules_path, payload)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(PrivateInputError) as captured:
        load_rules(rules_path)
    visible = f"{captured.value} {caplog.text}"
    for private_fragment in (
        "canary-rules-path",
        "canary-rule-matcher",
        "canary-selector-description",
        "canary-adjustment-reason",
        "18.76",
    ):
        assert private_fragment not in visible


def test_rule_pair_and_adjustment_contracts_are_exact() -> None:
    selector = _selector()
    rule = ClassificationRule(
        rule_id="transfer-rule",
        account_role=None,
        direction=Direction.DEBIT,
        matcher_kind=MatcherKind.PREFIX,
        matcher_value="fictional",
        cash_role=CashRole.TRANSFER,
        category="Transfer",
        pair_key="cash-move",
    )
    assert rule.matches(_transaction())
    second_selector = TransactionSelector(**{**selector.__dict__, "page_number": 2})
    pair = PairResolution(selector, second_selector, PairAction.PAIR_AS_TRANSFER)
    adjustment = TransactionAdjustment(
        selector,
        AdjustmentAction.EXCLUDE_FROM_CURRENT_BOARD_SPEND,
        "previous period",
    )
    rules = TreasurerRules(1, (rule,), (), (pair,), (adjustment,))
    assert rules.to_dict() == {
        "schema_version": 1,
        "classification_rules": [rule.to_dict()],
        "overlap_resolutions": [],
        "pair_resolutions": [
            {
                "first": selector.to_dict(),
                "second": second_selector.to_dict(),
                "action": "pair_as_transfer",
            }
        ],
        "transaction_adjustments": [
            {
                "selector": selector.to_dict(),
                "action": "exclude_from_current_board_spend",
                "reason": "previous period",
            }
        ],
    }
    for cash_role, pair_key in (
        (CashRole.TRANSFER, None),
        (CashRole.TRANSFER, " "),
        (CashRole.REVERSAL, None),
        (CashRole.SPENDING, "cash-move"),
    ):
        with pytest.raises(ContractError):
            ClassificationRule(
                rule_id="invalid-pair-key",
                account_role=None,
                direction=Direction.DEBIT,
                matcher_kind=MatcherKind.EXACT,
                matcher_value="fictional",
                cash_role=cash_role,
                category="Example category",
                pair_key=pair_key,
            )


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True)


def test_private_path_gate_blocks_tracked_and_nonignored_paths(tmp_path: Path) -> None:
    root = tmp_path / "private-git-repo"
    root.mkdir()
    _git(root, "init", "--quiet")
    (root / ".gitignore").write_text(
        "private/\nmanifests/private/\n*.eml\nCASE-TARGET.JSON\n", encoding="utf-8"
    )
    tracked = root / "tracked.json"
    tracked.write_text("{}", encoding="utf-8")
    tracked_stream_base = root / "README.md"
    tracked_stream_base.write_text("fictional", encoding="utf-8")
    casefold_tracked = root / "case-target.json"
    casefold_tracked.write_text("fictional", encoding="utf-8")
    _git(root, "add", ".gitignore", "tracked.json", "README.md")
    _git(root, "add", "--force", "case-target.json")
    nonignored = root / "nonignored.json"
    nonignored.write_text("{}", encoding="utf-8")
    ignored = root / "private" / "future-private-artifact.json"
    ignored.parent.mkdir()
    ignored.write_text("{}", encoding="utf-8")
    tracked_ignored = root / "private" / "tracked-private-artifact.json"
    tracked_ignored.write_text("{}", encoding="utf-8")
    _git(root, "add", "--force", "private/tracked-private-artifact.json")

    with pytest.raises(PrivateInputError):
        assert_private_path_allowed(tracked, require_file=True)
    with pytest.raises(PrivateInputError):
        assert_private_path_allowed(nonignored, require_file=True)
    assert assert_private_path_allowed(ignored, require_file=True) == ignored.resolve()
    with pytest.raises(PrivateInputError):
        assert_private_path_allowed(tracked_ignored, require_file=True)
    external = tmp_path / "external-private-artifact.json"
    external.write_text("{}", encoding="utf-8")
    assert assert_private_path_allowed(external, require_file=True) == external.resolve()
    with pytest.raises(PrivateInputError):
        assert_private_path_allowed(root / "README.md:private.eml", allow_missing=True)
    with pytest.raises(PrivateInputError):
        assert_private_path_allowed(root / "CASE-TARGET.JSON", allow_missing=True)

    manifest_parent = root / "manifests"
    (manifest_parent / "private").mkdir(parents=True)
    statement = manifest_parent / "private" / "statement.pdf"
    statement.write_bytes(b"fictional")
    assert (
        resolve_private_relative_path(manifest_parent, "private/statement.pdf")
        == statement.resolve()
    )


def test_relative_path_is_canonical_and_rejects_cross_platform_aliases(tmp_path: Path) -> None:
    base = tmp_path / "private"
    base.mkdir()
    source = base / "source.json"
    source.write_text("{}", encoding="utf-8")
    assert resolve_private_relative_path(base, "source.json") == source.resolve()
    nested = base / "nested"
    nested.mkdir()
    nested_source = nested / "source.json"
    nested_source.write_text("{}", encoding="utf-8")
    assert resolve_private_relative_path(base, r"nested\source.json") == nested_source.resolve()
    assert (
        DocumentSpec(
            AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, r"nested\source.json"
        ).relative_path
        == "nested/source.json"
    )
    for unsafe_path in (
        "../source.json",
        r"..\source.json",
        r"nested\..\source.json",
        "C:/outside.json",
        "C:drive-relative.json",
        "/posix-absolute.json",
        "//server/share/statement.json",
        r"\rooted-current-drive.json",
        "README.md:private.eml",
        "NUL",
        "nested/CON.txt",
        "nested/trailing-dot.",
        "nested/trailing-space ",
        "nested/short~1.json",
        r"\\?\C:\device-path.json",
    ):
        with pytest.raises(PrivateInputError):
            DocumentSpec(AccountRole.CHECKING, DocumentKind.MONTHLY_STATEMENT, unsafe_path)
    for forbidden_character in '<>"|?*':
        with pytest.raises(PrivateInputError):
            DocumentSpec(
                AccountRole.CHECKING,
                DocumentKind.MONTHLY_STATEMENT,
                f"nested/forbidden{forbidden_character}.json",
            )
    link = base / "linked.json"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(PrivateInputError):
        resolve_private_relative_path(base, "linked.json")


def test_private_path_gate_rejects_unc_and_dangling_reparse_before_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_filesystem_access(_path: Path) -> None:
        pytest.fail("unsafe Windows path reached filesystem checks")

    with monkeypatch.context() as context:
        context.setattr(treasurer_models, "_assert_no_link_segments", unexpected_filesystem_access)
        with pytest.raises(PrivateInputError):
            assert_private_path_allowed(Path(r"\\server\share\private.json"), allow_missing=True)
        with pytest.raises(PrivateInputError):
            resolve_private_relative_path(Path(r"\\server\share"), "private.json")

    base = tmp_path / "private"
    base.mkdir()
    dangling = base / "dangling-junction"
    monkeypatch.setattr(
        treasurer_models,
        "_is_reparse_point",
        lambda path: path == dangling,
    )
    with pytest.raises(PrivateInputError):
        assert_private_path_allowed(dangling / "candidate.json", allow_missing=True)


def test_private_path_gate_sanitizes_git_context_and_requires_containing_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "primary-repo"
    root.mkdir()
    _git(root, "init", "--quiet")
    (root / ".gitignore").write_text("private/\n", encoding="utf-8")
    tracked = root / "tracked.json"
    tracked.write_text("{}", encoding="utf-8")
    _git(root, "add", ".gitignore", "tracked.json")
    alternate = tmp_path / "alternate-repo"
    alternate.mkdir()
    _git(alternate, "init", "--quiet")

    with monkeypatch.context() as context:
        context.setenv("GIT_DIR", str(alternate / ".git"))
        context.setenv("GIT_WORK_TREE", str(alternate))
        with pytest.raises(PrivateInputError):
            assert_private_path_allowed(tracked, require_file=True)

    monkeypatch.setattr(treasurer_models, "_git_root_for", lambda _path: alternate)
    with pytest.raises(PrivateInputError):
        assert_private_path_allowed(tracked, require_file=True)


def test_sanitized_git_environment_ignores_external_config_locations() -> None:
    environment = treasurer_models._sanitized_git_environment(
        {
            "PATH": "fictional-path",
            "GIT_DIR": "fictional-git-dir",
            "HOME": "fictional-home",
            "USERPROFILE": "fictional-profile",
            "HOMEDRIVE": "fictional-drive",
            "HOMEPATH": "fictional-path-part",
            "XDG_CONFIG_HOME": "fictional-xdg-home",
            "XDG_CONFIG_DIRS": "fictional-xdg-dirs",
        }
    )
    assert environment["PATH"] == "fictional-path"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    for external_key in (
        "GIT_DIR",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "XDG_CONFIG_HOME",
        "XDG_CONFIG_DIRS",
    ):
        assert external_key not in environment


def test_private_loader_fails_closed_for_deep_json_and_git_operational_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deeply_nested = "[" * 101 + "0" + "]" * 101
    deep_path = tmp_path / "deep.json"
    deep_path.write_text(deeply_nested, encoding="utf-8")
    with pytest.raises(PrivateInputError):
        load_input_manifest(deep_path)

    root = tmp_path / "git-repo"
    root.mkdir()
    _git(root, "init", "--quiet")
    (root / ".gitignore").write_text("private.json\n", encoding="utf-8")
    private_path = root / "private.json"
    private_path.write_text("{}", encoding="utf-8")
    results = iter((128, 0))
    monkeypatch.setattr(
        treasurer_models,
        "_git_returncode",
        lambda *_args, **_kwargs: next(results),
    )
    with pytest.raises(PrivateInputError):
        assert_private_path_allowed(private_path, require_file=True)


def test_private_path_gate_fails_closed_when_git_is_unavailable_inside_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "git-repo"
    root.mkdir()
    _git(root, "init", "--quiet")
    (root / ".gitignore").write_text("private/\n", encoding="utf-8")
    private_path = root / "private" / "artifact.json"
    private_path.parent.mkdir()
    private_path.write_text("{}", encoding="utf-8")

    def unavailable_git(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable_git)
    with pytest.raises(PrivateInputError):
        assert_private_path_allowed(private_path, require_file=True)
