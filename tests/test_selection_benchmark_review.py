"""Review edge cases for RM03/RM09/RM16/RM25/RM28, spec section 12.4."""

import asyncio
import json
import sys
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from helpers import load_script_module
from relaymind.adapters import ToolCall
from relaymind.adapters.base import Completed, Usage
from relaymind.canonical import canonical_digest
from relaymind.evaluation import selection_benchmark as bench


def selection(candidate=None, *, identity=None):
    return {
        "schema_version": 1,
        "candidate": candidate or {"kind": "none"},
        "mechanism": "test",
        "confidence": None,
        "selector": identity or {"name": "test", "version": "1"},
        "model_calls": [],
    }


def tool_case():
    return bench.normalize_toolret(
        {
            "id": "test",
            "query": "keep it",
            "positive": [{"name": "keep", "parameters": {}}],
            "negative": [{"name": "drop", "parameters": {}}],
        },
        0,
    )


class ProposalAdapter:
    locality = "local"

    def __init__(self, names=("keep",)):
        self.names = names
        self.counted = []
        self.generated = []

    async def count_tokens(self, messages, tools):
        self.counted.append([t.name for t in tools])
        return 10

    async def generate(self, request):
        self.generated.append([t.name for t in request.tool_schemas])
        for index, name in enumerate(self.names):
            yield ToolCall(str(index), name, {})
        yield Usage(10, 2)
        yield Completed("stop")

    async def cancel(self, generation_id):
        pass


@pytest.mark.parametrize("first_success", [False, True])
def test_early_skill_bypass_clears_previous_diagnostics(first_success):
    """RM16/RM25/RM28: an uncalled case cannot inherit prior token evidence."""

    class Selector:
        name = "model_router"
        diagnostics = {"input_tokens": 999, "usage": {"input_tokens": 999}}

        async def select(self, request, context):
            self.diagnostics.update(input_tokens=17, usage={"input_tokens": 17})
            return selection()

    cases = [{"id": "overflow", "request": "large", "expected": [f"s{i}" for i in range(9)]}]
    if first_success:
        cases.insert(0, {"id": "first", "request": "first", "expected": ["s0"]})
    data = {"catalog": [{"id": f"s{i}", "description": str(i)} for i in range(9)], "cases": cases}
    report = asyncio.run(bench.run_skills(data, Selector(), seed=1))
    row = report["cases"][-1]
    assert row["status"] == "bypass"
    assert "input_tokens" not in row
    assert "usage" not in row
    if first_success:
        assert report["cases"][0]["input_tokens"] == 17


def production_http_selector(monkeypatch, handler):
    from relaymind.selection.http import HttpSelector

    original_client = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs == {"trust_env": False, "follow_redirects": False, "timeout": None}
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return HttpSelector(url="http://127.0.0.1:9000/select", name="test", version="1")


@pytest.fixture
def controlled_selector_time(monkeypatch):
    """Keep HTTP identity/error assertions independent of host speed (RM16/RM28).

    Deadline tests below retain real timers. These synchronous MockTransport
    responses exercise the production chain with a clock that does not advance.
    """
    from relaymind.selection import chain

    async def pending_timer(seconds):
        await asyncio.Event().wait()

    monkeypatch.setattr(
        chain,
        "SelectorChain",
        partial(chain.SelectorChain, clock=lambda: 0.0, sleep=pending_timer),
    )


@pytest.mark.parametrize("track", ["skills", "tools"])
@pytest.mark.parametrize(
    "exception,status",
    [
        (httpx.ReadTimeout, "timeout"),
        (httpx.ConnectTimeout, "timeout"),
        (httpx.ConnectError, "error"),
    ],
)
def test_actual_http_exceptions_are_classified(
    monkeypatch, controlled_selector_time, track, exception, status
):
    """RM25/RM28: production HTTP preserves timeout provenance and failure denominators."""

    def handler(request):
        raise exception("synthetic transport failure", request=request)

    selector = production_http_selector(monkeypatch, handler)

    async def run():
        if track == "skills":
            return await bench.run_skills(
                {
                    "catalog": [],
                    "cases": [{"id": "test", "request": "test", "expected": ["keep"]}],
                },
                selector,
                seed=1,
            )
        return (await bench.run_tools([tool_case()], selector))["narrowing"]

    report = asyncio.run(run())
    row = report["cases"][0]
    assert row["status"] == ("timeout" if status == "timeout" else "invalid_output")
    assert row["predicted"] == []
    assert row["selector_attempt"]["outcome"] == status
    assert row["selector_attempt"]["transport"] == "http"
    assert row["selector_attempt"]["model_calls_claim"] is None
    assert report["summary"]["count"] == 1
    assert report["summary"]["errors"] == (1 if status == "timeout" else 0)
    assert report["summary"]["macro"] == {"precision": 0, "recall": 0, "jaccard": 0}


