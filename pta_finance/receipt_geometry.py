"""The one definition of the Python-side receipt page geometry and decode budget.

Two groups live here and nowhere else; every producer imports them rather than restating a
number:

* :data:`NORMALIZATION` — how a producer turns a receipt image into a display page (long edge
  at most 2200 px, baseline JPEG at quality 85, flattened onto white), the fast-path ceilings
  an upload's header is checked against, and the budget the image decode child runs under.
  The page values are the measured defaults in ``documentation/receipt-autofill-plan.md`` § 2
  and § 6.6; the decode budget is calibrated per § 5A and recorded in § 6.3. The PNG→JPEG
  threshold is zero by design, so it has no field: every page, whatever its source format or
  size, is re-encoded as JPEG, which is the variant that was measured.
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
    """Deterministic display-page limits and the decode budget shared by every producer.

    Page shape: ``max_long_edge`` — a page whose longer side exceeds this many pixels is
    downscaled so that side is exactly this long; ``jpeg_quality`` and ``jpeg_subsampling`` —
    the baseline JPEG every display page is re-encoded as (subsampling is stated so an encoder
    default cannot drift); ``background`` — the colour transparent pixels are flattened onto.

    Source ceilings: ``max_source_bytes`` — the largest asset file that is read at all (the
    fetch cap and the read cap share it); ``max_source_pixels`` and ``max_source_edge`` —
    header fast paths (width x height, and either side) checked by the decode child before any
    pixel is decoded. These are cheap policy, not the safety bound: the bound is the budget.

    Decode budget, enforced by the operating system on the decode child (Windows Job Object,
    Linux rlimits): ``memory_bytes`` and ``cpu_seconds``; and by the broker alone, never sent to
    the child: ``wall_seconds`` from spawn to exit, ``ready_seconds`` from spawn to the child's
    ready line, and ``max_as_baseline``, the widest Linux interpreter baseline the broker
    accepts above ``memory_bytes`` in the ready line's ``rlimit_as``.
    """

    max_long_edge: int
    jpeg_quality: int
    jpeg_subsampling: str
    background: tuple[int, int, int]
    max_source_bytes: int
    max_source_pixels: int
    max_source_edge: int
    memory_bytes: int
    cpu_seconds: int
    wall_seconds: int
    ready_seconds: int
    max_as_baseline: int


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
    background=(255, 255, 255),
    # Matches the PDF worker's own max_pdf_bytes, so a fetched asset never exceeds what any
    # renderer accepts (plan § 5A, config block).
    max_source_bytes=25 * 1024 * 1024,
    # Above a 48-megapixel phone sensor, and below Pillow's own decompression-bomb error.
    max_source_pixels=80_000_000,
    # JPEG's own format limit; a longer edge exists only to amplify the resampler's cost.
    max_source_edge=65_535,
    # Calibrated per plan § 5A over four hosts (record in § 6.3): 1.5x the largest need —
    # 960 MiB, 5.50 s CPU, 5.64 s wall — rounded up to 64 MiB and whole seconds. Every known-good
    # anchor finishes within 80% of each limit; a Pillow move re-runs that gate, never a re-tune.
    memory_bytes=1472 * 1024 * 1024,
    cpu_seconds=9,
    wall_seconds=9,
    ready_seconds=5,
    # Pinned, not calibrated: at least 4x the measured Pillow-loaded interpreter baseline.
    max_as_baseline=256 * 1024 * 1024,
)

BOX_PADDING: Final[BoxPadding] = BoxPadding(
    rx_scale=0.60,
    rx_pad=0.006,
    ry_scale=0.72,
    ry_pad=0.003,
)
