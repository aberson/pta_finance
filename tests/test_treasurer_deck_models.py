"""Contract tests for the Treasurer-deck models: canonical JSON, IDs, state, persistence.

All fixtures are fictional (Example PTA style); no real identity, finance value, or
Google resource appears here.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from pta_finance.treasurer_deck import models

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)
HEX64 = "a" * 64


def _span(start: int = 0, end: int = 5, fragment_sha256: str = HEX64) -> models.SourceSpan:
    return models.SourceSpan(
        start_codepoint=start,
        end_codepoint=end,
        start_line=1,
        start_column=start + 1,
        end_line=1,
        end_column=end + 1,
        fragment_sha256=fragment_sha256,
    )


def _brief(run_id: str) -> models.BriefDraft:
    task_record = {
        "module_key": "position",
        "question": "What is the example balance?",
        "required": True,
        "source_spans": [_span().to_json()],
    }
    task = models.BriefTask(
        task_id=models.task_id_for(task_record, "position"),
        source_spans=(_span(),),
        module_key="position",
        question="What is the example balance?",
        required=True,
    )
    return models.BriefDraft(
        run_id=run_id,
        request_sha256=HEX64,
        tasks=(task,),
        workflow_guidance=(),
        ignored=(),
    )


# --- canonical JSON ------------------------------------------------------------------


def test_canonical_json_sorts_keys_and_compact_separators() -> None:
    """Objects serialize with lexicographically sorted keys and no whitespace."""
    assert models.canonical_json({"b": 1, "a": [True, False, None]}) == (
        '{"a":[true,false,null],"b":1}'
    )


def test_canonical_json_decimal_plain_forms() -> None:
    """Decimals are plain-notation JSON strings; trailing zeroes/dot and -0 normalize."""
    assert models.canonical_json(Decimal("10.00")) == '"10"'
    assert models.canonical_json(Decimal("3215.16")) == '"3215.16"'
    assert models.canonical_json(Decimal("-0.000")) == '"0"'
    assert models.canonical_json(Decimal("1E+3")) == '"1000"'


def test_canonical_json_rejects_floats_and_non_finite() -> None:
    """Binary floating point and non-finite Decimals are rejected outright."""
    with pytest.raises(models.ContractError):
        models.canonical_json(1.5)
    with pytest.raises(models.ContractError):
        models.canonical_json(Decimal("NaN"))


def test_canonical_json_nfc_normalizes_and_rejects_duplicate_keys() -> None:
    """Strings normalize to NFC; two keys that collide after NFC are rejected."""
    composed = "café"
    decomposed = "café"
    assert models.canonical_json(decomposed) == models.canonical_json(composed)
    with pytest.raises(models.ContractError):
        models.canonical_json({composed: 1, decomposed: 2})


def test_digest_of_omits_named_fields() -> None:
    """An artifact digest covers the canonical object minus its own digest field."""
    record = {"a": 1, "digest": "x"}
    assert models.digest_of(record, omit=("digest",)) == models.json_sha256({"a": 1})


# --- identifiers and containment -----------------------------------------------------


def test_new_run_id_matches_grammar() -> None:
    """Minted IDs are ``<YYYYMMDDTHHMMSSZ>-<24 lowercase hex>``."""
    run_id = models.new_run_id(NOW)
    assert run_id.startswith("20260831T120000Z-")
    assert models.validate_run_id(run_id) == run_id


def test_new_run_id_requires_utc() -> None:
    """Naive datetimes cannot mint run IDs."""
    with pytest.raises(models.ContractError):
        models.new_run_id(datetime(2026, 8, 31, 12, 0, 0))


@pytest.mark.parametrize(
    "bad",
    [
        "20261331T120000Z-" + "a" * 24,  # month 13
        "20260831T120000Z-" + "A" * 24,  # uppercase hex
        "20260831T120000Z-" + "a" * 23,  # short suffix
        "20260831T120000Z-" + "a" * 24 + "/x",  # path separator
        "../20260831T120000Z-" + "a" * 24,  # traversal
        "20260831t120000z-" + "a" * 24,  # lowercase stamp
    ],
)
def test_validate_run_id_rejects_bad_grammar(bad: str) -> None:
    """Run IDs are IDs, never paths; the closed grammar rejects everything else."""
    with pytest.raises(models.ContractError):
        models.validate_run_id(bad)


def test_resolve_run_dir_is_an_immediate_child(tmp_path: Path) -> None:
    """A valid run ID resolves to an immediate child of the run root."""
    run_id = models.new_run_id(NOW)
    resolved = models.resolve_run_dir(tmp_path, run_id)
    assert resolved.parent == tmp_path
    assert resolved.name == run_id


def test_resolve_run_dir_rejects_symlinked_run(tmp_path: Path) -> None:
    """A symlink/reparse-point run directory is rejected (no traversal component).

    On Windows, symlink creation may need privilege, but a directory junction does
    not — falling back to one keeps the reparse-point branch covered on the platform
    the gates run on.
    """
    run_id = models.new_run_id(NOW)
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / run_id
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("symlink creation is not permitted in this environment")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    with pytest.raises(models.ContractError, match="symlink/reparse point"):
        models.resolve_run_dir(tmp_path, run_id)


def test_content_slug_normalizes_code_owned_keys() -> None:
    """Slugs lowercase, hyphenate, trim, and truncate to 20 characters."""
    assert models.content_slug("Budget_vs_Actual") == "budget-vs-actual"
    assert len(models.content_slug("a" * 40)) == 20
    with pytest.raises(models.ContractError):
        models.content_slug("___")


# --- timestamps ----------------------------------------------------------------------


def test_format_and_parse_utc_round_trip() -> None:
    """RFC 3339 UTC values with a trailing Z round-trip exactly."""
    text = models.format_utc(NOW)
    assert text == "2026-08-31T12:00:00Z"
    assert models.parse_utc(text) == NOW


@pytest.mark.parametrize("bad", ["2026-08-31T12:00:00+00:00", "2026-08-31 12:00:00Z", "x"])
def test_parse_utc_rejects_non_canonical(bad: str) -> None:
    with pytest.raises(models.ContractError):
        models.parse_utc(bad)


def test_parse_iso_date_is_strict() -> None:
    assert models.parse_iso_date("2026-08-31") == date(2026, 8, 31)
    with pytest.raises(models.ContractError):
        models.parse_iso_date("2026-8-31")


# --- units, values, audience ---------------------------------------------------------


def test_validate_unit_accepts_the_closed_set() -> None:
    for unit in ("text", "boolean", "count", "percent", "date", "currency:USD"):
        assert models.validate_unit(unit) == unit
    for bad in ("currency:usd", "currency:US", "money", ""):
        with pytest.raises(models.ContractError):
            models.validate_unit(bad)


def test_validate_fact_value_enforces_type_coherence() -> None:
    """Money is finite Decimal; count rejects bools; date needs a real date."""
    models.validate_fact_value(Decimal("12.50"), "currency:USD")
    models.validate_fact_value(3, "count")
    with pytest.raises(models.ContractError):
        models.validate_fact_value(True, "count")
    with pytest.raises(models.ContractError):
        models.validate_fact_value("12.50", "currency:USD")
    with pytest.raises(models.ContractError):
        models.validate_fact_value(Decimal("NaN"), "currency:USD")
    with pytest.raises(models.ContractError):
        models.validate_fact_value("2026-08-31", "date")


def test_audience_lattice_is_monotonic() -> None:
    """Combining audiences takes the most restrictive; inputs can only restrict."""
    assert (
        models.combine_audience(models.AUDIENCE_PUBLIC, models.AUDIENCE_INTERNAL)
        == models.AUDIENCE_INTERNAL
    )
    assert models.restrict_audience(models.AUDIENCE_PUBLIC, None) == models.AUDIENCE_INTERNAL
    assert models.restrict_audience(models.AUDIENCE_PUBLIC, "bogus") == models.AUDIENCE_INTERNAL
    assert (
        models.restrict_audience(models.AUDIENCE_INTERNAL, models.AUDIENCE_PUBLIC)
        == models.AUDIENCE_INTERNAL
    )
    assert (
        models.restrict_audience(models.AUDIENCE_PUBLIC, models.AUDIENCE_PUBLIC)
        == models.AUDIENCE_PUBLIC
    )


# --- source spans and brief ----------------------------------------------------------


def test_source_span_round_trip_rejects_unknown_fields() -> None:
    span = _span()
    assert models.SourceSpan.from_json(span.to_json()) == span
    with pytest.raises(models.ContractError):
        models.SourceSpan.from_json({**span.to_json(), "extra": 1})


def test_source_span_rejects_empty_or_zero_based_coordinates() -> None:
    with pytest.raises(models.ContractError):
        models.SourceSpan(0, 0, 1, 1, 1, 1, HEX64)
    with pytest.raises(models.ContractError):
        models.SourceSpan(0, 1, 0, 1, 1, 2, HEX64)


def test_span_validation_binds_fragment_digest_to_the_request() -> None:
    text = "hello world"
    good = models.SourceSpan(0, 5, 1, 1, 1, 6, models.sha256_hex(text[0:5].encode("utf-8")))
    models.validate_span_against_text(good, text)
    bad = models.SourceSpan(0, 5, 1, 1, 1, 6, "b" * 64)
    with pytest.raises(models.ContractError):
        models.validate_span_against_text(bad, text)
    with pytest.raises(models.ContractError):
        models.validate_span_against_text(good, "hi")


def test_overlapping_spans_are_rejected() -> None:
    with pytest.raises(models.ContractError):
        models.validate_spans_disjoint([_span(0, 5), _span(3, 8)])


def test_brief_draft_round_trip_and_strict_root() -> None:
    """The brief artifact round-trips; unknown roots and bad versions reject."""
    run_id = models.new_run_id(NOW)
    brief = _brief(run_id)
    assert models.BriefDraft.from_json(brief.to_json()) == brief
    with pytest.raises(models.ContractError):
        models.BriefDraft.from_json({**brief.to_json(), "facts": []})
    with pytest.raises(models.ContractError):
        models.BriefDraft.from_json({**brief.to_json(), "schema_version": 2})


# --- fact records --------------------------------------------------------------------


def _available_fact(**overrides: object) -> models.FactRecord:
    base: dict[str, object] = {
        "fact_id": "position.bank_balance",
        "label": "Example bank balance",
        "value": Decimal("1234.56"),
        "unit": "currency:USD",
        "basis": "cash",
        "origin": "observed",
        "audience": models.AUDIENCE_INTERNAL,
        "status": "available",
        "as_of_date": date(2026, 8, 31),
        "source_alias": models.SOURCE_ALIAS_BUDGET_TIMESERIES,
        "locator": "'Example Tab'!A1:B9",
        "source_hash": HEX64,
        "captured_at": NOW,
    }
    base.update(overrides)
    return models.FactRecord(**base)  # type: ignore[arg-type]


def test_available_fact_requires_provenance_and_period() -> None:
    """An available fact carries value, unit coherence, provenance, and a period."""
    fact = _available_fact()
    assert models.FactRecord.from_json(fact.to_json()) == fact
    with pytest.raises(models.ContractError):
        _available_fact(source_hash=None)
    with pytest.raises(models.ContractError):
        _available_fact(as_of_date=None)
    with pytest.raises(models.ContractError):
        _available_fact(value=None)


def test_missing_fact_keeps_expected_unit_with_null_value() -> None:
    fact = models.FactRecord(
        fact_id="position.bank_balance",
        label="Example bank balance",
        value=None,
        unit="currency:USD",
        basis="cash",
        origin="observed",
        audience=models.AUDIENCE_INTERNAL,
        status="missing",
    )
    assert fact.value is None
    assert fact.unit == "currency:USD"
    with pytest.raises(models.ContractError):
        models.FactRecord(
            fact_id="position.bank_balance",
            label="Example bank balance",
            value=Decimal("1.00"),
            unit="currency:USD",
            basis="cash",
            origin="observed",
            audience=models.AUDIENCE_INTERNAL,
            status="missing",
        )


def test_financial_bases_are_a_closed_typed_set() -> None:
    """Cash/reserve/allocated/committed/spent/... are typed bases, not prose."""
    for basis in models.BASES:
        _available_fact(basis=basis)
    with pytest.raises(models.ContractError):
        _available_fact(basis="miscellaneous")


def test_derived_fact_requires_inputs_and_versioned_calculation() -> None:
    fact = _available_fact(
        fact_id="position.net_change",
        origin="derived",
        input_fact_ids=("position.bank_balance",),
        calculation_id="position.net_change@v1",
    )
    assert fact.calculation_id == "position.net_change@v1"
    with pytest.raises(models.ContractError):
        _available_fact(fact_id="position.net_change", origin="derived")
    with pytest.raises(models.ContractError):
        _available_fact(
            fact_id="position.net_change",
            origin="derived",
            input_fact_ids=("position.bank_balance",),
            calculation_id="unversioned",
        )


def test_fact_id_grammar_with_period_slug() -> None:
    models.validate_fact_id("history.year_end_balance@fy-2025-26")
    for bad in ("balance", "Position.balance", "position..balance", "position.balance@FY26"):
        with pytest.raises(models.ContractError):
            models.validate_fact_id(bad)


def test_fact_money_round_trips_as_typed_decimal() -> None:
    """Unit-typed decoding restores Decimal money (never a float, never a string)."""
    fact = _available_fact()
    restored = models.FactRecord.from_json(fact.to_json())
    assert isinstance(restored.value, Decimal)
    assert restored.value == Decimal("1234.56")


# --- graphic datasets ----------------------------------------------------------------


def _columns() -> tuple[models.DatasetColumn, ...]:
    return (
        models.DatasetColumn("category", "string", None, models.AUDIENCE_PUBLIC),
        models.DatasetColumn("amount", "money", "currency:USD", models.AUDIENCE_PUBLIC),
    )


def _dataset(**overrides: object) -> models.GraphicDataset:
    arguments: dict[str, object] = {
        "graphic_key": "expense_donut",
        "columns": _columns(),
        "rows": (("Example Dues", Decimal("100.00")), ("Example Events", Decimal("50.00"))),
        "source_fact_ids": ("budget.total_expense",),
        "source_grid_hashes": (HEX64,),
        "calculation_id": "expense_donut.rollup@v1",
        "selector": {"fiscal_year": "2027"},
        "selector_echo": {"fiscal_year": "2027"},
        "declared_totals": {"amount": Decimal("150.00")},
    }
    arguments.update(overrides)
    return models.GraphicDataset.create(**arguments)  # type: ignore[arg-type]


def test_graphic_dataset_id_and_provenance_are_content_derived() -> None:
    dataset = _dataset()
    assert dataset.dataset_id.startswith("expense_donut-")
    assert models.DATASET_ID_PATTERN.match(dataset.dataset_id)
    restored = models.GraphicDataset.from_json(dataset.to_json_with_provenance())
    assert restored == dataset
    with pytest.raises(models.ContractError):
        models.GraphicDataset.from_json(
            {**dataset.to_json_with_provenance(), "dataset_id": "expense_donut-" + "0" * 12}
        )


def test_graphic_dataset_blocks_numeric_text() -> None:
    """A string where a money cell belongs blocks the dataset (numeric text)."""
    with pytest.raises(models.ContractError):
        _dataset(rows=(("Example Dues", "100.00"),), declared_totals={})


def test_graphic_dataset_blocks_selector_mismatch_and_total_disagreement() -> None:
    with pytest.raises(models.ContractError):
        _dataset(selector_echo={"fiscal_year": "2026"})
    with pytest.raises(models.ContractError):
        _dataset(declared_totals={"amount": Decimal("999.00")})


def test_graphic_dataset_blocks_duplicate_columns_and_unit_rules() -> None:
    duplicate = (_columns()[0], _columns()[0])
    with pytest.raises(models.ContractError):
        _dataset(columns=duplicate, rows=(("a", "b"),), declared_totals={})
    with pytest.raises(models.ContractError):
        models.DatasetColumn("amount", "money", None, models.AUDIENCE_PUBLIC)
    with pytest.raises(models.ContractError):
        models.DatasetColumn("share", "percent", "currency:USD", models.AUDIENCE_PUBLIC)
    with pytest.raises(models.ContractError):
        models.DatasetColumn("label", "string", "text", models.AUDIENCE_PUBLIC)


# --- state machine and approvals -----------------------------------------------------


def test_state_machine_allows_only_the_durable_sequence() -> None:
    models.validate_transition(models.STATE_PREPARED, models.STATE_BRIEF_APPROVED)
    models.validate_transition(models.STATE_CANDIDATE_CREATED, models.STATE_QA_FAILED)
    with pytest.raises(models.RunStateError):
        models.validate_transition(models.STATE_PREPARED, models.STATE_FACTS_APPROVED)
    with pytest.raises(models.RunStateError):
        models.validate_transition(models.STATE_QA_FAILED, models.STATE_QA_PASSED)
    with pytest.raises(models.RunStateError):
        models.validate_transition(models.STATE_PROMOTED, models.STATE_PREPARED)


def _manifest_with_brief_approval(run_id: str) -> models.Manifest:
    request_digest = models.sha256_hex(b"example request")
    brief_digest = models.sha256_hex(b"example brief")
    approval = models.Approval(
        stage=models.APPROVAL_STAGE_BRIEF,
        approved_sha256=brief_digest,
        approved_at=NOW,
        upstream_sha256=(request_digest,),
    )
    return models.Manifest(
        run_id=run_id,
        created_at=NOW,
        state=models.STATE_BRIEF_APPROVED,
        generation=2,
        artifact_sha256=(
            ("brief.draft.json", brief_digest),
            ("request.txt", request_digest),
        ),
        approvals=(approval,),
    )


def test_manifest_consistency_accepts_a_sealed_approval() -> None:
    manifest = _manifest_with_brief_approval(models.new_run_id(NOW))
    models.validate_manifest_consistency(manifest)
    assert models.Manifest.from_json(manifest.to_json()) == manifest


def test_changed_artifact_invalidates_downstream_approval() -> None:
    """A changed brief digest invalidates the approval that sealed it."""
    manifest = _manifest_with_brief_approval(models.new_run_id(NOW))
    tampered = models.Manifest(
        run_id=manifest.run_id,
        created_at=manifest.created_at,
        state=manifest.state,
        generation=manifest.generation,
        artifact_sha256=(
            ("brief.draft.json", models.sha256_hex(b"changed brief")),
            manifest.artifact_sha256[1],
        ),
        approvals=manifest.approvals,
    )
    with pytest.raises(models.RunStateError):
        models.validate_manifest_consistency(tampered)


def test_changed_upstream_invalidates_downstream_approval() -> None:
    """A changed request digest invalidates the brief approval built on it."""
    manifest = _manifest_with_brief_approval(models.new_run_id(NOW))
    tampered = models.Manifest(
        run_id=manifest.run_id,
        created_at=manifest.created_at,
        state=manifest.state,
        generation=manifest.generation,
        artifact_sha256=(
            manifest.artifact_sha256[0],
            ("request.txt", models.sha256_hex(b"changed request")),
        ),
        approvals=manifest.approvals,
    )
    with pytest.raises(models.RunStateError):
        models.validate_manifest_consistency(tampered)


def test_state_requires_its_approvals() -> None:
    """BRIEF_APPROVED without a sealed brief approval is inconsistent (never inferred)."""
    run_id = models.new_run_id(NOW)
    manifest = models.Manifest(
        run_id=run_id,
        created_at=NOW,
        state=models.STATE_BRIEF_APPROVED,
        generation=2,
        artifact_sha256=(),
        approvals=(),
    )
    with pytest.raises(models.RunStateError):
        models.validate_manifest_consistency(manifest)


def _story_approved_manifest(run_id: str) -> models.Manifest:
    """A sealed STATE_STORY_APPROVED manifest exercising the facts/story stage rows."""
    artifacts = {
        "request.txt": models.sha256_hex(b"example request"),
        "brief.draft.json": models.sha256_hex(b"example brief"),
        "facts.snapshot.json": models.sha256_hex(b"example facts"),
        "deck.bundle.json": models.sha256_hex(b"example deck"),
    }
    base = models.Manifest(
        run_id=run_id,
        created_at=NOW,
        state=models.STATE_PREPARED,
        generation=1,
        artifact_sha256=tuple(sorted(artifacts.items())),
        approvals=(),
    )
    approvals = tuple(
        models.build_approval(base, stage, NOW)
        for stage in (
            models.APPROVAL_STAGE_BRIEF,
            models.APPROVAL_STAGE_FACTS,
            models.APPROVAL_STAGE_STORY,
        )
    )
    return models.Manifest(
        run_id=run_id,
        created_at=NOW,
        state=models.STATE_STORY_APPROVED,
        generation=4,
        artifact_sha256=base.artifact_sha256,
        approvals=approvals,
    )


def test_changed_fact_snapshot_invalidates_downstream_story_approval() -> None:
    """The facts/story stage mappings work: a changed snapshot kills the story seal."""
    run_id = models.new_run_id(NOW)
    manifest = _story_approved_manifest(run_id)
    models.validate_manifest_consistency(manifest)
    changed = {
        path: (models.sha256_hex(b"regenerated facts") if path == "facts.snapshot.json" else digest)
        for path, digest in manifest.artifact_sha256
    }
    tampered = models.Manifest(
        run_id=run_id,
        created_at=NOW,
        state=models.STATE_STORY_APPROVED,
        generation=5,
        artifact_sha256=tuple(sorted(changed.items())),
        approvals=manifest.approvals,
    )
    with pytest.raises(models.RunStateError):
        models.validate_manifest_consistency(tampered)
    # Dropping the story approval while claiming STORY_APPROVED is also inconsistent.
    truncated = models.Manifest(
        run_id=run_id,
        created_at=NOW,
        state=models.STATE_STORY_APPROVED,
        generation=5,
        artifact_sha256=manifest.artifact_sha256,
        approvals=manifest.approvals[:2],
    )
    with pytest.raises(models.RunStateError):
        models.validate_manifest_consistency(truncated)


def test_promotion_attempt_round_trips_with_final_version() -> None:
    """The 5.1 promotion-attempt shape (allowlisted final ID/version) round-trips."""
    attempt = models.PromotionAttempt(
        attempt=1,
        started_at=NOW,
        reconciliation_result="none",
        final_id=None,
        final_version=None,
        pre_sha256=HEX64,
        post_sha256=None,
        failure_code="EXAMPLE_FAILURE",
        finished_at=NOW,
    )
    assert models.PromotionAttempt.from_json(attempt.to_json()) == attempt
    with pytest.raises(models.ContractError):
        models.PromotionAttempt.from_json({**attempt.to_json(), "extra": 1})


def test_remote_ids_are_allowlisted_and_empty_in_step_14() -> None:
    """No remote ID field is allowlisted yet; anything else refuses to persist."""
    with pytest.raises(models.ContractError):
        models.Manifest(
            run_id=models.new_run_id(NOW),
            created_at=NOW,
            state=models.STATE_PREPARED,
            generation=1,
            artifact_sha256=(),
            approvals=(),
            remote_ids=(("candidate_file_id", "opaque"),),
        )


# --- locked atomic persistence -------------------------------------------------------


def test_create_run_claims_directory_and_commits_initial_manifest(tmp_path: Path) -> None:
    manifest, run_dir = models.create_run(tmp_path, now=NOW)
    assert run_dir.parent == tmp_path
    assert manifest.state == models.STATE_PREPARED
    assert manifest.generation == 1
    assert models.load_run(run_dir) == manifest


def test_claim_run_dir_collision_fails_closed(tmp_path: Path) -> None:
    run_id = models.new_run_id(NOW)
    models.claim_run_dir(tmp_path, run_id)
    with pytest.raises(models.RunCollisionError):
        models.claim_run_dir(tmp_path, run_id)


def test_run_lock_is_single_writer(tmp_path: Path) -> None:
    _, run_dir = models.create_run(tmp_path, now=NOW)
    with models.RunLock(run_dir):
        with pytest.raises(models.RunLockedError):
            models.RunLock(run_dir).acquire()
    # Released: the lock can be taken again.
    with models.RunLock(run_dir):
        pass


def test_transaction_writes_artifacts_first_and_manifest_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Artifacts hit disk before the manifest replace; digests match the objects."""
    manifest, run_dir = models.create_run(tmp_path, now=NOW)
    brief = _brief(manifest.run_id)
    write_order: list[str] = []
    real_write = models._atomic_write_bytes

    def recording_write(target: Path, data: bytes) -> None:
        write_order.append(target.name)
        real_write(target, data)

    monkeypatch.setattr(models, "_atomic_write_bytes", recording_write)
    with models.RunTransaction(run_dir) as txn:
        request_digest = txn.write_bytes_artifact("request.txt", b"example request text")
        brief_digest = txn.write_json_artifact("brief.draft.json", brief.to_json())
        updated = txn.commit()
    # Observed ordering: both artifacts strictly precede the manifest replace.
    assert write_order == ["request.txt", "brief.draft.json", models.MANIFEST_NAME]
    assert updated.generation == 2
    assert brief_digest == brief.digest()
    assert updated.artifact_map() == {
        "brief.draft.json": brief_digest,
        "request.txt": request_digest,
    }
    assert models.load_run(run_dir) == updated


