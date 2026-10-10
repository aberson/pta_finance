from __future__ import annotations

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
import tomllib
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageCms, ImageFile, PngImagePlugin, TiffImagePlugin
from test_reimbursement_report import _bundle, _write_bundle

from pta_finance import receipt_geometry, receipt_pages, receipt_viewer, reimbursement_report

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


def _png_header(width: int, height: int, depth: int = 8) -> bytes:
    """A grayscale PNG claiming ``width`` x ``height`` pixels with almost no image data."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0"))
        + chunk(b"IEND", b"")
    )


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


def test_geometry_and_page_budget_are_imported_not_restated() -> None:
    # Identity, not equality: a restated copy of either object fails here.
    assert receipt_pages.NORMALIZATION is receipt_geometry.NORMALIZATION
    assert receipt_pages.MAX_PAGE_BYTES is receipt_viewer.MAX_PAGE_BYTES
    normalization = receipt_geometry.NORMALIZATION
    assert (normalization.max_long_edge, normalization.jpeg_quality) == (2200, 85)
    assert (normalization.background, normalization.max_jpeg_scans) == ((255, 255, 255), 64)
    # The explicit ceiling, not Pillow's own bomb warning, is what refuses an oversize header.
    assert Image.MAX_IMAGE_PIXELS is not None
    assert normalization.max_source_pixels < Image.MAX_IMAGE_PIXELS


def test_normalization_values_are_read_from_the_shared_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A producer that restated any value inline would ignore these replacements.
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
    # re-serialised EXIF, which would have escaped the per-asset refusal and aborted a batch.
    stored = _marked().transpose(Image.Transpose.ROTATE_90)
    data = _encode(stored, "JPEG", quality=95, exif=_exif_with_corrupt_gps_count())
    events: list[receipt_pages.PageProgress] = []
    (page,) = _pages(tmp_path, _asset(tmp_path, data, "jpeg"), progress=events.append)
    with Image.open(page.path) as output:
        assert output.size == (120, 300)
        assert _mean(output, (4, 4, 36, 56)) < 40
    assert events[0].orientation == 6


@pytest.mark.parametrize("error", [TypeError, KeyError, IndexError, struct.error])
def test_any_decoder_failure_is_a_per_asset_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def broken(self: Image.Image) -> Image.Exif:
        raise error("fictional decoder failure")

    monkeypatch.setattr(Image.Image, "getexif", broken)
    with pytest.raises(receipt_pages.ReceiptPageError, match="not a readable image"):
        _pages(tmp_path, _asset(tmp_path, _encode(_receipt(), "PNG")))
    assert _files(tmp_path) == []


@pytest.mark.parametrize("value", [6.0, 9, 0, "6"], ids=["float", "nine", "zero", "text"])
def test_an_orientation_outside_integers_one_to_eight_is_ignored_and_recorded_as_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    monkeypatch.setattr(Image.Image, "getexif", lambda self: {0x0112: value})
    events: list[receipt_pages.PageProgress] = []
    stored = _marked().transpose(Image.Transpose.ROTATE_90)
    (page,) = _pages(tmp_path, _asset(tmp_path, _encode(stored, "PNG")), progress=events.append)
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


def test_failed_fetch_yields_zero_pages_and_writes_nothing(tmp_path: Path) -> None:
    seen: list[receipt_pages.PageProgress] = []
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


def test_an_unusable_pages_directory_aborts_with_its_own_error(tmp_path: Path) -> None:
    (tmp_path / "receipt-pages").write_text("a file where the pages directory belongs")
    with pytest.raises(receipt_pages.ReceiptPagesDirectoryError, match="not writable") as caught:
        _pages(tmp_path, _asset(tmp_path, _encode(_receipt(), "PNG")))
    assert not isinstance(caught.value, receipt_pages.ReceiptPageError), "must abort the stage"
    assert str(tmp_path) not in str(caught.value)


_REFUSALS = {
    "pdf": "PDF receipt assets",
    "unknown_type": "must be PNG, JPEG or PDF",
    "digest": "no longer matches its asset id",
    "malformed_id": "asset id is malformed",
    "svg_as_png": "not a readable image",
    "text_as_jpeg": "not a readable image",
    "png_as_jpeg": "not a readable image",
    "truncated": "not a readable image",
    "over_pixel_ceiling": "too many pixels",
    "header_over_ceiling": "too many pixels",
    "pillow_bomb": "too many pixels",
    "sixteen_bit": "unsupported pixel format",
    "animated_png": "animated PNG",
    "missing_file": "missing or unreadable",
    "unsafe_ref": "safe receipt page id",
}


@pytest.mark.parametrize("problem", sorted(_REFUSALS))
def test_refusals_name_the_problem_and_write_no_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    good = _encode(_receipt(), "PNG")
    asset = _asset(tmp_path, good)
    options: dict[str, Any] = {}
    if problem == "pdf":
        asset = _asset(tmp_path, b"%PDF-1.7\n% fictional\n", "pdf")
    elif problem == "unknown_type":
        asset = _asset(tmp_path, b"GIF89a fictional", "gif")
    elif problem == "digest":
        asset = _Asset("asset:v1:" + "0" * 64, "png", asset.path)
    elif problem == "malformed_id":
        asset = _Asset("asset:v1:not-a-digest", "png", asset.path)
    elif problem == "svg_as_png":
        asset = _asset(tmp_path, b'<svg onload="alert(1)"></svg>')
    elif problem == "text_as_jpeg":
        asset = _asset(tmp_path, b"fictional plain text", "jpeg")
    elif problem == "png_as_jpeg":
        asset = _asset(tmp_path, good, "jpeg")
    elif problem == "truncated":
        asset = _asset(tmp_path, good[: len(good) // 2])
    elif problem == "over_pixel_ceiling":
        # A valid, decodable page: only the configured ceiling can refuse it.
        lowered = dataclasses.replace(receipt_pages.NORMALIZATION, max_source_pixels=400 * 1000 - 1)
        monkeypatch.setattr(receipt_pages, "NORMALIZATION", lowered)
    elif problem == "header_over_ceiling":
        asset = _asset(tmp_path, _png_header(9000, 9000))  # 81 MP, below Pillow's own checks
    elif problem == "pillow_bomb":
        asset = _asset(tmp_path, _png_header(15000, 15000))  # Pillow's own bomb error
    elif problem == "sixteen_bit":
        # Header only: refused from the header, before any pixel could be decoded.
        asset = _asset(tmp_path, _png_header(40, 40, depth=16))
    elif problem == "animated_png":
        # Browsers would show the animation; one fixed frame is not evidence of what they show.
        still, frame = Image.new("RGB", (40, 20), "white"), Image.new("RGB", (40, 20), "black")
        asset = _asset(tmp_path, _encode(still, "PNG", save_all=True, append_images=[frame]))
    elif problem == "missing_file":
        asset = _Asset(asset.asset_id, "png", tmp_path / "cache" / "absent.png")
    else:
        options["ticket_ref"] = "../EX-01"
    with pytest.raises(receipt_pages.ReceiptPageError, match=_REFUSALS[problem]) as caught:
        _pages(tmp_path, asset, **options)
    assert str(tmp_path) not in str(caught.value)
    assert _files(tmp_path) == []


def test_a_page_over_the_viewer_page_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(receipt_pages, "MAX_PAGE_BYTES", 64)
    with pytest.raises(receipt_pages.ReceiptPageError, match="page limit"):
        _pages(tmp_path, _asset(tmp_path, _encode(_receipt(), "PNG")))
    assert _files(tmp_path) == []


def test_the_module_imports_without_pillow() -> None:
    script = "import sys; sys.modules['PIL'] = None\nimport pta_finance.receipt_pages"
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)


@pytest.mark.parametrize("state", ["missing", "outdated"])
def test_a_missing_or_outdated_pillow_aborts_naming_the_receipts_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    asset = _asset(tmp_path, _encode(_receipt(), "PNG"))
    if state == "missing":
        monkeypatch.setitem(sys.modules, "PIL", None)
    else:
        monkeypatch.setattr(sys.modules["PIL"], "__version__", "12.1.0")
    with pytest.raises(ImportError, match="'receipts' extra") as caught:
        _pages(tmp_path, asset)
    assert not isinstance(caught.value, receipt_pages.ReceiptPageError), "must abort the stage"
    assert _files(tmp_path) == []


def test_pillow_floor_matches_the_receipts_extra() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    (requirement,) = project["project"]["optional-dependencies"]["receipts"]
    major, minor = receipt_pages._PILLOW_FLOOR
    assert requirement == f"pillow>={major}.{minor}"


def _noise(size: tuple[int, int]) -> Image.Image:
    """Seeded fictional noise: unlike a blank page, its entropy data is full of FF 00 pairs."""

    width, height = size
    return Image.frombytes("RGB", size, hashlib.shake_256(b"fictional").digest(width * height * 3))


def _progressive(
    extra_scans: int, *, noisy: bool = False, after_app0: bytes = b"", between: bytes = b""
) -> tuple[bytes, int]:
    """A fictional progressive JPEG whose final scan is repeated ``extra_scans`` more times.

    ``after_app0`` is inserted right after the JFIF segment and ``between`` before each repeated
    scan: layouts libjpeg and Pillow tolerate (they skip the bytes and decode every scan).
    Returns the file and its count of scan-start markers.
    """

    picture = _noise((64, 48)) if noisy else Image.new("RGB", (64, 48), "white")
    options = {"restart_marker_rows": 1} if noisy else {}
    data = _encode(picture, "JPEG", progressive=True, **options)
    last = data.rindex(b"\xff\xda")
    data = data[:-2] + (between + data[last:-2]) * extra_scans + b"\xff\xd9"
    app0_end = 4 + int.from_bytes(data[4:6], "big")
    data = data[:app0_end] + after_app0 + data[app0_end:]
    return data, data.count(b"\xff\xda")


def _blocked(stage: str) -> Callable[..., None]:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"the ceiling must refuse before {stage}")

    return refuse


def _exif_with_thumbnail() -> bytes:
    """Fictional EXIF whose IFD1 holds a small JPEG thumbnail: one more FF DA in the file."""

    thumbnail = _encode(Image.new("RGB", (16, 12), "white"), "JPEG")
    ifd1 = 8 + 2 + 12 + 4
    offset = ifd1 + 2 + 2 * 12 + 4
    tiff = b"MM\x00*" + struct.pack(">I", 8)
    tiff += struct.pack(">HHHIHH", 1, 0x0112, 3, 1, 1, 0) + struct.pack(">I", ifd1)
    tiff += struct.pack(">HHHII", 2, 0x0201, 4, 1, offset)
    tiff += struct.pack(">HHII", 0x0202, 4, 1, len(thumbnail)) + struct.pack(">I", 0)
    return b"Exif\x00\x00" + tiff + thumbnail


def test_ordinary_jpegs_pass_the_raw_byte_scan_bound(tmp_path: Path) -> None:
    normal, scans = _progressive(0)
    assert scans == 10, "libjpeg's progressive script for a colour page"
    thumbnailed = _encode(_receipt((400, 1000), "RGB"), "JPEG", exif=_exif_with_thumbnail())
    assert thumbnailed.count(b"\xff\xda") == 2, "the EXIF thumbnail's own scan is counted too"
    for ordinal, data in enumerate((normal, thumbnailed), start=1):
        (page,) = _pages(tmp_path, _asset(tmp_path, data, "jpeg"), asset_ordinal=ordinal)
        assert page.path.is_file()


def test_the_scan_ceiling_counts_exactly_through_stuffed_bytes_and_restart_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ceiling = receipt_geometry.NORMALIZATION.max_jpeg_scans
    at_ceiling, counted = _progressive(ceiling - 10, noisy=True)
    assert counted == ceiling
    assert b"\xff\x00" in at_ceiling, "the fixture must carry stuffed bytes"
    assert any(bytes([0xFF, 0xD0 + n]) in at_ceiling for n in range(8)), "and restart markers"
    (page,) = _pages(tmp_path, _asset(tmp_path, at_ceiling, "jpeg"))
    assert (page.width, page.height) == (64, 48)
    over, _ = _progressive(ceiling - 10 + 1, noisy=True)
    monkeypatch.setattr(ImageFile.ImageFile, "load", _blocked("any decode"))
    with pytest.raises(receipt_pages.ReceiptPageError, match="too many scans"):
        _pages(tmp_path, _asset(tmp_path, over, "jpeg"), asset_ordinal=2)
    lowered = dataclasses.replace(receipt_pages.NORMALIZATION, max_jpeg_scans=9)
    monkeypatch.setattr(receipt_pages, "NORMALIZATION", lowered)
    with pytest.raises(receipt_pages.ReceiptPageError, match="too many scans"):
        _pages(tmp_path, _asset(tmp_path, _progressive(0)[0], "jpeg"), asset_ordinal=3)


_LENIENT_LAYOUTS = {
    "junk_after_app0": {"after_app0": b"\x00"},
    "stuffed_pair_at_a_segment_boundary": {"after_app0": b"\xff\x00"},
    "zero_length_app15": {"after_app0": b"\xff\xef\x00\x00"},
    "length_one_app15": {"after_app0": b"\xff\xef\x00\x01"},
    "junk_between_repeated_scans": {"between": b"\x00"},
}


@pytest.mark.parametrize("layout", sorted(_LENIENT_LAYOUTS))
def test_layouts_the_decoder_tolerates_cannot_hide_scans_from_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    # Regression: a marker walk stopped counting at these bytes while the decoder skipped them
    # and ran every scan. The bound must stay an upper bound on the decoder's work.
    ceiling = receipt_geometry.NORMALIZATION.max_jpeg_scans
    data, counted = _progressive(ceiling - 10 + 1, **_LENIENT_LAYOUTS[layout])
    assert counted == ceiling + 1
    monkeypatch.setattr(ImageFile.ImageFile, "load", _blocked("any decode"))
    with pytest.raises(receipt_pages.ReceiptPageError, match="too many scans"):
        _pages(tmp_path, _asset(tmp_path, data, "jpeg"))
    assert _files(tmp_path) == []


def _app_segment(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def test_a_full_size_exif_segment_still_normalizes(tmp_path: Path) -> None:
    exif = Image.Exif()
    exif[0x0112] = 1
    exif[0x927C] = bytes(60_000)  # a fictional maker note
    blob = exif.tobytes()
    assert 60_000 < len(blob) <= 65_533 <= receipt_geometry.NORMALIZATION.max_exif_bytes
    data = _encode(_receipt((400, 1000), "RGB"), "JPEG", exif=blob)
    (page,) = _pages(tmp_path, _asset(tmp_path, data, "jpeg"))
    assert page.path.is_file()


def _oversized_metadata(carrier: str) -> tuple[bytes, str]:
    picture = _receipt((40, 100), "RGB")
    padding = bytes(40_000)
    if carrier == "jpeg_exif_segments":
        # Each segment is legal on its own; Pillow concatenates them past the ceiling.
        segment = _app_segment(0xE1, b"Exif\x00\x00MM\x00*\x00\x00\x00\x08" + padding)
        data = _encode(picture, "JPEG")
        return data[:2] + segment + segment + data[2:], "jpeg"
    if carrier == "jpeg_mpf_index":
        data = _encode(picture, "JPEG")
        mpf = _app_segment(0xE2, b"MPF\x00MM\x00*\x00\x00\x00\x08" + bytes(5_000))
        return data[:2] + mpf + data[2:], "jpeg"
    if carrier == "png_exif_chunk":
        blob = b"Exif\x00\x00MM\x00*\x00\x00\x00\x08" + bytes(70_000)
        return _encode(picture, "PNG", exif=blob), "png"
    text = PngImagePlugin.PngInfo()
    text.add_text("Raw profile type exif", "\nexif\n   70000\n" + "00" * 70_000)
    return _encode(picture, "PNG", pnginfo=text), "png"


@pytest.mark.parametrize(
    "carrier", ["jpeg_exif_segments", "jpeg_mpf_index", "png_exif_chunk", "png_raw_profile"]
)
def test_oversized_exif_or_multi_picture_metadata_is_refused_before_it_is_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, carrier: str
) -> None:
    data, media_type = _oversized_metadata(carrier)
    # Pillow's EXIF and MP-index loaders both run through this one directory parser.
    monkeypatch.setattr(
        TiffImagePlugin.ImageFileDirectory_v2, "load", _blocked("metadata is parsed")
    )
    with pytest.raises(receipt_pages.ReceiptPageError, match="too much EXIF or multi-picture"):
        _pages(tmp_path, _asset(tmp_path, data, media_type))
    assert _files(tmp_path) == []


@pytest.mark.parametrize("ordinal", [0, -1, True, 1.0], ids=["zero", "negative", "bool", "float"])
def test_an_invalid_asset_ordinal_is_a_caller_bug_never_a_recorded_refusal(
    tmp_path: Path, ordinal: object
) -> None:
    with pytest.raises(ValueError, match="asset_ordinal") as caught:
        _pages(tmp_path, _asset(tmp_path, _encode(_receipt(), "PNG")), asset_ordinal=ordinal)
    assert not isinstance(caught.value, receipt_pages.ReceiptPageError)
    assert _files(tmp_path) == []


@pytest.mark.filterwarnings("error::PIL.Image.DecompressionBombWarning")
def test_a_bomb_warning_raised_as_an_error_still_reports_too_many_pixels(tmp_path: Path) -> None:
    with pytest.raises(receipt_pages.ReceiptPageError, match="too many pixels"):
        _pages(tmp_path, _asset(tmp_path, _png_header(10000, 10000)))


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
