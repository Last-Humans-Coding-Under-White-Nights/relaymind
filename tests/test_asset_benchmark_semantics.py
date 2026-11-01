"""RM16/RM38/RM40/RM41 AC29/AC31/AC32: native paths and pure evidence closure."""

import builtins
import copy
import json

import pytest

from relaymind.canonical import canonical_digest
from test_embedding_activation import configured
from test_embedding_launcher import loader_for


def semantic_production(tmp_path, *, disabled_adaptation=False):
    from relaymind.assets.embedding_config import load_adaptation_config
    from relaymind.assets.embedding_qualification import qualification_key

    root = tmp_path / "original" / "nested"
    semantic_root = root / "semantics"
    semantic_root.mkdir(parents=True)
    hybrid = configured(semantic_root)
    loader = loader_for(hybrid)
    suffix = ""
    if disabled_adaptation:
        raw = dict(
            schema_version=1,
            identity=dict(name="fixture_adaptation", version="1"),
            manifest="manifest.json",
            manifest_digest=hybrid.manifest.manifest_digest,
            representation=dict(
                version="task_text.v1",
                normalization="NFC",
                query_prefix="",
                max_tokens=100,
                overflow="unavailable",
            ),
            similarity=dict(version="exact_cosine.v1", threshold=0.9),
            ranking_version="bounded_votes.v1",
        )
        (semantic_root / "adaptation.json").write_text(json.dumps(raw), encoding="utf-8")
        adaptation = load_adaptation_config(semantic_root / "adaptation.json")
        loader.qualifications = loader.qualifications | {
            qualification_key(adaptation.manifest, adaptation.digest)
        }
        suffix = '\n[asset_adaptation]\nenabled=false\nconfig="semantics/adaptation.json"\n'
    path = root / "production.toml"
    path.write_text(
        """[external_model]
base_url="https://unused.example.invalid/v1"
model="not-used-by-retrieval"
context_limit=4096
[assets]
sources=[{source_id="fixture",kind="metadata_import",path="catalog.json"}]
[recommendations]
enabled=true
user_id="fixture-user"
workspace_id="fixture-workspace"
primary="hybrid"
fallback=false
total_ms=500
primary_ms=150
[asset_hybrid]
manifest="semantics/manifest.json"
config="semantics/hybrid.json"
"""
        + suffix,
        encoding="utf-8",
        newline="\n",
    )
    return path, hybrid, loader


@pytest.mark.parametrize("disabled_adaptation", [False, True])
def test_benchmark_snapshot_keeps_original_relative_semantic_roots(
    tmp_path, monkeypatch, disabled_adaptation
):
    from relaymind import launcher
    from relaymind.evaluation import asset_benchmark

    path, expected, loader = semantic_production(tmp_path, disabled_adaptation=disabled_adaptation)
    # Only trusted constructor qualification changes; both entries call the real parser.
    original_load = launcher.load_config
    monkeypatch.setattr(
        asset_benchmark,
        "load_config",
        lambda path: original_load(path, embedding_loader=loader),
        raising=False,
    )
    monkeypatch.setattr(
        asset_benchmark,
        "parse_launcher_config",
        lambda raw, *, config_dir: launcher.parse_launcher_config(
            raw, config_dir=config_dir, embedding_loader=loader
        ),
        raising=False,
    )
    production = asset_benchmark._load_production_config(path)
    assert production.asset_semantics.hybrid.digest == expected.digest
    assert production.asset_semantics.hybrid.manifest.root == path.parent / "semantics"
    assert production.assets.sources[0].path == path.parent / "catalog.json"
    assert production.asset_semantics.adaptation_enabled is False
    assert (production.asset_semantics.adaptation is not None) == disabled_adaptation


def test_pure_semantic_validation_preserves_bindings_without_opening_any_locator(
    tmp_path, monkeypatch
):
    from relaymind.assets import embedding_config, embedding_manifest

    path, hybrid, loader = semantic_production(tmp_path, disabled_adaptation=True)
    manifest_raw = copy.deepcopy(hybrid.manifest.data)
    hybrid_raw = copy.deepcopy(hybrid.data)
    adaptation_raw = json.loads((path.parent / "semantics/adaptation.json").read_text())

    def forbidden(*args, **kwargs):
        raise AssertionError("offline semantic validation must not read a locator")

    monkeypatch.setattr(embedding_manifest, "read_semantic_json", forbidden)
    monkeypatch.setattr(embedding_config, "read_semantic_json", forbidden)
    original_import = builtins.__import__

    def no_native(name, *args, **kwargs):
        if name.split(".")[0] in {"onnxruntime", "tokenizers", "numpy"}:
            raise AssertionError("offline validation must not import native dependencies")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_native)
    parsed = embedding_manifest.validate_embedding_manifest(manifest_raw)
    assert parsed.root is None and parsed.manifest_digest == hybrid.manifest.manifest_digest
    assert embedding_config.validate_hybrid_config(hybrid_raw, parsed).digest == hybrid.digest
    assert embedding_config.validate_adaptation_config(adaptation_raw, parsed).manifest is parsed
    with pytest.raises(embedding_manifest.EmbeddingError, match="artifact_root_unresolved"):
        embedding_manifest.verify_embedding_files(parsed)
    changed = copy.deepcopy(hybrid_raw)
    changed["manifest_digest"] = "f" * 64
    with pytest.raises(embedding_manifest.EmbeddingError, match="manifest_digest_mismatch"):
        embedding_config.validate_hybrid_config(changed, parsed)
    assert canonical_digest(manifest_raw) == parsed.manifest_digest


