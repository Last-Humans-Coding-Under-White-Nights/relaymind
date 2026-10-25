"""Selection benchmarks use synthetic fixtures, RM02/RM08/RM09/RM25/RM28 §12.4."""

import asyncio
import json
from pathlib import Path

import pytest

from relaymind.adapters import ToolCall
from relaymind.evaluation import selection_benchmark as bench


def test_metrics_empty_macro_and_micro():
    assert bench.set_metrics([], ["a"]) == {"precision": 0, "recall": 0, "jaccard": 0}
    rows = [
        {"expected": ["a", "b"], "predicted": ["a"], "status": "ok"},
        {"expected": ["c"], "predicted": [], "status": "error"},
    ]
    summary = bench.summarize(rows)
    assert summary["macro"] == {"precision": 0.5, "recall": 0.25, "jaccard": 0.25}
    assert summary["micro"] == {"precision": 1, "recall": 1 / 3, "jaccard": 1 / 3}
    assert summary["count"] == 2


def test_lock_rejects_changed_file(tmp_path):
    data = tmp_path / "skills.v1.json"
    data.write_text('{"cases": []}')
    lock = tmp_path / "lock.json"
    lock.write_text(json.dumps({"sha256": bench.file_digest(data)}))
    assert bench.load_locked(data, lock) == {"cases": []}
    data.write_text('{"cases": [1]}')
    with pytest.raises(ValueError, match="digest"):
        bench.load_locked(data, lock)


def test_catalog_cap_preserves_expected_and_leave_one_out():
    cases = [
        {"id": "self", "request": "secret self prompt", "expected": ["s1", "s2"]},
        {"id": "other", "request": "other example", "expected": ["s1"]},
    ]
    data = {"cases": cases, "catalog": [{"id": f"s{i}", "description": str(i)} for i in range(12)]}
    catalog = bench.skill_catalog(data, cases[0], seed=42, cap=8)
    assert len(catalog) == 8
    assert {"s1", "s2"} <= {s["id"] for s in catalog}
    assert catalog == bench.skill_catalog(data, cases[0], seed=42, cap=8)
    assert all("secret self prompt" not in s["examples"] for s in catalog)
    assert next(s for s in catalog if s["id"] == "s1")["examples"] == ["other example"]
    with pytest.raises(ValueError, match="expected"):
        bench.skill_catalog(
            data, {"id": "over", "expected": [f"s{i}" for i in range(9)]}, seed=42, cap=8
        )


