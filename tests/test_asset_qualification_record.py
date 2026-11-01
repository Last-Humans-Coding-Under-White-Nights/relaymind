"""Spec 0.66 section 12.6.6 owner-run qualification records (#251).

RM16, RM39, RM40, RM42, RM44; AC30, AC31, AC33, AC35, AC36, AC37. A reviewed
QualificationRecordV1 binds the frozen inputs and reports by SHA-256 and is
approved in a pull request by at least one organization member other than the
owner. Only an approved record whose digests all match can lift a report to
``qualified``; a negative record closes a host-operation gate with the
operation disabled and listed as a known host limitation. Every record here is
a synthetic fixture: nothing below is genuine evidence.
"""

import copy
import hashlib
import json

import pytest

from relaymind.evaluation.asset_qualification_record import (
    class_kappa,
    known_host_limitations,
    load_record,
)
from relaymind.evaluation.asset_whole_task import WholeTaskRejected
from relaymind.evaluation.asset_whole_task_qualification import (
    REVIEW_UNDEFINED,
    assess_qualification,
)
from whole_task_helpers import ENVIRONMENT

H = "a" * 64
COMMIT = "b" * 40
GIB8 = 8 * 1024**3


def _subject(root, role, name, payload: bytes) -> dict:
    (root / name).write_bytes(payload)
    return dict(
        role=role, path=name, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload)
    )


def host_record(root, **overrides) -> dict:
    subjects = [
        _subject(root, "freeze", "freeze.json", b'{"freeze":1}'),
        _subject(root, "report", "report.json", b'{"report":1}'),
        _subject(root, "tasks", "tasks.json", b'{"tasks":1}'),
    ]
    record = dict(
        schema_version=1,
        record_id="m10-host-acceptance-1",
        profile="owner_run",
        outcome="positive",
        scope="host_acceptance",
        labelling="owner_machine",
        annotators=dict(
            owner_annotator_id="owner",
            machine=dict(
                annotator_id="machine",
                model_id="example-model-2026-10-01",
                prompt_sha256=H,
                temperature=0,
                saw_tuning_results=False,
                saw_arm_outputs=False,
            ),
        ),
        agreement=dict(paired_cases=10, class_agreements=8, cohen_kappa=0.6),
        corpus_review=None,
        preregistration=dict(commit=COMMIT, committed_at="2026-11-02T00:00:00Z"),
        runs_started_at="2026-11-03T00:00:00Z",
        machine=None,
        revisions=dict(repository_commit=COMMIT, adapter_revision="adapter-1", harness_sha256=H),
        operation=None,
        subjects=subjects,
        review=dict(
            owner_login="owner-login",
            pull_request=300,
            reviewer_logins=["team-member"],
            approved_at="2026-11-05T00:00:00Z",
        ),
        recorded_at="2026-11-04T00:00:00Z",
    )
    record.update(overrides)
    return record


def macbook(**overrides) -> dict:
    machine = dict(
        role="target_macos_m1_8gib",
        os="Darwin",
        os_build="27A100",
        os_version="macOS 27.0",
        architecture="arm64",
        cpu_model="Apple M1",
        ram_bytes=GIB8,
        hostname_sha256=H,
    )
    machine.update(overrides)
    return machine


def platform_record(root, machine, **overrides) -> dict:
    return host_record(
        root,
        record_id="m10-platform-macbook",
        scope="platform",
        labelling="not_applicable",
        annotators=None,
        agreement=None,
        preregistration=None,
        runs_started_at=None,
        machine=machine,
        subjects=[_subject(root, "report", "platform.json", b'{"platform":1}')],
        **overrides,
    )


