"use strict";

(() => {
  const root = document.querySelector("main");
  const {states, owners, actions} = JSON.parse(root.dataset.workflow);
  const requestCap = Number(root.dataset.requestCap);
  const eventCap = Number(root.dataset.eventCap);
  const search = document.querySelector("#search");
  const status = document.querySelector("#status");
  const owner = document.querySelector("#owner");
  const rows = document.querySelector("#rows");
  const count = document.querySelector("#count");
  const reload = document.querySelector("#queue-reload");
  const reset = document.querySelector("#reset-filters");
  const message = document.querySelector("#queue-message");
  const freshness = document.querySelector("#queue-freshness");
  const session = document.querySelector("#session-refresh");
  const panel = document.querySelector("#queue-panel");
  const cards = [...document.querySelectorAll("[data-filter]")];
  const money = new Intl.NumberFormat("en-US", {style: "currency", currency: "USD"});
  const dateTime = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "medium"});
  let requests = null;
  let busy = false;
  let loadSequence = 0;

  function working(value) {
    busy = value;
    reload.disabled = value;
    panel.setAttribute("aria-busy", String(value));
    [search, status, owner, reset, ...cards].forEach(control => { control.disabled = requests === null; });
  }
  function cell(row, text, className) {
    const td = document.createElement("td");
    if (text !== undefined) td.textContent = text;
    if (className) td.className = className;
    row.append(td);
    return td;
  }
  function emptyRow(text) {
    const row = document.createElement("tr");
    cell(row, text, "empty").colSpan = 5;
    return row;
  }
  function resetFilters() {
    search.value = "";
    status.value = "";
    owner.value = "";
    render();
  }
  function render() {
    cards.forEach(card => { card.setAttribute("aria-pressed", String(card.dataset.filter === status.value)); });
    if (requests === null) return;
    const query = search.value.trim().toLowerCase();
    const visible = requests.filter(request =>
      (!status.value || request.state === status.value) &&
      (!owner.value || (request.next_owner_role || "none") === owner.value) &&
      (request.display.title.toLowerCase().includes(query) || request.display.ref.toLowerCase().includes(query)));
    const rendered = visible.map(request => {
      const row = document.createElement("tr");
      row.dataset.requestId = request.request_id;
      const first = cell(row);
      const link = document.createElement("a");
      link.className = "request";
      link.href = `/requests/${request.request_id}`;
      link.textContent = request.display.title;
      const ref = document.createElement("span");
      ref.className = "ref";
      ref.textContent = request.display.ref;
      first.append(link, ref);
      cell(row, money.format(Number(request.display.total)));
      const badge = document.createElement("span");
      badge.className = `badge ${states[request.state].css}`;
      badge.textContent = states[request.state].label;
      cell(row).append(badge);
      cell(row, owners[request.next_owner_role || "none"], "owner");
      const event = request.latest_event;
      const last = cell(row, event ? `${event.actor_label} ${actions[event.action]}` : "Awaiting first activity");
      if (event) {
        const time = document.createElement("time");
        time.className = "latest";
        time.dateTime = event.created_at;
        time.textContent = dateTime.format(new Date(event.created_at));
        last.append(time);
      }
      return row;
    });
    rows.replaceChildren(...(rendered.length ? rendered : [emptyRow("No requests match these filters. Use Reset filters to show all requests.")]));
    for (const card of cards) {
      card.querySelector("[data-count]").textContent =
        String(requests.filter(request => request.state === card.dataset.filter).length);
    }
    count.textContent = `Showing ${visible.length} of ${requests.length} fictional requests · Latest activity first`;
  }
  function validResponse(data) {
    if (!Number.isInteger(requestCap) || requestCap < 1 || !Number.isInteger(eventCap) || eventCap < 1 ||
        data?.request_cap !== requestCap ||
        !Array.isArray(data.requests) || data.request_count !== data.requests.length ||
        data.request_count !== data.request_cap) return false;
    const ids = new Set();
    return data.requests.every(request => {
      if (!request || typeof request.request_id !== "string" || !/^[a-f0-9]{64}$/.test(request.request_id) || ids.has(request.request_id)) return false;
      ids.add(request.request_id);
      const display = request.display;
      const event = request.latest_event;
      return Object.hasOwn(states, request.state) && request.next_owner_role === states[request.state].owner &&
        Number.isInteger(request.version) && request.version >= 0 && request.version <= eventCap &&
        typeof display?.ref === "string" && typeof display.title === "string" &&
        typeof display.total === "string" && /^\d+\.\d{2}$/.test(display.total) && Number.isFinite(Number(display.total)) &&
        Number.isFinite(Date.parse(request.updated_at)) &&
        (request.version === 0 ? event === null : event !== null && typeof event === "object" &&
          Object.hasOwn(actions, event.action) && typeof event.actor_label === "string" &&
          Number.isFinite(Date.parse(event.created_at)));
    });
  }
  async function load() {
    const sequence = ++loadSequence;
    working(true);
    message.dataset.failed = "false";
    message.textContent = requests === null ? "Loading requests…" : "Refreshing requests…";
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 25000);
    try {
      const response = await fetch("/api/requests", {credentials: "same-origin", cache: "no-store", signal: controller.signal});
      if (response.status === 401 || response.redirected || !(response.headers.get("content-type") || "").includes("application/json")) {
        throw {code: "SESSION_REFRESH"};
      }
      let data;
      try { data = await response.json(); } catch { throw {code: "SESSION_REFRESH"}; }
      if (!response.ok) throw {code: data?.error?.code || "TEMPORARILY_UNAVAILABLE"};
      if (!validResponse(data)) throw {code: "TEMPORARILY_UNAVAILABLE"};
      if (sequence !== loadSequence) return;
      requests = data.requests;
      render();
      freshness.textContent = `Last loaded ${dateTime.format(new Date())}.`;
      session.hidden = true;
      message.textContent = "Latest requests loaded.";
    } catch (error) {
      if (sequence !== loadSequence) return;
      const needsSession = ["SESSION_REFRESH", "UNAUTHENTICATED"].includes(error.code);
      session.hidden = !needsSession;
      message.dataset.failed = "true";
      message.textContent = requests === null ? "Could not load requests." : "Refresh failed; showing previously loaded requests.";
      message.textContent += needsSession ? " Refresh your sign-in, then reload the queue." : " Use Reload to try again.";
      if (requests === null) {
        rows.replaceChildren(emptyRow("Requests are unavailable until a successful load."));
        count.textContent = "Request counts are unavailable.";
      }
    } finally {
      clearTimeout(timer);
      if (sequence === loadSequence) working(false);
    }
  }
  search.addEventListener("input", render);
  status.addEventListener("change", render);
  owner.addEventListener("change", render);
  reset.addEventListener("click", resetFilters);
  cards.forEach(card => card.addEventListener("click", () => {
    status.value = card.dataset.filter;
    render();
  }));
  reload.addEventListener("click", () => { if (!busy) void load(); });
  window.addEventListener("pageshow", event => {
    resetFilters();
    if (event.persisted) void load();
  });
  resetFilters();
  void load();
})();
