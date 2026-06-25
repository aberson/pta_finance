"""Tests for pta_finance.sheets — mocked gspread, NO live Google calls.

The headline test exercises the **production** ``SheetsClient.upsert_rows`` path (not a
parallel helper) and asserts (a) a single row-targeted ``batch_update`` and (b) the
429-then-success backoff retry through ``_with_retry`` (with an injected no-op sleep).
"""

from __future__ import annotations

import pytest
from gspread.exceptions import APIError

from pta_finance import schema
from pta_finance.config import Config
from pta_finance.sheets import SchemaError, SheetsClient
from tests.conftest import (
    FakeClient,
    FakeSpreadsheet,
    FakeWorksheet,
    RecordingSleep,
    make_api_error,
)

_TXN = schema.TAB_TRANSACTIONS
_COLS = schema.TRANSACTIONS_COLUMNS


def _client(config: Config, worksheet: FakeWorksheet, **kwargs: object) -> SheetsClient:
    """Build a SheetsClient wired to a single-tab fake spreadsheet (no network)."""
    spreadsheet = FakeSpreadsheet({_TXN: worksheet})
    fake_client = FakeClient(spreadsheet)
    return SheetsClient(config, gspread_client=fake_client, **kwargs)  # type: ignore[arg-type]


def _header_row() -> list[str]:
    return list(_COLS)


def _row_for(txn_id: str) -> dict[str, str]:
    row = {col: "" for col in _COLS}
    row["id"] = txn_id
    row["payee"] = "Example Vendor"
    return row


# --- Integration test through the production caller -------------------------


def test_upsert_rows_issues_single_row_targeted_batch_update(fake_config: Config) -> None:
    """upsert_rows updates an existing id in place and appends a new id — one batch_update,
    each request a single-row A1 range, NOT a full-sheet write."""
    # Grid: header + one existing row (TXN-FY26-0001 at sheet row 2).
    grid = [_header_row(), [_row_for("TXN-FY26-0001")[c] for c in _COLS]]
    ws = FakeWorksheet(grid)
    sleep = RecordingSleep()
    client = _client(fake_config, ws, sleep=sleep)

    client.upsert_rows(
        _TXN,
        {
            "TXN-FY26-0001": _row_for("TXN-FY26-0001"),  # existing -> update row 2
            "TXN-FY26-0002": _row_for("TXN-FY26-0002"),  # new -> append at row 3
        },
    )

    # Exactly ONE atomic batch_update was issued (all-or-nothing write).
    assert len(ws.batch_update_calls) == 1
    requests = ws.batch_update_calls[0]
    assert len(requests) == 2

    ranges = [req["range"] for req in requests]
    ncols = len(_COLS)  # 15 -> column O
    # Existing id updates ONLY its own row (row 2); new id targets the appended row 3.
    assert "A2:O2" in ranges  # literal asserts the row-targeted range, not a full-sheet write
    assert "A3:O3" in ranges
    # Each request writes exactly one row of exactly ncols values — never the whole tab.
    for req in requests:
        assert len(req["values"]) == 1
        assert len(req["values"][0]) == ncols
    # No append_rows was used (upsert batches everything atomically).
    assert ws.append_rows_calls == []
    # Happy path: no retries, so sleep was never called here.
    assert sleep.calls == []


def test_upsert_rows_retries_on_429_then_succeeds(fake_config: Config) -> None:
    """A 429 APIError on the first batch_update is retried via _with_retry; the second
    call succeeds. The injected sleep proves the backoff path ran (instantly)."""
    grid = [_header_row()]
    ws = FakeWorksheet(grid, fail_first={"batch_update": [make_api_error(429)]})
    sleep = RecordingSleep()
    client = _client(fake_config, ws, sleep=sleep, max_retries=5)

    client.upsert_rows(_TXN, {"TXN-FY26-0001": _row_for("TXN-FY26-0001")})

    # batch_update was attempted twice (429 then success) and ultimately committed once.
    assert len(ws.batch_update_calls) == 1
    # The backoff slept exactly once (one retryable failure).
    assert len(sleep.calls) == 1
    assert sleep.calls[0] > 0  # base_delay * 2**1 + jitter > 0
    # The committed batch carries the appended row's value.
    committed = ws.batch_update_calls[0]
    assert committed[0]["values"][0][0] == "TXN-FY26-0001"


