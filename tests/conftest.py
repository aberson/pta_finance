"""Shared test fixtures — fake-org config + a fake gspread layer (no live Google calls).

Identity here is obviously-fake placeholders only (``Example PTA`` etc.). NOTHING in
these fixtures touches the network: a ``FakeWorksheet`` / ``FakeSpreadsheet`` / ``FakeClient``
stand in for the gspread objects, and :class:`~pta_finance.sheets.SheetsClient` is always
constructed with ``gspread_client=`` so :func:`gspread.service_account` is never called.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import Any

import pytest
from gspread.exceptions import APIError
from requests.models import Response

from pta_finance.config import Config, load_config

_FULL_CONFIG = """\
[organization]
name = "Example PTA"
school_name = "Example Elementary"
school_email = "office@example.org"

[contacts]
president = ["president@example.org"]
treasurer = "treasurer@example.org"
cfo = "cfo@example.org"
account_holders = ["president@example.org", "treasurer@example.org"]

[fiscal_year]
start_month = 1

[grades]
labels = ["K", "1", "2", "3", "4", "5"]

[sheets]
spreadsheet_id = "fake-spreadsheet-id"
test_spreadsheet_id = "fake-test-sheet-id"
drive_receipts_folder_id = "fake-receipts-folder-id"
drive_reports_folder_id = "fake-reports-folder-id"

[google]
service_account_file = "secrets/service-account.json"
"""


@pytest.fixture
def fake_config(tmp_path: Path) -> Config:
    """A loaded :class:`Config` with fake placeholder identity."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_FULL_CONFIG, encoding="utf-8")
    return load_config(cfg_path)


def _a1_start_row(range_name: str) -> int:
    """Parse the 1-based start row from an A1 range like ``A1:O1`` -> ``1``."""
    start = range_name.split(":", 1)[0]
    digits = "".join(ch for ch in start if ch.isdigit())
    return int(digits) if digits else 1


def make_api_error(
    status: int,
    *,
    message: str = "rate limited",
    api_status: str = "RESOURCE_EXHAUSTED",
) -> APIError:
    """Build a real :class:`gspread.exceptions.APIError` with the given HTTP status.

    Constructs a minimal ``requests.Response`` carrying ``status_code`` and a JSON error
    body so ``APIError`` parses it the same way a live 429 would — exercising the
    production ``_status_of`` path, not a stubbed status. ``message``/``api_status`` let a
    test reproduce a specific rejection (e.g. a 400 duplicate-title error) verbatim.
    """
    response = Response()
    response.status_code = status
    body = {"error": {"code": status, "message": message, "status": api_status}}
    response._content = json.dumps(body).encode("utf-8")
    return APIError(response)


