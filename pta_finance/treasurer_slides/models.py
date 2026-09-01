"""Strict private-input and normalized-finance contracts for Treasurer Slides.

This module is intentionally independent of Google, PDF, OCR, and Sheet clients.  It
owns the small v1 data shapes that later adapters exchange and rejects ambiguous or
unsafe private input before any statement text or financial value can cross a boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from enum import Enum, StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NoReturn

MANIFEST_SCHEMA_VERSION = 1
RULES_SCHEMA_VERSION = 1
PRIVATE_JSON_MAX_BYTES = 1_048_576
CENT = Decimal("0.01")
MAX_CANONICAL_JSON_DEPTH = 100
MAX_MONEY_INTEGER_DIGITS = 64
MAX_POSITION_DECIMAL_PLACES = 12
MAX_BALANCE_ROW_HEIGHT = Decimal("0.20")

_MONEY_TEXT_RE = re.compile(r"^(?:0|[1-9][0-9]*)\.[0-9]{2}$")
_SOURCE_MONEY_RE = re.compile(
    r"(?<![0-9A-Za-z,.])(?:[$]\s*)?((?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)\.[0-9]{2})(?![0-9A-Za-z,.])"
)
_SOURCE_DATE_RE = re.compile(
    r"(?<![0-9])(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}/[0-9]{1,2}(?:/[0-9]{2,4})?)(?![0-9/])"
)
_POSITION_TEXT_RE = re.compile(r"^(?:0(?:\.[0-9]+)?|1(?:\.0+)?)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FISCAL_YEAR_RE = re.compile(r"^FY[0-9]{4}$")
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_DASH_TRANSLATION = str.maketrans({character: "-" for character in _DASHES})
_WINDOWS_FORBIDDEN_COMPONENT_CHARACTERS = frozenset('<>:"/\\|?*~')
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CLOCK$",
        "CONIN$",
        "CONOUT$",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
)


class TreasurerSlidesError(ValueError):
    """Base exception for safe, operator-actionable Treasurer Slides failures."""


class PrivateInputError(TreasurerSlidesError):
    """Private input cannot be safely read or does not meet the v1 contract."""


class ContractError(TreasurerSlidesError):
    """A normalized in-memory contract is invalid or internally inconsistent."""


class AccountRole(StrEnum):
    CHECKING = "checking"
    SAVINGS = "savings"
    TIME = "time"

    @property
    def display_name(self) -> str:
        return {
            AccountRole.CHECKING: "Checking",
            AccountRole.SAVINGS: "Savings",
            AccountRole.TIME: "Time Account (Buffer)",
        }[self]


class DocumentKind(StrEnum):
    MONTHLY_STATEMENT = "monthly_statement"
    CURRENT_ACTIVITY = "current_activity"


class CashBasis(StrEnum):
    AVAILABLE_INCLUDING_PENDING = "available_including_pending"


class Direction(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"


class TransactionStatus(StrEnum):
    POSTED = "posted"
    PENDING = "pending"


class BalanceKind(StrEnum):
    OPENING = "opening"
    CLOSING = "closing"
    COLLECTED = "collected"
    AVAILABLE = "available"


class BalanceBoundary(StrEnum):
    START_OF_DAY = "start_of_day"
    END_OF_DAY = "end_of_day"
    CAPTURE = "capture"


class ExtractionMethod(StrEnum):
    NATIVE = "native"
    OCR = "ocr"


class PageKind(StrEnum):
    """Supported statement-page evidence kinds, including non-financial boilerplate."""

    MONTHLY_SUMMARY = "monthly_summary"
    MONTHLY_ACTIVITY = "monthly_activity"
    CURRENT_BALANCE = "current_balance"
    CURRENT_ACTIVITY = "current_activity"
    BOILERPLATE = "boilerplate"


class PageFingerprint(StrEnum):
    """Recognized, fail-closed statement-layout fingerprints."""

    WELLS_FARGO_V1_MONTHLY_SUMMARY = "wf-v1/monthly-summary"
    WELLS_FARGO_V1_MONTHLY_ACTIVITY = "wf-v1/monthly-activity"
    WELLS_FARGO_V1_CURRENT_BALANCE = "wf-v1/current-balance"
    WELLS_FARGO_V1_CURRENT_ACTIVITY = "wf-v1/current-activity"
    WELLS_FARGO_V1_BOILERPLATE = "wf-v1/boilerplate"

    @property
    def parser_version(self) -> str:
        return "wf-v1"

    @property
    def page_kind(self) -> PageKind:
        return {
            PageFingerprint.WELLS_FARGO_V1_MONTHLY_SUMMARY: PageKind.MONTHLY_SUMMARY,
            PageFingerprint.WELLS_FARGO_V1_MONTHLY_ACTIVITY: PageKind.MONTHLY_ACTIVITY,
            PageFingerprint.WELLS_FARGO_V1_CURRENT_BALANCE: PageKind.CURRENT_BALANCE,
            PageFingerprint.WELLS_FARGO_V1_CURRENT_ACTIVITY: PageKind.CURRENT_ACTIVITY,
            PageFingerprint.WELLS_FARGO_V1_BOILERPLATE: PageKind.BOILERPLATE,
        }[self]


class ActivityColumn(StrEnum):
    """The positioned columns that define a supported activity table."""

    DATE = "date"
    DESCRIPTION = "description"
    DEBIT = "debit"
    CREDIT = "credit"


class EvidenceField(StrEnum):
    """A statement fact component that must point back to positioned source text."""

    DATE = "date"
    DESCRIPTION = "description"
    DIRECTION = "direction"
    STATUS = "status"
    MAGNITUDE = "magnitude"
    BALANCE = "balance"
    KIND = "kind"
    BOUNDARY = "boundary"
    INCLUDES_PENDING = "includes_pending"


class CashRole(StrEnum):
    FUNDRAISING = "fundraising"
    SPENDING = "spending"
    INTEREST = "interest"
    TRANSFER = "transfer"
    REVERSAL = "reversal"


class MatcherKind(StrEnum):
    EXACT = "exact"
    PREFIX = "prefix"
    CONTAINS = "contains"


class PairAction(StrEnum):
    PAIR_AS_TRANSFER = "pair_as_transfer"
    PAIR_AS_REVERSAL = "pair_as_reversal"


class AdjustmentAction(StrEnum):
    EXCLUDE_FROM_CURRENT_BOARD_SPEND = "exclude_from_current_board_spend"


_TRANSACTION_EVIDENCE_FIELDS = (
    EvidenceField.DATE,
    EvidenceField.DESCRIPTION,
    EvidenceField.DIRECTION,
    EvidenceField.STATUS,
    EvidenceField.MAGNITUDE,
)
_BALANCE_EVIDENCE_FIELDS = (
    EvidenceField.DATE,
    EvidenceField.BALANCE,
    EvidenceField.KIND,
    EvidenceField.BOUNDARY,
    EvidenceField.INCLUDES_PENDING,
)
_ACTIVITY_PAGE_KINDS = frozenset({PageKind.MONTHLY_ACTIVITY, PageKind.CURRENT_ACTIVITY})
_PAGE_KINDS_BY_DOCUMENT_KIND = {
    DocumentKind.MONTHLY_STATEMENT: frozenset(
        {PageKind.MONTHLY_SUMMARY, PageKind.MONTHLY_ACTIVITY, PageKind.BOILERPLATE}
    ),
    DocumentKind.CURRENT_ACTIVITY: frozenset(
        {PageKind.CURRENT_BALANCE, PageKind.CURRENT_ACTIVITY, PageKind.BOILERPLATE}
    ),
}
_ACTIVITY_COLUMNS = (
    ActivityColumn.DATE,
    ActivityColumn.DESCRIPTION,
    ActivityColumn.DEBIT,
    ActivityColumn.CREDIT,
)
_SUPPORTED_PARSER_VERSIONS = frozenset(
    fingerprint.parser_version for fingerprint in PageFingerprint
)
_DIRECTION_EVIDENCE_TERMS = {
    Direction.CREDIT: ("credit", "credits", "deposit", "deposits", "addition", "additions"),
    Direction.DEBIT: ("debit", "debits", "withdrawal", "withdrawals"),
}
_ACTIVITY_COLUMN_HEADER_TERMS = {
    ActivityColumn.DATE: ("date",),
    ActivityColumn.DESCRIPTION: ("description", "details"),
    ActivityColumn.DEBIT: _DIRECTION_EVIDENCE_TERMS[Direction.DEBIT],
    ActivityColumn.CREDIT: _DIRECTION_EVIDENCE_TERMS[Direction.CREDIT],
}
_STATUS_EVIDENCE_TERMS = {
    TransactionStatus.POSTED: ("posted",),
    TransactionStatus.PENDING: ("pending",),
}
_BALANCE_KIND_EVIDENCE_TERMS = {
    BalanceKind.OPENING: ("opening", "beginning"),
    BalanceKind.CLOSING: ("closing", "ending"),
    BalanceKind.COLLECTED: ("collected",),
    BalanceKind.AVAILABLE: ("available",),
}
_BALANCE_BOUNDARY_EVIDENCE_TERMS = {
    BalanceBoundary.START_OF_DAY: ("start-of-day", "start of day", "beginning-of-day"),
    BalanceBoundary.END_OF_DAY: ("end-of-day", "end of day"),
    BalanceBoundary.CAPTURE: ("capture", "as-of", "as of"),
}
_PENDING_BASIS_EVIDENCE_TERMS = {
    True: ("includes-pending", "includes pending", "including pending", "available"),
    False: ("excludes-pending", "excludes pending", "excluding pending", "collected"),
}
_SELECTOR_FIELDS = frozenset(
    {
        "account_role",
        "effective_date",
        "status",
        "direction",
        "magnitude",
        "normalized_description",
        "occurrence_ordinal",
        "source_sha256",
        "page_number",
        "source_row_ordinal",
    }
)
_EXTERNAL_GIT_CONFIGURATION_KEYS = frozenset(
    {
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "XDG_CONFIG_HOME",
        "XDG_CONFIG_DIRS",
    }
)


def _fail_input() -> NoReturn:
    """Raise one non-leaking error for malformed private input."""
    raise PrivateInputError("private input does not satisfy the Treasurer Slides contract")


def _fail_contract() -> NoReturn:
    """Raise one non-leaking error for an invalid normalized contract."""
    raise ContractError("Treasurer Slides contract is invalid")


def _raise(private: bool) -> NoReturn:
    if private:
        _fail_input()
    _fail_contract()


def _parse_enum[E: Enum](enum_type: type[E], value: object, *, private: bool) -> E:
    if not isinstance(value, str):
        _raise(private)
    try:
        return enum_type(value)
    except ValueError:
        _raise(private)


def _require_enum[E: Enum](enum_type: type[E], value: object) -> E:
    """Require an already-normalized enum rather than silently coercing strings."""
    if not isinstance(value, enum_type):
        _fail_contract()
    return value


def _require_calendar_date(value: object) -> date:
    """Require a date object that cannot carry an unplanned time component."""
    if not isinstance(value, date) or isinstance(value, datetime):
        _fail_contract()
    return value


def _require_positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        _fail_contract()
    return value


def _require_nonblank_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail_contract()
    return value


def _require_supported_parser_version(value: object) -> str:
    """Require the version of a known, recognized page fingerprint."""
    text = _require_nonblank_text(value)
    if text not in _SUPPORTED_PARSER_VERSIONS:
        _fail_contract()
    return text


def _require_bool(value: object) -> bool:
    if not isinstance(value, bool):
        _fail_contract()
    return value


def _require_optional_sha256(value: object) -> str | None:
    if value is None:
        return None
    return _parse_sha256(value, private=False)


def parse_iso_date(value: object, *, private: bool = False) -> date:
    """Parse only an exact ISO calendar date without reflecting the source value."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or len(value) != 10:
        _raise(private)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _raise(private)
    if parsed.isoformat() != value:
        _raise(private)
    return parsed


def _integer_digit_count(value: Decimal) -> int:
    """Count a finite Decimal's integer digits without consulting Decimal context."""
    return 1 if value.is_zero() else max(1, value.adjusted() + 1)


def _quantize_cent_exact(value: Decimal, *, private: bool, allow_negative: bool) -> Decimal:
    """Quantize an exact, bounded monetary Decimal without ambient-context dependence."""
    if (
        not value.is_finite()
        or (value.is_zero() and value.is_signed())
        or (not allow_negative and value.is_signed())
    ):
        _raise(private)
    if value.is_zero():
        return Decimal("0.00")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent < -2:
        _raise(private)
    integer_digits = _integer_digit_count(value)
    if integer_digits > MAX_MONEY_INTEGER_DIGITS:
        _raise(private)
    try:
        context = Context(
            prec=max(28, integer_digits + 2),
            rounding=ROUND_HALF_EVEN,
            Emin=-MAX_MONEY_INTEGER_DIGITS,
            Emax=MAX_MONEY_INTEGER_DIGITS,
            capitals=1,
            clamp=0,
            traps=[InvalidOperation],
        )
        normalized = context.quantize(value, CENT)
    except (InvalidOperation, ValueError):
        _raise(private)
    if not normalized.is_finite() or (normalized.is_zero() and normalized.is_signed()):
        _raise(private)
    return normalized


def _coerce_money(value: object, *, allow_zero: bool, private: bool) -> Decimal:
    """Return an exact cent Decimal, rejecting floats and noncanonical magnitudes."""
    if isinstance(value, bool) or isinstance(value, float):
        _raise(private)
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, str) and _MONEY_TEXT_RE.fullmatch(value):
        try:
            amount = Decimal(value)
        except InvalidOperation:
            _fail_input() if private else _fail_contract()
    else:
        _raise(private)
    normalized = _quantize_cent_exact(amount, private=private, allow_negative=False)
    if not allow_zero and normalized == Decimal("0.00"):
        _raise(private)
    return normalized


