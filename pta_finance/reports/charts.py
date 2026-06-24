"""Deterministic, headless charts (matplotlib **Agg** backend) -> PNG bytes.

The Agg backend is selected via :func:`matplotlib.use` **before** ``pyplot`` is imported, so
rendering never opens a window or needs a browser/display — it works the same in CI as on a
dev box. Each function returns raw PNG ``bytes``; :func:`png_data_uri` wraps those bytes as a
base64 ``data:`` URI so a chart embeds directly in the report HTML and the report stays a
single self-contained file (no sidecar image files).

All charts take plain dataclass sequences from :mod:`pta_finance.reports.builder` /
:mod:`pta_finance.analytics` — no Google or disk I/O here.
"""

from __future__ import annotations

import base64
from io import BytesIO
from typing import TYPE_CHECKING

import matplotlib

# Select the non-interactive Agg backend BEFORE importing pyplot — headless + deterministic.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (must follow matplotlib.use)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from matplotlib.figure import Figure

    from pta_finance.analytics.trends import YearAmounts
    from pta_finance.reports.builder import CategoryLine, GradeLine

__all__ = [
    "png_data_uri",
    "by_grade_bar_png",
    "by_category_bar_png",
    "multi_year_trend_png",
]

# A fixed figure size + DPI so output bytes are stable run-to-run (deterministic in CI).
_FIGSIZE = (7.0, 4.0)
_DPI = 100


def png_data_uri(png_bytes: bytes) -> str:
    """Wrap raw PNG ``bytes`` as a base64 ``data:image/png`` URI for inline ``<img src>``."""
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _render(fig: Figure) -> bytes:
    """Render a finished figure to PNG bytes (Agg) and release it.

    ``tight`` layout + a fixed DPI keep the output stable; the figure is always closed so a
    long batch never leaks figures.
    """
    buffer = BytesIO()
    fig.tight_layout()
    fig.savefig(buffer, format="png", dpi=_DPI)
    plt.close(fig)
    return buffer.getvalue()


def by_grade_bar_png(by_grade: Sequence[GradeLine]) -> bytes:
    """Bar chart of net allocation per grade (one bar per grade). PNG bytes."""
    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    labels = [g.grade for g in by_grade]
    values = [float(g.net) for g in by_grade]
    ax.bar(labels, values, color="#4C72B0")
    ax.set_title("Net allocation by grade")
    ax.set_xlabel("Grade")
    ax.set_ylabel("Net ($)")
    ax.axhline(0, color="#333333", linewidth=0.8)
    return _render(fig)


def by_category_bar_png(by_category: Sequence[CategoryLine]) -> bytes:
    """Bar chart of expense per category (one bar per category). PNG bytes."""
    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    labels = [c.category for c in by_category]
    values = [float(c.expense) for c in by_category]
    ax.bar(labels, values, color="#C44E52")
    ax.set_title("Expense by category")
    ax.set_xlabel("Category")
    ax.set_ylabel("Expense ($)")
    fig.autofmt_xdate(rotation=30)
    return _render(fig)


def multi_year_trend_png(by_year: Sequence[YearAmounts]) -> bytes:
    """Line chart of income vs expense across fiscal years (multi-year trend). PNG bytes."""
    fig, ax = plt.subplots(figsize=_FIGSIZE, dpi=_DPI)
    years = [str(y.fiscal_year) for y in by_year]
    income = [float(y.income) for y in by_year]
    expense = [float(y.expense) for y in by_year]
    ax.plot(years, income, marker="o", label="Income", color="#55A868")
    ax.plot(years, expense, marker="o", label="Expense", color="#C44E52")
    ax.set_title("Income vs expense by fiscal year")
    ax.set_xlabel("Fiscal year")
    ax.set_ylabel("Amount ($)")
    ax.legend()
    return _render(fig)
