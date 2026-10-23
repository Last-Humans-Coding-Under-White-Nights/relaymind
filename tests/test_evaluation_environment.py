"""The environment manifest of a benchmark report (spec 0.28 section 12.2, #33; RM25).

Section 12.2: record OS version, CPU, GPU, RAM, backend, thread
settings, model hashes, quantization, context settings, sampling
parameters, runtime and application revisions, external endpoint region
if known, and test time. Cold startup is reported with model load time.
"""

import re
import subprocess
import sys

import pytest

from relaymind.evaluation import environment
from relaymind.evaluation.environment import (
    application_revision,
    machine_manifest,
    parse_runtime_log,
)

LOG = """\
0.00.003.203 I srv  llama_server: initializing ...
0.00.140.147 I srv    load_model: loading model 'models/Qwen3-0.6B-Q8_0.gguf'
0.01.210.533 I cmn          init: llama threadpool init, n_threads = 4
0.01.338.771 I srv    load_model: initializing, n_slots = 4, n_ctx_slot = 8192, kv_unified = 'true'
0.01.405.156 I srv  llama_server: model loaded
0.01.405.169 I srv  llama_server: listening on http://127.0.0.1:8199
"""


def test_the_runtime_log_gives_threads_slots_and_model_load_time():
    facts = parse_runtime_log(LOG)
    assert facts["n_threads"] == 4
    assert facts["n_slots"] == 4
    assert facts["n_ctx_slot"] == 8192
    assert facts["model_load_ms"] == pytest.approx(1265.009)
    assert "threadpool init" in facts["lines"][1]


def test_minutes_in_the_log_clock_count():
    log = (
        "1.59.000.000 I srv    load_model: loading model 'x'\n"
        "2.01.500.000 I srv  llama_server: model loaded\n"
    )
    assert parse_runtime_log(log)["model_load_ms"] == pytest.approx(2500.0)


def test_a_log_without_the_lines_records_none():
    facts = parse_runtime_log("something else\n")
    assert facts == {
        "n_threads": None,
        "n_slots": None,
        "n_ctx_slot": None,
        "model_load_ms": None,
        "lines": [],
    }


def test_the_machine_manifest_names_the_section_12_2_fields():
    manifest = machine_manifest()
    for key in ("os", "os_version", "machine", "cpu", "cpu_count", "ram_bytes", "gpu", "python"):
        assert key in manifest
    assert manifest["cpu_count"] >= 1


def test_command_output_that_does_not_decode_is_kept_with_replacements():
    """A system tool may print bytes the locale cannot decode; the manifest still builds."""
    script = "import sys; sys.stdout.buffer.write(b'\\xff\\xfe GPU')"
    assert environment._run(sys.executable, "-c", script).endswith(" GPU")


def git(path, *args):
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=path,
        check=True,
        capture_output=True,
    )


def test_the_application_revision_is_the_head_and_whether_the_tree_is_dirty(tmp_path):
    """An offline analysis records the revision it ran at (spec 0.31 section 12.2)."""
    git(tmp_path, "init", "-q")
    (tmp_path / "a.txt").write_text("a", "utf-8")
    git(tmp_path, "add", "a.txt")
    git(tmp_path, "commit", "-q", "-m", "first")
    clean = application_revision(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{40}", clean["head"]) and clean["dirty"] is False
    (tmp_path / "b.txt").write_text("b", "utf-8")
    dirty = application_revision(tmp_path)
    assert dirty["head"] == clean["head"] and dirty["dirty"] is True


def test_the_application_revision_of_a_directory_that_is_not_a_checkout_is_unknown(tmp_path):
    assert application_revision(tmp_path) == {"head": None, "dirty": None}
