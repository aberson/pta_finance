"""Fetched receipt assets become deterministic display pages.

This is the image half. PDF rendering crosses the isolated worker boundary and is not part of
this module yet, so a PDF asset is refused by name rather than parsed in this process.

**Untrusted image bytes are never decoded here.** Every PNG/JPEG asset is decoded by a fresh,
short-lived child process, :mod:`pta_finance.receipt_decode`, under operating-system memory and
CPU limits (a Windows Job Object, or Linux ``RLIMIT_AS``/``RLIMIT_CPU``) and a wall clock this
broker enforces. The child does everything that touches the upload — open, the header fast
paths, EXIF/XMP orientation, alpha-flatten, LANCZOS downscale — and returns only bounded raw
pixels plus a fixed-key header. This broker re-derives and validates everything the child sends
and then encodes the page with :func:`pixels_to_page`, the one pixels-to-page step every
producer shares, so the PDF path (a later step) is downscaled and encoded exactly like an
upload. A compromised child therefore cannot add a metadata segment to a page: the page is
re-encoded from pixels alone, so no source comment, EXIF, XMP, ICC profile or text chunk can
reach it.

The fixed normalization order, with every value read from
:data:`pta_finance.receipt_geometry.NORMALIZATION`: (1) orientation from a fixed transpose
table, (2) alpha-flatten onto white, before scaling, so palette and bilevel sources are never
resampled nearest-neighbour, (3) a uniform LANCZOS downscale only above the long-edge limit,
(4) a baseline JPEG re-encode. Steps 1-3 run in the child and step 4 here. Output is
byte-identical for the same asset under one Pillow/libjpeg build, which is why the child must
report the broker's own Pillow version before it is sent a byte.

**The decode budget is the safety bound**, uniformly, whatever the format or decoder path: the
worst per-asset cost is the configured memory and wall time, and any breach is a per-asset
refusal. The header checks — pixel and edge ceilings, the mode allowlist, the animated-PNG
refusal — are cheap fast paths and policy only.

Geometry invariant: a box is ``[left, top, width, height]`` as fractions of the *displayed*
page. Uniform scaling preserves those fractions, while rotation destroys them. Orientation is
therefore fixed here, before any human marks a box, and nothing afterwards may rotate a page.

Progress is reported **per page**, as each page file is published, never once at the end. A
page is published with a hard link, which never replaces a file: a different file already
holding the page id — or a name differing from it only in letter case — is a
:class:`ReceiptPageConflictError`, because a sidecar may already reference its digest. A Pillow
or libjpeg upgrade can change the bytes a source re-normalizes to, so callers must not
re-normalize an asset whose pages a sidecar already references.

Errors follow the plan's raise-vs-record boundary (§ 5A). :class:`ReceiptPageError` is
per-asset and carries ``reason``, its fill-ledger value, and ``usage``, the decode child's
measured :class:`DecodeUsage` or ``None``: the caller records it against that asset and carries
on. :class:`ReceiptPagesDirectoryError` means no page can be written at all, and
:class:`ReceiptDecodeUnavailableError` means the decode child cannot be started or limited (an
unsupported host, a missing or outdated Pillow, a failed spawn, unconfirmed limits, or a late,
malformed or mismatched ready line); both abort the stage. The abort/refusal boundary is the
accepted ready line. Pillow is imported lazily, so importing this module never requires it.
Messages carry file names and remediation only — never a URL, vendor, requestor or upload
filename — and causes are suppressed (``from None``) so no decoder internals of an untrusted
upload reach a traceback.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pta_finance import process_limits
from pta_finance.receipt_assets import ASSET_ID
from pta_finance.receipt_decode import (
    DECODED_KEYS,
    EXIT_BUDGET,
    EXIT_CHILD_ERROR,
    PROTOCOL,
    READY_KEYS,
    READY_LINE_MAX_BYTES,
    REFUSED_KEYS,
    RESPONSE_HEADER_MAX_BYTES,
    DecodeRequest,
    WireError,
    parse_line,
    parse_request,
    scaled_size,
    wire_float,
    wire_int,
    wire_text,
)
from pta_finance.receipt_decode import (
    REFUSAL_REASONS as CHILD_REFUSAL_REASONS,
)
from pta_finance.receipt_geometry import NORMALIZATION
from pta_finance.receipt_viewer import MAX_PAGE_BYTES

__all__ = [
    "PAGE_SUFFIX",
    "REFUSAL_REASONS",
    "DecodeUsage",
    "EncodedPage",
    "PageImage",
    "PageProgress",
    "PageSource",
    "ReceiptDecodeUnavailableError",
    "ReceiptPageConflictError",
    "ReceiptPageError",
    "ReceiptPagesDirectoryError",
    "cleanup_warning_count",
    "pixels_to_page",
    "to_pages",
]

PAGE_SUFFIX = ".jpg"

# Every per-asset refusal, by its fill-ledger page_outcomes[] value (plan § 5A). The PDF values
# belong to the render path a later step adds; the image path raises the others.
REFUSAL_REASONS = frozenset(
    {
        "too-large",
        "unreadable",
        "too-many-pixels",
        "source-edge",
        "unsupported-mode",
        "animated",
        "budget",
        "child-error",
        "failed-validation",
        "digest-mismatch",
        "pdf-not-rendered",
        "pdf-too-many-pages",
        "pdf-page-too-large",
        "pdf-render-failed",
        "pdf-render-invalid",
        "bad-ticket-ref",
        "conflict",
    }
)
# Hosts whose limits the child can both apply and confirm. macOS accepts and reports RLIMIT_AS
# without enforcing it, so a read-back there would attest a limit that does not exist.
SUPPORTED_DECODE_PLATFORMS = frozenset({"win32", "linux"})

# The oldest supported Pillow, as (major, minor): the floor of the 'receipts' extra in
# pyproject.toml, which a test compares with this value.
_PILLOW_FLOOR = (12, 2)
_RECEIPTS_EXTRA = (
    "receipt page normalization requires the optional 'receipts' extra (uv sync --extra receipts)"
)
_DECODE_MODULE = "pta_finance.receipt_decode"
_WINDOWS_CREATE_NO_WINDOW = 0x08000000
_PIPE_CHUNK = 1 << 16
_TICKET_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_CHANNELS = {"L": 1, "RGB": 3}
_MESSAGES = {
    "too-large": "receipt asset is larger than the source size cap; export a smaller copy",
    "unreadable": "receipt asset is not a readable image of its type",
    "too-many-pixels": "receipt image has too many pixels to normalize safely",
    "source-edge": "receipt image has a side too long to normalize safely",
    "unsupported-mode": (
        "receipt image uses an unsupported pixel format; export it as an 8-bit PNG or JPEG"
    ),
    "animated": "animated PNG receipts are not accepted; export a still image",
    "budget": "receipt image exceeded the decode budget; export a smaller or simpler copy",
    "child-error": "receipt decode child failed; this is a toolkit fault, not the asset",
    "failed-validation": "receipt decode child returned an invalid page; nothing was kept",
}
_UNWRITABLE = "receipt pages directory is not writable"

_CLEANUP_WARNINGS = 0
_ONE_CHILD_AT_A_TIME = threading.Lock()
# Job handles that could not be closed. No process is in them; they stay referenced until the
# process (and so the stage) ends rather than being silently abandoned.
_RETAINED_JOB_HANDLES: list[int] = []


class ReceiptPageError(ValueError):
    """Per-asset refusal: this asset yields no trustworthy page; record it and continue.

    ``reason`` is the fill-ledger ``page_outcomes[]`` value (one of :data:`REFUSAL_REASONS`),
    so the ledger writer never parses a message. ``usage`` is the decode child's measured
    :class:`DecodeUsage`, or ``None`` when no child ran.
    """

    def __init__(self, message: str, *, reason: str, usage: DecodeUsage | None = None) -> None:
        if reason not in REFUSAL_REASONS:
            raise ValueError(f"unknown receipt page refusal reason {reason!r}")
        super().__init__(message)
        self.reason = reason
        self.usage = usage


class ReceiptPageConflictError(ReceiptPageError):
    """Per-asset refusal: a different page file already holds this page id."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="conflict")


