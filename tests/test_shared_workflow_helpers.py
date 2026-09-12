"""Synthetic setup shared by the proof's tests; never included in its image."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("google.cloud.firestore_v1")
pytest.importorskip("cryptography")

from pta_finance.shared_workflow.auth import IAPVerifier, KeyCache  # noqa: E402
from pta_finance.shared_workflow.config import Config  # noqa: E402
from pta_finance.shared_workflow.models import load_source  # noqa: E402
from pta_finance.shared_workflow.store import Store  # noqa: E402
from scripts.shared_workflow_smoke import (  # noqa: E402
    CertificateTransport,
    Signer,
    emulator_client,
    fixture_config,
)


def setup() -> tuple[Config, Signer, IAPVerifier, CertificateTransport]:
    config = fixture_config("proof_" + str(uuid4()))
    signer = Signer()
    transport = CertificateTransport({signer.kid: signer.public_pem()})
    return config, signer, IAPVerifier(config, KeyCache(transport)), transport


def new_store() -> tuple[Config, Signer, IAPVerifier, Store]:
    config, signer, verifier, transport = setup()
    host = os.environ.get("FIRESTORE_EMULATOR_HOST")
    if not host:
        raise pytest.UsageError(
            "Web integration tests require FIRESTORE_EMULATOR_HOST at loopback."
        )
    store = Store(config, load_source(), emulator_client(host))
    store.seed()
    return config, signer, verifier, store


def require_emulator() -> None:
    if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
        raise pytest.UsageError(
            "Web integration tests require FIRESTORE_EMULATOR_HOST; refusing to skip."
        )
