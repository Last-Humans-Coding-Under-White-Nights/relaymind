"""The OpenClaw plugin's release artifact (#145; RM12, RM18, RM19, RM33; AC24; sections 7.5, 14).

The artifact an operator installs is the tarball ``npm run pack:release``
writes, with its SHA-256 and a release record. These tests check that it is
built from locked dependencies, that two builds of one checkout are the same
bytes, that it holds only the runtime files, and that the plugin loads and
round-trips from the extracted artifact through the pinned transport against
the real gateway over model doubles, without the checkout's sources or its
``dist/``. The real host's install of the artifact is
``scripts/openclaw_install_check.py`` (CI and operators), not this suite.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.parse import unquote

import pytest

from api_helpers import RunningServer, token_of
from gateway_helpers import text_turn, tool_turn
from test_openclaw_host_transport import PACKAGE, REQUIRED, _missing, _node
from test_openclaw_plugin import client_gateway, drive_plugin
from test_openclaw_round_trips import ECHO

PINNED = "2026.10.7"
RUNTIME_FILES = {"package.json", "README.md", "openclaw.plugin.json"}


def _npm_cli(node: Path) -> Path | None:
    """The npm-cli.js beside this node, or None where npm is a native shim (Volta)."""
    candidates = [
        # POSIX: bin/npm links to lib/node_modules/npm/bin/npm-cli.js.
        Path(shutil.which("npm") or node.with_name("npm")).resolve(),
        node.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
        # Windows: npm.cmd sits next to node.exe.
        node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js",
    ]
    return next((c for c in candidates if c.suffix == ".js" and c.exists()), None)


def _npm() -> list[str]:
    """npm through node, so a Windows npm.cmd shim never needs a shell."""
    node = Path(_node())
    cli = _npm_cli(node)
    if cli is None:
        reason = f"no npm-cli.js found for {node}: npm is not a link to it (a native shim?)"
        if REQUIRED:
            pytest.fail(reason)
        pytest.skip(reason)
    return [str(node), str(cli)]


@pytest.fixture
def node_dependencies():
    reason = _missing()
    if reason is not None:
        if REQUIRED:
            pytest.fail(f"RELAYMIND_OPENCLAW_NODE=1 but {reason}")
        pytest.skip(reason)


def pack_release(dest: Path) -> dict:
    env = {**os.environ, "PATH": f"{Path(_node()).parent}{os.pathsep}{os.environ['PATH']}"}
    completed = subprocess.run(
        [*_npm(), "run", "--silent", "pack:release", "--", str(dest)],
        cwd=PACKAGE,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads((dest / "release.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def releases(tmp_path_factory):
    """Two release builds of the same checkout, each from a clean dist/."""
    if _missing() is not None:
        if REQUIRED:
            pytest.fail(f"RELAYMIND_OPENCLAW_NODE=1 but {_missing()}")
        pytest.skip(_missing())
    first = tmp_path_factory.mktemp("release-a")
    second = tmp_path_factory.mktemp("release-b")
    return (first, pack_release(first)), (second, pack_release(second))


# -- locked dependencies (no Node needed) ------------------------------------------------


def test_the_lockfile_pins_the_host_and_the_transport_exactly():
    """Section 7.5: the package is built and tested against the pinned release only."""
    package = json.loads((PACKAGE / "package.json").read_text("utf-8"))
    lock = json.loads((PACKAGE / "package-lock.json").read_text("utf-8"))
    assert lock["lockfileVersion"] == 3
    root = lock["packages"][""]
    assert root["name"] == package["name"]
    assert root["version"] == package["version"]
    assert root["devDependencies"] == package["devDependencies"]
    assert root["peerDependencies"] == package["peerDependencies"]
    assert root["engines"] == package["engines"]
    for name in ("openclaw", "@openclaw/ai"):
        entry = lock["packages"][f"node_modules/{name}"]
        assert entry["version"] == PINNED
        assert entry["integrity"].startswith("sha512-")
    # Every dependency of the build is exact, so npm ci gives the same tree.
    for spec in package["devDependencies"].values():
        assert spec[0].isdigit(), spec
    assert package.get("dependencies", {}) == {}


def test_the_release_script_is_declared():
    package = json.loads((PACKAGE / "package.json").read_text("utf-8"))
    assert "release-pack.mjs" in package["scripts"]["pack:release"]


# -- the release artifact (needs npm ci) --------------------------------------------------


def test_two_release_builds_are_the_same_bytes(releases):
    """Issue #145: the package builds reproducibly from one checkout."""
    (dir_a, a), (dir_b, b) = releases
    assert a["sha256"] == b["sha256"]
    assert a["files"] == b["files"]
    tarball = dir_a / a["filename"]
    assert hashlib.sha256(tarball.read_bytes()).hexdigest() == a["sha256"]
    assert (dir_a / "SHA256SUMS").read_text("utf-8") == f"{a['sha256']}  {a['filename']}\n"


