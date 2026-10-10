"""Step 41 spike: prove a real LPAC render of a non-embedded-font PDF yields legible glyphs.

pdfium rasterizes on the CPU, but its Win32 font mapper reaches system fonts through GDI and
the LPAC worker has no window-station grant, so a degraded mapper could draw blank or boxed
text that no fake-backend unit test can see.  The measurement below therefore renders the
subject inside the REAL attested LPAC worker (never mocked) and scores it against a
reference: the embedded-font twin rendered outside the LPAC.

Both fixture PDFs come from committed generators (no binary is committed): the subject is
hand-written PDF syntax using the non-embedded standard Type1 ``/Helvetica``; the twin is
built at test time with pypdfium2 and embeds ``%WINDIR%\\Fonts\\arial.ttf`` read at test time.
All fixture text is fictional.

Each metric's threshold is pinned below and trusted only after calibration in the same test
(workspace measurement-validity rule): the known-good anchor (the subject outside the LPAC)
must score above it and both known-garbage anchors (an all-white page and a box-glyph page
synthesized from the reference's glyph bounding boxes) below it.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import io
import json
import math
import operator
import os
import zlib
from collections.abc import Callable
from pathlib import Path

import pytest

from pta_finance.treasurer_slides import bank_statements, native_sandbox
from pta_finance.treasurer_slides.bank_statements import (
    ReceiptRenderError,
    RenderedPage,
    StatementExtractionError,
)

# Pinned pass thresholds.  Calibrated on the 2026-10-10 dev-box readings recorded in
# documentation/findings/step-41-lpac-render.md (known-good 0.9991 / 0.9549; box-glyph
# 0.4357 / 0.6540; all-white 0 / 0), one threshold near each midpoint, and re-calibrated by
# every run of the measurement before the LPAC render is judged.
INK_COVERAGE_THRESHOLD = 0.75
GLYPH_NCC_THRESHOLD = 0.80

# The verdict recorded in documentation/findings/step-41-lpac-render.md.  The measurement
# must reproduce it; "blocked" additionally requires the blocked findings file that
# Step 42's predicate reads.
RECORDED_VERDICT = "pass"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FINDINGS = _REPO_ROOT / "documentation" / "findings" / "step-41-lpac-render.md"
_BLOCKED_FINDINGS = _REPO_ROOT / "documentation" / "findings" / "step-41-lpac-render-blocked.md"
_PROPERTY_PREFIX = "lpac_render_spike."
_SUMMARY_PREFIX = "LPAC_RENDER_SPIKE "

_PAGE_POINTS = (612, 792)
# (left, bottom, font size, text) in PDF points; every line is fictional.
_FIXTURE_LINES: tuple[tuple[int, int, int, str], ...] = (
    (72, 700, 24, "Example PTA"),
    (72, 660, 16, "Fictional Supply Receipt"),
    (72, 620, 12, "Order EX-0000 placed by the Example PTA treasurer"),
    (72, 596, 12, "Poster board, pack of 4 ............ 12.00"),
    (72, 572, 12, "Washable markers, set of 8 ........... 8.50"),
    (72, 548, 12, "Glue sticks, box of 12 ................ 6.25"),
    (72, 516, 14, "Total due 26.75"),
    (72, 480, 12, "The quick brown fox jumps over the lazy dog."),
    (72, 456, 12, "PACK MY BOX WITH FIVE DOZEN LIQUOR JUGS 0123456789"),
    (72, 420, 10, "This receipt is entirely fictional and identifies no real organization."),
)
_FIXTURE_GLYPH_COUNT = sum(
    1 for *_position, text in _FIXTURE_LINES for character in text if not character.isspace()
)
_DARK_GRAY_VALUES = bytes(range(128))


# --- Committed fixture generators -------------------------------------------------------


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _subject_pdf_bytes() -> bytes:
    """Hand-written one-page PDF whose only font is the non-embedded standard /Helvetica."""

    width, height = _PAGE_POINTS
    stream = "\n".join(
        f"BT /F1 {size} Tf 1 0 0 1 {left} {bottom} Tm ({_escape_pdf_text(text)}) Tj ET"
        for left, bottom, size, text in _FIXTURE_LINES
    ).encode("cp1252")
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        4: (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            "/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"
        ).encode("ascii"),
        5: f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream",
    }
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number in range(1, max(objects) + 1):
        offsets.append(len(body))
        body.extend(f"{object_number} 0 obj\n".encode("ascii"))
        body.extend(objects[object_number])
        body.extend(b"\nendobj\n")
    xref_offset = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    trailer = f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
    body.extend(f"{trailer}startxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
    return bytes(body)


def _embedded_twin_pdf_bytes() -> bytes:
    """The same text with Arial embedded, built through pypdfium2's raw text-object calls."""

    import pypdfium2
    import pypdfium2.raw as pdfium_c

    # No fallback: a missing WINDIR or font file is a failure, never a substitute face.
    font_bytes = (Path(os.environ["WINDIR"]) / "Fonts" / "arial.ttf").read_bytes()
    font_buffer = (ctypes.c_uint8 * len(font_bytes)).from_buffer_copy(font_bytes)
    document = pypdfium2.PdfDocument.new()
    try:
        page = document.new_page(*_PAGE_POINTS)
        font = pdfium_c.FPDFText_LoadFont(
            document, font_buffer, len(font_bytes), pdfium_c.FPDF_FONT_TRUETYPE, False
        )
        assert font, "pdfium refused to load the TrueType face"
        try:
            for left, bottom, size, text in _FIXTURE_LINES:
                text_object = pdfium_c.FPDFPageObj_CreateTextObj(document, font, float(size))
                assert text_object, "pdfium refused to create a text object"
                wide_text = ctypes.create_string_buffer(text.encode("utf-16-le") + b"\x00\x00")
                assert pdfium_c.FPDFText_SetText(
                    text_object, ctypes.cast(wide_text, ctypes.POINTER(pdfium_c.FPDF_WCHAR))
                )
                pdfium_c.FPDFPageObj_Transform(
                    text_object, 1.0, 0.0, 0.0, 1.0, float(left), float(bottom)
                )
                pdfium_c.FPDFPage_InsertObject(page, text_object)
            assert pdfium_c.FPDFPage_GenerateContent(page), "pdfium did not write the content"
        finally:
            pdfium_c.FPDFFont_Close(font)
            page.close()
        output = io.BytesIO()
        document.save(output)
    finally:
        document.close()
    return output.getvalue()


