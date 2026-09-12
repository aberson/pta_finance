"""Fail-closed environment configuration; no config file or credential discovery."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from .models import ROLES, WorkflowError, is_uuid4, strict_json


@dataclass(frozen=True)
class User:
    email: str
    subject: str | None
    role: str
    enabled: bool


@dataclass(frozen=True)
class Config:
    mode: str
    project_id: str
    project_number: str
    region: str
    service_name: str
    origin: str | None
    database: str | None
    namespace: str | None
    users: tuple[User, User]
    port: int = 8080

    @property
    def audience(self) -> str:
        return (
            f"/projects/{self.project_number}/locations/{self.region}/services/{self.service_name}"
        )


def load_config(environment: Mapping[str, str] | None = None) -> Config:
    env = os.environ if environment is None else environment
    for key in env:
        if (
            "EMULATOR" in key.upper()
            or key
            in {
                "GOOGLE_APPLICATION_CREDENTIALS",
                # SDK and native gRPC handlers bypass the application's safe request logs.
                "GOOGLE_SDK_PYTHON_LOGGING_SCOPE",
                "GRPC_TRACE",
                "GRPC_VERBOSITY",
            }
            or (key.startswith("PTA_WORKFLOW_") and key != "PTA_WORKFLOW_CONFIG")
            or key.startswith(("PTA_TEST_", "FIREBASE_AUTH_"))
        ):
            raise WorkflowError("UNSAFE_RUNTIME_ENV")
    raw = env.get("PTA_WORKFLOW_CONFIG")
    if not raw:
        raise WorkflowError("CONFIG_MISSING")
    data = strict_json(raw, "CONFIG_INVALID")
    fields = {
        "schema_version",
        "mode",
        "project_id",
        "project_number",
        "region",
        "service_name",
        "origin",
        "database",
        "namespace",
        "users",
    }
    if not isinstance(data, dict) or set(data) != fields:
        raise WorkflowError("CONFIG_INVALID")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise WorkflowError("CONFIG_INVALID")
    if data["mode"] not in ("identity", "comments"):
        raise WorkflowError("CONFIG_INVALID")
    patterns = {
        "project_id": r"[a-z][a-z0-9-]{4,28}[a-z0-9]",
        "project_number": r"[0-9]{1,20}",
        "region": r"[a-z]+-[a-z]+[0-9]+",
        "service_name": r"[a-z][a-z0-9-]{0,61}[a-z0-9]",
    }
    for key, pattern in patterns.items():
        if not isinstance(data[key], str) or not re.fullmatch(pattern, data[key]):
            raise WorkflowError("CONFIG_INVALID")
    origin = data["origin"]
    if origin is not None:
        try:
            parsed = urlsplit(origin) if isinstance(origin, str) else None
            if (
                parsed is None
                or parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or origin != f"https://{parsed.netloc}"
                or parsed.netloc.endswith(":")
                or parsed.netloc != parsed.netloc.lower()
                or parsed.port is not None
                or any(char.isspace() for char in origin)
                or "\\" in origin
                or not origin.isascii()
            ):
                raise ValueError("invalid origin")
        except ValueError as exc:
            raise WorkflowError("ORIGIN_INVALID") from exc
    users = data["users"]
    if not isinstance(users, list) or len(users) != 2:
        raise WorkflowError("CONFIG_INVALID")
    roster: list[User] = []
    for value in users:
        if not isinstance(value, dict) or set(value) != {"email", "subject", "role", "enabled"}:
            raise WorkflowError("CONFIG_INVALID")
        email, subject = value["email"], value["subject"]
        if (
            not isinstance(email, str)
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip())
            or value["role"] not in ROLES
            or type(value["enabled"]) is not bool
            or (subject is not None and (not isinstance(subject, str) or not subject.strip()))
        ):
            raise WorkflowError("CONFIG_INVALID")
        roster.append(User(email.strip().casefold(), subject, value["role"], value["enabled"]))
    if {user.role for user in roster} != set(ROLES):
        raise WorkflowError("CONFIG_INVALID")
    if roster[0].email == roster[1].email or (
        roster[0].subject is not None and roster[0].subject == roster[1].subject
    ):
        raise WorkflowError("DUPLICATE_IDENTITY")
    database, namespace = data["database"], data["namespace"]
    if database is not None and (
        not isinstance(database, str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,61}[a-z0-9]", database)
    ):
        raise WorkflowError("CONFIG_INVALID")
    if namespace is not None and (
        not isinstance(namespace, str)
        or not namespace.startswith("proof_")
        or not is_uuid4(namespace[6:])
    ):
        raise WorkflowError("CONFIG_INVALID")
    if data["mode"] == "comments":
        if any(user.subject is None for user in roster):
            raise WorkflowError("IDENTITY_BINDING_REQUIRED")
        if origin is None:
            raise WorkflowError("ORIGIN_INVALID")
        if database is None or namespace is None:
            raise WorkflowError("CONFIG_INVALID")
    port = env.get("PORT", "8080")
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise WorkflowError("CONFIG_INVALID")
    return Config(
        data["mode"],
        data["project_id"],
        data["project_number"],
        data["region"],
        data["service_name"],
        origin,
        database,
        namespace,
        (roster[0], roster[1]),
        int(port),
    )
