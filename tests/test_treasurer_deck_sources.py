"""Behavior tests for read-only source capture, precedence, and the v2 adapter.

Every fixture is fictional (Example Organization / Fictional Person style); no real
identity, finance value, Google ID, or credential appears here. The fake Sheets
transport exposes only ``get_json_text`` — the source test double has no write method.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pta_finance import reimbursement_report, report_source
from pta_finance.treasurer_deck import models, sources

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
HEX64_A = "a" * 64
HEX64_B = "b" * 64

#: Verbs that would indicate a write-capable surface on the reader or its double.
_WRITE_VERBS = (
    "write",
    "update",
    "append",
    "clear",
    "delete",
    "insert",
    "replace",
    "upsert",
    "ensure",
    "batch",
    "create",
    "set_",
    "post",
    "put",
    "patch",
)


class _FakeTransport:
    """GET-only fake returning canned JSON text; records every call it serves."""

    def __init__(self, responses: dict[str, str]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get_json_text(self, path: str, params: Mapping[str, str]) -> str:
        self.calls.append((path, dict(params)))
        for marker, payload in self._responses.items():
            if marker in path:
                return payload
        raise AssertionError(f"unexpected Sheets path: {path}")


def _values_payload(grid: list[list[object]]) -> str:
    return json.dumps({"range": "'Example'!A1:Z9", "majorDimension": "ROWS", "values": grid})


def _grid_payload() -> str:
    return json.dumps(
        {
            "sheets": [
                {
                    "properties": {"title": "Budget Timeseries"},
                    "data": [
                        {
                            "startRow": 0,
                            "startColumn": 0,
                            "rowData": [
                                {
                                    "values": [
                                        {
                                            "userEnteredValue": {"stringValue": "amount"},
                                            "effectiveValue": {"stringValue": "amount"},
                                        },
                                        {
                                            "userEnteredValue": {"stringValue": "flag"},
                                            "effectiveValue": {"stringValue": "flag"},
                                        },
                                    ]
                                },
                                {
                                    "values": [
                                        {
                                            "userEnteredValue": {"formulaValue": "=SUM(B2:B3)"},
                                            "effectiveValue": {"numberValue": 1234.56},
                                            "effectiveFormat": {
                                                "numberFormat": {"type": "CURRENCY"}
                                            },
                                        },
                                        {
                                            "userEnteredValue": {"boolValue": True},
                                            "effectiveValue": {"boolValue": True},
                                        },
                                    ]
                                },
                            ],
                        }
                    ],
                }
            ]
        }
    )


def _reader(responses: dict[str, str]) -> tuple[sources.ReadOnlySheetsReader, _FakeTransport]:
    transport = _FakeTransport(responses)
    reader = sources.ReadOnlySheetsReader("fake-spreadsheet-id", transport)
    return reader, transport


# --- the read-only reader ------------------------------------------------------------


def test_reader_and_test_double_expose_no_write_method() -> None:
    """Neither the reader nor the fake transport has any write-shaped surface."""
    for target in (sources.ReadOnlySheetsReader, _FakeTransport):
        public = [name for name in dir(target) if not name.startswith("_")]
        for name in public:
            lowered = name.lower()
            assert not any(verb in lowered for verb in _WRITE_VERBS), name
    assert [name for name in dir(_FakeTransport) if not name.startswith("_")] == ["get_json_text"]


def test_read_values_returns_formatted_strings() -> None:
    reader, transport = _reader({"/values/": _values_payload([["a", "b"], ["1", ""]])})
    assert reader.read_values("Budget Timeseries") == [["a", "b"], ["1", ""]]
    path, params = transport.calls[0]
    assert path.startswith("spreadsheets/fake-spreadsheet-id/values/")
    assert params == {"valueRenderOption": "FORMATTED_VALUE", "majorDimension": "ROWS"}


def test_existing_timeseries_parser_accepts_the_read_only_adapter() -> None:
    """`read_timeseries` (typed against ValuesReader) consumes the dedicated reader."""
    header = list(report_source.TIMESERIES_COLUMNS)
    row = ["2027", "Example Group", "income", "proposed", "100", "", "", "Example Dues", ""]
    reader, _ = _reader({"/values/": _values_payload([header, row])})
    rows = report_source.read_timeseries(reader)
    assert len(rows) == 1
    assert rows[0][report_source.RAW_CATEGORY] == "Example Dues"


def test_read_grid_captures_typed_effective_entered_and_formula_values() -> None:
    """Money is Decimal (never float text), formulas are kept, formats are tagged."""
    reader, transport = _reader({"spreadsheets/fake-spreadsheet-id": _grid_payload()})
    grid = reader.read_grid("Budget Timeseries", "A1:B2")
    assert grid.worksheet_title == "Budget Timeseries"
    assert grid.returned_range == "A1:B2"
    assert grid.source_alias == models.SOURCE_ALIAS_BUDGET_TIMESERIES
    _, params = transport.calls[0]
    assert params["includeGridData"] == "true"
    assert params["ranges"] == "'Budget Timeseries'!A1:B2"

    money = next(cell for cell in grid.cells if cell.a1 == "A2")
    assert isinstance(money.effective_value, Decimal)
    assert money.effective_value == Decimal("1234.56")
    assert money.formula == "=SUM(B2:B3)"
    assert money.user_entered_value is None
    assert money.number_format == "CURRENCY"

    flag = next(cell for cell in grid.cells if cell.a1 == "B2")
    assert flag.effective_value is True
    assert flag.user_entered_value is True


def _grid_hash_of(payload: str) -> str:
    reader, _ = _reader({"spreadsheets/fake-spreadsheet-id": payload})
    return reader.read_grid("Budget Timeseries", "A1:B2").content_sha256()


def test_grid_hash_distinguishes_number_from_numeric_text() -> None:
    """Formatted currency text is never the canonical money value."""
    number_payload = _grid_payload()
    text_payload = number_payload.replace(
        '"effectiveValue": {"numberValue": 1234.56}',
        '"effectiveValue": {"stringValue": "1234.56"}',
    )
    assert _grid_hash_of(number_payload) != _grid_hash_of(text_payload)


def test_grid_hash_covers_entered_values_and_formulas() -> None:
    """A formula or entered-value edit changes the hash even when the evaluated
    value is unchanged — user-entered/formula provenance is part of the capture."""
    baseline = _grid_hash_of(_grid_payload())
    formula_variant = _grid_payload().replace(
        '"userEnteredValue": {"formulaValue": "=SUM(B2:B3)"}',
        '"userEnteredValue": {"formulaValue": "=SUM(B2:B4)"}',
    )
    assert formula_variant != _grid_payload()  # the replace really applied
    assert _grid_hash_of(formula_variant) != baseline
    entered_variant = _grid_payload().replace(
        '"userEnteredValue": {"boolValue": true}',
        '"userEnteredValue": {"stringValue": "TRUE"}',
    )
    assert entered_variant != _grid_payload()
    assert _grid_hash_of(entered_variant) != baseline


def test_read_grid_fails_closed_on_error_cells_and_wrong_worksheet() -> None:
    error_payload = _grid_payload().replace(
        '"effectiveValue": {"numberValue": 1234.56}',
        '"effectiveValue": {"errorValue": {"type": "DIV0"}}',
    )
    reader, _ = _reader({"spreadsheets/fake-spreadsheet-id": error_payload})
    with pytest.raises(models.ContractError):
        reader.read_grid("Budget Timeseries", "A1:B2")
    reader_two, _ = _reader({"spreadsheets/fake-spreadsheet-id": _grid_payload()})
    with pytest.raises(models.ContractError):
        reader_two.read_grid("Some Other Tab", "A1:B2")


def test_grid_source_snapshot_carries_private_provenance() -> None:
    reader, _ = _reader({"spreadsheets/fake-spreadsheet-id": _grid_payload()})
    grid = reader.read_grid("Budget Timeseries", "A1:B2")
    snapshot = sources.grid_source_snapshot(
        grid, locator="spreadsheet:fake-spreadsheet-id", captured_at=NOW
    )
    assert snapshot.source_alias == models.SOURCE_ALIAS_BUDGET_TIMESERIES
    assert snapshot.content_sha256 == grid.content_sha256()
    assert snapshot.captured_ranges == ("'Budget Timeseries'!A1:B2",)


def test_timeseries_field_audience_defaults_to_internal() -> None:
    """Only the enumerated fields may feed public aggregates; unknown -> internal."""
    assert sources.timeseries_field_audience("strategic_goal") == models.AUDIENCE_PUBLIC
    assert sources.timeseries_field_audience("raw_category") == models.AUDIENCE_INTERNAL
    assert sources.timeseries_field_audience("requestor") == models.AUDIENCE_INTERNAL
    assert sources.timeseries_field_audience("brand_new_field") == models.AUDIENCE_INTERNAL


# --- Treasurer Briefing Inputs -------------------------------------------------------


def _briefing_grid(*rows: list[str]) -> list[list[str]]:
    return [list(sources.BRIEFING_COLUMNS), *rows]


def test_parse_briefing_inputs_builds_typed_operator_facts() -> None:
    facts = sources.parse_briefing_inputs(
        _briefing_grid(
            [
                "position.bank_balance",
                "",
                "Example bank balance",
                "$12,345.67",
                "currency:USD",
                "cash",
                "2026-08-31",
                "",
                "Balance per example statement",
                "",
            ],
            [
                "history.year_end_balance",
                "fy-2025-26",
                "Example year-end balance",
                "1000.00",
                "currency:USD",
                "reserve",
                "2026-06-30",
                "public_aggregate",
                "",
                "Example source note",
            ],
        ),
        captured_at=NOW,
        source_hash=HEX64_A,
        locator="'Treasurer Briefing Inputs'",
    )
    assert [fact.fact_id for fact in facts] == [
        "position.bank_balance",
        "history.year_end_balance@fy-2025-26",
    ]
    first, second = facts
    assert first.value == Decimal("12345.67")
    assert first.audience == models.AUDIENCE_INTERNAL  # blank audience -> internal
    assert first.origin == "operator_supplied"
    assert second.audience == models.AUDIENCE_PUBLIC
    assert second.period == "fy-2025-26"


def test_briefing_audience_is_restrict_only() -> None:
    """An unrecognized audience value becomes internal — never a promotion."""
    facts = sources.parse_briefing_inputs(
        _briefing_grid(
            [
                "position.bank_balance",
                "",
                "Example bank balance",
                "10.00",
                "currency:USD",
                "cash",
                "2026-08-31",
                "Public!",
                "",
                "",
            ]
        ),
        captured_at=NOW,
        source_hash=HEX64_A,
        locator="'Treasurer Briefing Inputs'",
    )
    assert facts[0].audience == models.AUDIENCE_INTERNAL


def test_parse_briefing_inputs_fails_closed() -> None:
    """Wrong header, duplicate rows, and malformed/non-finite money all reject."""
    with pytest.raises(models.ContractError):
        sources.parse_briefing_inputs(
            [["wrong", "header"]], captured_at=NOW, source_hash=HEX64_A, locator="x"
        )
    duplicate = [
        "position.bank_balance",
        "",
        "Example bank balance",
        "10.00",
        "currency:USD",
        "cash",
        "2026-08-31",
        "",
        "",
        "",
    ]
    with pytest.raises(models.ContractError):
        sources.parse_briefing_inputs(
            _briefing_grid(duplicate, list(duplicate)),
            captured_at=NOW,
            source_hash=HEX64_A,
            locator="x",
        )
    for bad_value in ("abc", "NaN", "Infinity"):
        row = list(duplicate)
        row[3] = bad_value
        with pytest.raises(models.ContractError):
            sources.parse_briefing_inputs(
                _briefing_grid(row), captured_at=NOW, source_hash=HEX64_A, locator="x"
            )
    dateless = list(duplicate)
    dateless[6] = ""  # neither period nor as_of_date
    with pytest.raises(models.ContractError):
        sources.parse_briefing_inputs(
            _briefing_grid(dateless), captured_at=NOW, source_hash=HEX64_A, locator="x"
        )


def test_briefing_boolean_cells_are_a_closed_set() -> None:
    """Only TRUE/FALSE parse; a malformed cell never becomes an available False."""

    def _row(value: str) -> list[str]:
        return [
            "position.reserve_released",
            "",
            "Example reserve released",
            value,
            "boolean",
            "definition",
            "2026-08-31",
            "",
            "",
            "",
        ]

    good = sources.parse_briefing_inputs(
        _briefing_grid(
            _row("TRUE"),
        ),
        captured_at=NOW,
        source_hash=HEX64_A,
        locator="x",
    )
    assert good[0].value is True
    for bad in ("maybe", "", "N/A", "yes"):
        with pytest.raises(models.ContractError):
            sources.parse_briefing_inputs(
                _briefing_grid(_row(bad)),
                captured_at=NOW,
                source_hash=HEX64_A,
                locator="x",
            )


# --- override envelope ---------------------------------------------------------------


def _override_fact_entry(**overrides: object) -> dict[str, object]:
    fact = models.FactRecord(
        fact_id="position.bank_balance",
        label="Corrected example balance",
        value=Decimal("999.00"),
        unit="currency:USD",
        basis="cash",
        origin="operator_supplied",
        audience=models.AUDIENCE_PUBLIC,
        status="available",
        as_of_date=date(2026, 8, 31),
        source_alias=models.SOURCE_ALIAS_OVERRIDE,
        source_hash=HEX64_B,
        captured_at=NOW,
    )
    entry: dict[str, object] = dict(fact.to_json())
    entry["reason"] = "example correction with stated reason"
    entry["replaces_source_hash"] = HEX64_A
    entry.update(overrides)
    return entry


def _override_document(**entry_overrides: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "facts": [_override_fact_entry(**entry_overrides)],
        "table_graphics": [],
    }


def test_parse_override_document_strict_roots_and_entries() -> None:
    document = _override_document()
    parsed = sources.parse_override_document(document)
    assert parsed.facts[0].reason == "example correction with stated reason"
    with pytest.raises(models.ContractError):
        sources.parse_override_document({**document, "extra": 1})
    with pytest.raises(models.ContractError):
        sources.parse_override_document(_override_document(surprise="field"))
    with pytest.raises(models.ContractError):
        sources.parse_override_document(
            _override_document(source_alias=models.SOURCE_ALIAS_BRIEFING)
        )


def test_table_graphic_override_hash_rules() -> None:
    """Null replaces hash is allowed only for the first structured input."""
    good = {
        "schema_version": 1,
        "facts": [],
        "table_graphics": [
            {
                "graphic_key": sources.TABLE_GRAPHIC_KEY,
                "input": {"schema_version": 1},
                "reason": sources.OVERRIDE_INITIAL_REASON,
                "replaces_source_hash": None,
            }
        ],
    }
    parsed = sources.parse_override_document(good)
    assert parsed.table_graphics[0].replaces_source_hash is None
    bad_reason = json.loads(json.dumps(good))
    bad_reason["table_graphics"][0]["reason"] = "just because"
    with pytest.raises(models.ContractError):
        sources.parse_override_document(bad_reason)
    bad_key = json.loads(json.dumps(good))
    bad_key["table_graphics"][0]["graphic_key"] = "expense_donut"
    with pytest.raises(models.ContractError):
        sources.parse_override_document(bad_key)


# --- precedence ----------------------------------------------------------------------


def _candidate(
    fact_id: str = "position.bank_balance",
    *,
    alias: str,
    value: Decimal | None = None,
    audience: str = models.AUDIENCE_INTERNAL,
    source_hash: str = HEX64_A,
    label: str = "Example balance",
) -> models.FactRecord:
    return models.FactRecord(
        fact_id=fact_id,
        label=label,
        value=Decimal("100.00") if value is None else value,
        unit="currency:USD",
        basis="cash",
        origin="operator_supplied" if alias == models.SOURCE_ALIAS_BRIEFING else "observed",
        audience=audience,
        status="available",
        as_of_date=date(2026, 8, 31),
        source_alias=alias,
        locator="example",
        source_hash=source_hash,
        captured_at=NOW,
    )


def _requirement(fact_id: str = "position.bank_balance") -> sources.FactRequirement:
    return sources.FactRequirement(
        fact_id=fact_id,
        label="Example balance",
        unit="currency:USD",
        basis="cash",
        module_keys=("position",),
    )


def test_briefing_beats_canonical_when_sources_agree() -> None:
    resolution = sources.resolve_facts(
        required=[_requirement()],
        briefing=[_candidate(alias=models.SOURCE_ALIAS_BRIEFING)],
        canonical=[_candidate(alias=models.SOURCE_ALIAS_BUDGET_TIMESERIES)],
    )
    assert resolution.conflicts == ()
    assert resolution.facts[0].source_alias == models.SOURCE_ALIAS_BRIEFING


def test_disagreeing_sources_conflict_and_block_required_modules() -> None:
    """Two disagreeing sources with no override -> conflicting, and the run blocks."""
    resolution = sources.resolve_facts(
        required=[_requirement()],
        briefing=[_candidate(alias=models.SOURCE_ALIAS_BRIEFING, value=Decimal("100.00"))],
        canonical=[
            _candidate(
                alias=models.SOURCE_ALIAS_BUDGET_TIMESERIES,
                value=Decimal("200.00"),
                source_hash=HEX64_B,
            )
        ],
    )
    assert resolution.facts[0].status == "conflicting"
    conflict = resolution.conflicts[0]
    assert conflict.blocking is True
    assert {item.source_alias for item in conflict.candidates} == {
        models.SOURCE_ALIAS_BRIEFING,
        models.SOURCE_ALIAS_BUDGET_TIMESERIES,
    }
    snapshot = sources.capture_fact_snapshot(
        run_id=models.new_run_id(NOW),
        captured_at=NOW,
        as_of_date=date(2026, 8, 31),
        audience=models.AUDIENCE_INTERNAL,
        source_snapshots=(),
        resolution=resolution,
    )
    with pytest.raises(models.RunStateError):
        snapshot.require_advanceable()


def test_override_resolves_a_conflict_without_promoting_audience() -> None:
    """A matching override wins; a public override of internal data stays internal."""
    override = sources.parse_override_document(_override_document())
    resolution = sources.resolve_facts(
        required=[_requirement()],
        briefing=[
            _candidate(
                alias=models.SOURCE_ALIAS_BRIEFING,
                value=Decimal("100.00"),
                audience=models.AUDIENCE_INTERNAL,
                source_hash=HEX64_A,
            )
        ],
        canonical=[
            _candidate(
                alias=models.SOURCE_ALIAS_BUDGET_TIMESERIES,
                value=Decimal("200.00"),
                source_hash=HEX64_B,
            )
        ],
        override=override,
    )
    fact = resolution.facts[0]
    assert fact.value == Decimal("999.00")
    assert fact.source_alias == models.SOURCE_ALIAS_OVERRIDE
    assert fact.audience == models.AUDIENCE_INTERNAL  # no promotion past the source
    assert resolution.conflicts == ()


def test_stale_override_fails_closed() -> None:
    """An override naming a superseded hash that no longer matches is stale."""
    override = sources.parse_override_document(_override_document())
    with pytest.raises(models.ContractError, match="stale"):
        sources.resolve_facts(
            required=[_requirement()],
            briefing=[_candidate(alias=models.SOURCE_ALIAS_BRIEFING, source_hash=HEX64_B)],
            override=override,
        )


def test_override_without_any_candidate_requires_null_hash() -> None:
    override = sources.parse_override_document(_override_document())
    with pytest.raises(models.ContractError):
        sources.resolve_facts(required=[_requirement()], override=override)
    fresh = sources.parse_override_document(_override_document(replaces_source_hash=None))
    resolution = sources.resolve_facts(required=[_requirement()], override=fresh)
    assert resolution.facts[0].value == Decimal("999.00")


def test_required_gap_blocks_and_optional_gap_is_omitted_and_logged() -> None:
    resolution = sources.resolve_facts(
        required=[_requirement("position.bank_balance")],
        optional=[_requirement("position.reserve_balance")],
    )
    assert resolution.missing_required[0].fact_id == "position.bank_balance"
    assert resolution.missing_required[0].blocking is True
    assert resolution.facts[0].status == "missing"
    assert resolution.missing_optional[0].fact_id == "position.reserve_balance"
    assert resolution.missing_optional[0].blocking is False
    assert all(fact.fact_id != "position.reserve_balance" for fact in resolution.facts)


def test_derived_fact_fallback_and_input_validation() -> None:
    """Derived facts fill gaps from approved inputs and inherit their audience."""
    input_fact = _candidate(alias=models.SOURCE_ALIAS_BRIEFING)
    derived = sources.build_derived_fact(
        fact_id="position.projected_balance",
        label="Example projected balance",
        value=Decimal("150.00"),
        unit="currency:USD",
        basis="projected",
        calculation_id="position.projected_balance@v1",
        inputs=[input_fact],
        audience=models.AUDIENCE_PUBLIC,
    )
    assert derived.audience == models.AUDIENCE_INTERNAL  # inputs were internal
    resolution = sources.resolve_facts(
        required=[_requirement(), _requirement("position.projected_balance")],
        briefing=[input_fact],
        derived=[derived],
    )
    by_id = {fact.fact_id: fact for fact in resolution.facts}
    assert by_id["position.projected_balance"].origin == "derived"

    orphan = sources.build_derived_fact(
        fact_id="position.projected_balance",
        label="Example projected balance",
        value=Decimal("150.00"),
        unit="currency:USD",
        basis="projected",
        calculation_id="position.projected_balance@v1",
        inputs=[_candidate("position.reserve_balance", alias=models.SOURCE_ALIAS_BRIEFING)],
    )
    orphan_resolution = sources.resolve_facts(
        required=[_requirement("position.projected_balance")], derived=[orphan]
    )
    assert orphan_resolution.missing_required[0].absence_reason == (
        "derived inputs are unavailable"
    )
    assert orphan_resolution.facts[0].status == "missing"


def test_derived_invalidation_cascades_through_chains() -> None:
    """A derived fact resting on an invalidated derived fact cannot survive."""
    base = _candidate("position.base_value", alias=models.SOURCE_ALIAS_BRIEFING)
    first = sources.build_derived_fact(
        fact_id="position.chain_one",
        label="Example chained value",
        value=Decimal("1.00"),
        unit="currency:USD",
        basis="calculated",
        calculation_id="position.chain_one@v1",
        inputs=[base],
    )
    second = sources.build_derived_fact(
        fact_id="position.chain_two",
        label="Example chained value two",
        value=Decimal("2.00"),
        unit="currency:USD",
        basis="calculated",
        calculation_id="position.chain_two@v1",
        inputs=[first],
    )
    # `base` is NOT supplied to the resolver: first is invalid, so second must be too.
    resolution = sources.resolve_facts(
        required=[_requirement("position.chain_two")], derived=[first, second]
    )
    assert not any(fact.status == "available" for fact in resolution.facts)
    assert [item.fact_id for item in resolution.missing_required] == ["position.chain_two"]
    assert resolution.missing_required[0].blocking is True
    assert [item.fact_id for item in resolution.missing_optional] == ["position.chain_one"]
    placeholder = {fact.fact_id: fact for fact in resolution.facts}["position.chain_two"]
    assert placeholder.status == "missing"


# --- schema-v2 reimbursement summary adapter -----------------------------------------
# Fictional bundle fixtures modeled on tests/test_reimbursement_report.py.


def _item(key: str, index: int, amount: str, status: str) -> dict[str, object]:
    return {
        "item_key": key,
        "source_index": index,
        "source_date": "2026-08-01",
        "source_description": "Fictional classroom materials",
        "source_amount": amount,
        "canonical_category": "Classroom Supplies",
        "display_date": "",
        "display_item": "",
        "reviewed_amount": "",
        "status": status,
        "why": "Fictional evidence supports this test disposition.",
    }


def _ticket(
    *,
    review_key: str,
    ref: str,
    order: int,
    origin: str,
    status: str,
    decision: str,
    items: list[dict[str, object]],
    workflow: str = "ACTIVE",
    payment_status: str = "NOT_PAID",
) -> dict[str, object]:
    mapped_total = sum(Decimal(str(item["source_amount"])) for item in items)
    questions = ["Please confirm the fictional approval detail."] if status == "C" else []
    return {
        "review_key": review_key,
        "ref": ref,
        "form_label": "",
        "origin": origin,
        "display_order": order,
        "requestor_name": f"Fictional Person {order}",
        "form_type": "Example Reimbursement Form",
        "submitted": "2026-08-02",
        "submitted_label": "",
        "payment_method": "Zelle",
        "source_evidence_sha256": f"{order:x}" * 64,
        "source": {
            "stated_total": f"{mapped_total:.2f}",
            "mapped_total": f"{mapped_total:.2f}",
            "categories": ["Classroom Supplies"],
            "flags": [],
        },
        "live": {
            "workflow_state": workflow,
            "decision": decision,
            "payment_status": payment_status,
            "payment_date": "2026-08-03" if payment_status != "NOT_PAID" else "",
            "confirmations": (["FICTIONAL-CONFIRMATION"] if payment_status != "NOT_PAID" else []),
        },
        "review": {
            "status": status,
            "action": "Complete the fictional next step",
            "block": "Use the structured test evidence.",
            "asks": questions,
            "note": "Fictional reviewer note.",
            "email_questions": questions,
            "email_context": "",
        },
        "items": items,
        "messages": [{"kind": "draft", "date": "", "mode": "generated", "body": ""}],
        "archive_note": "Fictional archive note." if workflow == "SETTLED" else "",
    }


def _bundle() -> dict[str, object]:
    tickets = [
        _ticket(
            review_key="submission:v1:" + "a" * 64,
            ref="NEW-01",
            order=1,
            origin="submission",
            status="A",
            decision="UNREVIEWED",
            items=[_item("submission-a:line:1", 1, "10.00", "A")],
        ),
        _ticket(
            review_key="submission:v1:" + "b" * 64,
            ref="NEW-02",
            order=2,
            origin="submission",
            status="C",
            decision="UNREVIEWED",
            items=[
                _item("submission-b:line:1", 1, "5.00", "A"),
                _item("submission-b:line:2", 2, "7.00", "C"),
            ],
        ),
        _ticket(
            review_key="submission:v1:" + "c" * 64,
            ref="NEW-03",
            order=3,
            origin="submission",
            status="Q",
            decision="UNREVIEWED",
            items=[_item("submission-c:line:1", 1, "3.00", "Q")],
        ),
        _ticket(
            review_key="legacy:v1:p-001",
            ref="P-001",
            order=4,
            origin="legacy",
            status="A",
            decision="APPROVED",
            workflow="SETTLED",
            payment_status="PAID_PRIOR",
            items=[_item("legacy-p001:line:1", 1, "20.00", "A")],
        ),
    ]
    return {
        "schema_version": 1,
        "report": {
            "title": "Example Reimbursement Review",
            "eyebrow": "Example Organization · Treasurer",
            "subtitle": "Structured fictional review queue.",
            "organization": "Example Organization",
            "email_signoff": ["Thank you!", "Example Treasurer Team"],
            "logo_data_uri": "",
            "confirmed_outstanding": "15.00",
            "cutoff_date": "2026-06-01",
            "policy_version": "example-v1",
            "as_of_date": "2026-08-04",
        },
        "provenance": {
            "mapped_sha256": "1" * 64,
            "policy_sha256": "2" * 64,
            "source_snapshot_sha256": "3" * 64,
            "accounted_review_keys": sorted(
                [
                    "submission:v1:" + "a" * 64,
                    "submission:v1:" + "b" * 64,
                    "submission:v1:" + "c" * 64,
                ]
            ),
        },
        "source_summary": {
            "mapped_rows": 4,
            "mapped_submissions": 3,
            "mapped_total": "45.00",
            "first_received": "2026-08-02",
            "last_received": "2026-08-03",
        },
        "tickets": tickets,
        "appendix": {"amendments": [], "cfo_checks": [], "excluded": [], "defects": []},
    }


def _seal(record: dict[str, object]) -> None:
    record.pop("record_sha256", None)
    record["record_sha256"] = hashlib.sha256(
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _write_bundle(path: Path, bundle: dict[str, object]) -> None:
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")


def _facts_by_id(
    facts: tuple[models.FactRecord, ...],
) -> dict[str, models.FactRecord]:
    return {fact.fact_id: fact for fact in facts}


def test_reimbursement_adapter_summarizes_settlement_and_payment_state(
    tmp_path: Path,
) -> None:
    """Outstanding/approved/settled aggregates come from the validated report."""
    path = tmp_path / "reimbursement-report.json"
    _write_bundle(path, _bundle())
    facts, snapshot = sources.load_reimbursement_summary(path, captured_at=NOW)
    by_id = _facts_by_id(facts)
    assert by_id["reimbursements.outstanding_total"].value == Decimal("15.00")
    assert by_id["reimbursements.outstanding_total"].basis == "pending"
    assert by_id["reimbursements.approved_total"].value == Decimal("35.00")
    assert by_id["reimbursements.approved_total"].basis == "committed"
    assert by_id["reimbursements.settled_paid_total"].value == Decimal("20.00")
    assert by_id["reimbursements.settled_paid_total"].basis == "spent"
    assert by_id["reimbursements.recorded_payments_total"].value == Decimal("0")
    assert by_id["reimbursements.active_count"].value == 3
    assert by_id["reimbursements.settled_count"].value == 1
    assert by_id["reimbursements.unreviewed_count"].value == 3
    assert by_id["reimbursements.active_count"].as_of_date == date(2026, 8, 4)
    assert snapshot.source_alias == models.SOURCE_ALIAS_REIMBURSEMENT
    assert snapshot.contract_version == reimbursement_report.SCHEMA_VERSION
    assert snapshot.content_sha256 == models.sha256_hex(path.read_bytes())


def test_reimbursement_adapter_counts_recorded_payment_events(tmp_path: Path) -> None:
    """A PAYMENT_RECORDED supplemental event feeds the evidence-backed paid total."""
    bundle = reimbursement_report.migrate_bundle(_bundle())
    tickets = bundle["tickets"]
    assert isinstance(tickets, list)
    tickets[3]["live"]["payment_status"] = "PAID"
    message_id = "<payment@example.invalid>"
    evidence_key = "mail:v1:" + hashlib.sha256(message_id.encode()).hexdigest()
    ticket_key = "legacy:v1:p-001"
    event_key = (
        "event:v1:"
        + hashlib.sha256(f"{evidence_key}\0{ticket_key}\0PAYMENT_RECORDED".encode()).hexdigest()
    )
    evidence = {
        "evidence_key": evidence_key,
        "source_type": "MAIL",
        "message_id": message_id,
        "in_reply_to": ["<outbound@example.invalid>"],
        "references": ["<outbound@example.invalid>"],
        "occurred_on": "2026-08-05",
        "occurred_at": "2026-08-05T12:00:00+00:00",
        "top_authored_sha256": "7" * 64,
        "evidence_sha256": "4" * 64,
        "attachments": [],
    }
    _seal(evidence)
    event = {
        "event_key": event_key,
        "evidence_key": evidence_key,
        "ticket_review_key": ticket_key,
        "kind": "PAYMENT_RECORDED",
        "occurred_on": "2026-08-05",
        "occurred_at": "2026-08-05T12:00:00+00:00",
        "evidence_sha256": "4" * 64,
        "summary": "One fictional payment was recorded.",
        "amount": "20.00",
        "reference": "FICTIONAL-CHECK-1042",
        "discrepancy": "",
    }
    _seal(event)
    bundle["supplemental"] = {
        "anchors_sha256": "6" * 64,
        "evidence": [evidence],
        "events": [event],
        "unmatched": [],
    }
    path = tmp_path / "reimbursement-report.json"
    _write_bundle(path, bundle)
    facts, _ = sources.load_reimbursement_summary(path, captured_at=NOW)
    by_id = _facts_by_id(facts)
    assert by_id["reimbursements.recorded_payments_total"].value == Decimal("20.00")
    assert by_id["reimbursements.settled_paid_total"].value == Decimal("20.00")


def test_reimbursement_adapter_never_counts_unmatched_evidence(tmp_path: Path) -> None:
    """Unmatched supplemental evidence changes no obligation total."""
    base_path = tmp_path / "before.json"
    _write_bundle(base_path, _bundle())
    base_facts, _ = sources.load_reimbursement_summary(base_path, captured_at=NOW)

    bundle = reimbursement_report.migrate_bundle(_bundle())
    message_id = "<unmatched@example.invalid>"
    evidence_key = "mail:v1:" + hashlib.sha256(message_id.encode()).hexdigest()
    evidence = {
        "evidence_key": evidence_key,
        "source_type": "MAIL",
        "message_id": message_id,
        "in_reply_to": [],
        "references": [],
        "occurred_on": "2026-08-05",
        "occurred_at": "2026-08-05T12:00:00+00:00",
        "top_authored_sha256": "9" * 64,
        "evidence_sha256": "5" * 64,
        "attachments": [],
    }
    _seal(evidence)
    bundle["supplemental"] = {
        "anchors_sha256": "6" * 64,
        "evidence": [evidence],
        "events": [],
        "unmatched": [{"evidence_key": evidence_key, "reason": "NO_EXACT_LINK"}],
    }
    path = tmp_path / "after.json"
    _write_bundle(path, bundle)
    facts, _ = sources.load_reimbursement_summary(path, captured_at=NOW)

    base_by_id = _facts_by_id(base_facts)
    by_id = _facts_by_id(facts)
    assert set(by_id) == set(base_by_id)
    for fact_id, fact in by_id.items():
        assert fact.value == base_by_id[fact_id].value


def test_reimbursement_facts_expose_no_person_level_detail(tmp_path: Path) -> None:
    """Aggregates only: no requestor label or ticket text leaves the bundle."""
    path = tmp_path / "reimbursement-report.json"
    _write_bundle(path, _bundle())
    facts, _ = sources.load_reimbursement_summary(path, captured_at=NOW)
    for fact in facts:
        assert "Fictional Person" not in fact.label
        assert not isinstance(fact.value, str) or "Fictional Person" not in fact.value
        assert fact.fact_id.startswith("reimbursements.")
        # Closed public policy: approved aggregate money totals only; workflow counts
        # stay internal.
        if fact.fact_id.endswith("_total"):
            assert fact.audience == models.AUDIENCE_PUBLIC
        else:
            assert fact.audience == models.AUDIENCE_INTERNAL


def test_reimbursement_adapter_fails_closed_on_invalid_bundles(tmp_path: Path) -> None:
    """The strict loader is reused: a corrupted bundle never yields facts."""
    path = tmp_path / "reimbursement-report.json"
    bundle = _bundle()
    bundle["surprise"] = True
    _write_bundle(path, bundle)
    with pytest.raises(ValueError):  # ReimbursementReportError subclasses ValueError
        sources.load_reimbursement_summary(path, captured_at=NOW)


def test_snapshot_capture_with_reimbursement_source_detects_staleness(
    tmp_path: Path,
) -> None:
    """A refreshed bundle hash marks the captured snapshot's run stale."""
    path = tmp_path / "reimbursement-report.json"
    _write_bundle(path, _bundle())
    facts, source_snapshot = sources.load_reimbursement_summary(path, captured_at=NOW)
    resolution = sources.resolve_facts(canonical=list(facts))
    snapshot = sources.capture_fact_snapshot(
        run_id=models.new_run_id(NOW),
        captured_at=NOW,
        as_of_date=date(2026, 8, 31),
        audience=models.AUDIENCE_INTERNAL,
        source_snapshots=(source_snapshot,),
        resolution=resolution,
    )
    snapshot.require_advanceable()
    snapshot.verify_source_hashes(
        {models.SOURCE_ALIAS_REIMBURSEMENT: source_snapshot.content_sha256}
    )
    with pytest.raises(models.RunStateError):
        snapshot.verify_source_hashes({models.SOURCE_ALIAS_REIMBURSEMENT: "e" * 64})