def test_with_retry_reraises_after_max_attempts(fake_config: Config) -> None:
    """Persistent 429s exhaust max_retries and re-raise the APIError."""
    # 4 leading failures with max_retries=5 -> attempts 1..4 retry, attempt 5 re-raises.
    failures = [make_api_error(429) for _ in range(5)]
    ws = FakeWorksheet([_header_row()], fail_first={"batch_update": failures})
    sleep = RecordingSleep()
    client = _client(fake_config, ws, sleep=sleep, max_retries=5)

    with pytest.raises(APIError):
        client.upsert_rows(_TXN, {"TXN-FY26-0001": _row_for("TXN-FY26-0001")})
    # Slept on attempts 1..4 (4 retries), then re-raised on attempt 5.
    assert len(sleep.calls) == 4


def test_non_retryable_apierror_propagates_without_retry(fake_config: Config) -> None:
    """A 404 APIError is NOT retried — it propagates on the first failure, no sleep."""
    ws = FakeWorksheet([_header_row()], fail_first={"batch_update": [make_api_error(404)]})
    sleep = RecordingSleep()
    client = _client(fake_config, ws, sleep=sleep)

    with pytest.raises(APIError):
        client.upsert_rows(_TXN, {"TXN-FY26-0001": _row_for("TXN-FY26-0001")})
    assert sleep.calls == []


# --- validate_schema --------------------------------------------------------


def test_validate_schema_passes_on_matching_header(fake_config: Config) -> None:
    ws = FakeWorksheet([_header_row()])
    client = _client(fake_config, ws)
    client.validate_schema(_TXN)  # no raise


def test_validate_schema_raises_on_mismatched_header(fake_config: Config) -> None:
    bad_header = ["id", "WRONG", *list(_COLS[2:])]
    ws = FakeWorksheet([bad_header])
    client = _client(fake_config, ws)

    with pytest.raises(SchemaError) as exc_info:
        client.validate_schema(_TXN)
    err = exc_info.value
    assert err.tab == _TXN
    assert err.expected == _COLS
    assert err.actual == tuple(bad_header)


# --- read_tab ---------------------------------------------------------------


def test_read_tab_returns_dicts_keyed_by_schema_columns(fake_config: Config) -> None:
    row = _row_for("TXN-FY26-0001")
    grid = [_header_row(), [row[c] for c in _COLS]]
    ws = FakeWorksheet(grid)
    client = _client(fake_config, ws)

    records = client.read_tab(_TXN)

    assert len(records) == 1
    assert set(records[0]) == set(_COLS)
    assert records[0]["id"] == "TXN-FY26-0001"
    assert records[0]["payee"] == "Example Vendor"


def test_read_values_returns_raw_grid_coerced_to_str(fake_config: Config) -> None:
    """read_values returns the RAW grid (header row included) with every cell coerced to str —
    no header-keyed dicts, unlike read_tab. Used by import-budget on a non-canonical tab."""
    grid = [
        ["", "Type", "Line Item", "Proposed"],  # a non-row-1 header, leading junk column
        ["", "Income", "Membership", 1500],  # an int cell -> "1500"
    ]
    ws = FakeWorksheet(grid)
    client = _client(fake_config, ws)

    values = client.read_values(_TXN)

    assert values == [
        ["", "Type", "Line Item", "Proposed"],
        ["", "Income", "Membership", "1500"],
    ]


def test_append_rows_writes_in_schema_order(fake_config: Config) -> None:
    ws = FakeWorksheet([_header_row()])
    client = _client(fake_config, ws)

    client.append_rows(_TXN, [_row_for("TXN-FY26-0003")])

    assert len(ws.append_rows_calls) == 1
    appended = ws.append_rows_calls[0][0]
    assert appended[0] == "TXN-FY26-0003"  # id is column 0 in schema order
    assert len(appended) == len(_COLS)


def test_delete_rows_by_id_targets_only_matching_rows(fake_config: Config) -> None:
    grid = [
        _header_row(),
        [_row_for("TXN-FY26-0001")[c] for c in _COLS],
        [_row_for("TXN-FY26-0002")[c] for c in _COLS],
    ]
    ws = FakeWorksheet(grid)
    client = _client(fake_config, ws)

    client.delete_rows_by_id(_TXN, ["TXN-FY26-0002"])

    # Only the second data row (sheet row 3) was deleted.
    assert ws.delete_rows_calls == [3]
    remaining_ids = [r[0] for r in ws.grid[1:]]
    assert remaining_ids == ["TXN-FY26-0001"]


