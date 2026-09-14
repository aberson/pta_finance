"""The fixed, entirely fictional queue inventory. No runtime catalog discovery."""

from __future__ import annotations

import hashlib
from importlib.resources import files
from typing import Any

from .models import WorkflowError, load_packaged_source, load_source

REQUEST_CAP = 6
# Filename, reference, title, exact total, item count. Original source stays separately owned.
ADDITIONAL_SOURCES = (
    ("example-request-02.json", "DEMO-02", "Family reading night materials", "96.00", 2),
    ("example-request-03.json", "DEMO-03", "Garden club seed kits", "142.75", 2),
    ("example-request-04.json", "DEMO-04", "Volunteer appreciation refreshments", "78.20", 2),
    ("example-request-05.json", "DEMO-05", "Field day activity supplies", "225.00", 2),
    ("example-request-06.json", "DEMO-06", "Art display mounting materials", "63.40", 2),
)


def load_catalog() -> dict[str, dict[str, Any]]:
    """Validate the complete inventory before the caller can seed any request."""
    try:
        resources = files("pta_finance.shared_workflow")
        for name in ("templates/queue.html.j2", "static/queue.js", "static/queue.css"):
            if not resources.joinpath(name).is_file():
                raise ValueError("missing queue resource")
        sources = [load_source()]
        for specification in ADDITIONAL_SOURCES:
            source = load_packaged_source(*specification)
            review_key = (
                "submission:v1:"
                + hashlib.sha256(f"shared-queue-v1:{specification[1]}".encode()).hexdigest()
            )
            if source["review_key"] != review_key:
                raise ValueError("unexpected fictional review key")
            sources.append(source)
        if (
            len(sources) != REQUEST_CAP
            or any(
                len({source[field] for source in sources}) != REQUEST_CAP
                for field in ("request_id", "review_key", "source_sha256")
            )
            or len({source["display"]["ref"] for source in sources}) != REQUEST_CAP
        ):
            raise ValueError("duplicate or incomplete fictional inventory")
        return {source["request_id"]: source for source in sources}
    except Exception as exc:
        raise WorkflowError("FIXTURE_INVALID") from exc
