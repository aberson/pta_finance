"""Normalize a messy legacy/raw ``transactions`` ledger into the canonical schema.

The module separates **pure logic** (unit-testable, no I/O) from the **I/O orchestration**:

* :func:`normalize_rows` is a pure function over a list of ``transactions`` row dicts. It
  assigns missing ids, flags exact duplicates, and flags malformed rows — returning the
  normalized rows plus a :class:`NormalizeResult` of counts. It performs no Google calls
  and no disk writes, so it is trivially unit-testable and deterministic.
* :func:`normalize` is the I/O orchestration: it snapshots the sheet *first* (corruption
  protection), reads the ``transactions`` tab, calls :func:`normalize_rows`, then writes
  only the changed rows back via row-targeted :meth:`SheetsClient.upsert_rows`.

Invariants (plan.md §3, §11 "Step 4", §12 Appendix):

* **ID assignment delegates to** :mod:`pta_finance.ids` — this module NEVER re-derives the
  ``TXN-FY{yy}-{seq}`` format. A row already bearing an ``id`` keeps it untouched; an id-less
  *valid* row gets the next unused per-fiscal-year sequence, seeded from the max existing seq
  among rows already carrying a ``TXN-FY{yy}-NNNN`` id for that FY. Re-running is therefore
  **idempotent**: no existing id is reassigned and no duplicate id is minted.
* **Dedup** uses the plan's natural key ``sha1(f"{iso_date}|{amount_cents}|{normalized_payee}")``.
  The FIRST row with a given hash is kept; any later row with the same hash is FLAGGED
  ``needs_review`` — never silently dropped and never double-counted.
* **Malformed rows never crash the run.** A row whose ``date`` or ``amount`` is unparseable is
  flagged ``needs_review`` and SKIPPED for id-assignment + dedup (you cannot compute a fiscal
  year without a date). One bad legacy row must not abort normalization of the rest.

Columns come from :mod:`pta_finance.schema`; parsing from :mod:`pta_finance.models`. Neither is
re-implemented here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from pta_finance import ids, models, schema

if TYPE_CHECKING:
    from pta_finance.config import Config
    from pta_finance.sheets import SheetsClient

__all__ = [
    "NormalizeResult",
    "normalize_rows",
    "normalize",
]

# Matches a canonical transaction id so we can recover its fiscal-year token + sequence to
# seed the per-FY counter. The format itself is owned by ids.py; this only *reads* it back
# (an id is never re-derived from this pattern — ids.txn_id mints every new id).
_TXN_ID_RE = re.compile(r"^TXN-FY(?P<yy>\d{2})-(?P<seq>\d{4,})$")

# For normalizing a payee before hashing: anything that is not a letter or digit (after
# casefolding) is treated as punctuation/whitespace and collapsed away.
_NON_ALNUM = re.compile(r"[^0-9a-z]+")

# Sheets-native boolean text for the needs_review flag (matches models.format_bool).
_TRUE = "TRUE"


@dataclass(frozen=True)
class NormalizeResult:
    """Outcome of a :func:`normalize_rows` / :func:`normalize` run.

    ``rows`` are the normalized output rows (same shape and order as the input). The counts
    describe what happened to the rows. ``ids_assigned`` and ``duplicates_flagged`` are
    ORTHOGONAL (a single id-less row that collides with an earlier one is counted in both):

    * ``ids_assigned`` — id-less *valid* rows that received a freshly minted id.
    * ``duplicates_flagged`` — valid rows flagged ``needs_review`` because an earlier row had
      the same dedup hash (whether or not they were also assigned an id).
    * ``malformed_flagged`` — rows flagged ``needs_review`` because their date or amount was
      unparseable (id-assignment + dedup were skipped for them). Disjoint from the two above.
    * ``unchanged`` — rows that already had an id and were neither a duplicate nor malformed.
    """

    rows: list[dict[str, str]]
    ids_assigned: int
    duplicates_flagged: int
    malformed_flagged: int
    unchanged: int


def _normalize_payee(payee: str) -> str:
    """casefold + collapse whitespace + strip punctuation, per plan.md §12 dedup hash."""
    return _NON_ALNUM.sub(" ", payee.casefold()).strip()


def _amount_cents(amount: Decimal) -> int:
    """Integer cents for the dedup key (rounds to the nearest cent)."""
    return int((amount * 100).to_integral_value())


def _dedup_hash(d: date, amount: Decimal, payee: str) -> str:
    """The plan's natural key: ``sha1(f"{iso_date}|{amount_cents}|{normalized_payee}")``."""
    key = f"{d.isoformat()}|{_amount_cents(amount)}|{_normalize_payee(payee)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()  # noqa: S324  (non-crypto natural key)