@pytest.mark.parametrize("track", ["skills", "tools"])
@pytest.mark.parametrize("http_transport", [False, True])
def test_pending_selector_deadline_cancels_work(monkeypatch, track, http_transport):
    """RM25/RM28: the actual outer deadline cancels a pending selector."""
    cancelled = []

    class Selector:
        name = "pending"

        async def select(self, request, context):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

    async def pending_http(request):
        return await Selector().select(None, None)

    selector = production_http_selector(monkeypatch, pending_http) if http_transport else Selector()
    if track == "skills":
        report = asyncio.run(
            bench.run_skills(
                {"catalog": [], "cases": [{"id": "test", "request": "test", "expected": ["keep"]}]},
                selector,
                seed=1,
                timeout=0.01,
            )
        )
    else:
        report = asyncio.run(bench.run_tools([tool_case()], selector, timeout=0.01))["narrowing"]
    assert cancelled == [True]
    assert report["cases"][0]["status"] == "timeout"
    assert report["summary"]["count"] == report["summary"]["errors"] == 1


@pytest.mark.parametrize(
    "failure,status", [(TimeoutError, "timeout"), (RuntimeError, "error"), (None, "invalid_output")]
)
def test_failed_narrowing_records_failed_arm_without_model_work(failure, status):
    """RM25/RM28: failure is not a valid empty-set proposal experiment."""

    class Selector:
        name = "failing"

        async def select(self, request, context):
            if failure:
                raise failure("synthetic")
            return {"bad": "result"}

    adapter = ProposalAdapter()
    report = asyncio.run(bench.run_tools([tool_case()], Selector(), adapter=adapter))
    assert len(adapter.generated) == len(adapter.counted) == 1
    assert set(adapter.generated[0]) == {"keep", "drop"}
    row = report["proposal_narrowed"]["cases"][0]
    assert row["status"] == row["narrowing_status"] == status
    assert row["generation_attempted"] is False
    assert row["predicted"] == []
    assert row["proposal_count"] == 0
    assert row["schema_validity"] is None
    assert "generation_options" not in row and "usage" not in row
    assert row["metrics"] == {"precision": 0, "recall": 0, "jaccard": 0}
    assert all(arm["summary"]["count"] == 1 for arm in report.values())
    assert report["proposal_narrowed"]["summary"]["micro"]["recall"] == 0


@pytest.mark.parametrize("kind", ["none", "clarify"])
def test_valid_abstention_still_measures_empty_arm(kind):
    """RM25/RM28: schema-valid abstention remains an intentional measurement."""

    class Selector:
        name = "test"

        async def select(self, request, context):
            candidate = {"kind": kind}
            if kind == "clarify":
                candidate["clarification"] = "Which resource?"
            return selection(candidate)

    adapter = ProposalAdapter(names=())
    report = asyncio.run(bench.run_tools([tool_case()], Selector(), adapter=adapter))
    assert len(adapter.generated) == 2
    assert adapter.generated[1] == []
    assert report["proposal_narrowed"]["cases"][0]["status"] == "ok"


