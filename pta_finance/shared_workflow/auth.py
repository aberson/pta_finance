"""IAP signature verification with a bounded, single-flight certificate cache."""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from google.auth.transport import Request as TransportRequest
from google.auth.transport import Response as TransportResponse
from google.auth.transport.requests import Request
from google.oauth2.id_token import verify_token

from .config import Config
from .models import Actor, Deadline, WorkflowError, strict_json

CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
ISSUER = "https://cloud.google.com/iap"
CACHE_SECONDS = 300
KEY_SECONDS = 5


@dataclass
class _Fetch:
    started: float
    done: threading.Event = field(default_factory=threading.Event)
    finished: float = 0
    keys: dict[str, str] | None = None


class _Response(TransportResponse):
    def __init__(self, data: bytes) -> None:
        self._data = data

    @property
    def data(self) -> bytes:
        return self._data

    @property
    def status(self) -> int:
        return 200

    @property
    def headers(self) -> dict[str, str]:
        return {"content-type": "application/json"}


class _Certificates(TransportRequest):
    def __init__(self, keys: dict[str, str]) -> None:
        self.keys = keys

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> _Response:
        if url != CERTS_URL or method != "GET":
            raise WorkflowError("TEMPORARILY_UNAVAILABLE")
        return _Response(json.dumps(self.keys).encode("utf-8"))


class KeyCache:
    def __init__(self, transport: Callable[..., Any] | None = None) -> None:
        self._transport = transport if transport is not None else Request()
        self._lock = threading.Lock()
        self._keys: dict[str, str] = {}
        self._fetched = 0.0
        self._pending: _Fetch | None = None

    def _fetch(self, pending: _Fetch) -> None:
        try:
            response = self._transport(
                CERTS_URL, method="GET", timeout=KEY_SECONDS, allow_redirects=False
            )
            raw = response.data
            if response.status != 200 or len(raw) > 131072:
                return
            keys = strict_json(raw)
            if (
                isinstance(keys, dict)
                and 0 < len(keys) <= 100
                and "keys" not in keys
                and all(isinstance(k, str) and isinstance(v, str) for k, v in keys.items())
            ):
                pending.keys = keys
        except Exception:
            # Transport failures never print the response or an exception containing headers.
            pass
        finally:
            pending.finished = time.monotonic()
            pending.done.set()

    def for_key(self, kid: str, deadline: Deadline) -> dict[str, str]:
        key_deadline = Deadline(min(deadline.expires, time.monotonic() + KEY_SECONDS))
        with self._lock:
            if time.monotonic() - self._fetched < CACHE_SECONDS and kid in self._keys:
                return self._keys.copy()
            # Only one daemon worker can exist, even if an upstream read ignores its timeout.
            # A late worker can neither populate the cache nor cause queued worker buildup.
            if self._pending is None:
                self._pending = _Fetch(time.monotonic())
                threading.Thread(target=self._fetch, args=(self._pending,), daemon=True).start()
            pending = self._pending
        if not pending.done.wait(key_deadline.remaining(KEY_SECONDS)):
            raise WorkflowError("TEMPORARILY_UNAVAILABLE")
        key_deadline.remaining(KEY_SECONDS)
        with self._lock:
            if self._pending is pending:
                self._pending = None
            if pending.keys is None or pending.finished - pending.started > KEY_SECONDS:
                raise WorkflowError("TEMPORARILY_UNAVAILABLE")
            if time.monotonic() - pending.finished >= CACHE_SECONDS:
                raise WorkflowError("TEMPORARILY_UNAVAILABLE")
            self._keys, self._fetched = pending.keys, pending.finished
            keys = self._keys.copy()
        if kid not in keys:
            raise WorkflowError("UNAUTHENTICATED")
        return keys


class IAPVerifier:
    def __init__(self, config: Config, keys: KeyCache | None = None) -> None:
        self.config = config
        self.keys = keys if keys is not None else KeyCache()

    def verify(self, assertion: str | None, deadline: Deadline) -> Actor:
        if not assertion or len(assertion) > 16384:
            raise WorkflowError("UNAUTHENTICATED")
        try:
            parts = assertion.split(".")
            if len(parts) != 3:
                raise ValueError("token shape")
            header = strict_json(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
            claims = strict_json(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            if (
                not isinstance(header, dict)
                or header.get("alg") != "ES256"
                or not isinstance(header.get("kid"), str)
                or not header["kid"]
                or not isinstance(claims, dict)
            ):
                raise ValueError("token header")
            for key in ("sub", "email", "aud", "iss"):
                if not isinstance(claims.get(key), str) or not claims[key].strip():
                    raise ValueError("claim type")
            if type(claims.get("iat")) is not int or type(claims.get("exp")) is not int:
                raise ValueError("claim time type")
            if not 0 < claims["exp"] - claims["iat"] <= 660:
                raise ValueError("claim lifetime")
            if claims["iss"] != ISSUER or claims["aud"] != self.config.audience:
                raise ValueError("claim authority")
        except (ValueError, UnicodeError, WorkflowError, TypeError) as exc:
            raise WorkflowError("UNAUTHENTICATED") from exc
        keys = self.keys.for_key(header["kid"], deadline)

        try:
            verified = verify_token(
                assertion,
                _Certificates(keys),
                audience=self.config.audience,
                certs_url=CERTS_URL,
                clock_skew_in_seconds=30,
            )
        except Exception as exc:
            raise WorkflowError("UNAUTHENTICATED") from exc
        deadline.remaining()
        email = verified["email"].strip().casefold()
        subject = verified["sub"]
        for user in self.config.users:
            if user.enabled and user.email == email:
                if self.config.mode == "identity" or user.subject == subject:
                    return Actor(subject, email, user.role)
        raise WorkflowError("FORBIDDEN")
