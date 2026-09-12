from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("google.cloud.firestore_v1")
pytest.importorskip("cryptography")

from fastapi.testclient import TestClient  # noqa: E402
from httpx import Response  # noqa: E402
from test_shared_workflow_helpers import new_store, require_emulator, setup  # noqa: E402

from pta_finance.shared_workflow.app import create_app  # noqa: E402
from pta_finance.shared_workflow.auth import IAPVerifier  # noqa: E402
from pta_finance.shared_workflow.config import Config  # noqa: E402
from pta_finance.shared_workflow.store import Store  # noqa: E402
from scripts.shared_workflow_smoke import ORIGIN, Signer, assert_shapes  # noqa: E402

require_emulator()
pytestmark = pytest.mark.integration


@pytest.fixture
def http() -> Iterator[tuple[TestClient, Config, Signer, Store]]:
    config, signer, verifier, store = new_store()
    with TestClient(create_app(config, verifier, store), base_url=ORIGIN) as client:
        client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config)
        yield client, config, signer, store


def post(
    client: TestClient,
    store: Store,
    data: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    raw: str | bytes | None = None,
) -> Response:
    base = {"Origin": ORIGIN, "Content-Type": "application/json", "X-PTA-CSRF": "1"}
    if headers:
        base.update(headers)
    if data is None:
        data = {"operation_id": str(uuid4()), "expected_version": 0, "body": "Fictional comment"}
    return client.post(
        f"/api/requests/{store.source['request_id']}/comments",
        headers=base,
        content=raw if raw is not None else json.dumps(data),
    )


def assert_error(response: Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    data = response.json()
    assert set(data) == {"error"} and set(data["error"]) == {"code", "message", "correlation_id"}
    assert data["error"]["code"] == code and UUID(data["error"]["correlation_id"]).version == 4


def test_exact_http_wire_attribution_and_no_phase_b(
    http: tuple[TestClient, Config, Signer, Store], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="pta_finance.shared_workflow")
    client, config, signer, store = http
    endpoint = f"/api/requests/{store.source['request_id']}"
    assert_shapes(client.get(endpoint).json())
    response = post(client, store)
    assert response.status_code == 200 and set(response.json()) == {"receipt"}
    receipt = response.json()["receipt"]
    assert receipt["actor_sub"] == "example-reviewer" and receipt["actor_label"] == "Reviewer"
    client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, "processor")
    current = client.get(endpoint)
    assert_shapes(current.json())
    assert current.json()["events"] == [receipt]
    for suffix in ("decision", "complete"):
        assert_error(client.post(endpoint + "/" + suffix), 404, "NOT_FOUND")
    records = [record for record in caplog.records if record.name == "pta_finance.shared_workflow"]
    assert len(records) == 5
    messages = [record.getMessage() for record in records]
    assert all(
        re.fullmatch(r"route=\S+ code=\S+ duration_ms=\d+ correlation_id=[0-9a-f-]{36}", message)
        for message in messages
    )
    assert sum("route=/protected code=OK " in message for message in messages) == 3
    for canary in (
        "Fictional comment",
        "example-reviewer",
        "example-processor",
        "reviewer@example.org",
        "processor@example.org",
        client.headers["X-Goog-IAP-JWT-Assertion"],
    ):
        assert canary not in caplog.text
    assert current.headers["cache-control"] == "no-store"
    assert current.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in current.headers["content-security-policy"]