@pytest.mark.parametrize(
    "budget,names,status,predicted",
    [
        ("one", ("keep", "drop"), "invalid_output", []),
        ("many", ("keep", "drop"), "ok", ["drop", "keep"]),
        ("one", ("keep",), "ok", ["keep"]),
        ("one", ("unknown",), "ok", ["unknown"]),
    ],
)
def test_call_budget_rejection_scores_zero_but_preserves_raw_evidence(
    budget, names, status, predicted
):
    """RM03/RM09/RM25: budget validity and schema validity have separate evidence."""
    report = asyncio.run(
        bench.run_tools(
            [tool_case()],
            SimpleNamespace(name="model_router"),
            adapter=ProposalAdapter(names),
            calls_per_generation=budget,
        )
    )
    for key in ("proposal_full", "proposal_narrowed"):
        row, summary = report[key]["cases"][0], report[key]["summary"]
        assert row["status"] == status
        assert row["predicted"] == predicted
        assert row["proposal_count"] == len(names)
        assert row["usage"] == {"input_tokens": 10, "output_tokens": 2}
        assert row["schema_validity"] == (0 if names == ("unknown",) else 1)
        if status == "invalid_output":
            assert row["proposed_tools"] == ["drop", "keep"]
            assert row["call_budget_exceeded"] is True
            assert (
                summary["macro"] == summary["micro"] == {"precision": 0, "recall": 0, "jaccard": 0}
            )
            assert summary["completeness"] == 0


def test_schema_invalid_arguments_retain_name_metrics():
    """RM09/RM25: schema rejection alone does not remove tool-name credit."""
    case = tool_case()
    case["tools"][0]["input_schema"]["required"] = ["required_arg"]
    case["tools"][0]["input_schema"]["properties"] = {"required_arg": {"type": "string"}}
    score = bench.score_proposals(
        case, [ToolCall("1", "keep", {})], bench.benchmark_registry(case["tools"]), ["keep"]
    )
    assert score["predicted"] == ["keep"]
    assert score["metrics"] == {"precision": 1, "recall": 1, "jaccard": 1}
    assert score["schema_validity"] == 0


@pytest.mark.parametrize("proposal", ["local", "external"])
def test_skills_rejects_proposal_before_sources_or_adapters(tmp_path, monkeypatch, proposal):
    """RM16/RM25: irrelevant options cannot fabricate a model measurement."""
    script = load_script_module("selection_benchmark")

    def forbidden(*args, **kwargs):
        pytest.fail("rejection must precede source, adapter, and network work")

    for name in (
        "application_revision",
        "load_locked",
        "_toolret_cases",
        "LlamaCppAdapter",
        "ChatCompletionsAdapter",
    ):
        monkeypatch.setattr(script, name, forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    args = script.parser().parse_args(
        [
            "--track",
            "skills",
            "--selector",
            "example_match",
            "--proposal",
            proposal,
            "--output",
            str(tmp_path / "out.json"),
        ]
    )
    with pytest.raises(ValueError, match="proposal.*tools"):
        asyncio.run(script.run(args))
    assert args.timeout is None


@pytest.mark.parametrize(
    "identities", [[("actual", "2"), ("actual", "2")], [("a", "1"), ("b", "2")], []]
)
def test_http_report_keeps_configured_identity_and_per_case_claims(
    tmp_path, monkeypatch, controlled_selector_time, identities
):
    """RM16/RM25/RM28: response claims cannot change configured identity or leak cases."""
    script = load_script_module("selection_benchmark")
    original_client = httpx.AsyncClient
    pending = iter(identities)

    def handler(request):
        try:
            name, version = next(pending)
        except StopIteration:
            raise httpx.ConnectError("no identity", request=request) from None
        body = selection(identity={"name": name, "version": version})
        body["model_calls"] = [{"model": name, "call_id": version}]
        return httpx.Response(200, json=body)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw),
    )
    args = script.parser().parse_args(
        [
            "--track",
            "skills",
            "--selector",
            "http://127.0.0.1:9000/select",
            "--selector-name",
            "actual",
            "--selector-version",
            "2",
            "--limit",
            "2",
            "--output",
            str(tmp_path / "out.json"),
        ]
    )
    report = asyncio.run(script.run(args))
    identity = report["selector"]
    assert identity["transport"] == "http"
    assert identity["endpoint"] == args.selector
    assert identity["name"] == "actual"
    assert identity["version"] == "2"
    assert identity["selector_internal_model_cost"] == "not_verified"
    rows = report["results"]["cases"]
    assert len(rows) == report["results"]["summary"]["count"] == 2
    for index, row in enumerate(rows):
        attempt = row["selector_attempt"]
        assert attempt["name"] == "actual"
        assert attempt["version"] == "2"
        assert attempt["transport"] == "http"
        if identities:
            name, version = identities[index]
            assert attempt["model_calls_claim"] == [{"model": name, "call_id": version}]
            assert row["status"] == (
                "ok" if (name, version) == ("actual", "2") else "invalid_output"
            )
        else:
            assert attempt["model_calls_claim"] is None
            assert row["status"] == "invalid_output"


