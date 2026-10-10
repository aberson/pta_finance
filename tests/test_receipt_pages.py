from __future__ import annotations

import ast
import base64
import dataclasses
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import tomllib
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageCms, PngImagePlugin
from test_reimbursement_report import _bundle, _write_bundle

from pta_finance import (
    process_limits,
    receipt_decode,
    receipt_geometry,
    receipt_pages,
    receipt_viewer,
    reimbursement_report,
)

_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE = _ROOT / "pta_finance" / "reports" / "templates" / "receipt_viewer.js.j2"
_INJECTION = "</script><script>window.injected=true</script>"
_SUFFIXES = {"png": "png", "jpeg": "jpg", "pdf": "pdf"}
# A baseline JPEG page may hold only these segments: JFIF APP0, quantization and Huffman
# tables, the baseline frame header and the scan. No COM, APPn metadata or progressive frame.
_CLEAN_MARKERS = {0xE0, 0xDB, 0xC4, 0xC0, 0xDA}
_XMP_ORIENTATION_6 = (
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
    b'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
    b'xmlns:tiff="http://ns.adobe.com/tiff/1.0/" tiff:Orientation="6"/></rdf:RDF></x:xmpmeta>'
)
# What a stored image must undergo to be displayed, inverted: stored = displayed.transpose(...).
_STORED_FROM_DISPLAYED = {
    1: None,
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_90,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_270,
}
_NORMALIZATION_FIELDS = {
    "max_long_edge",
    "jpeg_quality",
    "jpeg_subsampling",
    "background",
    "max_source_bytes",
    "max_source_pixels",
    "max_source_edge",
    "memory_bytes",
    "cpu_seconds",
    "wall_seconds",
    "ready_seconds",
    "max_as_baseline",
}


@dataclass(frozen=True)
class _Asset:
    """A fictional stand-in for a fetched asset (``receipt_assets.AssetResult``)."""

    asset_id: str
    media_type: str
    path: Path | None