def negative_record(root, **overrides) -> dict:
    return host_record(
        root,
        record_id="m10-negative-mcp-use",
        outcome="negative",
        scope="host_operation",
        labelling="not_applicable",
        annotators=None,
        agreement=None,
        preregistration=None,
        runs_started_at=None,
        machine=dict(
            role="linux_host",
            os="Linux",
            os_build="Ubuntu 24.04.1 LTS 6.8.0-45-generic",
            os_version="Linux-6.8.0-45-generic-x86_64-with-glibc2.39",
            architecture="x86_64",
            cpu_model="AMD EPYC",
            ram_bytes=4 * 1024**3,
            hostname_sha256=H,
        ),
        operation=dict(
            name="mcp_use",
            host="openclaw",
            host_version="2026.10.7",
            finding="exact_route_absent",
            disabled=True,
        ),
        subjects=[_subject(root, "capture", "capture.json", b'{"capture":1}')],
        **overrides,
    )


def save(root, record, name="record.json"):
    path = root / name
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


# --- the record itself ---------------------------------------------------------


def test_an_approved_owner_run_record_verifies_every_bound_digest(tmp_path):
    record = load_record(save(tmp_path, host_record(tmp_path)))
    assert record.approved and record.labelling == "owner_machine"
    assert record.scope == "host_acceptance" and record.outcome == "positive"
    assert record.binds("report", hashlib.sha256(b'{"report":1}').hexdigest())


def test_a_changed_subject_byte_rejects(tmp_path):
    path = save(tmp_path, host_record(tmp_path))
    (tmp_path / "tasks.json").write_bytes(b'{"tasks":2}')
    with pytest.raises(WholeTaskRejected, match="record_digest_mismatch"):
        load_record(path)


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda r: r.update(extra=1), "invalid_qualification_record"),
        (lambda r: r.update(labelling="independent_humans"), "invalid_qualification_record"),
        (lambda r: r["annotators"]["machine"].update(temperature=0.2), "invalid_qualification"),
        (
            lambda r: r["annotators"]["machine"].update(saw_tuning_results=True),
            "invalid_qualification",
        ),
        (lambda r: r.update(annotators=None), "labelling_profile"),
        (lambda r: r["annotators"].update(owner_annotator_id="machine"), "labelling_profile"),
        (lambda r: r.update(preregistration=None), "preregistration_missing"),
        (lambda r: r.update(runs_started_at="2026-11-01T00:00:00Z"), "preregistration_late"),
        (lambda r: r["subjects"].pop(0), "record_subjects"),
        (lambda r: r["review"].update(reviewer_logins=["owner-login"]), "self_review"),
        (lambda r: r["review"].update(pull_request=None), "review_incomplete"),
        (lambda r: r.update(outcome="negative"), "record_outcome"),
    ],
)
def test_malformed_or_contradictory_records_reject(tmp_path, mutate, reason):
    record = host_record(tmp_path)
    mutate(record)
    with pytest.raises(WholeTaskRejected, match=reason):
        load_record(save(tmp_path, record))


def test_a_record_pending_review_loads_but_is_not_approved(tmp_path):
    """The PR number and reviewer login are recorded after the merge (12.6.6)."""
    pending = host_record(tmp_path)
    pending["review"].update(pull_request=None, reviewer_logins=[], approved_at=None)
    assert not load_record(save(tmp_path, pending)).approved


@pytest.mark.parametrize(
    ("machine", "ok"),
    [
        (macbook(), True),
        (macbook(ram_bytes=2 * GIB8), False),
        (macbook(cpu_model="Apple M2"), False),
        (macbook(architecture="x86_64"), False),
        (
            dict(
                macbook(),
                role="target_windows_x64",
                os="Windows",
                os_build="10.0.26100",
                architecture="AMD64",
                cpu_model="Intel Core i7",
                ram_bytes=2 * GIB8,
            ),
            True,
        ),
        (dict(macbook(), role="target_windows_x64", os="Windows", architecture="ARM64"), False),
        (dict(macbook(), role="linux_host", os="Darwin"), False),
    ],
)
def test_machine_identity_matches_its_declared_role(tmp_path, machine, ok):
    """Spec 12.6.6: M1 8 GiB macOS, named native x86-64 Windows, Linux host."""
    path = save(tmp_path, platform_record(tmp_path, machine))
    if ok:
        assert load_record(path).machine["role"] == machine["role"]
    else:
        with pytest.raises(WholeTaskRejected, match="machine_identity"):
            load_record(path)