class FakeWorksheet:
    """A stand-in for ``gspread.Worksheet`` recording the calls a test cares about.

    ``grid`` is the current sheet contents as a list of rows (list[str]); row 0 is the
    header. ``batch_update`` / ``append_rows`` / ``delete_rows`` mutate ``grid`` and log
    their args. Any method can be made to raise on its first N calls via ``fail_first``
    to simulate a transient 429.
    """

    def __init__(
        self,
        grid: list[list[str]] | None = None,
        *,
        title: str = "",
        fail_first: MutableMapping[str, list[BaseException]] | None = None,
    ) -> None:
        self.grid: list[list[str]] = grid if grid is not None else []
        self.title = title
        self.batch_update_calls: list[list[dict[str, Any]]] = []
        self.append_rows_calls: list[list[list[str]]] = []
        self.delete_rows_calls: list[int] = []
        self.update_calls: list[tuple[str, list[list[str]]]] = []
        # method-name -> list of exceptions to raise (one per leading call), then succeed.
        self._fail_first: MutableMapping[str, list[BaseException]] = dict(fail_first or {})

    def _maybe_fail(self, method: str) -> None:
        queue = self._fail_first.get(method)
        if queue:
            raise queue.pop(0)

    # --- reads --------------------------------------------------------------

    def row_values(self, row: int) -> list[str]:
        self._maybe_fail("row_values")
        if 1 <= row <= len(self.grid):
            return list(self.grid[row - 1])
        return []

    def col_values(self, col: int) -> list[str]:
        self._maybe_fail("col_values")
        return [r[col - 1] if col - 1 < len(r) else "" for r in self.grid]

    def get_all_records(self) -> list[dict[str, Any]]:
        self._maybe_fail("get_all_records")
        if not self.grid:
            return []
        header = self.grid[0]
        return [dict(zip(header, row, strict=False)) for row in self.grid[1:]]

    # --- writes -------------------------------------------------------------

    def batch_update(self, data: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        self._maybe_fail("batch_update")
        requests = [dict(req) for req in data]
        self.batch_update_calls.append(requests)
        return {"replies": [{} for _ in requests]}

    def append_rows(self, values: list[list[str]]) -> dict[str, Any]:
        self._maybe_fail("append_rows")
        self.append_rows_calls.append([list(v) for v in values])
        self.grid.extend(list(v) for v in values)
        return {}

    def update(
        self,
        values: list[list[str]] | None = None,
        range_name: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Stand-in for gspread v6 ``Worksheet.update`` (``values`` positional, ``range_name`` kw).

        Mirrors the real v6 signature so a future refactor to positional/old-order args is
        CATCHABLE rather than silently accepted: production's contract (see sheets.py) is to
        always pass ``range_name=`` and ``values=`` by name, and this fake rejects a call
        that omits ``range_name`` (the v5-positional shape) so the wrong call would fail
        under test. Records the call and writes the values into ``grid`` starting at the
        range's first row (1-based, parsed from the ``A<row>`` start of ``range_name``).
        """
        self._maybe_fail("update")
        if values is None or range_name is None:
            raise TypeError("update() requires values= and range_name= (v6 named-arg contract)")
        self.update_calls.append((range_name, [list(v) for v in values]))
        start_row = _a1_start_row(range_name)
        for offset, row in enumerate(values):
            idx = start_row - 1 + offset
            while len(self.grid) <= idx:
                self.grid.append([])
            self.grid[idx] = list(row)
        return {}

    def delete_rows(self, start: int, end: int | None = None) -> dict[str, Any]:
        self._maybe_fail("delete_rows")
        self.delete_rows_calls.append(start)
        idx = start - 1
        if 0 <= idx < len(self.grid):
            del self.grid[idx]
        return {}


class FakeSpreadsheet:
    """A stand-in for ``gspread.Spreadsheet`` mapping tab name -> FakeWorksheet.

    Tracks insertion order so ``worksheets()`` returns titles in a stable order, and
    records ``add_worksheet`` calls so ``ensure_tab`` tests can assert a tab was created.
    """

    def __init__(
        self,
        worksheets: Mapping[str, FakeWorksheet],
        *,
        add_worksheet_error: BaseException | None = None,
    ) -> None:
        self._worksheets: dict[str, FakeWorksheet] = {}
        for title, ws in worksheets.items():
            if not ws.title:
                ws.title = title
            self._worksheets[title] = ws
        self.add_worksheet_calls: list[tuple[str, int, int]] = []
        # When set, add_worksheet inserts the worksheet (the create applied server-side)
        # and THEN raises — simulating a retried-create that already took effect, so the
        # caller's recovery path sees the tab as now-present.
        self._add_worksheet_error = add_worksheet_error

    def worksheet(self, title: str) -> FakeWorksheet:
        return self._worksheets[title]

    def worksheets(self) -> list[FakeWorksheet]:
        return list(self._worksheets.values())

    def add_worksheet(self, *, title: str, rows: int, cols: int) -> FakeWorksheet:
        self.add_worksheet_calls.append((title, rows, cols))
        ws = FakeWorksheet([], title=title)
        self._worksheets[title] = ws
        if self._add_worksheet_error is not None:
            raise self._add_worksheet_error
        return ws


class FakeClient:
    """A stand-in for ``gspread.Client`` returning a fixed :class:`FakeSpreadsheet`."""

    def __init__(self, spreadsheet: FakeSpreadsheet) -> None:
        self._spreadsheet = spreadsheet
        self.opened_keys: list[str] = []

    def open_by_key(self, key: str) -> FakeSpreadsheet:
        self.opened_keys.append(key)
        return self._spreadsheet


class RecordingSleep:
    """A no-op ``sleep`` that records the delays it was asked to wait."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