def test_transaction_artifacts_are_write_once(tmp_path: Path) -> None:
    manifest, run_dir = models.create_run(tmp_path, now=NOW)
    brief = _brief(manifest.run_id)
    with models.RunTransaction(run_dir) as txn:
        txn.write_bytes_artifact("request.txt", b"one")
        txn.write_json_artifact("brief.draft.json", brief.to_json())
        txn.commit()
    with models.RunTransaction(run_dir) as txn:
        with pytest.raises(models.ContractError):
            txn.write_bytes_artifact("request.txt", b"two")


def test_transaction_generation_compare_and_swap_fails_closed(tmp_path: Path) -> None:
    """A manifest that moved underneath the transaction refuses to commit."""
    manifest, run_dir = models.create_run(tmp_path, now=NOW)
    with models.RunTransaction(run_dir) as txn:
        moved = models.Manifest(
            run_id=manifest.run_id,
            created_at=manifest.created_at,
            state=manifest.state,
            generation=2,
            artifact_sha256=(),
            approvals=(),
        )
        (run_dir / models.MANIFEST_NAME).write_bytes(models.canonical_json_bytes(moved.to_json()))
        with pytest.raises(models.RunStateError):
            txn.commit()


def test_uncommitted_transaction_leaves_previous_manifest_governing(tmp_path: Path) -> None:
    """Manifest-last: staged artifacts without a commit do not change the run."""
    manifest, run_dir = models.create_run(tmp_path, now=NOW)
    with models.RunTransaction(run_dir) as txn:
        txn.write_bytes_artifact("request.txt", b"staged but never committed")
    # The staged bytes are durably on disk (written first)...
    assert (run_dir / "request.txt").read_bytes() == b"staged but never committed"
    # ...but the previous manifest still governs the run.
    assert models.load_run(run_dir) == manifest


