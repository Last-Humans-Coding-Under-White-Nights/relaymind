"""Spec 0.60 section 12.6.5 corpus closure for host tasks and episodes (#229, #205, #202).

RM40, RM42, RM44; AC31, AC33, AC36. The frozen corpus lock and its catalog,
cases, labels and evidence are resolved from explicit companion bytes and
verified by the existing release verifier; tasks and episodes are bound to it.
"""

import json

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_whole_task import WholeTaskBlocked, WholeTaskRejected
from relaymind.evaluation.asset_whole_task_corpus import (
    bind_episodes,
    bind_host_tasks,
    release_labels,
    resolve_corpus,
)
from whole_task_helpers import expanded_release


def companions(root):
    """Documents and evidence bytes keyed as an input manifest would hold them."""
    documents, evidence = {}, {}
    lock = json.loads((root / "lock.json").read_text(encoding="utf-8"))
    documents["lock:" + canonical_digest(lock)] = (root / "lock.json").read_bytes()
    for kind in ("catalog", "cases", "labels"):
        raw = (root / lock[kind]["path"]).read_bytes()
        documents[kind + ":" + canonical_digest(json.loads(raw))] = raw
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    for item in catalog["evidence"]:
        evidence[item["id"]] = dict(item, payload=(root / item["path"]).read_bytes())
    return canonical_digest(lock), documents, evidence


@pytest.fixture
def corpus(tmp_path):
    return companions(expanded_release(tmp_path / "release"))


def test_complete_companions_resolve_to_the_verified_release(corpus):
    digest, documents, evidence = corpus
    release = resolve_corpus(digest, documents, evidence)
    assert len(release.cases["cases"]) == 8
    labels = release_labels(release)
    assert labels["case-01"]["acceptable_asset_ids"] == ["calendar"]


@pytest.mark.parametrize("missing", ["lock:", "catalog:", "cases:", "labels:"])
def test_a_missing_corpus_document_blocks(corpus, missing):
    digest, documents, evidence = corpus
    documents = {k: v for k, v in documents.items() if not k.startswith(missing)}
    with pytest.raises(WholeTaskBlocked, match="corpus_unresolved"):
        resolve_corpus(digest, documents, evidence)


def test_changed_corpus_bytes_reject(corpus):
    digest, documents, evidence = corpus
    key = next(k for k in documents if k.startswith("cases:"))
    documents[key] = documents[key].replace(b"Show my calendar", b"Show my calendaR")
    with pytest.raises(WholeTaskRejected):
        resolve_corpus(digest, documents, evidence)


def test_changed_evidence_bytes_reject(corpus):
    digest, documents, evidence = corpus
    evidence["license"] = dict(evidence["license"], payload=b"changed")
    with pytest.raises(WholeTaskRejected):
        resolve_corpus(digest, documents, evidence)


def host_tasks(release, **change):
    record = release.catalog["assets"][0]["record"]
    task = dict(
        id="t1",
        lane="en_to_en",
        family_ids=["task-calendar"],
        prompt_case_id="case-01",
        setup_evidence_ids=[],
        success_rubric_evidence_id="r",
        permissions_digest="a" * 64,
        budgets_digest="a" * 64,
        asset_bindings=[
            dict(
                asset_id=record["asset_id"],
                content_hash=record["content_hash"],
                schema_hash=record["identity"].get("schema_hash"),
            )
        ],
        mutation=False,
    )
    task.update(change)
    return dict(tasks=[task])


def test_host_tasks_bind_to_cases_and_catalog(corpus):
    release = resolve_corpus(*corpus)
    bind_host_tasks(host_tasks(release), release)


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"prompt_case_id": "case-99"}, "case_binding"),
        ({"lane": "code_switch"}, "case_binding"),
        ({"family_ids": ["task-other"]}, "case_binding"),
        (
            {
                "asset_bindings": [
                    dict(asset_id="calendar", content_hash="0" * 64, schema_hash=None)
                ]
            },
            "asset_binding",
        ),
        (
            {"asset_bindings": [dict(asset_id="absent", content_hash="0" * 64, schema_hash=None)]},
            "asset_binding",
        ),
    ],
)
def test_host_task_contradictions_reject(corpus, change, reason):
    release = resolve_corpus(*corpus)
    with pytest.raises(WholeTaskRejected, match=reason):
        bind_host_tasks(host_tasks(release, **change), release)


def episodes(release, target=None):
    record = release.catalog["assets"][0]["record"]
    target = target or dict(asset_id=record["asset_id"], content_hash=record["content_hash"])
    return dict(
        episodes=[
            dict(
                id="e1",
                intent_family_ids=["task-calendar"],
                corrections=[
                    dict(
                        case_id=c,
                        target_asset_id=target["asset_id"],
                        content_hash=target["content_hash"],
                        schema_hash=None,
                    )
                    for c in ("case-03", "case-01", "case-02")
                ],
                paraphrase_case_ids=["case-04", "case-05"],
                control_case_ids=["case-06"],
                reset_probe_case_ids=["case-07"],
            )
        ]
    )