class ReceiptPagesDirectoryError(OSError):
    """No page can be written to the pages directory; the stage aborts.

    Deliberately not a :class:`ReceiptPageError`, so a caller that records per-asset refusals
    cannot swallow it.
    """


class ReceiptDecodeUnavailableError(RuntimeError):
    """The image decode child cannot be started or limited; the stage aborts.

    Deliberately neither a :class:`ReceiptPageError` nor an ``OSError`` or ``ValueError``, so
    no record-and-continue, transport or validation handler can swallow it.
    """


class PageSource(Protocol):
    """The fields of a fetched receipt asset this module reads.

    ``receipt_assets.AssetResult`` satisfies it structurally; ``path`` is ``None`` when the
    fetch accepted no bytes.
    """

    @property
    def asset_id(self) -> str | None: ...

    @property
    def media_type(self) -> str | None: ...

    @property
    def path(self) -> Path | None: ...


@dataclass(frozen=True)
class PageImage:
    """One normalized display page, ready to be referenced from a sidecar."""

    page_id: str
    asset_id: str
    asset_page: int
    path: Path
    sha256: str
    label: str
    width: int
    height: int
    scale: float


@dataclass(frozen=True)
class DecodeUsage:
    """What this broker measured for one decode child, each in the unit its limit counts.

    ``peak_memory_bytes`` — Windows: the child Job's ``PeakProcessMemoryUsed`` (commit); Linux:
    ``None``, because ``RLIMIT_AS`` counts address space and is calibrated by bisection.
    ``cpu_seconds`` — Windows: the Job's ``TotalUserTime`` (what ``PerProcessUserTimeLimit``
    counts); Linux: the ``ru_utime + ru_stime`` delta of ``RUSAGE_CHILDREN`` across the reap
    (what ``RLIMIT_CPU`` counts). ``wall_seconds`` — ``time.monotonic()`` from spawn to reap.
    """

    peak_memory_bytes: int | None
    cpu_seconds: float
    wall_seconds: float


