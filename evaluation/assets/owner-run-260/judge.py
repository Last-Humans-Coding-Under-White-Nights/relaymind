"""Blind task-success judging for the #260 host report (spec 0.66 section 12.6.6).

RM16, RM44; AC36. Under ``machine_dual_owner_approved`` the two machine
annotators judge every completed row against its preregistered rubric, blind to
arm, and the machine adjudicator resolves disagreements:

* ``blind`` gives every completed row a random blind ID, anonymizes its final
  answer (assistant, provider and model names become ``[ASSISTANT]``), orders
  the items by SHA-256 of canonical ``{seed, task_id, blind_id}`` and writes the
  private blind-ID-to-arm map plus its digest (commit the digest first);
* ``judge`` runs one judge over the items (temperature 0, its own template);
* ``adjudicate`` resolves the items the two judges disagree on;
* ``apply`` opens the map, writes each verdict into its row with the judging
  evidence, and recomputes the summary. A failed or timed-out row is a known
  non-success; a not-run row keeps a null verdict.
"""

import argparse
import hashlib
import json
import re
import secrets
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))
sys.path.insert(0, str(HERE))

import harness  # noqa: E402
from harness import read  # noqa: E402

from relaymind.canonical import canonical_digest, canonical_json  # noqa: E402

_NAMES = re.compile(
    r"relay\s*mind|openclaw|glm[-\w.]*|zhipu|z\.ai|external-openai-compatible", re.IGNORECASE
)


class JudgingIncomplete(ValueError):
    """The spec 12.6.6 two-judge rule is not met; nothing is adjudicated or applied."""


def _valid(row) -> bool:
    return isinstance(row, dict) and type(row.get("success")) is bool


def check_judges(blind_items, a: dict, b: dict) -> None:
    """Both judges gave one valid raw verdict for every blind item, and no other."""
    expected = set(blind_items)
    for name, raw in (("a", a), ("b", b)):
        if not isinstance(raw, dict) or set(raw) - expected:
            raise JudgingIncomplete(f"judge_{name}_unknown_item")
        if expected - set(raw):
            raise JudgingIncomplete(f"judge_{name}_incomplete")
        if not all(_valid(raw[i]) for i in expected):
            raise JudgingIncomplete(f"judge_{name}_invalid")


def check_judgments(document: dict, mapping: dict) -> None:
    """Before applying: both raw judgments and one consistent final verdict per item.

    Agreed items keep the agreed verdict; every disputed item needs an
    adjudicated verdict with a nonempty reason.
    """
    a, b = document["judges"]["a"], document["judges"]["b"]
    check_judges(mapping, a, b)
    final = document["final"]
    if set(final) != set(mapping) or not all(_valid(final[i]) for i in mapping):
        raise JudgingIncomplete("final_incomplete")
    disputed = sorted(i for i in mapping if a[i]["success"] != b[i]["success"])
    if sorted(document["disputed"]) != disputed:
        raise JudgingIncomplete("disputed_mismatch")
    for item in mapping:
        if item not in disputed and final[item]["success"] != a[item]["success"]:
            raise JudgingIncomplete("final_contradicts_agreement")
        if item in disputed and not str(final[item].get("reason", "")).strip():
            raise JudgingIncomplete("final_unreasoned")


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes((canonical_json(value) + "\n").encode("utf-8"))


def final_answer(evidence_root: Path, row) -> str:
    ref = next(i for i in row["evidence_ids"] if i.endswith(":host-result.json"))
    key = ref.split(":", 1)[0]
    result = read(evidence_root / "host-runs" / key / "host-result.json")
    report = result.get("report") or {}
    return report.get("final") or ""