# --- negative qualification of host limits (7.6.1/7.6.2) ------------------------


def test_a_negative_record_lists_a_disabled_known_host_limitation(tmp_path):
    record = load_record(save(tmp_path, negative_record(tmp_path)))
    assert known_host_limitations([record]) == [
        dict(
            operation="mcp_use",
            host="openclaw",
            host_version="2026.10.7",
            finding="exact_route_absent",
            record_id="m10-negative-mcp-use",
        )
    ]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda r: r.update(operation=None), "record_operation"),
        (lambda r: r["operation"].update(disabled=False), "record_outcome"),
        (lambda r: r["operation"].update(finding="stop_point_absent"), "record_outcome"),
        (lambda r: r["operation"].update(finding="qualified"), "record_outcome"),
        (lambda r: r.update(subjects=[]), "invalid_qualification_record"),
    ],
)
def test_contradictory_negative_records_reject(tmp_path, mutate, reason):
    record = negative_record(tmp_path)
    mutate(record)
    with pytest.raises(WholeTaskRejected, match=reason):
        load_record(save(tmp_path, record))


def test_an_unapproved_negative_record_is_not_a_known_limitation(tmp_path):
    record = negative_record(tmp_path)
    record["review"].update(pull_request=None, reviewer_logins=[], approved_at=None)
    assert known_host_limitations([load_record(save(tmp_path, record))]) == []


# --- the record lifts the assessment only when it binds the report --------------


def _assess(record=None, report_sha256=None, **overrides):
    values = dict(
        kind="host",
        claimed="qualified",
        corpus_purpose="acceptance",
        corpus_status="qualified",
        evidence=[{"provenance": "observed"}],
        artifacts=[{"provenance": "observed"}],
        unverified=(),
        platform=None,
        record=record,
        report_sha256=report_sha256,
    )
    values.update(overrides)
    return assess_qualification(**values)


REPORT_SHA = hashlib.sha256(b'{"report":1}').hexdigest()


def test_an_approved_bound_record_reaches_qualified_with_owner_machine_label(tmp_path):
    record = load_record(save(tmp_path, host_record(tmp_path)))
    assessment = _assess(record, REPORT_SHA)
    assert (assessment.assessed, assessment.reasons) == ("qualified", ())
    assert assessment.as_output()["labelling"] == "owner_machine"


def test_without_a_record_the_review_stays_undefined():
    assessment = _assess()
    assert (assessment.assessed, assessment.reasons) == ("provisional", (REVIEW_UNDEFINED,))
    assert assessment.as_output()["labelling"] is None


def test_a_pending_record_keeps_the_report_provisional(tmp_path):
    pending = host_record(tmp_path)
    pending["review"].update(pull_request=None, reviewer_logins=[], approved_at=None)
    assessment = _assess(load_record(save(tmp_path, pending)), REPORT_SHA)
    assert (assessment.assessed, assessment.reasons) == ("provisional", (REVIEW_UNDEFINED,))


@pytest.mark.parametrize(
    ("kind", "report_sha256", "reason"),
    [
        ("host", "c" * 64, "qualification_record_unbound"),
        ("adaptation", REPORT_SHA, "qualification_record_scope"),
    ],
)
def test_a_record_for_another_report_or_scope_rejects(tmp_path, kind, report_sha256, reason):
    record = load_record(save(tmp_path, host_record(tmp_path)))
    with pytest.raises(WholeTaskRejected, match=reason):
        _assess(record, report_sha256, kind=kind)


def test_a_negative_record_never_qualifies_a_report(tmp_path):
    record = load_record(save(tmp_path, negative_record(tmp_path)))
    with pytest.raises(WholeTaskRejected, match="qualification_record_scope"):
        _assess(record, REPORT_SHA)


def test_a_fixture_input_stays_fixture_even_with_a_record(tmp_path):
    record = load_record(save(tmp_path, host_record(tmp_path)))
    assert _assess(record, REPORT_SHA, corpus_purpose="fixture").assessed == "fixture"