# --- Regression: the header row (row 1) is never a data-write target ---------


def test_upsert_never_targets_header_row(fake_config: Config) -> None:
    """Regression: the header (row 1) is never treated as a data row — an id equal to a
    column name cannot clobber it, and a header-only sheet appends at row 2 (not row 1)."""
    ws = FakeWorksheet([_header_row()])  # header only, no data rows
    client = _client(fake_config, ws)

    client.upsert_rows(
        _TXN,
        {
            "TXN-FY26-0009": _row_for("TXN-FY26-0009"),  # normal new id
            "id": _row_for("id"),  # pathological id == the header value
        },
    )

    ranges = {req["range"] for req in ws.batch_update_calls[0]}
    assert "A1:O1" not in ranges  # header row never written
    assert ranges == {"A2:O2", "A3:O3"}  # both new rows land at/after row 2


def test_delete_does_not_remove_header_row(fake_config: Config) -> None:
    """Regression: deleting an id equal to the header value must not delete the header."""
    grid = [_header_row(), [_row_for("TXN-FY26-0001")[c] for c in _COLS]]
    ws = FakeWorksheet(grid)
    client = _client(fake_config, ws)

    client.delete_rows_by_id(_TXN, ["id"])  # "id" is the header value in row 1

    assert ws.delete_rows_calls == []  # header (row 1) not deleted
    assert ws.grid[0] == _header_row()  # header intact


def test_update_rows_by_index_targets_only_given_rows(fake_config: Config) -> None:
    """update_rows_by_index writes ONLY the given 1-based rows, one A1 range each, one batch."""
    grid = [_header_row(), [_row_for("TXN-FY26-0001")[c] for c in _COLS]]
    ws = FakeWorksheet(grid)
    client = _client(fake_config, ws)

    flagged = _row_for("")  # a malformed, id-less row
    flagged["needs_review"] = "TRUE"
    client.update_rows_by_index(_TXN, {2: flagged})

    assert len(ws.batch_update_calls) == 1
    ranges = {req["range"] for req in ws.batch_update_calls[0]}
    assert ranges == {"A2:O2"}  # row-targeted, never the whole tab


def test_update_rows_by_index_rejects_header_row(fake_config: Config) -> None:
    """Writing sheet row 1 (the header) is refused — a guard against clobbering the header."""
    ws = FakeWorksheet([_header_row()])
    client = _client(fake_config, ws)
    with pytest.raises(ValueError):
        client.update_rows_by_index(_TXN, {1: _row_for("X")})


# --- ensure_tab / list_worksheet_titles (init-sheet bootstrap) --------------


def _multi_client(
    config: Config, worksheets: dict[str, FakeWorksheet]
) -> tuple[SheetsClient, FakeSpreadsheet]:
    """Build a SheetsClient over a multi-tab fake spreadsheet, returning both for asserts."""
    spreadsheet = FakeSpreadsheet(worksheets)
    fake_client = FakeClient(spreadsheet)
    client = SheetsClient(config, gspread_client=fake_client)  # type: ignore[arg-type]
    return client, spreadsheet


def test_list_worksheet_titles_returns_all_tab_names(fake_config: Config) -> None:
    client, _ = _multi_client(
        fake_config,
        {_TXN: FakeWorksheet([_header_row()]), schema.TAB_BUDGET: FakeWorksheet([])},
    )
    assert client.list_worksheet_titles() == [_TXN, schema.TAB_BUDGET]


def test_ensure_tab_creates_missing_worksheet_with_schema_headers(fake_config: Config) -> None:
    """A missing tab is added (sized to the schema) and row 1 gets the EXACT schema headers."""
    # Spreadsheet starts with an unrelated tab; the target tab is absent.
    client, spreadsheet = _multi_client(fake_config, {"unrelated": FakeWorksheet([])})

    status = client.ensure_tab(_TXN)

    assert status == "created"
    # add_worksheet was called once, sized to len(columns).
    assert spreadsheet.add_worksheet_calls == [(_TXN, 100, len(_COLS))]
    created = spreadsheet.worksheet(_TXN)
    # The header row written equals list(schema.TABS[tab]) exactly.
    assert len(created.update_calls) == 1
    _range, values = created.update_calls[0]
    assert values == [list(schema.TABS[_TXN])]
    assert created.grid[0] == list(_COLS)


