"""Guards of the #260 owner-run scripts (spec 0.66 sections 12.6.5, 12.6.6).

RM16, RM40, RM44; AC36. The host runner dispatches only prompts of the
verified frozen corpus its preregistered tasks and freeze name; the success
judging needs complete, valid raw judgments from both machine judges before
adjudication and before any verdict is applied; and the external-token cap is
enforced before a row starts and while it runs, charging unknown consumption
at its worst case. No model or host is called: fixtures and doubles only.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from relaymind.canonical import canonical_digest
from whole_task_helpers import expanded_release

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "evaluation" / "assets" / "owner-run-260"


def _module(name: str):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(
            f"owner_run_260_{name}", SCRIPTS / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


run_host = _module("run_host")
judge = _module("judge")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def relock(root: Path) -> None:
    lock, labels = read(root / "lock.json"), read(root / "labels.json")
    labels["cases_digest"] = canonical_digest(read(root / "cases.json"))
    write(root / "labels.json", labels)
    for name in ("catalog", "cases", "labels"):
        lock[name]["digest"] = canonical_digest(read(root / f"{name}.json"))
    lock.pop("digest")
    lock["digest"] = canonical_digest(lock)
    write(root / "lock.json", lock)


def frozen(tmp_path):
    root = expanded_release(tmp_path / "release", 3)
    case = read(root / "cases.json")["cases"][0]
    digest = canonical_digest(read(root / "lock.json"))
    tasks = dict(
        corpus_lock_digest=digest,
        tasks=[
            dict(
                id="t1",
                lane=case["lane"],
                family_ids=case["task_family_ids"],
                prompt_case_id=case["id"],
                asset_bindings=[],
            )
        ],
    )
    return root, tasks, dict(corpus_lock_digest=digest), case


# --- finding 1: prompts come only from the verified frozen corpus -----------------


def test_prompts_come_from_the_verified_lock_the_tasks_and_freeze_name(tmp_path):
    root, tasks, freeze, case = frozen(tmp_path)
    prompts = run_host.verified_prompts(tasks, freeze, root / "lock.json", root)
    assert prompts == {case["id"]: case["task_text"]}


def test_an_edited_cases_file_with_the_same_ids_is_rejected(tmp_path):
    """Same case IDs, different prompt text: the lock's cases digest no longer matches."""
    root, tasks, freeze, _ = frozen(tmp_path)
    cases = read(root / "cases.json")
    cases["cases"][0]["task_text"] += " (edited)"
    write(root / "cases.json", cases)
    with pytest.raises(ValueError):
        run_host.verified_prompts(tasks, freeze, root / "lock.json", root)


def test_a_relocked_corpus_is_not_the_preregistered_one(tmp_path):
    """A consistent new lock still differs from the digest the tasks were frozen with."""
    root, tasks, freeze, _ = frozen(tmp_path)
    cases = read(root / "cases.json")
    cases["cases"][0]["task_text"] += " (edited)"
    write(root / "cases.json", cases)
    relock(root)
    with pytest.raises(run_host.CorpusMismatch, match="corpus_lock_mismatch"):
        run_host.verified_prompts(tasks, freeze, root / "lock.json", root)


def test_tasks_and_freeze_must_name_the_same_corpus(tmp_path):
    root, tasks, _, _ = frozen(tmp_path)
    with pytest.raises(run_host.CorpusMismatch, match="corpus_lock_mismatch"):
        run_host.verified_prompts(
            tasks, dict(corpus_lock_digest="0" * 64), root / "lock.json", root
        )


# --- finding 3: the external-token cap bounds spending ---------------------------


def budget(tmp_path, spent=0, cap=1_000_000, row=200_000, output=8_000):
    ledger = tmp_path / "ledger.json"
    write(ledger, dict(total_input=spent, total_output=0, stages={}))
    return run_host.Budget(ledger, cap=cap, row_budget=row, max_output=output)


def test_a_row_starts_only_when_its_whole_reservation_fits(tmp_path):
    assert budget(tmp_path, spent=800_000).can_start()
    assert not budget(tmp_path, spent=800_001).can_start()


def test_a_settled_row_is_charged_its_gated_consumption(tmp_path):
    """The gate's charge (reported usage, or the bound of a call without usage) is settled."""
    b = budget(tmp_path)
    assert b.reserve("t1-provider_only") is None
    b.settle("t1-provider_only", "provider_only", prompt=10_000, completion=1_000, charged=61_000)
    assert b.spent() == 61_000
    saved = read(tmp_path / "ledger.json")
    assert saved["total_input"] + saved["total_output"] == b.spent()
    # Reported usage above the gate's charge is never under-recorded.
    assert b.reserve("t2-native") is None
    b.settle("t2-native", "native", prompt=5_000, completion=500, charged=0)
    assert b.spent() == 66_500


def test_the_watchdog_stops_a_host_turn_at_the_row_limit(tmp_path):
    used = iter([0, 60_000, 120_000, 160_000, 400_000])
    result = run_host.run_bounded(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        timeout=20,
        usage=lambda: next(used),
        limit_reached=lambda tokens: tokens >= 150_000,
        poll=0.05,
    )
    assert result["status"] == "budget_stopped"
    assert result["row_tokens_at_stop"] == 160_000
    assert result["elapsed_ms"] < 10_000


def test_the_watchdog_keeps_an_ordinary_turn(tmp_path):
    result = run_host.run_bounded(
        [sys.executable, "-c", "print('{\"ok\": true}')"],
        env=None,
        timeout=20,
        usage=lambda: 0,
        limit_reached=lambda tokens: False,
        poll=0.05,
    )
    assert (result["status"], result["exit_code"]) == ("completed", 0)
    assert json.loads(result["stdout"]) == {"ok": True}


# --- finding 2: two complete raw judgments before adjudication and apply --------


def items(*ids):
    return {i: dict(blind_id=i) for i in ids}


