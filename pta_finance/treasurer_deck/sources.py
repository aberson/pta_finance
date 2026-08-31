"""Read-only source capture, precedence, and typed provenance (plan section 5.3).

This module owns every way private finance data enters a Treasurer-deck run:

* :class:`ReadOnlySheetsReader` — a dedicated reader built from service-account
  credentials scoped exactly to ``spreadsheets.readonly``. It does **not** wrap or
  subclass the write-capable :class:`~pta_finance.sheets.SheetsClient`; it exposes only
  ``read_values`` (the evaluated projection the existing
  :func:`pta_finance.report_source.read_timeseries` parser consumes through the
  ``ValuesReader`` protocol) and ``read_grid`` (typed effective/user-entered/formula
  cell provenance through the Sheets ``spreadsheets.get`` grid-data surface). The
  source snapshot hashes the canonical grid cells, so formatted currency text is never
  the canonical money value.
* The operator-owned **Treasurer Briefing Inputs** contract (read-only tab) and the
  strict per-run **private override** envelope.
* Explicit **source precedence** (override > briefing > canonical dataset/validated
  reimbursement summary > derived) with fail-closed conflicts, stale-override
  rejection, required-gap blocking, and optional-omission logging.
* The **schema-v2 reimbursement summary adapter**, reusing the strict
  :func:`pta_finance.reimbursement_report.load_bundle` loader. Outstanding facts come
  from the validated summary, active tickets, settled workflow state, and supplemental
  payment events; unmatched supplemental evidence is never counted as an obligation,
  and person-level detail remains internal.

Bounded Google retry policy is deliberately NOT implemented here; it arrives with the
dedicated ``google_client`` (plan Step 19). Errors from the transport propagate and
fail the capture closed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from pta_finance import models as base_models
from pta_finance import reimbursement_report
from pta_finance.treasurer_deck.models import (
    AUDIENCE_INTERNAL,
    AUDIENCE_PUBLIC,
    SOURCE_ALIAS_BRIEFING,
    SOURCE_ALIAS_BUDGET_TIMESERIES,
    SOURCE_ALIAS_OVERRIDE,
    SOURCE_ALIAS_REIMBURSEMENT,
    UNIT_BOOLEAN,
    UNIT_COUNT,
    UNIT_DATE,
    UNIT_PERCENT,
    UNIT_TEXT,
    ConflictCandidate,
    ConflictRecord,
    ContractError,
    FactRecord,
    FactSnapshot,
    FactValue,
    GraphicDataset,
    MissingFact,
    SourceSnapshot,
    TreasurerDeckError,
    as_mapping,
    as_str,
    canonical_json,
    check_schema_version,
    combine_audience,
    ensure_unique,
    json_sha256,
    require_keys,
    restrict_audience,
    sha256_hex,
    validate_fact_id,
    validate_run_id,
    validate_sha256,
    validate_unit,
)

# --- read-only Sheets access ---------------------------------------------------------

SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
READONLY_SCOPES: tuple[str, ...] = (SHEETS_READONLY_SCOPE,)
SHEETS_API_BASE = "https://sheets.googleapis.com/v4/"

_GRID_FIELDS = (
    "sheets(properties(title),data(startRow,startColumn,"
    "rowData(values(userEnteredValue,effectiveValue,effectiveFormat.numberFormat.type))))"
)


class SheetsTransport(Protocol):
    """Minimal GET-only transport seam; the live implementation and fakes share it."""

    def get_json_text(self, path: str, params: Mapping[str, str]) -> str:
        """Return the raw JSON text of one ``GET`` against the Sheets v4 API."""
        ...


class ServiceAccountTransport:
    """Live GET-only transport pinned to exactly the ``spreadsheets.readonly`` scope.

    The scope is re-checked at construction with exact tuple equality — a credential
    carrying any other scope set is rejected before a single request is made.
    """

    def __init__(self, service_account_path: Path) -> None:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials

        credentials = Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            str(service_account_path), scopes=list(READONLY_SCOPES)
        )
        scopes = tuple(credentials.scopes or ())
        if scopes != READONLY_SCOPES:
            raise ContractError(
                "read-only Sheets credentials must carry exactly the spreadsheets.readonly scope"
            )
        self._session = AuthorizedSession(credentials)  # type: ignore[no-untyped-call]

    def get_json_text(self, path: str, params: Mapping[str, str]) -> str:
        response = self._session.get(SHEETS_API_BASE + path, params=dict(params), timeout=60)
        status = int(response.status_code)
        if status != 200:
            # Never echo the response body: it may carry private resource detail.
            raise TreasurerDeckError(f"Sheets read failed with HTTP status {status}")
        text = response.text
        if not isinstance(text, str):  # pragma: no cover - requests contract
            raise TreasurerDeckError("Sheets read returned a non-text body")
        return text


@dataclass(frozen=True)
class CellSnapshot:
    """One captured cell: typed effective value, typed entered value or formula.

    ``row``/``column`` are zero-based within the returned rectangle; ``a1`` is the
    absolute coordinate. Exactly one of ``user_entered_value``/``formula`` is set for
    a non-empty entered cell.
    """

    row: int
    column: int
    a1: str
    effective_value: str | bool | int | Decimal | None
    user_entered_value: str | bool | int | Decimal | None
    formula: str | None
    number_format: str | None

    def to_json(self) -> dict[str, object]:
        return {
            "row": self.row,
            "column": self.column,
            "a1": self.a1,
            "effective_value": _tagged_scalar(self.effective_value),
            "user_entered_value": _tagged_scalar(self.user_entered_value),
            "formula": self.formula,
            "number_format": self.number_format,
        }


@dataclass(frozen=True)
class GridSnapshot:
    """One captured worksheet rectangle with row-major typed cells."""

    source_alias: str
    worksheet_title: str
    requested_range: str
    returned_range: str
    source_revision: str | None
    cells: tuple[CellSnapshot, ...]

    def content_sha256(self) -> str:
        """Canonical hash over the typed cells — never over formatted display text."""
        return json_sha256(
            {
                "worksheet_title": self.worksheet_title,
                "returned_range": self.returned_range,
                "cells": [cell.to_json() for cell in self.cells],
            }
        )


def _tagged_scalar(value: str | bool | int | Decimal | None) -> dict[str, object] | None:
    """Type-tagged canonical form so ``"12.5"`` and the number 12.5 never collide."""
    if value is None:
        return None
    if isinstance(value, bool):
        return {"kind": "boolean", "value": value}
    if isinstance(value, int):
        return {"kind": "number", "value": Decimal(value)}
    if isinstance(value, Decimal):
        return {"kind": "number", "value": value}
    return {"kind": "string", "value": value}


def _column_letters(index: int) -> str:
    letters = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _a1(column_index: int, row_index: int) -> str:
    return f"{_column_letters(column_index)}{row_index + 1}"


def _quote_tab(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def _parse_json_text(text: str, context: str) -> Mapping[str, Any]:
    try:
        value = json.loads(
            text,
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ContractError(f"{context}: response is not valid JSON") from exc
    return as_mapping(value, context)


def _reject_constant(name: str) -> None:
    raise ContractError(f"non-finite JSON constant is rejected: {name}")


def _typed_extended_value(
    value: Mapping[str, Any] | None, context: str
) -> tuple[str | bool | int | Decimal | None, str | None]:
    """Decode one Sheets ``ExtendedValue`` into ``(typed value, formula)``."""
    if value is None:
        return None, None
    keys = set(value)
    if not keys:
        return None, None
    if len(keys) != 1:
        raise ContractError(f"{context}: ExtendedValue must carry exactly one variant")
    if "stringValue" in keys:
        return as_str(value["stringValue"], f"{context}.stringValue"), None
    if "boolValue" in keys:
        raw_bool = value["boolValue"]
        if not isinstance(raw_bool, bool):
            raise ContractError(f"{context}.boolValue must be a boolean")
        return raw_bool, None
    if "numberValue" in keys:
        number = value["numberValue"]
        if isinstance(number, bool) or not isinstance(number, int | Decimal):
            raise ContractError(f"{context}.numberValue must decode as int/Decimal")
        if isinstance(number, Decimal) and not number.is_finite():
            raise ContractError(f"{context}.numberValue must be finite")
        return number, None
    if "formulaValue" in keys:
        return None, as_str(value["formulaValue"], f"{context}.formulaValue")
    if "errorValue" in keys:
        raise ContractError(f"{context}: cell evaluates to an error; capture fails closed")
    raise ContractError(f"{context}: unknown ExtendedValue variant {sorted(keys)}")


class ReadOnlySheetsReader:
    """Dedicated read-only Sheet reader (no write method exists on this class).

    Satisfies :class:`pta_finance.report_source.ValuesReader` structurally, so the
    existing timeseries parser is reused through the smallest possible seam while the
    deck path never holds a client that can write to a spreadsheet.
    """

    def __init__(
        self,
        spreadsheet_id: str,
        transport: SheetsTransport,
        *,
        source_alias: str = SOURCE_ALIAS_BUDGET_TIMESERIES,
    ) -> None:
        if not spreadsheet_id:
            raise ContractError("spreadsheet_id must be non-empty")
        self._spreadsheet_id = spreadsheet_id
        self._transport = transport
        self._source_alias = source_alias

    @classmethod
    def from_service_account(
        cls,
        service_account_path: Path,
        spreadsheet_id: str,
        *,
        source_alias: str = SOURCE_ALIAS_BUDGET_TIMESERIES,
    ) -> ReadOnlySheetsReader:
        """Build the live reader from service-account credentials (readonly scope)."""
        return cls(
            spreadsheet_id,
            ServiceAccountTransport(service_account_path),
            source_alias=source_alias,
        )

    @property
    def source_alias(self) -> str:
        return self._source_alias

    def read_values(self, tab: str) -> list[list[str]]:
        """One worksheet's evaluated grid as formatted strings (header row included).

        This is only the evaluated projection the existing parser needs; it is not
        used as provenance — :meth:`read_grid` is.
        """
        encoded = quote(_quote_tab(tab), safe="")
        payload = self._transport.get_json_text(
            f"spreadsheets/{quote(self._spreadsheet_id, safe='')}/values/{encoded}",
            {"valueRenderOption": "FORMATTED_VALUE", "majorDimension": "ROWS"},
        )
        body = _parse_json_text(payload, "values read")
        rows_raw = body.get("values", [])
        if isinstance(rows_raw, str | bytes) or not isinstance(rows_raw, Sequence):
            raise ContractError("values read: 'values' must be an array")
        grid: list[list[str]] = []
        for row_raw in rows_raw:
            if isinstance(row_raw, str | bytes) or not isinstance(row_raw, Sequence):
                raise ContractError("values read: each row must be an array")
            grid.append([_formatted_cell_text(cell) for cell in row_raw])
        return grid

    def read_grid(self, tab: str, a1_range: str) -> GridSnapshot:
        """Typed grid capture through the ``spreadsheets.get`` grid-data surface."""
        requested = f"{_quote_tab(tab)}!{a1_range}"
        payload = self._transport.get_json_text(
            f"spreadsheets/{quote(self._spreadsheet_id, safe='')}",
            {
                "includeGridData": "true",
                "ranges": requested,
                "fields": _GRID_FIELDS,
            },
        )
        body = _parse_json_text(payload, "grid read")
        sheets = body.get("sheets")
        if not isinstance(sheets, Sequence) or len(sheets) != 1:
            raise ContractError("grid read must return exactly one worksheet")
        sheet = as_mapping(sheets[0], "grid read sheet")
        properties = as_mapping(sheet.get("properties", {}), "grid read properties")
        title = as_str(properties.get("title", ""), "grid read title")
        if title != tab:
            raise ContractError("grid read returned a different worksheet than requested")
        data_blocks = sheet.get("data")
        if not isinstance(data_blocks, Sequence) or len(data_blocks) != 1:
            raise ContractError("grid read must return exactly one data block")
        block = as_mapping(data_blocks[0], "grid read data")
        start_row = block.get("startRow", 0)
        start_column = block.get("startColumn", 0)
        if not isinstance(start_row, int) or not isinstance(start_column, int):
            raise ContractError("grid read start coordinates must be integers")
        row_data_raw = block.get("rowData", [])
        if isinstance(row_data_raw, str | bytes) or not isinstance(row_data_raw, Sequence):
            raise ContractError("grid read rowData must be an array")
        rows: list[Sequence[Any]] = []
        for row_raw in row_data_raw:
            row_map = as_mapping(row_raw, "grid read row")
            values = row_map.get("values", [])
            if isinstance(values, str | bytes) or not isinstance(values, Sequence):
                raise ContractError("grid read row values must be an array")
            rows.append(values)
        column_count = max((len(row) for row in rows), default=0)
        cells: list[CellSnapshot] = []
        for row_index, row in enumerate(rows):
            for column_index in range(column_count):
                raw_cell: Mapping[str, Any] = {}
                if column_index < len(row):
                    raw_cell = as_mapping(row[column_index], "grid read cell")
                context = f"cell {_a1(start_column + column_index, start_row + row_index)}"
                effective, effective_formula = _typed_extended_value(
                    _opt_mapping(raw_cell.get("effectiveValue"), f"{context}.effectiveValue"),
                    f"{context}.effectiveValue",
                )
                if effective_formula is not None:
                    raise ContractError(f"{context}: effective values cannot be formulas")
                entered, formula = _typed_extended_value(
                    _opt_mapping(raw_cell.get("userEnteredValue"), f"{context}.userEnteredValue"),
                    f"{context}.userEnteredValue",
                )
                number_format = _number_format_kind(raw_cell)
                cells.append(
                    CellSnapshot(
                        row=row_index,
                        column=column_index,
                        a1=_a1(start_column + column_index, start_row + row_index),
                        effective_value=effective,
                        user_entered_value=entered,
                        formula=formula,
                        number_format=number_format,
                    )
                )
        if rows and column_count:
            returned = (
                f"{_a1(start_column, start_row)}:"
                f"{_a1(start_column + column_count - 1, start_row + len(rows) - 1)}"
            )
        else:
            returned = ""
        return GridSnapshot(
            source_alias=self._source_alias,
            worksheet_title=title,
            requested_range=requested,
            returned_range=returned,
            source_revision=None,
            cells=tuple(cells),
        )


def _opt_mapping(value: object, context: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    return as_mapping(value, context)


def _number_format_kind(raw_cell: Mapping[str, Any]) -> str | None:
    fmt = raw_cell.get("effectiveFormat")
    if fmt is None:
        return None
    number_format = as_mapping(fmt, "effectiveFormat").get("numberFormat")
    if number_format is None:
        return None
    kind = as_mapping(number_format, "numberFormat").get("type")
    if kind is None:
        return None
    return as_str(kind, "numberFormat.type")


def _formatted_cell_text(cell: object) -> str:
    if cell is None:
        return ""
    if isinstance(cell, bool):
        return "TRUE" if cell else "FALSE"
    if isinstance(cell, str):
        return cell
    if isinstance(cell, int):
        return str(cell)
    if isinstance(cell, Decimal):
        return str(cell)
    raise ContractError(f"unsupported cell type from values read: {type(cell).__name__}")


def grid_source_snapshot(
    grid: GridSnapshot, *, locator: str, captured_at: datetime, contract_version: int = 1
) -> SourceSnapshot:
    """Private provenance record for one captured grid (canonical cell hash)."""
    return SourceSnapshot(
        source_alias=grid.source_alias,
        captured_at=captured_at,
        contract_version=contract_version,
        locator=locator,
        source_revision=grid.source_revision,
        content_sha256=grid.content_sha256(),
        captured_ranges=(grid.requested_range,),
    )


# --- code-owned audience policy (section 5.3) ----------------------------------------

#: Budget Timeseries fields whose values may feed public aggregates.
PUBLIC_ELIGIBLE_TIMESERIES_FIELDS: tuple[str, ...] = (
    "fiscal_year",
    "type",
    "measure",
    "category_group",
    "strategic_group",
    "strategic_goal",
    "amount",
)

#: Fields that are always internal (raw category, tab, grade, people, free text).
INTERNAL_TIMESERIES_FIELDS: tuple[str, ...] = (
    "raw_category",
    "source_tab",
    "grade",
    "is_fundraiser",
    "notes",
)


def timeseries_field_audience(field_name: str) -> str:
    """Maximum audience eligibility for one Budget Timeseries field.

    Unknown fields default to internal; nothing an input file says can raise this.
    """
    if field_name in PUBLIC_ELIGIBLE_TIMESERIES_FIELDS:
        return AUDIENCE_PUBLIC
    return AUDIENCE_INTERNAL


# --- operator-owned Treasurer Briefing Inputs (read-only tab) ------------------------

BRIEFING_TAB = "Treasurer Briefing Inputs"
BRIEFING_COLUMNS: tuple[str, ...] = (
    "fact_key",
    "period",
    "label",
    "value",
    "unit",
    "basis",
    "as_of_date",
    "audience",
    "definition",
    "source_note",
)

_COUNT_RE = re.compile(r"\A-?\d+\Z", re.ASCII)


def _parse_briefing_value(text: str, unit: str, context: str) -> FactValue:
    if unit == UNIT_TEXT:
        if not text:
            raise ContractError(f"{context}: text value must be non-empty")
        return text
    if unit == UNIT_BOOLEAN:
        # Exact closed set (Sheets checkbox output); parse_bool's lenient coercion
        # would turn a malformed cell into an available False fact (fail-open).
        lowered = text.casefold()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        raise ContractError(f"{context}: boolean value must be TRUE or FALSE")
    if unit == UNIT_COUNT:
        if _COUNT_RE.match(text) is None:
            raise ContractError(f"{context}: count value must be an integer")
        return int(text)
    if unit == UNIT_DATE:
        return base_models.parse_date(text)
    candidate_text = text[:-1].strip() if unit == UNIT_PERCENT and text.endswith("%") else text
    try:
        amount = base_models.parse_amount(candidate_text)
    except ValueError as exc:
        raise ContractError(f"{context}: malformed money/percent value") from exc
    if not amount.is_finite():
        raise ContractError(f"{context}: non-finite money/percent value")
    return amount


def parse_briefing_inputs(
    values: Sequence[Sequence[str]],
    *,
    captured_at: datetime,
    source_hash: str,
    locator: str,
) -> tuple[FactRecord, ...]:
    """Parse the read-only briefing tab into operator-supplied fact candidates.

    One exact header row; rows are uniquely identified by
    ``(fact_key, period, label, basis)``. ``audience`` is an optional restriction
    only — blank or unrecognized values become internal.
    """
    validate_sha256(source_hash, "briefing source hash")
    if not values:
        raise ContractError("briefing tab is empty (missing its header row)")
    header = tuple(str(cell).strip() for cell in values[0])
    if header != BRIEFING_COLUMNS:
        raise ContractError(
            "briefing tab header does not match the read-only contract "
            f"(expected {list(BRIEFING_COLUMNS)})"
        )
    facts: list[FactRecord] = []
    seen_rows: set[tuple[str, str, str, str]] = set()
    for row_number, raw_row in enumerate(values[1:], start=2):
        cells = [str(cell).strip() for cell in raw_row]
        if all(cell == "" for cell in cells):
            continue
        padded = cells + [""] * (len(BRIEFING_COLUMNS) - len(cells))
        if len(padded) > len(BRIEFING_COLUMNS):
            raise ContractError(f"briefing row {row_number} has extra columns")
        row = dict(zip(BRIEFING_COLUMNS, padded, strict=True))
        context = f"briefing row {row_number}"
        fact_key = row["fact_key"]
        period = row["period"] or None
        row_key = (fact_key, row["period"], row["label"], row["basis"])
        if row_key in seen_rows:
            raise ContractError(f"{context}: duplicate (fact_key, period, label, basis)")
        seen_rows.add(row_key)
        fact_id = fact_key if period is None else f"{fact_key}@{period}"
        validate_fact_id(fact_id, f"{context}.fact_key")
        unit = validate_unit(row["unit"], f"{context}.unit")
        as_of = row["as_of_date"]
        facts.append(
            FactRecord(
                fact_id=fact_id,
                label=row["label"],
                value=_parse_briefing_value(row["value"], unit, context),
                unit=unit,
                basis=row["basis"],
                origin="operator_supplied",
                audience=restrict_audience(AUDIENCE_PUBLIC, row["audience"] or None),
                status="available",
                period=period,
                as_of_date=None if not as_of else base_models.parse_date(as_of),
                source_alias=SOURCE_ALIAS_BRIEFING,
                locator=f"{locator}!row{row_number}",
                source_revision=None,
                source_hash=source_hash,
                captured_at=captured_at,
                note=row["definition"] or row["source_note"] or None,
            )
        )
    ensure_unique((fact.fact_id for fact in facts), "briefing fact_id")
    return tuple(facts)


# --- per-run private override envelope -----------------------------------------------

OVERRIDE_INITIAL_REASON = "initial_structured_input"
TABLE_GRAPHIC_KEY = "participation_table"


@dataclass(frozen=True)
class OverrideFact:
    """One validated override fact entry (stated reason + expected superseded hash)."""

    fact: FactRecord
    reason: str
    replaces_source_hash: str | None

    def __post_init__(self) -> None:
        if not self.reason:
            raise ContractError(f"{self.fact.fact_id}: override reason must be stated")
        if self.replaces_source_hash is not None:
            validate_sha256(self.replaces_source_hash, "replaces_source_hash")
        if self.fact.source_alias != SOURCE_ALIAS_OVERRIDE:
            raise ContractError(
                f"{self.fact.fact_id}: override facts carry source_alias run_override"
            )
        if self.fact.status != "available":
            raise ContractError(f"{self.fact.fact_id}: override facts must be available")


@dataclass(frozen=True)
class TableGraphicOverride:
    """The one explicit structured ingress for participation-table data.

    ``input`` carries the section-5.5 ``TableGraphicInput`` shape; its deep validation
    lands with the graphics catalog (plan Step 16). The envelope contract is enforced
    here: the hash is required when replacing an existing structured source and may be
    ``null`` only for the first structured input with the fixed initial reason.
    """

    graphic_key: str
    input: Mapping[str, Any]
    reason: str
    replaces_source_hash: str | None

    def __post_init__(self) -> None:
        if self.graphic_key != TABLE_GRAPHIC_KEY:
            raise ContractError(f"table graphic override key must be {TABLE_GRAPHIC_KEY!r}")
        if not self.reason:
            raise ContractError("table graphic override reason must be stated")
        if self.replaces_source_hash is None:
            if self.reason != OVERRIDE_INITIAL_REASON:
                raise ContractError(
                    "replaces_source_hash may be null only for the first structured "
                    f"input with reason={OVERRIDE_INITIAL_REASON!r}"
                )
        else:
            validate_sha256(self.replaces_source_hash, "replaces_source_hash")


@dataclass(frozen=True)
class OverrideDocument:
    """The strict private override envelope (``schema_version=1``)."""

    facts: tuple[OverrideFact, ...]
    table_graphics: tuple[TableGraphicOverride, ...]

    def __post_init__(self) -> None:
        ensure_unique((entry.fact.fact_id for entry in self.facts), "override fact_id")

    def fact_by_id(self) -> dict[str, OverrideFact]:
        return {entry.fact.fact_id: entry for entry in self.facts}


def parse_override_document(value: Mapping[str, Any]) -> OverrideDocument:
    """Strictly parse the override envelope; unknown root or entry fields reject."""
    context = "override"
    check_schema_version(value, context)
    require_keys(value, required=("schema_version", "facts", "table_graphics"), context=context)
    facts_raw = value["facts"]
    tables_raw = value["table_graphics"]
    if isinstance(facts_raw, str | bytes) or not isinstance(facts_raw, Sequence):
        raise ContractError(f"{context}.facts must be an array")
    if isinstance(tables_raw, str | bytes) or not isinstance(tables_raw, Sequence):
        raise ContractError(f"{context}.table_graphics must be an array")
    facts: list[OverrideFact] = []
    for position, raw in enumerate(facts_raw):
        entry = as_mapping(raw, f"{context}.facts[{position}]")
        entry_context = f"{context}.facts[{position}]"
        entry_keys = dict(entry)
        reason = as_str(entry_keys.pop("reason", None), f"{entry_context}.reason")
        replaces = entry_keys.pop("replaces_source_hash", None)
        replaces_text = (
            None if replaces is None else as_str(replaces, f"{entry_context}.replaces_source_hash")
        )
        if entry_keys.get("source_hash") is None:
            # The override entry's own content hash is its source hash: the operator
            # file IS the source, so provenance stays verifiable without self-reference.
            entry_keys["source_hash"] = json_sha256(
                {"entry": {key: item for key, item in entry.items() if key != "source_hash"}}
            )
        fact = FactRecord.from_json(entry_keys, entry_context)
        facts.append(OverrideFact(fact=fact, reason=reason, replaces_source_hash=replaces_text))
    tables: list[TableGraphicOverride] = []
    for position, raw in enumerate(tables_raw):
        entry = as_mapping(raw, f"{context}.table_graphics[{position}]")
        entry_context = f"{context}.table_graphics[{position}]"
        require_keys(
            entry,
            required=("graphic_key", "input", "reason", "replaces_source_hash"),
            context=entry_context,
        )
        replaces_value = entry["replaces_source_hash"]
        tables.append(
            TableGraphicOverride(
                graphic_key=as_str(entry["graphic_key"], f"{entry_context}.graphic_key"),
                input=as_mapping(entry["input"], f"{entry_context}.input"),
                reason=as_str(entry["reason"], f"{entry_context}.reason"),
                replaces_source_hash=(
                    None
                    if replaces_value is None
                    else as_str(replaces_value, f"{entry_context}.replaces_source_hash")
                ),
            )
        )
    return OverrideDocument(facts=tuple(facts), table_graphics=tuple(tables))


# --- schema-v2 reimbursement summary adapter -----------------------------------------


def load_reimbursement_summary(
    bundle_path: Path,
    *,
    captured_at: datetime,
    currency_code: str = "USD",
) -> tuple[tuple[FactRecord, ...], SourceSnapshot]:
    """Aggregate outstanding/settlement/payment facts from the strict v2 bundle.

    Reuses :func:`pta_finance.reimbursement_report.load_bundle` (which migrates a v1
    bundle in memory and fails closed on any contract violation). Derived facts:

    * ``reimbursements.outstanding_total`` — approved, awaiting payment (``pending``);
      unmatched supplemental evidence is never counted as an obligation.
    * ``reimbursements.approved_total`` — approved across review rows (``committed``).
    * ``reimbursements.settled_paid_total`` — approved value of settled tickets
      (``spent``; covers ``PAID`` and ``PAID_PRIOR`` settlements).
    * ``reimbursements.recorded_payments_total`` — sum of ``PAYMENT_RECORDED``
      supplemental events (``spent``; evidence-backed payments only).
    * ``reimbursements.active_count`` / ``settled_count`` / ``unreviewed_count``
      (internal: the closed public policy covers approved aggregate totals only).

    Only the approved aggregate money totals are ``public_aggregate``-eligible; raw
    tickets, messages, evidence, and requestor labels never leave the bundle.
    """
    unit = validate_unit(f"currency:{currency_code}", "reimbursement currency")
    raw = bundle_path.read_bytes()
    content_hash = sha256_hex(raw)
    report = reimbursement_report.load_bundle(bundle_path)
    summary = report.summary
    as_of = report.settings.as_of_date
    settled_paid_total = sum((ticket.approved for ticket in report.closed_tickets), Decimal("0.00"))
    recorded_payments_total = Decimal("0.00")
    for event in report.supplemental.events:
        if event.kind == "PAYMENT_RECORDED":
            if event.amount is None:
                raise ContractError(
                    "PAYMENT_RECORDED event without an amount escaped bundle validation"
                )
            recorded_payments_total += event.amount

    def money_fact(fact_id: str, label: str, value: Decimal, basis: str) -> FactRecord:
        return FactRecord(
            fact_id=fact_id,
            label=label,
            value=value,
            unit=unit,
            basis=basis,
            origin="observed",
            audience=AUDIENCE_PUBLIC,
            status="available",
            as_of_date=as_of,
            source_alias=SOURCE_ALIAS_REIMBURSEMENT,
            locator=bundle_path.name,
            source_hash=content_hash,
            captured_at=captured_at,
        )

    def count_fact(fact_id: str, label: str, value: int) -> FactRecord:
        return FactRecord(
            fact_id=fact_id,
            label=label,
            value=value,
            unit=UNIT_COUNT,
            basis="calculated",
            origin="observed",
            # Workflow counts are NOT "overall approved aggregate totals" (the closed
            # public policy in plan 5.3); they stay internal.
            audience=AUDIENCE_INTERNAL,
            status="available",
            as_of_date=as_of,
            source_alias=SOURCE_ALIAS_REIMBURSEMENT,
            locator=bundle_path.name,
            source_hash=content_hash,
            captured_at=captured_at,
        )

    facts = (
        money_fact(
            "reimbursements.outstanding_total",
            "Reimbursements approved and awaiting payment",
            summary.outstanding,
            "pending",
        ),
        money_fact(
            "reimbursements.approved_total",
            "Reimbursements approved to date",
            summary.approved,
            "committed",
        ),
        money_fact(
            "reimbursements.settled_paid_total",
            "Reimbursements settled and paid",
            settled_paid_total,
            "spent",
        ),
        money_fact(
            "reimbursements.recorded_payments_total",
            "Reimbursement payments recorded from evidence",
            recorded_payments_total,
            "spent",
        ),
        count_fact("reimbursements.active_count", "Open reimbursement requests", summary.active),
        count_fact(
            "reimbursements.settled_count", "Settled reimbursement requests", summary.settled
        ),
        count_fact(
            "reimbursements.unreviewed_count",
            "Reimbursement requests awaiting review",
            summary.live_unreviewed,
        ),
    )
    snapshot = SourceSnapshot(
        source_alias=SOURCE_ALIAS_REIMBURSEMENT,
        captured_at=captured_at,
        contract_version=reimbursement_report.SCHEMA_VERSION,
        locator=bundle_path.name,
        source_revision=None,
        content_sha256=content_hash,
        captured_ranges=("report", "tickets", "supplemental"),
    )
    return facts, snapshot


# --- derived facts -------------------------------------------------------------------


def build_derived_fact(
    *,
    fact_id: str,
    label: str,
    value: FactValue,
    unit: str,
    basis: str,
    calculation_id: str,
    inputs: Sequence[FactRecord],
    audience: str = AUDIENCE_PUBLIC,
    period: str | None = None,
    note: str | None = None,
) -> FactRecord:
    """A derived fact from approved inputs; audience can never exceed its inputs'."""
    if not inputs:
        raise ContractError(f"{fact_id}: derived facts need at least one input")
    for input_fact in inputs:
        if input_fact.status != "available":
            raise ContractError(f"{fact_id}: derived inputs must be available facts")
    first = inputs[0]
    if first.source_alias is None or first.source_hash is None or first.captured_at is None:
        raise ContractError(f"{fact_id}: derived inputs must carry provenance")
    return FactRecord(
        fact_id=fact_id,
        label=label,
        value=value,
        unit=unit,
        basis=basis,
        origin="derived",
        audience=combine_audience(audience, *(item.audience for item in inputs)),
        status="available",
        period=period,
        as_of_date=first.as_of_date,
        source_alias=first.source_alias,
        locator=first.locator,
        source_revision=first.source_revision,
        source_hash=first.source_hash,
        captured_at=first.captured_at,
        note=note,
        input_fact_ids=tuple(item.fact_id for item in inputs),
        calculation_id=calculation_id,
    )


