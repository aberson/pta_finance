from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("google.cloud.firestore_v1")
pytest.importorskip("cryptography")

from pta_finance.shared_workflow.config import load_config  # noqa: E402
from pta_finance.shared_workflow.models import WorkflowError, load_source, strict_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def runtime() -> dict[str, object]:
    return json.loads((ROOT / "deployment/shared-workflow/runtime.example.json").read_text())


def comments() -> dict[str, object]:
    value = runtime()
    value.update(
        mode="comments",
        origin="https://example.run.app",
        database="workflow-proof",
        namespace="proof_" + str(uuid4()),
    )
    for user in value["users"]:
        user["subject"] = "example-" + user["role"]
    return value


def test_identity_and_comments_config_are_explicit() -> None:
    config = load_config({"PTA_WORKFLOW_CONFIG": json.dumps(runtime())})
    assert config.mode == "identity" and config.database is None and config.origin is None
    config = load_config({"PTA_WORKFLOW_CONFIG": json.dumps(comments()), "PORT": "8788"})
    assert config.port == 8788 and config.origin == "https://example.run.app"


@pytest.mark.parametrize("mode", ["identity", "comments"])
@pytest.mark.parametrize("origin", ["https://example.org", "https://[::1]"])
def test_supported_origin_is_returned_unchanged(mode: str, origin: str) -> None:
    value = runtime() if mode == "identity" else comments()
    value["origin"] = origin
    config = load_config({"PTA_WORKFLOW_CONFIG": json.dumps(value)})
    assert config.mode == mode and config.origin == origin


@pytest.mark.parametrize("mode", ["identity", "comments"])
@pytest.mark.parametrize(
    "origin",
    [
        "https://example.org?",
        "https://example.org#",
        "https://example.org?#",
        "HTTPS://example.org",
        "https://example.org:",
        "\x00https://example.org",
        "https://example.org#x",
        "https://example.org:443",
    ],
)
def test_config_rejects_unsupported_origin_spellings(mode: str, origin: str) -> None:
    value = runtime() if mode == "identity" else comments()
    value["origin"] = origin
    with pytest.raises(WorkflowError, match="ORIGIN_INVALID"):
        load_config({"PTA_WORKFLOW_CONFIG": json.dumps(value)})


@pytest.mark.parametrize(
    "raw",
    [
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":1e400}',
        '{"x":"\\ud800"}',
        '{"\\udfff":1}',
        b"\xff",
    ],
)
def test_strict_json_rejects_ambiguous_nonfinite_and_invalid_unicode(raw: str | bytes) -> None:
    with pytest.raises(WorkflowError, match="INVALID_INPUT"):
        strict_json(raw)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"schema_version": True}, "CONFIG_INVALID"),
        ({"extra": True}, "CONFIG_INVALID"),
        ({"mode": "handoff"}, "CONFIG_INVALID"),
        ({"project_id": "../private"}, "CONFIG_INVALID"),
        ({"origin": "http://example.org"}, "ORIGIN_INVALID"),
        ({"origin": "https://example.org/"}, "ORIGIN_INVALID"),
        ({"origin": "https://user@example.org"}, "ORIGIN_INVALID"),
        ({"origin": "https://example.org?x=1"}, "ORIGIN_INVALID"),
        ({"origin": "https://example.org:invalid"}, "ORIGIN_INVALID"),
        ({"database": "(default)"}, "CONFIG_INVALID"),
        ({"namespace": "proof_not-a-uuid"}, "CONFIG_INVALID"),
    ],
)
def test_config_rejects_unsafe_shapes(change: dict[str, object], code: str) -> None:
    value = comments()
    value.update(change)
    with pytest.raises(WorkflowError, match=code):
        load_config({"PTA_WORKFLOW_CONFIG": json.dumps(value)})


@pytest.mark.parametrize(
    "key",
    [
        "FIRESTORE_EMULATOR_HOST",
        "FIREBASE_AUTH_EMULATOR_HOST",
        "PTA_WORKFLOW_TEST_KEY",
        "PTA_WORKFLOW_TRUST_HEADERS",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_SDK_PYTHON_LOGGING_SCOPE",
        "GRPC_TRACE",
        "GRPC_VERBOSITY",
    ],
)
def test_production_rejects_test_emulator_and_keyfile_environment(key: str) -> None:
    with pytest.raises(WorkflowError, match="UNSAFE_RUNTIME_ENV"):
        load_config({"PTA_WORKFLOW_CONFIG": json.dumps(runtime()), key: "synthetic"})


@pytest.mark.parametrize("port", ["0", "65536", "True", "1.1", " 8080", "-1"])
def test_port_is_strict(port: str) -> None:
    with pytest.raises(WorkflowError, match="CONFIG_INVALID"):
        load_config({"PTA_WORKFLOW_CONFIG": json.dumps(runtime()), "PORT": port})


def test_binding_and_duplicate_subject_email_guards() -> None:
    with pytest.raises(WorkflowError, match="CONFIG_MISSING"):
        load_config({})
    value = comments()
    value["users"][0]["subject"] = None
    with pytest.raises(WorkflowError, match="IDENTITY_BINDING_REQUIRED"):
        load_config({"PTA_WORKFLOW_CONFIG": json.dumps(value)})
    value["users"][0]["subject"] = value["users"][1]["subject"]
    with pytest.raises(WorkflowError, match="DUPLICATE_IDENTITY"):
        load_config({"PTA_WORKFLOW_CONFIG": json.dumps(value)})
    value = comments()
    value["users"][0]["email"] = " PROCESSOR@EXAMPLE.ORG "
    with pytest.raises(WorkflowError, match="DUPLICATE_IDENTITY"):
        load_config({"PTA_WORKFLOW_CONFIG": json.dumps(value)})


def test_fixture_projection_uses_only_source_fields() -> None:
    from pta_finance.reimbursement_report import load_bundle

    source = load_source()
    bundle = load_bundle(ROOT / "pta_finance/shared_workflow/example-request.json")
    assert bundle.settings.organization == "Example PTA"
    assert source["source_sha256"] == bundle.tickets[0].source_evidence_sha256
    assert source["display"]["total"] == "184.50"
    assert "payment" not in json.dumps(source) and "requestor" not in json.dumps(source)
