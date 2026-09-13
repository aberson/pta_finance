from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Never
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("google.cloud.firestore_v1")
pytest.importorskip("cryptography")

from google.api_core.exceptions import Aborted, ServiceUnavailable  # noqa: E402
from test_shared_workflow_helpers import new_store, require_emulator  # noqa: E402

from pta_finance.shared_workflow.config import Config  # noqa: E402
from pta_finance.shared_workflow.models import Actor, Deadline, WorkflowError  # noqa: E402
from pta_finance.shared_workflow.store import Store  # noqa: E402

require_emulator()
pytestmark = pytest.mark.integration


def body(version: int = 0, text: str = "Fictional comment") -> dict[str, str | int]:
    return {"operation_id": str(uuid4()), "expected_version": version, "body": text}


def actor(config: Config, role: str = "reviewer") -> Actor:
    user = next(user for user in config.users if user.role == role)
    return Actor(user.subject, user.email, user.role)


def test_transaction_persists_exact_receipt_and_seed_never_resets() -> None:
    config, _, _, store = new_store()
    data = body(text="  Fictional exact text\n ")
    receipt = store.comment(actor(config), data, Deadline.after())
    assert receipt["body"] == data["body"] and receipt["result_version"] == 1
    assert receipt["actor_label"] == "Reviewer" and receipt["created_at"].tzinfo is not None
    assert store.comment(actor(config), data, Deadline.after()) == receipt
    reopened = Store(config, store.source, store.client)
    reopened.seed()
    assert reopened.read(Deadline.after())["events"] == [receipt]


def test_colliding_changed_and_cross_actor_retries_fail_before_stale_check() -> None:
    config, _, _, store = new_store()
    data = body()
    store.comment(actor(config), data, Deadline.after())
    for principal, changed in [
        (actor(config), {**data, "body": "Changed"}),
        (actor(config, "processor"), data),
    ]:
        with pytest.raises(WorkflowError, match="OPERATION_CONFLICT"):
            store.comment(principal, changed, Deadline.after())
    with pytest.raises(WorkflowError, match="STALE_VERSION"):
        store.comment(actor(config), body(), Deadline.after())
    disabled = replace(config, users=(replace(config.users[0], enabled=False), config.users[1]))
    with pytest.raises(WorkflowError, match="FORBIDDEN"):
        Store(disabled, store.source, store.client).comment(actor(config), data, Deadline.after())
    assert store.read(Deadline.after())["request"]["version"] == 1


@pytest.mark.parametrize("mode", ["comments", "handoff"])
def test_event_cap_rejects_new_operation_but_accepts_identical_retry(mode: str) -> None:
    config, _, _, store = new_store(mode)
    first = body()
    action = "approve" if mode == "handoff" else "comment"
    receipt = store.mutate(actor(config), first, action, Deadline.after())
    for version in range(1, 100):
        store.comment(actor(config), body(version), Deadline.after())
    with pytest.raises(WorkflowError, match="EVENT_CAP_REACHED"):
        store.comment(actor(config), body(100), Deadline.after())
    assert store.mutate(actor(config), first, action, Deadline.after()) == receipt
    if mode == "handoff":
        with pytest.raises(WorkflowError, match="EVENT_CAP_REACHED"):
            store.mutate(actor(config, "processor"), body(100), "complete", Deadline.after())
    result = store.read(Deadline.after())
    assert result["request"]["version"] == len(result["events"]) == 100


