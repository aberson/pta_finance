"""Fixed catalog exercised through real authenticated HTTP and emulator storage."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("google.cloud.firestore_v1")
pytest.importorskip("cryptography")

from fastapi.testclient import TestClient  # noqa: E402
from google.cloud.firestore_v1.types import Document, Value  # noqa: E402
from test_shared_workflow_helpers import require_emulator, setup  # noqa: E402
from test_shared_workflow_http import assert_error  # noqa: E402
from test_shared_workflow_store import actor, body  # noqa: E402

from pta_finance.shared_workflow import models  # noqa: E402
from pta_finance.shared_workflow.app import CSP, create_app  # noqa: E402
from pta_finance.shared_workflow.auth import IAPVerifier  # noqa: E402
from pta_finance.shared_workflow.catalog import load_catalog  # noqa: E402
from pta_finance.shared_workflow.config import Config  # noqa: E402
from pta_finance.shared_workflow.models import (  # noqa: E402
    Deadline,
    WorkflowError,
    load_source,
    wire,
)
from pta_finance.shared_workflow.store import Store  # noqa: E402
from scripts.shared_workflow_smoke import ORIGIN, Signer, emulator_client  # noqa: E402

require_emulator()
pytestmark = pytest.mark.integration
HEADERS = {"Origin": ORIGIN, "Content-Type": "application/json", "X-PTA-CSRF": "1"}
Queue = tuple[TestClient, Config, Signer, dict[str, Store]]


def queue_stores(config: Config) -> dict[str, Store]:
    client = emulator_client(os.environ["FIRESTORE_EMULATOR_HOST"])
    return {
        request_id: Store(config, source, client) for request_id, source in load_catalog().items()
    }


def app_for(config: Config, verifier: IAPVerifier, stores: dict[str, Store]) -> Any:
    return create_app(config, verifier, stores[load_source()["request_id"]], catalog_stores=stores)


@pytest.fixture
def queue() -> Iterator[Queue]:
    config, signer, verifier, _ = setup("queue")
    stores = queue_stores(config)
    with TestClient(app_for(config, verifier, stores), base_url=ORIGIN) as client:
        client.headers.update(HEADERS)
        client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config)
        yield client, config, signer, stores


def by_ref(stores: dict[str, Store], ref: str) -> Store:
    return next(store for store in stores.values() if store.source["display"]["ref"] == ref)


def endpoint(store: Store) -> str:
    return "/api/requests/" + store.source["request_id"]


def test_exact_catalog_and_list_projection(queue: Queue, monkeypatch: pytest.MonkeyPatch) -> None:
    client, config, signer, stores = queue
    expected = {
        "NEW-01": ("Classroom supply reimbursement", "184.50"),
        "DEMO-02": ("Family reading night materials", "96.00"),
        "DEMO-03": ("Garden club seed kits", "142.75"),
        "DEMO-04": ("Volunteer appreciation refreshments", "78.20"),
        "DEMO-05": ("Field day activity supplies", "225.00"),
        "DEMO-06": ("Art display mounting materials", "63.40"),
    }
    source_bytes = Path("pta_finance/shared_workflow/example-request.json").read_bytes()
    # Pin the accepted source with Git checkout line-ending normalization only.
    assert (
        hashlib.sha256(source_bytes.replace(b"\r\n", b"\n")).hexdigest()
        == "ccbc5ac6302166a1b4f7ddfd17c56bc0c1ace6d50f1e57d359a54a7a5024e740"
    )
    me = client.get("/api/me").json()
    assert set(me) == {"mode", "actor", "request_id"}
    assert me["mode"] == "queue" and me["request_id"] is None
    assert me["actor"] == actor(config).public()

    def assert_queue_page(role: str, label: str) -> None:
        page = client.get("/")
        assert page.status_code == 200
        assert 'data-request-cap="6"' in page.text
        assert 'data-event-cap="100"' in page.text
        assert "6 fixed fictional requests" in page.text
        assert f'data-role="{role}"' in page.text
        assert f"Signed in as <strong>{label}</strong>" in page.text
        assert page.headers["Cache-Control"] == "no-store"
        assert page.headers["Content-Security-Policy"] == CSP
        assert page.text.count("<script") == 1
        assert '<script src="/static/queue.js" defer></script>' in page.text
        attribute = re.search(r'data-workflow="([^"]+)"', page.text)
        assert attribute is not None
        encoded = attribute.group(1)
        decoded = html.unescape(encoded)
        assert decoded != encoded, "The JSON must be escaped inside its HTML attribute"
        workflow = json.loads(decoded)
        assert set(workflow) == {"states", "owners", "actions"}
        assert {state: value["owner"] for state, value in workflow["states"].items()} == (
            models.STATE_OWNERS
        )
        assert set(workflow["owners"]) == {*models.ROLES, "none"}
        assert set(workflow["actions"]) == {"comment", *models.TRANSITIONS}
        for user in config.users:
            assert user.subject not in page.text
            assert user.email not in page.text

    assert_queue_page("reviewer", "Reviewer")
    # The context-managed client's portal runs the ASGI app and this callback.
    assert client.portal is not None
    event_loop_thread = client.portal.call(threading.get_ident)
    deadlines: list[Deadline] = []
    worker_threads: list[int] = []
    original_read = Store.read

    def observed_read(store: Store, deadline: Deadline) -> dict[str, Any]:
        deadlines.append(deadline)
        worker_threads.append(threading.get_ident())
        return original_read(store, deadline)

    monkeypatch.setattr(Store, "read", observed_read)
    response = client.get("/api/requests")
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"requests", "request_count", "request_cap"}
    assert data["request_count"] == data["request_cap"] == len(data["requests"]) == 6
    assert len(deadlines) == 6 and all(item is deadlines[0] for item in deadlines)
    assert len(set(worker_threads)) == 1 and worker_threads[0] != event_loop_thread, (
        "Storage reads must run outside the ASGI event loop"
    )
    for row in data["requests"]:
        assert set(row) == {
            "request_id",
            "display",
            "state",
            "next_owner_role",
            "version",
            "updated_at",
            "latest_event",
        }
        assert set(row["display"]) == {"ref", "title", "submitted_on", "total"}
        assert (row["display"]["title"], row["display"]["total"]) == expected[row["display"]["ref"]]
        assert row["state"] == "AWAITING_REVIEW" and row["next_owner_role"] == "reviewer"
        assert row["version"] == 0 and row["latest_event"] is None
        assert row["updated_at"].endswith("Z") and len(row["updated_at"].split(".")[1]) == 7
    assert [row["updated_at"] for row in data["requests"]] == sorted(
        [row["updated_at"] for row in data["requests"]], reverse=True
    )
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, "processor")
    assert_queue_page("processor", "Processor")
    assert client.get("/api/requests").json() == data
    store = by_ref(stores, "DEMO-02")
    saved = client.post(endpoint(store) + "/comments", json=body(text="Private-body canary"))
    receipt = saved.json()["receipt"]
    listed = client.get("/api/requests").json()["requests"]
    assert listed[0]["request_id"] == store.source["request_id"]
    assert listed[0]["version"] == 1
    assert listed[0]["latest_event"] == {
        key: receipt[key] for key in ("action", "actor_label", "actor_role", "created_at")
    }
    assert all(
        token not in json.dumps(listed)
        for token in (
            "Private-body canary",
            "example-processor",
            "processor@example.org",
            "source_sha256",
        )
    )


def test_tied_server_timestamps_use_request_id_order(queue: Queue) -> None:
    client, _, _, stores = queue
    stamp = datetime(2026, 8, 15, tzinfo=UTC)
    for store in stores.values():
        store.client.update_document(
            request={
                "document": Document(
                    name=store.path, fields={"updated_at": Value(timestamp_value=stamp)}
                ),
                "update_mask": {"field_paths": ["updated_at"]},
            },
            retry=None,
            timeout=5,
        )
    rows = client.get("/api/requests").json()["requests"]
    assert [row["request_id"] for row in rows] == sorted(stores)


def test_not_approved_is_terminal_and_comments_keep_owner(queue: Queue) -> None:
    client, config, signer, stores = queue
    selected = by_ref(stores, "DEMO-04")
    operation = {**body(), "decision": "not_approve"}
    response = client.post(endpoint(selected) + "/decision", json=operation)
    assert response.status_code == 200
    assert response.json()["receipt"]["result_state"] == "NOT_APPROVED"
    assert client.post(endpoint(selected) + "/decision", json=operation).json() == response.json()
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, "processor")
    assert_error(
        client.post(endpoint(selected) + "/complete", json=body(1, "")),
        409,
        "INVALID_TRANSITION",
    )
    assert client.post(endpoint(selected) + "/comments", json=body(1)).status_code == 200
    current = client.get(endpoint(selected)).json()
    assert current["request"]["state"] == "NOT_APPROVED"
    assert current["request"]["next_owner_role"] is None
    assert [event["action"] for event in current["events"]] == ["not_approve", "comment"]


def test_partial_startup_retries_without_reset_and_source_mismatch_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, signer, verifier, _ = setup("queue")
    stores = queue_stores(config)
    original = stores[load_source()["request_id"]]
    original.seed()
    receipt = original.comment(actor(config), body(), Deadline.after())
    before = original.read(Deadline.after())
    interrupted = by_ref(stores, "DEMO-04")

    def unavailable() -> None:
        raise WorkflowError("STORE_UNAVAILABLE")

    with monkeypatch.context() as patch:
        patch.setattr(interrupted, "seed", unavailable)
        with pytest.raises(WorkflowError, match="STORE_UNAVAILABLE"):
            app_for(config, verifier, stores)
    assert original.read(Deadline.after()) == before
    assert by_ref(stores, "DEMO-03").read(Deadline.after())["events"] == []
    assert interrupted._read(interrupted.path, Deadline.after()) is None
    app = app_for(config, verifier, stores)
    with TestClient(app, base_url=ORIGIN) as client:
        client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config)
        assert len(client.get("/api/requests").json()["requests"]) == 6
        assert client.get(endpoint(original)).json()["events"] == [wire(receipt)]
    interrupted.client.update_document(
        request={
            "document": Document(
                name=interrupted.path, fields={"source_sha256": Value(string_value="0" * 64)}
            ),
            "update_mask": {"field_paths": ["source_sha256"]},
        },
        retry=None,
        timeout=5,
    )
    with pytest.raises(WorkflowError, match="SOURCE_MISMATCH"):
        app_for(config, verifier, queue_stores(config))
    assert original.read(Deadline.after()) == before


def test_original_handoff_upgrade_replays_and_downgrade_preserve_all_histories() -> None:
    config, signer, verifier, _ = setup("handoff")
    source = load_source()
    old = Store(config, source, emulator_client(os.environ["FIRESTORE_EMULATOR_HOST"]))
    old.seed()
    operations = [body(), body(1), body(2, "")]
    receipts = [
        old.comment(actor(config), operations[0], Deadline.after()),
        old.mutate(actor(config), operations[1], "approve", Deadline.after()),
        old.mutate(actor(config, "processor"), operations[2], "complete", Deadline.after()),
    ]
    before = wire(old.read(Deadline.after()))
    queue_config = replace(config, mode="queue")
    stores = queue_stores(queue_config)
    with TestClient(
        app_for(queue_config, IAPVerifier(queue_config, verifier.keys), stores), base_url=ORIGIN
    ) as client:
        client.headers.update(HEADERS)
        client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(queue_config)
        assert client.get(endpoint(old)).json() == before
        for operation, action, receipt in zip(
            operations, ("comments", "decision", "complete"), receipts, strict=True
        ):
            client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(
                queue_config, "processor" if action == "complete" else "reviewer"
            )
            submitted = {**operation, **({"decision": "approve"} if action == "decision" else {})}
            assert client.post(endpoint(old) + "/" + action, json=submitted).json() == {
                "receipt": wire(receipt)
            }
        assert client.get(endpoint(old)).json() == before
        untouched = by_ref(stores, "DEMO-06")
        assert client.post(endpoint(untouched) + "/comments", json=body()).status_code == 200
    create_app(config, verifier, old)
    assert wire(old.read(Deadline.after())) == before
    extra = untouched.read(Deadline.after())
    app_for(queue_config, IAPVerifier(queue_config, verifier.keys), queue_stores(queue_config))
    assert untouched.read(Deadline.after()) == extra and load_source() == source


def test_independent_operations_roles_versions_and_histories(queue: Queue) -> None:
    client, config, signer, stores = queue
    first, second = by_ref(stores, "DEMO-02"), by_ref(stores, "DEMO-03")
    same_id = body()
    first_receipt = client.post(endpoint(first) + "/comments", json=same_id).json()["receipt"]
    second_receipt = client.post(endpoint(second) + "/comments", json=same_id).json()["receipt"]
    assert first_receipt["operation_id"] == second_receipt["operation_id"]
    assert first_receipt["payload_sha256"] != second_receipt["payload_sha256"]
    untouched = {
        key: client.get(endpoint(store)).json()
        for key, store in stores.items()
        if store not in (first, second)
    }
    decision = {**body(1), "decision": "approve"}
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, "processor")
    assert_error(client.post(endpoint(first) + "/decision", json=decision), 403, "FORBIDDEN")
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config)
    assert client.post(endpoint(first) + "/decision", json=decision).status_code == 200
    assert_error(client.post(endpoint(first) + "/complete", json=body(2, "")), 403, "FORBIDDEN")
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, "processor")
    assert client.post(endpoint(first) + "/complete", json=body(2, "")).status_code == 200
    current = client.get(endpoint(first)).json()
    assert current["request"]["state"] == "COMPLETED" and current["request"]["version"] == 3
    assert current["events"][0] == first_receipt
    assert client.get(endpoint(second)).json()["events"] == [second_receipt]
    assert all(
        client.get(endpoint(stores[key])).json() == value for key, value in untouched.items()
    )


@pytest.mark.parametrize(
    "problem", ["missing", "extra", "source", "namespace", "original", "database", "old-mode"]
)
def test_dependencies_fail_before_any_seed(problem: str, monkeypatch: pytest.MonkeyPatch) -> None:
    config, _, verifier, _ = setup("queue")
    stores = queue_stores(config)
    original = stores[load_source()["request_id"]]
    selected = by_ref(stores, "DEMO-02")
    if problem == "missing":
        del stores[selected.source["request_id"]]
    elif problem == "extra":
        stores["0" * 64] = selected
    elif problem == "source":
        selected.source = {**selected.source, "source_sha256": "0" * 64}
    elif problem == "namespace":
        selected.config = replace(config, namespace="proof_" + str(uuid4()))
    elif problem == "original":
        original = Store(config, load_source(), original.client)
    elif problem == "database":
        selected.database += "-other"
    else:
        config = replace(config, mode="handoff")

    def forbidden_seed(store: Store) -> None:
        raise AssertionError("Dependencies must be checked before writes")

    monkeypatch.setattr(Store, "seed", forbidden_seed)
    with pytest.raises(WorkflowError, match="CONFIG_INVALID"):
        create_app(config, verifier, original, catalog_stores=stores)


def test_catalog_validation_precedes_all_seeding_and_map_is_copied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pta_finance.shared_workflow.catalog as inventory

    config, signer, verifier, _ = setup("queue")
    stores = queue_stores(config)
    original = stores[load_source()["request_id"]]
    with pytest.raises(WorkflowError, match="CONFIG_INVALID"):
        create_app(config, verifier, original)
    with monkeypatch.context() as patch:
        patch.setattr(inventory, "ADDITIONAL_SOURCES", inventory.ADDITIONAL_SOURCES[:-1])
        with pytest.raises(WorkflowError, match="FIXTURE_INVALID"):
            app_for(config, verifier, stores)
        assert original._read(original.path, Deadline.after()) is None
    app = app_for(config, verifier, stores)
    stores.clear()
    with TestClient(app, base_url=ORIGIN) as client:
        client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config)
        assert len(client.get("/api/requests").json()["requests"]) == 6


@pytest.mark.parametrize("role", ["reviewer", "processor"])
def test_disabled_identity_blocks_all_queue_routes_and_exact_replay(
    queue: Queue, role: str
) -> None:
    client, config, signer, stores = queue
    selected = by_ref(stores, "DEMO-02")
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, role)
    operation = body()
    assert client.post(endpoint(selected) + "/comments", json=operation).status_code == 200
    disabled = replace(
        config,
        users=tuple(
            replace(user, enabled=False) if user.role == role else user for user in config.users
        ),
    )
    new_stores = queue_stores(disabled)
    _, _, _, transport = setup("queue")
    from pta_finance.shared_workflow.auth import KeyCache

    transport.keys = {signer.kid: signer.public_pem()}
    with TestClient(
        app_for(disabled, IAPVerifier(disabled, KeyCache(transport)), new_stores), base_url=ORIGIN
    ) as denied:
        denied.headers.update(HEADERS)
        denied.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, role)
        for path in (
            "/",
            "/api/me",
            "/api/requests",
            "/requests/" + selected.source["request_id"],
            endpoint(selected),
            "/static/queue.js",
        ):
            assert_error(denied.get(path), 403, "FORBIDDEN")
        assert_error(
            denied.post(endpoint(selected) + "/comments", json=operation), 403, "FORBIDDEN"
        )
    assert len(selected.read(Deadline.after())["events"]) == 1


def test_unknown_unauthenticated_and_invalid_boundaries(queue: Queue) -> None:
    client, config, signer, stores = queue
    selected = by_ref(stores, "DEMO-02")
    for request_id in ("unknown", "0" * 64, selected.source["request_id"].upper()):
        for path in ("/requests/" + request_id, "/api/requests/" + request_id):
            assert_error(client.get(path), 404, "NOT_FOUND")
        for action in ("comments", "decision", "complete"):
            assert_error(
                client.post("/api/requests/" + request_id + "/" + action, json=body()),
                404,
                "NOT_FOUND",
            )
    for path in (
        "/",
        "/api/me",
        "/api/requests",
        "/requests/" + selected.source["request_id"],
        endpoint(selected),
        "/static/queue.js",
    ):
        assert_error(client.get(path + "?filter=example"), 400, "INVALID_INPUT")
        assertion = client.headers.pop("X-Goog-IAP-JWT-Assertion")
        assert_error(client.get(path), 401, "UNAUTHENTICATED")
        client.headers["X-Goog-IAP-JWT-Assertion"] = assertion
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, sub="example-unadmitted")
    assert_error(client.get("/api/requests"), 403, "FORBIDDEN")
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config)
    assert_error(
        client.post(endpoint(selected) + "/comments", json={**body(), "request_id": "0" * 64}),
        400,
        "INVALID_INPUT",
    )
    for headers in (
        {"Origin": "https://other.example.org"},
        {"X-PTA-CSRF": ""},
        {"Content-Type": "text/plain"},
    ):
        assert_error(
            client.post(endpoint(selected) + "/comments", headers=headers, json=body()),
            403,
            "FORBIDDEN",
        )
    assert selected.read(Deadline.after())["events"] == []


def test_one_inconsistent_history_fails_whole_list(queue: Queue) -> None:
    client, _, _, stores = queue
    selected = by_ref(stores, "DEMO-06")
    operation = body()
    assert client.post(endpoint(selected) + "/comments", json=operation).status_code == 200
    selected.client.delete_document(
        request={"name": selected.path + "/events/" + operation["operation_id"]},
        retry=None,
        timeout=5,
    )
    assert_error(client.get("/api/requests"), 503, "STORE_INCONSISTENT")
    assert client.get(endpoint(by_ref(stores, "DEMO-02"))).json()["request"]["version"] == 0


def test_deadline_failure_returns_no_partial_list(
    queue: Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, stores = queue
    # Replace only the deadline module's clock binding; JWT wall time and the
    # event loop's timeout clock stay real. Two real reads consume the actual
    # incoming 20-second budget before the scan can enter any later store.
    clock = SimpleNamespace(now=time.monotonic())
    monkeypatch.setattr(models, "time", SimpleNamespace(monotonic=lambda: clock.now))
    attempted: list[str] = []
    completed: list[str] = []
    deadlines: list[Deadline] = []
    budgets: list[float] = []
    original_read = Store.read

    def consuming_read(store: Store, deadline: Deadline) -> dict[str, Any]:
        attempted.append(store.source["request_id"])
        deadlines.append(deadline)
        budgets.append(deadline.remaining(20))
        result = original_read(store, deadline)
        completed.append(result["request"]["request_id"])
        clock.now += 12
        return result

    monkeypatch.setattr(Store, "read", consuming_read)
    assert_error(client.get("/api/requests"), 503, "TEMPORARILY_UNAVAILABLE")
    assert len(completed) == 2 and len(set(completed)) == 2
    assert attempted == completed, "The exhausted scan must not enter another store"
    assert set(stores) - set(attempted), "Later catalog stores must remain unread"
    assert deadlines[0] is deadlines[1]
    assert budgets == [20, 8], "Each successful read must consume the same incoming budget"


def test_event_cap_and_exact_replay_are_independent(queue: Queue) -> None:
    client, _, _, stores = queue
    selected, other = by_ref(stores, "DEMO-02"), by_ref(stores, "DEMO-03")
    first = body()
    receipt = client.post(endpoint(selected) + "/comments", json=first).json()
    for version in range(1, 100):
        assert client.post(endpoint(selected) + "/comments", json=body(version)).status_code == 200
    assert_error(
        client.post(endpoint(selected) + "/comments", json=body(100)), 409, "EVENT_CAP_REACHED"
    )
    assert client.post(endpoint(selected) + "/comments", json=first).json() == receipt
    assert client.post(endpoint(other) + "/comments", json=first).status_code == 200
    rows = client.get("/api/requests").json()["requests"]
    assert sorted(row["version"] for row in rows) == [0, 0, 0, 0, 1, 100]


def test_opposing_decisions_resolve_to_one_history(queue: Queue) -> None:
    client, _, _, stores = queue
    selected = by_ref(stores, "DEMO-04")
    operations = [{**body(), "decision": decision} for decision in ("approve", "not_approve")]

    def submit(operation: dict[str, Any]) -> Any:
        return client.post(endpoint(selected) + "/decision", json=operation)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, operations))
    assert time.monotonic() - started < 25
    assert sum(response.status_code == 200 for response in responses) <= 1
    for response in responses:
        assert response.status_code == 200 or response.json()["error"]["code"] in {
            "STALE_VERSION",
            "RETRY_CONFLICT",
            "TEMPORARILY_UNAVAILABLE",
        }
    # Release emulator contention, then replay these exact operations with bounded calls.
    until = time.monotonic() + 35
    while time.monotonic() < until:
        responses = [submit(operation) for operation in operations]
        if sorted(response.status_code for response in responses) == [200, 409]:
            break
        time.sleep(0.2)
    else:
        pytest.fail("Exact operations did not resolve bounded emulator contention")
    current = client.get(endpoint(selected)).json()
    assert current["request"]["version"] == len(current["events"]) == 1
    assert current["events"][0] in [response.json().get("receipt") for response in responses]
    assert_error(
        next(response for response in responses if response.status_code == 409),
        409,
        "STALE_VERSION",
    )


@pytest.mark.parametrize("mode", ["identity", "comments", "handoff"])
def test_old_modes_expose_no_queue(mode: str) -> None:
    config, signer, verifier, _ = setup(mode)
    store = (
        None
        if mode == "identity"
        else Store(config, load_source(), emulator_client(os.environ["FIRESTORE_EMULATOR_HOST"]))
    )
    with TestClient(create_app(config, verifier, store), base_url=ORIGIN) as client:
        client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config)
        for path in (
            "/api/requests",
            "/requests/" + load_source()["request_id"],
            "/static/queue.js",
            "/static/queue.css",
        ):
            assert_error(client.get(path), 404, "NOT_FOUND")
