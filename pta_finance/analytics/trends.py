"""Multi-year trend series over the ledger — fundraising/spend by year + YoY (exact money).

Both functions consume the frame built by :func:`pta_finance.analytics.aggregate.build_frame`
and aggregate over the signed integer-cents column, so every figure is exact (no dollar
float). They return plain dataclasses of :class:`~decimal.Decimal` dollar values.

* :func:`fundraising_and_spend_by_year` — total income (fundraising) and total expense per
  fiscal-year label, oldest year first. This is the input the Phase-4 one-year-ahead
  forecaster will consume.
* :func:`year_over_year` — absolute + percent change in income and expense between each pair
  of CONSECUTIVE fiscal years present in the data.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from pta_finance.analytics.aggregate import (
    AMOUNT_CENTS,
    FISCAL_YEAR_INT,
    cents_to_dollars,
)

__all__ = [
    "YearAmounts",
    "fundraising_and_spend_by_year",
    "YoYChange",
    "year_over_year",
]


@dataclass(frozen=True)
class YearAmounts:
    """Total income (fundraising) and total expense for one fiscal year (exact dollars)."""

    fiscal_year: int
    income: Decimal
    expense: Decimal
    net: Decimal


def fundraising_and_spend_by_year(frame: pd.DataFrame) -> list[YearAmounts]:
    """Total income (fundraising) and total expense per ``fiscal_year``, oldest first.

    Sums the signed integer-cents column grouped by the integer fiscal-year label, so the
    per-year figures are exact.
    """
    if frame.empty:
        return []
    out: list[YearAmounts] = []
    for fiscal_year, group in frame.groupby(FISCAL_YEAR_INT, sort=True):
        cents = group[AMOUNT_CENTS]
        income = int(cents[cents > 0].sum())
        expense = int(-cents[cents < 0].sum())
        out.append(
            YearAmounts(
                fiscal_year=int(fiscal_year),
                income=cents_to_dollars(income),
                expense=cents_to_dollars(expense),
                net=cents_to_dollars(income - expense),
            )
        )
    return out


@dataclass(frozen=True)
class YoYChange:
    """Year-over-year change between two consecutive fiscal years.

    ``*_change`` are absolute dollar deltas (this year minus prior year); ``*_pct`` are
    percent changes relative to the prior year, or ``None`` when the prior year was zero
    (percent change is undefined against a zero base).
    """

    prior_year: int
    year: int
    income_change: Decimal
    income_pct: Decimal | None
    expense_change: Decimal
    expense_pct: Decimal | None


def year_over_year(frame: pd.DataFrame) -> list[YoYChange]:
    """Absolute + percent YoY change in income and expense between consecutive years.

    Builds on :func:`fundraising_and_spend_by_year` (oldest first) and emits one
    :class:`YoYChange` per adjacent pair of fiscal years actually present in the data — a gap
    year with no transactions is simply absent, so a comparison always spans the two nearest
    populated years. Percent change is ``None`` when the prior-year base was zero.
    """
    series = fundraising_and_spend_by_year(frame)
    out: list[YoYChange] = []
    for prior, current in zip(series, series[1:], strict=False):
        out.append(
            YoYChange(
                prior_year=prior.fiscal_year,
                year=current.fiscal_year,
                income_change=current.income - prior.income,
                income_pct=_pct(prior.income, current.income),
                expense_change=current.expense - prior.expense,
                expense_pct=_pct(prior.expense, current.expense),
            )
        )
    return out


def _pct(prior: Decimal, current: Decimal) -> Decimal | None:
    """Percent change from ``prior`` to ``current``, rounded to 0.01%, or ``None`` if base 0.

    Computed in :class:`~decimal.Decimal` so the percentage carries no binary-float drift.
    """
    if prior == 0:
        return None
    return ((current - prior) / prior * Decimal(100)).quantize(Decimal("0.01"))
