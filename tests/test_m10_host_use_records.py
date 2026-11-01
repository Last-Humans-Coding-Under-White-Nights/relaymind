"""#255 committed host-operation records (spec 0.66 sections 7.6.1, 7.6.2, 12.6.6).

RM16, RM39; AC30, AC35. The negative record for mcp_use on the pinned OpenClaw
2026.10.7 host must load with the packaged checker: closed shape, linux_host
machine identity, disabled operation, and every bound capture and inventory
byte. Until the pull request review is written into it the record is pending and
lists no known host limitation. No negative skill_loaded_use record is
committed: the host source shows a native delegation (a retained session access
borrow) that a provider check might use as the section 7.6.1 fence, so
`stop_point_absent` is not established. The shipped adapter registry never
lists either operation.
"""

import collections
import copy
import functools
import hashlib
import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

from relaymind.evaluation.asset_qualification_record import known_host_limitations, load_record

ROOT = Path(__file__).resolve().parents[1]
HOST = "integrations/openclaw/node_modules/openclaw/"
EVIDENCE = ROOT / "docs/evidence/m10"
INVENTORY = EVIDENCE / "openclaw-host-use-search-20261101-vps.json"
MCP_RECORD = EVIDENCE / "m10-negative-mcp-use-openclaw-2026.10.7.json"
RECORDS = {
    "m10-negative-mcp-use-openclaw-2026.10.7": ("mcp_use", "exact_route_absent"),
}


@pytest.mark.parametrize("record_id", sorted(RECORDS))
def test_committed_negative_record_loads_and_disables_the_operation(record_id):
    record = load_record(EVIDENCE / f"{record_id}.json")
    operation = record.document["operation"]
    assert record.record_id == record_id
    assert (record.outcome, record.scope) == ("negative", "host_operation")
    assert (operation["name"], operation["finding"]) == RECORDS[record_id]
    assert operation["disabled"] is True
    assert (operation["host"], operation["host_version"]) == ("openclaw", "2026.10.7")
    assert record.machine["role"] == "linux_host"
    roles = {subject["role"] for subject in record.document["subjects"]}
    assert roles == {"capture", "inventory"}
    # An approved record is a known limitation; a pending one lifts nothing.
    listed = known_host_limitations([record])
    assert listed == (
        []
        if not record.approved
        else [
            dict(
                operation=operation["name"],
                host="openclaw",
                host_version="2026.10.7",
                finding=operation["finding"],
                record_id=record_id,
            )
        ]
    )


def _module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


@functools.cache
def _capture(name):
    """One parsed evidence file, read once per session; callers must not mutate it."""
    return json.loads((EVIDENCE / name).read_text())


def _inventory():
    return _capture(INVENTORY.name)


def cited_source_mismatches(inventory):
    """Cited host sources whose path or SHA-256 differ from the inventory files table."""
    counts = collections.Counter(row["path"] for row in inventory["files"])
    files = {row["path"]: row["sha256"] for row in inventory["files"]}
    problems = [("duplicate", path) for path, count in counts.items() if count > 1]
    loaded = inventory["skill_loaded_use"]
    cited = [
        (row["name"], row["source"])
        for row in (*loaded["surfaces"], *inventory["mcp_use"]["routes"])
    ]
    cited.append(("reset_policy_source", loaded["reset_policy_source"]))
    for name, sources in cited:
        problems += [
            (name, source["path"])
            for source in sources
            if files.get(source["path"]) != source["sha256"]
        ]
    return problems


def test_inventory_concludes_each_finding_and_binds_host_files():
    """Every cited host file names the exact (path, SHA-256) of the files table (#255)."""
    inventory = _inventory()
    assert inventory["host"]["version"] == "2026.10.7"
    assert inventory["skill_loaded_use"]["finding"] == "not_established"
    assert inventory["mcp_use"]["finding"] == "exact_route_absent"
    assert "exact_route_absent" in inventory["mcp_use"]["conclusion"]
    assert all(len(row["sha256"]) == 64 for row in inventory["files"])
    assert cited_source_mismatches(inventory) == []