def verdict(success=True):
    return dict(success=success, notes="n")


def test_adjudication_needs_a_valid_judgment_from_both_judges_for_every_item():
    blind = items("x1", "x2")
    full = {"x1": verdict(), "x2": verdict(False)}
    judge.check_judges(blind, full, full)
    for a, b, reason in [
        (full, {}, "judge_b_incomplete"),
        ({"x1": verdict()}, full, "judge_a_incomplete"),
        (full, {"x1": verdict(), "x2": dict(success="yes", notes="")}, "judge_b_invalid"),
        (full, dict(full, x3=verdict()), "judge_b_unknown_item"),
    ]:
        with pytest.raises(judge.JudgingIncomplete, match=reason):
            judge.check_judges(blind, a, b)


def judgments(a, b, final):
    return dict(
        format="relaymind.owner_run_success_judgments.v1",
        judges=dict(a=a, b=b),
        disputed=sorted(i for i in a if i in b and a[i]["success"] != b[i]["success"]),
        final=final,
    )


def test_apply_rechecks_both_judges_and_every_final_verdict():
    mapping = {
        "x1": dict(task_id="t1", arm="native"),
        "x2": dict(task_id="t1", arm="provider_only"),
    }
    a = {"x1": verdict(), "x2": verdict()}
    b = {"x1": verdict(), "x2": verdict(False)}
    final = {"x1": dict(success=True, reason="agree"), "x2": dict(success=False, reason="r")}
    judge.check_judgments(judgments(a, b, final), mapping)
    bad = [
        (judgments(a, {}, final), "judge_b_incomplete"),
        (judgments(a, b, {"x1": final["x1"]}), "final_incomplete"),
        # An agreed item cannot be overturned by the adjudication rule.
        (judgments(a, b, dict(final, x1=dict(success=False, reason="r"))), "final_contradicts"),
        # A disputed item needs an adjudication reason.
        (judgments(a, b, dict(final, x2=dict(success=False, reason=" "))), "final_unreasoned"),
    ]
    for document, reason in bad:
        with pytest.raises(judge.JudgingIncomplete, match=reason):
            judge.check_judgments(document, mapping)


# --- review of #278 ---------------------------------------------------------------

record_script = _module("record")
run_episodes = _module("run_episodes")
EVIDENCE = ROOT / "docs" / "evidence" / "m10"
CONFIGS = SCRIPTS / "configs"
FREEZE = read(SCRIPTS / "host-freeze.json")
MODEL = "glm-5.3-flash"


def test_every_committed_owner_run_260_record_loads_against_its_schema():
    """H1 (spec 0.66 section 12.6.6; AC33, AC36): the records validate and bind."""
    from relaymind.evaluation.asset_qualification_record import load_record

    records = sorted(EVIDENCE.glob("m10-owner-run-260-*.json"))
    assert records
    for path in records:
        assert load_record(path).record_id == path.stem


def test_the_record_machine_takes_its_os_version_from_the_measured_environment():
    """H1: the record's machine identity carries the report environment's os_version."""
    machine = dict(role="linux_host", os="Linux", architecture="x86_64")
    environment = dict(os="Linux", architecture="x86_64", os_version="Linux-7")
    assert record_script.machine_identity(machine, environment)["os_version"] == "Linux-7"
    with pytest.raises(ValueError, match="record_machine_mismatch"):
        record_script.machine_identity(dict(machine, os_version="Linux-8"), environment)
    with pytest.raises(ValueError, match="record_machine_mismatch"):
        record_script.machine_identity(dict(machine, os="Darwin"), environment)


def prepared(tmp_path) -> Path:
    target = tmp_path / "prepared"
    target.mkdir()
    for name in ("effective-off.json", "effective-on.json", "adaptation-config.json"):
        (target / name).write_bytes((SCRIPTS / "episode-configs" / name).read_bytes())
    (target / "episode-freeze.json").write_bytes((SCRIPTS / "episode-freeze.json").read_bytes())
    return target


def test_the_episode_run_executes_exactly_the_frozen_adaptation_config(tmp_path):
    """H2 (RM42; AC33): the executed adaptation config and manifest are the frozen ones."""
    semantic, manifest = run_episodes.frozen_adaptation(
        prepared(tmp_path), ROOT / "models" / "embeddings"
    )
    frozen = read(SCRIPTS / "episode-configs" / "adaptation-config.json")
    assert manifest.manifest_digest == frozen["manifest_digest"]
    assert (
        canonical_digest(frozen)
        == read(SCRIPTS / "episode-freeze.json")["adaptation_config_digest"]
    )
    assert semantic is not None


def test_a_run_local_manifest_is_not_the_frozen_one(tmp_path):
    """H2: a manifest with another runtime identity needs a new preregistration."""
    models = tmp_path / "models"
    models.mkdir()
    name = "multilingual-e5-small-linux-x86_64.manifest.json"
    manifest = read(ROOT / "models" / "embeddings" / name)
    manifest["runtime"] = dict(manifest["runtime"], build="0" * 64)
    write(models / name, manifest)
    with pytest.raises(ValueError, match="adaptation_manifest_mismatch"):
        run_episodes.frozen_adaptation(prepared(tmp_path), models)


def test_an_edited_adaptation_config_is_not_the_frozen_one(tmp_path):
    root = prepared(tmp_path)
    value = read(root / "adaptation-config.json")
    value["similarity"]["threshold"] = 0.8
    write(root / "adaptation-config.json", value)
    with pytest.raises(ValueError, match="adaptation_config_mismatch"):
        run_episodes.frozen_adaptation(root, ROOT / "models" / "embeddings")


