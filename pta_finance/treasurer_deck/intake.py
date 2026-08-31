"""Private request intake and the deterministic brief cleaner (plan section 5.2).

``--request`` always names a private UTF-8 text file (never literal command-line text).
Inputs are opened read-only, read once, never echoed, and never passed to a shell.
Symlinks/reparse points and non-regular files are rejected; the request is limited to
1 MiB and JSON side inputs to 5 MiB.

The cleaner is PURE and deterministic. It repairs/flags replacement characters,
normalizes whitespace and repeated headings, identifies standalone recording/export
filenames and transcript metadata, separates task-like questions from conversational
workflow guidance, retains source spans for every proposed task, and records every
discarded fragment as ``{source_span, reason, confidence}``. It never converts an
ambiguous sentence into an approved financial fact, and no content is silently
dropped: every non-blank fragment lands in a task, a guidance item, or the ignored
log. Cleanup may normalize the copied display text but never changes span coordinates
or fragment digests.
"""

from __future__ import annotations

import json
import os
import re
import stat
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from pta_finance.treasurer_deck.models import (
    SCHEMA_VERSION,
    BriefDraft,
    BriefTask,
    ContractError,
    IgnoredFragment,
    RunChoices,
    SourceSpan,
    WorkflowGuidance,
    as_mapping,
    as_str,
    check_schema_version,
    ignored_choices_document,
    require_keys,
    sha256_hex,
    task_id_for,
    validate_run_id,
    validate_sha256,
    validate_span_against_text,
    validate_spans_disjoint,
)

MAX_REQUEST_BYTES = 1_048_576  # 1 MiB
MAX_JSON_BYTES = 5_242_880  # 5 MiB

_REPLACEMENT = "�"

#: Standalone recording/export filename (a single token on its own line).
_FILENAME_RE = re.compile(
    r"\S+\.(?:mp4|mov|mkv|m4a|mp3|wav|aac|flac|vtt|srt|log|docx|pdf)",
    re.IGNORECASE | re.ASCII,
)

#: Bare transcript timestamps such as ``00:12:34`` or ``[12:03]``.
_TIMESTAMP_RE = re.compile(r"\[?\d{1,2}:\d{2}(?::\d{2})?\]?", re.ASCII)

#: Case-insensitive transcript/export metadata line prefixes (code-owned).
_METADATA_PREFIXES: tuple[str, ...] = (
    "recording started",
    "recording stopped",
    "recording saved",
    "transcript ",
    "meeting started",
    "meeting ended",
    "webvtt",
)

#: Interrogative lead words that mark a task-like question without a ``?``.
_INTERROGATIVES = frozenset(
    {
        "what",
        "how",
        "why",
        "when",
        "where",
        "which",
        "who",
        "whose",
        "can",
        "could",
        "should",
        "would",
        "do",
        "does",
        "did",
        "is",
        "are",
        "will",
        "have",
        "has",
    }
)

#: Imperative lead verbs that mark conversational workflow guidance (code-owned).
_GUIDANCE_VERBS = frozenset(
    {
        "get",
        "gather",
        "check",
        "challenge",
        "verify",
        "confirm",
        "review",
        "present",
        "keep",
        "avoid",
        "remember",
        "focus",
        "start",
        "make",
        "use",
        "prepare",
        "walk",
        "double-check",
        "compare",
        "highlight",
    }
)

#: First-match keyword -> proposed module key (code-owned; module catalog = Step 15).
_MODULE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("reimburs", "reimbursements"),
    ("balance", "position"),
    ("bank", "position"),
    ("cash", "position"),
    ("reserve", "position"),
    ("budget", "budget_vs_actual"),
    ("spend", "budget_vs_actual"),
    ("spent", "budget_vs_actual"),
    ("actual", "budget_vs_actual"),
    ("fundrais", "fundraising"),
    ("trend", "history"),
    ("history", "history"),
    ("year", "history"),
)
_DEFAULT_MODULE_KEY = "general"


