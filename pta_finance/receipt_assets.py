"""Allowlisted HTTPS fetcher for receipt assets, with a content-addressed cache.

Transport only: this module turns a ticket's upload URLs into cached bytes and records what
happened to each one. It never decodes, renders or links anything (that is
:mod:`pta_finance.receipt_pages` and the linker), and it never imports either of them.

**The allowlist is the whole safety property of this lane**, so every URL handed to the network
— the requested one and every redirect hop — passes the same full check first:

* the scheme is ``https`` and the port is the default (an explicit ``:443`` is the default);
* the URL carries no userinfo (``https://allowed@elsewhere/`` is refused, never "repaired");
* the host is a name, never an IP literal — dotted, bracketed, integer, hex or shortened forms
  (``127.1``) are all refused rather than resolved;
* the host matches an operator rule: an exact host name, or a leading-dot suffix
  (``.uploads.example.net``) that matches only at a label boundary, so it covers
  ``a.uploads.example.net`` but never ``evil-uploads.example.net`` nor the bare apex.

Automatic redirect following is disabled in the opener itself: a 30x response comes back to
this module, which re-checks the target and follows at most ``max_redirects`` hops. The
production opener has exactly one handler, HTTPS with certificate and hostname verification;
it carries no proxy, redirect, cookie or error handler, so an ``http://`` URL cannot be opened
by construction even if a check above were wrong.

**Bytes decide the type.** A body is accepted only when it starts with one of three magic
signatures — ``%PDF-``, the PNG signature or the JPEG SOI marker — and ``Content-Type`` is never
read. The body is read with a hard cap: at most ``max_bytes + 1`` bytes are ever requested, so
an oversize body is detected rather than silently truncated. ``max_bytes`` is the configured
``max_asset_mib``, bounded above by :data:`pta_finance.receipt_geometry.NORMALIZATION`'s
``max_source_bytes`` (the cap every later reader of a cached asset applies), and deliberately
not the viewer's page caps, which bound rendered pages rather than source files.

**Content-addressed cache.** An accepted body is stored as ``<sha256 hex>.<pdf|png|jpg>`` in
``cache_dir`` — named from its bytes, never from a URL or an upload filename — and identified as
``asset:v1:<sha256 hex>``. The private fetch ledger ``<cache_dir>/fill-ledger.json`` maps each
requested URL to its digest; before any socket opens, a URL whose ledger row names an existing
cache file is a ``cached`` hit. ``offline=True`` never opens a socket at all: a miss is
``uncached``.

**Per-asset isolation.** Every per-asset outcome — refused, unreachable, oversize, gone — is
recorded and returned, never raised, so one bad asset never aborts the batch.
:class:`ReceiptAssetError` is raised only when the stage cannot run at all: invalid arguments
or allowlist rules, an unwritable cache directory, or a malformed ledger.

**Politeness.** At most ``max_parallel`` fetches run at once. A 429 or 5xx response is retried
after ``2**attempt`` seconds (capped at 30) until ``max_attempts`` attempts have been made;
every other status is final. Upload URLs on a third-party CDN may expire: a 404 or 410 is the
``gone`` outcome, a link-rot finding about the evidence, not a toolkit bug.

Messages and ``detail`` strings carry paths, status codes and reason classes only — never a URL,
host name, vendor or requestor. The URLs themselves live only in the private ledger.
"""

from __future__ import annotations

import functools
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import ssl
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol
from urllib.parse import urljoin, urlsplit

from pta_finance.receipt_geometry import NORMALIZATION

__all__ = [
    "ACCEPTED_OUTCOMES",
    "ASSET_ID_PREFIX",
    "ASSET_OUTCOMES",
    "ASSET_ROW_KEYS",
    "CACHE_SUFFIXES",
    "FETCH_CEILINGS",
    "LEDGER_KEYS",
    "LEDGER_NAME",
    "LEDGER_SCHEMA_VERSION",
    "AssetResult",
    "FetchCeilings",
    "ReceiptAssetError",
    "canonical_key",
    "fetch_assets",
    "normalize_allowlist",
]

