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
        asyncio.run(self.start_async(timeout))

    @staticmethod
    async def _port_occupied() -> bool:
        with socket.socket() as probe:
            probe.setblocking(False)
            try:
                await asyncio.get_running_loop().sock_connect(probe, ("127.0.0.1", 8788))
            except ConnectionRefusedError:
                return False
        return True

    async def start_async(self, timeout: float = 25) -> None:
        async with asyncio.timeout(timeout):
            if await self._port_occupied():
                raise RuntimeError(
                    "Port 8788 is occupied; stop its owner before running the smoke."
                )
            self._launch()
            assert self.process is not None
            async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
                while True:
                    if self.process.poll() is not None:
                        assert self.process.stderr is not None
                        details = self.process.stderr.read()
                        raise RuntimeError(
                            "The synthetic app process failed before readiness: " + details
                        )
                    try:
                        if (await client.get(ORIGIN + "/healthz")).status_code == 200:
                            return
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)

    def _launch(self) -> None:
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

    def stop(self) -> None:
        asyncio.run(self.stop_async())

    async def _wait_for_exit(self) -> None:
        assert self.process is not None
        async with asyncio.timeout(5):
            while self.process.poll() is None:
                await asyncio.sleep(0.05)

    async def stop_async(self) -> None:
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                await self._wait_for_exit()
            except TimeoutError:
                self.process.kill()
                await self._wait_for_exit()
            # Cancellation leaves the handle owned by Server for __exit__ to reap.
            if self.process.stderr is not None:
                self.process.stderr.close()
            self.process = None
            async with asyncio.timeout(5):
                while await self._port_occupied():
                    await asyncio.sleep(0.05)

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
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=True)
                    try:
                        async with asyncio.timeout(deadline_seconds - (time.monotonic() - started)):
                            await cycle(browser)
                        async with asyncio.timeout(90):
                            await handoff_cycle(browser)
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
                    await expect(
                        page.get_by_text(
                            "This fictional request is for shared comments.", exact=False
                        )
                    ).to_be_visible()
                    await expect(
                        page.get_by_text(
                            "This fictional workflow is an administrative handoff.", exact=False
                        )
                    ).to_have_count(0)
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
                await server.stop_async()
                await server.start_async()
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

            async def handoff_cycle(browser: Any) -> None:
                # Upgrade the preserved comments namespace using the installed wheel.
                await server.stop_async()
                server.mode = "handoff"
                await server.start_async()
                contexts = [
                    await browser.new_context(
                        extra_http_headers={"X-Goog-IAP-JWT-Assertion": signer.token(config, role)}
                    )
                    for role in ("reviewer", "processor")
                ]
                reviewer, processor = [await context.new_page() for context in contexts]
                for page in (reviewer, processor):
                    await page.goto(ORIGIN)
                    await expect(page.locator("#save")).to_be_enabled()
                    await expect(page.locator("#history li")).to_have_count(2)
                    await expect(
                        page.get_by_text(
                            "This fictional workflow is an administrative handoff.", exact=False
                        )
                    ).to_be_visible()
                    await expect(
                        page.get_by_text(
                            "This fictional request is for shared comments.", exact=False
                        )
                    ).to_have_count(0)
                await expect(
                    reviewer.get_by_role("group", name="Review outcome (choose one)")
                ).to_be_visible()
                assert await reviewer.get_by_role("radio", checked=True).count() == 0
                await expect(processor.locator("#next-action")).to_contain_text(
                    "Waiting for the reviewer"
                )
                await reviewer.get_by_role("radio", name="Approve", exact=True).check()
                await reviewer.locator("#decision-comment").fill(
                    "Fictional approval <img src=x onerror=alert(1)>"
                )
                await reviewer.get_by_role("button", name="Save decision", exact=True).click()
                await expect(reviewer.locator("#feedback")).to_have_text("Decision saved.")
                await processor.locator("#reload").click()
                await expect(processor.locator("#request-state")).to_contain_text("APPROVED")
                assert await processor.locator("#history img").count() == 0
                await processor.get_by_role("button", name="Mark workflow handoff complete").click()
                await expect(processor.locator("#feedback")).to_contain_text(
                    "does not record a payment"
                )
                me = await (await contexts[0].request.get(ORIGIN + "/api/me")).json()
                assert me["mode"] == "handoff"
                endpoint = ORIGIN + "/api/requests/" + me["request_id"]
                approved = await (await contexts[0].request.get(endpoint)).json()
                assert_shapes(approved)
                assert approved["request"]["state"] == "COMPLETED"
                assert approved["request"]["next_owner_role"] is None
                assert [event["action"] for event in approved["events"]] == [
                    "comment",
                    "comment",
                    "approve",
                    "complete",
                ]
                original_namespace = server.namespace
                await server.stop_async()
                await server.start_async()
                assert await (await contexts[0].request.get(endpoint)).json() == approved
                await server.stop_async()
                server.namespace = "proof_" + str(uuid4())
                await server.start_async()
                for page in (reviewer, processor):
                    await page.reload()
                    await expect(page.locator("#history li")).to_have_count(0)
                    await expect(page.locator("#save")).to_be_enabled()
                await reviewer.get_by_role("radio", name="Not approve", exact=True).check()
                await reviewer.locator("#decision-comment").fill(
                    "Fictional reason for not approving"
                )
                await reviewer.locator("#save-decision").click()
                await expect(reviewer.locator("#feedback")).to_have_text("Decision saved.")
                await processor.locator("#reload").click()
                await expect(processor.locator("#request-state")).to_contain_text("NOT_APPROVED")
                await expect(processor.locator("#complete-form")).to_be_hidden()
                denied = await contexts[1].request.post(
                    endpoint + "/complete",
                    headers={
                        "Origin": ORIGIN,
                        "Content-Type": "application/json",
                        "X-PTA-CSRF": "1",
                    },
                    data={"operation_id": str(uuid4()), "expected_version": 1, "body": ""},
                )
                assert (
                    denied.status == 409
                    and (await denied.json())["error"]["code"] == "INVALID_TRANSITION"
                )
                rejected = await (await contexts[0].request.get(endpoint)).json()
                assert_shapes(rejected)
                assert len(rejected["events"]) == 1
                await server.stop_async()
                await server.start_async()
                assert await (await contexts[0].request.get(endpoint)).json() == rejected
                await server.stop_async()
                server.namespace = original_namespace
                await server.start_async()
                assert await (await contexts[0].request.get(endpoint)).json() == approved
                print(
                    "HANDOFF PASS: installed wheel; approve/complete and not-approve; "
                    "both namespaces durable",
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
