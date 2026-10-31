"""RM34/RM40, AC25/AC31: genuine source bytes become ordinary product records."""

import hashlib
import importlib
import json

import pytest

from test_asset_archive import archive


def api():
    try:
        return importlib.import_module("relaymind.evaluation.asset_source_catalog")
    except ModuleNotFoundError:
        pytest.fail("RM40 source catalog builder is missing")


def original(*, kind="skill", name="english-skill", payload=None, **changes):
    if payload is None:
        payload = archive(
            [
                (
                    "SKILL.md",
                    (
                        b"---\nname: english-skill\n"
                        b"description: Inspect source code for memory leaks.\n---\n"
                        b"Analyze object lifecycle and unreleased references."
                    ),
                )
            ]
        )
    fields = dict(
        asset_id="source:skill",
        source_id="arbitrary-user-source",
        source_url="https://example.org/original",
        revision="source-revision",
        version="1",
        captured_at="2026-10-30T20:00:00Z",
        kind=kind,
        name=name,
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        format="skill_zip" if kind == "skill" else "mcp_snapshot",
    )
    fields.update(changes)
    return api().SourceOriginal(**fields)


def test_rm40_ac31_builds_real_english_skill_and_retains_zip_and_selected_hashes(tmp_path):
    module = api()
    source = original()
    result = module.build_source_catalog([source], tmp_path / "out", dataset_id="actual-sources")
    data = json.loads(result.catalog_path.read_text(encoding="utf-8"))
    entry = data["assets"][0]
    assert data["people"] == [] and data["equivalent_pairs"] == []
    assert entry["record"]["source_id"] == "arbitrary-user-source"
    assert entry["record"]["identity"] == {"package_id": "english-skill", "package_version": "1"}
    assert entry["original_languages"] == ["en"]
    assert entry["record"]["availability"] == "unknown"
    assert entry["rights"]["redistribution"] == "unknown"
    refs = {e["id"]: e for e in data["evidence"]}
    evidence = refs[entry["original_evidence_id"]]
    assert (
        (result.catalog_path.parent / evidence["path"])
        .read_bytes()
        .startswith(b"---\nname: english-skill")
    )
    assert evidence["sha256"] == entry["record"]["content_hash"]
    assert any(e["sha256"] == source.sha256 for e in data["evidence"])


def test_rm40_ac31_mcp_original_schema_namespace_and_projection_stay_bound(tmp_path):
    schema = {"type": "object", "properties": {"owner": {"type": "string"}}}
    payload = json.dumps(
        {"name": "search_issues", "description": "Search repository issues.", "inputSchema": schema}
    ).encode()
    source = original(
        kind="mcp_tool", name="search_issues", payload=payload, asset_id="source:tool"
    )
    result = api().build_source_catalog([source], tmp_path / "out", dataset_id="actual-sources")
    entry = json.loads(result.catalog_path.read_text(encoding="utf-8"))["assets"][0]
    assert entry["record"]["content_hash"] == hashlib.sha256(payload).hexdigest()
    assert entry["record"]["identity"]["server_id"] == "arbitrary-user-source"
    assert entry["record"]["identity"]["tool_name"] == "search_issues"
    assert entry["origin"] == "genuine_mcp"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("digest", "source_digest_mismatch"),
        ("identity", "package_identity_mismatch"),
        ("duplicate", "duplicate_original"),
    ],
)
def test_rm40_ac31_rejections_preserve_explicit_source_identities(tmp_path, change, reason):
    source = original()
    if change == "digest":
        source = original(sha256="0" * 64)
    if change == "identity":
        source = original(name="different-name")
    sources = [source, original(asset_id="duplicate")] if change == "duplicate" else [source]
    result = api().build_source_catalog(sources, tmp_path / "out", dataset_id="actual-sources")
    data = json.loads(result.catalog_path.read_text(encoding="utf-8"))
    assert any(row["reason"] == reason for row in data["exclusions"])
    assert all(row["original_id"] for row in data["exclusions"])


def test_rm40_ac31_existing_destination_is_never_overwritten(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    (destination / "keep").write_text("retain", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="output_exists"):
        api().build_source_catalog([original()], destination, dataset_id="actual-sources")
    assert (destination / "keep").read_text(encoding="utf-8") == "retain"


def test_rm34_ac25_built_example_imports_through_generic_product_reader(tmp_path):
    from datetime import UTC, datetime

    from relaymind.assets import sources
    from relaymind.assets.settings import SourceConfig

    result = api().build_source_catalog([original()], tmp_path / "out", dataset_id="actual-sources")
    path = result.import_paths["arbitrary-user-source"]
    capture = sources.capture_source(
        SourceConfig("arbitrary-user-source", "metadata_import", path),
        previous=sources.SourceInventory.empty("arbitrary-user-source"),
        context=sources.CaptureContext(datetime(2026, 10, 30, tzinfo=UTC)),
    )
    assert capture.documents[0].record.asset_id == "source:skill"
    assert capture.documents[0].record.availability == "unknown"


def test_rm34_ac25_arbitrary_chinese_original_does_not_require_matrix(tmp_path):
    payload = archive(
        [
            (
                "SKILL.md",
                (
                    "---\nname: local-chinese\n"
                    "description: 检查本地文件夹并归纳其中的文件。\n---\n"
                    "检查本地文件并整理结果。"
                ).encode(),
            )
        ]
    )
    source = original(name="local-chinese", payload=payload, source_id="user-chosen-local")
    result = api().build_source_catalog([source], tmp_path / "out", dataset_id="user-library")
    entry = json.loads(result.catalog_path.read_text(encoding="utf-8"))["assets"][0]
    assert entry["origin"] == "other_original"
    assert entry["original_languages"] == ["zh-Hans"]
    assert entry["record"]["source_id"] == "user-chosen-local"


def test_rm40_ac31_predeclared_rights_and_acquisition_exclusions_preserve_evidence(tmp_path):
    license_bytes = b"Explicit source retention restriction; no skill body is copied."
    license_id = "license:" + hashlib.sha256(license_bytes).hexdigest()
    exclusions = [
        dict(
            source_id="restricted-source",
            original_id="denied-package",
            reason="retention_prohibited",
            evidence_ids=[license_id],
        ),
        dict(
            source_id="restricted-source",
            original_id=None,
            reason="inventory_unavailable",
            evidence_ids=[],
        ),
    ]
    result = api().build_source_catalog(
        [],
        tmp_path / "out",
        dataset_id="actual-sources",
        exclusions=exclusions,
        additional_evidence=[("license", license_bytes)],
    )
    catalog = json.loads(result.catalog_path.read_text(encoding="utf-8"))
    assert catalog["assets"] == [] and result.excluded == 2
    assert catalog["exclusions"][0]["original_id"] is None
    assert catalog["exclusions"][1]["evidence_ids"] == [license_id]
    assert len(list((result.catalog_path.parent / "originals").iterdir())) == 1


def test_rm34_ac25_oversized_original_is_excluded_without_retaining_oversized_bytes(tmp_path):
    source = original(payload=b"x" * (10 * 1024 * 1024 + 1))
    result = api().build_source_catalog([source], tmp_path / "out", dataset_id="actual-sources")
    catalog = json.loads(result.catalog_path.read_text(encoding="utf-8"))
    assert result.admitted == 0 and result.excluded == 1
    assert catalog["exclusions"][0]["reason"] == "source_limit"
    assert not list((result.catalog_path.parent / "originals").glob("*"))
