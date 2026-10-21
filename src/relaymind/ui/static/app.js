"use strict";
// The demo UI script (RM23, spec 0.20 section 9). It is thin on purpose:
// the server renders and escapes every trace, and this script only
// fetches them and submits the controls to the native run API. The
// application token lives in this closure and nowhere else.
(() => {
  const POLL_MS = 1000;
  let token = "";
  const open = new Set();
  const approvalTokens = new Map(); // approval_id -> token, read once from the native view
  const lostTokens = new Set(); // approval_id whose token the native view no longer returns
  const submittedAt = new Map(); // run_id -> browser clock time of the submission

  const byId = (id) => document.getElementById(id);
  const say = (text) => { byId("status").textContent = text; };

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
    if (trace && trace.dataset.terminal === "true") open.delete(runId);
    if (trace) await prepareApproval(trace, slot);
  }

  async function prepareApproval(trace, slot) {
    const approve = trace.querySelector('[data-action="approve"]');
    if (!approve) return;
    const id = approve.dataset.approvalId;
    if (!approvalTokens.has(id) && !lostTokens.has(id)) {
      // The native view returns the token once; this is its only reader.
      const response = await api("GET", "/relay/v1/runs/" + trace.dataset.runId);
      if (response.ok) {
        const view = await response.json();
        const pending = view.pending_approval;
        if (pending && pending.approval_id === id && pending.token) {
          approvalTokens.set(id, pending.token);
        } else {
          lostTokens.add(id);
        }
      }
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

  function track(runId) {
    open.add(runId);
    return refresh(runId, byId("traces"));
  }

  async function poll() {
    for (const runId of Array.from(open)) {
      try { await refresh(runId, byId("traces")); } catch (_) { say("Could not reach the gateway."); }
    }
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
    open.add(runId);
    await refresh(runId, slot.parentElement);
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
    open.add(runId);
    await refresh(runId, slot.parentElement);
  }

  async function startRun(event) {
    event.preventDefault();
    say("");
    const body = {
      messages: [{ role: "user", content: byId("prompt").value }],
      execution_mode: byId("mode").value,
    };
    const when = new Date().toISOString();
    const response = await submit("/relay/v1/runs", body, null);
    if (!response.ok) return;
    const created = await response.json();
    submittedAt.set(created.run_id, when);
    await track(created.run_id);
  }

  async function compare() {
    say("Comparing auto and external_only. This can take a while.");
    const body = { messages: [{ role: "user", content: byId("prompt").value }] };
    const when = new Date().toISOString();
    const response = await submit("/ui/comparisons", body, null);
    if (!response.ok) return;
    say("");
    const result = await response.json();
    const area = byId("comparison");
    area.replaceChildren();
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
      await refresh(arm.run_id, holder);
      open.delete(arm.run_id);
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
  setInterval(poll, POLL_MS);
})();