def test_normalize_styles_collision_and_handlers_never_run():
    row = {
        "id": "synthetic",
        "query": "Get weather",
        "prompt": "not selector input",
        "positive": [
            json.dumps(
                {
                    "name": "Weather API",
                    "description": "weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                }
            )
        ],
        "negative": [
            json.dumps(
                {
                    "name": "Weather API",
                    "description": "other",
                    "required_parameters": [{"name": "id", "type": "NUMBER"}],
                    "optional_parameters": [{"name": "unit", "type": "STRING"}],
                }
            )
        ],
    }
    case = bench.normalize_toolret(row, 3)
    assert case["expected"] == ["weather_api"]
    assert case["tools"][1]["name"] == "weather_api_1"
    assert case["tools"][1]["input_schema"]["required"] == ["id"]
    assert case["tools"][1]["input_schema"]["properties"]["id"]["type"] == "number"
    assert all(t["input_schema"]["additionalProperties"] is False for t in case["tools"])
    registry = bench.benchmark_registry(case["tools"])
    with pytest.raises(RuntimeError, match="never execute"):
        registry.get("weather_api").declaration.handler({})
    calls = [
        ToolCall("a", "weather_api", {"city": "Paris"}),
        ToolCall("b", "weather_api", {}),
        ToolCall("c", "missing", {}),
    ]
    result = bench.score_proposals(case, calls, registry, ["weather_api"])
    assert result["schema_validity"] == 1 / 3
    assert result["rejections"] == ["invalid_arguments", "unknown_tool"]
    assert result["predicted"] == ["missing", "weather_api"]


def test_stratification_and_row_digest_lock():
    rows = [
        {
            "id": str(i),
            "query": str(i),
            "positive": [json.dumps({"name": f"t{j}", "parameters": {}}) for j in range(n)],
            "negative": [],
        }
        for i, n in enumerate([1] * 7 + [2] * 7 + [4] * 7)
    ]
    cases, lock = bench.derive_toolret(rows, revision="a" * 40, seed=7, size=9)
    assert len(cases) == 9
    assert {n: sum(len(c["expected"]) == n for c in cases) for n in (1, 2, 4)} == {1: 3, 2: 3, 4: 3}
    assert bench.verify_toolret(cases, lock, revision="a" * 40) == cases
    with pytest.raises(ValueError, match="revision"):
        bench.verify_toolret(cases, lock, revision="b" * 40)
    cases[0]["request"] = "changed"
    with pytest.raises(ValueError, match="digest"):
        bench.verify_toolret(cases, lock, revision="a" * 40)


def test_none_does_not_score_alternatives_and_errors_are_retained():
    class Selector:
        name = "synthetic"
        version = "1"
        model_based = False

        async def select(self, request, context):
            if request.run_id.endswith("error"):
                raise TimeoutError("test")
            return {
                "schema_version": 1,
                "candidate": {"kind": "none"},
                "alternatives": ["a"],
                "mechanism": "test",
                "confidence": None,
                "selector": {"name": self.name, "version": self.version},
                "model_calls": [],
            }

    data = {
        "catalog": [{"id": "a", "description": "a"}],
        "cases": [
            {
                "id": "none",
                "request": "nothing",
                "expected": ["a"],
                "category": "x",
                "difficulty": "easy",
            },
            {
                "id": "error",
                "request": "error",
                "expected": ["a"],
                "category": "x",
                "difficulty": "hard",
            },
        ],
    }
    result = asyncio.run(bench.run_skills(data, Selector(), seed=4))
    assert [c["predicted"] for c in result["cases"]] == [[], []]
    assert result["cases"][1]["status"] == "timeout"
    assert result["summary"]["count"] == 2
    assert result["by_category"]["x"]["count"] == 2


def test_checked_in_skills_lock_and_attribution():
    root = Path(__file__).resolve().parents[1] / "evaluation"
    data = bench.load_locked(root / "skills.v1.json", root / "skills.v1.lock.json")
    assert len(data["cases"]) > 50
    assert data["source"]["license"] == "Apache-2.0"
    assert len(data["source"]["commit"]) == 40
    assert (root / "skillsbench.LICENSE").is_file()


def test_skills_derivation_records_duplicates_exclusions_and_metadata(tmp_path):
    from helpers import load_script_module

    derive = load_script_module("benchmarks/derive_skillsbench").derive
    tasks = []
    for name, description in [("a", "first"), ("b", "second"), ("empty", None)]:
        task = tmp_path / "tasks" / name
        task.mkdir(parents=True)
        (task / "task.md").write_text(
            "---\nmetadata:\n  category: coding\n  difficulty: hard\n---\nrequest " + name
        )
        tasks.append({"name": name, "path": f"tasks/{name}", "digest": "upstream:" + name})
        if description:
            skill = task / "environment/skills/common"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: common\ndescription: " + description + "\n---\nbody"
            )
    data = derive(tmp_path, tasks, commit="a" * 40)
    assert len(data["cases"]) == 2
    assert data["catalog"][0]["description"] == "first"
    assert data["catalog"][0]["tasks"] == ["a", "b"]
    assert data["differences"][0]["task"] == "b"
    assert data["excluded"] == [{"id": "empty", "reason": "no bundled skill"}]
    assert data["cases"][0]["category"] == "coding"
    assert data["cases"][0]["registry_digest"] == "upstream:a"
    assert data["cases"][0]["examples"]["common"] == ["request b"]


def test_router_bypass_uses_actual_count_without_generation():
    class Adapter:
        async def count_tokens(self, messages, tools):
            return 2049

        async def generate(self, request):
            raise AssertionError("must bypass")
            yield

    data = {
        "catalog": [{"id": "a", "description": "a"}],
        "cases": [{"id": "large", "request": "large request", "expected": ["a"]}],
    }
    result = asyncio.run(
        bench.run_skills(data, bench.RouterBenchmarkSelector(Adapter(), "test"), seed=1)
    )
    assert result["cases"][0]["status"] == "bypass"
    assert result["cases"][0]["input_tokens"] == 2049
    assert result["summary"]["bypasses"] == 1


