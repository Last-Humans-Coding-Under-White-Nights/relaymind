"""RM40/AC31: fixture acceptance extensions never invent genuine source evidence."""

import copy
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from relaymind.canonical import canonical_digest
from test_asset_corpus import (
    link_fixture_releases,
    read,
    relock,
    revise_fixture_release,
    stage_fixture,
    write,
    write_unanimous_fixture_cases,
)


def assessment_api():
    module = importlib.import_module("relaymind.evaluation.asset_qualification")
    if not hasattr(module, "assess_acceptance_release"):
        pytest.fail("RM40/AC31 exact acceptance extension assessment is not implemented")
    return module.assess_acceptance_release


def classify_api():
    module = importlib.import_module("relaymind.evaluation.asset_splits")
    if not hasattr(module, "classify_latin_overlap"):
        pytest.fail("RM40/AC31 pinned all-target LatinOverlap interface is not implemented")
    return module.classify_latin_overlap


def add_original(root, evidence_id, payload, evidence):
    relative = f"originals/{evidence_id}.txt"
    (root / relative).write_bytes(payload)
    item = dict(
        id=evidence_id,
        kind="original",
        access="redistributable",
        path=relative,
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
    )
    evidence.append(item)
    return item["sha256"]


def replace_catalog(root, assets, evidence):
    catalog = read(root, "catalog")
    catalog.update(
        assets=sorted(assets, key=lambda a: a["record"]["asset_id"]),
        evidence=sorted(evidence, key=lambda e: e["id"]),
    )
    write(root, "catalog", catalog)
    lock = read(root, "lock")
    lock["evidence"] = [
        {key: item[key] for key in ("id", "sha256", "bytes")} for item in catalog["evidence"]
    ]
    write(root, "lock", lock)
    relock(root)


@pytest.fixture(scope="module")
def acceptance_templates(tmp_path_factory):
    """Explicitly synthetic source bytes; purpose remains fixture in both locks."""
    base = tmp_path_factory.mktemp("synthetic-acceptance")
    development, acceptance = base / "development", base / "acceptance"
    stage_fixture(development)
    catalog = read(development, "catalog")
    template = catalog["assets"][0]
    evidence = [item for item in catalog["evidence"] if item["kind"] != "original"]
    assets = []
    for language, origin in [("en", "original_en"), ("zh", "matrix_original_zh")]:
        for index in range(100):
            asset = copy.deepcopy(template)
            asset_id = f"{language}-{index:03d}"
            payload = f"SYNTHETIC fixture original {asset_id}; no real cohort.\n".encode()
            digest = add_original(development, f"original-{asset_id}", payload, evidence)
            asset.update(
                origin=origin,
                original_languages=["en" if language == "en" else "zh-Hans"],
                asset_family_ids=[f"family-{index:03d}"],
                original_evidence_id=f"original-{asset_id}",
                source_url=f"fixture:{asset_id}",
            )
            asset["rights"].update(
                acquisition="permitted",
                evaluation="permitted",
                retention="permitted",
                redistribution="prohibited",
            )
            asset["record"].update(
                asset_id=asset_id,
                source_locator=f"fixture:{asset_id}",
                content_hash=digest,
                description=f"Inspect calendar {index}" if language == "en" else f"查询日程{index}",
                language_tags=asset["original_languages"],
                identity={
                    "package_id": f"en-fixture-{index}" if language == "en" else f"中文技能{index}",
                    "package_version": "1",
                },
            )
            assets.append(asset)
    catalog["people"][0]["competencies"] = ["native_zh", "proficient_en"]
    write(development, "catalog", catalog)
    replace_catalog(development, assets, evidence)
    write_unanimous_fixture_cases(development, [])
    shutil.copytree(development, acceptance)
    revise_fixture_release(acceptance, 2)
    for index in range(100):
        asset_id = f"mcp-{index:03d}"
        server = f"fixture-server-{index:03d}"
        schema = {"type": "object", "properties": {"query": {"type": "string"}}}
        identity = dict(
            server_id=server,
            tool_name="lookup",
            exposed_name="shared_lookup",
            input_schema=schema,
            schema_hash=canonical_digest(schema),
        )
        payload = json.dumps(
            {"fixture": True, "server": server, "tools": [identity]}, sort_keys=True
        ).encode()
        digest = add_original(acceptance, f"original-{asset_id}", payload, evidence)
        entry = copy.deepcopy(assets[0])
        entry.update(
            origin="genuine_mcp",
            original_languages=["en"],
            asset_family_ids=[f"mcp-family-{index:03d}"],
            original_evidence_id=f"original-{asset_id}",
            source_url=f"fixture:{server}",
        )
        entry["record"].update(
            asset_id=asset_id,
            kind="mcp_tool",
            source_locator=f"fixture:{server}",
            content_hash=digest,
            description=f"Fixture tool {index}",
            identity=identity,
        )
        assets.append(entry)
    replace_catalog(acceptance, assets, evidence)
    catalog = read(acceptance, "catalog")
    catalog["equivalent_pairs"] = [
        dict(
            zh_asset_id=f"zh-{index:03d}",
            en_asset_id=f"en-{index:03d}",
            reviewer_ids=["reviewer"],
            evidence_ids=["language_review"],
        )
        for index in range(25)
    ]
    catalog["exclusions"] = [
        dict(
            source_id="fixture-denied",
            original_id="excluded-tool",
            reason="acquisition_denied",
            evidence_ids=["permission"],
        )
    ]
    write(acceptance, "catalog", catalog)
    first = read(acceptance, "cases")
    template_case = read(Path(__file__).parent / "fixtures/asset-corpus", "cases")["cases"][0]
    template_case.update(
        split="held_out",
        target_asset_family_ids=["family-000"],
        task_family_ids=["held-task"],
        task_text="显示日程",
        lane="zh_hans_to_en",
    )
    first["cases"] = [template_case]
    write(acceptance, "cases", first)
    labels = read(acceptance, "labels")
    # Restore base raw/final synthetic rows before using the unanimous builder.
    source = Path(__file__).parent / "fixtures/asset-corpus"
    labels["judgments"] = read(source, "labels")["judgments"]
    labels["adjudications"] = read(source, "labels")["adjudications"]
    for row in labels["judgments"] + labels["adjudications"]:
        row["acceptable_asset_ids"] = ["en-000", "zh-000"]
    write(acceptance, "labels", labels)
    write_unanimous_fixture_cases(acceptance, [template_case])
    return development, acceptance


