"""Fixed M8 calibration/matrix driver. Plans are offline; --execute starts inference."""

import argparse
import importlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from evaluation_config import configuration_from_args  # noqa: E402
from evaluation_session import prerequisites  # noqa: E402

from relaymind.canonical import canonical_digest  # noqa: E402
from relaymind.evaluation.checkpoint import atomic_json, utc_now  # noqa: E402
from relaymind.evaluation.environment import checkout_environment, source_fingerprint  # noqa: E402
from relaymind.evaluation.prior import byte_digest, load_prior  # noqa: E402
from relaymind.store import RunStore  # noqa: E402

RUNTIME_ENV = "RELAYMIND_RUNTIME"


def resolve_runtime(given: Path | None) -> Path:
    """The llama-server to measure: --runtime, else RELAYMIND_RUNTIME, else the one on PATH."""
    if given is not None:
        return given
    found = os.environ.get(RUNTIME_ENV) or shutil.which("llama-server")
    if not found:
        raise ValueError(f"pass --runtime, set {RUNTIME_ENV}, or put llama-server on PATH")
    return Path(found)


def matrix_cells() -> list[dict]:
    cells = []

    def add(phase, configuration, policy, chain, stream, runner):
        cells.append(
            dict(
                id="-".join((phase, configuration, policy, chain, stream, runner)),
                phase=phase,
                configuration=configuration,
                policy=policy,
                chain=chain,
                stream=stream,
                runner=runner,
                requests=(104 if configuration == "none" else 156) if runner == "corpus" else 124,
            )
        )

    for config in ("normal", "constrained"):
        add("calibration", config, "local_first", "router", "buffered", "corpus")
    for config in ("normal", "constrained"):
        for policy in ("local_first", "device_aware", "expected_wait"):
            for chain in ("router", "example-router"):
                for runner in ("corpus", "latency"):
                    add("placement", config, policy, chain, "buffered", runner)
    add("streaming", "normal", "local_first", "router", "live", "corpus")
    for stream in ("buffered", "live"):
        for runner in ("corpus", "latency"):
            add("streaming", "none", "local_first", "example", stream, runner)
    return cells


def declare_restart(index: dict, cell: dict, name: str) -> dict:
    attempts = index["attempts"].setdefault(cell["id"], [])
    if (
        not name
        or any(a["name"] == name for a in attempts)
        or (attempts and attempts[-1]["status"] == "complete")
    ):
        raise ValueError("restart requires a unique name and an unfinished prior attempt")
    attempt = dict(
        name=name,
        status="declared",
        declared_at_utc=utc_now(),
        reason="explicit infrastructure recovery" if attempts else "initial attempt",
    )
    attempts.append(attempt)
    return attempt


