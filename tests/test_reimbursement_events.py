"""Synthetic tests for strict private reimbursement anchors and pure event parsers."""

from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from pta_finance import reimbursement_events


def _selector(ref: str = "NEW-01") -> dict[str, str]:
    return {
        "review_key": f"submission:v1:{'a' * 64}",
        "ref": ref,
        "form_label": "",
    }


def _anchors() -> dict[str, object]:
    return {
        "schema_version": 1,
        "actors": {
            "payment_operators": ["payments@example.invalid"],
            "secondary_approvers": ["reviewer@example.invalid"],
        },
        "thread_anchors": [
            {
                "message_id": "<case@example.invalid>",
                "purpose": "CASE",
                "tickets": [_selector()],
            }
        ],
        "direct_links": [],
        "operator_reviews": [
            {
                "ticket": _selector(),
                "record_decision": True,
                "items": [
                    {
                        "source_index": 1,
                        "status": "C",
                        "why": "The synthetic claimed and receipt amounts differ.",
                        "reviewed_amount": "",
                    }
                ],
                "action": "Resolve the synthetic amount question",
                "block": "Confirm the exact amount.",
                "asks": ["Which fictional amount is intended?"],
                "note": "Synthetic operator review.",
                "email_questions": ["Which fictional amount is intended?"],
                "email_context": "",
            }
        ],
    }


def _reference_sha256(reference: str) -> str:
    return hashlib.sha256(reference.encode("utf-8")).hexdigest()


def _anchors_v2() -> dict[str, object]:
    value = _anchors()
    value["schema_version"] = 2
    value["payment_links"] = [
        {
            "message_id": "<payment@example.invalid>",
            "bindings": [
                {
                    "ticket": _selector(),
                    "reference_sha256": _reference_sha256("EXAMPLE-PAY-1000"),
                }
            ],
        }
    ]
    value["operator_payments"] = [
        {
            "record_payment": True,
            "ticket": _selector("NEW-02"),
            "date": "2030-09-09",
            "amount": "8.25",
            "reference": "EXAMPLE-OP-825",
            "audit_note": "Synthetic payment confirmed outside the archived mailbox.",
        }
    ]
    return value


def test_anchor_config_is_strict_normalized_and_digestable(tmp_path: Path) -> None:
    path = tmp_path / "anchors.json"
    path.write_text(json.dumps(_anchors()), encoding="utf-8")

    first = reimbursement_events.load_anchor_config(path)
    second = reimbursement_events.load_anchor_config(path)

    assert first == second
    assert first.thread_anchors[0].message_id == "<case@example.invalid>"
    assert first.operator_reviews[0].items[0].status == "C"
    assert len(first.sha256) == 64
    assert first.operator_reviews[0].evidence_sha256 != first.sha256


def test_anchor_config_rejects_unknown_keys_and_duplicate_message_ids(tmp_path: Path) -> None:
    value = _anchors()
    value["private_canary"] = "must not echo"  # type: ignore[assignment]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(reimbursement_events.ReimbursementEventError) as caught:
        reimbursement_events.load_anchor_config(path)
    assert "must not echo" not in str(caught.value)

    value = _anchors()
    value["thread_anchors"] = [  # type: ignore[assignment]
        value["thread_anchors"][0],  # type: ignore[index]
        value["thread_anchors"][0],  # type: ignore[index]
    ]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(reimbursement_events.ReimbursementEventError, match="must not contain"):
        reimbursement_events.load_anchor_config(path)


def test_schema_v1_anchor_digests_remain_byte_compatible(tmp_path: Path) -> None:
    path = tmp_path / "anchors.json"
    path.write_text(json.dumps(_anchors()), encoding="utf-8")

    assert (
        reimbursement_events.empty_anchor_config().sha256
        == "84e476c8841110d7adff41194ee5440c83dd1624b9b13dff97171c126a790d1b"
    )
    assert (
        reimbursement_events.load_anchor_config(path).sha256
        == "1a50651ee8832849919188fbd1e80850a00e7a9b7d181fca8f4f74358ec32612"
    )


