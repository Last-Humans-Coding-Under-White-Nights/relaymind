"""Run one planned early-trial row on the host and write its observation (#259).

Spec 0.66 sections 12.6.5/12.6.6 (RM16, RM39, RM44; AC30, AC36). Operator evidence
tooling, not product code. For the planned ordinal it runs one headless
``openclaw agent exec`` turn with the frozen arm configuration, in a new empty
working and state directory under the private operator root, and writes the
``row-NNNN.json`` observation that ``scripts/asset_whole_task.py trial`` reads.

Evidence kept per row (under ``<observations>/evidence/NNNN/``):

- ``host-envelope.json``: the host's own agent-exec JSON envelope;
- ``host-transcript.jsonl``: the host's persisted session transcript events of
  that session (``transcript_events`` of the agent database), the spec 12.6.6
  native usage source;
- ``gateway-trace.json`` (RelayMind arms): the TraceEvent objects of the gateway
  runs started during the row.

Attempts: the native arm takes one attempt per assistant message from the host
transcript (provider-reported usage persisted by the host; ``input_tokens`` is
the host's ``contextUsage.promptTokens``); the RelayMind arms take the
gateway's external model calls through the existing ``attempts_from_trace``.
Both use ``cached_within_input_reasoning_within_output`` with the
``usage-relationship`` evidence. ``success`` is always ``null``: under the
``machine_dual_owner_approved`` profile the two machine annotators judge rows
blind against the frozen rubric afterwards, the machine adjudicator settles any
disagreement, and the owner only approves (``machine_passes.py judge``).
The external credential is passed to the host only for the native arm and is
never written.
"""

import argparse
import hashlib
import json
import os
import resource
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("RELAYMIND_REPO") or HERE.parents[3])
sys.path.insert(0, str(REPO / "src"))

from relaymind.canonical import canonical_digest, canonical_json  # noqa: E402

MODEL_REFS = {
    "native": "external-direct/glm-5.3-flash",
    "provider_only": "relaymind/relaymind-client-tools",
    "recommendations": "relaymind/relaymind-client-tools",
}
SECRETS = ("RELAYMIND_EXTERNAL_API_KEY", "RELAYMIND_EXTERNAL_BASE_URL")
RELATIONSHIP_ID = "usage-relationship"


def _machine_names() -> list[bytes]:
    """This Mac's computer, local host and host names: never published (spec 12.6.6)."""
    import socket

    names = {socket.gethostname(), socket.gethostname().split(".")[0]}
    for key in ("ComputerName", "LocalHostName", "HostName"):
        done = subprocess.run(["scutil", "--get", key], capture_output=True, text=True)
        if done.returncode == 0 and done.stdout.strip():
            names.add(done.stdout.strip())
    return sorted((n.encode("utf-8") for n in names if len(n) >= 3), key=len, reverse=True)


def redact(payload: bytes) -> bytes:
    """Replace the machine names in captured evidence with a fixed placeholder."""
    for name in _machine_names():
        payload = payload.replace(name, b"<machine-name>")
    return payload


def _utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _json(payload) -> bytes:
    return (canonical_json(payload) + "\n").encode("utf-8")


def _frozen(inputs: Path) -> tuple[dict, dict, dict]:
    manifest = json.loads(inputs.read_text(encoding="utf-8"))

    def one(kind: str) -> dict:
        (doc,) = [d for d in manifest["documents"] if d["kind"] == kind]
        return json.loads((inputs.parent / doc["path"]).read_text(encoding="utf-8"))

    cases = {c["id"]: c for c in one("cases")["cases"]}
    return one("host_tasks"), one("host_freeze"), cases


def _transcript(state: Path, session_id: str) -> list[dict]:
    database = state / "agents/main/agent/openclaw-agent.sqlite"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT seq, event_json, event_zstd FROM transcript_events"
            " WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
    finally:
        connection.close()
    events = []
    for seq, text, compressed in rows:
        if text is None:
            # Large events are stored zstd-compressed by the host.
            text = subprocess.run(
                ["zstd", "-d", "-c", "-q"], input=compressed, capture_output=True, check=True
            ).stdout.decode("utf-8")
        events.append(dict(seq=seq, event=json.loads(text)))
    return events


def _gateway_events(store: Path, before: set[str]) -> list[dict]:
    connection = sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)
    try:
        runs = [r for (r,) in connection.execute("SELECT run_id FROM runs ORDER BY rowid")]
        new = [r for r in runs if r not in before]
        events = []
        for run_id in new:
            for row in connection.execute(
                "SELECT schema_version, run_id, seq, timestamp, elapsed_ms, event_type, actor,"
                " payload FROM events WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ):
                keys = ("schema_version", "run_id", "seq", "timestamp", "elapsed_ms")
                event = dict(zip(keys, row[:5], strict=True))
                event.update(event_type=row[5], actor=row[6], payload=json.loads(row[7]))
                events.append(event)
    finally:
        connection.close()
    return events


def _run_ids(store: Path) -> set[str]:
    connection = sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)
    try:
        return {r for (r,) in connection.execute("SELECT run_id FROM runs")}
    finally:
        connection.close()


