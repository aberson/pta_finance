"""Versioned run/brief/fact/dataset contracts and locked atomic run persistence.

Single source of truth for the Treasurer-deck data contracts defined in
``documentation/treasurer-slides-plan.md`` section 5.1-5.3:

* **Canonical JSON** (:func:`canonical_json`) — UTF-8, Unicode NFC strings,
  lexicographically sorted object keys, compact separators, preserved array order,
  JSON ``true``/``false``/``null`` literals. Finite :class:`~decimal.Decimal` values
  serialize as JSON strings in plain decimal notation (trailing fractional zeroes/dot
  removed, negative zero normalized to ``"0"``); binary floating-point values are
  rejected outright. Artifact digests cover the complete canonical object with the
  digest field omitted; content-derived IDs additionally omit the ID field.
* **Closed identifier grammars** — ``run_id``, ``fact_id`` (+ ``@period-slug``),
  ``dataset_id``, ``task_id``, source aliases, units, bases, origins, audiences.
* **The run state machine and approvals** — a changed upstream artifact invalidates
  every downstream approval; approvals are never inferred from a previous run.
* **Locked atomic persistence** — atomic run-directory claim, a per-run single-writer
  lock, write-once artifacts, and a manifest-generation compare-and-swap where every
  multi-file transition writes immutable artifacts first and atomically replaces
  ``manifest.json`` last. Resume validates every referenced digest and treats
  partial/corrupt state as blocked rather than guessing.

Everything here is pure local computation and private local file I/O; nothing in this
module can touch a Google API.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Any

SCHEMA_VERSION = 1

# --- errors --------------------------------------------------------------------------


class TreasurerDeckError(ValueError):
    """Base class for every Treasurer-deck contract/persistence failure (fail closed)."""


class ContractError(TreasurerDeckError):
    """A value violates one of the versioned data contracts."""


class RunStateError(TreasurerDeckError):
    """An invalid state transition, approval, or stale-source condition."""


class RunLockedError(TreasurerDeckError):
    """The per-run single-writer lock is already held."""


class RunCollisionError(TreasurerDeckError):
    """The atomic run-directory claim lost to an existing directory."""


class RunCorruptError(TreasurerDeckError):
    """Partial/corrupt on-disk run state; the run is blocked, never guessed at."""


# --- canonical JSON + hashing --------------------------------------------------------


def decimal_to_plain(value: Decimal) -> str:
    """Plain decimal notation: no exponent, trailing fractional zeroes/dot removed.

    Negative zero normalizes to ``"0"``. Non-finite values are rejected.
    """
    if not value.is_finite():
        raise ContractError(f"non-finite Decimal is not representable: {value}")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"-0", ""}:
        text = "0"
    return text


def _emit_canonical(value: object, out: list[str]) -> None:
    if value is None:
        out.append("null")
        return
    if value is True:
        out.append("true")
        return
    if value is False:
        out.append("false")
        return
    if isinstance(value, str):
        out.append(json.dumps(unicodedata.normalize("NFC", value), ensure_ascii=False))
        return
    if isinstance(value, float):
        raise ContractError("binary floating-point values are rejected in canonical JSON")
    if isinstance(value, int):
        out.append(str(value))
        return
    if isinstance(value, Decimal):
        out.append(json.dumps(decimal_to_plain(value)))
        return
    if isinstance(value, Mapping):
        normalized: list[tuple[str, object]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError("canonical JSON object keys must be strings")
            normalized.append((unicodedata.normalize("NFC", key), item))
        keys = [key for key, _ in normalized]
        if len(set(keys)) != len(keys):
            raise ContractError("canonical JSON object has duplicate keys after NFC")
        out.append("{")
        for index, (key, item) in enumerate(sorted(normalized, key=lambda pair: pair[0])):
            if index:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _emit_canonical(item, out)
        out.append("}")
        return
    if isinstance(value, list | tuple):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _emit_canonical(item, out)
        out.append("]")
        return
    raise ContractError(f"unsupported type in canonical JSON: {type(value).__name__}")


def canonical_json(value: object) -> str:
    """Serialize ``value`` to the canonical JSON text every digest is computed over."""
    out: list[str] = []
    _emit_canonical(value, out)
    return "".join(out)


def canonical_json_bytes(value: object) -> bytes:
    """UTF-8 bytes of :func:`canonical_json` — exactly what run artifacts store."""
    return canonical_json(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """Lowercase hex SHA-256 of ``data``."""
    return sha256(data).hexdigest()


def json_sha256(value: object) -> str:
    """SHA-256 of the canonical JSON encoding of ``value``."""
    return sha256_hex(canonical_json_bytes(value))


def digest_of(record: Mapping[str, object], *, omit: tuple[str, ...]) -> str:
    """Digest of ``record`` with the named fields omitted (its own digest/ID fields)."""
    return json_sha256({key: item for key, item in record.items() if key not in omit})


def content_suffix(record: Mapping[str, object], *, omit: tuple[str, ...]) -> str:
    """First 12 hex characters of the content digest — the content-derived ID suffix."""
    return digest_of(record, omit=omit)[:12]


# --- timestamps and dates ------------------------------------------------------------


def format_utc(value: datetime) -> str:
    """RFC 3339 UTC timestamp with a trailing ``Z``; requires an aware UTC datetime."""
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(None):
        raise ContractError("timestamps must be timezone-aware UTC datetimes")
    return value.replace(tzinfo=None).isoformat() + "Z"


def parse_utc(text: str, context: str = "timestamp") -> datetime:
    """Strict inverse of :func:`format_utc` (round-trip enforced).

    Offending values are never interpolated into errors — these parsers see raw
    private-input text and must not echo it (plan section 5.2).
    """
    if not text.endswith("Z"):
        raise ContractError(f"{context} must be an RFC 3339 UTC value ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1])
    except ValueError as exc:
        raise ContractError(f"{context} is not a valid RFC 3339 UTC value") from exc
    if parsed.tzinfo is not None:
        raise ContractError(f"{context} must not carry an explicit offset")
    value = parsed.replace(tzinfo=UTC)
    if format_utc(value) != text:
        raise ContractError(f"{context} is not in canonical form")
    return value


def parse_iso_date(text: str, context: str = "date") -> date:
    """Strict ISO ``YYYY-MM-DD`` calendar date (round-trip enforced, no value echo)."""
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(f"{context} is not a valid ISO date") from exc
    if parsed.isoformat() != text:
        raise ContractError(f"{context} is not in canonical YYYY-MM-DD form")
    return parsed


# --- identifier grammars -------------------------------------------------------------

RUN_ID_PATTERN = re.compile(r"\A(\d{8}T\d{6}Z)-([0-9a-f]{24})\Z")
FACT_ID_PATTERN = re.compile(
    r"\A[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+"
    r"(?:@[a-z0-9]+(?:-[a-z0-9]+)*)?\Z"
)
PERIOD_SLUG_PATTERN = re.compile(r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z")
GRAPHIC_KEY_PATTERN = re.compile(r"\A[a-z][a-z0-9_]*\Z")
MODULE_KEY_PATTERN = re.compile(r"\A[a-z][a-z0-9_]*\Z")
CALCULATION_ID_PATTERN = re.compile(r"\A[a-z][a-z0-9_.]*@v\d+\Z")
SHA256_HEX_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
DATASET_ID_PATTERN = re.compile(r"\A[a-z][a-z0-9_]*-[0-9a-f]{12}\Z")
TASK_ID_PATTERN = re.compile(r"\Atask-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{12}\Z")


def new_run_id(now: datetime) -> str:
    """Mint ``<YYYYMMDDTHHMMSSZ>-<24 lowercase hex>`` (UTC + 96 random bits)."""
    if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(None):
        raise ContractError("run IDs are minted from an aware UTC datetime")
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(12)}"


def validate_run_id(run_id: str, context: str = "run_id") -> str:
    """Enforce the closed run-ID grammar, including a real UTC timestamp component."""
    match = RUN_ID_PATTERN.match(run_id)
    if match is None:
        raise ContractError(f"{context} does not match the run-ID grammar: {run_id!r}")
    try:
        datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise ContractError(f"{context} timestamp component is invalid: {run_id!r}") from exc
    return run_id


def resolve_run_dir(run_root: Path, run_id: str, context: str = "run") -> Path:
    """Resolve a run ID (never a path) to an immediate child of ``run_root``.

    The value must match the run-ID grammar, name an immediate child of the run root,
    resolve beneath that root, and traverse no symlink/reparse-point component.
    """
    validate_run_id(run_id, context)
    candidate = run_root / run_id
    if candidate.is_symlink() or os.path.isjunction(str(candidate)):
        raise ContractError(f"{context} directory is a symlink/reparse point: {run_id!r}")
    root_resolved = run_root.resolve()
    resolved = candidate.resolve()
    if resolved.name != run_id or resolved.parent != root_resolved:
        raise ContractError(f"{context} does not resolve beneath the run root: {run_id!r}")
    return candidate


def content_slug(key: str) -> str:
    """Readable slug from a CODE-OWNED key (never private text).

    Lowercases, maps underscores/non-alphanumerics to one hyphen, trims hyphens, and
    truncates to 20 characters.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:20].strip("-")
    if not slug:
        raise ContractError(f"key produces an empty slug: {key!r}")
    return slug


def task_id_for(record: Mapping[str, object], module_key: str) -> str:
    """``task-<module-slug>-<12 hex>``; the suffix hashes the record without its ID."""
    return f"task-{content_slug(module_key)}-{content_suffix(record, omit=('task_id',))}"


def ensure_unique(values: Iterable[str], kind: str) -> None:
    """Namespace-collision check for generated identifiers."""
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ContractError(f"duplicate {kind}: {value!r}")
        seen.add(value)


def validate_fact_id(fact_id: str, context: str = "fact_id") -> str:
    """Enforce the dotted snake-case fact-ID grammar (+ optional ``@period-slug``).

    The offending value is deliberately NOT interpolated into the error: this
    validator sees raw private-input text (briefing cells, override files), which
    must never be echoed (plan section 5.2).
    """
    if FACT_ID_PATTERN.match(fact_id) is None:
        raise ContractError(f"{context} does not match the fact-ID grammar")
    return fact_id


def validate_sha256(value: str, context: str = "digest") -> str:
    """Exactly 64 lowercase hexadecimal SHA-256 characters (offending value not echoed)."""
    if SHA256_HEX_PATTERN.match(value) is None:
        raise ContractError(f"{context} is not a lowercase hex SHA-256")
    return value


