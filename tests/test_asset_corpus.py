"""RM40/AC31: offline inputs are verified without inventing real evidence."""

import copy
import hashlib
import importlib
import json
import os
import shutil
import unicodedata
from pathlib import Path

import pytest

from relaymind.canonical import canonical_digest

FIXTURE = Path(__file__).parent / "fixtures" / "asset-corpus"


def api():
    try:
        return importlib.import_module("relaymind.evaluation.asset_corpus")
    except ModuleNotFoundError:
        pytest.fail("RM40/AC31 bounded release loader is not implemented")


@pytest.fixture
def release_root(tmp_path):
    root = tmp_path / "release"
    stage_fixture(root)
    return root


def stage_fixture(root):
    shutil.copytree(FIXTURE, root)
    # This provisional test copy pins the interpreter's actual Unicode table;
    # it is never a genuine frozen development/acceptance release.
    lock = read(root, "lock")
    lock["unicode_version"] = unicodedata.unidata_version
    lock.pop("digest")
    lock["digest"] = canonical_digest(lock)
    write(root, "lock", lock)


def read(root, name):
    return json.loads((root / f"{name}.json").read_text(encoding="utf-8"))


def write(root, name, document):
    (root / f"{name}.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )


def relock(root):
    lock = read(root, "lock")
    labels = read(root, "labels")
    labels["cases_digest"] = canonical_digest(read(root, "cases"))
    write(root, "labels", labels)
    for name in ("catalog", "cases", "labels"):
        lock[name]["digest"] = canonical_digest(read(root, name))
    lock.pop("digest")
    lock["digest"] = canonical_digest(lock)
    write(root, "lock", lock)


def load(root, **kwargs):
    return api().load_asset_release(
        root / "lock.json", evidence_root=root, related_roots={}, **kwargs
    )


def test_rm40_ac31_rejects_tampered_linked_original(release_root):
    release = load(release_root)
    assert release.lock["purpose"] == "fixture"
    catalog = read(release_root, "catalog")
    (release_root / "catalog.json").write_text(
        json.dumps(catalog, separators=(",", ":")), encoding="utf-8", newline="\n"
    )
    assert load(release_root).lock["digest"] == release.lock["digest"]
    (release_root / "originals/calendar.md").write_bytes(b"tampered original")
    with pytest.raises(ValueError, match="evidence_digest"):
        load(release_root)


def test_fixture_never_qualifies_baseline(release_root):
    catalog = read(release_root, "catalog")
    template = catalog["assets"][0]
    catalog["assets"] = []
    for n in range(200):
        asset = copy.deepcopy(template)
        asset["record"]["asset_id"] = f"fixture-{n:03d}"
        asset["record"]["identity"]["package_id"] = f"package-{n:03d}"
        asset["record"]["source_locator"] = f"fixture:{n}"
        payload = f"SYNTHETIC independent fixture {n}.\n".encode()
        original = copy.deepcopy(catalog["evidence"][-1])
        original.update(
            id=f"original-{n:03d}",
            kind="original",
            path=f"originals/{n}.md",
            bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        (release_root / original["path"]).write_bytes(payload)
        catalog["evidence"].append(original)
        asset["original_evidence_id"] = original["id"]
        asset["record"]["content_hash"] = original["sha256"]
        catalog["assets"].append(asset)
    catalog["assets"][0]["record"]["asset_id"] = "calendar"
    catalog["assets"].sort(key=lambda a: a["record"]["asset_id"])
    catalog["evidence"].sort(key=lambda e: e["id"])
    lock = read(release_root, "lock")
    lock["evidence"] = [{k: e[k] for k in ("id", "sha256", "bytes")} for e in catalog["evidence"]]
    write(release_root, "lock", lock)
    write(release_root, "catalog", catalog)
    relock(release_root)
    assessment = api().assess_asset_release(load(release_root))
    assert assessment.status == "fixture"
    assert "fixture_purpose" in assessment.reasons


@pytest.mark.parametrize(
    "name,change",
    [
        ("catalog", lambda d: d.update(extra=True)),
        ("cases", lambda d: d.update(schema_version=True)),
        ("cases", lambda d: d.update(release=1.0)),
        ("labels", lambda d: d.update(rubric_version="relevance.v2")),
        ("catalog", lambda d: d["assets"].append(copy.deepcopy(d["assets"][0]))),
        ("catalog", lambda d: d["assets"][0].update(original_evidence_id="unknown")),
        ("labels", lambda d: d["adjudications"][0].update(acceptable_asset_ids=["unknown"])),
        ("labels", lambda d: d["judgments"].pop()),
        ("labels", lambda d: d["judgments"][1].update(annotator_id="judge-a")),
        ("labels", lambda d: d["agreement"]["groups"][0].update(joint_agreements=0)),
        ("catalog", lambda d: d["people"][1]["roles"].append("tuner")),
        ("cases", lambda d: d["cases"][0].update(derived_from_case_id="unknown")),
        ("catalog", lambda d: d["evidence"][0].update(path="../outside")),
        ("catalog", lambda d: d["assets"][0].update(source_revision="2")),
    ],
)
def test_rm40_ac31_rejects_invalid_closed_release(release_root, name, change):
    document = read(release_root, name)
    change(document)
    write(release_root, name, document)
    relock(release_root)
    with pytest.raises(ValueError):
        load(release_root)


@pytest.mark.parametrize(
    "payload",
    [b"{", b'{"schema_version":1,"schema_version":1}', b'{"n":NaN}', b"[" * 2000 + b"]" * 2000],
)
def test_rm40_ac31_rejects_unsafe_json(release_root, payload):
    (release_root / "lock.json").write_bytes(payload)
    with pytest.raises(ValueError):
        load(release_root)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"{", "invalid_json"),
        (b'{"schema_version":1,"schema_version":1}', "duplicate_key"),
        (b'{"n":NaN}', "invalid_number"),
        (b"[]", "invalid_document"),
    ],
)
def test_rm40_ac31_unsafe_json_keeps_its_specific_reason(payload, reason):
    from relaymind.evaluation.asset_corpus_io import AssetCorpusError, parse_json

    with pytest.raises(AssetCorpusError, match=f"^{reason}$"):
        parse_json(payload)


