"""Fail-closed native-text statement extraction for the optional Treasurer Slides flow.

The adapter deliberately recognizes only the fixed ``wf-v1`` contract.  It accepts a
manifest-declared account role and document kind, never a filename or an account
identifier, and returns the strict private data shapes defined in :mod:`models`.

``pypdfium2`` is an optional dependency.  Importing this module (and therefore every
legacy command) remains safe without the ``slides`` extra; the backend is imported only
when an extraction is requested.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import Enum
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast, runtime_checkable

from pta_finance.treasurer_slides.models import (
    ActivityColumn,
    ActivityColumnBand,
    ActivityRowEvidence,
    ActivityStatusControl,
    ActivityTableEvidence,
    BalanceBoundary,
    BalanceControlEvidence,
    BalanceKind,
    BalanceObservation,
    BalanceRowEvidence,
    BoundingBox,
    Direction,
    DocumentKind,
    DocumentSpec,
    EvidenceField,
    ExtractionMethod,
    NormalizedTransaction,
    PageEvidence,
    PageFingerprint,
    PageKind,
    ParseEvidence,
    PositionedToken,
    PrivateInputError,
    SafeSourceLocator,
    StatementObservation,
    TransactionStatus,
    TreasurerSlidesError,
    _assert_no_link_segments,
    assert_private_path_allowed,
    assign_occurrence_ordinals,
    normalize_description,
    parse_nonnegative_money,
    parse_positive_money,
)

NATIVE_PARSER_VERSION = "wf-v1"
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PDF_PAGES = 25
MAX_NATIVE_CHARACTERS = 2_000_000
MAX_RENDERED_PIXELS_PER_PAGE = 20_000_000
MAX_TRANSACTION_ROWS = 2_500
MIN_NATIVE_NONWHITESPACE_CHARACTERS = 80
MAX_NATIVE_TOKENS_PER_PAGE = 25_000
MAX_NATIVE_LINES_PER_PAGE = 10_000
MAX_NATIVE_PAGE_WIRE_BYTES = 16 * 1024 * 1024
MAX_NATIVE_EXTRACTION_SECONDS = 15
MAX_NATIVE_WORKER_MEMORY_BYTES = 512 * 1024 * 1024
MAX_NATIVE_WORKER_CPU_SECONDS = 10

# Public limit knobs remain deliberately lower than the non-negotiable worker ceilings.
# Tests may lower a public knob, but no caller can accidentally make the isolated native
# engine parse more than this fixed, release-reviewed envelope.
_HARD_MAX_NATIVE_CHARACTERS = 2_000_000
_HARD_MAX_PDF_BYTES = 25 * 1024 * 1024
_HARD_MAX_PDF_PAGES = 25
_HARD_MAX_TRANSACTION_ROWS = 2_500
_HARD_MAX_RENDERED_PIXELS_PER_PAGE = 20_000_000
_HARD_MAX_NATIVE_TOKENS_PER_PAGE = 25_000
_HARD_MAX_NATIVE_LINES_PER_PAGE = 10_000
_HARD_MAX_NATIVE_PAGE_WIRE_BYTES = 16 * 1024 * 1024
_HARD_MAX_NATIVE_EXTRACTION_SECONDS = 15
_HARD_MAX_NATIVE_WORKER_MEMORY_BYTES = 512 * 1024 * 1024
_HARD_MAX_NATIVE_WORKER_CPU_SECONDS = 10

_LETTER_WIDTH_POINTS = Decimal("612")
_LETTER_HEIGHT_POINTS = Decimal("792")
_DIMENSION_TOLERANCE_POINTS = Decimal("0.5")
_LINE_VERTICAL_TOLERANCE = Decimal("0.006")
_WORD_GAP = Decimal("0.006")
_BAND_GAP = Decimal("0.0001")
_PDF_DPI = Decimal("300")
_PDF_POINTS_PER_INCH = Decimal("72")
_POSITION_QUANTUM = Decimal("0.000000000001")
_MONEY_TOKEN_RE = re.compile(r"^[$]?((?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)\.[0-9]{2})$")
_UNSUPPORTED_EXPONENTIAL_NUMBER_RE = re.compile(
    r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[eE][+-]?[0-9]+$"
)
_DATE_TOKEN_RE = re.compile(
    r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}/[0-9]{1,2}(?:/[0-9]{2,4})?)$"
)
_PUBLIC_DATE_RANGE_RE = re.compile(
    r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})"
    r"\s*[-\u2013\u2014]\s*"
    r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4})"
)
_WRITTEN_DATE_RE = re.compile(
    r"(?<![a-z])(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\.?\s+(?:(?:[0-3]?\d)(?:st|nd|rd|th)?[,]?\s+)?"
    r"\d{4}(?!\d)",
    re.IGNORECASE,
)
# The short public ``A/C`` label can use a Unicode slash lookalike.  Keep the known
# grammar narrow, and let the reject-only NFA below catch other visual separators
# across visual lines or page boundaries before an identifier can reach evidence.
_ACCOUNT_ABBREVIATION_SEPARATOR_RE = r"[/\u2044\u2215\u29f8]"
_ACCOUNT_LABEL_TOKEN_RE = rf"(?:account|acct\.?|a\s*{_ACCOUNT_ABBREVIATION_SEPARATOR_RE}\s*c\.?)"
_ACCOUNT_LABEL_START_RE = rf"(?<![\w]){_ACCOUNT_LABEL_TOKEN_RE}(?![\w])"
_ACCOUNT_ABBREVIATION_PREFIX_RE = re.compile(
    rf"(?<![\w])a\s*{_ACCOUNT_ABBREVIATION_SEPARATOR_RE}\s*c\.?", re.IGNORECASE
)
_ACCOUNT_ATTACHED_IDENTIFIER_RE = re.compile(
    _ACCOUNT_LABEL_START_RE + r"[^\w#*xX\u20220-9.\-]*+"
    r"[#*xX\u20220-9.\-]{4,}",
    re.IGNORECASE,
)
_ACCOUNT_ID_LABEL_TERM_RE = (
    r"(?:[(\[]\s*)?id(?:entifier)?(?=\s*(?:[)\]>]\s*)?(?:[:;=,]\s*)?"
    r"(?:[#*xX\u20220-9]|\(|\[|<|$))"
)
_ACCOUNT_NUMBER_LABEL_TERM_RE = r"num(?:ber)?\.?(?=\s|[#*xX\u20220-9.,:;]|$)"
_ACCOUNT_LABEL_RE = re.compile(
    _ACCOUNT_LABEL_START_RE + r"(?:\s*+[:;,]\s*+|\s*+)(?:"
    rf"{_ACCOUNT_NUMBER_LABEL_TERM_RE}|"
    r"no\.?(?=\s|[#*xX\u20220-9.,:;]|$)|"
    rf"{_ACCOUNT_ID_LABEL_TERM_RE}|"
    r"ending\s+in(?=\s|[#*xX\u20220-9.,:;]|$)|#)",
    re.IGNORECASE,
)
_ACCOUNT_LABEL_PREFIX_RE = re.compile(rf"^{_ACCOUNT_LABEL_TOKEN_RE}\s*[:;,]?$", re.IGNORECASE)
_ACCOUNT_IDENTIFIER_FRAGMENT_RE = r"[#*xX\u20220-9.\-]+"
_ACCOUNT_LABEL_CANDIDATE_RE = re.compile(
    _ACCOUNT_LABEL_START_RE + r"[^\w]*(?:"
    rf"{_ACCOUNT_NUMBER_LABEL_TERM_RE}|no\.?(?=$|[^\w])|{_ACCOUNT_ID_LABEL_TERM_RE}|#|ending\b)",
    re.IGNORECASE,
)
_ACCOUNT_LABEL_SUFFIX_CANDIDATE_RE = re.compile(
    rf"^[^\w]*(?:{_ACCOUNT_NUMBER_LABEL_TERM_RE}|no\.?(?=$|[^\w])|{_ACCOUNT_ID_LABEL_TERM_RE}|#|ending\b)",
    re.IGNORECASE,
)
_ACCOUNT_ENDING_IN_RE = re.compile(
    rf"^ending\s+in(?:\s*[:;,])?(?:\s*{_ACCOUNT_IDENTIFIER_FRAGMENT_RE})*$",
    re.IGNORECASE,
)
_ACCOUNT_LABEL_SUFFIX_RE = re.compile(
    rf"^(?:(?:num(?:ber)?\.?|no\.?|id(?:entifier)?|#)(?:\s*[:;,])?(?:\s+ending\s+in(?:\s*[:;,])?)?|"
    rf"ending\s+in(?:\s*[:;,])?)(?:\s*{_ACCOUNT_IDENTIFIER_FRAGMENT_RE})*$",
    re.IGNORECASE,
)
_ACCOUNT_INLINE_PREFIX_RE = re.compile(
    rf"^{_ACCOUNT_LABEL_TOKEN_RE}(?:\s*+[:;,]\s*+|\s+)(?P<identifier>.+)$",
    re.IGNORECASE,
)
_ACCOUNT_VALUE_PREFIX_RE = re.compile(
    _ACCOUNT_LABEL_START_RE + r"(?:\s+(?:num(?:ber)?\.?|no\.?|id(?:entifier)?|ending\s+in))?"
    r"\s*(?:[^\w\s]{0,4}\s*|\s+)",
    re.IGNORECASE,
)
_UNSUPPORTED_IDENTIFIER_MASK_GLYPHS = frozenset(
    {
        "\u00d7",
        "\u2023",
        "\u2217",
        "\u2219",
        *(chr(codepoint) for codepoint in range(0x25A0, 0x2600)),
        *(chr(codepoint) for codepoint in range(0x2605, 0x2607)),
        *(chr(codepoint) for codepoint in range(0x26AA, 0x26AC)),
        *(chr(codepoint) for codepoint in range(0x2715, 0x273E)),
        *(chr(codepoint) for codepoint in range(0x2B1B, 0x2B25)),
    }
)
_MONEY_SEQUENCE_FRAGMENT_CHARACTERS = frozenset("0123456789$\u20ac\u00a3\u00a5+-.,()")
_CURRENCY_SYMBOLS = frozenset("$€£¥")
_COMPACT_ISO_CURRENCY_CODES = frozenset(
    {
        "AUD",
        "BRL",
        "CAD",
        "CHF",
        "CNY",
        "DKK",
        "EUR",
        "GBP",
        "HKD",
        "INR",
        "JPY",
        "KRW",
        "MXN",
        "NOK",
        "NZD",
        "PLN",
        "SEK",
        "SGD",
        "USD",
        "ZAR",
    }
)
_PUBLIC_PHONE_RE = re.compile(
    r"(?<![\w])(?:1[-.\s])?(?:\([2-9][0-9]{2}\)|[2-9][0-9]{2})[-.\s][0-9]{3}[-.\s][0-9]{4}(?![\w])"
)
_PUBLIC_PHONE_CONTACT_TERMS = ("call", "contact", "questions", "customer service")
_VISUAL_IDENTIFIER_DIGIT_LETTERS = frozenset("BbIiLlOoSsZz")


class _AccountScrubState(Enum):
    NONE = "none"
    AWAITING_IDENTIFIER = "awaiting_identifier"
    IDENTIFIER_RUN = "identifier_run"


def _screening_text(value: str) -> str:
    """Return an NFKC lexical view without changing retained source evidence."""

    return unicodedata.normalize("NFKC", value)


def _is_identifier_mask(character: str) -> bool:
    """Recognize bounded masking glyphs without changing retained source evidence."""

    normalized = _screening_text(character)
    return normalized in {"#", "*", "x", "X", "\u2022", "\u266f"}


def _is_identifier_mask_in_candidate(value: str, index: int) -> bool:
    """Recognize a mask without turning ordinary words containing ``x`` into IDs."""

    character = value[index]
    normalized = _screening_text(character)
    if not _is_identifier_mask(character):
        return False
    if normalized not in {"x", "X"}:
        return True
    adjacent_letters = tuple(
        _screening_text(value[neighbor]).casefold()
        for neighbor in (index - 1, index + 1)
        if 0 <= neighbor < len(value) and _screening_text(value[neighbor]).isalpha()
    )
    # Adjacent x/X glyphs can themselves be a mask run (``xx1234``), but any
    # other alphabetic neighbor makes this ordinary prose such as ``axis``.
    return not any(letter != "x" for letter in adjacent_letters)


def _is_unsupported_identifier_mask(character: str) -> bool:
    """Recognize explicitly unsupported alternate mask glyphs in an ID-shaped run."""

    return character in _UNSUPPORTED_IDENTIFIER_MASK_GLYPHS


def _is_identifier_syntax_character(character: str) -> bool:
    """Recognize an identifier digit, mask, or harmless intra-identifier punctuation."""

    normalized = _screening_text(character)
    return normalized.isdecimal() or _is_identifier_mask(character) or normalized in {".", "-"}


def _is_generic_identifier_syntax_character(character: str) -> bool:
    """Recognize generic identifier syntax only for the privacy screen.

    Account-label continuation parsing intentionally continues to use
    :func:`_is_identifier_syntax_character` and its smaller, fixed grammar.  The
    generic privacy screen needs a broader delimiter rule, however: a source can
    group a private number with any Unicode punctuation (for example ``U+00B7``
    MIDDLE DOT) or another non-alphanumeric, non-whitespace glyph.  Using that
    lexical property rather than an allowlist avoids silently retaining a newly
    encountered grouped identifier in positioned evidence.

    Alphabetic glyphs also join a candidate without contributing to its digit count.
    That lets the bounded count span a visually digit-like letter embedded in an
    otherwise numeric value, while ordinary prose with no sufficiently long numeric
    run remains outside the guard.

    This only says that the glyph *separates* numeric groups.  It does not make the
    glyph an account mask; mask classification and counting remain exclusively in
    :func:`_is_identifier_mask` (and the reject-only unsupported-mask path).  Exact
    full-token date and money exemptions remain earlier in
    :func:`_identifier_fragment`, before this rule is evaluated.
    """

    normalized = _screening_text(character)
    return (
        _is_identifier_syntax_character(character)
        or normalized.isalpha()
        or (
            bool(normalized)
            and all(not glyph.isalnum() and not glyph.isspace() for glyph in normalized)
        )
    )


def _identifier_counts(
    value: str,
    *,
    include_unsupported_masks: bool = False,
    include_visual_digit_lookalikes: bool = False,
) -> tuple[int, int]:
    """Count a bounded leading identifier candidate in a lexical screening view."""

    candidate = _screening_text(value).lstrip(" \t:;,()[]{}<>=|/\\")
    digits = 0
    visual_digit_letters = 0
    masks = 0
    for index, character in enumerate(candidate):
        normalized = _screening_text(character)
        if normalized.isdecimal():
            digits += 1
        elif include_visual_digit_lookalikes and (
            normalized in _VISUAL_IDENTIFIER_DIGIT_LETTERS
            or (
                not normalized.isascii()
                and len(normalized) == 1
                and (normalized.isalpha() or normalized.isnumeric())
            )
        ):
            visual_digit_letters += 1
        elif _is_identifier_mask_in_candidate(candidate, index) or (
            include_unsupported_masks and _is_unsupported_identifier_mask(character)
        ):
            masks += 1
        elif not _is_generic_identifier_syntax_character(character):
            break
    if include_visual_digit_lookalikes and digits:
        digits += visual_digit_letters
    return digits, masks


def _counts_form_short_identifier_candidate(digits: int, masks: int) -> bool:
    """Recognize the bounded short-ID threshold from count-only state."""

    return digits >= 4 or (masks > 0 and digits + masks >= 2)


@dataclass(frozen=True)
class _ShortIdentifierRun:
    """Count-only state for one possible short identifier across layout fragments."""

    real_digits: int = 0
    visual_digits: int = 0
    masks: int = 0

    def forms_candidate(self) -> bool:
        digits = self.real_digits + (self.visual_digits if self.real_digits else 0)
        return _counts_form_short_identifier_candidate(digits, self.masks)


def _is_known_public_short_identifier_exempt_token(value: str) -> bool:
    """Recognize a known date/money fact in public-heading screening only."""

    candidate = _screening_text(value).strip("()[]{}<>")
    money_candidate = candidate.removeprefix("-")
    return (
        _DATE_TOKEN_RE.fullmatch(candidate) is not None
        or _PUBLIC_DATE_RANGE_RE.fullmatch(candidate) is not None
        or _MONEY_TOKEN_RE.fullmatch(money_candidate) is not None
    )


def _is_visual_identifier_digit_character(character: str) -> bool:
    """Recognize one conservative visual-digit glyph without retaining its text."""

    normalized = _screening_text(character)
    return normalized in _VISUAL_IDENTIFIER_DIGIT_LETTERS or (
        not normalized.isascii()
        and len(normalized) == 1
        and (normalized.isalpha() or normalized.isnumeric())
    )


def _is_short_identifier_soft_separator(character: str) -> bool:
    """Keep an identifier run through visual layout separators only."""

    normalized = _screening_text(character)
    return bool(normalized) and (
        normalized.isspace()
        or unicodedata.category(character) == "Cf"
        or (not character.isascii() and len(normalized) != 1)
        or all(not glyph.isalnum() and not glyph.isspace() for glyph in normalized)
    )


def _advance_short_identifier_run(
    run: _ShortIdentifierRun,
    line: list[tuple[str, BoundingBox]],
    *,
    include_known_financial_tokens: bool,
) -> tuple[_ShortIdentifierRun, bool]:
    """Advance one identifier-shaped run across token and visual-line boundaries.

    The reduced state intentionally never stores source text.  It keeps whitespace,
    punctuation, and Unicode-format layout artifacts inside one run, while ordinary
    prose resets it.  Visual digit lookalikes count only after the same run contains a
    real digit, so a split ``1 | O | 2 | 3`` cannot evade the short-ID boundary but an
    ordinary word beginning with ``O`` remains harmless.
    """

    current = run
    for value, _ in line:
        for part in _screening_text(value).split():
            if not include_known_financial_tokens:
                malformed_money_digits, _ = _identifier_counts(part)
                if _is_known_public_short_identifier_exempt_token(part) or (
                    _is_monetary_like_text(part) and malformed_money_digits < 4
                ):
                    current = _ShortIdentifierRun()
                    continue
            for character in part:
                normalized = _screening_text(character)
                if normalized.isdecimal():
                    current = _ShortIdentifierRun(
                        real_digits=current.real_digits + 1,
                        visual_digits=current.visual_digits,
                        masks=current.masks,
                    )
                elif _is_identifier_mask(character) or _is_unsupported_identifier_mask(character):
                    current = _ShortIdentifierRun(
                        real_digits=current.real_digits,
                        visual_digits=current.visual_digits,
                        masks=current.masks + 1,
                    )
                elif _is_visual_identifier_digit_character(character):
                    current = _ShortIdentifierRun(
                        real_digits=current.real_digits,
                        visual_digits=current.visual_digits + 1,
                        masks=current.masks,
                    )
                elif not _is_short_identifier_soft_separator(character):
                    current = _ShortIdentifierRun()
                if current.forms_candidate():
                    return current, True
    return current, False


def _advance_nonfinancial_short_identifier_run(
    run: _ShortIdentifierRun,
    line: list[tuple[str, BoundingBox]],
    *,
    provisional_phone_token_indexes: frozenset[int] = frozenset(),
) -> tuple[_ShortIdentifierRun, bool]:
    """Advance a public-heading short-ID run while excluding known public facts."""

    for token_index, token in enumerate(line):
        if token_index in provisional_phone_token_indexes:
            # A designated public phone is an explicit visual delimiter, not an
            # identifier fragment that may be joined to adjacent short values.
            run = _ShortIdentifierRun()
            continue
        value, box = token
        run, found = _advance_short_identifier_run(
            run,
            [(_WRITTEN_DATE_RE.sub(" ", value), box)],
            include_known_financial_tokens=False,
        )
        if found:
            return run, True
    return run, False


def _line_has_nonfinancial_short_identifier_candidate(
    line: list[tuple[str, BoundingBox]],
    *,
    provisional_phone_token_indexes: frozenset[int] = frozenset(),
) -> bool:
    """Recognize a short suffix that invalidates a public-account-heading exception."""

    _, found = _advance_nonfinancial_short_identifier_run(
        _ShortIdentifierRun(),
        line,
        provisional_phone_token_indexes=provisional_phone_token_indexes,
    )
    return found


def _is_identifier_candidate(value: str, *, include_unsupported_masks: bool = False) -> bool:
    """Recognize a short account-labelled value or bounded generic identifier shape."""

    digits, masks = _identifier_counts(value, include_unsupported_masks=include_unsupported_masks)
    return _counts_form_short_identifier_candidate(digits, masks)


def _has_unsupported_non_ascii_identifier_syntax(value: str) -> bool:
    """Reject identifier glyphs NFKC cannot reduce to the explicit v1 grammar."""

    for character in value:
        if character.isascii():
            continue
        normalized = _screening_text(character)
        if character.isdecimal() and normalized == character:
            return True
        if _is_unsupported_identifier_mask(character):
            return True
    return False


def _is_confusable_label_word(value: str, expected: str) -> bool:
    """Recognize a bounded non-ASCII letter substitution in a fixed label word."""

    candidate = _screening_text(value).casefold().strip(".,:;()[]{}<>")
    base = "".join(
        character
        for character in unicodedata.normalize("NFD", candidate)
        if unicodedata.category(character) not in {"Mn", "Mc", "Me"}
    )
    return (
        len(base) == len(expected)
        and any(not character.isascii() for character in candidate)
        and all(
            character == expected[index] or (not character.isascii() and character.isalpha())
            for index, character in enumerate(base)
        )
    )


def _is_confusable_account_word(value: str) -> bool:
    """Conservatively recognize an account label with non-ASCII letter substitutions."""

    return any(_is_confusable_label_word(value, expected) for expected in ("account", "acct"))


def _account_label_words(value: str) -> tuple[str, ...]:
    """Split the small account-label grammar without making punctuation generally unsafe."""

    return tuple(
        word for word in re.split(r"[\s:;=,()\[\]{}<>/\\#]+", _screening_text(value)) if word
    )


_ASCII_LEET_LABEL_TRANSLATION = str.maketrans(
    {
        "@": "a",
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "8": "b",
    }
)
# These substitutions are deliberately limited to the fixed account-label grammar
# below.  They are not general text normalization: each one is a common visual
# rendering of the expected label character, and remains reject-only.
_VISUAL_ACCOUNT_LABEL_CHARACTER_ALIASES = {
    "!": "i",
    "|": "i",
    "l": "i",
    "\u212e": "e",
}
_ACCOUNT_QUALIFIER_WORDS = frozenset({"num", "number", "no", "id", "identifier"})
_ACCOUNT_ENDING_IN_STREAM = "endingin"


def _has_tight_ascii_label_separator(value: str) -> bool:
    """Recognize a non-space separator between ASCII label components."""

    return re.search(r"(?<=[A-Za-z0-9])(?:_|[^A-Za-z0-9\s])+(?=[A-Za-z0-9])", value) is not None


def _has_split_ascii_label_spacing(value: str) -> bool:
    """Recognize a label word split across visual text objects by whitespace."""

    return re.search(r"[A-Za-z]\s+[A-Za-z]", value) is not None


def _ascii_obfuscated_label_stream(value: str) -> tuple[str, bool]:
    """Collapse visual ASCII label obfuscation without translating an ID suffix."""

    normalized = _screening_text(value)
    characters: list[str] = []
    obfuscated = False
    for index, character in enumerate(normalized):
        if character.isascii() and character.isalpha():
            characters.append(character.casefold())
            continue
        if character.isascii() and character.isdecimal():
            adjacent_to_letter = (index > 0 and normalized[index - 1].isalpha()) or (
                index + 1 < len(normalized) and normalized[index + 1].isalpha()
            )
            if ord(character) in _ASCII_LEET_LABEL_TRANSLATION and adjacent_to_letter:
                characters.append(_ASCII_LEET_LABEL_TRANSLATION[ord(character)])
                obfuscated = True
            else:
                characters.append(character)
            continue
        if ord(character) in _ASCII_LEET_LABEL_TRANSLATION:
            characters.append(_ASCII_LEET_LABEL_TRANSLATION[ord(character)])
            obfuscated = True
            continue
        if not character.isspace():
            obfuscated = True
    return "".join(characters), obfuscated


def _has_short_identifier_context(value: str) -> bool:
    """Recognize a short identifier only inside an already suspicious label context."""

    digits = sum(_screening_text(character).isdecimal() for character in value)
    masks = sum(
        _is_identifier_mask(character) or _is_unsupported_identifier_mask(character)
        for character in value
    )
    return digits >= 4 or (masks > 0 and digits + masks >= 2)


def _obfuscated_account_label_context(
    line: list[tuple[str, BoundingBox]], *, account_prefix: bool = False
) -> bool:
    """Fail closed on leetspeak or tightly-separated account labels with a short ID.

    Supported ASCII account labels are handled by the explicit scrub grammar below.
    This detector only owns visually altered forms, whose identifier boundary is too
    ambiguous to redact safely.  It is deliberately limited to the fixed account and
    qualifier vocabulary, so public phrases such as ``Account Information`` and
    ``Account Numbering`` remain ordinary prose.
    """

    text = _raw_line_text(line)
    stream, leet_obfuscated = _ascii_obfuscated_label_stream(text)
    starts = (
        (0,)
        if account_prefix
        else tuple(match.end() for match in re.finditer(r"(?:account|acct)", stream))
    )
    is_obfuscated_label = (
        leet_obfuscated
        or _has_tight_ascii_label_separator(text)
        or _has_split_ascii_label_spacing(text)
    )
    if not is_obfuscated_label:
        return False
    for start in starts:
        remainder = stream[start:]
        if (
            remainder
            and (remainder[0].isdecimal() or _is_identifier_mask(remainder[0]))
            and _has_short_identifier_context(text)
        ):
            return True
        qualifier_ends = [
            len(qualifier)
            for qualifier in _ACCOUNT_QUALIFIER_WORDS
            if remainder.startswith(qualifier)
        ]
        if remainder.startswith(_ACCOUNT_ENDING_IN_STREAM):
            qualifier_ends.append(len(_ACCOUNT_ENDING_IN_STREAM))
        for qualifier_end in qualifier_ends:
            suffix = remainder[qualifier_end:]
            # An altered label that ends at a known qualifier can be continued on
            # the next visual line.  Reject it before a following ID can leak.
            if not suffix:
                return True
            if (
                suffix[0].isdecimal() or _is_identifier_mask(suffix[0])
            ) and _has_short_identifier_context(text):
                return True
    return False


def _is_obfuscated_account_word_line(line: list[tuple[str, BoundingBox]]) -> bool:
    """Reject a standalone leetspeak account prefix before it can wrap an identifier."""

    text = _screening_text(_raw_line_text(line))
    stream, _ = _ascii_obfuscated_label_stream(text)
    standard = text.strip(" \t:;,.()[]{}<>").casefold()
    return stream in {"account", "acct"} and standard != stream


def _account_qualifier_match(value: str, expected: str) -> tuple[bool, bool]:
    """Return whether a qualifier word matches and whether it used an altered glyph."""

    normalized = _screening_text(value).casefold().strip(".,:;()[]{}<>")
    if normalized == expected:
        return True, False
    confusable = _is_confusable_label_word(value, expected)
    if confusable:
        return True, True
    stream, leet_obfuscated = _ascii_obfuscated_label_stream(value)
    return leet_obfuscated and stream == expected, leet_obfuscated and stream == expected


def _ending_in_qualifier(words: tuple[str, ...], start: int) -> tuple[int, bool] | None:
    """Recognize the bounded ``ending in`` account qualifier phrase."""

    if start + 1 >= len(words):
        return None
    ending_matches, ending_confusable = _account_qualifier_match(words[start], "ending")
    in_matches, in_confusable = _account_qualifier_match(words[start + 1], "in")
    if not ending_matches or not in_matches:
        return None
    return start + 2, ending_confusable or in_confusable


def _confusable_account_qualifier_context(
    line: list[tuple[str, BoundingBox]], *, account_prefix: bool = False
) -> bool:
    """Identify only an account label with a non-ASCII qualifier lookalike.

    The explicit qualifier set mirrors the supported account-label grammar.  This is
    intentionally narrower than generic fuzzy matching: phrases such as ``Account
    Information`` and ordinary no-ID prose do not become account-label contexts.
    """

    words = _account_label_words(_raw_line_text(line))
    candidate_starts: tuple[int, ...]
    if account_prefix:
        candidate_starts = (0,)
    else:
        candidate_starts = tuple(
            index + 1
            for index, word in enumerate(words)
            if _screening_text(word).casefold().strip(".,:;()[]{}<>") in {"account", "acct"}
            or _is_confusable_account_word(word)
        )
    for start in candidate_starts:
        if start >= len(words):
            continue
        qualifier_matches = False
        qualifier_confusable = False
        for expected in ("num", "number", "no", "id", "identifier"):
            matches, confusable = _account_qualifier_match(words[start], expected)
            if matches:
                qualifier_matches = True
                qualifier_confusable = confusable
                end = start + 1
                optional_ending = _ending_in_qualifier(words, end)
                if optional_ending is not None:
                    _, ending_confusable = optional_ending
                    qualifier_confusable = qualifier_confusable or ending_confusable
                break
        if not qualifier_matches:
            ending = _ending_in_qualifier(words, start)
            if ending is not None:
                _, qualifier_confusable = ending
                qualifier_matches = True
        if qualifier_matches and qualifier_confusable:
            return True
    return False


def _abbreviated_account_qualifier_context(line: list[tuple[str, BoundingBox]]) -> bool:
    """Reject altered qualifiers following the bounded public ``A/C`` abbreviation.

    ``A/C`` is a common account-label abbreviation, but it is too ambiguous to retain
    when a qualifier uses a confusable or ASCII-leet spelling. Canonical spellings are
    handled by the explicit scrub grammar; altered spellings are rejected before their
    short identifier can become positioned evidence.
    """

    text = _screening_text(_raw_line_text(line))
    match = _ACCOUNT_ABBREVIATION_PREFIX_RE.search(text)
    if match is None:
        return False
    remainder = text[match.end() :]
    has_word_boundary = match.end() == len(text) or not (
        text[match.end()].isalnum() or text[match.end()] == "_"
    )
    stream, leet_obfuscated = _ascii_obfuscated_label_stream(remainder)
    if not has_word_boundary:
        return _has_short_identifier_context(remainder) and any(
            stream.startswith(qualifier)
            for qualifier in (*_ACCOUNT_QUALIFIER_WORDS, _ACCOUNT_ENDING_IN_STREAM)
        )
    words = _account_label_words(remainder)
    if not words:
        return False
    for expected in _ACCOUNT_QUALIFIER_WORDS:
        matches, confusable = _account_qualifier_match(words[0], expected)
        if matches:
            return confusable
    ending = _ending_in_qualifier(words, 0)
    if ending is not None:
        return ending[1]
    return (
        leet_obfuscated
        and _has_short_identifier_context(remainder)
        and any(
            stream.startswith(qualifier)
            for qualifier in (*_ACCOUNT_QUALIFIER_WORDS, _ACCOUNT_ENDING_IN_STREAM)
        )
    )


class _PdfTextPage(Protocol):
    def count_chars(self) -> int: ...

    def get_text_range(self, index: int = 0, count: int = -1, errors: str = "ignore") -> str: ...

    def get_charbox(self, index: int, loose: bool = False) -> tuple[float, float, float, float]: ...

    def close(self) -> None: ...


class _PdfPage(Protocol):
    def get_size(self) -> tuple[float, float]: ...

    def get_textpage(self) -> _PdfTextPage: ...

    def close(self) -> None: ...


class _PdfDocument(Protocol):
    def __len__(self) -> int: ...

    def get_page(self, index: int) -> _PdfPage: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class _RawCharacter:
    index: int
    text: str
    box: BoundingBox


@dataclass(frozen=True)
class _LayoutToken:
    ordinal: int
    text: str
    box: BoundingBox
    line_ordinal: int


@dataclass(frozen=True)
class _NativeLine:
    ordinal: int
    token_ordinals: tuple[int, ...]


@dataclass(frozen=True)
class _NativePage:
    page_number: int
    tokens: tuple[_LayoutToken, ...]
    lines: tuple[_NativeLine, ...]
    provisional_contact_phone_token_ordinals: tuple[int, ...] = ()

    def line_tokens(self, line: _NativeLine) -> tuple[_LayoutToken, ...]:
        return tuple(self.tokens[ordinal - 1] for ordinal in line.token_ordinals)

    def token(self, ordinal: int) -> _LayoutToken:
        return self.tokens[ordinal - 1]


@dataclass(frozen=True)
class _NativeExtractionLimits:
    """The one serializable native-engine envelope shared by parent and worker."""

    max_pages: int
    max_transaction_rows: int
    max_pdf_bytes: int
    max_characters: int
    max_rendered_pixels_per_page: int
    max_tokens_per_page: int
    max_lines_per_page: int
    max_wire_bytes: int
    wall_seconds: int
    worker_memory_bytes: int
    worker_cpu_seconds: int


class _NativeWorkerConnection(Protocol):
    def recv_bytes(self, maxlength: int | None = ...) -> bytes: ...

    def send_bytes(self, buffer: bytes) -> None: ...

    def close(self) -> None: ...


@dataclass
class _NativeWorkerIoResult:
    value: bytes | None = None
    error: BaseException | None = None


class _NativeWorkerProcess(Protocol):
    def join(self, timeout: float | None = ...) -> None: ...

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def close(self) -> None: ...


class StatementExtractionError(TreasurerSlidesError):
    """A source document does not meet the public, versioned extraction contract."""


class SlidesDependencyError(TreasurerSlidesError):
    """The caller requested optional native-PDF support without its dependency extra."""


@runtime_checkable
class StatementExtractor(Protocol):
    """Read one manifest-declared private statement into its normalized observation."""

    def extract(
        self,
        source_path: Path,
        *,
        document_ordinal: int,
        document: DocumentSpec,
    ) -> StatementObservation:
        """Extract one statement without exposing source text or identifiers in errors."""


def _require_pdfium() -> ModuleType:
    """Load the optional backend only at the native extraction boundary."""

    try:
        return importlib.import_module("pypdfium2")
    except ModuleNotFoundError as exc:
        if exc.name == "pypdfium2":
            raise SlidesDependencyError(
                "Treasurer Slides native statement parsing requires the optional 'slides' extra"
            ) from None
        raise


def _require_pdfium_distribution() -> None:
    """Check the optional distribution without importing its native extension in the broker."""

    try:
        metadata.version("pypdfium2")
    except metadata.PackageNotFoundError:
        raise SlidesDependencyError(
            "Treasurer Slides native statement parsing requires the optional 'slides' extra"
        ) from None


def _private_input_error() -> PrivateInputError:
    return PrivateInputError("private input does not satisfy the Treasurer Slides contract")


def _bounded_pdf_byte_limit(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= _HARD_MAX_PDF_BYTES
    ):
        raise _private_input_error()
    return value


def _page_error(document_ordinal: int, page_number: int | None = None) -> StatementExtractionError:
    location = f"document {document_ordinal}"
    if page_number is not None:
        location = f"{location} page {page_number}"
    return StatementExtractionError(f"Treasurer Slides statement extraction failed for {location}")


def _bounded_native_limit(value: object, *, ceiling: int, document_ordinal: int) -> int:
    """Accept a release-configured worker limit only inside its hard ceiling."""

    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= ceiling:
        raise _page_error(document_ordinal)
    return value


def _native_extraction_limits(document_ordinal: int) -> _NativeExtractionLimits:
    """Snapshot bounded settings before handing private bytes to the worker."""

    return _NativeExtractionLimits(
        max_pages=_bounded_native_limit(
            MAX_PDF_PAGES, ceiling=_HARD_MAX_PDF_PAGES, document_ordinal=document_ordinal
        ),
        max_transaction_rows=_bounded_native_limit(
            MAX_TRANSACTION_ROWS,
            ceiling=_HARD_MAX_TRANSACTION_ROWS,
            document_ordinal=document_ordinal,
        ),
        max_pdf_bytes=_bounded_native_limit(
            MAX_PDF_BYTES, ceiling=_HARD_MAX_PDF_BYTES, document_ordinal=document_ordinal
        ),
        max_characters=_bounded_native_limit(
            MAX_NATIVE_CHARACTERS,
            ceiling=_HARD_MAX_NATIVE_CHARACTERS,
            document_ordinal=document_ordinal,
        ),
        max_rendered_pixels_per_page=_bounded_native_limit(
            MAX_RENDERED_PIXELS_PER_PAGE,
            ceiling=_HARD_MAX_RENDERED_PIXELS_PER_PAGE,
            document_ordinal=document_ordinal,
        ),
        max_tokens_per_page=_bounded_native_limit(
            MAX_NATIVE_TOKENS_PER_PAGE,
            ceiling=_HARD_MAX_NATIVE_TOKENS_PER_PAGE,
            document_ordinal=document_ordinal,
        ),
        max_lines_per_page=_bounded_native_limit(
            MAX_NATIVE_LINES_PER_PAGE,
            ceiling=_HARD_MAX_NATIVE_LINES_PER_PAGE,
            document_ordinal=document_ordinal,
        ),
        max_wire_bytes=_bounded_native_limit(
            MAX_NATIVE_PAGE_WIRE_BYTES,
            ceiling=_HARD_MAX_NATIVE_PAGE_WIRE_BYTES,
            document_ordinal=document_ordinal,
        ),
        wall_seconds=_bounded_native_limit(
            MAX_NATIVE_EXTRACTION_SECONDS,
            ceiling=_HARD_MAX_NATIVE_EXTRACTION_SECONDS,
            document_ordinal=document_ordinal,
        ),
        worker_memory_bytes=_bounded_native_limit(
            MAX_NATIVE_WORKER_MEMORY_BYTES,
            ceiling=_HARD_MAX_NATIVE_WORKER_MEMORY_BYTES,
            document_ordinal=document_ordinal,
        ),
        worker_cpu_seconds=_bounded_native_limit(
            MAX_NATIVE_WORKER_CPU_SECONDS,
            ceiling=_HARD_MAX_NATIVE_WORKER_CPU_SECONDS,
            document_ordinal=document_ordinal,
        ),
    )


_NATIVE_LIMIT_FIELD_CEILINGS: tuple[tuple[str, int], ...] = (
    ("max_pages", _HARD_MAX_PDF_PAGES),
    ("max_transaction_rows", _HARD_MAX_TRANSACTION_ROWS),
    ("max_pdf_bytes", _HARD_MAX_PDF_BYTES),
    ("max_characters", _HARD_MAX_NATIVE_CHARACTERS),
    ("max_rendered_pixels_per_page", _HARD_MAX_RENDERED_PIXELS_PER_PAGE),
    ("max_tokens_per_page", _HARD_MAX_NATIVE_TOKENS_PER_PAGE),
    ("max_lines_per_page", _HARD_MAX_NATIVE_LINES_PER_PAGE),
    ("max_wire_bytes", _HARD_MAX_NATIVE_PAGE_WIRE_BYTES),
    ("wall_seconds", _HARD_MAX_NATIVE_EXTRACTION_SECONDS),
    ("worker_memory_bytes", _HARD_MAX_NATIVE_WORKER_MEMORY_BYTES),
    ("worker_cpu_seconds", _HARD_MAX_NATIVE_WORKER_CPU_SECONDS),
)


def _serialize_native_limits(limits: _NativeExtractionLimits) -> str:
    """Serialize only bounded public limit values for the sandboxed worker command line."""

    return json.dumps(
        {field: getattr(limits, field) for field, _ in _NATIVE_LIMIT_FIELD_CEILINGS},
        sort_keys=True,
        separators=(",", ":"),
    )


def _deserialize_native_limits(value: str, document_ordinal: int) -> _NativeExtractionLimits:
    """Revalidate the public broker envelope before the child receives any PDF bytes."""

    if not isinstance(value, str):
        raise _page_error(document_ordinal)
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        raise _page_error(document_ordinal) from None
    if not isinstance(decoded, dict) or set(decoded) != {
        field for field, _ in _NATIVE_LIMIT_FIELD_CEILINGS
    }:
        raise _page_error(document_ordinal)
    checked = {
        field: _bounded_native_limit(
            decoded[field], ceiling=ceiling, document_ordinal=document_ordinal
        )
        for field, ceiling in _NATIVE_LIMIT_FIELD_CEILINGS
    }
    return _NativeExtractionLimits(**checked)


def _close_quietly(value: object | None) -> None:
    if value is None:
        return
    closer = getattr(value, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _read_bounded_pdf(source_path: Path, *, maximum_bytes: int | None = None) -> bytes:
    """Read one private PDF through the checked descriptor that passed the path gate."""

    effective_maximum = _bounded_pdf_byte_limit(
        MAX_PDF_BYTES if maximum_bytes is None else maximum_bytes
    )
    absolute = assert_private_path_allowed(source_path, require_file=True)
    try:
        expected = absolute.lstat()
    except OSError:
        raise _private_input_error() from None
    if not stat.S_ISREG(expected.st_mode) or expected.st_size > effective_maximum:
        raise _private_input_error()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(absolute, flags)
    except OSError:
        raise _private_input_error() from None
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > effective_maximum:
                raise _private_input_error()
            if not os.path.samestat(expected, opened):
                raise _private_input_error()
            try:
                _assert_no_link_segments(absolute)
            except PrivateInputError:
                raise _private_input_error() from None
            try:
                named = absolute.lstat()
            except OSError:
                raise _private_input_error() from None
            if not os.path.samestat(opened, named):
                raise _private_input_error()
            payload = handle.read(effective_maximum + 1)
    except PrivateInputError:
        raise
    except OSError:
        raise _private_input_error() from None
    if len(payload) > effective_maximum:
        raise _private_input_error()
    return payload


def _open_pdf_document(pdfium: ModuleType, payload: bytes, document_ordinal: int) -> _PdfDocument:
    factory = getattr(pdfium, "PdfDocument", None)
    if not callable(factory):
        raise _page_error(document_ordinal)
    try:
        return cast(_PdfDocument, factory(payload))
    except Exception:
        raise _page_error(document_ordinal) from None


def _decimal_from_backend(value: object, document_ordinal: int, page_number: int) -> Decimal:
    if isinstance(value, bool):
        raise _page_error(document_ordinal, page_number)
    try:
        result = Decimal(str(value))
    except Exception:
        raise _page_error(document_ordinal, page_number) from None
    if not result.is_finite():
        raise _page_error(document_ordinal, page_number)
    return result


def _position(value: Decimal, document_ordinal: int, page_number: int) -> Decimal:
    try:
        result = value.quantize(_POSITION_QUANTUM, rounding=ROUND_HALF_EVEN)
    except InvalidOperation:
        raise _page_error(document_ordinal, page_number) from None
    if not Decimal("0") <= result <= Decimal("1"):
        raise _page_error(document_ordinal, page_number)
    return result


def _page_dimensions(
    page: _PdfPage,
    document_ordinal: int,
    page_number: int,
    limits: _NativeExtractionLimits,
) -> tuple[Decimal, Decimal]:
    try:
        raw_size = page.get_size()
    except Exception:
        raise _page_error(document_ordinal, page_number) from None
    if not isinstance(raw_size, tuple) or len(raw_size) != 2:
        raise _page_error(document_ordinal, page_number)
    width = _decimal_from_backend(raw_size[0], document_ordinal, page_number)
    height = _decimal_from_backend(raw_size[1], document_ordinal, page_number)
    if (
        width <= 0
        or height <= 0
        or abs(width - _LETTER_WIDTH_POINTS) > _DIMENSION_TOLERANCE_POINTS
        or abs(height - _LETTER_HEIGHT_POINTS) > _DIMENSION_TOLERANCE_POINTS
    ):
        raise _page_error(document_ordinal, page_number)
    rendered_width = int(
        (width * _PDF_DPI / _PDF_POINTS_PER_INCH).to_integral_value(rounding=ROUND_CEILING)
    )
    rendered_height = int(
        (height * _PDF_DPI / _PDF_POINTS_PER_INCH).to_integral_value(rounding=ROUND_CEILING)
    )
    if rendered_width * rendered_height > limits.max_rendered_pixels_per_page:
        raise _page_error(document_ordinal, page_number)
    return width, height


def _char_box(
    text_page: _PdfTextPage,
    index: int,
    width: Decimal,
    height: Decimal,
    document_ordinal: int,
    page_number: int,
) -> BoundingBox:
    try:
        raw_box = text_page.get_charbox(index)
    except Exception:
        try:
            raw_box = text_page.get_charbox(index, loose=True)
        except Exception:
            raise _page_error(document_ordinal, page_number) from None
    if not isinstance(raw_box, tuple) or len(raw_box) != 4:
        raise _page_error(document_ordinal, page_number)
    left = _decimal_from_backend(raw_box[0], document_ordinal, page_number)
    bottom = _decimal_from_backend(raw_box[1], document_ordinal, page_number)
    right = _decimal_from_backend(raw_box[2], document_ordinal, page_number)
    top = _decimal_from_backend(raw_box[3], document_ordinal, page_number)
    if left < 0 or bottom < 0 or right > width or top > height or left >= right or bottom >= top:
        raise _page_error(document_ordinal, page_number)
    try:
        return BoundingBox(
            left=_position(left / width, document_ordinal, page_number),
            top=_position((height - top) / height, document_ordinal, page_number),
            right=_position(right / width, document_ordinal, page_number),
            bottom=_position((height - bottom) / height, document_ordinal, page_number),
        )
    except (InvalidOperation, TreasurerSlidesError):
        raise _page_error(document_ordinal, page_number) from None


def _box_for_tokens(tokens: tuple[_RawCharacter, ...] | tuple[_LayoutToken, ...]) -> BoundingBox:
    if not tokens:
        raise ValueError("empty box")
    return BoundingBox(
        left=min(token.box.left for token in tokens),
        top=min(token.box.top for token in tokens),
        right=max(token.box.right for token in tokens),
        bottom=max(token.box.bottom for token in tokens),
    )


def _is_account_identifier_line(line: list[tuple[str, BoundingBox]]) -> bool:
    compact = "".join(_screening_text("".join(text for text, _ in line)).split())
    return (
        bool(compact)
        and all(_is_identifier_syntax_character(character) for character in compact)
        and _is_identifier_candidate(compact)
    )


def _has_compact_iso_currency_prefix(value: str) -> bool:
    """Recognize a bounded three-letter ISO currency prefix before an amount shape."""

    normalized = _screening_text(value)
    if len(normalized) < 4 or normalized[:3].upper() not in _COMPACT_ISO_CURRENCY_CODES:
        return False
    suffix = normalized[3:]
    return suffix[:1] in _CURRENCY_SYMBOLS or suffix[:1] in {"+", "-"} or suffix[:1].isdecimal()


def _is_monetary_like_text(text: str) -> bool:
    """Recognize bounded money syntax without classifying bare identifier digits as money."""

    value = _screening_text(text).strip(";:")
    if len(value) >= 2 and value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    if not value:
        return False
    if _UNSUPPORTED_EXPONENTIAL_NUMBER_RE.fullmatch(value) is not None:
        return True
    if _has_compact_iso_currency_prefix(value) or value[:1] in _CURRENCY_SYMBOLS:
        return True
    signed = value[:1] in {"+", "-"}
    unsigned = value[1:] if signed else value
    if _has_compact_iso_currency_prefix(unsigned) or unsigned[:1] in _CURRENCY_SYMBOLS:
        return True
    if not any(character.isdecimal() for character in unsigned):
        return False
    if signed:
        return True
    # A plain digit run is an account-identifier candidate, not a monetary
    # candidate. Decimal and grouping punctuation distinguish amount-shaped
    # tokens, including malformed forms such as ``1,000.000``.
    return unsigned[:1].isdecimal() and ("." in unsigned or "," in unsigned)


def _is_monetary_sequence_fragment(text: str) -> bool:
    """Recognize one bounded component of a visually split amount candidate."""

    value = _screening_text(text).strip()
    return bool(value) and (
        value.upper() in _COMPACT_ISO_CURRENCY_CODES
        or all(
            character.isdecimal() or character in _MONEY_SEQUENCE_FRAGMENT_CHARACTERS
            for character in value
        )
    )


def _has_fragmented_monetary_like_sequence(texts: tuple[str, ...]) -> bool:
    """Reject an amount-shaped visual sequence split into separate PDF words.

    PDF content streams can separate punctuation or digit groups into individual
    positioned words. Join only adjacent visual fragments composed solely of
    amount syntax; date tokens, prose, and ordinary hyphenated phone numbers
    terminate the candidate sequence.
    """

    fragments: list[str] = []
    for text in (*texts, ""):
        value = text.strip()
        if _is_monetary_sequence_fragment(value):
            fragments.append(value)
            continue
        if len(fragments) > 1 and _is_monetary_like_text("".join(fragments)):
            return True
        fragments.clear()
    return False


def _raw_line_has_financial_candidate(line: list[tuple[str, BoundingBox]]) -> bool:
    """Recognize the same date and amount tokens used after layout construction."""

    texts = tuple(text for text, _ in line)
    return _has_fragmented_monetary_like_sequence(texts) or any(
        _DATE_TOKEN_RE.fullmatch(_screening_text(part).strip(".,:;")) is not None
        or _is_monetary_like_text(part)
        for text in texts
        for part in _screening_text(text).split()
    )


def _raw_line_text(line: list[tuple[str, BoundingBox]]) -> str:
    return " ".join(text for text, _ in line).strip()


def _has_known_boilerplate_context(lines: list[list[tuple[str, BoundingBox]]]) -> bool:
    page_text = normalize_description(" ".join(_raw_line_text(line) for line in lines))
    return any(_has_phrase(page_text, marker) for marker in _KNOWN_BOILERPLATE_MARKERS)


def _provisional_contact_phone_token_indexes(
    line: list[tuple[str, BoundingBox]], has_known_boilerplate_context: bool
) -> frozenset[int]:
    """Temporarily classify exact contact-phone tokens without retaining an exemption.

    A phone-shaped source token is never generally safe evidence: it is allowed through
    the generic identifier screen only while the whole page is being recognized.  The
    recognizer later accepts this provisional exception exclusively for known
    boilerplate, and model construction omits the original token there.
    """

    if not has_known_boilerplate_context:
        return frozenset()
    normalized = normalize_description(_raw_line_text(line))
    if not any(_has_phrase(normalized, term) for term in _PUBLIC_PHONE_CONTACT_TERMS):
        return frozenset()
    return frozenset(
        index
        for index, (text, _) in enumerate(line)
        if _PUBLIC_PHONE_RE.fullmatch(text) is not None
    )


def _identifier_fragment(
    text: str,
    *,
    include_unsupported_masks: bool = False,
    include_known_financial_tokens: bool = False,
) -> str | None:
    """Return a trailing identifier-shaped fragment after harmless outer punctuation."""

    value = _screening_text(text).strip(" \t:;,()[]{}<>=|/\\")
    if not value or (
        not include_known_financial_tokens and _is_known_public_short_identifier_exempt_token(value)
    ):
        return None
    start = len(value)
    while start > 0 and (
        _is_generic_identifier_syntax_character(value[start - 1])
        or (include_unsupported_masks and _is_unsupported_identifier_mask(value[start - 1]))
    ):
        start -= 1
    fragment = value[start:]
    if not fragment or (
        start > 0
        and value[start - 1].isalpha()
        and not any(
            _screening_text(character).isdecimal()
            or _is_identifier_mask(character)
            or (include_unsupported_masks and _is_unsupported_identifier_mask(character))
            for character in fragment
        )
    ):
        return None
    return fragment


def _is_identifier_like_counts(digits: int, masks: int) -> bool:
    """Apply the bounded bare-identifier threshold without retaining source text."""

    return digits >= 6 or (masks > 0 and digits + masks >= 2)


def _text_has_unhandled_identifier_candidate(text: str) -> bool:
    """Apply the bounded generic identifier rule to a redacted visual line."""

    # A complete written date is a public fact just like the numeric date forms
    # exempted by ``_identifier_fragment``.  Remove it before aggregating adjacent
    # numeric tokens, otherwise e.g. ``January 15, 2026`` looks like a six-digit
    # identifier solely because PDF text split the date into separate words.
    text = _WRITTEN_DATE_RE.sub(" ", _screening_text(text))
    if _text_has_unsupported_masked_identifier_candidate(text):
        return True
    digits = 0
    masks = 0
    for part in text.split():
        fragment = _identifier_fragment(part)
        if fragment is None:
            digits = 0
            masks = 0
            continue
        fragment_digits, fragment_masks = _identifier_counts(fragment)
        if fragment_digits + fragment_masks == 0:
            digits = 0
            masks = 0
            continue
        digits += fragment_digits
        masks += fragment_masks
        if _is_identifier_like_counts(digits, masks):
            return True
    return False


def _text_has_unsupported_masked_identifier_candidate(text: str) -> bool:
    """Reject only non-ASCII symbol masks when they form an identifier-shaped run."""

    digits = 0
    masks = 0
    for part in text.split():
        fragment = _identifier_fragment(part, include_unsupported_masks=True)
        if fragment is None:
            digits = 0
            masks = 0
            continue
        fragment_digits = sum(_screening_text(character).isdecimal() for character in fragment)
        fragment_masks = sum(
            _is_identifier_mask_in_candidate(fragment, index)
            or _is_unsupported_identifier_mask(character)
            for index, character in enumerate(fragment)
        )
        if fragment_digits + fragment_masks == 0:
            digits = 0
            masks = 0
            continue
        digits += fragment_digits
        masks += fragment_masks
        if _is_identifier_like_counts(digits, masks):
            return True
    return False


def _line_has_unhandled_identifier_candidate(
    line: list[tuple[str, BoundingBox]], *, provisional_phone_token_indexes: frozenset[int]
) -> bool:
    """Reject remaining bare identifiers after confirmed account-label removal.

    A date or canonical money token is a known source fact and is left for the
    strict page recognizer. Other contiguous identifier fragments are fail-closed:
    this keeps raw account identifiers and long numeric references out of every
    positioned-evidence path, including balance rows, activity descriptions, and
    headers.
    """

    text = _raw_line_text(line)
    if _ACCOUNT_ATTACHED_IDENTIFIER_RE.search(_screening_text(text)) is not None:
        return True
    screening_line = " ".join(
        "public-phone" if index in provisional_phone_token_indexes else value
        for index, (value, _) in enumerate(line)
    )
    return _text_has_unhandled_identifier_candidate(screening_line)


def _line_ascii_label_stream(line: list[tuple[str, BoundingBox]]) -> str:
    """Collapse one visual line for bounded ASCII account-label screening."""

    stream, _ = _ascii_obfuscated_label_stream(_raw_line_text(line))
    return stream


def _stream_has_ascii_account_word(value: str) -> bool:
    return re.search(r"(?:account|acct)", value) is not None


_PUBLIC_ASCII_ACCOUNT_CONTEXT_SUFFIXES = frozenset(
    {"activity", "balance", "information", "numbering", "statement", "summary"}
)
_ACCOUNT_LABEL_WORDS = ("account", "acct")
_PUBLIC_ACCOUNT_HEADING_PATTERNS = (
    ("account", "activity"),
    ("account", "balance"),
    ("account", "information"),
    ("account", "information", "guide"),
    ("account", "numbering"),
    ("account", "numbering", "notes"),
    ("account", "numbering", "reference"),
    ("account", "statement"),
    ("account", "summary"),
    ("current", "account", "balance"),
    ("important", "account", "information"),
    ("monthly", "account", "activity"),
    ("monthly", "account", "statement"),
)
_PUBLIC_ACCOUNT_REARM_TERMS = (
    "endingin",
    "id",
    "identifier",
    "no",
    "num",
    "number",
    "ref",
    "reference",
)
_ABBREVIATED_ACCOUNT_A_PREFIX = 1
_ABBREVIATED_ACCOUNT_SEPARATOR_PREFIX = 2
_ABBREVIATED_ACCOUNT_A_SYMBOLS = frozenset({"\u2200", "\u2227"})
_ABBREVIATED_ACCOUNT_A_INSERTION_GLYPHS = frozenset({"0", "3"})
_ABBREVIATED_ACCOUNT_C_LOOKALIKE_CHARACTERS = frozenset(
    {
        "(",
        "<",
        "[",
        "{",
        "\u2282",
        "\u2286",
        "\u228a",
        "\u228f",
        "\u2291",
        "\u2329",
        "\u3008",
        "\u27c3",
        "\u27e8",
        "\u2985",
        "\u3014",
    }
)


def _account_label_pattern(value: str) -> str:
    """Build a bounded account-label view with explicit confusable wildcards."""

    normalized = _screening_text(value)
    pattern: list[str] = []
    for index, character in enumerate(normalized):
        if character.isascii() and character.isalpha():
            pattern.append(character.casefold())
            continue
        if character.isascii() and character.isdecimal():
            adjacent_to_letter = (index > 0 and normalized[index - 1].isalpha()) or (
                index + 1 < len(normalized) and normalized[index + 1].isalpha()
            )
            if adjacent_to_letter and ord(character) in _ASCII_LEET_LABEL_TRANSLATION:
                pattern.append(_ASCII_LEET_LABEL_TRANSLATION[ord(character)])
            continue
        if not character.isascii() and (character.isalpha() or character.isnumeric()):
            pattern.append("?")
    return "".join(pattern)


def _account_pattern_matches(value: str, expected: str) -> bool:
    return len(value) == len(expected) and all(
        character == expected[index] or character == "?" for index, character in enumerate(value)
    )


def _account_pattern_has_altered_word(value: str) -> bool:
    """Detect a complete account spelling that uses an explicit wildcard."""

    pattern = _account_label_pattern(value)
    return any(
        "?" in pattern[index : index + len(expected)]
        and _account_pattern_matches(pattern[index : index + len(expected)], expected)
        for expected in _ACCOUNT_LABEL_WORDS
        for index in range(max(0, len(pattern) - len(expected) + 1))
    )


@dataclass(frozen=True)
class _AccountWordPrefix:
    """One bounded residual account-word scan state."""

    expected: str
    matched: int
    suffix: str = ""
    altered: bool = False
    post_word_boundary: bool = False
    public_suffix_trailing_word: bool = False
    public_suffix_after_boundary: bool = False
    suffix_pending_rn_m: bool = False

    @property
    def complete(self) -> bool:
        return self.matched == len(self.expected)


@dataclass(frozen=True)
class _PublicAccountQualifierPrefix:
    """One bounded qualifier word after a neutral public account phrase."""

    expected: str
    matched: int
    altered: bool = False
    pending_rn_m: bool = False

    @property
    def complete(self) -> bool:
        return self.matched == len(self.expected)


_ACCOUNT_POST_WORD_TERMS = (
    "activity",
    "balance",
    "ending",
    "id",
    "identifier",
    "information",
    "no",
    "num",
    "number",
    "numbering",
    "ref",
    "reference",
    "statement",
    "summary",
)


def _matches_expected_account_label_character(character: str, expected: str) -> bool:
    """Match one expected label character without translating an adjacent identifier."""

    normalized = _screening_text(character)
    if len(normalized) != 1:
        return False
    if normalized.casefold() == expected:
        return True
    if _VISUAL_ACCOUNT_LABEL_CHARACTER_ALIASES.get(normalized) == expected:
        return True
    if _is_identifier_mask(character) or _is_unsupported_identifier_mask(character):
        # Within the fixed reject-only label grammar, a masking glyph may stand in
        # for one letter (or be an insertion, via the alternate NFA path below).
        # It never participates in the raw identifier scanner.
        return True
    if unicodedata.category(character).startswith("P") or unicodedata.category(character) == "Sm":
        return False
    if normalized.isascii() and normalized.translate(_ASCII_LEET_LABEL_TRANSLATION) == expected:
        return True
    # A non-ASCII non-space source glyph in a fixed label slot is an untrusted
    # visual substitution (for example U+20AC/U+25CB in place of a letter).  The
    # scanner never retains it; accepting it only advances a reject-only, static
    # account-label NFA.
    return (
        not normalized.isascii()
        and not normalized.isspace()
        and unicodedata.category(character) != "Cf"
    )


def _account_label_character_is_altered(character: str, expected: str) -> bool:
    normalized = _screening_text(character)
    return normalized.casefold() != expected


def _account_label_character_may_be_visual_insertion(character: str, expected: str) -> bool:
    """Keep an alternate NFA path when a visual glyph may be inserted before a letter."""

    normalized = _screening_text(character)
    return _matches_expected_account_label_character(character, expected) and (
        not character.isascii() or normalized.casefold() != expected
    )


def _is_account_soft_separator(character: str) -> bool:
    """Keep a residual account label intact through visual layout separators."""

    normalized = _screening_text(character)
    return bool(normalized) and (
        normalized.isspace()
        or unicodedata.category(character) == "Cf"
        or (not character.isascii() and len(normalized) != 1)
        or all(not glyph.isalnum() and not glyph.isspace() for glyph in normalized)
    )


def _advance_account_post_word_suffix(
    prefix: _AccountWordPrefix, character: str
) -> tuple[_AccountWordPrefix, ...]:
    """Advance an attached account qualifier/reference without whole-line leet mapping."""

    if prefix.suffix_pending_rn_m:
        normalized = _screening_text(character)
        if normalized.casefold() != "n":
            return ()
        return (
            _AccountWordPrefix(
                prefix.expected,
                prefix.matched,
                prefix.suffix + "m",
                True,
                prefix.post_word_boundary,
                prefix.public_suffix_trailing_word,
                prefix.public_suffix_after_boundary,
            ),
        )

    next_candidates = tuple(
        term for term in _ACCOUNT_POST_WORD_TERMS if term.startswith(prefix.suffix)
    )
    suffixes = {
        prefix.suffix + term[len(prefix.suffix)]
        for term in next_candidates
        if len(prefix.suffix) < len(term)
        and _matches_expected_account_label_character(character, term[len(prefix.suffix)])
    }
    advanced: set[_AccountWordPrefix] = {
        _AccountWordPrefix(
            prefix.expected,
            prefix.matched,
            suffix,
            prefix.altered
            or _account_label_character_is_altered(character, suffix[len(prefix.suffix)]),
            prefix.post_word_boundary,
            prefix.public_suffix_trailing_word,
            prefix.public_suffix_after_boundary,
            prefix.suffix_pending_rn_m,
        )
        for suffix in suffixes
    }
    if any(
        _account_label_character_may_be_visual_insertion(character, suffix[len(prefix.suffix)])
        for suffix in suffixes
    ):
        advanced.add(
            _AccountWordPrefix(
                prefix.expected,
                prefix.matched,
                prefix.suffix,
                True,
                prefix.post_word_boundary,
                prefix.public_suffix_trailing_word,
                prefix.public_suffix_after_boundary,
            )
        )
    if (
        any(
            term[len(prefix.suffix)] == "m"
            for term in next_candidates
            if len(prefix.suffix) < len(term)
        )
        and _screening_text(character).casefold() == "r"
    ):
        advanced.add(
            _AccountWordPrefix(
                prefix.expected,
                prefix.matched,
                prefix.suffix,
                True,
                prefix.post_word_boundary,
                prefix.public_suffix_trailing_word,
                prefix.public_suffix_after_boundary,
                True,
            )
        )
    return tuple(advanced)


def _account_post_word_suffix_is_complete(prefix: _AccountWordPrefix) -> bool:
    return prefix.suffix in _ACCOUNT_POST_WORD_TERMS


def _account_post_word_suffix_is_reference(prefix: _AccountWordPrefix) -> bool:
    return prefix.suffix in {"ref", "reference", "ending"}


def _account_post_word_suffix_is_public(prefix: _AccountWordPrefix) -> bool:
    return prefix.suffix in _PUBLIC_ASCII_ACCOUNT_CONTEXT_SUFFIXES


def _account_post_word_suffix_is_qualifier(prefix: _AccountWordPrefix) -> bool:
    return prefix.suffix in {"id", "identifier", "no", "num", "number"}


def _longest_account_continuation_match(pattern: str, remaining: str) -> int:
    """Find one boundary-safe account-label continuation in a source token.

    A partial continuation must be its own fragment.  A complete remaining suffix may
    follow a token-local header prefix (``Headerount`` after ``Acc``).  This admits
    the supported visual split without allowing unrelated prose to donate one letter
    at a time to an account label.
    """

    longest = 0
    for start, character in enumerate(pattern):
        if character != remaining[0] and character != "?":
            continue
        matched = 0
        while (
            start + matched < len(pattern)
            and matched < len(remaining)
            and (pattern[start + matched] == remaining[matched] or pattern[start + matched] == "?")
        ):
            matched += 1
        if start + matched != len(pattern):
            continue
        if matched < len(remaining) and start != 0:
            continue
        if matched > longest:
            longest = matched
            if longest == len(remaining):
                return longest
    return longest


def _advance_account_prefixes(
    prefixes: tuple[_AccountWordPrefix, ...], fragments: tuple[str, ...]
) -> tuple[tuple[_AccountWordPrefix, ...], bool, bool, bool]:
    """Advance residual ``Account``/``Acct`` labels through positioned source atoms.

    This reject-only scanner preserves state through whitespace, punctuation, and
    Unicode-format layout artifacts, even when they occupy independent PDF tokens.
    It maps ASCII leet glyphs only while consuming the next known label character;
    once a label completes, identifier digits stay raw. Arbitrary prose clears an
    unfinished prefix, apart from the narrowly retained terminal ``Headerount``
    compatibility case needed for positioned legacy splits.
    """

    active = set(prefixes)
    completed = False
    advanced_existing_prefix = False
    public_suffix_completed = False
    for fragment_index, fragment in enumerate(fragments):
        if fragment_index:
            active = {
                _AccountWordPrefix(
                    prefix.expected,
                    prefix.matched,
                    prefix.suffix,
                    prefix.altered if prefix.complete else True,
                    True if prefix.complete else prefix.post_word_boundary,
                    prefix.public_suffix_trailing_word,
                    prefix.public_suffix_after_boundary,
                    prefix.suffix_pending_rn_m,
                )
                for prefix in active
            }
        embedded_completions: set[_AccountWordPrefix] = set()
        pattern = _account_label_pattern(fragment)
        for prefix in active:
            if prefix.complete or prefix.matched < 2:
                continue
            remaining = prefix.expected[prefix.matched :]
            if _longest_account_continuation_match(pattern, remaining) == len(remaining):
                embedded_completions.add(
                    _AccountWordPrefix(prefix.expected, len(prefix.expected), altered=True)
                )
                advanced_existing_prefix = True
        for character in fragment:
            next_active: set[_AccountWordPrefix] = set()
            for prefix in active:
                if not prefix.complete:
                    if _matches_expected_account_label_character(
                        character, prefix.expected[prefix.matched]
                    ):
                        next_active.add(
                            _AccountWordPrefix(
                                prefix.expected,
                                prefix.matched + 1,
                                altered=(
                                    prefix.altered
                                    or _account_label_character_is_altered(
                                        character, prefix.expected[prefix.matched]
                                    )
                                ),
                            )
                        )
                        if _account_label_character_may_be_visual_insertion(
                            character, prefix.expected[prefix.matched]
                        ):
                            next_active.add(
                                _AccountWordPrefix(
                                    prefix.expected,
                                    prefix.matched,
                                    prefix.suffix,
                                    True,
                                    prefix.post_word_boundary,
                                    prefix.public_suffix_trailing_word,
                                    prefix.public_suffix_after_boundary,
                                )
                            )
                        advanced_existing_prefix = True
                    elif _is_account_soft_separator(character):
                        next_active.add(
                            _AccountWordPrefix(
                                prefix.expected,
                                prefix.matched,
                                prefix.suffix,
                                True,
                                prefix.post_word_boundary,
                                prefix.public_suffix_trailing_word,
                                prefix.public_suffix_after_boundary,
                            )
                        )
                        advanced_existing_prefix = True
                    continue
                if _account_post_word_suffix_is_qualifier(prefix):
                    advanced = _advance_account_post_word_suffix(prefix, character)
                    if advanced:
                        next_active.update(advanced)
                        advanced_existing_prefix = True
                        continue
                    if _is_account_soft_separator(character):
                        next_active.add(
                            _AccountWordPrefix(
                                prefix.expected,
                                prefix.matched,
                                prefix.suffix,
                                prefix.altered,
                                True,
                                prefix.public_suffix_trailing_word,
                                prefix.public_suffix_after_boundary,
                            )
                        )
                        advanced_existing_prefix = True
                        continue
                    normalized = _screening_text(character)
                    if (
                        normalized.isdecimal()
                        or _is_identifier_mask(character)
                        or _is_unsupported_identifier_mask(character)
                    ):
                        completed = True
                        advanced_existing_prefix = True
                    continue
                if _account_post_word_suffix_is_public(prefix):
                    # A canonical public suffix is neutral rather than a delimiter.
                    # Preserve it through later prose so a subsequent qualifier, a
                    # later short identifier, or an earlier unresolved short value
                    # cannot use the public wording to clear the account context.
                    # Only a character attached directly to the suffix itself makes
                    # the spelling unsafe immediately (``ActivityRef``).
                    if _is_account_soft_separator(character):
                        next_active.add(
                            _AccountWordPrefix(
                                prefix.expected,
                                prefix.matched,
                                prefix.suffix,
                                prefix.altered,
                                False,
                                False,
                                True,
                            )
                        )
                    elif prefix.public_suffix_trailing_word:
                        next_active.add(
                            _AccountWordPrefix(
                                prefix.expected,
                                prefix.matched,
                                prefix.suffix,
                                prefix.altered,
                                False,
                                True,
                                False,
                            )
                        )
                    elif prefix.public_suffix_after_boundary:
                        next_active.add(
                            _AccountWordPrefix(
                                prefix.expected,
                                prefix.matched,
                                prefix.suffix,
                                prefix.altered,
                                False,
                                True,
                                False,
                            )
                        )
                    else:
                        completed = True
                    advanced_existing_prefix = True
                    continue
                advanced = _advance_account_post_word_suffix(prefix, character)
                if advanced:
                    advanced_existing_prefix = True
                    for candidate in advanced:
                        if _account_post_word_suffix_is_complete(candidate):
                            if _account_post_word_suffix_is_reference(candidate) or (
                                _account_post_word_suffix_is_qualifier(candidate)
                                and (not candidate.post_word_boundary or candidate.altered)
                            ):
                                completed = True
                                next_active.add(candidate)
                            elif _account_post_word_suffix_is_qualifier(candidate):
                                next_active.add(candidate)
                            elif _account_post_word_suffix_is_public(candidate):
                                public_suffix_completed = True
                                if candidate.altered:
                                    completed = True
                                else:
                                    next_active.add(candidate)
                            else:
                                next_active.add(candidate)
                        else:
                            next_active.add(candidate)
                    continue
                if _is_account_soft_separator(character):
                    next_active.add(
                        _AccountWordPrefix(
                            prefix.expected,
                            prefix.matched,
                            prefix.suffix,
                            prefix.altered or bool(prefix.suffix),
                            True,
                            prefix.public_suffix_trailing_word,
                            prefix.public_suffix_after_boundary,
                            prefix.suffix_pending_rn_m,
                        )
                    )
                    advanced_existing_prefix = True
                    continue
                normalized = _screening_text(character)
                if (
                    normalized.isdecimal()
                    or _is_identifier_mask(character)
                    or _is_unsupported_identifier_mask(character)
                ):
                    completed = True
                    advanced_existing_prefix = True
                    continue
            for expected in _ACCOUNT_LABEL_WORDS:
                if _matches_expected_account_label_character(character, expected[0]):
                    next_active.add(
                        _AccountWordPrefix(
                            expected,
                            1,
                            altered=_account_label_character_is_altered(character, expected[0]),
                        )
                    )
            active = next_active
        active.update(embedded_completions)
    return (
        tuple(
            sorted(
                active,
                key=lambda prefix: (
                    prefix.expected,
                    prefix.matched,
                    prefix.suffix,
                    prefix.altered,
                    prefix.post_word_boundary,
                    prefix.public_suffix_trailing_word,
                    prefix.public_suffix_after_boundary,
                    prefix.suffix_pending_rn_m,
                ),
            )
        ),
        completed,
        advanced_existing_prefix,
        public_suffix_completed,
    )


def _line_is_short_nonfinancial_header(line: list[tuple[str, BoundingBox]]) -> bool:
    """Recognize bounded header noise that cannot delimit a split account label."""

    value = _screening_text(_raw_line_text(line)).strip()
    return (
        bool(value)
        and len(value) <= 80
        and (
            _line_is_page_header_noise(line)
            or all(
                character.isalnum() or character.isspace() or character in " .,&'-_/():;#"
                for character in value
            )
        )
    )


def _line_is_page_header_noise(line: list[tuple[str, BoundingBox]]) -> bool:
    """Recognize bounded page-number header shapes across visual glyph variants.

    PDF headers commonly omit the word ``Page`` and render only a decorated page
    number. Those shapes are layout noise, not safe delimiters for a partially
    recognized account label.
    """

    value = "".join(
        character
        for character in _screening_text(_raw_line_text(line))
        if unicodedata.category(character) != "Cf"
    ).strip()

    def is_decorated_numeric_page_marker() -> bool:
        """Accept only a visibly framed decimal counter, never a money/date value."""

        saw_decoration = False
        for character in value:
            if character.isdecimal() or character.isspace():
                continue
            if character.isalnum():
                return False
            category = unicodedata.category(character)
            if (
                character in "-/|"
                or ord(character) in {0x00B7, 0x2022, 0x2044, 0x2215}
                or category in {"Pd", "Ps", "Pe"}
            ):
                saw_decoration = True
                continue
            return False
        return saw_decoration and any(character.isdecimal() for character in value)

    page_start = 0
    while page_start < len(value) and not value[page_start].isalnum():
        page_start += 1
    page_value = value[page_start:]
    expected = "page"
    if len(page_value) < len(expected):
        return is_decorated_numeric_page_marker()
    for index, character in enumerate(page_value[: len(expected)]):
        if not _matches_expected_account_label_character(character, expected[index]):
            return is_decorated_numeric_page_marker()
    suffix = page_value[len(expected) :].strip()
    # Page-number decorations are layout punctuation, not a trustworthy delimiter
    # for a partial account label.  Permit only decimal digits plus non-alphanumeric
    # framing (``Page — 2`` and ``Page 2/3``), never words such as ``Page No. 2``.
    return (
        bool(suffix)
        and any(character.isdecimal() for character in suffix)
        and all(character.isdecimal() or not character.isalnum() for character in suffix)
    )


def _is_abbreviated_account_start(character: str) -> bool:
    """Recognize a bounded visual ``A`` slot in the reject-only ``A/C`` automaton."""

    normalized = _screening_text(character)
    return (
        normalized.casefold() == "a"
        or normalized in {"4", "@"}
        or normalized in _ABBREVIATED_ACCOUNT_A_SYMBOLS
        or (
            not normalized.isascii()
            and len(normalized) == 1
            and (normalized.isalpha() or normalized.isnumeric())
        )
    )


def _is_abbreviated_account_separator(character: str) -> bool:
    """Recognize a non-alphanumeric visual abbreviated-account separator."""

    normalized = _screening_text(character)
    return bool(normalized) and all(
        not glyph.isspace() and not glyph.isalnum() for glyph in normalized
    )


def _is_abbreviated_account_c_slot(character: str) -> bool:
    """Recognize a bounded visual ``C`` slot after an abbreviated-account separator."""

    normalized = _screening_text(character)
    return (
        normalized.casefold() == "c"
        or normalized in _ABBREVIATED_ACCOUNT_C_LOOKALIKE_CHARACTERS
        or (
            not normalized.isascii()
            and len(normalized) == 1
            and (normalized.isalpha() or normalized.isnumeric())
        )
    )


def _advance_abbreviated_account_prefixes(
    prefixes: frozenset[int], fragment: str
) -> tuple[frozenset[int], bool]:
    """Advance a tiny reject-only visual ``A/C`` automaton over one source fragment.

    The explicit scrub grammar owns supported ``A/C`` labels. This automaton is only a
    privacy backstop for altered spellings: it recognizes a bounded ``A``-like start,
    one or more visual separators, then a ``C``-like slot. A bare ``A`` carries only
    through whitespace or another A-like glyph; after the separator, the stronger
    state may span visual lines and page headers. It rejects only when a later short
    identifier makes that context meaningful.
    """

    active = set(prefixes)
    completed = False
    for character in _screening_text(fragment):
        next_active: set[int] = set()
        is_start = _is_abbreviated_account_start(character)
        is_separator = _is_abbreviated_account_separator(character)
        if is_start:
            next_active.add(_ABBREVIATED_ACCOUNT_A_PREFIX)
        for prefix in active:
            if prefix == _ABBREVIATED_ACCOUNT_A_PREFIX:
                if (
                    character.isspace()
                    or is_start
                    or character in _ABBREVIATED_ACCOUNT_A_INSERTION_GLYPHS
                ):
                    next_active.add(_ABBREVIATED_ACCOUNT_A_PREFIX)
                elif is_separator:
                    next_active.add(_ABBREVIATED_ACCOUNT_SEPARATOR_PREFIX)
            elif prefix == _ABBREVIATED_ACCOUNT_SEPARATOR_PREFIX:
                if _is_abbreviated_account_c_slot(character):
                    completed = True
                next_active.add(_ABBREVIATED_ACCOUNT_SEPARATOR_PREFIX)
        active = next_active
    return frozenset(active), completed


def _stream_has_confusable_account_word(value: str) -> bool:
    """Detect a visually split account word containing a non-ASCII lookalike."""

    compact = "".join(character for character in _screening_text(value) if character.isalpha())
    return any(
        _is_confusable_label_word(compact[index : index + len(expected)], expected)
        for expected in ("account", "acct")
        for index in range(max(0, len(compact) - len(expected) + 1))
    )


def _label_letter_stream(value: str) -> str:
    """Keep only label letters while preserving non-ASCII confusable candidates."""

    return "".join(character for character in _screening_text(value) if character.isalpha())


def _ascii_label_words(value: str) -> tuple[str, ...]:
    """Return bounded ASCII words without collapsing their source boundaries."""

    return tuple(re.findall(r"[a-z]+", _screening_text(value).casefold()))


def _advance_public_account_qualifier_prefixes(
    prefixes: tuple[_PublicAccountQualifierPrefix, ...], fragments: tuple[str, ...]
) -> tuple[tuple[_PublicAccountQualifierPrefix, ...], bool, bool]:
    """Screen a neutral public phrase for a later account-reference word.

    Canonical labels such as ``Account Activity`` are public only as headings.  They
    remain a bounded neutral sentinel, so a later ``Ref``/``Number`` cannot use that
    wording to make a short identifier look like ordinary activity.  The scanner
    stores no source text and admits visual splits only inside this small fixed
    qualifier vocabulary; callers retain an unfinished state across header noise.
    """

    active = set(prefixes)
    completed = False
    advanced_existing_prefix = False
    for _fragment_index, fragment in enumerate(fragments):
        starts_at_word_boundary = True
        for character in fragment:
            next_active: set[_PublicAccountQualifierPrefix] = set()
            for prefix in active:
                if prefix.complete:
                    if _is_account_soft_separator(character):
                        completed = True
                        next_active.add(prefix)
                    elif (
                        _screening_text(character).isdecimal()
                        or _is_identifier_mask(character)
                        or _is_unsupported_identifier_mask(character)
                    ):
                        completed = True
                    # A trailing letter makes this ordinary prose rather than the
                    # bounded qualifier word (``Refund``/``Numbered``/``Notice``).
                    continue
                if prefix.pending_rn_m:
                    # ``rn`` is a common visual rendering of a lowercase ``m``.
                    # Permit it only while matching the literal ``m`` slot of one
                    # fixed qualifier word; a separator or any other glyph clears
                    # the partial pair rather than making general prose fuzzy.
                    if _screening_text(character).casefold() == "n":
                        next_active.add(
                            _PublicAccountQualifierPrefix(
                                prefix.expected,
                                prefix.matched + 1,
                                True,
                            )
                        )
                        advanced_existing_prefix = True
                    continue
                if (
                    prefix.expected[prefix.matched] == "m"
                    and _screening_text(character).casefold() == "r"
                ):
                    next_active.add(
                        _PublicAccountQualifierPrefix(
                            prefix.expected,
                            prefix.matched,
                            True,
                            True,
                        )
                    )
                    advanced_existing_prefix = True
                    continue
                if _matches_expected_account_label_character(
                    character, prefix.expected[prefix.matched]
                ):
                    advanced = _PublicAccountQualifierPrefix(
                        prefix.expected,
                        prefix.matched + 1,
                        prefix.altered
                        or _account_label_character_is_altered(
                            character, prefix.expected[prefix.matched]
                        ),
                    )
                    advanced_existing_prefix = True
                    next_active.add(advanced)
                    if _account_label_character_may_be_visual_insertion(
                        character, prefix.expected[prefix.matched]
                    ):
                        next_active.add(
                            _PublicAccountQualifierPrefix(
                                prefix.expected,
                                prefix.matched,
                                True,
                            )
                        )
                elif _is_account_soft_separator(character):
                    next_active.add(
                        _PublicAccountQualifierPrefix(
                            prefix.expected,
                            prefix.matched,
                            True,
                        )
                    )
                    advanced_existing_prefix = True
            if starts_at_word_boundary:
                for expected in _PUBLIC_ACCOUNT_REARM_TERMS:
                    if _matches_expected_account_label_character(character, expected[0]):
                        next_active.add(
                            _PublicAccountQualifierPrefix(
                                expected,
                                1,
                                _account_label_character_is_altered(character, expected[0]),
                            )
                        )
            active = next_active
            starts_at_word_boundary = _is_account_soft_separator(character)
        # Positioned source atoms constitute a word boundary. A complete qualifier
        # at their end is unsafe, while an incomplete visual split remains active.
        if any(prefix.complete for prefix in active):
            completed = True
    return (
        tuple(
            sorted(
                active,
                key=lambda prefix: (
                    prefix.expected,
                    prefix.matched,
                    prefix.altered,
                    prefix.pending_rn_m,
                ),
            )
        ),
        completed,
        advanced_existing_prefix,
    )


def _line_is_bare_public_account_no_qualifier(line: list[tuple[str, BoundingBox]]) -> bool:
    """Recognize an isolated ``No`` label without treating ordinary prose as one."""

    return _screening_text(_raw_line_text(line)).casefold().strip() in {"no", "no.", "no:"}


def _line_is_bare_public_account_mask_qualifier(
    line: list[tuple[str, BoundingBox]],
) -> bool:
    """Recognize one framed mask label after a neutral public account heading.

    This grammar is intentionally exact: it accepts one mask plus only non-alphanumeric
    framing. It is not a general rule for ordinary symbols in prose.
    """

    value = _screening_text(_raw_line_text(line)).strip()
    masks = tuple(
        character
        for character in value
        if _is_identifier_mask(character) or _is_unsupported_identifier_mask(character)
    )
    return len(masks) == 1 and all(
        _is_identifier_mask(character)
        or _is_unsupported_identifier_mask(character)
        or not character.isalnum()
        for character in value
    )


def _line_is_bare_public_account_qualifier(line: list[tuple[str, BoundingBox]]) -> bool:
    """Recognize a qualifier-only line without letting ordinary prose re-arm a heading."""

    compact = "".join(
        character
        for character in _screening_text(_raw_line_text(line)).casefold()
        if character.isalnum()
    )
    return compact in _PUBLIC_ACCOUNT_REARM_TERMS


def _line_has_public_account_no_value_shape(
    line: list[tuple[str, BoundingBox]],
    *,
    provisional_phone_token_indexes: frozenset[int],
) -> bool:
    """Recognize a bounded ``No`` qualifier only when this line carries a value."""

    has_no = any(
        _account_qualifier_match(word, "no")[0]
        for word in _account_label_words(_raw_line_text(line))
    )
    return has_no and (
        _line_has_nonfinancial_short_identifier_candidate(
            line,
            provisional_phone_token_indexes=provisional_phone_token_indexes,
        )
        or _raw_line_has_financial_candidate(line)
        or any(
            _is_known_public_short_identifier_exempt_token(part)
            for part in _screening_text(_raw_line_text(line)).split()
        )
    )


def _advance_public_account_heading_prefixes(
    prefixes: tuple[tuple[int, int], ...],
    line: list[tuple[str, BoundingBox]],
    *,
    line_has_nonfinancial_short_identifier: bool,
) -> tuple[tuple[tuple[int, int], ...], bool]:
    """Recognize a fixed public account heading across visual source lines.

    The state contains only an index into the static allowlist and a consumed-word
    count. A heading starts only at a visual-line boundary (or from a prior fixed
    heading prefix) and is public only when its final word ends the visual line and
    the line contains no nonfinancial short identifier. Trailing prose therefore
    cannot turn ``Fictional Account Activity Fee`` into a privileged context.
    """

    active = set(prefixes)
    words = _ascii_label_words(_raw_line_text(line))
    for word_index, word in enumerate(words):
        next_active: set[tuple[int, int]] = set()
        for pattern_index, matched in active:
            pattern = _PUBLIC_ACCOUNT_HEADING_PATTERNS[pattern_index]
            if matched < len(pattern) and word == pattern[matched]:
                next_active.add((pattern_index, matched + 1))
        if word_index == 0:
            next_active.update(
                (pattern_index, 1)
                for pattern_index, pattern in enumerate(_PUBLIC_ACCOUNT_HEADING_PATTERNS)
                if word == pattern[0]
            )
        active = next_active
    completed = bool(words) and any(
        matched == len(_PUBLIC_ACCOUNT_HEADING_PATTERNS[pattern_index])
        for pattern_index, matched in active
    )
    if line_has_nonfinancial_short_identifier:
        return (), completed
    return tuple(sorted(active)), completed


def _line_has_only_known_public_ascii_account_contexts(
    line: list[tuple[str, BoundingBox]],
    *,
    provisional_phone_token_indexes: frozenset[int] = frozenset(),
) -> bool:
    """Allow only fixed, token-bounded public account phrases."""

    if _line_has_nonfinancial_short_identifier_candidate(
        line,
        provisional_phone_token_indexes=provisional_phone_token_indexes,
    ):
        return False
    words = _ascii_label_words(_raw_line_text(line))
    return words in _PUBLIC_ACCOUNT_HEADING_PATTERNS


def _line_has_public_account_suffix_shape(line: list[tuple[str, BoundingBox]]) -> bool:
    """Recognize a canonical public suffix after a bounded account word.

    This is intentionally weaker than the exact-heading allowlist: callers use it
    only when the same visual line also carries a nonfinancial short identifier.
    It consequently catches attached forms such as ``ActivityRef1234`` without
    arming ordinary description text that contains only dates or money.
    """

    words = _ascii_label_words(_raw_line_text(line))
    return any(
        word in _ACCOUNT_LABEL_WORDS
        and index + 1 < len(words)
        and any(
            words[index + 1].startswith(suffix) for suffix in _PUBLIC_ASCII_ACCOUNT_CONTEXT_SUFFIXES
        )
        for index, word in enumerate(words)
    )


def _line_is_complete_known_public_ascii_account_context(
    line: list[tuple[str, BoundingBox]],
    *,
    provisional_phone_token_indexes: frozenset[int] = frozenset(),
) -> bool:
    """Recognize one complete, identifier-free public account heading.

    A fragment such as ``A`` remains a possible start of a hostile split label until
    it is resolved, even when an ordinary heading occurs in between.  This marker only
    prevents the heading's own canonical ``Account`` token from advancing that state;
    it never clears a prior state.  Match only the fixed heading suffix exactly, so a
    trailing value cannot use this exception.
    """

    return _line_has_only_known_public_ascii_account_contexts(
        line,
        provisional_phone_token_indexes=provisional_phone_token_indexes,
    )


def _line_starts_with_ascii_account_word(line: list[tuple[str, BoundingBox]]) -> bool:
    """Recognize an ordinary ASCII account label at a visual-line boundary."""

    value = _screening_text(_raw_line_text(line)).lstrip()
    return re.match(r"(?:account|acct)(?=[^a-z]|$)", value, flags=re.IGNORECASE) is not None


def _line_is_bare_ascii_account_label(line: list[tuple[str, BoundingBox]]) -> bool:
    """Recognize an inconclusive standalone ``Account`` or ``Acct`` source line."""

    value = _screening_text(_raw_line_text(line)).strip()
    return re.fullmatch(r"(?:account|acct)\.?\s*[:;,]?", value, flags=re.IGNORECASE) is not None


def _line_is_public_ascii_account_context_suffix(
    line: list[tuple[str, BoundingBox]],
    *,
    provisional_phone_token_indexes: frozenset[int] = frozenset(),
) -> bool:
    """Recognize a public heading continuation after a standalone account word."""

    words = _ascii_label_words(_raw_line_text(line))
    return (
        len(words) == 1
        and words[0] in _PUBLIC_ASCII_ACCOUNT_CONTEXT_SUFFIXES
        and not _line_has_nonfinancial_short_identifier_candidate(
            line,
            provisional_phone_token_indexes=provisional_phone_token_indexes,
        )
    )


def _line_is_account_label_qualifier_or_reference(line: list[tuple[str, BoundingBox]]) -> bool:
    """Recognize a value-bearing or qualifier-only line that can revive a heading."""

    if _screening_text(_raw_line_text(line)).casefold().strip() in {
        "no activity",
        "no transactions",
    }:
        return False
    words = _account_label_words(_raw_line_text(line))
    if not words:
        return False
    for expected in ("num", "number", "no", "id", "identifier", "ref", "reference"):
        matches, _ = _account_qualifier_match(words[0], expected)
        if matches:
            return (
                _line_is_bare_public_account_qualifier(line)
                or _raw_line_has_financial_candidate(line)
                or _line_has_nonfinancial_short_identifier_candidate(line)
            )
    # ``Ending Balance`` is an ordinary public financial label. An ``ending in``
    # account reference is still rejected when its short masked/numeric suffix is
    # encountered, so the word ``ending`` alone must not re-arm a public heading.
    return False


def _line_has_unresolved_account_context(
    line: list[tuple[str, BoundingBox]],
    *,
    preceding_ascii_label_tail: str,
    preceding_label_tail: str,
) -> bool:
    """Retain an unsafe account-label context until every short suffix is screened.

    The explicit scrubber owns known account labels.  Any remaining confusable,
    visually altered, or unrecognized ASCII account wording is ambiguous: a later
    short value can complete a private identifier even if PDF layout inserts one or
    more headers between them.  Fixed public phrases remain ordinary prose.
    """

    raw_text = _raw_line_text(line)
    if _account_pattern_has_altered_word(raw_text):
        return True
    if any(_is_confusable_account_word(word) for word in _account_label_words(raw_text)):
        return True
    if _stream_has_confusable_account_word(preceding_label_tail + raw_text):
        return True
    ascii_label_stream, leet_obfuscated = _ascii_obfuscated_label_stream(raw_text)
    combined_stream = preceding_ascii_label_tail + ascii_label_stream
    return (
        _line_starts_with_ascii_account_word(line)
        and _stream_has_ascii_account_word(combined_stream)
        and (leet_obfuscated or not _line_has_only_known_public_ascii_account_contexts(line))
    )


def _identifier_only_line_counts(
    line: list[tuple[str, BoundingBox]], *, provisional_phone_token_indexes: frozenset[int]
) -> tuple[int, int] | None:
    """Return counts only when a whole visual line is an identifier continuation."""

    screening_line = " ".join(
        "public-phone" if index in provisional_phone_token_indexes else value
        for index, (value, _) in enumerate(line)
    )
    parts = _screening_text(screening_line).split()
    if not parts:
        return None
    digits = 0
    masks = 0
    for part in parts:
        fragment = _identifier_fragment(part, include_unsupported_masks=True)
        if fragment is None:
            return None
        part_digits, part_masks = _identifier_counts(
            fragment,
            include_unsupported_masks=True,
        )
        if part_digits + part_masks == 0:
            return None
        digits += part_digits
        masks += part_masks
    return digits, masks


def _short_identifier_fragment_counts(
    line: list[tuple[str, BoundingBox]],
) -> tuple[int, int]:
    """Count identifier fragments in an unresolved account context without evidence.

    A date or money-shaped token is ordinarily a safe public fact. Once an unresolved
    account label has armed this fail-closed screen, that same shape can be a disguised
    short identifier and cannot retain its exemption. The same is true of a
    provisionally public contact phone: its exemption is limited to otherwise-public
    boilerplate, not an armed account-label context. In that narrow state, a token
    with at least one real digit also counts the small fixed set of ASCII OCR-like
    digit letters and one non-ASCII letter/numeric glyph, so ``1O23`` cannot preserve
    a four-character account suffix.
    """

    digits = 0
    masks = 0
    for value, _ in line:
        # Worker-wire tokens are allowed to contain ordinary Unicode whitespace.
        # Screen every visual sub-fragment so an adversary cannot hide ``12 34`` in
        # one token and bypass the four-digit account-context threshold.
        for part in _screening_text(value).split():
            fragment = _identifier_fragment(
                part,
                include_unsupported_masks=True,
                include_known_financial_tokens=True,
            )
            if fragment is None:
                continue
            part_digits, part_masks = _identifier_counts(
                fragment,
                include_unsupported_masks=True,
                include_visual_digit_lookalikes=True,
            )
            digits += part_digits
            masks += part_masks
    return digits, masks


def _short_identifier_run_has_content(run: _ShortIdentifierRun) -> bool:
    """Return whether a reduced identifier run must survive header noise."""

    return bool(run.real_digits or run.visual_digits or run.masks)


def _line_is_visual_identifier_fragment(line: list[tuple[str, BoundingBox]]) -> bool:
    """Recognize a token made only of visual identifier glyphs and separators."""

    has_visual_glyph = False
    for value, _ in line:
        for character in _screening_text(value):
            if _is_visual_identifier_digit_character(character):
                has_visual_glyph = True
            elif not _is_short_identifier_soft_separator(character):
                return False
    return has_visual_glyph


def _line_is_nonidentifier_header_noise(
    line: list[tuple[str, BoundingBox]],
    *,
    provisional_phone_token_indexes: frozenset[int],
) -> bool:
    """Keep a partial identifier run through a source header, never through its digits."""

    return (
        _line_is_short_nonfinancial_header(line)
        and not _line_is_visual_identifier_fragment(line)
        and not _line_has_nonfinancial_short_identifier_candidate(
            line,
            provisional_phone_token_indexes=provisional_phone_token_indexes,
        )
        and (
            _identifier_only_line_counts(
                line,
                provisional_phone_token_indexes=provisional_phone_token_indexes,
            )
            is None
        )
    )


def _first_unhandled_identifier_candidate_line(
    lines: list[list[tuple[str, BoundingBox]]],
    *,
    provisional_phone_token_indexes: tuple[frozenset[int], ...],
) -> int | None:
    """Return the one-based visual line that forms an unhandled identifier.

    An unresolved account-label context remains suspicious until the whole supplied
    layout has been screened. Page headers are not a safe delimiter, and visual source
    order is not a trustworthy identifier boundary: allowing either a header or a
    preceding short value to clear the context would let a hostile document retain its
    identifier in positioned evidence. The document validator supplies every sanitized
    page at once, while per-page materialization applies the same fail-closed rule before
    its layout can cross the worker boundary.
    """

    digits = 0
    masks = 0
    preceding_ascii_label_tail = ""
    preceding_label_tail = ""
    unresolved_account_context = False
    preceding_identifier_run = _ShortIdentifierRun()
    preceding_short_identifier_seen = False
    unresolved_identifier_run = _ShortIdentifierRun()
    active_account_prefixes: tuple[_AccountWordPrefix, ...] = ()
    deferred_account_prefixes: tuple[_AccountWordPrefix, ...] = ()
    active_public_account_qualifier_prefixes: tuple[_PublicAccountQualifierPrefix, ...] = ()
    deferred_public_account_qualifier_prefixes: tuple[_PublicAccountQualifierPrefix, ...] = ()
    active_abbreviated_account_prefixes: frozenset[int] = frozenset()
    deferred_abbreviated_account_prefixes: frozenset[int] = frozenset()
    active_public_account_heading_prefixes: tuple[tuple[int, int], ...] = ()
    pending_bare_ascii_account_label = False
    public_account_phrase_seen = False
    public_account_phrase_preceded_short_identifier = False
    pending_public_account_no_qualifier = False
    pending_public_account_mask_qualifier = False
    public_ascii_account_heading_pending = False
    public_heading_identifier_run = _ShortIdentifierRun()
    for line_number, (line, phone_token_indexes) in enumerate(
        zip(lines, provisional_phone_token_indexes, strict=True), start=1
    ):
        if _line_has_unhandled_identifier_candidate(
            line,
            provisional_phone_token_indexes=phone_token_indexes,
        ):
            return line_number
        ascii_label_stream = _line_ascii_label_stream(line)
        line_has_nonfinancial_short_identifier = _line_has_nonfinancial_short_identifier_candidate(
            line,
            provisional_phone_token_indexes=phone_token_indexes,
        )
        (
            active_public_account_heading_prefixes,
            public_account_heading_completed,
        ) = _advance_public_account_heading_prefixes(
            active_public_account_heading_prefixes,
            line,
            line_has_nonfinancial_short_identifier=line_has_nonfinancial_short_identifier,
        )
        line_is_known_public_account_context = _line_is_complete_known_public_ascii_account_context(
            line,
            provisional_phone_token_indexes=phone_token_indexes,
        )
        public_suffix_completed = False
        if line_is_known_public_account_context:
            account_word_completed = False
            active_account_prefixes = tuple(
                dict.fromkeys((*active_account_prefixes, *deferred_account_prefixes))
            )
            deferred_account_prefixes = ()
        else:
            source_account_prefixes = tuple(
                dict.fromkeys(
                    _AccountWordPrefix(
                        prefix.expected,
                        prefix.matched,
                        prefix.suffix,
                        prefix.altered if prefix.complete else True,
                        prefix.post_word_boundary,
                        prefix.public_suffix_trailing_word,
                        prefix.public_suffix_after_boundary
                        or _account_post_word_suffix_is_public(prefix),
                        prefix.suffix_pending_rn_m,
                    )
                    for prefix in (*active_account_prefixes, *deferred_account_prefixes)
                )
            )
            (
                active_account_prefixes,
                account_word_completed,
                advanced_existing_account_prefix,
                public_suffix_completed,
            ) = _advance_account_prefixes(
                source_account_prefixes,
                tuple(value for value, _ in line),
            )
            # A page/header line cannot delimit an unfinished account label or any of
            # its qualifier/public-suffix prefixes. Retain the source state as a
            # parallel fail-closed path even when header text happened to advance a
            # few characters, so ``A``/``AccountN``/``Account Acti`` cannot be erased
            # by a page heading before the next visual fragment completes it.
            if (
                source_account_prefixes
                and _line_is_short_nonfinancial_header(line)
                and not account_word_completed
            ):
                deferred_account_prefixes = tuple(
                    prefix
                    for prefix in source_account_prefixes
                    if not prefix.complete
                    or (bool(prefix.suffix) and not _account_post_word_suffix_is_public(prefix))
                )
            else:
                deferred_account_prefixes = ()
        public_account_phrase_seen_before_line = public_account_phrase_seen
        public_account_phrase_seen = public_account_phrase_seen or (
            line_is_known_public_account_context
            or public_account_heading_completed
            or public_suffix_completed
        )
        if public_account_phrase_seen and not public_account_phrase_seen_before_line:
            public_account_phrase_preceded_short_identifier = preceding_short_identifier_seen
        source_public_account_qualifier_prefixes = tuple(
            dict.fromkeys(
                (
                    *active_public_account_qualifier_prefixes,
                    *deferred_public_account_qualifier_prefixes,
                )
            )
        )
        public_account_qualifier_completed = False
        if public_account_phrase_seen:
            if line_is_known_public_account_context or public_account_heading_completed:
                # The fixed allowlist owns e.g. ``Account Numbering Reference``.
                # It is a heading, not a reference label; retain only an earlier
                # partial hostile qualifier across this visual header.
                active_public_account_qualifier_prefixes = ()
                deferred_public_account_qualifier_prefixes = (
                    source_public_account_qualifier_prefixes
                )
            else:
                (
                    active_public_account_qualifier_prefixes,
                    public_account_qualifier_completed,
                    _advanced_existing_public_account_qualifier_prefix,
                ) = _advance_public_account_qualifier_prefixes(
                    source_public_account_qualifier_prefixes,
                    tuple(value for value, _ in line),
                )
                if (
                    source_public_account_qualifier_prefixes
                    and _line_is_short_nonfinancial_header(line)
                    and not public_account_qualifier_completed
                ):
                    deferred_public_account_qualifier_prefixes = (
                        source_public_account_qualifier_prefixes
                    )
                else:
                    deferred_public_account_qualifier_prefixes = ()
        else:
            active_public_account_qualifier_prefixes = ()
            deferred_public_account_qualifier_prefixes = ()
        if public_account_qualifier_completed and not (
            line_has_nonfinancial_short_identifier
            or _raw_line_has_financial_candidate(line)
            or _line_is_bare_public_account_qualifier(line)
            or any(prefix.complete for prefix in active_public_account_qualifier_prefixes)
        ):
            # A qualifier embedded in ordinary prose (for example ``No ID is
            # required``) does not arm a public heading forever.  Keep only a
            # qualifier carrying a candidate/value, a qualifier-only line, or one
            # that ends at this visual boundary and can legitimately wrap a value.
            public_account_qualifier_completed = False
        public_account_no_completed = False
        if public_account_phrase_seen:
            if _line_has_public_account_no_value_shape(
                line,
                provisional_phone_token_indexes=phone_token_indexes,
            ):
                public_account_no_completed = True
                pending_public_account_no_qualifier = False
            elif pending_public_account_no_qualifier:
                if line_has_nonfinancial_short_identifier or _raw_line_has_financial_candidate(
                    line
                ):
                    public_account_no_completed = True
                    pending_public_account_no_qualifier = False
                elif _screening_text(_raw_line_text(line)).casefold().strip() in {
                    "activity",
                    "transactions",
                }:
                    pending_public_account_no_qualifier = False
            if _line_is_bare_public_account_no_qualifier(line) or any(
                prefix.expected == "no" and prefix.complete
                for prefix in active_public_account_qualifier_prefixes
            ):
                pending_public_account_no_qualifier = True
        else:
            pending_public_account_no_qualifier = False
        public_account_mask_completed = False
        if public_account_phrase_seen:
            if _line_is_bare_public_account_mask_qualifier(line):
                pending_public_account_mask_qualifier = True
            elif pending_public_account_mask_qualifier and (
                line_has_nonfinancial_short_identifier or _raw_line_has_financial_candidate(line)
            ):
                public_account_mask_completed = True
                pending_public_account_mask_qualifier = False
        else:
            pending_public_account_mask_qualifier = False
        source_abbreviated_account_prefixes = (
            active_abbreviated_account_prefixes | deferred_abbreviated_account_prefixes
        )
        active_abbreviated_account_prefixes, abbreviated_account_completed = (
            _advance_abbreviated_account_prefixes(
                source_abbreviated_account_prefixes,
                _raw_line_text(line),
            )
        )
        if (
            source_abbreviated_account_prefixes
            and _line_is_page_header_noise(line)
            and not abbreviated_account_completed
        ):
            # A bare ``A`` is intentionally not retained through arbitrary prose,
            # but a real page header cannot delimit an ``A/C`` label split by the
            # PDF layout engine.
            deferred_abbreviated_account_prefixes = source_abbreviated_account_prefixes
        else:
            deferred_abbreviated_account_prefixes = frozenset()
        line_is_bare_ascii_account_label = _line_is_bare_ascii_account_label(line)
        direct_account_context = _line_has_unresolved_account_context(
            line,
            preceding_ascii_label_tail=preceding_ascii_label_tail,
            preceding_label_tail=preceding_label_tail,
        )
        direct_account_context = direct_account_context or (
            line_has_nonfinancial_short_identifier
            and (
                public_account_phrase_seen
                or public_account_heading_completed
                or _line_has_public_account_suffix_shape(line)
            )
        )
        if line_is_bare_ascii_account_label:
            direct_account_context = False
        previous_public_ascii_account_heading = public_ascii_account_heading_pending
        line_is_public_ascii_account_heading = (
            line_is_known_public_account_context or public_account_heading_completed
        )
        public_ascii_account_heading_pending = False
        if pending_bare_ascii_account_label:
            if line_is_known_public_account_context or _line_is_public_ascii_account_context_suffix(
                line,
                provisional_phone_token_indexes=phone_token_indexes,
            ):
                pending_bare_ascii_account_label = False
                line_is_public_ascii_account_heading = True
                account_word_completed = False
            else:
                direct_account_context = True
                pending_bare_ascii_account_label = False
        if line_is_public_ascii_account_heading:
            public_ascii_account_heading_pending = True
        elif previous_public_ascii_account_heading:
            if (
                _line_is_account_label_qualifier_or_reference(line)
                and not _line_is_bare_public_account_no_qualifier(line)
            ) or line_has_nonfinancial_short_identifier:
                direct_account_context = True
            # A recognized public heading does not itself arm the short-ID screen,
            # but it cannot be erased by source-order noise. A later qualifier can
            # still turn it into an unsafe account-label context.
            else:
                public_ascii_account_heading_pending = True
        if (
            public_account_phrase_seen
            or previous_public_ascii_account_heading
            or line_is_public_ascii_account_heading
        ):
            if (
                _short_identifier_run_has_content(public_heading_identifier_run)
                and _line_is_nonidentifier_header_noise(
                    line,
                    provisional_phone_token_indexes=phone_token_indexes,
                )
                and not _raw_line_has_financial_candidate(line)
            ):
                public_heading_short_identifier_found = False
            else:
                (
                    public_heading_identifier_run,
                    public_heading_short_identifier_found,
                ) = _advance_nonfinancial_short_identifier_run(
                    public_heading_identifier_run,
                    line,
                    provisional_phone_token_indexes=phone_token_indexes,
                )
            if public_heading_short_identifier_found:
                return line_number
            direct_account_context = direct_account_context or public_heading_short_identifier_found
        else:
            public_heading_identifier_run = _ShortIdentifierRun()
        has_unresolved_account_context = (
            direct_account_context
            or account_word_completed
            or abbreviated_account_completed
            or public_account_qualifier_completed
            or public_account_no_completed
            or public_account_mask_completed
        )
        # Positioned source order is not an identifier boundary: an earlier short
        # candidate can belong to a later residual account label just as readily as a
        # suffix after it. The reduced run state records no source text.
        has_strong_account_context = has_unresolved_account_context
        if has_strong_account_context and preceding_short_identifier_seen:
            return line_number
        if unresolved_account_context or has_unresolved_account_context:
            if (
                _short_identifier_run_has_content(unresolved_identifier_run)
                and _line_is_nonidentifier_header_noise(
                    line,
                    provisional_phone_token_indexes=phone_token_indexes,
                )
                and not _raw_line_has_financial_candidate(line)
            ):
                found_short_identifier = False
            else:
                unresolved_identifier_run, found_short_identifier = _advance_short_identifier_run(
                    unresolved_identifier_run,
                    line,
                    include_known_financial_tokens=True,
                )
            if found_short_identifier:
                return line_number
        else:
            if _short_identifier_run_has_content(
                preceding_identifier_run
            ) and _line_is_nonidentifier_header_noise(
                line,
                provisional_phone_token_indexes=phone_token_indexes,
            ):
                found_short_identifier = False
            else:
                preceding_identifier_run, found_short_identifier = _advance_short_identifier_run(
                    preceding_identifier_run,
                    line,
                    include_known_financial_tokens=True,
                )
            preceding_short_identifier_seen = (
                preceding_short_identifier_seen or found_short_identifier
            )
        unresolved_account_context = unresolved_account_context or has_unresolved_account_context
        if line_is_bare_ascii_account_label:
            pending_bare_ascii_account_label = True
        preceding_ascii_label_tail = (preceding_ascii_label_tail + ascii_label_stream)[
            -(len("account") - 1) :
        ]
        preceding_label_tail = (preceding_label_tail + _label_letter_stream(_raw_line_text(line)))[
            -(len("account") - 1) :
        ]
        counts = _identifier_only_line_counts(
            line,
            provisional_phone_token_indexes=phone_token_indexes,
        )
        if counts is None:
            digits = 0
            masks = 0
            continue
        line_digits, line_masks = counts
        digits += line_digits
        masks += line_masks
        if _is_identifier_like_counts(digits, masks):
            return line_number
    if public_account_phrase_preceded_short_identifier and (
        public_account_phrase_seen
        or any(prefix.complete for prefix in (*active_account_prefixes, *deferred_account_prefixes))
    ):
        return len(lines)
    return None


def _lines_have_unhandled_identifier_candidate(
    lines: list[list[tuple[str, BoundingBox]]],
    *,
    provisional_phone_token_indexes: tuple[frozenset[int], ...],
) -> bool:
    """Return whether any supplied visual line forms an unhandled identifier."""

    return (
        _first_unhandled_identifier_candidate_line(
            lines,
            provisional_phone_token_indexes=provisional_phone_token_indexes,
        )
        is not None
    )


def _validate_cross_page_identifier_fragments(
    pages: tuple[_NativePage, ...], document_ordinal: int
) -> None:
    """Reject residual identifier fragments that become meaningful across page breaks.

    Each page is scrubbed before it crosses the worker boundary, but an unknown label
    form can remain harmless in isolation and become unsafe only when the next page
    supplies its short suffix. Reapply the same line-state screen across the complete
    sanitized document in both worker and parent without including source text in an
    error.
    """

    raw_lines: list[list[tuple[str, BoundingBox]]] = []
    provisional_phone_token_indexes: list[frozenset[int]] = []
    page_numbers: list[int] = []
    for page in pages:
        provisional_phone_ordinals = frozenset(page.provisional_contact_phone_token_ordinals)
        for line in page.lines:
            line_tokens = tuple(page.token(ordinal) for ordinal in line.token_ordinals)
            raw_lines.append([(token.text, token.box) for token in line_tokens])
            page_numbers.append(page.page_number)
            provisional_phone_token_indexes.append(
                frozenset(
                    index
                    for index, token in enumerate(line_tokens)
                    if token.ordinal in provisional_phone_ordinals
                )
            )
    first_unhandled_line = _first_unhandled_identifier_candidate_line(
        raw_lines,
        provisional_phone_token_indexes=tuple(provisional_phone_token_indexes),
    )
    if first_unhandled_line is not None:
        raise _page_error(document_ordinal, page_numbers[first_unhandled_line - 1])


def _line_has_explicit_account_label_identifier_context(
    line: list[tuple[str, BoundingBox]],
) -> bool:
    """Recognize a bounded ASCII account label paired with a short value or mask."""

    text = _screening_text(_raw_line_text(line))
    return any(
        _is_identifier_candidate(text[match.end() :], include_unsupported_masks=True)
        for match in _ACCOUNT_VALUE_PREFIX_RE.finditer(text)
    )


def _line_has_short_identifier_candidate(
    line: list[tuple[str, BoundingBox]],
    *,
    provisional_phone_token_indexes: frozenset[int] = frozenset(),
) -> bool:
    """Recognize a four-digit-or-masked candidate only in an already suspicious context."""

    return any(
        _is_identifier_candidate(fragment, include_unsupported_masks=True)
        for fragment in (
            _identifier_fragment(part, include_unsupported_masks=True)
            for part in _screening_text(
                " ".join(
                    "public-phone" if index in provisional_phone_token_indexes else value
                    for index, (value, _) in enumerate(line)
                )
            ).split()
        )
        if fragment is not None
    )


def _line_has_confusable_account_identifier_context(
    line: list[tuple[str, BoundingBox]],
) -> bool:
    """Fail closed when a non-ASCII account-word lookalike shares a line with an ID."""

    text = _screening_text(_raw_line_text(line))
    return any(
        _is_confusable_account_word(word) for word in re.split(r"[\s:;=,()\[\]{}<>/\\]+", text)
    ) and (_line_has_short_identifier_candidate(line))


def _is_account_label_prefix_line(line: list[tuple[str, BoundingBox]]) -> bool:
    return _ACCOUNT_LABEL_PREFIX_RE.fullmatch(_screening_text(_raw_line_text(line))) is not None


def _is_account_label_suffix_line(line: list[tuple[str, BoundingBox]]) -> bool:
    return _ACCOUNT_LABEL_SUFFIX_RE.fullmatch(_screening_text(_raw_line_text(line))) is not None


def _is_account_ending_in_line(line: list[tuple[str, BoundingBox]]) -> bool:
    return _ACCOUNT_ENDING_IN_RE.fullmatch(_screening_text(_raw_line_text(line))) is not None


def _is_account_label_candidate_line(line: list[tuple[str, BoundingBox]]) -> bool:
    return _ACCOUNT_LABEL_CANDIDATE_RE.search(_screening_text(_raw_line_text(line))) is not None


def _is_account_label_suffix_candidate_line(line: list[tuple[str, BoundingBox]]) -> bool:
    return (
        _ACCOUNT_LABEL_SUFFIX_CANDIDATE_RE.search(_screening_text(_raw_line_text(line))) is not None
    )


def _line_ends_with_account_identifier(line: list[tuple[str, BoundingBox]]) -> bool:
    text = _screening_text(_raw_line_text(line)).rstrip(" \t:;,")
    start = len(text)
    while start > 0 and (
        text[start - 1].isspace() or _is_identifier_syntax_character(text[start - 1])
    ):
        start -= 1
    compact = "".join(text[start:].split())
    return (
        bool(compact)
        and all(_is_identifier_syntax_character(character) for character in compact)
        and _is_identifier_candidate(compact)
    )


def _is_inline_account_prefix_identifier_line(line: list[tuple[str, BoundingBox]]) -> bool:
    match = _ACCOUNT_INLINE_PREFIX_RE.fullmatch(_screening_text(_raw_line_text(line)))
    if match is None:
        return False
    compact = "".join(match.group("identifier").strip(" \t:;,").split())
    return (
        bool(compact)
        and all(_is_identifier_syntax_character(character) for character in compact)
        and _is_identifier_candidate(compact)
    )


def _has_unsupported_inline_account_identifier_candidate(
    line: list[tuple[str, BoundingBox]],
) -> bool:
    """Reject a punctuated account-ID spelling that the explicit grammar does not own.

    Punctuation is normalized only while checking the bounded ``Account|Acct``
    prefix plus identifier shape.  This catches wrappers such as ``Account
    (1234)`` without treating ordinary prose such as ``Account Information`` as
    an account label or silently retaining an identifier in evidence.
    """

    normalized = "".join(
        character if character.isalnum() or _is_identifier_syntax_character(character) else " "
        for character in _screening_text(_raw_line_text(line))
    )
    parts = normalized.split()
    if not parts or parts[0].casefold().rstrip(".") not in {"account", "acct"}:
        return False
    identifier_length = 0
    for part in parts[1:]:
        if all(_is_identifier_syntax_character(character) for character in part):
            identifier_length += len(part)
            if identifier_length >= 4:
                return True
        else:
            break
    return False


def _discard_account_identifier_tokens(
    lines: list[list[tuple[str, BoundingBox]]],
    document_ordinal: int,
    page_number: int,
) -> list[list[tuple[str, BoundingBox]]]:
    """Remove only non-financial account labels and adjacent identifier continuations.

    Account identifiers must never reach evidence, but an account-label phrase can also
    occur in a financial source line. A complete label can span an otherwise standalone
    ``Account``/``Acct.`` line and its immediately adjacent ``Number``/``No.``/``ID``/``#``
    or ``ending in`` suffix line, including ordinary punctuation. A complete label or
    identifier-only continuation with a date or amount is therefore ambiguous and
    rejected before any evidence is built. Identifier-only continuations remain in the
    removal state until a non-identifier line ends that contiguous run and is available
    to the strict page validators. An unconfirmed standalone account word is retained
    unchanged.
    """

    filtered: list[list[tuple[str, BoundingBox]]] = []
    state = _AccountScrubState.NONE
    pending_label_prefix: list[tuple[str, BoundingBox]] | None = None
    for line in lines:
        line_text = _raw_line_text(line)
        if (
            (
                _line_has_explicit_account_label_identifier_context(line)
                and _has_unsupported_non_ascii_identifier_syntax(line_text)
            )
            or _line_has_confusable_account_identifier_context(line)
            or _confusable_account_qualifier_context(line)
            or _abbreviated_account_qualifier_context(line)
            or _is_obfuscated_account_word_line(line)
        ):
            raise _page_error(document_ordinal, page_number)
        if pending_label_prefix is not None:
            if _is_account_label_suffix_line(line):
                if _raw_line_has_financial_candidate(
                    pending_label_prefix
                ) or _raw_line_has_financial_candidate(line):
                    raise _page_error(document_ordinal, page_number)
                filtered.extend(([], []))
                pending_label_prefix = None
                state = (
                    _AccountScrubState.NONE
                    if _line_ends_with_account_identifier(line)
                    else _AccountScrubState.AWAITING_IDENTIFIER
                )
                continue
            if _confusable_account_qualifier_context(
                line, account_prefix=True
            ) or _obfuscated_account_label_context(line, account_prefix=True):
                raise _page_error(document_ordinal, page_number)
            if _is_account_label_suffix_candidate_line(line) or _is_account_identifier_line(line):
                raise _page_error(document_ordinal, page_number)
            filtered.append(pending_label_prefix)
            pending_label_prefix = None

        if state is _AccountScrubState.AWAITING_IDENTIFIER:
            if _is_account_ending_in_line(line):
                if _raw_line_has_financial_candidate(line):
                    raise _page_error(document_ordinal, page_number)
                filtered.append([])
                state = (
                    _AccountScrubState.NONE
                    if _line_ends_with_account_identifier(line)
                    else _AccountScrubState.AWAITING_IDENTIFIER
                )
                continue
            if _is_account_identifier_line(line):
                if _raw_line_has_financial_candidate(line):
                    raise _page_error(document_ordinal, page_number)
                filtered.append([])
                state = _AccountScrubState.IDENTIFIER_RUN
                continue
            raise _page_error(document_ordinal, page_number)

        screening_line_text = _screening_text(line_text)
        if _ACCOUNT_LABEL_RE.search(screening_line_text):
            if _raw_line_has_financial_candidate(line):
                raise _page_error(document_ordinal, page_number)
            filtered.append([])
            state = (
                _AccountScrubState.NONE
                if _line_ends_with_account_identifier(line)
                else _AccountScrubState.AWAITING_IDENTIFIER
            )
        elif _is_inline_account_prefix_identifier_line(line):
            if _raw_line_has_financial_candidate(line):
                raise _page_error(document_ordinal, page_number)
            filtered.append([])
            state = _AccountScrubState.NONE
        elif _has_unsupported_inline_account_identifier_candidate(line):
            raise _page_error(document_ordinal, page_number)
        elif _obfuscated_account_label_context(line):
            raise _page_error(document_ordinal, page_number)
        elif _is_account_label_prefix_line(line):
            pending_label_prefix = line
            state = _AccountScrubState.NONE
        elif _is_account_label_candidate_line(line):
            raise _page_error(document_ordinal, page_number)
        elif state is _AccountScrubState.IDENTIFIER_RUN and _is_account_ending_in_line(line):
            if _raw_line_has_financial_candidate(line):
                raise _page_error(document_ordinal, page_number)
            filtered.append([])
            state = (
                _AccountScrubState.NONE
                if _line_ends_with_account_identifier(line)
                else _AccountScrubState.AWAITING_IDENTIFIER
            )
            continue
        elif state is _AccountScrubState.IDENTIFIER_RUN and _is_account_identifier_line(line):
            if _raw_line_has_financial_candidate(line):
                raise _page_error(document_ordinal, page_number)
            filtered.append([])
            continue
        else:
            filtered.append(line)
            state = _AccountScrubState.NONE
    if pending_label_prefix is not None or state is not _AccountScrubState.NONE:
        raise _page_error(document_ordinal, page_number)
    return filtered


def _are_visually_adjacent_money_fragments(left: BoundingBox, right: BoundingBox) -> bool:
    gap = right.left - left.right
    return Decimal(0) <= gap <= _WORD_GAP


def _box_for_raw_words(words: list[tuple[str, BoundingBox]]) -> BoundingBox:
    return BoundingBox(
        left=min(box.left for _, box in words),
        top=min(box.top for _, box in words),
        right=max(box.right for _, box in words),
        bottom=max(box.bottom for _, box in words),
    )


def _coalesce_visual_money_fragments(
    line: list[tuple[str, BoundingBox]],
) -> list[tuple[str, BoundingBox]]:
    """Reconstruct a close, visual amount split across separate PDF text words.

    Only a run that joins to a canonical or reject-only money-like value is
    coalesced. Other digit fragments stay distinct for the generic identifier
    guard, and a larger visual gap remains a separate source candidate that the
    line-level fail-closed validator will reject when money-shaped.
    """

    coalesced: list[tuple[str, BoundingBox]] = []
    index = 0
    while index < len(line):
        text, box = line[index]
        if not _is_monetary_sequence_fragment(text):
            coalesced.append((text, box))
            index += 1
            continue
        end = index + 1
        words = [(text, box)]
        value = text.strip()
        while (
            end < len(line)
            and _is_monetary_sequence_fragment(line[end][0])
            and _are_visually_adjacent_money_fragments(words[-1][1], line[end][1])
        ):
            value += line[end][0].strip()
            words.append(line[end])
            end += 1
        if len(words) > 1 and _is_monetary_like_text(value):
            coalesced.append((value, _box_for_raw_words(words)))
        else:
            coalesced.extend(words)
        index = end
    return coalesced


def _materialize_scrubbed_native_layout(
    raw_token_lines: list[list[tuple[str, BoundingBox]]],
    document_ordinal: int,
    page_number: int,
    limits: _NativeExtractionLimits,
) -> tuple[tuple[_LayoutToken, ...], tuple[_NativeLine, ...], tuple[int, ...]]:
    """Screen raw layout before it can become parser evidence.

    This boundary is shared by the PDFium worker and its parent.  The worker applies it
    before serializing layout, while the parent applies it again after validating the
    JSON wire shape.  A compromised or malformed worker response can therefore never
    materialize an account identifier in evidence.
    """

    has_known_boilerplate_context = _has_known_boilerplate_context(raw_token_lines)
    retained_lines = _discard_account_identifier_tokens(
        raw_token_lines,
        document_ordinal,
        page_number,
    )
    provisional_phone_token_indexes = tuple(
        _provisional_contact_phone_token_indexes(line, has_known_boilerplate_context)
        for line in retained_lines
    )
    if _lines_have_unhandled_identifier_candidate(
        retained_lines,
        provisional_phone_token_indexes=provisional_phone_token_indexes,
    ):
        raise _page_error(document_ordinal, page_number)
    tokens: list[_LayoutToken] = []
    lines: list[_NativeLine] = []
    provisional_phone_token_ordinals: list[int] = []
    for raw_line, phone_token_indexes in zip(
        retained_lines, provisional_phone_token_indexes, strict=True
    ):
        if not raw_line:
            continue
        ordinals: list[int] = []
        line_ordinal = len(lines) + 1
        for raw_token_index, (text, box) in enumerate(raw_line):
            if not text.strip():
                continue
            if len(tokens) >= limits.max_tokens_per_page:
                raise _page_error(document_ordinal, page_number)
            token = _LayoutToken(
                ordinal=len(tokens) + 1,
                text=text,
                box=box,
                line_ordinal=line_ordinal,
            )
            tokens.append(token)
            ordinals.append(token.ordinal)
            if raw_token_index in phone_token_indexes:
                provisional_phone_token_ordinals.append(token.ordinal)
        if ordinals:
            lines.append(_NativeLine(ordinal=line_ordinal, token_ordinals=tuple(ordinals)))
    if not tokens:
        raise _page_error(document_ordinal, page_number)
    return tuple(tokens), tuple(lines), tuple(provisional_phone_token_ordinals)


def _group_native_characters(
    characters: tuple[_RawCharacter, ...],
    document_ordinal: int,
    page_number: int,
    limits: _NativeExtractionLimits | None = None,
) -> tuple[tuple[_LayoutToken, ...], tuple[_NativeLine, ...], tuple[int, ...]]:
    """Group positioned characters into visual lines and words without text-order inference."""

    effective_limits = limits or _native_extraction_limits(document_ordinal)
    visual = sorted(
        characters,
        key=lambda character: (
            (character.box.top + character.box.bottom) / Decimal(2),
            character.box.left,
            character.index,
        ),
    )
    character_lines: list[list[_RawCharacter]] = []
    centers: list[Decimal] = []
    for character in visual:
        center = (character.box.top + character.box.bottom) / Decimal(2)
        if not character_lines or abs(center - centers[-1]) > _LINE_VERTICAL_TOLERANCE:
            if len(character_lines) >= effective_limits.max_lines_per_page:
                raise _page_error(document_ordinal, page_number)
            character_lines.append([character])
            centers.append(center)
        else:
            character_lines[-1].append(character)
            count = Decimal(len(character_lines[-1]))
            centers[-1] = centers[-1] + (center - centers[-1]) / count

    raw_token_lines: list[list[tuple[str, BoundingBox]]] = []
    for line in character_lines:
        ordered = sorted(line, key=lambda character: (character.box.left, character.index))
        words: list[list[_RawCharacter]] = []
        previous: _RawCharacter | None = None
        for character in ordered:
            if previous is None:
                words.append([character])
            else:
                gap = character.box.left - previous.box.right
                if character.index != previous.index + 1 or gap > _WORD_GAP:
                    words.append([character])
                else:
                    words[-1].append(character)
            previous = character
        raw_token_lines.append(
            _coalesce_visual_money_fragments(
                [
                    ("".join(character.text for character in word), _box_for_tokens(tuple(word)))
                    for word in words
                ]
            )
        )

    return _materialize_scrubbed_native_layout(
        raw_token_lines,
        document_ordinal,
        page_number,
        effective_limits,
    )


def _extract_native_page(
    page: _PdfPage,
    document_ordinal: int,
    page_number: int,
    remaining_characters: int,
    limits: _NativeExtractionLimits,
) -> tuple[_NativePage, int]:
    """Read and position one embedded-text page under every native v1 limit."""

    width, height = _page_dimensions(page, document_ordinal, page_number, limits)
    if (
        not isinstance(remaining_characters, int)
        or isinstance(remaining_characters, bool)
        or not 0 <= remaining_characters <= limits.max_characters
    ):
        raise _page_error(document_ordinal, page_number)
    text_page: _PdfTextPage | None = None
    try:
        text_page = page.get_textpage()
        count = text_page.count_chars()
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise _page_error(document_ordinal, page_number)
        if count > remaining_characters:
            raise _page_error(document_ordinal, page_number)
        raw_text = text_page.get_text_range(index=0, count=count)
        if not isinstance(raw_text, str) or len(raw_text) != count:
            raise _page_error(document_ordinal, page_number)
        nonwhitespace_characters = sum(1 for character in raw_text if not character.isspace())
        if nonwhitespace_characters < MIN_NATIVE_NONWHITESPACE_CHARACTERS:
            raise _page_error(document_ordinal, page_number)
        characters: list[_RawCharacter] = []
        for index, character in enumerate(raw_text):
            if character.isspace():
                continue
            if unicodedata.category(character).startswith("C"):
                raise _page_error(document_ordinal, page_number)
            characters.append(
                _RawCharacter(
                    index=index,
                    text=character,
                    box=_char_box(text_page, index, width, height, document_ordinal, page_number),
                )
            )
        tokens, lines, provisional_phone_ordinals = _group_native_characters(
            tuple(characters), document_ordinal, page_number, limits
        )
        return (
            _NativePage(
                page_number=page_number,
                tokens=tokens,
                lines=lines,
                provisional_contact_phone_token_ordinals=provisional_phone_ordinals,
            ),
            count,
        )
    except StatementExtractionError:
        raise
    except Exception:
        raise _page_error(document_ordinal, page_number) from None
    finally:
        _close_quietly(text_page)


def _extract_native_pages(
    pdfium: ModuleType,
    payload: bytes,
    document_ordinal: int,
    *,
    limits: _NativeExtractionLimits | None = None,
) -> tuple[_NativePage, ...]:
    """Extract pages only inside the already resource-capped native worker.

    Tests may exercise this lower-level helper with a fake backend.  Production callers
    must use :func:`_extract_native_pages_in_worker`, which places PDFium's untrusted
    document opening and text decoding in a bounded child process.
    """

    effective_limits = limits or _native_extraction_limits(document_ordinal)
    document = _open_pdf_document(pdfium, payload, document_ordinal)
    try:
        page_count = len(document)
        if (
            not isinstance(page_count, int)
            or isinstance(page_count, bool)
            or not 1 <= page_count <= effective_limits.max_pages
        ):
            raise _page_error(document_ordinal)
        total_characters = 0
        pages: list[_NativePage] = []
        for index in range(page_count):
            page: _PdfPage | None = None
            try:
                page = document.get_page(index)
                extracted, character_count = _extract_native_page(
                    page,
                    document_ordinal,
                    index + 1,
                    effective_limits.max_characters - total_characters,
                    effective_limits,
                )
            except StatementExtractionError:
                raise
            except Exception:
                raise _page_error(document_ordinal, index + 1) from None
            finally:
                _close_quietly(page)
            total_characters += character_count
            if total_characters > effective_limits.max_characters:
                raise _page_error(document_ordinal, index + 1)
            pages.append(extracted)
        extracted_pages = tuple(pages)
        _validate_cross_page_identifier_fragments(extracted_pages, document_ordinal)
        return extracted_pages
    except StatementExtractionError:
        raise
    except Exception:
        raise _page_error(document_ordinal) from None
    finally:
        _close_quietly(document)


def _wire_box(box: BoundingBox) -> list[str]:
    """Encode a normalized box without ever serializing a backend object."""

    return [
        format(box.left, "f"),
        format(box.top, "f"),
        format(box.right, "f"),
        format(box.bottom, "f"),
    ]


def _serialize_native_pages(
    pages: tuple[_NativePage, ...], document_ordinal: int, limits: _NativeExtractionLimits
) -> bytes:
    """Return a bounded, JSON-only copy after a worker-local scrub replay."""

    scrubbed_pages = _rescrub_native_pages_before_serialization(pages, document_ordinal, limits)

    value: dict[str, object] = {
        "status": "ok",
        "pages": [
            {
                "page_number": page.page_number,
                "tokens": [
                    [
                        token.ordinal,
                        token.text,
                        *_wire_box(token.box),
                        token.line_ordinal,
                    ]
                    for token in page.tokens
                ],
                "lines": [[line.ordinal, list(line.token_ordinals)] for line in page.lines],
                "provisional_contact_phone_token_ordinals": list(
                    page.provisional_contact_phone_token_ordinals
                ),
            }
            for page in scrubbed_pages
        ],
    }
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise _page_error(document_ordinal) from None
    if len(encoded) > limits.max_wire_bytes:
        raise _page_error(document_ordinal)
    return encoded


def _rescrub_native_pages_before_serialization(
    pages: tuple[_NativePage, ...],
    document_ordinal: int,
    limits: _NativeExtractionLimits,
) -> tuple[_NativePage, ...]:
    """Replay the privacy boundary immediately before worker data crosses IPC.

    Native extraction already materializes scrubbed layout, but the worker must not
    rely on that upstream invariant when it serializes a wire reply.  This bounded
    replay means a compromised or future extraction path that returns a raw account
    identifier fails in the worker before any response bytes are emitted.
    """

    scrubbed_pages: list[_NativePage] = []
    for page in pages:
        tokens, lines, provisional_phone_ordinals = _materialize_scrubbed_native_layout(
            [[(token.text, token.box) for token in page.line_tokens(line)] for line in page.lines],
            document_ordinal,
            page.page_number,
            limits,
        )
        scrubbed_pages.append(
            _NativePage(
                page_number=page.page_number,
                tokens=tokens,
                lines=lines,
                provisional_contact_phone_token_ordinals=provisional_phone_ordinals,
            )
        )
    result = tuple(scrubbed_pages)
    _validate_cross_page_identifier_fragments(result, document_ordinal)
    return result


def _safe_rejection_page_number(
    error: StatementExtractionError, document_ordinal: int, limits: _NativeExtractionLimits
) -> int | None:
    """Recover only the controlled page ordinal from one generic parser failure."""

    match = re.fullmatch(
        rf"Treasurer Slides statement extraction failed for document {document_ordinal}"
        r"(?: page ([1-9][0-9]*))?",
        str(error),
    )
    if match is None or match.group(1) is None:
        return None
    page_number = int(match.group(1))
    return page_number if page_number <= limits.max_pages else None


def _serialize_worker_rejection(
    error: StatementExtractionError, document_ordinal: int, limits: _NativeExtractionLimits
) -> bytes:
    """Return a fixed, non-private rejection code rather than an exception string."""

    value: dict[str, object] = {"status": "rejected"}
    if (page_number := _safe_rejection_page_number(error, document_ordinal, limits)) is not None:
        value["page_number"] = page_number
    return json.dumps(value, separators=(",", ":")).encode("ascii")


def _wire_mapping(value: object, document_ordinal: int) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _page_error(document_ordinal)
    return cast(dict[str, object], value)


def _wire_list(value: object, document_ordinal: int) -> list[object]:
    if not isinstance(value, list):
        raise _page_error(document_ordinal)
    return cast(list[object], value)


def _wire_string(value: object, document_ordinal: int, *, maximum_length: int) -> str:
    if not isinstance(value, str) or len(value) > maximum_length:
        raise _page_error(document_ordinal)
    return value


def _wire_integer(value: object, document_ordinal: int, *, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise _page_error(document_ordinal)
    return value


def _wire_box_value(value: object, document_ordinal: int, page_number: int) -> BoundingBox:
    fields = _wire_list(value, document_ordinal)
    if len(fields) != 4:
        raise _page_error(document_ordinal, page_number)
    decimals: list[Decimal] = []
    for field in fields:
        text = _wire_string(field, document_ordinal, maximum_length=32)
        if re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?", text) is None:
            raise _page_error(document_ordinal, page_number)
        try:
            decimals.append(Decimal(text))
        except InvalidOperation:
            raise _page_error(document_ordinal, page_number) from None
    try:
        return BoundingBox(
            left=_position(decimals[0], document_ordinal, page_number),
            top=_position(decimals[1], document_ordinal, page_number),
            right=_position(decimals[2], document_ordinal, page_number),
            bottom=_position(decimals[3], document_ordinal, page_number),
        )
    except TreasurerSlidesError:
        raise _page_error(document_ordinal, page_number) from None


def _deserialize_native_pages(
    payload: bytes, document_ordinal: int, limits: _NativeExtractionLimits
) -> tuple[_NativePage, ...]:
    """Validate the worker's bounded JSON protocol before materializing evidence."""

    if not 1 <= len(payload) <= limits.max_wire_bytes:
        raise _page_error(document_ordinal)
    try:
        raw: object = json.loads(payload.decode("ascii"))
    except (UnicodeError, ValueError, json.JSONDecodeError):
        raise _page_error(document_ordinal) from None
    root = _wire_mapping(raw, document_ordinal)
    if root.get("status") == "rejected":
        if set(root) == {"status"}:
            raise _page_error(document_ordinal)
        if set(root) == {"status", "page_number"}:
            page_number = _wire_integer(
                root["page_number"],
                document_ordinal,
                minimum=1,
                maximum=limits.max_pages,
            )
            raise _page_error(document_ordinal, page_number)
        raise _page_error(document_ordinal)
    if set(root) != {"status", "pages"} or root["status"] != "ok":
        raise _page_error(document_ordinal)
    raw_pages = _wire_list(root["pages"], document_ordinal)
    if not 1 <= len(raw_pages) <= limits.max_pages:
        raise _page_error(document_ordinal)
    total_characters = 0
    pages: list[_NativePage] = []
    for expected_page_number, raw_page in enumerate(raw_pages, start=1):
        page = _wire_mapping(raw_page, document_ordinal)
        if set(page) != {
            "page_number",
            "tokens",
            "lines",
            "provisional_contact_phone_token_ordinals",
        }:
            raise _page_error(document_ordinal, expected_page_number)
        page_number = _wire_integer(
            page["page_number"],
            document_ordinal,
            minimum=expected_page_number,
            maximum=expected_page_number,
        )
        raw_tokens = _wire_list(page["tokens"], document_ordinal)
        if not 1 <= len(raw_tokens) <= limits.max_tokens_per_page:
            raise _page_error(document_ordinal, page_number)
        tokens: list[_LayoutToken] = []
        for expected_ordinal, raw_token in enumerate(raw_tokens, start=1):
            fields = _wire_list(raw_token, document_ordinal)
            if len(fields) != 7:
                raise _page_error(document_ordinal, page_number)
            ordinal = _wire_integer(
                fields[0],
                document_ordinal,
                minimum=expected_ordinal,
                maximum=expected_ordinal,
            )
            text = _wire_string(fields[1], document_ordinal, maximum_length=limits.max_characters)
            if not text.strip() or any(
                unicodedata.category(character).startswith("C") for character in text
            ):
                raise _page_error(document_ordinal, page_number)
            total_characters += len(text)
            if total_characters > limits.max_characters:
                raise _page_error(document_ordinal, page_number)
            line_ordinal = _wire_integer(
                fields[6],
                document_ordinal,
                minimum=1,
                maximum=limits.max_lines_per_page,
            )
            tokens.append(
                _LayoutToken(
                    ordinal=ordinal,
                    text=text,
                    box=_wire_box_value(fields[2:6], document_ordinal, page_number),
                    line_ordinal=line_ordinal,
                )
            )
        raw_lines = _wire_list(page["lines"], document_ordinal)
        if not 1 <= len(raw_lines) <= limits.max_lines_per_page:
            raise _page_error(document_ordinal, page_number)
        lines: list[_NativeLine] = []
        for expected_line_ordinal, raw_line in enumerate(raw_lines, start=1):
            fields = _wire_list(raw_line, document_ordinal)
            if len(fields) != 2:
                raise _page_error(document_ordinal, page_number)
            line_ordinal = _wire_integer(
                fields[0],
                document_ordinal,
                minimum=expected_line_ordinal,
                maximum=expected_line_ordinal,
            )
            raw_ordinals = _wire_list(fields[1], document_ordinal)
            if not raw_ordinals:
                raise _page_error(document_ordinal, page_number)
            ordinals = tuple(
                _wire_integer(
                    ordinal,
                    document_ordinal,
                    minimum=1,
                    maximum=len(tokens),
                )
                for ordinal in raw_ordinals
            )
            expected_ordinals = tuple(
                token.ordinal for token in tokens if token.line_ordinal == line_ordinal
            )
            if ordinals != expected_ordinals:
                raise _page_error(document_ordinal, page_number)
            lines.append(_NativeLine(ordinal=line_ordinal, token_ordinals=ordinals))
        if {token.line_ordinal for token in tokens} != {line.ordinal for line in lines}:
            raise _page_error(document_ordinal, page_number)
        raw_phone_ordinals = _wire_list(
            page["provisional_contact_phone_token_ordinals"], document_ordinal
        )
        phone_ordinals = tuple(
            _wire_integer(
                ordinal,
                document_ordinal,
                minimum=1,
                maximum=len(tokens),
            )
            for ordinal in raw_phone_ordinals
        )
        if len(set(phone_ordinals)) != len(phone_ordinals):
            raise _page_error(document_ordinal, page_number)
        scrubbed_tokens, scrubbed_lines, scrubbed_phone_ordinals = (
            _materialize_scrubbed_native_layout(
                [
                    [
                        (tokens[ordinal - 1].text, tokens[ordinal - 1].box)
                        for ordinal in line.token_ordinals
                    ]
                    for line in lines
                ],
                document_ordinal,
                page_number,
                limits,
            )
        )
        if phone_ordinals != scrubbed_phone_ordinals:
            raise _page_error(document_ordinal, page_number)
        pages.append(
            _NativePage(
                page_number=page_number,
                tokens=scrubbed_tokens,
                lines=scrubbed_lines,
                provisional_contact_phone_token_ordinals=scrubbed_phone_ordinals,
            )
        )
    deserialized_pages = tuple(pages)
    _validate_cross_page_identifier_fragments(deserialized_pages, document_ordinal)
    return deserialized_pages