def test_the_host_freeze_model_and_config_digests_are_enforced(tmp_path):
    """M1 (spec 0.66 section 12.6.5; AC36): the runner executes only the frozen arms."""
    frozen = run_host.frozen_arm_configs(FREEZE, CONFIGS, MODEL)
    assert set(frozen) == {"native", "provider_only"}
    assert frozen["native"]["config"]["model"] == MODEL
    with pytest.raises(run_host.FreezeMismatch, match="model_mismatch"):
        run_host.frozen_arm_configs(FREEZE, CONFIGS, "another-model")
    edited = tmp_path / "configs"
    edited.mkdir()
    for path in CONFIGS.glob("*.json"):
        (edited / path.name).write_bytes(path.read_bytes())
    document = read(edited / "host-native.json")
    document["tools"]["exec"]["mode"] = "allow"
    write(edited / "host-native.json", document)
    with pytest.raises(run_host.FreezeMismatch, match="config_digest_mismatch"):
        run_host.frozen_arm_configs(FREEZE, edited, MODEL)


def test_the_effective_host_config_must_be_the_frozen_document():
    """M1: the frozen document, with secrets filled in, is what the host runs."""
    native = read(CONFIGS / "host-native.json")
    effective = run_host.realize(native, dict(apiKey="k", baseUrl="https://x/v1"))
    provider = effective["models"]["providers"]["external"]
    assert (provider["apiKey"], provider["baseUrl"]) == ("k", "https://x/v1")
    assert "omitted_secret_fields" not in provider
    # Host-owned state outside the frozen document is allowed.
    run_host.check_effective(
        native,
        dict(effective, meta=dict(lastTouched="t")),
        dict(apiKey="k", baseUrl="https://x/v1"),
    )
    changed = json.loads(json.dumps(effective))
    changed["models"]["providers"]["external"]["models"][0]["id"] = "other"
    extra = json.loads(json.dumps(effective))
    extra["models"]["providers"]["external"]["headers"] = {}
    missing = json.loads(json.dumps(effective))
    del missing["models"]["providers"]["external"]["apiKey"]
    for document in (changed, extra, missing):
        with pytest.raises(run_host.FreezeMismatch, match="host_config_mismatch"):
            run_host.check_effective(native, document, dict(apiKey="k", baseUrl="https://x/v1"))
    plugin = read(CONFIGS / "host-provider_only.json")
    run_host.check_effective(plugin, run_host.realize(plugin, {}))
    diagnostics = run_host.realize(plugin, {})
    diagnostics["plugins"]["entries"]["relaymind"]["config"]["diagnosticsPath"] = "/d"
    with pytest.raises(run_host.FreezeMismatch, match="host_config_mismatch"):
        run_host.check_effective(plugin, diagnostics)


def test_the_executor_uses_the_frozen_arm_config():
    """M1: a row's config must be the verified frozen configuration of its arm."""
    run = object.__new__(run_host.Run)
    run.frozen = run_host.frozen_arm_configs(FREEZE, CONFIGS, MODEL)
    entry = next(c for c in FREEZE["configurations"] if c["arm"] == "native")
    assert run.arm_config("native", entry) == entry
    with pytest.raises(run_host.FreezeMismatch, match="freeze_config_mismatch"):
        run.arm_config("native", dict(entry, model="another-model"))


def test_a_truncated_run_directory_path_is_sanitized():
    """M2: host output that cuts the run directory path still loses its private prefix."""
    work = "/var/tmp/area-0/workspaces-x/0123abcd-session/scratchpad/i260/host-run/work2"
    text = f"failed: aces-x/0123abcd-session/scratchpad/i260/host-run/work2/state/h1.lock; {work}/a"
    clean = run_host.sanitize(text + " sk-test-secret", secrets=["sk-test-secret"], work=work)
    assert clean == "failed: <run-dir>/state/h1.lock; <run-dir>/a <omitted>"


def test_a_fresh_ledger_path_starts_at_zero(tmp_path):
    """M5: the documented rerun starts on a new ledger."""
    ledger = tmp_path / "new" / "ledger.json"
    b = run_host.Budget(ledger, cap=1_000_000, row_budget=200_000, max_output=8_000)
    assert b.spent() == 0 and b.can_start()
    assert b.reserve("t1-native") is None
    b.settle("t1-native", "native", prompt=10, completion=5, charged=15)
    assert read(ledger)["total_input"] + read(ledger)["total_output"] == 15


def test_a_budget_cancelled_row_is_a_known_non_success():
    """L1 (spec 0.66 section 12.6.5): every started row that did not complete failed."""
    rows = [
        dict(task_id="t1", arm="native", status=status, success=None, evidence_ids=[])
        for status in ("completed", "failed", "timeout", "cancelled", "not_run")
    ]
    for n, row in enumerate(rows):
        row["task_id"] = f"t{n}"
    verdicts = {("t0", "native"): dict(success=True)}
    judge.apply_verdicts(dict(rows=rows), verdicts)
    assert [r["success"] for r in rows] == [True, False, False, False, None]


def test_the_host_turn_is_stopped_when_usage_cannot_be_read(tmp_path):
    """L2: a failing usage read never leaves the host process running."""
    pid_file = tmp_path / "pid"
    script = (
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)"
    )

    def usage():
        import time

        deadline = time.monotonic() + 10
        while not pid_file.exists() or not pid_file.read_text():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        raise OSError("usage store unavailable")

    with pytest.raises(OSError, match="usage store unavailable"):
        run_host.run_bounded(
            [sys.executable, "-c", script],
            env=None,
            timeout=20,
            usage=usage,
            limit_reached=lambda used: False,
            poll=0.05,
        )
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        import os

        os.kill(pid, 0)


# --- second review of #278: the token guard holds before dispatch ----------------

harness = _module("harness")


