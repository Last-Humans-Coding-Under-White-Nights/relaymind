"""Builders for launcher tests: a config file over a tiny artifact and the checked-in skills."""

import dataclasses
import json
from pathlib import Path

from gguf_builder import sample_kvs, write_gguf
from helpers import BASELINE, DEMO_SKILLS_ROOT
from relaymind.adapters.manifest import build_manifest


def toml_path(path) -> str:
    """A TOML basic string for a path; forward slashes keep Windows paths valid."""
    return json.dumps(Path(path).as_posix())


def make_artifact(directory: Path) -> tuple[Path, Path]:
    """A tiny GGUF file and the manifest built from it; returns (manifest, artifact)."""
    artifact = write_gguf(directory / "tiny.gguf", sample_kvs())
    manifest = build_manifest(
        artifact,
        model_id="tiny-double",
        source_url="https://example.invalid/tiny",
        source_revision="r1",
        quantization="Q8_0",
        runtime="llama.cpp",
        runtime_version="test",
        launch_settings={"ctx_size": 4096, "jinja": True, "reasoning": "off"},
        license="Apache-2.0",
    )
    path = directory / "tiny.manifest.json"
    path.write_text(json.dumps(dataclasses.asdict(manifest)))
    return path, artifact


def write_config(tmp_path: Path, extra: str = "", *, base: str | None = None) -> Path:
    """A valid config file; extra is appended TOML (a table that must come last)."""
    manifest, artifact = make_artifact(tmp_path)
    if base is None:
        base = (
            f"[store]\npath = {toml_path(tmp_path / 'runs.sqlite3')}\n"
            f"[skills]\nroots = [{toml_path(DEMO_SKILLS_ROOT)}]\n"
            f"[sandbox]\nseed = {toml_path(BASELINE)}\n"
            f"work_dir = {toml_path(tmp_path / 'sandbox')}\n"
            f"[local_model]\nmanifest = {toml_path(manifest)}\n"
            f"artifact = {toml_path(artifact)}\n"
        )
    path = tmp_path / "relaymind.toml"
    path.write_text(base + extra)
    return path
