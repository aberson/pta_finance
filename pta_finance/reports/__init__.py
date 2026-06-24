"""Monthly report generation — pure-template, two variants (internal + external).

The report data model is computed ONCE by :mod:`pta_finance.reports.builder` from the
analytics layer (:mod:`pta_finance.analytics`), then rendered by
:mod:`pta_finance.reports.render` into HTML via two Jinja2 templates. Charts are
matplotlib (Agg backend) PNGs embedded as base64 ``data:`` URIs so each report is a
single self-contained HTML file (:mod:`pta_finance.reports.charts`).

Two variants, with a hard PII boundary between them:

* **internal** — full ledger detail: org/school name, per-category budget variance,
  by-grade allocation, and recent transactions WITH payee, memo, and receipt links.
* **external** — public-safe ONLY: period, income/expense totals, by-grade allocation,
  fundraising progress, headline budget numbers. It carries **no** payee names, receipt
  links, memos, or any member PII. This is enforced at runtime by
  :func:`pta_finance.reports.builder._assert_external_safe`, which scans the external
  data model for any denylisted PII-shaped field and raises
  :class:`~pta_finance.reports.builder.ExternalReportPIIError` before the model is ever
  returned or rendered (workspace ``security`` rule: a public-facing safety control must
  be a guard, not documentation).
"""

from __future__ import annotations

from pta_finance.reports.builder import (
    EXTERNAL_PII_DENYLIST,
    ExternalReport,
    ExternalReportPIIError,
    InternalReport,
    ReportTransaction,
    build_external_report,
    build_internal_report,
    build_reports,
)
from pta_finance.reports.render import render_external, render_internal

__all__ = [
    "EXTERNAL_PII_DENYLIST",
    "ExternalReport",
    "ExternalReportPIIError",
    "InternalReport",
    "ReportTransaction",
    "build_external_report",
    "build_internal_report",
    "build_reports",
    "render_external",
    "render_internal",
]