def test_toolret_scripts_share_persistent_cache_default(tmp_path, monkeypatch):
    """RM25: deriving and measuring use the same persistent default cache."""
    runner = load_script_module("selection_benchmark")
    derive = load_script_module("benchmarks/derive_toolret")
    args = runner.parser().parse_args(
        ["--track", "tools", "--selector", "example_match", "--output", "/tmp/out.json"]
    )
    assert (
        args.toolret_cache
        == derive.DEFAULT_TOOLRET_CACHE
        == Path.home() / ".cache/relaymind/toolret"
    )
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            {
                "dataset": bench.TOOLRET_DATASET,
                "revision": bench.TOOLRET_REVISION,
                "cases": [],
            }
        )
    )
    fetched = []
    monkeypatch.setattr(derive, "fetch_shards", lambda cache: fetched.append(cache) or [])
    monkeypatch.setattr(derive, "iter_rows", lambda paths: iter(()))
    monkeypatch.setattr(sys, "argv", ["derive_toolret", "--verify", "--lock", str(lock)])
    derive.main()
    assert fetched == [args.toolret_cache]
    explicit = tmp_path / "cache"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "derive_toolret",
            "--verify",
            "--lock",
            str(lock),
            "--cache-dir",
            str(explicit),
        ],
    )
    derive.main()
    assert fetched[-1] == explicit
    runner_args = runner.parser().parse_args(
        [
            "--track",
            "tools",
            "--selector",
            "example_match",
            "--output",
            "/tmp/out.json",
            "--toolret-cache",
            str(explicit),
        ]
    )
    assert runner_args.toolret_cache == explicit


@pytest.mark.parametrize("change", ["lock_digest", "lock_id", "embedded", "payload"])
def test_skill_case_lock_crosschecks_self_excluding_digest(tmp_path, change):
    """RM25: file digest alone cannot conceal inconsistent case lock evidence."""
    case = {"id": "test", "request": "request", "expected": ["keep"]}
    digest = canonical_digest(case)
    case["sha256"] = digest
    lock_case = {"id": "test", "sha256": digest}
    if change == "lock_digest":
        lock_case["sha256"] = "0" * 64
    elif change == "lock_id":
        lock_case["id"] = "other"
    elif change == "embedded":
        case["sha256"] = "0" * 64
    else:
        case["request"] = "changed"
    path, lock_path = tmp_path / "skills.json", tmp_path / "lock.json"
    path.write_text(json.dumps({"cases": [case]}))
    lock_path.write_text(json.dumps({"sha256": bench.file_digest(path), "cases": [lock_case]}))
    with pytest.raises(ValueError, match="case.*(digest|identit)"):
        bench.load_locked(path, lock_path)


def test_frozen_skills_bytes_and_self_excluding_case_hashes_are_unchanged():
    """RM25: strengthening verification preserves the frozen v1 bytes."""
    root = Path(__file__).resolve().parents[1] / "evaluation"
    path = root / "skills.v1.json"
    assert (
        bench.file_digest(path)
        == "1e89686583aa2334c266bd0b6c26d22129d43a218885b7617e65f2b0bc8670c8"
    )
    data = bench.load_locked(path, root / "skills.v1.lock.json")
    for case in data["cases"]:
        assert case["sha256"] == canonical_digest({k: v for k, v in case.items() if k != "sha256"})


