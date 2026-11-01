"""Preregistered paired host-task run for #260 (spec 0.66 sections 12.6.5, 12.6.6).

RM16, RM40, RM44; AC36, AC37. Executes the frozen three-arm plan through the
journaled ``run_host_plan`` driver of ``relaymind.evaluation.asset_host_runner``
with an in-process executor over the pinned OpenClaw 2026.10.7 host:

* ``native``: the host's own provider entry for the external model;
* ``provider_only``: the RelayMind provider plugin (release tarball) over a
  gateway started with ``relaymind serve --config``, recommendations off;
* ``recommendations``: needs a signed-in person in the native Control UI to
  choose, and exact skill/MCP use is disabled on this host version (the two
  pending negative records), so an unattended run cannot exercise it. Its
  rows are ``not_run`` with reason ``person_choice_unavailable``.

Every row runs ``openclaw agent exec`` in a fresh sandbox and state directory.
The native arm's usage is the host's own persisted per-message usage (the
session transcript), and the provider arm's usage the gateway TraceEvents. The
verdict stays null here: blind success judging is a later, separate step. A row
that would pass the external-token cap is ``not_run`` with reason
``budget_cap``. Credentials and the provider endpoint live only in private
files of the run directory, which are deleted at the end, and in this process.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import live_acceptance  # noqa: E402
import openclaw_install_check as install_check  # noqa: E402

from relaymind.canonical import canonical_json  # noqa: E402
from relaymind.evaluation.asset_host_runner import (  # noqa: E402
    attempts_from_trace,
    run_host_plan,
)
from relaymind.evaluation.environment import checkout_environment  # noqa: E402

PROVIDER = "external-openai-compatible"
DIRECT = "external"
TIMEOUT = 600.0


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_bytes(path: Path, payload: bytes) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return dict(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Run:
    def __init__(self, args):
        self.args = args
        self.work = args.work.absolute()
        self.evidence_root = args.evidence.absolute()
        self.cases = {c["id"]: c for c in read(args.cases)["cases"]}
        self.secrets = [
            os.environ["RELAYMIND_EXTERNAL_API_KEY"],
            os.environ["RELAYMIND_EXTERNAL_BASE_URL"],
        ]
        self.ledger = read(args.ledger)
        self.gateway = None

    # -- ledger ------------------------------------------------------------------
    def spent(self) -> int:
        d = self.ledger
        return d["total_input"] + d["total_output"] + d.get("unlogged_estimate", 0)

    def charge(self, arm: str, prompt: int, completion: int) -> None:
        d = self.ledger
        d["total_input"] += prompt
        d["total_output"] += completion
        row = d["stages"].setdefault(f"host_{arm}", dict(input=0, output=0, calls=0))
        row["input"] += prompt
        row["output"] += completion
        row["calls"] += 1
        Path(self.args.ledger).write_text(json.dumps(d, indent=1), encoding="utf-8")

    # -- setup -------------------------------------------------------------------
    def host(self, name: str, provider_network: bool):
        import run_openclaw_eval as m9

        return m9.Host(
            [str(self.args.openclaw)], self.work / name, provider_network=provider_network
        )

    def setup(self):
        import run_openclaw_eval as m9

        self.work.mkdir(parents=True, exist_ok=True)
        os.umask(0o077)
        self.native = self.host("native-home", True)
        direct = m9.DirectEndpoint(
            os.environ["RELAYMIND_EXTERNAL_BASE_URL"],
            self.args.model,
            os.environ["RELAYMIND_EXTERNAL_API_KEY"],
            None,
        )
        m9.DIRECT_PROVIDER = DIRECT
        steps, ok = m9.setup_direct(self.native, direct)
        self.native.merge_config({"tools": {"exec": {"mode": "deny"}}})
        if not ok:
            raise SystemExit("native_setup_failed")
        gateway_dir = self.work / "gateway"
        gateway_dir.mkdir(exist_ok=True)
        external = live_acceptance.ExternalEndpoint(
            os.environ["RELAYMIND_EXTERNAL_BASE_URL"], self.args.model, self.args.context_limit
        )
        config = gateway_dir / "relaymind.toml"
        config.write_text(
            live_acceptance.gateway_config(
                work=gateway_dir,
                manifest=None,
                artifact=None,
                local_url=None,
                port=live_acceptance._free_port(),
                external=external,
                modes=("auto", "external_only"),
                local_model=False,
            ),
            "utf-8",
        )
        process = live_acceptance.GatewayProcess(
            config, gateway_dir / "gateway.log", checkout_environment(ROOT, dict(os.environ))
        )
        output = process.start()
        self.gateway = process
        self.store = gateway_dir / "runs.sqlite3"
        self.relay = self.host("relay-home", False)
        handle = m9.GatewayHandle(
            output.url.rstrip("/") + "/v1",
            output.token,
            lambda: [],
            lambda ids: [],
            process.stop,
            {},
        )
        steps, plugin, ok = m9.setup_relaymind(
            "relaymind_external_only", self.relay, handle, self.args.tarball, {}
        )
        self.relay.merge_config({"tools": {"exec": {"mode": "deny"}}})
        if not ok:
            raise SystemExit("relay_setup_failed")

    def close(self):
        if self.gateway is not None:
            self.gateway.stop()
        for path in (self.native.config_path, self.work / "gateway" / "relaymind.toml"):
            path.unlink(missing_ok=True)

    # -- one row -------------------------------------------------------------------
    def run_ids(self):
        conn = sqlite3.connect(self.store.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return [r[0] for r in conn.execute("SELECT run_id FROM runs ORDER BY rowid")]
        finally:
            conn.close()

    def sanitize(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "<omitted>")
        return text.replace(str(self.work), "<run-dir>")

    def evidence(self, key: str, name: str, value, refs: list) -> str:
        payload = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
        payload = self.sanitize(payload.decode("utf-8")).encode("utf-8")
        relative = f"host-runs/{key}/{name}"
        digest = write_bytes(self.evidence_root / relative, payload)
        evidence_id = f"{key}:{name}"
        refs.append(dict(id=evidence_id, path=relative, provenance="observed", **digest))
        return evidence_id

    def transcript_usage(self, state_dir: Path) -> list[dict]:
        found = []
        for db in state_dir.rglob("openclaw-agent.sqlite"):
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                rows = conn.execute("SELECT * FROM transcript_events").fetchall()
            finally:
                conn.close()
            for row in rows:
                for cell in row:
                    if not isinstance(cell, str) or '"usage"' not in cell:
                        continue
                    try:
                        value = json.loads(cell)
                    except ValueError:
                        continue
                    stack = [value]
                    while stack:
                        item = stack.pop()
                        if isinstance(item, dict):
                            if isinstance(item.get("usage"), dict) and "input" in item["usage"]:
                                found.append(
                                    dict(
                                        responseId=item.get("responseId"),
                                        timestamp=item.get("timestamp"),
                                        model=item.get("model"),
                                        usage=item["usage"],
                                    )
                                )
                            stack.extend(item.values())
                        elif isinstance(item, list):
                            stack.extend(item)
        return found

    def not_run(self, task, arm, reason):
        return dict(
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
            reason=reason,
        ), []

    def execute(self, task, arm, config):
        if arm == "recommendations":
            return self.not_run(task, arm, "person_choice_unavailable")
        if self.spent() + self.args.row_estimate > self.args.cap:
            return self.not_run(task, arm, "budget_cap")
        key = f"{task['id']}-{arm}"
        host = self.native if arm == "native" else self.relay
        model = f"{DIRECT}/{self.args.model}" if arm == "native" else install_check.MODEL_REF
        sandbox = self.work / "sandboxes" / key
        state = self.work / "state" / key
        for path in (sandbox, state):
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True)
        before = set(self.run_ids()) if arm != "native" else set()
        prompt = self.cases[task["prompt_case_id"]]["task_text"]
        started = now()
        result = host.run(
            [
                "agent",
                "exec",
                prompt,
                "--model",
                model,
                "--json",
                "--config",
                str(host.config_path),
                "--cwd",
                str(sandbox),
                "--state-dir",
                str(state),
            ],
            timeout=TIMEOUT,
        )
        ended = now()
        refs: list = []
        report = None
        for line in reversed(result["stdout"].strip().splitlines() or [""]):
            try:
                report = json.loads(result["stdout"].strip())
                break
            except ValueError:
                try:
                    report = json.loads(line)
                    break
                except ValueError:
                    continue
        host_id = self.evidence(
            key,
            "host-result.json",
            dict(
                argv_tail=["agent", "exec", "<prompt>", "--model", model, "--json"],
                status=result["status"],
                exit_code=result["exit_code"],
                elapsed_ms=result["elapsed_ms"],
                report=report,
                stderr_tail=result["stderr"][-4000:],
                started_at=started,
                ended_at=ended,
            ),
            refs,
        )
        attempts = []
        local_turns = None
        if arm == "native":
            usages = self.transcript_usage(state)
            usage_id = self.evidence(key, "transcript-usage.json", usages, refs)
            prompt_total = completion_total = 0
            for n, item in enumerate(usages, 1):
                u = item["usage"]
                prompt_total += u.get("input") or 0
                completion_total += u.get("output") or 0
                attempts.append(
                    dict(
                        id=f"{key}-{n}",
                        ordinal=n,
                        model=item.get("model") or self.args.model,
                        provider=PROVIDER,
                        external=True,
                        started_at=None,
                        ended_at=None,
                        input_tokens=u.get("input"),
                        output_tokens=u.get("output"),
                        cached_tokens=u.get("cacheRead"),
                        reasoning_tokens=u.get("reasoningTokens"),
                        total_tokens=None,
                        token_relationship="unknown",
                        calls=1 if item.get("responseId") else None,
                        usage_provenance="reported",
                        evidence_ids=[usage_id],
                        outcome="success",
                    )
                )
            local_turns = 0
            self.charge(arm, prompt_total, completion_total)
        else:
            time.sleep(1.0)
            window = [i for i in self.run_ids() if i not in before]
            traces = live_acceptance._traces(self.store, window)
            events = [e for i in window for e in traces[i]]
            trace_id = self.evidence(key, "gateway-trace.json", events, refs)
            attempts = attempts_from_trace(
                events, provider=PROVIDER, attempt_prefix=f"{key}-", evidence_id=trace_id
            )
            self.charge(
                arm,
                sum(a["input_tokens"] or 0 for a in attempts),
                sum(a["output_tokens"] or 0 for a in attempts),
            )
            local_turns = sum(
                1 for e in events if e["event_type"] == "model.started" and e["actor"] == "local"
            )
        status = {"completed": "completed", "timed_out": "timeout"}.get(result["status"], "failed")
        if status == "completed" and not (isinstance(report, dict) and report.get("ok")):
            status = "failed"
        row = dict(
            task_id=task["id"],
            arm=arm,
            status=status,
            success=None,
            attempts=attempts,
            elapsed_ms=float(result["elapsed_ms"]),
            active_ms=None,
            human_wait_ms=0.0,
            interactions=0,
            local_turns=local_turns,
            cpu_ms=None,
            peak_bytes=None,
            recommendation_ids=[],
            host_session_id=(report or {}).get("sessionId") if isinstance(report, dict) else None,
            evidence_ids=sorted({host_id, *(i for a in attempts for i in a["evidence_ids"])}),
            reason=None if status == "completed" else "host_failed",
        )
        return row, refs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "tasks",
        "freeze",
        "cases",
        "journal",
        "environment",
        "ledger",
        "tarball",
        "openclaw",
        "work",
        "evidence",
        "output",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--context-limit", type=int, default=128000)
    parser.add_argument("--cap", type=int, default=5_000_000)
    parser.add_argument("--row-estimate", type=int, default=40000)
    args = parser.parse_args()
    run = Run(args)
    stop = False

    def interrupt(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    run.setup()
    try:
        report = run_host_plan(
            read(args.tasks),
            read(args.freeze),
            run.execute,
            args.journal,
            environment=read(args.environment),
            artifacts=[],
            qualification="provisional",
            input_manifest=dict(path=None, digest="0" * 64, bytes=0),
            interrupted=lambda: stop,
        )
    finally:
        run.close()
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(json.dumps(dict(summary=report["summary"]["arms"], spent=run.spent())))


if __name__ == "__main__":
    main()