class Upstream:
    """A loopback stand-in for the external provider; it counts what reaches it."""

    def __init__(self, usage=None, stream=False):
        import http.server
        import threading

        self.bodies: list[dict] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.bodies.append(body)
                if stream:
                    chunks = [dict(choices=[dict(delta=dict(content="ok"))])]
                    if usage:
                        chunks.append(dict(choices=[], usage=usage))
                    payload = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
                    payload += "data: [DONE]\n\n"
                    kind = "text/event-stream"
                else:
                    answer = dict(choices=[dict(message=dict(content="ok"))])
                    if usage:
                        answer["usage"] = usage
                    payload, kind = json.dumps(answer), "application/json"
                data = payload.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v4"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def post(url: str, body: dict) -> tuple[int, dict | str]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        url + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer test-key"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read().decode("utf-8"), error.code
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


@pytest.fixture
def gated():
    made = []

    def make(usage=None, stream=False, max_output=1_000):
        upstream = Upstream(usage, stream)
        gate = run_host.RequestGate(upstream.url, max_output=max_output)
        gate.start()
        made.append((upstream, gate))
        return upstream, gate

    yield make
    for upstream, gate in made:
        gate.stop()
        upstream.close()


def test_an_oversized_request_is_refused_before_dispatch(gated):
    """High 1 (RM03; AC36): a call that could pass the row's remaining reservation
    never reaches the provider; the refusal has a closed reason and stops the turn."""
    upstream, gate = gated(usage=dict(prompt_tokens=120, completion_tokens=30))
    gate.open_row("t1-native", 20_000)
    big = dict(model="m", messages=[dict(role="user", content="x" * 30_000)])
    status, answer = post(gate.url, big)
    assert status == 400
    assert answer["error"]["code"] == "request_budget_exceeded"
    assert upstream.bodies == []
    assert gate.refused
    row = gate.close_row()
    assert (row["charged"], row["refused"]) == (0, ["request_budget_exceeded"])


def test_a_dispatched_request_has_bounded_output_and_settles_to_its_usage(gated):
    """High 1: the gate enforces max output tokens and charges the reported usage."""
    upstream, gate = gated(usage=dict(prompt_tokens=120, completion_tokens=30))
    gate.open_row("t1-native", 20_000)
    status, _ = post(gate.url, dict(model="m", messages=[dict(role="user", content="hi")]))
    assert status == 200
    # A larger request than the bound allows is clamped to it.
    post(gate.url, dict(model="m", messages=[], max_tokens=50_000))
    assert [b["max_tokens"] for b in upstream.bodies] == [1_000, 1_000]
    row = gate.close_row()
    assert (row["charged"], row["prompt"], row["completion"]) == (300, 240, 60)
    assert not row["refused"]


def test_a_call_without_reported_usage_stays_charged_at_its_bound(gated):
    """High 1: unknown consumption keeps the request's whole admitted bound."""
    upstream, gate = gated(usage=None, stream=True)
    gate.open_row("t1-provider_only", 20_000)
    status, text = post(gate.url, dict(model="m", messages=[], stream=True))
    assert status == 200 and "[DONE]" in text
    row = gate.close_row()
    assert row["charged"] == row["calls"][0]["bound"] > 1_000
    assert row["calls"][0]["usage"] is None


def test_streamed_usage_is_read_from_the_event_stream(gated):
    upstream, gate = gated(usage=dict(prompt_tokens=70, completion_tokens=7), stream=True)
    gate.open_row("t1-provider_only", 20_000)
    post(gate.url, dict(model="m", messages=[], stream=True))
    assert gate.close_row()["charged"] == 77


def test_a_request_outside_an_open_row_is_refused(gated):
    upstream, gate = gated(usage=dict(prompt_tokens=1, completion_tokens=1))
    status, answer = post(gate.url, dict(model="m", messages=[]))
    assert (status, answer["error"]["code"]) == (400, "no_reservation")
    assert upstream.bodies == []


def test_the_request_bound_covers_the_body_bytes_and_the_output_bound():
    body = json.dumps(dict(model="m", messages=[dict(role="user", content="请" * 100)]))
    bound = harness.request_bound(body.encode("utf-8"), 500)
    # Each token of a byte-level tokenizer covers at least one byte of the text.
    assert bound >= len(body.encode("utf-8")) + 500


def test_a_failure_before_settlement_keeps_the_row_charged_on_resume(tmp_path):
    """High 2 (RM03, RM17; AC36): the reservation is durable before dispatch; a crash
    or an evidence-write failure leaves it charged, and resume cannot overspend."""
    b = budget(tmp_path, cap=500_000, row=200_000)
    assert b.reserve("t1-native") is None
    # The process dies here: a fresh Budget reads the reservation back.
    resumed = budget_from(tmp_path, cap=500_000, row=200_000)
    assert resumed.spent() == 200_000
    assert resumed.reserve("t1-native") == "row_reserved"
    assert resumed.reserve("t2-native") is None
    # 400k reserved: a third row would pass the cap.
    assert resumed.reserve("t3-native") == "budget_cap"
    # Settlement is idempotent.
    resumed.settle("t2-native", "native", prompt=100, completion=10, charged=110)
    resumed.settle("t2-native", "native", prompt=100, completion=10, charged=110)
    assert budget_from(tmp_path, cap=500_000, row=200_000).spent() == 200_110


def budget_from(tmp_path, *, cap, row):
    return run_host.Budget(tmp_path / "ledger.json", cap=cap, row_budget=row, max_output=8_000)


