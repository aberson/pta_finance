from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("google.cloud.firestore_v1")
pytest.importorskip("cryptography")

from playwright.sync_api import APIResponse, Route, expect, sync_playwright  # noqa: E402
from test_shared_workflow_helpers import require_emulator, setup  # noqa: E402

from scripts.shared_workflow_smoke import ORIGIN, Server, run_smoke  # noqa: E402

require_emulator()
pytestmark = pytest.mark.integration


def test_00_installed_wheel_real_http_browser_smoke() -> None:
    run_smoke(os.environ["FIRESTORE_EMULATOR_HOST"], 60)


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
