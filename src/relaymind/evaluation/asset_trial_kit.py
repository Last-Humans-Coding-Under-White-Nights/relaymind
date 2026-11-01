"""Early installed-skill trial kit for the customer's Apple M1 8 GiB machine (#234).

Spec 0.65 section 12.6.5 (RM39, RM44; AC30, AC36). Operator tooling around the
existing host-trial driver and grader, never a replacement for them:

- ``preflight`` checks the target machine (macOS arm64, Apple M1, 8 GiB), the
  frozen environment lock, the pinned OpenClaw host version and the packaged
  plugin release (tarball, ``SHA256SUMS`` and ``release.json`` from
  ``npm run pack:release``, and the frozen package bytes);
- ``install_plugin`` installs that tarball with the pinned host CLI through the
  operator guide's lifecycle: install, configure ``baseUrl``, enable, inspect
  (the pinned host installs the plugin disabled until it is configured, #269);
- ``trial_run`` executes the frozen early-trial plan through
  ``run_host_trial`` over operator-captured row observations, collects every
  original into a bundle with its SHA-256, writes the companion input
  manifest and grades the bundle offline with the existing grader.

The kit never executes a task, a tool or an approval: only the host does. A
row without an observation is exported ``not_run``; nothing is inferred from a
gateway trace or a selection receipt. Output is ``fixture`` or ``provisional``,
never ``qualified``, so an early trial can never pass acceptance.
"""

import copy
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_benchmark_io import (
    CONFIG_BYTES,
    atomic_bytes,
    json_bytes,
    read_json_path,
)
from relaymind.evaluation.asset_corpus_io import (
    DOCUMENT_BYTES,
    AssetCorpusError,
    read_local,
    relative_path,
)
from relaymind.evaluation.asset_whole_task import (
    WholeTaskBlocked,
    WholeTaskRejected,
    plan_host_trial,
    run_host_trial,
    validate_whole_task,
    verify_host_tasks,
)

PINNED_OPENCLAW = "2026.10.7"
TARGET_OS, TARGET_ARCHITECTURE, TARGET_CPU = "Darwin", "arm64", "Apple M1"
TARGET_RAM_BYTES = 8 * 1024**3
PREFLIGHT_CHECKS = (
    "macos_arm64",
    "apple_m1",
    "memory_8gib",
    "environment_lock",
    "host_version",
    "release_checksum",
    "frozen_package",
)
QUALIFICATIONS = ("fixture", "provisional")
HOST_TIMEOUT_SECONDS = 120
PLUGIN_ID = "relaymind"
# The gateway's default loopback listener (relaymind.example.toml) and the
# operator guide's ``baseUrl``; the plugin refuses a non-loopback URL itself.
DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
_VERSION = re.compile(r"\b\d{4}\.\d{1,2}\.\d{1,2}\b")
_OBSERVATION_KEYS = {"schema_version", "ordinal", "task_id", "arm", "row", "evidence"}
_PROVENANCE = ("observed", "reported", "fixture")


def observed_environment() -> dict:
    """The section 12.6.2 Environment of this machine, from the shared collector."""
    from relaymind.evaluation.asset_evidence import collect_benchmark_environment

    return collect_benchmark_environment()


def preflight_environment(record: Mapping) -> dict:
    """The Environment a preflight record observed on the target machine."""
    from relaymind.evaluation.asset_evidence import validate_definition

    observed = record.get("observed") if isinstance(record, Mapping) else None
    environment = observed.get("environment") if isinstance(observed, Mapping) else None
    if not isinstance(environment, Mapping):
        raise WholeTaskRejected("preflight_environment")
    try:
        return dict(validate_definition("Environment", dict(environment)))
    except (ValueError, AssetCorpusError):
        raise WholeTaskRejected("preflight_environment") from None


def observation_name(ordinal: int) -> str:
    """The file the operator saves for one planned row, named by plan ordinal."""
    return f"row-{ordinal:04d}.json"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# --- preflight ------------------------------------------------------------------


