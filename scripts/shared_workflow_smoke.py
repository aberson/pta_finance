"""Local-only real HTTP/emulator/browser proof. Never stage or package this test launcher.

Start the emulator separately, then run:
    uv run python scripts/shared_workflow_smoke.py --emulator-host 127.0.0.1:8787

The runner owns port 8788, two ephemeral signers/browser contexts, and its app processes.
It builds/installs a wheel in a temporary directory and serves it outside the checkout.
No ADC, private runtime JSON, Google login, or cloud service is used.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ipaddress
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import grpc
import httpx
import uvicorn
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1.services.firestore import FirestoreClient
from google.cloud.firestore_v1.services.firestore.transports.grpc import FirestoreGrpcTransport

ORIGIN = "http://127.0.0.1:8788"
PROJECT = "example-workflow-test"


def validate_emulator(host: str) -> None:
    try:
        address, port = host.rsplit(":", 1)
        if not ipaddress.ip_address(address.strip("[]")).is_loopback or not 1 <= int(port) <= 65535:
            raise ValueError("loopback required")
    except ValueError as exc:
        raise ValueError("The emulator must use a loopback IP and valid port.") from exc


def emulator_client(host: str) -> FirestoreClient:
    validate_emulator(host)
    return FirestoreClient(
        transport=FirestoreGrpcTransport(
            host=host,
            channel=grpc.insecure_channel(host),
            credentials=AnonymousCredentials(),
        )
    )


def fixture_config(namespace: str, mode: str = "comments") -> Any:
    from pta_finance.shared_workflow.config import Config, User

    return Config(
        mode,
        PROJECT,
        "123456789012",
        "us-central1",
        "pta-workflow-proof",
        ORIGIN,
        "workflow-test",
        namespace,
        (
            User("reviewer@example.org", "example-reviewer", "reviewer", True),
            User("processor@example.org", "example-processor", "processor", True),
        ),
        8788,
    )


@dataclass
class Signer:
    key: Any = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))
    kid: str = "example-key"

    def public_pem(self) -> str:
        return str(
            self.key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )

    def token(self, config: Any, role: str = "reviewer", **changes: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": "https://cloud.google.com/iap",
            "aud": config.audience,
            "sub": f"example-{role}",
            "email": f"{role}@example.org",
            "iat": now,
            "exp": now + 600,
            **changes,
        }
        return self.sign(claims)

    def sign(self, claims: dict[str, Any], **headers: Any) -> str:
        def encoded(value: Any) -> bytes:
            return base64.urlsafe_b64encode(json.dumps(value).encode("utf-8")).rstrip(b"=")

        raw = encoded({"alg": "ES256", "kid": self.kid, **headers}) + b"." + encoded(claims)
        r, s = decode_dss_signature(self.key.sign(raw, ec.ECDSA(hashes.SHA256())))
        signature = base64.urlsafe_b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).rstrip(
            b"="
        )
        return (raw + b"." + signature).decode("ascii")


class CertificateTransport:
    def __init__(self, keys: dict[str, str]) -> None:
        self.keys = keys
        self.calls = 0

    def __call__(self, url: str, **kwargs: Any) -> Any:
        from pta_finance.shared_workflow.auth import CERTS_URL, _Response

        if url != CERTS_URL:
            raise ValueError("unexpected certificate URL")
        self.calls += 1
        return _Response(json.dumps(self.keys).encode("utf-8"))


def serve() -> int:
    """Nonpackaged subprocess factory; stdin contains synthetic setup and public keys only."""
    from pta_finance.shared_workflow.app import create_app
    from pta_finance.shared_workflow.auth import IAPVerifier, KeyCache
    from pta_finance.shared_workflow.models import load_source
    from pta_finance.shared_workflow.store import Store

    setup = json.loads(sys.stdin.readline())
    config = fixture_config(setup["namespace"], setup.get("mode", "comments"))
    verifier = IAPVerifier(config, KeyCache(CertificateTransport(setup["keys"])))
    store = (
        None
        if config.mode == "identity"
        else Store(config, load_source(), emulator_client(setup["host"]))
    )
    app = create_app(config, verifier, store)
    uvicorn.run(
        app, host="127.0.0.1", port=8788, access_log=False, proxy_headers=False, log_level="error"
    )
    return 0


@dataclass
class Server:
    host: str
    namespace: str
    keys: dict[str, str]
    directory: Path
    package_path: Path | None = None
    mode: str = "comments"
    process: subprocess.Popen[str] | None = None

    def start(self, timeout: float = 25) -> None:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", 8788)) == 0:
                raise RuntimeError(
                    "Port 8788 is occupied; stop its owner before running the smoke."
                )
        launcher = Path(__file__).resolve()
        bootstrap = "import runpy,sys; "
        if self.package_path is not None:
            bootstrap += "sys.path.insert(0,sys.argv.pop(1)); "
        bootstrap += "runpy.run_path(sys.argv.pop(1),run_name='__main__')"
        command = [sys.executable, "-I", "-c", bootstrap]
        if self.package_path is not None:
            command.append(str(self.package_path))
        command.extend([str(launcher), "--serve"])
        self.process = subprocess.Popen(
            command,
            cwd=self.directory,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps(
                {
                    "host": self.host,
                    "namespace": self.namespace,
                    "keys": self.keys,
                    "mode": self.mode,
                }
            )
            + "\n"
        )
        self.process.stdin.close()
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if self.process.poll() is not None:
                assert self.process.stderr is not None
                details = self.process.stderr.read()
                raise RuntimeError("The synthetic app process failed before readiness: " + details)
            try:
                if (
                    httpx.get(
                        ORIGIN + "/healthz", timeout=min(1, max(0.01, until - time.monotonic()))
                    ).status_code
                    == 200
                ):
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("The synthetic app did not become ready.")

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
            until = time.monotonic() + 5
            while time.monotonic() < until:
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", 8788)) != 0:
                        return
                time.sleep(0.05)

    def __enter__(self) -> Server:
        try:
            self.start()
            return self
        except BaseException:
            self.stop()
            raise

    def __exit__(self, *args: Any) -> None:
        self.stop()


def assert_shapes(data: dict[str, Any]) -> None:
    """Assert the published D3 contract independently of the implementation's constants."""
    assert set(data) == {"request", "events", "event_cap"} and data["event_cap"] == 100
    request = data["request"]
    assert set(request) == {
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
    assert set(request["display"]) == {"ref", "title", "submitted_on", "total", "items"}
    assert request["display"]["total"] == "184.50"
    assert len(request["display"]["items"]) == 2
    assert all(
        set(item) == {"item_key", "description", "amount", "category"}
        for item in request["display"]["items"]
    )
    assert len(data["events"]) == request["version"]
    for event in data["events"]:
        assert set(event) == {
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
        assert event["created_at"].endswith("Z") and len(event["created_at"].split(".")[1]) == 7


def run_smoke(host: str, deadline_seconds: int) -> None:
    from playwright.async_api import async_playwright, expect

    validate_emulator(host)
    if "PTA_WORKFLOW_CONFIG" in os.environ:
        raise RuntimeError("Remove private PTA_WORKFLOW_CONFIG before running the local proof.")
    if not 1 <= deadline_seconds <= 60:
        raise ValueError("The smoke deadline must be between 1 and 60 seconds.")
    address, port = host.rsplit(":", 1)
    with socket.create_connection((address.strip("[]"), int(port)), timeout=2):
        pass
    namespace = "proof_" + str(uuid4())
    config, signer = fixture_config(namespace), Signer()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="pta-workflow-smoke-") as temporary:
        directory = Path(temporary)
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(directory / "dist")],
            cwd=root,
            check=True,
            capture_output=True,
        )
        wheel = next((directory / "dist").glob("*.whl"))
        package_path = directory / "installed"
        subprocess.run(
            ["uv", "pip", "install", "--no-deps", "--target", str(package_path), str(wheel)],
            check=True,
            capture_output=True,
        )
        with Server(
            host, namespace, {signer.kid: signer.public_pem()}, directory, package_path
        ) as server:
            started = time.monotonic()

            async def exercise() -> None:
                async with asyncio.timeout(deadline_seconds - (time.monotonic() - started)):
                    async with async_playwright() as playwright:
                        browser = await playwright.chromium.launch(headless=True)
                        try:
                            await cycle(browser)
                        finally:
                            await browser.close()

            async def cycle(browser: Any) -> None:
                contexts = [
                    await browser.new_context(
                        extra_http_headers={
                            "X-Goog-IAP-JWT-Assertion": signer.token(config, role),
                        }
                    )
                    for role in ("reviewer", "processor")
                ]
                for context in contexts:
                    context.set_default_timeout(min(deadline_seconds * 1000, 10000))
                pages = [await context.new_page() for context in contexts]
                for page in pages:
                    await page.goto(ORIGIN)
                    await expect(page.locator("#save")).to_be_enabled()
                me = await (await contexts[0].request.get(ORIGIN + "/api/me")).json()
                assert set(me) == {"mode", "actor", "request_id"}
                assert set(me["actor"]) == {"subject", "email", "label", "role"}
                endpoint = ORIGIN + "/api/requests/" + me["request_id"]
                first_body = "Fictional reviewer comment <img src=x onerror=alert(1)>"
                await pages[0].locator("#comment").fill(first_body)
                async with pages[0].expect_response("**/comments") as response:
                    await pages[0].locator("#save").click()
                received = await response.value
                assert received.status == 200 and set(await received.json()) == {"receipt"}
                await expect(pages[0].locator("#feedback")).to_have_text("Comment saved.")
                await pages[1].locator("#reload").click()
                await expect(pages[1].locator("#history")).to_contain_text(first_body)
                assert await pages[1].locator("#history img").count() == 0
                await pages[1].locator("#comment").fill("Fictional processor reply")
                await pages[1].locator("#save").click()
                await expect(pages[1].locator("#feedback")).to_have_text("Comment saved.")
                await pages[0].locator("#reload").click()
                await expect(pages[0].locator("#history")).to_contain_text(
                    "Fictional processor reply"
                )
                before = await (await contexts[0].request.get(endpoint)).json()
                assert_shapes(before)
                assert [event["actor_role"] for event in before["events"]] == [
                    "reviewer",
                    "processor",
                ]
                denied = await contexts[0].request.get(ORIGIN + "/api/requests/unknown")
                denied_data = await denied.json()
                assert denied.status == 404 and set(denied_data) == {"error"}
                assert set(denied_data["error"]) == {"code", "message", "correlation_id"}
                server.stop()
                remaining = deadline_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("Smoke deadline exhausted before app restart.")
                server.start(timeout=min(25, remaining))
                assert await (await contexts[0].request.get(endpoint)).json() == before
                await pages[1].reload()
                await expect(pages[1].locator("#history li")).to_have_count(2)
                elapsed = time.monotonic() - started
                assert elapsed <= deadline_seconds, "Smoke exceeded its deadline after readiness."
                print(
                    "SMOKE PASS: installed wheel; two signed actors; "
                    f"restart durable; {elapsed:.2f}s",
                    flush=True,
                )

            asyncio.run(exercise())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emulator-host", default="127.0.0.1:8787")
    parser.add_argument("--deadline-seconds", type=int, default=60)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.serve:
            return serve()
        run_smoke(args.emulator_host, args.deadline_seconds)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f"SMOKE FAIL: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
