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
external-token cap is enforced per row and per request: a row starts only if
its whole reservation fits, and that reservation is written durably, keyed to
the journal row, before anything is dispatched. Both arms reach the provider
only through a loopback ``RequestGate`` that admits each request before
dispatch: its output is bounded by ``max_tokens`` and its body bytes plus that
bound must fit in what is left of the reservation, or it is refused (closed
reason) and the host turn is stopped (``cancelled``, ``budget_cap``). Unknown
consumption is charged at the request's bound, and a row that is never settled
keeps its whole reservation charged, also after a resume. A row that cannot
start is ``not_run`` with reason ``budget_cap``. Credentials and the provider
endpoint live only in private files of the run directory, which are deleted at
the end, and in this process.

The host freeze is enforced: before setup, the executed arms' frozen model,
provider and provider, sampling and host config digests must equal the
committed documents in ``--configs`` and ``--model``; each arm's host is
configured from its frozen host document (secrets filled in from the
environment, the gateway on the frozen ``baseUrl``), and the effective host
config is checked against that document after setup. Every row's config must
be the verified frozen configuration of its arm. A mismatch rejects with a
closed reason (``FreezeMismatch``).
"""

import argparse
import hashlib
import http.server
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(HERE))

import harness  # noqa: E402
import live_acceptance  # noqa: E402
import openclaw_install_check as install_check  # noqa: E402

from relaymind.canonical import canonical_digest, canonical_json  # noqa: E402
from relaymind.evaluation.asset_host_runner import (  # noqa: E402
    attempts_from_trace,
    run_host_plan,
)
from relaymind.evaluation.environment import checkout_environment  # noqa: E402

PROVIDER = "external-openai-compatible"
DIRECT = "external"
TIMEOUT = 600.0
EXECUTED_ARMS = ("native", "provider_only")
SECRETS_KEY = "omitted_secret_fields"
# The shortest tail of the run directory path that is still redacted.
MIN_PATH_TAIL = 16


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


class FreezeMismatch(ValueError):
    """The executed configuration is not the one the host freeze binds."""


def frozen_arm_configs(freeze, configs_dir: Path, model: str) -> dict[str, dict]:
    """The frozen configuration and host document of each executed arm.

    The model must be the freeze's, and the committed provider, sampling and
    host documents must have the freeze's digests.
    """
    configs_dir = Path(configs_dir)
    provider, sampling = read(configs_dir / "provider.json"), read(configs_dir / "sampling.json")
    entries = {c["arm"]: c for c in freeze["configurations"]}
    frozen = {}
    for arm in EXECUTED_ARMS:
        config = entries.get(arm)
        if config is None:
            raise FreezeMismatch("freeze_arm_missing")
        if config["provider"] != PROVIDER or provider.get("provider") != PROVIDER:
            raise FreezeMismatch("provider_mismatch")
        if not (model == config["model"] == provider.get("model")):
            raise FreezeMismatch("model_mismatch")
        host = read(configs_dir / f"host-{arm}.json")
        if (
            canonical_digest(provider) != config["provider_config_digest"]
            or canonical_digest(sampling) != config["sampling_config_digest"]
            or canonical_digest(host) != config["host_config_digest"]
        ):
            raise FreezeMismatch("config_digest_mismatch")
        frozen[arm] = dict(config=config, host=host)
    return frozen


def realize(frozen, secrets) -> dict:
    """The frozen host document with its omitted secret fields filled in."""
    if isinstance(frozen, dict):
        value = {k: realize(v, secrets) for k, v in frozen.items() if k != SECRETS_KEY}
        for name in frozen.get(SECRETS_KEY, ()):
            if name not in secrets:
                raise FreezeMismatch("secret_unavailable")
            value[name] = secrets[name]
        return value
    if isinstance(frozen, list):
        return [realize(v, secrets) for v in frozen]
    return frozen


def check_effective(frozen, effective, path=()) -> None:
    """The effective host config must carry the frozen document exactly.

    Every frozen value must be present and equal; a frozen object that lists
    omitted secret fields, and every plugin ``config`` object, is closed: it
    has exactly the frozen keys and the named secrets. Host-owned keys outside
    those objects are allowed.
    """
    if isinstance(frozen, dict):
        if not isinstance(effective, dict):
            raise FreezeMismatch("host_config_mismatch")
        keys = {k for k in frozen if k != SECRETS_KEY}
        secrets = set(frozen.get(SECRETS_KEY, ()))
        closed = SECRETS_KEY in frozen or path[-1:] == ("config",)
        if not secrets <= set(effective) or (closed and set(effective) != keys | secrets):
            raise FreezeMismatch("host_config_mismatch")
        for key in keys:
            if key not in effective:
                raise FreezeMismatch("host_config_mismatch")
            check_effective(frozen[key], effective[key], (*path, key))
    elif canonical_json(frozen) != canonical_json(effective):
        raise FreezeMismatch("host_config_mismatch")


def sanitize(text: str, *, secrets, work) -> str:
    """Remove secrets and the run directory path, also when the host cut its head off."""
    for secret in secrets:
        text = text.replace(secret, "<omitted>")
    work = str(work)
    text = text.replace(work, "<run-dir>")
    for start in range(1, len(work) - MIN_PATH_TAIL + 1):
        tail = work[start:]
        if tail in text:
            text = text.replace(tail, "<run-dir>")
    return text


class Budget:
    """The external-token cap over rows, with a durable reservation per row.

    A row starts only when ``spent + row_budget <= cap``; its whole reservation
    is written (fsynced) to the ledger, keyed to the journal row, before any
    request is dispatched. Settlement replaces it by the row's gated charge,
    once. A row that is never settled (a crash, a usage-read or evidence-write
    failure) keeps its whole reservation charged, also after a resume.
    """

    def __init__(self, ledger: Path, *, cap: int, row_budget: int, max_output: int):
        if not 0 < max_output < row_budget:
            raise ValueError("budget_allowances")
        self.path, self.cap = Path(ledger), cap
        self.row_budget, self.max_output = row_budget, max_output
        # A fresh ledger starts at zero; an existing one carries its spending.
        self.data = (
            read(self.path)
            if self.path.exists()
            else dict(total_input=0, total_output=0, calls=0, stages={})
        )
        self.data.setdefault("rows", {})

    def spent(self) -> int:
        d = self.data
        held = sum(r["amount"] for r in d["rows"].values() if r["state"] != "settled")
        return d["total_input"] + d["total_output"] + d.get("unlogged_estimate", 0) + held

    def can_start(self) -> bool:
        return self.spent() + self.row_budget <= self.cap

    def reserve(self, key: str) -> str | None:
        """Reserve the row's budget durably; the closed reason when it cannot start."""
        if key in self.data["rows"]:
            return "row_reserved"
        if not self.can_start():
            return "budget_cap"
        self.data["rows"][key] = dict(state="reserved", amount=self.row_budget)
        harness.durable_write(self.path, self.data)
        return None

    def settle(self, key: str, arm: str, *, prompt: int, completion: int, charged: int):
        """Replace the row's reservation by its charge, once.

        ``charged`` is the gate's charge: reported usage, and the admitted bound
        of every call without reported usage. Reported usage above it counts.
        """
        held = self.data["rows"][key]
        if held["state"] == "settled":
            return
        charge = max(charged, prompt + completion)
        extra = charge - prompt - completion
        d = self.data
        d["total_input"] += prompt + extra
        d["total_output"] += completion
        row = d.setdefault("stages", {}).setdefault(f"host_{arm}", {})
        for name in ("input", "output", "conservative", "calls"):
            row.setdefault(name, 0)
        row["input"] += prompt
        row["output"] += completion
        row["conservative"] += extra
        row["calls"] += 1
        held.update(state="settled", amount=charge)
        harness.durable_write(self.path, d)