@dataclass(frozen=True)
class PageProgress:
    """What one finished page records: raw and display size, orientation, scale and digest.

    ``page_count`` is the number of pages the asset yields (``page.asset_page`` counts up to
    it); ``source_width`` and ``source_height`` are the stored pixels before orientation is
    applied; ``orientation`` is the EXIF orientation that was applied, 1 when there was none or
    it was not an integer from 1 to 8; ``byte_count`` is the page file's size;
    ``decode_usage`` is the decode child's measured usage (``None`` for a page no child made).
    """

    page: PageImage
    page_count: int
    source_width: int
    source_height: int
    orientation: int
    byte_count: int
    decode_usage: DecodeUsage | None


@dataclass(frozen=True)
class EncodedPage:
    """One encoded display page; ``scale`` is the scale of the encode step alone."""

    data: bytes
    width: int
    height: int
    scale: float


@dataclass(frozen=True)
class _Decoded:
    """A validated decode response: the child's header and its exact pixel body."""

    mode: Literal["L", "RGB"]
    width: int
    height: int
    source_width: int
    source_height: int
    orientation: int
    scale: float
    pixels: bytes


@dataclass(frozen=True)
class _ChildResult:
    """How one decode child ended, as the exit-status mapper sees it."""

    returncode: int | None
    timed_out: bool
    overflowed: bool
    body_written: bool
    stream: bytes  # everything the child wrote after its ready line, within the cap


def cleanup_warning_count() -> int:
    """How many decode working directories could not be removed after their child was reaped.

    Never raised: such a directory never held an asset byte, which arrives over stdin.
    """

    return _CLEANUP_WARNINGS


def to_pages(
    asset: PageSource,
    *,
    pages_dir: Path,
    ticket_ref: str,
    asset_ordinal: int,
    progress: Callable[[PageProgress], None] | None = None,
) -> list[PageImage]:
    """Normalize one fetched asset into display pages under ``pages_dir``.

    Page ids are ``<ticket ref>-a<asset ordinal>-p<page number>`` and labels are ordinal-only,
    because a label reaches the rendered report. A failed fetch (``asset.path is None``) yields
    zero pages. ``progress`` is called once per page, after that page is published. A per-asset
    refusal raises :class:`ReceiptPageError` before any page file is written. An invalid
    ``asset_ordinal`` is a caller bug, not an asset outcome, so it raises a plain
    :class:`ValueError` that a record-and-continue handler cannot swallow.
    """

    if type(asset_ordinal) is not int or asset_ordinal < 1:
        raise ValueError("asset_ordinal must be a positive integer (1 is the first upload)")
    if not isinstance(ticket_ref, str) or not _TICKET_REF_RE.fullmatch(ticket_ref):
        raise ReceiptPageError(
            "ticket ref cannot form a safe receipt page id", reason="bad-ticket-ref"
        )
    if asset.path is None:
        return []
    if asset.media_type == "pdf":
        raise ReceiptPageError(
            "PDF receipt assets are not rendered by this build; export the pages manually",
            reason="pdf-not-rendered",
        )
    if asset.media_type not in ("png", "jpeg"):
        raise ReceiptPageError("receipt asset type must be PNG, JPEG or PDF", reason="unreadable")
    match = ASSET_ID.pattern.fullmatch(asset.asset_id or "")
    if match is None:
        raise ReceiptPageError("receipt asset id is malformed", reason="digest-mismatch")
    if sys.platform not in SUPPORTED_DECODE_PLATFORMS:
        raise ReceiptDecodeUnavailableError(
            "receipt images can be decoded only on Windows or Linux, where the decode child's "
            "limits are enforced; run the refresh on a supported host"
        )
    pillow = _broker_pillow_version()
    data = _read_capped_asset(asset.path)
    if hashlib.sha256(data).hexdigest() != match.group(1):
        raise ReceiptPageError(
            "cached receipt asset no longer matches its asset id; re-fetch it",
            reason="digest-mismatch",
        )
    if not data:
        # The child refuses a zero-byte body before its ready line, which the broker would
        # have to treat as a stage abort; an empty upload is this asset's problem alone.
        raise ReceiptPageError(_MESSAGES["unreadable"], reason="unreadable")
    decoded, usage = _decode_in_child(data, asset.media_type, pillow=pillow)
    del data
    encoded = pixels_to_page(
        decoded.pixels, mode=decoded.mode, width=decoded.width, height=decoded.height
    )
    if len(encoded.data) > MAX_PAGE_BYTES:
        raise ReceiptPageError(
            "normalized receipt page exceeds the offline report page limit",
            reason="too-large",
            usage=usage,
        )

    # An image asset is exactly one page; the loop is the per-page contract the PDF path shares.
    rendered = [encoded]
    root = pages_dir.resolve()
    pages: list[PageImage] = []
    for page_number, page_data in enumerate(rendered, start=1):
        page_id = f"{ticket_ref}-a{asset_ordinal}-p{page_number}"
        page = PageImage(
            page_id=page_id,
            asset_id=match.group(0),
            asset_page=page_number,
            path=_write_page(root, page_id + PAGE_SUFFIX, page_data.data),
            sha256=hashlib.sha256(page_data.data).hexdigest(),
            label=f"Ticket {ticket_ref} · upload {asset_ordinal} · page {page_number}",
            width=page_data.width,
            height=page_data.height,
            scale=decoded.scale * page_data.scale,
        )
        pages.append(page)
        if progress is not None:
            progress(
                PageProgress(
                    page=page,
                    page_count=len(rendered),
                    source_width=decoded.source_width,
                    source_height=decoded.source_height,
                    orientation=decoded.orientation,
                    byte_count=len(page_data.data),
                    decode_usage=usage,
                )
            )
    return pages


