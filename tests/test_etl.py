"""Tests for pta_finance.etl — pure-logic normalization + the production normalize path.

Two tiers:

* **Pure logic** on :func:`etl.normalize_rows`: a legacy fixture (ids present + absent, an
  exact-duplicate pair, a malformed-amount row, a malformed-date row) and an idempotency check.
  No I/O at all.
* **Integration through the production caller** :func:`etl.normalize`: a fake
  :class:`~pta_finance.sheets.SheetsClient` seeded with a legacy grid, asserting (a) the
  snapshot was taken BEFORE any write, (b) ``upsert_rows`` carried the assigned ids, and (c)
  existing ids were not changed. This exercises the real ``normalize`` path end-to-end, not a
  reimplementation.
"""

from __future__ import annotations

from pathlib import Path

from pta_finance import etl, ids, schema
from pta_finance.config import Config

_COLS = schema.TRANSACTIONS_COLUMNS
_START_MONTH = 1  # calendar-year fiscal year => FY label == date.year


def _row(**overrides: str) -> dict[str, str]:
    """A transactions row dict with every schema column present (blank unless overridden)."""
    row = {col: "" for col in _COLS}
    row.update(overrides)
    return row


# --- A legacy fixture mixing every case the normalizer must handle. ----------
#
#   1. existing-id valid row       -> id untouched, unchanged
#   2. id-less valid row           -> id assigned
#   3. id-less valid row (dup of 2) -> id assigned + flagged needs_review (dedup)
#   4. id-less malformed AMOUNT     -> flagged needs_review, no id, skipped dedup
#   5. id-less malformed DATE       -> flagged needs_review, no id, skipped dedup
#   6. id-less valid row, new FY    -> id assigned in its own FY counter
def _legacy_fixture() -> list[dict[str, str]]:
    return [
        _row(
            id="TXN-FY26-0007",
            date="2026-01-10",
            amount="100.00",
            payee="Existing Vendor",
            type="expense",
        ),
        _row(date="2026-02-15", amount="50.00", payee="Office Supplies Co.", type="expense"),
        # Exact duplicate of the previous row (same date/amount/payee, different punctuation
        # + casing to prove the payee normalization in the dedup hash).
        _row(date="2026-02-15", amount="50.00", payee="office  supplies co", type="expense"),
        _row(date="2026-03-01", amount="not-a-number", payee="Bad Amount LLC", type="expense"),
        _row(date="13/40/2026", amount="20.00", payee="Bad Date LLC", type="expense"),
        _row(date="2025-12-31", amount="75.00", payee="Prior Year Vendor", type="income"),
    ]


# --- Pure-logic tests --------------------------------------------------------


def test_normalize_rows_legacy_fixture() -> None:
    """Legacy mix: ids assigned only to id-less valid rows; assigned ids unique +
    FY-correct; the duplicate + both malformed rows flagged needs_review; existing id
    untouched; nothing crashed."""
    rows = _legacy_fixture()
    result = etl.normalize_rows(rows, start_month=_START_MONTH)
    out = result.rows

    # Same shape + order, no rows lost.
    assert len(out) == len(rows)
    assert all(set(r) == set(_COLS) for r in out)

    # (1) Existing id is untouched.
    assert out[0]["id"] == "TXN-FY26-0007"
    assert out[0]["needs_review"] == ""  # valid, non-dup -> not flagged

    # (2) First id-less valid row gets a fresh FY26 id (seeded past the existing 0007).
    assert out[1]["id"] == ids.txn_id(2026, 8)  # max existing seq (7) + 1
    assert out[1]["needs_review"] == ""

    # (3) Exact duplicate of (2): still gets its OWN id (never dropped) but is FLAGGED.
    assert out[2]["id"] == ids.txn_id(2026, 9)
    assert out[2]["needs_review"] == "TRUE"

    # (4) Malformed amount: flagged, no id assigned, original cells preserved.
    assert out[3]["needs_review"] == "TRUE"
    assert out[3]["id"] == ""
    assert out[3]["amount"] == "not-a-number"  # untouched, not crashed

    # (5) Malformed date: flagged, no id assigned.
    assert out[4]["needs_review"] == "TRUE"
    assert out[4]["id"] == ""
    assert out[4]["date"] == "13/40/2026"

    # (6) New fiscal year (2025) gets its own counter starting at 0001.
    assert out[5]["id"] == ids.txn_id(2025, 1)
    assert out[5]["needs_review"] == ""

    # Assigned ids are globally unique.
    assigned = [r["id"] for r in out if r["id"]]
    assert len(assigned) == len(set(assigned))

    # Counts: ids_assigned and duplicates_flagged overlap (row 3 is both id-less AND a dup).
    assert result.ids_assigned == 3  # rows 2, 3, 6
    assert result.duplicates_flagged == 1  # row 3 (also counted in ids_assigned)
    assert result.malformed_flagged == 2  # rows 4, 5
    assert result.unchanged == 1  # row 1


def test_normalize_rows_existing_ids_never_reassigned() -> None:
    """A row with a non-empty id keeps it verbatim even when its FY counter is busy."""
    rows = [
        _row(id="TXN-FY26-0042", date="2026-04-01", amount="10.00", payee="Kept"),
        _row(date="2026-04-02", amount="11.00", payee="New"),
    ]
    out = etl.normalize_rows(rows, start_month=_START_MONTH).rows
    assert out[0]["id"] == "TXN-FY26-0042"  # untouched
    # The id-less row continues PAST the existing 0042 (idempotency seed), not from 0001.
    assert out[1]["id"] == ids.txn_id(2026, 43)


