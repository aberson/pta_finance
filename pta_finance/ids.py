"""ID grammar and fiscal-year logic — the single source of truth for ID formats.

Every producer (ETL) and consumer (analytics, reports) imports its ID formats from
here. Re-deriving an ID string anywhere else risks silent key drift; don't.

Grammar (see plan.md §12 Appendix):

    TXN-FY{yy}-{seq:04d}     # transactions
    RCP-FY{yy}-{seq:04d}     # receipts
    BUD-FY{yy}-{slug}        # budget   (slug = kebab category, optional -g{grade})
    EVT-FY{yy}-{slug}        # events
    yy   = last two digits of fiscal_year_label(date, start_month)
    seq  = per-fiscal-year, per-entity zero-padded counter
"""

from __future__ import annotations

import re
from datetime import date

__all__ = [
    "fiscal_year_label",
    "slugify",
    "txn_id",
    "summary_txn_id",
    "receipt_id",
    "budget_id",
    "event_id",
]

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def fiscal_year_label(d: date, start_month: int = 1) -> int:
    """Return the integer fiscal-year label for a date given the FY start month.

    For ``start_month == 1`` (calendar year) the label is simply ``d.year``.

    For a non-January start month the fiscal year spans two calendar years and is
    labeled by its **ending** calendar year. Example with ``start_month == 7``
    (July start): Jul 2025 .. Jun 2026 is fiscal year ``2026``; so a date in
    Jul-Dec falls in the FY that ends the *next* calendar year, and a date in
    Jan-Jun falls in the FY that ends the *current* calendar year.
    """
    if not 1 <= start_month <= 12:
        raise ValueError(f"start_month must be in 1..12, got {start_month}")
    if start_month == 1:
        return d.year
    # Span starts in `start_month` of one year and ends in `start_month - 1` of the
    # next; the label is that ending calendar year.
    if d.month >= start_month:
        return d.year + 1
    return d.year


def slugify(s: str) -> str:
    """Lowercase kebab-case slug: punctuation/whitespace -> single hyphens, trimmed."""
    return _SLUG_STRIP.sub("-", s.casefold()).strip("-")


def _yy(fy: int) -> str:
    """Last two digits of a fiscal-year label, zero-padded to width 2."""
    return f"{fy % 100:02d}"


def txn_id(fy: int, seq: int) -> str:
    """Transaction id, e.g. ``TXN-FY26-0001``."""
    return f"TXN-FY{_yy(fy)}-{seq:04d}"


def summary_txn_id(fy: int, category: str, grade: str | None = None) -> str:
    """Summary-transaction id for an imported budget actual, e.g. ``TXN-FY26-SUM-supplies``.

    Mirrors :func:`budget_id`'s category(+grade) slug shape so a budget line and its
    imported summary transaction line up by slug. Appends ``-g{grade}`` when ``grade`` is
    non-empty (e.g. ``TXN-FY26-SUM-supplies-g3``).

    The ``-SUM-{slug}`` body is INTENTIONALLY not matched by ``etl._TXN_ID_RE`` (whose
    sequence token must be ``\\d{4,}``), so a summary row never perturbs
    :func:`pta_finance.etl.normalize`'s per-FY sequence seeding — it is invisible to the
    counter that mints ``TXN-FY{yy}-NNNN`` ids (workspace ``code-quality`` rule: grep all
    downstream consumers when introducing an id shape; this id's only consumer of note is
    that regex, asserted in ``tests/test_ids.py``).
    """
    base = f"TXN-FY{_yy(fy)}-SUM-{slugify(category)}"
    if grade is not None and str(grade).strip() != "":
        base = f"{base}-g{slugify(str(grade))}"
    return base


def receipt_id(fy: int, seq: int) -> str:
    """Receipt id, e.g. ``RCP-FY26-0001``."""
    return f"RCP-FY{_yy(fy)}-{seq:04d}"


def budget_id(fy: int, category: str, grade: str | None = None) -> str:
    """Budget id, e.g. ``BUD-FY26-supplies`` or ``BUD-FY26-supplies-g3``."""
    base = f"BUD-FY{_yy(fy)}-{slugify(category)}"
    if grade is not None and str(grade).strip() != "":
        base = f"{base}-g{slugify(str(grade))}"
    return base


def event_id(fy: int, name: str) -> str:
    """Event id, e.g. ``EVT-FY26-fall-festival``."""
    return f"EVT-FY{_yy(fy)}-{slugify(name)}"