def _platform(tmp_path, machine, environment):
    record = load_record(save(tmp_path, platform_record(tmp_path, machine)))
    sha = hashlib.sha256(b'{"platform":1}').hexdigest()
    return _assess(record, sha, kind="platform", claimed="measured", platform=environment)


MAC_ENVIRONMENT = dict(
    ENVIRONMENT,
    os="Darwin",
    os_version="macOS 27.0",
    architecture="arm64",
    cpu="Apple M1",
    ram_bytes=GIB8,
)


def test_a_target_machine_record_measures_its_platform_report(tmp_path):
    assert _platform(tmp_path, macbook(), MAC_ENVIRONMENT).assessed == "qualified"


@pytest.mark.parametrize(
    "change",
    [
        dict(ram_bytes=2 * GIB8),
        dict(cpu="Apple M2"),
        dict(os_version="macOS 26.1"),
        dict(architecture="x86_64"),
    ],
)
def test_a_platform_report_from_another_machine_rejects(tmp_path, change):
    """Spec 12.6.6: OS, version, architecture, CPU and RAM must all match."""
    with pytest.raises(WholeTaskRejected, match="record_machine_mismatch"):
        _platform(tmp_path, macbook(), dict(MAC_ENVIRONMENT, **change))


@pytest.mark.parametrize("field", ["cpu", "os_version", "ram_bytes"])
def test_an_unrecorded_platform_identity_field_stays_unverified(tmp_path, field):
    assessment = _platform(tmp_path, macbook(), dict(MAC_ENVIRONMENT, **{field: None}))
    assert assessment.assessed == "provisional"
    assert "machine_identity_unverified" in assessment.reasons


# --- Cohen's kappa over the owner/machine raw judgments --------------------------


def _labels(pairs, method="owner_machine.v1", adjudicator="owner"):
    judgments, adjudications = [], []
    for n, (owner, machine) in enumerate(pairs):
        case = f"case-{n:02d}"
        judgments.append(dict(case_id=case, annotator_id="machine", **{"class": machine}))
        judgments.append(dict(case_id=case, annotator_id="owner", **{"class": owner}))
        adjudications.append(dict(case_id=case, adjudicator_id=adjudicator))
    return dict(
        judgments=judgments,
        adjudications=adjudications,
        agreement=dict(method=method, groups=[]),
    )


PEOPLE = {
    "owner": dict(id="owner", roles=["adjudicator", "annotator", "custodian", "tuner"]),
    "machine": dict(id="machine", roles=["annotator"]),
}


def test_class_kappa_is_cohens_kappa_over_the_two_annotators():
    pairs = [("positive", "positive")] * 6 + [("negative", "negative")] * 2
    pairs += [("positive", "negative"), ("negative", "positive")]
    # p_o = 0.8; p_e = 0.7*0.7 + 0.3*0.3 = 0.58; kappa = 0.22/0.42.
    assert class_kappa(_labels(pairs)) == pytest.approx(0.22 / 0.42)
    # One class only: chance agreement is 1 and kappa is undefined (null).
    assert class_kappa(_labels([("positive", "positive")] * 3)) is None


def test_record_kappa_must_match_the_frozen_labels(tmp_path):
    record = load_record(save(tmp_path, host_record(tmp_path)))
    pairs = [("positive", "positive")] * 6 + [("negative", "negative")] * 2
    pairs += [("positive", "negative"), ("negative", "positive")]
    labels = _labels(pairs)
    labels["agreement"]["groups"] = [
        dict(lane="all", paired_cases=10, class_agreements=8),
    ]
    bad = copy.deepcopy(record.document)
    with pytest.raises(WholeTaskRejected, match="record_agreement_mismatch"):
        record.check_labels(labels, PEOPLE)
    bad["agreement"]["cohen_kappa"] = round(0.22 / 0.42, 12)
    fixed = load_record(save(tmp_path, bad, "fixed.json"))
    fixed.check_labels(labels, PEOPLE)
    with pytest.raises(WholeTaskRejected, match="labelling_mismatch"):
        fixed.check_labels(
            dict(labels, agreement=dict(labels["agreement"], method="double_annotation.v1")),
            PEOPLE,
        )
    # The named machine holds the annotator role only; the named owner adjudicates.
    tuner = dict(PEOPLE, machine=dict(id="machine", roles=["annotator", "tuner"]))
    with pytest.raises(WholeTaskRejected, match="labelling_roles"):
        fixed.check_labels(labels, tuner)
    pairs_labels = _labels(pairs, adjudicator="someone-else")
    pairs_labels["agreement"]["groups"] = labels["agreement"]["groups"]
    with pytest.raises(WholeTaskRejected, match="labelling_roles"):
        fixed.check_labels(pairs_labels, PEOPLE)


