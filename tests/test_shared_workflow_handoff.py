from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("google.cloud.firestore_v1")
pytest.importorskip("cryptography")

from fastapi.testclient import TestClient  # noqa: E402
from google.api_core.exceptions import ServiceUnavailable  # noqa: E402
from test_shared_workflow_helpers import new_store, require_emulator  # noqa: E402
from test_shared_workflow_http import assert_error  # noqa: E402
from test_shared_workflow_store import actor, body  # noqa: E402

from pta_finance.shared_workflow.app import create_app  # noqa: E402
from pta_finance.shared_workflow.config import Config  # noqa: E402
from pta_finance.shared_workflow.models import Deadline, WorkflowError  # noqa: E402
from pta_finance.shared_workflow.store import Store  # noqa: E402
from scripts.shared_workflow_smoke import ORIGIN, Signer, assert_shapes  # noqa: E402

require_emulator()
pytestmark = pytest.mark.integration
HEADERS = {"Origin": ORIGIN, "Content-Type": "application/json", "X-PTA-CSRF": "1"}


@pytest.fixture
def handoff() -> Iterator[tuple[TestClient, Config, Signer, Store]]:
    config, signer, verifier, store = new_store("handoff")
    with TestClient(create_app(config, verifier, store), base_url=ORIGIN) as client:
        client.headers.update(HEADERS)
        client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config)
        yield client, config, signer, store


@pytest.mark.parametrize("decision", ["approve", "not_approve"])
def test_http_lifecycle_immutable_outcomes_and_original_retry(
    handoff: tuple[TestClient, Config, Signer, Store], decision: str
) -> None:
    client, config, signer, store = handoff
    endpoint = f"/api/requests/{store.source['request_id']}"
    # Upgrade the original comments namespace, preserving the exact earlier receipt.
    legacy = Store(replace(config, mode="comments"), store.source, store.client)
    earlier = legacy.comment(actor(config), body(), Deadline.after())
    store.seed()
    before = client.get(endpoint).json()
    operation = {**body(1, "  Fictional outcome <script>example</script>\n"), "decision": decision}
    assert_error(client.post(endpoint + "/complete", json=body(1)), 403, "FORBIDDEN")
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, "processor")
    assert_error(client.post(endpoint + "/decision", json=operation), 403, "FORBIDDEN")
    assert_error(client.post(endpoint + "/complete", json=body(1)), 409, "INVALID_TRANSITION")
    assert client.get(endpoint).json() == before
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config)
    result = client.post(endpoint + "/decision", json=operation)
    assert result.status_code == 200
    receipt = result.json()["receipt"]
    canonical = {
        "request_id": store.source["request_id"],
        "source_sha256": store.source["source_sha256"],
        "action": decision,
        **{key: value for key, value in operation.items() if key != "decision"},
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    assert receipt["payload_sha256"] == digest
    assert receipt["previous_state"] == "AWAITING_REVIEW"
    assert receipt["result_state"] == ("APPROVED" if decision == "approve" else "NOT_APPROVED")
    assert receipt["next_owner_role"] == ("processor" if decision == "approve" else None)
    assert receipt["body"] == operation["body"]
    for changed in (
        {"body": "Changed"},
        {"decision": "not_approve" if decision == "approve" else "approve"},
    ):
        assert_error(
            client.post(endpoint + "/decision", json={**operation, **changed}),
            409,
            "OPERATION_CONFLICT",
        )
    assert_error(
        client.post(
            endpoint + "/comments",
            json={key: value for key, value in operation.items() if key != "decision"},
        ),
        409,
        "OPERATION_CONFLICT",
    )
    assert_error(
        client.post(endpoint + "/decision", json={**operation, "operation_id": str(uuid4())}),
        409,
        "STALE_VERSION",
    )
    assert_error(
        client.post(endpoint + "/decision", json={**body(2), "decision": "approve"}),
        409,
        "INVALID_TRANSITION",
    )
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, "processor")
    completion = body(2, "")
    complete = client.post(endpoint + "/complete", json=completion)
    if decision == "approve":
        assert complete.status_code == 200
        assert complete.json()["receipt"]["result_state"] == "COMPLETED"
        assert complete.json()["receipt"]["actor_role"] == "processor"
        assert client.post(endpoint + "/complete", json=completion).json() == complete.json()
        version = 3
    else:
        assert_error(complete, 409, "INVALID_TRANSITION")
        version = 2
    assert_error(client.post(endpoint + "/complete", json=body(version)), 409, "INVALID_TRANSITION")
    terminal = client.get(endpoint).json()["request"]["state"]
    for role in ("processor", "reviewer"):
        client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, role)
        added = client.post(endpoint + "/comments", json=body(version))
        assert added.status_code == 200
        assert added.json()["receipt"]["result_state"] == terminal
        assert added.json()["receipt"]["next_owner_role"] is None
        version += 1
    assert client.post(endpoint + "/decision", json=operation).json() == result.json()
    store.seed()
    current = client.get(endpoint).json()
    assert_shapes(current)
    assert current["request"]["version"] == version
    assert current["events"][0] == before["events"][0]
    assert store.read(Deadline.after())["events"][0] == earlier
    assert client.get("/api/me").json()["mode"] == "handoff"


