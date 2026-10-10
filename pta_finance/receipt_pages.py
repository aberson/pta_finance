"""Fetched receipt assets become deterministic display pages.

This is the image half: PNG and JPEG assets are normalized here. PDF rendering crosses the
isolated worker boundary and is not part of this module yet, so a PDF asset is refused by name
rather than parsed in this process.

Normalization is deterministic — the same asset bytes always yield byte-identical page files
under one Pillow/libjpeg build — and runs in a fixed order, using
:data:`pta_finance.receipt_geometry.NORMALIZATION`:

1. **Orientation.** The EXIF (or XMP) orientation is applied from a fixed transpose table, so
   stored pixels match what a viewer displays. EXIF is read, never re-serialised.
2. **Alpha-flatten** onto white, so a transparent region never renders as black. This comes
   before scaling so every page is resampled as 8-bit L or RGB: Pillow would silently fall back
   to nearest-neighbour for a palette or bilevel source.
3. **Downscale only above the long-edge limit**, uniformly, with LANCZOS resampling.
4. **Re-encode from pixels alone** as a baseline JPEG. The encoder is handed a fresh image with
   no inherited ``info``, so no source comment, EXIF, XMP, ICC profile or text chunk can reach
   the page — those are requestor-controlled bytes, and the page is embedded in the report.

A JPEG is first bounded from its raw bytes, before Pillow parses anything: the count of
scan-start markers (each scan is another full decode pass, so scan count, not file size, bounds
decode time), the summed size of its EXIF segments, and the size of its multi-picture index
(Pillow parses both metadata blocks while merely opening the file). Each bound over-counts
rather than under-counts what the decoder will process — every scan the decoder runs is a
literal ``FF DA`` pair, because byte stuffing keeps that pair out of entropy-coded data — so
decoder leniency (junk between segments, stuffed bytes, short lengths) can only make the bound
stricter. A guard that instead modelled the stream would fail open wherever the decoder is more
lenient than the model. Then, still before any pixel is decoded, an upload is refused when its
header exceeds the pixel ceiling, uses an unsupported pixel format, or is an animated PNG (a
browser and this module could show different frames); and before EXIF is parsed, when it
carries more EXIF than the EXIF ceiling. Each intermediate image is released as soon as the
next exists, so peak memory stays a small multiple of one page.

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
per-asset: the caller records it against that asset and carries on with the batch.
:class:`ReceiptPagesDirectoryError` means no page can be written at all, and aborts the stage;
so does the :class:`ImportError` raised when the first image is normalized without a supported
Pillow. Pillow is imported lazily, like the package's other optional extras, so importing this
module never requires it. Messages carry file names and remediation only — never a URL,
vendor, requestor or upload filename — and causes are suppressed (``from None``) so no path or
decoder internals of an untrusted upload reach a traceback.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pta_finance.receipt_geometry import NORMALIZATION
from pta_finance.receipt_viewer import MAX_PAGE_BYTES

if TYPE_CHECKING:
    from PIL import Image

__all__ = [
    "PAGE_SUFFIX",
    "PageImage",
    "PageProgress",
    "PageSource",
    "ReceiptPageConflictError",
    "ReceiptPageError",
    "ReceiptPagesDirectoryError",
    "to_pages",
]

PAGE_SUFFIX = ".jpg"

# The oldest supported Pillow, as (major, minor): the floor of the 'receipts' extra in
# pyproject.toml, which a test compares with this value.
_PILLOW_FLOOR = (12, 2)
_RECEIPTS_EXTRA = (
    "receipt page normalization requires the optional 'receipts' extra (uv sync --extra receipts)"
)
_ASSET_ID_RE = re.compile(r"asset:v1:([0-9a-f]{64})")
_TICKET_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# The only Pillow decoder each declared media type may reach. A phone's multi-picture JPEG
# opens through the JPEG decoder as "MPO"; only its first, primary picture is used.
_DECODERS = {"png": "PNG", "jpeg": "JPEG"}
_GRAY_MODES = frozenset({"1", "L", "LA", "La"})
_COLOR_MODES = frozenset({"P", "RGB", "RGBA", "RGBa", "CMYK"})
_EXIF_ORIENTATION = 0x0112
# EXIF orientation -> the Pillow transpose that turns stored pixels into the displayed page.
_DISPLAY_TRANSPOSE = {
    2: "FLIP_LEFT_RIGHT",
    3: "ROTATE_180",
    4: "FLIP_TOP_BOTTOM",
    5: "TRANSPOSE",
    6: "ROTATE_270",
    7: "TRANSVERSE",
    8: "ROTATE_90",
}
# Every EXIF / MP-index segment Pillow reads is literally its marker, a two-byte length and its
# signature, so these over-count (never under-count) the segments it will parse.
_JPEG_EXIF_SEGMENT = re.compile(rb"\xff\xe1(..)Exif\x00\x00", re.DOTALL)
_JPEG_MPF_SEGMENT = re.compile(rb"\xff\xe2(..)MPF\x00", re.DOTALL)
_JPEG_SCAN_START = b"\xff\xda"
_TOO_MANY_PIXELS = "receipt image has too many pixels to normalize safely"
_TOO_MANY_SCANS = "receipt JPEG has too many scans to normalize safely"
_TOO_MUCH_METADATA = "receipt image carries too much EXIF or multi-picture metadata to read safely"
_UNREADABLE = "receipt asset is not a readable image of its type"
_UNWRITABLE = "receipt pages directory is not writable"


class ReceiptPageError(ValueError):
    """Per-asset refusal: this asset yields no trustworthy page; record it and continue."""


class ReceiptPageConflictError(ReceiptPageError):
    """Per-asset refusal: a different page file already holds this page id."""


class ReceiptPagesDirectoryError(OSError):
    """No page can be written to the pages directory; the stage aborts.

    Deliberately not a :class:`ReceiptPageError`, so a caller that records per-asset refusals
    cannot swallow it.
    """


class PageSource(Protocol):
    """The fields of a fetched receipt asset this module reads.

    ``receipt_assets.AssetResult`` satisfies it structurally; ``path`` is ``None`` when the
    fetch failed.
    """

    @property
    def asset_id(self) -> str: ...

    @property
    def media_type(self) -> str: ...

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
class PageProgress:
    """What one finished page records: raw and display size, orientation, scale and digest.

    ``page_count`` is the number of pages the asset yields (``page.asset_page`` counts up to
    it); ``source_width`` and ``source_height`` are the stored pixels before orientation is
    applied; ``orientation`` is the EXIF orientation that was applied, 1 when there was none or
    it was not an integer from 1 to 8.
    """

    page: PageImage
    page_count: int
    source_width: int
    source_height: int
    orientation: int
    byte_count: int


@dataclass(frozen=True)
class _Normalized:
    payload: bytes
    width: int
    height: int
    scale: float
    source_width: int
    source_height: int
    orientation: int


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
        raise ReceiptPageError("ticket ref cannot form a safe receipt page id")
    if asset.path is None:
        return []
    if asset.media_type == "pdf":
        raise ReceiptPageError(
            "PDF receipt assets are not rendered by this build; export the pages manually"
        )
    if asset.media_type not in _DECODERS:
        raise ReceiptPageError("receipt asset type must be PNG, JPEG or PDF")
    match = _ASSET_ID_RE.fullmatch(asset.asset_id)
    if match is None:
        raise ReceiptPageError("receipt asset id is malformed")
    try:
        data = asset.path.read_bytes()
    except OSError:
        raise ReceiptPageError("cached receipt asset is missing or unreadable") from None
    if hashlib.sha256(data).hexdigest() != match.group(1):
        raise ReceiptPageError("cached receipt asset no longer matches its asset id; re-fetch it")
    normalized = _normalize(data, asset.media_type)
    if len(normalized.payload) > MAX_PAGE_BYTES:
        raise ReceiptPageError("normalized receipt page exceeds the offline report page limit")

    # An image asset is exactly one page; the loop is the per-page contract the PDF path shares.
    rendered = [normalized]
    root = pages_dir.resolve()
    pages: list[PageImage] = []
    for page_number, page_data in enumerate(rendered, start=1):
        page_id = f"{ticket_ref}-a{asset_ordinal}-p{page_number}"
        page = PageImage(
            page_id=page_id,
            asset_id=asset.asset_id,
            asset_page=page_number,
            path=_write_page(root, page_id + PAGE_SUFFIX, page_data.payload),
            sha256=hashlib.sha256(page_data.payload).hexdigest(),
            label=f"Ticket {ticket_ref} · upload {asset_ordinal} · page {page_number}",
            width=page_data.width,
            height=page_data.height,
            scale=page_data.scale,
        )
        pages.append(page)
        if progress is not None:
            progress(
                PageProgress(
                    page=page,
                    page_count=len(rendered),
                    source_width=page_data.source_width,
                    source_height=page_data.source_height,
                    orientation=page_data.orientation,
                    byte_count=len(page_data.payload),
                )
            )
    return pages


def _normalize(data: bytes, media_type: str) -> _Normalized:
    """Decode one PNG/JPEG and apply the fixed normalization order in the module docstring."""

    try:
        import PIL
        from PIL import Image
    except ModuleNotFoundError as exc:
        if exc.name != "PIL":
            raise
        raise ImportError(_RECEIPTS_EXTRA) from None
    version = re.match(r"(\d+)\.(\d+)", PIL.__version__)
    if version is None or (int(version[1]), int(version[2])) < _PILLOW_FLOOR:
        raise ImportError(_RECEIPTS_EXTRA)

    if media_type == "jpeg":
        # Raw-byte upper bounds, taken before Pillow parses anything (see the module docstring).
        if data.count(_JPEG_SCAN_START) > NORMALIZATION.max_jpeg_scans:
            raise ReceiptPageError(_TOO_MANY_SCANS)
        exif_bytes = sum(int.from_bytes(m[1], "big") for m in _JPEG_EXIF_SEGMENT.finditer(data))
        mpf_bytes = max(
            (int.from_bytes(m[1], "big") for m in _JPEG_MPF_SEGMENT.finditer(data)), default=0
        )
        if exif_bytes > NORMALIZATION.max_exif_bytes or mpf_bytes > NORMALIZATION.max_mpf_bytes:
            raise ReceiptPageError(_TOO_MUCH_METADATA)
    try:
        source = Image.open(io.BytesIO(data), formats=[_DECODERS[media_type]])
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ReceiptPageError(_TOO_MANY_PIXELS) from None
    except Exception:
        raise ReceiptPageError(_UNREADABLE) from None
    source_width, source_height = source.size
    if source_width * source_height > NORMALIZATION.max_source_pixels:
        raise ReceiptPageError(_TOO_MANY_PIXELS)
    if source.mode not in _GRAY_MODES | _COLOR_MODES:
        # 16-bit and float modes would clip to white rather than scale: refuse, never guess.
        raise ReceiptPageError(
            "receipt image uses an unsupported pixel format; export it as an 8-bit PNG or JPEG"
        )
    if media_type == "png" and getattr(source, "is_animated", False):
        raise ReceiptPageError("animated PNG receipts are not accepted; export a still image")
    try:
        source.load()
    except Exception:
        raise ReceiptPageError(_UNREADABLE) from None
    # A PNG's EXIF (an eXIf chunk, possibly after the pixels, or a text chunk) is parsed only by
    # getexif(): bound exactly what that would parse. A JPEG's was bounded before it was opened.
    exif = source.info.get("exif")
    profile = source.info.get("Raw profile type exif")
    exif_bytes = len(exif) if isinstance(exif, bytes) else 0
    if exif is None and isinstance(profile, str):
        exif_bytes = len(profile) // 2
    if exif_bytes > NORMALIZATION.max_exif_bytes:
        raise ReceiptPageError(_TOO_MUCH_METADATA)
    try:
        orientation = source.getexif().get(_EXIF_ORIENTATION, 1)
        if type(orientation) is not int or not 1 <= orientation <= 8:
            orientation = 1
        # Every step rebinds ``image``, so each full-size intermediate is released as soon as
        # the next one exists.
        image: Image.Image = source
        del source
        if orientation in _DISPLAY_TRANSPOSE:
            image = image.transpose(Image.Transpose[_DISPLAY_TRANSPOSE[orientation]])
        target = "L" if image.mode in _GRAY_MODES else "RGB"
        if image.has_transparency_data:
            rgba = image if image.mode == "RGBA" else image.convert("RGBA")
            del image
            alpha = rgba.getchannel("A")
            color = rgba.convert(target)
            del rgba
            # The background in the target mode: white stays 255 in L, exactly as converted.
            backdrop = Image.new("RGB", (1, 1), NORMALIZATION.background).convert(target)
            image = Image.new(target, color.size, backdrop.getpixel((0, 0)))
            image.paste(color, mask=alpha)
            del color, alpha
        elif image.mode != target:
            image = image.convert(target)
        long_edge = max(image.size)
        limit = NORMALIZATION.max_long_edge
        scale = 1.0
        if long_edge > limit:
            width, height = image.size
            # Uniform scale: the long side lands exactly on the limit, the short side rounds
            # half up.
            size = (_scaled(width, limit, long_edge), _scaled(height, limit, long_edge))
            image = image.resize(size, Image.Resampling.LANCZOS)
            scale = limit / long_edge
        clean = Image.frombytes(image.mode, image.size, image.tobytes())
        del image
        buffer = io.BytesIO()
        clean.save(
            buffer,
            format="JPEG",
            quality=NORMALIZATION.jpeg_quality,
            subsampling=NORMALIZATION.jpeg_subsampling,
            optimize=True,
        )
    except Exception:
        # Whatever a decoder raises on an untrusted upload is a per-asset refusal, never a
        # reason to abort the batch.
        raise ReceiptPageError(_UNREADABLE) from None
    return _Normalized(
        payload=buffer.getvalue(),
        width=clean.width,
        height=clean.height,
        scale=scale,
        source_width=source_width,
        source_height=source_height,
        orientation=orientation,
    )


def _scaled(side: int, limit: int, long_edge: int) -> int:
    return max(1, (side * limit * 2 + long_edge) // (long_edge * 2))


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