def test_rm40_ac31_symlink_and_oversize_refused(release_root, tmp_path):
    original = release_root / "originals/calendar.md"
    other = tmp_path / "outside"
    other.write_bytes(original.read_bytes())
    original.unlink()
    original.symlink_to(other)
    with pytest.raises(ValueError):
        load(release_root)
    original.unlink()
    with original.open("wb") as file:
        file.truncate(10 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        load(release_root)


def test_rm40_ac31_result_is_deeply_immutable(release_root):
    release = load(release_root)
    with pytest.raises(TypeError):
        release.catalog["assets"][0]["record"]["description"] = "changed"
    with pytest.raises(TypeError):
        release.labels["judgments"].clear()


def test_rm40_ac31_missing_restricted_bytes_are_unverified(release_root):
    catalog = read(release_root, "catalog")
    catalog["purpose"] = "development"
    catalog["evidence"][0].update(access="restricted", path=None)
    write(release_root, "catalog", catalog)
    lock = read(release_root, "lock")
    lock["purpose"] = "development"
    write(release_root, "lock", lock)
    relock(release_root)
    assessment = api().assess_asset_release(load(release_root))
    assert assessment.status == "unverified"
    assert "evidence_unavailable" in assessment.reasons
    assert "external_review_missing" in assessment.reasons


def test_rm40_ac31_rejects_canonical_skill_alias(release_root):
    catalog = read(release_root, "catalog")
    other = copy.deepcopy(catalog["assets"][0])
    other["record"].update(asset_id="renamed", source_locator="fixture:renamed")
    other["record"]["identity"]["package_id"] = "renamed"
    catalog["assets"].append(other)
    write(release_root, "catalog", catalog)
    relock(release_root)
    with pytest.raises(ValueError, match="duplicate_source_identity"):
        load(release_root)


def test_rm40_ac31_shared_mcp_original_keeps_distinct_tool_identities(release_root):
    catalog = read(release_root, "catalog")
    template = catalog["assets"][0]
    catalog["assets"] = []
    for name in ("a", "b"):
        asset = copy.deepcopy(template)
        schema = {"type": "object", "properties": {}}
        asset["record"].update(
            asset_id=name,
            kind="mcp_tool",
            identity={
                "server_id": "fixture-server",
                "tool_name": name,
                "exposed_name": name,
                "input_schema": schema,
                "schema_hash": canonical_digest(schema),
            },
        )
        catalog["assets"].append(asset)
    catalog["assets"][0]["record"]["asset_id"] = "calendar"
    catalog["assets"][1]["record"]["asset_id"] = "second"
    write(release_root, "catalog", catalog)
    relock(release_root)
    assert len(load(release_root).catalog["assets"]) == 2
    catalog["assets"][1]["record"]["identity"]["tool_name"] = "a"
    write(release_root, "catalog", catalog)
    relock(release_root)
    with pytest.raises(ValueError, match="duplicate_source_identity"):
        load(release_root)


def test_rm40_ac31_agreement_uses_both_raw_rows_before_exclusion(release_root):
    labels = read(release_root, "labels")
    labels["judgments"][1].update(
        **{"class": "negative"},
        acceptable_asset_ids=[],
        language_valid=False,
        independent_case=False,
    )
    labels["adjudications"][0].update(disposition="exclude")
    for group in labels["agreement"]["groups"]:
        if group["lane"] in ("all", "en_to_en"):
            for key in (
                "class_agreements",
                "acceptable_set_agreements",
                "language_agreements",
                "independence_agreements",
                "joint_agreements",
                "jaccard_sum",
            ):
                group[key] = 0
    write(release_root, "labels", labels)
    relock(release_root)
    release = load(release_root)
    assert release.agreement["all"]["paired_cases"] == 1
    assert release.agreement["all"]["jaccard_sum"] == 0
    assert len(release.labels["judgments"]) == 2


def test_rm40_ac31_latin_tokens_respect_mixed_scripts():
    splits = importlib.import_module("relaymind.evaluation.asset_splits")
    assert splits.latin_tokens("GitHub_日本語 API_v2 foo中文bar 123 _99 Straße") == frozenset(
        {"github_", "api_v2", "foo", "bar", "strasse"}
    )


def test_rm40_ac31_hard_subset_checks_every_alternative_and_conversation(release_root):
    cases = read(release_root, "cases")
    cases["cases"][0].update(
        lane="zh_hans_to_en",
        task_text="显示日历",
        conversation=[{"role": "user", "text": "CALENDAR"}],
    )
    labels = read(release_root, "labels")
    for group in labels["agreement"]["groups"]:
        if group["lane"] == "en_to_en":
            group["lane"] = "zh_hans_to_en"
        elif group["lane"] == "zh_hans_to_en":
            group["lane"] = "en_to_en"
    labels["agreement"]["groups"].sort(key=lambda g: g["lane"])
    write(release_root, "labels", labels)
    write(release_root, "cases", cases)
    relock(release_root)
    diagnostic = load(release_root).hard_subsets["case-a"]
    assert diagnostic["subset"] == "shared_latin"
    assert diagnostic["token_intersections"]["calendar"] == ["calendar"]
    assert diagnostic["hard_precision_included"] is False


@pytest.mark.parametrize("kind", ["normalized", "task_family", "target_family", "derivation"])
def test_rm40_ac31_cross_release_split_leakage(release_root, tmp_path, kind):
    related = tmp_path / "related"
    shutil.copytree(release_root, related)
    cases = read(related, "cases")
    case = cases["cases"][0]
    case.update(
        id="held-case",
        split="held_out",
        task_text="Different request",
        task_family_ids=["different-task"],
        target_asset_family_ids=["different-target"],
    )
    if kind == "normalized":
        case["task_text"] = "  SHOW\tMy   CALENDAR  "
    if kind == "task_family":
        case["task_family_ids"] = ["task-calendar"]
    if kind == "target_family":
        case["target_asset_family_ids"] = ["calendar-family"]
    if kind == "derivation":
        case["derived_from_case_id"] = "case-a"
    # Held case is negative so its empty target set does not introduce an
    # unrelated target-coverage failure before the leakage boundary is tested.
    labels = read(related, "labels")
    for row in labels["judgments"] + labels["adjudications"]:
        row.update(
            case_id="held-case",
            **{"class": "positive" if kind == "target_family" else "negative"},
            acceptable_asset_ids=["calendar"] if kind == "target_family" else [],
        )
    write(related, "cases", cases)
    write(related, "labels", labels)
    for name in ("catalog", "cases", "labels", "lock"):
        document = read(related, name)
        document["release"] = 2
        write(related, name, document)
    relock(related)
    lock = read(release_root, "lock")
    lock["related_locks"] = [
        {"id": "held", "path": "lock.json", "digest": read(related, "lock")["digest"]}
    ]
    write(release_root, "lock", lock)
    relock(release_root)
    with pytest.raises(ValueError, match="split|derivation"):
        api().load_asset_release(
            release_root / "lock.json", evidence_root=release_root, related_roots={"held": related}
        )


def test_rm40_ac31_related_digest_must_match(release_root, tmp_path):
    related = tmp_path / "related"
    shutil.copytree(release_root, related)
    lock = read(release_root, "lock")
    lock["related_locks"] = [{"id": "other", "path": "lock.json", "digest": "0" * 64}]
    write(release_root, "lock", lock)
    relock(release_root)
    with pytest.raises(ValueError, match="lock_digest"):
        api().load_asset_release(
            release_root / "lock.json", evidence_root=release_root, related_roots={"other": related}
        )


def test_rm40_ac31_default_missing_and_mismatched_custodian_review(release_root):
    catalog = read(release_root, "catalog")
    catalog["purpose"] = "development"
    write(release_root, "catalog", catalog)
    lock = read(release_root, "lock")
    lock.update(purpose="development", state="frozen", frozen_at="2026-10-30T00:00:00Z")
    write(release_root, "lock", lock)
    relock(release_root)
    release = load(release_root)
    assert "external_review_missing" in api().assess_asset_release(release).reasons
    review = api().CustodianReview("0" * 64, "reviewer", ("access_control",), {})
    assessment = api().assess_asset_release(release, custodian_review=review)
    assert assessment.status == "unverified"
    assert "external_review_lock_mismatch" in assessment.reasons
    assert "review_domain_incomplete" in assessment.reasons
    assert "rights_unqualified" in assessment.reasons
    assert "asset_cohort_incomplete" in assessment.reasons
    assert assessment.missing_review_domains["original"] == ["source_provenance"]


def test_rm40_ac31_trusted_review_cannot_upgrade_fixture(release_root):
    release = load(release_root)
    review = api().CustodianReview(release.lock["digest"], "reviewer", ("access_control",), {})
    assert api().assess_asset_release(release, custodian_review=review).status == "fixture"


def test_rm40_ac31_restricted_resolver_cannot_escape_explicit_roots(release_root, tmp_path):
    catalog = read(release_root, "catalog")
    catalog["evidence"][0].update(access="restricted", path=None)
    write(release_root, "catalog", catalog)
    relock(release_root)
    outside = tmp_path / "restricted.txt"
    outside.write_bytes((release_root / "originals/access_control.txt").read_bytes())

    class Resolver:
        def resolve(self, evidence_id):
            return outside

    with pytest.raises(ValueError, match="resolver_outside_roots"):
        load(release_root, evidence_resolver=Resolver())


def test_rm40_ac31_repeated_source_locator_cannot_hide_behind_renamed_package(release_root):
    catalog = read(release_root, "catalog")
    other = copy.deepcopy(catalog["assets"][0])
    other["record"].update(asset_id="renamed", content_hash="1" * 64)
    other["record"]["identity"]["package_id"] = "renamed"
    other["original_evidence_id"] = "renamed-original"
    payload = b"SYNTHETIC different content\n"
    path = "originals/different.md"
    (release_root / path).write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    other["record"]["content_hash"] = digest
    catalog["assets"].append(other)
    catalog["evidence"].append(
        dict(
            id="renamed-original",
            kind="original",
            access="redistributable",
            path=path,
            sha256=digest,
            bytes=len(payload),
        )
    )
    catalog["evidence"].sort(key=lambda e: e["id"])
    lock = read(release_root, "lock")
    lock["evidence"] = [{k: e[k] for k in ("id", "sha256", "bytes")} for e in catalog["evidence"]]
    write(release_root, "catalog", catalog)
    write(release_root, "lock", lock)
    relock(release_root)
    with pytest.raises(ValueError, match="duplicate_source_identity"):
        load(release_root)


def test_rm40_ac31_same_original_path_cannot_have_conflicting_unused_evidence(release_root):
    catalog = read(release_root, "catalog")
    evidence = copy.deepcopy(next(e for e in catalog["evidence"] if e["id"] == "original"))
    evidence.update(id="unused-original", sha256="1" * 64)
    catalog["evidence"].append(evidence)
    catalog["evidence"].sort(key=lambda e: e["id"])
    lock = read(release_root, "lock")
    lock["evidence"] = [{k: e[k] for k in ("id", "sha256", "bytes")} for e in catalog["evidence"]]
    write(release_root, "catalog", catalog)
    write(release_root, "lock", lock)
    relock(release_root)
    with pytest.raises(ValueError):
        load(release_root)


def test_rm40_ac31_export_never_accepts_embedded_url_credentials(release_root):
    catalog = read(release_root, "catalog")
    catalog["assets"][0]["source_url"] = "https://username:password@example.invalid/asset"
    write(release_root, "catalog", catalog)
    relock(release_root)
    with pytest.raises(ValueError, match="source_credentials"):
        load(release_root)


def test_rm40_ac31_custodian_review_cannot_mutate_coverage():
    review = api().CustodianReview(
        "0" * 64, "reviewer", ("access_control",), {"original": frozenset({"source_provenance"})}
    )
    with pytest.raises(TypeError):
        review.reviewed_evidence["original"] = []
    assert review.reviewed_evidence["original"] == frozenset({"source_provenance"})


def test_rm40_ac31_label_family_exposure_does_not_include_unlabeled_negatives(release_root):
    cases = read(release_root, "cases")
    cases["cases"][0]["target_asset_family_ids"] = ["calendar-family"]
    labels = read(release_root, "labels")
    for row in labels["judgments"] + labels["adjudications"]:
        row.update(**{"class": "negative"}, acceptable_asset_ids=[])
    write(release_root, "cases", cases)
    write(release_root, "labels", labels)
    relock(release_root)
    # An intended family on a genuine no-match is not a labeled catalog
    # target; a common public catalog alone must not create leakage.
    first = load(release_root)
    held = copy.deepcopy(cases)
    held["cases"][0].update(
        id="held",
        split="held_out",
        task_family_ids=["other"],
        task_text="Another genuinely unavailable request",
    )
    held_labels = copy.deepcopy(labels)
    held_labels["adjudications"][0]["case_id"] = "held"
    splits = importlib.import_module("relaymind.evaluation.asset_splits")
    diagnostic = splits.validate_splits(
        [
            (first.catalog, first.cases, first.labels),
            (first.catalog, held, held_labels),
        ]
    )
    assert diagnostic["split_counts"] == {"development": 1, "held_out": 1, "diagnostic": 0}


@pytest.mark.parametrize("field", ["provenance_evidence_ids", "language_review_evidence_ids"])
def test_rm40_ac31_dangling_support_is_always_rejected(release_root, field):
    catalog = read(release_root, "catalog")
    catalog["assets"][0][field] = ["unknown-evidence"]
    write(release_root, "catalog", catalog)
    relock(release_root)
    with pytest.raises(ValueError, match="unknown_reference"):
        load(release_root)


def test_rm40_ac31_file_growth_during_read_is_rejected(release_root, monkeypatch):
    from relaymind.assets import source_io

    if os.name != "posix":
        pytest.skip("native POSIX reader fault injection")
    original_read = source_io._PosixFile.read
    changed = False

    def growing_read(file, count):
        nonlocal changed
        payload = original_read(file, count)
        if not changed:
            changed = True
            with (release_root / "lock.json").open("ab") as writer:
                writer.write(b" ")
        return payload

    monkeypatch.setattr(source_io._PosixFile, "read", growing_read)
    with pytest.raises(ValueError, match="file_growth|file_changed"):
        load(release_root)


@pytest.mark.parametrize(
    "name,change",
    [
        ("catalog", lambda d: d.update(dataset_id="汉" * 100)),
        ("cases", lambda d: d["cases"][0].update(task_text="汉" * 3000)),
        (
            "cases",
            lambda d: d["cases"][0].update(conversation=[{"role": "user", "text": "a" * 32769}]),
        ),
        ("catalog", lambda d: d["assets"][0]["record"].update(schema_version=True)),
        ("labels", lambda d: d["judgments"][0].update(recorded_at="2026-02-30T00:00:00Z")),
        ("catalog", lambda d: d["people"][0].update(competencies=["imaginary_competency"])),
    ],
)
def test_rm40_ac31_utf8_types_and_time_boundaries(release_root, name, change):
    value = read(release_root, name)
    change(value)
    write(release_root, name, value)
    relock(release_root)
    with pytest.raises(ValueError):
        load(release_root)


def test_rm40_ac31_oversize_lock_is_rejected_before_parsing(release_root):
    with (release_root / "lock.json").open("wb") as file:
        file.truncate(4 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="size_limit"):
        load(release_root)


def test_rm40_ac31_restricted_bytes_can_be_resolved_only_as_offline_io(release_root):
    catalog = read(release_root, "catalog")
    catalog["evidence"][0].update(access="restricted", path=None)
    write(release_root, "catalog", catalog)
    relock(release_root)

    class Resolver:
        def resolve(self, evidence_id):
            assert evidence_id == "access_control"
            return release_root / "originals/access_control.txt"

    release = load(release_root, evidence_resolver=Resolver())
    assert all(item.status == "verified" for item in release.evidence_findings)
    assert api().assess_asset_release(release).status == "fixture"


def test_rm40_ac31_self_linked_lock_is_rejected(release_root):
    lock = read(release_root, "lock")
    lock["related_locks"] = [{"id": "self", "path": "lock.json", "digest": lock["digest"]}]
    write(release_root, "lock", lock)
    relock(release_root)
    # A cryptographically consistent self-reference would require a SHA-256
    # fixed point; an actual export fails its edge digest before recursive use.
    with pytest.raises(ValueError, match="lock_digest|lock_cycle"):
        api().load_asset_release(
            release_root / "lock.json",
            evidence_root=release_root,
            related_roots={"self": release_root},
        )


def test_rm40_ac31_reused_source_case_evidence_cannot_cross_splits(release_root):
    catalog, cases, labels = (read(release_root, name) for name in ("catalog", "cases", "labels"))
    cases["cases"][0]["source_case_evidence_id"] = "authorship"
    second = copy.deepcopy(cases)
    second["cases"][0].update(
        id="held",
        split="held_out",
        task_text="Independent wording",
        task_family_ids=["other-task"],
        target_asset_family_ids=[],
    )
    other_labels = copy.deepcopy(labels)
    other_labels["adjudications"][0].update(
        case_id="held", **{"class": "negative"}, acceptable_asset_ids=[]
    )
    splits = importlib.import_module("relaymind.evaluation.asset_splits")
    with pytest.raises(ValueError, match="split_leakage"):
        splits.validate_splits([(catalog, cases, labels), (catalog, second, other_labels)])


def test_rm40_ac31_tuner_is_not_an_independent_heldout_custodian(release_root):
    catalog = read(release_root, "catalog")
    catalog["purpose"] = "development"
    catalog["people"].append(
        dict(
            id="custodian-only",
            roles=["custodian", "tuner"],
            competencies=[],
            evidence_ids=["access_control"],
        )
    )
    catalog["people"].sort(key=lambda p: p["id"])
    write(release_root, "catalog", catalog)
    lock = read(release_root, "lock")
    lock["purpose"] = "development"
    write(release_root, "lock", lock)
    relock(release_root)
    release = load(release_root)
    review = api().CustodianReview(
        release.lock["digest"], "custodian-only", ("access_control",), {}
    )
    assessment = api().assess_asset_release(release, custodian_review=review)
    assert "custodian_unqualified" in assessment.reasons


def test_rm40_ac31_conflicting_same_dataset_release_is_not_a_new_release(release_root, tmp_path):
    related = tmp_path / "conflicting"
    shutil.copytree(release_root, related)
    catalog = read(related, "catalog")
    catalog["assets"][0]["record"]["description"] = "A changed searchable projection"
    write(related, "catalog", catalog)
    relock(related)
    lock = read(release_root, "lock")
    lock["related_locks"] = [
        {"id": "conflict", "path": "lock.json", "digest": read(related, "lock")["digest"]}
    ]
    write(release_root, "lock", lock)
    relock(release_root)
    with pytest.raises(ValueError, match="conflicting_release"):
        api().load_asset_release(
            release_root / "lock.json",
            evidence_root=release_root,
            related_roots={"conflict": related},
        )


def test_rm40_ac31_related_claims_need_scoped_external_review(release_root, tmp_path):
    catalog = read(release_root, "catalog")
    catalog["purpose"] = "development"
    write(release_root, "catalog", catalog)
    lock = read(release_root, "lock")
    lock["purpose"] = "development"
    write(release_root, "lock", lock)
    relock(release_root)
    related = tmp_path / "related"
    shutil.copytree(release_root, related)
    for name in ("catalog", "cases", "labels", "lock"):
        value = read(related, name)
        value["release"] = 2
        if name == "catalog":
            next(e for e in value["evidence"] if e["id"] == "permission")["id"] = (
                "related-permission"
            )
            value["evidence"].sort(key=lambda e: e["id"])
            value["assets"][0]["rights"]["evidence_ids"] = ["license", "related-permission"]
        if name == "lock":
            next(e for e in value["evidence"] if e["id"] == "permission")["id"] = (
                "related-permission"
            )
            value["evidence"].sort(key=lambda e: e["id"])
        write(related, name, value)
    relock(related)
    lock = read(release_root, "lock")
    lock["related_locks"] = [
        {"id": "related", "path": "lock.json", "digest": read(related, "lock")["digest"]}
    ]
    write(release_root, "lock", lock)
    relock(release_root)
    release = api().load_asset_release(
        release_root / "lock.json", evidence_root=release_root, related_roots={"related": related}
    )
    review = api().CustodianReview(release.lock["digest"], "reviewer", ("access_control",), {})
    result = api().assess_asset_release(release, custodian_review=review)
    assert result.missing_review_domains["related-permission"] == ["rights"]


def test_rm40_ac31_long_transitive_lock_chain_uses_bounded_iteration(release_root, monkeypatch):
    # Fault-inject parsed local documents at the I/O boundary to isolate graph
    # traversal. Every canonical document and lock hash is still checked, and
    # original evidence is still read through the real no-follow native reader.
    module = api()
    source = [read(release_root, name) for name in ("catalog", "cases", "labels", "lock")]
    documents, roots, next_digest = {}, {}, None
    for index in reversed(range(1001)):
        node = [copy.deepcopy(value) for value in source]
        for value in node:
            value["release"] = index + 1
        node[2]["cases_digest"] = canonical_digest(node[1])
        for name, value in zip(("catalog", "cases", "labels"), node[:3], strict=True):
            node[3][name]["digest"] = canonical_digest(value)
        node[3]["related_locks"] = (
            []
            if next_digest is None
            else [{"id": f"linked-{index + 1}", "path": "lock.json", "digest": next_digest}]
        )
        node[3].pop("digest")
        node[3]["digest"] = canonical_digest(node[3])
        next_digest = node[3]["digest"]
        documents[f"release-{index}"] = node
        roots[f"linked-{index}"] = release_root / f"release-{index}"

    def supplied_document(root, relative, kind):
        return documents[root.name][{"catalog_set": 0, "cases": 1, "labels": 2, "lock": 3}[kind]]

    monkeypatch.setattr(module, "document", supplied_document)
    release = module.load_asset_release(
        release_root / "release-0/lock.json", evidence_root=release_root, related_roots=roots
    )
    assert len(release.related_releases) == 1000
    assert release.split_diagnostics["split_counts"]["development"] == 1001


def test_rm40_ac31_acceptance_counts_require_heldout_both_kind_discovery(release_root):
    catalog = read(release_root, "catalog")
    catalog["purpose"] = "acceptance"
    write(release_root, "catalog", catalog)
    lock = read(release_root, "lock")
    lock["purpose"] = "acceptance"
    write(release_root, "lock", lock)
    relock(release_root)
    result = api().assess_asset_release(load(release_root))
    assert result.counts["lanes"]["en_to_en"]["positive"] == 0
    assert "case_cohort_incomplete" in result.reasons
    cases = read(release_root, "cases")
    cases["cases"][0].update(split="held_out", requested_kinds=["skill"])
    write(release_root, "cases", cases)
    relock(release_root)
    result = api().assess_asset_release(load(release_root))
    assert "discovery_kinds_incomplete" in result.reasons


def test_rm40_ac31_missing_named_restricted_file_is_unavailable_not_verified(release_root):
    catalog = read(release_root, "catalog")
    catalog["purpose"] = "development"
    catalog["evidence"][0]["access"] = "restricted"
    (release_root / catalog["evidence"][0]["path"]).unlink()
    write(release_root, "catalog", catalog)
    lock = read(release_root, "lock")
    lock["purpose"] = "development"
    write(release_root, "lock", lock)
    relock(release_root)
    release = load(release_root)
    result = api().assess_asset_release(release)
    assert result.status == "unverified"
    assert result.unavailable_evidence_ids == ("access_control",)
    assert "evidence_unavailable" in result.reasons


def test_rm40_ac31_tool_schema_hard_subset_uses_property_names_and_descriptions(release_root):
    catalog = read(release_root, "catalog")
    record = catalog["assets"][0]["record"]
    schema = {"type": "object", "properties": {"calendar": {"type": "string"}}}
    record.update(
        kind="mcp_tool",
        description="查询事件",
        identity={
            "server_id": "fixture-server",
            "tool_name": "extract_events",
            "exposed_name": "extract_events",
            "input_schema": schema,
            "schema_hash": canonical_digest(schema),
        },
    )
    cases = read(release_root, "cases")
    cases["cases"][0].update(lane="en_to_zh_hans", task_text="calendar")
    labels = read(release_root, "labels")
    for group in labels["agreement"]["groups"]:
        if group["lane"] == "en_to_en":
            group["lane"] = "en_to_zh_hans"
        elif group["lane"] == "en_to_zh_hans":
            group["lane"] = "en_to_en"
    labels["agreement"]["groups"].sort(key=lambda group: group["lane"])
    write(release_root, "catalog", catalog)
    write(release_root, "cases", cases)
    write(release_root, "labels", labels)
    relock(release_root)
    result = load(release_root).hard_subsets["case-a"]
    # Authoritative 2a55654 replaces the earlier string-values-only ruling.
    assert result["subset"] == "shared_latin"
    assert result["token_intersections"]["calendar"] == ["calendar"]
    cases["cases"][0]["task_text"] = "object string"
    write(release_root, "cases", cases)
    relock(release_root)
    assert load(release_root).hard_subsets["case-a"]["subset"] == "no_shared_latin"
    cases["cases"][0]["task_text"] = "calendar"
    write(release_root, "cases", cases)
    schema["properties"]["calendar"]["description"] = "calendar"
    record["identity"]["schema_hash"] = canonical_digest(schema)
    write(release_root, "catalog", catalog)
    relock(release_root)
    result = load(release_root).hard_subsets["case-a"]
    assert result["subset"] == "shared_latin"
    assert result["token_intersections"]["calendar"] == ["calendar"]


def revise_fixture_release(root, release):
    for name in ("catalog", "cases", "labels", "lock"):
        document = read(root, name)
        document["release"] = release
        write(root, name, document)
    relock(root)


def drop_declared_evidence(root, evidence_id):
    for name in ("catalog", "lock"):
        document = read(root, name)
        document["evidence"] = [row for row in document["evidence"] if row["id"] != evidence_id]
        write(root, name, document)
    relock(root)


def link_fixture_releases(root, related):
    lock = read(root, "lock")
    lock["related_locks"] = [
        {"id": name, "path": "lock.json", "digest": read(path, "lock")["digest"]}
        for name, path in sorted(related.items())
    ]
    write(root, "lock", lock)
    relock(root)


@pytest.mark.parametrize("supplier", ["ancestor", "sibling"])
def test_rm40_ac31_each_release_must_commit_its_own_evidence_closure(
    release_root, tmp_path, supplier
):
    child = tmp_path / "uncommitted-child"
    stage_fixture(child)
    drop_declared_evidence(child, "original")
    with pytest.raises(ValueError, match="unknown_reference"):
        load(child)
    revise_fixture_release(release_root, 3)
    related = {"child": child}
    if supplier == "sibling":
        sibling = tmp_path / "sibling"
        stage_fixture(sibling)
        revise_fixture_release(sibling, 2)
        # The root's own original is legitimately supplied by its descendant;
        # the unlinked child cannot acquire that sibling's commitment.
        drop_declared_evidence(release_root, "original")
        related["sibling"] = sibling
    link_fixture_releases(release_root, related)
    with pytest.raises(ValueError, match="unknown_reference"):
        api().load_asset_release(
            release_root / "lock.json", evidence_root=release_root, related_roots=related
        )


def test_rm40_ac31_evidence_committed_by_own_transitive_descendants_is_valid(
    release_root, tmp_path
):
    leaf, middle = tmp_path / "leaf", tmp_path / "middle"
    stage_fixture(leaf)
    stage_fixture(middle)
    revise_fixture_release(middle, 2)
    revise_fixture_release(release_root, 3)
    drop_declared_evidence(middle, "original")
    drop_declared_evidence(release_root, "original")
    link_fixture_releases(middle, {"leaf": leaf})
    link_fixture_releases(release_root, {"middle": middle})
    release = api().load_asset_release(
        release_root / "lock.json",
        evidence_root=release_root,
        related_roots={"leaf": leaf, "middle": middle},
    )
    assert len(release.related_releases) == 2
    assert any(
        row.evidence_id == "original" and row.status == "verified"
        for row in release.evidence_findings
    )


def write_unanimous_fixture_cases(root, rows):
    """Synthetic fixture rows only; all raw pairs agree before adjudication."""
    cases = read(root, "cases")
    cases["cases"] = sorted(rows, key=lambda row: row["id"])
    labels = read(root, "labels")
    raw, final = copy.deepcopy(labels["judgments"][:2]), copy.deepcopy(labels["adjudications"][0])
    labels["judgments"] = [
        dict(copy.deepcopy(judgment), case_id=case["id"])
        for case in cases["cases"]
        for judgment in raw
    ]
    labels["adjudications"] = [
        dict(copy.deepcopy(final), case_id=case["id"]) for case in cases["cases"]
    ]
    for group in labels["agreement"]["groups"]:
        count = sum(group["lane"] in ("all", case["lane"]) for case in cases["cases"])
        for key in group:
            if key != "lane":
                group[key] = count
    write(root, "cases", cases)
    write(root, "labels", labels)
    relock(root)


@pytest.mark.parametrize("lane", ["en_to_en", "zh_hans_to_en"])
def test_rm40_ac31_repeated_inputs_cannot_inflate_independent_minima(release_root, lane):
    first = read(release_root, "cases")["cases"][0]
    first.update(
        split="held_out",
        lane=lane,
        task_text="显示日程" if lane == "zh_hans_to_en" else "Show my calendar",
        conversation=[{"role": "user", "text": "This week"}],
    )
    repeated = copy.deepcopy(first)
    repeated.update(
        id="case-b",
        task_text="  " + first["task_text"].upper() + "  ",
        conversation=[{"role": "user", "text": "  THIS   WEEK  "}],
    )
    write_unanimous_fixture_cases(release_root, [first, repeated])
    for name in ("catalog", "lock"):
        value = read(release_root, name)
        value["purpose"] = "acceptance"
        write(release_root, name, value)
    relock(release_root)
    release = load(release_root)
    result = api().assess_asset_release(release)
    if lane == "zh_hans_to_en":
        assert result.counts["no_shared_latin"][lane] == 1
    assert result.counts["lanes"][lane]["positive"] == 1
    assert result.counts["repeated_input_case_ids"] == ["case-b"]
    assert "repeated_independent_input" in result.reasons
    assert result.status == "unverified"
    assert len(release.labels["judgments"]) == 4
    assert release.agreement["all"]["paired_cases"] == 2


@pytest.mark.parametrize("context_change", ["text", "role"])
def test_rm40_ac31_different_contexts_are_distinct_independent_inputs(release_root, context_change):
    first = read(release_root, "cases")["cases"][0]
    first.update(
        split="held_out",
        lane="zh_hans_to_en",
        task_text="显示日程",
        conversation=[{"role": "user", "text": "This week"}],
    )
    second = copy.deepcopy(first)
    second["id"] = "case-b"
    second["conversation"][0][context_change] = (
        "Next month" if context_change == "text" else "system"
    )
    write_unanimous_fixture_cases(release_root, [first, second])
    for name in ("catalog", "lock"):
        value = read(release_root, name)
        value["purpose"] = "acceptance"
        write(release_root, name, value)
    relock(release_root)
    result = api().assess_asset_release(load(release_root))
    assert result.counts["lanes"]["zh_hans_to_en"]["positive"] == 2
    assert result.counts["no_shared_latin"]["zh_hans_to_en"] == 2
    assert result.counts["repeated_input_case_ids"] == []
    assert "repeated_independent_input" not in result.reasons


def fixture_derivation_cases(root, edges):
    template = read(root, "cases")["cases"][0]
    rows = [
        dict(
            copy.deepcopy(template),
            id=case_id,
            derived_from_case_id=parent,
            task_text=f"Synthetic lineage request {case_id}",
        )
        for case_id, parent in edges.items()
    ]
    write_unanimous_fixture_cases(root, rows)


def test_rm40_ac31_later_release_cannot_erase_a_committed_derivation_cycle(release_root, tmp_path):
    child = tmp_path / "cyclic-child"
    stage_fixture(child)
    fixture_derivation_cases(child, {"a": "b", "b": "a"})
    with pytest.raises(ValueError, match="derivation_cycle"):
        load(child)
    revise_fixture_release(release_root, 2)
    fixture_derivation_cases(release_root, {"a": "c", "b": "a", "c": None})
    link_fixture_releases(release_root, {"child": child})
    with pytest.raises(ValueError, match="derivation_cycle"):
        api().load_asset_release(
            release_root / "lock.json", evidence_root=release_root, related_roots={"child": child}
        )


@pytest.mark.parametrize("latest_parent", ["b", "c"])
def test_rm40_ac31_valid_repeated_derivation_links_remain_valid(
    release_root, tmp_path, latest_parent
):
    child = tmp_path / "valid-child"
    stage_fixture(child)
    fixture_derivation_cases(child, {"a": "b", "b": None})
    revise_fixture_release(release_root, 2)
    fixture_derivation_cases(release_root, {"a": latest_parent, "b": None, "c": None})
    link_fixture_releases(release_root, {"child": child})
    release = api().load_asset_release(
        release_root / "lock.json", evidence_root=release_root, related_roots={"child": child}
    )
    assert len(release.related_releases) == 1
    assert release.split_diagnostics["split_counts"]["development"] == 5


def test_rm40_ac31_qualification_evidence_kind_is_structural_not_authentication(release_root):
    catalog = read(release_root, "catalog")
    item = copy.deepcopy(next(e for e in catalog["evidence"] if e["id"] == "access_control"))
    item.update(id="qualification", kind="qualification")
    catalog["evidence"].append(item)
    catalog["evidence"].sort(key=lambda e: e["id"])
    write(release_root, "catalog", catalog)
    lock = read(release_root, "lock")
    lock["evidence"] = [
        {key: e[key] for key in ("id", "sha256", "bytes")} for e in catalog["evidence"]
    ]
    lock["qualification_evidence_ids"] = ["qualification"]
    write(release_root, "lock", lock)
    relock(release_root)
    release = load(release_root)
    assert api().assess_asset_release(release).status == "fixture"
    assert (
        next(e for e in release.catalog["evidence"] if e["id"] == "qualification")["kind"]
        == "qualification"
    )


def test_rm40_ac31_stored_jaccard_scalar_is_hashed_without_recomputed_replacement(release_root):
    labels = read(release_root, "labels")
    stored = 0.9999999999998
    labels["agreement"]["groups"][0]["jaccard_sum"] = stored
    write(release_root, "labels", labels)
    relock(release_root)
    release = load(release_root)
    assert release.labels["agreement"]["groups"][0]["jaccard_sum"] == stored
    assert release.agreement["all"]["jaccard_sum"] == 1.0
    assert release.lock["labels"]["digest"] == canonical_digest(labels)
    labels["agreement"]["groups"][0]["jaccard_sum"] = 1.0
    write(release_root, "labels", labels)
    with pytest.raises(ValueError, match="document_digest"):
        load(release_root)


@pytest.mark.parametrize(
    "query,shared",
    [
        ("calendar", True),
        ("synopsis", True),
        ("object", False),
        ("string", False),
        ("properties", False),
        ("enum_value", False),
    ],
)
def test_rm40_ac31_schema_search_text_excludes_keywords_and_non_description_values(
    release_root, query, shared
):
    module = importlib.import_module("relaymind.evaluation.asset_splits")
    record = read(release_root, "catalog")["assets"][0]["record"]
    schema = {
        "type": "object",
        "description": "synopsis",
        "properties": {"calendar": {"type": "string", "enum": ["enum_value"]}},
    }
    record.update(
        kind="mcp_tool",
        description="查询事件",
        identity={
            "server_id": "fixture-server",
            "tool_name": "extract_events",
            "exposed_name": "extract_events",
            "input_schema": schema,
            "schema_hash": canonical_digest(schema),
        },
    )
    case = dict(
        read(release_root, "cases")["cases"][0],
        task_text=query,
        conversation=[{"role": "user", "text": "中文"}],
    )
    result = module.classify_latin_overlap(
        case, [record], unicode_version=unicodedata.unidata_version
    )
    assert result.all_targets_disjoint is (not shared)


def test_rm40_ac31_related_lock_path_selects_nested_file_under_explicit_root(
    release_root, tmp_path
):
    allowed = tmp_path / "allowed"
    child = allowed / "nested" / "release"
    stage_fixture(child)
    (child / "lock.json").rename(child / "custom.lock.json")
    # Document refs stay relative to the selected lock, inside the allowed root.
    revise_fixture_release(release_root, 2)
    child_lock = json.loads((child / "custom.lock.json").read_text(encoding="utf-8"))
    lock = read(release_root, "lock")
    lock["related_locks"] = [
        {
            "id": "custodian-alias",
            "path": "nested/release/custom.lock.json",
            "digest": child_lock["digest"],
        }
    ]
    write(release_root, "lock", lock)
    relock(release_root)
    release = api().load_asset_release(
        release_root / "lock.json",
        evidence_root=release_root,
        related_roots={"custodian-alias": allowed},
    )
    assert len(release.related_releases) == 1
    assert release.related_releases[0].lock["digest"] == child_lock["digest"]


def test_rm40_ac31_related_lock_without_declared_path_is_rejected(release_root, tmp_path):
    child = tmp_path / "child"
    stage_fixture(child)
    revise_fixture_release(release_root, 2)
    lock = read(release_root, "lock")
    lock["related_locks"] = [{"id": "child", "digest": read(child, "lock")["digest"]}]
    write(release_root, "lock", lock)
    relock(release_root)
    with pytest.raises(ValueError):
        api().load_asset_release(
            release_root / "lock.json", evidence_root=release_root, related_roots={"child": child}
        )


@pytest.mark.parametrize(
    "declared_path", ["missing.lock.json", "../child/lock.json", "escape/nested/lock.json"]
)
def test_rm40_ac31_related_path_is_honored_even_when_default_lock_hash_matches(
    release_root, tmp_path, declared_path
):
    allowed = tmp_path / "allowed"
    stage_fixture(allowed)
    outside = tmp_path / "outside" / "nested"
    stage_fixture(outside)
    (allowed / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    revise_fixture_release(release_root, 2)
    lock = read(release_root, "lock")
    lock["related_locks"] = [
        {"id": "a", "path": "lock.json", "digest": read(allowed, "lock")["digest"]},
        {"id": "b", "path": declared_path, "digest": read(allowed, "lock")["digest"]},
    ]
    write(release_root, "lock", lock)
    relock(release_root)
    with pytest.raises(ValueError):
        api().load_asset_release(
            release_root / "lock.json",
            evidence_root=release_root,
            related_roots={"a": allowed, "b": allowed},
        )


def test_rm40_ac31_related_paths_can_select_two_explicit_custodian_roots(release_root, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    stage_fixture(one)
    stage_fixture(two)
    revise_fixture_release(two, 2)
    revise_fixture_release(release_root, 3)
    (one / "lock.json").rename(one / "a.lock.json")
    (two / "lock.json").rename(two / "b.lock.json")
    lock = read(release_root, "lock")
    lock["related_locks"] = [
        {
            "id": "first-alias",
            "path": "a.lock.json",
            "digest": json.loads((one / "a.lock.json").read_text(encoding="utf-8"))["digest"],
        },
        {
            "id": "second-alias",
            "path": "b.lock.json",
            "digest": json.loads((two / "b.lock.json").read_text(encoding="utf-8"))["digest"],
        },
    ]
    write(release_root, "lock", lock)
    relock(release_root)
    release = api().load_asset_release(
        release_root / "lock.json",
        evidence_root=release_root,
        related_roots={"first-alias": one, "second-alias": two},
    )
    assert len(release.related_releases) == 2


@pytest.mark.parametrize(
    "schema,query,shared",
    [
        *[
            (
                {keyword: value},
                "instance_description instance_property",
                False,
            )
            for keyword, value in [
                (
                    "enum",
                    [
                        {
                            "description": "instance_description",
                            "properties": {"instance_property": 1},
                        }
                    ],
                ),
                (
                    "const",
                    {"description": "instance_description", "properties": {"instance_property": 1}},
                ),
                (
                    "default",
                    {"description": "instance_description", "properties": {"instance_property": 1}},
                ),
                (
                    "examples",
                    [
                        {
                            "description": "instance_description",
                            "properties": {"instance_property": 1},
                        }
                    ],
                ),
            ]
        ],
        ({"properties": {"properties": {"type": "string"}}}, "type", False),
        (
            {"unknown_annotation": {"description": "instance_description"}},
            "instance_description",
            False,
        ),
        (
            {"properties": {"outer": {"properties": {"nested_property": {"type": "string"}}}}},
            "nested_property",
            True,
        ),
        (
            {"properties": {"properties": {"description": "nested_description"}}},
            "nested_description",
            True,
        ),
        *[
            ({keyword: [{"description": "schema_description"}]}, "schema_description", True)
            for keyword in ("allOf", "anyOf", "oneOf", "prefixItems")
        ],
        *[
            ({keyword: {"description": "schema_description"}}, "schema_description", True)
            for keyword in (
                "items",
                "contains",
                "additionalProperties",
                "unevaluatedProperties",
                "unevaluatedItems",
                "propertyNames",
                "not",
                "if",
                "then",
                "else",
            )
        ],
        *[
            (
                {keyword: {"child": {"description": "schema_description"}}},
                "schema_description",
                True,
            )
            for keyword in ("$defs", "definitions", "patternProperties", "dependentSchemas")
        ],
    ],
)
def test_rm40_ac31_loader_schema_text_respects_subschema_positions(
    release_root, schema, query, shared
):
    """Instance-data objects must not change the locked hard-positive membership."""
    catalog = read(release_root, "catalog")
    catalog["assets"][0]["record"].update(
        kind="mcp_tool",
        description="查询事件",
        identity={
            "server_id": "服务",
            "tool_name": "查询",
            "exposed_name": "查询",
            "input_schema": schema,
            "schema_hash": canonical_digest(schema),
        },
    )
    cases = read(release_root, "cases")
    cases["cases"][0].update(lane="en_to_zh_hans", task_text=query)
    labels = read(release_root, "labels")
    for group in labels["agreement"]["groups"]:
        if group["lane"] == "en_to_en":
            group["lane"] = "en_to_zh_hans"
        elif group["lane"] == "en_to_zh_hans":
            group["lane"] = "en_to_en"
    labels["agreement"]["groups"].sort(key=lambda group: group["lane"])
    write(release_root, "catalog", catalog)
    write(release_root, "cases", cases)
    write(release_root, "labels", labels)
    relock(release_root)

    result = load(release_root).hard_subsets["case-a"]
    assert result["subset"] == ("shared_latin" if shared else "no_shared_latin")
    assert result["hard_precision_included"] is (not shared)
    expected = query.split() if shared else []
    assert result["token_intersections"]["calendar"] == expected


def test_rm40_ac31_fixture_assessment_keeps_diagnostic_counts(release_root):
    assessment = api().assess_asset_release(load(release_root))
    assert assessment.status == "fixture"
    assert "fixture_purpose" in assessment.reasons
    for key in ("lanes", "no_shared_latin", "repeated_input_case_ids"):
        assert key in assessment.counts
