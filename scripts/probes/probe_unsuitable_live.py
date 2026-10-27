"""Live check of unsuitable detection (#170; RM26; spec 0.48 section 14.4.7).

A short check of the class the gateway reports, not a performance run. It
starts the pinned runtime and, with ``placement.detect_stages = true``:

A. constrained, cold: a new runtime with the constrained extra arguments; the
   startup check should find the routing and skill warm-ups over
   active_seconds together. Two auto requests (English and Chinese) then show
   the class in run.accepted and the managed external route.
B. constrained, warm: a new gateway and store on the same runtime, so the
   warm-up is served from the runtime's prefix cache and fits. Three local_only
   requests then show local stages that run out of local time; readiness, one
   more auto request, and readiness again show the stage rule.
C. normal: a new runtime without extra arguments; the same three local_only
   requests and readiness show whether the normal configuration stays ready.

Each phase is one gateway launch. The export records the launcher lines (the
token is never recorded), readiness, and per request the mode, the state, the
class fields of run.accepted, the route reason, and the local stage evidence.
The external model is configured from the environment variables of
``scripts/live_acceptance.py`` (the key is read from the environment, never
written). Needs llama-server on PATH, the model artifact, and a provider.

Usage:
    python scripts/probes/probe_unsuitable_live.py --out FILE.json \\
        --external-base-url URL --external-model MODEL --external-context-limit N
"""

import argparse
import asyncio
import json
import os
import shlex
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRIPTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(SCRIPTS_DIR))

import live_acceptance as harness  # noqa: E402
from probe_timing import _free_port, _stop  # noqa: E402

CONSTRAINED = "--n-gpu-layers 0 --threads 2"
AUTO_PROMPTS = ("simple-02", "simple-02-zh")
# Single-skill prompts: simple-01 can route to the parallel workflow, which ends the
# local stage for another reason.
LOCAL_ONLY_PROMPTS = ("simple-03", "simple-04", "simple-05")


def corpus_prompts() -> dict[str, str]:
    document = json.loads((REPO_ROOT / "evaluation" / "corpus.v1.json").read_text("utf-8"))
    cases = document["cases"] if isinstance(document, dict) else document
    return {case["id"]: case["prompt"] for case in cases}


def run_record(case_id: str, mode: str, view: dict, events: list[dict]) -> dict[str, Any]:
    by_type: dict[str, dict] = {}
    for event in events:
        by_type.setdefault(event["event_type"], event["payload"])
    accepted = by_type.get("run.accepted", {})
    route = by_type.get("route.selected", {})
    end = (
        by_type.get("handoff.started")
        or by_type.get("run.completed")
        or by_type.get("run.failed", {})
    )
    return dict(
        case_id=case_id,
        mode=mode,
        run_id=view["run_id"],
        state=view["state"],
        local_model_status=accepted.get("local_model_status"),
        local_model_reason=accepted.get("local_model_reason"),
        route_reason_code=(route.get("decision") or {}).get("reason_code"),
        route_local_model_status=route.get("local_model_status"),
        local_stage_end=dict(
            reason_code=end.get("reason_code"),
            budget=end.get("budget"),
            local_active_ms=end.get("local_active_ms"),
        ),
        model_calls=[
            dict(actor=e["payload"].get("actor"), purpose=e["payload"].get("purpose"))
            for e in events
            if e["event_type"] == "model.started"
        ],
    )


async def requests(gateway, sandbox: Path, plan: list[tuple[str, str]], prompts) -> list[dict]:
    api = harness.Api(gateway.output.url, gateway.output.token, sandbox_dir=sandbox)
    records = []
    try:
        for case_id, mode in plan:
            response = await api.start(prompts[case_id], mode=mode)
            response.raise_for_status()
            view, events = await api.ended(response.json()["run_id"])
            records.append(run_record(case_id, mode, view, events))
            print(f"  {case_id} {mode}: {records[-1]['state']} {records[-1]['local_model_status']}")
    finally:
        await api.aclose()
    return records


def phase(name, runtime_url, runtime, external, env, work, plan, prompts, *, readiness_after):
    directory = work / name
    directory.mkdir()
    config = directory / "relaymind.toml"
    config.write_text(
        harness.gateway_config(
            work=directory,
            manifest=runtime.manifest_path,
            artifact=runtime.artifact,
            local_url=runtime_url,
            port=_free_port(),
            external=external,
            modes=("auto", "local_only", "external_only"),
            placement={"detect_stages": True},
        )
    )
    gateway = harness.GatewayProcess(config, directory / "gateway.log", env)
    output = gateway.start()
    print(f"phase {name}", flush=True)
    try:
        record = dict(
            launch_lines=output.recorded_lines,
            readiness_at_start=gateway.readiness()["body"]["local_model"],
        )
        record["runs"] = asyncio.run(requests(gateway, directory / "sandbox", plan, prompts))
        if readiness_after:
            record["readiness_after"] = gateway.readiness()["body"]["local_model"]
            extra = asyncio.run(
                requests(gateway, directory / "sandbox", [(AUTO_PROMPTS[0], "auto")], prompts)
            )
            record["runs"] += extra
            record["readiness_end"] = gateway.readiness()["body"]["local_model"]
        return record
    finally:
        gateway.stop()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--manifest", default=os.fspath(harness.DEFAULT_MANIFEST))
    parser.add_argument("--external-base-url", required=True)
    parser.add_argument("--external-model", required=True)
    parser.add_argument("--external-context-limit", type=int, required=True)
    args = parser.parse_args(argv)
    if args.out.exists():
        parser.error(f"{args.out} exists")
    prompts = corpus_prompts()
    external = harness.ExternalEndpoint(
        args.external_base_url, args.external_model, args.external_context_limit
    )
    if not os.environ.get(external.key_env):
        parser.error(f"{external.key_env} is not set")
    env = dict(os.environ)
    work = Path(tempfile.mkdtemp(prefix="relaymind-unsuitable-live-"))
    export: dict[str, Any] = dict(
        kind="relaymind.unsuitable_live_check",
        specification="0.48",
        started_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
        application=harness.application_revision(),
        external=dict(base_url=external.base_url, model=external.model),
        prompts={k: prompts[k] for k in (*AUTO_PROMPTS, *LOCAL_ONLY_PROMPTS)},
        phases={},
    )
    llama = runtime = url = None
    try:
        for name, extra_args, plan, after in (
            ("constrained-cold", CONSTRAINED, [(p, "auto") for p in AUTO_PROMPTS], False),
            ("constrained-warm", None, [(p, "local_only") for p in LOCAL_ONLY_PROMPTS], True),
            ("normal", "", [(p, "local_only") for p in LOCAL_ONLY_PROMPTS], True),
        ):
            if extra_args is not None:
                if llama is not None:
                    _stop(llama)
                runtime = harness.check_runtime(Path(args.manifest), shlex.split(extra_args))
                llama, url, cold_start = runtime.launch(work / f"llama-{name}.log")
                export.setdefault("runtimes", {})[name] = runtime.server_record(cold_start)
            export["phases"][name] = phase(
                name, url, runtime, external, env, work, plan, prompts, readiness_after=after
            )
        export["runtime_banner"] = runtime.banner
        model = runtime.model_record()
        model["manifest"] = Path(model["manifest"]).resolve().relative_to(REPO_ROOT).as_posix()
        export["model"] = model
    finally:
        if llama is not None:
            _stop(llama)
        shutil.rmtree(work, ignore_errors=True)
    export["finished_at_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(export, indent=2, ensure_ascii=False) + "\n", "utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