ASSET_ID_PREFIX: Final = "asset:v1:"
LEDGER_NAME: Final = "fill-ledger.json"
LEDGER_SCHEMA_VERSION: Final = 1
# The ledger's exact top-level key set. This module owns assets[]; the linker owns
# page_outcomes[] and unjoinable_tickets[]. Each writer preserves the lists it does not own.
LEDGER_KEYS: Final = frozenset(
    {"schema_version", "updated_at", "assets", "page_outcomes", "unjoinable_tickets"}
)
# The exact key set of one assets[] row, in the order rows are written.
ASSET_ROW_KEYS: Final = (
    "url",
    "canonical_key",
    "asset_id",
    "media_type",
    "outcome",
    "byte_count",
    "attempts",
    "first_seen",
    "last_seen",
    "detail",
)
# The closed AssetResult.outcome vocabulary, which is also exactly assets[].outcome.
ASSET_OUTCOMES: Final = (
    "fetched",
    "cached",
    "uncached",
    "refused",
    "unreachable",
    "oversize",
    "gone",
)
# The outcomes under which bytes were accepted and an asset id exists.
ACCEPTED_OUTCOMES: Final = frozenset({"fetched", "cached"})
# Media type (decided by magic bytes) -> cache file extension.
CACHE_SUFFIXES: Final[Mapping[str, str]] = MappingProxyType(
    {"pdf": ".pdf", "png": ".png", "jpeg": ".jpg"}
)


@dataclass(frozen=True)
class FetchCeilings:
    """Hard bounds the fetcher enforces on its own arguments, whatever a caller passes.

    ``timeout_s`` — the per-operation socket timeout; ``max_redirects`` — redirect hops
    followed per attempt; ``max_parallel`` — concurrent fetches; ``max_attempts`` — attempts per
    URL, retries included; ``backoff_cap_s`` — the longest wait between attempts. Grouped in a
    frozen instance so a config parser that imports it can be held to it with ``is``: CPython
    shares small integers between modules, so an identity check on a bare ``5`` would still
    pass after the number had been restated.
    """

    timeout_s: int
    max_redirects: int
    max_parallel: int
    max_attempts: int
    backoff_cap_s: int


FETCH_CEILINGS: Final[FetchCeilings] = FetchCeilings(
    timeout_s=120,
    max_redirects=5,
    max_parallel=8,
    max_attempts=5,
    backoff_cap_s=30,
)

# Magic signatures, the only bytes that decide an asset's type.
_SIGNATURES: Final = (
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
)
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
_GONE_STATUSES: Final = frozenset({404, 410})
_RETRYABLE: Final = "retryable"  # internal marker; never returned or recorded
_READ_CHUNK: Final = 1 << 16
_USER_AGENT: Final = "pta-finance-receipt-fetch/1"
# RFC 3986 characters only: no whitespace, controls, quotes, angle brackets or backslashes,
# which parsers disagree about and which urllib would otherwise strip or unwrap.
_URL_CHARS: Final = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_LABEL: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
# A final label that is all digits or hex is an address in some inet_aton form, never a name:
# no top-level domain is numeric.
_NUMERIC_LABEL: Final = re.compile(r"[0-9]+|0x[0-9a-f]*")
_ASSET_ID_RE: Final = re.compile(re.escape(ASSET_ID_PREFIX) + r"([0-9a-f]{64})")
_TIMESTAMP_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_TRANSPORT_ERRORS: Final = (OSError, http.client.HTTPException)


class ReceiptAssetError(ValueError):
    """The fetch stage cannot run at all, so it aborts; never raised for one asset's outcome.

    Raised for invalid arguments or allowlist rules, an unwritable cache directory and a
    malformed fetch ledger. Messages carry paths and remediation only, never a URL or host.
    """


