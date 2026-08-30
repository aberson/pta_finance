"""Synthetic regression tests for the private reimbursement evidence pipeline.

Every identity and reimbursement in this module is fictional.  The tests use real RFC-822
``.eml`` bytes, but never credentials, production mail, or private report artifacts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import fields
from datetime import date
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from typing import TypedDict

import pytest

from pta_finance import reimbursement_events, reimbursement_pipeline, reimbursement_report

_CUTOFF = date(2030, 9, 1)
_AS_OF = date(2030, 9, 10)


class _RefreshKwargs(TypedDict):
    bundle_path: Path
    source: Path
    category_map_path: Path
    start_month: int
    received_since: date | None
    as_of: date


def _write_category_map(root: Path) -> Path:
    path = root / "category-map.csv"
    path.write_text(
        "raw_category,canonical_category\nSupplies,Program Supplies\n",
        encoding="utf-8",
    )
    return path


def _write_eml(
    path: Path,
    *,
    message_id: str,
    received: str,
    requestor_name: str = "Morgan Example",
    requestor_email: str = "morgan@example.invalid",
    item_date: str = "2030-09-02",
    description: str = "Fictional notebooks",
    amount: str = "12.50",
    stated_total: str | None = None,
    receipt_label: str | None = None,
) -> None:
    """Write one obviously fictional reimbursement submission as RFC-822 bytes."""
    message = EmailMessage()
    message["Subject"] = "Example Reimbursement Form got a new submission"
    message["From"] = "forms@example.invalid"
    message["To"] = "treasurer@example.invalid"
    message["Date"] = received
    message["Message-ID"] = message_id
    message.set_content(
        f"""\
Example Reimbursement Form got a new submission
Requestor First and Last Name:
{requestor_name}
Email:
{requestor_email}
1. Date:
{item_date}
1. Event or Budget Category:
Supplies
1. Description:
{description}
1. Amount:
{amount}
Total Amount $:
{stated_total if stated_total is not None else amount}
Choose Payment Type:
Check
{f"{receipt_label}:\nhttps://receipts.example.invalid/fake-receipt" if receipt_label else ""}
"""
    )
    path.write_bytes(message.as_bytes())


def _write_two_item_eml(path: Path, *, message_id: str, received: str) -> None:
    message = EmailMessage()
    message["Subject"] = "Example Reimbursement Form got a new submission"
    message["From"] = "forms@example.invalid"
    message["To"] = "treasurer@example.invalid"
    message["Date"] = received
    message["Message-ID"] = message_id
    message.set_content(
        """\