def test_schema_v2_payment_lanes_are_strict_and_normalized(tmp_path: Path) -> None:
    path = tmp_path / "anchors.json"
    path.write_text(json.dumps(_anchors_v2()), encoding="utf-8")

    anchors = reimbursement_events.load_anchor_config(path)

    assert anchors.payment_links[0].message_id == "<payment@example.invalid>"
    assert anchors.payment_links[0].bindings[0].reference_sha256 == _reference_sha256(
        "EXAMPLE-PAY-1000"
    )
    assert anchors.operator_payments[0].amount == "8.25"
    assert anchors.operator_payments[0].reference == "EXAMPLE-OP-825"


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown-key",
        "colliding-message-id",
        "empty-bindings",
        "duplicate-ticket",
        "duplicate-reference",
        "malformed-reference-digest",
        "duplicate-operator-ticket",
        "duplicate-operator-reference",
        "cross-lane-ticket",
        "cross-lane-reference",
    ],
)
def test_schema_v2_rejects_unknown_colliding_empty_duplicate_and_malformed_values(
    tmp_path: Path, mutation: str
) -> None:
    value = copy.deepcopy(_anchors_v2())
    payment_link = value["payment_links"][0]  # type: ignore[index]
    binding = payment_link["bindings"][0]  # type: ignore[index]
    if mutation == "unknown-key":
        value["unknown"] = "synthetic"
    elif mutation == "colliding-message-id":
        payment_link["message_id"] = "<case@example.invalid>"  # type: ignore[index]
    elif mutation == "empty-bindings":
        payment_link["bindings"] = []  # type: ignore[index]
    elif mutation == "duplicate-ticket":
        payment_link["bindings"] = [binding, copy.deepcopy(binding)]  # type: ignore[index]
        payment_link["bindings"][1]["reference_sha256"] = _reference_sha256(  # type: ignore[index]
            "EXAMPLE-PAY-1001"
        )
    elif mutation == "duplicate-reference":
        second = copy.deepcopy(binding)
        second["ticket"] = _selector("NEW-03")
        payment_link["bindings"] = [binding, second]  # type: ignore[index]
    elif mutation == "malformed-reference-digest":
        binding["reference_sha256"] = "not-a-sha256"  # type: ignore[index]
    elif mutation == "duplicate-operator-ticket":
        second_payment = copy.deepcopy(value["operator_payments"][0])  # type: ignore[index]
        second_payment["reference"] = "EXAMPLE-OP-826"
        value["operator_payments"].append(second_payment)  # type: ignore[union-attr]
    elif mutation == "duplicate-operator-reference":
        second_payment = copy.deepcopy(value["operator_payments"][0])  # type: ignore[index]
        second_payment["ticket"] = _selector("NEW-03")
        value["operator_payments"].append(second_payment)  # type: ignore[union-attr]
    elif mutation == "cross-lane-ticket":
        value["operator_payments"][0]["ticket"] = _selector()  # type: ignore[index]
    else:
        value["operator_payments"][0]["reference"] = "EXAMPLE-PAY-1000"  # type: ignore[index]
    path = tmp_path / "anchors.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(reimbursement_events.ReimbursementEventError):
        reimbursement_events.load_anchor_config(path)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("record_payment", None),
        ("ticket", None),
        ("date", None),
        ("amount", None),
        ("reference", None),
        ("audit_note", None),
        ("record_payment", False),
        ("date", "09/09/2030"),
        ("amount", "8.2"),
        ("reference", "invalid"),
        ("audit_note", "   "),
    ],
)
def test_schema_v2_operator_payment_requires_every_exact_field(
    tmp_path: Path, field: str, replacement: object
) -> None:
    value = copy.deepcopy(_anchors_v2())
    payment = value["operator_payments"][0]  # type: ignore[index]
    if replacement is None:
        del payment[field]  # type: ignore[index]
    else:
        payment[field] = replacement  # type: ignore[index]
    path = tmp_path / "anchors.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(reimbursement_events.ReimbursementEventError):
        reimbursement_events.load_anchor_config(path)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Yes, please.", "POSITIVE"),
        ("Not approved.", "NEGATIVE"),
        ("Yes, but only after another review.", None),
        ("> Yes, please.", None),
    ],
)
def test_approval_reply_classifier_is_deliberately_narrow(text: str, expected: str | None) -> None:
    assert reimbursement_events.classify_approval_reply(text) == expected