def _send_worker_failure(connection: _NativeWorkerConnection) -> None:
    try:
        connection.send_bytes(b'{"status":"failed"}')
    except Exception:
        pass


def _native_worker_ready_frame(nonce: str) -> bytes:
    """Build the exact public readiness frame that gates every source-byte transfer."""

    if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{64}", nonce) is None:
        raise RuntimeError("invalid native worker readiness nonce")
    return f'{{"status":"ready","nonce":"{nonce}"}}'.encode("ascii")


def _native_page_extraction_after_limits(
    request: _NativeWorkerConnection,
    response: _NativeWorkerConnection,
    document_ordinal: int,
    limits: _NativeExtractionLimits,
) -> None:
    """Parse native bytes only after the caller has installed or preinstalled its cap."""

    try:
        payload = request.recv_bytes(limits.max_pdf_bytes)
        if not isinstance(payload, bytes) or len(payload) > limits.max_pdf_bytes:
            raise _page_error(document_ordinal)
        _close_quietly(request)
        pdfium = _require_pdfium()
        pages = _extract_native_pages(
            pdfium,
            payload,
            document_ordinal,
            limits=limits,
        )
        response.send_bytes(_serialize_native_pages(pages, document_ordinal, limits))
    except StatementExtractionError as error:
        try:
            response.send_bytes(_serialize_worker_rejection(error, document_ordinal, limits))
        except Exception:
            pass
    except BaseException:
        _send_worker_failure(response)
    finally:
        _close_quietly(request)
        _close_quietly(response)