def parse_positive_money(value: object, *, private: bool = False) -> Decimal:
    """Parse a positive, finite, cent-exact monetary magnitude."""
    return _coerce_money(value, allow_zero=False, private=private)


def parse_nonnegative_money(value: object, *, private: bool = False) -> Decimal:
    """Parse a finite, cent-exact magnitude that may be zero (for balances)."""
    return _coerce_money(value, allow_zero=True, private=private)


def money_text(value: Decimal) -> str:
    """Render a validated Decimal as its canonical two-decimal JSON representation."""
    amount = parse_nonnegative_money(value)
    return f"{amount:.2f}"


def _parse_unit_position(value: object) -> Decimal:
    """Parse an exact normalized coordinate without allowing a binary float."""
    if isinstance(value, bool) or isinstance(value, float):
        _fail_contract()
    if isinstance(value, Decimal):
        position = value
    elif isinstance(value, str) and _POSITION_TEXT_RE.fullmatch(value):
        try:
            position = Decimal(value)
        except InvalidOperation:
            _fail_contract()
    else:
        _fail_contract()
    if (
        not position.is_finite()
        or position.is_signed()
        or position < Decimal("0")
        or position > Decimal("1")
    ):
        _fail_contract()
    if position.is_zero():
        return Decimal("0")
    exponent = position.as_tuple().exponent
    if not isinstance(exponent, int):
        _fail_contract()
    digits = list(position.as_tuple().digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if exponent < -MAX_POSITION_DECIMAL_PLACES:
        _fail_contract()
    return Decimal((0, tuple(digits), exponent))


def _position_text(value: Decimal) -> str:
    text = format(_parse_unit_position(value), "f")
    return text if "." in text else f"{text}.0"


def normalize_description(value: str) -> str:
    """Produce the v1 description identity form without deleting punctuation or digits."""
    if not isinstance(value, str):
        _fail_contract()
    normalized = unicodedata.normalize("NFKC", value).translate(_DASH_TRANSLATION).casefold()
    normalized = " ".join(normalized.split())
    if not normalized:
        _fail_contract()
    return normalized


def _parse_sha256(value: object, *, private: bool) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _raise(private)
    return value


def _canonical_decimal(value: Decimal) -> str:
    return f"{_quantize_cent_exact(value, private=False, allow_negative=True):.2f}"


def _canonical_text(value: str) -> str:
    """Return text only when it has a stable UTF-8 scalar representation."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail_contract()
    return value


def _canonical_value(value: object, *, depth: int = 0) -> object:
    """Convert the limited private-artifact value domain into JSON-safe primitives."""
    if depth > MAX_CANONICAL_JSON_DEPTH:
        _fail_contract()
    if isinstance(value, Enum):
        return _canonical_value(value.value, depth=depth + 1)
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, datetime):
        _fail_contract()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return _canonical_text(value)
    if value is None or isinstance(value, (int, bool)):
        return value
    if isinstance(value, float):
        _fail_contract()
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                _fail_contract()
            result[_canonical_text(key)] = _canonical_value(nested, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item, depth=depth + 1) for item in value]
    _fail_contract()


def canonical_json_text(value: object) -> str:
    """Serialize an approved value with v1's stable UTF-8 JSON representation."""
    try:
        return json.dumps(
            _canonical_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError):
        _fail_contract()


def canonical_json_bytes(value: object) -> bytes:
    try:
        return canonical_json_text(value).encode("utf-8")
    except UnicodeEncodeError:
        _fail_contract()


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _reject_json_float(_: str) -> NoReturn:
    _fail_input()


def _reject_json_constant(_: str) -> NoReturn:
    _fail_input()


def _read_checked_private_file(path: Path) -> bytes:
    """Read one checked private regular file through a stable descriptor.

    The descriptor remains attached to the object that passed the post-open checks,
    so a path replacement after the checks cannot redirect the bytes subsequently
    parsed by a caller.  ``O_NOFOLLOW`` is used when the platform provides it; the
    link-segment and descriptor identity checks provide the same fail-closed result
    on Windows, where that flag is not available.
    """
    absolute = assert_private_path_allowed(path, require_file=True)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags | no_follow)
    except OSError:
        _fail_input()
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > PRIVATE_JSON_MAX_BYTES:
                _fail_input()
            _assert_no_link_segments(absolute)
            try:
                named = absolute.lstat()
            except OSError:
                _fail_input()
            if not os.path.samestat(opened, named):
                _fail_input()
            raw = handle.read(PRIVATE_JSON_MAX_BYTES + 1)
    except OSError:
        _fail_input()
    if len(raw) > PRIVATE_JSON_MAX_BYTES:
        _fail_input()
    return raw


def _load_canonical_json(path: Path) -> object:
    """Read one bounded canonical JSON document after the shared privacy gate."""
    try:
        raw = _read_checked_private_file(path)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        _fail_input()
    try:
        parsed: object = json.loads(
            text, parse_float=_reject_json_float, parse_constant=_reject_json_constant
        )
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
        _fail_input()
    try:
        canonical = canonical_json_text(parsed)
    except (ContractError, RecursionError):
        _fail_input()
    if text != canonical:
        _fail_input()
    return parsed