def cmd_blind(args) -> None:
    report, tasks = read(args.report), read(args.tasks)
    cases = {c["id"]: c for c in read(args.cases)["cases"]}
    prompt = {t["id"]: cases[t["prompt_case_id"]]["task_text"] for t in tasks["tasks"]}
    items, mapping = [], {}
    for row in report["rows"]:
        if row["status"] != "completed":
            continue
        blind_id = secrets.token_hex(8)
        mapping[blind_id] = dict(task_id=row["task_id"], arm=row["arm"])
        rubric = read(HERE / "rubrics" / f"{row['task_id']}.json")["criteria"]
        items.append(
            dict(
                blind_id=blind_id,
                task_id=row["task_id"],
                request=prompt[row["task_id"]],
                rubric=rubric,
                answer=_NAMES.sub("[ASSISTANT]", final_answer(args.evidence, row)),
            )
        )
    items.sort(
        key=lambda i: hashlib.sha256(
            canonical_json(
                dict(seed=tasks["seed"], task_id=i["task_id"], blind_id=i["blind_id"])
            ).encode("utf-8")
        ).hexdigest()
    )
    write(args.items, dict(format="relaymind.owner_run_blind_items.v1", items=items))
    blind_map = dict(format="relaymind.owner_run_blind_map.v1", map=mapping)
    write(args.map, blind_map)
    write(
        args.map_digest,
        dict(
            format="relaymind.owner_run_blind_map_digest.v1",
            map_canonical_sha256=canonical_digest(blind_map),
            items=len(items),
        ),
    )
    print(json.dumps(dict(items=len(items))))


def cmd_judge(args) -> None:
    items = read(args.items)["items"]
    template = (HERE / "prompts" / args.template).read_text(encoding="utf-8")
    ledger = harness.Ledger(args.ledger, args.cap)
    out = read(args.output) if args.output.exists() else {}
    # Batches are fixed positions in the full item sequence, so a resume never
    # gives a new batch the name of one already sent.
    for number, batch in harness.batches(items, args.batch, lambda i: i["blind_id"] not in out):
        asked = {i["blind_id"] for i in batch}
        request = [{k: i[k] for k in ("blind_id", "request", "rubric", "answer")} for i in batch]
        messages = [
            dict(role="system", content=template),
            dict(role="user", content=json.dumps(request, ensure_ascii=False)),
        ]
        name = f"{args.name}-{number:03d}"
        answer = harness.chat(ledger, args.name, messages, args.log, name)
        try:
            rows = harness.answer_json(answer)["judgments"]
        except (ValueError, KeyError, TypeError):
            print("unparsed", name, file=sys.stderr)
            continue
        for row in rows:
            if row.get("blind_id") in asked and isinstance(row.get("success"), bool):
                out[row["blind_id"]] = dict(success=row["success"], notes=str(row.get("notes")))
        write(args.output, out)
        print(json.dumps(dict(done=len(out), ledger=ledger.total)))


