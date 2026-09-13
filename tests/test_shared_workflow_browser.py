from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("google.cloud.firestore_v1")
pytest.importorskip("cryptography")

import httpx  # noqa: E402
from playwright.sync_api import APIResponse, Route, expect, sync_playwright  # noqa: E402
from test_shared_workflow_helpers import require_emulator, setup  # noqa: E402

from scripts.shared_workflow_smoke import ORIGIN, Server, run_smoke  # noqa: E402

require_emulator()
pytestmark = pytest.mark.integration


def test_00_installed_wheel_real_http_browser_smoke() -> None:
    run_smoke(os.environ["FIRESTORE_EMULATOR_HOST"], 60)


@pytest.mark.parametrize("stall", ["readiness", "shutdown"])
def test_stalled_restart_deadline_reaps_child_and_releases_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stall: str
) -> None:
    config, signer, _, _ = setup()
    server = Server(
        os.environ["FIRESTORE_EMULATOR_HOST"],
        config.namespace,
        {signer.kid: signer.public_pem()},
        tmp_path,
    )
    stalled = False
    with server:
        original = server.process
        assert original is not None
        with monkeypatch.context() as patch:
            if stall == "readiness":

                async def hold_readiness(*args: Any, **kwargs: Any) -> httpx.Response:
                    nonlocal stalled
                    stalled = True
                    await asyncio.sleep(10)
                    raise AssertionError("Stalled readiness was not cancelled.")

                patch.setattr(httpx.AsyncClient, "get", hold_readiness)
            else:

                def ignore_terminate() -> None:
                    nonlocal stalled
                    stalled = True

                patch.setattr(original, "terminate", ignore_terminate)

            async def restart() -> None:
                # Windows loopback refusal takes about two seconds per free-port probe.
                async with asyncio.timeout(5):
                    await server.stop_async()
                    await server.start_async()

            started = time.monotonic()
            with pytest.raises(TimeoutError):
                asyncio.run(restart())
            assert time.monotonic() - started < 6
            assert stalled
            interrupted = server.process
            assert interrupted is not None and interrupted.poll() is None
        # Restore termination before the owner's exit cleans up the cancelled operation.
    assert time.monotonic() - started < 9
    assert server.process is None
    assert original.poll() is not None and interrupted.poll() is not None
    with server:
        assert httpx.get(ORIGIN + "/healthz", timeout=2).status_code == 200


@pytest.mark.parametrize("old_response", ["success", "session_error"])
def test_superseded_initial_read_cannot_replace_saved_view(
    tmp_path: Path, old_response: str
) -> None:
    config, signer, _, _ = setup()
    with Server(
        os.environ["FIRESTORE_EMULATOR_HOST"],
        config.namespace,
        {signer.kid: signer.public_pem()},
        tmp_path,
    ):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    extra_http_headers={"X-Goog-IAP-JWT-Assertion": signer.token(config)}
                )
                page = context.new_page()
                delayed: list[tuple[Route, APIResponse]] = []

                def hold_initial_read(route: Route) -> None:
                    delayed.append((route, route.fetch()))

                page.route("**/api/requests/*", hold_initial_read, times=1)
                page.goto(ORIGIN, wait_until="domcontentloaded")
                page.locator("#reload").click()
                expect(page.locator("#save")).to_be_enabled()
                assert len(delayed) == 1
                route, initial = delayed[0]
                assert initial.json()["request"]["version"] == 0
                assert initial.json()["events"] == []
                page.locator("#comment").fill("Fictional saved before the old read")
                page.locator("#save").click()
                expect(page.locator("#feedback")).to_have_text("Comment saved.")
                expect(page.locator("#history li")).to_have_count(1)
                with page.expect_response("**/api/requests/*") as released:
                    if old_response == "success":
                        route.fulfill(response=initial)
                    else:
                        route.fulfill(
                            status=200, content_type="text/html", body="<html>Sign in</html>"
                        )
                released.value.finished()
                # Let the completed fetch and its promise continuations reach the page.
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
                expect(page.locator("#feedback")).to_have_text("Comment saved.")
                expect(page.locator("#session-refresh")).to_be_hidden()
                expect(page.locator("#history li")).to_have_count(1)
                expect(page.locator("#request-state")).to_contain_text("Version 1")
                page.locator("#comment").fill("Fictional next comment")
                with page.expect_request("**/comments") as saved:
                    page.locator("#save").click()
                assert saved.value.post_data_json["expected_version"] == 1
                expect(page.locator("#feedback")).to_have_text("Comment saved.")
                expect(page.locator("#history li")).to_have_count(2)
                current = context.request.get(route.request.url).json()
                assert current["request"]["version"] == 2 and len(current["events"]) == 2
            finally:
                browser.close()