# --- Measurement ---------------------------------------------------------------------------


def _render_outside_lpac(pdf: bytes) -> RenderedPage:
    """Render in this process through the worker's own render function (same code path)."""

    import pypdfium2

    page_count, page = bank_statements._render_native_page_pixels(
        pypdfium2,
        pdf,
        bank_statements._native_render_limits(1, bank_statements.RENDER_SCALE_PERMILLE),
    )
    assert page_count == 1
    return page


def _box_glyph_pixels(reference_pdf: bytes, width: int, height: int) -> bytes:
    """Fill each of the reference's tight glyph bounding boxes: over-inked boxed text."""

    import pypdfium2

    document = pypdfium2.PdfDocument(reference_pdf)
    try:
        page = document[0]
        text_page = page.get_textpage()
        try:
            page_width, page_height = page.get_size()
            scale_x, scale_y = width / page_width, height / page_height
            canvas = bytearray(b"\xff" * (width * height))
            boxes = 0
            for index in range(text_page.count_chars()):
                if text_page.get_text_range(index, 1).isspace():
                    continue
                left, bottom, right, top = text_page.get_charbox(index)
                x0, x1 = max(0, round(left * scale_x)), min(width, round(right * scale_x))
                y0 = max(0, round((page_height - top) * scale_y))
                y1 = min(height, round((page_height - bottom) * scale_y))
                assert x0 < x1 and y0 < y1, "a reference glyph box is empty"
                for row in range(y0, y1):
                    canvas[row * width + x0 : row * width + x1] = bytes(x1 - x0)
                boxes += 1
        finally:
            text_page.close()
            page.close()
    finally:
        document.close()
    assert boxes == _FIXTURE_GLYPH_COUNT, "the reference text page lost glyphs"
    return bytes(canvas)


def _dark_fraction(pixels: bytes) -> float:
    return (len(pixels) - len(pixels.translate(None, _DARK_GRAY_VALUES))) / len(pixels)


def ink_coverage_score(candidate: bytes, reference: bytes) -> float:
    """min(r, 1/r) with r = candidate dark fraction / reference dark fraction; 0 when r = 0."""

    reference_dark = _dark_fraction(reference)
    assert reference_dark > 0, "the reference render has no ink"
    ratio = _dark_fraction(candidate) / reference_dark
    return 0.0 if ratio == 0 else min(ratio, 1 / ratio)


