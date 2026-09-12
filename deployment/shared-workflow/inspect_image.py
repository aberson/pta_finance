"""Inspect the actual built image before Cloud Build publishes it. No cloud calls."""

from __future__ import annotations

import hashlib
import json
import os
from importlib.resources import files
from pathlib import Path


def inspect(app: Path = Path("/app")) -> None:
    forbidden = {
        "secrets",
        "mail_samples",
        "snapshots",
        ".git",
        ".claude",
        ".build-step",
        "config.toml",
        "gmail-token.json",
        "gmail-client-secret.json",
        "service-account.json",
    }
    for path in app.rglob("*"):
        if any(part.casefold() in forbidden for part in path.relative_to(app).parts):
            raise RuntimeError("Forbidden private-input path in image.")
        if path.suffix.casefold() in {".eml", ".mbox"}:
            raise RuntimeError("Mail archive in image.")
    if any(
        "EMULATOR" in key
        or key.startswith("PTA_WORKFLOW_")
        or key == "GOOGLE_APPLICATION_CREDENTIALS"
        for key in os.environ
    ):
        raise RuntimeError("Runtime configuration or test trust was baked into the image.")
    manifest = json.loads((app / "source-content-manifest.json").read_text())
    expected = {
        name.removeprefix("pta_finance/"): digest
        for name, digest in manifest.items()
        if name.startswith("pta_finance/")
    }
    package = Path(str(files("pta_finance")))
    actual = {path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()}
    if actual != set(expected):
        raise RuntimeError("Installed application inventory differs from the staged allowlist.")
    for name, digest in expected.items():
        content = (package / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise RuntimeError("Installed application content differs from the staged receipt.")
        if b'"private_key":' in content or b"PTA_PRIVATE_CANARY" in content:
            raise RuntimeError("Credential or synthetic private canary found in application files.")
    from pta_finance.shared_workflow.models import load_source

    assert load_source()["display"]["total"] == "184.50"
    assert os.getuid() != 0
    print(
        "IMAGE INSPECTION PASS: non-root; inventory/environment clean; packaged resources loaded."
    )


if __name__ == "__main__":
    inspect()