def cmd_adjudicate(args) -> None:
    items = {i["blind_id"]: i for i in read(args.items)["items"]}
    a, b = read(args.a), read(args.b)
    # Spec 12.6.6: two complete raw judgments before any adjudication.
    check_judges(items, a, b)
    final = {}
    disputed = []
    for blind_id in items:
        if a[blind_id]["success"] == b[blind_id]["success"]:
            final[blind_id] = dict(
                success=a[blind_id]["success"],
                reason="Both judges agree; the adjudication rule adopts the verdict.",
            )
        else:
            disputed.append(blind_id)
    # A rerun keeps the adjudications already paid for over the same two judgments.
    if args.output.exists():
        earlier = read(args.output)
        if earlier.get("judges") == dict(a=a, b=b):
            for blind_id in disputed:
                kept = earlier.get("final", {}).get(blind_id)
                if _valid(kept) and str(kept.get("reason", "")).strip():
                    final[blind_id] = kept

    def document():
        return dict(
            format="relaymind.owner_run_success_judgments.v1",
            judges=dict(a=a, b=b),
            disputed=sorted(disputed),
            final=final,
        )

    ledger = harness.Ledger(args.ledger, args.cap)
    template = (HERE / "prompts" / "judge-adjudicator.txt").read_text(encoding="utf-8")
    for number, batch in harness.batches(disputed, args.batch, lambda i: i not in final):
        request = [
            dict(
                {k: items[i][k] for k in ("blind_id", "request", "rubric", "answer")},
                judges=[a.get(i), b.get(i)],
            )
            for i in batch
        ]
        messages = [
            dict(role="system", content=template),
            dict(role="user", content=json.dumps(request, ensure_ascii=False)),
        ]
        name = f"judge-adjudicator-{number:03d}"
        answer = harness.chat(ledger, "judge-adjudicator", messages, args.log, name)
        try:
            rows = harness.answer_json(answer)["adjudications"]
            if not isinstance(rows, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            print("unparsed", name, file=sys.stderr)
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("blind_id") not in batch:
                continue
            reason = str(row.get("reason") or "").strip()
            if isinstance(row.get("success"), bool) and reason:
                final[row["blind_id"]] = dict(success=row["success"], reason=reason)
        # Written after every batch; apply rechecks it, so a partial file is never applied.
        write(args.output, document())
    write(args.output, document())
    # A disputed item the adjudicator left out stops here; rerun the command.
    check_judgments(document(), items)
    agree = len(items) - len(disputed)
    print(json.dumps(dict(items=len(items), agree=agree, disputed=len(disputed))))


def apply_verdicts(report: dict, by_row: dict) -> None:
    """Set every row's success: the judged verdict of a completed row, and
    ``False`` for every started row that did not complete (failed, timeout,
    cancelled, for example stopped at the token cap). A ``not_run`` row keeps
    ``None``.
    """
    for row in report["rows"]:
        if row["status"] == "completed":
            verdict = by_row[(row["task_id"], row["arm"])]
            row["success"] = verdict["success"]
            row["evidence_ids"] = sorted(
                {*row["evidence_ids"], "judging:blind-map", "judging:success-judgments"},
                key=str.encode,
            )
        elif row["status"] != "not_run":
            row["success"] = False


def cmd_apply(args) -> None:
    from relaymind.evaluation.asset_whole_task import host_summary

    report, tasks, freeze = read(args.report), read(args.tasks), read(args.freeze)
    mapping = read(args.map)["map"]
    document = read(args.judgments)
    check_judgments(document, mapping)
    completed = {(r["task_id"], r["arm"]) for r in report["rows"] if r["status"] == "completed"}
    if {(m["task_id"], m["arm"]) for m in mapping.values()} != completed:
        raise JudgingIncomplete("blind_map_coverage")
    verdicts = document["final"]
    by_row = {(m["task_id"], m["arm"]): verdicts.get(b) for b, m in mapping.items()}
    files = {}
    for name, path in (("blind-map", args.map), ("success-judgments", args.judgments)):
        data = Path(path).read_bytes()
        relative = f"judging/{Path(path).name}"
        target = args.evidence / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[name] = dict(
            id=f"judging:{name}",
            path=relative,
            sha256=harness.sha256(data),
            bytes=len(data),
            provenance="observed",
        )
    apply_verdicts(report, by_row)
    # Idempotent: a rerun over an applied report replaces its judging entries.
    by_id = {e["id"]: e for e in report["evidence"]}
    by_id.update((e["id"], e) for e in files.values())
    report["evidence"] = sorted(by_id.values(), key=lambda e: e["id"].encode())
    report["summary"] = host_summary(report, tasks, freeze)
    args.output.write_bytes((json.dumps(report, ensure_ascii=False, indent=1) + "\n").encode())
    print(json.dumps(report["summary"]["arms"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("blind")
    for name in ("report", "tasks", "cases", "evidence", "items", "map", "map-digest"):
        p.add_argument("--" + name, type=Path, required=True)
    p = sub.add_parser("judge")
    for name in ("items", "output", "log", "ledger"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--template", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--cap", type=int, default=5_000_000)
    p.add_argument("--batch", type=int, default=6)
    p = sub.add_parser("adjudicate")
    for name in ("items", "a", "b", "output", "log", "ledger"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--cap", type=int, default=5_000_000)
    p.add_argument("--batch", type=int, default=6)
    p = sub.add_parser("apply")
    for name in ("report", "tasks", "freeze", "map", "judgments", "evidence", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    globals()["cmd_" + args.command](args)


if __name__ == "__main__":
    main()