def _start_native_page_worker(
    document_ordinal: int, limits: _NativeExtractionLimits
) -> tuple[_NativeWorkerConnection, _NativeWorkerConnection, _NativeWorkerProcess]:
    """Start a ready-attested LPAC worker before the broker reads a source PDF."""

    try:
        from pta_finance.treasurer_slides.native_sandbox import (
            NativeSandboxUnavailable,
            start_native_pdf_worker,
        )

        session = start_native_pdf_worker(
            document_ordinal=document_ordinal,
            limits_json=_serialize_native_limits(limits),
            worker_memory_bytes=limits.worker_memory_bytes,
            worker_cpu_seconds=limits.worker_cpu_seconds,
            ready_timeout_seconds=float(min(limits.wall_seconds, 5)),
        )
    except NativeSandboxUnavailable:
        raise _page_error(document_ordinal) from None
    return session.request_sender, session.response_receiver, session.process


def _stop_native_page_worker(worker: _NativeWorkerProcess, *, terminate_immediately: bool) -> bool:
    """Reap a worker and report a lifecycle/cleanup failure without leaking its details."""

    failed = False
    try:
        if terminate_immediately and worker.is_alive():
            worker.terminate()
        worker.join(timeout=0.25)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=0.25)
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=0.25)
        if worker.is_alive():
            failed = True
    except Exception:
        failed = True
    finally:
        try:
            worker.close()
        except Exception:
            failed = True
    return failed