@pytest.fixture
def corpus_pair(acceptance_templates, tmp_path):
    development, acceptance = tmp_path / "development", tmp_path / "acceptance"
    shutil.copytree(acceptance_templates[0], development)
    shutil.copytree(acceptance_templates[1], acceptance)
    link_fixture_releases(acceptance, {"development": development})
    return development, acceptance


def verified_pair(pair):
    development, acceptance = pair
    module = importlib.import_module("relaymind.evaluation.asset_corpus")
    dev = module.load_asset_release(
        development / "lock.json", evidence_root=development, related_roots={}
    )
    current = module.load_asset_release(
        acceptance / "lock.json",
        evidence_root=acceptance,
        related_roots={"development": development},
    )
    return current, dev


def test_rm40_ac31_exact_200_to_300_fixture_is_never_genuine(corpus_pair):
    current, development = verified_pair(corpus_pair)
    result = assessment_api()(current, development)
    assert result.status == "fixture"
    assert result.counts["assets"] == 300
    assert result.counts["matrix_original_zh"] == 100
    assert result.counts["original_en"] == 100
    assert result.counts["genuine_mcp"] == 100
    assert result.counts["baseline_matched"] == 200
    assert result.counts["equivalent_pairs"] == 25
    assert "asset_cohort_incomplete" not in result.reasons
    assert "development_baseline_changed" not in result.reasons
    assert "external_review_missing" in result.reasons