@dataclass(frozen=True)
class AssetResult:
    """What happened to one requested URL.

    ``url`` is the requested URL (never printed, never in the sidecar); ``canonical_key`` is
    :func:`canonical_key` of it. ``asset_id`` (``asset:v1:<sha256 hex of the bytes>``),
    ``media_type`` (``pdf``/``png``/``jpeg``, from magic bytes) and ``path`` (the cache file)
    are ``None`` and ``byte_count`` is 0 unless bytes were accepted — that is, for every
    ``outcome`` except ``fetched`` and ``cached``. ``detail`` is a short reason class or
    status, never a URL or vendor string.
    """

    url: str
    canonical_key: str
    asset_id: str | None
    media_type: str | None
    path: Path | None
    byte_count: int
    outcome: str
    detail: str


class _Response(Protocol):
    """The part of an HTTP response the fetcher reads: status, one header, the body."""

    @property
    def status(self) -> int: ...

    def getheader(self, name: str) -> str | None: ...

    def read(self, amt: int, /) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class _Policy:
    allowlist: tuple[str, ...]
    max_bytes: int
    timeout: float
    max_redirects: int
    max_attempts: int


@dataclass(frozen=True)
class _Fetch:
    """One URL's network result; ``body`` and ``media_type`` are set only when fetched."""

    outcome: str
    detail: str
    attempts: int
    body: bytes | None = None
    media_type: str | None = None


# --- Seams: the only places that touch the network or the clock --------------------------


