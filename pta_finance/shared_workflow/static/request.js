"use strict";

(() => {
  const root = document.querySelector("main");
  const requestId = root.dataset.requestId;
  const url = `/api/requests/${requestId}`;
  const form = document.querySelector("#comment-form");
  const draft = document.querySelector("#comment");
  const save = document.querySelector("#save");
  const retry = document.querySelector("#retry");
  const reload = document.querySelector("#reload");
  const feedback = document.querySelector("#feedback");
  const session = document.querySelector("#session-refresh");
  let version = null;
  let pending = null;
  let busy = false;
  let loadSequence = 0;

  function message(text) { feedback.textContent = text; }
  function working(value) {
    busy = value;
    save.disabled = value || version === null;
    reload.disabled = value;
    retry.disabled = value;
    draft.disabled = value;
    form.setAttribute("aria-busy", String(value));
  }
  async function jsonRequest(target, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch(target, {credentials: "same-origin", cache: "no-store",
        ...options, signal: controller.signal});
      if (response.redirected || !(response.headers.get("content-type") || "").includes("application/json")) {
        throw {code: "SESSION_REFRESH"};
      }
      let data;
      try { data = await response.json(); } catch { throw {code: "SESSION_REFRESH"}; }
      if (!response.ok) throw {code: data.error?.code || "TEMPORARILY_UNAVAILABLE"};
      return data;
    } finally { clearTimeout(timer); }
  }
  function listItem(text) {
    const element = document.createElement("li");
    element.textContent = text;
    return element;
  }
  async function load() {
    const sequence = ++loadSequence;
    let data;
    try { data = await jsonRequest(url); }
    catch (error) {
      if (sequence !== loadSequence) return;
      throw error;
    }
    if (sequence !== loadSequence) return;
    const request = data.request;
    version = request.version;
    document.querySelector("#request-title").textContent = request.display.title;
    document.querySelector("#request-meta").textContent =
      `${request.display.ref} · Submitted ${request.display.submitted_on} · $${request.display.total}`;
    document.querySelector("#request-state").textContent =
      `${request.state} · Next owner: ${request.next_owner_role || "None"} · Version ${version}`;
    document.querySelector("#items").replaceChildren(...request.display.items.map(item =>
      listItem(`${item.description} · ${item.category} · $${item.amount}`)));
    document.querySelector("#history").replaceChildren(...data.events.map(event =>
      listItem(`${event.actor_label} · ${event.created_at} · ${event.body}`)));
    save.disabled = busy;
  }
  function failure(error) {
    const code = error.code || "TEMPORARILY_UNAVAILABLE";
    if (code === "SESSION_REFRESH" || code === "UNAUTHENTICATED") {
      session.hidden = false;
      message("Refresh your sign-in, then check whether your comment was saved.");
    } else if (["TEMPORARILY_UNAVAILABLE", "RETRY_CONFLICT", "INTERNAL_ERROR"].includes(code)) {
      retry.hidden = pending === null;
      message(`${code}: Your draft is retained. Retry the same comment or reload to check it.`);
    } else {
      message(`${code}: Reload and check the request before submitting again.`);
    }
  }
  async function submit() {
    if (busy || version === null) return;
    if (!pending) pending = {operation_id: crypto.randomUUID(), expected_version: version, body: draft.value};
    working(true);
    retry.hidden = true;
    message("Saving…");
    try {
      const result = await jsonRequest(`${url}/comments`, {method: "POST",
        headers: {"Content-Type": "application/json", "X-PTA-CSRF": "1"}, body: JSON.stringify(pending)});
      if (!result.receipt || result.receipt.operation_id !== pending.operation_id) {
        throw {code: "TEMPORARILY_UNAVAILABLE"};
      }
      pending = null;
      draft.value = "";
      await load();
      message("Comment saved.");
    } catch (error) {
      if (error.code === "STALE_VERSION") {
        pending = null;
        version = null;
        try {
          await load();
          message("STALE_VERSION: Latest comments loaded. Review them, then deliberately save your draft.");
        } catch (loadError) { failure(loadError); }
      } else { failure(error); }
    } finally { working(false); }
  }
  form.addEventListener("submit", event => { event.preventDefault(); void submit(); });
  retry.addEventListener("click", () => { void submit(); });
  draft.addEventListener("input", () => { pending = null; retry.hidden = true; });
  reload.addEventListener("click", async () => {
    if (busy) return;
    working(true);
    try { await load(); message("Latest comments loaded."); } catch (error) { failure(error); }
    finally { working(false); }
  });
  void load().catch(failure);
})();