# --- closed vocabularies -------------------------------------------------------------

AUDIENCE_PUBLIC = "public_aggregate"
AUDIENCE_INTERNAL = "internal"
AUDIENCES: tuple[str, ...] = (AUDIENCE_PUBLIC, AUDIENCE_INTERNAL)

BASES: tuple[str, ...] = (
    "cash",
    "reserve",
    "allocated",
    "committed",
    "spent",
    "received",
    "pending",
    "projected",
    "definition",
    "calculated",
)

ORIGINS: tuple[str, ...] = ("observed", "operator_supplied", "derived", "projected")

FACT_STATUSES: tuple[str, ...] = (
    "available",
    "missing",
    "conflicting",
    "stale",
    "not_applicable",
)

SOURCE_ALIAS_BUDGET_TIMESERIES = "budget_timeseries"
SOURCE_ALIAS_BRIEFING = "treasurer_briefing_inputs"
SOURCE_ALIAS_REIMBURSEMENT = "reimbursement_bundle"
SOURCE_ALIAS_OVERRIDE = "run_override"
SOURCE_ALIASES: tuple[str, ...] = (
    SOURCE_ALIAS_BUDGET_TIMESERIES,
    SOURCE_ALIAS_BRIEFING,
    SOURCE_ALIAS_REIMBURSEMENT,
    SOURCE_ALIAS_OVERRIDE,
)

UNIT_TEXT = "text"
UNIT_BOOLEAN = "boolean"
UNIT_COUNT = "count"
UNIT_PERCENT = "percent"
UNIT_DATE = "date"
CURRENCY_UNIT_PREFIX = "currency:"
_CURRENCY_CODE_PATTERN = re.compile(r"\A[A-Z]{3}\Z")

IGNORE_REASONS: tuple[str, ...] = (
    "invalid_unicode",
    "export_filename",
    "transcript_metadata",
    "repeated_heading",
    "ambiguous_prose",
)
CONFIDENCE_LEVELS: tuple[str, ...] = ("high", "medium", "low")

DATASET_COLUMN_KINDS: tuple[str, ...] = (
    "string",
    "integer",
    "decimal",
    "money",
    "percent",
    "date",
    "period",
)
_NUMERIC_COLUMN_KINDS = frozenset({"integer", "decimal", "money", "percent"})

FactValue = str | bool | int | Decimal | date | None


def validate_unit(unit: str, context: str = "unit") -> str:
    """One code-owned unit: text/boolean/count/percent/date/``currency:<ISO-4217>``.

    The offending value is deliberately NOT interpolated into the error: a misaligned
    private-input row can put arbitrary private text into the unit column.
    """
    if unit in (UNIT_TEXT, UNIT_BOOLEAN, UNIT_COUNT, UNIT_PERCENT, UNIT_DATE):
        return unit
    if unit.startswith(CURRENCY_UNIT_PREFIX):
        code = unit[len(CURRENCY_UNIT_PREFIX) :]
        if _CURRENCY_CODE_PATTERN.match(code) is not None:
            return unit
    raise ContractError(f"{context} is not a supported unit")


def validate_fact_value(value: FactValue, unit: str, context: str = "value") -> FactValue:
    """Type-check an *available* fact value against its unit (money is finite Decimal)."""
    validate_unit(unit, context)
    if unit == UNIT_TEXT:
        if not isinstance(value, str) or not value:
            raise ContractError(f"{context}: text unit requires a non-empty string")
    elif unit == UNIT_BOOLEAN:
        if not isinstance(value, bool):
            raise ContractError(f"{context}: boolean unit requires a bool")
    elif unit == UNIT_COUNT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError(f"{context}: count unit requires an integer")
    elif unit == UNIT_DATE:
        if not isinstance(value, date):
            raise ContractError(f"{context}: date unit requires an ISO date")
    elif unit == UNIT_PERCENT:
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ContractError(f"{context}: percent unit requires a finite Decimal")
    else:  # currency:<code>
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ContractError(f"{context}: money requires a finite Decimal")
    return value


def combine_audience(*levels: str) -> str:
    """Monotonic audience lattice: combining inputs takes the most restrictive value."""
    if not levels:
        raise ContractError("combine_audience needs at least one level")
    for level in levels:
        if level not in AUDIENCES:
            raise ContractError(f"unknown audience level: {level!r}")
    if AUDIENCE_INTERNAL in levels:
        return AUDIENCE_INTERNAL
    return AUDIENCE_PUBLIC


def restrict_audience(ceiling: str, requested: str | None) -> str:
    """Apply an optional input restriction: it may only restrict, never promote.

    Blank or unrecognized requested values become internal (per section 5.3).
    """
    if ceiling not in AUDIENCES:
        raise ContractError(f"unknown audience ceiling: {ceiling!r}")
    if requested is None or requested == "":
        return combine_audience(ceiling, AUDIENCE_INTERNAL)
    if requested not in AUDIENCES:
        return combine_audience(ceiling, AUDIENCE_INTERNAL)
    return combine_audience(ceiling, requested)


# --- strict-parse helpers ------------------------------------------------------------


def require_keys(
    value: Mapping[str, Any],
    *,
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
    context: str,
) -> None:
    """Closed keyset: unknown root/entry fields and missing required fields reject."""
    keys = set(value)
    missing = [key for key in required if key not in keys]
    unknown = sorted(keys - set(required) - set(optional))
    if missing:
        raise ContractError(f"{context}: missing field(s) {missing}")
    if unknown:
        raise ContractError(f"{context}: unknown field(s) {unknown}")