# --- private input files -------------------------------------------------------------


@dataclass(frozen=True)
class RequestText:
    """The exact decoded request plus the digest of its validated UTF-8 file bytes."""

    text: str
    sha256: str


def _open_regular_file(path: Path, *, max_bytes: int, context: str) -> bytes:
    """Read one private input read-only, once, with symlink/size/type checks."""
    if path.is_symlink() or os.path.isjunction(str(path)):
        raise ContractError(f"{context} must not be a symlink/reparse point")
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ContractError(f"{context} is not readable") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ContractError(f"{context} must be a regular file")
    if info.st_size > max_bytes:
        raise ContractError(f"{context} exceeds the {max_bytes}-byte limit")
    with open(path, "rb") as handle:
        return handle.read()


def read_request_file(path: Path) -> RequestText:
    """Validate and decode the private request file; digest covers the exact bytes."""
    raw = _open_regular_file(path, max_bytes=MAX_REQUEST_BYTES, context="request file")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("request file is not valid UTF-8") from exc
    return RequestText(text=text, sha256=sha256_hex(raw))


def read_private_json_file(path: Path, context: str = "input file") -> Mapping[str, Any]:
    """Read one private UTF-8 JSON side input (proposals/overrides/directives)."""
    raw = _open_regular_file(path, max_bytes=MAX_JSON_BYTES, context=context)
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{context} is not valid UTF-8 JSON") from exc
    return as_mapping(value, context)


def _reject_json_constant(name: str) -> None:
    raise ContractError(f"non-finite JSON constant is rejected: {name}")


# --- span bookkeeping ----------------------------------------------------------------


class _SpanIndex:
    """Zero-based code-point offsets <-> one-based line/column coordinates."""

    def __init__(self, text: str) -> None:
        self._text = text
        starts = [0]
        for offset, char in enumerate(text):
            if char == "\n":
                starts.append(offset + 1)
        self._line_starts = starts

    def coords(self, offset: int) -> tuple[int, int]:
        line_index = bisect_right(self._line_starts, offset) - 1
        return line_index + 1, offset - self._line_starts[line_index] + 1

    def span(self, start: int, end: int) -> SourceSpan:
        start_line, start_column = self.coords(start)
        end_line, end_column = self.coords(end)
        return SourceSpan(
            start_codepoint=start,
            end_codepoint=end,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
            fragment_sha256=sha256_hex(self._text[start:end].encode("utf-8")),
        )


def _trimmed_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Shrink ``[start, end)`` to exclude leading/trailing whitespace in ``text``."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _normalize_display(fragment: str) -> str:
    """Whitespace normalization for the copied display text (coordinates untouched)."""
    return re.sub(r"\s+", " ", fragment).strip()


# --- classification ------------------------------------------------------------------


def _is_export_filename(display: str) -> bool:
    return _FILENAME_RE.fullmatch(display) is not None


def _is_transcript_metadata(display: str) -> bool:
    if _TIMESTAMP_RE.fullmatch(display) is not None:
        return True
    lowered = display.casefold()
    return any(lowered.startswith(prefix) for prefix in _METADATA_PREFIXES)


def _is_question(display: str) -> bool:
    stripped = display.rstrip("\"')")
    if stripped.endswith("?"):
        return True
    words = display.casefold().split()
    return len(words) >= 3 and words[0] in _INTERROGATIVES


def _is_guidance(display: str) -> bool:
    words = display.casefold().split()
    return len(words) >= 2 and words[0] in _GUIDANCE_VERBS


def _is_heading_like(display: str) -> bool:
    if display.startswith("#"):
        return True
    return len(display.split()) <= 8 and not display.endswith((".", "?", "!", ":"))


def propose_module_key(display: str) -> str:
    """Deterministic first-match keyword mapping to a proposed module key."""
    lowered = display.casefold()
    for keyword, module_key in _MODULE_KEYWORDS:
        if keyword in lowered:
            return module_key
    return _DEFAULT_MODULE_KEY