@pytest.mark.parametrize("change", ["none", "shard", "normalization", "revision"])
def test_explicit_parquet_verify_checks_bytes_before_rows_without_fetch_or_write(
    tmp_path, monkeypatch, change
):
    """RM25: --verify covers source bytes and normalization without downloading."""
    derive = load_script_module("benchmarks/derive_toolret")
    rows = [
        {
            "id": str(i),
            "query": str(i),
            "positive": [{"name": f"t{j}", "parameters": {}} for j in range(n)],
            "negative": [],
        }
        for i, n in enumerate((1, 2, 4))
    ]
    _, lock = bench.derive_toolret(rows, revision=bench.TOOLRET_REVISION, seed=7, size=3)
    paths = [tmp_path / f"operator-name-{i}.parquet" for i in range(6)]
    for i, path in enumerate(paths):
        path.write_bytes(str(i).encode())
    lock["shards"] = [
        {"name": n, "sha256": bench.file_digest(p)}
        for n, p in zip(derive.SHARDS, paths, strict=True)
    ]
    if change == "shard":
        paths[-1].write_bytes(b"changed unsampled bytes")
    elif change == "normalization":
        rows[0]["query"] = "changed normalization"
    elif change == "revision":
        lock["revision"] = "0" * 40
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock))
    before = lock_path.read_bytes()
    iterated = []

    def iter_rows(actual):
        iterated.append(list(actual))
        yield from rows

    def forbidden(*args, **kwargs):
        pytest.fail("explicit parquet verification must never fetch")

    monkeypatch.setattr(derive, "fetch_shards", forbidden)
    monkeypatch.setattr(derive, "iter_rows", iter_rows)
    monkeypatch.setattr(
        sys,
        "argv",
        ["derive_toolret", "--verify", "--lock", str(lock_path), "--parquet", *map(str, paths)],
    )
    if change != "none":
        with pytest.raises(ValueError, match="digest|revision"):
            derive.main()
    else:
        derive.main()
    assert lock_path.read_bytes() == before
    assert iterated == ([] if change in ("shard", "revision") else [paths])


def collision_row(first_positive, row_id="collide"):
    """Two source tools whose normalized names collide (section 12.4)."""
    web = {"name": "Search", "description": "search the web", "parameters": {}}
    files = {
        "name": "search",
        "description": "search files",
        "parameters": {"q": {"type": "string"}},
    }
    positive, negative = (web, files) if first_positive else (files, web)
    return {"id": row_id, "query": "find", "positive": [positive], "negative": [negative]}


def test_collision_suffixes_follow_declarations_not_positivity():
    """RM25 §12.4: a collision suffix never tells which candidate is positive."""
    names = []
    for first_positive in (True, False):
        case = bench.normalize_toolret(collision_row(first_positive), 0)
        by_description = {tool["description"]: tool["name"] for tool in case["tools"]}
        assert sorted(by_description.values()) == ["search", "search_1"]
        names.append(by_description)
    assert names[0] == names[1]


def test_identical_colliding_declarations_do_not_favor_the_positive():
    """RM25 §12.4: equal declarations are suffixed in a seeded, not source, order."""
    plain = set()
    for index in range(16):
        tool = {"name": "dup", "description": "same", "parameters": {}}
        row = {"id": f"dup-{index}", "query": "q", "positive": [tool], "negative": [dict(tool)]}
        case = bench.normalize_toolret(row, index)
        assert sorted(t["name"] for t in case["tools"]) == ["dup", "dup_1"]
        plain.add(case["expected"] == ["dup"])
    assert plain == {True, False}


def test_source_order_suffixes_remain_reproducible_for_the_v1_lock():
    """RM25 §12.4: the recorded v1 lock keeps its source-order suffixes."""
    case = bench.normalize_toolret(collision_row(False), 0, suffix_order="source-order")
    assert case["expected"] == ["search"]
    with pytest.raises(ValueError, match="suffix order"):
        bench.normalize_toolret(collision_row(False), 0, suffix_order="unknown")


def test_lock_records_suffix_order_and_verification_follows_it(tmp_path, monkeypatch):
    """RM25 §12.4: each lock is verified with the suffix order it was derived with."""
    derive = load_script_module("benchmarks/derive_toolret")
    rows = [collision_row(n % 2 == 0, f"row-{n}") for n in range(3)]
    for row, extra in zip(rows, (0, 1, 3), strict=True):
        row["positive"] += [{"name": f"extra{j}", "parameters": {}} for j in range(extra)]
    cases, lock = bench.derive_toolret(rows, revision=bench.TOOLRET_REVISION, seed=7, size=3)
    assert lock["name_suffix_order"] == "declaration-digest"
    monkeypatch.setattr(derive, "iter_rows", lambda paths: iter(rows))
    assert derive.verify_cases([], lock) == cases
    v1 = [
        bench.normalize_toolret(row, i, suffix_order="source-order") for i, row in enumerate(rows)
    ]
    v1_lock = {key: value for key, value in lock.items() if key != "name_suffix_order"}
    v1_lock["cases"] = [
        {"id": c["id"], "row_index": c["row_index"], "sha256": canonical_digest(c)} for c in v1
    ]
    assert v1_lock["cases"] != lock["cases"]
    assert derive.verify_cases([], v1_lock) == v1
    with pytest.raises(ValueError, match="digest"):
        derive.verify_cases([], {**v1_lock, "name_suffix_order": "declaration-digest"})