def test_the_executor_reserves_before_dispatch_and_keeps_the_charge_on_failure(
    tmp_path, monkeypatch
):
    """High 2: the row is reserved before the host turn; an evidence-write failure
    after the turn leaves it charged, and the resumed run cannot start past the cap."""
    run = object.__new__(run_host.Run)
    run.frozen = run_host.frozen_arm_configs(FREEZE, CONFIGS, MODEL)
    run.budget = budget(tmp_path, cap=300_000, row=200_000)
    run.prompts = {"c1": "prompt"}
    run.work = tmp_path / "work"
    run.native = type("Host", (), dict(command=["openclaw"], env={}, config_path="c"))()
    seen = {}

    class Gate:
        refused = False

        def open_row(self, key, limit):
            seen["reserved_at_dispatch"] = budget_from(tmp_path, cap=300_000, row=200_000).spent()

        def charged(self):
            return 0

        def close_row(self):
            return dict(charged=0, prompt=0, completion=0, calls=[], refused=[])

    run.gate = Gate()

    def broken(*args, **kwargs):
        raise OSError("evidence volume full")

    monkeypatch.setattr(run_host, "run_bounded", broken)
    entry = next(c for c in FREEZE["configurations"] if c["arm"] == "native")
    with pytest.raises(OSError):
        run.execute(dict(id="t1", prompt_case_id="c1"), "native", entry)
    assert seen["reserved_at_dispatch"] == 200_000
    resumed = budget_from(tmp_path, cap=300_000, row=200_000)
    assert resumed.spent() == 200_000 and not resumed.can_start()


def test_provider_timeouts_across_retries_are_each_charged_and_stop_at_the_cap(
    tmp_path, monkeypatch
):
    """High 3 (RM03): every attempt is reserved durably before it is sent, a timeout
    keeps its reservation charged, and no retry is sent past the cap."""
    monkeypatch.setenv("RELAYMIND_EXTERNAL_BASE_URL", "http://127.0.0.1:9/v4")
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "test-key")
    monkeypatch.setattr(harness.time, "sleep", lambda seconds: None)
    sent = []

    def timeout(request, timeout):
        sent.append(json.loads(request.data))
        raise TimeoutError("timed out")

    monkeypatch.setattr(harness.urllib.request, "urlopen", timeout)
    messages = [dict(role="user", content="hi")]
    body = json.dumps(dict(model=harness.MODEL, messages=messages, **harness.SAMPLING))
    bound = harness.request_bound(body.encode("utf-8"), 1_000) + 100
    ledger = harness.Ledger(tmp_path / "ledger.json", cap=2 * bound + bound // 2)
    with pytest.raises(SystemExit, match="budget_cap"):
        harness.chat(ledger, "draft", messages, tmp_path / "log", "n1", max_output=1_000)
    assert len(sent) == 2
    assert all(b["max_tokens"] == 1_000 for b in sent)
    saved = harness.Ledger(tmp_path / "ledger.json", cap=10**9)
    assert 2 * (bound - 100) <= saved.total <= 2 * bound
    # With room for all of them, each of the four attempts is charged.
    sent.clear()
    ledger = harness.Ledger(tmp_path / "ledger2.json", cap=10**9)
    with pytest.raises(SystemExit, match="provider_error"):
        harness.chat(ledger, "draft", messages, tmp_path / "log", "n2", max_output=1_000)
    assert len(sent) == 4
    assert harness.Ledger(tmp_path / "ledger2.json", cap=10**9).total >= 4 * (bound - 100)


def test_a_answered_call_settles_its_reservation_to_the_reported_usage(tmp_path, monkeypatch):
    """High 3: a call with reported usage is charged exactly that usage, once."""
    monkeypatch.setenv("RELAYMIND_EXTERNAL_BASE_URL", "http://127.0.0.1:9/v4")
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "test-key")

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self.payload

    answer = dict(
        choices=[dict(message=dict(content="{}"))],
        usage=dict(prompt_tokens=40, completion_tokens=5),
    )
    monkeypatch.setattr(
        harness.urllib.request,
        "urlopen",
        lambda request, timeout: Response(json.dumps(answer).encode("utf-8")),
    )
    ledger = harness.Ledger(tmp_path / "ledger.json", cap=10**9)
    harness.chat(ledger, "draft", [dict(role="user", content="hi")], tmp_path / "log", "n1")
    saved = harness.Ledger(tmp_path / "ledger.json", cap=10**9)
    assert saved.total == 45
    assert saved.data["stages"]["draft"]["calls"] == 1


# --- third review of #278 ------------------------------------------------------------

bundle_script = _module("bundle")


def test_a_failed_setup_still_closes_the_run(tmp_path, monkeypatch):
    """RM12, RM16 (AC36): a setup failure still stops the gate and gateway and deletes
    the private config files that hold the provider credentials."""
    calls = []

    class FakeRun:
        def __init__(self, args):
            pass

        def setup(self):
            calls.append("setup")
            raise SystemExit("relay_setup_failed")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(run_host, "Run", FakeRun)
    argv = ["run_host.py", "--model", MODEL]
    for name in (
        "tasks",
        "freeze",
        "lock",
        "release-root",
        "journal",
        "environment",
        "ledger",
        "tarball",
        "openclaw",
        "work",
        "evidence",
        "output",
    ):
        argv += ["--" + name, str(tmp_path / name)]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit, match="relay_setup_failed"):
        run_host.main()
    assert calls == ["setup", "close"]


def test_closing_a_partly_set_up_run_does_not_raise(tmp_path):
    run = object.__new__(run_host.Run)
    run.work = tmp_path / "work"
    run.gateway = run.gate = None
    run.close()


def test_the_gate_bounds_the_output_field_the_client_sent(gated):
    """RM03 (AC36): a request that names only max_completion_tokens is clamped in
    that field and is not given a second, conflicting output field."""
    upstream, gate = gated(usage=dict(prompt_tokens=1, completion_tokens=1))
    gate.open_row("t1-native", 20_000)
    post(gate.url, dict(model="m", messages=[], max_completion_tokens=50_000))
    post(gate.url, dict(model="m", messages=[], max_completion_tokens=10))
    assert [b.get("max_completion_tokens") for b in upstream.bodies] == [1_000, 10]
    assert all("max_tokens" not in b for b in upstream.bodies)
    gate.close_row()


def test_a_row_reserved_before_reports_its_own_reason(tmp_path):
    """RM16: a row whose key is already held in the ledger is not labelled budget_cap."""
    run = object.__new__(run_host.Run)
    run.frozen = run_host.frozen_arm_configs(FREEZE, CONFIGS, MODEL)
    run.budget = budget(tmp_path)
    assert run.budget.reserve("t1-native") is None
    entry = next(c for c in FREEZE["configurations"] if c["arm"] == "native")
    assert run.skip_reason(dict(id="t1"), "native", entry) == "row_reserved"