@pytest.mark.parametrize(
    "change", ["missing", "asset_id", "package_version", "source_version", "content_hash"]
)
def test_rm40_ac31_original_baseline_cannot_drift(corpus_pair, change):
    development, root = corpus_pair
    catalog = read(root, "catalog")
    entry = next(a for a in catalog["assets"] if a["record"]["asset_id"] == "en-000")
    if change == "missing":
        # Keep labeled target present; change an unlabeled baseline asset instead.
        catalog["assets"] = [a for a in catalog["assets"] if a["record"]["asset_id"] != "en-099"]
    elif change == "asset_id":
        next(a for a in catalog["assets"] if a["record"]["asset_id"] == "en-099")["record"][
            "asset_id"
        ] = "renamed"
        catalog["assets"].sort(key=lambda a: a["record"]["asset_id"])
    elif change == "package_version":
        entry["record"]["identity"]["package_version"] = "2"
    elif change == "source_version":
        entry["record"]["source_version"] = entry["source_revision"] = "2"
    else:
        evidence = copy.deepcopy(
            next(e for e in catalog["evidence"] if e["id"] == entry["original_evidence_id"])
        )
        payload = b"SYNTHETIC changed original, never a genuine baseline\n"
        evidence.update(
            id="changed-original",
            path="originals/changed.txt",
            bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        (root / evidence["path"]).write_bytes(payload)
        catalog["evidence"].append(evidence)
        entry["record"]["content_hash"] = evidence["sha256"]
        entry["original_evidence_id"] = evidence["id"]
    replace_catalog(root, catalog["assets"], catalog["evidence"])
    current, dev = verified_pair(corpus_pair)
    result = assessment_api()(current, dev)
    assert result.status == "fixture"
    assert result.counts["baseline_matched"] < 200
    assert "development_baseline_changed" in result.reasons


@pytest.mark.parametrize("count", [99, 101])
def test_rm40_ac31_acceptance_requires_exactly_100_genuine_tool_entries(corpus_pair, count):
    development, root = corpus_pair
    catalog = read(root, "catalog")
    if count == 99:
        catalog["assets"] = [a for a in catalog["assets"] if a["record"]["asset_id"] != "mcp-099"]
    else:
        entry = copy.deepcopy(
            next(a for a in catalog["assets"] if a["record"]["asset_id"] == "mcp-099")
        )
        entry["record"]["asset_id"] = "mcp-100"
        entry["record"]["identity"]["server_id"] = "fixture-server-100"
        entry["record"]["source_locator"] = "fixture:server-100"
        catalog["assets"].append(entry)
    replace_catalog(root, catalog["assets"], catalog["evidence"])
    result = assessment_api()(*verified_pair(corpus_pair))
    assert result.status == "fixture"
    assert result.counts["genuine_mcp"] == count
    assert "asset_cohort_incomplete" in result.reasons


def test_rm40_ac31_same_exposed_names_on_distinct_servers_remain_distinct(corpus_pair):
    current, development = verified_pair(corpus_pair)
    tools = [a["record"] for a in current.catalog["assets"] if a["record"]["kind"] == "mcp_tool"]
    assert len({a["identity"]["server_id"] for a in tools}) == 100
    assert {a["identity"]["exposed_name"] for a in tools} == {"shared_lookup"}
    assert assessment_api()(current, development).status == "fixture"


def test_rm40_ac31_duplicate_tool_identity_is_rejected(corpus_pair):
    _, root = corpus_pair
    catalog = read(root, "catalog")
    tools = [a for a in catalog["assets"] if a["record"]["kind"] == "mcp_tool"]
    tools[1]["record"]["identity"]["server_id"] = tools[0]["record"]["identity"]["server_id"]
    replace_catalog(root, catalog["assets"], catalog["evidence"])
    with pytest.raises(ValueError, match="duplicate_source_identity"):
        verified_pair(corpus_pair)


def test_rm40_ac31_too_few_equivalent_pairs_remains_visible(corpus_pair):
    _, root = corpus_pair
    catalog = read(root, "catalog")
    catalog["equivalent_pairs"].pop()
    write(root, "catalog", catalog)
    relock(root)
    result = assessment_api()(*verified_pair(corpus_pair))
    assert result.status == "fixture"
    assert "equivalent_pairs_incomplete" in result.reasons


@pytest.mark.parametrize(
    "text,target,want",
    [
        ("NAI\u0308VE", "naïve", ["naïve"]),
        ("foo中文bar", "bar", ["bar"]),
        ("lookup_v2", "LOOKUP_v2", ["lookup_v2"]),
        ("123 ___", "123 ___", []),
        ("显示日程", "calendar", []),
    ],
)
def test_rm40_ac31_pinned_latin_overlap_exact_unicode_rules(corpus_pair, text, target, want):
    current, _ = verified_pair(corpus_pair)
    record = copy.deepcopy(current.catalog["assets"][0]["record"])
    record["description"] = target
    record["identity"]["package_id"] = "纯中文"
    case = dict(current.cases["cases"][0], task_text=text, conversation=[])
    result = classify_api()(case, [record], unicode_version=unicodedata.unidata_version)
    assert list(result.token_intersections[record["asset_id"]]) == want
    assert result.all_targets_disjoint is (not want)
    assert result.unicode_version == unicodedata.unidata_version


def test_rm40_ac31_latin_overlap_checks_every_alternative_and_conversation(corpus_pair):
    current, _ = verified_pair(corpus_pair)
    first = copy.deepcopy(current.catalog["assets"][0]["record"])
    second = copy.deepcopy(current.catalog["assets"][1]["record"])
    first.update(description="纯中文", identity={"package_id": "中文一", "package_version": "1"})
    second.update(description="CALENDAR", identity={"package_id": "中文二", "package_version": "1"})
    case = dict(
        current.cases["cases"][0],
        task_text="显示日程",
        conversation=[{"role": "user", "text": "calendar"}],
    )
    result = classify_api()(case, [first, second], unicode_version=unicodedata.unidata_version)
    assert result.token_intersections[first["asset_id"]] == ()
    assert result.token_intersections[second["asset_id"]] == ("calendar",)
    assert result.all_targets_disjoint is False
    with pytest.raises(TypeError):
        result.token_intersections[first["asset_id"]] = ("changed",)
    with pytest.raises(ValueError, match="unicode_version_mismatch"):
        classify_api()(case, [first], unicode_version="0.0.0")


def populate_fixture_cohort(root):
    template = read(root, "cases")["cases"][0]
    rows, classes = [], {}
    lanes = ["zh_hans_to_zh_hans", "en_to_en", "zh_hans_to_en", "en_to_zh_hans", "code_switch"]
    for lane_index, lane in enumerate(lanes):
        for kind_index, (kind, count) in enumerate(
            [("positive", 200), ("negative", 50), ("ambiguous", 30)]
        ):
            for index in range(count):
                case_id = f"cohort-{lane_index}-{kind_index}-{index:03d}"
                case = copy.deepcopy(template)
                text = (
                    "Find request"
                    if lane.startswith("en_")
                    else "查找 request"
                    if lane == "code_switch"
                    else "查找请求"
                )
                case.update(
                    id=case_id,
                    lane=lane,
                    task_text=f"{text} {lane_index}-{kind_index}-{index}",
                    task_family_ids=[f"task-{case_id}"],
                    code_switched=lane == "code_switch",
                    language_tags=["en", "zh-Hans"]
                    if lane == "code_switch"
                    else ["en"]
                    if lane.startswith("en_")
                    else ["zh-Hans"],
                    target_asset_family_ids=[] if kind == "negative" else ["family-000"],
                )
                rows.append(case)
                classes[case_id] = kind
    write_unanimous_fixture_cases(root, rows)
    labels = read(root, "labels")
    for row in labels["judgments"] + labels["adjudications"]:
        kind = classes[row["case_id"]]
        row.update(
            **{"class": kind},
            acceptable_asset_ids=[] if kind == "negative" else ["en-000", "zh-000"],
        )
    write(root, "labels", labels)
    relock(root)


@pytest.fixture(scope="module")
def complete_fixture_cohort(acceptance_templates, tmp_path_factory):
    root = tmp_path_factory.mktemp("complete-synthetic-cohort") / "acceptance"
    shutil.copytree(acceptance_templates[1], root)
    populate_fixture_cohort(root)
    return root


@pytest.fixture
def full_cohort_pair(acceptance_templates, complete_fixture_cohort, tmp_path):
    development, root = tmp_path / "development", tmp_path / "acceptance"
    shutil.copytree(acceptance_templates[0], development)
    shutil.copytree(complete_fixture_cohort, root)
    link_fixture_releases(root, {"development": development})
    return development, root


def test_rm40_ac31_all_required_fixture_cohorts_still_do_not_qualify_real_inputs(full_cohort_pair):
    result = assessment_api()(*verified_pair(full_cohort_pair))
    assert result.status == "fixture"
    for lane in result.counts["lanes"].values():
        assert lane == {"positive": 200, "negative": 50, "ambiguous": 30}
    assert result.counts["no_shared_latin"] == {"zh_hans_to_en": 200, "en_to_zh_hans": 200}
    assert "case_cohort_incomplete" not in result.reasons
    assert "hard_subset_incomplete" not in result.reasons
    assert "external_review_missing" in result.reasons


@pytest.mark.parametrize(
    "lane_index,kind_index", [(index, 0) for index in range(5)] + [(1, 1), (1, 2)]
)
def test_rm40_ac31_every_lane_and_class_minimum_is_individual(
    full_cohort_pair, lane_index, kind_index
):
    _, root = full_cohort_pair
    case_id = f"cohort-{lane_index}-{kind_index}-000"
    cases, labels = read(root, "cases"), read(root, "labels")
    cases["cases"] = [case for case in cases["cases"] if case["id"] != case_id]
    labels["judgments"] = [row for row in labels["judgments"] if row["case_id"] != case_id]
    labels["adjudications"] = [row for row in labels["adjudications"] if row["case_id"] != case_id]
    lane = next(row["lane"] for row in read(root, "cases")["cases"] if row["id"] == case_id)
    for group in labels["agreement"]["groups"]:
        if group["lane"] in ("all", lane):
            for key in group:
                if key != "lane":
                    group[key] -= 1
    write(root, "cases", cases)
    write(root, "labels", labels)
    relock(root)
    result = assessment_api()(*verified_pair(full_cohort_pair))
    assert result.status == "fixture"
    assert "case_cohort_incomplete" in result.reasons
    assert (
        result.counts["lanes"][lane][["positive", "negative", "ambiguous"][kind_index]]
        == [199, 49, 29][kind_index]
    )


@pytest.mark.parametrize("authorship", ["translated", "generated", "diagnostic"])
def test_rm40_ac31_nonoriginal_or_diagnostic_cases_never_fill_required_minima(
    full_cohort_pair, authorship
):
    _, root = full_cohort_pair
    cases = read(root, "cases")
    case = next(row for row in cases["cases"] if row["id"] == "cohort-1-0-000")
    if authorship == "diagnostic":
        case.update(split="diagnostic", target_asset_family_ids=["mcp-family-000"])
        labels = read(root, "labels")
        for row in labels["judgments"] + labels["adjudications"]:
            if row["case_id"] == case["id"]:
                row["acceptable_asset_ids"] = ["mcp-000"]
        write(root, "labels", labels)
    else:
        case["authorship"] = authorship
    write(root, "cases", cases)
    relock(root)
    result = assessment_api()(*verified_pair(full_cohort_pair))
    assert result.counts["lanes"]["en_to_en"]["positive"] == 199
    assert "case_cohort_incomplete" in result.reasons
    assert result.status == "fixture"


@pytest.mark.parametrize("shared_count,failed", [(100, False), (101, True)])
def test_rm40_ac31_half_cross_language_positives_require_all_target_disjointness(
    full_cohort_pair, shared_count, failed
):
    _, root = full_cohort_pair
    cases = read(root, "cases")
    for row in cases["cases"]:
        if row["id"].startswith("cohort-2-0-") and int(row["id"].rsplit("-", 1)[1]) < shared_count:
            row["task_text"] += " calendar"
    write(root, "cases", cases)
    relock(root)
    current, development = verified_pair(full_cohort_pair)
    result = assessment_api()(current, development)
    assert result.status == "fixture"
    assert result.counts["no_shared_latin"]["zh_hans_to_en"] == 200 - shared_count
    assert ("hard_subset_incomplete" in result.reasons) is failed
    assert current.hard_subsets["cohort-2-0-000"]["token_intersections"] == {
        "en-000": ["calendar"],
        "zh-000": [],
    }


@pytest.mark.parametrize("missing", ["permission", "notice", "author", "annotator", "pair_review"])
def test_rm40_ac31_rights_people_and_review_evidence_cannot_be_inferred(corpus_pair, missing):
    _, root = corpus_pair
    catalog = read(root, "catalog")
    if missing == "permission":
        catalog["assets"][0]["rights"]["acquisition"] = "unknown"
        reason = "rights_unqualified"
    elif missing == "notice":
        catalog["assets"][0]["rights"]["notice_evidence_ids"] = []
        reason = "supporting_evidence_missing"
    elif missing == "author":
        catalog["people"][0]["competencies"] = []
        reason = "author_competence_unqualified"
    elif missing == "annotator":
        next(p for p in catalog["people"] if p["id"] == "judge-a")["evidence_ids"] = []
        reason = "supporting_evidence_missing"
    else:
        catalog["equivalent_pairs"][0]["evidence_ids"] = []
        reason = "supporting_evidence_missing"
    write(root, "catalog", catalog)
    relock(root)
    result = assessment_api()(*verified_pair(corpus_pair))
    assert result.status == "fixture"
    assert reason in result.reasons
    assert "external_review_missing" in result.reasons


def test_rm40_ac31_explicit_baseline_must_belong_to_committed_lock_closure(corpus_pair):
    _, root = corpus_pair
    link_fixture_releases(root, {})
    result = assessment_api()(*verified_pair(corpus_pair))
    assert result.counts["baseline_matched"] == 200
    assert "development_baseline_uncommitted" in result.reasons
    assert result.status == "fixture"


def test_rm40_ac31_generic_api_flag_and_frozen_json_never_authenticate_genuine_mcp(
    full_cohort_pair,
):
    development, root = full_cohort_pair
    for directory, purpose in [(development, "development"), (root, "acceptance")]:
        for name in ("catalog", "lock"):
            document = read(directory, name)
            document["purpose"] = purpose
            if name == "lock":
                document.update(state="frozen", frozen_at="2026-10-30T00:00:00Z")
            elif directory == root:
                for entry in document["assets"]:
                    if entry["record"]["kind"] == "mcp_tool":
                        entry["source_url"] = "https://example.invalid/rapidapi/export"
            write(directory, name, document)
        relock(directory)
    link_fixture_releases(root, {"development": development})
    result = assessment_api()(*verified_pair(full_cohort_pair))
    assert result.status == "unverified"
    assert "external_review_missing" in result.reasons
    assert "asset_cohort_incomplete" not in result.reasons
    assert "case_cohort_incomplete" not in result.reasons


def derive_api():
    module = importlib.import_module("relaymind.evaluation.asset_derivation")
    if not hasattr(module, "derive_mcp_export"):
        pytest.fail("RM40/AC31 pinned offline MCP export derivation is not implemented")
    return module.derive_mcp_export


@pytest.fixture
def mcp_input(acceptance_templates, tmp_path):
    root = tmp_path / "supplied-export"
    shutil.copytree(acceptance_templates[1], root)
    return root


def test_rm40_ac31_mcp_export_reproduces_exact_pinned_originals_and_exclusions(mcp_input, tmp_path):
    output = tmp_path / "reproduced"
    original = read(mcp_input, "catalog")
    result = derive_api()(mcp_input, mcp_input / "lock.json", output)
    assert result.lock_digest == read(mcp_input, "lock")["digest"]
    assert result.assessment.status == "fixture"
    assert result.exclusions[0]["original_id"] == "excluded-tool"
    for path in result.output_paths:
        assert path.read_bytes() == (mcp_input / path.relative_to(output)).read_bytes()
    assert read(output, "catalog") == original
    tools = [a for a in read(output, "catalog")["assets"] if a["record"]["kind"] == "mcp_tool"]
    assert len(tools) == 100
    assert len({a["record"]["identity"]["server_id"] for a in tools}) == 100
    assert {a["record"]["identity"]["exposed_name"] for a in tools} == {"shared_lookup"}


@pytest.mark.parametrize("tamper", ["original", "schema_hash", "rights"])
def test_rm40_ac31_invalid_or_denied_mcp_export_leaves_no_output(mcp_input, tmp_path, tamper):
    catalog = read(mcp_input, "catalog")
    entry = next(a for a in catalog["assets"] if a["record"]["kind"] == "mcp_tool")
    if tamper == "original":
        evidence = next(e for e in catalog["evidence"] if e["id"] == entry["original_evidence_id"])
        (mcp_input / evidence["path"]).write_bytes(b"Changed source bytes")
    else:
        if tamper == "schema_hash":
            entry["record"]["identity"]["schema_hash"] = "0" * 64
        else:
            entry["rights"]["acquisition"] = "unknown"
        write(mcp_input, "catalog", catalog)
        relock(mcp_input)
    output = tmp_path / "denied"
    with pytest.raises(ValueError):
        derive_api()(mcp_input, mcp_input / "lock.json", output)
    assert not output.exists()


def test_rm40_ac31_mcp_export_cannot_replace_an_existing_destination(mcp_input, tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    original = output / "original.txt"
    original.write_text("User content", encoding="utf-8", newline="\n")
    with pytest.raises(ValueError, match="output_exists"):
        derive_api()(mcp_input, mcp_input / "lock.json", output)
    assert original.read_text(encoding="utf-8") == "User content"
    assert list(output.iterdir()) == [original]


def test_rm40_ac31_mcp_export_requires_pinned_mcp_records_not_a_generic_source_flag(
    mcp_input, tmp_path
):
    catalog = read(mcp_input, "catalog")
    catalog["assets"] = [a for a in catalog["assets"] if a["record"]["kind"] == "skill"]
    replace_catalog(mcp_input, catalog["assets"], catalog["evidence"])
    output = tmp_path / "no-mcp"
    with pytest.raises(ValueError, match="mcp_catalog_required"):
        derive_api()(mcp_input, mcp_input / "lock.json", output)
    assert not output.exists()


def test_rm40_ac31_generic_api_export_cannot_be_converted_through_mcp_cli(tmp_path):
    source = tmp_path / "generic"
    source.mkdir()
    (source / "openapi.json").write_text(
        json.dumps({"openapi": "3.0.0", "paths": {}}), encoding="utf-8", newline="\n"
    )
    output = tmp_path / "fake-mcp"
    with pytest.raises(ValueError):
        derive_api()(source, source / "openapi.json", output)
    assert not output.exists()


def test_rm40_ac31_mcp_cli_is_offline_and_preserves_fixture_qualification(mcp_input, tmp_path):
    # A declared source locator is provenance data; no fetch or server launch.
    catalog = read(mcp_input, "catalog")
    for entry in catalog["assets"]:
        if entry["record"]["kind"] == "mcp_tool":
            entry["source_url"] = "https://example.invalid/not-a-qualified-mcp-inventory"
    write(mcp_input, "catalog", catalog)
    relock(mcp_input)
    (mcp_input / "never-extract.zip").write_bytes(b"PK unused untrusted archive")
    script = Path(__file__).parents[1] / "scripts/benchmarks/derive_mcp_tools.py"
    output = tmp_path / "cli-output"
    process = subprocess.run(
        [
            sys.executable,
            str(script),
            "--input",
            str(mcp_input),
            "--lock",
            str(mcp_input / "lock.json"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    summary = json.loads(process.stdout)
    assert summary["qualification"] == "fixture"
    assert summary["lock_digest"] == read(mcp_input, "lock")["digest"]
    assert summary["mcp_tools"] == 100
    assert summary["excluded_original_ids"] == ["excluded-tool"]
    assert not (output / "never-extract.zip").exists()


def test_rm40_ac31_repeated_case_iterations_never_repair_an_independent_minimum(full_cohort_pair):
    _, root = full_cohort_pair
    cases = read(root, "cases")
    first = next(case for case in cases["cases"] if case["id"] == "cohort-2-0-000")
    repeated = next(case for case in cases["cases"] if case["id"] == "cohort-2-0-001")
    repeated["task_text"] = first["task_text"]
    repeated["conversation"] = copy.deepcopy(first["conversation"])
    write(root, "cases", cases)
    relock(root)
    current, development = verified_pair(full_cohort_pair)
    result = assessment_api()(current, development)
    assert result.counts["lanes"]["zh_hans_to_en"]["positive"] == 199
    assert result.counts["no_shared_latin"]["zh_hans_to_en"] == 199
    assert result.counts["repeated_input_case_ids"] == ["cohort-2-0-001"]
    assert "case_cohort_incomplete" in result.reasons
    assert "repeated_independent_input" in result.reasons
    assert result.status == "fixture"
    assert current.agreement["all"]["paired_cases"] == 1400


def test_rm40_ac31_unavailable_source_proof_blocks_mcp_reproduction(mcp_input, tmp_path):
    catalog = read(mcp_input, "catalog")
    evidence = next(e for e in catalog["evidence"] if e["id"] == "source_provenance")
    evidence.update(access="restricted", path=None)
    write(mcp_input, "catalog", catalog)
    relock(mcp_input)
    output = tmp_path / "unavailable"
    with pytest.raises(ValueError, match="evidence_unavailable"):
        derive_api()(mcp_input, mcp_input / "lock.json", output)
    assert not output.exists()


def test_rm40_ac31_mcp_cli_reports_invalid_pinned_schema_without_a_traceback(mcp_input, tmp_path):
    catalog = read(mcp_input, "catalog")
    next(a for a in catalog["assets"] if a["record"]["kind"] == "mcp_tool")["record"]["identity"][
        "schema_hash"
    ] = "0" * 64
    write(mcp_input, "catalog", catalog)
    relock(mcp_input)
    output = tmp_path / "invalid"
    process = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts/benchmarks/derive_mcp_tools.py"),
            "--input",
            str(mcp_input),
            "--lock",
            str(mcp_input / "lock.json"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=20,
    )
    assert process.returncode == 1
    assert json.loads(process.stderr) == {"status": "blocked", "reason": "invalid_input_schema"}
    assert not output.exists()


@pytest.mark.parametrize("array", ["equivalent_pairs", "exclusions", "judgments"])
def test_rm40_ac31_corpus_object_arrays_have_explicit_canonical_key_order(corpus_pair, array):
    _, root = corpus_pair
    name = "labels" if array == "judgments" else "catalog"
    document = read(root, name)
    if array == "exclusions":
        other = copy.deepcopy(document[array][0])
        other.update(source_id="fixture-other", original_id=None)
        document[array].append(other)
    document[array].reverse()
    write(root, name, document)
    relock(root)
    with pytest.raises(ValueError, match="unsorted_set"):
        verified_pair(corpus_pair)


def test_rm40_ac31_exclusion_composite_key_is_unique_even_with_nullable_original(corpus_pair):
    _, root = corpus_pair
    catalog = read(root, "catalog")
    row = copy.deepcopy(catalog["exclusions"][0])
    row["original_id"] = None
    catalog["exclusions"] = [row, copy.deepcopy(row)]
    write(root, "catalog", catalog)
    relock(root)
    with pytest.raises(ValueError, match="duplicate"):
        verified_pair(corpus_pair)


def test_rm40_ac31_nullable_exclusion_ids_sort_before_nonempty_ids(corpus_pair):
    _, root = corpus_pair
    catalog = read(root, "catalog")
    entry = copy.deepcopy(catalog["exclusions"][0])
    entry["original_id"] = None
    catalog["exclusions"] = [entry, catalog["exclusions"][0]]
    write(root, "catalog", catalog)
    relock(root)
    current, development = verified_pair(corpus_pair)
    assert [e["original_id"] for e in current.catalog["exclusions"]] == [None, "excluded-tool"]
    assert assessment_api()(current, development).status == "fixture"


def test_rm40_ac31_complete_fixture_cannot_be_upgraded_by_custodian_assertions(full_cohort_pair):
    current, development = verified_pair(full_cohort_pair)
    module = importlib.import_module("relaymind.evaluation.asset_qualification")
    domains = frozenset(
        {
            "rights",
            "source_provenance",
            "language",
            "authorship",
            "independence",
            "adjudication",
            "split",
            "access_control",
            "derivation",
        }
    )
    review = module.CustodianReview(
        current.lock["digest"],
        "reviewer",
        ("access_control",),
        {e.evidence_id: domains for e in current.evidence_findings},
    )
    result = assessment_api()(current, development, custodian_review=review)
    assert result.status == "fixture"
    assert "fixture_purpose" in result.reasons
    assert "external_review_missing" not in result.reasons
    assert "asset_cohort_incomplete" not in result.reasons
    assert "case_cohort_incomplete" not in result.reasons