def test_an_approved_record_carries_the_corpus_custodian_review(tmp_path):
    """Spec 12.6.6: the owner's corpus review travels in the approved record."""
    review = dict(
        lock_digest=H,
        custodian_id="owner",
        review_record_evidence_ids=["review"],
        reviewed_evidence=[dict(evidence_id="review", domains=["access_control"])],
    )
    record = load_record(save(tmp_path, host_record(tmp_path, corpus_review=review)))
    custodian = record.custodian_review()
    assert (custodian.lock_digest, custodian.custodian_id) == (H, "owner")
    assert custodian.reviewed_evidence["review"] == frozenset({"access_control"})
    pending = host_record(tmp_path, corpus_review=review)
    pending["review"].update(pull_request=None, reviewer_logins=[], approved_at=None)
    assert load_record(save(tmp_path, pending)).custodian_review() is None


# --- the record's input subjects are the report's resolved inputs (12.6.6) --------


TASKS = dict(schema_version=1, kind="acceptance", tasks=[])
FREEZE = dict(schema_version=1, freeze="frozen")


def _input_record(root, scope="host_acceptance", tasks=TASKS, freeze=FREEZE, **overrides):
    record = host_record(root, scope=scope, **overrides)
    payloads = {"tasks": json.dumps(tasks).encode(), "freeze": json.dumps(freeze).encode()}
    for subject in record["subjects"]:
        if subject["role"] in payloads:
            payload = payloads[subject["role"]]
            (root / subject["path"]).write_bytes(payload)
            subject.update(sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload))
    if scope == "host_trial":
        record.update(preregistration=None, runs_started_at=None)
    return load_record(save(root, record))


def _inputs(root=TASKS, freeze=FREEZE):
    from types import SimpleNamespace

    return SimpleNamespace(root=root, freeze=freeze)


def test_the_bound_tasks_and_freeze_are_the_resolved_inputs(tmp_path):
    _input_record(tmp_path).check_inputs("host", _inputs())


@pytest.mark.parametrize(
    "inputs",
    [
        _inputs(root=dict(TASKS, tasks=[{"id": "other"}])),
        _inputs(freeze=dict(FREEZE, freeze="other")),
    ],
)
def test_unrelated_freeze_or_task_subjects_reject(tmp_path, inputs):
    with pytest.raises(WholeTaskRejected, match="record_input_mismatch"):
        _input_record(tmp_path).check_inputs("host", inputs)


def test_a_duplicate_input_subject_rejects(tmp_path):
    record = host_record(tmp_path)
    record["subjects"].append(_subject(tmp_path, "tasks", "tasks2.json", b'{"tasks":2}'))
    with pytest.raises(WholeTaskRejected, match="record_subjects"):
        load_record(save(tmp_path, record))


@pytest.mark.parametrize(
    ("scope", "kind"),
    [("host_trial", "acceptance"), ("host_acceptance", "early_trial")],
)
def test_the_host_scope_must_match_the_task_document_kind(tmp_path, scope, kind):
    """An acceptance report cannot be reviewed by a trial record without preregistration."""
    tasks = dict(TASKS, kind=kind)
    record = _input_record(tmp_path, scope=scope, tasks=tasks)
    with pytest.raises(WholeTaskRejected, match="qualification_record_scope"):
        record.check_inputs("host", _inputs(root=tasks))