def _native_attempts(transcript: list[dict], prefix: str, evidence_id: str) -> list[dict]:
    attempts = []
    for item in transcript:
        message = item["event"].get("message") or {}
        if message.get("role") != "assistant":
            continue
        usage = message.get("usage") or {}
        context = usage.get("contextUsage") or {}
        prompt = context.get("promptTokens") if context.get("state") == "available" else None
        ended = message.get("timestamp")
        attempts.append(
            dict(
                id=f"{prefix}{len(attempts) + 1}",
                ordinal=len(attempts) + 1,
                model=message.get("model"),
                provider=message.get("provider"),
                external=True,
                started_at=None,
                ended_at=_utc(ended / 1000) if isinstance(ended, int) else None,
                input_tokens=prompt,
                output_tokens=usage.get("output"),
                cached_tokens=usage.get("cacheRead"),
                reasoning_tokens=usage.get("reasoningTokens"),
                total_tokens=usage.get("totalTokens"),
                token_relationship="cached_within_input_reasoning_within_output",
                calls=1 if message.get("responseId") else None,
                usage_provenance="reported",
                evidence_ids=sorted([evidence_id, RELATIONSHIP_ID]),
                outcome="success" if message.get("stopReason") != "error" else "failed",
            )
        )
    return attempts


def observe(args) -> int:
    from relaymind.evaluation.asset_host_runner import attempts_from_trace

    root, observations = args.root.absolute(), args.observations.absolute()
    host_tasks, freeze, cases = _frozen(args.inputs.absolute())
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    entries = plan["planned"] if isinstance(plan, dict) else plan
    (entry,) = [p for p in entries if p["ordinal"] == args.ordinal]
    task = next(t for t in host_tasks["tasks"] if t["id"] == entry["task_id"])
    arm = entry["arm"]
    if args.not_run is not None:
        # Execution never started for this row: no attempts, no session, no timings.
        row = dict(
            task_id=task["id"],
            arm=arm,
            status="not_run",
            success=None,
            attempts=[],
            elapsed_ms=None,
            active_ms=None,
            human_wait_ms=None,
            interactions=None,
            local_turns=None,
            cpu_ms=None,
            peak_bytes=None,
            recommendation_ids=[],
            host_session_id=None,
            evidence_ids=[],
            reason=args.not_run,
        )
        observation = dict(
            schema_version=1,
            ordinal=args.ordinal,
            task_id=task["id"],
            arm=arm,
            row=row,
            evidence=[],
        )
        observations.mkdir(parents=True, exist_ok=True)
        (observations / f"row-{args.ordinal:04d}.json").write_bytes(_json(observation))
        print(
            json.dumps(dict(ordinal=args.ordinal, arm=arm, status="not_run", reason=args.not_run))
        )
        return 0
    config_path = root / "host" / f"arm-{arm}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    frozen_config = next(c for c in freeze["configurations"] if c["arm"] == arm)
    if canonical_digest(config) != frozen_config["host_config_digest"]:
        raise SystemExit(f"{config_path} is not the frozen {arm} host configuration")
    number = f"{args.ordinal:04d}"
    run_dir = root / "runs" / number
    (run_dir / "cwd").mkdir(parents=True)
    (run_dir / "state").mkdir()
    evidence_dir = observations / "evidence" / number
    evidence_dir.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("OPENCLAW_")}
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        env.pop(key, None)
    env.update(
        OPENCLAW_HOME=str(root / "host/home"),
        OPENCLAW_STATE_DIR=str(root / "host/state"),
        OPENCLAW_CONFIG_PATH=str(config_path),
    )
    openclaw = str(root / "cli/node_modules/.bin/openclaw")
    store = None
    if arm == "native":
        if any(not env.get(k) for k in SECRETS):
            raise SystemExit("the native arm needs the external credentials in the environment")
    else:
        for key in SECRETS:
            env.pop(key, None)
        gateway = root / f"gateway-{arm}"
        store = gateway / "relaymind.sqlite3"
        token = next(
            line.split(": ", 1)[1]
            for line in (gateway / "stdout.log").read_text(encoding="utf-8").splitlines()
            if line.startswith("Application token: ")
        )
        stored = subprocess.run(
            [openclaw, "models", "auth", "paste-api-key", "--provider", "relaymind"],
            input=token + "\n",
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        if stored.returncode != 0:
            raise SystemExit("storing the gateway token failed")
        if canonical_digest(json.loads(config_path.read_text())) != canonical_digest(config):
            raise SystemExit("storing the token changed the frozen host configuration")
    before = _run_ids(store) if store else set()
    prompt = cases[task["prompt_case_id"]]["task_text"]
    argv = [openclaw, "agent", "exec", prompt, "--model", MODEL_REFS[arm], "--json"]
    argv += ["--config", str(config_path), "--cwd", str(run_dir / "cwd")]
    argv += ["--state-dir", str(run_dir / "state"), "--timeout", str(args.timeout)]
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.time()
    status, reason = "completed", None
    try:
        done = subprocess.run(
            argv, capture_output=True, env=env, timeout=args.timeout + 60, check=False
        )
        stdout, code = done.stdout, done.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, code, status, reason = exc.stdout or b"", None, "timeout", "host_timeout"
    elapsed_ms = (time.time() - started) * 1000
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_ms = (
        (usage_after.ru_utime - usage_before.ru_utime)
        + (usage_after.ru_stime - usage_before.ru_stime)
    ) * 1000
    try:
        envelope = json.loads(stdout)
    except ValueError:
        envelope = None
    if status == "completed" and (code != 0 or not isinstance(envelope, dict)):
        status, reason = "failed", "host_exit_nonzero" if code else "host_output_unusable"
    evidence = []
    # Keep the host's own output before anything else can fail.
    (evidence_dir / "host-envelope.json").write_bytes(redact(stdout or b""))

    def keep(evidence_id: str, name: str, payload: bytes) -> None:
        (evidence_dir / name).write_bytes(redact(payload))
        file = f"evidence/{number}/{name}"
        evidence.append(dict(id=evidence_id, file=file, provenance="observed"))

    session_id = envelope.get("sessionId") if isinstance(envelope, dict) else None
    keep(f"host-envelope:{number}", "host-envelope.json", stdout or b"")
    transcript = _transcript(run_dir / "state", session_id) if session_id else []
    keep(
        f"host-transcript:{number}",
        "host-transcript.jsonl",
        b"".join(_json(item) for item in transcript),
    )
    if arm == "native":
        attempts = _native_attempts(transcript, f"a{number}-", f"host-transcript:{number}")
    else:
        events = _gateway_events(store, before)
        keep(f"gateway-trace:{number}", "gateway-trace.json", _json(events))
        attempts = attempts_from_trace(
            events,
            provider=frozen_config["provider"],
            attempt_prefix=f"a{number}-",
            evidence_id=f"gateway-trace:{number}",
            token_relationship="cached_within_input_reasoning_within_output",
            relationship_evidence_id=RELATIONSHIP_ID,
            first_ordinal=1,
        )
    relationship = (HERE / "usage-relationship.md").read_bytes()
    (observations / "usage-relationship.md").write_bytes(relationship)
    evidence.append(dict(id=RELATIONSHIP_ID, file="usage-relationship.md", provenance="observed"))
    recommendation_ids = []
    if args.recommendation_receipt is not None:
        receipt = args.recommendation_receipt.read_bytes()
        keep(f"recommendation-receipt:{number}", "recommendation-receipt.json", receipt)
        recommendation_ids = [json.loads(receipt)["recommendation_id"]]
    if not attempts and status == "completed":
        status, reason = "failed", "no_attempt_observed"
    row = dict(
        task_id=task["id"],
        arm=arm,
        status=status,
        success=None,
        attempts=attempts,
        elapsed_ms=round(elapsed_ms, 3),
        active_ms=round(elapsed_ms, 3),
        human_wait_ms=args.human_wait_ms,
        interactions=args.interactions,
        local_turns=0,
        cpu_ms=round(cpu_ms, 3),
        peak_bytes=usage_after.ru_maxrss,
        recommendation_ids=recommendation_ids,
        host_session_id=session_id,
        evidence_ids=sorted({e["id"] for e in evidence}, key=str.encode),
        reason=reason,
    )
    observation = dict(
        schema_version=1,
        ordinal=args.ordinal,
        task_id=task["id"],
        arm=arm,
        row=row,
        evidence=sorted(evidence, key=lambda e: e["id"].encode()),
    )
    (observations / f"row-{number}.json").write_bytes(_json(observation))
    summary = dict(
        ordinal=args.ordinal,
        arm=arm,
        status=status,
        attempts=len(attempts),
        input_tokens=sum(a["input_tokens"] or 0 for a in attempts),
        output_tokens=sum(a["output_tokens"] or 0 for a in attempts),
        elapsed_ms=round(elapsed_ms),
        envelope_sha256=hashlib.sha256(stdout or b"").hexdigest(),
    )
    print(json.dumps(summary))
    return 0 if status == "completed" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="private operator root")
    parser.add_argument("--inputs", type=Path, required=True, help="frozen inputs.json")
    parser.add_argument("--plan", type=Path, required=True, help="plan-host output")
    parser.add_argument("--ordinal", type=int, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--recommendation-receipt", type=Path)
    parser.add_argument("--interactions", type=int, default=0)
    parser.add_argument("--human-wait-ms", type=float, default=0.0)
    parser.add_argument("--not-run", metavar="REASON", help="record the row not_run, run nothing")
    return observe(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
