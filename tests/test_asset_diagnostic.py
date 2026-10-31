"""RM40/AC31: source-grounded development references do not invent human labels."""

import copy
import importlib
import json

import pytest

from relaymind.canonical import canonical_digest
from test_asset_corpus import read, stage_fixture, write


def api():
    try:
        return importlib.import_module("relaymind.evaluation.asset_diagnostic")
    except ModuleNotFoundError:
        pytest.fail("RM40 source-grounded diagnostic loader is missing")


@pytest.fixture
def development_input(tmp_path):
    root = tmp_path / "development"
    stage_fixture(root)
    catalog = read(root, "catalog")
    catalog["people"] = []
    write(root, "catalog", catalog)
    doc = dict(
        schema_version=1,
        dataset_id=catalog["dataset_id"],
        profile="source_grounded_development",
        catalog_digest=canonical_digest(catalog),
        evidence=[],
        cases=[
            dict(
                id="cn-en",
                task_text="查看我的日历安排",
                conversation=[],
                lane="zh_hans_to_en",
                query_language="zh",
                requested_kinds=["skill", "mcp_tool"],
                source_asset_ids=["calendar"],
                source_evidence_ids=["original"],
                authorship="generated",
                reference_kind="author_defined",
                reference_class="positive",
                acceptable_asset_ids=["calendar"],
                reference_evidence_ids=["original"],
                notes=(
                    "Synthetic test: Chinese query with an English original; no human annotation."
                ),
            )
        ],
    )
    save_input(root, doc)
    return root


def save_input(root, doc):
    doc.pop("digest", None)
    doc["digest"] = canonical_digest(doc)
    (root / "diagnostic.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8", newline="\n"
    )


def load(root):
    return api().load_asset_diagnostic(
        root / "diagnostic.json", catalog_path=root / "catalog.json", evidence_root=root
    )


def test_rm40_ac31_cn_query_english_skill_without_people_or_human_judgments(development_input):
    value = load(development_input)
    assert value.document["cases"][0]["query_language"] == "zh"
    assert value.catalog["assets"][0]["record"]["language_tags"] == ["en"]
    assert value.catalog["people"] == []
    assert value.qualification == "provisional"
    assert value.acceptance_eligible is False
    assert not hasattr(value, "labels") and not hasattr(value, "lock")
    with pytest.raises(TypeError):
        value.document["cases"][0]["task_text"] = "changed"


@pytest.mark.parametrize(
    "change",
    [
        "original",
        "digest",
        "catalog",
        "target",
        "evidence",
        "duplicate",
        "extra",
        "query_limit",
        "reference_class",
        "order",
    ],
)
def test_rm40_ac31_rejects_tamper_and_unbound_source_references(development_input, change):
    root = development_input
    doc = json.loads((root / "diagnostic.json").read_text(encoding="utf-8"))
    if change == "original":
        (root / "originals/calendar.md").write_bytes(b"changed")
    elif change == "digest":
        doc["digest"] = "0" * 64
    elif change == "catalog":
        doc["catalog_digest"] = "0" * 64
    elif change == "target":
        doc["cases"][0]["acceptable_asset_ids"] = ["missing"]
    elif change == "evidence":
        doc["cases"][0]["source_evidence_ids"] = ["missing"]
    elif change == "duplicate":
        doc["cases"].append(copy.deepcopy(doc["cases"][0]))
    elif change == "extra":
        doc["fake_qualified"] = True
    elif change == "query_limit":
        doc["cases"][0]["task_text"] = "中" * 2731
    elif change == "reference_class":
        doc["cases"][0]["reference_class"] = "negative"
    else:
        doc["cases"][0]["source_evidence_ids"] = ["original", "license"]
    if change == "digest":
        (root / "diagnostic.json").write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    else:
        save_input(root, doc)
    with pytest.raises(ValueError):
        load(root)


def test_rm40_ac31_diagnostic_cannot_be_constructed_with_qualified_status(development_input):
    verified = load(development_input)
    with pytest.raises(TypeError):
        api().VerifiedDiagnostic(verified.document, verified.catalog, qualification="qualified")