def test_corrupt_artifact_blocks_resume(tmp_path: Path) -> None:
    """A referenced artifact whose bytes changed makes the run blocked, not guessed."""
    manifest, run_dir = models.create_run(tmp_path, now=NOW)
    brief = _brief(manifest.run_id)
    with models.RunTransaction(run_dir) as txn:
        txn.write_bytes_artifact("request.txt", b"example request text")
        txn.write_json_artifact("brief.draft.json", brief.to_json())
        txn.commit()
    (run_dir / "brief.draft.json").write_bytes(b"{}")
    with pytest.raises(models.RunCorruptError):
        models.load_run(run_dir)


def test_missing_referenced_artifact_blocks_resume(tmp_path: Path) -> None:
    manifest, run_dir = models.create_run(tmp_path, now=NOW)
    with models.RunTransaction(run_dir) as txn:
        txn.write_bytes_artifact("request.txt", b"example request text")
        txn.commit()
    (run_dir / "request.txt").unlink()
    with pytest.raises(models.RunCorruptError):
        models.load_run(run_dir)


def test_unreadable_manifest_blocks_resume(tmp_path: Path) -> None:
    _, run_dir = models.create_run(tmp_path, now=NOW)
    (run_dir / models.MANIFEST_NAME).write_bytes(b"not json")
    with pytest.raises(models.RunCorruptError):
        models.load_run(run_dir)


