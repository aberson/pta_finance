"""Small Firestore RPC transaction lane with explicit deadlines and no implicit retries."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from google.api_core.exceptions import Aborted, GoogleAPICallError, NotFound
from google.cloud.firestore_v1.services.firestore import FirestoreClient
from google.cloud.firestore_v1.types import (
    ArrayValue,
    Document,
    DocumentTransform,
    MapValue,
    Precondition,
    StructuredQuery,
    Value,
    Write,
)

from .config import Config
from .models import (
    EVENT_CAP,
    EVENT_FIELDS,
    INITIAL_OWNER,
    INITIAL_STATE,
    REQUEST_FIELDS,
    ROLES,
    SCHEMA_VERSION,
    STATE_OWNERS,
    TRANSITIONS,
    Actor,
    Deadline,
    WorkflowError,
    mutation_input,
    payload_hash,
    transition,
)

T = TypeVar("T")


def _value(value: Any) -> Value:
    if value is None:
        return Value(null_value=0)
    if type(value) is bool:
        return Value(boolean_value=value)
    if type(value) is int:
        return Value(integer_value=value)
    if isinstance(value, str):
        return Value(string_value=value)
    if isinstance(value, datetime):
        return Value(timestamp_value=value)
    if isinstance(value, dict):
        return Value(map_value=MapValue(fields={key: _value(item) for key, item in value.items()}))
    if isinstance(value, list):
        return Value(array_value=ArrayValue(values=[_value(item) for item in value]))
    raise WorkflowError("STORE_INCONSISTENT")


def _python(value: Value) -> Any:
    kind = Value.pb(value).WhichOneof("value_type")
    if kind == "null_value":
        return None
    if kind == "map_value":
        return {key: _python(item) for key, item in value.map_value.fields.items()}
    if kind == "array_value":
        return [_python(item) for item in value.array_value.values]
    if kind in ("string_value", "integer_value", "boolean_value", "timestamp_value"):
        return getattr(value, kind)
    raise WorkflowError("STORE_INCONSISTENT")


class Store:
    def __init__(self, config: Config, source: dict[str, Any], client: FirestoreClient) -> None:
        if config.database is None or config.namespace is None:
            raise WorkflowError("CONFIG_INVALID")
        self.client = client
        self.config = config
        self.source = source
        self.database = f"projects/{config.project_id}/databases/{config.database}"
        self.path = (
            f"{self.database}/documents/workflow_proofs/{config.namespace}"
            f"/requests/{source['request_id']}"
        )

    def _read(
        self, path: str, deadline: Deadline, transaction: bytes = b""
    ) -> dict[str, Any] | None:
        try:
            document = self.client.get_document(
                request={"name": path, **({"transaction": transaction} if transaction else {})},
                retry=None,
                timeout=deadline.remaining(),
            )
            return {key: _python(value) for key, value in document.fields.items()}
        except NotFound:
            return None

    def _write(
        self, path: str, data: dict[str, Any], timestamps: tuple[str, ...], *, create: bool = False
    ) -> Write:
        return Write(
            update=Document(name=path, fields={key: _value(value) for key, value in data.items()}),
            current_document=Precondition(exists=not create),
            update_transforms=[
                DocumentTransform.FieldTransform(field_path=key, set_to_server_value="REQUEST_TIME")
                for key in timestamps
            ],
        )

    def _transaction(
        self, callback: Callable[[bytes], tuple[T, list[Write]]], deadline: Deadline
    ) -> T:
        retry_id = b""
        for attempt in range(3):
            transaction = b""
            committed = False
            try:
                response = self.client.begin_transaction(
                    request={
                        "database": self.database,
                        "options": {
                            "read_write": {
                                "retry_transaction": retry_id,
                            }
                        },
                    },
                    retry=None,
                    timeout=deadline.remaining(),
                )
                transaction = response.transaction
                result, writes = callback(transaction)
                self.client.commit(
                    request={
                        "database": self.database,
                        "transaction": transaction,
                        "writes": writes,
                    },
                    retry=None,
                    timeout=deadline.remaining(),
                )
                committed = True
                return result
            except Aborted as exc:
                retry_id = transaction
                if attempt == 2:
                    raise WorkflowError("RETRY_CONFLICT") from exc
            except GoogleAPICallError as exc:
                raise WorkflowError("TEMPORARILY_UNAVAILABLE") from exc
            finally:
                if transaction and not committed:
                    try:
                        self.client.rollback(
                            request={"database": self.database, "transaction": transaction},
                            retry=None,
                            timeout=deadline.remaining(),
                        )
                    except (GoogleAPICallError, WorkflowError):
                        pass
        raise WorkflowError("RETRY_CONFLICT")

    def _request(self, request: dict[str, Any] | None) -> dict[str, Any]:
        if (
            request is None
            or type(request.get("schema_version")) is not int
            or request["schema_version"] != SCHEMA_VERSION
            or any(request.get(key) != value for key, value in self.source.items())
        ):
            raise WorkflowError("SOURCE_MISMATCH")
        if (
            set(request) != REQUEST_FIELDS
            or type(request["version"]) is not int
            or not 0 <= request["version"] <= EVENT_CAP
            or not isinstance(request["state"], str)
            or request["state"] not in STATE_OWNERS
            or request["next_owner_role"] != STATE_OWNERS[request["state"]]
            or not isinstance(request["created_at"], datetime)
            or not isinstance(request["updated_at"], datetime)
        ):
            raise WorkflowError("STORE_INCONSISTENT")
        return request

    def _event(self, event: dict[str, Any] | None) -> dict[str, Any]:
        if (
            event is None
            or set(event) != EVENT_FIELDS
            or event["request_id"] != self.source["request_id"]
            or event["source_sha256"] != self.source["source_sha256"]
            or not isinstance(event["action"], str)
            or not isinstance(event["previous_state"], str)
            or type(event["result_version"]) is not int
            or not 1 <= event["result_version"] <= EVENT_CAP
            or not isinstance(event["created_at"], datetime)
            or event["actor_role"] not in ROLES
            or event["actor_label"] != event["actor_role"].capitalize()
            or not isinstance(event["actor_sub"], str)
            or not event["actor_sub"]
        ):
            raise WorkflowError("STORE_INCONSISTENT")
        data = {key: event[key] for key in ("operation_id", "expected_version", "body")}
        try:
            mutation_input(data, event["action"])
            state, owner = transition(event["previous_state"], event["actor_role"], event["action"])
        except WorkflowError as exc:
            raise WorkflowError("STORE_INCONSISTENT") from exc
        if (
            event["result_state"] != state
            or event["next_owner_role"] != owner
            or event["result_version"] != data["expected_version"] + 1
            or event["payload_sha256"] != payload_hash(self.source, data, event["action"])
        ):
            raise WorkflowError("STORE_INCONSISTENT")
        return event

    def seed(self) -> None:
        deadline = Deadline.after()

        def create(transaction: bytes) -> tuple[None, list[Write]]:
            existing = self._read(self.path, deadline, transaction)
            if existing is not None:
                self._request(existing)
                return None, []
            initial = {
                **self.source,
                "state": INITIAL_STATE,
                "version": 0,
                "next_owner_role": INITIAL_OWNER,
            }
            return None, [
                self._write(self.path, initial, ("created_at", "updated_at"), create=True)
            ]

        try:
            self._transaction(create, deadline)
        except WorkflowError as exc:
            if exc.code in ("TEMPORARILY_UNAVAILABLE", "RETRY_CONFLICT"):
                raise WorkflowError("STORE_UNAVAILABLE") from exc
            raise

    def read(self, deadline: Deadline) -> dict[str, Any]:
        try:
            request = self._request(self._read(self.path, deadline))
            query = StructuredQuery(
                from_=[StructuredQuery.CollectionSelector(collection_id="events")],
                where=StructuredQuery.Filter(
                    field_filter=StructuredQuery.FieldFilter(
                        field=StructuredQuery.FieldReference(field_path="result_version"),
                        op="LESS_THAN_OR_EQUAL",
                        value=Value(integer_value=request["version"]),
                    )
                ),
                order_by=[
                    StructuredQuery.Order(
                        field=StructuredQuery.FieldReference(field_path="result_version"),
                        direction="ASCENDING",
                    )
                ],
                limit=EVENT_CAP,
            )
            rows = self.client.run_query(
                request={"parent": self.path, "structured_query": query},
                retry=None,
                timeout=deadline.remaining(),
            )
            events = []
            for row in rows:
                deadline.remaining()
                if row.document.name:
                    event = self._event(
                        {key: _python(value) for key, value in row.document.fields.items()}
                    )
                    if row.document.name != f"{self.path}/events/{event['operation_id']}":
                        raise WorkflowError("STORE_INCONSISTENT")
                    events.append(event)
            if [event["result_version"] for event in events] != list(
                range(1, request["version"] + 1)
            ):
                raise WorkflowError("STORE_INCONSISTENT")
            state = INITIAL_STATE
            for event in events:
                if event["previous_state"] != state:
                    raise WorkflowError("STORE_INCONSISTENT")
                state = event["result_state"]
            if request["state"] != state:
                raise WorkflowError("STORE_INCONSISTENT")
            return {"request": request, "events": events, "event_cap": EVENT_CAP}
        except GoogleAPICallError as exc:
            raise WorkflowError("TEMPORARILY_UNAVAILABLE") from exc

    def comment(self, actor: Actor, data: dict[str, Any], deadline: Deadline) -> dict[str, Any]:
        return self.mutate(actor, data, "comment", deadline)

    def mutate(
        self, actor: Actor, data: dict[str, Any], action: str, deadline: Deadline
    ) -> dict[str, Any]:
        # Defense at the database boundary, including previously committed-operation retries.
        if not any(
            user.enabled
            and user.subject == actor.subject
            and user.email == actor.email
            and user.role == actor.role
            for user in self.config.users
        ):
            raise WorkflowError("FORBIDDEN")
        if self.config.mode == "identity" or (
            action != "comment" and self.config.mode != "handoff"
        ):
            raise WorkflowError("NOT_FOUND")
        mutation_input(data, action)
        # Role authorization precedes lookup even for a previously committed operation.
        if action != "comment":
            if actor.role != TRANSITIONS[action][0]:
                raise WorkflowError("FORBIDDEN")
        digest = payload_hash(self.source, data, action)
        event_path = f"{self.path}/events/{data['operation_id']}"

        def mutate(transaction: bytes) -> tuple[None, list[Write]]:
            request = self._request(self._read(self.path, deadline, transaction))
            existing = self._read(event_path, deadline, transaction)
            if existing is not None:
                if (
                    existing.get("actor_sub") != actor.subject
                    or existing.get("payload_sha256") != digest
                ):
                    raise WorkflowError("OPERATION_CONFLICT")
                self._event(existing)
                return None, []
            if request["version"] >= EVENT_CAP:
                raise WorkflowError("EVENT_CAP_REACHED")
            if data["expected_version"] != request["version"]:
                raise WorkflowError("STALE_VERSION")
            state, owner = transition(request["state"], actor.role, action)
            event = {
                **data,
                "payload_sha256": digest,
                "actor_sub": actor.subject,
                "actor_label": actor.label,
                "actor_role": actor.role,
                "request_id": self.source["request_id"],
                "source_sha256": self.source["source_sha256"],
                "action": action,
                "previous_state": request["state"],
                "result_state": state,
                "result_version": request["version"] + 1,
                "next_owner_role": owner,
            }
            updated = {
                **request,
                "version": event["result_version"],
                "state": state,
                "next_owner_role": owner,
            }
            del updated["updated_at"]
            return None, [
                self._write(self.path, updated, ("updated_at",)),
                self._write(event_path, event, ("created_at",), create=True),
            ]

        self._transaction(mutate, deadline)
        try:
            # An ambiguous read-after-commit remains retryable with the original operation ID.
            receipt = self._read(event_path, deadline)
            if receipt is None:
                raise WorkflowError("TEMPORARILY_UNAVAILABLE")
            return self._event(receipt)
        except GoogleAPICallError as exc:
            raise WorkflowError("TEMPORARILY_UNAVAILABLE") from exc