@pytest.mark.parametrize(
    "payload",
    [b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":[[[[]]]]}', b"{} {}", b" " * 65537, b"\xff"],
    ids=["duplicate", "nonfinite", "depth", "trailing", "oversize", "utf8"],
)
def test_shared_semantic_byte_parser_refuses_noncanonical_boundaries(payload):
    from relaymind.assets import embedding_manifest

    with pytest.raises(embedding_manifest.EmbeddingError):
        embedding_manifest.parse_semantic_json(payload)


def test_semantic_effective_artifact_and_runtime_closure_is_offline_and_exact(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from relaymind import launcher
    from relaymind.assets import embedding_config, embedding_manifest
    from relaymind.evaluation import asset_evidence
    from relaymind.evaluation.asset_corpus_io import AssetCorpusError

    path, hybrid, loader = semantic_production(tmp_path, disabled_adaptation=True)
    import tomllib

    production = launcher.parse_launcher_config(
        tomllib.loads(path.read_text()), config_dir=path.parent, embedding_loader=loader
    )
    effective = asset_evidence.effective_asset_config(
        production,
        SimpleNamespace(catalog={"assets": []}),
        runner_digest="a" * 64,
        protocol_digest="b" * 64,
    )
    documents = asset_evidence.semantic_config_documents(production)
    required = {reference["document_id"] for reference in effective["references"]}
    assert set(documents) == required and len(documents) == 3
    assert {r["role"] for r in effective["references"]} == {
        "embedding",
        "index_config",
        "adaptation",
    }
    assert {a["role"] for a in effective["artifacts"]} == {
        "embedding",
        "index_config",
        "adaptation",
    }
    assert all(a["provenance"] == "observed" for a in effective["artifacts"])
    assert effective["adaptation"] == {"enabled": False, "config_digest": None}
    environment = asset_evidence.collect_benchmark_environment(production=production)
    assert (
        environment["runtime"],
        environment["runtime_version"],
        environment["runtime_config_digest"],
    ) == ("fixture", "1", hybrid.manifest.manifest_digest)
    manifest = {
        "documents": [
            {
                "id": id,
                "kind": "referenced_config",
                "digest": canonical_digest(value),
                "bytes": len(json.dumps(value).encode()),
                "format": "json",
                "path": None,
            }
            for id, value in sorted(documents.items())
        ]
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("offline closure must not open embedded paths or initialize a runtime")

    monkeypatch.setattr(embedding_manifest, "read_semantic_json", forbidden)
    monkeypatch.setattr(embedding_config, "read_semantic_json", forbidden)
    original_import = builtins.__import__

    def no_native(name, *args, **kwargs):
        if name.split(".")[0] in {"onnxruntime", "tokenizers", "numpy"}:
            return forbidden()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_native)
    asset_evidence.validate_effective_integrity(effective)
    asset_evidence.verify_asset_configuration_closure(documents, manifest, effective, environment)
    for change in ("missing", "tampered", "extra", "runtime", "manifest_locator"):
        changed_documents = copy.deepcopy(documents)
        changed_environment = copy.deepcopy(environment)
        changed_effective = copy.deepcopy(effective)
        if change == "missing":
            changed_documents.pop(next(iter(documents)))
        elif change == "tampered":
            id = next(id for id, doc in documents.items() if "fusion" in doc)
            changed_documents[id]["confidence"]["min_margin"] = 0.002
        elif change == "extra":
            changed_effective["references"].append(
                {"role": "primary_model", "document_id": "referenced_config:" + "f" * 64}
            )
        elif change == "runtime":
            changed_environment["runtime_version"] = "different"
        else:
            id = next(id for id, doc in documents.items() if "ranking_version" in doc)
            changed_documents[id]["manifest"] = "/another/manifest.json"
        with pytest.raises(AssetCorpusError):
            asset_evidence.verify_asset_configuration_closure(
                changed_documents, manifest, changed_effective, changed_environment
            )


def test_retrieval_helper_refuses_enabled_adaptation_instead_of_dropping_it(tmp_path, monkeypatch):
    from relaymind import launcher
    from relaymind.evaluation import asset_benchmark
    from relaymind.evaluation.asset_corpus_io import AssetCorpusError

    path, hybrid, loader = semantic_production(tmp_path, disabled_adaptation=True)
    path.write_text(path.read_text().replace("enabled=false", "enabled=true"), encoding="utf-8")
    monkeypatch.setattr(
        asset_benchmark,
        "parse_launcher_config",
        lambda raw, *, config_dir: launcher.parse_launcher_config(
            raw, config_dir=config_dir, embedding_loader=loader
        ),
    )
    with pytest.raises(AssetCorpusError, match="adaptation_unavailable"):
        asset_benchmark._load_production_config(path)


def test_unused_supplied_semantics_export_private_preimages_and_reload_without_locators(
    tmp_path, monkeypatch
):
    import asyncio
    import tomllib

    from relaymind import launcher
    from relaymind.assets import embedding_config, embedding_manifest
    from relaymind.evaluation import asset_benchmark, asset_evidence
    from test_asset_selection_benchmark import fixture_config

    config = fixture_config(tmp_path, repetitions=1, warmups=0)
    semantic_path, hybrid, loader = semantic_production(tmp_path, disabled_adaptation=True)
    production_path = config.path.parent / config.data["production_config"]
    semantic_root = semantic_path.parent / "semantics"
    production_path.write_text(
        production_path.read_text()
        + "\n[asset_hybrid]\nmanifest="
        + json.dumps(str(semantic_root / "manifest.json"))
        + "\nconfig="
        + json.dumps(str(semantic_root / "hybrid.json"))
        + "\n[asset_adaptation]\nenabled=false\nconfig="
        + json.dumps(str(semantic_root / "adaptation.json"))
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        asset_benchmark,
        "parse_launcher_config",
        lambda raw, *, config_dir: launcher.parse_launcher_config(
            raw, config_dir=config_dir, embedding_loader=loader
        ),
    )
    production = asset_benchmark._load_production_config(production_path)
    effective = asset_evidence.effective_asset_config(
        production,
        config.release,
        runner_digest=canonical_digest(config.inventories["runner_inventory"]),
        protocol_digest=canonical_digest(config.inventories["protocol_inventory"]),
    )
    raw = tomllib.loads(config.path.read_text())
    raw["production_config_digest"] = canonical_digest(effective)
    config.path.write_text(
        "\n".join(f"{key}={json.dumps(value)}" for key, value in raw.items()), encoding="utf-8"
    )
    config = asset_benchmark.load_benchmark_config(config.path)
    report = asyncio.run(asset_benchmark.run_asset_benchmark(config))
    assert report.data["status"] == "completed"
    assert report.data["environment"]["runtime"] is None
    output = tmp_path / "exported.json"
    asset_benchmark.write_asset_benchmark_report(report, output)
    manifest = json.loads(output.with_name(output.name + ".inputs.json").read_text())
    references = [item for item in manifest["documents"] if item["kind"] == "referenced_config"]
    assert len(references) == 3 and all(item["path"] is None for item in references)
    semantic_root.rename(semantic_root.with_name("no-longer-at-embedded-locator"))

    def forbidden(*args, **kwargs):
        raise AssertionError("offline load cannot reopen semantic/artifact locators")

    monkeypatch.setattr(embedding_manifest, "read_semantic_json", forbidden)
    monkeypatch.setattr(embedding_config, "read_semantic_json", forbidden)
    loaded = asset_benchmark.load_asset_benchmark_report(
        output, resolver=output.with_name(output.name + ".resolver.json")
    )
    assert loaded.input_error is None and loaded.inputs is not None
    assert loaded.data == report.data


@pytest.mark.parametrize(
    "payload", [b'{"x":[[[[]]]]}', b" " * 65537], ids=["depth_before_decode", "semantic_byte_cap"]
)
def test_offline_semantic_preimage_uses_the_shared_bounded_parser(payload):
    from relaymind.evaluation.asset_corpus_io import AssetCorpusError
    from relaymind.evaluation.asset_evidence import semantic_document

    with pytest.raises(AssetCorpusError):
        semantic_document(payload, "json", semantic=True)


def test_cancellation_before_the_request_starts_is_a_cancelled_row():
    """RM40/AC31: a cancel before the recommendation exists is recorded, not a crash."""
    import asyncio

    from relaymind.evaluation.asset_benchmark import execute_asset_slot

    class Budget:
        def elapsed_ms(self):
            return 0

    class Service:
        def new_budget(self):
            return Budget()

        def precheck(self, request):
            raise asyncio.CancelledError

    case = dict(task_text="t", conversation=[], language_tags=["en"], code_switched=False)
    normal = dict(requested_kinds=["skill"], selection_scope="installed")
    label = {"class": "easy", "acceptable_asset_ids": ["a"]}
    row, status, recommendation_id = asyncio.run(
        execute_asset_slot(Service(), None, normal, case, dict(ordinal=0), label)
    )
    assert row["status"] == "cancelled" and row["reason"] == "cancelled"
    assert recommendation_id is None