def test_a_changed_cited_host_hash_is_detected():
    inventory = _inventory()
    changed = copy.deepcopy(inventory)
    changed["mcp_use"]["routes"][0]["source"][0]["sha256"] = "0" * 64
    assert cited_source_mismatches(changed) == [
        ("tools.invoke", changed["mcp_use"]["routes"][0]["source"][0]["path"])
    ]
    reset = copy.deepcopy(inventory)
    reset["skill_loaded_use"]["reset_policy_source"][0]["sha256"] = "0" * 64
    assert cited_source_mismatches(reset) == [
        ("reset_policy_source", reset["skill_loaded_use"]["reset_policy_source"][0]["path"])
    ]
    duplicated = copy.deepcopy(inventory)
    duplicated["files"].append(dict(duplicated["files"][0]))
    assert ("duplicate", inventory["files"][0]["path"]) in cited_source_mismatches(duplicated)


def test_inventory_states_the_spec_0_66_finding_scope():
    """Spec 0.66 section 12.6.6: stop-only points and App view routes do not count."""
    inventory = _inventory()
    loaded, mcp = inventory["skill_loaded_use"], inventory["mcp_use"]
    assert "stop or fence" not in loaded["question"]
    for text in (loaded["question"], loaded["conclusion"]):
        assert "fresh person authority, the session incarnation and the pinned bytes" in text
    # The conclusion must not claim that no host point can fence.
    assert "No host point can fence" not in loaded["conclusion"]
    assert "not established" in loaded["conclusion"]
    for text in (mcp["question"], mcp["conclusion"]):
        assert "independent, non-App MCP tool by its exact native identity" in text
    app = next(row for row in mcp["routes"] if row["name"].startswith("mcp.app.callTool"))
    assert "App-scoped" in app["verdict"] and "does not count" in app["verdict"]
    effective = next(row for row in mcp["routes"] if "tools.effective" in row["name"])
    assert "empty tool list for the person session" not in effective["verdict"]
    assert "groups[].tools" in effective["verdict"]


def _surface(inventory, name):
    return next(row for row in inventory["skill_loaded_use"]["surfaces"] if row["name"] == name)


def test_inventory_records_the_retained_borrow_as_a_fence_candidate():
    """Spec 7.6.1: a supported native delegation can carry the use-time fence.

    The pinned host's retain() borrow re-checks the original person's grant, profile,
    role, sharing and tool policy and the exact session incarnation (sessionId,
    lifecycleRevision, store path and generation) on every assertCurrent(), and
    aborts its signal on revocation. The inventory cites that implementation and
    leaves the finding open instead of claiming stop_point_absent.
    """
    inventory = _inventory()
    retained = _surface(inventory, "GatewaySessionAccessAuthority.retain()")
    paths = {source["path"] for source in retained["source"]}
    assert HOST + "dist/request-authorization-DsdcZTZi.mjs" in paths
    assert "fence candidate" in retained["verdict"]
    for fact in ("lifecycleRevision", "sessionId", "originalGrant", "signal"):
        assert fact in retained["authority"]
    assert inventory["skill_loaded_use"]["open_questions"]


def test_inventory_lists_before_prompt_build_and_tool_authority():
    """Spec 7.6.1: before_prompt_build and ctx.toolAuthority are not person authority."""
    inventory = _inventory()
    row = _surface(inventory, "before_prompt_build (requiresToolAuthority, ctx.toolAuthority)")
    paths = {source["path"] for source in row["source"]}
    assert HOST + "docs/plugins/hooks/prompt-and-session.md" in paths
    assert "logged and skipped" in row["can_stop"]
    assert "not person authority" in row["authority"]