def test_normalize_rows_is_idempotent() -> None:
    """Feeding normalize_rows's OUTPUT back through it produces no new ids and no newly
    flagged duplicates — the run is stable."""
    rows = _legacy_fixture()
    first = etl.normalize_rows(rows, start_month=_START_MONTH)
    second = etl.normalize_rows(first.rows, start_month=_START_MONTH)

    # The rows are byte-for-byte identical across the second pass.
    assert second.rows == first.rows

    # No NEW ids minted and no NEW duplicates flagged on the second pass: every row that now
    # has an id keeps it (so nothing is id-less to assign), and the already-flagged dup stays
    # flagged but is not re-counted as a fresh assignment.
    assert second.ids_assigned == 0
    # The exact-duplicate row is still flagged (its hash collides on every pass), but no row
    # gained a NEW id.
    second_ids = [r["id"] for r in second.rows if r["id"]]
    first_ids = [r["id"] for r in first.rows if r["id"]]
    assert second_ids == first_ids


# --- Integration through the production caller (etl.normalize) ---------------


class RecordingNormalizeClient:
    """A fake SheetsClient that records the order of read/snapshot/write calls.

    ``read_tab`` returns the seeded legacy grid as record dicts; ``upsert_rows`` records the
    payload; a shared ``events`` log proves the snapshot read happened BEFORE the write.
    """

    def __init__(self, transactions: list[dict[str, str]]) -> None:
        self._tabs: dict[str, list[dict[str, str]]] = {tab: [] for tab in schema.TABS}
        self._tabs[schema.TAB_TRANSACTIONS] = transactions
        self.events: list[str] = []
        self.upserts: list[tuple[str, dict[str, dict[str, str]]]] = []
        self.index_writes: list[tuple[str, dict[int, dict[str, str]]]] = []

    def read_tab(self, tab: str) -> list[dict[str, str]]:
        self.events.append(f"read:{tab}")
        return [dict(r) for r in self._tabs.get(tab, [])]

    def upsert_rows(self, tab: str, rows_by_id: dict[str, dict[str, str]]) -> None:
        self.events.append(f"upsert:{tab}")
        self.upserts.append((tab, {k: dict(v) for k, v in rows_by_id.items()}))

    def update_rows_by_index(self, tab: str, rows_by_index: dict[int, dict[str, str]]) -> None:
        self.events.append(f"update_index:{tab}")
        self.index_writes.append((tab, {k: dict(v) for k, v in rows_by_index.items()}))


def test_normalize_snapshots_before_write_and_upserts_assigned_ids(
    fake_config: Config, tmp_path: Path
) -> None:
    """etl.normalize: (a) a snapshot is taken before any write, (b) upsert_rows carries the
    assigned ids, (c) existing ids are not changed. Real production path, mocked sheet."""
    legacy = [
        _row(
            id="TXN-FY26-0001",
            date="2026-01-05",
            amount="100.00",
            payee="Existing Vendor",
            type="expense",
        ),
        _row(date="2026-01-06", amount="40.00", payee="New Vendor", type="expense"),
        _row(date="2026-01-07", amount="bad", payee="Bad Amount", type="expense"),
    ]
    client = RecordingNormalizeClient(legacy)

    result = etl.normalize(client, fake_config, dest_dir=tmp_path)  # type: ignore[arg-type]

    # (a) Ordering: the snapshot reads every tab BEFORE the single transactions upsert.
    upsert_index = client.events.index(f"upsert:{schema.TAB_TRANSACTIONS}")
    snapshot_reads = [i for i, e in enumerate(client.events) if e.startswith("read:")]
    assert snapshot_reads, "snapshot must read tabs"
    assert max(snapshot_reads) < upsert_index, "all snapshot reads precede the write"
    # A snapshot directory was actually written before the upsert.
    snap_root = tmp_path / "snapshots"
    assert snap_root.is_dir()
    assert any(snap_root.iterdir())

    # Exactly one upsert, to the transactions tab.
    assert len(client.upserts) == 1
    tab, rows_by_id = client.upserts[0]
    assert tab == schema.TAB_TRANSACTIONS

    # (b) The newly assigned id was written. The existing FY26 row is 0001, so the id-less
    # valid row becomes 0002.
    assigned_id = ids.txn_id(2026, 2)
    assert assigned_id in rows_by_id
    assert rows_by_id[assigned_id]["payee"] == "New Vendor"

    # (c) The existing id is NOT in the changed set — its row was untouched, so normalize did
    # not rewrite it.
    assert "TXN-FY26-0001" not in rows_by_id

    # The malformed row has no id, so it is written by POSITION (not the id-keyed upsert) so
    # its needs_review flag still reaches the sheet. It is the 3rd data row => sheet row 4.
    assert all(row_id for row_id in rows_by_id), "id-keyed upsert never carries a blank-id row"
    assert len(client.index_writes) == 1
    _itab, by_index = client.index_writes[0]
    assert _itab == schema.TAB_TRANSACTIONS
    assert 4 in by_index, "malformed row persisted at its sheet position (row 4)"
    assert by_index[4]["needs_review"] == "TRUE"
    assert by_index[4]["payee"] == "Bad Amount"

    # Counts from the production path match the input.
    assert result.ids_assigned == 1
    assert result.malformed_flagged == 1
    assert result.unchanged == 1