def write_cases_inputs(tmp_path, prompts):
    root = tmp_path / "root"
    root.mkdir()
    write(root / "catalog.json", dict(assets=[], evidence=[], people=[]))
    plan = tmp_path / "plan.json"
    write(plan, dict(seed=1, items=[dict(item_id="e001", cohort="episode", lane="en_to_en")]))
    drafts = tmp_path / "drafts.json"
    write(drafts, {"e001": dict(prompts=prompts)})
    log = tmp_path / "drafting-log.json"
    log.write_text("{}", encoding="utf-8")
    return type("Args", (), dict(root=root, plan=plan, drafts=drafts, drafting_log=log))()


def test_two_equal_prompts_inside_one_item_are_rejected(tmp_path):
    """RM40 (AC31): held-out cases stay independent; a drafted item whose prompts
    repeat each other (after NFC and casefold) is rejected, not split into cases."""
    args = write_cases_inputs(tmp_path, ["Find X", "b", "c", "find x", "e"])
    with pytest.raises(SystemExit, match="duplicate_prompt e001"):
        harness.cmd_cases(args)


def test_a_judgment_with_no_known_handle_is_not_turned_into_a_negative():
    """RM40 (AC31): a positive or ambiguous judgment whose handles are all unknown is
    left for retry, never stored as negative with an empty acceptable set."""
    names = {"asset:one": "A001", "asset:two": "A002"}
    row = dict(case_id="c", language_valid=True, independent_case=True)
    assert harness._classes(dict(row, **{"class": "positive", "acceptable": ["A002"]}), names)[
        "acceptable"
    ] == ["asset:two"]
    assert (
        harness._classes(dict(row, **{"class": "negative", "acceptable": []}), names)["cls"]
        == "negative"
    )
    for label in ("positive", "ambiguous"):
        for acceptable in (["A999"], [], ["A001", "A9"]):
            with pytest.raises(ValueError, match="acceptable"):
                harness._classes(dict(row, **{"class": label, "acceptable": acceptable}), names)


def test_adjudication_keeps_paid_batches_when_a_later_reply_is_malformed(tmp_path, monkeypatch):
    """RM16, RM44 (AC36): a malformed or unreasoned adjudicator reply leaves its items
    for a rerun; the batches already answered are written and not asked again."""
    blind = {i: dict(blind_id=i, request="r", rubric=["c"], answer="a") for i in ("x1", "x2", "x3")}
    a = {"x1": verdict(), "x2": verdict(), "x3": verdict()}
    b = {"x1": verdict(False), "x2": verdict(False), "x3": verdict(False)}
    paths = dict(items=tmp_path / "items.json", a=tmp_path / "a.json", b=tmp_path / "b.json")
    write(paths["items"], dict(items=list(blind.values())))
    write(paths["a"], a)
    write(paths["b"], b)
    replies = iter(
        [
            dict(adjudications=[dict(blind_id="x1", success=True, reason="rubric met")]),
            "not json",
            dict(adjudications=[dict(blind_id="x3", success=False, reason=" ")]),
        ]
    )
    asked = []

    def chat(ledger, stage, messages, log, name):
        asked.append([i["blind_id"] for i in json.loads(messages[1]["content"])])
        reply = next(replies)
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return dict(choices=[dict(message=dict(content=content))])

    monkeypatch.setattr(judge.harness, "chat", chat)
    args = type(
        "Args",
        (),
        dict(
            **paths,
            output=tmp_path / "judgments.json",
            log=tmp_path / "log",
            ledger=tmp_path / "ledger.json",
            cap=10**9,
            batch=1,
        ),
    )()
    with pytest.raises(judge.JudgingIncomplete, match="final_incomplete"):
        judge.cmd_adjudicate(args)
    saved = read(args.output)
    assert saved["final"] == {"x1": dict(success=True, reason="rubric met")}
    replies = iter(
        [
            dict(adjudications=[dict(blind_id="x2", success=False, reason="missing step")]),
            dict(adjudications=[dict(blind_id="x3", success=True, reason="all criteria")]),
        ]
    )
    asked.clear()
    judge.cmd_adjudicate(args)
    assert asked == [["x2"], ["x3"]]
    done = read(args.output)
    judge.check_judgments(done, blind)
    assert {i: v["success"] for i, v in done["final"].items()} == {
        "x1": True,
        "x2": False,
        "x3": True,
    }


def test_unpublished_extra_evidence_is_recorded_as_restricted(tmp_path):
    """RM44 (AC36): evidence kept out of the published bundle is not called
    redistributable in the input manifest."""
    private, public = tmp_path / "private.txt", tmp_path / "public.txt"
    private.write_bytes(b"private")
    public.write_bytes(b"public")
    bundle = bundle_script.Bundle(tmp_path / "out", tmp_path / "resolver")
    bundle_script.add_extra_evidence(
        bundle,
        {
            "extra:private": dict(path=str(private), publish=False),
            "extra:public": dict(path=str(public)),
        },
    )
    assert bundle.evidence["extra:private"]["access"] == "restricted"
    assert bundle.evidence["extra:private"]["path"] is None
    assert bundle.evidence["extra:public"]["access"] == "redistributable"


