"""Journaled adaptation episode run for #260 (spec 0.66 sections 12.6.5, 12.6.6).

RM42, RM44; AC33. Runs the preregistered AssetEpisodesV1 in-process through
``relaymind.evaluation.asset_episode_driver`` against the real recommendation
service over the locked corpus, with the pinned multilingual-e5-small ONNX
bundle for the on arm's adaptation stage. The driver's evaluation-only action
registration is not the host's native feedback RPC, so the report is
``provisional`` and its corrections do not count as section 12.6.6 episode
corrections. No external model is called.

The ``prepare`` command writes the off/on EffectiveConfigV1 documents and the
EpisodeFreezeV1; ``run`` executes the episodes resumably and writes the report
and the reset evidence documents.

``run`` executes exactly the frozen adaptation config and the manifest it
names by digest: the off/on documents and the adaptation config must have the
digests of the freeze, and the manifest in ``--model-dir`` must have the
config's ``manifest_digest``. The ONNX backend then rejects a runtime build or
device that differs from the manifest (``embedding_runtime_mismatch``). A
different runtime identity is a new manifest and so a new preregistration
(section 12.6.6); the run never substitutes a run-local manifest.
"""

import argparse
import asyncio
import copy
import hashlib
import json
import platform
import signal
import tomllib
from pathlib import Path

from relaymind import launcher
from relaymind.canonical import canonical_digest, canonical_json
from relaymind.evaluation import asset_evidence
from relaymind.evaluation.asset_corpus import load_asset_release


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def production(path: Path):
    path = path.absolute()
    return launcher.parse_launcher_config(
        tomllib.loads(path.read_text(encoding="utf-8")), config_dir=path.parent
    )


def inventory_digests(repo: Path) -> tuple[str, str]:
    runner = asset_evidence.file_inventory(repo)
    protocol = asset_evidence.file_inventory(repo, protocol=True)
    return canonical_digest(runner), canonical_digest(protocol)


def cmd_prepare(args) -> None:
    release = load_asset_release(
        args.root.absolute() / "lock.json", evidence_root=args.root.absolute(), related_roots={}
    )
    runner, protocol = inventory_digests(args.repo)
    off = asset_evidence.effective_asset_config(
        production(args.production), release, runner_digest=runner, protocol_digest=protocol
    )
    adaptation = read(args.adaptation_config)
    adaptation_digest = canonical_digest(adaptation)
    on = copy.deepcopy(off)
    on["adaptation"] = dict(enabled=True, config_digest=adaptation_digest)
    freeze = dict(
        schema_version=1,
        off_config_digest=canonical_digest(off),
        on_config_digest=canonical_digest(on),
        adaptation_config_digest=adaptation_digest,
        seed=args.seed,
    )
    out = args.output
    write(out / "effective-off.json", off)
    write(out / "effective-on.json", on)
    write(out / "adaptation-config.json", adaptation)
    write(out / "episode-freeze.json", freeze)
    print(json.dumps(dict(freeze_digest=canonical_digest(freeze))))


class NotFrozen(ValueError):
    """The run would execute a configuration the freeze does not bind."""


def frozen_adaptation(prepared: Path, model_dir: Path):
    """The frozen adaptation config and its manifest, both checked by digest."""
    from relaymind.assets.embedding_config import validate_adaptation_config
    from relaymind.assets.embedding_manifest import load_embedding_manifest

    freeze = read(prepared / "episode-freeze.json")
    off, on = read(prepared / "effective-off.json"), read(prepared / "effective-on.json")
    value = read(prepared / "adaptation-config.json")
    digest = canonical_digest(value)
    if digest != freeze["adaptation_config_digest"]:
        raise NotFrozen("adaptation_config_mismatch")
    if (
        canonical_digest(off) != freeze["off_config_digest"]
        or canonical_digest(on) != freeze["on_config_digest"]
        or (on.get("adaptation") or {}).get("config_digest") != digest
    ):
        raise NotFrozen("effective_config_mismatch")
    manifest = load_embedding_manifest(Path(model_dir) / value["manifest"])
    if manifest.manifest_digest != value["manifest_digest"]:
        raise NotFrozen("adaptation_manifest_mismatch")
    return validate_adaptation_config(value, manifest), manifest