def as_str(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{context} must be a string")
    return value


def as_opt_str(value: object, context: str) -> str | None:
    if value is None:
        return None
    return as_str(value, context)


def as_bool(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{context} must be a boolean")
    return value


def as_int(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{context} must be an integer")
    return value


def as_mapping(value: object, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{context} must be an object")
    return value


def as_sequence(value: object, context: str) -> Sequence[Any]:
    if isinstance(value, str | bytes) or not isinstance(value, Sequence):
        raise ContractError(f"{context} must be an array")
    return value


def as_decimal_text(value: object, context: str) -> Decimal:
    """A canonical plain-decimal JSON string -> finite Decimal (round-trip enforced)."""
    text = as_str(value, context)
    try:
        parsed = Decimal(text)
    except ArithmeticError as exc:
        raise ContractError(f"{context} is not a decimal") from exc
    if not parsed.is_finite():
        raise ContractError(f"{context} must be finite")
    if decimal_to_plain(parsed) != text:
        raise ContractError(f"{context} is not in canonical plain form")
    return parsed


def check_schema_version(value: Mapping[str, Any], context: str) -> None:
    """Every v1 run artifact/record carries the integer ``schema_version`` 1."""
    version = value.get("schema_version")
    if version is not SCHEMA_VERSION and version != SCHEMA_VERSION:
        raise ContractError(f"{context}: unsupported schema_version {version!r}")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ContractError(f"{context}: schema_version must be the integer 1")


# --- source spans --------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpan:
    """Half-open code-point span into the exact decoded request text.

    Code-point offsets are zero-based; line/column coordinates are one-based and point
    at the same bounds. ``fragment_sha256`` covers the exact UTF-8 substring, binding
    the span to the reviewed input.
    """

    start_codepoint: int
    end_codepoint: int
    start_line: int
    start_column: int
    end_line: int
    end_column: int
    fragment_sha256: str

    def __post_init__(self) -> None:
        if self.start_codepoint < 0 or self.end_codepoint <= self.start_codepoint:
            raise ContractError("source span must be non-empty and non-negative")
        if min(self.start_line, self.start_column, self.end_line, self.end_column) < 1:
            raise ContractError("source-span line/column coordinates are one-based")
        if (self.end_line, self.end_column) <= (self.start_line, self.start_column):
            raise ContractError("source-span end coordinates must follow the start")
        validate_sha256(self.fragment_sha256, "fragment_sha256")

    def to_json(self) -> dict[str, object]:
        return {
            "start_codepoint": self.start_codepoint,
            "end_codepoint": self.end_codepoint,
            "start_line": self.start_line,
            "start_column": self.start_column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "fragment_sha256": self.fragment_sha256,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "source_span") -> SourceSpan:
        require_keys(
            value,
            required=(
                "start_codepoint",
                "end_codepoint",
                "start_line",
                "start_column",
                "end_line",
                "end_column",
                "fragment_sha256",
            ),
            context=context,
        )
        return cls(
            start_codepoint=as_int(value["start_codepoint"], f"{context}.start_codepoint"),
            end_codepoint=as_int(value["end_codepoint"], f"{context}.end_codepoint"),
            start_line=as_int(value["start_line"], f"{context}.start_line"),
            start_column=as_int(value["start_column"], f"{context}.start_column"),
            end_line=as_int(value["end_line"], f"{context}.end_line"),
            end_column=as_int(value["end_column"], f"{context}.end_column"),
            fragment_sha256=as_str(value["fragment_sha256"], f"{context}.fragment_sha256"),
        )


def validate_span_against_text(span: SourceSpan, text: str, context: str = "span") -> None:
    """The span must lie inside ``text`` and its fragment digest must match exactly."""
    if span.end_codepoint > len(text):
        raise ContractError(f"{context} exceeds the request length")
    fragment = text[span.start_codepoint : span.end_codepoint]
    if sha256_hex(fragment.encode("utf-8")) != span.fragment_sha256:
        raise ContractError(f"{context} fragment digest does not match the request text")


def validate_spans_disjoint(spans: Sequence[SourceSpan], context: str = "spans") -> None:
    """Spans across one brief must be non-overlapping."""
    ordered = sorted(spans, key=lambda span: span.start_codepoint)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if later.start_codepoint < earlier.end_codepoint:
            raise ContractError(f"{context} overlap at codepoint {later.start_codepoint}")


# --- brief draft ---------------------------------------------------------------------


@dataclass(frozen=True)
class BriefTask:
    """One proposed finance question with provenance back into the request."""

    task_id: str
    source_spans: tuple[SourceSpan, ...]
    module_key: str
    question: str
    required: bool

    def __post_init__(self) -> None:
        if TASK_ID_PATTERN.match(self.task_id) is None:
            raise ContractError("task_id does not match its grammar")
        if MODULE_KEY_PATTERN.match(self.module_key) is None:
            raise ContractError("module_key is not snake-case")
        if not self.source_spans:
            raise ContractError("a task needs at least one source span")
        if not self.question:
            raise ContractError("a task question must be non-empty")

    def to_json(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "source_spans": [span.to_json() for span in self.source_spans],
            "module_key": self.module_key,
            "question": self.question,
            "required": self.required,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "task") -> BriefTask:
        require_keys(
            value,
            required=("task_id", "source_spans", "module_key", "question", "required"),
            context=context,
        )
        return cls(
            task_id=as_str(value["task_id"], f"{context}.task_id"),
            source_spans=tuple(
                SourceSpan.from_json(as_mapping(item, f"{context}.source_spans[]"))
                for item in as_sequence(value["source_spans"], f"{context}.source_spans")
            ),
            module_key=as_str(value["module_key"], f"{context}.module_key"),
            question=as_str(value["question"], f"{context}.question"),
            required=as_bool(value["required"], f"{context}.required"),
        )


@dataclass(frozen=True)
class WorkflowGuidance:
    """Conversational process guidance separated from task-like questions."""

    text: str
    source_spans: tuple[SourceSpan, ...]

    def __post_init__(self) -> None:
        if not self.text:
            raise ContractError("workflow guidance text must be non-empty")
        if not self.source_spans:
            raise ContractError("workflow guidance needs at least one source span")

    def to_json(self) -> dict[str, object]:
        return {
            "text": self.text,
            "source_spans": [span.to_json() for span in self.source_spans],
        }

    @classmethod
    def from_json(
        cls, value: Mapping[str, Any], context: str = "workflow_guidance"
    ) -> WorkflowGuidance:
        require_keys(value, required=("text", "source_spans"), context=context)
        return cls(
            text=as_str(value["text"], f"{context}.text"),
            source_spans=tuple(
                SourceSpan.from_json(as_mapping(item, f"{context}.source_spans[]"))
                for item in as_sequence(value["source_spans"], f"{context}.source_spans")
            ),
        )


@dataclass(frozen=True)
class IgnoredFragment:
    """One discarded request fragment: ``{source_span, reason, confidence}``."""

    source_span: SourceSpan
    reason: str
    confidence: str

    def __post_init__(self) -> None:
        if self.reason not in IGNORE_REASONS:
            raise ContractError("unknown ignore reason")
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ContractError("unknown confidence level")

    def to_json(self) -> dict[str, object]:
        return {
            "source_span": self.source_span.to_json(),
            "reason": self.reason,
            "confidence": self.confidence,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "ignored") -> IgnoredFragment:
        require_keys(value, required=("source_span", "reason", "confidence"), context=context)
        return cls(
            source_span=SourceSpan.from_json(
                as_mapping(value["source_span"], f"{context}.source_span")
            ),
            reason=as_str(value["reason"], f"{context}.reason"),
            confidence=as_str(value["confidence"], f"{context}.confidence"),
        )


@dataclass(frozen=True)
class BriefDraft:
    """``brief.draft.json``: the reviewable cleanup of one private request."""

    run_id: str
    request_sha256: str
    tasks: tuple[BriefTask, ...]
    workflow_guidance: tuple[WorkflowGuidance, ...]
    ignored: tuple[IgnoredFragment, ...]

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        validate_sha256(self.request_sha256, "request_sha256")
        ensure_unique((task.task_id for task in self.tasks), "task_id")

    def all_spans(self) -> tuple[SourceSpan, ...]:
        spans: list[SourceSpan] = []
        for task in self.tasks:
            spans.extend(task.source_spans)
        for guidance in self.workflow_guidance:
            spans.extend(guidance.source_spans)
        for fragment in self.ignored:
            spans.append(fragment.source_span)
        return tuple(spans)

    def validate_against_request(self, request_text: str) -> None:
        """Every span must be in-bounds, digest-bound, and non-overlapping."""
        spans = self.all_spans()
        for span in spans:
            validate_span_against_text(span, request_text)
        validate_spans_disjoint(spans, "brief spans")

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "request_sha256": self.request_sha256,
            "tasks": [task.to_json() for task in self.tasks],
            "workflow_guidance": [item.to_json() for item in self.workflow_guidance],
            "ignored": [fragment.to_json() for fragment in self.ignored],
        }

    def digest(self) -> str:
        return json_sha256(self.to_json())

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "brief.draft") -> BriefDraft:
        check_schema_version(value, context)
        require_keys(
            value,
            required=(
                "schema_version",
                "run_id",
                "request_sha256",
                "tasks",
                "workflow_guidance",
                "ignored",
            ),
            context=context,
        )
        return cls(
            run_id=as_str(value["run_id"], f"{context}.run_id"),
            request_sha256=as_str(value["request_sha256"], f"{context}.request_sha256"),
            tasks=tuple(
                BriefTask.from_json(as_mapping(item, f"{context}.tasks[]"))
                for item in as_sequence(value["tasks"], f"{context}.tasks")
            ),
            workflow_guidance=tuple(
                WorkflowGuidance.from_json(as_mapping(item, f"{context}.workflow_guidance[]"))
                for item in as_sequence(value["workflow_guidance"], f"{context}.workflow_guidance")
            ),
            ignored=tuple(
                IgnoredFragment.from_json(as_mapping(item, f"{context}.ignored[]"))
                for item in as_sequence(value["ignored"], f"{context}.ignored")
            ),
        )


# --- run choices ---------------------------------------------------------------------


@dataclass(frozen=True)
class RunChoices:
    """Explicit run choices recorded beside the ignored fragments."""

    audience: str
    skip_overview: bool
    excluded_modules: tuple[str, ...]
    requested_graphics: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.audience not in AUDIENCES:
            raise ContractError("unknown run-choice audience")
        for module_key in self.excluded_modules:
            if MODULE_KEY_PATTERN.match(module_key) is None:
                raise ContractError("excluded module is not snake-case")
        for graphic_key in self.requested_graphics:
            if GRAPHIC_KEY_PATTERN.match(graphic_key) is None:
                raise ContractError("requested graphic is not snake-case")

    def to_json(self) -> dict[str, object]:
        return {
            "audience": self.audience,
            "skip_overview": self.skip_overview,
            "excluded_modules": list(self.excluded_modules),
            "requested_graphics": list(self.requested_graphics),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "choices") -> RunChoices:
        require_keys(
            value,
            required=("audience", "skip_overview", "excluded_modules", "requested_graphics"),
            context=context,
        )
        return cls(
            audience=as_str(value["audience"], f"{context}.audience"),
            skip_overview=as_bool(value["skip_overview"], f"{context}.skip_overview"),
            excluded_modules=tuple(
                as_str(item, f"{context}.excluded_modules[]")
                for item in as_sequence(value["excluded_modules"], f"{context}.excluded_modules")
            ),
            requested_graphics=tuple(
                as_str(item, f"{context}.requested_graphics[]")
                for item in as_sequence(
                    value["requested_graphics"], f"{context}.requested_graphics"
                )
            ),
        )


def ignored_choices_document(
    *, run_id: str, request_sha256: str, ignored: Sequence[IgnoredFragment], choices: RunChoices
) -> dict[str, object]:
    """Build the ``ignored-choices.json`` root object."""
    validate_run_id(run_id)
    validate_sha256(request_sha256, "request_sha256")
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "request_sha256": request_sha256,
        "ignored": [fragment.to_json() for fragment in ignored],
        "choices": choices.to_json(),
    }


# --- fact records --------------------------------------------------------------------


def _encode_fact_value(value: FactValue) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        # Canonical JSON serializes Decimals as exactly this plain string, so encoding
        # it eagerly changes no digest while keeping to_json/from_json symmetric.
        return decimal_to_plain(value)
    return value


def _decode_fact_value(raw: object, unit: str, context: str) -> FactValue:
    if raw is None:
        return None
    if unit == UNIT_DATE:
        return parse_iso_date(as_str(raw, context), context)
    if unit in (UNIT_PERCENT,) or unit.startswith(CURRENCY_UNIT_PREFIX):
        return as_decimal_text(raw, context)
    if unit == UNIT_COUNT:
        return as_int(raw, context)
    if unit == UNIT_BOOLEAN:
        return as_bool(raw, context)
    return as_str(raw, context)


@dataclass(frozen=True)
class FactRecord:
    """One typed, provenance-bearing fact (plan section 5.3).

    An *available* fact carries exactly one typed value coherent with its unit plus its
    full private provenance (source alias, locator, revision/hash, capture timestamp).
    Missing/not-applicable facts keep ``value=None`` but retain the expected unit.
    Derived facts retain their input fact IDs and a versioned calculation identifier.
    """

    fact_id: str
    label: str
    value: FactValue
    unit: str
    basis: str
    origin: str
    audience: str
    status: str
    period: str | None = None
    as_of_date: date | None = None
    source_alias: str | None = None
    locator: str | None = None
    source_revision: str | None = None
    source_hash: str | None = None
    captured_at: datetime | None = None
    note: str | None = None
    input_fact_ids: tuple[str, ...] = ()
    calculation_id: str | None = None

    def __post_init__(self) -> None:
        validate_fact_id(self.fact_id)
        if not self.label:
            raise ContractError(f"{self.fact_id}: label must be non-empty")
        validate_unit(self.unit, f"{self.fact_id}.unit")
        if self.basis not in BASES:
            raise ContractError(f"{self.fact_id}: unknown basis")
        if self.origin not in ORIGINS:
            raise ContractError(f"{self.fact_id}: unknown origin")
        if self.audience not in AUDIENCES:
            raise ContractError(f"{self.fact_id}: unknown audience")
        if self.status not in FACT_STATUSES:
            raise ContractError(f"{self.fact_id}: unknown status")
        if self.period is not None and PERIOD_SLUG_PATTERN.match(self.period) is None:
            raise ContractError(f"{self.fact_id}: period is not a period slug")
        if self.source_alias is not None and self.source_alias not in SOURCE_ALIASES:
            raise ContractError(f"{self.fact_id}: unknown source alias")
        if self.source_hash is not None:
            validate_sha256(self.source_hash, f"{self.fact_id}.source_hash")
        for input_id in self.input_fact_ids:
            validate_fact_id(input_id, f"{self.fact_id}.input_fact_ids[]")
        if self.origin == "derived":
            if not self.input_fact_ids or self.calculation_id is None:
                raise ContractError(
                    f"{self.fact_id}: derived facts retain input fact IDs and a "
                    "versioned calculation identifier"
                )
        if self.calculation_id is not None and (
            CALCULATION_ID_PATTERN.match(self.calculation_id) is None
        ):
            raise ContractError(f"{self.fact_id}: calculation_id is not versioned")
        if self.status == "available":
            validate_fact_value(self.value, self.unit, f"{self.fact_id}.value")
            if self.period is None and self.as_of_date is None:
                raise ContractError(
                    f"{self.fact_id}: an available fact needs a period and/or as_of_date"
                )
            if self.source_alias is None or self.source_hash is None or self.captured_at is None:
                raise ContractError(
                    f"{self.fact_id}: an available fact carries source alias, hash, and "
                    "capture timestamp"
                )
        else:
            if self.value is not None:
                raise ContractError(f"{self.fact_id}: {self.status} facts carry value=null")

    def to_json(self) -> dict[str, object]:
        return {
            "fact_id": self.fact_id,
            "label": self.label,
            "value": _encode_fact_value(self.value),
            "unit": self.unit,
            "basis": self.basis,
            "origin": self.origin,
            "audience": self.audience,
            "status": self.status,
            "period": self.period,
            "as_of_date": None if self.as_of_date is None else self.as_of_date.isoformat(),
            "source_alias": self.source_alias,
            "locator": self.locator,
            "source_revision": self.source_revision,
            "source_hash": self.source_hash,
            "captured_at": None if self.captured_at is None else format_utc(self.captured_at),
            "note": self.note,
            "input_fact_ids": list(self.input_fact_ids),
            "calculation_id": self.calculation_id,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "fact") -> FactRecord:
        require_keys(
            value,
            required=(
                "fact_id",
                "label",
                "value",
                "unit",
                "basis",
                "origin",
                "audience",
                "status",
                "period",
                "as_of_date",
                "source_alias",
                "locator",
                "source_revision",
                "source_hash",
                "captured_at",
                "note",
                "input_fact_ids",
                "calculation_id",
            ),
            context=context,
        )
        unit = as_str(value["unit"], f"{context}.unit")
        as_of_raw = as_opt_str(value["as_of_date"], f"{context}.as_of_date")
        captured_raw = as_opt_str(value["captured_at"], f"{context}.captured_at")
        return cls(
            fact_id=as_str(value["fact_id"], f"{context}.fact_id"),
            label=as_str(value["label"], f"{context}.label"),
            value=_decode_fact_value(value["value"], unit, f"{context}.value"),
            unit=unit,
            basis=as_str(value["basis"], f"{context}.basis"),
            origin=as_str(value["origin"], f"{context}.origin"),
            audience=as_str(value["audience"], f"{context}.audience"),
            status=as_str(value["status"], f"{context}.status"),
            period=as_opt_str(value["period"], f"{context}.period"),
            as_of_date=None if as_of_raw is None else parse_iso_date(as_of_raw),
            source_alias=as_opt_str(value["source_alias"], f"{context}.source_alias"),
            locator=as_opt_str(value["locator"], f"{context}.locator"),
            source_revision=as_opt_str(value["source_revision"], f"{context}.source_revision"),
            source_hash=as_opt_str(value["source_hash"], f"{context}.source_hash"),
            captured_at=None if captured_raw is None else parse_utc(captured_raw),
            note=as_opt_str(value["note"], f"{context}.note"),
            input_fact_ids=tuple(
                as_str(item, f"{context}.input_fact_ids[]")
                for item in as_sequence(value["input_fact_ids"], f"{context}.input_fact_ids")
            ),
            calculation_id=as_opt_str(value["calculation_id"], f"{context}.calculation_id"),
        )


# --- graphic datasets ----------------------------------------------------------------


@dataclass(frozen=True)
class DatasetColumn:
    """Ordered column descriptor: key, scalar kind, unit, sensitivity."""

    key: str
    kind: str
    unit: str | None
    sensitivity: str

    def __post_init__(self) -> None:
        if GRAPHIC_KEY_PATTERN.match(self.key) is None:
            raise ContractError(f"dataset column key is not snake-case: {self.key!r}")
        if self.kind not in DATASET_COLUMN_KINDS:
            raise ContractError(f"{self.key}: unknown column kind {self.kind!r}")
        if self.sensitivity not in AUDIENCES:
            raise ContractError(f"{self.key}: unknown sensitivity {self.sensitivity!r}")
        if self.kind == "money":
            if self.unit is None or not self.unit.startswith(CURRENCY_UNIT_PREFIX):
                raise ContractError(f"{self.key}: money columns require a currency unit")
            validate_unit(self.unit, f"{self.key}.unit")
        elif self.kind == "percent":
            if self.unit != UNIT_PERCENT:
                raise ContractError(f"{self.key}: percent columns require unit 'percent'")
        elif self.unit is not None:
            raise ContractError(f"{self.key}: unit must be null for kind {self.kind!r}")

    def to_json(self) -> dict[str, object]:
        return {
            "key": self.key,
            "kind": self.kind,
            "unit": self.unit,
            "sensitivity": self.sensitivity,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "column") -> DatasetColumn:
        require_keys(value, required=("key", "kind", "unit", "sensitivity"), context=context)
        return cls(
            key=as_str(value["key"], f"{context}.key"),
            kind=as_str(value["kind"], f"{context}.kind"),
            unit=as_opt_str(value["unit"], f"{context}.unit"),
            sensitivity=as_str(value["sensitivity"], f"{context}.sensitivity"),
        )


DatasetCell = str | int | Decimal | date


def _validate_dataset_cell(cell: object, column: DatasetColumn, context: str) -> DatasetCell:
    if column.kind == "string":
        if not isinstance(cell, str):
            raise ContractError(f"{context}: string column requires text")
        return cell
    if column.kind == "integer":
        if isinstance(cell, bool) or not isinstance(cell, int):
            raise ContractError(f"{context}: integer column rejects non-integers")
        return cell
    if column.kind in ("decimal", "money", "percent"):
        if not isinstance(cell, Decimal) or not cell.is_finite():
            raise ContractError(
                f"{context}: numeric column requires a finite Decimal (numeric text blocks)"
            )
        return cell
    if column.kind == "date":
        if not isinstance(cell, date):
            raise ContractError(f"{context}: date column requires an ISO date")
        return cell
    # period
    if not isinstance(cell, str) or PERIOD_SLUG_PATTERN.match(cell) is None:
        raise ContractError(f"{context}: period column requires a period slug")
    return cell


def _encode_dataset_cell(cell: DatasetCell) -> object:
    if isinstance(cell, date):
        return cell.isoformat()
    if isinstance(cell, Decimal):
        return decimal_to_plain(cell)
    return cell


def _decode_dataset_cell(raw: object, column: DatasetColumn, context: str) -> DatasetCell:
    if column.kind in ("decimal", "money", "percent"):
        return as_decimal_text(raw, context)
    if column.kind == "date":
        return parse_iso_date(as_str(raw, context), context)
    if column.kind == "integer":
        return as_int(raw, context)
    return as_str(raw, context)


@dataclass(frozen=True)
class GraphicDataset:
    """Versioned dataset — the sole producer shape later graphic specs consume.

    ``dataset_id`` is ``<graphic-key>-<12 hex>`` where the suffix is the first 12
    characters of the canonical dataset SHA-256 (ID and provenance digest omitted);
    ``provenance_sha256`` covers the complete canonical record with itself omitted.
    Rows preserve source ordering. Missing cells, numeric text, duplicate column keys,
    selector mismatch, and declared-total disagreement block the dataset.
    """

    dataset_id: str
    graphic_key: str
    columns: tuple[DatasetColumn, ...]
    rows: tuple[tuple[DatasetCell, ...], ...]
    source_fact_ids: tuple[str, ...]
    source_grid_hashes: tuple[str, ...]
    calculation_id: str
    selector: tuple[tuple[str, str], ...]
    selector_echo: tuple[tuple[str, str], ...]
    declared_totals: tuple[tuple[str, Decimal], ...]
    provenance_sha256: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if GRAPHIC_KEY_PATTERN.match(self.graphic_key) is None:
            raise ContractError(f"graphic_key is not snake-case: {self.graphic_key!r}")
        if not self.columns:
            raise ContractError(f"{self.graphic_key}: a dataset needs at least one column")
        ensure_unique((column.key for column in self.columns), "dataset column key")
        for row_index, row in enumerate(self.rows):
            if len(row) != len(self.columns):
                raise ContractError(
                    f"{self.graphic_key}: row {row_index} does not match the column count"
                )
            for column, cell in zip(self.columns, row, strict=True):
                _validate_dataset_cell(
                    cell, column, f"{self.graphic_key}.rows[{row_index}].{column.key}"
                )
        for fact_id in self.source_fact_ids:
            validate_fact_id(fact_id, f"{self.graphic_key}.source_fact_ids[]")
        for grid_hash in self.source_grid_hashes:
            validate_sha256(grid_hash, f"{self.graphic_key}.source_grid_hashes[]")
        if CALCULATION_ID_PATTERN.match(self.calculation_id) is None:
            raise ContractError(f"{self.graphic_key}: calculation_id is not versioned")
        if dict(self.selector) != dict(self.selector_echo):
            raise ContractError(f"{self.graphic_key}: selector state does not match its echo")
        column_by_key = {column.key: column for column in self.columns}
        for total_key, declared in self.declared_totals:
            total_column = column_by_key.get(total_key)
            if total_column is None or total_column.kind not in _NUMERIC_COLUMN_KINDS:
                raise ContractError(
                    f"{self.graphic_key}: declared total names a non-numeric column {total_key!r}"
                )
            index = next(
                position
                for position, candidate in enumerate(self.columns)
                if candidate.key == total_key
            )
            actual = sum(
                (Decimal(cell) if isinstance(cell, int) else cell)
                for row in self.rows
                for cell in (row[index],)
                if isinstance(cell, int | Decimal)
            )
            if Decimal(actual) != declared:
                raise ContractError(
                    f"{self.graphic_key}: declared total for {total_key!r} disagrees "
                    f"with the rows ({declared} != {actual})"
                )
        expected_id = f"{self.graphic_key}-{content_suffix(self._body(), omit=())}"
        if self.dataset_id != expected_id:
            raise ContractError(
                f"{self.graphic_key}: dataset_id {self.dataset_id!r} does not match the "
                "canonical content hash"
            )
        expected_provenance = digest_of(self.to_json(), omit=("provenance_sha256",))
        if self.provenance_sha256 != expected_provenance:
            raise ContractError(f"{self.graphic_key}: provenance hash does not verify")

    def _body(self) -> dict[str, object]:
        """The canonical record with ID and digest fields omitted (content-derived ID)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "graphic_key": self.graphic_key,
            "columns": [column.to_json() for column in self.columns],
            "rows": [[_encode_dataset_cell(cell) for cell in row] for row in self.rows],
            "source_fact_ids": list(self.source_fact_ids),
            "source_grid_hashes": list(self.source_grid_hashes),
            "calculation_id": self.calculation_id,
            "selector": {key: item for key, item in self.selector},
            "selector_echo": {key: item for key, item in self.selector_echo},
            "declared_totals": {
                key: decimal_to_plain(total) for key, total in self.declared_totals
            },
        }

    @classmethod
    def create(
        cls,
        *,
        graphic_key: str,
        columns: Sequence[DatasetColumn],
        rows: Sequence[Sequence[DatasetCell]],
        source_fact_ids: Sequence[str],
        source_grid_hashes: Sequence[str],
        calculation_id: str,
        selector: Mapping[str, str],
        selector_echo: Mapping[str, str],
        declared_totals: Mapping[str, Decimal],
    ) -> GraphicDataset:
        """Build a dataset, deriving ``dataset_id`` and the provenance hash."""
        provisional = {
            "schema_version": SCHEMA_VERSION,
            "graphic_key": graphic_key,
            "columns": [column.to_json() for column in columns],
            "rows": [[_encode_dataset_cell(cell) for cell in row] for row in rows],
            "source_fact_ids": list(source_fact_ids),
            "source_grid_hashes": list(source_grid_hashes),
            "calculation_id": calculation_id,
            "selector": dict(selector),
            "selector_echo": dict(selector_echo),
            "declared_totals": dict(declared_totals),
        }
        dataset_id = f"{graphic_key}-{content_suffix(provisional, omit=())}"
        body = dict(provisional)
        body["dataset_id"] = dataset_id
        provenance = digest_of(body, omit=("provenance_sha256",))
        return cls(
            dataset_id=dataset_id,
            graphic_key=graphic_key,
            columns=tuple(columns),
            rows=tuple(tuple(row) for row in rows),
            source_fact_ids=tuple(source_fact_ids),
            source_grid_hashes=tuple(source_grid_hashes),
            calculation_id=calculation_id,
            selector=tuple(sorted(selector.items())),
            selector_echo=tuple(sorted(selector_echo.items())),
            declared_totals=tuple(sorted(declared_totals.items())),
            provenance_sha256=provenance,
        )

    def to_json(self) -> dict[str, object]:
        body = self._body()
        body["dataset_id"] = self.dataset_id
        return body

    def to_json_with_provenance(self) -> dict[str, object]:
        body = self.to_json()
        body["provenance_sha256"] = self.provenance_sha256
        return body

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "dataset") -> GraphicDataset:
        check_schema_version(value, context)
        require_keys(
            value,
            required=(
                "schema_version",
                "dataset_id",
                "graphic_key",
                "columns",
                "rows",
                "source_fact_ids",
                "source_grid_hashes",
                "calculation_id",
                "selector",
                "selector_echo",
                "declared_totals",
                "provenance_sha256",
            ),
            context=context,
        )
        columns = tuple(
            DatasetColumn.from_json(as_mapping(item, f"{context}.columns[]"))
            for item in as_sequence(value["columns"], f"{context}.columns")
        )
        rows: list[tuple[DatasetCell, ...]] = []
        for row_index, raw_row in enumerate(as_sequence(value["rows"], f"{context}.rows")):
            cells = as_sequence(raw_row, f"{context}.rows[{row_index}]")
            if len(cells) != len(columns):
                raise ContractError(f"{context}.rows[{row_index}]: column count mismatch")
            rows.append(
                tuple(
                    _decode_dataset_cell(
                        raw_cell, column, f"{context}.rows[{row_index}].{column.key}"
                    )
                    for column, raw_cell in zip(columns, cells, strict=True)
                )
            )
        selector_map = as_mapping(value["selector"], f"{context}.selector")
        echo_map = as_mapping(value["selector_echo"], f"{context}.selector_echo")
        totals_map = as_mapping(value["declared_totals"], f"{context}.declared_totals")
        return cls(
            dataset_id=as_str(value["dataset_id"], f"{context}.dataset_id"),
            graphic_key=as_str(value["graphic_key"], f"{context}.graphic_key"),
            columns=columns,
            rows=tuple(rows),
            source_fact_ids=tuple(
                as_str(item, f"{context}.source_fact_ids[]")
                for item in as_sequence(value["source_fact_ids"], f"{context}.source_fact_ids")
            ),
            source_grid_hashes=tuple(
                as_str(item, f"{context}.source_grid_hashes[]")
                for item in as_sequence(
                    value["source_grid_hashes"], f"{context}.source_grid_hashes"
                )
            ),
            calculation_id=as_str(value["calculation_id"], f"{context}.calculation_id"),
            selector=tuple(
                sorted(
                    (key, as_str(item, f"{context}.selector[{key}]"))
                    for key, item in selector_map.items()
                )
            ),
            selector_echo=tuple(
                sorted(
                    (key, as_str(item, f"{context}.selector_echo[{key}]"))
                    for key, item in echo_map.items()
                )
            ),
            declared_totals=tuple(
                sorted(
                    (key, as_decimal_text(item, f"{context}.declared_totals[{key}]"))
                    for key, item in totals_map.items()
                )
            ),
            provenance_sha256=as_str(value["provenance_sha256"], f"{context}.provenance_sha256"),
        )


# --- source snapshots, conflicts, missing items --------------------------------------


@dataclass(frozen=True)
class SourceSnapshot:
    """Private provenance for one captured source (alias, locator, revision, hash)."""

    source_alias: str
    captured_at: datetime
    contract_version: int
    locator: str
    source_revision: str | None
    content_sha256: str
    captured_ranges: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.source_alias not in SOURCE_ALIASES:
            raise ContractError("unknown source alias")
        if self.contract_version < 1:
            raise ContractError("source contract_version must be >= 1")
        if not self.locator:
            raise ContractError(f"{self.source_alias}: locator must be non-empty")
        validate_sha256(self.content_sha256, f"{self.source_alias}.content_sha256")

    def to_json(self) -> dict[str, object]:
        return {
            "source_alias": self.source_alias,
            "captured_at": format_utc(self.captured_at),
            "contract_version": self.contract_version,
            "locator": self.locator,
            "source_revision": self.source_revision,
            "content_sha256": self.content_sha256,
            "captured_ranges": list(self.captured_ranges),
        }

    @classmethod
    def from_json(
        cls, value: Mapping[str, Any], context: str = "source_snapshot"
    ) -> SourceSnapshot:
        require_keys(
            value,
            required=(
                "source_alias",
                "captured_at",
                "contract_version",
                "locator",
                "source_revision",
                "content_sha256",
                "captured_ranges",
            ),
            context=context,
        )
        return cls(
            source_alias=as_str(value["source_alias"], f"{context}.source_alias"),
            captured_at=parse_utc(as_str(value["captured_at"], f"{context}.captured_at")),
            contract_version=as_int(value["contract_version"], f"{context}.contract_version"),
            locator=as_str(value["locator"], f"{context}.locator"),
            source_revision=as_opt_str(value["source_revision"], f"{context}.source_revision"),
            content_sha256=as_str(value["content_sha256"], f"{context}.content_sha256"),
            captured_ranges=tuple(
                as_str(item, f"{context}.captured_ranges[]")
                for item in as_sequence(value["captured_ranges"], f"{context}.captured_ranges")
            ),
        )


@dataclass(frozen=True)
class ConflictCandidate:
    """One disagreeing source candidate inside a conflict record."""

    source_alias: str
    value: FactValue
    unit: str
    source_hash: str | None

    def __post_init__(self) -> None:
        if self.source_alias not in SOURCE_ALIASES:
            raise ContractError("unknown source alias")
        validate_unit(self.unit)
        if self.source_hash is not None:
            validate_sha256(self.source_hash)

    def to_json(self) -> dict[str, object]:
        return {
            "source_alias": self.source_alias,
            "value": _encode_fact_value(self.value),
            "unit": self.unit,
            "source_hash": self.source_hash,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "candidate") -> ConflictCandidate:
        require_keys(
            value, required=("source_alias", "value", "unit", "source_hash"), context=context
        )
        unit = as_str(value["unit"], f"{context}.unit")
        return cls(
            source_alias=as_str(value["source_alias"], f"{context}.source_alias"),
            value=_decode_fact_value(value["value"], unit, f"{context}.value"),
            unit=unit,
            source_hash=as_opt_str(value["source_hash"], f"{context}.source_hash"),
        )


@dataclass(frozen=True)
class ConflictRecord:
    """Two sources disagree and no override explicitly resolves the conflict."""

    fact_id: str
    module_keys: tuple[str, ...]
    candidates: tuple[ConflictCandidate, ...]
    blocking: bool

    def __post_init__(self) -> None:
        validate_fact_id(self.fact_id)
        if len(self.candidates) < 2:
            raise ContractError(f"{self.fact_id}: a conflict needs at least two candidates")

    def to_json(self) -> dict[str, object]:
        return {
            "fact_id": self.fact_id,
            "module_keys": list(self.module_keys),
            "candidates": [candidate.to_json() for candidate in self.candidates],
            "blocking": self.blocking,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "conflict") -> ConflictRecord:
        require_keys(
            value,
            required=("fact_id", "module_keys", "candidates", "blocking"),
            context=context,
        )
        return cls(
            fact_id=as_str(value["fact_id"], f"{context}.fact_id"),
            module_keys=tuple(
                as_str(item, f"{context}.module_keys[]")
                for item in as_sequence(value["module_keys"], f"{context}.module_keys")
            ),
            candidates=tuple(
                ConflictCandidate.from_json(as_mapping(item, f"{context}.candidates[]"))
                for item in as_sequence(value["candidates"], f"{context}.candidates")
            ),
            blocking=as_bool(value["blocking"], f"{context}.blocking"),
        )


@dataclass(frozen=True)
class MissingFact:
    """A required or optional fact no source could supply (with its absence reason)."""

    fact_id: str
    module_keys: tuple[str, ...]
    absence_reason: str
    blocking: bool

    def __post_init__(self) -> None:
        validate_fact_id(self.fact_id)
        if not self.absence_reason:
            raise ContractError(f"{self.fact_id}: absence_reason must be non-empty")

    def to_json(self) -> dict[str, object]:
        return {
            "fact_id": self.fact_id,
            "module_keys": list(self.module_keys),
            "absence_reason": self.absence_reason,
            "blocking": self.blocking,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "missing") -> MissingFact:
        require_keys(
            value,
            required=("fact_id", "module_keys", "absence_reason", "blocking"),
            context=context,
        )
        return cls(
            fact_id=as_str(value["fact_id"], f"{context}.fact_id"),
            module_keys=tuple(
                as_str(item, f"{context}.module_keys[]")
                for item in as_sequence(value["module_keys"], f"{context}.module_keys")
            ),
            absence_reason=as_str(value["absence_reason"], f"{context}.absence_reason"),
            blocking=as_bool(value["blocking"], f"{context}.blocking"),
        )


@dataclass(frozen=True)
class FactSnapshot:
    """``facts.snapshot.json``: the one immutable fact capture for a run."""

    run_id: str
    captured_at: datetime
    as_of_date: date
    audience: str
    source_snapshots: tuple[SourceSnapshot, ...]
    facts: tuple[FactRecord, ...]
    graphic_datasets: tuple[GraphicDataset, ...]
    conflicts: tuple[ConflictRecord, ...]
    missing_required: tuple[MissingFact, ...]
    missing_optional: tuple[MissingFact, ...]

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if self.audience not in AUDIENCES:
            raise ContractError("unknown snapshot audience")
        ensure_unique((fact.fact_id for fact in self.facts), "fact_id")
        ensure_unique((dataset.dataset_id for dataset in self.graphic_datasets), "dataset_id")
        ensure_unique((snapshot.source_alias for snapshot in self.source_snapshots), "source alias")

    def blocking_issues(self) -> tuple[str, ...]:
        """Human-readable blockers: required gaps and blocking conflicts fail closed."""
        issues = [
            f"missing required fact: {item.fact_id} ({item.absence_reason})"
            for item in self.missing_required
        ]
        issues.extend(
            f"conflicting fact: {conflict.fact_id}"
            for conflict in self.conflicts
            if conflict.blocking
        )
        issues.extend(
            f"stale fact: {fact.fact_id}" for fact in self.facts if fact.status == "stale"
        )
        return tuple(issues)

    def require_advanceable(self) -> None:
        """Required unavailable/conflicting/stale facts block story approval."""
        issues = self.blocking_issues()
        if issues:
            raise RunStateError("fact snapshot is blocked: " + "; ".join(issues))

    def verify_source_hashes(self, current: Mapping[str, str]) -> None:
        """Compare captured source hashes against a fresh capture; mismatch = stale run.

        Facts are never silently refreshed beneath an approval — a changed source marks
        the run stale and a new prepare/run is required.
        """
        for snapshot in self.source_snapshots:
            fresh = current.get(snapshot.source_alias)
            if fresh is None:
                # A vanished source is at least as stale as a changed one: fail closed.
                raise RunStateError(
                    f"source {snapshot.source_alias} is missing from the fresh capture; "
                    "the run is stale and a new prepare/run is required"
                )
            if fresh != snapshot.content_sha256:
                raise RunStateError(
                    f"source {snapshot.source_alias} changed since capture; the run is "
                    "stale and a new prepare/run is required"
                )

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "captured_at": format_utc(self.captured_at),
            "as_of_date": self.as_of_date.isoformat(),
            "audience": self.audience,
            "source_snapshots": [item.to_json() for item in self.source_snapshots],
            "facts": [fact.to_json() for fact in self.facts],
            "graphic_datasets": [
                dataset.to_json_with_provenance() for dataset in self.graphic_datasets
            ],
            "conflicts": [conflict.to_json() for conflict in self.conflicts],
            "missing_required": [item.to_json() for item in self.missing_required],
            "missing_optional": [item.to_json() for item in self.missing_optional],
        }

    def digest(self) -> str:
        return json_sha256(self.to_json())

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "facts.snapshot") -> FactSnapshot:
        check_schema_version(value, context)
        require_keys(
            value,
            required=(
                "schema_version",
                "run_id",
                "captured_at",
                "as_of_date",
                "audience",
                "source_snapshots",
                "facts",
                "graphic_datasets",
                "conflicts",
                "missing_required",
                "missing_optional",
            ),
            context=context,
        )
        return cls(
            run_id=as_str(value["run_id"], f"{context}.run_id"),
            captured_at=parse_utc(as_str(value["captured_at"], f"{context}.captured_at")),
            as_of_date=parse_iso_date(as_str(value["as_of_date"], f"{context}.as_of_date")),
            audience=as_str(value["audience"], f"{context}.audience"),
            source_snapshots=tuple(
                SourceSnapshot.from_json(as_mapping(item, f"{context}.source_snapshots[]"))
                for item in as_sequence(value["source_snapshots"], f"{context}.source_snapshots")
            ),
            facts=tuple(
                FactRecord.from_json(as_mapping(item, f"{context}.facts[]"))
                for item in as_sequence(value["facts"], f"{context}.facts")
            ),
            graphic_datasets=tuple(
                GraphicDataset.from_json(as_mapping(item, f"{context}.graphic_datasets[]"))
                for item in as_sequence(value["graphic_datasets"], f"{context}.graphic_datasets")
            ),
            conflicts=tuple(
                ConflictRecord.from_json(as_mapping(item, f"{context}.conflicts[]"))
                for item in as_sequence(value["conflicts"], f"{context}.conflicts")
            ),
            missing_required=tuple(
                MissingFact.from_json(as_mapping(item, f"{context}.missing_required[]"))
                for item in as_sequence(value["missing_required"], f"{context}.missing_required")
            ),
            missing_optional=tuple(
                MissingFact.from_json(as_mapping(item, f"{context}.missing_optional[]"))
                for item in as_sequence(value["missing_optional"], f"{context}.missing_optional")
            ),
        )


# --- run state machine + approvals ---------------------------------------------------

STATE_PREPARED = "PREPARED"
STATE_BRIEF_APPROVED = "BRIEF_APPROVED"
STATE_FACTS_APPROVED = "FACTS_APPROVED"
STATE_PREVIEW_PASSED = "PREVIEW_PASSED"
STATE_STORY_APPROVED = "STORY_APPROVED"
STATE_CANDIDATE_CREATED = "CANDIDATE_CREATED"
STATE_QA_PASSED = "QA_PASSED"
STATE_QA_FAILED = "QA_FAILED"
STATE_CANDIDATE_APPROVED = "CANDIDATE_APPROVED"
STATE_PROMOTED = "PROMOTED"

RUN_STATES: tuple[str, ...] = (
    STATE_PREPARED,
    STATE_BRIEF_APPROVED,
    STATE_FACTS_APPROVED,
    STATE_PREVIEW_PASSED,
    STATE_STORY_APPROVED,
    STATE_CANDIDATE_CREATED,
    STATE_QA_PASSED,
    STATE_QA_FAILED,
    STATE_CANDIDATE_APPROVED,
    STATE_PROMOTED,
)

ALLOWED_TRANSITIONS: Mapping[str, tuple[str, ...]] = {
    STATE_PREPARED: (STATE_BRIEF_APPROVED,),
    STATE_BRIEF_APPROVED: (STATE_FACTS_APPROVED,),
    STATE_FACTS_APPROVED: (STATE_PREVIEW_PASSED,),
    STATE_PREVIEW_PASSED: (STATE_STORY_APPROVED,),
    STATE_STORY_APPROVED: (STATE_CANDIDATE_CREATED,),
    STATE_CANDIDATE_CREATED: (STATE_QA_PASSED, STATE_QA_FAILED),
    STATE_QA_PASSED: (STATE_CANDIDATE_APPROVED,),
    STATE_QA_FAILED: (),
    STATE_CANDIDATE_APPROVED: (STATE_PROMOTED,),
    STATE_PROMOTED: (),
}

APPROVAL_STAGE_BRIEF = "brief"
APPROVAL_STAGE_FACTS = "facts"
APPROVAL_STAGE_STORY = "story"
APPROVAL_STAGE_CANDIDATE = "candidate"
APPROVAL_STAGES: tuple[str, ...] = (
    APPROVAL_STAGE_BRIEF,
    APPROVAL_STAGE_FACTS,
    APPROVAL_STAGE_STORY,
    APPROVAL_STAGE_CANDIDATE,
)

#: The run-relative artifact each approval stage seals.
STAGE_ARTIFACTS: Mapping[str, str] = {
    APPROVAL_STAGE_BRIEF: "brief.draft.json",
    APPROVAL_STAGE_FACTS: "facts.snapshot.json",
    APPROVAL_STAGE_STORY: "deck.bundle.json",
    APPROVAL_STAGE_CANDIDATE: "qa/candidate-report.json",
}

#: The artifact whose digest each stage's approval must name as its upstream.
STAGE_UPSTREAM_ARTIFACTS: Mapping[str, str] = {
    APPROVAL_STAGE_BRIEF: "request.txt",
    APPROVAL_STAGE_FACTS: "brief.draft.json",
    APPROVAL_STAGE_STORY: "facts.snapshot.json",
    APPROVAL_STAGE_CANDIDATE: "deck.bundle.json",
}

#: Approval stages that must be present (in order) for each state.
STATE_REQUIRED_STAGES: Mapping[str, tuple[str, ...]] = {
    STATE_PREPARED: (),
    STATE_BRIEF_APPROVED: (APPROVAL_STAGE_BRIEF,),
    STATE_FACTS_APPROVED: (APPROVAL_STAGE_BRIEF, APPROVAL_STAGE_FACTS),
    STATE_PREVIEW_PASSED: (APPROVAL_STAGE_BRIEF, APPROVAL_STAGE_FACTS),
    STATE_STORY_APPROVED: (APPROVAL_STAGE_BRIEF, APPROVAL_STAGE_FACTS, APPROVAL_STAGE_STORY),
    STATE_CANDIDATE_CREATED: (
        APPROVAL_STAGE_BRIEF,
        APPROVAL_STAGE_FACTS,
        APPROVAL_STAGE_STORY,
    ),
    STATE_QA_PASSED: (APPROVAL_STAGE_BRIEF, APPROVAL_STAGE_FACTS, APPROVAL_STAGE_STORY),
    STATE_QA_FAILED: (APPROVAL_STAGE_BRIEF, APPROVAL_STAGE_FACTS, APPROVAL_STAGE_STORY),
    STATE_CANDIDATE_APPROVED: APPROVAL_STAGES,
    STATE_PROMOTED: APPROVAL_STAGES,
}


def validate_transition(current: str, new: str) -> None:
    """Only the durable state sequence's edges are legal; anything else fails closed."""
    if current not in ALLOWED_TRANSITIONS:
        raise RunStateError(f"unknown run state: {current!r}")
    if new not in RUN_STATES:
        raise RunStateError(f"unknown run state: {new!r}")
    if new not in ALLOWED_TRANSITIONS[current]:
        raise RunStateError(f"illegal state transition {current} -> {new}")


@dataclass(frozen=True)
class Approval:
    """Stage name, approved artifact digest, timestamp, and upstream digests."""

    stage: str
    approved_sha256: str
    approved_at: datetime
    upstream_sha256: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.stage not in APPROVAL_STAGES:
            raise ContractError(f"unknown approval stage: {self.stage!r}")
        validate_sha256(self.approved_sha256, f"{self.stage}.approved_sha256")
        for digest in self.upstream_sha256:
            validate_sha256(digest, f"{self.stage}.upstream_sha256[]")

    def to_json(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "approved_sha256": self.approved_sha256,
            "approved_at": format_utc(self.approved_at),
            "upstream_sha256": list(self.upstream_sha256),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "approval") -> Approval:
        require_keys(
            value,
            required=("stage", "approved_sha256", "approved_at", "upstream_sha256"),
            context=context,
        )
        return cls(
            stage=as_str(value["stage"], f"{context}.stage"),
            approved_sha256=as_str(value["approved_sha256"], f"{context}.approved_sha256"),
            approved_at=parse_utc(as_str(value["approved_at"], f"{context}.approved_at")),
            upstream_sha256=tuple(
                as_str(item, f"{context}.upstream_sha256[]")
                for item in as_sequence(value["upstream_sha256"], f"{context}.upstream_sha256")
            ),
        )


@dataclass(frozen=True)
class CleanupReceipt:
    """Resource role, deletion attempt time, verified-absent time, fixed result code."""

    resource_role: str
    attempted_at: datetime
    verified_absent_at: datetime | None
    result_code: str

    def __post_init__(self) -> None:
        if not self.resource_role or not self.result_code:
            raise ContractError("cleanup receipts carry a role and a fixed result code")

    def to_json(self) -> dict[str, object]:
        return {
            "resource_role": self.resource_role,
            "attempted_at": format_utc(self.attempted_at),
            "verified_absent_at": (
                None if self.verified_absent_at is None else format_utc(self.verified_absent_at)
            ),
            "result_code": self.result_code,
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "receipt") -> CleanupReceipt:
        require_keys(
            value,
            required=("resource_role", "attempted_at", "verified_absent_at", "result_code"),
            context=context,
        )
        verified = as_opt_str(value["verified_absent_at"], f"{context}.verified_absent_at")
        return cls(
            resource_role=as_str(value["resource_role"], f"{context}.resource_role"),
            attempted_at=parse_utc(as_str(value["attempted_at"], f"{context}.attempted_at")),
            verified_absent_at=None if verified is None else parse_utc(verified),
            result_code=as_str(value["result_code"], f"{context}.result_code"),
        )


@dataclass(frozen=True)
class PromotionAttempt:
    """Immutable record of one promotion attempt (monotonic attempt number)."""

    attempt: int
    started_at: datetime
    reconciliation_result: str
    final_id: str | None
    final_version: str | None
    pre_sha256: str | None
    post_sha256: str | None
    failure_code: str | None
    finished_at: datetime | None

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ContractError("promotion attempt numbers start at 1")
        if not self.reconciliation_result:
            raise ContractError("promotion attempts carry a reconciliation result")
        for digest in (self.pre_sha256, self.post_sha256):
            if digest is not None:
                validate_sha256(digest, "promotion digest")

    def to_json(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "started_at": format_utc(self.started_at),
            "reconciliation_result": self.reconciliation_result,
            "final_id": self.final_id,
            "final_version": self.final_version,
            "pre_sha256": self.pre_sha256,
            "post_sha256": self.post_sha256,
            "failure_code": self.failure_code,
            "finished_at": None if self.finished_at is None else format_utc(self.finished_at),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "attempt") -> PromotionAttempt:
        require_keys(
            value,
            required=(
                "attempt",
                "started_at",
                "reconciliation_result",
                "final_id",
                "final_version",
                "pre_sha256",
                "post_sha256",
                "failure_code",
                "finished_at",
            ),
            context=context,
        )
        finished = as_opt_str(value["finished_at"], f"{context}.finished_at")
        return cls(
            attempt=as_int(value["attempt"], f"{context}.attempt"),
            started_at=parse_utc(as_str(value["started_at"], f"{context}.started_at")),
            reconciliation_result=as_str(
                value["reconciliation_result"], f"{context}.reconciliation_result"
            ),
            final_id=as_opt_str(value["final_id"], f"{context}.final_id"),
            final_version=as_opt_str(value["final_version"], f"{context}.final_version"),
            pre_sha256=as_opt_str(value["pre_sha256"], f"{context}.pre_sha256"),
            post_sha256=as_opt_str(value["post_sha256"], f"{context}.post_sha256"),
            failure_code=as_opt_str(value["failure_code"], f"{context}.failure_code"),
            finished_at=None if finished is None else parse_utc(finished),
        )


# --- manifest ------------------------------------------------------------------------

MANIFEST_NAME = "manifest.json"
LOCK_NAME = ".lock"

#: The fixed private run-directory layout (section 5.1). Artifact writes outside this
#: allowlist are containment violations. ``manifest.json`` is written only by commit.
RUN_ARTIFACT_PATHS: tuple[str, ...] = (
    "request.txt",
    "brief.draft.json",
    "facts.snapshot.json",
    "deck.bundle.json",
    "ignored-choices.json",
    "review.html",
    "preview/storyboard.html",
    "qa/local-report.json",
    "qa/candidate-gallery.html",
    "qa/candidate-report.json",
)
_RUN_ARTIFACT_PREFIXES: tuple[str, ...] = ("preview/slides/",)

#: Allowlisted manifest fields for opaque remote IDs/versions. Steps 19-21 extend this
#: single owner when the Google workspace lands; in Step 14 no remote ID may persist.
REMOTE_ID_ALLOWLIST: tuple[str, ...] = ()


def validate_artifact_path(relpath: str) -> str:
    """Canonical run-relative artifact path from the fixed layout (containment)."""
    if relpath in RUN_ARTIFACT_PATHS:
        return relpath
    for prefix in _RUN_ARTIFACT_PREFIXES:
        if relpath.startswith(prefix):
            remainder = relpath[len(prefix) :]
            if remainder and not any(part in remainder for part in ("/", "\\", "..")):
                return relpath
    raise ContractError(f"artifact path is outside the run layout: {relpath!r}")


@dataclass(frozen=True)
class Manifest:
    """``manifest.json``: durable state, generation counter, and artifact digests."""

    run_id: str
    created_at: datetime
    state: str
    generation: int
    artifact_sha256: tuple[tuple[str, str], ...]
    approvals: tuple[Approval, ...]
    remote_ids: tuple[tuple[str, str], ...] = ()
    cleanup_receipts: tuple[CleanupReceipt, ...] = ()
    promotion_attempts: tuple[PromotionAttempt, ...] = ()
    supersedes_run_id: str | None = None

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if self.supersedes_run_id is not None:
            validate_run_id(self.supersedes_run_id, "supersedes_run_id")
        if self.state not in RUN_STATES:
            raise ContractError(f"unknown run state: {self.state!r}")
        if self.generation < 1:
            raise ContractError("manifest generation is monotonic and starts at 1")
        ensure_unique((path for path, _ in self.artifact_sha256), "artifact path")
        for path, digest in self.artifact_sha256:
            validate_artifact_path(path)
            validate_sha256(digest, f"artifact_sha256[{path}]")
        ensure_unique((approval.stage for approval in self.approvals), "approval stage")
        for key, _ in self.remote_ids:
            if key not in REMOTE_ID_ALLOWLIST:
                raise ContractError(f"remote ID field is not allowlisted: {key!r}")
        attempts = [attempt.attempt for attempt in self.promotion_attempts]
        if attempts != sorted(set(attempts)) or (attempts and attempts[0] != 1):
            raise ContractError("promotion attempts must be monotonic from 1")

    def artifact_map(self) -> dict[str, str]:
        return dict(self.artifact_sha256)

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "supersedes_run_id": self.supersedes_run_id,
            "created_at": format_utc(self.created_at),
            "state": self.state,
            "generation": self.generation,
            "artifact_sha256": dict(sorted(self.artifact_sha256)),
            "approvals": [approval.to_json() for approval in self.approvals],
            "remote_ids": dict(sorted(self.remote_ids)),
            "cleanup_receipts": [receipt.to_json() for receipt in self.cleanup_receipts],
            "promotion_attempts": [attempt.to_json() for attempt in self.promotion_attempts],
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any], context: str = "manifest") -> Manifest:
        check_schema_version(value, context)
        require_keys(
            value,
            required=(
                "schema_version",
                "run_id",
                "supersedes_run_id",
                "created_at",
                "state",
                "generation",
                "artifact_sha256",
                "approvals",
                "remote_ids",
                "cleanup_receipts",
                "promotion_attempts",
            ),
            context=context,
        )
        artifacts = as_mapping(value["artifact_sha256"], f"{context}.artifact_sha256")
        remote = as_mapping(value["remote_ids"], f"{context}.remote_ids")
        return cls(
            run_id=as_str(value["run_id"], f"{context}.run_id"),
            supersedes_run_id=as_opt_str(
                value["supersedes_run_id"], f"{context}.supersedes_run_id"
            ),
            created_at=parse_utc(as_str(value["created_at"], f"{context}.created_at")),
            state=as_str(value["state"], f"{context}.state"),
            generation=as_int(value["generation"], f"{context}.generation"),
            artifact_sha256=tuple(
                sorted(
                    (key, as_str(item, f"{context}.artifact_sha256[{key}]"))
                    for key, item in artifacts.items()
                )
            ),
            approvals=tuple(
                Approval.from_json(as_mapping(item, f"{context}.approvals[]"))
                for item in as_sequence(value["approvals"], f"{context}.approvals")
            ),
            remote_ids=tuple(
                sorted(
                    (key, as_str(item, f"{context}.remote_ids[{key}]"))
                    for key, item in remote.items()
                )
            ),
            cleanup_receipts=tuple(
                CleanupReceipt.from_json(as_mapping(item, f"{context}.cleanup_receipts[]"))
                for item in as_sequence(value["cleanup_receipts"], f"{context}.cleanup_receipts")
            ),
            promotion_attempts=tuple(
                PromotionAttempt.from_json(as_mapping(item, f"{context}.promotion_attempts[]"))
                for item in as_sequence(
                    value["promotion_attempts"], f"{context}.promotion_attempts"
                )
            ),
        )