def test_tools_full_and_narrowed_generation_and_instructions_are_data():
    from relaymind.adapters.base import Completed

    class Selector:
        name, version = "narrow", "1"

        async def select(self, request, context):
            assert "DO NOT SEND" not in str(request.to_json_object())
            return {
                "schema_version": 1,
                "candidate": {"kind": "skill", "skill_id": "benchmark_case"},
                "tools": ["keep"],
                "mechanism": "test",
                "confidence": None,
                "selector": {"name": self.name, "version": self.version},
                "model_calls": [],
            }

    class Adapter:
        locality = "local"
        offered = []

        async def count_tokens(self, messages, tools):
            return 10

        async def generate(self, request):
            self.offered.append([t.name for t in request.tool_schemas])
            yield ToolCall("1", "keep", {})
            yield Completed("stop")

        async def cancel(self, generation_id):
            pass

    case = bench.normalize_toolret(
        {
            "id": "test",
            "query": "keep it",
            "instruction": "DO NOT SEND",
            "positive": [{"name": "keep", "parameters": {}}],
            "negative": [{"name": "drop", "parameters": {}}],
        },
        0,
    )
    adapter = Adapter()
    report = asyncio.run(bench.run_tools([case], Selector(), adapter=adapter))
    assert set(adapter.offered[0]) == {"keep", "drop"}
    assert adapter.offered[1] == ["keep"]
    assert report["narrowing"]["summary"]["macro"]["precision"] == 1
    assert report["proposal_full"]["cases"][0]["schema_validity"] == 1
    assert report["proposal_narrowed"]["summary"]["macro"]["recall"] == 1


def test_http_selector_uses_published_request_and_validates_response():
    import httpx

    from helpers import load_script_module

    script = load_script_module("selection_benchmark")
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "candidate": {"kind": "none"},
                "mechanism": "test",
                "confidence": None,
                "selector": {"name": "http-test", "version": "2"},
                "model_calls": [],
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            selector = script.HttpSelector("http://127.0.0.1:9000/select", client)
            request = bench.selection_request({"id": "http", "request": "hello"}, [])
            result = await selector.select(request, None)
            assert result["selector"]["version"] == "2"

    asyncio.run(run())
    assert seen[0]["conversation"] == [{"role": "user", "content": "hello"}]


