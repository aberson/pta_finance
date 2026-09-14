"use strict";

(() => {
  const root = document.querySelector("main");
  const url = `/api/requests/${root.dataset.requestId}`;
  const retry = document.querySelector("#retry");
  const reload = document.querySelector("#reload");
  const allRequests = document.querySelector("#all-requests");
  const feedback = document.querySelector("#feedback");
  const session = document.querySelector("#session-refresh");
  const forms = [
    {kind: "comments", id: "comment-form", draft: "comment", saved: "Comment saved."},
    {kind: "decision", id: "decision-form", draft: "decision-comment", saved: "Decision saved."},
    {kind: "complete", id: "complete-form", draft: "complete-comment", saved: "Workflow handoff complete. This does not record a payment."}
  ].map(spec => ({...spec, form: document.getElementById(spec.id), input: document.getElementById(spec.draft)}))
    .filter(spec => spec.form);
  let version = null;
  let state = null;
  let pending = null;
  let busy = false;
  let loadSequence = 0;

  function message(text) { feedback.textContent = text; }
  function available(spec) {
    return spec.kind === "comments" || (spec.kind === "decision" && state === "AWAITING_REVIEW") ||
      (spec.kind === "complete" && state === "APPROVED");
  }
  function guardQueueNavigation() {
    if (!allRequests) return;
    if (pending) allRequests.setAttribute("aria-disabled", "true");
    else allRequests.removeAttribute("aria-disabled");
  }
  function working(value) {
    busy = value;
    reload.disabled = value;
    retry.disabled = value;
    forms.forEach(spec => {
      spec.form.hidden = spec.kind !== "comments" && !available(spec);
      spec.form.querySelectorAll("input, textarea, button").forEach(control => {
        control.disabled = value || (control.type === "submit" && (version === null || !available(spec)));
      });
      spec.form.setAttribute("aria-busy", String(value));
    });
    guardQueueNavigation();
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
      version = null;
      working(busy);
      throw error;
    }
    if (sequence !== loadSequence) return;
    const request = data.request;
    version = request.version;
    state = request.state;
    session.hidden = true;
    document.querySelector("#request-title").textContent = request.display.title;
    document.querySelector("#request-meta").textContent =
      `${request.display.ref} · Submitted ${request.display.submitted_on} · $${request.display.total}`;
    document.querySelector("#request-state").textContent =
      `${state} · Next owner: ${request.next_owner_role || "None"} · Version ${version}`;
    const next = state === "AWAITING_REVIEW" ?
      (root.dataset.role === "reviewer" ? "Choose an outcome and add a decision comment." : "Waiting for the reviewer's decision.") :
      state === "APPROVED" ? (root.dataset.role === "processor" ? "You can mark the workflow handoff complete." : "Waiting for the processor to complete the handoff.") :
      state === "NOT_APPROVED" ? "Not approved. This outcome is final; both participants may still comment." :
      "Workflow handoff complete. Both participants may still comment. This does not record a payment.";
    document.querySelector("#next-action").textContent = ["handoff", "queue"].includes(root.dataset.mode) ? next : "Both participants may add shared comments.";
    document.querySelector("#items").replaceChildren(...request.display.items.map(item =>
      listItem(`${item.description} · ${item.category} · $${item.amount}`)));
    document.querySelector("#history").replaceChildren(...data.events.map(event =>
      listItem(`${event.actor_label} · ${event.created_at} · ${event.action} · ${event.body}`)));
    working(busy);
  }
  function failure(error) {
    const code = typeof error.code === "string" ? error.code : "TEMPORARILY_UNAVAILABLE";
    retry.hidden = pending === null;
    if (code === "SESSION_REFRESH" || code === "UNAUTHENTICATED") {
      version = null;
      session.hidden = false;
      if (root.dataset.mode === "queue") {
        message("Keep this request tab open. Refresh your sign-in in a new tab, then return here and " +
          (pending ? "choose Retry same operation to check whether your save completed." : "choose Reload to load this request."));
      } else {
        message("Refresh your sign-in, then check whether your operation was saved.");
      }
    } else if (["TEMPORARILY_UNAVAILABLE", "RETRY_CONFLICT", "INTERNAL_ERROR"].includes(code)) {
      message(`${code}: Your draft is retained. Retry the same operation or reload to check it.`);
    } else {
      pending = null;
      retry.hidden = true;
      version = null;
      message(`${code}: Reload and check the request before submitting again.`);
    }
    working(busy);
  }
  async function submit(spec, replay = false) {
    if (busy || (!replay && (version === null || !available(spec)))) return;
    if (!replay && (!pending || pending.kind !== spec.kind)) {
      const data = {operation_id: crypto.randomUUID(), expected_version: version, body: spec.input.value};
      if (spec.kind === "decision") data.decision = spec.form.querySelector("input:checked")?.value;
      if (spec.kind !== "complete" && !data.body.trim()) {
        message("A nonblank comment is required.");
        return;
      }
      if (spec.kind === "decision" && !data.decision) {
        message("Choose exactly one review outcome.");
        return;
      }
      pending = {kind: spec.kind, data};
    }
    if (!pending) return;
    working(true);
    retry.hidden = true;
    message("Saving...");
    try {
      const result = await jsonRequest(`${url}/${pending.kind}`, {method: "POST",
        headers: {"Content-Type": "application/json", "X-PTA-CSRF": "1"}, body: JSON.stringify(pending.data)});
      if (!result.receipt || result.receipt.operation_id !== pending.data.operation_id) {
        throw {code: "TEMPORARILY_UNAVAILABLE"};
      }
      await load();
      pending = null;
      spec.input.value = "";
      spec.form.querySelectorAll("input[type=radio]").forEach(input => { input.checked = false; });
      message(spec.saved);
    } catch (error) {
      if (error.code === "STALE_VERSION") {
        pending = null;
        version = null;
        try {
          await load();
          message("STALE_VERSION: Latest history loaded. Review it, then deliberately resubmit your draft if the action is available.");
        } catch (loadError) { failure(loadError); }
      } else { failure(error); }
    } finally { working(false); }
  }
  forms.forEach(spec => {
    spec.form.addEventListener("submit", event => { event.preventDefault(); void submit(spec); });
    spec.form.addEventListener("input", () => {
      pending = null;
      retry.hidden = true;
      guardQueueNavigation();
    });
  });
  allRequests?.addEventListener("click", event => {
    if (!pending) return;
    event.preventDefault();
    message(busy ? "Saving this operation. Wait before returning to all requests." :
      "Resolve this pending save before returning to all requests. Retry the same operation or reload to check it.");
    if (!busy && !retry.hidden) retry.focus();
  });
  window.addEventListener("beforeunload", event => {
    if (!pending) return;
    event.preventDefault();
    event.returnValue = "";
  });
  retry.addEventListener("click", () => {
    if (pending) void submit(forms.find(spec => spec.kind === pending.kind), true);
  });
  reload.addEventListener("click", async () => {
    if (busy) return;
    working(true);
    try { await load(); message("Latest history loaded."); } catch (error) { failure(error); }
    finally { working(false); }
  });
  void load().catch(failure);
})();
