from __future__ import annotations

import copy
import hashlib
import html
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from pta_finance import reimbursement_events, reimbursement_report


def _item(
    key: str,
    index: int,
    amount: str,
    status: str,
    *,
    category: str = "Classroom Supplies",
    description: str = "Fictional classroom materials",
) -> dict[str, object]:
    return {
        "item_key": key,
        "source_index": index,
        "source_date": "2026-08-01",
        "source_description": description,
        "source_amount": amount,
        "canonical_category": category,
        "display_date": "",
        "display_item": "",
        "reviewed_amount": "",
        "status": status,
        "why": "Fictional evidence supports this test disposition.",
    }


def _ticket(
    *,
    review_key: str,
    ref: str,
    order: int,
    origin: str,
    status: str,
    decision: str,
    payment_method: str,
    items: list[dict[str, object]],
    workflow: str = "ACTIVE",
    payment_status: str = "NOT_PAID",
    messages: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    categories = list(
        dict.fromkeys(
            str(item["canonical_category"]) for item in items if item["canonical_category"]
        )
    )
    mapped_total = sum(float(str(item["source_amount"])) for item in items)
    questions = ["Please confirm the fictional approval detail."] if status == "C" else []
    if messages is None:
        messages = [{"kind": "draft", "date": "", "mode": "generated", "body": ""}]
    return {
        "review_key": review_key,
        "ref": ref,
        "form_label": "",
        "origin": origin,
        "display_order": order,
        "requestor_name": f"Fictional Person {order}",
        "form_type": "Example Reimbursement Form",
        "submitted": "2026-08-02",
        "submitted_label": "",
        "payment_method": payment_method,
        "source_evidence_sha256": f"{order:x}" * 64,
        "source": {
            "stated_total": f"{mapped_total:.2f}",
            "mapped_total": f"{mapped_total:.2f}",
            "categories": categories,
            "flags": [],
        },
        "live": {
            "workflow_state": workflow,
            "decision": decision,
            "payment_status": payment_status,
            "payment_date": "2026-08-03" if payment_status != "NOT_PAID" else "",
            "confirmations": ["FICTIONAL-CONFIRMATION"] if payment_status != "NOT_PAID" else [],
        },
        "review": {
            "status": status,
            "action": "Complete the fictional next step",
            "block": "Use the structured test evidence.",
            "asks": questions,
            "note": "Fictional reviewer note.",
            "email_questions": questions,
            "email_context": "",
        },
        "items": items,
        "messages": messages,
        "archive_note": "Fictional archive note." if workflow == "SETTLED" else "",
    }


def _bundle() -> dict[str, object]:
    approved = _ticket(
        review_key="submission:v1:" + "a" * 64,
        ref="NEW-01",
        order=1,
        origin="submission",
        status="A",
        decision="UNREVIEWED",
        payment_method="Zelle",
        items=[
            _item(
                "submission-a:line:1",
                1,
                "10.00",
                "A",
                description='<script data-test="unsafe">alert(1)</script>',
            )
        ],
    )
    clarification = _ticket(
        review_key="submission:v1:" + "b" * 64,
        ref="NEW-02",
        order=2,
        origin="submission",
        status="C",
        decision="UNREVIEWED",
        payment_method="Check",
        items=[
            _item("submission-b:line:1", 1, "5.00", "A"),
            _item(
                "submission-b:line:2",
                2,
                "7.00",
                "C",
                category="Equipment",
                description="Fictional reusable equipment",
            ),
        ],
    )
    unreviewed = _ticket(
        review_key="submission:v1:" + "c" * 64,
        ref="NEW-03",
        order=3,
        origin="submission",
        status="Q",
        decision="UNREVIEWED",
        payment_method="Electronic transfer",
        items=[_item("submission-c:line:1", 1, "3.00", "Q")],
    )
    closed = _ticket(
        review_key="legacy:v1:p-001",
        ref="P-001",
        order=4,
        origin="legacy",
        status="A",
        decision="APPROVED",
        payment_method="Zelle",
        workflow="SETTLED",
        payment_status="PAID_PRIOR",
        items=[_item("legacy-p001:line:1", 1, "20.00", "A", category="")],
        messages=[
            {
                "kind": "sent",
                "date": "2026-08-03",
                "mode": "verbatim",
                "body": "Hello Fictional Person,\n\nThis test payment was sent.",
            }
        ],
    )
    return {
        "schema_version": 1,
        "report": {
            "title": "Example Reimbursement Review",
            "eyebrow": "Example Organization · Treasurer",
            "subtitle": "Structured fictional review queue.",
            "organization": "Example Organization",
            "email_signoff": ["Thank you!", "Example Treasurer Team"],
            "logo_data_uri": "",
            "confirmed_outstanding": "15.00",
            "cutoff_date": "2026-06-01",
            "policy_version": "example-v1",
            "as_of_date": "2026-08-04",
        },
        "provenance": {
            "mapped_sha256": "1" * 64,
            "policy_sha256": "2" * 64,
            "source_snapshot_sha256": "3" * 64,
            "accounted_review_keys": sorted(
                [
                    "submission:v1:" + "a" * 64,
                    "submission:v1:" + "b" * 64,
                    "submission:v1:" + "c" * 64,
                    "submission:v1:" + "d" * 64,
                ]
            ),
        },
        "source_summary": {
            "mapped_rows": 5,
            "mapped_submissions": 4,
            "mapped_total": "50.00",
            "first_received": "2026-08-02",
            "last_received": "2026-08-03",
        },
        "tickets": [approved, clarification, unreviewed, closed],
        "appendix": {
            "amendments": [
                {
                    "title": "Fictional policy interpretation",
                    "body": "Apply the example rule consistently.",
                    "effect": "Keeps this fixture deterministic.",
                    "scope": "Fictional tests only.",
                }
            ],
            "cfo_checks": [
                {
                    "ticket": "NEW-02",
                    "question": "Is the example evidence complete?",
                    "answer": "The fixture intentionally leaves one clarification.",
                }
            ],
            "excluded": [
                {"label": "Out-of-scope example", "detail": "Intentionally excluded in tests."}
            ],
            "defects": ["Replace fictional inputs before operational use."],
        },
    }


def _write_bundle(path: Path, bundle: dict[str, object]) -> None:
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")


def _seal_record(record: dict[str, object]) -> None:
    record.pop("record_sha256", None)
    record["record_sha256"] = hashlib.sha256(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _recorded_payment_bundle(*, source_type: str = "MAIL") -> dict[str, object]:
    bundle = reimbursement_report.migrate_bundle(_bundle())
    ticket = bundle["tickets"][0]
    ticket["items"][0]["reviewed_amount"] = "9.00"
    ticket["live"] = {
        "workflow_state": "SETTLED",
        "decision": "APPROVED",
        "payment_status": "PAID",
        "payment_date": "2026-08-04",
        "confirmations": ["Reference EXAMPLE-100; amount $9.00"],
    }
    ticket["messages"] = []
    ticket["archive_note"] = "Synthetic payment event test."
    evidence_digest = "d" * 64
    if source_type == "MAIL":
        message_id = "<payment-report@example.invalid>"
        evidence_key = "mail:v1:" + hashlib.sha256(message_id.encode()).hexdigest()
        occurred_at = "2026-08-04T12:00:00+00:00"
        top_authored_sha256 = "e" * 64
    else:
        assert source_type == "OPERATOR_PAYMENT"
        message_id = ""
        evidence_key = f"operator-payment:v1:{evidence_digest}"
        occurred_at = ""
        top_authored_sha256 = evidence_digest
    evidence: dict[str, object] = {
        "evidence_key": evidence_key,
        "source_type": source_type,
        "message_id": message_id,
        "in_reply_to": [],
        "references": [],
        "occurred_on": "2026-08-04",
        "occurred_at": occurred_at,
        "top_authored_sha256": top_authored_sha256,
        "evidence_sha256": evidence_digest,
        "attachments": [],
    }
    _seal_record(evidence)
    ticket_key = str(ticket["review_key"])
    event: dict[str, object] = {
        "event_key": "event:v1:"
        + hashlib.sha256(f"{evidence_key}\0{ticket_key}\0PAYMENT_RECORDED".encode()).hexdigest(),
        "evidence_key": evidence_key,
        "ticket_review_key": ticket_key,
        "kind": "PAYMENT_RECORDED",
        "occurred_on": "2026-08-04",
        "occurred_at": occurred_at,
        "evidence_sha256": evidence_digest,
        "summary": "Synthetic configured-operator payment recorded.",
        "amount": "9.00",
        "reference": "EXAMPLE-100",
        "discrepancy": "",
    }
    _seal_record(event)
    bundle["supplemental"] = {
        "anchors_sha256": "f" * 64,
        "evidence": [evidence],
        "events": [event],
        "unmatched": [],
    }
    return bundle


def test_load_render_aggregates_emails_and_layout(tmp_path: Path) -> None:
    path = tmp_path / "bundle.json"
    _write_bundle(path, _bundle())

    report = reimbursement_report.load_bundle(path)
    summary = report.summary
    html = reimbursement_report.render_html(report)

    assert summary.review_rows == 4
    assert summary.active == 3
    assert summary.settled == 1
    assert summary.live_unreviewed == 3
    assert summary.new_records == 3
    assert summary.legacy_records == 1
    assert summary.item_lines == 5
    assert summary.known_total == Decimal("45.00")
    assert summary.approved == Decimal("35.00")
    assert summary.clarification == Decimal("7.00")
    assert summary.question == Decimal("3.00")
    assert summary.outstanding == Decimal("15.00")
    assert summary.emails_to_send == 3

    assert "$15.00 still to pay" in html
    assert "What to do, ticket by ticket" in html
    assert 'class="box a"' in html
    assert 'class="box c"' in html
    assert 'class="box q"' in html
    assert ".current-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))" in html
    current_grid = html.split('<div class="current-grid">', 1)[1].split(
        '<p class="review-disclaimer">', 1
    )[0]
    assert re.findall(r'<div class="k">([^<]+)</div>', current_grid) == [
        "Review rows",
        "Legacy + new",
        "Mapped ledger",
        "Mapped ledger total",
        "Settled",
        "Active",
        "Live unreviewed",
        "Received range",
    ]
    assert html.count("Email to send") == 3
    assert "Zelle confirmation: [ZELLE CONFIRMATION]" in html
    assert "Check number: [CHECK NUMBER]" in html
    assert "We are reviewing your $3.00 reimbursement request" in html
    assert "Appendix &mdash; closed cases" in html
    assert "Appendix &mdash; General rule interpretations and amendments" in html
    assert "&lt;script data-test=&#34;unsafe&#34;&gt;alert(1)&lt;/script&gt;" in html
    assert '<script data-test="unsafe">' not in html
    for private_key in (
        "submission:v1:" + "a" * 64,
        "source_evidence_sha256",
        "review_key",
    ):
        assert private_key not in html
    assert reimbursement_report.render_html(report) == html


@pytest.mark.parametrize(
    ("signoff", "context"),
    [
        (("Regards,",), ""),
        (("Thank you!", "Example Treasurer Team"), "Synthetic exact payment context."),
        (
            ("With appreciation,", "Example Treasurer", "Example Association"),
            "Synthetic context line one.\nSynthetic context line two.",
        ),
    ],
)
def test_generated_zelle_email_round_trips_through_strict_payment_parser(
    tmp_path: Path, signoff: tuple[str, ...], context: str
) -> None:
    path = tmp_path / "bundle.json"
    bundle = _bundle()
    bundle["report"]["email_signoff"] = list(signoff)
    bundle["tickets"][0]["review"]["email_context"] = context
    _write_bundle(path, bundle)

    report = reimbursement_report.load_bundle(path)
    rendered = reimbursement_report.render_html(report)
    body_match = re.search(r'<pre class="mail-body">(.*?)</pre>', rendered, re.DOTALL)
    assert body_match is not None
    sent_body = html.unescape(body_match.group(1)).replace(
        "[ZELLE CONFIRMATION]", "EXAMPLE-ZELLE-1000"
    )

    assert reimbursement_events.parse_payment_evidence_blocks(
        sent_body,
        expected_signoff=report.settings.email_signoff,
        expected_context=report.tickets[0].review.email_context,
    ) == (
        reimbursement_events.PaymentEvidence(
            amount=Decimal("10.00"), reference="EXAMPLE-ZELLE-1000"
        ),
    )
    assert reimbursement_events.parse_payment_evidence(sent_body) is None
    assert (
        reimbursement_events.parse_payment_evidence_blocks(
            sent_body + "\nThis is only a notice",
            expected_signoff=report.settings.email_signoff,
            expected_context=report.tickets[0].review.email_context,
        )
        is None
    )


@pytest.mark.parametrize("payment_method", ["Not Zelle", "Non-Zelle"])
def test_negative_zelle_payment_methods_do_not_get_zelle_confirmation(
    payment_method: str,
) -> None:
    assert reimbursement_report._payment_confirmation(payment_method) == (
        "Check number or Zelle confirmation: [CHECK NUMBER OR ZELLE CONFIRMATION]"
    )


def test_legacy_review_preserves_informational_and_unknown_amount_lines(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    clarification = bundle["tickets"][1]  # type: ignore[index]
    first_item, second_item = clarification["items"]  # type: ignore[index,union-attr]
    first_item.update(  # type: ignore[union-attr]
        {
            "source_amount": "2.00",
            "status": "-",
            "why": "Informational evidence line with a known historical amount.",
        }
    )
    second_item["source_amount"] = ""  # type: ignore[index]
    clarification["source"]["mapped_total"] = "2.00"  # type: ignore[index]

    path = tmp_path / "legacy-edge.json"
    _write_bundle(path, bundle)
    report = reimbursement_report.load_bundle(path)
    ticket = next(ticket for ticket in report.tickets if ticket.ref == "NEW-02")

    assert ticket.items[0].effective_amount == Decimal("2.00")
    assert ticket.items[1].effective_amount is None
    assert ticket.review.status == "C"
    assert ticket.total == Decimal("2.00")
    assert report.summary.item_lines == 5


def test_build_report_writes_atomically_and_returns_result(tmp_path: Path) -> None:
    data_path = tmp_path / "bundle.json"
    output_path = tmp_path / "private" / "queue.html"
    _write_bundle(data_path, _bundle())

    result = reimbursement_report.build_report(data_path, output_path)
    output = output_path.read_bytes()

    assert result.output_path == output_path.resolve()
    assert result.bytes_written == len(output)
    assert result.sha256 == hashlib.sha256(output).hexdigest()
    assert result.summary.active == 3
    assert result.receipt_linked_items == 0
    assert output.startswith(b"<!doctype html>")
    assert not list(output_path.parent.glob(f".{output_path.name}.*.tmp"))


def test_paid_approved_and_declined_lines_can_close_claim(tmp_path: Path) -> None:
    bundle = _bundle()
    ticket = bundle["tickets"][1]  # type: ignore[index]
    ticket["items"][1]["status"] = "D"  # type: ignore[index]
    ticket["review"]["status"] = "D"  # type: ignore[index]
    ticket["live"].update(  # type: ignore[union-attr]
        {
            "workflow_state": "SETTLED",
            "decision": "DECLINED",
            "payment_status": "PAID_PRIOR",
            "payment_date": "2026-08-03",
            "confirmations": ["Fictional payment for the approved $5.00 line"],
        }
    )
    ticket["messages"] = []  # type: ignore[index]
    path = tmp_path / "mixed.json"
    _write_bundle(path, bundle)

    report = reimbursement_report.load_bundle(path)
    mixed = next(t for t in report.closed_tickets if t.ref == "NEW-02")
    assert mixed.approved == Decimal("5.00")
    assert mixed.amount_for("D") == Decimal("7.00")
    assert mixed.pay_now == Decimal("0.00")
    assert report.summary.active == 2
    assert report.summary.settled == 2
    rendered = reimbursement_report.render_html(report)
    assert "Resolved claims; nothing remains to pay" in rendered
    assert "Paid + declined" in rendered

    ticket["items"][0]["status"] = "D"  # type: ignore[index]
    _write_bundle(path, bundle)
    with pytest.raises(reimbursement_report.ReimbursementReportError, match="paid with declined"):
        reimbursement_report.load_bundle(path)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data.update({"unknown": True}), "bundle has invalid keys"),
        (
            lambda data: data["report"].pop("title"),  # type: ignore[union-attr]
            "report has invalid keys",
        ),
        (
            lambda data: data["tickets"][0]["review"].update({"status": "D"}),  # type: ignore[index,union-attr]
            "does not match its item status roll-up",
        ),
        (
            lambda data: data["tickets"][1]["source"].update({"mapped_total": "99.00"}),  # type: ignore[index,union-attr]
            "mapped_total does not equal its source item sum",
        ),
        (
            lambda data: data["tickets"][1].update({"ref": "NEW-01"}),  # type: ignore[index,union-attr]
            "duplicate ref",
        ),
        (
            lambda data: data["provenance"].update(  # type: ignore[union-attr]
                {
                    "accounted_review_keys": list(
                        reversed(data["provenance"]["accounted_review_keys"])  # type: ignore[index]
                    )
                }
            ),
            "accounted_review_keys must contain unique sorted strings",
        ),
    ],
)
def test_strict_bundle_rejects_drift(tmp_path: Path, mutate: object, message: str) -> None:
    bundle = copy.deepcopy(_bundle())
    assert callable(mutate)
    mutate(bundle)  # type: ignore[operator]
    path = tmp_path / "invalid.json"
    _write_bundle(path, bundle)

    with pytest.raises(reimbursement_report.ReimbursementReportError, match=message):
        reimbursement_report.load_bundle(path)