def test_the_release_record_names_the_pins_and_the_build(releases):
    (_dir, record), _ = releases
    package = json.loads((PACKAGE / "package.json").read_text("utf-8"))
    assert record["package"] == package["name"]
    assert record["version"] == package["version"]
    assert record["filename"] == "relaymind-openclaw-provider-0.1.0.tgz"
    assert record["openclaw"] == {"peer": PINNED, "pluginApi": PINNED, "built_with": PINNED}
    assert record["engines"] == package["engines"]
    assert record["node"].startswith("v")
    assert record["integrity"].startswith("sha512-")


def test_the_artifact_holds_only_the_runtime_files(releases):
    """The built entry, every built module, the manifest, and the README; no sources."""
    (dir_a, record), _ = releases
    with tarfile.open(dir_a / record["filename"]) as archive:
        names = sorted(m.name.removeprefix("package/") for m in archive.getmembers())
    built = {
        f"dist/{p.stem}.js" for p in (PACKAGE / "src").glob("*.ts") if not p.name.endswith(".d.ts")
    }
    assert set(names) == RUNTIME_FILES | built
    assert names == record["files"]
    assert "dist/index.js" in names


def _install_from_artifact(tarball: Path, prefix: Path) -> Path:
    """Unpack the artifact as an installed package; the host provides openclaw as the peer."""
    modules = prefix / "node_modules"
    target = modules / "@relaymind" / "openclaw-provider"
    target.parent.mkdir(parents=True)
    with tarfile.open(tarball) as archive:
        archive.extractall(prefix / "unpacked", filter="data")
    (prefix / "unpacked" / "package").rename(target)
    peer = modules / "openclaw"
    host = PACKAGE / "node_modules" / "openclaw"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(host), str(peer))
    else:
        peer.symlink_to(host, target_is_directory=True)
    return target / "dist" / "index.js"


def test_the_plugin_round_trips_from_the_extracted_artifact(releases, tmp_path):
    """AC24, RM33: the packaged entry, not the checkout, registers and serves the alias."""
    (dir_a, record), _ = releases
    entry = _install_from_artifact(dir_a / record["filename"], tmp_path / "prefix")
    gateway, app = client_gateway(
        tmp_path,
        [
            text_turn("Hello from the artifact."),
            tool_turn(ECHO, call_id="call_echo_1", text="lantern"),
            text_turn("The tool said: echo: lantern."),
        ],
        external_turns=[],
    )
    with RunningServer(app) as running:
        text = drive_plugin(
            running.base_url,
            token_of(app),
            tmp_path,
            entry=str(entry),
            resolve="dynamic",
            prompt="Say hello.",
        )
        tool = drive_plugin(
            running.base_url,
            token_of(app),
            tmp_path,
            entry=str(entry),
            resolve="dynamic",
            prompt="Echo the word lantern.",
            hostTools=[ECHO],
        )
    assert unquote(text["entry"]) == unquote(entry.resolve().as_uri())
    assert text["register_error"] is None
    (result,) = text["results"]
    assert result["text"] == "Hello from the artifact."
    assert [r["stop_reason"] for r in tool["results"]] == ["toolUse", "stop"]
    assert tool["host_tool_executions"] == [
        {"id": "call_echo_1", "name": ECHO, "arguments": {"text": "lantern"}}
    ]
    assert tool["results"][1]["text"] == "The tool said: echo: lantern."
    gateway.store.close()


