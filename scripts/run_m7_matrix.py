"""Fixed M7 matrix: offline plans; explicit execution and separately reviewed ablation."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from evaluation_config import configuration_from_args  # noqa: E402
from run_m8_matrix import (  # noqa: E402
    PINNED_RUNTIME,
    attempt_cost,
    declare_restart,
    provider_environment,
    reconcile_attempt,
)

from relaymind.canonical import canonical_digest  # noqa: E402
from relaymind.evaluation.checkpoint import atomic_json, utc_now  # noqa: E402
from relaymind.evaluation.environment import checkout_environment, source_fingerprint  # noqa: E402
from relaymind.evaluation.prior import byte_digest  # noqa: E402

HISTORICAL = {
    "corpus": "docs/evidence/2026-10-22-corpus-apple-m1.json",
    "corpus_sha256": "931c7e260f81d586547bc0834d3befb1f9d558c011e39343f4da5e6f8b778284",
    "latency": [
        "docs/evidence/2026-10-23-latency-" + name + "-apple-m1.json"
        for name in ("before", "after-warmup", "after-no-warmup")
    ],
    "comparison_scope": "Historical comparisons are confounded; not an isolated criteria effect.",
}


def matrix_cells(
    revision: str, criteria_revision: str | None = None, *, omit_criteria: bool = False
) -> list[dict]:
    for value in (revision, criteria_revision):
        if value is not None and re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("revision must be a full committed hash")
    if criteria_revision == revision:
        raise ValueError("criteria ablation requires a distinct experimental revision")
    cells = []
    for number in range(7):
        if number == 6 and omit_criteria:
            continue
        modes = (
            ["auto", "external_only", "local_only"]
            if number in (0, 4)
            else ["auto", "external_only"]
            if number == 5
            else ["auto", "local_only"]
        )
        cells.append(
            dict(
                id=f"M7-C{number}",
                runner="corpus",
                revision=criteria_revision if number == 6 else revision,
                modes=modes,
                requests=52 * len(modes),
                no_local_model=number == 5,
                performance=dict(
                    answer_pass="draft" if number in (1, 4) else "final",
                    calls_per_generation="many" if number in (2, 4) else "one",
                    prefetch=number in (3, 4),
                    answer_stream="buffered",
                    early_headers=False,
                ),
                selection_chain=["example_match"] if number == 5 else ["model_router"],
                timing_prior=None,
            )
        )
    for number in range(2):
        cells.append(
            {
                **cells[0],
                "id": f"M7-L{number}",
                "runner": "latency",
                "modes": [],
                "requests": 124,
                "performance": {**cells[0]["performance"], "early_headers": bool(number)},
            }
        )
    return cells


def make_plan(
    revision: str, criteria_revision: str | None = None, *, omit_criteria: bool = False
) -> dict:
    cells = matrix_cells(revision, criteria_revision, omit_criteria=omit_criteria)
    return dict(
        schema_version=1,
        kind="relaymind.m7_matrix",
        cells=cells,
        run_order=[c["id"] for c in cells],
        corpus_runs=728 if omit_criteria else 832,
        latency_requests=248,
        external_inference_ceiling=2808 if omit_criteria else 3120,
        token_ceiling=None,
        ceiling_scope="One uninterrupted attempt per cell; restarts add retained cost and bounds.",
        conditional_reuse=dict(
            cells=["M7-C0", "M7-C5"],
            incremental_corpus_runs=468 if omit_criteria else 572,
            external_inference_ceiling=1560 if omit_criteria else 1872,
        ),
        criteria_hypothesis="not measured"
        if omit_criteria
        else "controlled revision comparison"
        if criteria_revision
        else "requires experimental revision",
        historical_sources=HISTORICAL,
        defaults_changed=False,
    )


def command(cell: dict, directory: Path, env: dict, *, root: Path = ROOT) -> list[str]:
    runner = "run_corpus.py" if cell["runner"] == "corpus" else "latency_benchmark.py"
    argv = [
        sys.executable,
        str(root / "scripts" / runner),
        "--out",
        str(directory / "export.json"),
        "--checkpoint",
        str(directory / "checkpoint.json"),
        "--placement",
        "policy=local_first",
        "--selection-chain",
        *cell["selection_chain"],
        "--performance",
        *[
            key + "=" + (str(value).lower() if isinstance(value, bool) else value)
            for key, value in cell["performance"].items()
        ],
    ]
    if cell["no_local_model"]:
        argv += ["--no-local-model"]
    else:
        argv += ["--manifest", str(root / "models/qwen3-0.6b-q8_0.manifest.json")]
    if cell["runner"] == "corpus":
        argv += [
            "--external-base-url",
            env["RELAYMIND_EXTERNAL_BASE_URL"],
            "--external-model",
            env["RELAYMIND_EXTERNAL_MODEL"],
            "--external-context-limit",
            "128000",
            "--modes",
            *cell["modes"],
        ]
    else:
        argv += ["--runs", "10", "--timeout", "60", "--seed", "20261023"]
    return argv


def validate_reuse_identity(cell_id: str, expected: dict, actual: dict) -> None:
    if cell_id not in ("M7-C0", "M7-C5"):
        raise ValueError("only C0 and C5 corpus evidence may be reused")
    if expected.get("timing_prior") is not None or actual.get("timing_prior") is not None:
        raise ValueError("M7 reuse identity requires an explicitly null timing prior")
    if expected != actual:
        raise ValueError("reuse identity differs")


def fixture_digest(root: Path) -> str:
    return canonical_digest(
        {
            str(p.relative_to(root)): byte_digest(p)
            for p in sorted((root / "tests/fixtures/sandbox").rglob("*"))
            if p.is_file()
        }
    )


def measured_source(root: Path) -> dict:
    if root.resolve() == ROOT.resolve():
        return source_fingerprint(root)
    # The alternate checkout, not this interpreter's editable installation, identifies itself.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from pathlib import Path; "
            "from relaymind.evaluation.environment import source_fingerprint; "
            "print(json.dumps(source_fingerprint(Path.cwd())))",
        ],
        cwd=root,
        env=checkout_environment(root, dict(os.environ)),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def reuse_identity(
    cell: dict, root: Path, source: dict, configuration: dict, runtime_sha256: str, provider: dict
) -> dict:
    manifest = (
        json.loads((root / "models/qwen3-0.6b-q8_0.manifest.json").read_text(encoding="utf-8"))
        if not cell["no_local_model"]
        else None
    )
    import run_corpus

    corpus = run_corpus.load_frozen_corpus()
    return dict(
        source=source,
        configuration=configuration,
        timing_prior=None,
        runtime_sha256=runtime_sha256,
        manifest_sha256=byte_digest(root / "models/qwen3-0.6b-q8_0.manifest.json")
        if manifest
        else None,
        model_sha256=manifest.get("file_sha256") if manifest else None,
        provider=provider,
        corpus_sha256=corpus.digest,
        sidecar_sha256=byte_digest(root / "evaluation/corpus.v1.skills.json"),
        fixtures_sha256=fixture_digest(root),
        modes=cell["modes"],
    )


def reuse_attempt(cell: dict, foreign_index: Path, expected: dict) -> dict:
    """Reuse a finalized M8 physical attempt, preserving original paths and costs."""
    foreign = json.loads(foreign_index.read_text(encoding="utf-8"))
    name = (
        "calibration-normal-local_first-router-buffered-corpus"
        if cell["id"] == "M7-C0"
        else "streaming-none-local_first-example-buffered-corpus"
    )
    attempts = foreign["attempts"].get(name, [])
    if not attempts:
        raise ValueError("reuse has no completed source attempt")
    original = attempts[-1]
    directory = foreign_index.parent / name / original["name"]
    checkpoint = json.loads((directory / "checkpoint.json").read_text(encoding="utf-8"))
    doc = json.loads((directory / "export.json").read_text(encoding="utf-8"))
    actual = {
        **expected,
        "source": checkpoint["identity"]["application"],
        "configuration": doc["configuration"],
        "timing_prior": doc.get("timing_prior"),
        "runtime_sha256": foreign["identity"]["runtime_sha256"],
        "provider": foreign["identity"]["provider"],
        "corpus_sha256": doc["corpus"]["sha256"],
        "sidecar_sha256": doc["selection_sidecar_sha256"],
        "modes": doc["selection"]["modes"],
        "model_sha256": doc.get("model_sha256"),
        "manifest_sha256": (doc["prior_configuration"].get("local") or {}).get("manifest_sha256"),
    }
    original_fixtures = {
        k: v
        for k, v in actual["source"].get("files", {}).items()
        if k.startswith("tests/fixtures/sandbox/")
    }
    if not original_fixtures:
        raise ValueError("reuse fixture identity unavailable")
    actual["fixtures_sha256"] = canonical_digest(original_fixtures)
    import run_corpus

    original_args = run_corpus.argument_parser().parse_args(original["argv"][2:])
    expected_args = run_corpus.argument_parser().parse_args(
        command(cell, directory, expected["provider"])[2:]
    )
    for key in (
        "runtime_extra_args",
        "cases",
        "languages",
        "timing_samples",
        "external_context_limit",
        "external_region",
    ):
        if getattr(original_args, key) != getattr(expected_args, key):
            raise ValueError("reuse runner argument identity differs")
    validate_reuse_identity(cell["id"], expected, actual)
    for old in attempts:
        old_dir = foreign_index.parent / name / old["name"]
        reconcile_attempt(old, cell, old_dir, old["argv"], expected["source"])
    if original["status"] != "complete":
        raise ValueError("reuse requires a finalized source checkpoint")
    return dict(
        name="reused",
        status="complete",
        reused=True,
        source_index=str(foreign_index),
        source_cell=name,
        physical_attempts=[
            dict(identity=str((foreign_index.parent / name / a["name"]).resolve()), cost=a["cost"])
            for a in attempts
        ],
        source_attempt=original,
        export_path=str(directory / "export.json"),
        export_sha256=byte_digest(directory / "export.json"),
        reuse_identity=actual,
        requests=cell["requests"],
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--criteria-revision")
    parser.add_argument("--criteria-checkout", type=Path)
    parser.add_argument("--omit-criteria", action="store_true")
    parser.add_argument("--runtime", type=Path, default=Path(PINNED_RUNTIME))
    parser.add_argument("--provider-env", type=Path)
    parser.add_argument("--reuse-m8-index", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--restart", metavar="CELL=ATTEMPT")
    args = parser.parse_args(argv)
    plan = make_plan(args.revision, args.criteria_revision, omit_criteria=args.omit_criteria)
    out = args.out.resolve()
    if out == ROOT or ROOT in out.parents:
        raise ValueError("outputs must remain outside checkout")
    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / "plan.json"
    if plan_path.exists() and json.loads(plan_path.read_text(encoding="utf-8")) != plan:
        raise ValueError("matrix plan differs")
    atomic_json(plan_path, plan)
    if not args.execute:
        print(
            f"Offline M7 plan: {len(plan['cells'])} cells, {plan['corpus_runs']} corpus runs, "
            "248 latency requests; no inference."
        )
        return
    if not args.omit_criteria and (not args.criteria_revision or not args.criteria_checkout):
        raise ValueError(
            "execution requires explicit reviewed criteria revision and checkout, "
            "or --omit-criteria"
        )
    roots = {
        c["id"]: (args.criteria_checkout.resolve() if c["id"] == "M7-C6" else ROOT)
        for c in plan["cells"]
    }
    sources = {str(root): measured_source(root) for root in set(roots.values())}
    for c in plan["cells"]:
        source = sources[str(roots[c["id"]])]
        if source.get("dirty") is not False or source.get("head") != c["revision"]:
            raise ValueError("execution requires each reviewed clean committed revision")
    if args.runtime.name not in ("llama-server", "llama-server.exe") or not args.runtime.is_file():
        raise ValueError("pinned runtime executable missing")
    env = provider_environment(args.provider_env)
    env["PATH"] = str(args.runtime.resolve().parent) + os.pathsep + env.get("PATH", "")
    provider = {k: env[k] for k in ("RELAYMIND_EXTERNAL_BASE_URL", "RELAYMIND_EXTERNAL_MODEL")}
    identity = dict(
        sources=sources,
        runtime_sha256=byte_digest(args.runtime),
        runtime=str(args.runtime.resolve()),
        provider=provider,
        context_limit=128000,
        plan_sha256=canonical_digest(plan),
        fixtures_sha256=fixture_digest(ROOT),
        reuse_m8_index=str(args.reuse_m8_index.resolve()) if args.reuse_m8_index else None,
    )
    index_path = out / "index.json"
    index = (
        json.loads(index_path.read_text(encoding="utf-8"))
        if index_path.exists()
        else dict(identity=identity, attempts={})
    )
    if index["identity"] != identity:
        raise ValueError("matrix source/runtime/provider/settings identity differs")
    restart = args.restart.split("=", 1) if args.restart else None
    if restart and (
        len(restart) != 2
        or restart[0] not in plan["run_order"]
        or not re.fullmatch(r"[A-Za-z0-9_-]+", restart[1])
    ):
        raise ValueError("restart must name CELL=NEW_ATTEMPT using a safe directory name")
    for cell in plan["cells"]:
        root = roots[cell["id"]]
        source = sources[str(root)]
        attempts = index["attempts"].setdefault(cell["id"], [])
        if args.reuse_m8_index and cell["id"] in ("M7-C0", "M7-C5"):
            import run_corpus

            config = configuration_from_args(
                run_corpus.argument_parser().parse_args(
                    command(cell, out / cell["id"] / "reuse", env, root=root)[2:]
                )
            ).to_json_object()
            reused = reuse_attempt(
                cell,
                args.reuse_m8_index.resolve(),
                reuse_identity(cell, root, source, config, identity["runtime_sha256"], provider),
            )
            if attempts and attempts != [reused]:
                raise ValueError("reuse identity differs from retained attempt")
            index["attempts"][cell["id"]] = [reused]
            atomic_json(index_path, index)
            continue
        for old in attempts:
            directory = out / cell["id"] / old["name"]
            try:
                reconcile_attempt(
                    old, cell, directory, command(cell, directory, env, root=root), source
                )
            finally:
                atomic_json(index_path, index)
        if any(a["status"] == "complete" for a in attempts[:-1]):
            raise ValueError("prior attempt already finalized cell")
        if attempts and attempts[-1]["status"] == "complete":
            continue
        if attempts and not (restart and restart[0] == cell["id"]):
            raise ValueError("interrupted cell requires explicit --restart CELL=NEW_ATTEMPT")
        name = restart[1] if restart and restart[0] == cell["id"] else "initial"
        attempt = declare_restart(index, cell, name)
        directory = out / cell["id"] / name
        directory.mkdir(parents=True, exist_ok=False)
        child_argv = command(cell, directory, env, root=root)
        attempt.update(
            argv=child_argv, prior_sha256=None, status="started", started_at_utc=utc_now()
        )
        atomic_json(index_path, index)
        try:
            with (directory / "runner.log").open("wb") as log:
                result = subprocess.run(
                    child_argv,
                    cwd=root,
                    env=checkout_environment(root, env),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
            if result.returncode:
                raise RuntimeError("runner failed; retained checkpoint and logs")
            reconcile_attempt(attempt, cell, directory, child_argv, source)
            if attempt["status"] != "complete":
                raise ValueError("runner returned without finalized checkpoint")
        except BaseException:
            attempt["cost"] = attempt_cost(directory)
            if attempt["status"] != "complete":
                attempt.update(status="interrupted", finished_at_utc=utc_now())
            raise
        finally:
            atomic_json(index_path, index)


if __name__ == "__main__":
    main()