def _write_native_worker_request(
    connection: _NativeWorkerConnection,
    payload: bytes,
    result: _NativeWorkerIoResult,
    complete: threading.Event,
) -> None:
    try:
        connection.send_bytes(payload)
    except BaseException as error:
        result.error = error
    finally:
        _close_quietly(connection)
        complete.set()


def _read_native_worker_response(
    connection: _NativeWorkerConnection,
    maximum_bytes: int,
    result: _NativeWorkerIoResult,
    complete: threading.Event,
) -> None:
    try:
        result.value = connection.recv_bytes(maximum_bytes)
    except BaseException as error:
        result.error = error
    finally:
        complete.set()


def _extract_native_pages_in_worker(
    payload: bytes,
    document_ordinal: int,
    limits: _NativeExtractionLimits,
    *,
    prepared_worker: tuple[_NativeWorkerConnection, _NativeWorkerConnection, _NativeWorkerProcess]
    | None = None,
) -> tuple[_NativePage, ...]:
    """Exchange bounded bytes under one deadline, then validate scrubbed layout."""

    request_sender: _NativeWorkerConnection | None = None
    response_receiver: _NativeWorkerConnection | None = None
    worker: _NativeWorkerProcess | None = None
    request_complete = threading.Event()
    response_complete = threading.Event()
    request_result = _NativeWorkerIoResult()
    response_result = _NativeWorkerIoResult()
    deadline_breached = False
    succeeded = False
    try:
        deadline = time.monotonic() + limits.wall_seconds
        if prepared_worker is None:
            request_sender, response_receiver, worker = _start_native_page_worker(
                document_ordinal, limits
            )
        else:
            request_sender, response_receiver, worker = prepared_worker
        response_thread = threading.Thread(
            target=_read_native_worker_response,
            args=(response_receiver, limits.max_wire_bytes, response_result, response_complete),
            daemon=True,
        )
        request_thread = threading.Thread(
            target=_write_native_worker_request,
            args=(request_sender, payload, request_result, request_complete),
            daemon=True,
        )
        response_thread.start()
        request_thread.start()
        if not response_complete.wait(max(0, deadline - time.monotonic())):
            deadline_breached = True
            raise _page_error(document_ordinal)
        if response_result.error is not None or response_result.value is None:
            raise _page_error(document_ordinal)
        pages = _deserialize_native_pages(response_result.value, document_ordinal, limits)
        succeeded = True
        return pages
    except StatementExtractionError:
        raise
    except Exception:
        raise _page_error(document_ordinal) from None
    finally:
        cleanup_failed = False
        if worker is not None:
            cleanup_failed = _stop_native_page_worker(
                worker, terminate_immediately=deadline_breached
            )
        _close_quietly(request_sender)
        _close_quietly(response_receiver)
        if deadline_breached:
            request_complete.wait(timeout=0.25)
            response_complete.wait(timeout=0.25)
        if succeeded and cleanup_failed:
            raise _page_error(document_ordinal)