def test_cli_skills_export_records_versions_digests_and_seed(tmp_path):
    from helpers import run_script

    output = tmp_path / "run.json"
    result = run_script(
        "selection_benchmark",
        "--track",
        "skills",
        "--selector",
        "example_match",
        "--limit",
        "2",
        "--seed",
        "7",
        "--output",
        output,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert report["seed"] == 7
    assert report["selector"]["name"] == "example_match"
    assert report["files"]["skills.v1.json"]
    assert len(report["results"]["cases"]) == 2
    assert report["source_training"] == "unknown"
    assert report["machine"]["python"]


def test_external_proposal_context_uses_production_size_guard():
    from relaymind.adapters.chat_completions import ChatCompletionsAdapter, ChatCompletionsConfig

    case = bench.normalize_toolret(
        {
            "id": "limit",
            "query": "large " * 300,
            "positive": [{"name": "keep", "description": "keeps", "parameters": {}}],
            "negative": [],
        },
        0,
    )

    async def run():
        async with ChatCompletionsAdapter(
            ChatCompletionsConfig("https://example.invalid/v1", "test", 100)
        ) as adapter:
            selector = type("Selector", (), {"name": "model_router"})()
            return await bench.run_tools([case], selector, adapter=adapter, context_limit=100)

    report = asyncio.run(run())
    for track in ("proposal_full", "proposal_narrowed"):
        row = report[track]["cases"][0]
        assert row["status"] == "bypass"
        assert row["input_size"]["method"] == "utf8_bytes"
        assert report[track]["summary"]["bypasses"] == 1


def test_http_selector_refuses_nonloopback_addresses():
    import httpx

    from helpers import load_script_module

    script = load_script_module("selection_benchmark")

    async def refuse():
        async with httpx.AsyncClient() as client:
            script.HttpSelector("https://example.org/select", client)

    with pytest.raises(ValueError, match="loopback"):
        asyncio.run(refuse())


def test_router_catalog_overflow_is_counted_as_bypass():
    data = {
        "catalog": [{"id": f"s{i}", "description": str(i)} for i in range(9)],
        "cases": [{"id": "overflow", "request": "all", "expected": [f"s{i}" for i in range(9)]}],
    }
    result = asyncio.run(
        bench.run_skills(data, bench.RouterBenchmarkSelector(None, "test"), seed=1)
    )
    assert result["cases"][0]["status"] == "bypass"
    assert result["summary"]["count"] == 1


def test_invalid_streamed_arguments_count_as_invalid_output():
    from relaymind.adapters.base import GenerationFailed

    class Adapter:
        async def count_tokens(self, messages, tools):
            return 10

        async def generate(self, request):
            yield GenerationFailed("invalid_tool_arguments", "bad JSON")

        async def cancel(self, generation_id):
            pass

    data = {
        "catalog": [{"id": "a", "description": "a"}],
        "cases": [{"id": "invalid", "request": "a", "expected": ["a"]}],
    }
    report = asyncio.run(
        bench.run_skills(data, bench.RouterBenchmarkSelector(Adapter(), "test"), seed=1)
    )
    assert report["cases"][0]["status"] == "invalid_output"
    assert report["summary"]["invalid_output_rate"] == 1


def test_skill_package_difference_in_resource_is_recorded(tmp_path):
    """RM25/RM28 §12.4: differing bundled files count even with the same SKILL.md."""
    from helpers import load_script_module

    derive = load_script_module("benchmarks/derive_skillsbench").derive
    tasks = []
    for name in ("a", "b"):
        task = tmp_path / "tasks" / name
        skill = task / "environment/skills/common"
        (skill / "references").mkdir(parents=True)
        (task / "task.md").write_text("---\nmetadata: {}\n---\nrequest " + name)
        (skill / "SKILL.md").write_text("---\nname: common\ndescription: common skill\n---\nbody")
        (skill / "references/data.txt").write_text("distinct " + name)
        tasks.append({"name": name, "path": f"tasks/{name}", "digest": "upstream:" + name})
    data = derive(tmp_path, tasks, commit="a" * 40)
    assert len(data["differences"]) == 1
    assert data["differences"][0]["task"] == "b"


def test_proposal_summary_reports_weighted_schema_validity_and_bypass_rate():
    rows = [
        {
            "expected": ["a"],
            "predicted": ["a"],
            "status": "ok",
            "proposal_count": 3,
            "schema_validity": 2 / 3,
            "rejections": ["invalid_arguments"],
        },
        {
            "expected": ["b"],
            "predicted": [],
            "status": "bypass",
            "proposal_count": 0,
            "schema_validity": 0,
        },
    ]
    summary = bench.summarize(rows)
    assert summary["schema_validity"] == 2 / 3
    assert summary["bypass_rate"] == 0.5


def test_tool_presentation_order_is_seeded_and_hides_positive_position():
    class Selector:
        name, version = "first", "1"
        orders = []

        async def select(self, request, context):
            self.orders.append(list(request.catalog[0]["allowed_tools"]))
            return {
                "schema_version": 1,
                "candidate": {"kind": "skill", "skill_id": "benchmark_case"},
                "tools": [self.orders[-1][0]],
                "mechanism": "first",
                "confidence": None,
                "selector": {"name": self.name, "version": self.version},
                "model_calls": [],
            }

    cases = [
        bench.normalize_toolret(
            {
                "id": str(i),
                "query": "test",
                "positive": [{"name": "positive", "parameters": {}}],
                "negative": [{"name": "negative", "parameters": {}}],
            },
            i,
        )
        for i in range(20)
    ]
    first, second = Selector(), Selector()
    first.orders, second.orders = [], []
    asyncio.run(bench.run_tools(cases, first, seed=17))
    asyncio.run(bench.run_tools(cases, second, seed=17))
    assert first.orders == second.orders
    assert any(order[0] == "negative" for order in first.orders)
    assert any(order[0] == "positive" for order in first.orders)


def test_toolret_loader_checks_revision_and_changed_rows_before_selector(tmp_path, monkeypatch):
    """RM25 §12.4: source rederivation verifies the locked cases before inference."""
    from helpers import load_script_module

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
    cases, lock = bench.derive_toolret(rows, revision=bench.TOOLRET_REVISION, seed=7, size=3)
    path = tmp_path / "lock.json"
    path.write_text(json.dumps(lock))
    fetched = []
    monkeypatch.setattr(derive, "fetch_shards", lambda *a, **k: fetched.append(True) or [])
    monkeypatch.setattr(derive, "iter_rows", lambda paths: iter(rows))
    with pytest.raises(ValueError, match="revision"):
        derive.load_cases(path, tmp_path, revision="b" * 40)
    assert fetched == []
    assert derive.load_cases(path, tmp_path) == cases
    rows[1]["query"] = "changed source"
    with pytest.raises(ValueError, match="digest"):
        derive.load_cases(path, tmp_path)


def test_skills_derivation_accepts_symlink_to_source_root(tmp_path):
    """RM25: /tmp and /var aliases must not change source derivation."""
    from helpers import load_script_module

    derive = load_script_module("benchmarks/derive_skillsbench").derive
    root = tmp_path / "source"
    skill = root / "tasks/a/environment/skills/example"
    skill.mkdir(parents=True)
    (root / "tasks/a/task.md").write_text("---\nmetadata: {}\n---\nrequest")
    (skill / "SKILL.md").write_text("---\nname: example\ndescription: sample\n---\nbody")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    tasks = [{"name": "a", "path": "tasks/a", "digest": "upstream:a"}]
    assert derive(alias, tasks, commit="a" * 40) == derive(root, tasks, commit="a" * 40)


def test_toolret_shorthand_parameters_preserve_top_level_required():
    """RM09/RM25: ToolRet shorthand required fields remain schema constraints."""
    case = bench.normalize_toolret(
        {
            "id": "required",
            "query": "test",
            "negative": [],
            "positive": [
                {
                    "name": "lookup",
                    "description": "lookup item",
                    "parameters": {"item": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["item"],
                }
            ],
        },
        0,
    )
    assert case["tools"][0]["input_schema"]["required"] == ["item"]
    registry = bench.benchmark_registry(case["tools"])
    scored = bench.score_proposals(case, [ToolCall("1", "lookup", {})], registry, ["lookup"])
    assert scored["rejections"] == ["invalid_arguments"]


def test_toolret_json_schema_normalizes_nested_source_type_aliases():
    """RM09: source dict/float aliases become valid JSON Schema at every depth."""
    case = bench.normalize_toolret(
        {
            "id": "aliases",
            "query": "lookup",
            "negative": [],
            "positive": [
                {
                    "name": "lookup",
                    "description": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filter": {
                                "type": "dict",
                                "properties": {
                                    "ratio": {"type": "float"},
                                    "count": {"type": "int"},
                                },
                            },
                            "items": {"type": "array", "items": {"type": "str"}},
                        },
                    },
                }
            ],
        },
        0,
    )
    schema = case["tools"][0]["input_schema"]
    assert schema["properties"]["filter"]["type"] == "object"
    assert schema["properties"]["filter"]["properties"]["ratio"]["type"] == "number"
    assert schema["properties"]["items"]["items"]["type"] == "string"
    bench.benchmark_registry(case["tools"])