@pytest.mark.parametrize("route,role", [("decision", "reviewer"), ("complete", "processor")])
@pytest.mark.parametrize(
    "fault,status,code",
    [
        ("actor_role", 400, "INVALID_INPUT"),
        ("actor_sub", 400, "INVALID_INPUT"),
        ("action", 400, "INVALID_INPUT"),
        ("bool_version", 400, "INVALID_INPUT"),
        ("unicode", 400, "INVALID_INPUT"),
        ("long_comment", 400, "INVALID_INPUT"),
        ("large_body", 413, "BODY_TOO_LARGE"),
        ("duplicate_key", 400, "INVALID_INPUT"),
        ("missing_body", 400, "INVALID_INPUT"),
        ("missing_csrf", 403, "FORBIDDEN"),
        ("origin", 403, "FORBIDDEN"),
        ("content_type", 403, "FORBIDDEN"),
        ("fetch_site", 403, "FORBIDDEN"),
        ("query", 400, "INVALID_INPUT"),
        ("unknown", 404, "NOT_FOUND"),
        ("unsigned", 401, "UNAUTHENTICATED"),
    ],
)
def test_new_routes_retain_parser_auth_and_origin_boundary(
    handoff: tuple[TestClient, Config, Signer, Store],
    route: str,
    role: str,
    fault: str,
    status: int,
    code: str,
) -> None:
    client, config, signer, store = handoff
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, role)
    endpoint = f"/api/requests/{store.source['request_id']}"
    operation = body()
    if route == "decision":
        operation["decision"] = "approve"
    headers = {}
    suffix = ""
    if fault in ("actor_role", "actor_sub", "action"):
        operation[fault] = "spoofed"
    elif fault == "bool_version":
        operation["expected_version"] = True
    elif fault == "unicode":
        operation["body"] = "\ud800"
    elif fault == "long_comment":
        operation["body"] = "x" * 2001
    elif fault == "missing_body":
        del operation["body"]
    elif fault == "missing_csrf":
        client.headers.pop("X-PTA-CSRF")
    elif fault == "origin":
        headers["Origin"] = "https://other.example.org"
    elif fault == "content_type":
        headers["Content-Type"] = "text/plain"
    elif fault == "fetch_site":
        headers["Sec-Fetch-Site"] = "cross-site"
    elif fault == "query":
        suffix = "?limit=1"
    elif fault == "unknown":
        endpoint = "/api/requests/unknown"
    elif fault == "unsigned":
        client.headers.pop("X-Goog-IAP-JWT-Assertion")
    raw = json.dumps(operation)
    if fault == "duplicate_key":
        raw = raw[:-1] + ',"body":"duplicate"}'
    elif fault == "large_body":
        raw = "x" * 8193
    assert_error(
        client.post(endpoint + "/" + route + suffix, content=raw, headers=headers), status, code
    )
    assert store.read(Deadline.after())["events"] == []


@pytest.mark.parametrize("decision", [None, True, [], "complete", "", "approve,not_approve"])
def test_decision_requires_one_known_choice(
    handoff: tuple[TestClient, Config, Signer, Store], decision: object
) -> None:
    client, _, _, store = handoff
    endpoint = f"/api/requests/{store.source['request_id']}/decision"
    assert_error(client.post(endpoint, json={**body(), "decision": decision}), 400, "INVALID_INPUT")
    assert_error(
        client.post(endpoint, json={**body(text=" \n\t"), "decision": "approve"}),
        400,
        "INVALID_INPUT",
    )
    assert store.read(Deadline.after())["events"] == []


