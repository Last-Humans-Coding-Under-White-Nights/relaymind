"""Abort run for the Mac qualification (scratch scaffold, not repo code).

Serves the harness's gated doubles gateway (the `abort-buffered` scenario's
gateway: the local generation waits on a gate that never opens), runs the real
host's `agent exec` against it, sends SIGINT once the gateway has a run in
flight, and records the run's final state and events.

Usage: python abort_run.py <out.json> <sigint-delay-s> -- <command...>
"""

import json
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / "scripts"))
import openclaw_qualification as q  # noqa: E402

out, delay = Path(sys.argv[1]), float(sys.argv[2])
command = sys.argv[sys.argv.index("--") + 1 :]

with tempfile.TemporaryDirectory(prefix="openclaw-abort-") as tmp:
    scenario = next(s for s in q.SCENARIOS if s.name == "abort-buffered")
    gateway, app = q._gateway(scenario, Path(tmp))
    token = q.token_of(app)
    with q.RunningServer(app) as running:
        env = q.child_environment(running.base_url, token)
        t0 = time.monotonic()
        child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # Wait for the gateway to accept a run (the generation is then held).
        seen_at = None
        while time.monotonic() - t0 < 120:
            if q._run_ids(gateway):
                seen_at = time.monotonic() - t0
                break
            if child.poll() is not None:
                break
            time.sleep(0.05)
        state_before = None
        if seen_at is not None:
            time.sleep(delay)
            rid = q._run_ids(gateway)[0]
            state_before = gateway.store.get_run(rid).state
            sigint_at = time.monotonic() - t0
            child.send_signal(signal.SIGINT)
        else:
            sigint_at = None
        try:
            stdout, stderr = child.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
            stdout, stderr = child.communicate()
        exit_at = time.monotonic() - t0
        # Let the gateway settle the run after the disconnect.
        final, settle_ms = None, None
        for _ in range(100):
            ids = q._run_ids(gateway)
            if ids:
                final = gateway.store.get_run(ids[0]).state
                if str(final).lower().endswith(("cancelled", "completed", "failed")):
                    settle_ms = round((time.monotonic() - t0 - exit_at) * 1000)
                    break
            time.sleep(0.1)
        runs = [
            {
                "run_id": rid,
                "state": str(gateway.store.get_run(rid).state),
                "events": [{"type": e.event_type.value, "actor": getattr(e, "actor", None), "payload": e.payload} for e in gateway.store.events(rid)],
            }
            for rid in q._run_ids(gateway)
        ]
        export = {
            "kind": "openclaw-host-abort (scratch scaffold over the harness's abort-buffered gateway)",
            "evidence": "deterministic gateway doubles; not live model evidence",
            "command": command,
            "run_seen_after_s": seen_at,
            "state_before_sigint": str(state_before),
            "sigint_after_s": sigint_at,
            "exit_code": child.returncode,
            "host_exit_after_s": exit_at,
            "final_state": str(final),
            "settle_after_exit_ms": settle_ms,
            "adapter_cancelled": list(getattr(gateway.local_model.adapter, "cancelled", []) if hasattr(gateway, "local_model") else []),
            "stdout": stdout.replace(token, q.NOT_RECORDED)[-8000:],
            "stderr": stderr.replace(token, q.NOT_RECORDED)[-8000:],
            "runs": runs,
        }
    gateway.store.close()
text = json.dumps(export, indent=2, ensure_ascii=False, default=str)
assert token not in text
out.write_text(text + "\n", "utf-8")
print(json.dumps({k: export[k] for k in ("state_before_sigint", "sigint_after_s", "exit_code", "host_exit_after_s", "final_state", "settle_after_exit_ms")}))