def test_external_proposals_use_production_external_generation_options():
    """RM25: external proposals preserve production provider sampling defaults."""
    from relaymind.adapters.base import Completed
    from relaymind.adapters.chat_completions import ChatCompletionsAdapter, ChatCompletionsConfig

    class Adapter(ChatCompletionsAdapter):
        async def generate(self, request):
            assert request.options.temperature is None
            yield Completed("stop")

    case = bench.normalize_toolret(
        {
            "id": "options",
            "query": "lookup",
            "positive": [{"name": "lookup", "parameters": {}}],
            "negative": [],
        },
        0,
    )

    async def run():
        async with Adapter(
            ChatCompletionsConfig("https://example.invalid/v1", "test", 32000)
        ) as adapter:
            return await bench.run_tools(
                [case], type("Selector", (), {"name": "model_router"})(), adapter=adapter
            )

    report = asyncio.run(run())
    assert report["proposal_full"]["cases"][0]["status"] == "ok"
    assert report["proposal_full"]["cases"][0]["generation_options"]["temperature"] is None


def test_documented_skillsbench_registry_digests_are_verified():
    """RM25: verify upstream digest when its method is documented (§12.4/#143)."""
    from helpers import load_script_module

    derive = load_script_module("benchmarks/derive_skillsbench")
    tasks = [{"name": "example", "path": "tasks/example", "digest": "sha256:" + "a" * 64}]
    with pytest.raises(ValueError, match="digest"):
        derive.verify_registry(tasks, {"tasks/example": "sha256:" + "b" * 64})
    derive.verify_registry(tasks, {"tasks/example": "sha256:" + "a" * 64})