# --- the deterministic cleaner -------------------------------------------------------


@dataclass(frozen=True)
class ProposedTask:
    """A cleaned task before its content-derived ID is minted."""

    question: str
    module_key: str
    required: bool
    source_spans: tuple[SourceSpan, ...]


@dataclass(frozen=True)
class CleanedRequest:
    """Pure cleaner output: every non-blank fragment is accounted for exactly once."""

    tasks: tuple[ProposedTask, ...]
    workflow_guidance: tuple[WorkflowGuidance, ...]
    ignored: tuple[IgnoredFragment, ...]


def clean_request(text: str) -> CleanedRequest:
    """Deterministically separate tasks, workflow guidance, and ignored fragments."""
    index = _SpanIndex(text)
    tasks: list[ProposedTask] = []
    guidance: list[WorkflowGuidance] = []
    ignored: list[IgnoredFragment] = []
    seen_headings: set[str] = set()

    offset = 0
    for line in text.split("\n"):
        line_start = offset
        offset += len(line) + 1

        # Split the line into replacement-character runs and content segments so a
        # flagged bad character never overlaps the spans of the content around it.
        content_segments: list[tuple[int, int]] = []
        cursor = 0
        while cursor < len(line):
            if line[cursor] == _REPLACEMENT:
                run_end = cursor
                while run_end < len(line) and line[run_end] == _REPLACEMENT:
                    run_end += 1
                ignored.append(
                    IgnoredFragment(
                        source_span=index.span(line_start + cursor, line_start + run_end),
                        reason="invalid_unicode",
                        confidence="high",
                    )
                )
                cursor = run_end
                continue
            run_end = cursor
            while run_end < len(line) and line[run_end] != _REPLACEMENT:
                run_end += 1
            start, end = _trimmed_bounds(line, cursor, run_end)
            if start < end:
                content_segments.append((line_start + start, line_start + end))
            cursor = run_end

        if not content_segments:
            continue
        spans = tuple(index.span(start, end) for start, end in content_segments)
        display = _normalize_display(" ".join(text[start:end] for start, end in content_segments))

        if _is_export_filename(display):
            ignored.extend(
                IgnoredFragment(source_span=span, reason="export_filename", confidence="high")
                for span in spans
            )
            continue
        if _is_transcript_metadata(display):
            ignored.extend(
                IgnoredFragment(source_span=span, reason="transcript_metadata", confidence="high")
                for span in spans
            )
            continue
        if _is_heading_like(display):
            heading_key = display.casefold()
            if heading_key in seen_headings:
                ignored.extend(
                    IgnoredFragment(
                        source_span=span, reason="repeated_heading", confidence="medium"
                    )
                    for span in spans
                )
                continue
            seen_headings.add(heading_key)
        if _is_question(display):
            tasks.append(
                ProposedTask(
                    question=display,
                    module_key=propose_module_key(display),
                    required=True,
                    source_spans=spans,
                )
            )
            continue
        if _is_guidance(display):
            guidance.append(WorkflowGuidance(text=display, source_spans=spans))
            continue
        ignored.extend(
            IgnoredFragment(source_span=span, reason="ambiguous_prose", confidence="low")
            for span in spans
        )

    return CleanedRequest(
        tasks=tuple(tasks), workflow_guidance=tuple(guidance), ignored=tuple(ignored)
    )


def build_brief_draft(run_id: str, request: RequestText) -> BriefDraft:
    """Run the cleaner and mint content-derived task IDs into a ``BriefDraft``."""
    validate_run_id(run_id)
    cleaned = clean_request(request.text)
    brief_tasks = []
    for task in cleaned.tasks:
        record = {
            "module_key": task.module_key,
            "question": task.question,
            "required": task.required,
            "source_spans": [span.to_json() for span in task.source_spans],
        }
        brief_tasks.append(
            BriefTask(
                task_id=task_id_for(record, task.module_key),
                source_spans=task.source_spans,
                module_key=task.module_key,
                question=task.question,
                required=task.required,
            )
        )
    draft = BriefDraft(
        run_id=run_id,
        request_sha256=request.sha256,
        tasks=tuple(brief_tasks),
        workflow_guidance=cleaned.workflow_guidance,
        ignored=cleaned.ignored,
    )
    draft.validate_against_request(request.text)
    return draft


