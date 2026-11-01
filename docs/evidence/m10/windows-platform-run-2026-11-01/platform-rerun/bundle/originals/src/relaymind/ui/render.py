"""HTML for the demo UI (RM23, RM25, spec 0.20 section 9).

The server renders the shell and every trace, so the rules of the page
are tested in Python and the page script stays thin. Every value is
escaped: tool output, model text, and prompts are data, never markup
(RM12). The mode label is part of the shell and of every trace, so a
pasted trace keeps it (RM23). Controls are plain elements with data
attributes; the script submits them to the native endpoints and adds
nothing the trace did not show.
"""

import html
import json
from collections.abc import Mapping, Sequence
from typing import Any

from relaymind.orchestrator.run_state import TERMINAL_STATES, RunState
from relaymind.ui.settings import MODE_LABELS, UiSettings

__all__ = ["render_shell", "render_trace"]

_ESCAPE = html.escape
_NONE = '<p class="none">{}</p>'
_EXECUTION_MODES = ("auto", "local_only", "external_only", "parallel_demo")


def _t(value: object) -> str:
    return _ESCAPE("" if value is None else str(value))


def _json(value: object) -> str:
    return _ESCAPE(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _error(value: object) -> str:
    """An error object (code and message, section 8) as text; anything else as it is."""
    if isinstance(value, Mapping) and "code" in value:
        message = value.get("message")
        return _t(value["code"]) + (f": {_t(message)}" if message else "")
    return _t(value)


def _recorded(item: Mapping[str, Any], key: str, null: str = "none") -> object:
    """A recorded value, its meaning when it was recorded as null, or not recorded (RM23).

    An absent key is evidence the event never held (an older run); a null
    is evidence the event did hold, such as no reason or no selection.
    """
    if key not in item:
        return "not recorded"
    value = item[key]
    return null if value is None else value


def _recorded_ms(item: Mapping[str, Any], key: str, null: str = "none") -> str:
    """A recorded time in ms, with the same three cases as _recorded."""
    if key not in item:
        return "not recorded"
    return _ms(item[key], null)


def _attrs(**values: object) -> str:
    return " ".join(f'{name.replace("_", "-")}="{_t(value)}"' for name, value in values.items())


def mode_banner(settings: UiSettings) -> str:
    """The persistent data mode label (RM23)."""
    word, sentence = MODE_LABELS[settings.mode]
    return (
        f'<div class="mode-label" role="status" data-ui-mode="{settings.mode.value}">'
        f"<strong>{word}</strong> {_t(sentence)}</div>"
    )


def sandbox_line(settings: UiSettings) -> str:
    """The sandbox data service label, when the operator configured one (RM23)."""
    if settings.fixtures is None:
        return ""
    return (
        f'<p class="sandbox">Sandbox data service: {_t(settings.fixtures.label)} '
        f"Scenario: {_t(settings.fixtures.scenario)}</p>"
    )


def render_shell(settings: UiSettings) -> str:
    """The static page: the label, the token field, and the empty areas the script fills."""
    forms = ""
    if settings.interactive:
        options = "".join(f'<option value="{m}">{m}</option>' for m in _EXECUTION_MODES)
        compare = ""
        if settings.fixtures is not None:
            compare = (
                '<button type="button" id="compare">Compare auto and external_only</button>'
                '<span class="hint">Resets the sandbox before each run.</span>'
            )
        forms = (
            '<form id="run-form"><label>Prompt<textarea id="prompt" rows="3" required>'
            "</textarea></label>"
            f'<label>Execution mode<select id="mode">{options}</select></label>'
            '<button type="submit">Start run</button>'
            f"{compare}</form>"
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>RelayMind demo trace</title>"
        '<link rel="stylesheet" href="/ui/app.css"></head><body>'
        f'<header class="top">{mode_banner(settings)}'
        "<h1>RelayMind demo trace</h1>"
        f"{sandbox_line(settings)}"
        '<label class="token">Application token<input id="token" type="password" '
        'autocomplete="off" spellcheck="false"></label></header>'
        '<aside id="run-configurations" aria-label="Run configurations" '
        'aria-live="polite">No run selected.</aside>'
        f"<main>{forms}"
        '<form id="open-form"><label>Open a run by ID<input id="open-id" type="text" '
        'autocomplete="off" spellcheck="false"></label><button type="submit">Open</button></form>'
        '<p id="status" role="alert"></p>'
        '<div id="comparison"></div><div id="traces" aria-live="polite"></div></main>'
        '<script src="/ui/app.js"></script></body></html>'
    )


def _section(title: str, body: str, css: str) -> str:
    return f'<section class="{css}"><h3>{_t(title)}</h3>{body}</section>'


def _prompt(view: Mapping[str, Any]) -> str:
    messages = view["prompt"]
    if messages is None:
        return _NONE.format("Not recorded: this run was stored before the prompt was recorded.")
    items = "".join(
        f'<li><span class="role">{_t(m["role"])}</span> <q>{_t(m["content"])}</q></li>'
        for m in messages
    )
    return f"<ol>{items}</ol>"


def _mode(view: Mapping[str, Any]) -> str:
    mode = view["execution_mode"]
    requested = mode["requested"] or "not set (auto applies)"
    return f"<p>Requested: {_t(requested)}. Effective: {_t(mode['effective'])}.</p>"


_OPTION_LABELS = (
    ("max_completion_tokens", "Token budget"),
    ("temperature", "Temperature"),
    ("top_p", "Top p"),
)


def _answer_options(view: Mapping[str, Any]) -> str:
    options = view["answer_options"]
    if options is None:
        return _NONE.format(
            "Not recorded: this run was stored before answer options were recorded."
        )
    if not options:
        return _NONE.format("No answer options were requested.")
    return (
        "<p>"
        + " ".join(
            f"{label}: {_t(options[name])}." for name, label in _OPTION_LABELS if name in options
        )
        + "</p>"
    )


# The labels the shell keeps on screen next to the data mode (spec 0.31 section 9).
_SHELL_LABELS = frozenset(
    {
        "local_model",
        "local_model_status",
        "local_model_reason",
        "placement_policy",
        "answer_pass",
        "answer_stream",
    }
)


def _configuration(view: Mapping[str, Any]) -> str:
    """Only this run's recorded settings, including explicit absence (RM23, RM26)."""
    config = view.get("configuration")
    if not isinstance(config, Mapping):
        config = {}
    labels = (
        ("local_model", "Local model"),
        ("local_model_status", "Local model status"),
        ("local_model_reason", "Local model reason"),
        ("placement_policy", "Placement policy"),
        ("answer_pass", "Answer pass"),
        ("answer_stream", "Answer stream"),
        ("calls_per_generation", "Calls per generation"),
        ("prefetch", "Prefetch"),
        ("early_headers", "Early headers"),
        ("selection", "Selector chain"),
    )
    parts = []
    for key, label in labels:
        # A null local model means none was configured (RM26).
        value = _recorded(config, key, "none configured" if key == "local_model" else "none")
        if isinstance(value, bool):
            value = "on" if value else "off"
        elif isinstance(value, list):
            value = ", ".join(str(item) for item in value) or "none"
        shell = " data-shell-label" if key in _SHELL_LABELS else ""
        parts.append(f"<p{shell}>{label}: {_t(value)}</p>")
    return "<div data-run-configuration>" + "".join(parts) + "</div>"


def _models(view: Mapping[str, Any]) -> str:
    active = view["active_model"]
    head = f"<p>Active model: <strong>{_t(active) if active else 'none yet'}</strong></p>"
    if not view["model_calls"]:
        return head + _NONE.format("No model call yet.")
    branched = view["workflow"] is not None
    rows = "".join(
        "<tr>"
        f"<td>{_t(c['purpose'])}</td>"
        + (f"<td>{_t(c['branch'])}</td>" if branched else "")
        + f"<td>{_t(c['actor'])}</td><td>{_t(c['model'])}</td>"
        f"<td>{_call_status(c)}</td><td>{_first_token(c)}</td>"
        f"<td>Predicted: {_recorded_ms(c, 'predicted_ms')}; actual: {_call_duration(c)}</td></tr>"
        for c in view["model_calls"]
    )
    branch_head = "<th>Branch</th>" if branched else ""
    return (
        f"{head}<table><thead><tr><th>Purpose</th>{branch_head}<th>Actor</th><th>Model</th>"
        f"<th>Result</th><th>First token</th><th>Placement timing</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def _workflow(view: Mapping[str, Any]) -> str:
    """The parallel workflow: each branch and the synthesis call as its own actor (RM23)."""
    workflow = view["workflow"]
    # The figures come from the terminal event; a run ended without them
    # (restart recovery) shows that they were not recorded (section 12).
    unknown = "not recorded" if workflow["ended"] else "known when the run ends"
    recorded = workflow["branch_wall_ms"] is not None
    no_synthesis = "no synthesis call" if recorded else unknown
    rows = "".join(
        f'<tr data-branch="{_t(s["branch"])}"><td>{_t(s["branch"])}</td><td>{_t(s["actor"])}</td>'
        f"<td>{_t(s['model'])}</td>"
        f"<td>{'finished' if s['finished'] else 'running'}"
        + (f", {_error(s['outcome'])}" if s["outcome"] else "")
        + f"</td><td>{_ms(s['duration_ms'], unknown)}</td></tr>"
        for s in workflow["stages"]
    )
    abandoned = workflow["abandoned_calls"]
    figures = (
        "<dl>"
        f"<dt>Extra call latency</dt><dd>{_ms(workflow['extra_call_ms'], no_synthesis)}"
        "</dd>"
        f"<dt>Parallel stage</dt><dd>{_ms(workflow['branch_wall_ms'], unknown)}"
        "</dd>"
        f"<dt>Abandoned calls</dt><dd>{_t(', '.join(abandoned)) if abandoned else 'none'}</dd>"
        "</dl>"
    )
    return (
        f"<p>Workflow: <strong>{_t(workflow['workflow_id'] or 'not recorded')}</strong></p>"
        "<table><thead><tr><th>Stage</th><th>Actor</th><th>Model</th><th>State</th>"
        f"<th>Duration</th></tr></thead><tbody>{rows}</tbody></table>{figures}"
        '<p class="note">The extra call latency is the added cost of the second external '
        "call, measured on the gateway's monotonic clock. It is not a saving against a "
        "sequential run.</p>"
    )


def _first_token(call: Mapping[str, Any]) -> str:
    """first_token_ms of model.finished; null means no token arrived (spec 0.31)."""
    if not call["finished"]:
        return "known when the call ends"
    return _recorded_ms(call, "first_token_ms", "no token")


def _call_duration(call: Mapping[str, Any]) -> str:
    if not call["finished"]:
        return "known when the call ends"
    return _ms(call["duration_ms"], "not recorded")


def _call_status(call: Mapping[str, Any]) -> str:
    if not call["finished"]:
        return "running"
    error = f", error: {_error(call['error'])}" if call["error"] else ""
    return f"{_t(call['finish_reason'])}{error}"


def _selector(selector: object) -> str:
    """Incomplete evidence stays readable; a null means the chain chose nothing (RM23, RM28)."""
    if not isinstance(selector, Mapping):
        return "<p>Selector: not recorded</p>"
    attempts = selector.get("attempts")
    if isinstance(attempts, list):
        attempts_text = (
            "; ".join(
                f"{attempt.get('name', 'not recorded')}: {attempt.get('outcome', 'not recorded')}"
                if isinstance(attempt, Mapping)
                else "not recorded"
                for attempt in attempts
            )
            or "none"
        )
    else:
        attempts_text = "not recorded"
    return (
        f"<p>Selector: {_t(_recorded(selector, 'name', 'none selected'))}; "
        f"version: {_t(_recorded(selector, 'version'))}; "
        f"mechanism: {_t(_recorded(selector, 'mechanism'))}; "
        f"elapsed: {_recorded_ms(selector, 'elapsed_ms')}. "
        f"Attempts: {_t(attempts_text)}</p>"
    )


def _skill_and_route(view: Mapping[str, Any]) -> str:
    skill = view["skill"]
    skill_text = (
        _NONE.format("No skill loaded.")
        if skill is None
        else f"<p>Skill: <strong>{_t(skill['name'])}</strong> version {_t(skill['version'])} "
        f"<code>{_t(skill['content_hash'])}</code></p>"
    )
    if view["route"] is None:
        return skill_text + _NONE.format("No route decision yet.")
    parts = [skill_text]
    for route in view["routes"]:
        parts.append(
            f"<p>Route: <strong>{_t(route['route'])}</strong>, skill {_t(route['skill_id'])}, "
            f"reason {_t(route['reason_code'])}, mechanism {_t(route['mechanism'])}.</p>"
        )
        parts.append(_selector(route.get("selector")))
        parts.append(f"<p>Placement: {_placement(route)}</p>")
        actual = _route_actual(route)
        parts.append(f"<p>Predicted: {_route_prediction(route)}; actual: {actual}</p>")
    return "".join(parts)


def _placement(route: Mapping[str, Any]) -> str:
    """Absent: not recorded. Null: the route recorded no placement (RM27)."""
    if "placement" not in route:
        return "not recorded"
    placement = route["placement"]
    return "none" if placement is None else f"<code>{_json(placement)}</code>"


def _route_prediction(route: Mapping[str, Any]) -> str:
    if "placement" not in route:
        return "not recorded"
    placement = route["placement"]
    return "none" if placement is None else _recorded_ms(placement, "predicted_ms")


def _route_actual(route: Mapping[str, Any]) -> str:
    """The measured time of the route's own routing call, joined by call ID (RM27)."""
    if route.get("call_id") is None:
        # A gateway decision names no model; a routing call without its ID is older evidence.
        return "no routing call" if route.get("model") is None else "not recorded"
    finished = route.get("call_finished")
    if finished is None:
        return "not recorded"
    if not finished:
        return "known when the call ends"
    return _ms(route.get("duration_ms"), "not recorded")


def _branch(item: Mapping[str, Any]) -> str:
    """The workflow branch of a proposal or operation, when it has one (spec 0.25)."""
    return f", branch {_t(item['branch'])}" if item.get("branch") else ""


def _proposals(view: Mapping[str, Any]) -> str:
    if not view["proposals"]:
        return _NONE.format("No tool proposed.")
    items = []
    for p in view["proposals"]:
        verdict = (
            "admissible"
            if p["admissible"]
            else f"rejected: {_t(p['rejection'] and p['rejection'].get('code'))}"
        )
        # A call the gateway made itself (prefetch, RM29) names no model: say so.
        who = (
            f"proposed by {_t(p['model'])} ({_t(p['actor'])})"
            if p["model"] is not None
            else f"made by the {_t(p['actor'])} ({_t(p['mechanism'])})"
        )
        items.append(
            f"<li><strong>{_t(p['tool_name'])}</strong> {who}"
            f"{_branch(p)}, {verdict}<pre>{_json(p['arguments'])}</pre></li>"
        )
    return f"<ul>{''.join(items)}</ul>"


def _operation(op: Mapping[str, Any]) -> str:
    replay = " (replayed from its record)" if op["replayed"] else ""
    error = f"<p>Error: {_error(op['error'])}</p>" if op["error"] else ""
    data = f"<pre>{_json(op['data'])}</pre>" if op["data"] is not None else ""
    return (
        f'<li class="op {_t(op["status"])}"><strong>{_t(op["tool_name"])}</strong> '
        f'<span class="status">{_t(op["status"])}</span>{replay}{_branch(op)}, '
        f"{_ms(op['duration_ms'], 'duration not recorded')}, source {_t(op['source_ref'])}"
        f"{_reference(op)}{error}{data}</li>"
    )


def _reference(op: Mapping[str, Any]) -> str:
    """A result stored by reference, with its size (spec 0.30 section 9)."""
    ref = op.get("result_ref")
    if not ref:
        return ""
    kept = "" if op["result_retained"] else "; the stored result is not retained"
    return (
        f"<p>Result stored by reference: {_t(ref['artifact_id'])}, "
        f"{_t(ref['size_bytes'])} bytes{kept}</p>"
    )


def _operation_list(operations: Sequence[Mapping[str, Any]], empty: str) -> str:
    if not operations:
        return _NONE.format(empty)
    return f"<ul>{''.join(_operation(op) for op in operations)}</ul>"


def _operations(view: Mapping[str, Any]) -> str:
    return _operation_list(view["operations"], "No tool has run.")


def _failed(view: Mapping[str, Any]) -> str:
    return _operation_list(view["failed_operations"], "None.")


def _escalation(view: Mapping[str, Any]) -> str:
    esc = view["escalation"]
    if esc is None:
        reason = ""
        if view["state"] == "failed" and view["reason_code"]:
            reason = f" Run stopped with reason {_t(view['reason_code'])}."
        return _NONE.format("No handoff to an external model." + reason)
    degraded = (
        f" Degraded mode: local model failed ({_t(esc['local_failure'])})."
        if esc["degraded_mode"]
        else ""
    )
    return (
        f"<p>Handed to <strong>{_t(esc['model'])}</strong>, reason "
        f"<strong>{_t(esc['reason_code'])}</strong>.{degraded}</p>"
    )


def _clarifications(view: Mapping[str, Any]) -> str:
    if not view["clarifications"]:
        return _NONE.format("No clarification asked.")
    items = "".join(
        f"<li>Question: {_t(c['question'])} (needs {_t(c['missing'])})"
        + (f"<br>Your answer: {_t(c['answer'])}" if c["answer"] is not None else "")
        + "</li>"
        for c in view["clarifications"]
    )
    return f"<ul>{items}</ul>"


def _confirmation(view: Mapping[str, Any]) -> str:
    c = view["confirmation"]
    if c is None:
        return _NONE.format("No confirmation asked.")
    return (
        f"<p>Status: <strong>{_t(c['status'])}</strong>. Tool {_t(c['tool_name'])} on target "
        f"<strong>{_t(c['target'])}</strong>, proposed by {_t(c['model'])}, "
        f"expires {_t(c['expires_at'])}.</p><pre>{_json(c['arguments'])}</pre>"
    )


def _answer(view: Mapping[str, Any]) -> str:
    answer = view["final_answer"]
    if answer is None:
        return _NONE.format("No answer published.")
    return (
        f'<p class="answer-text">{_t(answer["text"])}</p><p>Answered by {_t(answer["model"])}.</p>'
    )


def _ms(value: int | None, missing: str) -> str:
    return _t(missing) if value is None else f"{_t(value)} ms"


def _timing(view: Mapping[str, Any]) -> str:
    timing = view["timing"]
    return (
        "<dl>"
        "<dt>First model action</dt>"
        f"<dd>{_ms(timing['first_model_action_ms'], 'not seen yet')}</dd>"
        f"<dt>Active time</dt><dd>{_ms(timing['active_ms'], 'known when the run ends')}</dd>"
        f"<dt>Waiting time</dt><dd>{_ms(timing['waiting_ms'], 'known when the run ends')}</dd>"
        f"<dt>Server accepted the run (server clock)</dt><dd>{_t(view['accepted_at'])}</dd>"
        "</dl>"
        '<p data-slot="submitted"></p>'
        '<p class="note">The first figure is model action latency: the time until a model '
        "first produced a route, a question, or a tool call. It is not the time to the final "
        "answer. Active and waiting time are run totals, shown apart from it.</p>"
    )


def _controls(view: Mapping[str, Any], settings: UiSettings) -> str:
    state = RunState(view["state"])
    if not settings.interactive or state in TERMINAL_STATES:
        return ""
    base = _attrs(data_run_id=view["run_id"], data_expected_version=view["state_version"])
    parts = []
    if state is RunState.WAITING_INPUT:
        parts.append(
            f'<form data-action="answer" {base}><label>Your answer<input name="input" '
            'type="text" autocomplete="off" required></label>'
            '<button type="submit">Send answer</button></form>'
        )
    elif state is RunState.WAITING_CONFIRMATION and view["confirmation"] is not None:
        # Disabled until the page script holds the token from the native view (spec 0.27).
        approval = _attrs(data_approval_id=view["confirmation"]["approval_id"]) + " disabled"
        parts.append(
            f'<button type="button" data-action="approve" {base} {approval}>Approve</button>'
            f'<button type="button" data-action="deny" {base} {approval}>Deny</button>'
            '<p data-slot="token-note"></p>'
        )
    parts.append(f'<button type="button" data-action="cancel" {base}>Cancel run</button>')
    return _section("Controls", "".join(parts), "controls")


def render_trace(view: Mapping[str, Any], settings: UiSettings) -> str:
    """One run's trace as an HTML fragment; every value is escaped."""
    state = RunState(view["state"])
    terminal = state in TERMINAL_STATES
    if view["answer_pass_applied"]:
        answer_pass = view["answer_pass_applied"]
    elif not terminal:
        answer_pass = "known when the run ends"
    elif state is RunState.COMPLETED:
        answer_pass = "not recorded"
    else:
        # Only run.completed carries answer_pass_applied (spec 0.32).
        answer_pass = "none, the run did not complete"
    codes = ""
    if view["error_code"]:
        codes += f" Error code: {_t(view['error_code'])}."
    if view["reason_code"]:
        codes += f" Reason code: {_t(view['reason_code'])}."
    if view["cancel_reason"]:
        codes += f" Cancel reason: {_t(view['cancel_reason'])}."
    attributes = _attrs(
        data_run_id=view["run_id"],
        data_state=view["state"],
        data_state_version=view["state_version"],
        data_ui_mode=settings.mode.value,
        data_terminal="true" if terminal else "false",
    )
    sections: Sequence[str] = (
        _section("Original prompt", _prompt(view), "prompt"),
        _section("Execution mode", _mode(view), "mode"),
        _section("Answer options", _answer_options(view), "options"),
        _section("Run configuration", _configuration(view), "configuration"),
        _section("Models", _models(view), "models"),
        *(
            [_section("Parallel workflow", _workflow(view), "workflow")]
            if view["workflow"] is not None
            else []
        ),
        _section("Skill and route", _skill_and_route(view), "skill"),
        _section("Tool proposals", _proposals(view), "proposals"),
        _section("Tool operations", _operations(view), "operations"),
        _section("Failed operations", _failed(view), "failed"),
        _section("Escalation", _escalation(view), "escalation"),
        _section("Clarification", _clarifications(view), "clarification"),
        _section("Confirmation", _confirmation(view), "confirmation"),
        _section(
            "Final answer",
            _answer(view) + f"<p>Answer pass applied: {_t(answer_pass)}</p>",
            "answer",
        ),
        _section("Timing", _timing(view), "timing"),
    )
    return (
        f'<article class="trace" {attributes}>{mode_banner(settings)}{sandbox_line(settings)}'
        f"<header><h2>Run <code>{_t(view['run_id'])}</code></h2>"
        f"<p>State: <strong>{_t(view['state'])}</strong> (version {_t(view['state_version'])})."
        f"{codes}</p></header>{''.join(sections)}{_controls(view, settings)}"
        '<p data-slot="message" role="alert"></p></article>'
    )
