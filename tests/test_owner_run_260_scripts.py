"""Guards of the #260 owner-run scripts (spec 0.66 sections 12.6.5, 12.6.6).

RM16, RM40, RM44; AC36. The host runner dispatches only prompts of the
verified frozen corpus its preregistered tasks and freeze name; the success
judging needs complete, valid raw judgments from both machine judges before
adjudication and before any verdict is applied; and the external-token cap is
enforced before a row starts and while it runs, charging unknown consumption
at its worst case. No model or host is called: fixtures and doubles only.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from relaymind.canonical import canonical_digest
from whole_task_helpers import expanded_release

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "evaluation" / "assets" / "owner-run-260"


def _module(name: str):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(
            f"owner_run_260_{name}", SCRIPTS / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


run_host = _module("run_host")
judge = _module("judge")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def relock(root: Path) -> None:
    lock, labels = read(root / "lock.json"), read(root / "labels.json")
    labels["cases_digest"] = canonical_digest(read(root / "cases.json"))
    write(root / "labels.json", labels)
    for name in ("catalog", "cases", "labels"):
        lock[name]["digest"] = canonical_digest(read(root / f"{name}.json"))
    lock.pop("digest")
    lock["digest"] = canonical_digest(lock)
    write(root / "lock.json", lock)


def frozen(tmp_path):
    root = expanded_release(tmp_path / "release", 3)
    case = read(root / "cases.json")["cases"][0]
    digest = canonical_digest(read(root / "lock.json"))
    tasks = dict(
        corpus_lock_digest=digest,
        tasks=[
            dict(
                id="t1",
                lane=case["lane"],
                family_ids=case["task_family_ids"],
                prompt_case_id=case["id"],
                asset_bindings=[],
            )
        ],
    )
    return root, tasks, dict(corpus_lock_digest=digest), case


# --- finding 1: prompts come only from the verified frozen corpus -----------------


def test_prompts_come_from_the_verified_lock_the_tasks_and_freeze_name(tmp_path):
    root, tasks, freeze, case = frozen(tmp_path)
    prompts = run_host.verified_prompts(tasks, freeze, root / "lock.json", root)
    assert prompts == {case["id"]: case["task_text"]}


def test_an_edited_cases_file_with_the_same_ids_is_rejected(tmp_path):
    """Same case IDs, different prompt text: the lock's cases digest no longer matches."""
    root, tasks, freeze, _ = frozen(tmp_path)
    cases = read(root / "cases.json")
    cases["cases"][0]["task_text"] += " (edited)"
    write(root / "cases.json", cases)
    with pytest.raises(ValueError):
        run_host.verified_prompts(tasks, freeze, root / "lock.json", root)


def test_a_relocked_corpus_is_not_the_preregistered_one(tmp_path):
    """A consistent new lock still differs from the digest the tasks were frozen with."""
    root, tasks, freeze, _ = frozen(tmp_path)
    cases = read(root / "cases.json")
    cases["cases"][0]["task_text"] += " (edited)"
    write(root / "cases.json", cases)
    relock(root)
    with pytest.raises(run_host.CorpusMismatch, match="corpus_lock_mismatch"):
        run_host.verified_prompts(tasks, freeze, root / "lock.json", root)


def test_tasks_and_freeze_must_name_the_same_corpus(tmp_path):
    root, tasks, _, _ = frozen(tmp_path)
    with pytest.raises(run_host.CorpusMismatch, match="corpus_lock_mismatch"):
        run_host.verified_prompts(
            tasks, dict(corpus_lock_digest="0" * 64), root / "lock.json", root
        )


# --- finding 3: the external-token cap bounds spending ---------------------------


def budget(tmp_path, spent=0, cap=1_000_000, row=200_000, call=50_000):
    ledger = tmp_path / "ledger.json"
    write(ledger, dict(total_input=spent, total_output=0, stages={}))
    return run_host.Budget(ledger, cap=cap, row_budget=row, call_allowance=call)


def test_a_row_starts_only_when_its_whole_reservation_fits(tmp_path):
    assert budget(tmp_path, spent=800_000).can_start()
    assert not budget(tmp_path, spent=800_001).can_start()


