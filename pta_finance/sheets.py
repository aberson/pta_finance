"""The only module that talks to Google — a ``gspread`` service-account wrapper.

:class:`SheetsClient` opens the configured spreadsheet and exposes typed,
row/range-targeted reads and writes. Every Google API call routes through
:meth:`SheetsClient._with_retry`, which retries HTTP 429/500/503 with exponential
backoff + jitter (project quota: 300 req/min, per-user: 60 req/min — a ``batch_update``
counts as a single request).

Design for testability + corruption-safety:

* Authorization is **lazy** — construction is cheap and never touches the network.
  Tests inject a fake ``gspread`` client via ``gspread_client=`` (or patch
  :func:`gspread.service_account`); production calls :meth:`connect` (implicitly).
* Writes are **row/range-targeted**, never a full-tab overwrite: :meth:`upsert_rows`
  locates each row by its ``id`` in the id column and writes ONLY that row's A1 range;
  unknown ids are appended after the last data row. All edits + appends are issued as a
  single **atomic** ``worksheet.batch_update`` (all-or-nothing).
* gspread v6 reordered ``update()`` positional args, so every ``update``-shaped call uses
  named args (``range_name=``, ``values=``).
* The sleep function and retry count are **injectable** (``sleep=``, ``max_retries=``) so
  tests simulate a 429-then-success without real waiting.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, TypeVar

import gspread
from gspread.exceptions import APIError
from gspread.utils import ValueInputOption, rowcol_to_a1

from pta_finance import schema

if TYPE_CHECKING:
    from pta_finance.config import Config

__all__ = [
    "SchemaError",
    "SheetsClient",
]

T = TypeVar("T")

# HTTP statuses that are transient and worth retrying with backoff.
_RETRYABLE_STATUSES = frozenset({429, 500, 503})

# Column index (1-based) of the ``id`` key column. Every tab whose rows carry an
# ``id`` lists it first (see schema.py), so this is constant across upsert targets.
_ID_COLUMN_INDEX = 1


class SchemaError(Exception):
    """Raised when a worksheet's header row does not match the canonical schema.

    Carries the offending ``tab`` plus the ``expected`` and ``actual`` header tuples
    so the operator sees exactly which columns drifted.
    """

    def __init__(
        self,
        tab: str,
        expected: tuple[str, ...],
        actual: tuple[str, ...],
    ) -> None:
        self.tab = tab
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"schema mismatch on tab {tab!r}: expected {list(expected)}, got {list(actual)}"
        )


def _status_of(err: APIError) -> int | None:
    """Best-effort HTTP status code from a gspread :class:`APIError`.

    Prefers the underlying ``requests.Response.status_code``; falls back to the
    parsed JSON ``error.code`` (which gspread mirrors the HTTP status into).
    """
    response = getattr(err, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    code = getattr(err, "code", None)
    return code if isinstance(code, int) else None


def _is_duplicate_title_error(err: APIError) -> bool:
    """True when ``err`` is a 400 'sheet title already exists' rejection.

    ``add_worksheet`` is not idempotent: a retried create whose first attempt already
    applied server-side comes back as a non-retryable 400 naming a duplicate title.
    :meth:`SheetsClient.ensure_tab` treats this as benign (the tab now exists) rather than
    aborting the bootstrap loop. Status via :func:`_status_of`; the duplicate signal is a
    substring match on the error message (gspread surfaces the API's text verbatim).
    """
    if _status_of(err) != 400:
        return False
    message = str(err).lower()
    return "already exists" in message


class SheetsClient:
    """Service-account ``gspread`` wrapper around one spreadsheet.

    Parameters
    ----------
    config:
        The project :class:`~pta_finance.config.Config` (supplies the service-account
        key path and the ``spreadsheet_id``).
    spreadsheet_id:
        Override the spreadsheet to open. Defaults to ``config.sheets.spreadsheet_id``;
        the ``check`` round-trip passes ``config.sheets.test_spreadsheet_id`` here.
    gspread_client:
        An already-authorized gspread client. When provided, :meth:`connect` uses it
        instead of calling :func:`gspread.service_account` — this is the test seam.
    sleep:
        The sleep function used between retries (default :func:`time.sleep`). Tests pass
        a no-op recorder so the backoff path runs instantly.
    max_retries:
        Max attempts for a retryable (429/500/503) call before re-raising (default 5).
    base_delay, jitter:
        Backoff is ``base_delay * 2**attempt + random.uniform(0, jitter)`` seconds.
    """

    def __init__(
        self,
        config: Config,
        *,
        spreadsheet_id: str | None = None,
        gspread_client: gspread.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
        base_delay: float = 0.5,
        jitter: float = 0.25,
    ) -> None:
        self._config = config
        self._spreadsheet_id = spreadsheet_id or config.sheets.spreadsheet_id
        self._client: gspread.Client | None = gspread_client
        self._spreadsheet: gspread.Spreadsheet | None = None
        self._sleep = sleep
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._jitter = jitter

    # --- connection (lazy) -------------------------------------------------

    def connect(self) -> gspread.Spreadsheet:
        """Authorize (if needed) and open the spreadsheet, memoizing the handle.

        Cheap to call repeatedly: the authorize + open happen once. Tests that injected
        a ``gspread_client`` skip the network ``service_account`` call entirely.
        """
        if self._spreadsheet is not None:
            return self._spreadsheet
        if self._client is None:
            self._client = gspread.service_account(
                filename=str(self._config.google.service_account_path)
            )
        self._spreadsheet = self._with_retry(
            lambda: self._client.open_by_key(self._spreadsheet_id)  # type: ignore[union-attr]
        )
        return self._spreadsheet

    def worksheet(self, tab: str) -> gspread.Worksheet:
        """Return the worksheet handle for ``tab`` (opening the spreadsheet if needed)."""
        return self._with_retry(lambda: self.connect().worksheet(tab))

    # --- retry --------------------------------------------------------------

    def _with_retry(self, fn: Callable[[], T]) -> T:
        """Run ``fn``, retrying retryable :class:`APIError`\\ s with backoff + jitter.

        Retries only on HTTP 429/500/503; any other ``APIError`` (and any non-API
        exception) propagates immediately. After ``max_retries`` attempts the final
        error is re-raised. The wait is ``base_delay * 2**attempt + uniform(0, jitter)``;
        the injected ``sleep`` makes this instant under test.
        """
        attempt = 0
        while True:
            try:
                return fn()
            except APIError as err:
                status = _status_of(err)
                attempt += 1
                if status not in _RETRYABLE_STATUSES or attempt >= self._max_retries:
                    raise
                delay = self._base_delay * (2**attempt) + random.uniform(0, self._jitter)
                self._sleep(delay)

    # --- reads --------------------------------------------------------------

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        """Read a worksheet into a list of row dicts keyed by the header columns.

        Values are coerced to ``str`` so callers get the same shape regardless of how
        Sheets typed a cell. Uses ``get_all_records`` (header row → keys).
        """
        ws = self.worksheet(tab)
        records = self._with_retry(lambda: ws.get_all_records())
        return [{str(k): _as_str(v) for k, v in record.items()} for record in records]

    def read_values(self, tab: str) -> list[list[str]]:
        """Read a worksheet's RAW grid (every cell coerced to ``str``).

        Unlike :meth:`read_tab` this does NOT key by a header row — it returns the cells
        exactly as laid out, header row included. Used by ``import-budget`` to scan a messy
        human worksheet whose header is not row 1 (or not the canonical schema at all).
        Routed through :meth:`_with_retry`; uses ``get_all_values`` (whole-grid read).
        """
        ws = self.worksheet(tab)
        rows: list[list[Any]] = self._with_retry(lambda: ws.get_all_values())
        return [[_as_str(cell) for cell in row] for row in rows]

    def validate_schema(self, tab: str) -> None:
        """Assert the worksheet's header row equals ``schema.TABS[tab]``.

        Raises :class:`SchemaError` (naming ``tab`` + expected/actual headers) on any
        mismatch, including order, extra, or missing columns.
        """
        expected = schema.TABS[tab]
        ws = self.worksheet(tab)
        header_raw: list[Any] = self._with_retry(lambda: ws.row_values(1))
        actual = tuple(_as_str(cell) for cell in header_raw)
        if actual != expected:
            raise SchemaError(tab, expected, actual)

    def read_header(self, tab: str) -> list[str]:
        """Return row 1 (the header) of ``tab`` as a list of strings.

        Read-only — never validates or writes. Used by ``init-sheet --dry-run`` to report
        what :meth:`ensure_tab` would do without issuing any write. An empty/header-less
        sheet yields an empty list.
        """
        ws = self.worksheet(tab)
        header_raw: list[Any] = self._with_retry(lambda: ws.row_values(1))
        return [_as_str(cell) for cell in header_raw]

    def list_worksheet_titles(self) -> list[str]:
        """Return the titles of every worksheet in the spreadsheet.

        Opens the spreadsheet (if needed) and reads the ``worksheets()`` handle. Used by
        :meth:`ensure_tab` and the ``init-sheet`` command to tell which canonical tabs
        already exist before deciding whether to create one.
        """
        worksheets = self._with_retry(lambda: self.connect().worksheets())
        return [ws.title for ws in worksheets]

    # --- bootstrap (create-if-absent) ---------------------------------------

    def ensure_tab(self, tab: str) -> str:
        """Create ``tab`` if absent and ensure row 1 equals ``schema.TABS[tab]``.

        Idempotent and corruption-safe — never overwrites a non-empty, mismatched header
        (that would clobber a real but wrong-shaped tab). Returns a status string:

        * ``"created"`` — the worksheet did not exist; it was added (sized to the schema)
          and its header row was written.
        * ``"headers-written"`` — the worksheet existed but row 1 was empty; the header
          row was written.
        * ``"ok"`` — the worksheet existed and row 1 already equals the schema headers;
          no write was issued.

        Raises :class:`SchemaError` (naming ``tab`` + expected/actual headers) when the
        worksheet exists and row 1 is non-empty but does NOT match the schema.
        """
        columns = schema.TABS[tab]
        header = list(columns)
        header_range = self._a1_range(1, len(columns))

        if tab not in self.list_worksheet_titles():
            try:
                ws = self._with_retry(
                    lambda: self.connect().add_worksheet(title=tab, rows=100, cols=len(columns))
                )
            except APIError as err:
                # A retried create (after a 500/503/timeout that already applied
                # server-side) hits a non-retryable 400 duplicate-title error. Treat that as
                # "the tab now exists" and fall through to ensure its header — never abort
                # the init-sheet loop mid-way.
                if not _is_duplicate_title_error(err):
                    raise
            else:
                self._with_retry(lambda: ws.update(range_name=header_range, values=[header]))
                return "created"

        ws = self.worksheet(tab)
        header_raw: list[Any] = self._with_retry(lambda: ws.row_values(1))
        actual = tuple(_as_str(cell) for cell in header_raw)
        if actual == ():
            self._with_retry(lambda: ws.update(range_name=header_range, values=[header]))
            return "headers-written"
        if actual != columns:
            raise SchemaError(tab, columns, actual)
        return "ok"

    # --- writes (row/range-targeted, atomic) --------------------------------

    def append_rows(self, tab: str, rows: Sequence[Mapping[str, str]]) -> None:
        """Append ``rows`` to the bottom of ``tab`` in schema-column order.

        ``rows`` are dicts keyed by the tab's schema columns; missing keys serialize as
        empty cells. A no-op when ``rows`` is empty. Counts as a single API request.
        """
        if not rows:
            return
        columns = schema.TABS[tab]
        values = [[row.get(col, "") for col in columns] for row in rows]
        ws = self.worksheet(tab)
        self._with_retry(lambda: ws.append_rows(values))

    def upsert_rows(self, tab: str, rows_by_id: Mapping[str, Mapping[str, str]]) -> None:
        """Insert-or-update rows by ``id`` with a single atomic ``batch_update``.

        For each ``(id, row)`` in ``rows_by_id``: if ``id`` already exists in the id
        column, ONLY that row's A1 range is overwritten (never a full-tab write); if it
        is new, the row is appended after the current last data row. All edits and
        appends are collected into one ``worksheet.batch_update`` call so the write is
        all-or-nothing (a failed subrequest rolls the whole batch back).

        ``rows_by_id`` values are dicts keyed by the tab's schema columns; missing keys
        serialize as empty cells. A no-op when ``rows_by_id`` is empty.
        """
        if not rows_by_id:
            return
        columns = schema.TABS[tab]
        ncols = len(columns)
        ws = self.worksheet(tab)

        # One read to locate existing ids → their 1-based sheet row numbers.
        # Row 1 is the header; data starts at row 2. The header is never treated as a
        # data row, so an id equal to a column name (e.g. "id") cannot clobber it, and
        # an empty/header-only sheet never writes above row 2.
        id_cells: list[Any] = self._with_retry(lambda: ws.col_values(_ID_COLUMN_INDEX))
        row_of_id: dict[str, int] = {}
        for sheet_row, cell in enumerate(id_cells[1:], start=2):
            key = _as_str(cell)
            if key:
                row_of_id.setdefault(key, sheet_row)
        # New rows go after the last occupied row, but never above the header (row >= 2).
        next_row = max(len(id_cells) + 1, 2)

        requests: list[dict[str, Any]] = []
        for row_id, row in rows_by_id.items():
            values = [row.get(col, "") for col in columns]
            target_row = row_of_id.get(row_id)
            if target_row is None:
                target_row = next_row
                next_row += 1
            a1 = self._a1_range(target_row, ncols)
            requests.append({"range": a1, "values": [values]})

        # Single atomic batch — all ranges land together or not at all.
        self._with_retry(lambda: ws.batch_update(requests))

    def update_cells(self, tab: str, cell_values: Mapping[str, str]) -> None:
        """Atomically overwrite specific A1 cells in ``tab`` (schema-INDEPENDENT).

        ``cell_values`` maps an A1 cell (e.g. ``"E42"``) to its new value; all writes land in
        one ``worksheet.batch_update`` (all-or-nothing). Unlike :meth:`upsert_rows` (which
        assumes an ``id`` key column + ``schema.TABS`` column order), this targets arbitrary
        cells and never assumes a column layout — so it can reconcile edits into the operator-
        maintained "Budget Timeseries" tab, whose 14-column shape is NOT in ``schema.TABS``.

        Writes with ``value_input_option="USER_ENTERED"`` (NOT gspread's default ``RAW``): a
        numeric string like an amount ``"10000"`` must be stored as a **number**, or the
        operator tab's native ``SUM`` / ``QUERY`` / pivot formulas (e.g. the "Group Explorer"
        tab) silently skip the text cell and under-count. A no-op when empty.
        """
        if not cell_values:
            return
        ws = self.worksheet(tab)
        requests = [{"range": a1, "values": [[value]]} for a1, value in cell_values.items()]
        self._with_retry(
            lambda: ws.batch_update(requests, value_input_option=ValueInputOption.user_entered)
        )

    def append_raw_rows(self, tab: str, rows: Sequence[Sequence[str]]) -> None:
        """Append pre-ordered raw ``rows`` to ``tab`` (schema-INDEPENDENT).

        Unlike :meth:`append_rows` (which orders a dict of cells by ``schema.TABS[tab]``), the
        rows here are already cell-ordered to the tab's LIVE header — for appending to a tab,
        like "Budget Timeseries", that is not in the canonical schema registry.

        Writes with ``value_input_option="USER_ENTERED"`` (NOT gspread's default ``RAW``) so an
        appended amount string is stored as a **number** the operator tab's native SUM/QUERY/
        pivot formulas can total (see :meth:`update_cells`). A no-op when empty. One API request.
        """
        if not rows:
            return
        ws = self.worksheet(tab)
        values = [list(r) for r in rows]
        self._with_retry(
            lambda: ws.append_rows(values, value_input_option=ValueInputOption.user_entered)
        )

    def update_rows_by_index(
        self, tab: str, rows_by_index: Mapping[int, Mapping[str, str]]
    ) -> None:
        """Overwrite specific 1-based data rows (>= 2) in one atomic ``batch_update``.

        Companion to :meth:`upsert_rows` for rows that have no ``id`` to key an upsert on —
        e.g. a malformed row that ``etl.normalize`` flagged ``needs_review`` so the flag must
        still reach the sheet. Each given row's full A1 range is written; the header (row 1)
        is rejected. Row-targeted — never a full-tab write. A no-op when empty.
        """
        if not rows_by_index:
            return
        columns = schema.TABS[tab]
        ncols = len(columns)
        ws = self.worksheet(tab)
        requests: list[dict[str, Any]] = []
        for sheet_row, row in rows_by_index.items():
            if sheet_row < 2:
                raise ValueError(f"refusing to write header/invalid sheet row {sheet_row}")
            values = [row.get(col, "") for col in columns]
            requests.append({"range": self._a1_range(sheet_row, ncols), "values": [values]})
        self._with_retry(lambda: ws.batch_update(requests))

    def delete_rows_by_id(self, tab: str, ids: Sequence[str]) -> None:
        """Delete the rows whose ``id`` is in ``ids`` (row-targeted, never a full clear).

        Locates each id in the id column and deletes that sheet row. Deletes from the
        bottom up so earlier deletions don't shift the indices of later ones. Ids not
        present are silently skipped. A no-op when ``ids`` is empty. Used by the
        ``check`` round-trip to clean up its probe row.
        """
        if not ids:
            return
        ws = self.worksheet(tab)
        id_cells: list[Any] = self._with_retry(lambda: ws.col_values(_ID_COLUMN_INDEX))
        wanted = set(ids)
        # Skip the header row (row 1) so a column-name id can't delete the header.
        target_rows = sorted(
            (
                sheet_row
                for sheet_row, cell in enumerate(id_cells[1:], start=2)
                if _as_str(cell) in wanted
            ),
            reverse=True,
        )
        for sheet_row in target_rows:
            self._with_retry(partial(ws.delete_rows, sheet_row))

    @staticmethod
    def _a1_range(sheet_row: int, ncols: int) -> str:
        """A1 range covering one full schema row, e.g. ``A7:O7``."""
        start = rowcol_to_a1(sheet_row, 1)
        end = rowcol_to_a1(sheet_row, ncols)
        return f"{start}:{end}"


def _as_str(value: Any) -> str:
    """Coerce a Sheet cell value to ``str`` (``None`` -> empty string)."""
    if value is None:
        return ""
    return str(value)