def pixels_to_page(
    pixels: bytes, *, mode: Literal["L", "RGB"], width: int, height: int
) -> EncodedPage:
    """Encode validated raw pixels as one baseline-JPEG display page.

    The one pixels-to-page step for every producer: the image child's validated output and,
    later, the PDF worker's validated gray8 render. ``pixels`` is row-major, top row first, no
    row padding, channels interleaved for RGB, exactly ``width * height * C`` bytes. A LANCZOS
    downscale runs only when the long side exceeds ``NORMALIZATION.max_long_edge`` (never for
    child output). These are the only Pillow calls that ever see producer output; they read
    pixels alone, so no metadata can reach the page. A size mismatch is a caller bug and raises
    a plain :class:`ValueError`.
    """

    channels = _CHANNELS.get(mode)
    if channels is None:
        raise ValueError("page pixels must be L or RGB")
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        raise ValueError("page size must be positive integers")
    if len(pixels) != width * height * channels:
        raise ValueError("page pixels do not match their declared size")
    image_api = _pillow_image_module()
    image = image_api.frombytes(mode, (width, height), pixels)
    scale = 1.0
    if max(width, height) > NORMALIZATION.max_long_edge:
        target_width, target_height, scale = scaled_size(
            width, height, 1, NORMALIZATION.max_long_edge
        )
        image = image.resize((target_width, target_height), image_api.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(
        buffer,
        format="JPEG",
        quality=NORMALIZATION.jpeg_quality,
        subsampling=NORMALIZATION.jpeg_subsampling,
        optimize=True,
    )
    return EncodedPage(data=buffer.getvalue(), width=image.width, height=image.height, scale=scale)


def _pillow_image_module() -> Any:
    try:
        from PIL import Image
    except ModuleNotFoundError as exc:
        if exc.name != "PIL":
            raise
        raise ReceiptDecodeUnavailableError(_RECEIPTS_EXTRA) from None
    return Image


def _broker_pillow_version() -> str:
    """This broker's Pillow version, which the child must match; refuse one below the floor."""

    try:
        import PIL
    except ModuleNotFoundError as exc:
        if exc.name != "PIL":
            raise
        raise ReceiptDecodeUnavailableError(_RECEIPTS_EXTRA) from None
    version = str(PIL.__version__)
    parts = re.match(r"(\d+)\.(\d+)", version)
    if parts is None or (int(parts[1]), int(parts[2])) < _PILLOW_FLOOR:
        raise ReceiptDecodeUnavailableError(_RECEIPTS_EXTRA)
    return version


def _read_capped_asset(path: Path) -> bytes:
    """Read a cached asset, refusing one larger than ``max_source_bytes`` before any child."""

    cap = NORMALIZATION.max_source_bytes
    try:
        if path.stat().st_size > cap:
            raise ReceiptPageError(_MESSAGES["too-large"], reason="too-large")
        with path.open("rb") as handle:
            data = handle.read(cap + 1)
    except OSError:
        raise ReceiptPageError(
            "cached receipt asset is missing or unreadable; re-fetch it",
            reason="digest-mismatch",
        ) from None
    if len(data) > cap:
        raise ReceiptPageError(_MESSAGES["too-large"], reason="too-large")
    return data


def _request_for(data: bytes, media_type: str) -> DecodeRequest:
    return DecodeRequest(
        media_type=media_type,
        byte_count=len(data),
        max_source_bytes=NORMALIZATION.max_source_bytes,
        max_source_pixels=NORMALIZATION.max_source_pixels,
        max_source_edge=NORMALIZATION.max_source_edge,
        max_long_edge=NORMALIZATION.max_long_edge,
        background=NORMALIZATION.background,
        memory_bytes=NORMALIZATION.memory_bytes,
        cpu_seconds=NORMALIZATION.cpu_seconds,
    )


def _stdout_cap(request: DecodeRequest) -> int:
    """The most a valid response can be: 1 KiB of header plus a full-size RGB body."""

    return RESPONSE_HEADER_MAX_BYTES + request.max_long_edge**2 * 3


class _StdoutPump:
    """Reads a child's stdout into one buffer, never more than one byte past its cap.

    The ready line (at most ``READY_LINE_MAX_BYTES``) is found first; everything after it is
    held only up to ``response_cap`` bytes. One byte more marks the stream as overflowed and
    stops the read, so an oversize stream is detected, never buffered. ``run`` is the helper
    thread's body; the broker waits on it with deadlines and never blocks on the pipe itself.
    """

    def __init__(self, read: Callable[[int], bytes], *, response_cap: int) -> None:
        self._read = read
        self._response_cap = response_cap
        self._buffer = bytearray()
        self._ready_end: int | None = None
        self._ready_oversize = False
        self.overflowed = False
        self.finished = False
        self._condition = threading.Condition()

    def run(self) -> None:
        while True:
            with self._condition:
                if self._ready_end is None:
                    limit = READY_LINE_MAX_BYTES + 1
                else:
                    limit = self._ready_end + self._response_cap + 1
                wanted = min(_PIPE_CHUNK, limit - len(self._buffer))
            try:
                chunk = self._read(wanted)
            except (OSError, ValueError):
                chunk = b""
            with self._condition:
                if not chunk:
                    self.finished = True
                    self._condition.notify_all()
                    return
                self._buffer += chunk
                if self._ready_end is None:
                    newline = self._buffer.find(b"\n")
                    if 0 <= newline < READY_LINE_MAX_BYTES:
                        self._ready_end = newline + 1
                    elif newline >= 0 or len(self._buffer) > READY_LINE_MAX_BYTES:
                        self._ready_oversize = True
                        self.finished = True
                        self._condition.notify_all()
                        return
                elif len(self._buffer) > self._ready_end + self._response_cap:
                    self.overflowed = True
                    self.finished = True
                    self._condition.notify_all()
                    return
                self._condition.notify_all()

    def wait_ready(self, deadline: float) -> bytes | None:
        """The ready line's bytes (LF included); an oversize prefix; or ``None`` if none came."""

        with self._condition:
            while self._ready_end is None and not self.finished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            if self._ready_end is not None:
                return bytes(self._buffer[: self._ready_end])
            if self._ready_oversize:
                return bytes(self._buffer[: READY_LINE_MAX_BYTES + 1])
            return None

    def wait_finished(self, deadline: float) -> bool:
        with self._condition:
            while not self.finished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def response(self) -> bytes:
        """Everything after the ready line, copied once."""

        with self._condition:
            start = self._ready_end or 0
            return bytes(memoryview(self._buffer)[start:])

    def release(self) -> None:
        with self._condition:
            self._buffer = bytearray()


def _parse_ready_line(
    line: bytes | None,
    *,
    expected_pid: int,
    pillow: str,
    memory_bytes: int,
    max_as_baseline: int,
    platform: str,
) -> int | None:
    """Validate the child's ready line; the Linux ``rlimit_as``, else ``None``.

    Every failure is a stage abort (:class:`ReceiptDecodeUnavailableError`): the ready line is
    the abort/refusal boundary, so nothing before it is ever charged to an asset.
    """

    if line is None:
        raise ReceiptDecodeUnavailableError(
            "receipt decode child did not confirm its limits in time; check that this host "
            "can run a limited Python child process"
        )
    try:
        value = parse_line(line, cap=READY_LINE_MAX_BYTES)
        keys = READY_KEYS | ({"rlimit_as"} if platform == "linux" else frozenset())
        if set(value) != keys:
            raise WireError("ready key set is not exact")
        wire_text(value["status"], frozenset({"ready"}))
        wire_text(value["protocol"], frozenset({PROTOCOL}))
        pid = wire_int(value["pid"])
        if type(value["pillow"]) is not str:
            raise WireError("ready pillow must be a string")
        rlimit_as = wire_int(value["rlimit_as"]) if platform == "linux" else None
    except WireError:
        raise ReceiptDecodeUnavailableError(
            "receipt decode child sent a malformed ready line; reinstall the toolkit"
        ) from None
    if pid != expected_pid:
        raise ReceiptDecodeUnavailableError(
            "receipt decode child is not the process that carries its limits; reinstall the "
            "toolkit's Python environment"
        )
    if value["pillow"] != pillow:
        raise ReceiptDecodeUnavailableError(
            "receipt decode child runs a different Pillow than the broker; re-sync the "
            "environment (uv sync --extra receipts)"
        )
    if rlimit_as is not None and not memory_bytes <= rlimit_as <= memory_bytes + max_as_baseline:
        raise ReceiptDecodeUnavailableError(
            "receipt decode child applied an address-space limit outside its band; check this "
            "host's Python installation"
        )
    return rlimit_as


def _parse_decode_response(stream: bytes, request: DecodeRequest) -> _Decoded | str:
    """Validate the child's response: a decoded page, or the closed-set fast-path refusal reason.

    Anything else — a bad header, a wrong key set or type, sizes that disagree with
    :func:`scaled_size`, or a body of the wrong length — raises ``failed-validation``.
    """

    newline = stream.find(b"\n", 0, RESPONSE_HEADER_MAX_BYTES + 1)
    try:
        if newline < 0:
            raise WireError("response header is missing or over its cap")
        header = parse_line(stream[: newline + 1], cap=RESPONSE_HEADER_MAX_BYTES)
        status = wire_text(header.get("status"), frozenset({"decoded", "refused"}))
        body = memoryview(stream)[newline + 1 :]
        if status == "refused":
            if set(header) != REFUSED_KEYS or len(body):
                raise WireError("refusal response is not exact")
            return wire_text(header["reason"], CHILD_REFUSAL_REASONS)
        if set(header) != DECODED_KEYS:
            raise WireError("decoded response key set is not exact")
        mode = wire_text(header["mode"], frozenset(_CHANNELS))
        width = wire_int(header["width"])
        height = wire_int(header["height"])
        source_width = wire_int(header["source_width"])
        source_height = wire_int(header["source_height"])
        orientation = wire_int(header["orientation"])
        scale = wire_float(header["scale"])
        if not 1 <= orientation <= 8:
            raise WireError("orientation is outside 1-8")
        if (
            source_width < 1
            or source_height < 1
            or source_width * source_height > request.max_source_pixels
            or max(source_width, source_height) > request.max_source_edge
        ):
            raise WireError("source size is outside the request's ceilings")
        derived = scaled_size(source_width, source_height, orientation, request.max_long_edge)
        if (width, height, scale) != derived:
            raise WireError("page size disagrees with the one size derivation")
        if not (1 <= width <= request.max_long_edge and 1 <= height <= request.max_long_edge):
            raise WireError("page size is outside the long-edge limit")
        if len(body) != width * height * _CHANNELS[mode]:
            raise WireError("pixel body has the wrong length")
    except WireError:
        raise ReceiptPageError(_MESSAGES["failed-validation"], reason="failed-validation") from None
    return _Decoded(
        mode="L" if mode == "L" else "RGB",
        width=width,
        height=height,
        source_width=source_width,
        source_height=source_height,
        orientation=orientation,
        scale=scale,
        pixels=bytes(body),
    )


def _map_child_exit(result: _ChildResult, request: DecodeRequest) -> _Decoded | str:
    """The child failure taxonomy (plan § 5A, "Exit status"): a page, or a refusal reason.

    A stream past its cap and the wall clock are the budget; exit 0 is a validated response or
    the closed-set fast-path refusal (a malformed one is ``failed-validation``); exit
    ``EXIT_BUDGET`` is the budget; exit ``EXIT_CHILD_ERROR`` is a child bug, counted apart from
    the budget; any other ending — another code, a signal, a Job kill, a native crash — is the
    budget, because native code can fail an allocation that way.
    """

    if result.overflowed or result.timed_out:
        return "budget"
    if result.returncode == 0:
        if not result.body_written:
            return "failed-validation"
        try:
            return _parse_decode_response(result.stream, request)
        except ReceiptPageError as refusal:
            return refusal.reason
    if result.returncode == EXIT_BUDGET:
        return "budget"
    if result.returncode == EXIT_CHILD_ERROR:
        return "child-error"
    return "budget"


def _decode_in_child(data: bytes, media_type: str, *, pillow: str) -> tuple[_Decoded, DecodeUsage]:
    """Run one decode child over ``data``; a validated page, or a per-asset refusal.

    The request is first checked with the child's own validator, so nothing the child would
    refuse before its ready line is ever sent: the asset-dependent value (``byte_count``) has
    already been refused per asset by :func:`to_pages`, and anything left is a configuration
    fault that aborts the stage before a spawn. Children run one at a time per process, which
    also keeps the Linux ``RUSAGE_CHILDREN`` delta attributable to the one child.
    """

    request = _request_for(data, media_type)
    try:
        if parse_request(request.line()) != request:
            raise WireError("request does not survive its own wire form")
    except WireError:
        raise ReceiptDecodeUnavailableError(
            "receipt decode limits are invalid; restore the toolkit's receipt_geometry values"
        ) from None
    with _ONE_CHILD_AT_A_TIME:
        run = _DecodeChild(request, pillow=pillow)
        try:
            result = run.communicate(data)
        finally:
            run.close()
    usage = run.usage
    outcome = _map_child_exit(result, request)
    del result
    if isinstance(outcome, str):
        raise ReceiptPageError(_MESSAGES[outcome], reason=outcome, usage=usage)
    if usage is None:
        raise ReceiptPageError(_MESSAGES["child-error"], reason="child-error")
    return outcome, usage


class _DecodeChild:
    """One decode child process: spawn, limit, attest, feed, reap and measure."""

    def __init__(self, request: DecodeRequest, *, pillow: str) -> None:
        self._request = request
        self._pillow = pillow
        self._process: subprocess.Popen[bytes] | None = None
        self._kernel32: Any = None
        self._job: int | None = None
        self._in_job = False
        self._workdir: str | None = None
        self._pump: _StdoutPump | None = None
        self._threads: list[threading.Thread] = []
        self._reaped = False
        self._started = 0.0
        self._rusage_before = 0.0
        self.usage: DecodeUsage | None = None

    def communicate(self, data: bytes) -> _ChildResult:
        request = self._request
        self._spawn()
        process = self._process
        pump = self._pump
        assert process is not None and pump is not None and process.stdin is not None
        wall_deadline = self._started + NORMALIZATION.wall_seconds
        ready_deadline = min(self._started + NORMALIZATION.ready_seconds, wall_deadline)
        try:
            _write_all(process.stdin, request.line())
        except OSError:
            raise ReceiptDecodeUnavailableError(
                "receipt decode child exited before reading its request"
            ) from None
        _parse_ready_line(
            pump.wait_ready(ready_deadline),
            expected_pid=process.pid,
            pillow=self._pillow,
            memory_bytes=request.memory_bytes,
            max_as_baseline=NORMALIZATION.max_as_baseline,
            platform=sys.platform,
        )
        # The ready line is accepted: from here every failure is a per-asset refusal.
        written = threading.Event()
        writer = threading.Thread(
            target=_write_body, args=(process.stdin, data, written), daemon=True
        )
        self._threads.append(writer)
        writer.start()
        timed_out = not pump.wait_finished(wall_deadline)
        if timed_out or pump.overflowed:
            self._kill()
        if not self._wait_for_exit(wall_deadline):
            timed_out = True
            self._kill()
            self._wait_for_exit(time.monotonic() + NORMALIZATION.wall_seconds)
        writer.join(1.0)
        self._finish()
        result = _ChildResult(
            returncode=process.returncode,
            timed_out=timed_out,
            overflowed=pump.overflowed,
            body_written=written.is_set(),
            stream=b"" if timed_out or pump.overflowed else pump.response(),
        )
        pump.release()
        return result

    def _spawn(self) -> None:
        request = self._request
        environment: dict[str, str]
        if sys.platform == "win32":
            try:
                self._kernel32 = process_limits.load_kernel32()
                self._job = process_limits.make_job_object(
                    self._kernel32,
                    memory_bytes=request.memory_bytes,
                    cpu_seconds=request.cpu_seconds,
                )
            except process_limits.ProcessLimitsError as exc:
                if exc.handle is not None:
                    _RETAINED_JOB_HANDLES.append(exc.handle)
                raise ReceiptDecodeUnavailableError(
                    "receipt decode child limits cannot be created on this host"
                ) from None
            base: Any = getattr(sys, "_base_executable", None)
            if not isinstance(base, str) or not base:
                raise ReceiptDecodeUnavailableError(
                    "receipt decode child needs the base Python interpreter; reinstall Python"
                )
            command = [base, "-I", "-m", _DECODE_MODULE]
            environment = {
                "SYSTEMROOT": _windows_directory(self._kernel32),
                "__PYVENV_LAUNCHER__": sys.executable,
            }
        else:
            command = [sys.executable, "-I", "-m", _DECODE_MODULE]
            environment = {}
            import resource

            usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            self._rusage_before = usage.ru_utime + usage.ru_stime
        try:
            self._workdir = tempfile.mkdtemp(prefix="pta-receipt-decode-")
        except OSError:
            raise ReceiptDecodeUnavailableError(
                "receipt decode child needs a temporary working directory; check TEMP"
            ) from None
        self._started = time.monotonic()
        try:
            if sys.platform == "win32":
                self._process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    close_fds=True,
                    cwd=self._workdir,
                    env=environment,
                    creationflags=_WINDOWS_CREATE_NO_WINDOW,
                )
            else:
                self._process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    close_fds=True,
                    cwd=self._workdir,
                    env=environment,
                    start_new_session=True,
                )
        except (OSError, ValueError):
            raise ReceiptDecodeUnavailableError(
                "receipt decode child could not be started; check this host's Python"
            ) from None
        process = self._process
        assert process.stdout is not None
        if sys.platform == "win32":
            handle: Any = getattr(process, "_handle", None)
            job = self._job
            assert job is not None
            try:
                if not isinstance(handle, int):
                    raise process_limits.ProcessLimitsError("no process handle", code=6)
                process_limits.assign_process(self._kernel32, job, int(handle))
                if not process_limits.is_process_in_job(self._kernel32, int(handle), job):
                    raise process_limits.ProcessLimitsError("not in the Job", code=6)
                self._in_job = True
            except process_limits.ProcessLimitsError:
                raise ReceiptDecodeUnavailableError(
                    "receipt decode child could not be placed in its limits on this host"
                ) from None
        stdout = process.stdout
        self._pump = _StdoutPump(stdout.read, response_cap=_stdout_cap(self._request))
        reader = threading.Thread(target=self._pump.run, daemon=True)
        self._threads.append(reader)
        reader.start()

    def _kill(self) -> None:
        """End the child and anything it started: its Job on Windows, its group on Linux."""

        process = self._process
        if process is None or self._reaped:
            return
        if sys.platform == "win32":
            # Ending the Job reaches the child only once it is confirmed inside it; a child the
            # broker could not place in its Job is ended directly.
            if self._job is not None and self._in_job:
                try:
                    process_limits.terminate_job(self._kernel32, self._job)
                    return
                except process_limits.ProcessLimitsError:
                    pass
            try:
                process.kill()
            except OSError:
                pass
        else:
            _kill_group(process.pid)

    def _wait_for_exit(self, deadline: float) -> bool:
        """Wait until the child has exited; on Linux without reaping it, so its group id stays
        reserved for the kill that precedes the reap."""

        process = self._process
        assert process is not None
        if sys.platform == "win32":
            try:
                process.wait(max(0.0, deadline - time.monotonic()))
                return True
            except subprocess.TimeoutExpired:
                return False
        while True:
            try:
                if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG):
                    return True
            except ChildProcessError:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def _finish(self) -> None:
        """Measure, kill what remains, and reap: the order each platform requires."""

        process = self._process
        if process is None or self._reaped:
            return
        if sys.platform == "win32":
            try:
                process.wait(NORMALIZATION.wall_seconds)
            except subprocess.TimeoutExpired:
                return
            self._reaped = True
            try:
                limits = process_limits.query_job_limits(self._kernel32, self._job)
                accounting = process_limits.query_job_accounting(self._kernel32, self._job or 0)
            except process_limits.ProcessLimitsError:
                self.usage = None
            else:
                self.usage = DecodeUsage(
                    peak_memory_bytes=limits.peak_process_memory_used,
                    cpu_seconds=accounting.total_user_time
                    / process_limits.HUNDRED_NANOSECONDS_PER_SECOND,
                    wall_seconds=time.monotonic() - self._started,
                )
        else:
            import resource

            _kill_group(process.pid)
            try:
                process.wait(NORMALIZATION.wall_seconds)
            except subprocess.TimeoutExpired:
                return
            self._reaped = True
            wall = time.monotonic() - self._started
            usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            self.usage = DecodeUsage(
                peak_memory_bytes=None,
                cpu_seconds=max(0.0, usage.ru_utime + usage.ru_stime - self._rusage_before),
                wall_seconds=wall,
            )

    def close(self) -> None:
        """Release everything on every path, including a stage abort before the ready line."""

        global _CLEANUP_WARNINGS
        process = self._process
        if process is not None and not self._reaped:
            self._kill()
            if sys.platform == "win32":
                try:
                    process.wait(NORMALIZATION.wall_seconds)
                    self._reaped = True
                except subprocess.TimeoutExpired:
                    pass
            else:
                self._wait_for_exit(time.monotonic() + NORMALIZATION.wall_seconds)
                _kill_group(process.pid)
                try:
                    process.wait(NORMALIZATION.wall_seconds)
                    self._reaped = True
                except subprocess.TimeoutExpired:
                    pass
        if process is not None and not self._threads_alive():
            # A helper thread still blocked on a pipe keeps its stream; closing it under the
            # thread would be unsafe, and the thread is a daemon.
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        if self._job is not None:
            if not process_limits.close_handle(self._kernel32, self._job):
                _RETAINED_JOB_HANDLES.append(self._job)
            self._job = None
        if self._workdir is not None:
            # Removed only after the reap (a terminated process can still hold it briefly), or
            # at once when no child was ever started in it.
            if self._reaped or process is None:
                try:
                    shutil.rmtree(self._workdir)
                except OSError:
                    _CLEANUP_WARNINGS += 1
            else:
                _CLEANUP_WARNINGS += 1
            self._workdir = None

    def _threads_alive(self) -> bool:
        for thread in self._threads:
            thread.join(1.0)
        return any(thread.is_alive() for thread in self._threads)