class RequestGate:
    """A loopback proxy in front of the external provider for both arms.

    Every request is admitted before dispatch: its output is bounded by
    ``max_tokens`` (at most ``max_output``) and its bound, the request body
    bytes plus that output bound (``harness.request_bound``), must fit in what
    is left of the open row's reservation. The bound is charged on admission
    and replaced by the reported usage when the response carries it; a call
    whose usage is not reported (a stream without usage, an error, a timeout)
    stays charged at its bound. A request that could pass the reservation, or
    that arrives with no open row, is refused with HTTP 400 and a closed reason
    (``request_budget_exceeded``, ``no_reservation``, ``request_invalid``) and
    never reaches the provider.
    """

    def __init__(self, upstream: str, *, max_output: int, timeout: float = TIMEOUT):
        self.upstream = upstream.rstrip("/")
        self.max_output, self.timeout = max_output, timeout
        self.lock = threading.Lock()
        self.row = None
        self.server = None

    # -- the row ------------------------------------------------------------------
    def open_row(self, key: str, limit: int) -> None:
        with self.lock:
            self.row = dict(
                key=key, limit=limit, charged=0, prompt=0, completion=0, calls=[], refused=[]
            )

    def close_row(self) -> dict:
        """The row's charge; a call still in flight keeps its admitted bound."""
        with self.lock:
            row, self.row = self.row, None
        return dict(row, calls=[dict(c) for c in row["calls"]], refused=list(row["refused"]))

    @property
    def refused(self) -> bool:
        with self.lock:
            return bool(self.row and self.row["refused"])

    def charged(self) -> int:
        with self.lock:
            return self.row["charged"] if self.row else 0

    def admit(self, body: bytes):
        """The body to dispatch and its call record, or the closed refusal reason."""
        with self.lock:
            row = self.row
            if row is None:
                return None, "no_reservation"
            try:
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError
                output = self.max_output
                for name in ("max_tokens", "max_completion_tokens"):
                    asked = value.get(name)
                    if asked is None:
                        continue
                    if type(asked) is not int or asked <= 0:
                        raise ValueError
                    output = min(output, asked)
                value["max_tokens"] = output
                if "max_completion_tokens" in value:
                    value["max_completion_tokens"] = output
            except ValueError:
                row["refused"].append("request_invalid")
                return None, "request_invalid"
            payload = json.dumps(value).encode("utf-8")
            bound = harness.request_bound(payload, output)
            if row["charged"] + bound > row["limit"]:
                row["refused"].append("request_budget_exceeded")
                return None, "request_budget_exceeded"
            call = dict(bound=bound, usage=None)
            row["charged"] += bound
            row["calls"].append(call)
            return (payload, row, call), None

    def account(self, row, call, raw: bytes) -> None:
        usage = response_usage(raw)
        with self.lock:
            if usage is None or self.row is not row:
                # Unknown, or the row closed while the call was in flight: the bound stays.
                return
            call["usage"] = usage
            row["charged"] += usage[0] + usage[1] - call["bound"]
            row["prompt"] += usage[0]
            row["completion"] += usage[1]

    # -- the server -----------------------------------------------------------------
    def start(self) -> str:
        gate = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                admitted, reason = gate.admit(body)
                if admitted is None:
                    error = dict(error=dict(type="budget_cap", code=reason, message=reason))
                    self._reply(400, "application/json", json.dumps(error).encode("utf-8"))
                    return
                payload, row, call = admitted
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() in ("authorization", "content-type", "accept")
                }
                suffix = self.path.removeprefix("/v1")
                request = urllib.request.Request(
                    gate.upstream + suffix, data=payload, headers=headers, method="POST"
                )
                chunks = []
                try:
                    try:
                        response = urllib.request.urlopen(request, timeout=gate.timeout)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        self.send_response(getattr(response, "status", None) or response.code)
                        kind = response.headers.get("Content-Type", "application/json")
                        self.send_header("Content-Type", kind)
                        self.end_headers()
                        reader = getattr(response, "read1", response.read)
                        while chunk := reader(65536):
                            chunks.append(chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except OSError:
                    # A failed or cut-off call: its consumption is unknown.
                    if not chunks:
                        try:
                            self._reply(502, "application/json", b'{"error":"upstream"}')
                        except OSError:
                            pass
                    return
                gate.account(row, call, b"".join(chunks))

            def _reply(self, status, kind, data):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.url

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()


def response_usage(raw: bytes):
    """``(prompt, completion)`` the provider reported, from JSON or an event stream."""
    text = raw.decode("utf-8", "replace")
    try:
        documents = [json.loads(text)]
    except ValueError:
        documents = []
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    documents.append(json.loads(line[5:].strip()))
                except ValueError:
                    continue
    found = None
    for document in documents:
        usage = document.get("usage") if isinstance(document, dict) else None
        if isinstance(usage, dict) and type(usage.get("prompt_tokens")) is int:
            completion = usage.get("completion_tokens")
            found = (usage["prompt_tokens"], completion if type(completion) is int else 0)
    return found


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
        try:
            while process.poll() is None:
                used = usage()
                if limit_reached(used):
                    status, stopped_at = "budget_stopped", used
                elif time.monotonic() - started > timeout:
                    status = "timed_out"
                if status is not None:
                    break
                time.sleep(poll)
        finally:
            # Stopped, or a usage read failed: the host turn never outlives this call.
            if process.poll() is None:
                _stop(process)
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


def _stop(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        process.wait()


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
        # Only the frozen model and configuration documents are executed.
        self.frozen = frozen_arm_configs(read(args.freeze), args.configs, args.model)
        self.secrets = [
            os.environ["RELAYMIND_EXTERNAL_API_KEY"],
            os.environ["RELAYMIND_EXTERNAL_BASE_URL"],
        ]
        self.budget = Budget(
            args.ledger,
            cap=args.cap,
            row_budget=args.row_budget,
            max_output=args.max_output,
        )
        self.gateway = None
        self.gate = None

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
        # Both arms reach the provider only through the gate.
        self.gate = RequestGate(
            os.environ["RELAYMIND_EXTERNAL_BASE_URL"], max_output=self.budget.max_output
        )
        gate_url = self.gate.start()
        native_frozen = self.frozen["native"]["host"]
        relay_frozen = self.frozen["provider_only"]["host"]
        model = self.frozen["provider_only"]["config"]["model"]
        self.native = self.host("native-home", True)
        if self.native.run(["--version"], timeout=m9.SETUP_TIMEOUT)["status"] != "completed":
            raise SystemExit("native_setup_failed")
        self.native.merge_config(
            realize(
                native_frozen,
                dict(
                    apiKey=os.environ["RELAYMIND_EXTERNAL_API_KEY"],
                    baseUrl=gate_url,
                ),
            )
        )
        check_effective(native_frozen, read(self.native.config_path))
        plugin = relay_frozen["plugins"]["entries"][m9.PLUGIN_ID]["config"]
        port = urlsplit(plugin["baseUrl"]).port
        gateway_dir = self.work / "gateway"
        gateway_dir.mkdir(exist_ok=True)
        external = live_acceptance.ExternalEndpoint(gate_url, model, self.args.context_limit)
        config = gateway_dir / "relaymind.toml"
        config.write_text(
            live_acceptance.gateway_config(
                work=gateway_dir,
                manifest=None,
                artifact=None,
                local_url=None,
                # The frozen plugin config names the gateway's loopback address.
                port=port,
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
        handle = m9.GatewayHandle(
            output.url.rstrip("/") + "/v1",
            output.token,
            lambda: [],
            lambda ids: [],
            process.stop,
            {},
        )
        if handle.base_url != plugin["baseUrl"]:
            raise FreezeMismatch("gateway_url_mismatch")
        self.relay = self.host("relay-home", False)
        steps, _, ok = m9.setup_relaymind(
            "relaymind_external_only", self.relay, handle, self.args.tarball, {}
        )
        if not ok:
            raise SystemExit("relay_setup_failed")
        # The plugin config is exactly the frozen one (no diagnostics file).
        effective = read(self.relay.config_path)
        effective["plugins"]["entries"][m9.PLUGIN_ID]["config"] = realize(plugin, {})
        self.relay.config_path.write_text(json.dumps(effective, indent=2), "utf-8")
        self.relay.merge_config(realize(relay_frozen, {}))
        check_effective(relay_frozen, read(self.relay.config_path))

    def close(self):
        if self.gateway is not None:
            self.gateway.stop()
        if self.gate is not None:
            self.gate.stop()
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
        return sanitize(text, secrets=self.secrets, work=self.work)

    def arm_config(self, arm: str, config) -> dict:
        """The row's config, which must be the verified frozen configuration of its arm."""
        frozen = self.frozen[arm]["config"]
        if canonical_json(config) != canonical_json(frozen):
            raise FreezeMismatch("freeze_config_mismatch")
        return frozen

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

    def gateway_events(self, before: set) -> list:
        window = [i for i in self.run_ids() if i not in before]
        traces = live_acceptance._traces(self.store, window)
        return [e for i in window for e in traces[i]]

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
        config = self.arm_config(arm, config)
        key = f"{task['id']}-{arm}"
        # The whole row budget is reserved durably before anything is dispatched;
        # a row that is never settled keeps it charged.
        if self.budget.reserve(key) is not None:
            return self.not_run(task, arm, "budget_cap")
        host = self.native if arm == "native" else self.relay
        model = f"{DIRECT}/{config['model']}" if arm == "native" else install_check.MODEL_REF
        sandbox = self.work / "sandboxes" / key
        state = self.work / "state" / key
        for path in (sandbox, state):
            shutil.rmtree(path, ignore_errors=True)
            path.mkdir(parents=True)
        before = set(self.run_ids()) if arm != "native" else set()
        prompt = self.prompts[task["prompt_case_id"]]
        started = now()
        self.gate.open_row(key, self.budget.row_budget)
        try:
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
                usage=self.gate.charged,
                # A refused request stops the turn: the row's reservation is spent.
                limit_reached=lambda used: self.gate.refused,
            )
        finally:
            gated = self.gate.close_row()
        # Settled from the gate's charge, before any evidence is written.
        self.budget.settle(
            key,
            arm,
            prompt=gated["prompt"],
            completion=gated["completion"],
            charged=gated["charged"],
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
            for n, item in enumerate(usages, 1):
                u = item["usage"]
                attempts.append(
                    dict(
                        id=f"{key}-{n}",
                        ordinal=n,
                        model=item.get("model") or config["model"],
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
        else:
            time.sleep(1.0)
            events = self.gateway_events(before)
            trace_id = self.evidence(key, "gateway-trace.json", events, refs)
            attempts = attempts_from_trace(
                events, provider=PROVIDER, attempt_prefix=f"{key}-", evidence_id=trace_id
            )
            local_turns = sum(
                1 for e in events if e["event_type"] == "model.started" and e["actor"] == "local"
            )
        status = {
            "completed": "completed",
            "timed_out": "timeout",
            "budget_stopped": "cancelled",
        }.get(result["status"], "failed")
        if gated["refused"]:
            # The gate refused a request: the row stopped at its reservation.
            status = "cancelled"
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
    parser.add_argument("--model", required=True, help="must be the host freeze's model")
    parser.add_argument("--configs", type=Path, default=HERE / "configs")
    parser.add_argument("--context-limit", type=int, default=128000)
    parser.add_argument("--cap", type=int, default=5_000_000)
    # Observed rows reached 245k tokens. Every request is gated before dispatch:
    # its body bytes plus its output bound (max_tokens) must fit in the row's
    # remaining reservation.
    parser.add_argument("--row-budget", type=int, default=250_000)
    parser.add_argument("--max-output", type=int, default=8_192)
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
