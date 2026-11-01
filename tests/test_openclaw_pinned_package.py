"""Checks against the pinned OpenClaw 2026.10.7 package that ``npm ci`` installs.

They need ``integrations/openclaw/node_modules`` (``npm ci``) and, for the
``baseUrl`` check, a Node that strips TypeScript types. Without them they skip;
with ``RELAYMIND_OPENCLAW_NODE=1`` (the openclaw-plugin CI job) they fail instead.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from relaymind.evaluation import asset_trial_kit as kit
from test_asset_sources import capture
from test_asset_trial_kit import (
    PLUGIN_NORMALIZED_BASE_URLS,
    PLUGIN_REFUSED_BASE_URLS,
    REJECTED_BASE_URLS,
)

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "integrations/openclaw"
HOST = PACKAGE / "node_modules/openclaw"
REQUIRED = os.environ.get("RELAYMIND_OPENCLAW_NODE") == "1"


def _unmet(reason):
    if REQUIRED:
        pytest.fail(f"RELAYMIND_OPENCLAW_NODE=1 but {reason}")
    pytest.skip(reason)


def test_every_bundled_skill_of_the_pinned_host_is_admitted(tmp_path):
    """RM34/AC25 (#268, #271 review): a local scan admits all 49 bundled skills.

    The package is the one package-lock.json pins; the scan reads a copy, so
    nothing under node_modules is changed. A developer check of the bundled
    files, not an AC25 live host subcase.
    """
    if not (HOST / "skills").is_dir():
        _unmet("the pinned openclaw package is not installed (npm ci)")
    version = json.loads((HOST / "package.json").read_text(encoding="utf-8"))["version"]
    assert version == kit.PINNED_OPENCLAW
    root = tmp_path / "skills"
    shutil.copytree(HOST / "skills", root)
    expected = sorted(p.parent.name for p in root.glob("*/SKILL.md"))
    assert len(expected) == 49
    got = capture(root)
    assert got.complete
    assert got.rejected_count == 0 and got.exclusions == ()
    assert sorted(d.record.identity["package_id"] for d in got.documents) == expected
    assert {d.record.availability for d in got.documents} == {"unknown"}


PROBE = """const m = await import(process.argv[2]);
let text = "";
for await (const chunk of process.stdin) text += chunk;
const out = [];
for (const c of JSON.parse(text)) {
  try { out.push(m.parsePluginConfig({ baseUrl: c }).baseUrl); }
  catch (e) { if (e.name !== "RelayMindConfigError") throw e; out.push(null); }
}
console.log(JSON.stringify(out));
"""

HOSTS = (
    "127.0.0.1",
    "127.255.255.255",
    "0x7F.0.0.01",
    "127.0x0.0",
    "127.16777215",
    "127.16777216",
    "0x7f000001",
    "017700000001",
    "[::1]",
    "[0::1]",
    "[::0:1]",
    "[::127.0.0.1]",
    "[0:0:0:0:0:0:0.0.0.1]",
    "[1::]",
    "LocalHost",
    "%4c%4F%43%41%4c%48%4f%53%54",
    "localhost%2e",
    "local%00host",
    "xn--lcalhost-u9a",
    "127.0.0.1%2e",
    "1.0.0.127",
    "127.0.0.1x",
)


def test_base_url_rules_match_the_pinned_plugin_config(tmp_path):
    """#271 review: the kit's check_base_url against the plugin's own config.ts.

    Node strips the TypeScript types of ``integrations/openclaw/src/config.ts``
    and runs its ``parsePluginConfig``; every case gives the same normal form,
    or both refuse it.
    """
    node = shutil.which("node")
    if node is None:
        _unmet("node is not installed")
    script = tmp_path / "probe.mjs"
    script.write_text(PROBE, encoding="utf-8")
    cases = [u for u, _ in PLUGIN_NORMALIZED_BASE_URLS] + PLUGIN_REFUSED_BASE_URLS
    cases += REJECTED_BASE_URLS + [kit.DEFAULT_BASE_URL, "http://LOCALHOST:8080/v1/"]
    cases += [
        f"{scheme}://{host}{port}{path}"
        for scheme in ("http", "https", "HtTpS")
        for host in HOSTS
        for port in ("", ":80", ":443", ":0", ":65535", ":1x")
        for path in ("/v1", "/v1/", "/./v1", "/a/%2E%2e/v1", "/v1/..", "/v1?", "/v1/#x")
    ]
    completed = subprocess.run(
        [node, str(script), (PACKAGE / "src/config.ts").as_uri()],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    if completed.returncode != 0 and "ERR_UNKNOWN_FILE_EXTENSION" in completed.stderr:
        _unmet("this node cannot strip TypeScript types")
    assert completed.returncode == 0, completed.stderr
    expected = json.loads(completed.stdout)
    assert sum(e is not None for e in expected) > 300

    def ours(url):
        try:
            return kit.check_base_url(url)
        except ValueError:
            return None

    mismatches = [(c, e, ours(c)) for c, e in zip(cases, expected, strict=True) if ours(c) != e]
    assert mismatches == []