def _encode(image: Image.Image, fmt: str, **options: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def _asset(root: Path, data: bytes, media_type: str = "png") -> _Asset:
    """Cache fictional bytes the way the fetcher does: named by their own digest."""

    digest = hashlib.sha256(data).hexdigest()
    cache = root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{digest}.{_SUFFIXES.get(media_type, 'bin')}"
    path.write_bytes(data)
    return _Asset(f"asset:v1:{digest}", media_type, path)


def _receipt(size: tuple[int, int] = (400, 1000), mode: str = "L") -> Image.Image:
    """A fictional receipt: white paper with one dark line at 30-33% of the height."""

    width, height = size
    image = Image.new(mode, size, "white")
    image.paste("black", (width // 4, height * 30 // 100, width * 3 // 4, height * 33 // 100))
    return image


def _marked(size: tuple[int, int] = (120, 300)) -> Image.Image:
    """A fictional displayed page with a black block in its top-left corner."""

    image = Image.new("RGB", size, "white")
    image.paste("black", (0, 0, size[0] // 3, size[1] // 5))
    return image


def _pages(
    root: Path,
    asset: _Asset,
    *,
    ticket_ref: Any = "EX-01",
    asset_ordinal: Any = 1,
    progress: Callable[[receipt_pages.PageProgress], None] | None = None,
) -> list[receipt_pages.PageImage]:
    return receipt_pages.to_pages(
        asset,
        pages_dir=root / "receipt-pages" / "auto",
        ticket_ref=ticket_ref,
        asset_ordinal=asset_ordinal,
        progress=progress,
    )


def _mean(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    data = image.crop(box).convert("L").tobytes()
    return sum(data) / len(data)


def _files(root: Path) -> list[str]:
    """Every file outside the fictional asset cache, relative to the test root."""

    found = (path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    return sorted(name for name in found if not name.startswith("cache/"))


def _segments(payload: bytes) -> list[tuple[int, bytes]]:
    """Every JPEG segment before the entropy-coded scan, as (marker, body)."""

    assert payload[:2] == b"\xff\xd8"
    segments, index = [], 2
    while True:
        assert payload[index] == 0xFF
        marker = payload[index + 1]
        length = int.from_bytes(payload[index + 2 : index + 4], "big")
        segments.append((marker, payload[index + 4 : index + 2 + length]))
        if marker == 0xDA:
            return segments
        index += 2 + length


def _assert_clean_jpeg(payload: bytes) -> None:
    markers = [marker for marker, _ in _segments(payload)]
    assert set(markers) <= _CLEAN_MARKERS, f"unexpected JPEG segments {markers}"
    assert 0xC0 in markers, "the page must be a baseline JPEG"
    assert [body[:5] for marker, body in _segments(payload) if marker == 0xE0] == [b"JFIF\0"]


def _sampling(payload: bytes) -> int:
    """The first component's sampling factors from the baseline frame header."""

    (frame,) = [body for marker, body in _segments(payload) if marker == 0xC0]
    return frame[7]


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    crc = struct.pack(">I", zlib.crc32(kind + data))
    return struct.pack(">I", len(data)) + kind + data + crc


def _png_header(width: int, height: int, depth: int = 8) -> bytes:
    """A grayscale PNG claiming ``width`` x ``height`` pixels with almost no image data."""

    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, 0, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(b"\0"))
        + _png_chunk(b"IEND", b"")
    )


def _raw_exif(entries: list[tuple[int, int, int, bytes]], data: bytes = b"") -> bytes:
    """Fictional big-endian EXIF with one IFD of (tag, type, count, 4-byte value) entries.

    ``data`` follows the IFD; a value field may point into it (offsets from the TIFF header).
    """

    ifd = struct.pack(">H", len(entries))
    for tag, kind, count, value in entries:
        ifd += struct.pack(">HHI", tag, kind, count) + value.ljust(4, b"\0")
    return b"Exif\x00\x00MM\x00*" + struct.pack(">I", 8) + ifd + struct.pack(">I", 0) + data


def _exif_with_corrupt_gps_count() -> bytes:
    """Fictional EXIF (orientation 6) whose GPS directory claims 69 entries it lacks."""

    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010E] = "fictional"
    exif[0x8825] = {1: "N", 2: (1.0, 2.0, 3.0)}
    raw = bytearray(exif.tobytes())
    tiff = raw.index(b"Exif\x00\x00") + 6
    order = ">" if raw[tiff : tiff + 2] == b"MM" else "<"
    (ifd0,) = struct.unpack_from(order + "I", raw, tiff + 4)
    (count,) = struct.unpack_from(order + "H", raw, tiff + ifd0)
    for entry in range(count):
        offset = tiff + ifd0 + 2 + 12 * entry
        if struct.unpack_from(order + "H", raw, offset)[0] == 0x8825:
            (gps,) = struct.unpack_from(order + "I", raw, offset + 8)
            struct.pack_into(order + "H", raw, tiff + gps, 69)
            return bytes(raw)
    raise AssertionError("fixture has no GPS directory")


def _request(**changes: Any) -> receipt_decode.DecodeRequest:
    """The production request for a fictional 4 KiB PNG, with ``changes`` applied."""

    request = receipt_pages._request_for(bytes(4096), "png")
    return dataclasses.replace(request, **changes)


def _no_spawn(*args: object, **kwargs: object) -> None:
    raise AssertionError("a decode child must not be spawned")


# --- One source of truth -------------------------------------------------------------------


def test_geometry_and_page_budget_are_imported_not_restated() -> None:
    # Identity, not equality, on objects CPython never shares by accident: a restated copy of
    # either fails here.
    assert receipt_pages.NORMALIZATION is receipt_geometry.NORMALIZATION
    assert receipt_pages.MAX_PAGE_BYTES is receipt_viewer.MAX_PAGE_BYTES
    assert receipt_pages.scaled_size is receipt_decode.scaled_size
    normalization = receipt_geometry.NORMALIZATION
    assert {field.name for field in dataclasses.fields(normalization)} == _NORMALIZATION_FIELDS
    assert (normalization.max_long_edge, normalization.jpeg_quality) == (2200, 85)
    assert normalization.background == (255, 255, 255)
    assert normalization.max_source_bytes == 26_214_400
    assert (normalization.max_source_pixels, normalization.max_source_edge) == (80_000_000, 65_535)
    assert (normalization.ready_seconds, normalization.max_as_baseline) == (5, 256 * 1024 * 1024)
    # The explicit ceiling, not Pillow's own bomb warning, is what refuses an oversize header.
    assert Image.MAX_IMAGE_PIXELS is not None
    assert normalization.max_source_pixels < Image.MAX_IMAGE_PIXELS


def test_the_request_carries_every_child_value_from_the_shared_instance() -> None:
    request = receipt_pages._request_for(bytes(10), "jpeg")
    shared = receipt_geometry.NORMALIZATION
    sent = json.loads(request.line())
    assert set(sent) == receipt_decode.REQUEST_KEYS
    assert sent == {
        "protocol": "receipt-decode/1",
        "media_type": "jpeg",
        "byte_count": 10,
        "max_source_bytes": shared.max_source_bytes,
        "max_source_pixels": shared.max_source_pixels,
        "max_source_edge": shared.max_source_edge,
        "max_long_edge": shared.max_long_edge,
        "background": list(shared.background),
        "memory_bytes": shared.memory_bytes,
        "cpu_seconds": shared.cpu_seconds,
    }
    # The broker-only values never leave the broker.
    assert not {"wall_seconds", "ready_seconds", "max_as_baseline"} & set(sent)
    assert receipt_decode.parse_request(request.line()) == request


def test_normalization_values_are_read_from_the_shared_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A producer that restated any value inline would ignore these replacements; the child
    # applies them only because the broker sends them in the request.
    picture = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
    picture.paste((0, 0, 0, 255), (200, 0, 400, 200))
    asset = _asset(tmp_path, _encode(picture, "PNG"))
    shared = receipt_geometry.NORMALIZATION

    def run(name: str, **changes: Any) -> receipt_pages.PageImage:
        monkeypatch.setattr(receipt_pages, "NORMALIZATION", dataclasses.replace(shared, **changes))
        (page,) = receipt_pages.to_pages(
            asset, pages_dir=tmp_path / name, ticket_ref="EX-01", asset_ordinal=1
        )
        return page

    page = run("small", max_long_edge=100, background=(255, 0, 0), jpeg_subsampling="4:4:4")
    with Image.open(page.path) as output:
        assert output.size == (100, 50)
        red, green, blue = output.convert("RGB").getpixel((20, 25))  # type: ignore[misc]
        assert red > 230 and green < 40 and blue < 40
    assert _sampling(page.path.read_bytes()) == 0x11
    low = run("low", jpeg_quality=20).path.stat().st_size
    assert low < run("high", jpeg_quality=95).path.stat().st_size


def test_box_padding_mirrors_the_viewer_template() -> None:
    template = _TEMPLATE.read_text(encoding="utf-8")
    number = r"(\d*\.?\d+)"
    rx = re.findall(
        rf"setAttribute\('rx', Math\.min\(width \* {number} \+ {number}, cx, 1 - cx\)\)", template
    )
    ry = re.findall(
        rf"setAttribute\('ry', Math\.min\(height \* {number} \+ {number}, cy, 1 - cy\)\)", template
    )
    assert len(rx) == 1 and len(ry) == 1, "viewer ellipse inflation changed shape"
    padding = receipt_geometry.BOX_PADDING
    assert (float(rx[0][0]), float(rx[0][1])) == (padding.rx_scale, padding.rx_pad)
    assert (float(ry[0][0]), float(ry[0][1])) == (padding.ry_scale, padding.ry_pad)


@pytest.mark.parametrize(
    ("source", "orientation", "expected"),
    [
        ((4032, 3024), 6, (1650, 2200, 2200 / 4032)),
        ((4032, 3024), 1, (2200, 1650, 2200 / 4032)),
        ((2200, 1000), 8, (1000, 2200, 1.0)),
        ((2201, 1000), 1, (2200, 1000, 2200 / 2201)),
        ((1, 44_700_000), 1, (1, 2200, 2200 / 44_700_000)),
    ],
    ids=["phone-rotated", "phone", "at-limit-rotated", "one-over", "strip"],
)
def test_scaled_size_is_the_one_size_derivation(
    source: tuple[int, int], orientation: int, expected: tuple[int, int, float]
) -> None:
    assert receipt_decode.scaled_size(*source, orientation, 2200) == expected


# --- Normalization through the real decode child ---------------------------------------------


@pytest.mark.parametrize(
    "orientation", sorted(_STORED_FROM_DISPLAYED), ids=lambda value: f"orientation-{value}"
)
def test_rotated_exif_jpeg_normalizes_to_displayed_orientation(
    tmp_path: Path, orientation: int
) -> None:
    displayed = _marked()
    transpose = _STORED_FROM_DISPLAYED[orientation]
    stored = displayed if transpose is None else displayed.transpose(transpose)
    exif = Image.Exif()
    exif[0x0112] = orientation
    asset = _asset(tmp_path, _encode(stored, "JPEG", quality=95, exif=exif.tobytes()), "jpeg")
    events: list[receipt_pages.PageProgress] = []
    (page,) = _pages(tmp_path, asset, progress=events.append)
    payload = page.path.read_bytes()
    _assert_clean_jpeg(payload)  # no orientation tag left for a browser to apply again
    with Image.open(page.path) as output:
        assert output.size == (120, 300) == (page.width, page.height)
        assert _mean(output, (4, 4, 36, 56)) < 40
        for corner in ((84, 4, 116, 56), (4, 244, 36, 296), (84, 244, 116, 296)):
            assert _mean(output, corner) > 220
    (event,) = events
    assert event.orientation == orientation
    assert (event.source_width, event.source_height) == stored.size


@pytest.mark.parametrize("carrier", ["png-exif", "jpeg-xmp"])
def test_png_exif_and_xmp_orientation_are_applied_and_recorded(
    tmp_path: Path, carrier: str
) -> None:
    stored = _marked().transpose(Image.Transpose.ROTATE_90)
    if carrier == "png-exif":
        exif = Image.Exif()
        exif[0x0112] = 6
        asset = _asset(tmp_path, _encode(stored, "PNG", exif=exif.tobytes()))
    else:
        data = _encode(stored, "JPEG", quality=95, xmp=_XMP_ORIENTATION_6)
        asset = _asset(tmp_path, data, "jpeg")
    events: list[receipt_pages.PageProgress] = []
    (page,) = _pages(tmp_path, asset, progress=events.append)
    with Image.open(page.path) as output:
        assert output.size == (120, 300)
        assert _mean(output, (4, 4, 36, 56)) < 40
    assert events[0].orientation == 6


def test_a_corrupt_gps_directory_still_normalizes_upright(tmp_path: Path) -> None:
    # Regression: this GPS directory made Pillow's exif_transpose raise TypeError while it
    # re-serialised EXIF. The child reads the orientation only, never re-serialises EXIF.
    stored = _marked().transpose(Image.Transpose.ROTATE_90)
    data = _encode(stored, "JPEG", quality=95, exif=_exif_with_corrupt_gps_count())
    events: list[receipt_pages.PageProgress] = []
    (page,) = _pages(tmp_path, _asset(tmp_path, data, "jpeg"), progress=events.append)
    with Image.open(page.path) as output:
        assert output.size == (120, 300)
        assert _mean(output, (4, 4, 36, 56)) < 40
    assert events[0].orientation == 6


def _undecodable(kind: str) -> tuple[bytes, str]:
    good_png = _encode(_receipt(), "PNG")
    good_jpeg = _encode(_receipt((400, 1000), "RGB"), "JPEG", quality=90)
    if kind == "corrupt-zlib-stream":
        # A valid header and a CRC-correct IDAT whose deflate stream is fictional noise.
        noise = hashlib.shake_256(b"fictional-idat").digest(4096)
        start = good_png.index(b"IDAT") - 4
        end = start + 12 + int.from_bytes(good_png[start : start + 4], "big")
        return good_png[:start] + _png_chunk(b"IDAT", noise) + good_png[end:], "png"
    if kind == "truncated-jpeg-scan":
        return good_jpeg[: len(good_jpeg) * 2 // 3], "jpeg"
    if kind == "jpeg-without-a-frame":
        return b"\xff\xd8" + good_jpeg[2:20] + b"\xff\xd9", "jpeg"
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind", ["corrupt-zlib-stream", "truncated-jpeg-scan", "jpeg-without-a-frame"]
)
def test_any_decoder_failure_is_a_per_asset_refusal(tmp_path: Path, kind: str) -> None:
    # Crafted bytes, not a patched decoder: a patch in this process could never reach the child.
    data, media_type = _undecodable(kind)
    with pytest.raises(receipt_pages.ReceiptPageError, match="not a readable image") as caught:
        _pages(tmp_path, _asset(tmp_path, data, media_type))
    assert caught.value.reason == "unreadable"
    assert caught.value.usage is not None, "the child ran and was measured"
    assert _files(tmp_path) == []


@pytest.mark.parametrize(
    "entry",
    [
        (0x0112, 3, 1, struct.pack(">H", 9)),
        (0x0112, 3, 1, struct.pack(">H", 0)),
        (0x0112, 2, 2, b"6\0"),
        (0x0112, 5, 1, struct.pack(">I", 26)),  # RATIONAL 6/1, stored after the IFD
    ],
    ids=["nine", "zero", "text", "rational"],
)
def test_an_orientation_outside_integers_one_to_eight_is_ignored_and_recorded_as_one(
    tmp_path: Path, entry: tuple[int, int, int, bytes]
) -> None:
    exif = _raw_exif([entry], struct.pack(">II", 6, 1))
    stored = _marked().transpose(Image.Transpose.ROTATE_90)
    events: list[receipt_pages.PageProgress] = []
    data = _encode(stored, "JPEG", quality=95, exif=exif)
    (page,) = _pages(tmp_path, _asset(tmp_path, data, "jpeg"), progress=events.append)
    assert (page.width, page.height) == stored.size, "applied and recorded orientation agree"
    assert events[0].orientation == 1 and type(events[0].orientation) is int


def test_two_runs_produce_byte_identical_pages(tmp_path: Path) -> None:
    picture = _receipt((2600, 900), "RGBA")
    picture.putalpha(Image.linear_gradient("L").resize((2600, 900)))
    asset = _asset(tmp_path, _encode(picture, "PNG"))
    (first,) = receipt_pages.to_pages(
        asset, pages_dir=tmp_path / "one", ticket_ref="EX-01", asset_ordinal=1
    )
    (second,) = receipt_pages.to_pages(
        asset, pages_dir=tmp_path / "two", ticket_ref="EX-01", asset_ordinal=1
    )
    assert first.path.read_bytes() == second.path.read_bytes()
    assert {**vars(first), "path": None} == {**vars(second), "path": None}
    script = (
        "import sys\n"
        "from dataclasses import dataclass\n"
        "from pathlib import Path\n"
        "from pta_finance import receipt_pages\n"
        "@dataclass(frozen=True)\n"
        "class Asset:\n"
        "    asset_id: str\n"
        "    media_type: str\n"
        "    path: Path\n"
        "(page,) = receipt_pages.to_pages(Asset(sys.argv[1], 'png', Path(sys.argv[2])),\n"
        "    pages_dir=Path(sys.argv[3]), ticket_ref='EX-01', asset_ordinal=1)\n"
        "print(page.sha256)\n"
    )
    assert asset.path is not None
    fresh = subprocess.run(
        [sys.executable, "-c", script, asset.asset_id, str(asset.path), str(tmp_path / "three")],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONHASHSEED": "7"},
    )
    assert fresh.stdout.strip() == first.sha256
    assert (tmp_path / "three" / "EX-01-a1-p1.jpg").read_bytes() == first.path.read_bytes()


@pytest.mark.parametrize(
    ("size", "expected", "scale"),
    [
        ((300, 120), (300, 120), 1.0),
        ((2200, 1400), (2200, 1400), 1.0),
        ((2201, 1000), (2200, 1000), 2200 / 2201),
        ((4400, 1000), (2200, 500), 0.5),
        ((1000, 4400), (500, 2200), 0.5),
    ],
    ids=["small", "at-limit", "one-over", "wide", "tall"],
)
def test_downscales_only_above_the_long_edge_limit(
    tmp_path: Path, size: tuple[int, int], expected: tuple[int, int], scale: float
) -> None:
    (page,) = _pages(tmp_path, _asset(tmp_path, _encode(_receipt(size), "PNG")))
    with Image.open(page.path) as output:
        assert output.size == expected == (page.width, page.height)
    assert page.scale == scale


def test_downscale_keeps_aspect_and_content_placement(tmp_path: Path) -> None:
    (page,) = _pages(tmp_path, _asset(tmp_path, _encode(_receipt((4400, 1000)), "PNG")))
    with Image.open(page.path) as output:
        width, height = output.size
        pixels = output.convert("L").tobytes()
    assert width / height == pytest.approx(4.4, rel=1e-3)
    dark_rows = [y for y in range(height) if pixels[y * width + width // 2] < 128]
    middle = (dark_rows[0] + dark_rows[-1]) // 2
    dark_columns = [x for x in range(width) if pixels[middle * width + x] < 128]
    # The source line spans [0.25, 0.30, 0.50, 0.03]; the page box must still say so.
    assert dark_rows[0] / height == pytest.approx(0.30, abs=0.005)
    assert (dark_rows[-1] + 1) / height == pytest.approx(0.33, abs=0.005)
    assert dark_columns[0] / width == pytest.approx(0.25, abs=0.005)
    assert (dark_columns[-1] + 1) / width == pytest.approx(0.75, abs=0.005)


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_alpha_is_flattened_onto_white(tmp_path: Path, mode: str) -> None:
    picture = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    picture.paste((0, 0, 0, 255), (100, 0, 200, 100))
    if mode == "LA":
        source = picture.convert("LA")
    elif mode == "P":
        # Index 0 is red and transparent: ignoring the transparency would leave red, not white.
        source = Image.new("P", (200, 100), 0)
        source.putpalette([255, 0, 0, 0, 0, 0])
        source.paste(1, (100, 0, 200, 100))
    else:
        source = picture
    options = {"transparency": 0} if mode == "P" else {}
    (page,) = _pages(tmp_path, _asset(tmp_path, _encode(source, "PNG", **options)))
    with Image.open(page.path) as output:
        assert output.mode == ("L" if mode == "LA" else "RGB")
        assert _mean(output, (5, 5, 95, 95)) > 245
        assert _mean(output, (105, 5, 195, 95)) < 10


@pytest.mark.parametrize("source", ["cmyk", "mpo"])
def test_cmyk_jpeg_and_the_primary_mpo_picture_become_rgb_pages(
    tmp_path: Path, source: str
) -> None:
    if source == "cmyk":
        picture = Image.new("CMYK", (200, 100), (0, 0, 0, 0))
        picture.paste((0, 0, 0, 255), (100, 0, 200, 100))
        data = _encode(picture, "JPEG", quality=95)
    else:
        primary = Image.new("RGB", (200, 100), "white")
        primary.paste("black", (100, 0, 200, 100))
        second = Image.new("RGB", (200, 100), "black")
        data = _encode(primary, "MPO", save_all=True, append_images=[second], quality=95)
    (page,) = _pages(tmp_path, _asset(tmp_path, data, "jpeg"))
    _assert_clean_jpeg(page.path.read_bytes())
    with Image.open(page.path) as output:
        assert (output.mode, output.size) == ("RGB", (200, 100))
        assert _mean(output, (5, 5, 95, 95)) > 240
        assert _mean(output, (105, 5, 195, 95)) < 15


@pytest.mark.parametrize("source", ["jpeg", "png"])
def test_source_metadata_never_reaches_the_page(tmp_path: Path, source: str) -> None:
    exif = Image.Exif()
    exif[0x010E] = _INJECTION
    exif[0x8825] = {1: "N", 2: (1.0, 2.0, 3.0)}  # a fictional GPS directory
    xmp = b"<x:xmpmeta><rdf:Description>" + _INJECTION.encode() + b"</rdf:Description></x:xmpmeta>"
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    picture = _receipt((400, 1000), "RGB")
    if source == "jpeg":
        data = _encode(
            picture,
            "JPEG",
            quality=95,
            comment=_INJECTION.encode(),
            exif=exif.tobytes(),
            xmp=xmp,
            icc_profile=profile,
        )
    else:
        text = PngImagePlugin.PngInfo()
        text.add_text("comment", _INJECTION)  # the key Pillow's JPEG writer falls back to
        text.add_text("Comment", _INJECTION)
        text.add_text("Description", _INJECTION, zip=True)
        text.add_itxt("XML:com.adobe.xmp", xmp.decode())
        data = _encode(picture, "PNG", pnginfo=text, exif=exif.tobytes(), icc_profile=profile)
    (page,) = _pages(tmp_path, _asset(tmp_path, data, source))
    payload = page.path.read_bytes()
    _assert_clean_jpeg(payload)
    for needle in (_INJECTION.encode(), b"window.injected", b"Exif\x00", b"ICC_PROFILE"):
        assert needle not in payload
    with Image.open(page.path) as output:
        assert not {"comment", "exif", "icc_profile", "xmp", "progressive"} & set(output.info)


def test_bilevel_pages_are_flattened_before_a_lanczos_downscale(tmp_path: Path) -> None:
    # One-pixel stripes: LANCZOS on 8-bit gray averages them; a bilevel or nearest-neighbour
    # resize would leave pure black and white.
    stripes = Image.new("1", (4400, 100), 1)
    for x in range(0, 4400, 2):
        stripes.paste(0, (x, 0, x + 1, 100))
    (page,) = _pages(tmp_path, _asset(tmp_path, _encode(stripes, "PNG")))
    with Image.open(page.path) as output:
        assert output.size == (2200, 50)
        middle = output.crop((100, 10, 2100, 40)).tobytes()
    assert sum(64 <= value <= 192 for value in middle) > 0.9 * len(middle)


def test_pages_round_trip_through_the_production_sidecar_loader(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.json"
    _write_bundle(bundle, _bundle())
    report = reimbursement_report.load_bundle(bundle)
    ticket = report.tickets[0]
    asset = _asset(tmp_path, _encode(_receipt((2600, 3400)), "PNG"))
    (page,) = _pages(tmp_path, asset, asset_ordinal=2)
    assert (page.page_id, page.asset_page, page.path.name) == ("EX-01-a2-p1", 1, "EX-01-a2-p1.jpg")
    assert page.label == "Ticket EX-01 · upload 2 · page 1"
    assert page.sha256 == hashlib.sha256(page.path.read_bytes()).hexdigest()
    sidecar = bundle.with_suffix(".receipts.json")
    entry = {
        "id": page.page_id,
        "path": page.path.relative_to(tmp_path.resolve()).as_posix(),
        "sha256": page.sha256,
        "label": page.label,
    }
    item = {
        "review_key": ticket.review_key,
        "item_key": ticket.items[0].item_key,
        "item_sha256": receipt_viewer.item_fingerprint(ticket, ticket.items[0]),
        "regions": [{"page_id": page.page_id, "box": None}],
    }
    sidecar.write_text(
        json.dumps({"schema_version": 1, "pages": [entry], "items": [item]}), encoding="utf-8"
    )
    assert entry["path"] == "receipt-pages/auto/EX-01-a2-p1.jpg"
    loaded = receipt_viewer.load_receipts(sidecar, report)
    source = loaded["pages"][page.page_id]["src"]
    assert source.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(source.split(",", 1)[1]) == page.path.read_bytes()
    assert receipt_viewer.headroom_bytes(sidecar) == (
        receipt_viewer.MAX_TOTAL_BYTES - page.path.stat().st_size
    )
    output = tmp_path / "report.html"
    result = reimbursement_report.build_report(bundle, output)
    rendered = output.read_text(encoding="utf-8")
    assert result.receipt_linked_items == 1
    assert "View source receipt" in rendered
    assert str(tmp_path) not in rendered


def test_progress_is_reported_per_page_after_the_page_lands(tmp_path: Path) -> None:
    seen: list[receipt_pages.PageProgress] = []

    def record(event: receipt_pages.PageProgress) -> None:
        assert event.page.path.is_file(), "progress must follow the published page"
        assert hashlib.sha256(event.page.path.read_bytes()).hexdigest() == event.page.sha256
        seen.append(event)

    data = _encode(_receipt((3000, 1200)), "PNG")
    pages = _pages(tmp_path, _asset(tmp_path, data), progress=record)
    assert [event.page for event in seen] == pages
    (event,) = seen
    assert (event.page_count, event.orientation) == (1, 1)
    assert (event.source_width, event.source_height) == (3000, 1200)
    assert (event.page.width, event.page.height, event.page.scale) == (2200, 880, 2200 / 3000)
    assert event.byte_count == event.page.path.stat().st_size
    usage = event.decode_usage
    assert usage is not None and usage.wall_seconds > 0 and usage.cpu_seconds >= 0
    if sys.platform == "win32":
        assert usage.peak_memory_bytes is not None and usage.peak_memory_bytes > 0
    else:
        assert usage.peak_memory_bytes is None


def test_failed_fetch_yields_zero_pages_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[receipt_pages.PageProgress] = []
    monkeypatch.setattr(receipt_pages.subprocess, "Popen", _no_spawn)
    asset = _Asset("asset:v1:" + "0" * 64, "png", None)
    assert _pages(tmp_path, asset, progress=seen.append) == []
    assert not seen
    assert not (tmp_path / "receipt-pages").exists()


def test_rerun_is_a_no_op_and_a_different_page_is_never_replaced(tmp_path: Path) -> None:
    (page,) = _pages(tmp_path, _asset(tmp_path, _encode(_receipt(), "PNG")))
    before = page.path.stat().st_mtime_ns, page.path.read_bytes()
    assert _pages(tmp_path, _asset(tmp_path, _encode(_receipt(), "PNG"))) == [page]
    assert (page.path.stat().st_mtime_ns, page.path.read_bytes()) == before
    other = _asset(tmp_path, _encode(_receipt((500, 1000)), "PNG"))
    with pytest.raises(receipt_pages.ReceiptPageConflictError, match="EX-01-a1-p1.jpg") as caught:
        _pages(tmp_path, other)
    assert isinstance(caught.value, receipt_pages.ReceiptPageError), "conflicts are per-asset"
    assert caught.value.reason == "conflict"
    assert str(tmp_path) not in str(caught.value)
    assert page.path.read_bytes() == before[1]
    assert _files(tmp_path) == ["receipt-pages/auto/EX-01-a1-p1.jpg"]


def test_a_page_published_mid_write_is_compared_never_clobbered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rival = b"a fictional page another run published first"
    real_link = os.link

    def racing_link(source: str, destination: str) -> None:
        Path(destination).write_bytes(rival)
        real_link(source, destination)

    monkeypatch.setattr(receipt_pages.os, "link", racing_link)
    with pytest.raises(receipt_pages.ReceiptPageConflictError, match="refusing to replace"):
        _pages(tmp_path, _asset(tmp_path, _encode(_receipt(), "PNG")))
    page = tmp_path / "receipt-pages" / "auto" / "EX-01-a1-p1.jpg"
    assert page.read_bytes() == rival
    assert _files(tmp_path) == ["receipt-pages/auto/EX-01-a1-p1.jpg"]


def test_a_page_published_mid_write_with_identical_bytes_is_a_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link

    def racing_link(source: str, destination: str) -> None:
        Path(destination).write_bytes(Path(source).read_bytes())
        real_link(source, destination)

    asset = _asset(tmp_path, _encode(_receipt(), "PNG"))
    monkeypatch.setattr(receipt_pages.os, "link", racing_link)
    (page,) = _pages(tmp_path, asset)
    assert page.path.name == "EX-01-a1-p1.jpg"
    assert _files(tmp_path) == ["receipt-pages/auto/EX-01-a1-p1.jpg"]


def test_a_symlink_at_the_page_path_is_refused_even_with_identical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Simulated rather than created, so the case runs on hosts that cannot make symlinks: the
    # entry holds the identical page, which would otherwise be a no-op.
    asset = _asset(tmp_path, _encode(_receipt(), "PNG"))
    (page,) = _pages(tmp_path, asset)
    monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == page.path.name)
    with pytest.raises(receipt_pages.ReceiptPageConflictError, match="refusing to replace"):
        _pages(tmp_path, asset)


def test_ticket_refs_differing_only_in_letter_case_never_share_a_page(tmp_path: Path) -> None:
    asset = _asset(tmp_path, _encode(_receipt(), "PNG"))
    (page,) = _pages(tmp_path, asset, ticket_ref="EX-01")
    with pytest.raises(receipt_pages.ReceiptPageConflictError, match="letter case"):
        _pages(tmp_path, asset, ticket_ref="ex-01")
    assert _files(tmp_path) == ["receipt-pages/auto/EX-01-a1-p1.jpg"]
    assert page.path.read_bytes()


def test_a_failed_temporary_file_cleanup_never_changes_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(path: str) -> None:
        raise PermissionError("fictional sharing violation")

    asset = _asset(tmp_path, _encode(_receipt(), "PNG"))
    monkeypatch.setattr(receipt_pages.os, "unlink", locked)
    seen: list[receipt_pages.PageProgress] = []
    (page,) = _pages(tmp_path, asset, progress=seen.append)
    assert [event.page for event in seen] == [page] and page.path.is_file()


def test_an_unusable_pages_directory_aborts_with_its_own_error(tmp_path: Path) -> None:
    (tmp_path / "receipt-pages").write_text("a file where the pages directory belongs")
    with pytest.raises(receipt_pages.ReceiptPagesDirectoryError, match="not writable") as caught:
        _pages(tmp_path, _asset(tmp_path, _encode(_receipt(), "PNG")))
    assert not isinstance(caught.value, receipt_pages.ReceiptPageError), "must abort the stage"
    assert str(tmp_path) not in str(caught.value)


def test_a_pages_directory_without_hard_links_aborts_naming_the_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(source: str, destination: str) -> None:
        raise OSError("fictional: this filesystem has no hard links")

    asset = _asset(tmp_path, _encode(_receipt(), "PNG"))
    monkeypatch.setattr(receipt_pages.os, "link", unsupported)
    with pytest.raises(receipt_pages.ReceiptPagesDirectoryError, match="hard links") as caught:
        _pages(tmp_path, asset)
    assert "fictional" not in str(caught.value) and str(tmp_path) not in str(caught.value)


# --- Per-asset refusals ----------------------------------------------------------------------

_REFUSALS = {
    "pdf": ("PDF receipt assets", "pdf-not-rendered"),
    "unknown_type": ("must be PNG, JPEG or PDF", "unreadable"),
    "digest": ("no longer matches its asset id", "digest-mismatch"),
    "malformed_id": ("asset id is malformed", "digest-mismatch"),
    "missing_file": ("missing or unreadable", "digest-mismatch"),
    "unsafe_ref": ("safe receipt page id", "bad-ticket-ref"),
    "over_source_byte_cap": ("larger than the source size cap", "too-large"),
    "svg_as_png": ("not a readable image", "unreadable"),
    "text_as_jpeg": ("not a readable image", "unreadable"),
    "png_as_jpeg": ("not a readable image", "unreadable"),
    "truncated": ("not a readable image", "unreadable"),
    "over_pixel_ceiling": ("too many pixels", "too-many-pixels"),
    "header_over_ceiling": ("too many pixels", "too-many-pixels"),
    "pillow_bomb": ("too many pixels", "too-many-pixels"),
    "over_edge_ceiling": ("side too long", "source-edge"),
    "sixteen_bit": ("unsupported pixel format", "unsupported-mode"),
    "animated_png": ("animated PNG", "animated"),
}
# These are refused before any decode child exists.
_PRE_SPAWN = {
    "pdf",
    "unknown_type",
    "digest",
    "malformed_id",
    "missing_file",
    "unsafe_ref",
    "over_source_byte_cap",
}


@pytest.mark.parametrize("problem", sorted(_REFUSALS))
def test_refusals_name_the_problem_and_write_no_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    good = _encode(_receipt(), "PNG")
    asset = _asset(tmp_path, good)
    options: dict[str, Any] = {}
    shared = receipt_geometry.NORMALIZATION
    if problem == "pdf":
        asset = _asset(tmp_path, b"%PDF-1.7\n% fictional\n", "pdf")
    elif problem == "unknown_type":
        asset = _asset(tmp_path, b"GIF89a fictional", "gif")
    elif problem == "digest":
        asset = _Asset("asset:v1:" + "0" * 64, "png", asset.path)
    elif problem == "malformed_id":
        asset = _Asset("asset:v1:not-a-digest", "png", asset.path)
    elif problem == "missing_file":
        asset = _Asset(asset.asset_id, "png", tmp_path / "cache" / "absent.png")
    elif problem == "unsafe_ref":
        options["ticket_ref"] = "../EX-01"
    elif problem == "over_source_byte_cap":
        lowered = dataclasses.replace(shared, max_source_bytes=len(good) - 1)
        monkeypatch.setattr(receipt_pages, "NORMALIZATION", lowered)
    elif problem == "svg_as_png":
        asset = _asset(tmp_path, b'<svg onload="alert(1)"></svg>')
    elif problem == "text_as_jpeg":
        asset = _asset(tmp_path, b"fictional plain text", "jpeg")
    elif problem == "png_as_jpeg":
        asset = _asset(tmp_path, good, "jpeg")
    elif problem == "truncated":
        asset = _asset(tmp_path, good[: len(good) // 2])
    elif problem == "over_pixel_ceiling":
        # A valid, decodable page: only the ceiling the request carries can refuse it.
        lowered = dataclasses.replace(shared, max_source_pixels=400 * 1000 - 1)
        monkeypatch.setattr(receipt_pages, "NORMALIZATION", lowered)
    elif problem == "header_over_ceiling":
        asset = _asset(tmp_path, _png_header(9000, 9000))  # 81 MP, below Pillow's own error
    elif problem == "pillow_bomb":
        asset = _asset(tmp_path, _png_header(15000, 15000))  # Pillow's own bomb error
    elif problem == "over_edge_ceiling":
        asset = _asset(tmp_path, _png_header(70_000, 1))  # few pixels, one long side
    elif problem == "sixteen_bit":
        # Header only: refused from the header, before any pixel could be decoded.
        asset = _asset(tmp_path, _png_header(40, 40, depth=16))
    elif problem == "animated_png":
        # Browsers would show the animation; one fixed frame is not evidence of what they show.
        still, frame = Image.new("RGB", (40, 20), "white"), Image.new("RGB", (40, 20), "black")
        asset = _asset(tmp_path, _encode(still, "PNG", save_all=True, append_images=[frame]))
    if problem in _PRE_SPAWN:
        monkeypatch.setattr(receipt_pages.subprocess, "Popen", _no_spawn)
    message, reason = _REFUSALS[problem]
    with pytest.raises(receipt_pages.ReceiptPageError, match=message) as caught:
        _pages(tmp_path, asset, **options)
    assert caught.value.reason == reason
    assert (caught.value.usage is None) == (problem in _PRE_SPAWN)
    assert str(tmp_path) not in str(caught.value)
    assert _files(tmp_path) == []


def test_a_cached_file_one_byte_over_the_source_cap_is_refused_before_any_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = receipt_geometry.NORMALIZATION.max_source_bytes
    over = _asset(tmp_path, bytes(cap + 1))
    monkeypatch.setattr(receipt_pages.subprocess, "Popen", _no_spawn)
    with pytest.raises(receipt_pages.ReceiptPageError, match="source size cap") as caught:
        _pages(tmp_path, over)
    assert caught.value.reason == "too-large" and caught.value.usage is None
    # Exactly at the cap the file is read and reaches the child, which refuses its bytes.
    monkeypatch.undo()
    with pytest.raises(receipt_pages.ReceiptPageError) as at_cap:
        _pages(tmp_path, _asset(tmp_path, bytes(cap)))
    assert at_cap.value.reason == "unreadable"


def test_a_page_over_the_viewer_page_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(receipt_pages, "MAX_PAGE_BYTES", 64)
    with pytest.raises(receipt_pages.ReceiptPageError, match="page limit"):
        _pages(tmp_path, _asset(tmp_path, _encode(_receipt(), "PNG")))
    assert _files(tmp_path) == []


@pytest.mark.parametrize("ordinal", [0, -1, True, 1.0], ids=["zero", "negative", "bool", "float"])
def test_an_invalid_asset_ordinal_is_a_caller_bug_never_a_recorded_refusal(
    tmp_path: Path, ordinal: object
) -> None:
    with pytest.raises(ValueError, match="asset_ordinal") as caught:
        _pages(tmp_path, _asset(tmp_path, _encode(_receipt(), "PNG")), asset_ordinal=ordinal)
    assert not isinstance(caught.value, receipt_pages.ReceiptPageError)
    assert _files(tmp_path) == []


def test_every_refusal_reason_is_a_closed_ledger_value() -> None:
    assert receipt_decode.REFUSAL_REASONS <= receipt_pages.REFUSAL_REASONS
    assert {"budget", "child-error", "failed-validation", "too-large"} <= (
        receipt_pages.REFUSAL_REASONS
    )
    with pytest.raises(ValueError):
        receipt_pages.ReceiptPageError("fictional", reason="paged")


# --- The decode child cannot start: a stage abort, never N per-asset refusals --------------


def test_an_unsupported_host_aborts_before_any_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = _asset(tmp_path, _encode(_receipt(), "PNG"))
    monkeypatch.setattr(receipt_pages.subprocess, "Popen", _no_spawn)
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(receipt_pages.ReceiptDecodeUnavailableError, match="Windows or Linux") as e:
        _pages(tmp_path, asset)
    assert not isinstance(e.value, (receipt_pages.ReceiptPageError, OSError, ValueError))
    monkeypatch.undo()
    assert _files(tmp_path) == []


@pytest.mark.parametrize("state", ["missing", "outdated"])
def test_a_missing_or_outdated_pillow_aborts_naming_the_receipts_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    asset = _asset(tmp_path, _encode(_receipt(), "PNG"))
    monkeypatch.setattr(receipt_pages.subprocess, "Popen", _no_spawn)
    if state == "missing":
        monkeypatch.setitem(sys.modules, "PIL", None)
    else:
        monkeypatch.setattr(sys.modules["PIL"], "__version__", "12.1.0")
    with pytest.raises(receipt_pages.ReceiptDecodeUnavailableError, match="'receipts' extra"):
        _pages(tmp_path, asset)
    assert _files(tmp_path) == []


def test_a_pillow_mismatch_on_the_ready_line_aborts_the_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real child reports its own Pillow; a broker expecting another build must not send it
    # a byte, because the pages would no longer be byte-identical.
    asset = _asset(tmp_path, _encode(_receipt(), "PNG"))
    monkeypatch.setattr(receipt_pages, "_broker_pillow_version", lambda: "99.0.0")
    with pytest.raises(receipt_pages.ReceiptDecodeUnavailableError, match="different Pillow"):
        _pages(tmp_path, asset)
    assert _files(tmp_path) == []


@pytest.mark.parametrize("media_type", ["png", "jpeg"])
def test_an_empty_asset_is_a_per_asset_refusal_before_any_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, media_type: str
) -> None:
    # Its asset id is the digest of no bytes, so only the broker's own check can refuse it; the
    # child would refuse byte_count 0 before its ready line, which is a stage abort.
    empty = _asset(tmp_path, b"", media_type)
    assert empty.asset_id == "asset:v1:" + hashlib.sha256(b"").hexdigest()
    monkeypatch.setattr(receipt_pages.subprocess, "Popen", _no_spawn)
    with pytest.raises(receipt_pages.ReceiptPageError, match="not a readable image") as caught:
        _pages(tmp_path, empty)
    assert caught.value.reason == "unreadable" and caught.value.usage is None
    assert _files(tmp_path) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory_bytes", 0),
        ("memory_bytes", 256 * 1024 * 1024 + 1),
        ("cpu_seconds", 0),
        ("max_long_edge", 0),
        ("max_source_pixels", 0),
        ("max_source_edge", 0),
        ("background", (256, 255, 255)),
    ],
    ids=[
        "no-memory",
        "memory-not-whole-pages",
        "no-cpu",
        "no-long-edge",
        "no-pixel-ceiling",
        "no-edge-ceiling",
        "background-out-of-range",
    ],
)
def test_limits_the_child_would_refuse_abort_the_stage_before_any_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    # The broker runs the child's own request validator first, so a value the child would
    # refuse before its ready line never reaches a spawn, and it is a configuration fault for
    # the whole stage rather than a refusal charged to this asset.
    asset = _asset(tmp_path, _encode(_receipt(), "PNG"))
    changed = dataclasses.replace(receipt_geometry.NORMALIZATION, **{field: value})
    monkeypatch.setattr(receipt_pages, "NORMALIZATION", changed)
    monkeypatch.setattr(receipt_pages.subprocess, "Popen", _no_spawn)
    with pytest.raises(receipt_pages.ReceiptDecodeUnavailableError, match="decode limits"):
        _pages(tmp_path, asset)
    assert _files(tmp_path) == []


def test_the_child_refuses_a_memory_limit_that_is_not_whole_pages() -> None:
    line = _request(memory_bytes=256 * 1024 * 1024 + 1).line()
    with pytest.raises(receipt_decode.WireError, match="4 KiB pages"):
        receipt_decode.parse_request(line)
    assert receipt_decode.parse_request(_request(memory_bytes=256 * 1024 * 1024).line())


def test_an_abort_before_the_ready_line_kills_and_reaps_the_child_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[tuple[Any, dict[str, Any]]] = []
    real_popen = subprocess.Popen

    def recording(command: list[str], **options: Any) -> Any:
        process = real_popen(command, **options)
        launched.append((process, options))
        return process

    monkeypatch.setattr(receipt_pages.subprocess, "Popen", recording)
    if sys.platform == "win32":
        # The child exists but never enters its Job: ending the (empty) Job would not reach it.
        def unassignable(kernel32: Any, job: int, process_handle: int) -> None:
            raise process_limits.ProcessLimitsError("fictional refusal", code=5)

        monkeypatch.setattr(process_limits, "assign_process", unassignable)
    else:

        def refusing(line: bytes | None, **options: Any) -> int | None:
            raise receipt_pages.ReceiptDecodeUnavailableError("fictional refusal")

        monkeypatch.setattr(receipt_pages, "_parse_ready_line", refusing)
    started = time.monotonic()
    with pytest.raises(receipt_pages.ReceiptDecodeUnavailableError):
        _pages(tmp_path, _asset(tmp_path, _encode(_receipt(), "PNG")))
    elapsed = time.monotonic() - started
    ((process, options),) = launched
    assert process.returncode is not None, "the child must be killed and reaped"
    assert elapsed < 10, f"the abort waited {elapsed:.1f} s for a child it never killed"
    assert not Path(options["cwd"]).exists(), "the working directory is removed after the reap"


def test_the_module_imports_without_pillow() -> None:
    script = (
        "import sys; sys.modules['PIL'] = None\n"
        "import pta_finance.receipt_pages, pta_finance.receipt_decode"
    )
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)


def test_the_decode_child_imports_only_the_standard_library_and_the_leaf() -> None:
    source = (_ROOT / "pta_finance" / "receipt_decode.py").read_text(encoding="utf-8")
    top_level: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            top_level |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            top_level |= {f"{node.module}.{alias.name}" for alias in node.names}
    project = {name for name in top_level if name.startswith("pta_finance")}
    assert project == {"pta_finance.process_limits"}
    assert all(name.split(".")[0] in sys.stdlib_module_names for name in top_level - project)
    assert not any(name.startswith("PIL") for name in top_level), "Pillow loads in main() only"


def test_the_broker_never_opens_an_image() -> None:
    # No in-process seam: the broker hands untrusted bytes only to the child, and its Pillow
    # calls see validated pixels alone (Image.frombytes in pixels_to_page).
    source = (_ROOT / "pta_finance" / "receipt_pages.py").read_text(encoding="utf-8")
    assert "Image.open" not in source and "_decode(" not in source
    assert "receipt_decode import" in source


def test_pillow_floor_matches_the_receipts_extra() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    (requirement,) = project["project"]["optional-dependencies"]["receipts"]
    major, minor = receipt_pages._PILLOW_FLOOR
    assert requirement == f"pillow>={major}.{minor}"


def test_the_child_is_launched_minimally_and_its_directory_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    launches: list[tuple[list[str], dict[str, Any]]] = []
    real_popen = subprocess.Popen

    def recording(command: list[str], **options: Any) -> Any:
        launches.append((command, options))
        return real_popen(command, **options)

    monkeypatch.setattr(receipt_pages.subprocess, "Popen", recording)
    # 81 MP: the child's Pillow warns before the child refuses; nothing may reach our output.
    with pytest.raises(receipt_pages.ReceiptPageError, match="too many pixels"):
        _pages(tmp_path, _asset(tmp_path, _png_header(9000, 9000)))
    ((command, options),) = launches
    assert command[1:] == ["-I", "-m", "pta_finance.receipt_decode"]
    assert options["stderr"] is subprocess.DEVNULL and options["close_fds"] is True
    assert options["stdin"] is subprocess.PIPE and options["stdout"] is subprocess.PIPE
    if sys.platform == "win32":
        assert command[0] == sys._base_executable
        assert set(options["env"]) == {"SYSTEMROOT", "__PYVENV_LAUNCHER__"}
        assert options["env"]["__PYVENV_LAUNCHER__"] == sys.executable
        assert options["creationflags"] == 0x08000000
    else:
        assert command[0] == sys.executable
        assert options["env"] == {} and options["start_new_session"] is True
    workdir = Path(options["cwd"])
    assert not workdir.exists(), "the empty working directory is removed after the reap"
    assert receipt_pages.cleanup_warning_count() == 0
    assert capfd.readouterr().err == ""


# --- Direct broker tests: crafted bytes and exit outcomes; nothing here decodes -------------


def _decoded_header(**changes: Any) -> dict[str, Any]:
    header: dict[str, Any] = {
        "status": "decoded",
        "mode": "L",
        "width": 40,
        "height": 20,
        "source_width": 40,
        "source_height": 20,
        "orientation": 1,
        "scale": 1.0,
    }
    header.update(changes)
    return header


def _response(header: dict[str, Any] | bytes, body: bytes = bytes(800)) -> bytes:
    line = header if isinstance(header, bytes) else receipt_decode.encode_line(header)
    return line + body


def _malformed_response(case: str) -> bytes:
    valid = receipt_decode.encode_line(_decoded_header())
    if case == "wrong-length":
        return _response(_decoded_header(), bytes(799))
    if case == "extra-field":
        return _response(_decoded_header(extra=1))
    if case == "missing-field":
        header = _decoded_header()
        del header["orientation"]
        return _response(header)
    if case == "bool-int":
        return _response(_decoded_header(orientation=True))  # True == 1
    if case == "float-int":
        return _response(_decoded_header(width=40.0))  # 40.0 == 40
    if case == "nan-scale":
        return _response(valid.replace(b'"scale":1.0', b'"scale":NaN'))
    if case == "duplicate-key":
        return _response(valid.replace(b'{"status":"decoded"', b'{"status":"decoded","mode":"L"'))
    if case == "crlf-terminator":
        return _response(valid[:-1] + b"\r\n")
    if case == "oversize-header":
        return _response(valid[:-2] + b" " * 1100 + b"}\n")
    raise AssertionError(case)


_MALFORMED_RESPONSES = [
    "wrong-length",
    "extra-field",
    "missing-field",
    "bool-int",
    "float-int",
    "nan-scale",
    "duplicate-key",
    "crlf-terminator",
    "oversize-header",
]


def test_broker_accepts_an_exact_decode_response_and_its_refusals() -> None:
    request = _request()
    decoded = receipt_pages._parse_decode_response(_response(_decoded_header()), request)
    assert isinstance(decoded, receipt_pages._Decoded)
    assert (decoded.mode, decoded.width, decoded.height, decoded.pixels) == (
        "L",
        40,
        20,
        bytes(800),
    )
    for reason in sorted(receipt_decode.REFUSAL_REASONS):
        refused = receipt_decode.encode_line({"status": "refused", "reason": reason})
        assert receipt_pages._parse_decode_response(refused, request) == reason


@pytest.mark.parametrize("case", _MALFORMED_RESPONSES)
def test_broker_rejects_malformed_decode_response(case: str) -> None:
    with pytest.raises(receipt_pages.ReceiptPageError) as caught:
        receipt_pages._parse_decode_response(_malformed_response(case), _request())
    assert caught.value.reason == "failed-validation"


@pytest.mark.parametrize(
    "header",
    [
        _decoded_header(width=41),
        _decoded_header(scale=0.5),
        _decoded_header(orientation=9),
        _decoded_header(source_width=70_000, width=2200, height=1, scale=2200 / 70_000),
        _decoded_header(mode="RGBA"),
        {"status": "refused", "reason": "budget"},
        {"status": "rendered"},
    ],
    ids=["width", "scale", "orientation", "source-edge", "mode", "reason", "status"],
)
def test_broker_rederives_every_response_value(header: dict[str, Any]) -> None:
    body = b"" if header["status"] != "decoded" else bytes(800)
    with pytest.raises(receipt_pages.ReceiptPageError) as caught:
        receipt_pages._parse_decode_response(_response(header, body), _request())
    assert caught.value.reason == "failed-validation"


def _ready(**changes: Any) -> dict[str, Any]:
    line: dict[str, Any] = {"status": "ready", "protocol": "receipt-decode/1", "pid": 1}
    line["pillow"] = "12.2.0"
    line.update(changes)
    return line


_BAND = {"memory_bytes": 1 << 30, "max_as_baseline": 1 << 28}
_MALFORMED_READY = {
    "bool-pid": (_ready(pid=True), "win32"),  # True == 1
    "extra-key": (_ready(extra=1), "win32"),
    "oversize": (receipt_decode.encode_line(_ready())[:-2] + b" " * 300 + b"}\n", "win32"),
    "rlimit-out-of-band": (_ready(rlimit_as=(1 << 30) + (1 << 28) + 1), "linux"),
    "rlimit-below-the-budget": (_ready(rlimit_as=(1 << 30) - 1), "linux"),
    "rlimit-missing-on-linux": (_ready(), "linux"),
    "pid-mismatch": (_ready(pid=2), "win32"),
    "pillow-mismatch": (_ready(pillow="12.3.0"), "win32"),
    "crlf-terminator": (receipt_decode.encode_line(_ready())[:-1] + b"\r\n", "win32"),
    "none-came": (None, "win32"),
}


@pytest.mark.parametrize("case", sorted(_MALFORMED_READY))
def test_broker_rejects_malformed_ready_line(case: str) -> None:
    value, platform = _MALFORMED_READY[case]
    line = receipt_decode.encode_line(value) if isinstance(value, dict) else value
    with pytest.raises(receipt_pages.ReceiptDecodeUnavailableError):
        receipt_pages._parse_ready_line(
            line, expected_pid=1, pillow="12.2.0", platform=platform, **_BAND
        )


def test_broker_accepts_an_exact_ready_line_on_each_host() -> None:
    windows = receipt_decode.encode_line(_ready())
    assert (
        receipt_pages._parse_ready_line(
            windows, expected_pid=1, pillow="12.2.0", platform="win32", **_BAND
        )
        is None
    )
    for rlimit_as in ((1 << 30), (1 << 30) + (1 << 28)):
        linux = receipt_decode.encode_line(_ready(rlimit_as=rlimit_as))
        assert (
            receipt_pages._parse_ready_line(
                linux, expected_pid=1, pillow="12.2.0", platform="linux", **_BAND
            )
            == rlimit_as
        )


def _pumped(stream: bytes, cap: int) -> tuple[receipt_pages._StdoutPump, int]:
    """Run the capped reader over a fictional child stdout; it and the bytes it took."""

    delivered = 0

    def read(size: int) -> bytes:
        nonlocal delivered
        chunk = stream[delivered : delivered + size]
        delivered += len(chunk)
        return chunk

    pump = receipt_pages._StdoutPump(read, response_cap=cap)
    reader = threading.Thread(target=pump.run, daemon=True)
    reader.start()
    reader.join(60)
    assert not reader.is_alive(), "the capped reader must stop by itself"
    return pump, delivered


def test_broker_stdout_read_is_capped() -> None:
    request = _request()
    cap = receipt_pages._stdout_cap(request)
    assert cap == 1024 + 2200 * 2200 * 3
    ready = receipt_decode.encode_line(_ready())
    # One byte over the cap derived from the request: the reader stops there and the stream is
    # the budget, never failed validation (which is for a bad header or body within the cap).
    pump, delivered = _pumped(ready + bytes(cap + 1) + bytes(1 << 20), cap)
    assert pump.overflowed and pump.finished
    assert delivered == len(ready) + cap + 1, "it stops one byte past the cap, never later"
    result = receipt_pages._ChildResult(
        returncode=0,
        timed_out=False,
        overflowed=pump.overflowed,
        body_written=True,
        stream=pump.response(),
    )
    assert receipt_pages._map_child_exit(result, request) == "budget"
    # Exactly at the cap nothing overflows: the same bytes are judged as a response instead.
    at_cap, _ = _pumped(ready + bytes(cap), cap)
    assert not at_cap.overflowed and len(at_cap.response()) == cap


def test_a_test_lowered_long_edge_lowers_the_stdout_cap() -> None:
    assert receipt_pages._stdout_cap(_request(max_long_edge=100)) == 1024 + 100 * 100 * 3


_EXIT_CASES: dict[str, tuple[dict[str, Any], str]] = {
    "exit-0-valid": ({"returncode": 0, "stream": _response(_decoded_header())}, "page"),
    "exit-0-refusal": (
        {"returncode": 0, "stream": b'{"status":"refused","reason":"source-edge"}\n'},
        "source-edge",
    ),
    "exit-0-malformed": ({"returncode": 0, "stream": b"{}\n"}, "failed-validation"),
    "exit-0-short-body-write": (
        {"returncode": 0, "stream": _response(_decoded_header()), "body_written": False},
        "failed-validation",
    ),
    "exit-budget": ({"returncode": receipt_decode.EXIT_BUDGET}, "budget"),
    "exit-child-error": ({"returncode": receipt_decode.EXIT_CHILD_ERROR}, "child-error"),
    "other-nonzero": ({"returncode": 1816}, "budget"),
    "signal": ({"returncode": -9}, "budget"),
    "wall-clock": ({"returncode": receipt_decode.EXIT_CHILD_ERROR, "timed_out": True}, "budget"),
    "oversize-stream": ({"returncode": 0, "overflowed": True}, "budget"),
}


@pytest.mark.parametrize("case", list(_EXIT_CASES))
def test_broker_maps_child_exit_status(case: str) -> None:
    fields, expected = _EXIT_CASES[case]
    result = receipt_pages._ChildResult(
        **{
            "returncode": None,
            "timed_out": False,
            "overflowed": False,
            "body_written": True,
            "stream": b"",
            **fields,
        }
    )
    outcome = receipt_pages._map_child_exit(result, _request())
    if expected == "page":
        assert isinstance(outcome, receipt_pages._Decoded)
    else:
        assert outcome == expected
    assert receipt_decode.EXIT_BUDGET == 3 and receipt_decode.EXIT_CHILD_ERROR == 4


# --- pixels_to_page, the one pixels-to-page step ---------------------------------------------


def test_pixels_to_page_encodes_validated_pixels_only() -> None:
    pixels = bytes(range(256)) * 100
    first = receipt_pages.pixels_to_page(pixels, mode="L", width=160, height=160)
    second = receipt_pages.pixels_to_page(pixels, mode="L", width=160, height=160)
    assert first == second and (first.width, first.height, first.scale) == (160, 160, 1.0)
    _assert_clean_jpeg(first.data)
    # A producer's page larger than the limit (a PDF render) is downscaled like an upload.
    wide = receipt_pages.pixels_to_page(bytes(4400 * 30 * 3), mode="RGB", width=4400, height=30)
    assert (wide.width, wide.height, wide.scale) == (2200, 15, 0.5)
    for bad in (
        {"mode": "L", "width": 160, "height": 161},
        {"mode": "RGBA", "width": 160, "height": 160},
        {"mode": "L", "width": 0, "height": 160},
    ):
        with pytest.raises(ValueError):
            receipt_pages.pixels_to_page(pixels, **bad)