# --- explicit source precedence ------------------------------------------------------


@dataclass(frozen=True)
class FactRequirement:
    """One fact a selected module needs (required blocks; optional is logged)."""

    fact_id: str
    label: str
    unit: str
    basis: str
    module_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_fact_id(self.fact_id)
        validate_unit(self.unit, f"{self.fact_id}.unit")


@dataclass(frozen=True)
class FactResolution:
    """Precedence outcome: resolved facts plus conflict/missing bookkeeping."""

    facts: tuple[FactRecord, ...]
    conflicts: tuple[ConflictRecord, ...]
    missing_required: tuple[MissingFact, ...]
    missing_optional: tuple[MissingFact, ...]


def _value_key(fact: FactRecord) -> str:
    encoded: object = fact.value.isoformat() if isinstance(fact.value, date) else fact.value
    return canonical_json({"unit": fact.unit, "value": encoded})


def resolve_facts(
    *,
    required: Sequence[FactRequirement] = (),
    optional: Sequence[FactRequirement] = (),
    briefing: Sequence[FactRecord] = (),
    canonical: Sequence[FactRecord] = (),
    derived: Sequence[FactRecord] = (),
    override: OverrideDocument | None = None,
) -> FactResolution:
    """Apply the explicit precedence order (section 5.3) — never a silent merge.

    1. validated per-run private override (stated reason + expected superseded hash);
    2. operator-owned Treasurer Briefing Inputs record;
    3. canonical structured dataset or validated reimbursement summary;
    4. derived fact from approved inputs.

    Two disagreeing observed sources with no explicit override make the fact
    ``conflicting`` (blocking when required). A stale override — its expected
    superseded source hash no longer matching the captured source — fails closed.
    Required unavailable facts block; optional ones are omitted and logged.
    """
    for record in briefing:
        if record.source_alias != SOURCE_ALIAS_BRIEFING:
            raise ContractError(f"{record.fact_id}: briefing candidates carry the briefing alias")
    for record in derived:
        if record.origin != "derived":
            raise ContractError(f"{record.fact_id}: derived candidates carry origin=derived")

    observed: dict[str, list[FactRecord]] = {}
    for record in (*briefing, *canonical):
        if record.status != "available":
            raise ContractError(f"{record.fact_id}: source candidates must be available")
        observed.setdefault(record.fact_id, []).append(record)
    derived_by_id: dict[str, FactRecord] = {}
    for record in derived:
        if record.fact_id in derived_by_id:
            raise ContractError(f"duplicate derived fact: {record.fact_id}")
        derived_by_id[record.fact_id] = record
    overrides = {} if override is None else override.fact_by_id()

    required_by_id = {item.fact_id: item for item in required}
    optional_by_id = {item.fact_id: item for item in optional}
    if set(required_by_id) & set(optional_by_id):
        raise ContractError("a fact cannot be both required and optional")

    ordered_ids = [item.fact_id for item in required]
    ordered_ids.extend(item.fact_id for item in optional)
    extra_ids = (set(observed) | set(derived_by_id) | set(overrides)) - set(ordered_ids)
    ordered_ids.extend(sorted(extra_ids))

    facts: list[FactRecord] = []
    conflicts: list[ConflictRecord] = []
    missing_required: list[MissingFact] = []
    missing_optional: list[MissingFact] = []

    def module_keys_for(fact_id: str) -> tuple[str, ...]:
        requirement = required_by_id.get(fact_id) or optional_by_id.get(fact_id)
        return () if requirement is None else requirement.module_keys

    for fact_id in ordered_ids:
        candidates = observed.get(fact_id, [])
        override_entry = overrides.get(fact_id)
        best = candidates[0] if candidates else None
        if override_entry is not None:
            if best is None:
                if override_entry.replaces_source_hash is not None:
                    raise ContractError(
                        f"override for {fact_id} names a superseded source hash, but no "
                        "captured source supplies this fact"
                    )
                resolved = override_entry.fact
            else:
                if override_entry.replaces_source_hash is None:
                    raise ContractError(
                        f"override for {fact_id} must state the expected superseded source hash"
                    )
                if override_entry.replaces_source_hash != best.source_hash:
                    raise ContractError(
                        f"stale override for {fact_id}: the expected superseded source "
                        "hash does not match the captured source"
                    )
                resolved = replace(
                    override_entry.fact,
                    audience=combine_audience(best.audience, override_entry.fact.audience),
                )
            facts.append(resolved)
            continue
        if candidates:
            distinct = {_value_key(candidate) for candidate in candidates}
            if len(distinct) > 1:
                blocking = fact_id in required_by_id
                conflicts.append(
                    ConflictRecord(
                        fact_id=fact_id,
                        module_keys=module_keys_for(fact_id),
                        candidates=tuple(
                            ConflictCandidate(
                                source_alias=candidate.source_alias or SOURCE_ALIAS_OVERRIDE,
                                value=candidate.value,
                                unit=candidate.unit,
                                source_hash=candidate.source_hash,
                            )
                            for candidate in candidates
                        ),
                        blocking=blocking,
                    )
                )
                template = candidates[0]
                facts.append(
                    FactRecord(
                        fact_id=fact_id,
                        label=template.label,
                        value=None,
                        unit=template.unit,
                        basis=template.basis,
                        origin="observed",
                        audience=combine_audience(
                            *(candidate.audience for candidate in candidates)
                        ),
                        status="conflicting",
                        period=template.period,
                    )
                )
                continue
            assert best is not None
            facts.append(best)
            continue
        derived_candidate = derived_by_id.get(fact_id)
        if derived_candidate is not None:
            facts.append(derived_candidate)
            continue
        requirement = required_by_id.get(fact_id)
        if requirement is not None:
            missing_required.append(
                MissingFact(
                    fact_id=fact_id,
                    module_keys=requirement.module_keys,
                    absence_reason="no source supplies this fact",
                    blocking=True,
                )
            )
            facts.append(
                FactRecord(
                    fact_id=fact_id,
                    label=requirement.label,
                    value=None,
                    unit=requirement.unit,
                    basis=requirement.basis,
                    origin="observed",
                    audience=AUDIENCE_INTERNAL,
                    status="missing",
                )
            )
            continue
        optional_requirement = optional_by_id.get(fact_id)
        if optional_requirement is not None:
            missing_optional.append(
                MissingFact(
                    fact_id=fact_id,
                    module_keys=optional_requirement.module_keys,
                    absence_reason="no source supplies this optional fact; omitted",
                    blocking=False,
                )
            )

    # Derived facts rest on available inputs. Invalidation cascades to a fixpoint so a
    # derived fact resting on another invalidated derived fact can never survive with a
    # dangling input reference; each round strictly shrinks the surviving set.
    invalidated: set[str] = set()
    while True:
        available_ids = {
            fact.fact_id
            for fact in facts
            if fact.status == "available" and fact.fact_id not in invalidated
        }
        newly_invalid = [
            fact
            for fact in facts
            if fact.fact_id not in invalidated
            and fact.origin == "derived"
            and not set(fact.input_fact_ids) <= available_ids
        ]
        if not newly_invalid:
            break
        for fact in newly_invalid:
            invalidated.add(fact.fact_id)
            entry = MissingFact(
                fact_id=fact.fact_id,
                module_keys=module_keys_for(fact.fact_id),
                absence_reason="derived inputs are unavailable",
                blocking=fact.fact_id in required_by_id,
            )
            if fact.fact_id in required_by_id:
                missing_required.append(entry)
            else:
                missing_optional.append(entry)
    verified: list[FactRecord] = []
    for fact in facts:
        if fact.fact_id in invalidated:
            if fact.fact_id in required_by_id:
                verified.append(
                    FactRecord(
                        fact_id=fact.fact_id,
                        label=fact.label,
                        value=None,
                        unit=fact.unit,
                        basis=fact.basis,
                        origin="observed",
                        audience=AUDIENCE_INTERNAL,
                        status="missing",
                    )
                )
            continue
        verified.append(fact)

    return FactResolution(
        facts=tuple(verified),
        conflicts=tuple(conflicts),
        missing_required=tuple(missing_required),
        missing_optional=tuple(missing_optional),
    )


def capture_fact_snapshot(
    *,
    run_id: str,
    captured_at: datetime,
    as_of_date: date,
    audience: str,
    source_snapshots: Sequence[SourceSnapshot],
    resolution: FactResolution,
    graphic_datasets: Sequence[GraphicDataset] = (),
) -> FactSnapshot:
    """Assemble the one immutable fact snapshot for a run."""
    validate_run_id(run_id)
    return FactSnapshot(
        run_id=run_id,
        captured_at=captured_at,
        as_of_date=as_of_date,
        audience=audience,
        source_snapshots=tuple(source_snapshots),
        facts=resolution.facts,
        graphic_datasets=tuple(graphic_datasets),
        conflicts=resolution.conflicts,
        missing_required=resolution.missing_required,
        missing_optional=resolution.missing_optional,
    )