@functools.cache
def _tls_context() -> ssl.SSLContext:
    """Certificate and hostname verification, the platform trust store, TLS 1.2 or later."""

    return ssl.create_default_context()


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener that speaks HTTPS only and follows nothing.

    Built from a bare :class:`~urllib.request.OpenerDirector` rather than ``build_opener``, so
    it holds exactly one handler: no HTTP, FTP, file or data handler, no proxy handler (proxy
    environment variables are ignored), no redirect handler and no error processor — every
    status, a 30x included, is returned to the caller unchanged.
    """

    opener = urllib.request.OpenerDirector()
    opener.addheaders = [("User-Agent", _USER_AGENT)]
    opener.add_handler(urllib.request.HTTPSHandler(context=_tls_context()))
    return opener


def _open(url: str, *, timeout: float) -> _Response:
    """THE network seam: one HTTPS GET, redirects not followed, any status returned.

    Tests substitute this function; they never allowlist ``http://`` or a loopback host.
    ``timeout`` bounds each socket operation.
    """

    response: _Response | None = _build_opener().open(
        urllib.request.Request(url, method="GET"), timeout=timeout
    )
    if response is None:
        raise urllib.error.URLError("only https URLs can be opened")
    return response


_sleep: Callable[[float], None] = time.sleep


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Identity and allowlist ----------------------------------------------------------------


def canonical_key(url: str) -> str:
    """THE one canonicalization of an upload URL: ``<lowercased host><path>``.

    The query string and fragment are discarded, so two size variants of one upload share a
    key; the path keeps its case. Total by design: the linker numbers asset ordinals with it
    before anything is fetched, so a URL that cannot be split keeps its text up to the first
    ``?`` or ``#`` (such a URL is refused at fetch time anyway).
    """

    try:
        parts = urlsplit(url)
    except ValueError:
        return url.split("#", 1)[0].split("?", 1)[0]
    return (parts.hostname or "") + parts.path


def _is_hostname(host: str) -> bool:
    if not host or len(host) > 253:
        return False
    return all(_LABEL.fullmatch(label) for label in host.split("."))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return bool(_NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]))
    return True


def normalize_allowlist(rules: Iterable[str]) -> tuple[str, ...]:
    """Validate and lowercase operator host rules; THE allowlist grammar.

    A rule is an exact host name (``cdn.example.net``) or a leading-dot suffix
    (``.uploads.example.net``) that matches proper subdomains at a label boundary only. Either
    form needs at least two labels; IP addresses, wildcards, ports, trailing dots and single
    labels are refused. An empty allowlist is valid and refuses every URL.
    """

    if isinstance(rules, str):
        raise ReceiptAssetError(
            "receipt asset allowed_hosts must be a list of host rules, not a single string"
        )
    normalized: list[str] = []
    for position, rule in enumerate(rules, start=1):
        host = rule.lower() if isinstance(rule, str) and rule.isascii() else ""
        host = host.removeprefix(".")
        if _is_ip_literal(host) or not _is_hostname(host) or "." not in host:
            raise ReceiptAssetError(
                f"receipt asset allowed_hosts entry {position} must be an exact host name or a "
                "leading-dot suffix with at least two labels; IP addresses and wildcards are "
                "not accepted"
            )
        normalized.append(rule.lower())
    return tuple(dict.fromkeys(normalized))


def _host_allowed(host: str, allowlist: Sequence[str]) -> bool:
    for rule in allowlist:
        # A suffix rule starts with ".", so endswith() can only match at a label boundary.
        if host == rule or (rule.startswith(".") and host.endswith(rule)):
            return True
    return False


def _url_refusal(url: str, allowlist: Sequence[str]) -> str | None:
    """Why this URL must not be opened, or ``None``; run on every hop."""

    if not _URL_CHARS.fullmatch(url):
        return "malformed url"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "malformed url"
    if parts.scheme != "https":
        return "not https"
    if "@" in parts.netloc:
        return "userinfo in url"
    if port is not None and port != 443:
        return "non-default port"
    host = parts.hostname
    if not host:
        return "missing host"
    if _is_ip_literal(host):
        return "ip-literal host"
    if not _is_hostname(host):
        return "malformed host"
    if not _host_allowed(host, allowlist):
        return "host not allowlisted"
    return None


# --- Transport -----------------------------------------------------------------------------


def _backoff(attempt: int) -> int:
    """Seconds to wait after failed attempt number ``attempt`` (1-based)."""

    return int(min(2**attempt, FETCH_CEILINGS.backoff_cap_s))


def _sniff(body: bytes) -> str | None:
    for signature, media_type in _SIGNATURES:
        if body.startswith(signature):
            return media_type
    return None


def _transport_detail(exc: BaseException) -> str:
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, TimeoutError):
        return "timeout"
    if isinstance(reason, ssl.SSLError):  # certificate verification failures included
        return "tls error"
    return "connection error"


def _read_capped(response: _Response, cap: int) -> bytes:
    """The ``read(cap + 1)`` rule: take at most ``cap + 1`` body bytes, so oversize shows."""

    limit = cap + 1
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        want = min(_READ_CHUNK, limit - total)
        chunk = response.read(want)[:want]
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _close(response: _Response) -> None:
    try:
        response.close()
    except _TRANSPORT_ERRORS:
        pass


def _redirect_target(current: str, location: str) -> str | None:
    try:
        return urljoin(current, location)
    except ValueError:
        return None


def _attempt(url: str, policy: _Policy) -> _Fetch:
    """One attempt: the redirect chain from ``url``, every hop re-checked before it opens."""

    current: str | None = url
    for hop in range(policy.max_redirects + 1):
        if current is None:
            return _Fetch("refused", "redirect target: malformed url", 0)
        refusal = _url_refusal(current, policy.allowlist)
        if refusal is not None:
            return _Fetch("refused", refusal if hop == 0 else f"redirect target: {refusal}", 0)
        try:
            response = _open(current, timeout=policy.timeout)
        except _TRANSPORT_ERRORS as exc:
            return _Fetch("unreachable", _transport_detail(exc), 0)
        try:
            status = response.status
            if status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                if not location:
                    return _Fetch("unreachable", f"status {status} without location", 0)
                current = _redirect_target(current, location)
                continue
            if status == 200:
                body = _read_capped(response, policy.max_bytes)
                if len(body) > policy.max_bytes:
                    return _Fetch("oversize", "body over the size cap", 0)
                media_type = _sniff(body)
                if media_type is None:
                    return _Fetch("refused", "unrecognized content", 0)
                return _Fetch("fetched", "", 0, body=body, media_type=media_type)
            if status in _GONE_STATUSES:
                return _Fetch("gone", f"status {status}", 0)
            if status == 429 or 500 <= status <= 599:
                return _Fetch(_RETRYABLE, f"status {status}", 0)
            return _Fetch("unreachable", f"status {status}", 0)
        except _TRANSPORT_ERRORS as exc:
            return _Fetch("unreachable", _transport_detail(exc), 0)
        finally:
            _close(response)
    return _Fetch("refused", "too many redirects", 0)


def _fetch_one(url: str, policy: _Policy) -> _Fetch:
    """Attempts for one URL, retrying only 429/5xx, with backoff, up to the attempt ceiling."""

    attempt = 0
    while True:
        attempt += 1
        result = _attempt(url, policy)
        if result.outcome != _RETRYABLE:
            return replace(result, attempts=attempt)
        if attempt >= policy.max_attempts:
            return _Fetch("unreachable", result.detail, attempt)
        _sleep(_backoff(attempt))


# --- Cache -----------------------------------------------------------------------------------


def _cache_error(root: Path) -> ReceiptAssetError:
    return ReceiptAssetError(
        f"receipt asset cache {root} is not writable; check [receipt_assets] cache_dir and "
        "its permissions, then re-run"
    )


def _holds(path: Path, digest: str) -> bool:
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest() == digest
    except OSError:
        return False


def _publish(root: Path, body: bytes, media_type: str) -> tuple[str, Path]:
    """Store accepted bytes under their digest; an intact existing copy is left alone."""

    digest = hashlib.sha256(body).hexdigest()
    target = root / f"{digest}{CACHE_SUFFIXES[media_type]}"
    if _holds(target, digest):
        return ASSET_ID_PREFIX + digest, target
    temp: str | None = None
    try:
        fd, temp = tempfile.mkstemp(prefix=".fetch-", suffix=".tmp", dir=root)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        # Replacing is safe here: the name is the digest, so the only file it can replace is
        # a damaged copy of these same bytes.
        os.replace(temp, target)
        temp = None
    except OSError:
        raise _cache_error(root) from None
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass
    return ASSET_ID_PREFIX + digest, target


def _cache_hit(url: str, key: str, ledger: Mapping[str, Any], root: Path) -> AssetResult | None:
    """The ledger row for exactly this URL with accepted bytes, when its cache file exists."""

    best: tuple[tuple[str, int], Mapping[str, Any]] | None = None
    for index, row in enumerate(ledger["assets"]):
        if row["url"] == url and row["outcome"] in ACCEPTED_OUTCOMES:
            rank = (row["last_seen"], index)
            if best is None or rank > best[0]:
                best = (rank, row)
    if best is None:
        return None
    row = best[1]
    asset_id: str = row["asset_id"]
    media_type: str = row["media_type"]
    path = root / (asset_id.removeprefix(ASSET_ID_PREFIX) + CACHE_SUFFIXES[media_type])
    try:
        size = path.stat().st_size if path.is_file() else 0
    except OSError:
        return None
    if size < 1:
        # No accepted body is empty, so an empty file is a damaged copy: online it is
        # re-fetched and repaired, offline it is a miss.
        return None
    return AssetResult(url, key, asset_id, media_type, path, size, "cached", "")


# --- Ledger ------------------------------------------------------------------------------------


def _ledger_error(path: Path) -> ReceiptAssetError:
    return ReceiptAssetError(
        f"receipt fetch ledger {path} is malformed; repair or delete it (cached files stay "
        "valid and are re-indexed by the next online fetch), then re-run"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _refuse_constant(name: str) -> Any:
    raise ValueError("non-finite number")


def _is_count(value: object, low: int = 0) -> bool:
    return type(value) is int and value >= low


def _valid_asset_row(row: object) -> bool:
    if not isinstance(row, dict) or set(row) != set(ASSET_ROW_KEYS):
        return False
    texts = ("url", "canonical_key", "outcome", "first_seen", "last_seen", "detail")
    if not all(isinstance(row[name], str) for name in texts):
        return False
    if row["outcome"] not in ASSET_OUTCOMES or row["canonical_key"] != canonical_key(row["url"]):
        return False
    if not all(_TIMESTAMP_RE.fullmatch(row[name]) for name in ("first_seen", "last_seen")):
        return False
    if not _is_count(row["attempts"]):
        return False
    if row["outcome"] in ACCEPTED_OUTCOMES:
        return (
            isinstance(row["asset_id"], str)
            and _ASSET_ID_RE.fullmatch(row["asset_id"]) is not None
            and row["media_type"] in CACHE_SUFFIXES
            and _is_count(row["byte_count"], 1)
        )
    return (
        row["asset_id"] is None
        and row["media_type"] is None
        and _is_count(row["byte_count"])
        and row["byte_count"] == 0
    )


def _empty_ledger() -> dict[str, Any]:
    # updated_at is stamped when the ledger is written, never before.
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "updated_at": "",
        "assets": [],
        "page_outcomes": [],
        "unjoinable_tickets": [],
    }


def _load_ledger(path: Path) -> dict[str, Any]:
    """Read and strictly validate the ledger; an absent file is an empty ledger."""

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _empty_ledger()
    except OSError:
        raise ReceiptAssetError(
            f"receipt fetch ledger {path} cannot be read; check its permissions, then re-run"
        ) from None
    try:
        doc = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_refuse_constant,
        )
    except ValueError:  # includes UnicodeDecodeError and JSONDecodeError
        raise _ledger_error(path) from None
    if not isinstance(doc, dict) or set(doc) != LEDGER_KEYS:
        raise _ledger_error(path)
    version = doc["schema_version"]
    if type(version) is not int or version != LEDGER_SCHEMA_VERSION:
        raise _ledger_error(path)
    if not isinstance(doc["updated_at"], str) or not _TIMESTAMP_RE.fullmatch(doc["updated_at"]):
        raise _ledger_error(path)
    for name in ("assets", "page_outcomes", "unjoinable_tickets"):
        rows = doc[name]
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise _ledger_error(path)
    if not all(_valid_asset_row(row) for row in doc["assets"]):
        raise _ledger_error(path)
    keys = [(row["canonical_key"], row["asset_id"]) for row in doc["assets"]]
    if len(set(keys)) != len(keys):
        raise _ledger_error(path)
    return doc


def _write_ledger(path: Path, doc: Mapping[str, Any]) -> None:
    text = json.dumps(doc, indent=1, ensure_ascii=True) + "\n"
    temp: str | None = None
    try:
        fd, temp = tempfile.mkstemp(prefix=".fill-ledger.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        temp = None
    except OSError:
        raise ReceiptAssetError(
            f"receipt fetch ledger {path} cannot be written; check that its directory is "
            "writable and that no other program holds the file open, then re-run"
        ) from None
    finally:
        if temp is not None:
            try:
                os.unlink(temp)
            except OSError:
                pass


def _record(rows: list[dict[str, Any]], result: AssetResult, attempts: int, now: str) -> None:
    """Update this result's row in place by ``(canonical_key, asset_id)``, or append it."""

    for row in rows:
        if row["canonical_key"] == result.canonical_key and row["asset_id"] == result.asset_id:
            break
    else:
        row = {name: None for name in ASSET_ROW_KEYS}
        row.update(canonical_key=result.canonical_key, attempts=0, first_seen=now)
        rows.append(row)
    row.update(
        url=result.url,
        asset_id=result.asset_id,
        media_type=result.media_type,
        outcome=result.outcome,
        byte_count=result.byte_count,
        attempts=row["attempts"] + attempts,
        last_seen=now,
        detail=result.detail,
    )