def glyph_ncc_score(candidate: bytes, reference: bytes) -> float:
    """Normalized cross-correlation of two equal-size gray8 images; 0 for a constant image."""

    assert len(candidate) == len(reference)
    count = len(candidate)
    sum_c, sum_r = sum(candidate), sum(reference)
    variance_c = count * sum(map(operator.mul, candidate, candidate)) - sum_c * sum_c
    variance_r = count * sum(map(operator.mul, reference, reference)) - sum_r * sum_r
    if variance_c == 0 or variance_r == 0:
        return 0.0
    covariance = count * sum(map(operator.mul, candidate, reference)) - sum_c * sum_r
    return covariance / (math.sqrt(variance_c) * math.sqrt(variance_r))


_METRICS: tuple[tuple[str, Callable[[bytes, bytes], float], float], ...] = (
    ("ink", ink_coverage_score, INK_COVERAGE_THRESHOLD),
    ("ncc", glyph_ncc_score, GLYPH_NCC_THRESHOLD),
)


@pytest.mark.skipif(os.name != "nt", reason="LPAC is a Windows-only enforcement boundary")
def test_lpac_render_spike_scores_the_real_lpac_render(
    monkeypatch: pytest.MonkeyPatch,
    record_testsuite_property: Callable[[str, object], None],
) -> None:
    subject = _subject_pdf_bytes()
    twin = _embedded_twin_pdf_bytes()
    assert b"/FontFile" not in subject and b"/BaseFont /Helvetica" in subject
    assert b"/FontFile2" in twin, "the reference twin must embed its TrueType face"

    reference = _render_outside_lpac(twin)
    known_good = _render_outside_lpac(subject)
    assert (known_good.width, known_good.height) == (reference.width, reference.height)
    anchors = {
        "known_good": known_good.pixels,
        "all_white": b"\xff" * (reference.width * reference.height),
        "box_glyph": _box_glyph_pixels(twin, reference.width, reference.height),
    }
    scores = {
        name: {metric: score(pixels, reference.pixels) for metric, score, _ in _METRICS}
        for name, pixels in anchors.items()
    }
    summary: dict[str, object] = {}

    def record(values: dict[str, object]) -> None:
        # Visible in CI even when a later step fails: JUnit properties plus one summary line.
        summary.update(values)
        for key, value in values.items():
            record_testsuite_property(f"{_PROPERTY_PREFIX}{key}", value)
        print(_SUMMARY_PREFIX + json.dumps(summary, sort_keys=True))

    record(
        {
            "threshold.ink": INK_COVERAGE_THRESHOLD,
            "threshold.ncc": GLYPH_NCC_THRESHOLD,
            **{
                f"{name}.{metric}": round(value, 4)
                for name, values in scores.items()
                for metric, value in values.items()
            },
        }
    )

    # Calibration precedes any LPAC judgement: score(good) > T > max(score(garbage)).
    uncalibrated = [
        metric
        for metric, _, threshold in _METRICS
        if not scores["known_good"][metric]
        > threshold
        > max(scores["all_white"][metric], scores["box_glyph"][metric])
    ]
    if uncalibrated:
        pytest.fail(f"calibration failed for {uncalibrated}: no verdict; anchors {scores}")

    launches: list[int] = []
    real_start = native_sandbox.start_native_pdf_worker

    def observed_start(**kwargs: object) -> native_sandbox.NativeWorkerSession:
        launches.append(json.loads(str(kwargs["limits_json"]))["operation"])
        return real_start(**kwargs)

    # A pass-through observer only: the real attested launcher still runs.
    monkeypatch.setattr(native_sandbox, "start_native_pdf_worker", observed_start)
    page_count, lpac = bank_statements._render_native_page_in_worker(subject)
    assert launches == [bank_statements.OPERATION_RENDER]
    assert page_count == 1
    assert (lpac.page_number, lpac.width, lpac.height) == (1, reference.width, reference.height)
    scores["lpac"] = {metric: score(lpac.pixels, reference.pixels) for metric, score, _ in _METRICS}
    failing = [
        metric for metric, _, threshold in _METRICS if not scores["lpac"][metric] > threshold
    ]
    verdict = "blocked" if failing else "pass"

    record(
        {
            **{f"lpac.{metric}": round(value, 4) for metric, value in scores["lpac"].items()},
            "verdict": verdict,
            "lpac_identical_to_known_good": lpac.pixels == known_good.pixels,
            "lpac_raw_sha256": hashlib.sha256(lpac.pixels).hexdigest(),
            "size": f"{lpac.width}x{lpac.height}",
        }
    )

    assert verdict == RECORDED_VERDICT, (
        f"measured {verdict!r} but the findings record {RECORDED_VERDICT!r}: {summary}"
    )
    if verdict == "pass":
        assert all(scores["lpac"][metric] > threshold for metric, _, threshold in _METRICS)
        assert not _BLOCKED_FINDINGS.exists(), "a pass verdict must not leave Step 42's trigger"
    else:
        assert failing, "the blocked verdict needs a metric below its threshold"
        assert _BLOCKED_FINDINGS.stat().st_size > 0, "the blocked verdict needs its findings"
    findings = _FINDINGS.read_text(encoding="utf-8")
    assert f"T_INK = {INK_COVERAGE_THRESHOLD:.2f}" in findings
    assert f"T_NCC = {GLYPH_NCC_THRESHOLD:.2f}" in findings
    assert f"Verdict: **{RECORDED_VERDICT}**" in findings