def attempted_route_mismatches(inventory, load=_capture):
    """Attempted routes whose method and outcome the named capture does not hold."""
    problems = []
    for row in inventory["attempted_routes"]["mcp_use"]:
        events = load(row["capture"])["mcp_use"]["inventories"]
        if not any(
            (event["method"], event["status"]) == (row["method"], row["status"]) for event in events
        ):
            problems.append((row["capture"], row["method"]))
    for row in inventory["attempted_routes"]["skill_loaded_use"]:
        cases = load(row["capture"])["ordinary_skill_loaded_use"]["cases"]
        if not any(
            case["case"] == row["case"]
            and case["observed"] == row["observed"]
            and (case.get("intervention") or {}).get("method") == row["intervention"]
            for case in cases
        ):
            problems.append((row["capture"], row["case"]))
    return problems


def test_inventory_lists_the_attempted_routes_from_the_captures():
    """Spec 12.6.6: the search captures name the attempted routes, and what was not tried."""
    inventory = _inventory()
    assert attempted_route_mismatches(inventory) == []
    attempted = inventory["attempted_routes"]
    assert {row["method"] for row in attempted["mcp_use"]} == {
        "mcp.app.discover",
        "tools.effective",
        "mcp.app.listTools",
    }
    assert {"tools.invoke", "mcp.app.callTool"} <= set(attempted["not_attempted"]["mcp_use"])
    assert "none of the candidate gates was exercised" not in inventory["method"]
    changed = copy.deepcopy(inventory)
    changed["attempted_routes"]["mcp_use"][0]["status"] = "refused"
    assert attempted_route_mismatches(changed)


def test_inventory_route_omissions_are_limited_to_the_documented_older_reads():
    """RM16/RM39; AC35: preserve bound evidence and expose its known coverage gap.

    The settlement prose documents these two omitted old-projection reads;
    every method/status pair from the corrected VPS3 capture is listed.
    """
    subjects = load_record(MCP_RECORD).document["subjects"]
    observed = {
        (subject["path"], event["method"], event["status"])
        for subject in subjects
        if subject["role"] == "capture"
        for event in _capture(subject["path"]).get("mcp_use", {}).get("inventories", [])
        if event["method"].startswith(("mcp.app.", "tools."))
    }
    listed = {
        (row["capture"], row["method"], row["status"])
        for row in _inventory()["attempted_routes"]["mcp_use"]
    }
    assert listed <= observed
    assert observed - listed == {
        ("openclaw-mcp-use-20261101-ci.json", "tools.effective", "ok"),
        ("openclaw-mcp-use-20261101-vps2.json", "tools.effective", "ok"),
    }


def test_no_negative_skill_loaded_use_record_is_committed():
    """Spec 12.6.6: stop_point_absent is not established on 2026.10.7 (#255 review)."""
    for path in EVIDENCE.glob("m10-*.json"):
        document = json.loads(path.read_text())
        operation = document.get("operation") or {}
        assert operation.get("name") != "skill_loaded_use", path.name


def test_mcp_record_binds_the_corrected_discovery_capture():
    """RM39/AC35: the bba0cf1 VPS run lists both colliding tools and stays blocked.

    The earlier CI and VPS2 captures recorded an empty tools.effective list because
    the probe read payload.tools; the pinned host returns payload.groups[].tools.
    The current grader therefore rejects their discovery, and accepts it only in
    the corrected capture. No capture passes exact use.
    """
    record = load_record(MCP_RECORD)
    paths = {subject["path"] for subject in record.document["subjects"]}
    assert "openclaw-mcp-use-20261101-vps3.json" in paths
    grade = _module(
        "mcp_use_grade_255", "integrations/openclaw/test/host-mcp-use-qualification/grade.py"
    )
    corrected = _capture("openclaw-mcp-use-20261101-vps3.json")
    assert (corrected["status"], corrected["reason"]) == ("blocked", "exact_route_unqualified")
    assert grade.fixture_discovery_pass(corrected) is True
    listed = [
        event
        for event in corrected["mcp_use"]["inventories"]
        if event["method"] == "tools.effective"
    ]
    assert listed and all(
        {(row["server"], row["original"]) for row in event["mcp_tools"]}
        == {("alpha.one", "append"), ("alpha?one", "append")}
        for event in listed
    )
    assert {e["kind"] for e in corrected["mcp_use"]["server_events"]} <= {"started", "list"}
    for name in ("openclaw-mcp-use-20261101-ci.json", "openclaw-mcp-use-20261101-vps2.json"):
        earlier = _capture(name)
        assert all(
            event["tool_names"] == []
            for event in earlier["mcp_use"]["inventories"]
            if event["method"] == "tools.effective"
        )
        assert grade.fixture_discovery_pass(earlier) is False
    for path in paths - {"openclaw-host-use-search-20261101-vps.json"}:
        assert grade.mcp_use_pass(_capture(path)) is False


