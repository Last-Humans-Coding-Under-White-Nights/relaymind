"""Machine drafting, labelling and success judging for the #259 early trial.

Spec 0.66 section 12.6.6 (`machine_drafted_owner_approved` prompts,
`machine_dual_owner_approved` labels and success judging; RM40, RM44; AC31,
AC36). Operator evidence tooling, not product code. Every model call runs at
temperature 0 against the owner's OpenAI-compatible endpoint with a recorded
template; the exact request and response bytes are kept under ``machine/calls/``
and the provider-reported usage is appended to a private ledger. The endpoint
URL and the key come from the environment and are never written.

Commands:

``draft-cases``   the drafter writes every planned prompt -> ``inputs/cases.json``
``annotate``      annotators A and B judge their own blind templates
``adjudicate``    the machine adjudicator completes the worksheet
``blind``         anonymize the observed rows; write the blind map privately and
                  its digest publicly (commit the digest before judging)
``judge``         judges A and B, then the adjudicator on disagreements, set
                  ``success`` in each observed row. Both judges must give one
                  JSON-boolean verdict per blind ID; every disagreement needs one
                  adjudication with a nonempty reason. Anything else is a closed
                  error (``success_verdicts`` or ``success_adjudication``) and no
                  row is written.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("RELAYMIND_REPO") or HERE.parents[3])
sys.path.insert(0, str(REPO / "src"))

from relaymind.canonical import canonical_digest, canonical_json  # noqa: E402

MACHINE = HERE / "machine"
CALLS = MACHINE / "calls"
ROLES = {
    "drafter": ("machine-drafter", "glm-5.3-flash", "drafter.md"),
    "annotator_a": ("machine-annotator-a", "glm-5.3-flash", "annotator-a.md"),
    "annotator_b": ("machine-annotator-b", "glm-4.7", "annotator-b.md"),
    "adjudicator": ("machine-adjudicator", "glm-5.3", "adjudicator.md"),
    "judge_a": ("machine-annotator-a", "glm-5.3-flash", "success-judge-a.md"),
    "judge_b": ("machine-annotator-b", "glm-4.7", "success-judge-b.md"),
    "success_adjudicator": ("machine-adjudicator", "glm-5.3", "success-adjudicator.md"),
}
# Arm-identifying strings replaced before success judging (spec 12.6.6).
_MARKERS = re.compile(
    r"relaymind[\w./:-]*|external-direct[\w./:-]*|glm-[\w.]+|chatcmpl-[\w-]+|run_[0-9a-f]{8,}",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(value) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _ledger() -> Path:
    return Path(os.environ["TRIAL_ROOT"]) / "labelling" / "usage.jsonl"


def call(role: str, name: str, payload: dict) -> dict:
    """One recorded temperature-0 call; returns the parsed JSON answer."""
    _, model, template = ROLES[role]
    system = (MACHINE / template).read_text(encoding="utf-8")
    request = _json(
        dict(
            model=model,
            temperature=0,
            messages=[
                dict(role="system", content=system),
                dict(role="user", content=canonical_json(payload)),
            ],
        )
    )
    base = os.environ["RELAYMIND_EXTERNAL_BASE_URL"].rstrip("/")
    http = urllib.request.Request(
        base + "/chat/completions",
        data=request,
        headers={
            "Authorization": "Bearer " + os.environ["RELAYMIND_EXTERNAL_API_KEY"],
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(http, timeout=300) as response:
        body = response.read()
    CALLS.mkdir(parents=True, exist_ok=True)
    (CALLS / f"{name}.request.json").write_bytes(request)
    (CALLS / f"{name}.response.json").write_bytes(body)
    answer = json.loads(body)
    usage = answer.get("usage") or {}
    _ledger().parent.mkdir(parents=True, exist_ok=True)
    with _ledger().open("a", encoding="utf-8") as ledger:
        ledger.write(
            json.dumps(
                dict(
                    name=name,
                    model=answer.get("model"),
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                )
            )
            + "\n"
        )
    text = answer["choices"][0]["message"]["content"]
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise SystemExit(f"{name}: no JSON object in the answer")
    return dict(value=json.loads(match.group(0)), model=answer.get("model"))


def _projections(catalog: dict) -> list[dict]:
    keys = ("asset_id", "kind", "description", "language_tags", "availability")
    return [
        dict({k: a["record"][k] for k in keys}, name=a["record"]["identity"]["package_id"])
        for a in catalog["assets"]
    ]


def draft_cases(args) -> None:
    catalog = json.loads((HERE / "inputs/catalog.json").read_text())
    plan = json.loads(args.plan.read_text())
    result = call("drafter", "drafting", dict(catalog=_projections(catalog), plan=plan))
    drafted = {c["case_id"]: c["task_text"] for c in result["value"]["cases"]}
    if set(drafted) != {p["case_id"] for p in plan}:
        raise SystemExit("the drafter did not answer every planned case")
    cases = []
    for p in plan:
        cases.append(
            dict(
                id=p["case_id"],
                split="held_out",
                task_family_ids=p["task_family_ids"],
                target_asset_family_ids=p["target_asset_family_ids"],
                lane=p["lane"],
                task_text=drafted[p["case_id"]],
                conversation=[],
                requested_kinds=p["requested_kinds"],
                language_tags=["en"],
                code_switched=False,
                author_ids=[ROLES["drafter"][0]],
                authorship="machine_drafted_owner_approved",
                authorship_evidence_ids=["drafting-request", "drafting-response"],
                derived_from_case_id=None,
                source_case_evidence_id=None,
            )
        )
    document = dict(
        schema_version=1,
        dataset_id=catalog["dataset_id"],
        release=1,
        cases=sorted(cases, key=lambda c: c["id"].encode()),
        split_review_evidence_ids=["split_review"],
    )
    (HERE / "inputs/cases.json").write_bytes(_json(document))
    print(json.dumps(dict(model=result["model"], cases=drafted)))


def annotate(args) -> None:
    catalog = json.loads((HERE / "inputs/catalog.json").read_text())
    for role, template in (("annotator_a", args.template_a), ("annotator_b", args.template_b)):
        blind = json.loads(template.read_text())
        result = call(role, "labels-" + role, dict(template=blind, catalog=_projections(catalog)))
        answers = {j["case_id"]: j for j in result["value"]["judgments"]}
        for item in blind["items"]:
            answer = answers[item["case_id"]]
            item["judgment"] = dict(
                recorded_at=_now(),
                acceptable_asset_ids=sorted(answer["acceptable_asset_ids"]),
                language_valid=answer["language_valid"],
                independent_case=answer["independent_case"],
                notes=answer.get("notes", ""),
                independence_evidence_ids=["independence"],
                **{"class": answer["class"]},
            )
        out = args.out / f"judged-{ROLES[role][0]}.json"
        out.write_bytes(_json(blind))
        print(json.dumps(dict(role=role, model=result["model"], answers=list(answers.values()))))


def adjudicate(args) -> None:
    catalog = json.loads((HERE / "inputs/catalog.json").read_text())
    worksheet = json.loads(args.worksheet.read_text())
    result = call(
        "adjudicator", "adjudication", dict(worksheet=worksheet, catalog=_projections(catalog))
    )
    finals = {a["case_id"]: a for a in result["value"]["adjudications"]}
    for item in worksheet["items"]:
        final = finals[item["case_id"]]
        item["adjudication"] = dict(
            adjudicator_id=ROLES["adjudicator"][0],
            recorded_at=_now(),
            acceptable_asset_ids=sorted(final["acceptable_asset_ids"]),
            language_valid=final["language_valid"],
            independent_case=final["independent_case"],
            disposition=final["disposition"],
            reason=final["reason"],
            evidence_ids=["adjudication"],
            **{"class": final["class"]},
        )
    args.worksheet.write_bytes(_json(worksheet))
    print(json.dumps(dict(model=result["model"], adjudications=list(finals.values()))))


def _transcript_view(path: Path) -> dict:
    """The request, host tool calls with results, and final answer, anonymized."""
    turns, request, answer = [], None, None
    for line in path.read_text(encoding="utf-8").splitlines():
        message = (json.loads(line)["event"].get("message")) or {}
        role, content = message.get("role"), message.get("content")
        parts = content if isinstance(content, list) else [dict(type="text", text=content)]
        for part in parts:
            if not isinstance(part, dict):
                continue
            if role == "user" and part.get("type") == "text" and request is None:
                request = part.get("text")
            elif role == "assistant" and part.get("type") == "toolCall":
                turns.append(dict(tool_call=part.get("name"), arguments=part.get("arguments")))
            elif role == "toolResult" or part.get("type") == "toolResult":
                text = part.get("text") or json.dumps(part.get("content"))
                turns.append(dict(tool_result=(text or "")[:6000]))
            elif role == "assistant" and part.get("type") == "text" and part.get("text"):
                answer = part.get("text")
    view = dict(request=request, host_steps=turns, final_answer=answer)
    return json.loads(_MARKERS.sub("<redacted>", json.dumps(view, ensure_ascii=False)))


def blind(args) -> None:
    observations = args.observations
    rows = []
    for path in sorted(observations.glob("row-*.json")):
        observation = json.loads(path.read_text())
        row = observation["row"]
        if row["status"] == "not_run":
            continue
        number = f"{observation['ordinal']:04d}"
        transcript = observations / "evidence" / number / "host-transcript.jsonl"
        blind_id = secrets.token_hex(8)
        rows.append(
            dict(
                blind_id=blind_id,
                task_id=observation["task_id"],
                ordinal=observation["ordinal"],
                arm=observation["arm"],
                view=_transcript_view(transcript),
            )
        )
    mapping = dict(
        schema_version=1,
        seed=args.seed,
        entries=sorted(
            (dict(blind_id=r["blind_id"], ordinal=r["ordinal"], arm=r["arm"]) for r in rows),
            key=lambda e: e["blind_id"],
        ),
    )
    order = sorted(
        rows,
        key=lambda r: hashlib.sha256(
            canonical_json(
                dict(seed=args.seed, task_id=r["task_id"], blind_id=r["blind_id"])
            ).encode("utf-8")
        ).hexdigest(),
    )
    packet = dict(
        transcripts=[dict(blind_id=r["blind_id"], transcript=r["view"]) for r in order],
    )
    args.private.mkdir(parents=True, exist_ok=True)
    (args.private / "blind-map.json").write_bytes(_json(mapping))
    (args.private / "blind-packet.json").write_bytes(_json(packet))
    (MACHINE / "blind-map.digest.json").write_bytes(
        _json(dict(schema_version=1, blind_map_sha256=canonical_digest(mapping)))
    )
    print(json.dumps(dict(transcripts=len(order), blind_map_digest=canonical_digest(mapping))))


def _verdict_map(verdicts, expected: list[str], error: str, *, reasoned: bool) -> dict:
    """Exactly one verdict per expected blind ID, ``success`` a JSON boolean."""
    if not isinstance(verdicts, list):
        raise SystemExit(error)
    out = {}
    for verdict in verdicts:
        if (
            not isinstance(verdict, dict)
            or not isinstance(verdict.get("blind_id"), str)
            or verdict["blind_id"] in out
            or type(verdict.get("success")) is not bool
            or not isinstance(verdict.get("reason"), str)
            or (reasoned and not verdict["reason"].strip())
        ):
            raise SystemExit(error)
        out[verdict["blind_id"]] = verdict
    if set(out) != set(expected):
        raise SystemExit(error)
    return out


def check_judges(blind_ids: list[str], judge_a, judge_b) -> tuple[dict, list[str]]:
    """Both blind judges cover every blind ID once with a boolean (spec 12.6.6).

    Returns the agreed finals and the sorted disagreeing blind IDs; a closed
    ``success_verdicts`` error otherwise.
    """
    if len(set(blind_ids)) != len(blind_ids):
        raise SystemExit("success_verdicts")
    a = _verdict_map(judge_a, blind_ids, "success_verdicts", reasoned=False)
    b = _verdict_map(judge_b, blind_ids, "success_verdicts", reasoned=False)
    disagree = sorted(i for i in blind_ids if a[i]["success"] != b[i]["success"])
    return {i: a[i] for i in blind_ids if i not in disagree}, disagree


def adjudicated(finals: dict, disagree: list[str], verdicts) -> dict:
    """Every disagreement, and only those, adjudicated once with a nonempty reason.

    Spec 12.6.6: a disagreement is never settled by either judge's verdict; an
    omitted, repeated, non-boolean or unexplained adjudication is a closed
    ``success_adjudication`` error and nothing is written.
    """
    chosen = _verdict_map(verdicts, disagree, "success_adjudication", reasoned=True)
    return dict(finals, **chosen)


def judge(args) -> None:
    packet = json.loads((args.private / "blind-packet.json").read_text())
    rubric = args.rubric.read_text(encoding="utf-8")
    payload = dict(rubric=rubric, request_date=args.request_date, **packet)
    blind_ids = [t["blind_id"] for t in packet["transcripts"]]
    answers = {}
    for role in ("judge_a", "judge_b"):
        result = call(role, "success-" + role, payload)
        answers[role] = result["value"].get("verdicts")
    finals, disagree = check_judges(blind_ids, answers["judge_a"], answers["judge_b"])
    raw = {role: {v["blind_id"]: v for v in answers[role]} for role in answers}
    if disagree:
        result = call(
            "success_adjudicator",
            "success-adjudication",
            dict(payload, raw={b: [raw["judge_a"][b], raw["judge_b"][b]] for b in disagree}),
        )
        finals = adjudicated(finals, disagree, result["value"].get("verdicts"))
    # Only now is the blind map opened.
    mapping = json.loads((args.private / "blind-map.json").read_text())
    recorded = json.loads((MACHINE / "blind-map.digest.json").read_text())
    if canonical_digest(mapping) != recorded["blind_map_sha256"]:
        raise SystemExit("the blind map changed after its digest was committed")
    if sorted(e["blind_id"] for e in mapping["entries"]) != sorted(finals):
        raise SystemExit("success_verdicts")
    (MACHINE / "blind-map.json").write_bytes(_json(mapping))
    verdicts = []
    for entry in mapping["entries"]:
        final = finals[entry["blind_id"]]
        path = args.observations / f"row-{entry['ordinal']:04d}.json"
        observation = json.loads(path.read_text())
        observation["row"]["success"] = final["success"]
        path.write_bytes(_json(observation))
        verdicts.append(
            dict(
                entry,
                judge_a=raw["judge_a"].get(entry["blind_id"]),
                judge_b=raw["judge_b"].get(entry["blind_id"]),
                final=final,
            )
        )
    (MACHINE / "success-verdicts.json").write_bytes(_json(dict(schema_version=1, rows=verdicts)))
    print(json.dumps(verdicts))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("draft-cases")
    d.add_argument("--plan", type=Path, required=True)
    a = sub.add_parser("annotate")
    a.add_argument("--template-a", type=Path, required=True)
    a.add_argument("--template-b", type=Path, required=True)
    a.add_argument("--out", type=Path, required=True)
    j = sub.add_parser("adjudicate")
    j.add_argument("--worksheet", type=Path, required=True)
    b = sub.add_parser("blind")
    b.add_argument("--observations", type=Path, required=True)
    b.add_argument("--private", type=Path, required=True)
    b.add_argument("--seed", type=int, required=True)
    s = sub.add_parser("judge")
    s.add_argument("--observations", type=Path, required=True)
    s.add_argument("--private", type=Path, required=True)
    s.add_argument("--rubric", type=Path, required=True)
    s.add_argument("--request-date", required=True)
    args = parser.parse_args(argv)
    dict(
        draft_cases=draft_cases, annotate=annotate, adjudicate=adjudicate, blind=blind, judge=judge
    )[args.command.replace("-", "_")](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
