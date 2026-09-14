"""The proof's wire/storage contract; independent of private approval/payment state."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from importlib.resources import as_file, files
from typing import Any
from uuid import UUID

from pta_finance.reimbursement_report import load_bundle

SCHEMA_VERSION = 1
EVENT_CAP = 100
BODY_LIMIT = 8192
COMMENT_LIMIT = 2000
INITIAL_STATE = "AWAITING_REVIEW"
INITIAL_OWNER = "reviewer"
ROLES = ("reviewer", "processor")
STATE_OWNERS = {
    INITIAL_STATE: INITIAL_OWNER,
    "APPROVED": "processor",
    "NOT_APPROVED": None,
    "COMPLETED": None,
}
TRANSITIONS = {
    "approve": ("reviewer", INITIAL_STATE, "APPROVED"),
    "not_approve": ("reviewer", INITIAL_STATE, "NOT_APPROVED"),
    "complete": ("processor", "APPROVED", "COMPLETED"),
}
REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "request_id",
        "review_key",
        "source_sha256",
        "display",
        "state",
        "version",
        "next_owner_role",
        "created_at",
        "updated_at",
    }
)
EVENT_FIELDS = frozenset(
    {
        "operation_id",
        "payload_sha256",
        "actor_sub",
        "actor_label",
        "actor_role",
        "request_id",
        "source_sha256",
        "action",
        "body",
        "expected_version",
        "previous_state",
        "result_state",
        "result_version",
        "next_owner_role",
        "created_at",
    }
)
ERRORS: dict[str, tuple[int, str]] = {
    "CONFIG_MISSING": (500, "Runtime configuration is required."),
    "CONFIG_INVALID": (500, "Runtime configuration is invalid."),
    "IDENTITY_BINDING_REQUIRED": (500, "Both account subjects must be configured."),
    "DUPLICATE_IDENTITY": (500, "The two accounts must be distinct."),
    "ORIGIN_INVALID": (500, "A canonical HTTPS origin is required."),
    "UNSAFE_RUNTIME_ENV": (500, "Unsupported runtime trust settings are present."),
    "FIXTURE_INVALID": (500, "The packaged fictional request is unavailable or invalid."),
    "SOURCE_MISMATCH": (500, "Stored source identity does not match this proof."),
    "STORE_UNAVAILABLE": (503, "The workflow store is unavailable at startup."),
    "UNAUTHENTICATED": (401, "Sign in again to continue."),
    "FORBIDDEN": (403, "This account or request is not permitted."),
    "NOT_FOUND": (404, "The requested resource is unavailable."),
    "INVALID_INPUT": (400, "Check the submitted request."),
    "BODY_TOO_LARGE": (413, "The submitted request is too large."),
    "STALE_VERSION": (409, "Reload and deliberately submit against the latest version."),
    "INVALID_TRANSITION": (409, "This action is unavailable in the current workflow state."),
    "OPERATION_CONFLICT": (409, "This operation ID was already used. Reload before submitting."),
    "EVENT_CAP_REACHED": (409, "This demonstration has reached its event limit."),
    "RETRY_CONFLICT": (409, "The store is busy. Retry the same operation."),
    "STORE_INCONSISTENT": (503, "The stored history is incomplete. Contact the operator."),
    "TEMPORARILY_UNAVAILABLE": (503, "Verification or storage is unavailable. Retry shortly."),
    "INTERNAL_ERROR": (500, "The request could not be completed. Retry or reload."),
}


class WorkflowError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        self.status, self.message = ERRORS[code]
        super().__init__(code)


@dataclass(frozen=True)
class Deadline:
    expires: float

    @classmethod
    def after(cls, seconds: float = 20) -> Deadline:
        return cls(time.monotonic() + seconds)

    def remaining(self, maximum: float = 5) -> float:
        remaining = min(maximum, self.expires - time.monotonic())
        if remaining <= 0:
            raise WorkflowError("TEMPORARILY_UNAVAILABLE")
        return remaining


@dataclass(frozen=True)
class Actor:
    subject: str
    email: str
    role: str

    @property
    def label(self) -> str:
        return self.role.capitalize()

    def public(self) -> dict[str, str]:
        return {
            "subject": self.subject,
            "email": self.email,
            "label": self.label,
            "role": self.role,
        }


def strict_json(raw: str | bytes, code: str = "INVALID_INPUT") -> Any:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError("non-finite JSON")

    try:
        text = raw.decode("utf-8", errors="strict") if isinstance(raw, bytes) else raw
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        # JSON escape sequences can encode lone surrogates, including in object keys.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8", errors="strict")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise WorkflowError(code) from exc


def is_uuid4(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except ValueError:
        return False


def mutation_input(value: Any, action: str) -> dict[str, Any]:
    if action != "comment" and action not in TRANSITIONS:
        raise WorkflowError("INVALID_INPUT")
    if not isinstance(value, dict) or set(value) != {"operation_id", "expected_version", "body"}:
        raise WorkflowError("INVALID_INPUT")
    if not is_uuid4(value["operation_id"]):
        raise WorkflowError("INVALID_INPUT")
    if type(value["expected_version"]) is not int or value["expected_version"] < 0:
        raise WorkflowError("INVALID_INPUT")
    body = value["body"]
    if (
        not isinstance(body, str)
        or len(body) > COMMENT_LIMIT
        or (action != "complete" and not body.strip())
    ):
        raise WorkflowError("INVALID_INPUT")
    try:
        body.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise WorkflowError("INVALID_INPUT") from exc
    return value


def comment_input(value: Any) -> dict[str, Any]:
    return mutation_input(value, "comment")


def decision_input(value: Any) -> tuple[str, dict[str, Any]]:
    if (
        not isinstance(value, dict)
        or set(value) != {"operation_id", "expected_version", "body", "decision"}
        or value["decision"] not in ("approve", "not_approve")
    ):
        raise WorkflowError("INVALID_INPUT")
    action = str(value["decision"])
    return action, mutation_input(
        {key: item for key, item in value.items() if key != "decision"}, action
    )


def transition(state: str, role: str, action: str) -> tuple[str, str | None]:
    if role not in ROLES:
        raise WorkflowError("FORBIDDEN")
    if action == "comment" and state in STATE_OWNERS:
        return state, STATE_OWNERS[state]
    rule = TRANSITIONS.get(action)
    if rule is None:
        raise WorkflowError("INVALID_INPUT")
    if role != rule[0]:
        raise WorkflowError("FORBIDDEN")
    if state != rule[1]:
        raise WorkflowError("INVALID_TRANSITION")
    return rule[2], STATE_OWNERS[rule[2]]


def payload_hash(source: dict[str, Any], data: dict[str, Any], action: str = "comment") -> str:
    payload = {
        "request_id": source["request_id"],
        "source_sha256": source["source_sha256"],
        "action": action,
        **data,
    }
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_source() -> dict[str, Any]:
    """The original immutable source, including its historical projection."""
    return load_packaged_source(
        "example-request.json", "NEW-01", "Classroom supply reimbursement", "184.50", 2
    )


def load_packaged_source(
    filename: str, ref: str, title: str, expected_total: str, item_count: int
) -> dict[str, Any]:
    """Validate a package-owned bundle and construct the unchanged source wire shape."""
    try:
        resources = files("pta_finance.shared_workflow")
        for resource in ("templates/request.html.j2", "static/request.js", "static/request.css"):
            if not resources.joinpath(resource).is_file():
                raise ValueError("missing resource")
        with as_file(resources.joinpath(filename)) as path:
            report = load_bundle(path)
        if len(report.tickets) != 1:
            raise ValueError("one request required")
        ticket = report.tickets[0]
        total = sum(
            (item.source_amount or Decimal("0.00") for item in ticket.items), Decimal("0.00")
        )
        if (
            ticket.ref != ref
            or len(ticket.items) != item_count
            or total != Decimal(expected_total)
            or report.source_summary.mapped_rows != item_count
            or report.source_summary.mapped_submissions != 1
            or report.source_summary.mapped_total != total
            or report.provenance.accounted_review_keys != (ticket.review_key,)
            or report.settings.confirmed_outstanding != Decimal("0.00")
            or ticket.live.decision != "UNREVIEWED"
            or ticket.live.workflow_state != "ACTIVE"
            or ticket.live.payment_status != "NOT_PAID"
            or ticket.live.payment_date is not None
            or ticket.live.confirmations
        ):
            raise ValueError("fictional inventory mismatch")
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": hashlib.sha256(ticket.review_key.encode("utf-8")).hexdigest(),
            "review_key": ticket.review_key,
            "source_sha256": ticket.source_evidence_sha256,
            "display": {
                "ref": ticket.ref,
                "title": title,
                "submitted_on": ticket.submitted.isoformat(),
                "total": f"{total:.2f}",
                "items": [
                    {
                        "item_key": item.item_key,
                        "description": item.source_description,
                        "amount": f"{item.source_amount:.2f}",
                        "category": item.canonical_category,
                    }
                    for item in ticket.items
                ],
            },
        }
    except Exception as exc:
        raise WorkflowError("FIXTURE_INVALID") from exc


def wire(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [wire(item) for item in value]
    return value