def _normalized_token(token: _LayoutToken) -> str:
    return normalize_description(token.text)


def _word(token: _LayoutToken) -> str:
    return _normalized_token(token).strip(".,:;()[]{}")


def _normalized_page_text(page: _NativePage) -> str:
    return normalize_description(" ".join(token.text for token in page.tokens))


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(phrase)}(?![\w])", text) is not None


def _has_exact_title_control(line_texts: tuple[str, ...], phrase: str) -> bool:
    """Match a fixed fingerprint control only in its short page-title region.

    Issuer and page-kind phrases are controls, not transaction-description keywords.
    Restricting them to one exact top-of-page line prevents a generic table from
    becoming a recognized Wells Fargo page merely because narrative text quotes a
    control phrase.
    """

    return sum(line_text == phrase for line_text in line_texts[:_FINGERPRINT_TITLE_LINE_COUNT]) == 1


def _token_center(token: _LayoutToken) -> Decimal:
    return (token.box.left + token.box.right) / Decimal(2)


def _token_date_text(token: _LayoutToken) -> str | None:
    value = token.text.strip(".,:;")
    return value if _DATE_TOKEN_RE.fullmatch(value) else None


def _token_money_text(token: _LayoutToken) -> str | None:
    value = token.text.strip(";:")
    match = _MONEY_TOKEN_RE.fullmatch(value)
    return None if match is None else match.group(1).replace(",", "")


