"""Controller-only bootstrap and recovery provenance (RM16/RM17/RM25, spec 14.4.5)."""

import argparse
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path


def measurement_root(controller_root: Path) -> Path:
    """Select runner imports before importing any measured package or runner module."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--measurement-root", type=Path, default=controller_root)
    args, _ = parser.parse_known_args()
    root = args.measurement_root.resolve()
    if not (root / "src/relaymind/__init__.py").is_file():
        raise ValueError("measurement checkout package missing")
    sys.path[:0] = [str(root / "scripts"), str(root / "src")]
    return root


def controller_module(name: str):
    """M7 shares the repaired controller, even when measured scripts precede sys.path."""
    path = Path(__file__).resolve().with_name(name + ".py")
    existing = sys.modules.get(name)
    if existing is not None and Path(existing.__file__).resolve() == path:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def exported_identity(value: dict, *, root: Path, work: Path) -> dict:
    """The harness's JSON serialization order; raw checkpoint identity stays untouched."""
    text = json.dumps(value, indent=2, ensure_ascii=False)
    for path, replacement in ((work, "<work>"), (root, "<repo>")):
        text = text.replace(json.dumps(str(path))[1:-1], replacement)
    return json.loads(text)


def controller_record(controller_root: Path, root: Path) -> dict:
    from relaymind.evaluation.environment import application_revision
    from relaymind.evaluation.prior import byte_digest

    revision = application_revision(controller_root)
    if revision["dirty"] is not False or not revision["head"]:
        raise ValueError("controller requires a reviewed clean committed checkout")
    imports = {}
    for name, relative in (
        ("relaymind", "src/relaymind/__init__.py"),
        ("evaluation_config", "scripts/evaluation_config.py"),
        ("evaluation_session", "scripts/evaluation_session.py"),
        ("run_corpus", "scripts/run_corpus.py"),
        ("latency_benchmark", "scripts/latency_benchmark.py"),
    ):
        path = Path(importlib.import_module(name).__file__).resolve()
        if path != (root / relative).resolve():
            raise ValueError("controller imported a foreign measurement module")
        imports[name] = str(path)
    return {
        **revision,
        "root": str(controller_root.resolve()),
        "files": {
            name: byte_digest(controller_root / "scripts" / name)
            for name in ("matrix_controller.py", "run_m8_matrix.py", "run_m7_matrix.py")
        },
        "measurement_imports": imports,
    }


def record_controller(index: dict, record: dict, index_path: Path, artifacts: list[dict]) -> None:
    """Bind a clean controller independently of the immutable measurement identity."""
    from relaymind.canonical import canonical_digest
    from relaymind.evaluation.checkpoint import utc_now
    from relaymind.evaluation.prior import byte_digest

    digest = canonical_digest(index["identity"])
    history = index.get("controller_history", [])
    if history:
        if any(entry["measurement_identity_sha256"] != digest for entry in history):
            raise ValueError("controller provenance measurement identity differs")
        if history[-1]["controller"] != record:
            raise ValueError("controller provenance differs from the retained controller")
        for entry in history:
            digest = entry.get("previous_index_sha256")
            if digest is not None:
                snapshot = index_path.with_name("controller-before-" + digest + ".json")
                if (
                    entry.get("previous_index_file") != snapshot.name
                    or not snapshot.exists()
                    or byte_digest(snapshot) != digest
                ):
                    raise ValueError("controller recovery index snapshot differs or is missing")
        return
    index["controller_history"] = [
        {
            "controller": record,
            "measurement_identity_sha256": digest,
            "recorded_at_utc": utc_now(),
            "reason": "explicit controller recovery"
            if index_path.exists()
            else "initial controller",
            "previous_index_sha256": byte_digest(index_path) if index_path.exists() else None,
            "previous_index_file": ("controller-before-" + byte_digest(index_path) + ".json")
            if index_path.exists()
            else None,
            "artifacts": artifacts,
        }
    ]


def retained_artifacts(index: dict, out: Path) -> list[dict]:
    from relaymind.evaluation.prior import byte_digest

    artifacts = []
    for cell_id, attempts in index["attempts"].items():
        for attempt in attempts:
            if attempt.get("reused"):
                continue
            directory = out / cell_id / attempt["name"]
            artifacts.append(
                {
                    "cell": cell_id,
                    "attempt": attempt["name"],
                    **{
                        name + "_sha256": byte_digest(directory / (name + ".json"))
                        if (directory / (name + ".json")).exists()
                        else None
                        for name in ("checkpoint", "export")
                    },
                }
            )
    return artifacts


def offline_provider(index: dict) -> dict[str, str]:
    """Read only the already recorded public provider identity; never its environment file."""
    from urllib.parse import urlsplit

    provider = index["identity"]["provider"]
    if set(provider) != {"RELAYMIND_EXTERNAL_BASE_URL", "RELAYMIND_EXTERNAL_MODEL"}:
        raise ValueError("recorded provider identity differs")
    endpoint = urlsplit(provider["RELAYMIND_EXTERNAL_BASE_URL"])
    if (
        endpoint.scheme not in ("http", "https")
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
        or not provider["RELAYMIND_EXTERNAL_MODEL"]
    ):
        raise ValueError("recorded provider identity is invalid")
    return dict(provider)


def publish_index(path: Path, index: dict, writer) -> None:
    """Retain exact original index bytes before the first legitimate recovery update."""
    from relaymind.evaluation.prior import byte_digest

    entry = index["controller_history"][-1]
    digest = entry["previous_index_sha256"]
    if digest is not None:
        snapshot = path.with_name("controller-before-" + digest + ".json")
        if snapshot.exists():
            if byte_digest(snapshot) != digest:
                raise ValueError("controller recovery index snapshot differs")
        else:
            if byte_digest(path) != digest:
                raise ValueError("original controller index changed before recovery publication")
            temporary = snapshot.with_suffix(".tmp")
            with temporary.open("wb") as handle:
                handle.write(path.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, snapshot)
            if os.name != "nt":
                descriptor = os.open(snapshot.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    writer(path, index)