def test_the_episode_run_rejects_production_settings_the_freeze_does_not_bind(monkeypatch):
    """RM42 (AC33): the recommendation settings the run executes are the frozen ones."""
    off = dict(generator=dict(runner_digest="r" * 64, protocol_digest="p" * 64), budget=1)
    config = type("Config", (), dict(recommendations="settings"))()
    seen = {}

    def projection(production, release, *, runner_digest, protocol_digest):
        seen["digests"] = (runner_digest, protocol_digest)
        return dict(off, budget=production.budget)

    monkeypatch.setattr(run_episodes.asset_evidence, "effective_asset_config", projection)
    config.budget = 1
    assert run_episodes.frozen_settings(config, "release", off) == "settings"
    assert seen["digests"] == ("r" * 64, "p" * 64)
    config.budget = 2
    with pytest.raises(run_episodes.NotFrozen, match="production_config_mismatch"):
        run_episodes.frozen_settings(config, "release", off)


@pytest.mark.parametrize("held", [False, True])
def test_prestart_skips_survive_journal_resume_without_dispatch(tmp_path, held):
    """RM16/RM44, AC36: unavailable arms and budget refusals never start or fail."""
    from whole_task_helpers import ENVIRONMENT, freeze_doc, tasks_doc

    freeze = freeze_doc(task_ids=("t1",))
    tasks = tasks_doc(freeze, lanes=("en_to_en",))
    run = object.__new__(run_host.Run)
    run.frozen = {c["arm"]: dict(config=c) for c in freeze["configurations"]}
    run.budget = budget(tmp_path, cap=200_000, spent=0 if held else 1)
    if held:
        assert run.budget.reserve("t1-native") is None
        assert run.budget.reserve("t1-provider_only") == "budget_cap"
    journal = tmp_path / "run.jsonl"

    def unexpected(*args):
        pytest.fail("skipped work was dispatched")

    kwargs = dict(
        environment=ENVIRONMENT,
        artifacts=[],
        qualification="fixture",
        input_manifest=dict(path=None, digest="0" * 64, bytes=0),
        interrupted=lambda: False,
        skip_reason=run.skip_reason,
    )
    report = run_host.run_host_plan(tasks, freeze, unexpected, journal, **kwargs)
    reasons = {r["arm"]: r["reason"] for r in report["rows"]}
    assert reasons == dict(
        native="row_reserved" if held else "budget_cap",
        provider_only="budget_cap",
        recommendations="person_choice_unavailable",
    )
    assert all(r["status"] == "not_run" for r in report["rows"])
    assert all(a["failed"] == 0 and a["not_run"] == 1 for a in report["summary"]["arms"])
    assert all(json.loads(line)["type"] != "started" for line in journal.read_text().splitlines())
    kwargs["skip_reason"] = unexpected
    assert run_host.run_host_plan(tasks, freeze, unexpected, journal, **kwargs) == report


@pytest.mark.parametrize("field", ["baseUrl", "apiKey"])
def test_effective_config_rejects_changed_private_values(field):
    """RM03/RM16, AC36: keeping a secret key cannot bypass the request gate."""
    frozen = read(CONFIGS / "host-native.json")
    expected = dict(baseUrl="http://127.0.0.1:9/v1", apiKey="test-key")
    effective = run_host.realize(frozen, expected)
    run_host.check_effective(frozen, effective, expected)
    effective["models"]["providers"]["external"][field] = "changed"
    with pytest.raises(run_host.FreezeMismatch, match="host_config_mismatch"):
        run_host.check_effective(frozen, effective, expected)


def test_annotation_preserves_unpadded_answers_and_notes(tmp_path, monkeypatch):
    """RM16/RM40, AC31: a handle formatting slip cannot change the raw judgment."""
    from types import SimpleNamespace

    write(tmp_path / "catalog.json", dict(assets=[dict(record=dict(asset_id="asset:one"))]))
    template = tmp_path / "template.json"
    write(
        template,
        dict(
            annotator_id="machine-annotator-a",
            items=[
                dict(
                    case_id="c1",
                    lane="en_to_en",
                    task_text="Find it",
                    language_tags=["en"],
                    judgment=None,
                )
            ],
        ),
    )
    notes = "  original note " * 150
    raw = dict(
        case_id="c1",
        **{"class": "positive"},
        acceptable=["A1"],
        language_valid=False,
        independent_case=True,
        notes=notes,
    )
    monkeypatch.setattr(harness, "catalog_listing", lambda catalog: "A001 asset:one")
    monkeypatch.setattr(
        harness,
        "chat",
        lambda *a, **k: dict(
            choices=[dict(message=dict(content=json.dumps(dict(judgments=[raw]))))]
        ),
    )
    out = tmp_path / "out.json"
    harness.cmd_annotate(
        SimpleNamespace(
            root=tmp_path,
            template=template,
            ledger=tmp_path / "ledger",
            cap=1000,
            output=out,
            batch=1,
            max_batches=0,
            log=tmp_path / "log",
        )
    )
    result = read(out)["items"][0]["judgment"]
    assert result is not None
    assert result["class"] == "positive"
    assert result["acceptable_asset_ids"] == ["asset:one"]
    assert result["language_valid"] is False and result["independent_case"] is True
    assert result["notes"] == notes


def test_call_log_holds_original_bytes_and_individual_rejections(tmp_path):
    """RM16/RM40, AC31: drafting evidence keeps bytes, not only their hashes."""
    from types import SimpleNamespace

    logs = tmp_path / "log"
    logs.mkdir()
    sent = b'{ "messages": [ ], "temperature": 0 }\n'
    received = b'{ "created": 0, "model": "fixture" }\n'
    (logs / "draft.request.json").write_bytes(sent)
    (logs / "draft.response.json").write_bytes(received)
    out = tmp_path / "drafting-log.json"
    harness.cmd_log(
        SimpleNamespace(
            log=logs,
            copy_to=tmp_path / "copy",
            output=out,
            template="drafter.txt",
            role="author",
            rejected=[
                "e001:wrong_lane_language:draft.response.json e002:duplicate:draft.response.json",
            ],
        )
    )
    log = read(out)
    call = log["calls"][0]
    assert call["request_utf8"].encode("utf-8") == sent
    assert call["response_utf8"].encode("utf-8") == received
    assert call["request_sha256"] == harness.sha256(sent)
    assert call["response_sha256"] == harness.sha256(received)
    assert log["rejected"] == [
        dict(item="e001", reason="wrong_lane_language", response="draft.response.json"),
        dict(item="e002", reason="duplicate", response="draft.response.json"),
    ]