def test_approval_flow_seals_digests_through_the_transaction(tmp_path: Path) -> None:
    """PREPARED -> BRIEF_APPROVED with an approval sealed against recorded digests."""
    manifest, run_dir = models.create_run(tmp_path, now=NOW)
    brief = _brief(manifest.run_id)
    with models.RunTransaction(run_dir) as txn:
        txn.write_bytes_artifact("request.txt", b"example request text")
        txn.write_json_artifact("brief.draft.json", brief.to_json())
        txn.commit()
    with models.RunTransaction(run_dir) as txn:
        approval = models.build_approval(txn.manifest, models.APPROVAL_STAGE_BRIEF, NOW)
        updated = txn.commit(state=models.STATE_BRIEF_APPROVED, approvals=(approval,))
    assert updated.state == models.STATE_BRIEF_APPROVED
    assert updated.approvals[0].approved_sha256 == brief.digest()
    assert models.load_run(run_dir) == updated


def test_artifact_path_containment(tmp_path: Path) -> None:
    """Writes outside the fixed run layout are containment violations."""
    models.validate_artifact_path("facts.snapshot.json")
    models.validate_artifact_path("preview/slides/slide-1.png")
    for bad in (
        "secrets.txt",
        "../evil.json",
        "preview/slides/../evil.png",
        "preview/slides/nested/evil.png",
        "manifest.json",
    ):
        with pytest.raises(models.ContractError):
            models.validate_artifact_path(bad)
    _, run_dir = models.create_run(tmp_path, now=NOW)
    with models.RunTransaction(run_dir) as txn:
        with pytest.raises(models.ContractError):
            txn.write_bytes_artifact("../evil.json", b"x")