def frozen_settings(config, release, off: dict):
    """The recommendation settings of ``config``, which must project to the frozen off
    document (with the generator digests the freeze recorded)."""
    generator = off["generator"]
    actual = asset_evidence.effective_asset_config(
        config,
        release,
        runner_digest=generator["runner_digest"],
        protocol_digest=generator["protocol_digest"],
    )
    if canonical_json(actual) != canonical_json(off):
        raise NotFrozen("production_config_mismatch")
    return config.recommendations


def environment() -> dict:
    from relaymind.evaluation.asset_evidence import collect_benchmark_environment

    return collect_benchmark_environment()


def cmd_run(args) -> None:
    from relaymind.assets.embedding_backends import EmbeddingEngine
    from relaymind.assets.embedding_manifest import verify_embedding_files
    from relaymind.assets.embedding_onnx import OnnxCpuBackend
    from relaymind.evaluation.asset_episode_driver import (
        locked_episode_arms,
        run_adaptation_episodes,
    )
    from relaymind.selection.adaptation_stage import AdaptationStage

    release = load_asset_release(
        args.root.absolute() / "lock.json", evidence_root=args.root.absolute(), related_roots={}
    )
    prepared = args.prepared
    off, on = read(prepared / "effective-off.json"), read(prepared / "effective-on.json")
    freeze, episodes = read(prepared / "episode-freeze.json"), read(args.episodes)
    semantic, manifest = frozen_adaptation(prepared, args.model_dir)
    # The service settings are the frozen ones, not whatever the TOML now says.
    settings = frozen_settings(production(args.production), release, off)
    stop = False

    def interrupt(signum, frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    inputs = args.inputs_reference
    with verify_embedding_files(manifest) as artifact:
        engine = EmbeddingEngine(manifest, OnnxCpuBackend(artifact))

        def stage(feedback):
            return AdaptationStage(feedback, semantic, engine)

        arms = locked_episode_arms(
            release, dict(off=off, on=on), settings, args.work, adaptation_stage=stage
        )
        report, evidence = asyncio.run(
            run_adaptation_episodes(
                episodes,
                freeze,
                release,
                arms,
                environment=read(args.environment),
                qualification="provisional",
                input_manifest=(
                    read(inputs) if inputs else dict(path=None, digest="0" * 64, bytes=0)
                ),
                interrupted=lambda: stop,
                journal=args.journal,
            )
        )
    write(args.output, report)
    reset_dir = args.output.parent / "reset-evidence"
    reset_dir.mkdir(parents=True, exist_ok=True)
    for evidence_id, item in evidence.items():
        name = hashlib.sha256(evidence_id.encode()).hexdigest() + ".json"
        (reset_dir / name).write_bytes(item["payload"])
    write(
        args.output.parent / "reset-evidence.index.json",
        {
            k: dict(
                file=hashlib.sha256(k.encode()).hexdigest() + ".json",
                sha256=v["sha256"],
                bytes=v["bytes"],
            )
            for k, v in sorted(evidence.items())
        },
    )
    summary = report["summary"]
    print(json.dumps(dict(summary=summary, machine=platform.machine())))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--production", type=Path, required=True)
    p.add_argument("--adaptation-config", type=Path, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("run")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--prepared", type=Path, required=True)
    p.add_argument("--episodes", type=Path, required=True)
    p.add_argument("--production", type=Path, required=True)
    p.add_argument("--model-dir", type=Path, required=True)
    p.add_argument("--environment", type=Path, required=True)
    p.add_argument("--inputs-reference", type=Path)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--journal", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    {"prepare": cmd_prepare, "run": cmd_run}[args.command](args)


if __name__ == "__main__":
    main()
