"""Run-kit commands of scripts/asset_whole_task.py (#236, #237).

Spec 0.53 section 12.6.1 and 0.65 section 12.6.5 (RM40–RM44; AC31–AC36). The
commands wrap the tested library functions; these tests check the command
surface, exit codes and that every written run is fixture or provisional.
"""

import json
import os
import sys

import pytest

from relaymind.canonical import canonical_digest
from relaymind.evaluation.asset_whole_task_cli import main
from test_asset_host_runner import captured, setup, write_executor
from whole_task_helpers import ENVIRONMENT, expanded_release, row


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_labelling_commands_round_trip_to_the_frozen_labels(tmp_path):
    root = expanded_release(tmp_path / "release", 2)
    catalog, cases, labels = (root / "catalog.json", root / "cases.json", root / "labels.json")
    frozen = read(labels)
    returned = []
    for annotator in ("judge-a", "judge-b"):
        out = tmp_path / f"{annotator}.json"
        args = ["--catalog", str(catalog), "--cases", str(cases), "--output", str(out)]
        assert main(["label-template", *args, "--annotator", annotator]) == 0
        template = read(out)
        raw = {j["case_id"]: j for j in frozen["judgments"] if j["annotator_id"] == annotator}
        for item in template["items"]:
            judgment = dict(raw[item["case_id"]])
            judgment.pop("case_id"), judgment.pop("annotator_id")
            item["judgment"] = judgment
        returned.append(str(write(out, template)))
    common = ["--catalog", str(catalog), "--cases", str(cases), "--judgments", *returned]
    sheet = tmp_path / "worksheet.json"
    assert main(["label-worksheet", *common, "--output", str(sheet)]) == 0
    worksheet = read(sheet)
    finals = {a["case_id"]: a for a in frozen["adjudications"]}
    for item in worksheet["items"]:
        item["adjudication"] = {k: v for k, v in finals[item["case_id"]].items() if k != "case_id"}
    write(sheet, worksheet)
    out = tmp_path / "labels.json"
    assert main(["label-freeze", *common, "--worksheet", str(sheet), "--output", str(out)]) == 0
    assert canonical_digest(read(out)) == canonical_digest(frozen)


def test_label_freeze_rejects_with_a_safe_reason(tmp_path, capsys):
    root = expanded_release(tmp_path / "release", 2)
    template = tmp_path / "t.json"
    args = ["--catalog", str(root / "catalog.json"), "--cases", str(root / "cases.json")]
    assert main(["label-template", *args, "--annotator", "judge-a", "--output", str(template)]) == 0
    capsys.readouterr()
    out = tmp_path / "sheet.json"
    code = main(["label-worksheet", *args, "--judgments", str(template), "--output", str(out)])
    assert code == 2 and not out.exists()
    assert json.loads(capsys.readouterr().err) == dict(status="rejected", reason="unjudged_case")


def test_preregister_host_writes_the_bound_digests(tmp_path):
    freeze, tasks = setup()
    out = tmp_path / "prereg.json"
    code = main(
        [
            "preregister-host",
            "--tasks",
            str(write(tmp_path / "tasks.json", tasks)),
            "--freeze",
            str(write(tmp_path / "freeze.json", freeze)),
            "--output",
            str(out),
        ]
    )
    record = read(out)
    assert code == 0 and record["input_digest"] == canonical_digest(tasks)
    assert record["acceptance_eligible"] is False


def executor_script(tmp_path):
    return write_executor(
        tmp_path,
        f"""
import json, sys
sys.path[:0] = {sys.path!r}
from test_asset_host_runner import captured
from whole_task_helpers import row
request = json.load(sys.stdin)
r, refs = captured(row(request["task"]["id"], request["arm"]))
json.dump(dict(row=r, evidence=refs), sys.stdout)
""",
    )


def test_run_host_writes_a_fixture_report_that_resumes(tmp_path):
    freeze, tasks = setup()
    inputs = write(tmp_path / "inputs.json", dict(documents=[], evidence=[]))
    base = [
        "run-host",
        "--tasks",
        str(write(tmp_path / "tasks.json", tasks)),
        "--freeze",
        str(write(tmp_path / "freeze.json", freeze)),
        "--journal",
        str(tmp_path / "run.jsonl"),
        "--inputs",
        str(inputs),
        "--environment",
        str(write(tmp_path / "environment.json", ENVIRONMENT)),
        "--qualification",
        "fixture",
        "--output",
        str(tmp_path / "report.json"),
        "--",
        *executor_script(tmp_path),
    ]
    assert main(base) == 0
    report = read(tmp_path / "report.json")
    assert report["qualification"] == "fixture"
    assert [r["status"] for r in report["rows"]] == ["completed"] * 6
    assert report["input_manifest"]["digest"] == canonical_digest(read(inputs))
    expected = [captured(row(p["task_id"], p["arm"]))[0] for p in report["planned"]]
    assert report["rows"] == expected
    # A rerun restores every journaled row; nothing executes twice.
    assert main(base[:-3] + ["--", sys.executable, "-c", "raise SystemExit(9)"]) == 0
    assert read(tmp_path / "report.json")["rows"] == expected