# --- fact snapshot gates -------------------------------------------------------------


def _snapshot(run_id: str, **overrides: object) -> models.FactSnapshot:
    base: dict[str, object] = {
        "run_id": run_id,
        "captured_at": NOW,
        "as_of_date": date(2026, 8, 31),
        "audience": models.AUDIENCE_INTERNAL,
        "source_snapshots": (
            models.SourceSnapshot(
                source_alias=models.SOURCE_ALIAS_BUDGET_TIMESERIES,
                captured_at=NOW,
                contract_version=1,
                locator="spreadsheet:example/'Budget Timeseries'!A1:J9",
                source_revision=None,
                content_sha256=HEX64,
                captured_ranges=("'Budget Timeseries'!A1:J9",),
            ),
        ),
        "facts": (_available_fact(),),
        "graphic_datasets": (),
        "conflicts": (),
        "missing_required": (),
        "missing_optional": (),
    }
    base.update(overrides)
    return models.FactSnapshot(**base)  # type: ignore[arg-type]


def test_fact_snapshot_round_trip_rejects_unknown_root() -> None:
    run_id = models.new_run_id(NOW)
    snapshot = _snapshot(run_id)
    assert models.FactSnapshot.from_json(snapshot.to_json()) == snapshot
    with pytest.raises(models.ContractError):
        models.FactSnapshot.from_json({**snapshot.to_json(), "extra": 1})


