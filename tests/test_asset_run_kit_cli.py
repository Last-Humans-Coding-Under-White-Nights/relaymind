"""Run-kit commands of scripts/asset_whole_task.py (#236, #237).

Spec 0.53 section 12.6.1 and 0.65 section 12.6.5 (RM40–RM44; AC31–AC36). The
commands wrap the tested library functions; these tests check the command
surface, exit codes and that every written run is fixture or provisional.
"""

import json
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