def test_approval_reply_classifier_accepts_leading_assessment_agreement_only() -> None:
    sentence_reply = """\
Hello Example Reviewer,

I agree with your assessment. If a synthetic follow-up is needed, use the existing process.

Thank you,
Example Approver
"""
    conjunction_reply = """\
Hi Example Reviewer.

I agree with your assessment and I would like to add a synthetic note if follow-up is needed.

Thank you,
Example Approver
"""

    assert reimbursement_events.classify_approval_reply(sentence_reply) == "POSITIVE"
    assert reimbursement_events.classify_approval_reply(conjunction_reply) == "POSITIVE"


@pytest.mark.parametrize(
    "text",
    [
        "I agree with your assessment, but only for NEW-01.",
        "I agree with your assessment. Except for the second synthetic item.",
        "I agree with your assessment. I dispute the held recommendation.",
        "I agree with your assessment. Decline the remaining synthetic item.",
        "I agree with your assessment. Do-not approve the second ticket.",
        "I agree with your assessment. The second ticket is not-approved.",
        "I disagree with your assessment.",
        "I agree with your assessment and only the first synthetic ticket.",
        "I agree with your assessment; however, check the second synthetic ticket.",
        "I agree with your assessment, though one synthetic item needs review.",
        "I agree with your assessment, although one synthetic item needs review.",
        "I agree with your assessment unless the amount changes.",
        "I agree with your assessment subject to another review.",
        "I agree with your assessment provided the first synthetic ticket is excluded.",
        "I agree with your assessment and",
        "I agree with your assessment and not the second synthetic ticket.",
        "I agree with your assessment and I don't approve the second synthetic ticket.",
        "I agree with your assessment and I cannot approve the second synthetic ticket.",
        "I agree with your assessment and can't approve the second synthetic ticket.",
        "I agree with your assessment and won’t approve the second synthetic ticket.",
        "I agree with your assessment and no thanks.",
        "I agree with your assessment and reject the second synthetic ticket.",
        "I agree with your assessment and exclude the second synthetic ticket.",
        "I agree with your assessment and without the second synthetic ticket.",
        "I agree with your assessment and oppose the second synthetic ticket.",
        "I agree with your assessment and apart from the second synthetic ticket.",
        "I agree with your assessment and with the exception of the second synthetic ticket.",
        (
            "Hi Example Reviewer, but do not approve the second synthetic ticket.\n\n"
            "I agree with your assessment."
        ),
        "The reviewer said: I agree with your assessment.",
        "For context:\nI agree with your assessment.",
    ],
)
def test_approval_reply_classifier_rejects_modified_or_embedded_agreement(text: str) -> None:
    assert reimbursement_events.classify_approval_reply(text) is None


def test_payment_parser_requires_one_amount_and_one_reference() -> None:
    parsed = reimbursement_events.parse_payment_evidence(
        "Payment sent: $12.34. Confirmation EXAMPLE-123."
    )
    assert parsed is not None
    assert f"{parsed.amount:.2f}" == "12.34"
    assert parsed.reference == "EXAMPLE-123"
    assert reimbursement_events.parse_payment_evidence("Payment sent, thanks.") is None
    assert (
        reimbursement_events.parse_payment_evidence(
            "Paid $12.34 or $13.00; confirmation EXAMPLE-123."
        )
        is None
    )
    assert reimbursement_events.parse_payment_evidence("Check was sent for $10.00.") is None
    assert reimbursement_events.parse_payment_evidence("The check has $10.00 on it.") is None
    assert reimbursement_events.parse_payment_evidence("Confirmation was for $10.00.") is None
    assert (
        reimbursement_events.parse_payment_evidence(
            "Payment was not sent: $10.00. Confirmation ABC123."
        )
        is None
    )
    assert (
        reimbursement_events.parse_payment_evidence(
            "Do not mark paid. Payment $10.00 reference ABC123 was cancelled."
        )
        is None
    )
    assert (
        reimbursement_events.parse_payment_evidence(
            "FYI, the requestor wrote: Payment sent $10.00 confirmation ABC123."
        )
        is None
    )
    assert (
        reimbursement_events.parse_payment_evidence(
            "I sent the reimbursement request for $10.00. Reference ABC123."
        )
        is None
    )
    assert (
        reimbursement_events.parse_payment_evidence(
            "The request was transferred to accounting for $10.00. Reference ABC123."
        )
        is None
    )
    assert (
        reimbursement_events.parse_payment_evidence(
            "Payment is pending for $10.00. Reference ABC123; I sent the form."
        )
        is None
    )
    assert (
        reimbursement_events.parse_payment_evidence("Was this paid? $10.00 reference ABC123.")
        is None
    )
    assert (
        reimbursement_events.parse_payment_evidence(
            "Please confirm whether payment was sent for $10.00, reference ABC123."
        )
        is None
    )
    numbered_check = reimbursement_events.parse_payment_evidence(
        "Check number EXAMPLE-100 was sent for $10.00."
    )
    assert numbered_check is not None
    assert numbered_check.reference == "EXAMPLE-100"