def test_required_gaps_and_blocking_conflicts_fail_closed() -> None:
    run_id = models.new_run_id(NOW)
    snapshot = _snapshot(
        run_id,
        missing_required=(
            models.MissingFact(
                fact_id="position.bank_balance@fy-2026",
                module_keys=("position",),
                absence_reason="no source supplies this fact",
                blocking=True,
            ),
        ),
    )
    with pytest.raises(models.RunStateError):
        snapshot.require_advanceable()
    _snapshot(run_id).require_advanceable()


def test_changed_source_marks_the_run_stale() -> None:
    """Facts are never silently refreshed: a moved source hash blocks the run."""
    snapshot = _snapshot(models.new_run_id(NOW))
    snapshot.verify_source_hashes({models.SOURCE_ALIAS_BUDGET_TIMESERIES: HEX64})
    with pytest.raises(models.RunStateError):
        snapshot.verify_source_hashes({models.SOURCE_ALIAS_BUDGET_TIMESERIES: "b" * 64})


def test_vanished_source_also_marks_the_run_stale() -> None:
    """A captured source missing from the fresh capture fails closed, not open."""
    snapshot = _snapshot(models.new_run_id(NOW))
    with pytest.raises(models.RunStateError, match="missing from the fresh capture"):
        snapshot.verify_source_hashes({})