def validate_manifest_consistency(manifest: Manifest) -> None:
    """State/approval/digest coherence — a changed upstream invalidates downstream.

    For every approval present: its stage must be expected at (or before) the current
    state, its ``approved_sha256`` must equal the recorded digest of its stage artifact,
    and its first upstream digest must equal the recorded digest of its upstream
    artifact. Any mismatch means an upstream changed after approval: fail closed.
    """
    required = STATE_REQUIRED_STAGES[manifest.state]
    stages_present = tuple(approval.stage for approval in manifest.approvals)
    if stages_present != tuple(required):
        raise RunStateError(
            f"state {manifest.state} requires approvals {list(required)}, "
            f"found {list(stages_present)}"
        )
    artifacts = manifest.artifact_map()
    for approval in manifest.approvals:
        artifact_path = STAGE_ARTIFACTS[approval.stage]
        recorded = artifacts.get(artifact_path)
        if recorded is None:
            raise RunStateError(
                f"approval {approval.stage} names artifact {artifact_path} which is not "
                "in the manifest"
            )
        if recorded != approval.approved_sha256:
            raise RunStateError(
                f"approval {approval.stage} is invalidated: {artifact_path} changed after approval"
            )
        upstream_path = STAGE_UPSTREAM_ARTIFACTS[approval.stage]
        upstream_recorded = artifacts.get(upstream_path)
        if not approval.upstream_sha256:
            raise RunStateError(f"approval {approval.stage} carries no upstream digest")
        if upstream_recorded is None or upstream_recorded != approval.upstream_sha256[0]:
            raise RunStateError(
                f"approval {approval.stage} is invalidated: upstream {upstream_path} "
                "changed after approval"
            )