def test_ensure_tab_idempotent_on_correct_tab_issues_no_write(fake_config: Config) -> None:
    """Re-running on an already-correct tab returns 'ok' and issues NO write."""
    ws = FakeWorksheet([_header_row()])
    client, spreadsheet = _multi_client(fake_config, {_TXN: ws})

    status = client.ensure_tab(_TXN)

    assert status == "ok"
    assert ws.update_calls == []  # no write
    assert ws.batch_update_calls == []
    assert spreadsheet.add_worksheet_calls == []


def test_ensure_tab_writes_headers_when_row1_empty(fake_config: Config) -> None:
    """An existing tab whose row 1 is empty gets its header written, returning 'headers-written'."""
    ws = FakeWorksheet([])  # exists but completely empty (no header)
    client, spreadsheet = _multi_client(fake_config, {_TXN: ws})

    status = client.ensure_tab(_TXN)

    assert status == "headers-written"
    assert spreadsheet.add_worksheet_calls == []  # not created — already existed
    assert len(ws.update_calls) == 1
    _range, values = ws.update_calls[0]
    assert values == [list(schema.TABS[_TXN])]
    assert ws.grid[0] == list(_COLS)


def test_ensure_tab_raises_on_nonempty_mismatch_without_clobber(fake_config: Config) -> None:
    """A non-empty, wrong-shaped header raises SchemaError and issues NO write (no clobber)."""
    bad_header = ["id", "WRONG", *list(_COLS[2:])]
    ws = FakeWorksheet([bad_header])
    client, _ = _multi_client(fake_config, {_TXN: ws})

    with pytest.raises(SchemaError) as exc_info:
        client.ensure_tab(_TXN)

    err = exc_info.value
    assert err.tab == _TXN
    assert err.expected == _COLS
    assert err.actual == tuple(bad_header)
    # No write of any kind — the real (but wrong-shaped) data is preserved.
    assert ws.update_calls == []
    assert ws.batch_update_calls == []
    assert ws.grid == [bad_header]


def test_ensure_tab_recovers_from_duplicate_title_after_retried_create(
    fake_config: Config,
) -> None:
    """A retried create that already applied server-side raises a 400 duplicate-title error;
    ensure_tab must NOT abort — it falls through, finds the tab present with an empty row 1,
    writes the schema header, and returns a sensible status (not propagate the APIError)."""
    dup_err = make_api_error(
        400,
        message='A sheet with the name "transactions" already exists.',
        api_status="INVALID_ARGUMENT",
    )
    # The target tab is absent initially; add_worksheet applies the create (server-side)
    # then raises the duplicate-title 400 — exactly the retry hazard.
    spreadsheet = FakeSpreadsheet({"unrelated": FakeWorksheet([])}, add_worksheet_error=dup_err)
    fake_client = FakeClient(spreadsheet)
    client = SheetsClient(fake_config, gspread_client=fake_client)  # type: ignore[arg-type]

    status = client.ensure_tab(_TXN)

    # Recovered: the create applied, row 1 was empty, so the header was written.
    assert status == "headers-written"
    created = spreadsheet.worksheet(_TXN)
    assert len(created.update_calls) == 1
    _range, values = created.update_calls[0]
    assert values == [list(schema.TABS[_TXN])]
    assert created.grid[0] == list(_COLS)


def test_ensure_tab_propagates_non_duplicate_400(fake_config: Config) -> None:
    """A 400 that is NOT a duplicate-title error still propagates (no silent swallow)."""
    other_err = make_api_error(
        400, message="Invalid requests[0].addSheet: bad grid", api_status="INVALID_ARGUMENT"
    )
    spreadsheet = FakeSpreadsheet({"unrelated": FakeWorksheet([])}, add_worksheet_error=other_err)
    fake_client = FakeClient(spreadsheet)
    client = SheetsClient(fake_config, gspread_client=fake_client)  # type: ignore[arg-type]

    with pytest.raises(APIError):
        client.ensure_tab(_TXN)
