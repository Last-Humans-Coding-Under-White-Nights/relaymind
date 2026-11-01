"""The environment manifest of an evaluation export (spec 0.28 section 12.2, #33; RM25).

The machine fields are read from the operating system at run time and
left null when the system does not report them; nothing is typed in by
hand. Runtime facts (threads, slots, model load time) are read from the
llama-server log of the run.
"""

import copy
import functools
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_CLOCK = re.compile(r"^(\d+)\.(\d+)\.(\d+)\.(\d+)\s")
_THREADS = re.compile(r"n_threads = (\d+)")
_SLOTS = re.compile(r"n_slots = (\d+), n_ctx_slot = (\d+)")
_LOADING = "load_model: loading model"
_LOADED = "llama_server: model loaded"


def _run(*argv: str) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _clock_ms(line: str) -> float | None:
    """The log clock of a llama-server line (minutes.seconds.milliseconds.microseconds)."""
    match = _CLOCK.match(line)
    if match is None:
        return None
    minutes, seconds, millis, micros = (int(part) for part in match.groups())
    return (minutes * 60 + seconds) * 1000 + millis + micros / 1000


def parse_runtime_log(text: str) -> dict[str, Any]:
    """Thread and slot settings, and the model load time from the runtime's log clock."""
    facts: dict[str, Any] = {
        "n_threads": None,
        "n_slots": None,
        "n_ctx_slot": None,
        "model_load_ms": None,
        "lines": [],
    }
    loading = None
    for line in text.splitlines():
        kept = False
        if facts["n_threads"] is None and (match := _THREADS.search(line)):
            facts["n_threads"] = int(match.group(1))
            kept = True
        if match := _SLOTS.search(line):
            facts["n_slots"], facts["n_ctx_slot"] = int(match.group(1)), int(match.group(2))
            kept = True
        if _LOADING in line:
            loading = _clock_ms(line)
            kept = True
        if _LOADED in line:
            loaded = _clock_ms(line)
            if loading is not None and loaded is not None:
                facts["model_load_ms"] = round(loaded - loading, 3)
            kept = True
        if kept:
            facts["lines"].append(line)
    return facts


def _darwin() -> dict[str, Any]:
    gpu = None
    profile = _run("system_profiler", "SPDisplaysDataType", "-json")
    if profile:
        try:
            gpus = json.loads(profile).get("SPDisplaysDataType", [])
        except ValueError:
            gpus = []
        # Only the model and core count: the display entries carry serial numbers.
        gpu = [{"model": g.get("sppci_model"), "cores": g.get("sppci_cores")} for g in gpus]
    memory = _run("sysctl", "-n", "hw.memsize")
    return {
        "os_version": f"macOS {platform.mac_ver()[0]}",
        "cpu": _run("sysctl", "-n", "machdep.cpu.brand_string"),
        "ram_bytes": int(memory) if memory and memory.isdigit() else None,
        "gpu": gpu,
    }


def _windows() -> dict[str, Any]:
    def cim(query: str) -> str | None:
        return _run("powershell", "-NoProfile", "-Command", query)

    memory = cim("(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory")
    gpu = cim("(Get-CimInstance Win32_VideoController).Name")
    return {
        "os_version": f"Windows {platform.version()}",
        "cpu": cim("(Get-CimInstance Win32_Processor).Name") or platform.processor() or None,
        "ram_bytes": int(memory) if memory and memory.isdigit() else None,
        "gpu": None if gpu is None else [{"model": name} for name in gpu.splitlines() if name],
    }


def _other() -> dict[str, Any]:
    cpu = ram = None
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            cpu = next(
                (line.split(":", 1)[1].strip() for line in handle if line.startswith("model name")),
                None,
            )
        with open("/proc/meminfo", encoding="utf-8") as handle:
            total = next((line.split()[1] for line in handle if line.startswith("MemTotal")), None)
            ram = int(total) * 1024 if total else None
    except OSError:
        pass
    return {"os_version": platform.platform(), "cpu": cpu, "ram_bytes": ram, "gpu": None}


def machine_manifest() -> dict[str, Any]:
    """OS, CPU, GPU, RAM, and Python of this machine (section 12.2).

    The facts cannot change while a process runs, so the system tools
    (system_profiler, PowerShell) are asked once; every caller gets a copy.
    """
    return copy.deepcopy(_machine_facts())


@functools.cache
def _machine_facts() -> dict[str, Any]:
    if sys.platform == "darwin":
        details = _darwin()
    elif os.name == "nt":
        details = _windows()
    else:
        details = _other()
    return {
        "os": platform.system(),
        "os_version": details["os_version"],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu": details["cpu"],
        "cpu_count": os.cpu_count(),
        "ram_bytes": details["ram_bytes"],
        "gpu": details["gpu"],
        "python": platform.python_version(),
    }


def runtime_devices() -> str | None:
    """The backend devices llama-server reports (``llama-server --list-devices``)."""
    return _run("llama-server", "--list-devices")


def application_revision(repo_root: Path) -> dict[str, Any]:
    """The checkout's HEAD and whether its tree has changes; None where git cannot say.

    Read it before an analysis writes its files into the checkout, so the
    files it writes do not count as changes.
    """
    root = str(repo_root)
    head = _run("git", "-C", root, "rev-parse", "HEAD")
    status = _run("git", "-C", root, "status", "--porcelain")
    return {"head": head, "dirty": None if status is None else bool(status)}


def checkout_environment(repo_root: Path, environ: dict[str, str]) -> dict[str, str]:
    """Make every child import the measured checkout, including with a shared venv.

    The checkout comes first; entries already on PYTHONPATH are kept after it.
    """
    paths = [str(repo_root / "src")]
    if environ.get("PYTHONPATH"):
        paths.append(environ["PYTHONPATH"])
    return {**environ, "PYTHONPATH": os.pathsep.join(paths)}


def source_fingerprint(repo_root: Path) -> dict[str, Any]:
    """Record source bytes independently of the editable installation and Git labels."""
    import hashlib

    import relaymind
    from relaymind.canonical import canonical_digest

    files = {}
    for directory in ("src", "scripts", "skills", "evaluation", "tests/fixtures/sandbox"):
        for path in sorted((repo_root / directory).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files[str(path.relative_to(repo_root))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    return {
        **application_revision(repo_root),
        "source_sha256": canonical_digest(files),
        "files": files,
        "package_path": str(Path(relaymind.__file__).resolve()),
    }