def test_run_host_offers_no_qualified_flag():
    with pytest.raises(SystemExit) as exit_:
        main(["run-host", "--qualification", "qualified"])
    assert exit_.value.code == 2


def test_capture_costs_writes_host_attempts(tmp_path):
    from test_asset_host_runner import USAGE, finished, started

    events = write(tmp_path / "events.json", [started(1, "c1"), finished(2, "c1", USAGE)])
    out = tmp_path / "attempts.json"
    code = main(
        [
            "capture-costs",
            "--events",
            str(events),
            "--provider",
            "provider-x",
            "--attempt-prefix",
            "t1-native-",
            "--evidence-id",
            "trace-t1",
            "--output",
            str(out),
        ]
    )
    (attempt,) = read(out)["attempts"]
    assert code == 0 and attempt["id"] == "t1-native-1" and attempt["input_tokens"] == 100


def test_preregister_episodes_with_the_corpus(tmp_path):
    from test_asset_episode_driver import setup as episode_setup

    _, freeze, episodes, _ = episode_setup(tmp_path)
    out = tmp_path / "prereg.json"
    code = main(
        [
            "preregister-episodes",
            "--episodes",
            str(write(tmp_path / "episodes.json", episodes)),
            "--freeze",
            str(write(tmp_path / "freeze.json", freeze)),
            "--lock",
            str(tmp_path / "release" / "lock.json"),
            "--evidence-root",
            str(tmp_path / "release"),
            "--output",
            str(out),
        ]
    )
    record = read(out)
    assert code == 0 and record["episodes"] == 1
    assert "corpus_not_acceptance" in record["reasons"]


def test_a_malformed_worksheet_exits_2(tmp_path, capsys):
    """Review 4435556027: no traceback, a structured rejection."""
    root = expanded_release(tmp_path / "release", 2)
    args = ["--catalog", str(root / "catalog.json"), "--cases", str(root / "cases.json")]
    frozen = read(root / "labels.json")
    returned = []
    for annotator in ("judge-a", "judge-b"):
        out = tmp_path / f"{annotator}.json"
        assert main(["label-template", *args, "--annotator", annotator, "--output", str(out)]) == 0
        template = read(out)
        raw = {j["case_id"]: j for j in frozen["judgments"] if j["annotator_id"] == annotator}
        for item in template["items"]:
            judgment = dict(raw[item["case_id"]])
            judgment.pop("case_id"), judgment.pop("annotator_id")
            item["judgment"] = judgment
        returned.append(str(write(out, template)))
    sheet = tmp_path / "sheet.json"
    common = [*args, "--judgments", *returned]
    assert main(["label-worksheet", *common, "--output", str(sheet)]) == 0
    worksheet = read(sheet)
    worksheet["items"][0] = None
    write(sheet, worksheet)
    capsys.readouterr()
    out = tmp_path / "labels.json"
    code = main(["label-freeze", *common, "--worksheet", str(sheet), "--output", str(out)])
    assert code == 2 and not out.exists()
    assert json.loads(capsys.readouterr().err) == dict(
        status="rejected", reason="worksheet_binding"
    )


def test_an_unwritable_output_is_a_structured_rejection(tmp_path, capsys):
    """Review 4435556037: output failures exit 2 like every other rejection."""
    freeze, tasks = setup()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "prereg.json"
    link.symlink_to(target)
    code = main(
        [
            "preregister-host",
            "--tasks",
            str(write(tmp_path / "tasks.json", tasks)),
            "--freeze",
            str(write(tmp_path / "freeze.json", freeze)),
            "--output",
            str(link),
        ]
    )
    assert code == 2 and target.read_text(encoding="utf-8") == "{}"
    assert json.loads(capsys.readouterr().err) == dict(status="rejected", reason="unsafe_file")


def test_an_evidence_root_without_its_lock_rejects(tmp_path, capsys):
    """Review 4435556042: the paired corpus flags are never silently ignored."""
    from test_asset_episode_driver import setup as episode_setup

    _, freeze, episodes, _ = episode_setup(tmp_path)
    out = tmp_path / "prereg.json"
    code = main(
        [
            "preregister-episodes",
            "--episodes",
            str(write(tmp_path / "episodes.json", episodes)),
            "--freeze",
            str(write(tmp_path / "freeze.json", freeze)),
            "--evidence-root",
            str(tmp_path / "release"),
            "--output",
            str(out),
        ]
    )
    assert code == 2 and not out.exists()
    assert json.loads(capsys.readouterr().err) == dict(status="rejected", reason="lock")