# --- The minimal operation-2 branch (host-independent; no LPAC) --------------------------


class _Request:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.closed = False

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        assert maxlength == bank_statements.MAX_PDF_BYTES
        return self.payload

    def send_bytes(self, value: bytes) -> None:
        raise AssertionError("the worker request endpoint must not send")

    def close(self) -> None:
        self.closed = True


class _Response:
    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.closed = False

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        raise AssertionError("the worker response endpoint must not receive")

    def send_bytes(self, value: bytes) -> None:
        self.messages.append(value)

    def close(self) -> None:
        self.closed = True


def _worker_reply(payload: bytes, limits: bank_statements._NativeExtractionLimits) -> bytes:
    request, response = _Request(payload), _Response()
    bank_statements._native_page_extraction_after_limits(request, response, 1, limits)
    assert request.closed and response.closed
    assert len(response.messages) == 1
    return response.messages[0]


def _render_limits() -> bank_statements._NativeExtractionLimits:
    return bank_statements._native_render_limits(1, bank_statements.RENDER_SCALE_PERMILLE)


def test_every_request_carries_the_render_fields_and_extraction_keeps_operation_one() -> None:
    extraction = bank_statements._native_extraction_limits(document_ordinal=1)
    render = _render_limits()

    assert (extraction.operation, extraction.render_page_first) == (1, 1)
    assert extraction.render_scale_permille == bank_statements.RENDER_SCALE_PERMILLE == 2_778
    assert (render.operation, render.render_page_first) == (bank_statements.OPERATION_RENDER, 1)
    for limits in (extraction, render):
        encoded = json.loads(bank_statements._serialize_native_limits(limits))
        assert set(encoded) == {field for field, _ in bank_statements._NATIVE_LIMIT_FIELD_CEILINGS}
        assert {"operation", "render_page_first", "render_scale_permille"} <= set(encoded)
        assert (
            bank_statements._deserialize_native_limits(
                bank_statements._serialize_native_limits(limits), 1
            )
            == limits
        )


@pytest.mark.parametrize("operation", [0, 3, True, "2"])
def test_the_envelope_refuses_an_operation_outside_one_and_two(operation: object) -> None:
    encoded = json.loads(bank_statements._serialize_native_limits(_render_limits()))
    encoded["operation"] = operation

    with pytest.raises(StatementExtractionError):
        bank_statements._deserialize_native_limits(json.dumps(encoded), 1)
    del encoded["operation"]
    with pytest.raises(StatementExtractionError):
        bank_statements._deserialize_native_limits(json.dumps(encoded), 1)


def test_render_request_answers_one_validated_gray8_page_through_the_worker_entry() -> None:
    subject = _subject_pdf_bytes()
    limits = _render_limits()

    frame = _worker_reply(subject, limits)
    page_count, page = bank_statements._deserialize_rendered_page(frame, limits)

    root = json.loads(frame)
    assert set(root) == {"status", "page_count", "pages"} and root["status"] == "rendered"
    assert set(root["pages"][0]) == {
        "page_number",
        "width",
        "height",
        "format",
        "raw_sha256",
        "raw_length",
        "zlib",
    }
    assert root["pages"][0]["format"] == "gray8"
    assert page_count == 1
    # US Letter at 2,778 permille is exactly 1,700 x 2,200 (half-up rounding, section 5A).
    assert (page.page_number, page.width, page.height) == (1, 1_700, 2_200)
    assert len(page.pixels) == page.width * page.height
    assert page == _render_outside_lpac(subject)
    assert _dark_fraction(page.pixels) > 0