def _write_all(stream: Any, data: bytes) -> None:
    """Write every byte to a raw (unbuffered) pipe, which may accept a write only in part."""

    view = memoryview(data)
    while view:
        count = stream.write(view[:_PIPE_CHUNK])
        if not count:
            raise BrokenPipeError("the decode child stopped reading")
        view = view[count:]


def _write_body(stream: Any, data: bytes, written: threading.Event) -> None:
    """Write the whole asset body and close stdin; a broken pipe leaves ``written`` unset."""

    try:
        _write_all(stream, data)
        stream.close()
    except (OSError, ValueError):
        return
    written.set()


def _kill_group(pid: int) -> None:
    """SIGKILL the child's whole process group; an already-empty group is fine."""

    if sys.platform != "win32":
        import signal

        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _windows_directory(kernel32: Any) -> str:
    """The OS directory from Win32, never from a caller-controlled environment."""

    getter = kernel32.GetWindowsDirectoryW
    getter.argtypes = (ctypes.c_wchar_p, ctypes.c_uint)
    getter.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    length = int(getter(buffer, 32768))
    if length == 0 or length >= 32768 or not Path(buffer.value).is_dir():
        raise ReceiptDecodeUnavailableError(
            "receipt decode child needs the Windows directory; check this host"
        )
    return str(buffer.value)