def test_a_row_stops_before_another_call_could_pass_its_reservation(tmp_path):
    b = budget(tmp_path)
    assert not b.row_limit_reached(149_999)
    assert b.row_limit_reached(150_000)


def test_unknown_consumption_is_charged_at_its_worst_case(tmp_path):
    b = budget(tmp_path)
    # Known usage plus two calls whose usage was never reported.
    b.settle("provider_only", prompt=10_000, completion=1_000, unknown_calls=2, evidence=True)
    assert b.spent() == 11_000 + 2 * 50_000
    # A started row with no usage evidence at all is charged its whole reservation.
    b.settle("native", prompt=0, completion=0, unknown_calls=0, evidence=False)
    assert b.spent() == 111_000 + 200_000
    saved = read(tmp_path / "ledger.json")
    assert saved["total_input"] + saved["total_output"] == b.spent()


def test_the_watchdog_stops_a_host_turn_at_the_row_limit(tmp_path):
    b = budget(tmp_path)
    used = iter([0, 60_000, 120_000, 160_000, 400_000])
    result = run_host.run_bounded(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        timeout=20,
        usage=lambda: next(used),
        limit_reached=b.row_limit_reached,
        poll=0.05,
    )
    assert result["status"] == "budget_stopped"
    assert result["row_tokens_at_stop"] == 160_000
    assert result["elapsed_ms"] < 10_000


def test_the_watchdog_keeps_an_ordinary_turn(tmp_path):
    b = budget(tmp_path)
    result = run_host.run_bounded(
        [sys.executable, "-c", "print('{\"ok\": true}')"],
        env=None,
        timeout=20,
        usage=lambda: 0,
        limit_reached=b.row_limit_reached,
        poll=0.05,
    )
    assert (result["status"], result["exit_code"]) == ("completed", 0)
    assert json.loads(result["stdout"]) == {"ok": True}


# --- finding 2: two complete raw judgments before adjudication and apply --------


def items(*ids):
    return {i: dict(blind_id=i) for i in ids}


def verdict(success=True):
    return dict(success=success, notes="n")


def test_adjudication_needs_a_valid_judgment_from_both_judges_for_every_item():
    blind = items("x1", "x2")
    full = {"x1": verdict(), "x2": verdict(False)}
    judge.check_judges(blind, full, full)
    for a, b, reason in [
        (full, {}, "judge_b_incomplete"),
        ({"x1": verdict()}, full, "judge_a_incomplete"),
        (full, {"x1": verdict(), "x2": dict(success="yes", notes="")}, "judge_b_invalid"),
        (full, dict(full, x3=verdict()), "judge_b_unknown_item"),
    ]:
        with pytest.raises(judge.JudgingIncomplete, match=reason):
            judge.check_judges(blind, a, b)


def judgments(a, b, final):
    return dict(
        format="relaymind.owner_run_success_judgments.v1",
        judges=dict(a=a, b=b),
        disputed=sorted(i for i in a if i in b and a[i]["success"] != b[i]["success"]),
        final=final,
    )


def test_apply_rechecks_both_judges_and_every_final_verdict():
    mapping = {
        "x1": dict(task_id="t1", arm="native"),
        "x2": dict(task_id="t1", arm="provider_only"),
    }
    a = {"x1": verdict(), "x2": verdict()}
    b = {"x1": verdict(), "x2": verdict(False)}
    final = {"x1": dict(success=True, reason="agree"), "x2": dict(success=False, reason="r")}
    judge.check_judgments(judgments(a, b, final), mapping)
    bad = [
        (judgments(a, {}, final), "judge_b_incomplete"),
        (judgments(a, b, {"x1": final["x1"]}), "final_incomplete"),
        # An agreed item cannot be overturned by the adjudication rule.
        (judgments(a, b, dict(final, x1=dict(success=False, reason="r"))), "final_contradicts"),
        # A disputed item needs an adjudication reason.
        (judgments(a, b, dict(final, x2=dict(success=False, reason=" "))), "final_unreasoned"),
    ]
    for document, reason in bad:
        with pytest.raises(judge.JudgingIncomplete, match=reason):
            judge.check_judgments(document, mapping)