def test_chat_keeps_the_exact_transmitted_request_bytes(tmp_path, monkeypatch):
    """RM16/RM40, AC31: reserialization is not original request evidence."""
    monkeypatch.setenv("RELAYMIND_EXTERNAL_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("RELAYMIND_EXTERNAL_API_KEY", "test-key")
    import io

    sent = []
    raw = b'{"usage":{"prompt_tokens":1,"completion_tokens":1}}'

    def respond(request, timeout):
        sent.append(request.data)
        return io.BytesIO(raw)

    monkeypatch.setattr(harness.urllib.request, "urlopen", respond)
    harness.chat(
        harness.Ledger(tmp_path / "ledger", 10000),
        "draft",
        [dict(role="user", content="中文")],
        tmp_path / "log",
        "draft",
        max_output=100,
    )
    assert (tmp_path / "log/draft.request.json").read_bytes() == sent[0]
    assert (tmp_path / "log/draft.response.json").read_bytes() == raw


@pytest.mark.parametrize(
    "failure", [FileNotFoundError(), run_host.subprocess.CalledProcessError(1, ["zstd"])]
)
def test_transcript_decode_failure_retains_row_with_unknown_cost(tmp_path, monkeypatch, failure):
    """RM16/RM44, AC36: missing/broken decompression cannot lose the executed row."""
    import sqlite3
    from types import SimpleNamespace

    run = object.__new__(run_host.Run)
    run.frozen = run_host.frozen_arm_configs(FREEZE, CONFIGS, MODEL)
    run.budget = budget(tmp_path)
    run.prompts = {"c1": "prompt"}
    run.work = tmp_path / "work"
    run.evidence_root = tmp_path / "evidence"
    run.secrets = []
    run.native = SimpleNamespace(command=["fixture-host"], env={}, config_path="c")
    run.gate = SimpleNamespace(
        open_row=lambda *a: None,
        charged=lambda: 10,
        refused=False,
        close_row=lambda: dict(prompt=8, completion=2, charged=10, refused=False),
    )

    def executed(*args, **kwargs):
        state = run.work / "state/t1-native"
        with sqlite3.connect(state / "openclaw-agent.sqlite") as conn:
            conn.execute(
                "CREATE TABLE transcript_events(seq INTEGER, event_json TEXT, event_zstd BLOB)"
            )
            conn.execute(
                "INSERT INTO transcript_events VALUES(1, ?, NULL)",
                (json.dumps(dict(usage=dict(input=1, output=1), responseId="first")),),
            )
            conn.execute("INSERT INTO transcript_events VALUES(2, NULL, ?)", (b"packed",))
        return dict(
            status="completed",
            exit_code=0,
            elapsed_ms=1,
            row_tokens_at_stop=10,
            stdout='{"ok":true}',
            stderr="",
        )

    def broken(*args, **kwargs):
        raise failure

    monkeypatch.setattr(run_host, "run_bounded", executed)
    monkeypatch.setattr(run_host.subprocess, "run", broken)
    config = next(c for c in FREEZE["configurations"] if c["arm"] == "native")
    row, refs = run.execute(dict(id="t1", prompt_case_id="c1"), "native", config)
    assert row["status"] == "completed"
    assert row["reason"] == "transcript_usage_unavailable"
    assert row["attempts"] == []  # Partial coverage must not look complete.
    assert run.budget.spent() == 10
    usage = next(r for r in refs if r["id"].endswith("transcript-usage.json"))
    assert usage["id"] in row["evidence_ids"]
    assert read(run.evidence_root / usage["path"]) == dict(error="transcript_usage_unavailable")


def test_future_drafting_evidence_is_restricted_and_lock_bound(tmp_path):
    """RM12/RM40, AC31: exact drafting bytes stay private through corpus bundling."""
    from types import SimpleNamespace

    root = tmp_path / "release"
    root.mkdir()
    write(
        root / "catalog.json",
        dict(assets=[], evidence=[], people=[dict(id="machine-drafter", evidence_ids=[])]),
    )
    write(
        root / "plan.json",
        dict(seed=1, items=[dict(item_id="h001", cohort="host", lane="en_to_en")]),
    )
    write(root / "drafts.json", {"h001": dict(prompts=["Find it"], rubric=["Correct answer"])})
    write(root / "drafting-log.json", dict(calls=[dict(request_utf8="{}", response_utf8="{}")]))
    (root / "PROTOCOL.md").write_text("Fixture protocol")
    harness.cmd_cases(
        SimpleNamespace(
            root=root,
            plan=root / "plan.json",
            drafts=root / "drafts.json",
            drafting_log=root / "drafting-log.json",
        )
    )
    entry = next(e for e in read(root / "catalog.json")["evidence"] if e["kind"] == "authorship")
    assert entry["access"] == "restricted"
    assert entry["sha256"] == harness.sha256((root / "drafting-log.json").read_bytes())
    assert read(root / "cases.json")["cases"][0]["authorship_evidence_ids"] == [entry["id"]]


@pytest.mark.parametrize(
    "change",
    [
        {"class": "negative", "acceptable": ["A001"]},
        {"acceptable": None},
        {"language_valid": "false"},
        {"independent_case": None},
        {"notes": 7},
    ],
)
def test_annotation_rejects_inconsistent_answers_without_coercion(change):
    """RM16/RM40, AC31: invalid answers are retried rather than rewritten."""
    raw = dict(
        **{"class": "positive"},
        acceptable=["A001"],
        language_valid=True,
        independent_case=True,
        notes="unchanged",
    )
    raw.update(change)
    with pytest.raises(ValueError):
        harness._classes(raw, {"asset:one": "A001"})
