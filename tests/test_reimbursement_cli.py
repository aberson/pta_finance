"""CLI contracts for the private reimbursement refresh/report boundary.

The fixtures are deliberately fictional.  These tests also make every Google entry point
explode if touched: neither command is allowed to reach Sheets, and report-only rendering is
not allowed to load config or Gmail at all.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from pta_finance import cli, reimbursement_pipeline, reimbursement_report
from pta_finance.config import Config


def _bundle() -> dict[str, object]:
    review_key = "submission:v1:" + "a" * 64
    return {
        "schema_version": 1,
        "report": {
            "title": "Example Reimbursement Review",
            "eyebrow": "Example Organization - Treasurer",
            "subtitle": "Structured fictional review queue.",
            "organization": "Example Organization",
            "email_signoff": ["Thank you!", "Example Treasurer Team"],
            "logo_data_uri": "",
            "confirmed_outstanding": "12.34",
            "cutoff_date": "2026-06-01",
            "policy_version": "example-v1",
            "as_of_date": "2026-08-04",
        },
        "provenance": {
            "mapped_sha256": "1" * 64,
            "policy_sha256": "2" * 64,
            "source_snapshot_sha256": "3" * 64,
            "accounted_review_keys": [review_key],
        },
        "source_summary": {
            "mapped_rows": 1,
            "mapped_submissions": 1,
            "mapped_total": "12.34",
            "first_received": "2026-08-02",
            "last_received": "2026-08-02",
        },
        "tickets": [
            {
                "review_key": review_key,
                "ref": "NEW-01",
                "form_label": "",
                "origin": "submission",
                "display_order": 1,
                "requestor_name": "Fictional Requester",
                "form_type": "Example Reimbursement Form",
                "submitted": "2026-08-02",
                "submitted_label": "",
                "payment_method": "Zelle",
                "source_evidence_sha256": "4" * 64,
                "source": {
                    "stated_total": "12.34",
                    "mapped_total": "12.34",
                    "categories": ["Classroom Supplies"],
                    "flags": [],
                },
                "live": {
                    "workflow_state": "ACTIVE",
                    "decision": "APPROVED",
                    "payment_status": "NOT_PAID",
                    "payment_date": "",
                    "confirmations": [],
                },
                "review": {
                    "status": "A",
                    "action": "Send the fictional payment.",
                    "block": "No blocker in this example.",
                    "asks": [],
                    "note": "Fictional reviewer note.",
                    "email_questions": [],
                    "email_context": "",
                },
                "items": [
                    {
                        "item_key": "fictional-line-1",
                        "source_index": 1,
                        "source_date": "2026-08-01",
                        "source_description": "Fictional classroom materials",
                        "source_amount": "12.34",
                        "canonical_category": "Classroom Supplies",
                        "display_date": "",
                        "display_item": "",
                        "reviewed_amount": "",
                        "status": "A",
                        "why": "The fictional evidence is complete.",
                    }
                ],
                "messages": [{"kind": "draft", "date": "", "mode": "generated", "body": ""}],
                "archive_note": "",
            }
        ],
        "appendix": {"amendments": [], "cfo_checks": [], "excluded": [], "defects": []},
    }


def _report_summary() -> reimbursement_report.ReportSummary:
    return reimbursement_report.ReportSummary(
        review_rows=1,
        active=1,
        settled=0,
        live_unreviewed=0,
        new_records=1,
        legacy_records=0,
        item_lines=1,
        known_total=Decimal("12.34"),
        approved=Decimal("12.34"),
        clarification=Decimal("0.00"),
        declined=Decimal("0.00"),
        question=Decimal("0.00"),
        outstanding=Decimal("12.34"),
        legacy_outstanding=Decimal("0.00"),
        emails_to_send=1,
    )


def _merge_summary() -> reimbursement_pipeline.BundleMergeSummary:
    return reimbursement_pipeline.BundleMergeSummary(
        new_tickets=1,
        unchanged_tickets=2,
        total_source_tickets=3,
        mapped_rows=4,
        mapped_total=Decimal("45.67"),
        first_received="2026-06-02",
        last_received="2026-08-03",
        supplemental_evidence=5,
        supplemental_events=3,
        unmatched_evidence=2,
        supplemental_excluded_by_cutoff=7,
    )


def _forbidden(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("this command crossed an external-service boundary")


def _ban_google(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "SheetsClient", _forbidden)
    for name in ("needs_consent", "load_or_mint_credentials", "build_service", "fetch_window"):
        monkeypatch.setattr(cli.gmail_source, name, _forbidden)


def test_report_reimbursements_is_offline_and_prints_only_aggregates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_path = tmp_path / "bundle.json"
    output_path = tmp_path / "queue.html"
    data_path.write_text(json.dumps(_bundle()), encoding="utf-8")

    monkeypatch.setattr(cli, "_load", _forbidden)
    _ban_google(monkeypatch)

    rc = cli.main(
        [
            "report-reimbursements",
            "--data",
            str(data_path),
            "--output",
            str(output_path),
        ]
    )

    stdout = capsys.readouterr().out
    assert rc == 0
    assert output_path.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert "Fictional Requester" in output_path.read_text(encoding="utf-8")
    assert "1 active, 0 settled" in stdout
    assert "$12.34 approved" in stdout
    assert "Fictional Requester" not in stdout
    assert "example.org" not in stdout


def test_update_reimbursements_dry_run_preserves_bundle_and_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_config: Config,
) -> None:
    source = tmp_path / "mail"
    bundle_path = tmp_path / "bundle.json"
    output_path = tmp_path / "queue.html"
    category_map = tmp_path / "category-map.csv"
    anchors_path = tmp_path / "anchors.json"
    bundle_path.write_text("prior bundle", encoding="utf-8")
    output_path.write_text("prior report", encoding="utf-8")
    prior_bundle = bundle_path.read_bytes()
    prior_html = output_path.read_bytes()
    captured: dict[str, Any] = {}

    def plan(**kwargs: Any) -> tuple[dict[str, Any], reimbursement_pipeline.BundleMergeSummary]:
        captured.update(kwargs)
        return {}, _merge_summary()

    monkeypatch.setattr(cli, "_load", lambda _args: fake_config)
    monkeypatch.setattr(cli.reimbursement_pipeline, "plan_bundle_refresh", plan)
    monkeypatch.setattr(cli.reimbursement_pipeline, "refresh_bundle", _forbidden)
    monkeypatch.setattr(cli.reimbursement_report, "build_report", _forbidden)
    _ban_google(monkeypatch)

    rc = cli.main(
        [
            "update-reimbursements",
            "--source",
            str(source),
            "--category-map",
            str(category_map),
            "--data",
            str(bundle_path),
            "--anchors",
            str(anchors_path),
            "--output",
            str(output_path),
            "--received-since",
            "2026-06-01",
            "--as-of",
            "2026-08-04",
            "--dry-run",
        ]
    )

    stdout = capsys.readouterr().out
    assert rc == 0
    assert bundle_path.read_bytes() == prior_bundle
    assert output_path.read_bytes() == prior_html
    assert captured == {
        "bundle_path": bundle_path,
        "source": source,
        "category_map_path": category_map,
        "start_month": 1,
        "received_since": date(2026, 6, 1),
        "as_of": date(2026, 8, 4),
        "subject_filter": None,
        "anchors_path": anchors_path,
    }
    assert "3 source submission(s), 4 line(s), $45.67" in stdout
    assert "5 evidence, 3 event(s), 2 unmatched, 7 cutoff-excluded" in stdout
    assert "no bundle or report files written" in stdout
    assert "Fictional Requester" not in stdout


def test_update_reimbursements_refreshes_then_reports_without_sheets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_config: Config,
) -> None:
    source = tmp_path / "mail"
    bundle_path = tmp_path / "bundle.json"
    output_path = tmp_path / "queue.html"
    category_map = tmp_path / "category-map.csv"
    events: list[str] = []

    def refresh(**kwargs: Any) -> reimbursement_pipeline.BundleMergeSummary:
        events.append("refresh")
        assert kwargs["source"] == source
        assert kwargs["bundle_path"] == bundle_path
        bundle_path.write_text("Fictional Requester private bundle", encoding="utf-8")
        return _merge_summary()

    def report(data: Path, output: Path) -> reimbursement_report.BuildResult:
        events.append("report")
        assert data == bundle_path
        assert data.read_text(encoding="utf-8") == "Fictional Requester private bundle"
        output.write_text("rendered private report", encoding="utf-8")
        return reimbursement_report.BuildResult(
            output_path=output.resolve(),
            sha256="5" * 64,
            bytes_written=output.stat().st_size,
            summary=_report_summary(),
        )

    monkeypatch.setattr(cli, "_load", lambda _args: fake_config)
    monkeypatch.setattr(cli.reimbursement_pipeline, "refresh_bundle", refresh)
    monkeypatch.setattr(cli.reimbursement_pipeline, "plan_bundle_refresh", _forbidden)
    monkeypatch.setattr(cli.reimbursement_report, "build_report", report)
    _ban_google(monkeypatch)

    rc = cli.main(
        [
            "update-reimbursements",
            "--source",
            str(source),
            "--category-map",
            str(category_map),
            "--data",
            str(bundle_path),
            "--output",
            str(output_path),
            "--received-since",
            "2026-06-01",
            "--as-of",
            "2026-08-04",
        ]
    )

    stdout = capsys.readouterr().out
    assert rc == 0
    assert events == ["refresh", "report"]
    assert output_path.read_text(encoding="utf-8") == "rendered private report"
    assert "1 new, 2 unchanged" in stdout
    assert "5 evidence, 3 event(s), 2 unmatched, 7 cutoff-excluded" in stdout
    assert "1 active, 0 settled" in stdout
    assert "Fictional Requester" not in stdout


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--fetch-until", "2026-08-05"),
        ("--fetch-query", "has:attachment"),
        ("--fetch-limit", "5"),
    ],
)
def test_update_fetch_options_require_fetch_since(
    option: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_load", _forbidden)
    monkeypatch.setattr(cli.reimbursement_pipeline, "plan_bundle_refresh", _forbidden)
    monkeypatch.setattr(cli.reimbursement_pipeline, "refresh_bundle", _forbidden)
    monkeypatch.setattr(cli.reimbursement_report, "build_report", _forbidden)
    _ban_google(monkeypatch)

    rc = cli.main(["update-reimbursements", option, value])

    stdout = capsys.readouterr().out
    assert rc == 1
    assert "require --fetch-since" in stdout


def test_report_failure_preserves_prior_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data_path = tmp_path / "invalid.json"
    output_path = tmp_path / "queue.html"
    data_path.write_text('{"not": "a valid bundle"}', encoding="utf-8")
    output_path.write_text("prior complete report", encoding="utf-8")

    monkeypatch.setattr(cli, "_load", _forbidden)
    _ban_google(monkeypatch)

    rc = cli.main(
        [
            "report-reimbursements",
            "--data",
            str(data_path),
            "--output",
            str(output_path),
        ]
    )

    stdout = capsys.readouterr().out
    assert rc == 1
    assert output_path.read_text(encoding="utf-8") == "prior complete report"
    assert "report-reimbursements:" in stdout
    assert "Fictional Requester" not in stdout