def _run(
    argv: Sequence[str], timeout: float = HOST_TIMEOUT_SECONDS
) -> tuple[int | None, bytes, bytes]:
    """Exit code, standard output and standard error, kept separate."""
    try:
        done = subprocess.run(
            list(argv), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired as error:
        return None, error.stdout or b"", error.stderr or b""
    except OSError:
        return None, b"", b""
    return done.returncode, done.stdout, done.stderr


def host_version(host_command: Sequence[str]) -> tuple[str | None, str]:
    """``<openclaw> --version``: the pinned version only when it is the only one named."""
    code, stdout, stderr = _run([*host_command, "--version"], timeout=60)
    output = stdout + stderr
    found = set(_VERSION.findall(output.decode("utf-8", "replace")))
    version = PINNED_OPENCLAW if code == 0 and found == {PINNED_OPENCLAW} else None
    return version, _sha256(output)


def release_identity(tarball: Path) -> dict:
    """The tarball against the ``SHA256SUMS`` and ``release.json`` beside it."""
    tarball = Path(tarball)
    identity = dict(
        filename=tarball.name,
        sha256=None,
        bytes=None,
        package=None,
        version=None,
        checksum_ok=False,
    )
    try:
        payload = tarball.read_bytes()
    except OSError:
        # A missing or unreadable release fails its checks; it is not a crash.
        return identity
    digest = _sha256(payload)
    identity.update(sha256=digest, bytes=len(payload))
    try:
        listed = [
            line.split("  ", 1)
            for line in (tarball.parent / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        record = json.loads((tarball.parent / "release.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return identity
    if not isinstance(record, dict):
        return identity
    named = [entry[0] for entry in listed if len(entry) == 2 and entry[1] == tarball.name]
    openclaw = record.get("openclaw") if isinstance(record.get("openclaw"), dict) else {}
    identity.update(package=record.get("package"), version=record.get("version"))
    identity["checksum_ok"] = (
        named == [digest]
        and record.get("filename") == tarball.name
        and record.get("sha256") == digest
        and openclaw.get("peer") == PINNED_OPENCLAW
        and openclaw.get("pluginApi") == PINNED_OPENCLAW
    )
    return identity


def _check(name: str, passed: bool) -> dict:
    return dict(name=name, status="passed" if passed else "failed")


def preflight(
    *,
    environment: Mapping,
    lock: Mapping,
    host_command: Sequence[str],
    tarball: Path,
    freeze: Mapping,
) -> dict:
    """The early-trial preflight record; ``passed`` only when every check passed.

    Unknown observations (no CPU name, no memory size, no single version in the
    host's own output) fail their checks rather than pass them.
    """
    version, version_digest = host_version(host_command)
    identity = release_identity(tarball)
    packages = [
        p
        for p in freeze["packages"]
        if identity["sha256"] is not None
        and (p["sha256"], p["bytes"]) == (identity["sha256"], identity["bytes"])
    ]
    host = freeze["host"]
    checks = [
        _check(
            "macos_arm64",
            environment.get("os") == TARGET_OS
            and environment.get("architecture") == TARGET_ARCHITECTURE,
        ),
        _check("apple_m1", environment.get("cpu") == TARGET_CPU),
        _check("memory_8gib", environment.get("ram_bytes") == TARGET_RAM_BYTES),
        _check(
            "environment_lock",
            all(value is None or environment.get(key) == value for key, value in lock.items()),
        ),
        _check(
            "host_version",
            version == PINNED_OPENCLAW
            and host["version"] == PINNED_OPENCLAW
            and host["sdk_version"] == PINNED_OPENCLAW,
        ),
        _check("release_checksum", identity["checksum_ok"]),
        _check(
            "frozen_package",
            len(packages) == 1
            and identity["checksum_ok"]
            and packages[0]["version"] == identity["version"],
        ),
    ]
    return dict(
        schema_version=1,
        kind="asset-trial-preflight",
        target="apple_m1_8gib_macos",
        pinned_openclaw=PINNED_OPENCLAW,
        checks=checks,
        observed=dict(
            environment=dict(environment),
            host_version=version,
            host_version_output_sha256=version_digest,
            release={k: identity[k] for k in ("filename", "sha256", "bytes", "package", "version")},
            frozen_package_evidence_id=packages[0]["evidence_id"] if len(packages) == 1 else None,
        ),
        passed=all(c["status"] == "passed" for c in checks),
    )


# --- install --------------------------------------------------------------------


def _inspect(output: bytes, version: str | None) -> dict:
    try:
        value = json.loads(output)
    except ValueError:
        value = None
    plugin = value.get("plugin") if isinstance(value, dict) else None
    plugin = plugin if isinstance(plugin, dict) else {}
    check = dict(
        status=plugin.get("status"),
        enabled=plugin.get("enabled"),
        version=plugin.get("version"),
        diagnostics=value.get("diagnostics") if isinstance(value, dict) else None,
    )
    check["passed"] = (
        check["status"] == "loaded"
        and check["enabled"] is True
        and check["diagnostics"] == []
        and check["version"] == version
    )
    return check


def _loopback_host(hostname: str) -> bool:
    # isLoopbackHostname in integrations/openclaw/src/config.ts: no DNS.
    if hostname in ("localhost", "::1"):
        return True
    octets = hostname.split(".")
    return (
        len(octets) == 4
        and octets[0] == "127"
        and all(re.fullmatch(r"\d{1,3}", o) and int(o) <= 255 for o in octets)
    )


def check_base_url(value: object) -> str:
    """The plugin's ``baseUrl`` rules (``integrations/openclaw/src/config.ts``).

    http or https, a loopback host (localhost, 127.0.0.0/8 or [::1]), the /v1
    root, no credentials, query or fragment. Returns the plugin's own form,
    origin plus ``/v1``. A refused value raises ``invalid_base_url`` and is
    never echoed: it can carry a credential (#269 review).
    """
    if (
        not isinstance(value, str)
        or not value
        or any(ord(c) <= 0x20 or ord(c) >= 0x7F or c == "\\" for c in value)
    ):
        raise ValueError("invalid_base_url")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("invalid_base_url") from None
    host = url.hostname or ""
    if (
        url.scheme.lower() not in ("http", "https")
        or "@" in url.netloc
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or "?" in value
        or "#" in value
        or not _loopback_host(host)
        or url.path.rstrip("/") != "/v1"
    ):
        raise ValueError("invalid_base_url")
    shown = f"[{host}]" if ":" in host else host
    origin = f"{url.scheme.lower()}://{shown}"
    default = {"http": 80, "https": 443}[url.scheme.lower()]
    if port is not None and port != default:
        origin += f":{port}"
    return origin + "/v1"


def install_plugin(
    host_command: Sequence[str], tarball: Path, work: Path, *, base_url: str = DEFAULT_BASE_URL
) -> dict:
    """Install, configure, enable and inspect the packaged plugin with the pinned host CLI.

    The operator guide's lifecycle (``integrations/openclaw/README.md`` step 4)
    and the install check's steps: the pinned host installs the plugin
    disabled until it is configured, so ``plugins enable`` follows the
    ``baseUrl`` configuration (#269). Each step is recorded; a step that fails
    stops the rest (``not_run``). Each step's standard output and standard
    error stay in separate private logs under ``work``; the record keeps only
    placeholders, exit codes, SHA-256 digests and sizes, and the configured
    loopback ``base_url``. Inspect reads the JSON on standard output only.
    """
    base_url = check_base_url(base_url)
    tarball = Path(tarball).absolute()
    work = Path(work).absolute()
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    identity = release_identity(tarball)
    plan = (
        ("install", ["plugins", "install", "npm-pack:" + str(tarball), "--force"]),
        ("configure", ["config", "set", f"plugins.entries.{PLUGIN_ID}.config.baseUrl", base_url]),
        ("enable", ["plugins", "enable", PLUGIN_ID]),
        ("inspect", ["plugins", "inspect", PLUGIN_ID, "--runtime", "--json"]),
    )
    steps, failed = [], False
    for name, args in plan:
        if name == "install":
            args = [*args, "--accept-capabilities"]
        shown = ["<openclaw>", *(a.replace(str(tarball), "<artifact>") for a in args)]
        if failed:
            steps.append(dict(name=name, argv=shown, status="not_run", exit_code=None))
            continue
        code, output, errors = _run([*host_command, *args])
        log, error_log = work / (name + ".log"), work / (name + ".stderr.log")
        for path, payload in ((log, output), (error_log, errors)):
            path.write_bytes(payload)
            path.chmod(0o600)
        step = dict(
            name=name,
            argv=shown,
            exit_code=code,
            log=log.name,
            output_sha256=_sha256(output),
            output_bytes=len(output),
            stderr_log=error_log.name,
            stderr_sha256=_sha256(errors),
            stderr_bytes=len(errors),
        )
        passed = code == 0
        if name == "inspect":
            # Only standard output carries the JSON; host warnings stay in stderr.
            step["inspect"] = _inspect(output, identity["version"])
            passed = passed and step["inspect"]["passed"]
        step["status"] = "passed" if passed else "failed"
        failed = not passed
        steps.append(step)
    return dict(
        schema_version=1,
        kind="asset-trial-install",
        pinned_openclaw=PINNED_OPENCLAW,
        archive={k: identity[k] for k in ("filename", "sha256", "bytes", "package", "version")},
        base_url=base_url,
        steps=steps,
        passed=all(s["status"] == "passed" for s in steps),
    )


# --- run, bundle, grade ---------------------------------------------------------


def load_frozen(inputs: Path) -> dict:
    """The operator's frozen companion manifest and its host-trial documents."""
    inputs = Path(inputs).absolute()
    manifest, payload = read_json_path(inputs, CONFIG_BYTES)
    validate_whole_task("S3InputManifestV1", manifest)

    def document(kind: str) -> dict:
        found = [d for d in manifest["documents"] if d["kind"] == kind]
        # A null path needs an explicit resolver, which the kit does not read.
        if len(found) != 1 or found[0]["path"] is None:
            raise WholeTaskBlocked("input_unresolved")
        value, _ = read_json_path(inputs.parent / relative_path(found[0]["path"]), DOCUMENT_BYTES)
        if canonical_digest(value) != found[0]["digest"]:
            raise WholeTaskRejected("document_digest")
        return value

    tasks, freeze = document("host_tasks"), document("host_freeze")
    verify_host_tasks(tasks, freeze)
    return dict(
        manifest=manifest,
        payload=payload,
        root=inputs.parent,
        tasks=tasks,
        freeze=freeze,
        lock=document("environment_lock"),
    )


class _Bundle:
    """The output directory: copied originals, collected evidence, one registry."""

    def __init__(self, root: Path, frozen: Mapping):
        self.root = root
        self.manifest = copy.deepcopy(frozen["manifest"])
        self.registry = {e["id"]: e for e in self.manifest["evidence"]}
        self.refs: dict[str, dict] = {}
        source = frozen["root"]
        # Null paths are resolver-only originals: kept in the manifest, not copied.
        paths = [d["path"] for d in self.manifest["documents"] if d.get("path")]
        paths += [e["path"] for e in self.manifest["evidence"] if e.get("path")]
        for path in dict.fromkeys(paths):
            self.write(path, read_local(source, path, DOCUMENT_BYTES))

    def write(self, path: str, payload: bytes) -> None:
        target = self.root / relative_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise WholeTaskRejected("bundle_path_conflict")
        target.write_bytes(payload)

    def add(self, evidence_id: str, payload: bytes, provenance: str) -> dict:
        digest = _sha256(payload)
        entry = self.registry.get(evidence_id)
        if entry is None:
            path = "trial-evidence/" + digest
            if not (self.root / path).exists():
                self.write(path, payload)
            entry = dict(
                id=evidence_id,
                kind="original",
                access="restricted",
                path=path,
                sha256=digest,
                bytes=len(payload),
            )
            self.registry[evidence_id] = entry
        elif (entry["sha256"], entry["bytes"]) != (digest, len(payload)):
            raise WholeTaskRejected("evidence_conflict")
        ref = dict(
            id=evidence_id,
            path=entry["path"],
            sha256=entry["sha256"],
            bytes=entry["bytes"],
            provenance=provenance,
        )
        if self.refs.setdefault(evidence_id, ref) != ref:
            raise WholeTaskRejected("evidence_conflict")
        return ref

    def write_manifest(self) -> dict:
        self.manifest["evidence"] = sorted(self.registry.values(), key=lambda e: e["id"].encode())
        validate_whole_task("S3InputManifestV1", self.manifest)
        payload = json_bytes(self.manifest)
        atomic_bytes(self.root / "inputs.json", payload)
        return dict(path="inputs.json", digest=canonical_digest(self.manifest), bytes=len(payload))

    def write_sums(self) -> None:
        lines = [
            f"{_sha256(p.read_bytes())}  {p.relative_to(self.root).as_posix()}"
            for p in sorted(self.root.rglob("*"), key=lambda p: p.relative_to(self.root).as_posix())
            if p.is_file() and p.name != "SHA256SUMS"
        ]
        (self.root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _not_run(task_id: str, arm: str, reason: str) -> dict:
    return dict(
        task_id=task_id,
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
    )


def _executor(
    observations: Path,
    ordinals: Mapping[tuple[str, str], int],
    bundle: _Bundle,
    qualification: str,
    ask: Callable[[str], str] | None,
):
    prompting = [ask is not None]

    def execute(task: Mapping, arm: str, config: Mapping) -> tuple[dict, list[dict]]:
        ordinal = ordinals[(task["id"], arm)]
        name = observation_name(ordinal)
        if not (observations / name).exists() and prompting[0]:
            try:
                answer = ask(
                    f"Row {ordinal}: task {task['id']} ({task['lane']}, prompt case "
                    f"{task['prompt_case_id']}), arm {arm}, host configuration "
                    f"{config['host_config_digest']}. Run it on the host, save {name} in "
                    f"{observations}, then press Enter (or type skip): "
                )
            except EOFError:
                # Closed input: stop asking; this and later rows are read as saved.
                prompting[0], answer = False, ""
            if answer.strip().lower() == "skip":
                return _not_run(task["id"], arm, "operator_skipped"), []
        if not (observations / name).exists():
            return _not_run(task["id"], arm, "observation_missing"), []
        document, _ = read_json_path(observations / name, DOCUMENT_BYTES)
        if (
            not isinstance(document, dict)
            or set(document) != _OBSERVATION_KEYS
            or document["schema_version"] != 1
            or (document["ordinal"], document["task_id"], document["arm"])
            != (ordinal, task["id"], arm)
            or not isinstance(document["evidence"], list)
        ):
            raise WholeTaskRejected("observation_row")
        row = validate_whole_task("HostRow", document["row"])
        if (row["task_id"], row["arm"]) != (task["id"], arm):
            raise WholeTaskRejected("observation_row")
        refs = []
        for item in document["evidence"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"id", "file", "provenance"}
                or item["provenance"] not in _PROVENANCE
                or not isinstance(item["id"], str)
                or not isinstance(item["file"], str)
            ):
                raise WholeTaskRejected("observation_evidence")
            # A provisional report holds no fixture evidence.
            if qualification == "provisional" and item["provenance"] == "fixture":
                raise WholeTaskRejected("observation_provenance")
            try:
                relative_path(item["file"])
            except AssetCorpusError:
                raise WholeTaskRejected("observation_path") from None
            try:
                payload = read_local(observations, item["file"], DOCUMENT_BYTES)
            except AssetCorpusError:
                raise WholeTaskRejected("observation_evidence_missing") from None
            refs.append(bundle.add(item["id"], payload, item["provenance"]))
        return row, refs

    return execute


def _blocked(reason: str) -> tuple[int, dict]:
    return 3, dict(status="blocked", gate="blocked", reason=reason)


def trial_run(
    *,
    inputs: Path,
    observations: Path,
    bundle: Path,
    resolver: Path | None,
    qualification: str,
    environment: Mapping,
    preflight_record: Mapping,
    install_record: Mapping | None = None,
    tarball: Path | None = None,
    ask: Callable[[str], str] | None = None,
    interrupted: Callable[[], bool] = lambda: False,
) -> tuple[int, dict]:
    """Run the frozen early-trial plan, bundle every original and grade it offline.

    Returns the grader's exit code and result: 0 pass, 1 fail, 2 rejected,
    3 blocked. A fixture or provisional early trial is at best blocked.
    """
    if qualification not in QUALIFICATIONS:
        raise ValueError("qualification")
    out = Path(bundle).absolute()
    try:
        if out.exists():
            raise WholeTaskRejected("bundle_exists")
        frozen = load_frozen(inputs)
        tasks, freeze = frozen["tasks"], frozen["freeze"]
        if tasks["kind"] != "early_trial":
            raise WholeTaskRejected("early_trial_required")
        if qualification == "provisional":
            if preflight_record.get("passed") is not True:
                return _blocked("preflight_failed")
            if install_record is None or install_record.get("passed") is not True:
                return _blocked("install_failed")
        out.mkdir(parents=True)
        kit = _Bundle(out, frozen)
        plan = plan_host_trial(tasks)
        configs = {c["arm"]: c for c in freeze["configurations"]}
        atomic_bytes(
            out / "plan.json",
            json_bytes(
                dict(
                    planned=[
                        dict(
                            p,
                            observation=observation_name(p["ordinal"]),
                            host_config_digest=configs[p["arm"]]["host_config_digest"],
                        )
                        for p in plan
                    ]
                )
            ),
        )
        provenance = "observed" if qualification == "provisional" else "fixture"
        extra = []
        for evidence_id, name, record in (
            ("trial-preflight", "preflight.json", preflight_record),
            ("trial-install", "install.json", install_record),
        ):
            if record is not None:
                payload = json_bytes(record)
                atomic_bytes(out / name, payload)
                extra.append(kit.add(evidence_id, payload, provenance))
        if tarball is not None:
            payload = Path(tarball).read_bytes()
            package = [
                p
                for p in freeze["packages"]
                if (p["sha256"], p["bytes"]) == (_sha256(payload), len(payload))
            ]
            if len(package) != 1:
                raise WholeTaskRejected("package_binding")
            extra.append(kit.add(package[0]["evidence_id"], payload, provenance))
        ordinals = {(p["task_id"], p["arm"]): p["ordinal"] for p in plan}
        report = run_host_trial(
            tasks,
            freeze,
            _executor(Path(observations).absolute(), ordinals, kit, qualification, ask),
            environment=environment,
            artifacts=[],
            qualification=qualification,
            input_manifest=dict(path="inputs.json", digest="0" * 64, bytes=0),
            interrupted=interrupted,
        )
        evidence = {e["id"]: e for e in report["evidence"]}
        for ref in extra:
            if evidence.setdefault(ref["id"], ref) != ref:
                raise WholeTaskRejected("evidence_conflict")
        report["evidence"] = [evidence[i] for i in sorted(evidence, key=str.encode)]
        report["input_manifest"] = kit.write_manifest()
        validate_whole_task("AssetHostReportV1", report)
        atomic_bytes(out / "report.json", json_bytes(report))
    except WholeTaskBlocked as error:
        return _blocked(error.reason)
    except (WholeTaskRejected, AssetCorpusError, OSError, KeyError, TypeError) as error:
        reason = str(error) if str(error).isidentifier() else "invalid_input"
        return 2, dict(status="rejected", gate=None, reason=reason)
    from relaymind.evaluation.asset_whole_task_cli import main as grade

    argv = ["grade-host", "--report", str(out / "report.json"), "--inputs"]
    argv += [str(out / "inputs.json"), "--output", str(out / "grade.json")]
    if resolver is not None:
        argv += ["--resolver", str(Path(resolver).absolute())]
    code = grade(argv)
    kit.write_sums()
    result = json.loads((out / "grade.json").read_bytes())
    return code, dict(result, bundle=str(out))
