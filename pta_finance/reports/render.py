"""Render a report data model to a single self-contained HTML file (Jinja2).

The Jinja2 :class:`~jinja2.Environment` is built with **autoescape on** for HTML, so any
free-text ledger field (``payee``, ``memo``) is HTML-escaped on the way into the internal
template — a payee like ``<script>x</script>`` renders as inert text, never a live tag
(XSS / injection safety). Templates live in ``templates/`` next to this module and are loaded
via a :class:`~jinja2.FileSystemLoader` (the templates are data files, not Python).

Charts (:mod:`pta_finance.reports.charts`) are embedded as base64 ``data:`` URIs, so a
rendered report is one HTML file with no sidecar images.

PDF is optional and lazy: :func:`render_pdf` imports ``weasyprint`` only when called. That
dependency lives behind the ``[pdf]`` extra and is NOT installed in dev, so importing this
module never requires it — only an explicit PDF request pays the cost.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader

from pta_finance.reports import charts

if TYPE_CHECKING:
    from pta_finance.reports.builder import ExternalReport, InternalReport

__all__ = [
    "render_internal",
    "render_external",
    "render_pdf",
]

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_INTERNAL_TEMPLATE = "internal.html.j2"
_EXTERNAL_TEMPLATE = "external.html.j2"


def _autoescape_for(template_name: str | None) -> bool:
    """Autoescape predicate covering our ``*.html.j2`` template names.

    :func:`jinja2.select_autoescape` keys off the *final* filename suffix, so a
    ``*.html.j2`` template would NOT autoescape (its suffix is ``.j2``, not ``.html``).
    These report templates emit HTML and interpolate raw payee/memo text, so we enable
    autoescape whenever the name carries an ``.html``/``.htm``/``.xml`` extension anywhere
    (e.g. ``internal.html.j2``) and default to ON for an unknown name — fail safe.
    """
    if template_name is None:
        return True
    lowered = template_name.lower()
    return any(f".{ext}" in lowered for ext in ("html", "htm", "xml")) or lowered.endswith(".j2")


def _environment() -> Environment:
    """A Jinja2 Environment with HTML autoescape on and trimmed/lstripped blocks.

    Autoescape (via :func:`_autoescape_for`) escapes ``<``/``>``/``&``/quotes in every
    interpolated value for these ``*.html.j2`` templates, which is what makes the internal
    report safe to feed raw payee/memo text (a ``<script>`` payee renders inert).
    """
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=_autoescape_for,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_internal(model: InternalReport) -> str:
    """Render the full-detail internal report to an HTML string.

    Charts (by-grade, by-category, multi-year trend) are computed here from the model and
    embedded as inline base64 ``data:`` URIs so the output is a single self-contained file.
    """
    env = _environment()
    template = env.get_template(_INTERNAL_TEMPLATE)
    grade_chart = charts.png_data_uri(charts.by_grade_bar_png(model.by_grade))
    category_chart = charts.png_data_uri(charts.by_category_bar_png(model.by_category))
    return template.render(
        report=model,
        grade_chart_uri=grade_chart,
        category_chart_uri=category_chart,
    )


def render_external(model: ExternalReport) -> str:
    """Render the public-safe external report to an HTML string.

    Only the by-grade chart is embedded — the external variant exposes no per-category
    breakdown. The model is already PII-checked by ``build_external_report``; rendering adds
    no identifying field.
    """
    env = _environment()
    template = env.get_template(_EXTERNAL_TEMPLATE)
    grade_chart = charts.png_data_uri(charts.by_grade_bar_png(model.by_grade))
    return template.render(report=model, grade_chart_uri=grade_chart)


def render_pdf(html: str) -> bytes:
    """Render an HTML string to PDF bytes via WeasyPrint (lazy import; ``[pdf]`` extra).

    ``weasyprint`` is imported INSIDE the function so importing this module never requires the
    heavy Pango/Cairo native deps. Raises :class:`RuntimeError` with an actionable message if
    the ``[pdf]`` extra is not installed.
    """
    try:
        from weasyprint import HTML  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - exercised only with the [pdf] extra
        raise RuntimeError(
            "PDF output requires the optional '[pdf]' extra (WeasyPrint). "
            "Install it with: uv sync --extra pdf"
        ) from exc
    result: bytes = HTML(string=html).write_pdf()
    return result