MCP_HARNESS = "integrations/openclaw/test/host-mcp-use-qualification/qualify.py"


def capture_revision_mismatches(revisions, capture):
    """Record revisions that the capture's checkout and source inventory contradict."""
    problems = []
    if capture["ordinary_capture"]["environment"]["checkout"] != revisions["repository_commit"]:
        problems.append("repository_commit")
    harness = [row["sha256"] for row in capture["source_inventory"] if row["path"] == MCP_HARNESS]
    if harness != [revisions["harness_sha256"]]:
        problems.append("harness_sha256")
    return problems


def test_mcp_record_revisions_match_the_vps3_capture():
    """RM39/AC35: the record's commit and harness are the ones the VPS3 capture ran."""
    record = load_record(MCP_RECORD)
    capture = _capture("openclaw-mcp-use-20261101-vps3.json")
    revisions = record.document["revisions"]
    assert capture_revision_mismatches(revisions, capture) == []
    other = dict(revisions, repository_commit="0" * 40)
    assert capture_revision_mismatches(other, capture) == ["repository_commit"]
    other = dict(revisions, harness_sha256="0" * 64)
    assert capture_revision_mismatches(other, capture) == ["harness_sha256"]


def _git_blob_sha256(commit, path):
    result = subprocess.run(
        ["git", "show", f"{commit}:{path}"], cwd=ROOT, capture_output=True, check=False
    )
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout).hexdigest()


def test_vps3_tracked_sources_match_the_record_commit():
    """RM39/AC35: each tracked source and fixture row of VPS3 is that commit's bytes."""
    record = load_record(MCP_RECORD)
    commit = record.document["revisions"]["repository_commit"]
    present = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=ROOT, capture_output=True
    )
    if present.returncode != 0:
        pytest.fail(f"commit {commit} is not in this clone; the record check requires full history")
    tracked = set(
        subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", commit],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
    )
    capture = _capture("openclaw-mcp-use-20261101-vps3.json")
    rows = (
        capture["source_inventory"]
        + capture["ordinary_capture"]["environment"]["fixture_plugin_sources"]
    )
    checked = [row for row in rows if row["path"] in tracked]
    assert len(checked) == 58 + 22
    changed = [
        row["path"] for row in checked if _git_blob_sha256(commit, row["path"]) != row["sha256"]
    ]
    assert changed == []


def test_missing_record_commit_fails_instead_of_skipping(monkeypatch):
    """RM16/RM39; AC35: absent historical source cannot pass as a skipped check."""
    run = subprocess.run

    def missing_commit(args, **kwargs):
        if args[:2] == ["git", "cat-file"]:
            return subprocess.CompletedProcess(args, 1)
        return run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", missing_commit)
    with pytest.raises(pytest.fail.Exception, match="not in this clone"):
        try:
            test_vps3_tracked_sources_match_the_record_commit()
        except pytest.skip.Exception as exc:
            raise AssertionError("missing commit was skipped") from exc


def test_ci_runs_record_checks_with_history_and_installed_packages():
    """RM16/RM39; AC35: CI must execute source and installed-byte checks."""
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    for job_name in ("check", "openclaw-plugin"):
        job = jobs[job_name]
        checkout = next(s for s in job["steps"] if s.get("uses") == "actions/checkout@v4")
        assert checkout.get("with", {}).get("fetch-depth") == 0
    plugin = jobs["openclaw-plugin"]
    assert plugin["env"]["RELAYMIND_OPENCLAW_NODE"] == "1"
    assert any("tests/test_m10_host_use_records.py" in s.get("run", "") for s in plugin["steps"])