def test_episodes_bind_and_probe_labels_come_from_the_release(corpus):
    release = resolve_corpus(*corpus)
    bind_episodes(episodes(release), release)


def test_a_missing_pool_correction_target_is_retained_not_rejected(corpus):
    release = resolve_corpus(*corpus)
    bind_episodes(episodes(release, dict(asset_id="absent", content_hash="0" * 64)), release)


def test_an_unknown_episode_case_rejects(corpus):
    release = resolve_corpus(*corpus)
    doc = episodes(release)
    doc["episodes"][0]["control_case_ids"] = ["case-99"]
    with pytest.raises(WholeTaskRejected, match="case_binding"):
        bind_episodes(doc, release)


def test_a_present_correction_target_with_other_bytes_rejects(corpus):
    release = resolve_corpus(*corpus)
    doc = episodes(release, dict(asset_id="calendar", content_hash="0" * 64))
    with pytest.raises(WholeTaskRejected, match="asset_binding"):
        bind_episodes(doc, release)


# --- adaptation configuration semantics (#229, #202) -----------------------------


def ablation(change_off=None, change_on=None):
    from whole_task_helpers import effective_pair

    adaptation = {"fixture_adaptation_config": 1}
    off, on = effective_pair(canonical_digest(adaptation))
    if change_off:
        change_off(off)
    if change_on:
        change_on(on)
    freeze = dict(
        schema_version=1,
        off_config_digest=canonical_digest(off),
        on_config_digest=canonical_digest(on),
        adaptation_config_digest=canonical_digest(adaptation),
        seed=1,
    )
    configs = {canonical_digest(v): v for v in (off, on, adaptation)}
    return freeze, configs


def test_off_and_on_effective_configs_differ_only_in_adaptation():
    from relaymind.evaluation.asset_whole_task_corpus import verify_episode_configs

    verify_episode_configs(*ablation())


@pytest.mark.parametrize(
    "change_off, change_on",
    [
        (lambda c: c.update(adaptation=dict(enabled=True, config_digest="a" * 64)), None),
        (None, lambda c: c.update(adaptation=dict(enabled=True, config_digest="b" * 64))),
        (None, lambda c: c.update(adaptation=dict(enabled=False, config_digest=None))),
        (None, lambda c: c.update(permissions=["demo.read"])),
        (None, lambda c: c["recommendations"].update(total_ms=999)),
        (None, lambda c: c.update(selection_scope="runtime")),
        (
            None,
            lambda c: c["recommendations"].update(
                primary="http", name="other", version="2", url="http://127.0.0.1:8765/select"
            ),
        ),
        (None, lambda c: c["recommendations"].update(name="other-selector")),
    ],
)
def test_any_other_off_on_difference_rejects(change_off, change_on):
    from relaymind.evaluation.asset_whole_task_corpus import verify_episode_configs

    with pytest.raises(WholeTaskRejected, match="ablation_policy|episode_config"):
        verify_episode_configs(*ablation(change_off, change_on))


def test_a_settings_bag_is_not_an_effective_config():
    from relaymind.evaluation.asset_whole_task_corpus import verify_episode_configs

    freeze, configs = ablation()
    configs[freeze["off_config_digest"]] = {"enabled": True}
    with pytest.raises(WholeTaskRejected, match="episode_config"):
        verify_episode_configs(freeze, configs)


def test_unavailable_corpus_evidence_blocks(corpus):
    digest, documents, evidence = corpus
    from relaymind.evaluation import asset_whole_task_corpus
    from relaymind.evaluation.asset_corpus import EvidenceFinding

    release = resolve_corpus(digest, documents, evidence)
    unavailable = (EvidenceFinding("license", "0" * 64, 1, "unavailable"),)
    import dataclasses

    with pytest.raises(WholeTaskBlocked, match="corpus_evidence_unresolved"):
        asset_whole_task_corpus.require_available(
            dataclasses.replace(release, evidence_findings=unavailable)
        )


def test_effective_reference_closure_must_resolve():
    from relaymind.evaluation.asset_whole_task_corpus import verify_reference_closure
    from whole_task_helpers import ENVIRONMENT

    adaptation = {"fixture_adaptation_config": 1}
    from whole_task_helpers import effective_pair

    off, _ = effective_pair(canonical_digest(adaptation))
    verify_reference_closure(off, {}, {"documents": []}, ENVIRONMENT, adaptation_digest=None)
    missing = dict(
        off, references=[dict(role="embedding", document_id="referenced_config:" + "b" * 64)]
    )
    with pytest.raises(WholeTaskBlocked, match="referenced_config_unresolved"):
        verify_reference_closure(
            missing, {}, {"documents": []}, ENVIRONMENT, adaptation_digest=None
        )
