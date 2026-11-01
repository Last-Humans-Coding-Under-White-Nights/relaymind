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
verdict stays null here: blind success judging is a later, separate step.

Guards: prompts come only from the verified frozen corpus whose lock digest the
tasks and freeze name (checked before setup, on every start and resume). The
external-token cap is enforced per row: a row starts only if its whole
reservation fits, the host turn is stopped (``cancelled``, ``budget_cap``)
before another call could pass that reservation, and unknown consumption is
charged at its worst case. A row that cannot start is ``not_run`` with
reason ``budget_cap``. Credentials and the provider endpoint live only in private
files of the run directory, which are deleted at the end, and in this process.
"""

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
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


class CorpusMismatch(ValueError):
    """The corpus on disk is not the one the preregistered tasks and freeze name."""


def verified_prompts(tasks, freeze, lock_path: Path, evidence_root: Path) -> dict[str, str]:
    """Prompt text by case ID, only from the verified frozen corpus the plan names.

    The release verifier checks the lock and the exact catalog, cases and labels
    it commits; the canonical digest of the lock document must equal the
    ``corpus_lock_digest`` of both the tasks and the freeze, and every task must
    bind to its frozen case. An edited cases file therefore rejects, whether or
    not it was relocked, on the first start and on every resume.
    """
    from relaymind.canonical import canonical_digest
    from relaymind.evaluation.asset_corpus import load_asset_release
    from relaymind.evaluation.asset_whole_task_corpus import bind_host_tasks

    lock_path = Path(lock_path).absolute()
    digest = canonical_digest(read(lock_path))
    if not (digest == tasks["corpus_lock_digest"] == freeze["corpus_lock_digest"]):
        raise CorpusMismatch("corpus_lock_mismatch")
    release = load_asset_release(
        lock_path, evidence_root=Path(evidence_root).absolute(), related_roots={}
    )
    bind_host_tasks(tasks, release)
    cases = {case["id"]: case for case in release.cases["cases"]}
    return {
        task["prompt_case_id"]: cases[task["prompt_case_id"]]["task_text"]
        for task in tasks["tasks"]
    }


class Budget:
    """The external-token cap, enforced before a row starts and while it runs.

    A row starts only when ``spent + row_budget <= cap``. While it runs, the
    host turn is stopped once ``row_used + call_allowance >= row_budget``, so a
    call already in flight (at most ``call_allowance`` tokens: prompt plus
    bounded output) still fits in the reservation. Unknown consumption is
    charged at its worst case: ``call_allowance`` per dispatched call without
    reported usage, and the whole ``row_budget`` for a started row with no
    usage evidence at all.
    """

    def __init__(self, ledger: Path, *, cap: int, row_budget: int, call_allowance: int):
        if not 0 < call_allowance < row_budget:
            raise ValueError("budget_allowances")
        self.path, self.cap = Path(ledger), cap
        self.row_budget, self.call_allowance = row_budget, call_allowance
        self.data = read(self.path)

    def spent(self) -> int:
        d = self.data
        return d["total_input"] + d["total_output"] + d.get("unlogged_estimate", 0)

    def can_start(self) -> bool:
        return self.spent() + self.row_budget <= self.cap

    def row_limit_reached(self, used: int) -> bool:
        return used + self.call_allowance >= self.row_budget

    def settle(self, arm: str, *, prompt: int, completion: int, unknown_calls: int, evidence: bool):
        reserve = 0 if evidence else max(self.row_budget - prompt - completion, 0)
        extra = unknown_calls * self.call_allowance + reserve
        d = self.data
        d["total_input"] += prompt + extra
        d["total_output"] += completion
        row = d.setdefault("stages", {}).setdefault(f"host_{arm}", {})
        for key in ("input", "output", "conservative", "calls"):
            row.setdefault(key, 0)
        row["input"] += prompt
        row["output"] += completion
        row["conservative"] += extra
        row["calls"] += 1
        self.path.write_text(json.dumps(d, indent=1), encoding="utf-8")


def run_bounded(command, *, env, timeout, usage, limit_reached, poll=1.0) -> dict:
    """Run one host turn; stop it when the row's observed usage reaches its limit.

    Returns the same fields as the M9 ``Host.run`` plus ``row_tokens_at_stop``.
    ``status`` is ``completed``, ``failed``, ``timed_out`` or ``budget_stopped``.
    """
    import subprocess
    import tempfile

    started = time.monotonic()
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            # Own process group: stopping the turn also stops the host's children.
            process = subprocess.Popen(
                command, env=env, stdout=out, stderr=err, start_new_session=True
            )
        except OSError as error:
            return dict(
                argv=list(command),
                status="not_run",
                exit_code=None,
                elapsed_ms=0,
                stdout="",
                stderr=str(error),
                row_tokens_at_stop=None,
            )
        status, stopped_at = None, None
        while process.poll() is None:
            used = usage()
            if limit_reached(used):
                status, stopped_at = "budget_stopped", used
            elif time.monotonic() - started > timeout:
                status = "timed_out"
            if status is not None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                break
            time.sleep(poll)
        code = process.returncode
        if status is None:
            status = "completed" if code == 0 else "failed"
        out.seek(0)
        err.seek(0)
        return dict(
            argv=list(command),
            status=status,
            exit_code=code,
            elapsed_ms=round((time.monotonic() - started) * 1000),
            stdout=out.read().decode("utf-8", "replace"),
            stderr=err.read().decode("utf-8", "replace"),
            row_tokens_at_stop=stopped_at,
        )


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
        # Only prompts of the verified frozen corpus the plan names are dispatched.
        self.prompts = verified_prompts(
            read(args.tasks), read(args.freeze), args.lock, args.release_root
        )
        self.secrets = [
            os.environ["RELAYMIND_EXTERNAL_API_KEY"],
            os.environ["RELAYMIND_EXTERNAL_BASE_URL"],
        ]
        self.budget = Budget(
            args.ledger,
            cap=args.cap,
            row_budget=args.row_budget,
            call_allowance=args.call_allowance,
        )
        self.gateway = None

    def spent(self) -> int:
        return self.budget.spent()

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
                rows = conn.execute(
                    "SELECT event_json, event_zstd FROM transcript_events ORDER BY seq"
                ).fetchall()
            finally:
                conn.close()
            for text, packed in rows:
                if text is None and packed is not None:
                    # Large events are stored zstd-compressed by the pinned host.
                    text = subprocess.run(
                        ["zstd", "-dc"], input=packed, capture_output=True, check=True
                    ).stdout.decode("utf-8")
                for cell in (text,):
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
        unique, seen = [], set()
        for item in found:
            key = item["responseId"] or json.dumps(item, sort_keys=True)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def native_used(self, state: Path) -> int:
        """Provider tokens of the row so far, from the host's persisted usage."""
        total = 0
        for item in self.transcript_usage(state):
            u = item["usage"]
            total += (u.get("input") or 0) + (u.get("cacheRead") or 0) + (u.get("output") or 0)
        return total

    def gateway_events(self, before: set) -> list:
        window = [i for i in self.run_ids() if i not in before]
        traces = live_acceptance._traces(self.store, window)
        return [e for i in window for e in traces[i]]

    def provider_used(self, before: set) -> int:
        """Reported tokens of the row so far; a finished call without usage counts in full."""
        total = 0
        for event in self.gateway_events(before):
            if event["event_type"] != "model.finished" or event["actor"] != "external":
                continue
            usage = event["payload"].get("usage") or {}
            if usage.get("input_tokens") is None:
                total += self.budget.call_allowance
            else:
                total += usage["input_tokens"] + (usage.get("output_tokens") or 0)
        return total

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
        if not self.budget.can_start():
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
        prompt = self.prompts[task["prompt_case_id"]]
        started = now()
        if arm == "native":
            usage = lambda: self.native_used(state)  # noqa: E731
        else:
            usage = lambda: self.provider_used(before)  # noqa: E731
        result = run_bounded(
            [
                *host.command,
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
            env=host.env,
            timeout=TIMEOUT,
            usage=usage,
            limit_reached=self.budget.row_limit_reached,
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
                row_tokens_at_stop=result["row_tokens_at_stop"],
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
                # The provider bills cached reads as prompt tokens; the host
                # reports them apart from its input count.
                prompt_total += (u.get("input") or 0) + (u.get("cacheRead") or 0)
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
            # A turn that ended early may have lost one in-flight call's usage.
            interrupted = result["status"] != "completed"
            self.budget.settle(
                arm,
                prompt=prompt_total,
                completion=completion_total,
                unknown_calls=int(interrupted),
                evidence=bool(usages),
            )
        else:
            time.sleep(1.0)
            events = self.gateway_events(before)
            trace_id = self.evidence(key, "gateway-trace.json", events, refs)
            attempts = attempts_from_trace(
                events, provider=PROVIDER, attempt_prefix=f"{key}-", evidence_id=trace_id
            )
            self.budget.settle(
                arm,
                prompt=sum(a["input_tokens"] or 0 for a in attempts),
                completion=sum(a["output_tokens"] or 0 for a in attempts),
                unknown_calls=sum(a["input_tokens"] is None for a in attempts),
                evidence=bool(events),
            )
            local_turns = sum(
                1 for e in events if e["event_type"] == "model.started" and e["actor"] == "local"
            )
        status = {
            "completed": "completed",
            "timed_out": "timeout",
            "budget_stopped": "cancelled",
        }.get(result["status"], "failed")
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
            reason={"completed": None, "cancelled": "budget_cap"}.get(status, "host_failed"),
        )
        return row, refs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--context-limit", type=int, default=128000)
    parser.add_argument("--cap", type=int, default=5_000_000)
    # Observed rows reached 245k tokens; one call stays below 64k (context plus
    # bounded output). A row is stopped before another call could pass its budget.
    parser.add_argument("--row-budget", type=int, default=250_000)
    parser.add_argument("--call-allowance", type=int, default=64_000)
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