def test_browser_uncertain_retry_stale_refresh_and_session_errors(tmp_path: Path) -> None:
    config, signer, _, _ = setup()
    with Server(
        os.environ["FIRESTORE_EMULATOR_HOST"],
        config.namespace,
        {signer.kid: signer.public_pem()},
        tmp_path,
    ):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                contexts = [
                    browser.new_context(
                        extra_http_headers={
                            "X-Goog-IAP-JWT-Assertion": signer.token(config, role),
                        }
                    )
                    for role in ("reviewer", "processor")
                ]
                a, b = [context.new_page() for context in contexts]
                for page in (a, b):
                    page.goto(ORIGIN)
                    expect(page.locator("#save")).to_be_enabled()
                me = contexts[0].request.get(ORIGIN + "/api/me").json()
                endpoint = ORIGIN + "/api/requests/" + me["request_id"]
                lost_operations = []

                def lose_response(route: Route) -> None:
                    lost_operations.append(route.request.post_data_json)
                    response = route.fetch()
                    assert response.status == 200
                    route.abort("failed")

                a.route("**/comments", lose_response, times=1)
                xss = "<script>window.PROOF_XSS=1</script> Fictional comment"
                a.locator("#comment").fill(xss)
                a.locator("#save").click()
                expect(a.locator("#retry")).to_be_visible()
                expect(a.locator("#comment")).to_have_value(xss)
                with a.expect_response("**/comments") as response:
                    a.locator("#retry").click()
                assert (
                    response.value.json()["receipt"]["operation_id"]
                    == lost_operations[0]["operation_id"]
                )
                expect(a.locator("#feedback")).to_have_text("Comment saved.")
                assert a.evaluate("window.PROOF_XSS") is None
                assert a.locator("#history script").count() == 0
                assert len(contexts[0].request.get(endpoint).json()["events"]) == 1

                b.locator("#comment").fill("Fictional stale draft")
                b.locator("#save").click()
                expect(b.locator("#feedback")).to_contain_text("STALE_VERSION")
                expect(b.locator("#comment")).to_have_value("Fictional stale draft")
                expect(b.locator("#history li")).to_have_count(1)
                b.locator("#save").click()
                expect(b.locator("#feedback")).to_have_text("Comment saved.")
                assert len(contexts[0].request.get(endpoint).json()["events"]) == 2

                # Exercise real browser CSRF headers through fetch, bypassing all form controls.
                result = a.evaluate(
                    """async endpoint => {
                    const response = await fetch(endpoint + '/comments', {method:'POST',
                        headers:{'Content-Type':'application/json'}, body:JSON.stringify({
                            operation_id:crypto.randomUUID(), expected_version:2,
                            body:'Fictional denied'})});
                    return {status:response.status, data:await response.json()};
                }""",
                    endpoint,
                )
                assert result["status"] == 403 and result["data"]["error"]["code"] == "FORBIDDEN"

                a.route(
                    "**/api/requests/*",
                    lambda route: route.fulfill(
                        status=200, content_type="text/html", body="<html>Sign in</html>"
                    ),
                    times=1,
                )
                a.locator("#reload").click()
                expect(a.locator("#session-refresh")).to_be_visible()
                expect(a.locator("#feedback")).to_contain_text("Refresh your sign-in")
                assert len(contexts[0].request.get(endpoint).json()["events"]) == 2
            finally:
                browser.close()


def test_runner_rejects_private_config_and_remote_emulators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.shared_workflow_smoke import validate_emulator

    for host in ("example.org:8787", "192.0.2.1:8787", "127.0.0.1:0"):
        with pytest.raises(ValueError):
            validate_emulator(host)
    monkeypatch.setenv("PTA_WORKFLOW_CONFIG", "synthetic private setting")
    with pytest.raises(RuntimeError, match="Remove private"):
        run_smoke("127.0.0.1:8787", 60)


def test_runner_fails_if_owned_server_port_is_busy(tmp_path: Path) -> None:
    config, signer, _, _ = setup()
    with Server(
        os.environ["FIRESTORE_EMULATOR_HOST"],
        config.namespace,
        {signer.kid: signer.public_pem()},
        tmp_path,
    ):
        second = Server(
            os.environ["FIRESTORE_EMULATOR_HOST"],
            "proof_" + str(uuid4()),
            {signer.kid: signer.public_pem()},
            tmp_path,
        )
        with pytest.raises(RuntimeError, match="Port 8788 is occupied"):
            second.start()