def test_concurrent_comments_commit_once_against_one_version() -> None:
    config, _, _, store = new_store()
    operations = {role: body() for role in ("reviewer", "processor")}

    def submit(role: str) -> dict[str, object] | str:
        try:
            return store.comment(actor(config, role), operations[role], Deadline.after())
        except WorkflowError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["reviewer", "processor"]))
    assert sum(isinstance(result, dict) for result in results) <= 1
    allowed = {"STALE_VERSION", "RETRY_CONFLICT", "TEMPORARILY_UNAVAILABLE"}
    assert all(result in allowed for result in results if isinstance(result, str))
    initial = store.read(Deadline.after())
    assert initial["request"]["version"] == len(initial["events"]) <= 1
    # Emulator locks differ from production. After the bounded race, replay the EXACT
    # operations until one is accepted and the other is stale, never fabricate a new ID.
    import time

    until = time.monotonic() + 35
    while time.monotonic() < until:
        retried = [submit(role) for role in ("reviewer", "processor")]
        if sum(isinstance(result, dict) for result in retried) == 1 and "STALE_VERSION" in retried:
            break
        time.sleep(0.2)
    else:
        pytest.fail("Exact retries did not resolve the real emulator contention.")
    final = store.read(Deadline.after())
    assert final["request"]["version"] == len(final["events"]) == 1
    for result in results:
        if isinstance(result, dict):
            assert result == final["events"][0]


def test_read_is_bounded_at_its_observed_version_even_when_a_later_comment_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _, _, store = new_store()
    store.comment(actor(config), body(), Deadline.after())
    read = store._read
    inserted = False

    def interleaved(
        path: str, deadline: Deadline, transaction: bytes = b""
    ) -> dict[str, object] | None:
        nonlocal inserted
        result = read(path, deadline, transaction)
        if not transaction and path == store.path and not inserted:
            inserted = True
            store.comment(actor(config), body(1), Deadline.after())
        return result

    monkeypatch.setattr(store, "_read", interleaved)
    result = store.read(Deadline.after())
    assert result["request"]["version"] == len(result["events"]) == 1
    assert store.read(Deadline.after())["request"]["version"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("source_sha256", "f" * 64),
        ("request_id", "other"),
    ],
)
def test_existing_source_or_schema_mismatch_fails_without_reset(
    field: str, value: str | int
) -> None:
    _, _, _, store = new_store()
    document = store._read(store.path, Deadline.after())
    document[field] = value
    store.client.commit(
        request={"database": store.database, "writes": [store._write(store.path, document, ())]},
        retry=None,
        timeout=5,
    )
    with pytest.raises(WorkflowError, match="SOURCE_MISMATCH"):
        store.seed()
    assert store._read(store.path, Deadline.after())[field] == value


def test_incomplete_history_fails_safely() -> None:
    config, _, _, store = new_store()
    data = body()
    store.comment(actor(config), data, Deadline.after())
    store.client.delete_document(
        request={"name": f"{store.path}/events/{data['operation_id']}"}, retry=None, timeout=5
    )
    with pytest.raises(WorkflowError, match="STORE_INCONSISTENT"):
        store.read(Deadline.after())


def test_lost_read_after_commit_can_retry_original_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    config, _, _, store = new_store()
    read = store._read
    data = body()

    def fail_receipt(
        path: str, deadline: Deadline, transaction: bytes = b""
    ) -> dict[str, object] | None:
        if "/events/" in path and not transaction:
            raise ServiceUnavailable("synthetic unavailable")
        return read(path, deadline, transaction)

    monkeypatch.setattr(store, "_read", fail_receipt)
    with pytest.raises(WorkflowError, match="TEMPORARILY_UNAVAILABLE"):
        store.comment(actor(config), data, Deadline.after())
    monkeypatch.setattr(store, "_read", read)
    assert store.comment(actor(config), data, Deadline.after())["result_version"] == 1
    assert len(store.read(Deadline.after())["events"]) == 1


def test_contention_has_three_attempts_and_calls_have_bounded_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _, _, store = new_store()
    calls = []

    def abort(*args: object, **kwargs: object) -> Never:
        calls.append(kwargs)
        assert kwargs["retry"] is None and 0 < kwargs["timeout"] <= 5
        raise Aborted("synthetic contention")

    monkeypatch.setattr(store.client, "commit", abort)
    with pytest.raises(WorkflowError, match="RETRY_CONFLICT"):
        store.comment(actor(config), body(), Deadline.after())
    assert len(calls) == 3
    with pytest.raises(WorkflowError, match="TEMPORARILY_UNAVAILABLE"):
        store.comment(actor(config), body(), Deadline.after(-1))
    assert len(calls) == 3