def _date_matches(tokens: tuple[_LayoutToken, ...]) -> tuple[tuple[int, str], ...]:
    return tuple(
        (token.ordinal, value) for token in tokens if (value := _token_date_text(token)) is not None
    )


def _money_matches(tokens: tuple[_LayoutToken, ...]) -> tuple[tuple[int, str], ...]:
    return tuple(
        (token.ordinal, value)
        for token in tokens
        if (value := _token_money_text(token)) is not None
    )


def _monetary_like_matches(tokens: tuple[_LayoutToken, ...]) -> tuple[int, ...]:
    """Return every token that could be a supported or malformed monetary value.

    Canonical money remains deliberately narrower in ``_money_matches``.  This
    lexical guard exists solely to reject unsupported financial-looking source
    content before it can be mistaken for prose or boilerplate.
    """

    return tuple(token.ordinal for token in tokens if _is_monetary_like_text(token.text))


def _has_monetary_like_candidate(tokens: tuple[_LayoutToken, ...]) -> bool:
    """Recognize token-local or fragmented amount syntax on one visual line."""

    return bool(_monetary_like_matches(tokens)) or _has_fragmented_monetary_like_sequence(
        tuple(token.text for token in tokens)
    )


def _has_balance_like_candidate(tokens: tuple[_LayoutToken, ...]) -> bool:
    """Recognize an unsupported numeric balance line before boilerplate can ignore it."""

    if not any(_word(token) == "balance" for token in tokens):
        return False
    return (
        bool(_date_matches(tokens))
        or _has_monetary_like_candidate(tokens)
        or any(
            any(_screening_text(character).isdecimal() for character in _word(token))
            for token in tokens
        )
    )