def _seq_of(txn_id: str, fy: int) -> int | None:
    """Sequence number embedded in ``txn_id`` if it is the canonical id for ``fy``, else None.

    Only ids whose ``FY{yy}`` token matches this fiscal year's last-two-digits seed the
    counter — an id minted for a different FY must not bump this FY's sequence.
    """
    match = _TXN_ID_RE.match(txn_id)
    if match is None:
        return None
    if int(match.group("yy")) != fy % 100:
        return None
    return int(match.group("seq"))


def normalize_rows(
    rows: list[dict[str, str]],
    *,
    start_month: int,
) -> NormalizeResult:
    """Pure normalization of ``transactions`` rows: assign ids, dedup, flag malformed.

    Parameters
    ----------
    rows:
        Row dicts keyed by :data:`schema.TRANSACTIONS_COLUMNS` (a Google-Sheet ``transactions``
        record, values as cell strings). Order is preserved in the output.
    start_month:
        The fiscal-year start month (1 = January). Passed to
        :func:`pta_finance.ids.fiscal_year_label` to compute each row's FY label — never
        hard-coded here.

    Returns
    -------
    NormalizeResult
        The normalized rows (same shape/order) plus per-bucket counts. See
        :class:`NormalizeResult`.

    Notes
    -----
    Two passes over the rows:

    1. **Seed pass** — scan rows that already bear a canonical ``TXN-FY{yy}-NNNN`` id and record
       the max sequence seen per fiscal year. This is what makes id assignment idempotent: a
       second run sees the ids minted by the first run and continues from there rather than
       re-minting collisions.
    2. **Assign pass** — for each row in order: parse date+amount (catching
       :class:`ValueError`); a malformed row is flagged and skipped; a valid id-less row gets
       ``ids.txn_id(fy, next_seq)``; the dedup hash flags later collisions ``needs_review``.
    """
    # --- Pass 1: seed per-FY sequence counters from existing canonical ids. ---
    next_seq: dict[int, int] = {}
    for row in rows:
        existing_id = (row.get("id") or "").strip()
        if not existing_id:
            continue
        try:
            d = models.parse_date(row["date"])
        except (ValueError, KeyError):
            # A row with an id but an unparseable/absent date cannot seed an FY counter; its
            # id is still left untouched in pass 2.
            continue
        fy = ids.fiscal_year_label(d, start_month)
        seq = _seq_of(existing_id, fy)
        if seq is not None:
            next_seq[fy] = max(next_seq.get(fy, 0), seq)

    # --- Pass 2: assign ids, dedup, flag malformed (order preserved). ---
    out: list[dict[str, str]] = []
    seen_hashes: set[str] = set()
    ids_assigned = 0
    duplicates_flagged = 0
    malformed_flagged = 0
    unchanged = 0

    for row in rows:
        new_row = dict(row)  # copy; never mutate the caller's dict
        existing_id = (new_row.get("id") or "").strip()

        # Parse the two fields the run depends on. Either failing => malformed row.
        try:
            d = models.parse_date(new_row["date"])
            amount = models.parse_amount(new_row["amount"])
        except (ValueError, KeyError):
            # Malformed: flag for review, keep every other cell as-is, skip id + dedup.
            new_row["needs_review"] = _TRUE
            out.append(new_row)
            malformed_flagged += 1
            continue

        fy = ids.fiscal_year_label(d, start_month)

        assigned = False
        if not existing_id:
            seq = next_seq.get(fy, 0) + 1
            next_seq[fy] = seq
            new_row["id"] = ids.txn_id(fy, seq)
            assigned = True

        # Dedup: first occurrence of a hash is kept; later collisions are flagged.
        digest = _dedup_hash(d, amount, new_row.get("payee", ""))
        is_duplicate = digest in seen_hashes
        if is_duplicate:
            new_row["needs_review"] = _TRUE
        else:
            seen_hashes.add(digest)

        out.append(new_row)
        # id-assignment and dedup are ORTHOGONAL: a single row can be both id-less (assigned)
        # and a duplicate (flagged), so these counters overlap rather than partition. A row is
        # "unchanged" only when it already had an id and was neither a duplicate nor malformed.
        if assigned:
            ids_assigned += 1
        if is_duplicate:
            duplicates_flagged += 1
        if not assigned and not is_duplicate:
            unchanged += 1

    return NormalizeResult(
        rows=out,
        ids_assigned=ids_assigned,
        duplicates_flagged=duplicates_flagged,
        malformed_flagged=malformed_flagged,
        unchanged=unchanged,
    )


