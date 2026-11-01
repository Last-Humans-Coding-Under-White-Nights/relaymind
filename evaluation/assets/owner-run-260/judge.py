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

from relaymind.canonical import canonical_digest, canonical_json  # noqa: E402

_NAMES = re.compile(
    r"relay\s*mind|openclaw|glm[-\w.]*|zhipu|z\.ai|external-openai-compatible", re.IGNORECASE
)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


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
    write(args.map, dict(format="relaymind.owner_run_blind_map.v1", map=mapping))
    write(
        args.map_digest,
        dict(
            format="relaymind.owner_run_blind_map_digest.v1",
            map_canonical_sha256=canonical_digest(read(args.map)),
            items=len(items),
        ),
    )
    print(json.dumps(dict(items=len(items))))


def cmd_judge(args) -> None:
    items = read(args.items)["items"]
    template = (HERE / "prompts" / args.template).read_text(encoding="utf-8")
    ledger = harness.Ledger(args.ledger, args.cap)
    out = read(args.output) if args.output.exists() else {}
    todo = [i for i in items if i["blind_id"] not in out]
    for start in range(0, len(todo), args.batch):
        batch = todo[start : start + args.batch]
        request = [{k: i[k] for k in ("blind_id", "request", "rubric", "answer")} for i in batch]
        messages = [
            dict(role="system", content=template),
            dict(role="user", content=json.dumps(request, ensure_ascii=False)),
        ]
        name = f"{args.name}-{start // args.batch + 1:03d}"
        answer = harness.chat(ledger, args.name, messages, args.log, name, 30000)
        try:
            rows = harness.answer_json(answer)["judgments"]
        except (ValueError, KeyError, TypeError):
            print("unparsed", name, file=sys.stderr)
            continue
        for row in rows:
            if row.get("blind_id") in {i["blind_id"] for i in batch} and isinstance(
                row.get("success"), bool
            ):
                out[row["blind_id"]] = dict(success=row["success"], notes=str(row.get("notes")))
        write(args.output, out)
        print(json.dumps(dict(done=len(out), ledger=ledger.total)))


def cmd_adjudicate(args) -> None:
    items = {i["blind_id"]: i for i in read(args.items)["items"]}
    a, b = read(args.a), read(args.b)
    final = {}
    disputed = []
    for blind_id in items:
        if blind_id in a and blind_id in b and a[blind_id]["success"] == b[blind_id]["success"]:
            final[blind_id] = dict(
                success=a[blind_id]["success"],
                reason="Both judges agree; the adjudication rule adopts the verdict.",
            )
        else:
            disputed.append(blind_id)
    ledger = harness.Ledger(args.ledger, args.cap)
    template = (HERE / "prompts" / "judge-adjudicator.txt").read_text(encoding="utf-8")
    for start in range(0, len(disputed), args.batch):
        batch = disputed[start : start + args.batch]
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
        name = f"judge-adjudicator-{start // args.batch + 1:03d}"
        answer = harness.chat(ledger, "judge-adjudicator", messages, args.log, name, 30000)
        for row in harness.answer_json(answer)["adjudications"]:
            if row.get("blind_id") in batch and isinstance(row.get("success"), bool):
                final[row["blind_id"]] = dict(success=row["success"], reason=str(row["reason"]))
    write(
        args.output,
        dict(
            format="relaymind.owner_run_success_judgments.v1",
            judges=dict(a=a, b=b),
            disputed=sorted(disputed),
            final=final,
        ),
    )
    agree = sum(1 for i in items if i in a and i in b and a[i]["success"] == b[i]["success"])
    print(json.dumps(dict(items=len(items), agree=agree, disputed=len(disputed))))


def cmd_apply(args) -> None:
    from relaymind.evaluation.asset_whole_task import host_summary

    report, tasks, freeze = read(args.report), read(args.tasks), read(args.freeze)
    mapping = read(args.map)["map"]
    verdicts = read(args.judgments)["final"]
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
            sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data),
            provenance="observed",
        )
    for row in report["rows"]:
        if row["status"] == "completed":
            verdict = by_row.get((row["task_id"], row["arm"]))
            if verdict is not None:
                row["success"] = verdict["success"]
                row["evidence_ids"] = sorted(
                    {*row["evidence_ids"], "judging:blind-map", "judging:success-judgments"},
                    key=str.encode,
                )
        elif row["status"] in ("failed", "timeout"):
            row["success"] = False
    report["evidence"] = sorted(
        [*report["evidence"], *files.values()], key=lambda e: e["id"].encode()
    )
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
