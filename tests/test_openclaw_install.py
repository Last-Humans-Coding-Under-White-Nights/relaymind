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


def _npm() -> list[str]:
    """npm through node, so a Windows npm.cmd shim never needs a shell."""
    node = Path(_node())
    candidates = [
        # POSIX: bin/npm links to lib/node_modules/npm/bin/npm-cli.js.
        Path(shutil.which("npm") or node.with_name("npm")).resolve(),
        node.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
        # Windows: npm.cmd sits next to node.exe.
        node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js",
    ]
    cli = next(c for c in candidates if c.suffix == ".js" and c.exists())
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