def test_extraction_request_never_reaches_the_render_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_render(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("operation 1 must never render")

    monkeypatch.setattr(bank_statements, "_native_page_render_after_limits", unexpected_render)
    monkeypatch.setattr(bank_statements, "_render_native_page_pixels", unexpected_render)

    reply = json.loads(
        _worker_reply(_subject_pdf_bytes(), bank_statements._native_extraction_limits(1))
    )
    assert reply["status"] in {"ok", "rejected"}


def test_render_worker_answers_failed_when_it_cannot_render() -> None:
    limits = _render_limits()
    beyond_last_page = bank_statements._native_render_limits(
        2, bank_statements.RENDER_SCALE_PERMILLE
    )

    assert _worker_reply(b"%PDF-1.4 not a document", limits) == b'{"status":"failed"}'
    assert _worker_reply(_subject_pdf_bytes(), beyond_last_page) == b'{"status":"failed"}'
    with pytest.raises(ReceiptRenderError):
        bank_statements._deserialize_rendered_page(b'{"status":"failed"}', limits)


def _tampered(case: str) -> bytes:
    pixels = bytes(range(256)) * 4
    page: dict[str, object] = {
        "page_number": 1,
        "width": 32,
        "height": 32,
        "format": "gray8",
        "raw_sha256": hashlib.sha256(pixels).hexdigest(),
        "raw_length": len(pixels),
        "zlib": base64.b64encode(zlib.compress(pixels)).decode("ascii"),
    }
    root: dict[str, object] = {"status": "rendered", "page_count": 1, "pages": [page]}
    if case == "digest-mismatch":
        page["raw_sha256"] = hashlib.sha256(pixels[:-1] + b"\x00").hexdigest()
    elif case == "wrong-length":
        shorter = pixels[:-32]
        page["zlib"] = base64.b64encode(zlib.compress(shorter)).decode("ascii")
        page["raw_sha256"] = hashlib.sha256(shorter).hexdigest()
    elif case == "length-not-width-times-height":
        page["width"] = 31
    elif case == "leftover-bytes":
        page["zlib"] = base64.b64encode(zlib.compress(pixels) + b"\x00").decode("ascii")
    elif case == "inflates-past-length":
        longer = pixels + b"\x00"
        page["zlib"] = base64.b64encode(zlib.compress(longer)).decode("ascii")
    elif case == "extra-key":
        page["dpi"] = 200
    elif case == "wrong-page-number":
        page["page_number"] = 2
        root["page_count"] = 2
    elif case == "wrong-format":
        page["format"] = "bgr"
    elif case == "two-pages":
        root["pages"] = [page, page]
    elif case == "not-base64":
        page["zlib"] = "@@@@"
    return json.dumps(root).encode("ascii")


def test_an_untampered_frame_is_accepted() -> None:
    page_count, page = bank_statements._deserialize_rendered_page(
        _tampered("none"), _render_limits()
    )
    assert (page_count, page.width, page.height) == (1, 32, 32)
    assert page.pixels == bytes(range(256)) * 4


@pytest.mark.parametrize(
    "case",
    [
        "digest-mismatch",
        "wrong-length",
        "length-not-width-times-height",
        "leftover-bytes",
        "inflates-past-length",
        "extra-key",
        "wrong-page-number",
        "wrong-format",
        "two-pages",
        "not-base64",
    ],
)
def test_the_broker_refuses_a_tampered_rendered_frame(case: str) -> None:
    with pytest.raises(ReceiptRenderError):
        bank_statements._deserialize_rendered_page(_tampered(case), _render_limits())


def test_render_edges_round_half_up() -> None:
    assert bank_statements._render_edge(612.0, 2_778) == 1_700
    assert bank_statements._render_edge(792.0, 2_778) == 2_200
    assert bank_statements._render_edge(2.5, 1_000) == 3
    for invalid in (0.0, -1.0, float("nan"), float("inf"), True, "612"):
        with pytest.raises(ReceiptRenderError):
            bank_statements._render_edge(invalid, 2_778)


def test_a_render_worker_that_cannot_start_propagates_sandbox_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def start_failure(**_kwargs: object) -> None:
        raise native_sandbox.NativeSandboxUnavailable("native PDF sandbox unavailable")

    monkeypatch.setattr(native_sandbox, "start_native_pdf_worker", start_failure)

    with pytest.raises(native_sandbox.NativeSandboxUnavailable):
        bank_statements._render_native_page_in_worker(_subject_pdf_bytes())
    for page_number in (0, 26):
        with pytest.raises(ValueError):
            bank_statements._render_native_page_in_worker(
                _subject_pdf_bytes(), page_number=page_number
            )