def test_toolret_schema_property_text_shorthand_is_validated():
    """RM09/RM25: textual type shorthand is normalized, not forwarded as a schema."""
    case = bench.normalize_toolret(
        {
            "id": "text-schema",
            "query": "lookup",
            "negative": [],
            "positive": [
                {
                    "name": "lookup",
                    "description": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": "str, optional", "count": "int"},
                    },
                }
            ],
        },
        0,
    )
    schema = case["tools"][0]["input_schema"]
    assert schema["properties"]["query"] == {"type": "string"}
    assert schema["properties"]["count"] == {"type": "integer"}
    bench.benchmark_registry(case["tools"])


@pytest.mark.parametrize("name", ["tools.v1.lock.json", "tools.v2.lock.json"])
def test_checked_in_tool_lock_contains_only_frozen_source_identities(name):
    """RM25 §12.4: no ToolRet source row is redistributed with the lock."""
    root = Path(__file__).resolve().parents[1] / "evaluation"
    lock = json.loads((root / name).read_text())
    assert lock["revision"] == bench.TOOLRET_REVISION
    assert lock["size"] == len(lock["cases"]) == 500
    assert sum(lock["quotas"]) == 500
    assert len(lock["shards"]) == 6
    assert len({case["row_index"] for case in lock["cases"]}) == 500
    assert all(set(case) == {"id", "row_index", "sha256"} for case in lock["cases"])
    assert all(len(case["sha256"]) == 64 for case in lock["cases"])


def test_parameter_named_properties_is_not_misread_as_schema_container():
    """RM09: a source argument named properties stays an argument."""
    case = bench.normalize_toolret(
        {
            "id": "properties-arg",
            "query": "lookup",
            "negative": [],
            "positive": [
                {
                    "name": "lookup",
                    "parameters": {
                        "ids": {"type": "string", "description": "ids"},
                        "properties": {
                            "description": "Comma-separated property names",
                            "type": "str",
                            "default": "",
                        },
                    },
                }
            ],
        },
        0,
    )
    schema = case["tools"][0]["input_schema"]
    assert set(schema["properties"]) == {"ids", "properties"}
    assert schema["properties"]["properties"]["type"] == "string"
    bench.benchmark_registry(case["tools"])


def test_cli_captures_application_identity_before_running_cases(tmp_path, monkeypatch):
    """RM25: commits made during a run cannot relabel the code that started it."""
    from helpers import load_script_module

    script = load_script_module("selection_benchmark")
    started = False

    def revision(root):
        return {"head": "after" if started else "before", "dirty": True}

    async def fake_run(*args, **kwargs):
        nonlocal started
        started = True
        return {"cases": [], "summary": {}}

    monkeypatch.setattr(script, "application_revision", revision)
    monkeypatch.setattr(script, "run_skills", fake_run)
    args = script.parser().parse_args(
        ["--track", "skills", "--selector", "example_match", "--output", str(tmp_path / "run.json")]
    )
    report = asyncio.run(script.run(args))
    assert report["application"]["head"] == "before"
    assert report["implementation_files"]["scripts/selection_benchmark.py"]
    assert report["implementation_files"]["src/relaymind/evaluation/selection_benchmark.py"]


