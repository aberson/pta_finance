"""Real Chromium navigation, refresh recovery and selected-request mutation evidence."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("google.cloud.firestore_v1")
pytest.importorskip("cryptography")

from playwright.sync_api import Route, expect, sync_playwright  # noqa: E402
from test_shared_workflow_helpers import require_emulator, setup  # noqa: E402
from test_shared_workflow_queue import HEADERS, Queue, by_ref, endpoint  # noqa: E402
from test_shared_workflow_queue import queue as queue  # noqa: E402
from test_shared_workflow_store import body  # noqa: E402

from pta_finance.shared_workflow import models  # noqa: E402
from scripts.shared_workflow_smoke import ORIGIN, Server  # noqa: E402

require_emulator()
pytestmark = pytest.mark.integration
EVIDENCE = Path(__file__).resolve().parents[1] / ".build-step" / "queue-browser"


@pytest.fixture
def browser_queue(tmp_path: Path) -> Iterator[tuple[Any, Any, Any]]:
    config, signer, _, _ = setup("queue")
    with Server(
        os.environ["FIRESTORE_EMULATOR_HOST"],
        config.namespace,
        {signer.kid: signer.public_pem()},
        tmp_path,
        mode="queue",
    ):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                contexts = [
                    browser.new_context(
                        extra_http_headers={"X-Goog-IAP-JWT-Assertion": signer.token(config, role)}
                    )
                    for role in ("reviewer", "processor")
                ]
                yield browser, contexts[0], contexts[1]
            finally:
                browser.close()


def test_authoritative_workflow_reaches_authenticated_page_and_browser(
    queue: Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _, _, stores = queue
    # Vary the shared authority in place so the real Store validates this relation.
    # Removal also proves presentation keys cannot establish an accepted inventory.
    states = models.STATE_OWNERS.copy()
    transitions = models.TRANSITIONS.copy()
    try:
        models.STATE_OWNERS["APPROVED"] = "reviewer"
        del models.STATE_OWNERS["NOT_APPROVED"]
        del models.TRANSITIONS["not_approve"]
        monkeypatch.setattr(models, "ROLES", tuple(reversed(models.ROLES)))
        selected = by_ref(stores, "DEMO-03")
        saved = client.post(
            endpoint(selected) + "/decision", json={**body(), "decision": "approve"}
        )
        assert saved.status_code == 200
        assert saved.json()["receipt"]["next_owner_role"] == "reviewer"
        current = client.get("/api/requests")
        assert current.status_code == 200
        assert current.json()["requests"][0]["next_owner_role"] == "reviewer"
        malformed = False

        def serve_app(route: Route) -> None:
            # Browser transport adapter only: actual authentication, renderer,
            # packaged assets, summary projection and Store reads remain in use.
            response = client.get(route.request.url, headers=HEADERS)
            assert response.status_code == 200
            if malformed and route.request.url.endswith("/api/requests"):
                data = response.json()
                data["requests"][0]["latest_event"]["action"] = "not_approve"
                route.fulfill(status=200, content_type="application/json", json=data)
            else:
                route.fulfill(
                    status=response.status_code,
                    headers=dict(response.headers),
                    body=response.content,
                )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.route(ORIGIN + "/**", serve_app)
                page.goto(ORIGIN)
                expect(page.locator("#queue-message")).to_have_text("Latest requests loaded.")
                expect(page.locator("#rows tr[data-request-id]")).to_have_count(6)
                row = page.locator(f'tr[data-request-id="{selected.source["request_id"]}"]')
                expect(row.locator(".owner")).to_have_text("Reviewer")
                expect(row).to_contain_text("Reviewer approved")
                expect(page.locator('[data-count="APPROVED"]')).to_have_text("1")
                expect(page.locator('[data-filter="NOT_APPROVED"]')).to_have_count(0)
                expect(page.locator('#status option[value="NOT_APPROVED"]')).to_have_count(0)
                expect(page.locator("#owner option")).to_have_text(
                    ["Anyone", "Processor", "Reviewer", "No further action"]
                )
                page.locator('[data-filter="APPROVED"]').click()
                page.locator("#owner").select_option("reviewer")
                expect(page.locator("#rows tr[data-request-id]")).to_have_count(1)
                expect(row.locator(".badge")).to_have_text("Approved")
                # A formerly valid action must now fail the same derived validator.
                before = page.locator("#rows").inner_text()
                malformed = True
                page.locator("#queue-reload").click()
                expect(page.locator("#queue-message")).to_contain_text(
                    "Refresh failed; showing previously loaded requests"
                )
                assert page.locator("#rows").inner_text() == before
            finally:
                browser.close()
    finally:
        models.STATE_OWNERS.clear()
        models.STATE_OWNERS.update(states)
        models.TRANSITIONS.clear()
        models.TRANSITIONS.update(transitions)


def test_keyboard_filters_real_navigation_counts_and_mobile(
    browser_queue: tuple[Any, Any, Any],
) -> None:
    _, reviewer, processor = browser_queue
    page = reviewer.new_page()
    page.goto(ORIGIN)
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(6)
    expect(page.locator(".card span")).to_have_text(
        ["Needs review", "Ready for completion", "Completed", "Not approved"]
    )
    expect(page.locator("#status option")).to_have_text(
        [
            "All statuses",
            "Needs review",
            "Approved · ready for completion",
            "Completed",
            "Not approved",
        ]
    )
    expect(page.locator("#owner option")).to_have_text(
        ["Anyone", "Reviewer", "Processor", "No further action"]
    )
    expect(page.locator('[data-count="AWAITING_REVIEW"]')).to_have_text("6")
    expect(page.locator("#rows")).to_contain_text("Awaiting first activity")
    ids = [
        row["request_id"]
        for row in reviewer.request.get(ORIGIN + "/api/requests").json()["requests"]
    ]
    assert (
        page.locator("#rows tr").evaluate_all("rows => rows.map(row => row.dataset.requestId)")
        == ids
    )
    reads: list[str] = []
    page.on(
        "request",
        lambda request: (
            reads.append(request.url) if request.url.endswith("/api/requests") else None
        ),
    )
    page.get_by_label("Find a request").focus()
    page.keyboard.type("  gARDen  ")
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(1)
    expect(page.locator("#rows")).to_contain_text("DEMO-03")
    page.get_by_label("Next owner").select_option("processor")
    expect(page.locator("#rows")).to_contain_text("No requests match these filters")
    expect(page.locator('[data-count="AWAITING_REVIEW"]')).to_have_text("6")
    page.locator("#reset-filters").click()
    page.locator('[data-filter="COMPLETED"]').click()
    expect(page.locator("#status")).to_have_value("COMPLETED")
    expect(page.locator("#count")).to_contain_text("Showing 0 of 6")
    page.locator("#reset-filters").click()
    page.locator("#search").fill("materials DEMO-02")
    expect(page.locator("#rows")).to_contain_text("No requests match these filters")
    assert reads == [], "Filtering must stay local"
    page.locator("#search").fill("DEMO-03")
    link = page.locator("#rows a.request")
    href = link.get_attribute("href")
    # Traverse from the search control with actual keyboard modality, including the
    # scroll region; programmatic focus after a mouse click is not :focus-visible.
    page.locator("#search").focus()
    for _ in range(8):
        page.keyboard.press("Tab")
        if link.evaluate("link => document.activeElement === link"):
            break
    expect(link).to_be_focused()
    assert link.evaluate("link => getComputedStyle(link).outlineStyle") != "none"
    page.keyboard.press("Enter")
    expect(page).to_have_url(ORIGIN + href)
    expect(page.locator("#save")).to_be_enabled()

    def garden_detail(
        target: Any, state: str, owner: str, version: int, history: list[str]
    ) -> None:
        expect(target.locator("#request-title")).to_have_text("Garden club seed kits")
        expect(target.locator("#request-meta")).to_have_text(
            "DEMO-03 · Submitted 2026-08-12 · $142.75"
        )
        expect(target.locator("#items li")).to_have_text(
            ["Seed packets · Garden Club · $82.75", "Planting trays · Garden Club · $60.00"]
        )
        expect(target.locator("#request-state")).to_have_text(
            f"{state} · Next owner: {owner} · Version {version}"
        )
        expect(target.locator("#history li")).to_have_text(history)

    garden_detail(page, "AWAITING_REVIEW", "reviewer", 0, [])
    page.get_by_role("radio", name="Approve", exact=True).check()
    page.locator("#decision-comment").fill("Fictional queue browser approval")
    page.locator("#save-decision").click()
    expect(page.locator("#feedback")).to_have_text("Decision saved.")
    detail_endpoint = ORIGIN + "/api" + href
    approved = reviewer.request.get(detail_endpoint).json()
    assert [
        (event["actor_label"], event["actor_role"], event["action"], event["body"])
        for event in approved["events"]
    ] == [("Reviewer", "reviewer", "approve", "Fictional queue browser approval")]
    approval_history = [
        f"Reviewer · {approved['events'][0]['created_at']} · approve · "
        "Fictional queue browser approval"
    ]
    page.reload()
    garden_detail(page, "APPROVED", "processor", 1, approval_history)
    page.go_back()
    expect(page.locator("#search")).to_have_value("")
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(6)
    expect(page.locator('[data-count="APPROVED"]')).to_have_text("1")
    other = processor.new_page()
    other.goto(ORIGIN + href)
    other.reload()
    garden_detail(other, "APPROVED", "processor", 1, approval_history)
    expect(other.locator("#complete-form")).to_be_visible()
    other.locator("#save-complete").click()
    expect(other.locator("#feedback")).to_contain_text("does not record a payment")
    page.locator("#queue-reload").click()
    expect(page.locator('[data-count="COMPLETED"]')).to_have_text("1")
    completed = reviewer.request.get(detail_endpoint).json()
    assert completed["events"][:1] == approved["events"]
    assert [
        (event["actor_label"], event["actor_role"], event["action"], event["body"])
        for event in completed["events"]
    ] == [
        ("Reviewer", "reviewer", "approve", "Fictional queue browser approval"),
        ("Processor", "processor", "complete", ""),
    ]
    complete_history = approval_history + [
        f"Processor · {completed['events'][1]['created_at']} · complete · "
    ]
    page.locator(f'#rows a.request[href="{href}"]').click()
    for account_page in (page, other):
        account_page.reload()
        garden_detail(account_page, "COMPLETED", "None", 2, complete_history)
    assert processor.request.get(detail_endpoint).json() == completed
    page.locator("#all-requests").click()
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(6)

    # Establish the other terminal outcome through its actual selected detail.
    page.locator("#rows tr").filter(has_text="DEMO-04").locator("a.request").click()
    rejected_endpoint = ORIGIN + "/api" + page.url.removeprefix(ORIGIN)
    page.get_by_role("radio", name="Not approve", exact=True).check()
    page.locator("#decision-comment").fill("Fictional queue browser reason")
    page.locator("#save-decision").click()
    expect(page.locator("#feedback")).to_have_text("Decision saved.")
    rejected = reviewer.request.get(rejected_endpoint).json()
    assert [
        (event["actor_label"], event["actor_role"], event["action"], event["body"])
        for event in rejected["events"]
    ] == [("Reviewer", "reviewer", "not_approve", "Fictional queue browser reason")]
    rejected_time = rejected["events"][0]["created_at"]
    expect(page.locator("#request-state")).to_have_text(
        "NOT_APPROVED · Next owner: None · Version 1"
    )
    expect(page.locator("#history li")).to_have_text(
        [f"Reviewer · {rejected_time} · not_approve · Fictional queue browser reason"]
    )
    page.locator("#all-requests").click()
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(6)
    page.locator("#queue-reload").click()
    expect(page.locator("#queue-message")).to_have_text("Latest requests loaded.")

    def counts() -> None:
        for state, count in (
            ("AWAITING_REVIEW", "4"),
            ("APPROVED", "0"),
            ("COMPLETED", "1"),
            ("NOT_APPROVED", "1"),
        ):
            expect(page.locator(f'[data-count="{state}"]')).to_have_text(count)

    counts()
    rejected_row = page.locator(f'#rows tr[data-request-id="{rejected["request"]["request_id"]}"]')
    expect(rejected_row.locator("a.request")).to_have_text("Volunteer appreciation refreshments")
    expect(rejected_row.locator(".ref")).to_have_text("DEMO-04")
    expect(rejected_row.locator("td").nth(1)).to_have_text("$78.20")
    expect(rejected_row.locator(".badge")).to_have_text("Not approved")
    expect(rejected_row.locator(".owner")).to_have_text("No further action")
    expect(rejected_row.locator("td").last).to_contain_text("Reviewer did not approve")
    expect(rejected_row.locator("time")).to_have_attribute("datetime", rejected_time)
    displayed_time = page.evaluate(
        "value => new Intl.DateTimeFormat(undefined, "
        "{dateStyle: 'medium', timeStyle: 'medium'}).format(new Date(value))",
        rejected_time,
    )
    expect(rejected_row.locator("time")).to_have_text(displayed_time)
    reads.clear()
    page.locator('[data-filter="NOT_APPROVED"]').click()
    expect(page.locator("#status")).to_have_value("NOT_APPROVED")
    expect(page.locator('[data-filter="NOT_APPROVED"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(1)
    expect(rejected_row).to_be_visible()
    counts()
    page.locator("#reset-filters").click()
    page.get_by_label("Next owner").select_option("none")
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(2)
    expect(rejected_row).to_be_visible()
    expect(page.locator(f'#rows a.request[href="{href}"]')).to_be_visible()
    expect(page.locator("#rows")).to_contain_text("Processor completed the handoff")
    counts()
    assert reads == [], "Terminal-state and owner filters must stay local"
    page.locator("#reset-filters").click()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / "queue-desktop.png"), full_page=True)
    other.screenshot(path=str(EVIDENCE / "detail-completed.png"), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#search")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".table-wrap").evaluate(
        "element => element.scrollWidth > element.clientWidth"
    )
    page.locator(".table-wrap").focus()
    page.keyboard.press("End")
    page.screenshot(path=str(EVIDENCE / "queue-mobile.png"), full_page=True)


@pytest.mark.parametrize("failure", ["html", "401", "503", "network", "shape"])
def test_initial_and_retained_failure_recovery(
    browser_queue: tuple[Any, Any, Any], failure: str
) -> None:
    _, reviewer, _ = browser_queue
    page = reviewer.new_page()

    def fail(route: Route) -> None:
        if failure == "network":
            route.abort()
        elif failure == "shape":
            data = route.fetch().json()
            data["request_cap"] -= 1
            route.fulfill(status=200, content_type="application/json", json=data)
        elif failure == "html":
            route.fulfill(status=200, content_type="text/html", body="<html>Sign in</html>")
        else:
            route.fulfill(
                status=int(failure),
                content_type="application/json",
                json={
                    "error": {
                        "code": "UNAUTHENTICATED"
                        if failure == "401"
                        else "TEMPORARILY_UNAVAILABLE",
                        "message": "Fictional failure",
                        "correlation_id": "example",
                    }
                },
            )

    page.route("**/api/requests", fail)
    page.goto(ORIGIN)
    expect(page.locator("#queue-message")).to_contain_text("Could not load requests.")
    expect(page.locator("#count")).to_have_text("Request counts are unavailable.")
    expect(page.locator('[data-count="AWAITING_REVIEW"]')).to_have_text("—")
    expect(page.locator("#search")).to_be_disabled()
    expect(page.locator("#queue-reload")).to_be_enabled()
    if failure in ("html", "401"):
        expect(page.locator("#session-refresh")).to_be_visible()
    page.unroute("**/api/requests", fail)
    page.locator("#queue-reload").click()
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(6)
    before = page.locator("#rows").inner_text()
    loaded = page.locator("#queue-freshness").inner_text()
    page.route("**/api/requests", fail)
    page.locator("#queue-reload").click()
    expect(page.locator("#queue-message")).to_contain_text(
        "Refresh failed; showing previously loaded requests"
    )
    assert page.locator("#rows").inner_text() == before
    assert page.locator("#queue-freshness").inner_text() == loaded
    expect(page.locator('[data-count="AWAITING_REVIEW"]')).to_have_text("6")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(EVIDENCE / f"refresh-error-{failure}.png"), full_page=True)
    page.unroute("**/api/requests", fail)
    page.locator("#queue-reload").click()
    expect(page.locator("#queue-message")).to_have_text("Latest requests loaded.")
    expect(page.locator("#session-refresh")).to_be_hidden()


@pytest.mark.parametrize("late", ["success", "session_error"])
def test_pageshow_supersedes_pending_load_without_stale_overwrite(
    browser_queue: tuple[Any, Any, Any],
    late: str,
) -> None:
    _, reviewer, _ = browser_queue
    page = reviewer.new_page()
    initial = reviewer.request.get(ORIGIN + "/api/requests").json()
    held: list[Route] = []
    page.route("**/api/requests", lambda route: held.append(route), times=1)
    page.goto(ORIGIN)
    expect(page.locator("#queue-reload")).to_be_disabled()
    page.wait_for_function(
        "document.querySelector('#queue-panel').getAttribute('aria-busy') === 'true'"
    )
    selected = initial["requests"][0]["request_id"]
    response = reviewer.request.post(
        ORIGIN + "/api/requests/" + selected + "/comments",
        headers={"Origin": ORIGIN, "Content-Type": "application/json", "X-PTA-CSRF": "1"},
        data={
            "operation_id": page.evaluate("crypto.randomUUID()"),
            "expected_version": 0,
            "body": "Fictional restoration activity",
        },
    )
    assert response.status == 200
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    expect(page.locator("#rows")).to_contain_text("Reviewer added a comment")
    expect(page.locator("#queue-reload")).to_be_enabled()
    assert len(held) == 1
    if late == "success":
        held[0].fulfill(status=200, content_type="application/json", json=initial)
    else:
        held[0].fulfill(status=200, content_type="text/html", body="<html>Sign in</html>")
    # A second full fetch is an observable event-loop barrier after the held response.
    page.evaluate(
        "async () => { await fetch('/healthz'); "
        "await new Promise(resolve => setTimeout(resolve, 50)); }"
    )
    expect(page.locator("#rows")).to_contain_text("Reviewer added a comment")
    expect(page.locator("#session-refresh")).to_be_hidden()
    expect(page.locator("#queue-message")).to_have_text("Latest requests loaded.")
    page.locator("#search").fill("DEMO-02")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
    expect(page.locator("#search")).to_have_value("")
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(6)


@pytest.mark.parametrize("failure", ["network", "timeout"])
def test_lost_save_retries_selected_request_and_escapes_text(
    browser_queue: tuple[Any, Any, Any], failure: str
) -> None:
    _, reviewer, _ = browser_queue
    listing = reviewer.request.get(ORIGIN + "/api/requests").json()["requests"]
    selected = next(row["request_id"] for row in listing if row["display"]["ref"] == "DEMO-05")
    before = {
        row["request_id"]: reviewer.request.get(
            ORIGIN + "/api/requests/" + row["request_id"]
        ).json()
        for row in listing
    }
    page = reviewer.new_page()
    if failure == "timeout":
        page.clock.install(time="2030-01-01T00:00:00Z")
    page.goto(ORIGIN)
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(6)
    page.goto(ORIGIN + "/requests/" + selected)
    expect(page.locator("#save")).to_be_enabled()
    if failure == "timeout":
        page.clock.pause_at("2030-01-01T00:01:00Z")
    lost: list[dict[str, Any]] = []
    committed: list[dict[str, Any]] = []
    held: list[Route] = []
    dialogs: list[str] = []

    def dismiss_navigation(dialog: Any) -> None:
        dialogs.append(dialog.type)
        dialog.dismiss()

    page.on("dialog", dismiss_navigation)

    def lose(route: Route) -> None:
        lost.append(route.request.post_data_json)
        assert route.request.url == ORIGIN + "/api/requests/" + selected + "/comments"
        response = route.fetch()
        assert response.status == 200
        committed.append(response.json()["receipt"])
        held.append(route)
        page.evaluate("document.documentElement.dataset.timeoutResponseHeld = 'true'")

    page.route("**/comments", lose, times=1)
    draft = "<img src=x onerror=window.QUEUE_XSS=1> Fictional exact draft"
    page.locator("#comment").fill(draft)
    page.locator("#save").click()
    expect(page.locator("html")).to_have_attribute("data-timeout-response-held", "true")
    assert lost[0]["body"] == draft and lost[0]["expected_version"] == 0
    after_save = reviewer.request.get(ORIGIN + "/api/requests/" + selected).json()
    assert after_save["events"] == before[selected]["events"] + committed
    expect(page.locator("#all-requests")).to_have_attribute("aria-disabled", "true")
    expect(page.locator("#feedback")).to_have_text("Saving...")
    page.evaluate("history.back()")
    page.wait_for_timeout(100)
    expect(page).to_have_url(ORIGIN + "/requests/" + selected)
    assert dialogs == ["beforeunload"]
    page.locator("#all-requests").click(force=True)
    expect(page).to_have_url(ORIGIN + "/requests/" + selected)
    expect(page.locator("#feedback")).to_contain_text("Wait before returning")
    expect(page.locator("#comment")).to_have_value(draft)
    assert len(held) == 1
    if failure == "timeout":
        # Fire the application's 25-second AbortController timer with a real response held.
        try:
            page.clock.run_for(25000)
            expect(page.locator("#feedback")).to_contain_text("TEMPORARILY_UNAVAILABLE")
        finally:
            held[0].abort()
    else:
        held[0].abort()
    expect(page.locator("#retry")).to_be_visible()
    expect(page.locator("#retry")).to_be_enabled()
    expect(page.locator("#comment")).to_have_value(draft)
    page.evaluate("history.back()")
    page.wait_for_timeout(100)
    expect(page).to_have_url(ORIGIN + "/requests/" + selected)
    assert dialogs == ["beforeunload", "beforeunload"]
    page.locator("#all-requests").click(force=True)
    expect(page).to_have_url(ORIGIN + "/requests/" + selected)
    expect(page.locator("#feedback")).to_contain_text("Resolve this pending save")
    expect(page.locator("#retry")).to_be_focused()
    with page.expect_response("**/comments") as response:
        page.locator("#retry").click()
    receipt = response.value.json()["receipt"]
    assert receipt["operation_id"] == lost[0]["operation_id"] and receipt["request_id"] == selected
    assert receipt == committed[0]
    assert response.value.request.post_data_json == lost[0]
    expect(page.locator("#history li")).to_have_count(1)
    expect(page.locator("#history")).to_contain_text(draft)
    assert page.locator("#history img").count() == 0 and page.evaluate("window.QUEUE_XSS") is None
    expect(page.locator("#all-requests")).not_to_have_attribute("aria-disabled", "true")
    page.locator("#all-requests").click()
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(6)
    assert draft not in page.locator("#rows").inner_text()
    current = reviewer.request.get(ORIGIN + "/api/requests").json()["requests"]
    assert all(row["version"] == (1 if row["request_id"] == selected else 0) for row in current)
    for request_id, initial in before.items():
        assert reviewer.request.get(ORIGIN + "/api/requests/" + request_id).json() == (
            after_save if request_id == selected else initial
        )


@pytest.mark.parametrize(
    "kind,failure",
    [
        pytest.param("comments", "network", id="comment-network-read"),
        pytest.param("decision", "503", id="decision-temporary-read"),
        pytest.param("complete", "401", id="completion-unauthenticated-read"),
        pytest.param("comments", "html", id="comment-sign-in-html-read"),
        pytest.param("decision", "timeout", id="decision-native-timeout-read"),
    ],
)
def test_saved_receipt_then_failed_read_preserves_exact_retry(
    browser_queue: tuple[Any, Any, Any], kind: str, failure: str
) -> None:
    _, reviewer, processor = browser_queue
    listing = reviewer.request.get(ORIGIN + "/api/requests").json()["requests"]
    selected = next(row["request_id"] for row in listing if row["display"]["ref"] == "DEMO-05")
    endpoint = ORIGIN + "/api/requests/" + selected
    if kind == "complete":
        approved = reviewer.request.post(
            endpoint + "/decision",
            headers={"Origin": ORIGIN, "Content-Type": "application/json", "X-PTA-CSRF": "1"},
            data={
                "operation_id": str(uuid4()),
                "expected_version": 0,
                "decision": "approve",
                "body": "Fictional approval before post-save read recovery",
            },
        )
        assert approved.status == 200
    context = processor if kind == "complete" else reviewer
    before = {
        row["request_id"]: context.request.get(ORIGIN + "/api/requests/" + row["request_id"]).json()
        for row in listing
    }
    baseline = before[selected]
    page = context.new_page()
    if failure == "timeout":
        page.clock.install(time="2030-01-01T00:00:00Z")
    page.goto(ORIGIN + "/requests/" + selected)
    save = page.locator("#save" if kind == "comments" else "#save-" + kind)
    draft_input = page.locator("#comment" if kind == "comments" else "#" + kind + "-comment")
    expect(save).to_be_enabled()
    if failure == "timeout":
        page.clock.pause_at("2030-01-01T00:01:00Z")
    draft = "Fictional saved draft retained until history loads"
    draft_input.fill(draft)
    if kind == "decision":
        page.get_by_role("radio", name="Not approve", exact=True).check()
    failed_reads: list[str] = []
    held: list[Route] = []

    def fail_read(route: Route) -> None:
        assert route.request.method == "GET" and route.request.url == endpoint
        failed_reads.append(route.request.url)
        if failure == "timeout":
            assert route.fetch().status == 200
            held.append(route)
            page.evaluate("document.documentElement.dataset.timeoutResponseHeld = 'true'")
        elif failure == "network":
            route.abort()
        elif failure == "html":
            route.fulfill(status=200, content_type="text/html", body="<html>Sign in</html>")
        else:
            route.fulfill(
                status=int(failure),
                content_type="application/json",
                json={
                    "error": {
                        "code": "UNAUTHENTICATED" if failure == "401" else "TEMPORARILY_UNAVAILABLE"
                    }
                },
            )

    # Deliver the real POST receipt to Chromium; fail only its follow-up detail GET.
    page.route(endpoint, fail_read, times=1)
    with page.expect_response(endpoint + "/" + kind) as saved:
        save.click()
    assert saved.value.status == 200
    sent = saved.value.request.post_data_json
    receipt = saved.value.json()["receipt"]
    assert sent["expected_version"] == baseline["request"]["version"]
    assert sent["body"] == draft
    assert receipt["operation_id"] == sent["operation_id"]
    assert receipt["request_id"] == selected
    assert receipt["actor_role"] == ("processor" if kind == "complete" else "reviewer")
    assert receipt["action"] == {"comments": "comment", "decision": "not_approve"}.get(kind, kind)
    if kind == "decision":
        assert sent["decision"] == "not_approve"
    if failure == "timeout":
        expect(page.locator("html")).to_have_attribute("data-timeout-response-held", "true")
        try:
            page.clock.run_for(25000)
            expect(page.locator("#feedback")).to_contain_text("TEMPORARILY_UNAVAILABLE")
        finally:
            held[0].abort()
    expect(page.locator("#feedback")).to_contain_text("Retry")
    assert failed_reads == [endpoint]
    expect(draft_input).to_be_visible()
    expect(draft_input).to_have_value(draft)
    expect(page.locator("#retry")).to_be_visible()
    expect(page.locator("#retry")).to_be_enabled()
    expect(page.locator("#reload")).to_be_enabled()
    expect(save).to_be_disabled()
    expect(page.locator("#all-requests")).to_have_attribute("aria-disabled", "true")
    if kind == "decision":
        expect(page.get_by_role("radio", name="Not approve", exact=True)).to_be_checked()
    if failure in ("401", "html"):
        expect(page.locator("#session-refresh")).to_be_visible()
        expect(page.locator("#feedback")).to_contain_text("Keep this request tab open")
        expect(page.locator("#feedback")).to_contain_text("Refresh your sign-in in a new tab")
    else:
        expect(page.locator("#feedback")).to_contain_text("Your draft is retained")
        expect(page.locator("#session-refresh")).to_be_hidden()
    after_save = context.request.get(endpoint).json()
    assert after_save["events"] == baseline["events"] + [receipt]
    assert after_save["request"]["version"] == baseline["request"]["version"] + 1
    expect(page.locator("#history li")).to_have_count(len(baseline["events"]))

    # Reload can discover a completed state before replay; the operation stays retryable.
    if kind == "complete":
        page.locator("#reload").click()
        expect(page.locator("#feedback")).to_have_text("Latest history loaded.")
        expect(page.locator("#complete-form")).to_be_hidden()
        expect(draft_input).to_have_value(draft)
        expect(page.locator("#retry")).to_be_enabled()
        assert context.request.get(endpoint).json() == after_save
    with page.expect_response(endpoint + "/" + kind) as replayed:
        page.get_by_role("button", name="Retry same operation").click()
    assert replayed.value.status == 200
    assert replayed.value.request.post_data_json == sent
    assert replayed.value.json()["receipt"] == receipt
    expect(page.locator("#feedback")).to_have_text(
        {
            "comments": "Comment saved.",
            "decision": "Decision saved.",
            "complete": "Workflow handoff complete. This does not record a payment.",
        }[kind]
    )
    expect(page.locator("#history li")).to_have_text(
        [
            f"{event['actor_label']} · {event['created_at']} · {event['action']} · {event['body']}"
            for event in after_save["events"]
        ]
    )
    request = after_save["request"]
    expect(page.locator("#request-state")).to_have_text(
        f"{request['state']} · Next owner: {request['next_owner_role'] or 'None'} "
        f"· Version {request['version']}"
    )
    expect(draft_input).to_have_value("")
    assert page.locator('input[type="radio"]:checked').count() == 0
    expect(page.locator("#retry")).to_be_hidden()
    expect(page.locator("#session-refresh")).to_be_hidden()
    expect(page.locator("#save")).to_be_enabled()
    if kind != "comments":
        expect(page.locator("#" + kind + "-form")).to_be_hidden()
    expect(page.locator("#all-requests")).not_to_have_attribute("aria-disabled", "true")
    page.get_by_role("link", name="All requests").click()
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(6)
    for request_id, initial in before.items():
        assert context.request.get(ORIGIN + "/api/requests/" + request_id).json() == (
            after_save if request_id == selected else initial
        )


@pytest.mark.parametrize(
    "failure,kind,committed",
    [
        pytest.param("401", "decision", False, id="401-decision-not-saved"),
        pytest.param("401", "complete", True, id="401-complete-receipt-lost"),
        pytest.param("html", "comments", False, id="html-comments-not-saved"),
    ],
)
def test_detail_sign_in_recovery_preserves_exact_retry(
    browser_queue: tuple[Any, Any, Any], failure: str, kind: str, committed: bool
) -> None:
    _, reviewer, processor = browser_queue
    listing = reviewer.request.get(ORIGIN + "/api/requests").json()["requests"]
    selected = next(row["request_id"] for row in listing if row["display"]["ref"] == "DEMO-05")
    endpoint = ORIGIN + "/api/requests/" + selected
    detail_url = ORIGIN + "/requests/" + selected
    if kind == "complete":
        approved = reviewer.request.post(
            endpoint + "/decision",
            headers={"Origin": ORIGIN, "Content-Type": "application/json", "X-PTA-CSRF": "1"},
            data={
                "operation_id": str(uuid4()),
                "expected_version": 0,
                "decision": "approve",
                "body": "Fictional approval before completion recovery",
            },
        )
        assert approved.status == 200
    context = processor if kind == "complete" else reviewer
    page = context.new_page()
    page.goto(ORIGIN)
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(6)
    page.locator(f'#rows a.request[href="/requests/{selected}"]').click()
    expect(page).to_have_url(detail_url)
    save = page.locator("#save" if kind == "comments" else "#save-" + kind)
    draft_input = page.locator("#comment" if kind == "comments" else "#" + kind + "-comment")
    expect(save).to_be_enabled()
    baseline = context.request.get(endpoint).json()
    draft = "Fictional exact draft across sign-in recovery"
    draft_input.fill(draft)
    if kind == "decision":
        page.get_by_role("radio", name="Not approve", exact=True).check()
    sent: list[dict[str, Any]] = []
    original_receipts: list[dict[str, Any]] = []

    def session_response(route: Route) -> None:
        assert route.request.url == endpoint + "/" + kind
        sent.append(route.request.post_data_json)
        if committed:
            response = route.fetch()
            assert response.status == 200
            original_receipts.append(response.json()["receipt"])
        if failure == "html":
            route.fulfill(status=200, content_type="text/html", body="<html>Sign in</html>")
        elif committed:
            route.fulfill(
                status=401,
                content_type="application/json",
                json={"error": {"code": "UNAUTHENTICATED"}},
            )
        else:
            # Exercise the actual local auth rejection before any transaction runs.
            response = route.fetch(
                headers={**route.request.headers, "x-goog-iap-jwt-assertion": ""}
            )
            assert response.status == 401
            route.fulfill(response=response)

    page.route("**/" + kind, session_response, times=1)
    save.click()
    expect(page.locator("#session-refresh")).to_be_visible()
    expect(page.locator("#retry")).to_be_enabled()
    expect(draft_input).to_have_value(draft)
    assert len(sent) == 1
    assert sent[0]["expected_version"] == baseline["request"]["version"]
    assert sent[0]["body"] == draft
    if kind == "decision":
        assert sent[0]["decision"] == "not_approve"
    after_attempt = context.request.get(endpoint).json()
    assert after_attempt["events"] == baseline["events"] + original_receipts
    with context.expect_page(timeout=5000) as opened:
        page.get_by_role("link", name="Refresh your sign-in").click()
    sign_in = opened.value
    try:
        expect(sign_in).to_have_url(detail_url)
        expect(sign_in.locator("#request-title")).to_have_text("Field day activity supplies")
        expect(sign_in.locator("#history li")).to_have_count(len(after_attempt["events"]))
        assert sign_in.evaluate("window.opener === null")
        assert sign_in.evaluate("document.referrer") == ""
        expect(page).to_have_url(detail_url)
        expect(page.locator("#feedback")).to_contain_text("Keep this request tab open")
        expect(page.locator("#feedback")).to_contain_text("Retry same operation")
        expect(draft_input).to_have_value(draft)
        expect(page.locator("#all-requests")).to_have_attribute("aria-disabled", "true")
        if kind == "decision":
            expect(page.get_by_role("radio", name="Not approve", exact=True)).to_be_checked()
        assert context.request.get(endpoint).json() == after_attempt
    finally:
        sign_in.close()
    page.bring_to_front()
    with page.expect_response("**/" + kind) as retried:
        page.get_by_role("button", name="Retry same operation").click()
    assert retried.value.status == 200
    assert retried.value.request.url == endpoint + "/" + kind
    assert retried.value.request.post_data_json == sent[0]
    receipt = retried.value.json()["receipt"]
    assert receipt["operation_id"] == sent[0]["operation_id"]
    assert receipt["request_id"] == selected
    if committed:
        assert receipt == original_receipts[0]
    expect(page.locator("#history li")).to_have_count(len(baseline["events"]) + 1)
    expect(page.locator("#history")).to_contain_text(draft)
    expect(draft_input).to_have_value("")
    expect(page.locator("#session-refresh")).to_be_hidden()
    expect(page.locator("#retry")).to_be_hidden()
    assert context.request.get(endpoint).json()["events"] == baseline["events"] + [receipt]
    page.get_by_role("link", name="All requests").click()
    expect(page.locator("#rows tr[data-request-id]")).to_have_count(6)
    current = context.request.get(ORIGIN + "/api/requests").json()["requests"]
    assert all(
        row["version"] == (len(baseline["events"]) + 1 if row["request_id"] == selected else 0)
        for row in current
    )