@pytest.mark.parametrize("kind,role", [("decision", "reviewer"), ("complete", "processor")])
def test_handoff_browser_stale_deliberate_resubmit_and_lost_terminal_receipt(
    tmp_path: Path, kind: str, role: str
) -> None:
    config, signer, _, _ = setup("handoff")
    with Server(
        os.environ["FIRESTORE_EMULATOR_HOST"],
        config.namespace,
        {signer.kid: signer.public_pem()},
        tmp_path,
        mode="handoff",
    ):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    extra_http_headers={"X-Goog-IAP-JWT-Assertion": signer.token(config, role)}
                )
                endpoint = (
                    ORIGIN
                    + "/api/requests/"
                    + context.request.get(ORIGIN + "/api/me").json()["request_id"]
                )
                headers = {"Origin": ORIGIN, "Content-Type": "application/json", "X-PTA-CSRF": "1"}
                version = 0
                if kind == "complete":
                    approved = context.request.post(
                        endpoint + "/decision",
                        headers={**headers, "X-Goog-IAP-JWT-Assertion": signer.token(config)},
                        data={
                            "operation_id": str(uuid4()),
                            "expected_version": 0,
                            "decision": "approve",
                            "body": "Fictional approval",
                        },
                    )
                    assert approved.status == 200
                    version = 1
                page = context.new_page()
                page.goto(ORIGIN)
                expect(page.locator("#save-" + kind)).to_be_enabled()
                if kind == "decision":
                    assert page.get_by_role("radio", checked=True).count() == 0
                    page.get_by_role("radio", name="Approve", exact=True).check()
                    page.get_by_role("radio", name="Not approve", exact=True).check()
                    assert page.get_by_role("radio", checked=True).count() == 1
                draft = "Fictional retained <script>window.PROOF_XSS=1</script>"
                page.locator("#" + kind + "-comment").fill(draft)
                # A later shared comment changes the expected version. No automatic decision.
                assert (
                    context.request.post(
                        endpoint + "/comments",
                        headers=headers,
                        data={
                            "operation_id": str(uuid4()),
                            "expected_version": version,
                            "body": "Fictional intervening comment",
                        },
                    ).status
                    == 200
                )
                page.locator("#save-" + kind).click()
                expect(page.locator("#feedback")).to_contain_text("STALE_VERSION")
                expect(page.locator("#" + kind + "-comment")).to_have_value(draft)
                assert context.request.get(endpoint).json()["request"]["version"] == version + 1
                # A login HTML response must retain the draft and show no successful save.
                page.route(
                    "**/" + kind,
                    lambda route: route.fulfill(
                        status=200, content_type="text/html", body="<html>Sign in</html>"
                    ),
                    times=1,
                )
                page.locator("#save-" + kind).click()
                expect(page.locator("#session-refresh")).to_be_visible()
                expect(page.locator("#feedback")).to_contain_text("Refresh your sign-in")
                expect(page.locator("#" + kind + "-comment")).to_have_value(draft)
                assert context.request.get(endpoint).json()["request"]["version"] == version + 1
                page.locator("#reload").click()
                expect(page.locator("#session-refresh")).to_be_hidden()
                lost = []

                def lose(route: Route) -> None:
                    lost.append(route.request.post_data_json)
                    assert route.fetch().status == 200
                    route.abort("failed")

                page.route("**/" + kind, lose, times=1)
                page.locator("#retry").click()
                expect(page.locator("#feedback")).to_contain_text("TEMPORARILY_UNAVAILABLE")
                current = context.request.get(endpoint).json()
                assert current["request"]["version"] == version + 2
                page.locator("#reload").click()
                expect(page.locator("#" + kind + "-form")).to_be_hidden()
                # Retry remains usable even after reload discovers the terminal state.
                with page.expect_request("**/" + kind) as retried:
                    page.locator("#retry").click()
                assert retried.value.post_data_json == lost[0]
                expect(page.locator("#feedback")).to_contain_text(
                    "Decision saved." if kind == "decision" else "Workflow handoff complete."
                )
                assert context.request.get(endpoint).json() == current
                assert page.evaluate("window.PROOF_XSS") is None
                assert page.locator("#history script").count() == 0
            finally:
                browser.close()