def test_frozen_skills_survive_windows_autocrlf_checkout(tmp_path):
    """RM25: Windows checkout must preserve the bytes covered by the published lock."""
    import shutil
    import subprocess

    root = Path(__file__).resolve().parents[1]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.autocrlf", "true"], check=True)
    (tmp_path / "evaluation").mkdir()
    attributes = root / ".gitattributes"
    if attributes.exists():
        shutil.copyfile(attributes, tmp_path / ".gitattributes")
    for name in ("skills.v1.json", "skills.v1.lock.json"):
        shutil.copyfile(root / "evaluation" / name, tmp_path / "evaluation" / name)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True, capture_output=True)
    for path in (tmp_path / "evaluation").iterdir():
        path.unlink()
    subprocess.run(["git", "-C", str(tmp_path), "checkout-index", "--all"], check=True)
    data = bench.load_locked(
        tmp_path / "evaluation/skills.v1.json", tmp_path / "evaluation/skills.v1.lock.json"
    )
    assert len(data["cases"]) == 87
    assert b"\r\n" not in (tmp_path / "evaluation/skills.v1.json").read_bytes()


def test_locked_benchmark_uses_utf8_under_a_legacy_windows_locale(tmp_path, monkeypatch):
    """RM25: source text is UTF-8 regardless of the operator's Windows codepage."""
    data = tmp_path / "skills.json"
    lock = tmp_path / "skills.lock.json"
    data.write_bytes(json.dumps({"request": "检查数据库"}, ensure_ascii=False).encode("utf-8"))
    lock.write_bytes(json.dumps({"sha256": bench.file_digest(data)}).encode("utf-8"))
    read_text = Path.read_text

    def legacy_read(path, encoding=None, **kwargs):
        return read_text(path, encoding=encoding or "cp1252", **kwargs)

    monkeypatch.setattr(Path, "read_text", legacy_read)
    assert bench.load_locked(data, lock)["request"] == "检查数据库"


def test_tool_narrowing_reports_abstentions_and_unmeasured_schema_validity():
    """RM25: selector abstention is measured; absent proposal validity is not zero."""

    class AbstainingSelector:
        name = "abstain"

        async def select(self, request, context):
            return {
                "schema_version": 1,
                "candidate": {"kind": "none"},
                "mechanism": "test",
                "confidence": None,
                "selector": {"name": self.name, "version": "1"},
                "model_calls": [],
            }

    case = bench.normalize_toolret(
        {
            "id": "diagnostics",
            "query": "request",
            "positive": [{"name": "keep", "parameters": {}}],
            "negative": [],
        },
        0,
    )
    abstained = asyncio.run(bench.run_tools([case], AbstainingSelector()))
    assert abstained["narrowing"]["summary"]["none_rate"] == 1
    assert abstained["narrowing"]["cases"][0]["none"] is True
    assert abstained["narrowing"]["summary"]["schema_validity"] is None
    assert abstained["narrowing"]["summary"]["macro"] == {"precision": 0, "recall": 0, "jaccard": 0}
    baseline = asyncio.run(
        bench.run_tools([case], type("Baseline", (), {"name": "model_router"})())
    )
    assert baseline["narrowing"]["summary"]["none_rate"] == 0
    assert baseline["narrowing"]["summary"]["schema_validity"] is None
    assert baseline["narrowing"]["summary"]["macro"] == {"precision": 1, "recall": 1, "jaccard": 1}


def test_proposal_diagnostics_do_not_infer_abstention_or_validity_without_calls():
    """RM25: an error or no proposed calls supplies no schema-validity denominator."""
    rows = [
        {
            "id": "error",
            "expected": ["keep"],
            "predicted": [],
            "status": "error",
            "proposal_count": 0,
        }
    ]
    summary = bench.summarize(rows)
    assert summary["none_rate"] is None
    assert summary["schema_validity"] is None
    case = {"expected": ["keep"]}
    score = bench.score_proposals(case, [], bench.benchmark_registry([]), [])
    assert score["schema_validity"] is None