def _assert_capture_privacy(text):
    assert "runnervm" not in text
    assert re.findall(r"host=(?!sha256:[0-9a-f]{64})[^ |\\\"]+", text) == []
    # These are the documented VPS substitution and the standard CI account.
    allowed = {"/home/vps-user", "/home/runner"}
    homes = re.findall(r"/(?:home|Users)/[^/\\\"\s]+", text)
    assert set(homes) <= allowed, homes
    assert re.findall(r"[A-Za-z]:\\+(?i:Users)\\+[^\\\"\s]+", text) == []


@pytest.mark.parametrize(
    "path",
    [
        "/home/private-account/work",
        "/Users/private-account/work",
        "C:\\Users\\private-account\\work",
        "C:/Users/private-account/work",
        "/home/vps-user-extra/work",
        "/home/runner-extra/work",
    ],
)
def test_capture_privacy_rejects_personal_home_paths(path):
    """RM16/RM37: workspace and source locators must not publish account names."""
    for key in ("workspaceDir", "source_locator"):
        with pytest.raises(AssertionError):
            _assert_capture_privacy(json.dumps({key: path}))


@pytest.mark.parametrize("path", ["/home/vps-user/work", "/home/runner/work", "/tmp/work"])
def test_capture_privacy_allows_documented_placeholders(path):
    """RM16/RM37: retain the documented VPS placeholder and standard CI home."""
    _assert_capture_privacy(json.dumps({"workspaceDir": path}))


def file_hash_mismatches(rows, root):
    """Missing or changed files relative to an independently installed/built tree."""
    return [
        row["path"]
        for row in rows
        if not (root / row["path"]).is_file()
        or hashlib.sha256((root / row["path"]).read_bytes()).hexdigest() != row["sha256"]
    ]