def test_run_host_executor_timeout_fails_the_row(tmp_path):
    """Review 4435556059: --executor-timeout bounds a hung executor program."""
    freeze, tasks = setup(1)
    code = main(
        [
            "run-host",
            "--tasks",
            str(write(tmp_path / "tasks.json", tasks)),
            "--freeze",
            str(write(tmp_path / "freeze.json", freeze)),
            "--journal",
            str(tmp_path / "run.jsonl"),
            "--inputs",
            str(write(tmp_path / "inputs.json", dict(documents=[], evidence=[]))),
            "--environment",
            str(write(tmp_path / "environment.json", ENVIRONMENT)),
            "--qualification",
            "fixture",
            "--executor-timeout",
            "0.2",
            "--output",
            str(tmp_path / "report.json"),
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ]
    )
    report = read(tmp_path / "report.json")
    assert code == 0
    assert {(r["status"], r["reason"]) for r in report["rows"]} == {("failed", "executor_error")}


def test_capture_costs_reads_events_strictly(tmp_path, capsys):
    """Duplicate keys and non-finite numbers are rejected, as for every input."""
    from test_asset_host_runner import USAGE, finished, started

    text = json.dumps([started(1, "c1"), finished(2, "c1", USAGE)])
    events = tmp_path / "events.json"
    events.write_text(text.replace('"seq": 2,', '"seq": 7, "seq": 2,', 1), encoding="utf-8")
    out = tmp_path / "attempts.json"
    base = ["capture-costs", "--provider", "p", "--attempt-prefix", "a-", "--evidence-id", "t"]
    code = main([*base, "--events", str(events), "--output", str(out)])
    assert code == 2 and not out.exists()
    assert json.loads(capsys.readouterr().err)["status"] == "rejected"
    events.write_text(text.replace("100", "NaN", 1), encoding="utf-8")
    assert main([*base, "--events", str(events), "--output", str(out)]) == 2


def run_host_args(tmp_path, tasks, freeze, *extra):
    return [
        "run-host",
        "--tasks",
        str(write(tmp_path / "tasks.json", tasks)),
        "--freeze",
        str(write(tmp_path / "freeze.json", freeze)),
        "--journal",
        str(tmp_path / "run.jsonl"),
        "--inputs",
        str(write(tmp_path / "inputs.json", dict(documents=[], evidence=[]))),
        "--environment",
        str(write(tmp_path / "environment.json", ENVIRONMENT)),
        "--qualification",
        "fixture",
        "--output",
        str(tmp_path / "report.json"),
        *extra,
    ]


def test_run_host_reads_artifacts_strictly(tmp_path, capsys):
    freeze, tasks = setup(1)
    artifacts = tmp_path / "artifacts.json"
    artifacts.write_text('[{"role": "a", "role": "b"}]', encoding="utf-8")
    args = run_host_args(tmp_path, tasks, freeze, "--artifacts", str(artifacts), "--")
    assert main([*args, *executor_script(tmp_path)]) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_run_host_exits_3_after_an_interruption(tmp_path):
    """SIGINT ends the run; unstarted rows stay not_run and the exit is 3."""
    freeze, tasks = setup(1)
    script = tmp_path / "interrupting.py"
    script.write_text(
        f"""
import json, os, signal, sys
sys.path[:0] = {sys.path!r}
from test_asset_host_runner import captured
from whole_task_helpers import row
request = json.load(sys.stdin)
os.kill(os.getppid(), signal.SIGINT)
r, refs = captured(row(request["task"]["id"], request["arm"]))
json.dump(dict(row=r, evidence=refs), sys.stdout)
""",
        encoding="utf-8",
    )
    code = main(run_host_args(tmp_path, tasks, freeze, "--", sys.executable, str(script)))
    report = read(tmp_path / "report.json")
    assert code == 3
    assert [r["status"] for r in report["rows"]] == ["completed", "not_run", "not_run"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_a_second_signal_stops_a_hung_executor_and_keeps_its_row_started(tmp_path, capsys):
    """Section 12.6.5 (RM44; AC36): with no executor timeout, a second SIGINT
    stops the run at once. The executor's group is killed, the lock is released
    and the started row is exported as cancelled on resume, never rerun."""
    freeze, tasks = setup(1)
    pid_file = tmp_path / "executor.pid"
    script = tmp_path / "hanging.py"
    script.write_text(
        f"""
import os, signal, sys, time
open({str(pid_file)!r}, "w").write(str(os.getpid()))
os.kill(os.getppid(), signal.SIGINT)
time.sleep(0.3)
os.kill(os.getppid(), signal.SIGINT)
time.sleep(60)
""",
        encoding="utf-8",
    )
    args = run_host_args(tmp_path, tasks, freeze, "--", sys.executable, "-I", str(script))
    assert main(args) == 3
    assert json.loads(capsys.readouterr().err)["status"] == "interrupted"
    assert not (tmp_path / "run.jsonl.lock").exists()
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    resumed = run_host_args(tmp_path, tasks, freeze, "--", *executor_script(tmp_path))
    assert main(resumed) == 0
    rows = read(tmp_path / "report.json")["rows"]
    assert rows[0]["status"] == "cancelled" and rows[0]["reason"] == "interrupted_unrecorded"
    assert [r["status"] for r in rows[1:]] == ["completed", "completed"]
