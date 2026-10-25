"use strict";
// The demo UI script (RM23, spec 0.20 section 9). It is thin on purpose:
// the server renders and escapes every trace, and this script only
// fetches them and submits the controls to the native run API. The
// application token lives in this closure and nowhere else.
(() => {
  const POLL_MS = 1000;
  let token = "";
  const open = new Map(); // run_id -> the element its trace is refreshed in, while not terminal
  const approvalTokens = new Map(); // approval_id -> token, read once from the native view
  const lostTokens = new Set(); // approval_id whose token the native view no longer returns
  const tokenReads = new Map(); // approval_id -> the one native read in flight for it
  const submittedAt = new Map(); // run_id -> browser clock time of the submission
  const configurations = new Map(); // run_id -> its persistent configuration panel

  const byId = (id) => document.getElementById(id);
  const say = (text) => { byId("status").textContent = text; };
  const promptMessages = () => [{ role: "user", content: byId("prompt").value }];

  async function api(method, path, body) {
    const headers = { Authorization: "Bearer " + token };
    const init = { method, headers };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    return fetch(path, init);
  }

  async function errorText(response) {
    try {
      const body = await response.json();
      return body.error.code + ": " + body.error.message;
    } catch (_) {
      return "HTTP " + response.status;
    }
  }

  function slotFor(container, runId) {
    let slot = container.querySelector('[data-slot-run="' + runId + '"]');
    if (!slot) {
      slot = document.createElement("div");
      slot.dataset.slotRun = runId;
      container.prepend(slot);
    }
    return slot;
  }

  // The summary keeps the shell labels on screen next to the data mode
  // (spec 0.31 section 9); the other recorded options open below it.
  function fillPanel(item, runId, config) {
    const title = document.createElement("summary");
    const name = document.createElement("strong");
    name.textContent = "Run " + runId + " configuration";
    title.append(name);
    for (const label of config.querySelectorAll("[data-shell-label]")) {
      const shown = document.createElement("span");
      shown.textContent = label.textContent;
      title.append(shown);
    }
    const more = config.cloneNode(true);
    for (const label of more.querySelectorAll("[data-shell-label]")) label.remove();
    item.replaceChildren(title, more);
  }

  function showConfigurations() {
    const banner = byId("run-configurations");
    const visible = new Set();
    // One label per visible run also keeps comparison arms distinct (RM23).
    for (const trace of document.querySelectorAll("main .trace")) {
      const config = trace.querySelector("[data-run-configuration]");
      if (!config) continue;
      const runId = trace.dataset.runId;
      visible.add(runId);
      let panel = configurations.get(runId);
      if (!panel) {
        if (!configurations.size) banner.replaceChildren();
        const item = document.createElement("details");
        fillPanel(item, runId, config);
        panel = { item, html: config.innerHTML };
        configurations.set(runId, panel);
        banner.append(item);
      } else if (panel.html !== config.innerHTML) {
        fillPanel(panel.item, runId, config);
        panel.html = config.innerHTML;
      }
    }
    for (const [runId, panel] of configurations) {
      if (!visible.has(runId)) {
        panel.item.remove();
        configurations.delete(runId);
      }
    }
    if (!configurations.size && banner.textContent !== "No run selected.") {
      banner.textContent = "No run selected.";
    }
  }

  async function refresh(runId, container) {
    const response = await api("GET", "/ui/runs/" + encodeURIComponent(runId));
    if (!response.ok) {
      say(await errorText(response));
      open.delete(runId);
      return;
    }
    const html = await response.text();
    const slot = slotFor(container, runId);
    if (slot.dataset.last !== html) {
      slot.innerHTML = html; // the server escaped every value in html
      slot.dataset.last = html;
      const when = submittedAt.get(runId);
      const note = slot.querySelector('[data-slot="submitted"]');
      if (note && when) {
        // Two clocks, shown side by side and never subtracted (RM25).
        note.textContent = "You submitted the request (browser clock): " + when;
      }
    }
    const trace = slot.querySelector(".trace");
    showConfigurations();
    if (trace && trace.dataset.terminal === "true") open.delete(runId);
    if (trace) await prepareApproval(trace, slot);
  }

  async function prepareApproval(trace, slot) {
    const approve = trace.querySelector('[data-action="approve"]');
    if (!approve) return;
    const id = approve.dataset.approvalId;
    if (!approvalTokens.has(id) && !lostTokens.has(id)) {
      // The native view returns the token once; this is its only reader, and
      // a refresh that overlaps it waits for the same read instead of a second.
      if (!tokenReads.has(id)) {
        tokenReads.set(id, readToken(trace.dataset.runId, id).finally(() => tokenReads.delete(id)));
      }
      await tokenReads.get(id);
    }
    const note = slot.querySelector('[data-slot="token-note"]');
    const ready = approvalTokens.has(id);
    for (const button of slot.querySelectorAll('[data-action="approve"], [data-action="deny"]')) {
      button.disabled = !ready;
    }
    if (note && !ready) {
      note.textContent = "The confirmation token was already shown once. Cancel the run and retry.";
    }
  }

  async function readToken(runId, id) {
    const response = await api("GET", "/relay/v1/runs/" + runId);
    if (!response.ok) return;
    const view = await response.json();
    const pending = view.pending_approval;
    if (pending && pending.approval_id === id && pending.token) {
      approvalTokens.set(id, pending.token);
    } else {
      lostTokens.add(id);
    }
  }

  // Each open run is refreshed in the element it is shown in: the trace
  // list, or its column of a comparison.
  function watch(runId, container) {
    open.set(runId, container);
    return refresh(runId, container);
  }

  function track(runId) {
    return watch(runId, byId("traces"));
  }

  // The next poll is scheduled only after this one ends, so polls never overlap.
  async function poll() {
    for (const [runId, container] of Array.from(open)) {
      try { await refresh(runId, container); } catch (_) { say("Could not reach the gateway."); }
    }
    setTimeout(poll, POLL_MS);
  }

  async function submit(path, body, slot) {
    const response = await api("POST", path, body);
    if (!response.ok) {
      const message = slot && slot.querySelector('[data-slot="message"]');
      const text = await errorText(response);
      if (message) message.textContent = text; else say(text);
    }
    return response;
  }

  async function onClick(event) {
    const target = event.target.closest("[data-action]");
    if (!target || target.tagName === "FORM") return;
    const runId = target.dataset.runId;
    const version = Number(target.dataset.expectedVersion);
    const slot = target.closest("[data-slot-run]");
    const action = target.dataset.action;
    if (action === "cancel") {
      await submit("/relay/v1/runs/" + runId + "/cancel", { expected_version: version }, slot);
    } else if (action === "approve" || action === "deny") {
      const approvalId = target.dataset.approvalId;
      await submit("/relay/v1/runs/" + runId + "/resume", {
        expected_version: version,
        approval_id: approvalId,
        token: approvalTokens.get(approvalId),
        approve: action === "approve",
      }, slot);
    } else {
      return;
    }
    await watch(runId, slot.parentElement);
  }

  async function onSubmit(event) {
    const form = event.target;
    if (form.dataset.action !== "answer") return;
    event.preventDefault();
    const runId = form.dataset.runId;
    const slot = form.closest("[data-slot-run]");
    const input = form.elements.input.value;
    await submit("/relay/v1/runs/" + runId + "/resume", {
      expected_version: Number(form.dataset.expectedVersion),
      input,
    }, slot);
    await watch(runId, slot.parentElement);
  }

  async function startRun(event) {
    event.preventDefault();
    say("");
    const body = { messages: promptMessages(), execution_mode: byId("mode").value };
    const when = new Date().toISOString();
    const response = await submit("/relay/v1/runs", body, null);
    if (!response.ok) return;
    const created = await response.json();
    submittedAt.set(created.run_id, when);
    await track(created.run_id);
  }

  async function compare() {
    say("Comparing auto and external_only. This can take a while.");
    const when = new Date().toISOString();
    const response = await submit("/ui/comparisons", { messages: promptMessages() }, null);
    if (!response.ok) return;
    say("");
    const result = await response.json();
    const area = byId("comparison");
    area.replaceChildren();
    showConfigurations();
    const grid = document.createElement("div");
    grid.className = "compare";
    area.append(grid);
    for (const arm of result.arms) {
      const column = document.createElement("div");
      column.className = "arm";
      const heading = document.createElement("h2");
      heading.textContent = arm.execution_mode + ": " + arm.state;
      const reset = document.createElement("p");
      reset.textContent = "Fixtures reset before this run. Scenario: " + arm.fixture.scenario + ".";
      const holder = document.createElement("div");
      column.append(heading, reset, holder);
      grid.append(column);
      submittedAt.set(arm.run_id, when);
      // An arm still stopping stays open and is refreshed in its own column.
      await watch(arm.run_id, holder);
    }
    if (result.arms.length < 2) {
      const note = document.createElement("p");
      note.textContent = "The " + result.arms[0].execution_mode + " run did not stop in time, " +
        "so the comparison did not reset the sandbox or start the next run.";
      area.append(note);
    }
  }

  function openRun(event) {
    event.preventDefault();
    const id = byId("open-id").value.trim();
    if (id) track(id);
  }

  byId("token").addEventListener("input", (event) => { token = event.target.value; });
  byId("open-form").addEventListener("submit", openRun);
  const runForm = byId("run-form");
  if (runForm) runForm.addEventListener("submit", startRun);
  const compareButton = byId("compare");
  if (compareButton) compareButton.addEventListener("click", compare);
  document.addEventListener("click", onClick);
  document.addEventListener("submit", onSubmit);
  setTimeout(poll, POLL_MS);
})();