def test_payment_parser_accepts_only_strict_bare_zelle_confirmation_shape() -> None:
    text = """\
Hello,
Ok great, paid, confirmation info is below.
Synthetic signoff
Example Recipient
Zelle - 5550001234
Synthetic reimbursement description
ABCD99E9FGH9
10.00
"""
    parsed = reimbursement_events.parse_payment_evidence(text)
    assert parsed == reimbursement_events.PaymentEvidence(
        amount=Decimal("10.00"), reference="ABCD99E9FGH9"
    )

    assert reimbursement_events.parse_payment_evidence(text.replace("paid", "noted")) is None
    assert (
        reimbursement_events.parse_payment_evidence(text.replace("info is below", "details follow"))
        is None
    )
    assert (
        reimbursement_events.parse_payment_evidence(text.replace("Zelle - 5550001234", "")) is None
    )
    assert reimbursement_events.parse_payment_evidence(text + "\nZXCV12B3NM45\n") is None
    assert reimbursement_events.parse_payment_evidence(text + "\n11.00\n") is None


def test_exact_generated_single_and_sent_batch_payment_grammars() -> None:
    generated = """\
Hello Morgan,

Your $1,234.56 reimbursement has been approved and sent by Zelle.
Zelle confirmation: EXAMPLE-ZELLE-123456

Thank you!
Example Treasurer Team
"""
    sent_single = """\
Hello,

Your $10.00 reimbursement has been approved and sent by Zelle.
Zelle confirmation:

Morgan Example
Zelle - synthetic destination
Classroom Supplies
ABCD99E9FGH9
10.00

Thank you,
Example Treasurer
Example Association
"""
    sent_group = """\
Your Reimbursements have been approved and sent by Zelle.

Zelle confirmations

Morgan Example
Zelle - first synthetic destination
Classroom Supplies
ZXCV12B3NM45
1,234.56

Riley Example
Zelle - second synthetic destination
QWER98T7YUI6
8.25

Thank you!
Example Treasurer
Example Association
"""
    sent_group_three = """\
Your reimbursements have been approved and sent by Zelle.

Confirmation:

Morgan Example
Zelle - first synthetic destination
FIRSTREF1000
10.00

Riley Example
Zelle - second synthetic destination
Classroom Supplies
SECONDREF2000
20.00

Taylor Example
Zelle - third synthetic destination
THIRDREF3000
30.00

Thank you,
Example Treasurer
Example Association
"""

    assert reimbursement_events.parse_payment_evidence_blocks(generated) == (
        reimbursement_events.PaymentEvidence(
            amount=Decimal("1234.56"), reference="EXAMPLE-ZELLE-123456"
        ),
    )
    assert reimbursement_events.parse_payment_evidence_blocks(sent_single) == (
        reimbursement_events.PaymentEvidence(amount=Decimal("10.00"), reference="ABCD99E9FGH9"),
    )
    assert reimbursement_events.parse_payment_evidence_blocks(sent_group) == (
        reimbursement_events.PaymentEvidence(amount=Decimal("1234.56"), reference="ZXCV12B3NM45"),
        reimbursement_events.PaymentEvidence(amount=Decimal("8.25"), reference="QWER98T7YUI6"),
    )
    assert reimbursement_events.parse_payment_evidence_blocks(sent_group_three) == (
        reimbursement_events.PaymentEvidence(amount=Decimal("10.00"), reference="FIRSTREF1000"),
        reimbursement_events.PaymentEvidence(amount=Decimal("20.00"), reference="SECONDREF2000"),
        reimbursement_events.PaymentEvidence(amount=Decimal("30.00"), reference="THIRDREF3000"),
    )