Example Reimbursement Form got a new submission
Requestor First and Last Name:
Morgan Example
Email:
morgan@example.invalid
1. Date:
2030-09-02
1. Event or Budget Category:
Supplies
1. Description:
Fictional notebooks
1. Amount:
1.00
2. Date:
2030-09-02
2. Event or Budget Category:
Supplies
2. Description:
Synthetic staff meal
2. Amount:
1.00
Total Amount $:
2.00
Choose Payment Type:
Check
PDF:
https://receipts.example.invalid/two-item-fake-receipt
"""
    )
    path.write_bytes(message.as_bytes())


def _write_mail(
    path: Path,
    *,
    message_id: str,
    received: str,
    sender: str,
    body: str,
    subject: str = "Synthetic reimbursement response",
    in_reply_to: str | None = None,
    references: tuple[str, ...] = (),
    attachments: tuple[tuple[str, str, bytes], ...] = (),
) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = "treasurer@example.invalid"
    message["Date"] = received
    message["Message-ID"] = message_id
    if in_reply_to is not None:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = " ".join(references)
    message.set_content(body)
    for filename, mime_type, payload in attachments:
        maintype, subtype = mime_type.split("/", maxsplit=1)
        message.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
    path.write_bytes(message.as_bytes())


def _selector(message_id: str, ref: str, *, form_label: str = "") -> dict[str, str]:
    return {
        "review_key": _expected_review_key(message_id),
        "ref": ref,
        "form_label": form_label,
    }


def _write_anchors(
    path: Path,
    *,
    payment_operators: tuple[str, ...] = (),
    secondary_approvers: tuple[str, ...] = (),
    thread_anchors: list[dict[str, object]] | None = None,
    direct_links: list[dict[str, object]] | None = None,
    operator_reviews: list[dict[str, object]] | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "actors": {
                    "payment_operators": list(payment_operators),
                    "secondary_approvers": list(secondary_approvers),
                },
                "thread_anchors": thread_anchors or [],
                "direct_links": direct_links or [],
                "operator_reviews": operator_reviews or [],
            }
        ),
        encoding="utf-8",
    )


def _build_snapshot(
    source: Path,
    category_map_path: Path,
    *,
    received_since: date | None = _CUTOFF,
) -> reimbursement_pipeline.EvidenceSnapshot:
    return reimbursement_pipeline.build_evidence_snapshot(
        source=source,
        category_map_path=category_map_path,
        start_month=7,
        received_since=received_since,
    )


def _base_bundle() -> dict[str, object]:
    """Return a minimal valid bundle with one unrelated, settled legacy ticket."""
    return {
        "schema_version": 1,
        "report": {
            "title": "Fictional Reimbursement Review",
            "eyebrow": "Example Association · Treasurer",
            "subtitle": "Synthetic pipeline regression fixture.",
            "organization": "Example Association",
            "email_signoff": ["Thank you!", "Example Treasurer Team"],
            "logo_data_uri": "",
            "confirmed_outstanding": "0.00",
            "cutoff_date": _CUTOFF.isoformat(),
            "policy_version": "fictional-policy-v1",
            "as_of_date": "2030-09-02",
        },
        "provenance": {
            "mapped_sha256": "1" * 64,
            "policy_sha256": "2" * 64,
            "source_snapshot_sha256": "3" * 64,
            "accounted_review_keys": [],
        },
        "source_summary": {
            "mapped_rows": 0,
            "mapped_submissions": 0,
            "mapped_total": "0.00",
            "first_received": _CUTOFF.isoformat(),
            "last_received": _CUTOFF.isoformat(),
        },
        "tickets": [
            {
                "review_key": "legacy:v1:fictional-p-001",
                "ref": "P-001",
                "form_label": "",
                "origin": "legacy",
                "display_order": 1,
                "requestor_name": "Quinn Example",
                "form_type": "Fictional legacy record",
                "submitted": _CUTOFF.isoformat(),
                "submitted_label": "",
                "payment_method": "Check",
                "source_evidence_sha256": "0" * 64,
                "source": {
                    "stated_total": "1.00",
                    "mapped_total": "1.00",
                    "categories": ["Program Supplies"],
                    "flags": [],
                },
                "live": {
                    "workflow_state": "SETTLED",
                    "decision": "APPROVED",
                    "payment_status": "PAID_PRIOR",
                    "payment_date": "2030-09-02",
                    "confirmations": ["FICTIONAL-CHECK-001"],
                },
                "review": {
                    "status": "A",
                    "action": "No further fictional action.",
                    "block": "This synthetic legacy ticket is closed.",
                    "asks": [],
                    "note": "Fictional archive fixture.",
                    "email_questions": [],
                    "email_context": "",
                },
                "items": [
                    {
                        "item_key": "legacy:v1:fictional-p-001:line:1",
                        "source_index": 1,
                        "source_date": _CUTOFF.isoformat(),
                        "source_description": "Fictional archived supplies",
                        "source_amount": "1.00",
                        "canonical_category": "Program Supplies",
                        "display_date": "",
                        "display_item": "",
                        "reviewed_amount": "",
                        "status": "A",
                        "why": "Synthetic evidence was previously reviewed.",
                    }
                ],
                "messages": [
                    {
                        "kind": "sent",
                        "date": "2030-09-02",
                        "mode": "verbatim",
                        "body": "Fictional payment notice.",
                    }
                ],
                "archive_note": "Synthetic closed-ticket fixture.",
            }
        ],
        "appendix": {"amendments": [], "cfo_checks": [], "excluded": [], "defects": []},
    }


def _write_bundle(path: Path) -> None:
    path.write_text(json.dumps(_base_bundle(), ensure_ascii=False), encoding="utf-8")


def _refresh_kwargs(
    *, bundle_path: Path, source: Path, category_map_path: Path, as_of: date = _AS_OF
) -> _RefreshKwargs:
    return {
        "bundle_path": bundle_path,
        "source": source,
        "category_map_path": category_map_path,
        "start_month": 7,
        "received_since": _CUTOFF,
        "as_of": as_of,
    }


def _expected_review_key(message_id: str) -> str:
    return "submission:v1:" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()


def _expected_line_key(review_key: str, source_index: int) -> str:
    payload = f"{review_key}\0{source_index}".encode()
    return "line:v1:" + hashlib.sha256(payload).hexdigest()


def test_cutoff_is_applied_before_content_dedup_for_real_eml(tmp_path: Path) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)

    # These two messages intentionally share the mapper's requestor/total/first-date content key.
    # The older file sorts first, so deduplicating before the cutoff would suppress the newer one.
    _write_eml(
        source / "a-older.eml",
        message_id="<older-twin@example.invalid>",
        received="Sat, 31 Aug 2030 08:00:00 +0000",
        item_date="2030-08-20",
        amount="30.00",
    )
    newer_id = "<newer-twin@example.invalid>"
    _write_eml(
        source / "b-newer.eml",
        message_id=newer_id,
        received="Sun, 01 Sep 2030 08:00:00 +0000",
        item_date="2030-08-20",
        amount="30.00",
    )

    snapshot = _build_snapshot(source, category_map_path)

    assert [ticket.review_key for ticket in snapshot.tickets] == [_expected_review_key(newer_id)]
    assert snapshot.scanned_messages == 2
    assert snapshot.excluded_by_cutoff == 1
    assert snapshot.mail_excluded_by_cutoff == 1
    assert len(snapshot.mail_evidence) == 1
    assert snapshot.recognized_originals == 1
    assert snapshot.mapped_rows == 1
    assert snapshot.mapped_total == Decimal("30.00")
    assert snapshot.first_received == snapshot.last_received == _CUTOFF.isoformat()


def test_submission_and_line_keys_are_stable_across_archive_order(tmp_path: Path) -> None:
    first_source = tmp_path / "first-order"
    second_source = tmp_path / "second-order"
    first_source.mkdir()
    second_source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    messages = [
        {
            "message_id": "<morgan-key@example.invalid>",
            "received": "Thu, 05 Sep 2030 08:00:00 +0000",
            "requestor_name": "Morgan Example",
            "requestor_email": "morgan@example.invalid",
            "item_date": "2030-09-04",
            "amount": "12.50",
        },
        {
            "message_id": "<riley-key@example.invalid>",
            "received": "Thu, 05 Sep 2030 08:00:00 +0000",
            "requestor_name": "Riley Example",
            "requestor_email": "riley@example.invalid",
            "item_date": "2030-09-05",
            "amount": "8.25",
        },
    ]
    _write_eml(first_source / "z-last.eml", **messages[0])
    _write_eml(first_source / "a-first.eml", **messages[1])
    _write_eml(second_source / "a-first.eml", **messages[0])
    _write_eml(second_source / "z-last.eml", **messages[1])

    first = _build_snapshot(first_source, category_map_path)
    second = _build_snapshot(second_source, category_map_path)

    assert first == second
    morgan = next(ticket for ticket in first.tickets if ticket.requestor_name == "Morgan Example")
    expected_review = _expected_review_key("<morgan-key@example.invalid>")
    assert morgan.review_key == expected_review
    assert [item.item_key for item in morgan.items] == [_expected_line_key(expected_review, 1)]
    assert "Morgan" not in morgan.review_key
    assert "morgan@example.invalid" not in morgan.review_key
    assert first.mapped_sha256 == second.mapped_sha256
    assert first.source_snapshot_sha256 == second.source_snapshot_sha256


def test_new_image_receipt_fields_do_not_change_legacy_source_hash(tmp_path: Path) -> None:
    without_image = tmp_path / "without-image"
    with_image = tmp_path / "with-image"
    without_image.mkdir()
    with_image.mkdir()
    category_map_path = _write_category_map(tmp_path)
    message_id = "<legacy-hash-compatibility@example.invalid>"
    common = {
        "message_id": message_id,
        "received": "Thu, 05 Sep 2030 08:00:00 +0000",
    }
    _write_eml(without_image / "submission.eml", **common)
    _write_eml(with_image / "submission.eml", **common, receipt_label="JPEG")

    old_shape = _build_snapshot(without_image, category_map_path)
    expanded_shape = _build_snapshot(with_image, category_map_path)

    assert (
        old_shape.tickets[0].source_evidence_sha256
        == expanded_shape.tickets[0].source_evidence_sha256
    )
    assert old_shape.tickets[0].receipt_asset_count == 0
    assert expanded_shape.tickets[0].receipt_asset_count == 1


def test_new_refs_are_persisted_and_unchanged_review_fields_survive(tmp_path: Path) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    _write_bundle(bundle_path)

    first_id = "<first-arrival@example.invalid>"
    _write_eml(
        source / "first-arrival.eml",
        message_id=first_id,
        received="Thu, 05 Sep 2030 08:00:00 +0000",
        item_date="2030-09-05",
        amount="12.50",
    )
    first_summary = reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        )
    )
    assert (first_summary.new_tickets, first_summary.unchanged_tickets) == (1, 0)

    first_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    first_ticket = next(
        ticket for ticket in first_bundle["tickets"] if ticket["origin"] == "submission"
    )
    assert first_ticket["ref"] == "NEW-01"
    first_ticket["review"]["action"] = "Keep this operator-authored fictional action."
    first_ticket["review"]["note"] = "Operator-authored fictional annotation."
    bundle_path.write_text(json.dumps(first_bundle, ensure_ascii=False), encoding="utf-8")
    reimbursement_report.load_bundle(bundle_path)
    preserved_ticket = copy.deepcopy(first_ticket)

    late_id = "<late-older-arrival@example.invalid>"
    _write_eml(
        source / "late-older-arrival.eml",
        message_id=late_id,
        received="Tue, 03 Sep 2030 08:00:00 +0000",
        requestor_name="Riley Example",
        requestor_email="riley@example.invalid",
        item_date="2030-09-03",
        amount="8.25",
    )
    second_summary = reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        )
    )

    assert (second_summary.new_tickets, second_summary.unchanged_tickets) == (1, 1)
    second_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    by_key = {ticket["review_key"]: ticket for ticket in second_bundle["tickets"]}
    assert by_key[_expected_review_key(first_id)] == preserved_ticket
    assert by_key[_expected_review_key(first_id)]["ref"] == "NEW-01"
    assert by_key[_expected_review_key(late_id)]["ref"] == "NEW-02"
    assert (
        by_key[_expected_review_key(late_id)]["display_order"] > preserved_ticket["display_order"]
    )

    persisted = bundle_path.read_bytes()
    third_summary = reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        )
    )
    assert (third_summary.new_tickets, third_summary.unchanged_tickets) == (0, 2)
    assert bundle_path.read_bytes() == persisted


@pytest.mark.parametrize("source_change", ["stale", "missing"])
def test_stale_or_missing_evidence_fails_closed_without_pii_or_mutation(
    tmp_path: Path, source_change: str
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    _write_bundle(bundle_path)
    message_path = source / "privacy-canary.eml"
    message_id = "<privacy-canary-message@example.invalid>"
    private_values = (
        "Privacy Canary Example",
        "privacy.canary@example.invalid",
        message_id,
        "Privacy canary fictional supplies",
    )
    _write_eml(
        message_path,
        message_id=message_id,
        received="Thu, 05 Sep 2030 08:00:00 +0000",
        requestor_name=private_values[0],
        requestor_email=private_values[1],
        description=private_values[3],
    )
    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        )
    )
    original = bundle_path.read_bytes()

    if source_change == "stale":
        _write_eml(
            message_path,
            message_id=message_id,
            received="Thu, 05 Sep 2030 08:00:00 +0000",
            requestor_name=private_values[0],
            requestor_email=private_values[1],
            description="Changed privacy canary fictional supplies",
        )
    else:
        message_path.unlink()

    with pytest.raises(reimbursement_pipeline.ReimbursementPipelineError) as caught:
        reimbursement_pipeline.refresh_bundle(
            **_refresh_kwargs(
                bundle_path=bundle_path,
                source=source,
                category_map_path=category_map_path,
                as_of=date(2030, 9, 11),
            )
        )

    assert bundle_path.read_bytes() == original
    assert list(bundle_path.parent.glob(f".{bundle_path.name}.*.tmp")) == []
    public_error = f"{caught.value!s} {caught.value!r}"
    assert "private bundle was not changed" in public_error
    assert all(private_value not in public_error for private_value in private_values)


def test_changed_accounted_but_unrendered_evidence_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    _write_bundle(bundle_path)
    visible_id = "<visible-accounted@example.invalid>"
    hidden_id = "<hidden-accounted@example.invalid>"
    _write_eml(
        source / "visible.eml",
        message_id=visible_id,
        received="Thu, 05 Sep 2030 08:00:00 +0000",
        description="Visible fictional supplies",
    )
    hidden_path = source / "hidden.eml"
    _write_eml(
        hidden_path,
        message_id=hidden_id,
        received="Fri, 06 Sep 2030 08:00:00 +0000",
        description="Hidden but accounted fictional supplies",
    )
    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        )
    )

    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    hidden_key = _expected_review_key(hidden_id)
    bundle["tickets"] = [
        ticket for ticket in bundle["tickets"] if ticket["review_key"] != hidden_key
    ]
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    reimbursement_report.load_bundle(bundle_path)
    original = bundle_path.read_bytes()

    _write_eml(
        hidden_path,
        message_id=hidden_id,
        received="Fri, 06 Sep 2030 08:00:00 +0000",
        description="Changed hidden fictional supplies",
    )
    with pytest.raises(
        reimbursement_pipeline.ReimbursementPipelineError,
        match="previously recorded source evidence has changed",
    ):
        reimbursement_pipeline.refresh_bundle(
            **_refresh_kwargs(
                bundle_path=bundle_path,
                source=source,
                category_map_path=category_map_path,
            )
        )

    assert bundle_path.read_bytes() == original


def test_plan_is_read_only_and_refresh_validates_temp_before_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    _write_bundle(bundle_path)
    _write_eml(
        source / "planned.eml",
        message_id="<planned-refresh@example.invalid>",
        received="Thu, 05 Sep 2030 08:00:00 +0000",
    )
    original = bundle_path.read_bytes()
    events: list[tuple[str, Path]] = []
    real_load_bundle = reimbursement_report.load_bundle

    def recording_load(path: Path) -> reimbursement_report.ReimbursementReport:
        events.append(("validate", path.resolve()))
        return real_load_bundle(path)

    monkeypatch.setattr(reimbursement_report, "load_bundle", recording_load)
    planned, planned_summary = reimbursement_pipeline.plan_bundle_refresh(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        )
    )

    assert bundle_path.read_bytes() == original
    assert events == [("validate", bundle_path.resolve())]
    assert planned_summary.new_tickets == 1
    assert any(ticket["origin"] == "submission" for ticket in planned["tickets"])

    events.clear()
    real_replace = os.replace

    def recording_replace(source_path: Path, destination_path: Path) -> None:
        source_path = source_path.resolve()
        destination_path = destination_path.resolve()
        events.append(("replace", source_path))
        assert destination_path == bundle_path.resolve()
        assert bundle_path.read_bytes() == original
        real_replace(source_path, destination_path)

    monkeypatch.setattr(os, "replace", recording_replace)
    applied_summary = reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        )
    )

    assert applied_summary == planned_summary
    assert [event[0] for event in events] == ["validate", "validate", "replace"]
    assert events[0][1] == bundle_path.resolve()
    assert events[1][1] == events[2][1]
    assert events[1][1] != bundle_path.resolve()
    assert bundle_path.read_bytes() != original
    assert list(bundle_path.parent.glob(f".{bundle_path.name}.*.tmp")) == []


def test_failed_temp_validation_preserves_bundle_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    _write_bundle(bundle_path)
    _write_eml(
        source / "validation-failure.eml",
        message_id="<validation-failure@example.invalid>",
        received="Thu, 05 Sep 2030 08:00:00 +0000",
    )
    original = bundle_path.read_bytes()
    real_load_bundle = reimbursement_report.load_bundle
    validated_paths: list[Path] = []

    def reject_replacement(path: Path) -> reimbursement_report.ReimbursementReport:
        resolved = path.resolve()
        validated_paths.append(resolved)
        if resolved == bundle_path.resolve():
            return real_load_bundle(path)
        raise reimbursement_report.ReimbursementReportError(
            "synthetic replacement validation failed"
        )

    monkeypatch.setattr(reimbursement_report, "load_bundle", reject_replacement)
    with pytest.raises(
        reimbursement_report.ReimbursementReportError,
        match="synthetic replacement validation failed",
    ):
        reimbursement_pipeline.refresh_bundle(
            **_refresh_kwargs(
                bundle_path=bundle_path,
                source=source,
                category_map_path=category_map_path,
            )
        )

    assert validated_paths[0] == bundle_path.resolve()
    assert validated_paths[1] != bundle_path.resolve()
    assert bundle_path.read_bytes() == original
    assert list(bundle_path.parent.glob(f".{bundle_path.name}.*.tmp")) == []


def test_refresh_summary_is_aggregate_only(tmp_path: Path) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    _write_bundle(bundle_path)
    private_values = (
        "Aggregate Canary Example",
        "aggregate.canary@example.invalid",
        "<aggregate-canary@example.invalid>",
        "Aggregate canary fictional supplies",
    )
    _write_eml(
        source / "aggregate.eml",
        message_id=private_values[2],
        received="Thu, 05 Sep 2030 08:00:00 +0000",
        requestor_name=private_values[0],
        requestor_email=private_values[1],
        description=private_values[3],
    )

    summary = reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        )
    )

    assert tuple(field.name for field in fields(summary)) == (
        "new_tickets",
        "unchanged_tickets",
        "total_source_tickets",
        "mapped_rows",
        "mapped_total",
        "first_received",
        "last_received",
        "supplemental_evidence",
        "supplemental_events",
        "unmatched_evidence",
        "supplemental_excluded_by_cutoff",
    )
    assert all(isinstance(value, (int, Decimal, str)) for value in vars(summary).values())
    public_receipt = f"{summary!s} {summary!r}"
    assert all(private_value not in public_receipt for private_value in private_values)


def test_bad_evidence_exception_string_does_not_echo_private_values(tmp_path: Path) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    private_values = (
        "Evidence Canary Example",
        "evidence.canary@example.invalid",
        "<evidence-canary@example.invalid>",
        "Evidence canary fictional supplies",
        "NOT-MONEY-EVIDENCE-CANARY",
    )
    _write_eml(
        source / "private-evidence-canary.eml",
        message_id=private_values[2],
        received="Thu, 05 Sep 2030 08:00:00 +0000",
        requestor_name=private_values[0],
        requestor_email=private_values[1],
        description=private_values[3],
        amount=private_values[4],
        stated_total="12.50",
    )

    with pytest.raises(reimbursement_pipeline.ReimbursementPipelineError) as caught:
        _build_snapshot(source, category_map_path)

    public_error = f"{caught.value!s} {caught.value!r}"
    assert public_error.startswith("mapped line amount is not finite money")
    assert all(private_value not in public_error for private_value in private_values)


def test_linked_jpeg_reply_updates_same_ticket_and_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    anchors_path = tmp_path / "anchors.json"
    _write_bundle(bundle_path)
    original_id = "<linked-original@example.invalid>"
    outbound_id = "<linked-outbound@example.invalid>"
    _write_eml(
        source / "original.eml",
        message_id=original_id,
        received="Thu, 05 Sep 2030 08:00:00 +0000",
    )
    _write_mail(
        source / "outbound.eml",
        message_id=outbound_id,
        received="Fri, 06 Sep 2030 08:00:00 +0000",
        sender="treasurer@example.invalid",
        body="Please reply with replacement receipt images.",
    )
    first_jpeg = b"synthetic-jpeg-one"
    second_jpeg = b"synthetic-jpeg-two"
    _write_mail(
        source / "reply.eml",
        message_id="<linked-reply@example.invalid>",
        received="Sat, 07 Sep 2030 08:00:00 +0000",
        sender="requestor@example.invalid",
        body="The two replacement receipts are attached.",
        subject="Re: synthetic receipt request",
        in_reply_to=outbound_id,
        references=(outbound_id,),
        attachments=(
            ("receipt-one.JPG", "image/jpeg", first_jpeg),
            ("receipt-two.jpeg", "image/jpeg", second_jpeg),
        ),
    )
    _write_mail(
        source / "blank-reply.eml",
        message_id="<linked-blank@example.invalid>",
        received="Sat, 07 Sep 2030 09:00:00 +0000",
        sender="requestor@example.invalid",
        body="",
        in_reply_to=outbound_id,
        references=(outbound_id,),
    )
    _write_mail(
        source / "logo-only-reply.eml",
        message_id="<linked-logo-only@example.invalid>",
        received="Sat, 07 Sep 2030 10:00:00 +0000",
        sender="requestor@example.invalid",
        body="",
        in_reply_to=outbound_id,
        references=(outbound_id,),
        attachments=(("signature-logo.png", "image/png", b"synthetic-logo"),),
    )
    _write_anchors(
        anchors_path,
        thread_anchors=[
            {
                "message_id": outbound_id,
                "purpose": "CASE",
                "tickets": [_selector(original_id, "NEW-01")],
            }
        ],
    )

    summary = reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        ),
        anchors_path=anchors_path,
    )
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    submission_tickets = [
        ticket for ticket in bundle["tickets"] if ticket["origin"] == "submission"
    ]
    assert (summary.new_tickets, summary.total_source_tickets) == (1, 1)
    assert len(submission_tickets) == 1
    assert submission_tickets[0]["review"]["status"] == "Q"
    assert submission_tickets[0]["messages"] == []
    assert {event["kind"] for event in bundle["supplemental"]["events"]} == {
        "RECEIPT_RECEIVED",
        "CLARIFICATION_RECEIVED",
    }
    assert len(bundle["supplemental"]["unmatched"]) == 2
    assert {item["reason"] for item in bundle["supplemental"]["unmatched"]} == {
        "NO_ACTIONABLE_CONTENT"
    }
    evidence = bundle["supplemental"]["evidence"][0]
    assert {item["content_sha256"] for item in evidence["attachments"]} == {
        hashlib.sha256(first_jpeg).hexdigest(),
        hashlib.sha256(second_jpeg).hexdigest(),
    }
    first_bytes = bundle_path.read_bytes()

    rerun = reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        ),
        anchors_path=anchors_path,
    )
    assert (rerun.new_tickets, rerun.unchanged_tickets) == (0, 1)
    assert bundle_path.read_bytes() == first_bytes


@pytest.mark.parametrize("change", ["changed", "missing"])
def test_accounted_supplemental_evidence_drift_fails_closed(tmp_path: Path, change: str) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    anchors_path = tmp_path / "anchors.json"
    _write_bundle(bundle_path)
    original_id = "<drift-original@example.invalid>"
    outbound_id = "<drift-outbound@example.invalid>"
    reply_path = source / "reply.eml"
    _write_eml(
        source / "original.eml",
        message_id=original_id,
        received="Thu, 05 Sep 2030 08:00:00 +0000",
    )
    _write_mail(
        source / "outbound.eml",
        message_id=outbound_id,
        received="Fri, 06 Sep 2030 08:00:00 +0000",
        sender="treasurer@example.invalid",
        body="Please send the synthetic receipt.",
    )
    _write_mail(
        reply_path,
        message_id="<drift-reply@example.invalid>",
        received="Sat, 07 Sep 2030 08:00:00 +0000",
        sender="requestor@example.invalid",
        body="Receipt attached.",
        in_reply_to=outbound_id,
        references=(outbound_id,),
        attachments=(("receipt.jpg", "image/jpeg", b"original-bytes"),),
    )
    _write_anchors(
        anchors_path,
        thread_anchors=[
            {
                "message_id": outbound_id,
                "purpose": "CASE",
                "tickets": [_selector(original_id, "NEW-01")],
            }
        ],
    )
    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        ),
        anchors_path=anchors_path,
    )
    before = bundle_path.read_bytes()
    if change == "missing":
        reply_path.unlink()
    else:
        _write_mail(
            reply_path,
            message_id="<drift-reply@example.invalid>",
            received="Sat, 07 Sep 2030 08:00:00 +0000",
            sender="requestor@example.invalid",
            body="Receipt attached.",
            in_reply_to=outbound_id,
            references=(outbound_id,),
            attachments=(("receipt.jpg", "image/jpeg", b"changed-bytes"),),
        )
    with pytest.raises(
        reimbursement_pipeline.ReimbursementPipelineError,
        match="previously accounted supplemental evidence",
    ):
        reimbursement_pipeline.refresh_bundle(
            **_refresh_kwargs(
                bundle_path=bundle_path,
                source=source,
                category_map_path=category_map_path,
            ),
            anchors_path=anchors_path,
        )
    assert bundle_path.read_bytes() == before


def test_operator_payment_settles_only_exact_link_and_keeps_discrepancy(tmp_path: Path) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    anchors_path = tmp_path / "anchors.json"
    _write_bundle(bundle_path)
    first_id = "<payment-first@example.invalid>"
    second_id = "<payment-second@example.invalid>"
    outbound_id = "<payment-outbound@example.invalid>"
    _write_eml(
        source / "first.eml",
        message_id=first_id,
        received="Thu, 05 Sep 2030 08:00:00 +0000",
        receipt_label="PDF",
    )
    _write_eml(
        source / "second.eml",
        message_id=second_id,
        received="Fri, 06 Sep 2030 08:00:00 +0000",
        requestor_name="Riley Example",
        requestor_email="riley@example.invalid",
        item_date="2030-09-06",
        amount="8.25",
        receipt_label="PDF",
    )
    _write_mail(
        source / "outbound.eml",
        message_id=outbound_id,
        received="Sat, 07 Sep 2030 08:00:00 +0000",
        sender="treasurer@example.invalid",
        body="Please clarify the fictional amount.",
    )
    _write_mail(
        source / "payment.eml",
        message_id="<payment-reply@example.invalid>",
        received="Sun, 08 Sep 2030 08:00:00 +0000",
        sender="payments@example.invalid",
        body="The clarification is resolved. Payment sent: $13.00. Reference PAY-123.",
        in_reply_to=outbound_id,
        references=(outbound_id,),
    )
    _write_mail(
        source / "payment-exact.eml",
        message_id="<payment-exact-reply@example.invalid>",
        received="Mon, 09 Sep 2030 08:00:00 +0000",
        sender="payments@example.invalid",
        body="Payment sent: $12.50. Reference PAY-124.",
        in_reply_to=outbound_id,
        references=(outbound_id,),
    )
    _write_anchors(
        anchors_path,
        payment_operators=("payments@example.invalid",),
        thread_anchors=[
            {
                "message_id": outbound_id,
                "purpose": "CASE",
                "tickets": [_selector(first_id, "NEW-01")],
            }
        ],
    )

    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        ),
        anchors_path=anchors_path,
    )
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    by_ref = {ticket["ref"]: ticket for ticket in bundle["tickets"]}
    assert by_ref["NEW-01"]["live"] == {
        "workflow_state": "SETTLED",
        "decision": "APPROVED",
        "payment_status": "PAID",
        "payment_date": "2030-09-09",
        "confirmations": ["Reference PAY-124; amount $12.50"],
    }
    assert by_ref["NEW-02"]["live"]["workflow_state"] == "ACTIVE"
    assert by_ref["NEW-02"]["live"]["payment_status"] == "NOT_PAID"
    payment_event = next(
        event
        for event in bundle["supplemental"]["events"]
        if event["kind"] == "PAYMENT_DISCREPANCY"
    )
    assert payment_event["amount"] == "13.00"
    assert payment_event["reference"] == "PAY-123"
    assert "$13.00" in payment_event["discrepancy"]
    assert "$12.50" in payment_event["discrepancy"]
    assert any(
        event["kind"] == "PAYMENT_RECORDED"
        and event["amount"] == "12.50"
        and event["reference"] == "PAY-124"
        for event in bundle["supplemental"]["events"]
    )
    persisted = bundle_path.read_bytes()
    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        ),
        anchors_path=anchors_path,
    )
    assert bundle_path.read_bytes() == persisted


def test_direct_mail_requires_explicit_anchor_and_unknown_candidate_is_visible(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    anchors_path = tmp_path / "anchors.json"
    _write_bundle(bundle_path)
    original_id = "<direct-original@example.invalid>"
    linked_id = "<direct-linked@example.invalid>"
    _write_eml(
        source / "original.eml",
        message_id=original_id,
        received="Thu, 05 Sep 2030 08:00:00 +0000",
    )
    for name, message_id, payload in (
        ("linked", linked_id, b"linked-receipt"),
        ("similar", "<direct-similar@example.invalid>", b"similar-receipt"),
    ):
        _write_mail(
            source / f"{name}.eml",
            message_id=message_id,
            received="Fri, 06 Sep 2030 08:00:00 +0000",
            sender="same-sender@example.invalid",
            subject="Same synthetic receipt subject",
            body="A direct reimbursement receipt is attached.",
            attachments=((f"{name}.png", "image/png", payload),),
        )
    _write_mail(
        source / "neutral-receipt.eml",
        message_id="<neutral-receipt@example.invalid>",
        received="Fri, 06 Sep 2030 09:00:00 +0000",
        sender="other-sender@example.invalid",
        body="Here you go.",
        attachments=(("replacement.jpg", "image/jpeg", b"neutral-receipt"),),
    )
    _write_mail(
        source / "clarification-only.eml",
        message_id="<clarification-only@example.invalid>",
        received="Fri, 06 Sep 2030 10:00:00 +0000",
        sender="other-sender@example.invalid",
        body="This is a synthetic reimbursement clarification response.",
    )
    _write_mail(
        source / "unlinked-check.eml",
        message_id="<unlinked-check@example.invalid>",
        received="Fri, 06 Sep 2030 11:00:00 +0000",
        sender="other-sender@example.invalid",
        body="Check number EXAMPLE-222 was sent for $10.00.",
    )
    _write_mail(
        source / "unrelated-yes.eml",
        message_id="<unrelated-yes@example.invalid>",
        received="Fri, 06 Sep 2030 12:00:00 +0000",
        sender="other-sender@example.invalid",
        body="Yes, please.",
    )
    _write_anchors(
        anchors_path,
        direct_links=[
            {
                "message_id": linked_id,
                "purpose": "RECEIPT",
                "ticket": _selector(original_id, "NEW-01"),
            }
        ],
    )

    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        ),
        anchors_path=anchors_path,
    )
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert len([ticket for ticket in bundle["tickets"] if ticket["origin"] == "submission"]) == 1
    assert {event["kind"] for event in bundle["supplemental"]["events"]} == {
        "RECEIPT_RECEIVED",
        "CLARIFICATION_RECEIVED",
    }
    assert len(bundle["supplemental"]["unmatched"]) == 4
    assert {item["reason"] for item in bundle["supplemental"]["unmatched"]} == {"NO_EXACT_LINK"}
    assert all(
        "unrelated-yes" not in item["message_id"] for item in bundle["supplemental"]["evidence"]
    )


def test_specific_recommendations_and_operator_override_do_not_self_authorize(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    anchors_path = tmp_path / "anchors.json"
    _write_bundle(bundle_path)
    clean_id = "<recommend-clean@example.invalid>"
    held_id = "<recommend-held@example.invalid>"
    _write_eml(
        source / "clean.eml",
        message_id=clean_id,
        received="Thu, 05 Sep 2030 08:00:00 +0000",
        receipt_label="JPEG",
    )
    _write_eml(
        source / "held.eml",
        message_id=held_id,
        received="Fri, 06 Sep 2030 08:00:00 +0000",
        requestor_name="Riley Example",
        requestor_email="riley@example.invalid",
        item_date="2030-09-06",
        description="Staff lunch giveaway",
        amount="25.00",
        receipt_label="PNG",
    )
    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        )
    )
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    by_ref = {ticket["ref"]: ticket for ticket in bundle["tickets"]}
    assert by_ref["NEW-01"]["review"]["status"] == "A"
    assert "Receipt contents were not read" in by_ref["NEW-01"]["items"][0]["why"]
    assert by_ref["NEW-01"]["live"]["decision"] == "UNREVIEWED"
    assert by_ref["NEW-02"]["review"]["status"] == "C"
    assert "staff/adult meal" in by_ref["NEW-02"]["items"][0]["why"]
    assert "giveaway" in by_ref["NEW-02"]["items"][0]["why"]
    assert by_ref["NEW-02"]["live"]["decision"] == "UNREVIEWED"

    _write_anchors(
        anchors_path,
        operator_reviews=[
            {
                "ticket": _selector(held_id, "NEW-02"),
                "record_decision": True,
                "items": [
                    {
                        "source_index": 1,
                        "status": "C",
                        "why": "The synthetic receipt amount needs confirmation.",
                        "reviewed_amount": "",
                    }
                ],
                "action": "Ask the synthetic amount question",
                "block": "Confirm the claimed amount.",
                "asks": ["Is the fictional claimed amount correct?"],
                "note": "Synthetic visual review override.",
                "email_questions": ["Is the fictional claimed amount correct?"],
                "email_context": "",
            }
        ],
    )
    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        ),
        anchors_path=anchors_path,
    )
    overridden = json.loads(bundle_path.read_text(encoding="utf-8"))
    held = next(ticket for ticket in overridden["tickets"] if ticket["ref"] == "NEW-02")
    assert held["items"][0]["why"] == "The synthetic receipt amount needs confirmation."
    assert held["review"]["asks"] == ["Is the fictional claimed amount correct?"]
    assert held["live"]["decision"] == "CLARIFICATION"
    assert held["live"]["payment_status"] == "NOT_PAID"


def test_same_day_supplemental_events_follow_rfc_time_not_message_hash(tmp_path: Path) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    anchors_path = tmp_path / "anchors.json"
    _write_bundle(bundle_path)
    original_id = "<timed-original@example.invalid>"
    early_id = "<z-early@example.invalid>"
    late_id = "<a-late@example.invalid>"
    assert (
        hashlib.sha256(early_id.encode()).hexdigest() > hashlib.sha256(late_id.encode()).hexdigest()
    )
    _write_eml(
        source / "original.eml",
        message_id=original_id,
        received="Thu, 05 Sep 2030 07:00:00 +0000",
    )
    _write_mail(
        source / "early.eml",
        message_id=early_id,
        received="Fri, 06 Sep 2030 08:00:00 +0000",
        sender="requestor@example.invalid",
        body="First synthetic clarification response.",
    )
    _write_mail(
        source / "late.eml",
        message_id=late_id,
        received="Fri, 06 Sep 2030 09:00:00 +0000",
        sender="requestor@example.invalid",
        body="Second synthetic clarification response.",
    )
    _write_anchors(
        anchors_path,
        direct_links=[
            {
                "message_id": message_id,
                "purpose": "CLARIFICATION",
                "ticket": _selector(original_id, "NEW-01"),
            }
            for message_id in (early_id, late_id)
        ],
    )

    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        ),
        anchors_path=anchors_path,
    )
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    clarification_events = [
        event
        for event in bundle["supplemental"]["events"]
        if event["kind"] == "CLARIFICATION_RECEIVED"
    ]
    assert [event["occurred_at"] for event in clarification_events] == [
        "2030-09-06T08:00:00+00:00",
        "2030-09-06T09:00:00+00:00",
    ]


def test_scoped_approver_reply_authorizes_only_unambiguous_proposal(tmp_path: Path) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    anchors_path = tmp_path / "anchors.json"
    _write_bundle(bundle_path)
    first_id = "<approval-first@example.invalid>"
    second_id = "<approval-second@example.invalid>"
    proposal_id = "<approval-proposal@example.invalid>"
    ambiguous_id = "<approval-ambiguous@example.invalid>"
    _write_eml(
        source / "first.eml",
        message_id=first_id,
        received="Thu, 05 Sep 2030 08:00:00 +0000",
        receipt_label="PDF",
    )
    _write_eml(
        source / "second.eml",
        message_id=second_id,
        received="Fri, 06 Sep 2030 08:00:00 +0000",
        requestor_name="Riley Example",
        requestor_email="riley@example.invalid",
        item_date="2030-09-06",
        amount="8.25",
        receipt_label="PDF",
    )
    _write_mail(
        source / "proposal.eml",
        message_id=proposal_id,
        received="Sat, 07 Sep 2030 08:00:00 +0000",
        sender="treasurer@example.invalid",
        body="NEW-01\nApprove as is\nNEW-02\nClarification: confirm the synthetic fact\n",
    )
    _write_mail(
        source / "approval.eml",
        message_id="<approval-reply@example.invalid>",
        received="Sun, 08 Sep 2030 08:00:00 +0000",
        sender="reviewer@example.invalid",
        body=(
            "Hello Example Reviewer,\n\n"
            "I agree with your assessment and I would like to add a synthetic note if follow-up "
            "is needed.\n\nThank you,\nExample Approver\n"
        ),
        in_reply_to=proposal_id,
        references=(proposal_id,),
    )
    payment_id = "<approval-payment@example.invalid>"
    _write_mail(
        source / "payment.eml",
        message_id=payment_id,
        received="Sun, 08 Sep 2030 12:00:00 +0000",
        sender="treasurer@example.invalid",
        body="Payment sent: $12.50. Confirmation EXAMPLE-PAY-1250.",
    )
    _write_mail(
        source / "spoofed.eml",
        message_id="<approval-spoofed@example.invalid>",
        received="Mon, 09 Sep 2030 08:00:00 +0000",
        sender="spoofed@example.invalid",
        body="Yes, please.",
        in_reply_to=proposal_id,
        references=(proposal_id,),
    )
    _write_mail(
        source / "quoted.eml",
        message_id="<approval-quoted@example.invalid>",
        received="Tue, 10 Sep 2030 08:00:00 +0000",
        sender="reviewer@example.invalid",
        body="On Sun, someone wrote:\n> Yes, please.\n",
        in_reply_to=proposal_id,
        references=(proposal_id,),
    )
    _write_mail(
        source / "ambiguous-proposal.eml",
        message_id=ambiguous_id,
        received="Wed, 11 Sep 2030 08:00:00 +0000",
        sender="treasurer@example.invalid",
        body="NEW-01\nApprove as is\nClarification: extra action\n",
    )
    _write_mail(
        source / "ambiguous-reply.eml",
        message_id="<approval-ambiguous-reply@example.invalid>",
        received="Thu, 12 Sep 2030 08:00:00 +0000",
        sender="reviewer@example.invalid",
        body="Yes, please.",
        in_reply_to=ambiguous_id,
        references=(ambiguous_id,),
    )
    _write_mail(
        source / "unrelated.eml",
        message_id="<approval-unrelated@example.invalid>",
        received="Fri, 13 Sep 2030 08:00:00 +0000",
        sender="reviewer@example.invalid",
        body="Yes, please.",
    )
    _write_anchors(
        anchors_path,
        payment_operators=("treasurer@example.invalid",),
        secondary_approvers=("reviewer@example.invalid",),
        thread_anchors=[
            {
                "message_id": proposal_id,
                "purpose": "APPROVAL_PROPOSAL",
                "tickets": [
                    _selector(first_id, "NEW-01"),
                    _selector(second_id, "NEW-02"),
                ],
            },
            {
                "message_id": ambiguous_id,
                "purpose": "APPROVAL_PROPOSAL",
                "tickets": [_selector(first_id, "NEW-01")],
            },
        ],
        direct_links=[
            {
                "message_id": payment_id,
                "purpose": "CASE",
                "ticket": _selector(first_id, "NEW-01"),
            }
        ],
    )

    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        ),
        anchors_path=anchors_path,
    )
    first_bytes = bundle_path.read_bytes()
    bundle = json.loads(first_bytes)
    by_ref = {ticket["ref"]: ticket for ticket in bundle["tickets"]}
    assert by_ref["NEW-01"]["live"]["decision"] == "APPROVED"
    assert by_ref["NEW-01"]["items"][0]["status"] == "A"
    assert by_ref["NEW-02"]["live"]["decision"] == "CLARIFICATION"
    assert by_ref["NEW-02"]["items"][0]["status"] == "C"
    assert by_ref["NEW-01"]["live"]["payment_status"] == "PAID"
    assert by_ref["NEW-01"]["live"]["workflow_state"] == "SETTLED"
    assert by_ref["NEW-02"]["live"]["payment_status"] == "NOT_PAID"
    event_kinds = [event["kind"] for event in bundle["supplemental"]["events"]]
    assert event_kinds.count("APPROVAL_GRANTED") == 2
    assert event_kinds.count("APPROVAL_QUARANTINED") == 5
    granted = [
        event for event in bundle["supplemental"]["events"] if event["kind"] == "APPROVAL_GRANTED"
    ]
    assert {event["summary"] for event in granted} == {
        "The configured approver authorized only the exact scoped proposal; additional reply prose "
        "was not interpreted."
    }
    assert [item["status"] for item in by_ref["NEW-02"]["items"]] == ["C"]
    assert all(
        "approval-unrelated" not in evidence["message_id"]
        for evidence in bundle["supplemental"]["evidence"]
    )

    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        ),
        anchors_path=anchors_path,
    )
    assert bundle_path.read_bytes() == first_bytes


def test_grouped_and_held_only_proposal_expansion_is_exact() -> None:
    expected_refs = [f"NEW-{index:02d}" for index in range(1, 10)]
    parsed = reimbursement_events.parse_proposal_recommendations(
        """\
