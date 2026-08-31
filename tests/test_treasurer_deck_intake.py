"""Behavior tests for private request intake and the deterministic cleaner.

All request fixtures are fictional; no real identity or finance value appears here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pta_finance.treasurer_deck import intake, models

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

#: The motivating request shape: opening/closing process prose, finance questions,
#: a stray recording filename, transcript metadata, and one malformed character.
EXAMPLE_REQUEST = (
    "Treasurer update\n"
    "\n"
    "Get facts before making slides\n"
    "GMT20260830-recording.mp4\n"
    "Recording started at 10:02\n"
    "What is our current bank balance?\n"
    "How much have we spent against the budget?\n"
    "Balance �100 what is it?\n"
    "Treasurer update\n"
    "We may possibly owe the example vendor something.\n"
    "Challenge the narrative before presenting\n"
)


def _write_request(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "request.txt"
    path.write_bytes(text.encode("utf-8"))
    return path


def _request(tmp_path: Path, text: str = EXAMPLE_REQUEST) -> intake.RequestText:
    return intake.read_request_file(_write_request(tmp_path, text))


# --- private input files -------------------------------------------------------------


def test_read_request_file_digest_covers_exact_bytes(tmp_path: Path) -> None:
    request = _request(tmp_path)
    assert request.text == EXAMPLE_REQUEST
    assert request.sha256 == models.sha256_hex(EXAMPLE_REQUEST.encode("utf-8"))


def test_read_request_file_rejects_oversize(tmp_path: Path) -> None:
    path = tmp_path / "request.txt"
    path.write_bytes(b"x" * (intake.MAX_REQUEST_BYTES + 1))
    with pytest.raises(models.ContractError):
        intake.read_request_file(path)


def test_read_request_file_rejects_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "request.txt"
    path.write_bytes(b"\xff\xfe broken")
    with pytest.raises(models.ContractError):
        intake.read_request_file(path)


def test_read_request_file_rejects_non_regular_and_symlink(tmp_path: Path) -> None:
    with pytest.raises(models.ContractError):
        intake.read_request_file(tmp_path)  # a directory is not a regular file
    real = _write_request(tmp_path, "hello\n")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlink creation is not permitted in this environment")
    with pytest.raises(models.ContractError):
        intake.read_request_file(link)


def test_read_private_json_file_limits_and_strictness(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    assert intake.read_private_json_file(path)["schema_version"] == 1
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(models.ContractError):
        intake.read_private_json_file(path)
    path.write_text('{"x": NaN}', encoding="utf-8")
    with pytest.raises(models.ContractError):
        intake.read_private_json_file(path)


# --- the deterministic cleaner -------------------------------------------------------


def test_clean_request_is_deterministic() -> None:
    assert intake.clean_request(EXAMPLE_REQUEST) == intake.clean_request(EXAMPLE_REQUEST)


def test_cleaner_separates_tasks_guidance_and_noise(tmp_path: Path) -> None:
    """The motivating shape: prose -> guidance, questions -> tasks, noise -> ignored."""
    cleaned = intake.clean_request(EXAMPLE_REQUEST)

    questions = [task.question for task in cleaned.tasks]
    assert "What is our current bank balance?" in questions
    assert "How much have we spent against the budget?" in questions
    assert "Balance 100 what is it?" in questions  # repaired display text

    guidance_texts = [item.text for item in cleaned.workflow_guidance]
    assert "Get facts before making slides" in guidance_texts
    assert "Challenge the narrative before presenting" in guidance_texts

    reasons = {fragment.reason for fragment in cleaned.ignored}
    assert "export_filename" in reasons
    assert "transcript_metadata" in reasons
    assert "invalid_unicode" in reasons
    assert "repeated_heading" in reasons
    assert "ambiguous_prose" in reasons


def test_cleaner_proposes_module_keys_deterministically() -> None:
    cleaned = intake.clean_request(EXAMPLE_REQUEST)
    by_question = {task.question: task.module_key for task in cleaned.tasks}
    assert by_question["What is our current bank balance?"] == "position"
    assert by_question["How much have we spent against the budget?"] == "budget_vs_actual"
    assert intake.propose_module_key("Anything else entirely") == "general"


def test_cleaner_never_silently_drops_content() -> None:
    """Every non-whitespace code point of the request is covered by some span."""
    cleaned = intake.clean_request(EXAMPLE_REQUEST)
    covered: set[int] = set()
    spans = [fragment.source_span for fragment in cleaned.ignored]
    for task in cleaned.tasks:
        spans.extend(task.source_spans)
    for item in cleaned.workflow_guidance:
        spans.extend(item.source_spans)
    for span in spans:
        covered.update(range(span.start_codepoint, span.end_codepoint))
    for offset, char in enumerate(EXAMPLE_REQUEST):
        if not char.isspace():
            assert offset in covered, f"code point {offset} ({char!r}) was dropped"


def test_replacement_character_is_flagged_without_losing_the_line() -> None:
    """The malformed character is logged; the question around it keeps two spans."""
    cleaned = intake.clean_request(EXAMPLE_REQUEST)
    task = next(t for t in cleaned.tasks if t.question == "Balance 100 what is it?")
    assert len(task.source_spans) == 2
    bad = [f for f in cleaned.ignored if f.reason == "invalid_unicode"]
    assert len(bad) == 1
    span = bad[0].source_span
    assert EXAMPLE_REQUEST[span.start_codepoint : span.end_codepoint] == "�"


def test_cleaner_span_provenance_binds_to_the_request(tmp_path: Path) -> None:
    """Spans carry exact fragment digests and one-based line/column coordinates."""
    request = _request(tmp_path)
    run_id = models.new_run_id(NOW)
    brief = intake.build_brief_draft(run_id, request)
    brief.validate_against_request(request.text)
    first_guidance = brief.workflow_guidance[0]
    span = first_guidance.source_spans[0]
    assert span.start_line == 3  # "Get facts before making slides"
    assert span.start_column == 1
    fragment = request.text[span.start_codepoint : span.end_codepoint]
    assert fragment == "Get facts before making slides"


def test_build_brief_draft_mints_content_derived_task_ids(tmp_path: Path) -> None:
    request = _request(tmp_path)
    brief = intake.build_brief_draft(models.new_run_id(NOW), request)
    for task in brief.tasks:
        record = {
            "module_key": task.module_key,
            "question": task.question,
            "required": task.required,
            "source_spans": [span.to_json() for span in task.source_spans],
        }
        assert task.task_id == models.task_id_for(record, task.module_key)
    restored = models.BriefDraft.from_json(brief.to_json())
    assert restored == brief


def test_build_ignored_choices_document(tmp_path: Path) -> None:
    request = _request(tmp_path)
    run_id = models.new_run_id(NOW)
    brief = intake.build_brief_draft(run_id, request)
    choices = models.RunChoices(
        audience=models.AUDIENCE_INTERNAL,
        skip_overview=True,
        excluded_modules=("fundraising",),
        requested_graphics=("expense_donut",),
    )
    document = intake.build_ignored_choices(
        run_id=run_id, request=request, brief=brief, choices=choices
    )
    assert document["schema_version"] == 1
    assert document["request_sha256"] == request.sha256
    choices_json = document["choices"]
    assert isinstance(choices_json, dict)
    assert choices_json["skip_overview"] is True
    other = intake.RequestText(text="different", sha256="b" * 64)
    with pytest.raises(models.ContractError):
        intake.build_ignored_choices(run_id=run_id, request=other, brief=brief, choices=choices)


def test_run_choices_reject_unknown_values() -> None:
    with pytest.raises(models.ContractError):
        models.RunChoices(
            audience="everyone",
            skip_overview=False,
            excluded_modules=(),
            requested_graphics=(),
        )
    with pytest.raises(models.ContractError):
        models.RunChoices(
            audience=models.AUDIENCE_PUBLIC,
            skip_overview=False,
            excluded_modules=("Bad Module",),
            requested_graphics=(),
        )


# --- provider-neutral brief proposal -------------------------------------------------


def _proposal_document(request: intake.RequestText) -> dict[str, object]:
    brief = intake.build_brief_draft(models.new_run_id(NOW), request)
    return {
        "schema_version": 1,
        "request_sha256": request.sha256,
        "tasks": [task.to_json() for task in brief.tasks],
        "workflow_guidance": [item.to_json() for item in brief.workflow_guidance],
        "ignored": [fragment.to_json() for fragment in brief.ignored],
    }


def test_brief_proposal_round_trip(tmp_path: Path) -> None:
    request = _request(tmp_path)
    tasks, guidance, ignored = intake.parse_brief_proposal(
        _proposal_document(request), request=request
    )
    assert tasks and guidance and ignored


def test_brief_proposal_cannot_contain_facts_or_approvals(tmp_path: Path) -> None:
    request = _request(tmp_path)
    document = _proposal_document(request)
    for forbidden in ("facts", "approvals"):
        with pytest.raises(models.ContractError):
            intake.parse_brief_proposal({**document, forbidden: []}, request=request)


def test_brief_proposal_rejects_wrong_request_digest(tmp_path: Path) -> None:
    request = _request(tmp_path)
    document = _proposal_document(request)
    document["request_sha256"] = "c" * 64
    with pytest.raises(models.ContractError):
        intake.parse_brief_proposal(document, request=request)


def test_brief_proposal_rejects_forged_task_ids(tmp_path: Path) -> None:
    """A task whose ID does not hash its own content is rejected."""
    request = _request(tmp_path)
    document = _proposal_document(request)
    tasks = document["tasks"]
    assert isinstance(tasks, list)
    first = dict(tasks[0])
    first["question"] = first["question"] + " (edited)"
    tasks[0] = first
    with pytest.raises(models.ContractError):
        intake.parse_brief_proposal(document, request=request)


def test_brief_proposal_rejects_tampered_spans(tmp_path: Path) -> None:
    """A span whose fragment digest no longer matches the request is rejected."""
    request = _request(tmp_path)
    document = _proposal_document(request)
    ignored = document["ignored"]
    assert isinstance(ignored, list)
    fragment = dict(ignored[0])
    span = dict(fragment["source_span"])
    span["fragment_sha256"] = "d" * 64
    fragment["source_span"] = span
    ignored[0] = fragment
    with pytest.raises(models.ContractError):
        intake.parse_brief_proposal(document, request=request)