def _term_ordinals(
    tokens: tuple[_LayoutToken, ...], alternatives: tuple[tuple[str, ...], ...]
) -> tuple[int, ...] | None:
    """Return one unambiguous positioned spelling of a control phrase."""

    words = tuple(_word(token) for token in tokens)
    matches: list[tuple[int, ...]] = []
    for alternative in alternatives:
        for start in range(0, len(words) - len(alternative) + 1):
            if words[start : start + len(alternative)] == alternative:
                matches.append(
                    tuple(token.ordinal for token in tokens[start : start + len(alternative)])
                )
    if len(matches) != 1:
        return None
    return matches[0]


def _box_for_ordinals(page: _NativePage, ordinals: tuple[int, ...]) -> BoundingBox:
    return _box_for_tokens(tuple(page.token(ordinal) for ordinal in ordinals))


_BALANCE_KIND_TERMS: dict[BalanceKind, tuple[tuple[str, ...], ...]] = {
    BalanceKind.OPENING: (("opening",), ("beginning",)),
    BalanceKind.CLOSING: (("closing",), ("ending",)),
    BalanceKind.COLLECTED: (("collected",),),
    BalanceKind.AVAILABLE: (("available",),),
}
_BOUNDARY_TERMS: dict[BalanceBoundary, tuple[tuple[str, ...], ...]] = {
    BalanceBoundary.START_OF_DAY: (("start", "of", "day"), ("start-of-day",)),
    BalanceBoundary.END_OF_DAY: (("end", "of", "day"), ("end-of-day",)),
    BalanceBoundary.CAPTURE: (("capture",), ("as", "of"), ("as-of",)),
}
_PENDING_TERMS: dict[bool, tuple[tuple[str, ...], ...]] = {
    True: (("includes", "pending"), ("including", "pending")),
    False: (("excludes", "pending"), ("excluding", "pending")),
}
_ALLOWED_BALANCE_ROWS_BY_PAGE_KIND: dict[
    PageKind, frozenset[tuple[BalanceKind, BalanceBoundary, bool]]
] = {
    # The fixed monthly-summary contract presents cleared opening and closing
    # balances only.  Current reports instead present a pending-inclusive
    # available balance and may include a separately captured collected balance.
    PageKind.MONTHLY_SUMMARY: frozenset(
        {
            (BalanceKind.OPENING, BalanceBoundary.START_OF_DAY, False),
            (BalanceKind.CLOSING, BalanceBoundary.END_OF_DAY, False),
        }
    ),
    PageKind.CURRENT_BALANCE: frozenset(
        {
            (BalanceKind.AVAILABLE, BalanceBoundary.CAPTURE, True),
            (BalanceKind.COLLECTED, BalanceBoundary.CAPTURE, False),
        }
    ),
}
_HEADER_TERMS: dict[ActivityColumn, frozenset[str]] = {
    ActivityColumn.DATE: frozenset({"date"}),
    ActivityColumn.DESCRIPTION: frozenset({"description", "details"}),
    ActivityColumn.DEBIT: frozenset({"debit", "debits", "withdrawal", "withdrawals"}),
    ActivityColumn.CREDIT: frozenset(
        {"credit", "credits", "deposit", "deposits", "addition", "additions"}
    ),
}
_KNOWN_BOILERPLATE_MARKERS = (
    "member fdic",
    "equal housing lender",
    "important account information",
    "privacy notice",
    "questions? we re here to help",
)
_TABLE_FOOTER_MARKERS = (
    "end of activity",
    "end of transactions",
    "important account information",
    "member fdic",
    "equal housing lender",
)
_EMPTY_ACTIVITY_MARKERS = ("no activity", "no transactions")
_FINGERPRINT_TITLE_LINE_COUNT = 3


@dataclass(frozen=True)
class _BalanceDraft:
    line_ordinal: int
    kind: BalanceKind
    boundary: BalanceBoundary
    includes_pending: bool
    date_text: str
    amount_text: str
    date_ordinals: tuple[int, ...]
    amount_ordinals: tuple[int, ...]
    kind_ordinals: tuple[int, ...]
    boundary_ordinals: tuple[int, ...]
    pending_ordinals: tuple[int, ...]


@dataclass(frozen=True)
class _HeaderDraft:
    line_ordinal: int
    column_ordinals: dict[ActivityColumn, tuple[int, ...]]


@dataclass(frozen=True)
class _RecognizedPage:
    native: _NativePage
    page_kind: PageKind
    fingerprint: PageFingerprint
    header: _HeaderDraft | None
    balances: tuple[_BalanceDraft, ...]


def _detect_balance_drafts(page: _NativePage, document_ordinal: int) -> tuple[_BalanceDraft, ...]:
    drafts: list[_BalanceDraft] = []
    for line in page.lines:
        tokens = page.line_tokens(line)
        kind_matches = {
            kind: _term_ordinals(tokens, terms) for kind, terms in _BALANCE_KIND_TERMS.items()
        }
        matched_kinds = [(kind, ordinals) for kind, ordinals in kind_matches.items() if ordinals]
        balance_marker_ordinals = tuple(
            token.ordinal for token in tokens if _word(token) == "balance"
        )
        monetary_like = _monetary_like_matches(tokens)
        fragmented_monetary_like = _has_fragmented_monetary_like_sequence(
            tuple(token.text for token in tokens)
        )
        if not matched_kinds:
            if _has_balance_like_candidate(tokens):
                raise _page_error(document_ordinal, page.page_number)
            continue
        boundaries = {
            boundary: _term_ordinals(tokens, terms) for boundary, terms in _BOUNDARY_TERMS.items()
        }
        pending = {
            includes_pending: _term_ordinals(tokens, terms)
            for includes_pending, terms in _PENDING_TERMS.items()
        }
        matched_boundaries = [
            (boundary, ordinals) for boundary, ordinals in boundaries.items() if ordinals
        ]
        matched_pending = [(basis, ordinals) for basis, ordinals in pending.items() if ordinals]
        dates = _date_matches(tokens)
        amounts = _money_matches(tokens)
        if (
            len(balance_marker_ordinals) != 1
            or len(matched_kinds) != 1
            or len(matched_boundaries) != 1
            or len(matched_pending) != 1
            or len(dates) != 1
            or len(amounts) != 1
            or monetary_like != tuple(ordinal for ordinal, _ in amounts)
            or fragmented_monetary_like
        ):
            raise _page_error(document_ordinal, page.page_number)
        kind, kind_ordinals = matched_kinds[0]
        boundary, boundary_ordinals = matched_boundaries[0]
        includes_pending, pending_ordinals = matched_pending[0]
        date_ordinal, date_text = dates[0]
        amount_ordinal, amount_text = amounts[0]
        recognized_ordinals = {
            *balance_marker_ordinals,
            *kind_ordinals,
            *boundary_ordinals,
            *pending_ordinals,
            date_ordinal,
            amount_ordinal,
        }
        if recognized_ordinals != {token.ordinal for token in tokens}:
            raise _page_error(document_ordinal, page.page_number)
        drafts.append(
            _BalanceDraft(
                line_ordinal=line.ordinal,
                kind=kind,
                boundary=boundary,
                includes_pending=includes_pending,
                date_text=date_text,
                amount_text=amount_text,
                date_ordinals=(date_ordinal,),
                amount_ordinals=(amount_ordinal,),
                kind_ordinals=kind_ordinals,
                boundary_ordinals=boundary_ordinals,
                pending_ordinals=pending_ordinals,
            )
        )
    return tuple(drafts)


def _validate_balance_page_contract(
    page_kind: PageKind,
    balances: tuple[_BalanceDraft, ...],
    document_ordinal: int,
    page_number: int,
) -> None:
    """Reject balance controls not defined for this fixed page fingerprint."""

    allowed_rows = _ALLOWED_BALANCE_ROWS_BY_PAGE_KIND.get(page_kind)
    actual_rows = {
        (balance.kind, balance.boundary, balance.includes_pending) for balance in balances
    }
    if (
        allowed_rows is None
        or len(actual_rows) != len(balances)
        or not actual_rows <= allowed_rows
        or (page_kind is PageKind.MONTHLY_SUMMARY and actual_rows != allowed_rows)
        or (
            page_kind is PageKind.CURRENT_BALANCE
            and (BalanceKind.AVAILABLE, BalanceBoundary.CAPTURE, True) not in actual_rows
        )
    ):
        raise _page_error(document_ordinal, page_number)


def _header_column_for_token(token: _LayoutToken) -> ActivityColumn | None:
    text = _word(token)
    fragments = tuple(fragment for fragment in re.split(r"[/]", text) if fragment)
    matches = [
        column
        for column, terms in _HEADER_TERMS.items()
        if fragments and all(fragment in terms for fragment in fragments)
    ]
    return matches[0] if len(matches) == 1 else None


def _detect_activity_header(page: _NativePage, document_ordinal: int) -> _HeaderDraft | None:
    complete: list[_HeaderDraft] = []
    for line in page.lines:
        tokens = page.line_tokens(line)
        columns: dict[ActivityColumn, list[int]] = {column: [] for column in ActivityColumn}
        for token in tokens:
            column = _header_column_for_token(token)
            if column is not None:
                columns[column].append(token.ordinal)
        found = sum(1 for ordinals in columns.values() if ordinals)
        if 2 <= found < len(ActivityColumn):
            raise _page_error(document_ordinal, page.page_number)
        if found == len(ActivityColumn):
            recognized_ordinals = {ordinal for ordinals in columns.values() for ordinal in ordinals}
            if (
                any(len(ordinals) != 1 for ordinals in columns.values())
                or recognized_ordinals != {token.ordinal for token in tokens}
                or _date_matches(tokens)
                or _has_monetary_like_candidate(tokens)
            ):
                raise _page_error(document_ordinal, page.page_number)
            ordered = [
                _token_center(page.token(columns[column][0]))
                for column in (
                    ActivityColumn.DATE,
                    ActivityColumn.DESCRIPTION,
                    ActivityColumn.DEBIT,
                    ActivityColumn.CREDIT,
                )
            ]
            if ordered != sorted(ordered) or len(set(ordered)) != len(ordered):
                raise _page_error(document_ordinal, page.page_number)
            complete.append(
                _HeaderDraft(
                    line_ordinal=line.ordinal,
                    column_ordinals={column: tuple(value) for column, value in columns.items()},
                )
            )
    if len(complete) > 1:
        raise _page_error(document_ordinal, page.page_number)
    return complete[0] if complete else None


def _recognize_page(
    page: _NativePage, document_ordinal: int, document: DocumentSpec
) -> _RecognizedPage:
    text = _normalized_page_text(page)
    line_texts = tuple(
        normalize_description(" ".join(token.text for token in page.line_tokens(line)))
        for line in page.lines
    )
    header = _detect_activity_header(page, document_ordinal)
    # A complete transaction-table header establishes the page's only financial-row
    # grammar.  Balance-kind words are ordinary description text inside that table;
    # summary/current pages, which lack the header, retain the strict balance grammar.
    balances: tuple[_BalanceDraft, ...] = ()
    if header is None:
        balances = _detect_balance_drafts(page, document_ordinal)
    has_wells = _has_exact_title_control(line_texts, "wells fargo")
    monthly_summary_marked = _has_exact_title_control(line_texts, "monthly account statement")
    monthly_activity_marked = _has_exact_title_control(line_texts, "monthly account activity")
    current_balance_marked = _has_exact_title_control(line_texts, "current account balance")
    current_activity_marked = _has_exact_title_control(line_texts, "current activity")
    monthly_marked = monthly_summary_marked or monthly_activity_marked
    current_marked = current_balance_marked or current_activity_marked
    financial_candidates = any(
        _date_matches(page.line_tokens(line))
        or _has_monetary_like_candidate(page.line_tokens(line))
        or _has_balance_like_candidate(page.line_tokens(line))
        or any(_header_column_for_token(token) is not None for token in page.line_tokens(line))
        for line in page.lines
    )
    boilerplate_marked = any(_has_phrase(text, marker) for marker in _KNOWN_BOILERPLATE_MARKERS)
    if (
        sum(
            (
                monthly_summary_marked,
                monthly_activity_marked,
                current_balance_marked,
                current_activity_marked,
            )
        )
        > 1
    ):
        raise _page_error(document_ordinal, page.page_number)
    if header is not None:
        if not has_wells:
            raise _page_error(document_ordinal, page.page_number)
        if monthly_activity_marked:
            page_kind = PageKind.MONTHLY_ACTIVITY
            fingerprint = PageFingerprint.WELLS_FARGO_V1_MONTHLY_ACTIVITY
        elif current_activity_marked:
            page_kind = PageKind.CURRENT_ACTIVITY
            fingerprint = PageFingerprint.WELLS_FARGO_V1_CURRENT_ACTIVITY
        else:
            raise _page_error(document_ordinal, page.page_number)
    elif balances:
        _validate_balance_page_financial_lines(page, balances, document_ordinal)
        if not has_wells:
            raise _page_error(document_ordinal, page.page_number)
        if monthly_summary_marked:
            page_kind = PageKind.MONTHLY_SUMMARY
            fingerprint = PageFingerprint.WELLS_FARGO_V1_MONTHLY_SUMMARY
        elif current_balance_marked:
            page_kind = PageKind.CURRENT_BALANCE
            fingerprint = PageFingerprint.WELLS_FARGO_V1_CURRENT_BALANCE
        else:
            raise _page_error(document_ordinal, page.page_number)
    elif (
        has_wells
        and boilerplate_marked
        and not monthly_marked
        and not current_marked
        and not financial_candidates
    ):
        page_kind = PageKind.BOILERPLATE
        fingerprint = PageFingerprint.WELLS_FARGO_V1_BOILERPLATE
    else:
        raise _page_error(document_ordinal, page.page_number)
    allowed = {
        DocumentKind.MONTHLY_STATEMENT: {
            PageKind.MONTHLY_SUMMARY,
            PageKind.MONTHLY_ACTIVITY,
            PageKind.BOILERPLATE,
        },
        DocumentKind.CURRENT_ACTIVITY: {
            PageKind.CURRENT_BALANCE,
            PageKind.CURRENT_ACTIVITY,
            PageKind.BOILERPLATE,
        },
    }
    if page_kind not in allowed[document.document_kind]:
        raise _page_error(document_ordinal, page.page_number)
    if balances:
        _validate_balance_page_contract(
            page_kind,
            balances,
            document_ordinal,
            page.page_number,
        )
    if page.provisional_contact_phone_token_ordinals and page_kind is not PageKind.BOILERPLATE:
        raise _page_error(document_ordinal, page.page_number)
    return _RecognizedPage(
        native=page,
        page_kind=page_kind,
        fingerprint=fingerprint,
        header=header,
        balances=balances,
    )


def _parse_explicit_date(value: str, document_ordinal: int, page_number: int) -> date:
    try:
        if "-" in value:
            return date.fromisoformat(value)
        month, day, year = value.split("/")
        if len(year) == 2:
            year = f"20{year}"
        if len(year) != 4:
            raise ValueError
        return date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        raise _page_error(document_ordinal, page_number) from None


def _is_explicit_date(value: str) -> bool:
    return value.count("/") == 2 or "-" in value


def _is_allowed_date_metadata_line(
    tokens: tuple[_LayoutToken, ...], document_ordinal: int, page_number: int
) -> bool:
    """Accept only known non-financial date metadata outside recognized source rows."""

    dates = _date_matches(tokens)
    if _has_monetary_like_candidate(tokens):
        return False
    if not dates:
        return True
    text = normalize_description(" ".join(token.text for token in tokens))
    if _has_phrase(text, "statement period"):
        if len(dates) != 2 or any(not _is_explicit_date(value) for _, value in dates):
            return False
        start = _parse_explicit_date(dates[0][1], document_ordinal, page_number)
        end = _parse_explicit_date(dates[1][1], document_ordinal, page_number)
        return start <= end
    if _has_phrase(text, "as of"):
        if len(dates) != 1 or not _is_explicit_date(dates[0][1]):
            return False
        _parse_explicit_date(dates[0][1], document_ordinal, page_number)
        return True
    return False


def _validate_balance_page_financial_lines(
    page: _NativePage, balances: tuple[_BalanceDraft, ...], document_ordinal: int
) -> None:
    """Reject dated or monetary balance-page text not bound to a recognized balance row."""

    recognized_lines = {balance.line_ordinal for balance in balances}
    for line in page.lines:
        tokens = page.line_tokens(line)
        monetary_like = _monetary_like_matches(tokens)
        fragmented_monetary_like = _has_fragmented_monetary_like_sequence(
            tuple(token.text for token in tokens)
        )
        if not (_date_matches(tokens) or monetary_like or fragmented_monetary_like):
            continue
        if line.ordinal in recognized_lines:
            if (
                monetary_like != tuple(ordinal for ordinal, _ in _money_matches(tokens))
                or fragmented_monetary_like
            ):
                raise _page_error(document_ordinal, page.page_number)
            continue
        if not _is_allowed_date_metadata_line(tokens, document_ordinal, page.page_number):
            raise _page_error(document_ordinal, page.page_number)


def _periods_for_page(page: _NativePage, document_ordinal: int) -> tuple[tuple[date, date], ...]:
    periods: list[tuple[date, date]] = []
    for line in page.lines:
        tokens = page.line_tokens(line)
        text = normalize_description(" ".join(token.text for token in tokens))
        if not _has_phrase(text, "statement period"):
            continue
        values = _date_matches(tokens)
        if len(values) != 2 or any(
            value.count("/") != 2 and "-" not in value for _, value in values
        ):
            raise _page_error(document_ordinal, page.page_number)
        start = _parse_explicit_date(values[0][1], document_ordinal, page.page_number)
        end = _parse_explicit_date(values[1][1], document_ordinal, page.page_number)
        if end < start:
            raise _page_error(document_ordinal, page.page_number)
        periods.append((start, end))
    return tuple(periods)


def _capture_dates_for_current_balance_page(
    page: _RecognizedPage, document_ordinal: int
) -> tuple[date, ...]:
    """Return only explicit dates bound to accepted current balance capture controls."""

    if page.page_kind is not PageKind.CURRENT_BALANCE:
        return ()
    captures: list[date] = []
    for draft in page.balances:
        if draft.boundary is not BalanceBoundary.CAPTURE:
            continue
        if not _is_explicit_date(draft.date_text):
            raise _page_error(document_ordinal, page.native.page_number)
        captures.append(
            _parse_explicit_date(draft.date_text, document_ordinal, page.native.page_number)
        )
    return tuple(captures)


@dataclass(frozen=True)
class _DocumentDates:
    coverage_start: date | None
    coverage_end: date
    capture_date: date


def _document_dates(
    pages: tuple[_RecognizedPage, ...], document_ordinal: int, document: DocumentSpec
) -> _DocumentDates:
    if document.document_kind is DocumentKind.MONTHLY_STATEMENT:
        periods = tuple(
            period for page in pages for period in _periods_for_page(page.native, document_ordinal)
        )
        if not periods or len(set(periods)) != 1:
            raise _page_error(document_ordinal)
        start, end = periods[0]
        return _DocumentDates(coverage_start=start, coverage_end=end, capture_date=end)
    captures = tuple(
        capture
        for page in pages
        for capture in _capture_dates_for_current_balance_page(page, document_ordinal)
    )
    if not captures:
        raise _page_error(document_ordinal)
    # A current report may show separately captured available/collected balances.
    # Retain each row's date; the observation-level capture is the latest known one.
    capture = max(captures)
    return _DocumentDates(coverage_start=None, coverage_end=capture, capture_date=capture)


def _resolve_source_date(
    value: str,
    dates: _DocumentDates,
    document_ordinal: int,
    page_number: int,
) -> date:
    if value.count("/") == 2 or "-" in value:
        parsed = _parse_explicit_date(value, document_ordinal, page_number)
        if dates.coverage_start is not None:
            if not dates.coverage_start <= parsed <= dates.coverage_end:
                raise _page_error(document_ordinal, page_number)
        elif parsed > dates.capture_date or parsed < dates.capture_date - timedelta(days=366):
            raise _page_error(document_ordinal, page_number)
        return parsed
    try:
        month, day = (int(part) for part in value.split("/"))
    except (TypeError, ValueError):
        raise _page_error(document_ordinal, page_number) from None
    if dates.coverage_start is not None:
        candidates = tuple(
            candidate
            for year in range(dates.coverage_start.year - 1, dates.coverage_end.year + 2)
            if (candidate := _calendar_date(year, month, day)) is not None
            and dates.coverage_start <= candidate <= dates.coverage_end
        )
    else:
        candidates = tuple(
            candidate
            for year in (dates.capture_date.year, dates.capture_date.year - 1)
            if (candidate := _calendar_date(year, month, day)) is not None
            and dates.capture_date - timedelta(days=366) <= candidate <= dates.capture_date
        )
    if len(candidates) != 1:
        raise _page_error(document_ordinal, page_number)
    return candidates[0]


def _calendar_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


@dataclass(frozen=True)
class _StatusDraft:
    status: TransactionStatus
    token_ordinals: tuple[int, ...]
    box: BoundingBox


@dataclass(frozen=True)
class _ActivityDraft:
    line_ordinal: int
    status_index: int
    date_text: str
    direction: Direction
    amount_text: str
    date_ordinals: tuple[int, ...]
    description_ordinals: tuple[int, ...]
    money_ordinals: tuple[int, ...]
    row_box: BoundingBox


def _status_for_line(
    tokens: tuple[_LayoutToken, ...], document_ordinal: int, page_number: int
) -> tuple[TransactionStatus, tuple[int, ...]] | None:
    posted = tuple(token.ordinal for token in tokens if _word(token) == "posted")
    pending = tuple(token.ordinal for token in tokens if _word(token) == "pending")
    if posted and pending:
        return None
    if posted and _is_status_control_line(tokens, TransactionStatus.POSTED):
        return TransactionStatus.POSTED, posted
    if pending and _is_status_control_line(tokens, TransactionStatus.PENDING):
        return TransactionStatus.PENDING, pending
    return None


def _is_status_control_line(tokens: tuple[_LayoutToken, ...], status: TransactionStatus) -> bool:
    allowed = frozenset({status.value, "transaction", "transactions", "activity"})
    return bool(tokens) and all(_word(token) in allowed for token in tokens)


