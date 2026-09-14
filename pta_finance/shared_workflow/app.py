"""The same authenticated HTTP/page boundary is used in deployment and local proofs."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from jinja2 import Environment, StrictUndefined
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint

from . import models
from .auth import IAPVerifier
from .catalog import REQUEST_CAP, load_catalog
from .config import Config
from .models import (
    BODY_LIMIT,
    EVENT_CAP,
    Deadline,
    WorkflowError,
    comment_input,
    decision_input,
    load_source,
    mutation_input,
    strict_json,
    wire,
)
from .store import Store

LOGGER = logging.getLogger("pta_finance.shared_workflow")
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _queue_workflow() -> dict[str, Any]:
    # These overrides control presentation only. Accepted keys and owner relations
    # always come from the workflow authority, including entries without overrides.
    state_styles = {
        "AWAITING_REVIEW": ("Needs review", "awaiting"),
        "APPROVED": ("Approved", "approved"),
        "COMPLETED": ("Completed", "completed"),
        "NOT_APPROVED": ("Not approved", "rejected"),
    }
    order = {state: index for index, state in enumerate(state_styles)}
    states = {}
    for state in sorted(models.STATE_OWNERS, key=lambda state: order.get(state, len(order))):
        label, css = state_styles.get(state, (state.replace("_", " ").capitalize(), ""))
        states[state] = {
            "owner": models.STATE_OWNERS[state],
            "label": label,
            "css": css,
            "card_label": "Ready for completion" if state == "APPROVED" else label,
            "option_label": "Approved · ready for completion" if state == "APPROVED" else label,
        }
    action_labels = {
        "comment": "added a comment",
        "approve": "approved",
        "not_approve": "did not approve",
        "complete": "completed the handoff",
    }
    return {
        "states": states,
        "owners": {role: role.capitalize() for role in models.ROLES}
        | {"none": "No further action"},
        "actions": {
            action: action_labels.get(action, action.replace("_", " "))
            for action in ("comment", *models.TRANSITIONS)
        },
    }


def create_app(
    config: Config,
    verifier: IAPVerifier,
    store: Store | None,
    *,
    catalog_stores: Mapping[str, Store] | None = None,
) -> FastAPI:
    """Explicit dependencies; only the strict __main__ constructs production dependencies."""
    source = load_source()
    admitted: dict[str, Store] = {}
    if config.mode == "queue":
        catalog = load_catalog()
        if catalog_stores is None or store is None:
            raise WorkflowError("CONFIG_INVALID")
        admitted = dict(catalog_stores)
        if (
            set(admitted) != set(catalog)
            or admitted.get(source["request_id"]) is not store
            or any(
                not isinstance(target, Store)
                or target.source != catalog[request_id]
                or target.config != config
                or target.database != f"projects/{config.project_id}/databases/{config.database}"
                or target.path
                != (
                    f"projects/{config.project_id}/databases/{config.database}/documents/"
                    f"workflow_proofs/{config.namespace}/requests/{request_id}"
                )
                for request_id, target in admitted.items()
            )
        ):
            raise WorkflowError("CONFIG_INVALID")
    elif catalog_stores is not None:
        raise WorkflowError("CONFIG_INVALID")
    if config.mode == "identity" and store is not None:
        raise WorkflowError("CONFIG_INVALID")
    if config.mode != "identity" and store is None:
        raise WorkflowError("STORE_UNAVAILABLE")
    if config.mode != "queue" and store is not None:
        admitted[source["request_id"]] = store
    # Every dependency and packaged source is admitted before any create-if-absent write.
    for target in admitted.values():
        target.seed()
    resources = files("pta_finance.shared_workflow")
    template = Environment(autoescape=True, undefined=StrictUndefined).from_string(
        resources.joinpath("templates/request.html.j2").read_text(encoding="utf-8")
    )
    queue_template = (
        Environment(autoescape=True, undefined=StrictUndefined).from_string(
            resources.joinpath("templates/queue.html.j2").read_text(encoding="utf-8")
        )
        if config.mode == "queue"
        else None
    )
    static = {
        name: resources.joinpath(f"static/{name}").read_text(encoding="utf-8")
        for name in (
            ("request.js", "request.css", "queue.js", "queue.css")
            if config.mode == "queue"
            else ("request.js", "request.css")
        )
    }
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def error_response(error: WorkflowError, correlation: str) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "correlation_id": correlation,
                }
            },
            status_code=error.status,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code in (404, 405) else "INVALID_INPUT"
        return error_response(WorkflowError(code), request.state.correlation)

    @app.middleware("http")
    async def boundary(request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.monotonic()
        request.state.deadline = Deadline.after()
        request.state.correlation = str(uuid4())
        code = "OK"
        route = "/healthz" if request.url.path == "/healthz" else "/protected"
        try:
            async with asyncio.timeout(request.state.deadline.remaining(20)):
                if request.url.path != "/healthz":
                    if (
                        config.mode != "identity"
                        and request.headers.get("host") != urlsplit(config.origin or "").netloc
                    ):
                        raise WorkflowError("FORBIDDEN")
                    assertions = request.headers.getlist("x-goog-iap-jwt-assertion")
                    if len(assertions) != 1:
                        raise WorkflowError("UNAUTHENTICATED")
                    request.state.actor = await run_in_threadpool(
                        verifier.verify, assertions[0], request.state.deadline
                    )
                    if request.query_params:
                        raise WorkflowError("INVALID_INPUT")
                response = await call_next(request)
                if response.status_code >= 400:
                    code = f"HTTP_{response.status_code}"
        except WorkflowError as exc:
            code = exc.code
            response = error_response(exc, request.state.correlation)
        except TimeoutError:
            code = "TEMPORARILY_UNAVAILABLE"
            response = error_response(WorkflowError(code), request.state.correlation)
        except Exception:
            code = "INTERNAL_ERROR"
            response = error_response(WorkflowError(code), request.state.correlation)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": CSP,
                "Referrer-Policy": "no-referrer",
            }
        )
        LOGGER.info(
            "route=%s code=%s duration_ms=%d correlation_id=%s",
            route,
            code,
            int((time.monotonic() - start) * 1000),
            request.state.correlation,
        )
        return response

    def data_store(request_id: str) -> Store:
        if request_id not in admitted:
            raise WorkflowError("NOT_FOUND")
        return admitted[request_id]

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def page(request: Request) -> HTMLResponse:
        return HTMLResponse(
            (queue_template or template).render(
                actor=request.state.actor,
                mode=config.mode,
                request_id=source["request_id"] if store is not None else "",
                event_cap=EVENT_CAP,
                request_cap=REQUEST_CAP,
                origin=config.origin or "/",
                workflow=_queue_workflow() if queue_template is not None else None,
            )
        )

    @app.get("/requests/{request_id}")
    async def detail_page(request_id: str, request: Request) -> HTMLResponse:
        if config.mode != "queue":
            raise WorkflowError("NOT_FOUND")
        data_store(request_id)
        return HTMLResponse(
            template.render(
                actor=request.state.actor,
                mode=config.mode,
                request_id=request_id,
                origin=config.origin,
            )
        )

    @app.get("/static/{name}")
    async def asset(name: str) -> Response:
        if name not in static:
            raise WorkflowError("NOT_FOUND")
        return Response(
            static[name], media_type="text/javascript" if name.endswith(".js") else "text/css"
        )

    @app.get("/api/me")
    async def me(request: Request) -> dict[str, Any]:
        return {
            "mode": config.mode,
            "actor": request.state.actor.public(),
            "request_id": (
                source["request_id"] if store is not None and config.mode != "queue" else None
            ),
        }

    def summaries(deadline: Deadline) -> dict[str, Any]:
        rows = []
        for target in admitted.values():
            deadline.remaining()
            result = target.read(deadline)
            request = result["request"]
            latest = result["events"][-1] if result["events"] else None
            rows.append(
                {
                    **{
                        key: request[key]
                        for key in (
                            "request_id",
                            "state",
                            "next_owner_role",
                            "version",
                            "updated_at",
                        )
                    },
                    "display": {
                        key: request["display"][key]
                        for key in ("ref", "title", "submitted_on", "total")
                    },
                    "latest_event": (
                        {
                            key: latest[key]
                            for key in ("action", "actor_label", "actor_role", "created_at")
                        }
                        if latest
                        else None
                    ),
                }
            )
        rows.sort(key=lambda row: row["request_id"])
        rows.sort(key=lambda row: row["updated_at"], reverse=True)
        return {"requests": rows, "request_count": len(rows), "request_cap": REQUEST_CAP}

    @app.get("/api/requests")
    async def listing(request: Request) -> JSONResponse:
        if config.mode != "queue":
            raise WorkflowError("NOT_FOUND")
        result = await run_in_threadpool(summaries, request.state.deadline)
        return JSONResponse(wire(result))

    @app.get("/api/requests/{request_id}")
    async def read(request_id: str, request: Request) -> JSONResponse:
        result = await run_in_threadpool(data_store(request_id).read, request.state.deadline)
        return JSONResponse(wire(result))

    async def body(request: Request) -> Any:
        if (
            request.headers.getlist("origin") != [config.origin]
            or request.headers.getlist("content-type") != ["application/json"]
            or request.headers.getlist("x-pta-csrf") != ["1"]
            or any(value != "same-origin" for value in request.headers.getlist("sec-fetch-site"))
        ):
            raise WorkflowError("FORBIDDEN")
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isdecimal():
                raise WorkflowError("INVALID_INPUT")
            if len(length) > 5 or int(length) > BODY_LIMIT:
                raise WorkflowError("BODY_TOO_LARGE")
        chunks = bytearray()
        async for chunk in request.stream():
            chunks.extend(chunk)
            if len(chunks) > BODY_LIMIT:
                raise WorkflowError("BODY_TOO_LARGE")
        return strict_json(bytes(chunks))

    @app.post("/api/requests/{request_id}/comments")
    async def comment(request_id: str, request: Request) -> JSONResponse:
        target = data_store(request_id)
        data = comment_input(await body(request))
        receipt = await run_in_threadpool(
            target.comment, request.state.actor, data, request.state.deadline
        )
        return JSONResponse({"receipt": wire(receipt)})

    if config.mode in ("handoff", "queue"):

        @app.post("/api/requests/{request_id}/decision")
        async def decision(request_id: str, request: Request) -> JSONResponse:
            target = data_store(request_id)
            if request.state.actor.role != "reviewer":
                raise WorkflowError("FORBIDDEN")
            action, data = decision_input(await body(request))
            receipt = await run_in_threadpool(
                target.mutate, request.state.actor, data, action, request.state.deadline
            )
            return JSONResponse({"receipt": wire(receipt)})

        @app.post("/api/requests/{request_id}/complete")
        async def complete(request_id: str, request: Request) -> JSONResponse:
            target = data_store(request_id)
            if request.state.actor.role != "processor":
                raise WorkflowError("FORBIDDEN")
            data = mutation_input(await body(request), "complete")
            receipt = await run_in_threadpool(
                target.mutate, request.state.actor, data, "complete", request.state.deadline
            )
            return JSONResponse({"receipt": wire(receipt)})

    return app