def test_sent_batch_accepts_exact_classroom_support_thank_you_envelope() -> None:
    text = """\
Your Reimbursements have been approved and sent by Zelle.

Zelle confirmations

Morgan Example
Zelle - first synthetic destination
Classroom Supplies
LONGTHANK1000
10.00

Riley Example
Zelle - second synthetic destination
LONGTHANK2000
20.00

Thank you for supporting our classrooms!
Example Treasurer
Example Association
"""

    assert reimbursement_events.parse_payment_evidence_blocks(text) == (
        reimbursement_events.PaymentEvidence(amount=Decimal("10.00"), reference="LONGTHANK1000"),
        reimbursement_events.PaymentEvidence(amount=Decimal("20.00"), reference="LONGTHANK2000"),
    )
    assert (
        reimbursement_events.parse_payment_evidence_blocks(
            text.replace("supporting our classrooms", "supporting the classrooms")
        )
        is None
    )


@pytest.mark.parametrize(
    "text",
    [
        "Your $10.00 reimbursement has not been approved and sent by Zelle.\n"
        "Zelle confirmation: EXAMPLE-ZELLE-1000",
        "Was your $10.00 reimbursement approved and sent by Zelle?\n"
        "Zelle confirmation: EXAMPLE-ZELLE-1000",
        "The requestor wrote: Your $10.00 reimbursement has been approved and sent by Zelle.\n"
        "Zelle confirmation: EXAMPLE-ZELLE-1000",
        "Your $10.00 reimbursement will be approved and sent by Zelle.\n"
        "Zelle confirmation: EXAMPLE-ZELLE-1000",
        "Your $10.00 reimbursement has been approved and sent by Zelle.\n"
        "Zelle confirmation: EXAMPLE-ZELLE-1000\nReference: EXTRA-2000",
        "Your $10.00 reimbursement has been approved and sent by Zelle.\n"
        "Zelle confirmation: EXAMPLE-ZELLE-1000\n$1.00",
    ],
)
def test_new_payment_grammars_reject_altered_negative_question_attributed_and_extra_values(
    text: str,
) -> None:
    assert reimbursement_events.parse_payment_evidence_blocks(text) is None


def test_proposal_protocol_requires_exact_sections_and_action_counts() -> None:
    parsed = reimbursement_events.parse_proposal_recommendations(
        """\
NEW-01
Approve as is
Clarification: confirm the synthetic amount
NEW-02
Approve the fictional item
""",
        expected_refs=["NEW-01", "NEW-02"],
    )
    assert parsed is not None
    assert [item.statuses for item in parsed] == [("A", "C"), ("A",)]
    assert not any(item.all_items for item in parsed)
    assert (
        reimbursement_events.parse_proposal_recommendations(
            "NEW-01\nApprove as is\n", expected_refs=["NEW-01", "NEW-02"]
        )
        is None
    )


def test_proposal_protocol_expands_exact_grouped_approve_as_is_sections() -> None:
    expected = ["NEW-01", "NEW-02", "NEW-03", "NEW-04"]
    parsed = reimbursement_events.parse_proposal_recommendations(
        """\
NEW-02,NEW-03, NEW-04
* Approve as is
NEW-01
Clarification: confirm the synthetic amount
Approve the corrected fictional line
""",
        expected_refs=expected,
    )

    assert parsed is not None
    by_ref = {item.ref: item for item in parsed}
    assert by_ref["NEW-01"].statuses == ("C", "A")
    assert by_ref["NEW-01"].all_items is False
    assert all(by_ref[ref].statuses == ("A",) for ref in expected[1:])
    assert all(by_ref[ref].all_items for ref in expected[1:])

    assert (
        reimbursement_events.parse_proposal_recommendations(
            "NEW-01,NEW-02\nApprove one synthetic line\n",
            expected_refs=["NEW-01", "NEW-02"],
        )
        is None
    )
    assert (
        reimbursement_events.parse_proposal_recommendations(
            "NEW-01,NEW-02\nApprove as is\nClarification: extra line\n",
            expected_refs=["NEW-01", "NEW-02"],
        )
        is None
    )