def build_approval(manifest: Manifest, stage: str, approved_at: datetime) -> Approval:
    """Seal ``stage`` against the artifact digests currently recorded in the manifest."""
    if stage not in APPROVAL_STAGES:
        raise ContractError(f"unknown approval stage: {stage!r}")
    artifacts = manifest.artifact_map()
    artifact_path = STAGE_ARTIFACTS[stage]
    upstream_path = STAGE_UPSTREAM_ARTIFACTS[stage]
    if artifact_path not in artifacts:
        raise RunStateError(f"cannot approve {stage}: {artifact_path} is not recorded")
    if upstream_path not in artifacts:
        raise RunStateError(f"cannot approve {stage}: upstream {upstream_path} is missing")
    return Approval(
        stage=stage,
        approved_sha256=artifacts[artifact_path],
        approved_at=approved_at,
        upstream_sha256=(artifacts[upstream_path],),
    )


# --- locked atomic persistence -------------------------------------------------------


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Same-directory temp + fsync + ``os.replace`` (the repository's atomic idiom)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def claim_run_dir(run_root: Path, run_id: str) -> Path:
    """Atomically claim the run directory; an existing directory is a collision."""
    run_dir = resolve_run_dir(run_root, run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(run_dir)
    except FileExistsError as exc:
        raise RunCollisionError(f"run directory already exists: {run_id}") from exc
    return run_dir


class RunLock:
    """Per-run single-writer lock (exclusive-create lock file).

    A crash leaves the lock file behind; the next writer fails closed with
    :class:`RunLockedError` rather than guessing, and the operator clears it after
    confirming no writer is alive.
    """

    def __init__(self, run_dir: Path) -> None:
        self._path = run_dir / LOCK_NAME
        self._fd: int | None = None

    def acquire(self) -> None:
        try:
            self._fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RunLockedError(
                f"run is locked by another writer (lock file {self._path.name} exists)"
            ) from exc

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            try:
                os.unlink(self._path)
            except OSError:
                pass

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def _read_manifest_file(run_dir: Path) -> Manifest:
    manifest_path = run_dir / MANIFEST_NAME
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise RunCorruptError(f"manifest is unreadable: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunCorruptError(f"manifest is not valid JSON: {exc}") from exc
    try:
        return Manifest.from_json(as_mapping(value, "manifest"))
    except ContractError as exc:
        raise RunCorruptError(f"manifest violates its contract: {exc}") from exc


def load_manifest(run_dir: Path) -> Manifest:
    """Strictly parse ``manifest.json`` (no artifact verification)."""
    return _read_manifest_file(run_dir)


def load_run(run_dir: Path) -> Manifest:
    """Resume a run: verify every referenced digest and the approval chain.

    Partial or corrupt state (missing artifact, digest mismatch, inconsistent
    approvals) raises :class:`RunCorruptError`/:class:`RunStateError` — the run is
    blocked rather than guessed at.
    """
    manifest = _read_manifest_file(run_dir)
    for path, digest in manifest.artifact_sha256:
        artifact = run_dir / Path(path)
        try:
            data = artifact.read_bytes()
        except OSError as exc:
            raise RunCorruptError(f"referenced artifact is missing: {path}") from exc
        if sha256_hex(data) != digest:
            raise RunCorruptError(
                f"artifact {path} does not match its recorded digest; the run is blocked"
            )
    validate_manifest_consistency(manifest)
    return manifest


def create_run(
    run_root: Path,
    *,
    now: datetime,
    supersedes_run_id: str | None = None,
    max_attempts: int = 5,
) -> tuple[Manifest, Path]:
    """Mint a run ID, atomically claim its directory, and commit the initial manifest."""
    last_error: RunCollisionError | None = None
    for _ in range(max_attempts):
        run_id = new_run_id(now)
        try:
            run_dir = claim_run_dir(run_root, run_id)
        except RunCollisionError as exc:
            last_error = exc
            continue
        manifest = Manifest(
            run_id=run_id,
            created_at=now,
            state=STATE_PREPARED,
            generation=1,
            artifact_sha256=(),
            approvals=(),
            supersedes_run_id=supersedes_run_id,
        )
        _atomic_write_bytes(run_dir / MANIFEST_NAME, canonical_json_bytes(manifest.to_json()))
        return manifest, run_dir
    raise RunCollisionError(
        f"could not claim a run directory after {max_attempts} attempts"
    ) from last_error


class RunTransaction:
    """One locked, manifest-last, compare-and-swap run mutation.

    Usage::

        with RunTransaction(run_dir) as txn:
            txn.write_json_artifact("brief.draft.json", brief.to_json())
            txn.commit(state=..., approvals=...)

    Artifacts are written first (write-once, atomic per file); ``commit`` atomically
    replaces the manifest last with ``generation + 1``. If the transaction never
    commits, the previous manifest still governs. A concurrent writer is excluded by
    the per-run lock; a lost generation race fails closed.
    """

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir
        self._lock = RunLock(run_dir)
        self._base: Manifest | None = None
        self._staged: dict[str, str] = {}
        self._committed = False

    @property
    def manifest(self) -> Manifest:
        if self._base is None:
            raise RunStateError("transaction is not active")
        return self._base

    def __enter__(self) -> RunTransaction:
        self._lock.acquire()
        try:
            self._base = _read_manifest_file(self._run_dir)
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._lock.release()

    def write_json_artifact(self, relpath: str, payload: Mapping[str, object]) -> str:
        """Write one immutable canonical-JSON artifact; returns its digest."""
        data = canonical_json_bytes(payload)
        return self.write_bytes_artifact(relpath, data)

    def write_bytes_artifact(self, relpath: str, data: bytes) -> str:
        """Write one immutable artifact (atomic, write-once, containment-checked)."""
        validate_artifact_path(relpath)
        if self._base is None:
            raise RunStateError("transaction is not active")
        if relpath in self._base.artifact_map() or relpath in self._staged:
            raise ContractError(
                f"artifact {relpath} is already committed; run artifacts are immutable"
            )
        _atomic_write_bytes(self._run_dir / Path(relpath), data)
        digest = sha256_hex(data)
        self._staged[relpath] = digest
        return digest

    def commit(
        self,
        *,
        state: str | None = None,
        approvals: tuple[Approval, ...] | None = None,
        cleanup_receipts: tuple[CleanupReceipt, ...] | None = None,
        promotion_attempts: tuple[PromotionAttempt, ...] | None = None,
    ) -> Manifest:
        """Compare-and-swap the manifest (generation + 1), replacing it atomically last."""
        if self._base is None:
            raise RunStateError("transaction is not active")
        if self._committed:
            raise RunStateError("transaction already committed")
        current = _read_manifest_file(self._run_dir)
        if current.generation != self._base.generation:
            raise RunStateError(
                "manifest generation moved underneath the transaction "
                f"({self._base.generation} -> {current.generation}); fail closed"
            )
        new_state = self._base.state if state is None else state
        if new_state != self._base.state:
            validate_transition(self._base.state, new_state)
        merged = dict(self._base.artifact_sha256)
        merged.update(self._staged)
        updated = Manifest(
            run_id=self._base.run_id,
            created_at=self._base.created_at,
            state=new_state,
            generation=self._base.generation + 1,
            artifact_sha256=tuple(sorted(merged.items())),
            approvals=self._base.approvals if approvals is None else approvals,
            remote_ids=self._base.remote_ids,
            cleanup_receipts=(
                self._base.cleanup_receipts if cleanup_receipts is None else cleanup_receipts
            ),
            promotion_attempts=(
                self._base.promotion_attempts if promotion_attempts is None else promotion_attempts
            ),
            supersedes_run_id=self._base.supersedes_run_id,
        )
        validate_manifest_consistency(updated)
        _atomic_write_bytes(self._run_dir / MANIFEST_NAME, canonical_json_bytes(updated.to_json()))
        self._committed = True
        self._base = updated
        return updated