def test_active_ticket_may_pause_draft_while_response_is_reviewed(tmp_path: Path) -> None:
    bundle = _bundle()
    ticket = bundle["tickets"][1]  # type: ignore[index]
    ticket["messages"] = []  # type: ignore[index]
    path = tmp_path / "missing-draft.json"
    _write_bundle(path, bundle)
    reimbursement_report.load_bundle(path)

    ticket["messages"] = [  # type: ignore[index]
        {"kind": "draft", "date": "", "mode": "generated", "body": ""},
        {"kind": "draft", "date": "", "mode": "generated", "body": ""},
    ]
    _write_bundle(path, bundle)
    with pytest.raises(reimbursement_report.ReimbursementReportError, match="at most one draft"):
        reimbursement_report.load_bundle(path)

    bundle = _bundle()
    ticket = bundle["tickets"][1]  # type: ignore[index]
    ticket["review"]["asks"] = []  # type: ignore[index]
    ticket["review"]["email_questions"] = []  # type: ignore[index]
    _write_bundle(path, bundle)
    with pytest.raises(
        reimbursement_report.ReimbursementReportError,
        match="generated clarification email requires at least one question",
    ):
        reimbursement_report.load_bundle(path)


def test_legacy_forms_share_a_ref_but_keep_distinct_identity(tmp_path: Path) -> None:
    bundle = _bundle()
    form_a = bundle["tickets"][3]  # type: ignore[index]
    form_a["ref"] = "P-004"  # type: ignore[index]
    form_a["form_label"] = "Form A"  # type: ignore[index]
    form_a["submitted_label"] = "2026-07-01 form / reported 2026-07-02"  # type: ignore[index]
    form_b = copy.deepcopy(form_a)
    form_b["review_key"] = "legacy:v1:p-004:form-b"  # type: ignore[index]
    form_b["form_label"] = "Form B"  # type: ignore[index]
    form_b["display_order"] = 5  # type: ignore[index]
    form_b["items"][0]["item_key"] = "legacy-p004-form-b:line:1"  # type: ignore[index]
    bundle["tickets"].append(form_b)  # type: ignore[union-attr]
    path = tmp_path / "forms.json"
    _write_bundle(path, bundle)

    report = reimbursement_report.load_bundle(path)
    html = reimbursement_report.render_html(report)

    assert sum(ticket.ref == "P-004" for ticket in report.tickets) == 2
    assert "Form A" in html
    assert "Form B" in html
    assert "2026-07-01 form / reported 2026-07-02" in html
    assert 'id="p-004-form-a"' in html
    assert 'id="p-004-form-b"' in html


