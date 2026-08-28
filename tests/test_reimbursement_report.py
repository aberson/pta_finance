from __future__ import annotations

import copy
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from pta_finance import reimbursement_report


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
    assert output.startswith(b"<!doctype html>")
    assert not list(output_path.parent.glob(f".{output_path.name}.*.tmp"))


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


def test_active_ticket_requires_one_draft_and_clarification_question(tmp_path: Path) -> None:
    bundle = _bundle()
    ticket = bundle["tickets"][1]  # type: ignore[index]
    ticket["messages"] = []  # type: ignore[index]
    path = tmp_path / "missing-draft.json"
    _write_bundle(path, bundle)
    with pytest.raises(
        reimbursement_report.ReimbursementReportError,
        match="ACTIVE tickets require exactly one draft",
    ):
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


def test_incomplete_html_is_never_published(tmp_path: Path) -> None:
    output = tmp_path / "queue.html"
    output.write_text("old", encoding="utf-8")

    with pytest.raises(
        reimbursement_report.ReimbursementReportError,
        match="incomplete reimbursement report",
    ):
        reimbursement_report.write_html_atomic(output, "<html></html>")

    assert output.read_text(encoding="utf-8") == "old"