def _holds_same_page(target: Path, payload: bytes) -> bool:
    """False when nothing is at ``target``; True for identical bytes; otherwise a conflict."""

    if not target.is_symlink() and not target.exists():
        return False
    if target.is_symlink() or not target.is_file() or target.read_bytes() != payload:
        raise ReceiptPageConflictError(
            f"refusing to replace a different existing receipt page {target.name}; "
            "move it aside or choose another pages directory"
        )
    return True


def _write_page(root: Path, name: str, payload: bytes) -> Path:
    """Publish one page without ever replacing a file; identical bytes are a no-op."""

    target = root / name
    try:
        root.mkdir(parents=True, exist_ok=True)
        folded = name.casefold()
        if any(entry != name and entry.casefold() == folded for entry in os.listdir(root)):
            # One file on a case-insensitive disk, two on a case-sensitive one: never portable.
            raise ReceiptPageConflictError(
                f"refusing to write receipt page {name}: a page named differently only in "
                "letter case exists; ticket refs must not differ only by case"
            )
        if _holds_same_page(target, payload):
            return target
        fd, temp_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=root)
    except OSError:
        raise ReceiptPagesDirectoryError(_UNWRITABLE) from None
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # A hard link publishes the complete file atomically and, unlike os.replace, fails
            # rather than overwriting: a page that appeared since the check above is compared.
            os.link(temp_name, target)
        except FileExistsError:
            if not _holds_same_page(target, payload):
                raise ReceiptPageConflictError(
                    f"receipt page {name} changed while it was being written; run again"
                ) from None
        except OSError:
            raise ReceiptPagesDirectoryError(
                "receipt pages directory must be on a filesystem that supports hard links"
            ) from None
    except ReceiptPagesDirectoryError:
        raise
    except OSError:
        raise ReceiptPagesDirectoryError(_UNWRITABLE) from None
    finally:
        # Best effort: a leftover dot-prefixed temporary file is harmless, and its cleanup must
        # never change the outcome of a page that is already published.
        try:
            os.unlink(temp_name)
        except OSError:
            pass
    return target