def test_schema_v1_migration_preserves_reviews_and_source_hashes() -> None:
    original = _bundle()
    original_tickets = copy.deepcopy(original["tickets"])
    original_provenance = copy.deepcopy(original["provenance"])

    migrated = reimbursement_report.migrate_bundle(original)

    assert migrated["schema_version"] == 2
    assert migrated["tickets"] == original_tickets
    assert migrated["provenance"] == original_provenance
    assert migrated["supplemental"]["evidence"] == []
    assert original["schema_version"] == 1
    assert "supplemental" not in original


def test_supplemental_events_and_unmatched_render_without_changing_totals(
    tmp_path: Path,
) -> None:
    bundle = reimbursement_report.migrate_bundle(_bundle())
    before_path = tmp_path / "before.json"
    _write_bundle(before_path, _bundle())
    before = reimbursement_report.load_bundle(before_path).summary
    linked_digest = "4" * 64
    unmatched_digest = "5" * 64
    linked_message_id = "<linked@example.invalid>"
    unmatched_message_id = "<unmatched@example.invalid>"
    linked_key = "mail:v1:" + hashlib.sha256(linked_message_id.encode()).hexdigest()
    unmatched_key = "mail:v1:" + hashlib.sha256(unmatched_message_id.encode()).hexdigest()
    ticket_review_key = "submission:v1:" + "a" * 64
    event_key = (
        "event:v1:"
        + hashlib.sha256(
            f"{linked_key}\0{ticket_review_key}\0RECEIPT_RECEIVED".encode()
        ).hexdigest()
    )
    bundle["supplemental"] = {
        "anchors_sha256": "6" * 64,
        "evidence": [
            {
                "evidence_key": linked_key,
                "source_type": "MAIL",
                "message_id": linked_message_id,
                "in_reply_to": ["<outbound@example.invalid>"],
                "references": ["<outbound@example.invalid>"],
                "occurred_on": "2026-08-04",
                "occurred_at": "2026-08-04T12:00:00+00:00",
                "top_authored_sha256": "7" * 64,
                "evidence_sha256": linked_digest,
                "attachments": [
                    {
                        "mime_type": "image/jpeg",
                        "filename": "fictional-receipt.jpg",
                        "decoded_size": 123,
                        "content_sha256": "8" * 64,
                    }
                ],
            },
            {
                "evidence_key": unmatched_key,
                "source_type": "MAIL",
                "message_id": unmatched_message_id,
                "in_reply_to": [],
                "references": [],
                "occurred_on": "2026-08-04",
                "occurred_at": "2026-08-04T12:00:00+00:00",
                "top_authored_sha256": "9" * 64,
                "evidence_sha256": unmatched_digest,
                "attachments": [],
            },
        ],
        "events": [
            {
                "event_key": event_key,
                "evidence_key": linked_key,
                "ticket_review_key": ticket_review_key,
                "kind": "RECEIPT_RECEIVED",
                "occurred_on": "2026-08-04",
                "occurred_at": "2026-08-04T12:00:00+00:00",
                "evidence_sha256": linked_digest,
                "summary": "One fictional supplemental receipt was received.",
                "amount": "",
                "reference": "",
                "discrepancy": "",
            }
        ],
        "unmatched": [{"evidence_key": unmatched_key, "reason": "NO_EXACT_LINK"}],
    }
    for evidence in bundle["supplemental"]["evidence"]:
        evidence["record_sha256"] = hashlib.sha256(
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    for event in bundle["supplemental"]["events"]:
        event["record_sha256"] = hashlib.sha256(
            json.dumps(
                event,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    bundle["supplemental"]["evidence"].sort(key=lambda item: item["evidence_key"])
    path = tmp_path / "supplemental.json"
    _write_bundle(path, bundle)

    report = reimbursement_report.load_bundle(path)
    html = reimbursement_report.render_html(report)

    assert report.summary.known_total == before.known_total
    assert report.summary.approved == before.approved
    assert len(report.supplemental.events) == 1
    assert len(report.supplemental.unmatched) == 1
    assert "Supplemental event history" in html
    assert "fictional-receipt.jpg" in html
    assert "Unmatched or quarantined supplemental evidence" in html
    assert "Evidence-limited recommendation" in html
    assert "schema-v2" in html

    bad = copy.deepcopy(bundle)
    bad["supplemental"]["events"][0]["evidence_sha256"] = "0" * 64  # type: ignore[index]
    _write_bundle(path, bad)
    with pytest.raises(reimbursement_report.ReimbursementReportError, match="stored event|digest"):
        reimbursement_report.load_bundle(path)

    tampered_event = copy.deepcopy(bundle)
    tampered_event["supplemental"]["events"][0]["occurred_on"] = "2026-08-05"  # type: ignore[index]
    _write_bundle(path, tampered_event)
    with pytest.raises(reimbursement_report.ReimbursementReportError, match="stored event"):
        reimbursement_report.load_bundle(path)

    tampered_attachment = copy.deepcopy(bundle)
    tampered_record = next(
        item
        for item in tampered_attachment["supplemental"]["evidence"]  # type: ignore[index]
        if item["attachments"]
    )
    tampered_record["attachments"][0]["content_sha256"] = "f" * 64
    _write_bundle(path, tampered_attachment)
    with pytest.raises(reimbursement_report.ReimbursementReportError, match="stored evidence"):
        reimbursement_report.load_bundle(path)


def test_payment_event_amount_and_discrepancy_kind_are_strict(tmp_path: Path) -> None:
    bundle = _recorded_payment_bundle()
    ticket = bundle["tickets"][0]
    evidence = bundle["supplemental"]["evidence"][0]
    event = bundle["supplemental"]["events"][0]
    ticket_key = str(ticket["review_key"])
    evidence_key = str(evidence["evidence_key"])
    path = tmp_path / "payment.json"
    _write_bundle(path, bundle)
    reimbursement_report.load_bundle(path)

    wrong_amount = copy.deepcopy(bundle)
    wrong_event = wrong_amount["supplemental"]["events"][0]
    wrong_event["amount"] = "1.00"
    _seal_record(wrong_event)
    _write_bundle(path, wrong_amount)
    with pytest.raises(reimbursement_report.ReimbursementReportError, match="total to match"):
        reimbursement_report.load_bundle(path)

    wrong_parity = copy.deepcopy(bundle)
    parity_event = wrong_parity["supplemental"]["events"][0]
    parity_event["discrepancy"] = "Synthetic mismatch text."
    _seal_record(parity_event)
    _write_bundle(path, wrong_parity)
    with pytest.raises(reimbursement_report.ReimbursementReportError, match="cannot carry"):
        reimbursement_report.load_bundle(path)

    equal_discrepancy = copy.deepcopy(bundle)
    discrepancy_event = equal_discrepancy["supplemental"]["events"][0]
    discrepancy_event["kind"] = "PAYMENT_DISCREPANCY"
    discrepancy_event["event_key"] = (
        "event:v1:"
        + hashlib.sha256(f"{evidence_key}\0{ticket_key}\0PAYMENT_DISCREPANCY".encode()).hexdigest()
    )
    discrepancy_event["discrepancy"] = "Synthetic mismatch text."
    _seal_record(discrepancy_event)
    _write_bundle(path, equal_discrepancy)
    with pytest.raises(reimbursement_report.ReimbursementReportError, match="must differ"):
        reimbursement_report.load_bundle(path)

    duplicate_recorded = copy.deepcopy(bundle)
    second_message_id = "<payment-report-second@example.invalid>"
    second_evidence_key = "mail:v1:" + hashlib.sha256(second_message_id.encode()).hexdigest()
    second_evidence_digest = "c" * 64
    second_evidence = copy.deepcopy(evidence)
    second_evidence.update(
        {
            "evidence_key": second_evidence_key,
            "message_id": second_message_id,
            "occurred_at": "2026-08-04T13:00:00+00:00",
            "evidence_sha256": second_evidence_digest,
        }
    )
    _seal_record(second_evidence)
    second_event = copy.deepcopy(event)
    second_event.update(
        {
            "event_key": "event:v1:"
            + hashlib.sha256(
                f"{second_evidence_key}\0{ticket_key}\0PAYMENT_RECORDED".encode()
            ).hexdigest(),
            "evidence_key": second_evidence_key,
            "occurred_at": "2026-08-04T13:00:00+00:00",
            "evidence_sha256": second_evidence_digest,
            "reference": "EXAMPLE-200",
        }
    )
    _seal_record(second_event)
    duplicate_recorded["supplemental"]["evidence"].append(second_evidence)
    duplicate_recorded["supplemental"]["evidence"].sort(key=lambda item: item["evidence_key"])
    duplicate_recorded["supplemental"]["events"].append(second_event)
    duplicate_recorded["supplemental"]["events"].sort(
        key=lambda item: (item["occurred_at"], item["event_key"])
    )
    _write_bundle(path, duplicate_recorded)
    with pytest.raises(reimbursement_report.ReimbursementReportError, match="at most one"):
        reimbursement_report.load_bundle(path)


@pytest.mark.parametrize("source_type", ["MAIL", "OPERATOR_PAYMENT"])
def test_recorded_payment_accepts_exact_mail_and_operator_state(
    tmp_path: Path, source_type: str
) -> None:
    path = tmp_path / f"recorded-{source_type.casefold()}.json"
    _write_bundle(path, _recorded_payment_bundle(source_type=source_type))

    reimbursement_report.load_bundle(path)


def test_recorded_payment_preserves_unrelated_prior_confirmation(tmp_path: Path) -> None:
    bundle = _recorded_payment_bundle()
    bundle["tickets"][0]["live"]["confirmations"].insert(
        0, "Synthetic unrelated prior confirmation."
    )
    path = tmp_path / "recorded-with-prior-confirmation.json"
    _write_bundle(path, bundle)

    reimbursement_report.load_bundle(path)


def test_recorded_payment_rejects_paid_prior_target(tmp_path: Path) -> None:
    bundle = _recorded_payment_bundle()
    bundle["tickets"][0]["live"]["payment_status"] = "PAID_PRIOR"
    path = tmp_path / "paid-prior.json"
    _write_bundle(path, bundle)

    with pytest.raises(reimbursement_report.ReimbursementReportError, match="payment_status PAID"):
        reimbursement_report.load_bundle(path)


def test_recorded_payment_rejects_payment_date_mismatch(tmp_path: Path) -> None:
    bundle = _recorded_payment_bundle()
    bundle["tickets"][0]["live"]["payment_date"] = "2026-08-05"
    path = tmp_path / "wrong-date.json"
    _write_bundle(path, bundle)

    with pytest.raises(reimbursement_report.ReimbursementReportError, match="payment date"):
        reimbursement_report.load_bundle(path)


def test_recorded_payment_rejects_unrelated_reference_confirmation(tmp_path: Path) -> None:
    bundle = _recorded_payment_bundle()
    bundle["tickets"][0]["live"]["confirmations"] = ["Reference UNRELATED-100; amount $9.00"]
    path = tmp_path / "wrong-reference.json"
    _write_bundle(path, bundle)

    with pytest.raises(
        reimbursement_report.ReimbursementReportError, match="canonical confirmation"
    ):
        reimbursement_report.load_bundle(path)


def test_recorded_payment_rejects_unrelated_amount_confirmation(tmp_path: Path) -> None:
    bundle = _recorded_payment_bundle()
    bundle["tickets"][0]["live"]["confirmations"] = ["Reference EXAMPLE-100; amount $1.00"]
    path = tmp_path / "wrong-confirmation-amount.json"
    _write_bundle(path, bundle)

    with pytest.raises(
        reimbursement_report.ReimbursementReportError, match="canonical confirmation"
    ):
        reimbursement_report.load_bundle(path)


def test_operator_review_evidence_rejects_mail_timestamp_metadata() -> None:
    digest = "a" * 64
    evidence: dict[str, object] = {
        "evidence_key": f"operator-review:v1:{digest}",
        "source_type": "OPERATOR_REVIEW",
        "message_id": "",
        "in_reply_to": [],
        "references": [],
        "occurred_on": "",
        "occurred_at": "2026-08-04T12:00:00+00:00",
        "top_authored_sha256": digest,
        "evidence_sha256": digest,
        "attachments": [],
    }
    _seal_record(evidence)

    with pytest.raises(reimbursement_report.ReimbursementReportError, match="impersonate mail"):
        reimbursement_report._parse_supplemental(
            {
                "anchors_sha256": "b" * 64,
                "evidence": [evidence],
                "events": [],
                "unmatched": [],
            }
        )


def test_incomplete_html_is_never_published(tmp_path: Path) -> None:
    output = tmp_path / "queue.html"
    output.write_text("old", encoding="utf-8")

    with pytest.raises(
        reimbursement_report.ReimbursementReportError,
        match="incomplete reimbursement report",
    ):
        reimbursement_report.write_html_atomic(output, "<html></html>")

    assert output.read_text(encoding="utf-8") == "old"