def _object_fields(value: object, expected: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail_input()
    if set(value) != expected or any(not isinstance(key, str) for key in value):
        _fail_input()
    return value


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value:
        _fail_input()
    return value


def _required_list(value: object) -> list[object]:
    if not isinstance(value, list):
        _fail_input()
    return value


def _contract_fields(value: object, expected: frozenset[str]) -> Mapping[str, object]:
    """Validate an exact in-memory serialized contract without input redaction."""
    if not isinstance(value, Mapping):
        _fail_contract()
    if set(value) != expected or any(not isinstance(key, str) for key in value):
        _fail_contract()
    return value


def _require_confidence(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
        _fail_contract()
    return value


def _assert_safe_windows_path_component(component: str) -> None:
    """Reject Windows device, stream, and short-name aliases before Git access."""
    if (
        not component
        or any(character in _WINDOWS_FORBIDDEN_COMPONENT_CHARACTERS for character in component)
        or any(ord(character) < 32 for character in component)
        or any(0xD800 <= ord(character) <= 0xDFFF for character in component)
    ):
        _fail_input()
    canonical_component = component.rstrip(" .")
    if not canonical_component or canonical_component != component:
        _fail_input()
    device_stem = unicodedata.normalize("NFKC", canonical_component).partition(".")[0].upper()
    if device_stem in _WINDOWS_RESERVED_DEVICE_NAMES:
        _fail_input()


def _assert_no_windows_stream_or_device_path(path: Path) -> None:
    """Reject ADS and device-namespace aliases for every private-path admission."""
    path_text = str(path).replace("/", "\\")
    candidate = PureWindowsPath(path_text)
    if path_text.startswith("\\\\") or candidate.drive.startswith("\\\\"):
        _fail_input()
    for component in candidate.parts:
        if component != candidate.anchor:
            _assert_safe_windows_path_component(component)


def _relative_path_text(value: object) -> str:
    """Canonicalize one safe manifest-relative path to forward-slash components."""
    text = _required_text(value)
    windows_candidate = PureWindowsPath(text)
    canonical_text = text.replace("\\", "/")
    posix_candidate = PurePosixPath(canonical_text)
    parts = canonical_text.split("/")
    if (
        windows_candidate.is_absolute()
        or bool(windows_candidate.drive)
        or bool(windows_candidate.root)
        or posix_candidate.is_absolute()
        or not posix_candidate.parts
        or any(part in {"", ".", ".."} for part in parts)
    ):
        _fail_input()
    for part in parts:
        _assert_safe_windows_path_component(part)
    return "/".join(parts)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    except OSError:
        _fail_input()
    return bool(attributes & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _assert_no_link_segments(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            is_symlink = current.is_symlink()
        except OSError:
            _fail_input()
        if is_symlink or _is_reparse_point(current):
            _fail_input()


def _has_git_metadata_at_or_above(path: Path) -> bool:
    """Detect a worktree marker without treating a failed Git invocation as external."""
    for candidate in (path, *path.parents):
        marker = candidate / ".git"
        try:
            if marker.exists() or marker.is_symlink() or _is_reparse_point(marker):
                return True
        except OSError:
            _fail_input()
    return False


def _sanitized_git_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a Git environment isolated from caller-selected repositories/config.

    The private-path policy must interrogate the worktree containing the candidate,
    rather than a repository or ignore policy selected through inherited environment
    or global Git configuration. The two injected variables deliberately direct Git
    to its null configuration source and disable system configuration; the command
    itself also pins ``core.excludesFile`` to the null device.
    """
    source = os.environ if environment is None else environment
    result = {
        key: value
        for key, value in source.items()
        if not key.upper().startswith("GIT_")
        and key.upper() not in _EXTERNAL_GIT_CONFIGURATION_KEYS
    }
    result["GIT_CONFIG_NOSYSTEM"] = "1"
    result["GIT_CONFIG_GLOBAL"] = os.devnull
    return result


def _git_root_for(path: Path) -> Path | None:
    """Find an enclosing worktree without surfacing paths or Git stderr."""
    start = path if path.is_dir() else path.parent
    for candidate in (start, *start.parents):
        try:
            completed = subprocess.run(
                [
                    "git",
                    "-c",
                    f"core.excludesFile={os.devnull}",
                    "-C",
                    str(candidate),
                    "rev-parse",
                    "--show-toplevel",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=_sanitized_git_environment(),
            )
        except OSError:
            if _has_git_metadata_at_or_above(candidate):
                _fail_input()
            return None
        if completed.returncode == 0:
            output = completed.stdout.strip()
            if output:
                return Path(output)
        if _has_git_metadata_at_or_above(candidate):
            _fail_input()
    return None


def _git_returncode(root: Path, arguments: list[str], *, expected: frozenset[int]) -> int:
    try:
        completed = subprocess.run(
            ["git", "-c", f"core.excludesFile={os.devnull}", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=_sanitized_git_environment(),
        )
    except OSError:
        _fail_input()
    if completed.returncode not in expected:
        _fail_input()
    return completed.returncode


def _git_has_casefold_tracked_path(root: Path, relative: Path) -> bool:
    """Reject a Windows casing alias of any tracked path before opening it."""
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"core.excludesFile={os.devnull}",
                "-C",
                str(root),
                "ls-files",
                "-z",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=_sanitized_git_environment(),
        )
    except (OSError, UnicodeDecodeError):
        _fail_input()
    if completed.returncode != 0:
        _fail_input()
    candidate = relative.as_posix().casefold()
    return any(
        tracked.casefold() == candidate for tracked in completed.stdout.split("\0") if tracked
    )


def assert_private_path_allowed(
    path: Path,
    *,
    require_file: bool = False,
    require_directory: bool = False,
    allow_missing: bool = False,
) -> Path:
    """Apply the shared private-path gate before a source read or output write.

    A path inside a Git worktree must be untracked and match a Git-ignore rule.  A
    path outside a worktree is allowed after ordinary type and link checks.  The
    exception deliberately contains no supplied path or private value.
    """
    if require_file and require_directory:
        _fail_input()
    _assert_no_windows_stream_or_device_path(path)
    absolute = Path(os.path.abspath(path))
    _assert_no_windows_stream_or_device_path(absolute)
    _assert_no_link_segments(absolute)
    exists = absolute.exists()
    if not exists and not allow_missing:
        _fail_input()
    if exists:
        try:
            if require_file and not absolute.is_file():
                _fail_input()
            if require_directory and not absolute.is_dir():
                _fail_input()
        except OSError:
            _fail_input()

    root = _git_root_for(absolute)
    if root is not None:
        root_absolute = Path(os.path.abspath(root))
        try:
            relative = absolute.relative_to(root_absolute)
        except ValueError:
            _fail_input()
        relative_text = relative.as_posix()
        tracked = _git_returncode(
            root_absolute,
            ["ls-files", "--error-unmatch", "--", relative_text],
            expected=frozenset({0, 1}),
        )
        ignored = _git_returncode(
            root_absolute,
            ["check-ignore", "--no-index", "--quiet", "--", relative_text],
            expected=frozenset({0, 1}),
        )
        if tracked != 1 or _git_has_casefold_tracked_path(root_absolute, relative) or ignored != 0:
            _fail_input()
    return absolute


def resolve_private_relative_path(
    base: Path, relative_path: str, *, require_file: bool = True
) -> Path:
    """Resolve one manifest-relative private path without escaping or traversing links."""
    if not isinstance(relative_path, str):
        _fail_input()
    text = _relative_path_text(relative_path)
    _assert_no_windows_stream_or_device_path(base)
    base_absolute = Path(os.path.abspath(base))
    _assert_no_windows_stream_or_device_path(base_absolute)
    _assert_no_link_segments(base_absolute)
    try:
        if not base_absolute.is_dir():
            _fail_input()
    except OSError:
        _fail_input()
    candidate = Path(os.path.abspath(base_absolute / Path(text)))
    try:
        candidate.relative_to(base_absolute)
    except ValueError:
        _fail_input()
    return assert_private_path_allowed(candidate, require_file=require_file)


@dataclass(frozen=True)
class BoundingBox:
    left: Decimal
    top: Decimal
    right: Decimal
    bottom: Decimal

    def __post_init__(self) -> None:
        values = tuple(
            _parse_unit_position(value) for value in (self.left, self.top, self.right, self.bottom)
        )
        if values[0] >= values[2] or values[1] >= values[3]:
            _fail_contract()
        object.__setattr__(self, "left", values[0])
        object.__setattr__(self, "top", values[1])
        object.__setattr__(self, "right", values[2])
        object.__setattr__(self, "bottom", values[3])

    def to_dict(self) -> dict[str, str]:
        return {
            "left": _position_text(self.left),
            "top": _position_text(self.top),
            "right": _position_text(self.right),
            "bottom": _position_text(self.bottom),
        }

    @classmethod
    def from_dict(cls, value: object) -> BoundingBox:
        item = _contract_fields(value, frozenset({"left", "top", "right", "bottom"}))
        return cls(
            left=_parse_unit_position(item["left"]),
            top=_parse_unit_position(item["top"]),
            right=_parse_unit_position(item["right"]),
            bottom=_parse_unit_position(item["bottom"]),
        )


@dataclass(frozen=True)
class SafeSourceLocator:
    document_ordinal: int
    page_number: int
    table_ordinal: int
    row_ordinal: int
    row_box: BoundingBox

    def __post_init__(self) -> None:
        for value in (
            self.document_ordinal,
            self.page_number,
            self.table_ordinal,
            self.row_ordinal,
        ):
            _require_positive_int(value)
        if not isinstance(self.row_box, BoundingBox):
            _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {
            "document_ordinal": self.document_ordinal,
            "page_number": self.page_number,
            "table_ordinal": self.table_ordinal,
            "row_ordinal": self.row_ordinal,
            "row_box": self.row_box.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> SafeSourceLocator:
        item = _contract_fields(
            value,
            frozenset(
                {"document_ordinal", "page_number", "table_ordinal", "row_ordinal", "row_box"}
            ),
        )
        return cls(
            document_ordinal=_require_positive_int(item["document_ordinal"]),
            page_number=_require_positive_int(item["page_number"]),
            table_ordinal=_require_positive_int(item["table_ordinal"]),
            row_ordinal=_require_positive_int(item["row_ordinal"]),
            row_box=BoundingBox.from_dict(item["row_box"]),
        )


@dataclass(frozen=True)
class PositionedToken:
    page_number: int
    box: BoundingBox
    text: str
    extraction_method: ExtractionMethod
    confidence: int

    def __post_init__(self) -> None:
        _require_positive_int(self.page_number)
        if not isinstance(self.box, BoundingBox):
            _fail_contract()
        if not isinstance(self.text, str) or not self.text.strip():
            _fail_contract()
        if (
            not isinstance(self.confidence, int)
            or isinstance(self.confidence, bool)
            or not 0 <= self.confidence <= 100
        ):
            _fail_contract()
        _require_enum(ExtractionMethod, self.extraction_method)
        if self.extraction_method is ExtractionMethod.NATIVE and self.confidence != 100:
            _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "box": self.box.to_dict(),
            "text": self.text,
            "extraction_method": self.extraction_method.value,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, value: object) -> PositionedToken:
        item = _contract_fields(
            value, frozenset({"page_number", "box", "text", "extraction_method", "confidence"})
        )
        return cls(
            page_number=_require_positive_int(item["page_number"]),
            box=BoundingBox.from_dict(item["box"]),
            text=_require_nonblank_text(item["text"]),
            extraction_method=_parse_enum(
                ExtractionMethod, item["extraction_method"], private=False
            ),
            confidence=_require_confidence(item["confidence"]),
        )


@dataclass(frozen=True)
class ActivityColumnBand:
    """One horizontal activity-table band, defined by token centroids."""

    column: ActivityColumn
    left: Decimal
    right: Decimal
    header_box: BoundingBox
    header_token_ordinals: tuple[int, ...]

    def __post_init__(self) -> None:
        _require_enum(ActivityColumn, self.column)
        left = _parse_unit_position(self.left)
        right = _parse_unit_position(self.right)
        if (
            left >= right
            or not isinstance(self.header_box, BoundingBox)
            or not isinstance(self.header_token_ordinals, tuple)
            or not self.header_token_ordinals
        ):
            _fail_contract()
        if any(
            not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1
            for ordinal in self.header_token_ordinals
        ):
            _fail_contract()
        if tuple(sorted(self.header_token_ordinals)) != self.header_token_ordinals or len(
            set(self.header_token_ordinals)
        ) != len(self.header_token_ordinals):
            _fail_contract()
        object.__setattr__(self, "left", left)
        object.__setattr__(self, "right", right)

    def contains_token_centroid(self, token: PositionedToken) -> bool:
        if not isinstance(token, PositionedToken):
            _fail_contract()
        return self.contains_box_centroid(token.box)

    def contains_box_centroid(self, box: BoundingBox) -> bool:
        if not isinstance(box, BoundingBox):
            _fail_contract()
        centroid = (box.left + box.right) / Decimal(2)
        return self.left <= centroid <= self.right

    def to_dict(self) -> dict[str, object]:
        return {
            "column": self.column.value,
            "left": _position_text(self.left),
            "right": _position_text(self.right),
            "header_box": self.header_box.to_dict(),
            "header_token_ordinals": list(self.header_token_ordinals),
        }

    @classmethod
    def from_dict(cls, value: object) -> ActivityColumnBand:
        item = _contract_fields(
            value,
            frozenset({"column", "left", "right", "header_box", "header_token_ordinals"}),
        )
        ordinals = item["header_token_ordinals"]
        if not isinstance(ordinals, list):
            _fail_contract()
        return cls(
            column=_parse_enum(ActivityColumn, item["column"], private=False),
            left=_parse_unit_position(item["left"]),
            right=_parse_unit_position(item["right"]),
            header_box=BoundingBox.from_dict(item["header_box"]),
            header_token_ordinals=tuple(_require_positive_int(ordinal) for ordinal in ordinals),
        )


@dataclass(frozen=True)
class ActivityRowEvidence:
    """One recognized, non-overlapping activity row on a positioned page."""

    row_ordinal: int
    row_box: BoundingBox

    def __post_init__(self) -> None:
        _require_positive_int(self.row_ordinal)
        if not isinstance(self.row_box, BoundingBox):
            _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {"row_ordinal": self.row_ordinal, "row_box": self.row_box.to_dict()}

    @classmethod
    def from_dict(cls, value: object) -> ActivityRowEvidence:
        item = _contract_fields(value, frozenset({"row_ordinal", "row_box"}))
        return cls(
            row_ordinal=_require_positive_int(item["row_ordinal"]),
            row_box=BoundingBox.from_dict(item["row_box"]),
        )


def _boxes_overlap(first: BoundingBox, second: BoundingBox) -> bool:
    return not (
        first.right <= second.left
        or second.right <= first.left
        or first.bottom <= second.top
        or second.bottom <= first.top
    )


def _box_contains_box(outer: BoundingBox, inner: BoundingBox) -> bool:
    return (
        inner.left >= outer.left
        and inner.top >= outer.top
        and inner.right <= outer.right
        and inner.bottom <= outer.bottom
    )


def _boxes_horizontally_overlap(first: BoundingBox, second: BoundingBox) -> bool:
    return first.left < second.right and second.left < first.right


@dataclass(frozen=True)
class ActivityStatusControl:
    """A recognized posted/pending label bound to one contiguous table-row section."""

    status: TransactionStatus
    box: BoundingBox
    token_ordinals: tuple[int, ...]
    row_ordinals: tuple[int, ...]

    def __post_init__(self) -> None:
        _require_enum(TransactionStatus, self.status)
        if (
            not isinstance(self.box, BoundingBox)
            or not isinstance(self.token_ordinals, tuple)
            or not self.token_ordinals
            or not isinstance(self.row_ordinals, tuple)
            or not self.row_ordinals
        ):
            _fail_contract()
        for ordinals in (self.token_ordinals, self.row_ordinals):
            if any(
                not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1
                for ordinal in ordinals
            ):
                _fail_contract()
            if tuple(sorted(ordinals)) != ordinals or len(set(ordinals)) != len(ordinals):
                _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "box": self.box.to_dict(),
            "token_ordinals": list(self.token_ordinals),
            "row_ordinals": list(self.row_ordinals),
        }

    @classmethod
    def from_dict(cls, value: object) -> ActivityStatusControl:
        item = _contract_fields(
            value, frozenset({"status", "box", "token_ordinals", "row_ordinals"})
        )
        ordinals = item["token_ordinals"]
        row_ordinals = item["row_ordinals"]
        if not isinstance(ordinals, list) or not isinstance(row_ordinals, list):
            _fail_contract()
        return cls(
            status=_parse_enum(TransactionStatus, item["status"], private=False),
            box=BoundingBox.from_dict(item["box"]),
            token_ordinals=tuple(_require_positive_int(ordinal) for ordinal in ordinals),
            row_ordinals=tuple(_require_positive_int(ordinal) for ordinal in row_ordinals),
        )


@dataclass(frozen=True)
class BalanceRowEvidence:
    """A recognized, compact source row that supplies one dated balance fact."""

    locator: SafeSourceLocator
    date_token_ordinals: tuple[int, ...]
    balance_token_ordinals: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.locator, SafeSourceLocator):
            _fail_contract()
        if self.locator.row_box.bottom - self.locator.row_box.top > MAX_BALANCE_ROW_HEIGHT:
            _fail_contract()
        for ordinals in (self.date_token_ordinals, self.balance_token_ordinals):
            if not isinstance(ordinals, tuple) or not ordinals:
                _fail_contract()
            if any(
                not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1
                for ordinal in ordinals
            ):
                _fail_contract()
            if tuple(sorted(ordinals)) != ordinals or len(set(ordinals)) != len(ordinals):
                _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {
            "locator": self.locator.to_dict(),
            "date_token_ordinals": list(self.date_token_ordinals),
            "balance_token_ordinals": list(self.balance_token_ordinals),
        }

    @classmethod
    def from_dict(cls, value: object) -> BalanceRowEvidence:
        item = _contract_fields(
            value, frozenset({"locator", "date_token_ordinals", "balance_token_ordinals"})
        )
        date_ordinals = item["date_token_ordinals"]
        balance_ordinals = item["balance_token_ordinals"]
        if not isinstance(date_ordinals, list) or not isinstance(balance_ordinals, list):
            _fail_contract()
        return cls(
            locator=SafeSourceLocator.from_dict(item["locator"]),
            date_token_ordinals=tuple(_require_positive_int(ordinal) for ordinal in date_ordinals),
            balance_token_ordinals=tuple(
                _require_positive_int(ordinal) for ordinal in balance_ordinals
            ),
        )


@dataclass(frozen=True)
class BalanceControlEvidence:
    """Typed balance labels retained inside the exact source row they qualify."""

    locator: SafeSourceLocator
    kind: BalanceKind
    boundary: BalanceBoundary
    includes_pending: bool
    control_box: BoundingBox
    kind_token_ordinals: tuple[int, ...]
    boundary_token_ordinals: tuple[int, ...]
    includes_pending_token_ordinals: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.locator, SafeSourceLocator):
            _fail_contract()
        _require_enum(BalanceKind, self.kind)
        _require_enum(BalanceBoundary, self.boundary)
        if not isinstance(self.includes_pending, bool) or not isinstance(
            self.control_box, BoundingBox
        ):
            _fail_contract()
        if not _box_contains_box(self.locator.row_box, self.control_box):
            _fail_contract()
        for ordinals in (
            self.kind_token_ordinals,
            self.boundary_token_ordinals,
            self.includes_pending_token_ordinals,
        ):
            if not isinstance(ordinals, tuple) or not ordinals:
                _fail_contract()
            if any(
                not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1
                for ordinal in ordinals
            ):
                _fail_contract()
            if tuple(sorted(ordinals)) != ordinals or len(set(ordinals)) != len(ordinals):
                _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {
            "locator": self.locator.to_dict(),
            "kind": self.kind.value,
            "boundary": self.boundary.value,
            "includes_pending": self.includes_pending,
            "control_box": self.control_box.to_dict(),
            "kind_token_ordinals": list(self.kind_token_ordinals),
            "boundary_token_ordinals": list(self.boundary_token_ordinals),
            "includes_pending_token_ordinals": list(self.includes_pending_token_ordinals),
        }

    @classmethod
    def from_dict(cls, value: object) -> BalanceControlEvidence:
        item = _contract_fields(
            value,
            frozenset(
                {
                    "locator",
                    "kind",
                    "boundary",
                    "includes_pending",
                    "control_box",
                    "kind_token_ordinals",
                    "boundary_token_ordinals",
                    "includes_pending_token_ordinals",
                }
            ),
        )
        kind_ordinals = item["kind_token_ordinals"]
        boundary_ordinals = item["boundary_token_ordinals"]
        pending_ordinals = item["includes_pending_token_ordinals"]
        if (
            not isinstance(kind_ordinals, list)
            or not isinstance(boundary_ordinals, list)
            or not isinstance(pending_ordinals, list)
        ):
            _fail_contract()
        return cls(
            locator=SafeSourceLocator.from_dict(item["locator"]),
            kind=_parse_enum(BalanceKind, item["kind"], private=False),
            boundary=_parse_enum(BalanceBoundary, item["boundary"], private=False),
            includes_pending=_require_bool(item["includes_pending"]),
            control_box=BoundingBox.from_dict(item["control_box"]),
            kind_token_ordinals=tuple(_require_positive_int(ordinal) for ordinal in kind_ordinals),
            boundary_token_ordinals=tuple(
                _require_positive_int(ordinal) for ordinal in boundary_ordinals
            ),
            includes_pending_token_ordinals=tuple(
                _require_positive_int(ordinal) for ordinal in pending_ordinals
            ),
        )


@dataclass(frozen=True)
class ActivityTableEvidence:
    """Recognized activity-table header and non-overlapping financial columns."""

    table_ordinal: int
    header_box: BoundingBox
    columns: tuple[ActivityColumnBand, ...]
    rows: tuple[ActivityRowEvidence, ...]
    status_controls: tuple[ActivityStatusControl, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.table_ordinal)
        if (
            not isinstance(self.header_box, BoundingBox)
            or not isinstance(self.columns, tuple)
            or not isinstance(self.rows, tuple)
            or not isinstance(self.status_controls, tuple)
        ):
            _fail_contract()
        if any(not isinstance(column, ActivityColumnBand) for column in self.columns):
            _fail_contract()
        if any(not isinstance(row, ActivityRowEvidence) for row in self.rows):
            _fail_contract()
        if any(not isinstance(control, ActivityStatusControl) for control in self.status_controls):
            _fail_contract()
        if tuple(column.column for column in self.columns) != _ACTIVITY_COLUMNS:
            _fail_contract()
        if any(
            first.right >= second.left
            for first, second in zip(self.columns, self.columns[1:], strict=False)
        ):
            _fail_contract()
        if any(
            not _box_contains_box(self.header_box, column.header_box)
            or not column.contains_box_centroid(column.header_box)
            for column in self.columns
        ):
            _fail_contract()
        row_ordinals = [row.row_ordinal for row in self.rows]
        if row_ordinals != list(range(1, len(self.rows) + 1)):
            _fail_contract()
        if (
            tuple(
                sorted(
                    self.rows,
                    key=lambda row: (
                        row.row_box.top,
                        row.row_box.left,
                        row.row_box.bottom,
                        row.row_box.right,
                    ),
                )
            )
            != self.rows
        ):
            _fail_contract()
        if any(
            _boxes_overlap(first.row_box, second.row_box)
            for index, first in enumerate(self.rows)
            for second in self.rows[index + 1 :]
        ):
            _fail_contract()
        if any(_boxes_overlap(self.header_box, row.row_box) for row in self.rows):
            _fail_contract()
        if any(
            _boxes_overlap(control.box, row.row_box)
            for control in self.status_controls
            for row in self.rows
        ):
            _fail_contract()
        if any(
            _boxes_overlap(first.box, second.box)
            for index, first in enumerate(self.status_controls)
            for second in self.status_controls[index + 1 :]
        ):
            _fail_contract()
        if (
            tuple(
                sorted(
                    self.status_controls,
                    key=lambda control: (
                        control.box.top,
                        control.box.left,
                        control.box.bottom,
                        control.box.right,
                    ),
                )
            )
            != self.status_controls
        ):
            _fail_contract()
        if self.rows:
            controlled_row_ordinals = [
                row_ordinal
                for control in self.status_controls
                for row_ordinal in control.row_ordinals
            ]
            if sorted(controlled_row_ordinals) != list(range(1, len(self.rows) + 1)):
                _fail_contract()
        elif self.status_controls:
            _fail_contract()
        for control in self.status_controls:
            if control.row_ordinals != tuple(
                range(control.row_ordinals[0], control.row_ordinals[-1] + 1)
            ):
                _fail_contract()
            if any(
                control.box.bottom > self.row(row_ordinal).row_box.top
                or not _boxes_horizontally_overlap(control.box, self.row(row_ordinal).row_box)
                for row_ordinal in control.row_ordinals
            ):
                _fail_contract()

    def band(self, column: ActivityColumn) -> ActivityColumnBand:
        _require_enum(ActivityColumn, column)
        for candidate in self.columns:
            if candidate.column is column:
                return candidate
        _fail_contract()

    def row(self, row_ordinal: int) -> ActivityRowEvidence:
        _require_positive_int(row_ordinal)
        for candidate in self.rows:
            if candidate.row_ordinal == row_ordinal:
                return candidate
        _fail_contract()

    def status_control_for_row(self, row_ordinal: int) -> ActivityStatusControl:
        """Return the explicit owner only when it is the nearest source status label."""
        _require_positive_int(row_ordinal)
        row = self.row(row_ordinal)
        owners = tuple(
            control for control in self.status_controls if row_ordinal in control.row_ordinals
        )
        if len(owners) != 1:
            _fail_contract()
        candidates = tuple(
            control
            for control in self.status_controls
            if control.box.bottom <= row.row_box.top
            and _boxes_horizontally_overlap(control.box, row.row_box)
        )
        if not candidates:
            _fail_contract()
        nearest_bottom = max(control.box.bottom for control in candidates)
        nearest = tuple(control for control in candidates if control.box.bottom == nearest_bottom)
        if len(nearest) != 1 or owners[0] is not nearest[0]:
            _fail_contract()
        return owners[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "table_ordinal": self.table_ordinal,
            "header_box": self.header_box.to_dict(),
            "columns": [column.to_dict() for column in self.columns],
            "rows": [row.to_dict() for row in self.rows],
            "status_controls": [control.to_dict() for control in self.status_controls],
        }

    @classmethod
    def from_dict(cls, value: object) -> ActivityTableEvidence:
        item = _contract_fields(
            value,
            frozenset({"table_ordinal", "header_box", "columns", "rows", "status_controls"}),
        )
        columns = item["columns"]
        rows = item["rows"]
        status_controls = item["status_controls"]
        if (
            not isinstance(columns, list)
            or not isinstance(rows, list)
            or not isinstance(status_controls, list)
        ):
            _fail_contract()
        return cls(
            table_ordinal=_require_positive_int(item["table_ordinal"]),
            header_box=BoundingBox.from_dict(item["header_box"]),
            columns=tuple(ActivityColumnBand.from_dict(column) for column in columns),
            rows=tuple(ActivityRowEvidence.from_dict(row) for row in rows),
            status_controls=tuple(
                ActivityStatusControl.from_dict(control) for control in status_controls
            ),
        )


@dataclass(frozen=True)
class ParseEvidence:
    """One fact-field's ordered references into a page's positioned token sequence.

    ``token_ordinals`` name source-row tokens and must be positioned inside the
    fact's locator. Optional context ordinals retain the supporting table header or
    page label when a control fact is derived from it. Ordinals are one-based so an
    empty or missing row reference cannot be represented as a valid index. Token
    text remains stored once in ``PageEvidence`` rather than duplicated per fact.
    """

    field: EvidenceField
    token_ordinals: tuple[int, ...]
    context_token_ordinals: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _require_enum(EvidenceField, self.field)
        for ordinals, required in (
            (self.token_ordinals, True),
            (self.context_token_ordinals, False),
        ):
            if not isinstance(ordinals, tuple) or (required and not ordinals):
                _fail_contract()
            if any(
                not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1
                for ordinal in ordinals
            ):
                _fail_contract()
            if tuple(sorted(ordinals)) != ordinals or len(set(ordinals)) != len(ordinals):
                _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {
            "field": self.field.value,
            "token_ordinals": list(self.token_ordinals),
            "context_token_ordinals": list(self.context_token_ordinals),
        }

    @classmethod
    def from_dict(cls, value: object) -> ParseEvidence:
        item = _contract_fields(
            value, frozenset({"field", "token_ordinals", "context_token_ordinals"})
        )
        ordinals = item["token_ordinals"]
        context_ordinals = item["context_token_ordinals"]
        if not isinstance(ordinals, list) or not isinstance(context_ordinals, list):
            _fail_contract()
        return cls(
            field=_parse_enum(EvidenceField, item["field"], private=False),
            token_ordinals=tuple(_require_positive_int(ordinal) for ordinal in ordinals),
            context_token_ordinals=tuple(
                _require_positive_int(ordinal) for ordinal in context_ordinals
            ),
        )


def _require_parse_evidence(
    value: object, expected_fields: tuple[EvidenceField, ...]
) -> tuple[ParseEvidence, ...]:
    """Require one ordered, non-overlapping evidence record for every needed field."""
    if not isinstance(value, tuple) or any(not isinstance(item, ParseEvidence) for item in value):
        _fail_contract()
    if tuple(item.field for item in value) != expected_fields:
        _fail_contract()
    return value


@dataclass(frozen=True)
class DocumentSpec:
    account_role: AccountRole
    document_kind: DocumentKind
    relative_path: str

    def __post_init__(self) -> None:
        _require_enum(AccountRole, self.account_role)
        _require_enum(DocumentKind, self.document_kind)
        object.__setattr__(self, "relative_path", _relative_path_text(self.relative_path))

    def to_dict(self) -> dict[str, str]:
        return {
            "account_role": self.account_role.value,
            "document_kind": self.document_kind.value,
            "relative_path": self.relative_path,
        }

    @classmethod
    def from_dict(cls, value: object) -> DocumentSpec:
        item = _contract_fields(
            value, frozenset({"account_role", "document_kind", "relative_path"})
        )
        return cls(
            account_role=_parse_enum(AccountRole, item["account_role"], private=False),
            document_kind=_parse_enum(DocumentKind, item["document_kind"], private=False),
            relative_path=_relative_path_text(item["relative_path"]),
        )


@dataclass(frozen=True)
class InputManifest:
    schema_version: int
    reporting_start_date: date
    as_of_date: date
    budget_fiscal_year: str
    cash_basis: CashBasis
    documents: tuple[DocumentSpec, ...]
    rules_relative_path: str

    def __post_init__(self) -> None:
        _require_positive_int(self.schema_version)
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            _fail_contract()
        _require_calendar_date(self.reporting_start_date)
        _require_calendar_date(self.as_of_date)
        if self.as_of_date < self.reporting_start_date:
            _fail_contract()
        if (
            not isinstance(self.budget_fiscal_year, str)
            or _FISCAL_YEAR_RE.fullmatch(self.budget_fiscal_year) is None
        ):
            _fail_contract()
        if self.cash_basis is not CashBasis.AVAILABLE_INCLUDING_PENDING or not self.documents:
            _fail_contract()
        if not isinstance(self.documents, tuple) or any(
            not isinstance(document, DocumentSpec) for document in self.documents
        ):
            _fail_contract()
        object.__setattr__(
            self, "rules_relative_path", _relative_path_text(self.rules_relative_path)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "reporting_start_date": self.reporting_start_date.isoformat(),
            "as_of_date": self.as_of_date.isoformat(),
            "budget_fiscal_year": self.budget_fiscal_year,
            "cash_basis": self.cash_basis.value,
            "documents": [document.to_dict() for document in self.documents],
            "rules_relative_path": self.rules_relative_path,
        }

    @classmethod
    def from_dict(cls, value: object) -> InputManifest:
        item = _contract_fields(
            value,
            frozenset(
                {
                    "schema_version",
                    "reporting_start_date",
                    "as_of_date",
                    "budget_fiscal_year",
                    "cash_basis",
                    "documents",
                    "rules_relative_path",
                }
            ),
        )
        documents = item["documents"]
        if not isinstance(documents, list):
            _fail_contract()
        return cls(
            schema_version=_require_positive_int(item["schema_version"]),
            reporting_start_date=parse_iso_date(item["reporting_start_date"]),
            as_of_date=parse_iso_date(item["as_of_date"]),
            budget_fiscal_year=_require_nonblank_text(item["budget_fiscal_year"]),
            cash_basis=_parse_enum(CashBasis, item["cash_basis"], private=False),
            documents=tuple(DocumentSpec.from_dict(document) for document in documents),
            rules_relative_path=_relative_path_text(item["rules_relative_path"]),
        )


@dataclass(frozen=True)
class BalanceObservation:
    account_role: AccountRole
    amount: Decimal
    observed_on: date
    boundary: BalanceBoundary
    kind: BalanceKind
    includes_pending: bool
    source_sha256: str
    locator: SafeSourceLocator
    extraction_method: ExtractionMethod
    parse_evidence: tuple[ParseEvidence, ...]

    def __post_init__(self) -> None:
        _require_enum(AccountRole, self.account_role)
        _require_calendar_date(self.observed_on)
        _require_enum(BalanceBoundary, self.boundary)
        _require_enum(BalanceKind, self.kind)
        if not isinstance(self.locator, SafeSourceLocator):
            _fail_contract()
        _require_enum(ExtractionMethod, self.extraction_method)
        _require_parse_evidence(self.parse_evidence, _BALANCE_EVIDENCE_FIELDS)
        if not isinstance(self.includes_pending, bool):
            _fail_contract()
        object.__setattr__(self, "amount", parse_nonnegative_money(self.amount))
        _parse_sha256(self.source_sha256, private=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "account_role": self.account_role.value,
            "amount": money_text(self.amount),
            "observed_on": self.observed_on.isoformat(),
            "boundary": self.boundary.value,
            "kind": self.kind.value,
            "includes_pending": self.includes_pending,
            "source_sha256": self.source_sha256,
            "locator": self.locator.to_dict(),
            "extraction_method": self.extraction_method.value,
            "parse_evidence": [evidence.to_dict() for evidence in self.parse_evidence],
        }

    @classmethod
    def from_dict(cls, value: object) -> BalanceObservation:
        item = _contract_fields(
            value,
            frozenset(
                {
                    "account_role",
                    "amount",
                    "observed_on",
                    "boundary",
                    "kind",
                    "includes_pending",
                    "source_sha256",
                    "locator",
                    "extraction_method",
                    "parse_evidence",
                }
            ),
        )
        evidence = item["parse_evidence"]
        if not isinstance(evidence, list):
            _fail_contract()
        return cls(
            account_role=_parse_enum(AccountRole, item["account_role"], private=False),
            amount=parse_nonnegative_money(item["amount"]),
            observed_on=parse_iso_date(item["observed_on"]),
            boundary=_parse_enum(BalanceBoundary, item["boundary"], private=False),
            kind=_parse_enum(BalanceKind, item["kind"], private=False),
            includes_pending=_require_bool(item["includes_pending"]),
            source_sha256=_parse_sha256(item["source_sha256"], private=False),
            locator=SafeSourceLocator.from_dict(item["locator"]),
            extraction_method=_parse_enum(
                ExtractionMethod, item["extraction_method"], private=False
            ),
            parse_evidence=tuple(ParseEvidence.from_dict(item) for item in evidence),
        )


def build_source_row_id(parser_version: str, source_sha256: str, locator: SafeSourceLocator) -> str:
    """Build an identity for one physical source row, independent of manifest order."""
    _require_supported_parser_version(parser_version)
    _parse_sha256(source_sha256, private=False)
    if not isinstance(locator, SafeSourceLocator):
        _fail_contract()
    return canonical_sha256(
        {
            "parser_version": parser_version,
            "source_sha256": source_sha256,
            "page_number": locator.page_number,
            "table_ordinal": locator.table_ordinal,
            "row_ordinal": locator.row_ordinal,
            "row_box": locator.row_box.to_dict(),
        }
    )


def build_semantic_key(
    account_role: AccountRole,
    effective_date: date,
    status: TransactionStatus,
    direction: Direction,
    magnitude: Decimal,
    normalized_description: str,
    occurrence_ordinal: int,
) -> str:
    _require_enum(AccountRole, account_role)
    _require_calendar_date(effective_date)
    _require_enum(TransactionStatus, status)
    _require_enum(Direction, direction)
    if normalize_description(normalized_description) != normalized_description:
        _fail_contract()
    if (
        not isinstance(occurrence_ordinal, int)
        or isinstance(occurrence_ordinal, bool)
        or occurrence_ordinal < 1
    ):
        _fail_contract()
    return canonical_sha256(
        {
            "account_role": account_role.value,
            "effective_date": effective_date.isoformat(),
            "status": status.value,
            "direction": direction.value,
            "magnitude": money_text(parse_positive_money(magnitude)),
            "normalized_description": normalized_description,
            "occurrence_ordinal": occurrence_ordinal,
        }
    )


@dataclass(frozen=True)
class NormalizedTransaction:
    account_role: AccountRole
    effective_date: date
    status: TransactionStatus
    direction: Direction
    magnitude: Decimal
    normalized_description: str
    occurrence_ordinal: int
    source_sha256: str
    locator: SafeSourceLocator
    extraction_method: ExtractionMethod
    parser_version: str
    parse_evidence: tuple[ParseEvidence, ...]
    source_row_id: str | None = None
    semantic_key: str | None = None

    def __post_init__(self) -> None:
        _require_enum(AccountRole, self.account_role)
        _require_calendar_date(self.effective_date)
        _require_enum(TransactionStatus, self.status)
        _require_enum(Direction, self.direction)
        if not isinstance(self.locator, SafeSourceLocator):
            _fail_contract()
        _require_enum(ExtractionMethod, self.extraction_method)
        _require_supported_parser_version(self.parser_version)
        _require_parse_evidence(self.parse_evidence, _TRANSACTION_EVIDENCE_FIELDS)
        object.__setattr__(self, "magnitude", parse_positive_money(self.magnitude))
        if normalize_description(self.normalized_description) != self.normalized_description:
            _fail_contract()
        if (
            not isinstance(self.occurrence_ordinal, int)
            or isinstance(self.occurrence_ordinal, bool)
            or self.occurrence_ordinal < 1
        ):
            _fail_contract()
        _parse_sha256(self.source_sha256, private=False)
        expected_row_id = build_source_row_id(self.parser_version, self.source_sha256, self.locator)
        expected_semantic_key = build_semantic_key(
            self.account_role,
            self.effective_date,
            self.status,
            self.direction,
            self.magnitude,
            self.normalized_description,
            self.occurrence_ordinal,
        )
        if self.source_row_id is not None and self.source_row_id != expected_row_id:
            _fail_contract()
        if self.semantic_key is not None and self.semantic_key != expected_semantic_key:
            _fail_contract()
        object.__setattr__(self, "source_row_id", expected_row_id)
        object.__setattr__(self, "semantic_key", expected_semantic_key)

    def to_dict(self) -> dict[str, object]:
        return {
            "account_role": self.account_role.value,
            "effective_date": self.effective_date.isoformat(),
            "status": self.status.value,
            "direction": self.direction.value,
            "magnitude": money_text(self.magnitude),
            "normalized_description": self.normalized_description,
            "occurrence_ordinal": self.occurrence_ordinal,
            "source_sha256": self.source_sha256,
            "locator": self.locator.to_dict(),
            "extraction_method": self.extraction_method.value,
            "parser_version": self.parser_version,
            "parse_evidence": [evidence.to_dict() for evidence in self.parse_evidence],
            "source_row_id": self.source_row_id,
            "semantic_key": self.semantic_key,
        }

    @classmethod
    def from_dict(cls, value: object) -> NormalizedTransaction:
        item = _contract_fields(
            value,
            frozenset(
                {
                    "account_role",
                    "effective_date",
                    "status",
                    "direction",
                    "magnitude",
                    "normalized_description",
                    "occurrence_ordinal",
                    "source_sha256",
                    "locator",
                    "extraction_method",
                    "parser_version",
                    "parse_evidence",
                    "source_row_id",
                    "semantic_key",
                }
            ),
        )
        source_row_id = _require_optional_sha256(item["source_row_id"])
        semantic_key = _require_optional_sha256(item["semantic_key"])
        evidence = item["parse_evidence"]
        if not isinstance(evidence, list):
            _fail_contract()
        return cls(
            account_role=_parse_enum(AccountRole, item["account_role"], private=False),
            effective_date=parse_iso_date(item["effective_date"]),
            status=_parse_enum(TransactionStatus, item["status"], private=False),
            direction=_parse_enum(Direction, item["direction"], private=False),
            magnitude=parse_positive_money(item["magnitude"]),
            normalized_description=_normalized_contract_text(item["normalized_description"]),
            occurrence_ordinal=_require_positive_int(item["occurrence_ordinal"]),
            source_sha256=_parse_sha256(item["source_sha256"], private=False),
            locator=SafeSourceLocator.from_dict(item["locator"]),
            extraction_method=_parse_enum(
                ExtractionMethod, item["extraction_method"], private=False
            ),
            parser_version=_require_supported_parser_version(item["parser_version"]),
            parse_evidence=tuple(ParseEvidence.from_dict(item) for item in evidence),
            source_row_id=source_row_id,
            semantic_key=semantic_key,
        )


@dataclass(frozen=True)
class TransactionSelector:
    account_role: AccountRole
    effective_date: date
    status: TransactionStatus
    direction: Direction
    magnitude: Decimal
    normalized_description: str
    occurrence_ordinal: int
    source_sha256: str
    page_number: int
    source_row_ordinal: int

    def __post_init__(self) -> None:
        _require_enum(AccountRole, self.account_role)
        _require_calendar_date(self.effective_date)
        _require_enum(TransactionStatus, self.status)
        _require_enum(Direction, self.direction)
        object.__setattr__(self, "magnitude", parse_positive_money(self.magnitude))
        if normalize_description(self.normalized_description) != self.normalized_description:
            _fail_contract()
        for value in (self.occurrence_ordinal, self.page_number, self.source_row_ordinal):
            _require_positive_int(value)
        _parse_sha256(self.source_sha256, private=False)

    def matches(self, transaction: NormalizedTransaction) -> bool:
        return (
            self.account_role is transaction.account_role
            and self.effective_date == transaction.effective_date
            and self.status is transaction.status
            and self.direction is transaction.direction
            and self.magnitude == transaction.magnitude
            and self.normalized_description == transaction.normalized_description
            and self.occurrence_ordinal == transaction.occurrence_ordinal
            and self.source_sha256 == transaction.source_sha256
            and self.page_number == transaction.locator.page_number
            and self.source_row_ordinal == transaction.locator.row_ordinal
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "account_role": self.account_role.value,
            "effective_date": self.effective_date.isoformat(),
            "status": self.status.value,
            "direction": self.direction.value,
            "magnitude": money_text(self.magnitude),
            "normalized_description": self.normalized_description,
            "occurrence_ordinal": self.occurrence_ordinal,
            "source_sha256": self.source_sha256,
            "page_number": self.page_number,
            "source_row_ordinal": self.source_row_ordinal,
        }

    @classmethod
    def from_dict(cls, value: object) -> TransactionSelector:
        item = _contract_fields(value, _SELECTOR_FIELDS)
        return cls(
            account_role=_parse_enum(AccountRole, item["account_role"], private=False),
            effective_date=parse_iso_date(item["effective_date"]),
            status=_parse_enum(TransactionStatus, item["status"], private=False),
            direction=_parse_enum(Direction, item["direction"], private=False),
            magnitude=parse_positive_money(item["magnitude"]),
            normalized_description=_normalized_contract_text(item["normalized_description"]),
            occurrence_ordinal=_require_positive_int(item["occurrence_ordinal"]),
            source_sha256=_parse_sha256(item["source_sha256"], private=False),
            page_number=_require_positive_int(item["page_number"]),
            source_row_ordinal=_require_positive_int(item["source_row_ordinal"]),
        )


@dataclass(frozen=True)
class ClassificationRule:
    rule_id: str
    account_role: AccountRole | None
    direction: Direction
    matcher_kind: MatcherKind
    matcher_value: str
    cash_role: CashRole
    category: str
    pair_key: str | None

    def __post_init__(self) -> None:
        if self.account_role is not None:
            _require_enum(AccountRole, self.account_role)
        _require_enum(Direction, self.direction)
        _require_enum(MatcherKind, self.matcher_kind)
        _require_enum(CashRole, self.cash_role)
        if (
            not isinstance(self.rule_id, str)
            or not self.rule_id.strip()
            or not isinstance(self.category, str)
            or not self.category.strip()
        ):
            _fail_contract()
        if normalize_description(self.matcher_value) != self.matcher_value:
            _fail_contract()
        if self.cash_role in {CashRole.TRANSFER, CashRole.REVERSAL}:
            if not isinstance(self.pair_key, str) or not self.pair_key.strip():
                _fail_contract()
        elif self.pair_key is not None:
            _fail_contract()

    def matches(self, transaction: NormalizedTransaction) -> bool:
        if self.account_role is not None and self.account_role is not transaction.account_role:
            return False
        if self.direction is not transaction.direction:
            return False
        description = transaction.normalized_description
        if self.matcher_kind is MatcherKind.EXACT:
            return description == self.matcher_value
        if self.matcher_kind is MatcherKind.PREFIX:
            return description.startswith(self.matcher_value)
        return self.matcher_value in description

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "account_role": None if self.account_role is None else self.account_role.value,
            "direction": self.direction.value,
            "matcher_kind": self.matcher_kind.value,
            "matcher_value": self.matcher_value,
            "cash_role": self.cash_role.value,
            "category": self.category,
            "pair_key": self.pair_key,
        }


@dataclass(frozen=True)
class OverlapResolution:
    selected: TransactionSelector
    rejected: TransactionSelector

    def __post_init__(self) -> None:
        if not isinstance(self.selected, TransactionSelector) or not isinstance(
            self.rejected, TransactionSelector
        ):
            _fail_contract()
        if self.selected == self.rejected:
            _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {"selected": self.selected.to_dict(), "rejected": self.rejected.to_dict()}


@dataclass(frozen=True)
class PairResolution:
    first: TransactionSelector
    second: TransactionSelector
    action: PairAction

    def __post_init__(self) -> None:
        if not isinstance(self.first, TransactionSelector) or not isinstance(
            self.second, TransactionSelector
        ):
            _fail_contract()
        _require_enum(PairAction, self.action)
        if self.first == self.second:
            _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {
            "first": self.first.to_dict(),
            "second": self.second.to_dict(),
            "action": self.action.value,
        }


@dataclass(frozen=True)
class TransactionAdjustment:
    selector: TransactionSelector
    action: AdjustmentAction
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.selector, TransactionSelector):
            _fail_contract()
        _require_enum(AdjustmentAction, self.action)
        if not isinstance(self.reason, str) or not self.reason.strip():
            _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {
            "selector": self.selector.to_dict(),
            "action": self.action.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TreasurerRules:
    schema_version: int
    classification_rules: tuple[ClassificationRule, ...]
    overlap_resolutions: tuple[OverlapResolution, ...]
    pair_resolutions: tuple[PairResolution, ...]
    transaction_adjustments: tuple[TransactionAdjustment, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.schema_version)
        if self.schema_version != RULES_SCHEMA_VERSION:
            _fail_contract()
        if not all(
            isinstance(value, tuple)
            for value in (
                self.classification_rules,
                self.overlap_resolutions,
                self.pair_resolutions,
                self.transaction_adjustments,
            )
        ):
            _fail_contract()
        if any(not isinstance(rule, ClassificationRule) for rule in self.classification_rules):
            _fail_contract()
        if any(not isinstance(value, OverlapResolution) for value in self.overlap_resolutions):
            _fail_contract()
        if any(not isinstance(value, PairResolution) for value in self.pair_resolutions):
            _fail_contract()
        if any(
            not isinstance(value, TransactionAdjustment) for value in self.transaction_adjustments
        ):
            _fail_contract()
        identifiers = [rule.rule_id for rule in self.classification_rules]
        if len(set(identifiers)) != len(identifiers):
            _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "classification_rules": [rule.to_dict() for rule in self.classification_rules],
            "overlap_resolutions": [
                resolution.to_dict() for resolution in self.overlap_resolutions
            ],
            "pair_resolutions": [resolution.to_dict() for resolution in self.pair_resolutions],
            "transaction_adjustments": [
                adjustment.to_dict() for adjustment in self.transaction_adjustments
            ],
        }


@dataclass(frozen=True)
class PageEvidence:
    """Versioned recognition evidence for one statement page.

    Tokens are deliberately retained only in the private normalized contract; later
    pipeline stages may use them for review but never expose them in tracked output.
    """

    page_number: int
    page_kind: PageKind
    fingerprint_version: PageFingerprint
    extraction_method: ExtractionMethod
    ignored: bool
    activity_tables: tuple[ActivityTableEvidence, ...]
    balance_rows: tuple[BalanceRowEvidence, ...]
    balance_controls: tuple[BalanceControlEvidence, ...]
    tokens: tuple[PositionedToken, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.page_number)
        _require_enum(PageKind, self.page_kind)
        _require_enum(PageFingerprint, self.fingerprint_version)
        _require_enum(ExtractionMethod, self.extraction_method)
        if (
            not isinstance(self.ignored, bool)
            or not isinstance(self.activity_tables, tuple)
            or not isinstance(self.balance_rows, tuple)
            or not isinstance(self.balance_controls, tuple)
            or not isinstance(self.tokens, tuple)
        ):
            _fail_contract()
        if self.ignored is not (self.page_kind is PageKind.BOILERPLATE):
            _fail_contract()
        if self.fingerprint_version.page_kind is not self.page_kind:
            _fail_contract()
        if any(not isinstance(table, ActivityTableEvidence) for table in self.activity_tables):
            _fail_contract()
        if any(not isinstance(row, BalanceRowEvidence) for row in self.balance_rows):
            _fail_contract()
        if any(
            not isinstance(control, BalanceControlEvidence) for control in self.balance_controls
        ):
            _fail_contract()
        if any(not isinstance(token, PositionedToken) for token in self.tokens):
            _fail_contract()
        if not self.ignored and not self.tokens:
            _fail_contract()
        if self.page_kind in _ACTIVITY_PAGE_KINDS:
            if not self.activity_tables:
                _fail_contract()
        elif self.activity_tables:
            _fail_contract()
        if self.ignored and (self.balance_rows or self.balance_controls):
            _fail_contract()
        table_ordinals = [table.table_ordinal for table in self.activity_tables]
        if len(set(table_ordinals)) != len(table_ordinals):
            _fail_contract()
        activity_row_boxes = [row.row_box for table in self.activity_tables for row in table.rows]
        if any(
            _boxes_overlap(first, second)
            for index, first in enumerate(activity_row_boxes)
            for second in activity_row_boxes[index + 1 :]
        ):
            _fail_contract()
        balance_row_locators = [row.locator for row in self.balance_rows]
        if len(set(balance_row_locators)) != len(balance_row_locators):
            _fail_contract()
        control_locators = [control.locator for control in self.balance_controls]
        if len(set(control_locators)) != len(control_locators) or set(control_locators) != set(
            balance_row_locators
        ):
            _fail_contract()
        balance_row_boxes = [row.locator.row_box for row in self.balance_rows]
        if any(
            _boxes_overlap(first, second)
            for index, first in enumerate(balance_row_boxes)
            for second in balance_row_boxes[index + 1 :]
        ):
            _fail_contract()
        if any(row.locator.page_number != self.page_number for row in self.balance_rows) or any(
            control.locator.page_number != self.page_number for control in self.balance_controls
        ):
            _fail_contract()
        if any(token.page_number != self.page_number for token in self.tokens):
            _fail_contract()
        if any(token.extraction_method is not self.extraction_method for token in self.tokens):
            _fail_contract()
        for table in self.activity_tables:
            for column in table.columns:
                header_tokens = _tokens_for_ordinals(self, column.header_token_ordinals)
                if (
                    not _tokens_back_box(header_tokens, column.header_box)
                    or not _tokens_in_activity_column(header_tokens, table, column.column)
                    or any(token.confidence < 75 for token in header_tokens)
                ):
                    _fail_contract()
                if not _tokens_support_terms(
                    header_tokens, _ACTIVITY_COLUMN_HEADER_TERMS[column.column]
                ):
                    _fail_contract()
            for control in table.status_controls:
                control_tokens = _tokens_for_ordinals(self, control.token_ordinals)
                if (
                    not _tokens_back_box(control_tokens, control.box)
                    or any(token.confidence < 75 for token in control_tokens)
                    or not _tokens_support_unambiguous_terms(
                        control_tokens,
                        _STATUS_EVIDENCE_TERMS[control.status],
                        tuple(
                            candidate_terms
                            for status, candidate_terms in _STATUS_EVIDENCE_TERMS.items()
                            if status is not control.status
                        ),
                    )
                ):
                    _fail_contract()
        for balance_row in self.balance_rows:
            if self.page_kind in _ACTIVITY_PAGE_KINDS:
                _activity_table_for_locator(self, balance_row.locator)
            date_tokens = _tokens_for_ordinals(self, balance_row.date_token_ordinals)
            balance_tokens = _tokens_for_ordinals(self, balance_row.balance_token_ordinals)
            source_row_tokens = tuple(
                token
                for token in self.tokens
                if _tokens_back_locator((token,), balance_row.locator)
            )
            if (
                not _tokens_back_locator(date_tokens + balance_tokens, balance_row.locator)
                or any(token.confidence < 75 for token in date_tokens + balance_tokens)
                or not _tokens_look_like_date(date_tokens)
                or not _tokens_look_like_money(balance_tokens)
                or _source_date_match_count(source_row_tokens) != 1
                or _source_money_match_count(source_row_tokens) != 1
            ):
                _fail_contract()
        for balance_control in self.balance_controls:
            balance_row = _balance_row_for_locator(self, balance_control.locator)
            if self.page_kind in _ACTIVITY_PAGE_KINDS:
                _activity_table_for_locator(self, balance_control.locator)
            control_token_groups: list[tuple[PositionedToken, ...]] = []
            for ordinals, terms, conflicting_terms in (
                (
                    balance_control.kind_token_ordinals,
                    _BALANCE_KIND_EVIDENCE_TERMS[balance_control.kind],
                    tuple(
                        candidate_terms
                        for kind, candidate_terms in _BALANCE_KIND_EVIDENCE_TERMS.items()
                        if kind is not balance_control.kind
                    ),
                ),
                (
                    balance_control.boundary_token_ordinals,
                    _BALANCE_BOUNDARY_EVIDENCE_TERMS[balance_control.boundary],
                    tuple(
                        candidate_terms
                        for boundary, candidate_terms in _BALANCE_BOUNDARY_EVIDENCE_TERMS.items()
                        if boundary is not balance_control.boundary
                    ),
                ),
                (
                    balance_control.includes_pending_token_ordinals,
                    _PENDING_BASIS_EVIDENCE_TERMS[balance_control.includes_pending],
                    tuple(
                        candidate_terms
                        for includes_pending, candidate_terms in (
                            _PENDING_BASIS_EVIDENCE_TERMS.items()
                        )
                        if includes_pending is not balance_control.includes_pending
                    ),
                ),
            ):
                control_tokens = _tokens_for_ordinals(self, ordinals)
                control_token_groups.append(control_tokens)
                if (
                    not _tokens_back_box(control_tokens, balance_control.control_box)
                    or any(token.confidence < 75 for token in control_tokens)
                    or not _tokens_support_unambiguous_terms(
                        control_tokens, terms, conflicting_terms
                    )
                ):
                    _fail_contract()
            all_control_tokens = tuple(token for group in control_token_groups for token in group)
            row_tokens = (
                _tokens_for_ordinals(self, balance_row.date_token_ordinals)
                + _tokens_for_ordinals(self, balance_row.balance_token_ordinals)
                + all_control_tokens
            )
            if balance_control.control_box != _bounding_box_for_tokens(
                all_control_tokens
            ) or balance_row.locator.row_box != _bounding_box_for_tokens(row_tokens):
                _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {
            "page_number": self.page_number,
            "page_kind": self.page_kind.value,
            "fingerprint_version": self.fingerprint_version.value,
            "extraction_method": self.extraction_method.value,
            "ignored": self.ignored,
            "activity_tables": [table.to_dict() for table in self.activity_tables],
            "balance_rows": [row.to_dict() for row in self.balance_rows],
            "balance_controls": [control.to_dict() for control in self.balance_controls],
            "tokens": [token.to_dict() for token in self.tokens],
        }

    @classmethod
    def from_dict(cls, value: object) -> PageEvidence:
        item = _contract_fields(
            value,
            frozenset(
                {
                    "page_number",
                    "page_kind",
                    "fingerprint_version",
                    "extraction_method",
                    "ignored",
                    "activity_tables",
                    "balance_rows",
                    "balance_controls",
                    "tokens",
                }
            ),
        )
        activity_tables = item["activity_tables"]
        balance_rows = item["balance_rows"]
        balance_controls = item["balance_controls"]
        tokens = item["tokens"]
        if (
            not isinstance(activity_tables, list)
            or not isinstance(balance_rows, list)
            or not isinstance(balance_controls, list)
            or not isinstance(tokens, list)
        ):
            _fail_contract()
        return cls(
            page_number=_require_positive_int(item["page_number"]),
            page_kind=_parse_enum(PageKind, item["page_kind"], private=False),
            fingerprint_version=_parse_enum(
                PageFingerprint, item["fingerprint_version"], private=False
            ),
            extraction_method=_parse_enum(
                ExtractionMethod, item["extraction_method"], private=False
            ),
            ignored=_require_bool(item["ignored"]),
            activity_tables=tuple(
                ActivityTableEvidence.from_dict(table) for table in activity_tables
            ),
            balance_rows=tuple(BalanceRowEvidence.from_dict(row) for row in balance_rows),
            balance_controls=tuple(
                BalanceControlEvidence.from_dict(control) for control in balance_controls
            ),
            tokens=tuple(PositionedToken.from_dict(token) for token in tokens),
        )


def _tokens_back_box(tokens: tuple[PositionedToken, ...], box: BoundingBox) -> bool:
    """Return whether all named tokens are positioned inside one recognized box."""
    if not isinstance(box, BoundingBox):
        _fail_contract()
    return bool(tokens) and all(
        token.box.left >= box.left
        and token.box.top >= box.top
        and token.box.right <= box.right
        and token.box.bottom <= box.bottom
        for token in tokens
    )


def _tokens_back_locator(tokens: tuple[PositionedToken, ...], locator: SafeSourceLocator) -> bool:
    """Return whether all named field tokens are positioned inside a source row."""
    return _tokens_back_box(tokens, locator.row_box)


def _activity_table_for_locator(
    page: PageEvidence, locator: SafeSourceLocator
) -> ActivityTableEvidence:
    """Resolve a transaction locator only to a parser-recognized table and row."""
    if page.page_kind not in _ACTIVITY_PAGE_KINDS:
        _fail_contract()
    for table in page.activity_tables:
        if table.table_ordinal == locator.table_ordinal:
            row = table.row(locator.row_ordinal)
            if row.row_box != locator.row_box:
                _fail_contract()
            return table
    _fail_contract()


def _balance_control_for_locator(
    page: PageEvidence, locator: SafeSourceLocator
) -> BalanceControlEvidence:
    """Resolve a balance's semantic controls only to its exact source locator."""
    controls = tuple(control for control in page.balance_controls if control.locator == locator)
    if len(controls) != 1:
        _fail_contract()
    return controls[0]


def _balance_row_for_locator(page: PageEvidence, locator: SafeSourceLocator) -> BalanceRowEvidence:
    """Resolve a balance only to one parser-recognized compact source row."""
    rows = tuple(row for row in page.balance_rows if row.locator == locator)
    if len(rows) != 1:
        _fail_contract()
    return rows[0]


def _bounding_box_for_tokens(tokens: tuple[PositionedToken, ...]) -> BoundingBox:
    """Return the exact compact geometry of one nonempty set of positioned tokens."""
    if not tokens:
        _fail_contract()
    return BoundingBox(
        min(token.box.left for token in tokens),
        min(token.box.top for token in tokens),
        max(token.box.right for token in tokens),
        max(token.box.bottom for token in tokens),
    )


def _tokens_look_like_date(tokens: tuple[PositionedToken, ...]) -> bool:
    return any(_SOURCE_DATE_RE.search(token.text) is not None for token in tokens)


def _tokens_look_like_money(tokens: tuple[PositionedToken, ...]) -> bool:
    return any(_SOURCE_MONEY_RE.search(token.text) is not None for token in tokens)


def _source_date_match_count(tokens: tuple[PositionedToken, ...]) -> int:
    """Count lexical date candidates in one recognized balance source row."""
    return sum(1 for token in tokens for _match in _SOURCE_DATE_RE.finditer(token.text))


def _source_money_match_count(tokens: tuple[PositionedToken, ...]) -> int:
    """Count lexical monetary candidates in one recognized balance source row."""
    return sum(1 for token in tokens for _match in _SOURCE_MONEY_RE.finditer(token.text))


def _tokens_in_activity_column(
    tokens: tuple[PositionedToken, ...],
    table: ActivityTableEvidence,
    column: ActivityColumn,
) -> bool:
    return bool(tokens) and all(
        table.band(column).contains_token_centroid(token) for token in tokens
    )


def _money_tokens_in_row(
    page: PageEvidence, locator: SafeSourceLocator
) -> tuple[PositionedToken, ...]:
    """Return all individually positioned monetary tokens in the recognized source row."""
    return tuple(
        token
        for token in page.tokens
        if _tokens_back_locator((token,), locator)
        and _SOURCE_MONEY_RE.search(token.text) is not None
    )


def _activity_column_for_direction(direction: Direction) -> ActivityColumn:
    _require_enum(Direction, direction)
    return ActivityColumn.CREDIT if direction is Direction.CREDIT else ActivityColumn.DEBIT


def _tokens_for_ordinals(
    page: PageEvidence, ordinals: tuple[int, ...]
) -> tuple[PositionedToken, ...]:
    """Resolve validated one-based token ordinals on their source page."""
    try:
        return tuple(page.tokens[ordinal - 1] for ordinal in ordinals)
    except IndexError:
        _fail_contract()


def _tokens_for_parse_evidence(
    page: PageEvidence, evidence: ParseEvidence
) -> tuple[PositionedToken, ...]:
    return _tokens_for_ordinals(page, evidence.token_ordinals)


def _context_tokens_for_parse_evidence(
    page: PageEvidence, evidence: ParseEvidence
) -> tuple[PositionedToken, ...]:
    return _tokens_for_ordinals(page, evidence.context_token_ordinals)


def _parse_evidence_by_field(
    evidence: tuple[ParseEvidence, ...],
) -> dict[EvidenceField, ParseEvidence]:
    return {item.field: item for item in evidence}


def _normalized_evidence_text(tokens: tuple[PositionedToken, ...]) -> str:
    return normalize_description(" ".join(token.text for token in tokens))


def _tokens_support_date(tokens: tuple[PositionedToken, ...], value: date) -> bool:
    text = _normalized_evidence_text(tokens)
    month = f"(?:0{value.month}|{value.month})" if value.month < 10 else str(value.month)
    day = f"(?:0{value.day}|{value.day})" if value.day < 10 else str(value.day)
    year = f"(?:{value.year}|{value.year % 100:02d})"
    return bool(
        re.search(rf"(?<![0-9]){re.escape(value.isoformat())}(?![0-9])", text)
        or re.search(
            rf"(?<![0-9]){month}/{day}(?:/{year})?(?![0-9/])",
            text,
        )
    )


def _tokens_support_money(tokens: tuple[PositionedToken, ...], value: Decimal) -> bool:
    expected = money_text(value)
    return any(
        match.group(1).replace(",", "") == expected
        for match in _SOURCE_MONEY_RE.finditer(" ".join(token.text for token in tokens))
    )


def _tokens_support_terms(tokens: tuple[PositionedToken, ...], terms: tuple[str, ...]) -> bool:
    text = _normalized_evidence_text(tokens)
    return any(
        re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text) is not None for term in terms
    )


def _tokens_support_unambiguous_terms(
    tokens: tuple[PositionedToken, ...],
    expected_terms: tuple[str, ...],
    conflicting_terms: tuple[tuple[str, ...], ...],
) -> bool:
    """Require one typed label while rejecting any rival typed label in its source text."""
    return _tokens_support_terms(tokens, expected_terms) and not any(
        _tokens_support_terms(tokens, terms) for terms in conflicting_terms
    )


def _validate_transaction_parse_evidence(
    page: PageEvidence, transaction: NormalizedTransaction
) -> None:
    by_field = _parse_evidence_by_field(transaction.parse_evidence)
    table = _activity_table_for_locator(page, transaction.locator)
    row_tokens = {
        field: _tokens_for_parse_evidence(page, evidence) for field, evidence in by_field.items()
    }
    direction_evidence = by_field[EvidenceField.DIRECTION]
    status_evidence = by_field[EvidenceField.STATUS]
    direction_context = _context_tokens_for_parse_evidence(page, direction_evidence)
    status_context = _context_tokens_for_parse_evidence(page, status_evidence)
    status_control = table.status_control_for_row(transaction.locator.row_ordinal)
    direction_column = _activity_column_for_direction(transaction.direction)
    money_columns = {
        column
        for token in _money_tokens_in_row(page, transaction.locator)
        for column in (ActivityColumn.DEBIT, ActivityColumn.CREDIT)
        if table.band(column).contains_token_centroid(token)
    }
    if any(
        token.confidence < 75
        for field, evidence in by_field.items()
        if field is not EvidenceField.DESCRIPTION
        for token in (row_tokens[field] + _context_tokens_for_parse_evidence(page, evidence))
    ):
        _fail_contract()
    if (
        any(not _tokens_back_locator(tokens, transaction.locator) for tokens in row_tokens.values())
        or any(
            evidence.context_token_ordinals
            for field, evidence in by_field.items()
            if field not in {EvidenceField.DIRECTION, EvidenceField.STATUS}
        )
        or not direction_context
        or direction_evidence.context_token_ordinals
        != table.band(direction_column).header_token_ordinals
        or not _tokens_back_box(direction_context, table.band(direction_column).header_box)
        or table.header_box.bottom > transaction.locator.row_box.top
        or not _tokens_in_activity_column(
            row_tokens[EvidenceField.DATE], table, ActivityColumn.DATE
        )
        or not _tokens_in_activity_column(
            row_tokens[EvidenceField.DESCRIPTION], table, ActivityColumn.DESCRIPTION
        )
        or not _tokens_in_activity_column(
            row_tokens[EvidenceField.DIRECTION], table, direction_column
        )
        or not _tokens_in_activity_column(
            row_tokens[EvidenceField.MAGNITUDE], table, direction_column
        )
        or not set(direction_evidence.token_ordinals).intersection(
            by_field[EvidenceField.MAGNITUDE].token_ordinals
        )
        or money_columns != {direction_column}
        or not _tokens_support_date(row_tokens[EvidenceField.DATE], transaction.effective_date)
        or transaction.normalized_description
        != _normalized_evidence_text(row_tokens[EvidenceField.DESCRIPTION])
        or not _tokens_support_money(row_tokens[EvidenceField.MAGNITUDE], transaction.magnitude)
        or not _tokens_support_unambiguous_terms(
            direction_context,
            _DIRECTION_EVIDENCE_TERMS[transaction.direction],
            tuple(
                terms
                for direction, terms in _DIRECTION_EVIDENCE_TERMS.items()
                if direction is not transaction.direction
            ),
        )
        or not status_context
        or status_control.status is not transaction.status
        or status_evidence.context_token_ordinals != status_control.token_ordinals
        or not _tokens_back_box(status_context, status_control.box)
    ):
        _fail_contract()


def _validate_balance_parse_evidence(page: PageEvidence, balance: BalanceObservation) -> None:
    by_field = _parse_evidence_by_field(balance.parse_evidence)
    row_tokens = {
        field: _tokens_for_parse_evidence(page, evidence) for field, evidence in by_field.items()
    }
    all_tokens = {
        field: row_tokens[field] + _context_tokens_for_parse_evidence(page, evidence)
        for field, evidence in by_field.items()
    }
    if any(token.confidence < 75 for tokens in all_tokens.values() for token in tokens):
        _fail_contract()
    row = _balance_row_for_locator(page, balance.locator)
    control = _balance_control_for_locator(page, balance.locator)
    if (
        by_field[EvidenceField.DATE].token_ordinals != row.date_token_ordinals
        or by_field[EvidenceField.BALANCE].token_ordinals != row.balance_token_ordinals
        or by_field[EvidenceField.KIND].token_ordinals != control.kind_token_ordinals
        or by_field[EvidenceField.BOUNDARY].token_ordinals != control.boundary_token_ordinals
        or by_field[EvidenceField.INCLUDES_PENDING].token_ordinals
        != control.includes_pending_token_ordinals
        or by_field[EvidenceField.KIND].context_token_ordinals != control.kind_token_ordinals
        or by_field[EvidenceField.BOUNDARY].context_token_ordinals
        != control.boundary_token_ordinals
        or by_field[EvidenceField.INCLUDES_PENDING].context_token_ordinals
        != control.includes_pending_token_ordinals
        or balance.kind is not control.kind
        or balance.boundary is not control.boundary
        or balance.includes_pending is not control.includes_pending
    ):
        _fail_contract()
    if (
        any(not _tokens_back_locator(tokens, balance.locator) for tokens in row_tokens.values())
        or by_field[EvidenceField.DATE].context_token_ordinals
        or by_field[EvidenceField.BALANCE].context_token_ordinals
        or not _tokens_support_date(row_tokens[EvidenceField.DATE], balance.observed_on)
        or not _tokens_support_money(row_tokens[EvidenceField.BALANCE], balance.amount)
        or not _tokens_support_unambiguous_terms(
            all_tokens[EvidenceField.KIND],
            _BALANCE_KIND_EVIDENCE_TERMS[balance.kind],
            tuple(
                terms
                for kind, terms in _BALANCE_KIND_EVIDENCE_TERMS.items()
                if kind is not balance.kind
            ),
        )
        or not _tokens_support_unambiguous_terms(
            all_tokens[EvidenceField.BOUNDARY],
            _BALANCE_BOUNDARY_EVIDENCE_TERMS[balance.boundary],
            tuple(
                terms
                for boundary, terms in _BALANCE_BOUNDARY_EVIDENCE_TERMS.items()
                if boundary is not balance.boundary
            ),
        )
        or not _tokens_support_unambiguous_terms(
            all_tokens[EvidenceField.INCLUDES_PENDING],
            _PENDING_BASIS_EVIDENCE_TERMS[balance.includes_pending],
            tuple(
                terms
                for includes_pending, terms in _PENDING_BASIS_EVIDENCE_TERMS.items()
                if includes_pending is not balance.includes_pending
            ),
        )
    ):
        _fail_contract()


@dataclass(frozen=True)
class StatementObservation:
    document_ordinal: int
    document: DocumentSpec
    source_sha256: str
    parser_version: str
    coverage_start: date
    coverage_end: date
    capture_date: date
    source_page_count: int
    page_evidence: tuple[PageEvidence, ...]
    transactions: tuple[NormalizedTransaction, ...]
    balances: tuple[BalanceObservation, ...]

    def __post_init__(self) -> None:
        _require_positive_int(self.document_ordinal)
        if not isinstance(self.document, DocumentSpec):
            _fail_contract()
        _parse_sha256(self.source_sha256, private=False)
        _require_supported_parser_version(self.parser_version)
        _require_calendar_date(self.coverage_start)
        _require_calendar_date(self.coverage_end)
        _require_calendar_date(self.capture_date)
        _require_positive_int(self.source_page_count)
        if self.coverage_end < self.coverage_start or self.capture_date < self.coverage_end:
            _fail_contract()
        if (
            not isinstance(self.page_evidence, tuple)
            or not self.page_evidence
            or not isinstance(self.transactions, tuple)
            or not isinstance(self.balances, tuple)
        ):
            _fail_contract()
        if any(not isinstance(page, PageEvidence) for page in self.page_evidence):
            _fail_contract()
        if any(
            not isinstance(transaction, NormalizedTransaction) for transaction in self.transactions
        ):
            _fail_contract()
        if any(not isinstance(balance, BalanceObservation) for balance in self.balances):
            _fail_contract()
        pages = [page.page_number for page in self.page_evidence]
        if pages != list(range(1, self.source_page_count + 1)):
            _fail_contract()
        if any(
            page.page_kind not in _PAGE_KINDS_BY_DOCUMENT_KIND[self.document.document_kind]
            or page.fingerprint_version.parser_version != self.parser_version
            for page in self.page_evidence
        ):
            _fail_contract()
        page_by_number = {page.page_number: page for page in self.page_evidence}
        expected_transaction_locators: list[SafeSourceLocator] = []
        expected_balance_controls: dict[SafeSourceLocator, BalanceControlEvidence] = {}
        for page in self.page_evidence:
            if page.ignored:
                continue
            for table in page.activity_tables:
                for row in table.rows:
                    locator = SafeSourceLocator(
                        document_ordinal=self.document_ordinal,
                        page_number=page.page_number,
                        table_ordinal=table.table_ordinal,
                        row_ordinal=row.row_ordinal,
                        row_box=row.row_box,
                    )
                    expected_transaction_locators.append(locator)
            for control in page.balance_controls:
                if control.locator.document_ordinal != self.document_ordinal:
                    _fail_contract()
                if control.locator in expected_balance_controls:
                    _fail_contract()
                expected_balance_controls[control.locator] = control
        transaction_end = (
            self.coverage_end
            if self.document.document_kind is DocumentKind.MONTHLY_STATEMENT
            else self.capture_date
        )
        balance_end = transaction_end
        for transaction in self.transactions:
            if not self.coverage_start <= transaction.effective_date <= transaction_end:
                _fail_contract()
            transaction_page = page_by_number.get(transaction.locator.page_number)
            if transaction_page is None:
                _fail_contract()
            if (
                transaction.source_sha256 != self.source_sha256
                or transaction.account_role is not self.document.account_role
                or transaction.locator.document_ordinal != self.document_ordinal
                or transaction.parser_version != self.parser_version
                or transaction_page.ignored
                or transaction_page.page_kind not in _ACTIVITY_PAGE_KINDS
                or transaction_page.extraction_method is not transaction.extraction_method
            ):
                _fail_contract()
            _validate_transaction_parse_evidence(transaction_page, transaction)
        for balance in self.balances:
            if not self.coverage_start <= balance.observed_on <= balance_end:
                _fail_contract()
            balance_page = page_by_number.get(balance.locator.page_number)
            if balance_page is None:
                _fail_contract()
            if (
                balance.source_sha256 != self.source_sha256
                or balance.account_role is not self.document.account_role
                or balance.locator.document_ordinal != self.document_ordinal
                or balance_page.ignored
                or balance_page.extraction_method is not balance.extraction_method
            ):
                _fail_contract()
            _validate_balance_parse_evidence(balance_page, balance)
        row_ids = [transaction.source_row_id for transaction in self.transactions]
        if len(set(row_ids)) != len(row_ids):
            _fail_contract()
        transaction_locators = [transaction.locator for transaction in self.transactions]
        if len(set(transaction_locators)) != len(transaction_locators) or set(
            transaction_locators
        ) != set(expected_transaction_locators):
            _fail_contract()
        balance_locators = [balance.locator for balance in self.balances]
        if len(set(balance_locators)) != len(balance_locators) or set(balance_locators) != set(
            expected_balance_controls
        ):
            _fail_contract()
        if any(
            (
                expected_balance_controls[balance.locator].kind is not balance.kind
                or expected_balance_controls[balance.locator].boundary is not balance.boundary
                or expected_balance_controls[balance.locator].includes_pending
                is not balance.includes_pending
            )
            for balance in self.balances
        ):
            _fail_contract()
        if self.transactions != assign_occurrence_ordinals(self.transactions):
            _fail_contract()
        semantic_keys = [transaction.semantic_key for transaction in self.transactions]
        if len(set(semantic_keys)) != len(semantic_keys):
            _fail_contract()

    def to_dict(self) -> dict[str, object]:
        return {
            "document_ordinal": self.document_ordinal,
            "document": self.document.to_dict(),
            "source_sha256": self.source_sha256,
            "parser_version": self.parser_version,
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
            "capture_date": self.capture_date.isoformat(),
            "source_page_count": self.source_page_count,
            "page_evidence": [page.to_dict() for page in self.page_evidence],
            "transactions": [transaction.to_dict() for transaction in self.transactions],
            "balances": [balance.to_dict() for balance in self.balances],
        }

    @classmethod
    def from_dict(cls, value: object) -> StatementObservation:
        item = _contract_fields(
            value,
            frozenset(
                {
                    "document_ordinal",
                    "document",
                    "source_sha256",
                    "parser_version",
                    "coverage_start",
                    "coverage_end",
                    "capture_date",
                    "source_page_count",
                    "page_evidence",
                    "transactions",
                    "balances",
                }
            ),
        )
        pages = item["page_evidence"]
        transactions = item["transactions"]
        balances = item["balances"]
        if (
            not isinstance(pages, list)
            or not isinstance(transactions, list)
            or not isinstance(balances, list)
        ):
            _fail_contract()
        return cls(
            document_ordinal=_require_positive_int(item["document_ordinal"]),
            document=DocumentSpec.from_dict(item["document"]),
            source_sha256=_parse_sha256(item["source_sha256"], private=False),
            parser_version=_require_supported_parser_version(item["parser_version"]),
            coverage_start=parse_iso_date(item["coverage_start"]),
            coverage_end=parse_iso_date(item["coverage_end"]),
            capture_date=parse_iso_date(item["capture_date"]),
            source_page_count=_require_positive_int(item["source_page_count"]),
            page_evidence=tuple(PageEvidence.from_dict(page) for page in pages),
            transactions=tuple(
                NormalizedTransaction.from_dict(transaction) for transaction in transactions
            ),
            balances=tuple(BalanceObservation.from_dict(balance) for balance in balances),
        )


def _parse_document_spec(value: object) -> DocumentSpec:
    item = _object_fields(value, frozenset({"account_role", "document_kind", "relative_path"}))
    return DocumentSpec(
        account_role=_parse_enum(AccountRole, item["account_role"], private=True),
        document_kind=_parse_enum(DocumentKind, item["document_kind"], private=True),
        relative_path=_relative_path_text(item["relative_path"]),
    )


def _parse_selector(value: object) -> TransactionSelector:
    item = _object_fields(value, _SELECTOR_FIELDS)
    return TransactionSelector(
        account_role=_parse_enum(AccountRole, item["account_role"], private=True),
        effective_date=parse_iso_date(item["effective_date"], private=True),
        status=_parse_enum(TransactionStatus, item["status"], private=True),
        direction=_parse_enum(Direction, item["direction"], private=True),
        magnitude=parse_positive_money(item["magnitude"], private=True),
        normalized_description=_normalized_private_text(item["normalized_description"]),
        occurrence_ordinal=_positive_private_int(item["occurrence_ordinal"]),
        source_sha256=_parse_sha256(item["source_sha256"], private=True),
        page_number=_positive_private_int(item["page_number"]),
        source_row_ordinal=_positive_private_int(item["source_row_ordinal"]),
    )


def _positive_private_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        _fail_input()
    return value


def _normalized_private_text(value: object) -> str:
    text = _required_text(value)
    try:
        normalized = normalize_description(text)
    except ContractError:
        _fail_input()
    if normalized != text:
        _fail_input()
    return text


def _normalized_contract_text(value: object) -> str:
    text = _require_nonblank_text(value)
    if normalize_description(text) != text:
        _fail_contract()
    return text


def _parse_classification_rule(value: object) -> ClassificationRule:
    item = _object_fields(
        value,
        frozenset(
            {
                "rule_id",
                "account_role",
                "direction",
                "matcher_kind",
                "matcher_value",
                "cash_role",
                "category",
                "pair_key",
            }
        ),
    )
    account_value = item["account_role"]
    if account_value is not None and not isinstance(account_value, str):
        _fail_input()
    pair_value = item["pair_key"]
    if pair_value is not None and not isinstance(pair_value, str):
        _fail_input()
    try:
        return ClassificationRule(
            rule_id=_required_text(item["rule_id"]),
            account_role=None
            if account_value is None
            else _parse_enum(AccountRole, account_value, private=True),
            direction=_parse_enum(Direction, item["direction"], private=True),
            matcher_kind=_parse_enum(MatcherKind, item["matcher_kind"], private=True),
            matcher_value=_normalized_private_text(item["matcher_value"]),
            cash_role=_parse_enum(CashRole, item["cash_role"], private=True),
            category=_required_text(item["category"]),
            pair_key=pair_value,
        )
    except ContractError:
        _fail_input()


def load_input_manifest(path: Path) -> InputManifest:
    """Load and validate a v1 manifest, including every referenced private file path."""
    value = _load_canonical_json(path)
    item = _object_fields(
        value,
        frozenset(
            {
                "schema_version",
                "reporting_start_date",
                "as_of_date",
                "budget_fiscal_year",
                "cash_basis",
                "documents",
                "rules_relative_path",
            }
        ),
    )
    documents_value = _required_list(item["documents"])
    try:
        manifest = InputManifest(
            schema_version=item["schema_version"]
            if isinstance(item["schema_version"], int)
            and not isinstance(item["schema_version"], bool)
            else -1,
            reporting_start_date=parse_iso_date(item["reporting_start_date"], private=True),
            as_of_date=parse_iso_date(item["as_of_date"], private=True),
            budget_fiscal_year=_required_text(item["budget_fiscal_year"]),
            cash_basis=_parse_enum(CashBasis, item["cash_basis"], private=True),
            documents=tuple(_parse_document_spec(document) for document in documents_value),
            rules_relative_path=_relative_path_text(item["rules_relative_path"]),
        )
    except ContractError:
        _fail_input()
    base = Path(path).parent
    for document in manifest.documents:
        resolve_private_relative_path(base, document.relative_path)
    resolve_private_relative_path(base, manifest.rules_relative_path)
    return manifest


def load_rules(path: Path) -> TreasurerRules:
    """Load a strict v1 rule document without reflecting private selectors or reasons."""
    value = _load_canonical_json(path)
    item = _object_fields(
        value,
        frozenset(
            {
                "schema_version",
                "classification_rules",
                "overlap_resolutions",
                "pair_resolutions",
                "transaction_adjustments",
            }
        ),
    )
    classification_values = _required_list(item["classification_rules"])
    overlap_values = _required_list(item["overlap_resolutions"])
    pair_values = _required_list(item["pair_resolutions"])
    adjustment_values = _required_list(item["transaction_adjustments"])
    try:
        overlap = tuple(
            OverlapResolution(
                selected=_parse_selector(
                    _object_fields(entry, frozenset({"selected", "rejected"}))["selected"]
                ),
                rejected=_parse_selector(
                    _object_fields(entry, frozenset({"selected", "rejected"}))["rejected"]
                ),
            )
            for entry in overlap_values
        )
        pairs = tuple(
            PairResolution(
                first=_parse_selector(
                    _object_fields(entry, frozenset({"first", "second", "action"}))["first"]
                ),
                second=_parse_selector(
                    _object_fields(entry, frozenset({"first", "second", "action"}))["second"]
                ),
                action=_parse_enum(
                    PairAction,
                    _object_fields(entry, frozenset({"first", "second", "action"}))["action"],
                    private=True,
                ),
            )
            for entry in pair_values
        )
        adjustments = tuple(
            TransactionAdjustment(
                selector=_parse_selector(
                    _object_fields(entry, frozenset({"selector", "action", "reason"}))["selector"]
                ),
                action=_parse_enum(
                    AdjustmentAction,
                    _object_fields(entry, frozenset({"selector", "action", "reason"}))["action"],
                    private=True,
                ),
                reason=_required_text(
                    _object_fields(entry, frozenset({"selector", "action", "reason"}))["reason"]
                ),
            )
            for entry in adjustment_values
        )
        rules = TreasurerRules(
            schema_version=item["schema_version"]
            if isinstance(item["schema_version"], int)
            and not isinstance(item["schema_version"], bool)
            else -1,
            classification_rules=tuple(
                _parse_classification_rule(entry) for entry in classification_values
            ),
            overlap_resolutions=overlap,
            pair_resolutions=pairs,
            transaction_adjustments=adjustments,
        )
    except ContractError:
        _fail_input()
    return rules


def assign_occurrence_ordinals(
    transactions: Sequence[NormalizedTransaction],
) -> tuple[NormalizedTransaction, ...]:
    """Rebuild deterministic occurrence ordinals in source-row order for semantic duplicates."""
    row_ids = [transaction.source_row_id for transaction in transactions]
    if len(set(row_ids)) != len(row_ids):
        _fail_contract()
    ordered = sorted(
        transactions,
        key=lambda transaction: (
            transaction.account_role.value,
            transaction.effective_date,
            transaction.status.value,
            transaction.direction.value,
            money_text(transaction.magnitude),
            transaction.normalized_description,
            transaction.source_sha256,
            transaction.parser_version,
            transaction.locator.page_number,
            transaction.locator.table_ordinal,
            transaction.locator.row_ordinal,
            _position_text(transaction.locator.row_box.left),
            _position_text(transaction.locator.row_box.top),
            _position_text(transaction.locator.row_box.right),
            _position_text(transaction.locator.row_box.bottom),
            _source_row_id_for_order(transaction),
        ),
    )
    counts: dict[tuple[AccountRole, date, TransactionStatus, Direction, Decimal, str], int] = {}
    rebuilt: list[NormalizedTransaction] = []
    for transaction in ordered:
        key = (
            transaction.account_role,
            transaction.effective_date,
            transaction.status,
            transaction.direction,
            transaction.magnitude,
            transaction.normalized_description,
        )
        ordinal = counts.get(key, 0) + 1
        counts[key] = ordinal
        rebuilt.append(
            NormalizedTransaction(
                account_role=transaction.account_role,
                effective_date=transaction.effective_date,
                status=transaction.status,
                direction=transaction.direction,
                magnitude=transaction.magnitude,
                normalized_description=transaction.normalized_description,
                occurrence_ordinal=ordinal,
                source_sha256=transaction.source_sha256,
                locator=transaction.locator,
                extraction_method=transaction.extraction_method,
                parser_version=transaction.parser_version,
                parse_evidence=transaction.parse_evidence,
            )
        )
    return tuple(rebuilt)


def _source_row_id_for_order(transaction: NormalizedTransaction) -> str:
    """Require the terminal stable source identity used to break locator ties."""
    if transaction.source_row_id is None:
        _fail_contract()
    return transaction.source_row_id


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "RULES_SCHEMA_VERSION",
    "PRIVATE_JSON_MAX_BYTES",
    "MAX_CANONICAL_JSON_DEPTH",
    "MAX_BALANCE_ROW_HEIGHT",
    "AccountRole",
    "DocumentKind",
    "CashBasis",
    "Direction",
    "TransactionStatus",
    "BalanceKind",
    "BalanceBoundary",
    "ExtractionMethod",
    "PageKind",
    "PageFingerprint",
    "ActivityColumn",
    "EvidenceField",
    "CashRole",
    "MatcherKind",
    "PairAction",
    "AdjustmentAction",
    "TreasurerSlidesError",
    "PrivateInputError",
    "ContractError",
    "parse_iso_date",
    "parse_positive_money",
    "parse_nonnegative_money",
    "money_text",
    "normalize_description",
    "canonical_json_text",
    "canonical_json_bytes",
    "canonical_sha256",
    "assert_private_path_allowed",
    "resolve_private_relative_path",
    "BoundingBox",
    "SafeSourceLocator",
    "PositionedToken",
    "ActivityColumnBand",
    "ActivityRowEvidence",
    "ActivityStatusControl",
    "ActivityTableEvidence",
    "BalanceRowEvidence",
    "BalanceControlEvidence",
    "ParseEvidence",
    "PageEvidence",
    "DocumentSpec",
    "InputManifest",
    "BalanceObservation",
    "NormalizedTransaction",
    "TransactionSelector",
    "ClassificationRule",
    "OverlapResolution",
    "PairResolution",
    "TransactionAdjustment",
    "TreasurerRules",
    "StatementObservation",
    "build_source_row_id",
    "build_semantic_key",
    "load_input_manifest",
    "load_rules",
    "assign_occurrence_ordinals",
]
