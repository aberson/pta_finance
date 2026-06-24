"""Entity dataclasses + row (de)serialization to/from Google-Sheet row dicts.

A "row" is a ``dict[str, str]`` keyed by a tab's schema columns (see :mod:`schema`),
with values as the strings a Google-Sheet cell holds. ``from_row`` parses such a dict
into a typed, frozen dataclass; ``to_row`` serializes back to a row dict in schema order.

Finance discipline:

* Monetary fields are :class:`decimal.Decimal`, never ``float``. :func:`parse_amount`
  tolerantly accepts ``"1,234.56"``, ``"$1,234.56"``, ``1234.56``, ``1234``, or a
  ``Decimal``, and raises :class:`ValueError` on un-parseable input. Optional monetary
  fields treat empty string / ``None`` as ``None``.
* Date fields are :class:`datetime.date`, parsed from ISO ``YYYY-MM-DD`` (or passed
  through if already a ``date``) by :func:`parse_date`.
* ``needs_review`` is a ``bool``.

Column names are imported from :mod:`schema` (the single source of truth); this module
never re-lists a tab's columns beyond the dataclass field names themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pta_finance import schema

__all__ = [
    "parse_amount",
    "parse_optional_amount",
    "parse_date",
    "parse_optional_date",
    "parse_bool",
    "format_amount",
    "format_optional_amount",
    "format_date",
    "format_optional_date",
    "format_bool",
    "Transaction",
    "Receipt",
    "BudgetLine",
    "Event",
]


# --- Scalar parsers --------------------------------------------------------


def parse_amount(value: Any) -> Decimal:
    """Parse a monetary value into a :class:`~decimal.Decimal`.

    Accepts ``Decimal``, ``int``, ``float`` (via its string form to avoid binary
    artefacts), or a string that may carry a currency symbol, thousands separators,
    surrounding whitespace, or parenthesised negatives (e.g. ``"(1,234.56)"``).

    Raises :class:`ValueError` on empty or un-parseable input. (ETL catches this to
    flag a row ``needs_review``; that handling is not this module's concern.)
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        # bool is an int subclass; a boolean is not a valid monetary amount.
        raise ValueError(f"not a valid monetary amount: {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Route through str() so 0.1 parses as Decimal("0.1"), not the binary tail.
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.strip()
        negative = False
        if cleaned.startswith("(") and cleaned.endswith(")"):
            negative = True
            cleaned = cleaned[1:-1].strip()
        cleaned = cleaned.replace("$", "").replace(",", "").strip()
        if cleaned == "":
            raise ValueError("empty string is not a valid monetary amount")
        try:
            result = Decimal(cleaned)
        except InvalidOperation as exc:
            raise ValueError(f"not a valid monetary amount: {value!r}") from exc
        return -result if negative else result
    raise ValueError(f"not a valid monetary amount: {value!r}")


def parse_optional_amount(value: Any) -> Decimal | None:
    """Like :func:`parse_amount`, but empty string / ``None`` -> ``None``."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return parse_amount(value)


def parse_date(value: Any) -> date:
    """Parse an ISO ``YYYY-MM-DD`` string into a :class:`datetime.date`.

    A value that is already a ``date`` is returned unchanged (a ``datetime`` is a
    ``date`` subclass; callers wanting a pure date should pass ``.date()``). Raises
    :class:`ValueError` on empty or un-parseable input.
    """
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned == "":
            raise ValueError("empty string is not a valid date")
        # date.fromisoformat raises ValueError on bad input — exactly what we want.
        return date.fromisoformat(cleaned)
    raise ValueError(f"not a valid date: {value!r}")


def parse_optional_date(value: Any) -> date | None:
    """Like :func:`parse_date`, but empty string / ``None`` -> ``None``."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return parse_date(value)


def parse_bool(value: Any) -> bool:
    """Parse a Sheet cell into a ``bool``.

    ``TRUE``/``1``/``yes``/``y``/``t`` (any case) -> ``True``; empty string / ``None``
    / ``FALSE``/``0``/``no``/``n``/``f`` -> ``False``.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int,)):
        return value != 0
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes", "y", "t"}:
        return True
    return False


# --- Scalar formatters (back to Sheet-cell strings) ------------------------


def format_amount(value: Decimal) -> str:
    """Format a Decimal plainly (no scientific notation, no thousands separators)."""
    # Normalize away a trailing exponent form while keeping the literal scale.
    return f"{value:f}"


def format_optional_amount(value: Decimal | None) -> str:
    """Format an optional Decimal; ``None`` -> empty string."""
    return "" if value is None else format_amount(value)


def format_date(value: date) -> str:
    """Format a date as ISO ``YYYY-MM-DD``."""
    return value.isoformat()


def format_optional_date(value: date | None) -> str:
    """Format an optional date as ISO; ``None`` -> empty string."""
    return "" if value is None else value.isoformat()


def format_bool(value: bool) -> str:
    """Format a bool as ``TRUE`` / ``FALSE`` (Sheets-native boolean text)."""
    return "TRUE" if value else "FALSE"


def _str(row: dict[str, Any], key: str) -> str:
    """Required string field: missing/None -> empty string, else stripped str."""
    value = row.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _opt_str(row: dict[str, Any], key: str) -> str | None:
    """Optional string field: missing/None/empty -> ``None``, else stripped str."""
    value = row.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# --- Entities --------------------------------------------------------------


@dataclass(frozen=True)
class Transaction:
    """A single ledger line (income or expense). See plan.md §4."""

    id: str
    date: date
    fiscal_year: str
    type: str
    amount: Decimal
    category: str
    grade: str | None
    payee: str
    memo: str
    budget_id: str | None
    receipt_id: str | None
    source: str
    entered_by: str | None
    created_at: str
    needs_review: bool

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Transaction:
        return cls(
            id=_str(row, "id"),
            date=parse_date(row["date"]),
            fiscal_year=_str(row, "fiscal_year"),
            type=_str(row, "type"),
            amount=parse_amount(row["amount"]),
            category=_str(row, "category"),
            grade=_opt_str(row, "grade"),
            payee=_str(row, "payee"),
            memo=_str(row, "memo"),
            budget_id=_opt_str(row, "budget_id"),
            receipt_id=_opt_str(row, "receipt_id"),
            source=_str(row, "source"),
            entered_by=_opt_str(row, "entered_by"),
            created_at=_str(row, "created_at"),
            needs_review=parse_bool(row.get("needs_review")),
        )

    def to_row(self) -> dict[str, str]:
        values: dict[str, str] = {
            "id": self.id,
            "date": format_date(self.date),
            "fiscal_year": self.fiscal_year,
            "type": self.type,
            "amount": format_amount(self.amount),
            "category": self.category,
            "grade": self.grade or "",
            "payee": self.payee,
            "memo": self.memo,
            "budget_id": self.budget_id or "",
            "receipt_id": self.receipt_id or "",
            "source": self.source,
            "entered_by": self.entered_by or "",
            "created_at": self.created_at,
            "needs_review": format_bool(self.needs_review),
        }
        return _ordered(values, schema.TRANSACTIONS_COLUMNS)


@dataclass(frozen=True)
class Receipt:
    """A receipt record linked to a transaction (Drive URL). See plan.md §4."""

    id: str
    txn_id: str
    drive_url: str
    description: str | None
    amount: Decimal | None
    date: date | None
    added_by: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Receipt:
        return cls(
            id=_str(row, "id"),
            txn_id=_str(row, "txn_id"),
            drive_url=_str(row, "drive_url"),
            description=_opt_str(row, "description"),
            amount=parse_optional_amount(row.get("amount")),
            date=parse_optional_date(row.get("date")),
            added_by=_opt_str(row, "added_by"),
            created_at=_str(row, "created_at"),
        )

    def to_row(self) -> dict[str, str]:
        values: dict[str, str] = {
            "id": self.id,
            "txn_id": self.txn_id,
            "drive_url": self.drive_url,
            "description": self.description or "",
            "amount": format_optional_amount(self.amount),
            "date": format_optional_date(self.date),
            "added_by": self.added_by or "",
            "created_at": self.created_at,
        }
        return _ordered(values, schema.RECEIPTS_COLUMNS)


@dataclass(frozen=True)
class BudgetLine:
    """A budgeted amount per category (optionally per grade) per FY. See plan.md §4."""

    id: str
    fiscal_year: str
    category: str
    grade: str | None
    budgeted_amount: Decimal
    notes: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> BudgetLine:
        return cls(
            id=_str(row, "id"),
            fiscal_year=_str(row, "fiscal_year"),
            category=_str(row, "category"),
            grade=_opt_str(row, "grade"),
            budgeted_amount=parse_amount(row["budgeted_amount"]),
            notes=_opt_str(row, "notes"),
        )

    def to_row(self) -> dict[str, str]:
        values: dict[str, str] = {
            "id": self.id,
            "fiscal_year": self.fiscal_year,
            "category": self.category,
            "grade": self.grade or "",
            "budgeted_amount": format_amount(self.budgeted_amount),
            "notes": self.notes or "",
        }
        return _ordered(values, schema.BUDGET_COLUMNS)


@dataclass(frozen=True)
class Event:
    """A calendar event (fundraiser/meeting). Defined now, used in Phase 2. See §4."""

    id: str
    fiscal_year: str
    name: str
    date: date
    type: str
    expected_income: Decimal | None
    expected_expense: Decimal | None
    nag_schedule: str | None
    notes: str | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Event:
        return cls(
            id=_str(row, "id"),
            fiscal_year=_str(row, "fiscal_year"),
            name=_str(row, "name"),
            date=parse_date(row["date"]),
            type=_str(row, "type"),
            expected_income=parse_optional_amount(row.get("expected_income")),
            expected_expense=parse_optional_amount(row.get("expected_expense")),
            nag_schedule=_opt_str(row, "nag_schedule"),
            notes=_opt_str(row, "notes"),
        )

    def to_row(self) -> dict[str, str]:
        values: dict[str, str] = {
            "id": self.id,
            "fiscal_year": self.fiscal_year,
            "name": self.name,
            "date": format_date(self.date),
            "type": self.type,
            "expected_income": format_optional_amount(self.expected_income),
            "expected_expense": format_optional_amount(self.expected_expense),
            "nag_schedule": self.nag_schedule or "",
            "notes": self.notes or "",
        }
        return _ordered(values, schema.EVENTS_COLUMNS)


def _ordered(values: dict[str, str], columns: tuple[str, ...]) -> dict[str, str]:
    """Return ``values`` reordered to match ``columns`` (the schema's column order).

    Asserts the value keys exactly match the schema columns so a dataclass/schema
    drift surfaces immediately rather than silently dropping or adding a cell.
    """
    if set(values) != set(columns):
        missing = set(columns) - set(values)
        extra = set(values) - set(columns)
        raise ValueError(
            f"row keys do not match schema columns; missing={sorted(missing)} extra={sorted(extra)}"
        )
    return {col: values[col] for col in columns}


# Sanity check at import time: every entity's dataclass fields match its schema
# columns one-to-one (order included). Cheap, and it turns a drift into an
# ImportError instead of a runtime surprise deep in a write path.
def _check_field_schema_alignment() -> None:
    pairs = (
        (Transaction, schema.TRANSACTIONS_COLUMNS),
        (Receipt, schema.RECEIPTS_COLUMNS),
        (BudgetLine, schema.BUDGET_COLUMNS),
        (Event, schema.EVENTS_COLUMNS),
    )
    for cls, columns in pairs:
        field_names = tuple(f.name for f in fields(cls))
        if field_names != columns:
            raise AssertionError(
                f"{cls.__name__} fields {field_names} do not match schema columns {columns}"
            )


_check_field_schema_alignment()