@pytest.mark.parametrize(
    "setting",
    [
        None,
        ("GOOGLE_SDK_PYTHON_LOGGING_SCOPE", "google.cloud.firestore_v1"),
        ("GRPC_TRACE", "tcp"),
        ("GRPC_VERBOSITY", "DEBUG"),
    ],
)
def test_production_startup_logging_boundary_in_fresh_process(
    setting: tuple[str, str] | None,
) -> None:
    config, _, _, _ = setup()
    runtime = asdict(config)
    runtime.pop("port")
    runtime.update(schema_version=1, origin="https://example.run.app")
    # Explicit OS-only inheritance prevents test trust, ADC, or SDK logging settings
    # leaking into this child. Its sole database constructor is anonymous loopback.
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
    }
    env["PTA_WORKFLOW_CONFIG"] = json.dumps(runtime)
    if setting is not None:
        env[setting[0]] = setting[1]
    code = """import sys
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.cloud.firestore_v1.services.firestore import FirestoreClient
from pta_finance.shared_workflow import __main__ as launcher
from pta_finance.shared_workflow.auth import IAPVerifier, KeyCache
from pta_finance.shared_workflow.config import Config, load_config
from pta_finance.shared_workflow.models import load_source
from scripts.shared_workflow_smoke import CertificateTransport, Signer, emulator_client
from uuid import uuid4

host = sys.stdin.readline().strip()
signer = Signer()

def client() -> FirestoreClient:
    print('CLIENT_CONSTRUCTED', flush=True)
    return emulator_client(host)

def verifier(config: Config) -> IAPVerifier:
    return IAPVerifier(config, KeyCache(CertificateTransport({signer.kid: signer.public_pem()})))

def serve(app: FastAPI, **options: object) -> None:
    config = load_config()
    endpoint = '/api/requests/' + load_source()['request_id']
    with TestClient(app, base_url=config.origin) as http:
        http.headers['X-Goog-IAP-JWT-Assertion'] = signer.token(config)
        response = http.post(endpoint + '/comments',
            headers={'Origin': config.origin, 'Content-Type': 'application/json',
                     'X-PTA-CSRF': '1'},
            json={'operation_id': str(uuid4()), 'expected_version': 0,
                  'body': 'Fictional SDK logging canary'})
        assert response.status_code == 200
        assert response.json()['receipt']['actor_sub'] == 'example-reviewer'
        current = http.get(endpoint).json()
        assert current['request']['version'] == 1
        assert current['events'][0]['body'] == 'Fictional SDK logging canary'
        print('REQUEST_COMPLETED', flush=True)

launcher.FirestoreClient = client
launcher.IAPVerifier = verifier
launcher.uvicorn.run = serve
raise SystemExit(launcher.main())
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=os.environ["FIRESTORE_EMULATOR_HOST"] + "\n",
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    emitted = result.stdout + result.stderr
    if setting is None:
        assert result.returncode == 0, emitted
        assert "CLIENT_CONSTRUCTED" in result.stdout and "REQUEST_COMPLETED" in result.stdout
        assert (
            len(re.findall(r"route=/protected code=OK duration_ms=\d+ correlation_id=", emitted))
            == 2
        )
    else:
        assert result.returncode == 1
        assert "UNSAFE_RUNTIME_ENV:" in result.stderr
        assert "CLIENT_CONSTRUCTED" not in emitted and "REQUEST_COMPLETED" not in emitted
    for canary in ("Fictional SDK logging canary", "example-reviewer", "reviewer@example.org"):
        assert canary not in emitted


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "null"},
        {"Origin": "https://other.example.org"},
        {"Origin": ""},
        {"X-PTA-CSRF": ""},
        {"X-PTA-CSRF": "0"},
        {"Content-Type": "text/plain"},
        {"Content-Type": "application/x-www-form-urlencoded"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_site_and_simple_mutations_denied(
    http: tuple[TestClient, Config, Signer, Store], headers: dict[str, str]
) -> None:
    client, _, _, store = http
    assert_error(post(client, store, headers=headers), 403, "FORBIDDEN")
    assert client.get(f"/api/requests/{store.source['request_id']}").json()["events"] == []


@pytest.mark.parametrize("key", ["Origin", "Content-Type", "X-PTA-CSRF"])
def test_missing_required_mutation_headers(
    http: tuple[TestClient, Config, Signer, Store], key: str
) -> None:
    client, _, _, store = http
    headers = {"Origin": ORIGIN, "Content-Type": "application/json", "X-PTA-CSRF": "1"}
    del headers[key]
    response = client.post(
        f"/api/requests/{store.source['request_id']}/comments", headers=headers, content="{}"
    )
    assert_error(response, 403, "FORBIDDEN")


@pytest.mark.parametrize(
    "key",
    [
        "actor",
        "actor_sub",
        "actor_role",
        "role",
        "source_sha256",
        "request_id",
        "action",
        "namespace",
        "decision",
    ],
)
def test_client_owned_authority_fields_are_rejected(
    http: tuple[TestClient, Config, Signer, Store], key: str
) -> None:
    client, _, _, store = http
    data = {
        "operation_id": str(uuid4()),
        "expected_version": 0,
        "body": "Fictional",
        key: "spoofed",
    }
    assert_error(post(client, store, data), 400, "INVALID_INPUT")


@pytest.mark.parametrize(
    "change",
    [
        {"expected_version": True},
        {"expected_version": -1},
        {"expected_version": 0.0},
        {"operation_id": "not-a-uuid"},
        {"operation_id": str(uuid4()).upper()},
        {"body": ""},
        {"body": " \n\t"},
        {"body": "x" * 2001},
        {"body": "\ud800"},
    ],
)
def test_strict_body_validation(
    http: tuple[TestClient, Config, Signer, Store], change: dict[str, object]
) -> None:
    client, _, _, store = http
    data = {"operation_id": str(uuid4()), "expected_version": 0, "body": "Fictional", **change}
    assert_error(post(client, store, data), 400, "INVALID_INPUT")


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        '{"operation_id":"12345678-1234-4234-8234-123456789abc",'
        '"expected_version":0,"body":"x","body":"y"}',
        '{"expected_version":NaN}',
        '{"expected_version":1e400}',
        "[]",
        "{broken",
    ],
)
def test_malformed_json_is_safe(
    http: tuple[TestClient, Config, Signer, Store], raw: str | bytes
) -> None:
    client, _, _, store = http
    assert_error(post(client, store, raw=raw), 400, "INVALID_INPUT")
    current = client.get(f"/api/requests/{store.source['request_id']}")
    assert current.status_code == 200
    assert current.json()["events"] == []
    assert current.json()["request"]["version"] == 0


def test_limits_duplicate_headers_queries_host_and_no_cors(
    http: tuple[TestClient, Config, Signer, Store],
) -> None:
    client, _, _, store = http
    endpoint = f"/api/requests/{store.source['request_id']}"
    assert_error(post(client, store, raw="x" * 8193), 413, "BODY_TOO_LARGE")
    for path in (endpoint + "?limit=1", "/api/me?namespace=other"):
        assert_error(client.get(path), 400, "INVALID_INPUT")
    assert_error(client.get(endpoint, headers={"Host": "other.example.org"}), 403, "FORBIDDEN")
    headers = [
        ("Origin", ORIGIN),
        ("Content-Type", "application/json"),
        ("X-PTA-CSRF", "1"),
        ("Sec-Fetch-Site", "same-origin"),
        ("Sec-Fetch-Site", "cross-site"),
    ]
    assert_error(
        client.post(endpoint + "/comments", headers=headers, content="{}"), 403, "FORBIDDEN"
    )
    assert "access-control-allow-origin" not in client.options(endpoint + "/comments").headers


def test_all_routes_authenticate_and_unsigned_headers_never_admit(
    http: tuple[TestClient, Config, Signer, Store],
) -> None:
    client, _, _, store = http
    client.headers.pop("X-Goog-IAP-JWT-Assertion")
    client.headers.update(
        {
            "X-Goog-Authenticated-User-Email": "reviewer@example.org",
            "X-Goog-Authenticated-User-ID": "example-reviewer",
        }
    )
    for path in (
        "/",
        "/api/me",
        "/static/request.js",
        f"/api/requests/{store.source['request_id']}",
    ):
        assert_error(client.get(path), 401, "UNAUTHENTICATED")
    assert_error(post(client, store), 401, "UNAUTHENTICATED")
    assert client.get("/healthz", headers={"Host": "platform-probe"}).json() == {"status": "ok"}


def test_identity_mode_has_no_store_and_exposes_only_this_subject() -> None:
    config, signer, verifier, _ = setup()
    config = replace(config, mode="identity", origin=None)
    verifier = IAPVerifier(config, verifier.keys)
    with TestClient(create_app(config, verifier, None), base_url=ORIGIN) as client:
        client.headers["X-Goog-IAP-JWT-Assertion"] = signer.token(config, sub="discovered-subject")
        me = client.get("/api/me").json()
        assert me["request_id"] is None and me["actor"]["subject"] == "discovered-subject"
        page = client.get("/").text
        assert "discovered-subject" in page and "processor@example.org" not in page
        assert "comment-form" not in page and "Classroom supply" not in page
        assert_error(client.get("/api/requests/anything"), 404, "NOT_FOUND")
