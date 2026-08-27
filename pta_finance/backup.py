"""Safe CSV and exact user-entered-value snapshots — the corruption-protection safety net.

:func:`snapshot_all_tabs` reads a set of tabs through a
:class:`~pta_finance.sheets.SheetsClient` and writes a spreadsheet-safe ``<tab>.csv`` plus
an adjacent versioned ``<tab>.raw.json`` tagged ``userEnteredValue`` grid under
``snapshots/<utc-timestamp>/``. The toolkit runs this *before* any mutating operation.
The JSON distinguishes formulas from identical literal strings, native numbers/booleans, and
empty cells. Sheets version history is the primary recovery mechanism; JSON preserves entered
cell contents, while CSV is convenient for inspection/import. Neither artifact captures formatting
or comments.

By default it snapshots the **live** set — :data:`schema.REQUIRED_TABS` plus the operator-
maintained "Budget Timeseries" tab — and silently SKIPS any tab absent from the spreadsheet,
so the snapshot keeps working once the unused canonical tabs are deleted. Legacy callers
(``etl.normalize`` / ``import-budget``) pass ``tabs=schema.TABS`` to snapshot every canonical
tab before mutating them.

The snapshot is read-only with respect to Google: it issues only reads via the client.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any

from gspread.exceptions import WorksheetNotFound

from pta_finance import report_source, schema
from pta_finance.sheets import SheetsClient

__all__ = [
    "CsvText",
    "ValidatedCsvNumber",
    "decode_formula_safe_text",
    "encode_formula_safe_text",
    "force_csv_text",
    "is_formula_like_text",
    "snapshot_all_tabs",
    "snapshot_raw_tab",
    "validated_csv_number",
    "write_formula_safe_csv",
    "LIVE_SNAPSHOT_TABS",
]

_SPREADSHEET_FORMULA_MARKERS = ("=", "+", "-", "@")
_SAFE_PATH_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}\Z")
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

# The default snapshot set: the live-required tab(s) plus the operator-maintained
# "Budget Timeseries" source. A tab in this set that the spreadsheet doesn't have is skipped.
LIVE_SNAPSHOT_TABS: tuple[str, ...] = (
    *schema.REQUIRED_TABS,
    report_source.BUDGET_TIMESERIES_TAB,
)

_EXACT_SCHEMA_VERSION = 1
_EXACT_VALUE_MODEL = "google-sheets-userEnteredValue"
_USER_ENTERED_VALUE_KEYS = frozenset({"formulaValue", "stringValue", "numberValue", "boolValue"})

SnapshotCell = dict[str, Any]
SnapshotGrid = list[list[SnapshotCell]]


@dataclass(frozen=True)
class ValidatedCsvNumber:
    """A caller-validated numeric cell that may bypass CSV text protection."""

    value: str


@dataclass(frozen=True)
class CsvText:
    """Text that must stay text even when it resembles a spreadsheet number."""

    value: str


def _utc_stamp() -> str:
    """A filesystem-safe UTC timestamp, e.g. ``2026-06-23T144501Z``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _safe_path_component(raw: str) -> str:
    """Return a stable filename component that cannot traverse the snapshot directory."""
    reserved_stem = raw.split(".", 1)[0].rstrip(" .").upper()
    reserved = reserved_stem in _WINDOWS_RESERVED_NAMES
    if (
        _SAFE_PATH_COMPONENT.fullmatch(raw) is not None
        and not raw.endswith((".", " "))
        and not reserved
    ):
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    stem = re.sub(r"[^A-Za-z0-9._ -]", "_", raw).strip(" ._")[:64] or "snapshot"
    # Unsafe names occupy a namespace no accepted literal title can use, so a title that happens
    # to equal the readable stem + digest cannot overwrite the sanitized artifact.
    return f"~{stem}-{digest}"


def is_formula_like_text(raw: str) -> bool:
    """Return whether spreadsheet software could interpret ``raw`` as a formula."""
    return raw.lstrip().startswith(_SPREADSHEET_FORMULA_MARKERS)


def _has_formula_like_suffix(raw: str) -> bool:
    """Return whether leading literal apostrophes hide formula-like text."""
    return is_formula_like_text(raw.lstrip("'"))


def encode_formula_safe_text(raw: str) -> str:
    """Add one injective safety layer before a formula-like text suffix.

    Existing leading apostrophes are source data, so this always adds one more layer for values
    such as ``=x``, ``'=x``, and ``''=x``. The three encodings therefore remain distinct.
    """
    return f"'{raw}" if _has_formula_like_suffix(raw) else raw


def decode_formula_safe_text(raw: str) -> str:
    """Remove exactly one layer previously added by :func:`encode_formula_safe_text`."""
    if raw.startswith("'") and _has_formula_like_suffix(raw[1:]):
        return raw[1:]
    return raw


def _finite_decimal_text(raw: str) -> bool:
    """Return whether spreadsheet software could coerce text to a finite decimal."""
    try:
        return Decimal(raw).is_finite()
    except DecimalException:
        return False


