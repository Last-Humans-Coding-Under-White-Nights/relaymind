"""RM32/AC19: partition-independent gates against all six buffered predicates.

The buffered predicate is the oracle, including its documented syntactic limits;
these tests do not claim semantic validation beyond the existing predicates.
"""

import inspect
import random
from collections import Counter
from dataclasses import replace

import pytest

import test_completion_predicates as f
from relaymind.skills import predicates as p


def test_all_predicates_across_cut_random_and_adversarial_partitions(monkeypatch):
    """RM32/AC19: preserve safe units even when the same chunk later rejects."""
    cases = []
    origin = ""

    def collect(predicate, answer, results, expected):
        cases.append((origin, predicate, answer, results, expected))

    monkeypatch.setattr(f, "check", collect)
    for name, fn in vars(f).copy().items():
        if (
            not name.startswith("test_")
            or not callable(fn)
            or name == "test_statement_factorization_matches_all_existing_parameter_cases"
        ):
            continue
        origin = name
        marks = [m for m in getattr(fn, "pytestmark", []) if m.name == "parametrize"]
        if not marks:
            if not inspect.signature(fn).parameters:
                fn()
            continue
        assert len(marks) == 1
        mark = marks[0]
        names = mark.args[0].split(",") if isinstance(mark.args[0], str) else mark.args[0]
        names = [n.strip() for n in names]
        for i, row in enumerate(mark.args[1]):
            values = getattr(row, "values", None)
            if values is None:
                values = (row,) if len(names) == 1 else row
            origin = f"{name}[{i}]"
            fn(**dict(zip(names, values, strict=True)))
    fixture_count = len(cases)

    def add(name, predicate, answer, results):
        cases.append(
            (name, predicate, answer, results, p.COMPLETION_PREDICATES[predicate](answer, results))
        )

    health = f"payments-api is unavailable at {f.T}."
    zhhealth = f"payments-api 的状态是 unavailable（不可用），检查时间为 {f.T}。"
    deployment = "staging-api latest deployment: dep-staging-0142 at 2026-10-19T22:10:00Z."
    zhdeployment = "staging-api 部署 dep-staging-0142，时间 2026-10-19T22:10:00Z。"
    counts = "staging-api last 1440 minutes: total 7, critical 1, warning 3, info 3."
    zhcounts = "过去 1440 分钟，staging-api 总计 7，严重 1，警告 3，信息 3。"
    draft = "Diagnostic checklist (draft, not log-backed):"
    zhdraft = "诊断检查清单（草稿，未基于日志）："
    bases = {
        "health_status_preserved": ([f.UNAVAILABLE], [health, zhhealth]),
        "deployment_reported": ([f.DEPLOYED], [deployment, zhdeployment]),
        "incident_counts_reported": ([f.COUNTS], [counts, zhcounts]),
        "deletion_outcome_reported": (
            [f.DELETED],
            ["Deleted demo-orders-db.", "已删除沙箱数据库 demo-orders-db。"],
        ),
        "performance_evidence_separated": (
            [f.METRICS],
            ["Observations: p95 rose.\nHypotheses: None.", "观察：p95 上升。\n假设：未知。"],
        ),
        "health_checklist_separated": (
            [f.UNAVAILABLE],
            [
                f"Observation: {health}\n{draft}\n- Check ledger-db.",
                f"观察：{zhhealth}\n{zhdraft}\n- 检查依赖。",
            ],
        ),
    }
    suffixes = {
        "health_status_preserved": [
            " payments-api is healthy. ",
            " payments-api is not unavailable; ",
            " payments-api unavailable（健康）。",
            " ledger-db is healthy.",
            " payments-api is unavailable or healthy. ",
            "\r\npayments-api is healthy. ",
            " payments-api unavailable-ness.",
        ],
        "deployment_reported": [
            " dep-staging-0199. ",
            " dep-staging-0142-v2;",
            " Owner account bogus-account-99.",
            " deployment-2026-10。",
            " revision deadbee1.",
        ],
        "incident_counts_reported": [
            " Correction: 9 incidents, 2 critical. ",
            " because aliens did it. ",
            " 原因未知。",
            " critical 1 incident。",
            " 3 warningx。",
            " Critical: 1 incident; ",
            "\r\nwarning: 33. ",
        ],
        "deletion_outcome_reported": [
            " demo-users-db。",
            " not deleted. ",
            " 没有删除。",
            " deletion failed;",
            " demo-users-db-2。",
            " never successfully deleted. ",
        ],
        "performance_evidence_separated": [
            "\nHypotheses:",
            "\nObservations:\nHypotheses:\n- more.",
            "\nInsufficient evidence is not an issue: aliens.",
            "\n证据不足：未知。",
            "\r\nHypotheses: None.",
        ],
        "health_checklist_separated": [
            "\n- Observation: payments-api healthy.",
            "\nObservation: payments-api healthy.\n",
            f"\nObservation: payments-api unavailable.\n{draft}\npayments-api healthy.\n",
            "\n观察：payments-api unavailable（健康）。\n",
        ],
    }
    for predicate, (results, answers) in bases.items():
        for lang, answer in enumerate(answers):
            add(f"base-{predicate}-{lang}", predicate, answer, results)
            for i, suffix in enumerate(suffixes[predicate]):
                add(f"continuation-{predicate}-{lang}-{i}", predicate, answer + suffix, results)
            for newline in ("\n", "\r\n"):
                for sep in ("; ", "。", ". ", "! ", "? ", newline):
                    add(
                        f"punctuation-{predicate}-{lang}-{sep!r}-{newline!r}",
                        predicate,
                        answer.replace(". ", sep).replace("\n", newline) + sep + "More text.",
                        results,
                    )
            for result_state, newresults in [
                ("missing", []),
                ("latest-failed", results + [replace(results[-1], status=f.ToolStatus.FAILED)]),
                ("truncated", [replace(results[-1], truncated=True)]),
            ]:
                add(f"evidence-{predicate}-{lang}-{result_state}", predicate, answer, newresults)
            for end in (0, 1, len(answer) // 2):
                add(f"incomplete-{predicate}-{lang}-{end}", predicate, answer[:end], results)

    # Ambiguous token/negation/gloss/resource/label continuations, including old syntactic limits.
    special = [
        ("health_status_preserved", f"Healthy: payments-api; {health}", [f.UNAVAILABLE]),
        ("health_status_preserved", f"payments-api is unavailable\r\n{f.T}", [f.UNAVAILABLE]),
        ("health_status_preserved", f"payments-api unavailable（健康）{f.T}。", [f.UNAVAILABLE]),
        (
            "health_status_preserved",
            health + " Next check whether web-1 and ledger-db are healthy.",
            [f.UNAVAILABLE],
        ),
        ("health_status_preserved", f"payments-api is not healthy at {f.T}.", [f.HEALTHY]),
        ("health_status_preserved", f"payments-api is unavailablex. {health}", [f.UNAVAILABLE]),
        ("deployment_reported", "payments-api checked.", [f.NO_DEPLOYMENT]),
        ("deployment_reported", "payments-api 没有部署记录。部署编号为 0142。", [f.NO_DEPLOYMENT]),
        ("deployment_reported", "payments-api checked. revision deadbee1。", [f.NO_DEPLOYMENT]),
        ("deployment_reported", deployment + " Owner account bogus-account-99.", [f.DEPLOYED]),
        (
            "performance_evidence_separated",
            "Observations: payments-api is healthy because aliens fixed it.\nHypotheses: None.",
            [f.METRICS],
        ),
        (
            "performance_evidence_separated",
            "Observations:\r\nHypotheses:\r\n- ledger-db is slow.",
            [f.METRICS],
        ),
        (
            "performance_evidence_separated",
            "Observations: p95 rose. Hypotheses: unknown.",
            [f.METRICS],
        ),
        (
            "performance_evidence_separated",
            "**Observations:**\r\n- p95 rose.\r\n## Hypotheses:\r\n- unknown.",
            [f.METRICS],
        ),
    ]
    for label in [
        draft,
        "Diagnostic checklist\n(\ndraft,\nnot log-backed\n)\n",
        "诊断检查清单\n（草稿，\n未基于日志）\n",
    ]:
        for newline in ["\n", "\r\n"]:
            prefix = "Observation: " + health + newline
            for continuation in [
                "payments-api healthy.\n- Observation: payments-api healthy.",
                "payments-api healthy.\nObservation: payments-api unavailable.\n"
                + draft
                + "\npayments-api healthy.",
                "payments-api healthy.\nObservation:\npayments-api unavailable.",
                "payments-api healthy.\n**Observation:** payments-api unavailable.",
            ]:
                special.append(
                    (
                        "health_checklist_separated",
                        (prefix + label + "\n" + continuation).replace("\n", newline),
                        [f.UNAVAILABLE],
                    )
                )
    for i, (predicate, answer, results) in enumerate(special):
        add(f"special-{i}", predicate, answer, results)

    counts_by_predicate = Counter()
    partition_counts = Counter()
    findings = []
    early = Counter()
    early_offsets = {}
    seed = 0xAC1932

    def partitions(answer, index):
        yield "whole", [answer]
        for cut in range(len(answer) + 1):
            yield "single-cut", [answer[:cut], answer[cut:]]
        yield "one-char", list(answer)
        rng = random.Random(seed + index)
        for _ in range(12):
            cuts = sorted(
                set(
                    [0, len(answer)]
                    + [rng.randrange(len(answer) + 1) for _ in range(min(20, len(answer)))]
                )
            )
            yield "random", [answer[a:b] for a, b in zip(cuts, cuts[1:], strict=False)]

    for index, (name, predicate, answer, results, fixture_expected) in enumerate(cases):
        counts_by_predicate[predicate] += 1
        expected = p.COMPLETION_PREDICATES[predicate](answer, results)
        safety, complete = p.completion_parts(predicate, answer, results)
        if (safety and complete) != expected or fixture_expected != expected:
            findings.append(
                {
                    "kind": "factorization-or-fixture",
                    "name": name,
                    "predicate": predicate,
                    "answer": answer,
                }
            )
        rejection_offsets = set()
        rejection_prefixes = set()
        for mode, chunks in partitions(answer, index):
            partition_counts[mode] += 1
            gate = p.StatementGate(predicate, results)
            output = []
            rejected = False
            early_rejected = False
            consumed = 0
            try:
                for chunk in chunks:
                    consumed += len(chunk)
                    try:
                        output.extend(gate.push(chunk))
                    except p.StatementRejected:
                        early_rejected = True
                        raise
                output.append(gate.finish())
            except p.StatementRejected:
                rejected = True
                emitted = "".join(output)
                assert emitted == gate.raw[: gate.offset], (
                    "rejected-output-prefix",
                    name,
                    mode,
                    emitted,
                    gate.raw[: gate.offset],
                )
                # No emitted text makes a claim when evidence is absent; any actual
                # publication must satisfy safety (final completeness is not required).
                assert not emitted or p.completion_parts(predicate, emitted, results)[0], (
                    "unsafe-emitted-prefix",
                    name,
                    mode,
                    emitted,
                )
                rejection_prefixes.add(emitted)
                additional = []
                try:
                    additional.extend(gate.push(" continuation. "))
                except p.StatementRejected:
                    pass
                else:
                    findings.append({"kind": "rejection-not-sticky", "name": name})
                assert additional == [], ("sticky-rejection-output", name, mode, additional)
                if early_rejected:
                    early[predicate] += 1
                    rejection_offsets.add(gate.offset)
                    # A stable rejection cannot be invalidated by already received lookahead,
                    # by the full continuation, or by another later append.
                    raw_safe = p.completion_parts(predicate, gate.raw, results)[0]
                    if raw_safe or safety or expected:
                        findings.append(
                            {
                                "kind": "unstable-early-rejection",
                                "name": name,
                                "predicate": predicate,
                                "mode": mode,
                                "consumed": consumed,
                                "offset": gate.offset,
                                "raw_safe": raw_safe,
                                "full_safe": safety,
                                "expected": expected,
                                "answer": answer,
                                "raw": gate.raw,
                            }
                        )
            if rejected == expected:
                findings.append(
                    {
                        "kind": "acceptance-mismatch",
                        "name": name,
                        "predicate": predicate,
                        "mode": mode,
                        "expected": expected,
                        "answer": answer,
                        "consumed": consumed,
                    }
                )
            elif not rejected and "".join(output) != answer:
                findings.append(
                    {
                        "kind": "output-mismatch",
                        "name": name,
                        "predicate": predicate,
                        "mode": mode,
                        "answer": answer,
                        "output": "".join(output),
                    }
                )
        assert len(rejection_prefixes) <= 1, (
            "partition-dependent-emitted-prefix",
            name,
            predicate,
            rejection_prefixes,
        )
        if rejection_offsets:
            early_offsets[name] = sorted(rejection_offsets)
            if len(rejection_offsets) != 1:
                findings.append(
                    {
                        "kind": "partition-dependent-rejection-prefix",
                        "name": name,
                        "predicate": predicate,
                        "offsets": sorted(rejection_offsets),
                        "answer": answer,
                    }
                )
    assert set(counts_by_predicate) == set(p.COMPLETION_PREDICATES)
    assert fixture_count > 100
    assert len(cases) == 450
    assert sum(partition_counts.values()) == 38266
    assert not findings, findings[:12]


def test_same_chunk_retains_verified_statement_before_rejection():
    """RM32/AC19: a later invalid unit cannot leak or erase an already yielded unit."""
    verified = f"payments-api is unavailable at {f.T}."
    answer = verified + " payments-api is healthy. Another statement."
    gate = p.StatementGate("health_status_preserved", [f.UNAVAILABLE])
    emitted = []
    with pytest.raises(p.StatementRejected):
        emitted.extend(gate.push(answer))
    assert "".join(emitted) == verified == gate.raw[: gate.offset]
    assert p.completion_parts("health_status_preserved", "".join(emitted), [f.UNAVAILABLE])[0]
    additional = []
    with pytest.raises(p.StatementRejected):
        additional.extend(gate.push(" More text."))
    assert additional == []
