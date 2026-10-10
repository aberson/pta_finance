"""The one definition of the Python-side receipt page geometry.

Two groups live here and nowhere else; every producer imports them rather than restating a
number:

* :data:`NORMALIZATION` — how a producer turns a receipt image into a display page: long edge
  at most 2200 px, baseline JPEG at quality 85, flattened onto white. These are the measured
  defaults in ``documentation/receipt-autofill-plan.md`` § 2 and § 6.6. The PNG→JPEG threshold
  is zero by design, so it has no field: every page, whatever its source format or size, is
  re-encoded as JPEG, which is the variant that was measured. No size floor below which a PNG
  stays lossless was measured, so none is invented.
* :data:`BOX_PADDING` — the documented **mirror** of the viewer's ellipse inflation. The viewer
  draws its outlines in the browser from JavaScript
  (``pta_finance/reports/templates/receipt_viewer.js.j2``:
  ``rx = min(w * .60 + .006, cx, 1 - cx)`` and ``ry = min(h * .72 + .003, cy, 1 - cy)``), so a
  Python constant cannot be the source of truth for that geometry. A test parses the template
  and fails when the two disagree, so a change to either side fails CI.

Box coordinates are fractions of the *displayed* page, measured from its top left (see
:mod:`pta_finance.receipt_viewer`). Uniform scaling preserves those fractions; rotation destroys
them. A producer therefore fixes orientation before any box exists and afterwards only ever
scales uniformly.

The values are grouped in frozen instances on purpose: CPython shares small integers such as
``85`` between modules, so an identity assertion on a bare ``int`` would still pass after the
number had been restated elsewhere. An identity assertion on these objects cannot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = ["BOX_PADDING", "NORMALIZATION", "BoxPadding", "PageNormalization"]


@dataclass(frozen=True)
class PageNormalization:
    """Deterministic display-page limits shared by every receipt page producer.

    ``max_long_edge`` — a page whose longer side exceeds this many pixels is downscaled so that
    side is exactly this long. ``jpeg_quality`` and ``jpeg_subsampling`` — the baseline JPEG
    every display page is re-encoded as; subsampling is stated so an encoder default cannot
    drift. ``max_source_pixels`` — decode ceiling (width x height) for an uploaded image,
    refused from its header before pixels are read. ``max_jpeg_scans`` — ceiling on the scans in
    an uploaded JPEG: the pixel ceiling bounds one scan's memory, not how many times a
    progressive file makes the decoder sweep it. ``max_exif_bytes`` — ceiling on the EXIF a
    source carries (summed over a JPEG's EXIF segments, which Pillow concatenates); its IFD
    loader costs grow with the square of the EXIF size. ``max_mpf_bytes`` — ceiling on a JPEG's
    multi-picture (MPF) index segment, an IFD Pillow fully materializes while opening the file.
    The JPEG ceilings are bounded from the raw bytes before anything is parsed, so they
    over-count rather than under-count what the decoder will do. ``background`` — the colour
    transparent pixels are flattened onto.
    """

    max_long_edge: int
    jpeg_quality: int
    jpeg_subsampling: str
    max_source_pixels: int
    max_jpeg_scans: int
    max_exif_bytes: int
    max_mpf_bytes: int
    background: tuple[int, int, int]


@dataclass(frozen=True)
class BoxPadding:
    """Mirror of the viewer's outline inflation around a ``[left, top, width, height]`` box.

    ``rx = min(width * rx_scale + rx_pad, cx, 1 - cx)`` and
    ``ry = min(height * ry_scale + ry_pad, cy, 1 - cy)``, all as page fractions.
    """

    rx_scale: float
    rx_pad: float
    ry_scale: float
    ry_pad: float


NORMALIZATION: Final[PageNormalization] = PageNormalization(
    max_long_edge=2200,
    jpeg_quality=85,
    jpeg_subsampling="4:2:0",
    # Above a 48-megapixel phone sensor, and below Pillow's own decompression-bomb warning.
    max_source_pixels=80_000_000,
    # Encoders emit 1 scan (baseline) or about 10 (libjpeg's progressive script); far above both.
    max_jpeg_scans=64,
    # One full JPEG APP1 segment always fits. Measured worst case of Pillow's IFD loader at this
    # size: 362 MiB peak in 0.13 s; at twice the size, 1.4 GiB (plan § 6.3).
    max_exif_bytes=64 * 1024,
    # Real MP index segments are a few hundred bytes. Measured worst case at this size: 30 MiB
    # in 0.05 s; at 64 KiB, 2.4 GiB in 13.5 s (plan § 6.3).
    max_mpf_bytes=4 * 1024,
    background=(255, 255, 255),
)

BOX_PADDING: Final[BoxPadding] = BoxPadding(
    rx_scale=0.60,
    rx_pad=0.006,
    ry_scale=0.72,
    ry_pad=0.003,
)