def build_ignored_choices(
    *, run_id: str, request: RequestText, brief: BriefDraft, choices: RunChoices
) -> dict[str, object]:
    """Assemble the ``ignored-choices.json`` root for the run."""
    if brief.request_sha256 != request.sha256:
        raise ContractError("brief does not belong to this request (digest mismatch)")
    return ignored_choices_document(
        run_id=run_id,
        request_sha256=request.sha256,
        ignored=brief.ignored,
        choices=choices,
    )


# --- optional provider-neutral brief proposal (section 5.2) --------------------------


def parse_brief_proposal(
    value: Mapping[str, Any], *, request: RequestText
) -> tuple[tuple[BriefTask, ...], tuple[WorkflowGuidance, ...], tuple[IgnoredFragment, ...]]:
    """Validate a provider-neutral suggestion document (no facts, no approvals).

    The document uses the ``brief.draft.json`` task/guidance/ignored subset plus
    ``schema_version`` and ``request_sha256``. The same source-span and ignore-log
    validators apply, and every task ID must match its own content hash — a proposal
    can suggest cleanup, but it carries no factual or approval authority.
    """
    context = "brief proposal"
    check_schema_version(value, context)
    require_keys(
        value,
        required=("schema_version", "request_sha256", "tasks", "workflow_guidance", "ignored"),
        context=context,
    )
    declared = validate_sha256(
        as_str(value["request_sha256"], f"{context}.request_sha256"), "request_sha256"
    )
    if declared != request.sha256:
        raise ContractError(f"{context} does not match the request digest")
    tasks = tuple(
        BriefTask.from_json(as_mapping(item, f"{context}.tasks[]"))
        for item in _sequence(value, "tasks", context)
    )
    guidance = tuple(
        WorkflowGuidance.from_json(as_mapping(item, f"{context}.workflow_guidance[]"))
        for item in _sequence(value, "workflow_guidance", context)
    )
    ignored = tuple(
        IgnoredFragment.from_json(as_mapping(item, f"{context}.ignored[]"))
        for item in _sequence(value, "ignored", context)
    )
    spans: list[SourceSpan] = []
    for task in tasks:
        record = {
            "module_key": task.module_key,
            "question": task.question,
            "required": task.required,
            "source_spans": [span.to_json() for span in task.source_spans],
        }
        if task.task_id != task_id_for(record, task.module_key):
            raise ContractError(
                f"{context}: task_id {task.task_id!r} does not match its content hash"
            )
        spans.extend(task.source_spans)
    for item in guidance:
        spans.extend(item.source_spans)
    for fragment in ignored:
        spans.append(fragment.source_span)
    for span in spans:
        validate_span_against_text(span, request.text, f"{context} span")
    validate_spans_disjoint(spans, f"{context} spans")
    return tasks, guidance, ignored


def _sequence(value: Mapping[str, Any], key: str, context: str) -> list[Any]:
    item = value[key]
    if isinstance(item, str | bytes) or not isinstance(item, list):
        raise ContractError(f"{context}.{key} must be an array")
    return item


__all__ = [
    "MAX_JSON_BYTES",
    "MAX_REQUEST_BYTES",
    "CleanedRequest",
    "ProposedTask",
    "RequestText",
    "SCHEMA_VERSION",
    "build_brief_draft",
    "build_ignored_choices",
    "clean_request",
    "parse_brief_proposal",
    "propose_module_key",
    "read_private_json_file",
    "read_request_file",
]