def provider_environment(path: Path | None) -> dict[str, str]:
    env = checkout_environment(ROOT, dict(os.environ))
    allowed = {
        "RELAYMIND_EXTERNAL_API_KEY",
        "RELAYMIND_EXTERNAL_BASE_URL",
        "RELAYMIND_EXTERNAL_MODEL",
    }
    if path:
        for line in path.read_text("utf-8").splitlines():
            parts = shlex.split(line, comments=True)
            if not parts:
                continue
            if parts[0] == "export":
                parts = parts[1:]
            if len(parts) != 1 or "=" not in parts[0]:
                raise ValueError("provider environment requires literal KEY=value lines")
            key, _, value = parts[0].partition("=")
            if key not in allowed or "$" in value or "`" in value:
                raise ValueError("provider environment contains unsupported assignment")
            env[key] = value
    if not all(env.get(k) for k in allowed):
        raise ValueError("complete provider endpoint/model/key configuration required")
    endpoint = urlsplit(env["RELAYMIND_EXTERNAL_BASE_URL"])
    if (
        endpoint.scheme not in ("http", "https")
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError(
            "provider endpoint must be an absolute HTTP URL without credentials, query or fragment"
        )
    return env


def freeze_prior(export: Path, out: Path) -> None:
    document = json.loads(export.read_text("utf-8"))
    run_ids = [r["run_id"] for r in document["records"].values() if r.get("run_id") is not None]
    bundle = {
        "schema_version": 1,
        "kind": "relaymind.placement_prior",
        "partitions": [
            {
                "configuration": document["prior_configuration"],
                "include_outcomes": True,
                "sources": [
                    {
                        "file": os.path.relpath(export, out.parent),
                        "sha256": byte_digest(export),
                        "run_ids": run_ids,
                    }
                ],
            }
        ],
    }
    if out.exists():
        if json.loads(out.read_text()) != bundle:
            raise ValueError("frozen prior source changed")
    else:
        atomic_json(out, bundle)
    prior = load_prior(out, document["prior_configuration"])
    with RunStore(":memory:") as store:
        prior.seed(store)


def command(cell: dict, attempt_dir: Path, out: Path, env: dict) -> list[str]:
    runner = "run_corpus.py" if cell["runner"] == "corpus" else "latency_benchmark.py"
    argv = [
        sys.executable,
        str(ROOT / "scripts" / runner),
        "--out",
        str(attempt_dir / "export.json"),
        "--checkpoint",
        str(attempt_dir / "checkpoint.json"),
        "--external-base-url",
        env["RELAYMIND_EXTERNAL_BASE_URL"],
        "--external-model",
        env["RELAYMIND_EXTERNAL_MODEL"],
        "--external-context-limit",
        "128000",
        "--placement",
        "policy=" + cell["policy"],
        "--selection-chain",
        *(
            {
                "router": ["model_router"],
                "example-router": ["example_match", "model_router"],
                "example": ["example_match"],
            }[cell["chain"]]
        ),
        "--performance",
        "answer_pass=final",
        "calls_per_generation=one",
        "prefetch=false",
        "answer_stream=" + cell["stream"],
        "early_headers=false",
    ]
    if cell["configuration"] == "none":
        argv += ["--no-local-model"]
    else:
        argv += ["--manifest", str(ROOT / "models/qwen3-0.6b-q8_0.manifest.json")]
        if cell["configuration"] == "constrained":
            argv += ["--runtime-extra-args=--n-gpu-layers 0 --threads 2"]
        if cell["phase"] != "calibration":
            argv += ["--timing-samples", str(out / ("prior-" + cell["configuration"] + ".json"))]
    if cell["runner"] == "corpus":
        argv += [
            "--modes",
            "auto",
            "external_only",
            *(["local_only"] if cell["configuration"] != "none" else []),
        ]
    else:
        argv += ["--runs", "10", "--timeout", "60", "--seed", "1342026"]
    return argv


def attempt_cost(directory: Path) -> dict:
    """Recover gateway-observed external cost even when a request never returned."""
    calls, finished, unreadable = {}, {}, []
    for path in directory.rglob("*.sqlite3"):
        try:
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
                for run_id, kind, actor, raw in db.execute(
                    "SELECT run_id,event_type,actor,payload FROM events"
                    " WHERE event_type IN ('model.started','model.finished')"
                ):
                    if actor != "external":
                        continue
                    payload = json.loads(raw)
                    key = (str(path), run_id, payload["call_id"])
                    if kind == "model.started":
                        calls[key] = payload
                    else:
                        finished[key] = payload.get("usage") or {}
        except (sqlite3.Error, ValueError):
            unreadable.append(str(path.relative_to(directory)))
    result = {"calls": len(calls), "unreadable_databases": unreadable}
    for name in ("input_tokens", "output_tokens"):
        known = [finished.get(key, {}).get(name) for key in calls]
        result[name + "_known_total"] = sum(n for n in known if n is not None)
        result[name + "_missing_calls"] = sum(n is None for n in known)
    return result


def reconcile_attempt(
    attempt: dict,
    cell: dict,
    directory: Path,
    expected_argv: list[str],
    source: dict,
) -> None:
    """Reconcile durable runner completion before considering another measured attempt."""
    # Read cost even if the prior process died before running an exception handler.
    attempt["cost"] = attempt_cost(directory)
    if attempt.get("argv") != expected_argv:
        raise ValueError("attempt command identity differs")
    checkpoint_path = directory / "checkpoint.json"
    if not checkpoint_path.exists():
        if attempt["status"] == "complete":
            raise ValueError("completed cell checkpoint missing")
        attempt["status"] = "interrupted"
        return
    checkpoint = json.loads(checkpoint_path.read_text("utf-8"))
    runner = importlib.import_module(
        "run_corpus" if cell["runner"] == "corpus" else "latency_benchmark"
    )
    args = runner.argument_parser().parse_args(expected_argv[2:])
    expected_arguments = json.loads(json.dumps(vars(args)))
    identity = checkpoint["identity"]
    settings = configuration_from_args(args)
    configuration = settings.to_json_object()
    if (
        checkpoint.get("schema_version") != 1
        or identity.get("arguments") != expected_arguments
        or identity.get("application") != source
        or identity.get("configuration") != configuration
    ):
        raise ValueError("runner checkpoint identity differs")
    prior = exported_prior = None
    if args.timing_samples:
        loaded = load_prior(Path(args.timing_samples), identity["prior_configuration"])
        with RunStore(":memory:") as store:
            loaded.seed(store)
            exported_prior = loaded.provenance
            if cell["runner"] == "latency":
                exported_prior = {
                    **exported_prior,
                    "initial_expected_wait_prerequisites": prerequisites(loaded, store),
                }
        prior = loaded.provenance
    if identity.get("prior") != prior:
        raise ValueError("runner checkpoint prior identity differs")
    if checkpoint.get("completed") is not True:
        if attempt["status"] == "complete":
            raise ValueError("completed cell has unfinished runner checkpoint")
        attempt["status"] = "interrupted"
        return
    steps = checkpoint["steps"]
    if (
        len(steps) != cell["requests"]
        or len({s["identity"] for s in steps}) != len(steps)
        or any(s.get("result") is None for s in steps)
    ):
        raise ValueError("finalized runner checkpoint request count differs")
    export = directory / "export.json"
    digest = byte_digest(export)
    if (
        digest != checkpoint["result"]["export_sha256"]
        or attempt.get("export_sha256", digest) != digest
        or attempt.get("checkpoint_sha256", byte_digest(checkpoint_path))
        != byte_digest(checkpoint_path)
    ):
        raise ValueError("finalized runner export/checkpoint digest differs")
    document = json.loads(export.read_text("utf-8"))
    exported_configuration = (
        runner.effective_configuration(settings, no_startup_warmup=args.no_startup_warmup)
        if cell["runner"] == "latency"
        else configuration
    )
    application = (
        document.get("application")
        if cell["runner"] == "corpus"
        else document.get("environment", {}).get("application")
    )
    if (
        application != source
        or document.get("configuration") != exported_configuration
        or document.get("prior_configuration") != identity["prior_configuration"]
        or document.get("timing_prior") != exported_prior
    ):
        raise ValueError("finalized runner export identity differs")
    count = (
        len(document["grades"])
        if cell["runner"] == "corpus"
        else len(document["samples"]) + len(document["warmups"]) + 1
    )
    if count != cell["requests"]:
        raise ValueError("cell did not execute its complete fixed request count")
    attempt.update(
        status="complete",
        finished_at_utc=checkpoint["finished_at_utc"],
        export_sha256=digest,
        checkpoint_sha256=byte_digest(checkpoint_path),
        requests=count,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--runtime",
        type=Path,
        help=f"the llama-server to measure (default: ${RUNTIME_ENV}, else llama-server on PATH)",
    )
    parser.add_argument("--provider-env", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--restart", metavar="CELL=ATTEMPT")
    args = parser.parse_args(argv)
    cells = matrix_cells()
    out = args.out.resolve()
    if out == ROOT or ROOT in out.parents:
        raise ValueError("matrix outputs must remain outside checkout")
    out.mkdir(parents=True, exist_ok=True)
    plan = dict(
        schema_version=1,
        cells=cells,
        run_order=[c["id"] for c in cells],
        corpus_runs=2548,
        latency_requests=1736,
        external_inference_ceiling=21024,
        token_ceiling=None,
        defaults_changed=False,
    )
    plan_path = out / "plan.json"
    if plan_path.exists() and json.loads(plan_path.read_text()) != plan:
        raise ValueError("matrix plan differs")
    atomic_json(plan_path, plan)
    if not args.execute:
        print(
            f"Offline plan: {len(cells)} cells; 2548 corpus runs; "
            "1736 latency requests. No inference started."
        )
        return
    source = source_fingerprint(ROOT)
    if source["dirty"] is not False:
        raise ValueError("execution requires the reviewed clean committed checkout")
    args.runtime = resolve_runtime(args.runtime)
    # Windows names are case-insensitive; shutil.which may return llama-server.EXE.
    if args.runtime.name.lower() not in ("llama-server", "llama-server.exe"):
        raise ValueError("runtime must name the llama-server executable selected through PATH")
    if not args.runtime.is_file():
        raise ValueError("pinned runtime binary missing")
    env = provider_environment(args.provider_env)
    env["PATH"] = str(args.runtime.parent) + os.pathsep + env.get("PATH", "")
    identity = dict(
        source=source,
        runtime_sha256=byte_digest(args.runtime),
        runtime=str(args.runtime.resolve()),
        provider={k: env[k] for k in ("RELAYMIND_EXTERNAL_BASE_URL", "RELAYMIND_EXTERNAL_MODEL")},
        context_limit=128000,
        plan_sha256=canonical_digest(plan),
    )
    index_path = out / "index.json"
    index = (
        json.loads(index_path.read_text())
        if index_path.exists()
        else dict(identity=identity, attempts={})
    )
    if index["identity"] != identity:
        raise ValueError("matrix source/runtime/provider/settings identity differs")
    restart = None
    if args.restart:
        restart = args.restart.split("=", 1)
        if len(restart) != 2 or restart[0] not in plan["run_order"]:
            raise ValueError("restart must name CELL=NEW_ATTEMPT")
    for cell in cells:
        attempts = index["attempts"].setdefault(cell["id"], [])
        prior_path = out / ("prior-" + cell["configuration"] + ".json")
        prior_digest = (
            byte_digest(prior_path)
            if cell["phase"] != "calibration" and cell["configuration"] != "none"
            else None
        )
        if attempts and attempts[-1].get("prior_sha256") != prior_digest:
            raise ValueError("cell prior digest differs")
        for previous_attempt in attempts:
            previous_dir = out / cell["id"] / previous_attempt["name"]
            try:
                reconcile_attempt(
                    previous_attempt,
                    cell,
                    previous_dir,
                    command(cell, previous_dir, out, env),
                    source,
                )
            finally:
                # Recovery costs survive even a later refusal to resume or an identity error.
                atomic_json(index_path, index)
        if any(a["status"] == "complete" for a in attempts[:-1]):
            raise ValueError("a prior attempt already finalized this cell")
        if attempts and attempts[-1]["status"] == "complete":
            previous = out / cell["id"] / attempts[-1]["name"] / "export.json"
            if byte_digest(previous) != attempts[-1]["export_sha256"]:
                raise ValueError("completed cell export changed")
            if cell["phase"] == "calibration":
                freeze_prior(previous, out / ("prior-" + cell["configuration"] + ".json"))
            continue
        if attempts and not (restart and restart[0] == cell["id"]):
            raise ValueError(
                "interrupted cell requires explicit --restart CELL=NEW_ATTEMPT; "
                "prior evidence is retained"
            )
        name = restart[1] if restart and restart[0] == cell["id"] else "initial"
        attempt = declare_restart(index, cell, name)
        attempt_dir = out / cell["id"] / name
        attempt_dir.mkdir(parents=True, exist_ok=False)
        argv = command(cell, attempt_dir, out, env)
        attempt.update(
            argv=argv, prior_sha256=prior_digest, status="started", started_at_utc=utc_now()
        )
        atomic_json(index_path, index)
        print(cell["id"] + " " + name, flush=True)
        try:
            with (attempt_dir / "runner.log").open("wb") as log:
                result = subprocess.run(
                    argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=False
                )
            if result.returncode:
                raise RuntimeError("runner failed; inspect retained checkpoint and logs")
            export = attempt_dir / "export.json"
            reconcile_attempt(attempt, cell, attempt_dir, argv, source)
            if attempt["status"] != "complete":
                raise ValueError("runner returned without a finalized checkpoint")
            atomic_json(index_path, index)
            if cell["phase"] == "calibration":
                freeze_prior(export, out / ("prior-" + cell["configuration"] + ".json"))
        except BaseException:
            attempt["cost"] = attempt_cost(attempt_dir)
            if attempt["status"] != "complete":
                attempt.update(status="interrupted", finished_at_utc=utc_now())
            atomic_json(index_path, index)
            raise
    print("Matrix exports complete; analysis/default decisions still require review.")


if __name__ == "__main__":
    main()