def test_checked_in_v2_tool_lock_keeps_the_v1_sample_with_new_suffixes():
    """RM25 §12.4: v2 changes only the suffix order; the sampled rows are the same."""
    root = Path(__file__).resolve().parents[1] / "evaluation"
    v1 = json.loads((root / "tools.v1.lock.json").read_text(encoding="utf-8"))
    v2 = json.loads((root / "tools.v2.lock.json").read_text(encoding="utf-8"))
    assert "name_suffix_order" not in v1
    assert v2["name_suffix_order"] == "declaration-digest"
    for key in ("dataset", "revision", "seed", "size", "sampling", "population", "shards"):
        assert v1[key] == v2[key]
    assert [(c["id"], c["row_index"]) for c in v1["cases"]] == [
        (c["id"], c["row_index"]) for c in v2["cases"]
    ]


def test_runner_measures_v2_tool_lock_unless_v1_is_asked_for():
    """RM25 §12.4: new runs use the v2 lock; the v1 lock reproduces recorded runs."""
    runner = load_script_module("selection_benchmark")
    base = ["--track", "tools", "--selector", "model_router", "--output", "/tmp/out.json"]
    assert runner.parser().parse_args(base).tools_lock_version == 2
    assert runner.parser().parse_args([*base, "--tools-lock-version", "1"]).tools_lock_version == 1


def test_only_unknown_tool_and_invalid_arguments_are_schema_invalid():
    """RM09 §12.4: a not-permitted proposal is recorded but stays schema-valid."""
    case = tool_case()
    registry = bench.benchmark_registry(case["tools"])
    calls = [ToolCall("1", "keep", {}), ToolCall("2", "drop", {}), ToolCall("3", "gone", {})]
    score = bench.score_proposals(case, calls, registry, ["keep"])
    assert score["rejections"] == ["tool_not_permitted", "unknown_tool"]
    assert score["schema_validity"] == 2 / 3
    row = {**score, "status": "ok"}
    assert bench.summarize([row])["schema_validity"] == 2 / 3


def test_skillsbench_archive_is_checked_against_lock_digests(tmp_path, monkeypatch):
    """RM25 §12.4: a cached or supplied archive must match the locked digest."""
    derive = load_script_module("benchmarks/derive_skillsbench")
    assert derive.DEFAULT_CACHE == Path.home() / ".cache/relaymind/skillsbench"
    root = Path(__file__).resolve().parents[1] / "evaluation"
    lock = json.loads((root / "skills.v1.lock.json").read_text(encoding="utf-8"))
    assert derive.locked_archive_digests(root / "skills.v1.lock.json") == {
        derive.REGISTRY_COMMIT: lock["registry_archive_sha256"],
        derive.TASK_COMMIT: lock["task_archive_sha256"],
    }
    assert derive.locked_archive_digests(tmp_path / "missing.json") == {}

    def forbidden(*args, **kwargs):
        pytest.fail("a cached archive must not be fetched again")

    monkeypatch.setattr(derive.urllib.request, "urlretrieve", forbidden)
    cached = tmp_path / f"skillsbench-{derive.TASK_COMMIT}.tar.gz"
    cached.write_bytes(b"archive")
    digest = bench.file_digest(cached)
    assert derive._archive(derive.TASK_COMMIT, None, tmp_path, digest) == cached
    with pytest.raises(ValueError, match="digest"):
        derive._archive(derive.TASK_COMMIT, None, tmp_path, "0" * 64)
    with pytest.raises(ValueError, match="digest"):
        derive._archive(derive.TASK_COMMIT, cached, tmp_path, "0" * 64)

    def download(url, target):
        Path(target).write_bytes(b"other bytes")

    monkeypatch.setattr(derive.urllib.request, "urlretrieve", download)
    fresh = tmp_path / "fresh"
    with pytest.raises(ValueError, match="digest"):
        derive._archive(derive.TASK_COMMIT, None, fresh, digest)
    assert list(fresh.iterdir()) == []