def test_decision_lost_receipt_denied_retry_and_store_role_gates(
    handoff: tuple[TestClient, Config, Signer, Store], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, config, signer, store = handoff
    endpoint = f"/api/requests/{store.source['request_id']}/decision"
    operation = {**body(), "decision": "approve"}
    read = store._read

    def lose(path: str, deadline: Deadline, transaction: bytes = b"") -> object:
        if "/events/" in path and not transaction:
            raise ServiceUnavailable("synthetic lost receipt")
        return read(path, deadline, transaction)

    monkeypatch.setattr(store, "_read", lose)
    assert_error(client.post(endpoint, json=operation), 503, "TEMPORARILY_UNAVAILABLE")
    monkeypatch.setattr(store, "_read", read)
    receipt = client.post(endpoint, json=operation).json()["receipt"]
    assert receipt["result_version"] == 1
    disabled = replace(config, users=(replace(config.users[0], enabled=False), config.users[1]))
    with pytest.raises(WorkflowError, match="FORBIDDEN"):
        Store(disabled, store.source, store.client).mutate(
            actor(config),
            {key: value for key, value in operation.items() if key != "decision"},
            "approve",
            Deadline.after(),
        )
    for role, action in (("processor", "approve"), ("reviewer", "complete")):
        with pytest.raises(WorkflowError, match="FORBIDDEN"):
            store.mutate(actor(config, role), body(1), action, Deadline.after())
    assert store.read(Deadline.after())["events"][0]["operation_id"] == operation["operation_id"]


@pytest.mark.parametrize("identical", [False, True])
def test_http_competing_decisions_resolve_to_one_receipt(
    handoff: tuple[TestClient, Config, Signer, Store],
    identical: bool,
) -> None:
    client, _, _, store = handoff
    endpoint = f"/api/requests/{store.source['request_id']}/decision"
    operations = [{**body(), "decision": choice} for choice in ("approve", "not_approve")]
    if identical:
        operations[1] = operations[0]

    def submit(operation: dict[str, object]) -> object:
        return client.post(endpoint, json=operation)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, operations))
    if not identical:
        assert sum(response.status_code == 200 for response in responses) <= 1
    for response in responses:
        assert response.status_code == 200 or response.json()["error"]["code"] in {
            "STALE_VERSION",
            "RETRY_CONFLICT",
            "TEMPORARILY_UNAVAILABLE",
        }
    until = time.monotonic() + 35
    while time.monotonic() < until:
        resolved = [client.post(endpoint, json=operation) for operation in operations]
        if identical and all(response.status_code == 200 for response in resolved):
            assert resolved[0].json() == resolved[1].json()
            break
        if (
            not identical
            and sorted(response.status_code for response in resolved) == [200, 409]
            and any(
                response.json().get("error", {}).get("code") == "STALE_VERSION"
                for response in resolved
            )
        ):
            break
        time.sleep(0.2)
    else:
        pytest.fail("Exact competing decisions did not resolve emulator contention")
    current = client.get(endpoint.rsplit("/", 1)[0]).json()
    assert_shapes(current)
    assert current["request"]["version"] == 1
    assert current["events"] == [
        next(response.json()["receipt"] for response in resolved if response.status_code == 200)
    ]


def test_handoff_history_must_fold_to_the_observed_request_state() -> None:
    config, _, _, store = new_store("handoff")
    receipt = store.mutate(actor(config), body(), "approve", Deadline.after())
    comment = store.comment(actor(config, "processor"), body(1), Deadline.after())
    assert comment["previous_state"] == comment["result_state"] == "APPROVED"
    assert comment["next_owner_role"] == "processor"
    request = store._read(store.path, Deadline.after())
    request.update(state="COMPLETED", next_owner_role=None)
    store.client.commit(
        request={"database": store.database, "writes": [store._write(store.path, request, ())]},
        retry=None,
        timeout=5,
    )
    with pytest.raises(WorkflowError, match="STORE_INCONSISTENT"):
        store.read(Deadline.after())
    assert (
        store._read(f"{store.path}/events/{receipt['operation_id']}", Deadline.after()) == receipt
    )