def validated_csv_number(raw: str) -> ValidatedCsvNumber:
    """Mark a number already validated by its caller's semantic bounds."""
    return ValidatedCsvNumber(raw)


def force_csv_text(raw: str) -> CsvText:
    """Mark rejected numeric-looking text so spreadsheet import cannot coerce it."""
    return CsvText(raw)


def _safe_csv_cell(value: object) -> object:
    if isinstance(value, ValidatedCsvNumber):
        return value.value
    if isinstance(value, CsvText):
        encoded = encode_formula_safe_text(value.value)
        return encoded if encoded != value.value else f"'{value.value}"
    if isinstance(value, str):
        return encode_formula_safe_text(value)
    return value


def write_formula_safe_csv(
    path: Path,
    rows: Iterable[Sequence[object]],
    *,
    exclusive: bool = False,
) -> None:
    """Write rows at the shared spreadsheet-safe CSV serialization boundary.

    Formula-like strings receive one reversible apostrophe layer. Only native scalar values or
    :class:`ValidatedCsvNumber` cells bypass text protection; :class:`CsvText` forces a rejected
    numeric-looking value to remain text. ``exclusive`` prevents snapshot artifact truncation.
    """
    mode = "x" if exclusive else "w"
    with path.open(mode, encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow([_safe_csv_cell(value) for value in row])


def _entered_value(cell: SnapshotCell) -> tuple[str | None, object]:
    """Return one validated tagged user-entered value as ``(kind, value)``."""
    entered = cell.get("userEnteredValue")
    if entered is None:
        return None, ""
    if not isinstance(entered, dict):
        raise ValueError("snapshot userEnteredValue must be an object or null")
    present = [key for key in _USER_ENTERED_VALUE_KEYS if key in entered]
    if len(present) != 1:
        raise ValueError("snapshot userEnteredValue must contain exactly one supported value")
    key = present[0]
    value = entered[key]
    if key in {"formulaValue", "stringValue"} and not isinstance(value, str):
        raise ValueError(f"snapshot {key} must be text")
    if key == "numberValue" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise ValueError("snapshot numberValue must be numeric")
    if key == "boolValue" and not isinstance(value, bool):
        raise ValueError("snapshot boolValue must be boolean")
    return key, value


def _snapshot_plain_cell(cell: SnapshotCell) -> object:
    """Convert a tagged cell to a safe-CSV value without losing native numeric type."""
    kind, value = _entered_value(cell)
    if kind == "stringValue" and isinstance(value, str) and _finite_decimal_text(value):
        return force_csv_text(value)
    return value


def _snapshot_header(grid: Sequence[Sequence[SnapshotCell]]) -> list[str]:
    if not grid:
        return []
    return [str(_entered_value(cell)[1]) for cell in grid[0]]


def _columns_for(tab: str, grid: Sequence[Sequence[SnapshotCell]]) -> list[str]:
    """The CSV header columns for ``tab``.

    Prefers the canonical column list (``schema.TABS[tab]``) for a canonical tab; falls back
    to :data:`report_source.TIMESERIES_COLUMNS` for the "Budget Timeseries" tab; otherwise
    derives the columns from the raw grid's header (a tab outside both registries).
    """
    canonical = schema.TABS.get(tab)
    if canonical is not None:
        return list(canonical)
    if tab == report_source.BUDGET_TIMESERIES_TAB:
        return list(report_source.TIMESERIES_COLUMNS)
    return _snapshot_header(grid)


def _project_grid(
    grid: Sequence[Sequence[SnapshotCell]], columns: Sequence[str]
) -> list[list[object]]:
    """Project a tagged user-entered-value grid onto known safe-CSV columns."""
    header = _snapshot_header(grid)
    source_indexes: dict[str, int] = {}
    for index, name in enumerate(header):
        source_indexes.setdefault(name, index)
    rows: list[list[object]] = [list(columns)]
    for raw_row in grid[1:]:
        projected: list[object] = []
        for column in columns:
            source_index = source_indexes.get(column)
            projected.append(
                _snapshot_plain_cell(raw_row[source_index])
                if source_index is not None and source_index < len(raw_row)
                else ""
            )
        rows.append(projected)
    return rows


def _snapshot_csv_grid(grid: Sequence[Sequence[SnapshotCell]]) -> list[list[object]]:
    return [[_snapshot_plain_cell(cell) for cell in row] for row in grid]


def _write_exact_json(path: Path, grid: Sequence[Sequence[SnapshotCell]]) -> None:
    """Write the versioned, tagged user-entered-value artifact adjacent to a safe CSV."""
    artifact = {
        "schema_version": _EXACT_SCHEMA_VERSION,
        "value_model": _EXACT_VALUE_MODEL,
        "grid": grid,
    }
    with path.open("x", encoding="utf-8", newline="") as handle:
        json.dump(artifact, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def _filesystem_key(path_name: str) -> str:
    """Conservative key for filesystems that normalize Unicode and ignore case."""
    return unicodedata.normalize("NFC", path_name).casefold()


def _validate_unique_artifact_names(tabs: Sequence[str]) -> None:
    seen: dict[str, str] = {}
    for tab in tabs:
        stem = _safe_path_component(tab)
        for filename in (f"{stem}.csv", f"{stem}.raw.json"):
            key = _filesystem_key(filename)
            previous = seen.get(key)
            if previous is not None:
                raise ValueError(
                    "filesystem-equivalent snapshot artifact names for "
                    f"tabs {previous!r} and {tab!r}"
                )
            seen[key] = tab


def _claim_snapshot_dir(dest_dir: Path, stamp: str) -> Path:
    """Atomically claim an unused timestamp directory without truncating older artifacts."""
    snapshots_dir = Path(dest_dir) / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    suffix = 1
    while True:
        name = stamp if suffix == 1 else f"{stamp}-{suffix}"
        candidate = snapshots_dir / name
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            suffix += 1
            continue
        return candidate


def snapshot_all_tabs(
    client: SheetsClient,
    dest_dir: Path,
    *,
    timestamp: str | None = None,
    tabs: Iterable[str] | None = None,
) -> Path:
    """Export safe CSV and exact tagged user-entered-value JSON for requested tabs.

    Each CSV's header is the tab's column list (canonical via ``schema.TABS[tab]``, or
    :data:`report_source.TIMESERIES_COLUMNS` for the "Budget Timeseries" tab); each data row
    is the raw grid projected into column order (missing cells -> empty cells). Its adjacent
    ``.raw.json`` retains the complete tagged grid, distinguishing formulas from literal strings,
    native numbers/booleans, and empty cells. A tab
    that does not exist on the spreadsheet is **skipped** (a
    :class:`gspread.exceptions.WorksheetNotFound`
    from the read is caught and noted) rather than aborting the snapshot — so the backup keeps
    working once the unused canonical tabs are deleted. Returns the created snapshot directory.

    Parameters
    ----------
    client:
        The :class:`~pta_finance.sheets.SheetsClient` to read through. Tests inject a
        mock with ``read_snapshot_values`` that returns a canned grid (no live calls).
    dest_dir:
        The base directory; ``snapshots/<timestamp>/`` is atomically claimed beneath it. A
        collision uses ``<timestamp>-2`` (and so on) rather than overwriting an older snapshot.
    timestamp:
        Override the snapshot folder name (default: current UTC time). Useful for
        deterministic tests and for a caller that wants one stamp across artifacts.
    tabs:
        The tabs to snapshot (default: :data:`LIVE_SNAPSHOT_TABS` — the live-required tab(s)
        plus the "Budget Timeseries" source). Legacy callers (``etl.normalize`` /
        ``import-budget``) pass ``schema.TABS`` to back up every canonical tab before mutating.
    """
    target_tabs = list(tabs) if tabs is not None else list(LIVE_SNAPSHOT_TABS)
    _validate_unique_artifact_names(target_tabs)
    stamp = _safe_path_component(timestamp or _utc_stamp())
    snapshot_dir = _claim_snapshot_dir(dest_dir, stamp)

    for tab in target_tabs:
        try:
            grid = client.read_snapshot_values(tab)
        except WorksheetNotFound:
            print(f"snapshot: skipping {tab!r} (tab not present on the spreadsheet)")
            continue
        columns = _columns_for(tab, grid)
        out_path = snapshot_dir / f"{_safe_path_component(tab)}.csv"
        _write_exact_json(out_path.with_suffix(".raw.json"), grid)
        write_formula_safe_csv(
            out_path,
            _project_grid(grid, columns),
            exclusive=True,
        )

    return snapshot_dir


def snapshot_raw_tab(
    client: SheetsClient,
    tab: str,
    dest_dir: Path,
    *,
    timestamp: str | None = None,
) -> Path:
    """Snapshot one tab's full grid as safe CSV plus exact tagged user-entered-value JSON.

    Unlike :func:`snapshot_all_tabs` — which writes only a tab's *known* columns via
    :func:`_columns_for`, and so would DROP the "Budget Timeseries" tab's extra operator
    columns (``strategic_group``, ``strategic_goal``, ``notes``, ...) — this dumps the grid
    without dropping any row or column. The operator-facing CSV neutralizes formula-like text
    before it can be opened in spreadsheet software. The adjacent ``<tab>.raw.json`` retains the
    versioned tagged grid exactly as :meth:`SheetsClient.read_snapshot_values` returned it,
    distinguishing formulas from identical literal strings, native numbers/booleans, and empty
    cells for lossless entered-value recovery. Formatting and comments are not captured. Returns
    the current CSV path for caller compatibility. Reads only.
    """
    _validate_unique_artifact_names([tab])
    stamp = _safe_path_component(timestamp or _utc_stamp())
    snapshot_dir = _claim_snapshot_dir(dest_dir, stamp)
    grid = client.read_snapshot_values(tab)
    out_path = snapshot_dir / f"{_safe_path_component(tab)}.csv"
    lossless_path = out_path.with_suffix(".raw.json")
    _write_exact_json(lossless_path, grid)
    write_formula_safe_csv(
        out_path,
        _snapshot_csv_grid(grid),
        exclusive=True,
    )
    return out_path
