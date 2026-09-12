from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Never

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("google.cloud.firestore_v1")
pytest.importorskip("cryptography")

from google.auth.transport import Response as TransportResponse  # noqa: E402
from test_shared_workflow_helpers import setup  # noqa: E402

from pta_finance.shared_workflow.auth import IAPVerifier, KeyCache  # noqa: E402
from pta_finance.shared_workflow.models import Deadline, WorkflowError  # noqa: E402
from scripts.shared_workflow_smoke import CertificateTransport, Signer  # noqa: E402


def test_signed_subject_and_normalized_email_are_both_required() -> None:
    config, signer, verifier, transport = setup()
    actor = verifier.verify(signer.token(config, email=" Reviewer@Example.Org "), Deadline.after())
    assert actor.subject == "example-reviewer" and actor.label == "Reviewer"
    assert actor.email == "reviewer@example.org"
    verifier.verify(signer.token(config, "processor"), Deadline.after())
    assert transport.calls == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://example.org"},
        {"aud": "other"},
        {"aud": ["other"]},
        {"iat": True},
        {"exp": True},
        {"iat": "1"},
        {"exp": None},
        {"sub": None},
        {"sub": " "},
        {"email": ""},
        {"email": ["reviewer@example.org"]},
        {"iat": int(time.time()) + 60},
        {"exp": int(time.time()) + 1000},
        {"exp": 0, "iat": 0},
    ],
)
def test_rejects_malformed_claims(changes: dict[str, object]) -> None:
    config, signer, verifier, _ = setup()
    with pytest.raises(WorkflowError, match="UNAUTHENTICATED"):
        verifier.verify(signer.token(config, **changes), Deadline.after())


def test_valid_signed_lifetime_expired_beyond_clock_skew_is_rejected() -> None:
    config, signer, verifier, transport = setup()
    now = int(time.time())
    token = signer.token(config, iat=now - 600, exp=now - 60)
    with pytest.raises(WorkflowError, match="UNAUTHENTICATED") as error:
        verifier.verify(token, Deadline.after())
    assert transport.calls == 1  # Valid preflight claims reached the real signature verifier.
    assert error.value.__cause__ is not None
    assert "Token expired" in str(error.value.__cause__)
    assert verifier.verify(signer.token(config), Deadline.after()).subject == "example-reviewer"


@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "email", "iat", "exp"])
def test_missing_claims_fail(claim: str) -> None:
    config, signer, verifier, _ = setup()
    import base64

    token = signer.token(config)
    segment = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    del claims[claim]
    with pytest.raises(WorkflowError, match="UNAUTHENTICATED"):
        verifier.verify(signer.sign(claims), Deadline.after())


@pytest.mark.parametrize("token", [None, "", "unsigned", "a.b.c", "a.b.c.d"])
def test_missing_or_invalid_assertion(token: str | None) -> None:
    _, _, verifier, _ = setup()
    with pytest.raises(WorkflowError, match="UNAUTHENTICATED"):
        verifier.verify(token, Deadline.after())


def test_wrong_signature_algorithm_key_and_unsigned_token() -> None:
    import base64

    config, signer, verifier, transport = setup()
    token = signer.token(config)
    segment = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    alternatives = [
        Signer().token(config),
        signer.sign(claims, alg="RS256"),
        signer.sign(claims, alg="none"),
        signer.sign(claims, kid=""),
        signer.sign(claims, kid="unknown"),
    ]
    for token in alternatives:
        with pytest.raises(WorkflowError, match="UNAUTHENTICATED"):
            verifier.verify(token, Deadline.after())
    assert transport.calls == 2  # Initial fetch, then at most one unknown-key refresh.


@pytest.mark.parametrize(
    "changes",
    [
        {"email": "reviewer+alias@example.org"},
        {"email": "other@example.org"},
        {"sub": "example-processor"},
        {"sub": "other-subject"},
    ],
)
def test_valid_unlisted_or_mismatched_signed_identity_is_forbidden(changes: dict[str, str]) -> None:
    config, signer, verifier, _ = setup()
    with pytest.raises(WorkflowError, match="FORBIDDEN"):
        verifier.verify(signer.token(config, **changes), Deadline.after())


def test_identity_discovery_does_not_enroll_or_admit_disabled_users() -> None:
    config, signer, _, transport = setup()
    identity = replace(config, mode="identity")
    verifier = IAPVerifier(identity, KeyCache(transport))
    assert (
        verifier.verify(signer.token(config, sub="new-subject"), Deadline.after()).subject
        == "new-subject"
    )
    disabled = replace(config, users=(replace(config.users[0], enabled=False), config.users[1]))
    with pytest.raises(WorkflowError, match="FORBIDDEN"):
        IAPVerifier(disabled, KeyCache(transport)).verify(signer.token(config), Deadline.after())


def test_unknown_key_refresh_and_expiry_never_use_stale_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, signer, verifier, transport = setup()
    verifier.verify(signer.token(config), Deadline.after())
    next_signer = Signer(kid="rotated-key")
    transport.keys[next_signer.kid] = next_signer.public_pem()
    verifier.verify(next_signer.token(config), Deadline.after())
    assert transport.calls == 2
    verifier.keys._fetched -= 301

    def unavailable(*args: object, **kwargs: object) -> Never:
        raise OSError("sensitive upstream error")

    verifier.keys._transport = unavailable
    with pytest.raises(WorkflowError, match="TEMPORARILY_UNAVAILABLE"):
        verifier.verify(signer.token(config), Deadline.after())


def test_slow_key_fetch_has_one_worker_and_late_results_are_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pta_finance.shared_workflow.auth as auth

    monkeypatch.setattr(auth, "KEY_SECONDS", 0.08)
    release = threading.Event()
    started = threading.Event()
    signer = Signer()
    transport = CertificateTransport({signer.kid: signer.public_pem()})

    def slow(*args: object, **kwargs: object) -> TransportResponse:
        started.set()
        release.wait(2)
        return transport(*args, **kwargs)

    cache = KeyCache(slow)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(cache.for_key, signer.kid, Deadline.after()) for _ in range(8)]
        assert started.wait(1)
        # A caller deadline starts before the worker's budget. Keep the worker
        # blocked past its own budget, even if all callers time out a little earlier.
        assert not release.wait(auth.KEY_SECONDS * 2)
        for future in futures:
            with pytest.raises(WorkflowError, match="TEMPORARILY_UNAVAILABLE"):
                future.result(timeout=1)
        release.set()
    assert cache._pending is not None
    assert cache._pending.done.wait(1)
    with pytest.raises(WorkflowError, match="TEMPORARILY_UNAVAILABLE"):
        cache.for_key(signer.kid, Deadline.after())
    assert transport.calls == 1 and cache._keys == {}
