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

from pta_finance import reimbursement_pipeline, reimbursement_report

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
"""
    )
    path.write_bytes(message.as_bytes())


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