NEW-03,NEW-07,NEW-08,NEW-09
* Approve as is
NEW-01
Clarification: confirm one synthetic line
NEW-02
Approve the first held synthetic line
Clarification: confirm the second held synthetic line
Approve the third held synthetic line
NEW-04
Clarification: confirm one synthetic line
NEW-05
Approve the one held synthetic line
NEW-06
Clarification: confirm one synthetic line
""",
        expected_refs=expected_refs,
    )
    assert parsed is not None
    recommendations = {item.ref: item for item in parsed}

    statuses = {
        "NEW-01": ["A", "A", "C", "A", "A", "A"],
        "NEW-02": ["C", "A", "Q", "A", "D", "A"],
        "NEW-03": ["A", "A"],
        "NEW-04": ["A", "C"],
        "NEW-05": ["Q", "A"],
        "NEW-06": ["A", "C", "A"],
        "NEW-07": ["C", "A"],
        "NEW-08": ["Q"],
        "NEW-09": ["D", "C", "A"],
    }
    expanded = {
        ref: reimbursement_pipeline._expand_proposal_statuses(
            {"items": [{"status": status} for status in ticket_statuses]},
            recommendations[ref],
        )
        for ref, ticket_statuses in statuses.items()
    }

    assert expanded["NEW-01"] == ("A", "A", "C", "A", "A", "A")
    assert expanded["NEW-02"] == ("A", "A", "C", "A", "A", "A")
    assert expanded["NEW-04"] == ("A", "C")
    assert expanded["NEW-05"] == ("A", "A")
    assert expanded["NEW-06"] == ("A", "C", "A")
    for ref in ("NEW-03", "NEW-07", "NEW-08", "NEW-09"):
        assert expanded[ref] == tuple("A" for _status in statuses[ref])

    ambiguous = reimbursement_events.ProposalRecommendation(ref="NEW-01", statuses=("A", "C"))
    assert (
        reimbursement_pipeline._expand_proposal_statuses(
            {"items": [{"status": "A"}, {"status": "C"}, {"status": "A"}]},
            ambiguous,
        )
        is None
    )


def test_held_only_approval_grant_replays_idempotently(tmp_path: Path) -> None:
    source = tmp_path / "mail"
    source.mkdir()
    category_map_path = _write_category_map(tmp_path)
    bundle_path = tmp_path / "bundle.json"
    anchors_path = tmp_path / "anchors.json"
    _write_bundle(bundle_path)
    original_id = "<held-replay-original@example.invalid>"
    proposal_id = "<held-replay-proposal@example.invalid>"
    _write_two_item_eml(
        source / "original.eml",
        message_id=original_id,
        received="Thu, 05 Sep 2030 08:00:00 +0000",
    )
    _write_mail(
        source / "proposal.eml",
        message_id=proposal_id,
        received="Fri, 06 Sep 2030 08:00:00 +0000",
        sender="treasurer@example.invalid",
        body="NEW-01\nApprove as is\n",
    )
    _write_mail(
        source / "approval.eml",
        message_id="<held-replay-approval@example.invalid>",
        received="Sat, 07 Sep 2030 08:00:00 +0000",
        sender="reviewer@example.invalid",
        body="Approved.",
        in_reply_to=proposal_id,
        references=(proposal_id,),
    )
    _write_anchors(
        anchors_path,
        secondary_approvers=("reviewer@example.invalid",),
        thread_anchors=[
            {
                "message_id": proposal_id,
                "purpose": "APPROVAL_PROPOSAL",
                "tickets": [_selector(original_id, "NEW-01")],
            }
        ],
        operator_reviews=[
            {
                "ticket": _selector(original_id, "NEW-01"),
                "record_decision": True,
                "items": [
                    {
                        "source_index": index,
                        "status": "D",
                        "why": "Synthetic operator override supersedes the proposal.",
                        "reviewed_amount": "",
                    }
                    for index in (1, 2)
                ],
                "action": "Archive the synthetic declined items",
                "block": "Explicit synthetic operator review supersedes the earlier grant.",
                "asks": [],
                "note": "Synthetic superseding review.",
                "email_questions": [],
                "email_context": "",
            }
        ],
    )

    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        ),
        anchors_path=anchors_path,
    )
    first_bytes = bundle_path.read_bytes()
    first = json.loads(first_bytes)
    ticket = next(item for item in first["tickets"] if item["ref"] == "NEW-01")
    assert [item["status"] for item in ticket["items"]] == ["D", "D"]
    assert ticket["live"]["decision"] == "DECLINED"
    assert {event["kind"] for event in first["supplemental"]["events"]} == {
        "APPROVAL_GRANTED",
        "OPERATOR_REVIEW",
    }

    reimbursement_pipeline.refresh_bundle(
        **_refresh_kwargs(
            bundle_path=bundle_path,
            source=source,
            category_map_path=category_map_path,
        ),
        anchors_path=anchors_path,
    )
    assert bundle_path.read_bytes() == first_bytes