def _line_has_footer(text: str) -> bool:
    return any(_has_phrase(text, marker) for marker in _TABLE_FOOTER_MARKERS)


def _line_is_empty_activity(text: str) -> bool:
    # Unlike a footer phrase, this represents the entire state of a table.  Accepting
    # it as a substring would let a contradictory activity row be hidden in prose.
    return text in _EMPTY_ACTIVITY_MARKERS


def _tokens_in_band(
    tokens: tuple[_LayoutToken, ...], band: ActivityColumnBand
) -> tuple[_LayoutToken, ...]:
    return tuple(token for token in tokens if band.contains_box_centroid(token.box))


def _activity_columns(
    page: _NativePage, header: _HeaderDraft, document_ordinal: int
) -> tuple[ActivityColumnBand, ...]:
    ordered_columns = (
        ActivityColumn.DATE,
        ActivityColumn.DESCRIPTION,
        ActivityColumn.DEBIT,
        ActivityColumn.CREDIT,
    )
    header_boxes = {
        column: _box_for_ordinals(page, header.column_ordinals[column])
        for column in ordered_columns
    }
    centers = {column: (box.left + box.right) / Decimal(2) for column, box in header_boxes.items()}
    boundaries = [
        (centers[first] + centers[second]) / Decimal(2)
        for first, second in zip(ordered_columns, ordered_columns[1:], strict=False)
    ]
    lefts = tuple(
        _position(value, document_ordinal, page.page_number)
        for value in (
            Decimal("0"),
            boundaries[0] + _BAND_GAP,
            boundaries[1] + _BAND_GAP,
            boundaries[2] + _BAND_GAP,
        )
    )
    rights = tuple(
        _position(value, document_ordinal, page.page_number)
        for value in (
            boundaries[0] - _BAND_GAP,
            boundaries[1] - _BAND_GAP,
            boundaries[2] - _BAND_GAP,
            Decimal("1"),
        )
    )
    return tuple(
        ActivityColumnBand(
            column=column,
            left=left,
            right=right,
            header_box=header_boxes[column],
            header_token_ordinals=header.column_ordinals[column],
        )
        for column, left, right in zip(ordered_columns, lefts, rights, strict=True)
    )


def _activity_draft_for_line(
    page: _NativePage,
    tokens: tuple[_LayoutToken, ...],
    bands: tuple[ActivityColumnBand, ...],
    status_index: int,
    document_ordinal: int,
) -> _ActivityDraft:
    by_column = {band.column: _tokens_in_band(tokens, band) for band in bands}
    all_dates = _date_matches(tokens)
    all_amounts = _money_matches(tokens)
    monetary_like = _monetary_like_matches(tokens)
    fragmented_monetary_like = _has_fragmented_monetary_like_sequence(
        tuple(token.text for token in tokens)
    )
    date_tokens = _date_matches(by_column[ActivityColumn.DATE])
    debit_amounts = _money_matches(by_column[ActivityColumn.DEBIT])
    credit_amounts = _money_matches(by_column[ActivityColumn.CREDIT])
    descriptions = by_column[ActivityColumn.DESCRIPTION]
    if (
        len(all_dates) != 1
        or len(date_tokens) != 1
        or len(all_amounts) != 1
        or monetary_like != tuple(ordinal for ordinal, _ in all_amounts)
        or fragmented_monetary_like
        or len(debit_amounts) + len(credit_amounts) != 1
        or not descriptions
        or any(
            _token_date_text(token) is not None or _token_money_text(token) is not None
            for token in descriptions
        )
    ):
        raise _page_error(document_ordinal, page.page_number)
    selected = {
        date_tokens[0][0],
        *(token.ordinal for token in descriptions),
        *(ordinal for ordinal, _ in debit_amounts),
        *(ordinal for ordinal, _ in credit_amounts),
    }
    if selected != {token.ordinal for token in tokens}:
        raise _page_error(document_ordinal, page.page_number)
    if debit_amounts:
        direction = Direction.DEBIT
        amount_ordinal, amount_text = debit_amounts[0]
    else:
        direction = Direction.CREDIT
        amount_ordinal, amount_text = credit_amounts[0]
    date_ordinal, date_text = date_tokens[0]
    return _ActivityDraft(
        line_ordinal=tokens[0].line_ordinal,
        status_index=status_index,
        date_text=date_text,
        direction=direction,
        amount_text=amount_text,
        date_ordinals=(date_ordinal,),
        description_ordinals=tuple(token.ordinal for token in descriptions),
        money_ordinals=(amount_ordinal,),
        row_box=_box_for_tokens(tokens),
    )


def _parse_activity_table(
    page: _NativePage,
    header: _HeaderDraft,
    document_ordinal: int,
    remaining_rows: int,
    max_transaction_rows: int,
) -> tuple[ActivityTableEvidence, tuple[_ActivityDraft, ...], tuple[ActivityStatusControl, ...]]:
    if (
        not isinstance(remaining_rows, int)
        or isinstance(remaining_rows, bool)
        or not isinstance(max_transaction_rows, int)
        or isinstance(max_transaction_rows, bool)
        or not 0 <= remaining_rows <= max_transaction_rows <= _HARD_MAX_TRANSACTION_ROWS
    ):
        raise _page_error(document_ordinal, page.page_number)
    columns = _activity_columns(page, header, document_ordinal)
    header_box = _box_for_ordinals(
        page,
        tuple(ordinal for column in columns for ordinal in column.header_token_ordinals),
    )
    for line in page.lines:
        if line.ordinal >= header.line_ordinal:
            break
        if not _is_allowed_date_metadata_line(
            page.line_tokens(line), document_ordinal, page.page_number
        ):
            raise _page_error(document_ordinal, page.page_number)
    statuses: list[_StatusDraft] = []
    drafts: list[_ActivityDraft] = []
    reached_footer = False
    empty_activity_seen = False
    for line in page.lines:
        if line.ordinal <= header.line_ordinal:
            continue
        tokens = page.line_tokens(line)
        text = normalize_description(" ".join(token.text for token in tokens))
        dates = _date_matches(tokens)
        has_monetary_like = _has_monetary_like_candidate(tokens)
        if reached_footer:
            if dates or has_monetary_like:
                raise _page_error(document_ordinal, page.page_number)
            continue
        if _line_has_footer(text):
            if (
                dates
                or has_monetary_like
                or _status_for_line(tokens, document_ordinal, page.page_number)
            ):
                raise _page_error(document_ordinal, page.page_number)
            reached_footer = True
            continue
        line_status = _status_for_line(tokens, document_ordinal, page.page_number)
        if line_status is not None:
            if empty_activity_seen:
                raise _page_error(document_ordinal, page.page_number)
            value, ordinals = line_status
            if dates or has_monetary_like or not _is_status_control_line(tokens, value):
                raise _page_error(document_ordinal, page.page_number)
            statuses.append(_StatusDraft(value, ordinals, _box_for_ordinals(page, ordinals)))
            continue
        if _line_is_empty_activity(text):
            if dates or has_monetary_like or empty_activity_seen or statuses or drafts:
                raise _page_error(document_ordinal, page.page_number)
            empty_activity_seen = True
            continue
        if not (dates or has_monetary_like):
            raise _page_error(document_ordinal, page.page_number)
        if empty_activity_seen:
            raise _page_error(document_ordinal, page.page_number)
        if not statuses:
            raise _page_error(document_ordinal, page.page_number)
        if len(drafts) >= remaining_rows:
            raise _page_error(document_ordinal, page.page_number)
        drafts.append(
            _activity_draft_for_line(page, tokens, columns, len(statuses) - 1, document_ordinal)
        )
    if not drafts and not empty_activity_seen:
        raise _page_error(document_ordinal, page.page_number)
    rows = tuple(
        ActivityRowEvidence(row_ordinal=index, row_box=draft.row_box)
        for index, draft in enumerate(drafts, start=1)
    )
    controls: list[ActivityStatusControl] = []
    for status_index, status_draft in enumerate(statuses):
        controlled = tuple(
            row_ordinal
            for row_ordinal, draft in enumerate(drafts, start=1)
            if draft.status_index == status_index
        )
        if not controlled:
            raise _page_error(document_ordinal, page.page_number)
        controls.append(
            ActivityStatusControl(
                status=status_draft.status,
                box=status_draft.box,
                token_ordinals=status_draft.token_ordinals,
                row_ordinals=controlled,
            )
        )
    try:
        table = ActivityTableEvidence(
            table_ordinal=1,
            header_box=header_box,
            columns=columns,
            rows=rows,
            status_controls=tuple(controls),
        )
    except TreasurerSlidesError:
        raise _page_error(document_ordinal, page.page_number) from None
    return table, tuple(drafts), tuple(controls)


def _model_tokens(
    page: _NativePage, *, excluded_token_ordinals: frozenset[int] = frozenset()
) -> tuple[PositionedToken, ...]:
    """Build model evidence, omitting only post-recognition provisional phone tokens."""

    return tuple(
        PositionedToken(
            page_number=page.page_number,
            box=token.box,
            text=token.text,
            extraction_method=ExtractionMethod.NATIVE,
            confidence=100,
        )
        for token in page.tokens
        if token.ordinal not in excluded_token_ordinals
    )


def _resolve_balance_observed_on(
    draft: _BalanceDraft,
    recognized: _RecognizedPage,
    dates: _DocumentDates,
    document_ordinal: int,
) -> date:
    """Resolve one balance date and enforce the fixed monthly boundary semantics."""

    page_number = recognized.native.page_number
    observed_on = _resolve_source_date(draft.date_text, dates, document_ordinal, page_number)
    if recognized.page_kind is PageKind.MONTHLY_SUMMARY:
        expected = {
            BalanceKind.OPENING: dates.coverage_start,
            BalanceKind.CLOSING: dates.coverage_end,
        }.get(draft.kind)
        if expected is None or observed_on != expected:
            raise _page_error(document_ordinal, page_number)
    return observed_on


def _balance_artifacts(
    recognized: _RecognizedPage,
    model_tokens: tuple[PositionedToken, ...],
    document_ordinal: int,
    document: DocumentSpec,
    source_sha256: str,
    dates: _DocumentDates,
) -> tuple[PageEvidence, tuple[BalanceObservation, ...]]:
    page = recognized.native
    rows: list[BalanceRowEvidence] = []
    controls: list[BalanceControlEvidence] = []
    observations: list[BalanceObservation] = []
    for row_ordinal, draft in enumerate(recognized.balances, start=1):
        all_row_ordinals = tuple(
            sorted(
                {
                    *draft.date_ordinals,
                    *draft.amount_ordinals,
                    *draft.kind_ordinals,
                    *draft.boundary_ordinals,
                    *draft.pending_ordinals,
                }
            )
        )
        control_ordinals = tuple(
            sorted(
                {
                    *draft.kind_ordinals,
                    *draft.boundary_ordinals,
                    *draft.pending_ordinals,
                }
            )
        )
        row_box = _box_for_ordinals(page, all_row_ordinals)
        locator = SafeSourceLocator(
            document_ordinal=document_ordinal,
            page_number=page.page_number,
            table_ordinal=1,
            row_ordinal=row_ordinal,
            row_box=row_box,
        )
        rows.append(
            BalanceRowEvidence(
                locator=locator,
                date_token_ordinals=draft.date_ordinals,
                balance_token_ordinals=draft.amount_ordinals,
            )
        )
        controls.append(
            BalanceControlEvidence(
                locator=locator,
                kind=draft.kind,
                boundary=draft.boundary,
                includes_pending=draft.includes_pending,
                control_box=_box_for_ordinals(page, control_ordinals),
                kind_token_ordinals=draft.kind_ordinals,
                boundary_token_ordinals=draft.boundary_ordinals,
                includes_pending_token_ordinals=draft.pending_ordinals,
            )
        )
        try:
            amount = parse_nonnegative_money(draft.amount_text)
            observed_on = _resolve_balance_observed_on(
                draft,
                recognized,
                dates,
                document_ordinal,
            )
            observations.append(
                BalanceObservation(
                    account_role=document.account_role,
                    amount=amount,
                    observed_on=observed_on,
                    boundary=draft.boundary,
                    kind=draft.kind,
                    includes_pending=draft.includes_pending,
                    source_sha256=source_sha256,
                    locator=locator,
                    extraction_method=ExtractionMethod.NATIVE,
                    parse_evidence=(
                        ParseEvidence(EvidenceField.DATE, draft.date_ordinals),
                        ParseEvidence(EvidenceField.BALANCE, draft.amount_ordinals),
                        ParseEvidence(
                            EvidenceField.KIND,
                            draft.kind_ordinals,
                            draft.kind_ordinals,
                        ),
                        ParseEvidence(
                            EvidenceField.BOUNDARY,
                            draft.boundary_ordinals,
                            draft.boundary_ordinals,
                        ),
                        ParseEvidence(
                            EvidenceField.INCLUDES_PENDING,
                            draft.pending_ordinals,
                            draft.pending_ordinals,
                        ),
                    ),
                )
            )
        except TreasurerSlidesError:
            raise _page_error(document_ordinal, page.page_number) from None
    try:
        evidence = PageEvidence(
            page_number=page.page_number,
            page_kind=recognized.page_kind,
            fingerprint_version=recognized.fingerprint,
            extraction_method=ExtractionMethod.NATIVE,
            ignored=recognized.page_kind is PageKind.BOILERPLATE,
            activity_tables=(),
            balance_rows=tuple(rows),
            balance_controls=tuple(controls),
            tokens=model_tokens,
        )
    except TreasurerSlidesError:
        raise _page_error(document_ordinal, page.page_number) from None
    return evidence, tuple(observations)


def _activity_artifacts(
    recognized: _RecognizedPage,
    model_tokens: tuple[PositionedToken, ...],
    document_ordinal: int,
    document: DocumentSpec,
    source_sha256: str,
    dates: _DocumentDates,
    remaining_rows: int,
    max_transaction_rows: int,
) -> tuple[PageEvidence, tuple[NormalizedTransaction, ...]]:
    page = recognized.native
    if recognized.header is None:
        raise _page_error(document_ordinal, page.page_number)
    table, drafts, controls = _parse_activity_table(
        page,
        recognized.header,
        document_ordinal,
        remaining_rows,
        max_transaction_rows,
    )
    try:
        evidence = PageEvidence(
            page_number=page.page_number,
            page_kind=recognized.page_kind,
            fingerprint_version=recognized.fingerprint,
            extraction_method=ExtractionMethod.NATIVE,
            ignored=False,
            activity_tables=(table,),
            balance_rows=(),
            balance_controls=(),
            tokens=model_tokens,
        )
    except TreasurerSlidesError:
        raise _page_error(document_ordinal, page.page_number) from None
    transactions: list[NormalizedTransaction] = []
    for row_ordinal, draft in enumerate(drafts, start=1):
        status_control = controls[draft.status_index]
        locator = SafeSourceLocator(
            document_ordinal=document_ordinal,
            page_number=page.page_number,
            table_ordinal=table.table_ordinal,
            row_ordinal=row_ordinal,
            row_box=draft.row_box,
        )
        description = normalize_description(
            " ".join(page.token(ordinal).text for ordinal in draft.description_ordinals)
        )
        direction_column = (
            ActivityColumn.DEBIT if draft.direction is Direction.DEBIT else ActivityColumn.CREDIT
        )
        try:
            transactions.append(
                NormalizedTransaction(
                    account_role=document.account_role,
                    effective_date=_resolve_source_date(
                        draft.date_text, dates, document_ordinal, page.page_number
                    ),
                    status=status_control.status,
                    direction=draft.direction,
                    magnitude=parse_positive_money(draft.amount_text),
                    normalized_description=description,
                    occurrence_ordinal=1,
                    source_sha256=source_sha256,
                    locator=locator,
                    extraction_method=ExtractionMethod.NATIVE,
                    parser_version=NATIVE_PARSER_VERSION,
                    parse_evidence=(
                        ParseEvidence(EvidenceField.DATE, draft.date_ordinals),
                        ParseEvidence(EvidenceField.DESCRIPTION, draft.description_ordinals),
                        ParseEvidence(
                            EvidenceField.DIRECTION,
                            draft.money_ordinals,
                            table.band(direction_column).header_token_ordinals,
                        ),
                        ParseEvidence(
                            EvidenceField.STATUS,
                            draft.date_ordinals,
                            status_control.token_ordinals,
                        ),
                        ParseEvidence(EvidenceField.MAGNITUDE, draft.money_ordinals),
                    ),
                )
            )
        except TreasurerSlidesError:
            raise _page_error(document_ordinal, page.page_number) from None
    return evidence, tuple(transactions)


def _boilerplate_evidence(
    recognized: _RecognizedPage, model_tokens: tuple[PositionedToken, ...], document_ordinal: int
) -> PageEvidence:
    try:
        return PageEvidence(
            page_number=recognized.native.page_number,
            page_kind=PageKind.BOILERPLATE,
            fingerprint_version=PageFingerprint.WELLS_FARGO_V1_BOILERPLATE,
            extraction_method=ExtractionMethod.NATIVE,
            ignored=True,
            activity_tables=(),
            balance_rows=(),
            balance_controls=(),
            tokens=model_tokens,
        )
    except TreasurerSlidesError:
        raise _page_error(document_ordinal, recognized.native.page_number) from None


def _validate_document_shape(
    recognized_pages: tuple[_RecognizedPage, ...],
    document_ordinal: int,
    document: DocumentSpec,
    balances: tuple[BalanceObservation, ...],
) -> None:
    page_kinds = {page.page_kind for page in recognized_pages}
    balance_kinds = {balance.kind for balance in balances}
    if len(balance_kinds) != len(balances):
        raise _page_error(document_ordinal)
    if document.document_kind is DocumentKind.MONTHLY_STATEMENT:
        if (
            PageKind.MONTHLY_SUMMARY not in page_kinds
            or PageKind.MONTHLY_ACTIVITY not in page_kinds
            or balance_kinds != {BalanceKind.OPENING, BalanceKind.CLOSING}
        ):
            raise _page_error(document_ordinal)
    elif (
        PageKind.CURRENT_BALANCE not in page_kinds
        or BalanceKind.AVAILABLE not in balance_kinds
        or not balance_kinds <= {BalanceKind.AVAILABLE, BalanceKind.COLLECTED}
    ):
        raise _page_error(document_ordinal)


def _build_observation(
    recognized_pages: tuple[_RecognizedPage, ...],
    document_ordinal: int,
    document: DocumentSpec,
    source_sha256: str,
    dates: _DocumentDates,
    limits: _NativeExtractionLimits,
) -> StatementObservation:
    page_evidence: list[PageEvidence] = []
    transactions: list[NormalizedTransaction] = []
    balances: list[BalanceObservation] = []
    for recognized in recognized_pages:
        excluded_token_ordinals = (
            frozenset(recognized.native.provisional_contact_phone_token_ordinals)
            if recognized.page_kind is PageKind.BOILERPLATE
            else frozenset()
        )
        model_tokens = _model_tokens(
            recognized.native,
            excluded_token_ordinals=excluded_token_ordinals,
        )
        if recognized.page_kind is PageKind.BOILERPLATE:
            page_evidence.append(_boilerplate_evidence(recognized, model_tokens, document_ordinal))
        elif recognized.page_kind in {PageKind.MONTHLY_ACTIVITY, PageKind.CURRENT_ACTIVITY}:
            activity_evidence, parsed_transactions = _activity_artifacts(
                recognized,
                model_tokens,
                document_ordinal,
                document,
                source_sha256,
                dates,
                limits.max_transaction_rows - len(transactions),
                limits.max_transaction_rows,
            )
            page_evidence.append(activity_evidence)
            transactions.extend(parsed_transactions)
        else:
            balance_evidence, parsed_balances = _balance_artifacts(
                recognized,
                model_tokens,
                document_ordinal,
                document,
                source_sha256,
                dates,
            )
            page_evidence.append(balance_evidence)
            balances.extend(parsed_balances)
    if len(transactions) > limits.max_transaction_rows:
        raise _page_error(document_ordinal)
    canonical_transactions = assign_occurrence_ordinals(tuple(transactions))
    balance_tuple = tuple(balances)
    _validate_document_shape(recognized_pages, document_ordinal, document, balance_tuple)
    coverage_start = dates.coverage_start
    if coverage_start is None:
        fact_dates = [
            *(transaction.effective_date for transaction in canonical_transactions),
            *(balance.observed_on for balance in balance_tuple),
        ]
        if not fact_dates:
            raise _page_error(document_ordinal)
        coverage_start = min(fact_dates)
    try:
        return StatementObservation(
            document_ordinal=document_ordinal,
            document=document,
            source_sha256=source_sha256,
            parser_version=NATIVE_PARSER_VERSION,
            coverage_start=coverage_start,
            coverage_end=dates.coverage_end,
            capture_date=dates.capture_date,
            source_page_count=len(recognized_pages),
            page_evidence=tuple(page_evidence),
            transactions=canonical_transactions,
            balances=balance_tuple,
        )
    except TreasurerSlidesError:
        raise _page_error(document_ordinal) from None


class WellsFargoStatementExtractor:
    """The narrow, versioned Wells Fargo embedded-text adapter.

    The full parser is intentionally implemented in this module rather than loaded at
    package import time.  Keeping the call boundary here makes the optional dependency
    and the fail-closed document contract explicit for later OCR support.
    """

    parser_version = NATIVE_PARSER_VERSION

    def extract(
        self,
        source_path: Path,
        *,
        document_ordinal: int,
        document: DocumentSpec,
    ) -> StatementObservation:
        """Extract one supported native-text PDF.

        This native-only boundary intentionally does not fall back to OCR.  A later
        adapter may insert bounded positional OCR before page recognition; image-only,
        unknown, or contradictory inputs fail closed here.
        """

        if (
            not isinstance(document_ordinal, int)
            or isinstance(document_ordinal, bool)
            or document_ordinal < 1
            or not isinstance(document, DocumentSpec)
            or not isinstance(source_path, Path)
        ):
            raise _private_input_error()
        # Check only package metadata in the broker.  The parent never imports PDFium:
        # the ready-attested LPAC worker performs the native import after Windows has
        # applied its AppContainer, Job Object, environment, and handle restrictions.
        _require_pdfium_distribution()
        limits = _native_extraction_limits(document_ordinal)
        prepared_worker: (
            tuple[_NativeWorkerConnection, _NativeWorkerConnection, _NativeWorkerProcess] | None
        ) = _start_native_page_worker(document_ordinal, limits)
        try:
            payload = _read_bounded_pdf(source_path, maximum_bytes=limits.max_pdf_bytes)
            pages = _extract_native_pages_in_worker(
                payload,
                document_ordinal,
                limits,
                prepared_worker=prepared_worker,
            )
            prepared_worker = None
        finally:
            if prepared_worker is not None:
                request_sender, response_receiver, worker = prepared_worker
                _close_quietly(request_sender)
                _close_quietly(response_receiver)
                _stop_native_page_worker(worker, terminate_immediately=True)
        recognized = tuple(_recognize_page(page, document_ordinal, document) for page in pages)
        dates = _document_dates(recognized, document_ordinal, document)
        return _build_observation(
            recognized,
            document_ordinal,
            document,
            hashlib.sha256(payload).hexdigest(),
            dates,
            limits,
        )


def extract_wells_fargo_statement(
    source_path: Path,
    *,
    document_ordinal: int,
    document: DocumentSpec,
) -> StatementObservation:
    """Convenience entry point for the only v1 native-text statement adapter."""

    return WellsFargoStatementExtractor().extract(
        source_path,
        document_ordinal=document_ordinal,
        document=document,
    )


__all__ = [
    "NATIVE_PARSER_VERSION",
    "MAX_PDF_BYTES",
    "MAX_PDF_PAGES",
    "MAX_NATIVE_CHARACTERS",
    "MAX_RENDERED_PIXELS_PER_PAGE",
    "MAX_TRANSACTION_ROWS",
    "MIN_NATIVE_NONWHITESPACE_CHARACTERS",
    "MAX_NATIVE_TOKENS_PER_PAGE",
    "MAX_NATIVE_LINES_PER_PAGE",
    "MAX_NATIVE_PAGE_WIRE_BYTES",
    "MAX_NATIVE_EXTRACTION_SECONDS",
    "MAX_NATIVE_WORKER_MEMORY_BYTES",
    "MAX_NATIVE_WORKER_CPU_SECONDS",
    "StatementExtractionError",
    "SlidesDependencyError",
    "StatementExtractor",
    "WellsFargoStatementExtractor",
    "extract_wells_fargo_statement",
]