# --- Entry point -----------------------------------------------------------------------------


def _bounded_int(name: str, value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ReceiptAssetError(f"receipt asset {name} must be an integer from {low} to {high}")
    return value


def _policy(
    allowlist: Iterable[str],
    *,
    max_bytes: int,
    timeout: float,
    max_redirects: int,
    max_attempts: int,
) -> _Policy:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int | float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= FETCH_CEILINGS.timeout_s
    ):
        raise ReceiptAssetError(
            f"receipt asset timeout must be a number of seconds above 0 and at most "
            f"{FETCH_CEILINGS.timeout_s}"
        )
    return _Policy(
        allowlist=normalize_allowlist(allowlist),
        max_bytes=_bounded_int("max_bytes", max_bytes, 1, NORMALIZATION.max_source_bytes),
        timeout=float(timeout),
        max_redirects=_bounded_int("max_redirects", max_redirects, 0, FETCH_CEILINGS.max_redirects),
        max_attempts=_bounded_int("max_attempts", max_attempts, 1, FETCH_CEILINGS.max_attempts),
    )


def _failure(url: str, key: str, fetch: _Fetch) -> AssetResult:
    return AssetResult(url, key, None, None, None, 0, fetch.outcome, fetch.detail)


def fetch_assets(
    urls: Iterable[str],
    *,
    allowlist: Iterable[str],
    cache_dir: Path,
    max_bytes: int,
    timeout: float,
    max_redirects: int,
    max_parallel: int,
    max_attempts: int,
    offline: bool = False,
) -> list[AssetResult]:
    """Resolve each URL to cached bytes, a network fetch or a recorded failure.

    Returns one :class:`AssetResult` per input URL, in input order; a URL listed twice is
    resolved once. Each URL is checked against the allowlist first, in both modes. Then the
    ledger is consulted before any socket opens: the row whose ``url`` is exactly this URL with
    an accepted outcome (the latest ``last_seen`` when several match) names an asset id, and an
    existing cache file for it is a ``cached`` hit. On a miss, ``offline=True`` records
    ``uncached`` and opens no socket; otherwise the URL is fetched, at most ``max_parallel`` at
    a time. ``max_bytes`` caps a body (at most ``NORMALIZATION.max_source_bytes``); ``timeout``
    bounds each socket operation. Every outcome is written to the ledger's ``assets[]``
    before this returns; the other ledger lists are preserved as they are.

    Raises :class:`ReceiptAssetError` only when the stage cannot run: invalid arguments or
    allowlist rules, an unwritable cache directory or ledger, or a malformed ledger.
    """

    policy = _policy(
        allowlist,
        max_bytes=max_bytes,
        timeout=timeout,
        max_redirects=max_redirects,
        max_attempts=max_attempts,
    )
    parallel = _bounded_int("max_parallel", max_parallel, 1, FETCH_CEILINGS.max_parallel)
    if type(offline) is not bool:
        raise ReceiptAssetError("receipt asset offline must be true or false")
    requested = list(urls)
    if not requested:
        return []
    unique = list(dict.fromkeys(requested))
    root = Path(cache_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise _cache_error(root) from None
    ledger_path = root / LEDGER_NAME
    ledger = _load_ledger(ledger_path)

    resolved: dict[str, AssetResult] = {}
    attempts: dict[str, int] = {}
    pending: list[str] = []
    for url in unique:
        key = canonical_key(url)
        refusal = _url_refusal(url, policy.allowlist)
        hit = None if refusal is not None else _cache_hit(url, key, ledger, root)
        if refusal is not None:
            resolved[url] = _failure(url, key, _Fetch("refused", refusal, 0))
        elif hit is not None:
            resolved[url] = hit
        elif offline:
            resolved[url] = _failure(url, key, _Fetch("uncached", "not in cache", 0))
        else:
            pending.append(url)

    if pending:
        executor = ThreadPoolExecutor(
            max_workers=min(parallel, len(pending)), thread_name_prefix="receipt-fetch"
        )
        try:
            futures = {executor.submit(_fetch_one, url, policy): url for url in pending}
            for future in as_completed(futures):
                url = futures[future]
                fetch = future.result()
                attempts[url] = fetch.attempts
                if fetch.body is None or fetch.media_type is None:
                    resolved[url] = _failure(url, canonical_key(url), fetch)
                    continue
                asset_id, path = _publish(root, fetch.body, fetch.media_type)
                resolved[url] = AssetResult(
                    url,
                    canonical_key(url),
                    asset_id,
                    fetch.media_type,
                    path,
                    len(fetch.body),
                    "fetched",
                    "",
                )
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

    now = _utc_now()
    for url in unique:
        _record(ledger["assets"], resolved[url], attempts.get(url, 0), now)
    ledger["updated_at"] = now
    _write_ledger(ledger_path, ledger)
    return [resolved[url] for url in requested]