def test_the_driver_loads_the_checkout_sources_by_default(node_dependencies, tmp_path):
    gateway, app = client_gateway(tmp_path, [text_turn("Hello.")], external_turns=[])
    with RunningServer(app) as running:
        output = drive_plugin(running.base_url, token_of(app), tmp_path, prompt="Say hello.")
    assert unquote(output["entry"]) == unquote((PACKAGE / "src" / "index.ts").resolve().as_uri())
    gateway.store.close()


def test_a_destination_inside_the_package_is_refused(node_dependencies):
    """A tarball under integrations/openclaw could be packed into the next release."""
    completed = subprocess.run(
        [_node(), str(PACKAGE / "scripts" / "release-pack.mjs"), str(PACKAGE / "dist" / "x")],
        cwd=PACKAGE,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 2
    assert "outside the package" in completed.stderr
    assert not (PACKAGE / "dist" / "x").exists()


def test_a_child_folder_named_like_a_parent_is_still_inside(node_dependencies):
    """relative() gives '..release' for <package>/..release: inside, not a parent."""
    target = PACKAGE / "..release"
    try:
        completed = subprocess.run(
            [_node(), str(PACKAGE / "scripts" / "release-pack.mjs"), "..release"],
            cwd=PACKAGE,
            env={k: v for k, v in os.environ.items() if k != "INIT_CWD"},
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert completed.returncode == 2, completed.stdout + completed.stderr
        assert "outside the package" in completed.stderr
        assert not target.exists()
    finally:
        shutil.rmtree(target, ignore_errors=True)


def test_an_npm_that_is_a_native_shim_gives_a_clear_reason(tmp_path, monkeypatch):
    """No npm-cli.js beside node: None, so the fixture skips or fails with a reason."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "node").write_text("")
    (bin_dir / "npm").write_text("#!/bin/sh\n")
    monkeypatch.setattr(shutil, "which", lambda name: str(bin_dir / name))
    assert _npm_cli(bin_dir / "node") is None


# -- CI (issue #145 item 2) -----------------------------------------------------------------


def _ci_job(ci_text: str) -> dict:
    import yaml

    return yaml.safe_load(ci_text)["jobs"]["openclaw-plugin"]


CI_FILE = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def test_ci_builds_tests_and_runs_the_artifact_through_the_pinned_host_on_each_platform():
    """CI checks the declared host range, builds the release, and runs the host lifecycle."""
    job = _ci_job(CI_FILE.read_text("utf-8"))
    assert job["strategy"]["matrix"]["os"] == ["ubuntu-latest", "macos-latest", "windows-latest"]
    assert job["env"]["RELAYMIND_OPENCLAW_NODE"] == "1"
    (node,) = [s for s in job["steps"] if s.get("uses", "").startswith("actions/setup-node")]
    engines = json.loads((PACKAGE / "package.json").read_text("utf-8"))["engines"]["node"]
    # The lowest Node the pinned host accepts.
    assert node["with"]["node-version"] == "24.22.1"
    assert engines.startswith(">=24.22.1 ")
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    for command in ("npm ci", "npm run typecheck", "npm test", "npm run pack:release"):
        assert command in runs
    assert "scripts/openclaw_install_check.py" in runs
    assert "pytest -m live" not in runs
    # Every OpenClaw test module runs with Node required, so none silently skips.
    tests = Path(__file__).resolve().parent
    for module in sorted(tests.glob("test_openclaw_*.py")):
        assert f"tests/{module.name}" in runs, module.name


DRIVERS = (
    PACKAGE / "test" / "host-transport" / "drive.mjs",
    PACKAGE / "test" / "plugin-transport" / "drive-plugin.mjs",
)


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda p: p.name)
def test_a_driver_knows_it_is_the_entry_point_on_every_supported_platform(driver):
    """The pinned host supports native Windows (#145; section 7.5).

    A driver that builds its own ``file://`` URL from ``process.argv[1]`` never
    matches ``import.meta.url`` on Windows (``file:///C:/...`` against
    ``file://C:\\...``), so it exits 0 without running the scenario or writing
    its output. The entry check compares the URL Node itself builds.
    """
    source = driver.read_text("utf-8")
    assert "file://${process.argv[1]}" not in source
    assert "import.meta.url === pathToFileURL(process.argv[1]).href" in source