def build_recorded_adapter(commit, root):
    """Build the historical adapter in a disposable tree using the pinned compiler."""
    archive = subprocess.run(
        ["git", "archive", commit, "integrations/openclaw"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as files:
        files.extractall(root, filter="data")
    package = root / "integrations/openclaw"
    current = ROOT / "integrations/openclaw"
    # Reuse npm ci's dependencies only when the entire dependency lock agrees.
    assert (package / "package-lock.json").read_bytes() == (
        current / "package-lock.json"
    ).read_bytes()
    modules = package / "node_modules"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(current / "node_modules"), str(modules))
    else:
        modules.symlink_to(current / "node_modules", target_is_directory=True)
    for config in ("tsconfig.build.json", "tsconfig.control-ui.json"):
        result = subprocess.run(
            [shutil.which("node"), str(modules / "typescript/bin/tsc"), "-p", config],
            cwd=package,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    return root


def built_adapter_revision(dist):
    result = subprocess.run(
        [
            shutil.which("node"),
            "--input-type=module",
            "-e",
            "const n = await import(process.argv[1]); console.log(n.nativeAdapterRevision());",
            (dist / "recommendation-native.js").resolve().as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_file_hash_check_rejects_missing_and_changed_bytes(tmp_path):
    """RM16/RM39; AC35: consistent inventory claims cannot replace installed bytes."""
    path = tmp_path / "host.js"
    path.write_bytes(b"original")
    rows = [{"path": path.name, "sha256": hashlib.sha256(b"original").hexdigest()}]
    assert file_hash_mismatches(rows, tmp_path) == []
    path.write_bytes(b"different")
    assert file_hash_mismatches(rows, tmp_path) == [path.name]
    path.unlink()
    assert file_hash_mismatches(rows, tmp_path) == [path.name]


def test_installed_pinned_host_matches_inventory():
    """RM16/RM39; AC35: hash the actual package, not just repeated inventory rows."""
    from test_openclaw_host_transport import require_node_build_dependencies

    require_node_build_dependencies()
    rows = _inventory()["files"]
    assert rows and all(row["path"].startswith(HOST) for row in rows)
    assert file_hash_mismatches(rows, ROOT) == []


def test_vps3_built_adapter_matches_record(tmp_path):
    """RM16/RM39; AC35: reproduce the recorded build and its adapter revision."""
    from test_openclaw_host_transport import require_node_build_dependencies

    require_node_build_dependencies()
    revisions = load_record(MCP_RECORD).document["revisions"]
    build = build_recorded_adapter(revisions["repository_commit"], tmp_path)
    rows = _capture("openclaw-mcp-use-20261101-vps3.json")["source_inventory"]
    built = [row for row in rows if row["path"].startswith("integrations/openclaw/dist/")]
    assert len(built) == 37
    assert file_hash_mismatches(built, build) == []
    installed = [row for row in rows if "/node_modules/" in row["path"]]
    assert len(installed) == 34
    assert file_hash_mismatches(installed, ROOT) == []
    dist = build / "integrations/openclaw/dist"
    assert built_adapter_revision(dist) == revisions["adapter_revision"]
    # The recomputation must read emitted bytes, including files outside the entry point.
    with (dist / "config.js").open("ab") as stream:
        stream.write(b"\n// changed build\n")
    assert built_adapter_revision(dist) != revisions["adapter_revision"]
    (dist / "config.js").unlink()
    assert file_hash_mismatches(built, build) == ["integrations/openclaw/dist/config.js"]


def test_the_255_captures_publish_no_plaintext_host_name():
    """Spec 12.6.6: only the SHA-256 of a machine's hostname is published."""
    for path in sorted(EVIDENCE.glob("openclaw-*-20261101-*.json")):
        text = path.read_text()
        _assert_capture_privacy(text)


def test_current_loaded_use_predicate_accepts_the_vps2_capture():
    """RM39/AC30: the VPS2 capture predates the selected-skill pin check (5d92b64).

    Its embedded check is false; the current predicate accepts it. The reset case
    is the real-host operator.admin refusal. The fence finding is unchanged.
    """
    qualify = _module(
        "ordinary_qualify_255", "integrations/openclaw/test/host-ordinary-qualification/qualify.py"
    )
    capture = _capture("openclaw-loaded-use-observation-20261101-vps2.json")
    assert capture["checks"]["ordinary_skill_loaded_use_observation"] is False
    assert qualify.ordinary_skill_loaded_use_pass(capture) is True
    reset = [
        case
        for case in capture["ordinary_skill_loaded_use"]["cases"]
        if case.get("case") == "reset"
    ]
    assert [case["observed"] for case in reset] == ["control_refused"]


def shipped_operations(source):
    """The operations arrays of the exported NATIVE_QUALIFICATIONS entries."""
    declaration = re.search(r"export const NATIVE_QUALIFICATIONS\b[^=]*=(.*)", source, re.S)
    assert declaration, "NATIVE_QUALIFICATIONS declaration not found"
    arrays = re.findall(r"\boperations:\s*(?:Object\.freeze\()?\[([^\]]*)\]", declaration[1])
    assert arrays, "no operations array in NATIVE_QUALIFICATIONS"
    return [re.findall(r'"([a-z_]+)"', array) for array in arrays]


def test_shipped_registry_lists_neither_operation():
    """RM39; AC30, AC35: no shipped entry lists skill_loaded_use or mcp_use (#255)."""
    source = (ROOT / "integrations/openclaw/src/recommendation-native-qualification.ts").read_text()
    for operations in shipped_operations(source):
        assert operations and not {"skill_loaded_use", "mcp_use"} & set(operations)


def test_the_registry_check_reads_the_operations_arrays():
    listed = 'export const NATIVE_QUALIFICATIONS: X[] = [{ operations: ["direct_actions"] }];'
    assert shipped_operations(listed) == [["direct_actions"]]
    qualified = listed.replace('["direct_actions"]', 'Object.freeze(["mcp_use"] as const)')
    assert shipped_operations(qualified) == [["mcp_use"]]
    # Code after the registry that names an operation is not a registry entry.
    later = listed + '\nif (op === "mcp_use") {}'
    assert shipped_operations(later) == [["direct_actions"]]
