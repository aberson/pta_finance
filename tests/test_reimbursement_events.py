"""Synthetic tests for strict private reimbursement anchors and pure event parsers."""

from __future__ import annotations

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