def _row_changed(before: dict[str, str], after: dict[str, str]) -> bool:
    """True if any cell differs across the canonical transaction columns."""
    return any(before.get(col, "") != after.get(col, "") for col in schema.TRANSACTIONS_COLUMNS)


def normalize(
    client: SheetsClient,
    config: Config,
    *,
    dest_dir: Path | None = None,
) -> NormalizeResult:
    """Normalize the live ``transactions`` tab: snapshot first, then write changed rows.

    Orchestration order (corruption-safe):

    1. **Snapshot every tab** to ``dest_dir/snapshots/<utc>/`` via
       :func:`pta_finance.backup.snapshot_all_tabs` BEFORE any write — so a bad normalize can be
       reconstructed.
    2. Read the ``transactions`` tab and run the pure :func:`normalize_rows` over it.
    3. Write back ONLY the rows that actually changed, keyed by id, through the row-targeted
       atomic :meth:`SheetsClient.upsert_rows` (untouched rows are never rewritten).

    Parameters
    ----------
    client:
        The :class:`~pta_finance.sheets.SheetsClient` (reads + writes the live sheet).
    config:
        The project :class:`~pta_finance.config.Config`; supplies ``fiscal_year.start_month``.
    dest_dir:
        Base directory for the pre-write snapshot (default: current working directory).

    Returns
    -------
    NormalizeResult
        The same result :func:`normalize_rows` produced (counts reflect the full input).
    """
    # Import here (not at module top) to avoid a hard import cycle and to keep the pure-logic
    # section free of I/O dependencies.
    from pta_finance import backup

    base = Path(dest_dir) if dest_dir is not None else Path.cwd()

    # 1. Snapshot BEFORE any mutation. Legacy path mutates the canonical tabs, so snapshot the
    #    full canonical registry (not just the live set).
    backup.snapshot_all_tabs(client, base, tabs=schema.TABS)

    # 2. Read + normalize.
    tab = schema.TAB_TRANSACTIONS
    original_rows = client.read_tab(tab)
    result = normalize_rows(original_rows, start_month=config.fiscal_year.start_month)

    # 3. Write back ONLY changed rows. A row that now has an id goes through the id-keyed
    #    atomic upsert (robust to row reordering). A changed row with NO id — a malformed row
    #    whose needs_review flag was just set — is written by its sheet POSITION so the flag
    #    still reaches the sheet; otherwise the rows that most need review would be invisible
    #    to the operator. read_tab returns data rows in order, so original_rows[i] is sheet
    #    row i + 2 (row 1 is the header).
    changed_by_id: dict[str, dict[str, str]] = {}
    changed_by_index: dict[int, dict[str, str]] = {}
    for index, (before, after) in enumerate(zip(original_rows, result.rows, strict=True), start=2):
        if not _row_changed(before, after):
            continue
        row_id = (after.get("id") or "").strip()
        if row_id:
            changed_by_id[row_id] = after
        else:
            changed_by_index[index] = after

    if changed_by_id:
        client.upsert_rows(tab, changed_by_id)
    if changed_by_index:
        client.update_rows_by_index(tab, changed_by_index)
    return result
