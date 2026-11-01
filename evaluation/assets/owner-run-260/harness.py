"""Owner-run cohort harness for #260 (spec 0.66 sections 12.6.1, 12.6.5, 12.6.6).

RM40, RM42, RM44; AC31, AC33, AC36. Builds the machine-drafted, machine-dual
labelled release that the owner approves by pull request review:

* ``catalog``   derives the release catalog from the built development catalog;
* ``plan``      picks the targets and lanes deterministically from a seed;
* ``draft``     asks the pinned drafting model for the prompts and rubrics;
* ``cases``     assembles AssetCasesV1, the episodes and the host task stubs;
* ``annotate``  fills one blind label template with one machine annotator;
* ``adjudicate`` fills the adjudication worksheet (the third machine pass);
* ``lock``      writes the frozen AssetLockV1.

Model calls read ``RELAYMIND_EXTERNAL_API_KEY`` and ``RELAYMIND_EXTERNAL_BASE_URL``
from the environment of this process only. Neither value is printed, logged or
written; request/response logs keep only the request body (model, messages,
sampling, output bound) and the response body. Every attempt, retries included,
is reserved durably in a token ledger at its request bound (body bytes plus
``max_tokens``) before it is sent, and refused when the ledger would pass its
cap; the reservation is settled to the provider-reported usage, and stays
charged when the consumption is unknown.
"""

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))

from relaymind.canonical import canonical_digest  # noqa: E402

PROMPTS = HERE / "prompts"
DATASET = "relaymind-owner-run-260"
RELEASE = 1
MODEL = "glm-5.3"
SAMPLING = dict(temperature=0, reasoning_effort="low")
FAMILIES = {
    "matrix": "source-family:matrix",
    "skillsbench": "source-family:skillsbench",
    "github-mcp-server": "source-family:github-mcp-server",
}
PEOPLE = {
    "owner": ("custodian", "tuner"),
    "machine-drafter": ("author",),
    "machine-annotator-a": ("annotator",),
    "machine-annotator-b": ("annotator",),
    "machine-adjudicator": ("adjudicator",),
}
TEMPLATES = {
    "machine-drafter": "drafter.txt",
    "machine-annotator-a": "annotator-a.txt",
    "machine-annotator-b": "annotator-b.txt",
    "machine-adjudicator": "adjudicator.txt",
}
LANES = ("code_switch", "en_to_en", "en_to_zh_hans", "zh_hans_to_en", "zh_hans_to_zh_hans")
TAGS = {
    "code_switch": ["en", "zh-Hans"],
    "en_to_en": ["en"],
    "en_to_zh_hans": ["en"],
    "zh_hans_to_en": ["zh-Hans"],
    "zh_hans_to_zh_hans": ["zh-Hans"],
}


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1) + "\n"


def write(path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(value), encoding="utf-8")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# --- the model client and its token ledger ------------------------------------


# The tokens a request can use beyond its body bytes: chat template and role
# markers. A byte-level tokenizer token covers at least one byte of the body.
REQUEST_OVERHEAD = 1024
# The output bound sent with every call (completion and reasoning tokens).
MAX_OUTPUT = 32_768
ATTEMPTS = 4


def request_bound(body: bytes, max_output: int) -> int:
    """The most tokens one request can consume: its whole body and its output bound."""
    return len(body) + REQUEST_OVERHEAD + max_output


def durable_write(path, value) -> None:
    """Replace the file atomically and flush it to disk before returning."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(dump(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def open_ledger(path: Path, held: str) -> dict:
    """The ledger document at ``path`` (a fresh one at zero), with its ``held`` table."""
    data = read(path) if path.exists() else dict(total_input=0, total_output=0, calls=0, stages={})
    data.setdefault(held, {})
    return data


def ledger_total(data: dict, held: str) -> int:
    """Spent tokens plus every ``held`` reservation that is not settled."""
    pending = sum(r["amount"] for r in data[held].values() if r["state"] != "settled")
    return data["total_input"] + data["total_output"] + data.get("unlogged_estimate", 0) + pending


class Ledger:
    """The external-token ledger.

    Every attempt is reserved durably, at its request bound, before it is sent.
    A reservation is settled to the provider-reported usage once; until then,
    and for good when the consumption is unknown (a timeout, a lost or
    unreadable response, a crash), it stays charged at its bound.
    """

    def __init__(self, path: Path, cap: int):
        self.path, self.cap = Path(path), cap
        self.data = open_ledger(self.path, "reservations")

    @property
    def total(self) -> int:
        return ledger_total(self.data, "reservations")

    def check(self, estimate: int) -> None:
        if self.total + estimate > self.cap:
            raise SystemExit(f"budget_cap: ledger {self.total} + estimate {estimate} > {self.cap}")

    def reserve(self, stage: str, name: str, amount: int) -> str:
        """Hold ``amount`` before one attempt is sent; refuse it past the cap."""
        self.check(amount)
        entry = f"{len(self.data['reservations']) + 1:06d}"
        self.data["reservations"][entry] = dict(
            stage=stage, name=name, amount=amount, state="reserved"
        )
        durable_write(self.path, self.data)
        return entry

    def unknown(self, entry: str) -> None:
        """The attempt's consumption is unknown: it stays charged at its bound."""
        if self.data["reservations"][entry]["state"] == "reserved":
            self.data["reservations"][entry]["state"] = "unknown"
            durable_write(self.path, self.data)

    def settle(self, entry: str, usage: dict) -> None:
        """Replace the reservation by the reported usage, once."""
        held = self.data["reservations"][entry]
        if held["state"] == "settled":
            return
        if not isinstance(usage.get("prompt_tokens"), int):
            self.unknown(entry)
            return
        prompt, completion = usage["prompt_tokens"], usage.get("completion_tokens") or 0
        self.data["total_input"] += prompt
        self.data["total_output"] += completion
        self.data["calls"] += 1
        row = self.data["stages"].setdefault(held["stage"], {})
        for key in ("input", "output", "reasoning", "calls"):
            row.setdefault(key, 0)
        row["input"] += prompt
        row["output"] += completion
        row["reasoning"] += (usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens"
        ) or 0
        row["calls"] += 1
        held.update(state="settled", charged=prompt + completion)
        durable_write(self.path, self.data)


def chat(
    ledger: Ledger,
    stage: str,
    messages: list,
    log_dir: Path,
    name: str,
    *,
    max_output: int = MAX_OUTPUT,
):
    """One temperature-0 call; the request and response bodies are logged, never secrets.

    The output is bounded by ``max_tokens``. Each attempt, retries included, is
    reserved at its request bound before it is sent and refused past the cap;
    an attempt whose consumption is unknown keeps its reservation charged.
    """
    body = dict(model=MODEL, messages=messages, **SAMPLING, max_tokens=max_output)
    payload = json.dumps(body).encode("utf-8")
    bound = request_bound(payload, max_output)
    request = urllib.request.Request(
        os.environ["RELAYMIND_EXTERNAL_BASE_URL"].rstrip("/") + "/chat/completions",
        data=payload,
        headers={
            "Authorization": "Bearer " + os.environ["RELAYMIND_EXTERNAL_API_KEY"],
            "Content-Type": "application/json",
        },
    )
    for attempt in range(ATTEMPTS):
        entry = ledger.reserve(stage, name, bound)
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                raw = response.read()
            break
        except (urllib.error.URLError, OSError) as exc:
            ledger.unknown(entry)
            code = getattr(exc, "code", None)
            if attempt == ATTEMPTS - 1 or (code is not None and code < 500 and code != 429):
                raise SystemExit(f"provider_error: {type(exc).__name__} {code}") from None
            time.sleep(10 * (attempt + 1))
    try:
        answer = json.loads(raw)
    except ValueError:
        ledger.unknown(entry)
        raise SystemExit("provider_error: unparsed_response") from None
    ledger.settle(entry, (answer.get("usage") if isinstance(answer, dict) else None) or {})
    log_dir.mkdir(parents=True, exist_ok=True)
    base, attempt = name, 1
    while (log_dir / f"{name}.response.json").exists():
        attempt += 1
        name = f"{base}.attempt{attempt}"
    (log_dir / f"{name}.request.json").write_bytes(payload)
    (log_dir / f"{name}.response.json").write_bytes(raw)
    return answer


def answer_json(answer: dict):
    """The JSON answer; one known slip (a stray quote after an array) is repaired."""
    text = answer["choices"][0]["message"]["content"] or ""
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    text = match.group(1) if match else text
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(re.sub(r'\]"\s*}', "]}", text))


# --- catalog ----------------------------------------------------------------------


def evidence_entry(evidence_id, kind, relative, payload) -> dict:
    return dict(
        id=evidence_id,
        kind=kind,
        access="redistributable",
        path=relative,
        sha256=sha256(payload),
        bytes=len(payload),
    )


def cmd_catalog(args) -> None:
    """The release catalog: the built assets, coarse source families and the people."""
    built = read(args.built / "catalog.json")
    root = args.root
    if root.exists():
        raise SystemExit("root_exists")
    shutil.copytree(args.built / "originals", root / "originals")
    catalog = dict(built, dataset_id=DATASET, release=RELEASE)
    for asset in catalog["assets"]:
        asset["asset_family_ids"] = [FAMILIES[asset["record"]["source_id"]]]
    evidence = list(catalog["evidence"])
    people = []
    for person, roles in PEOPLE.items():
        ids = []
        if person in TEMPLATES:
            payload = (PROMPTS / TEMPLATES[person]).read_bytes()
            kind = {
                "author": "authorship",
                "annotator": "independence",
                "adjudicator": "adjudication",
            }[roles[0]]
            evidence_id = f"{kind}:{person}-template"
            evidence.append(
                evidence_entry(evidence_id, kind, f"prompts/{TEMPLATES[person]}", payload)
            )
            ids.append(evidence_id)
        else:
            payload = (HERE / "PROTOCOL.md").read_bytes()
            evidence.append(
                evidence_entry("access_control:protocol", "access_control", "PROTOCOL.md", payload)
            )
            ids.append("access_control:protocol")
        people.append(dict(id=person, roles=sorted(roles), competencies=[], evidence_ids=ids))
    catalog["people"] = sorted(people, key=lambda p: p["id"].encode())
    catalog["evidence"] = sorted(evidence, key=lambda e: e["id"].encode())
    write(root / "catalog.json", catalog)
    (root / "prompts").mkdir(exist_ok=True)
    for name in TEMPLATES.values():
        shutil.copy(PROMPTS / name, root / "prompts" / name)
    shutil.copy(HERE / "PROTOCOL.md", root / "PROTOCOL.md")
    print(json.dumps(dict(assets=len(catalog["assets"]), people=len(people))))


def projection(asset: dict, limit: int) -> str:
    record = asset["record"]
    identity = record["identity"]
    name = identity.get("package_id") or "/".join(
        filter(None, (identity.get("server_id"), identity.get("tool_name")))
    )
    text = " ".join(record["description"].split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return f"[{record['kind']}] {name} — {text}"


def handles(catalog: dict) -> dict[str, str]:
    ids = sorted(a["record"]["asset_id"] for a in catalog["assets"])
    return {asset_id: f"A{n:03d}" for n, asset_id in enumerate(ids, 1)}


def catalog_listing(catalog: dict, limit: int = 140) -> str:
    names = handles(catalog)
    rows = sorted(catalog["assets"], key=lambda a: names[a["record"]["asset_id"]])
    return "\n".join(f"{names[a['record']['asset_id']]} {projection(a, limit)}" for a in rows)


# --- plan and draft -------------------------------------------------------------------


def cmd_plan(args) -> None:
    """Deterministic targets: 20 host tasks per lane and 100 episode intents."""
    catalog = read(args.root / "catalog.json")
    rng = random.Random(args.seed)
    assets = sorted(catalog["assets"], key=lambda a: a["record"]["asset_id"])
    zh = [a for a in assets if "zh-Hans" in a["record"]["language_tags"]]
    en = [a for a in assets if "zh-Hans" not in a["record"]["language_tags"]]
    rng.shuffle(zh)
    rng.shuffle(en)
    items = []

    def take(pool):
        return pool.pop()["record"]["asset_id"]

    host_lanes = [
        ("zh_hans_to_zh_hans", zh),
        ("en_to_zh_hans", zh),
        ("zh_hans_to_en", en),
        ("en_to_en", en),
    ]
    for lane, pool in host_lanes:
        for _ in range(20):
            items.append(dict(cohort="host", lane=lane, target=take(pool)))
    for n in range(20):
        items.append(dict(cohort="host", lane="code_switch", target=take(zh if n < 10 else en)))
    episode_lanes = ("en_to_en", "zh_hans_to_en", "code_switch")
    for n in range(100):
        if zh:
            lane = ("zh_hans_to_zh_hans", "en_to_zh_hans")[n % 2]
            target = take(zh)
        else:
            lane, target = episode_lanes[n % 3], take(en)
        items.append(dict(cohort="episode", lane=lane, target=target))
    for n, item in enumerate(items):
        prefix = "h" if item["cohort"] == "host" else "e"
        item["item_id"] = f"{prefix}{n % 100 + 1:03d}" if prefix == "h" else f"e{n - 99:03d}"
    write(args.output, dict(seed=args.seed, items=items))
    print(json.dumps(dict(items=len(items), zh_left=len(zh), en_left=len(en))))


def prompt_count(item: dict) -> int:
    """One prompt for a host task, five (three corrections, two paraphrases) for an episode."""
    return 1 if item["cohort"] == "host" else 5


LANE_TEXT = {
    "zh_hans_to_zh_hans": "Simplified Chinese",
    "zh_hans_to_en": "Simplified Chinese",
    "en_to_en": "English",
    "en_to_zh_hans": "English",
    "code_switch": "natural Chinese-English code-switching (a Simplified Chinese sentence "
    "with some English words or terms, as bilingual developers write)",
}


def cmd_draft(args) -> None:
    catalog = read(args.root / "catalog.json")
    by_id = {a["record"]["asset_id"]: a for a in catalog["assets"]}
    plan = read(args.plan)
    template = (PROMPTS / "drafter.txt").read_text(encoding="utf-8")
    ledger = Ledger(args.ledger, args.cap)
    out = read(args.output) if args.output.exists() else {}
    items = [i for i in plan["items"] if i["item_id"] not in out]
    for start in range(0, len(items), args.batch):
        batch = items[start : start + args.batch]
        lines = []
        for item in batch:
            lines.append(
                json.dumps(
                    dict(
                        item_id=item["item_id"],
                        kind="host_task" if item["cohort"] == "host" else "episode",
                        prompts=prompt_count(item),
                        language=LANE_TEXT[item["lane"]],
                        capability=projection(by_id[item["target"]], 700),
                    ),
                    ensure_ascii=False,
                )
            )
        messages = [
            dict(role="system", content=template),
            dict(role="user", content="\n".join(lines)),
        ]
        name = f"draft-{batch[0]['item_id']}-{batch[-1]['item_id']}"
        answer = chat(ledger, "draft", messages, args.log, name)
        try:
            result = {row["item_id"]: row for row in answer_json(answer)["items"]}
        except (ValueError, KeyError, TypeError):
            print("unparsed", name, file=sys.stderr)
            continue
        for item in batch:
            row = result.get(item["item_id"])
            if row is None or len(row.get("prompts", [])) != prompt_count(item):
                print("incomplete", item["item_id"], file=sys.stderr)
                continue
            out[item["item_id"]] = dict(row, log=name)
        write(args.output, out)
        print(json.dumps(dict(done=len(out), ledger=ledger.total)))


# --- cases, episodes, host task stubs ---------------------------------------------------


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text).strip()


def cmd_cases(args) -> None:
    catalog = read(args.root / "catalog.json")
    plan = read(args.plan)
    drafts = read(args.drafts)
    drafting_log = args.drafting_log.read_bytes()
    authorship_id = "authorship:drafting-log"
    families = sorted(set(FAMILIES.values()))
    cases = []
    episodes = []
    host = []
    seen = set()
    for item in plan["items"]:
        draft = drafts[item["item_id"]]
        texts = [nfc(t) for t in draft["prompts"]]
        folded = [t.casefold() for t in texts]
        # Unique across items and within the item (a repeat would join two cases).
        if not all(texts) or len(set(folded)) != len(folded) or seen.intersection(folded):
            raise SystemExit(f"duplicate_prompt {item['item_id']}")
        seen.update(folded)
        ids = []
        for n, text in enumerate(texts):
            case_id = (
                f"{item['item_id']}-prompt"
                if item["cohort"] == "host"
                else f"{item['item_id']}-{('c1', 'c2', 'c3', 'p1', 'p2')[n]}"
            )
            ids.append(case_id)
            cases.append(
                dict(
                    id=case_id,
                    split="held_out",
                    task_family_ids=[f"intent:{item['item_id']}"],
                    target_asset_family_ids=families,
                    lane=item["lane"],
                    task_text=text,
                    conversation=[],
                    requested_kinds=["mcp_tool", "skill"],
                    language_tags=TAGS[item["lane"]],
                    code_switched=item["lane"] == "code_switch",
                    author_ids=["machine-drafter"],
                    authorship="machine_drafted_owner_approved",
                    authorship_evidence_ids=[authorship_id],
                    derived_from_case_id=None,
                    source_case_evidence_id=None,
                )
            )
        if item["cohort"] == "host":
            host.append(dict(item, case_id=ids[0], rubric=draft["rubric"]))
        else:
            episodes.append(dict(item, case_ids=ids))
    # Controls and reset probes are host prompts of other intents, picked by seed.
    rng = random.Random(plan["seed"] + 1)
    host_ids = [h["case_id"] for h in host]
    for episode in episodes:
        others = [c for c in host_ids if c != episode["case_ids"][0]]
        control, reset = rng.sample(others, 2)
        episode.update(control=control, reset=reset)
    cases.sort(key=lambda c: c["id"].encode())
    document = dict(
        schema_version=1,
        dataset_id=DATASET,
        release=RELEASE,
        cases=cases,
        split_review_evidence_ids=["split_review:protocol"],
    )
    write(args.root / "cases.json", document)
    write(args.root / "cohort-plan.json", dict(host=host, episodes=episodes))
    # The drafting log and split review are catalog evidence.
    own = (authorship_id, "split_review:protocol", "derivation:cohort-plan")
    catalog["evidence"] = [e for e in catalog["evidence"] if e["id"] not in own]
    plan_bytes = (args.root / "cohort-plan.json").read_bytes()
    catalog["evidence"].append(
        evidence_entry("derivation:cohort-plan", "derivation", "cohort-plan.json", plan_bytes)
    )
    (args.root / "drafting-log.json").write_bytes(drafting_log)
    catalog["evidence"].append(
        dict(
            evidence_entry(authorship_id, "authorship", "drafting-log.json", drafting_log),
            access="restricted",
        )
    )
    protocol = (args.root / "PROTOCOL.md").read_bytes()
    catalog["evidence"].append(
        evidence_entry("split_review:protocol", "split_review", "PROTOCOL.md", protocol)
    )
    catalog["evidence"].sort(key=lambda e: e["id"].encode())
    drafter = next(p for p in catalog["people"] if p["id"] == "machine-drafter")
    drafter["evidence_ids"] = sorted({*drafter["evidence_ids"], authorship_id})
    write(args.root / "catalog.json", catalog)
    print(json.dumps(dict(cases=len(cases), host=len(host), episodes=len(episodes))))


# --- annotation and adjudication ---------------------------------------------------------


def _classes(row: dict, names: dict) -> dict:
    back = {v: k for k, v in names.items()}
    # Accept omitted zero padding only for an existing numeric catalog handle.
    # The original spelling remains in the response bytes; no asset is guessed.
    for handle, asset_id in list(back.items()):
        if re.fullmatch(r"A[0-9]+", handle):
            back.setdefault(f"A{int(handle[1:])}", asset_id)
    label = row.get("class")
    if label not in ("positive", "negative", "ambiguous"):
        raise ValueError("class")
    given = row.get("acceptable")
    if not isinstance(given, list):
        raise ValueError("acceptable")
    if label == "negative":
        if given:
            raise ValueError("acceptable")
        chosen = []
    elif not given or any(not isinstance(h, str) or h not in back for h in given):
        # An unknown or missing handle leaves the item for retry; the judgment is
        # never changed to negative or narrowed to the handles that resolve.
        raise ValueError("acceptable")
    else:
        chosen = sorted({back[h] for h in given})
    for field in ("language_valid", "independent_case"):
        if not isinstance(row.get(field), bool):
            raise ValueError(field)
    notes = row.get("notes", "")
    if not isinstance(notes, str):
        raise ValueError("notes")
    return dict(
        cls=label,
        acceptable=chosen,
        language_valid=row["language_valid"],
        independent_case=row["independent_case"],
        notes=notes,
    )


def cmd_annotate(args) -> None:
    catalog = read(args.root / "catalog.json")
    names = handles(catalog)
    template = read(args.template)
    annotator = template["annotator_id"]
    prompt = (PROMPTS / TEMPLATES[annotator]).read_text(encoding="utf-8")
    listing = catalog_listing(catalog)
    ledger = Ledger(args.ledger, args.cap)
    out = read(args.output) if args.output.exists() else template
    todo = [i for i in out["items"] if i["judgment"] is None]
    # Each annotator sees the cases in its own seeded order, never grouped by intent.
    random.Random(sha256(annotator.encode())).shuffle(todo)
    evidence = [f"independence:{annotator}-template"]
    for start in range(0, len(todo), args.batch):
        if args.max_batches and start // args.batch >= args.max_batches:
            break
        batch = todo[start : start + args.batch]
        request = [
            dict(
                case_id=i["case_id"],
                lane=i["lane"],
                request=i["task_text"],
                language_tags=i["language_tags"],
            )
            for i in batch
        ]
        messages = [
            dict(role="system", content=prompt + "\n\nCATALOG\n" + listing),
            dict(role="user", content=json.dumps(request, ensure_ascii=False)),
        ]
        name = f"{annotator}-{start // args.batch + 1:03d}"
        answer = chat(ledger, annotator, messages, args.log, name)
        try:
            rows = {r["case_id"]: r for r in answer_json(answer)["judgments"]}
        except (ValueError, KeyError, TypeError):
            print("unparsed", name, file=sys.stderr)
            continue
        for item in batch:
            row = rows.get(item["case_id"])
            if row is None:
                continue
            try:
                value = _classes(row, names)
            except ValueError:
                continue
            item["judgment"] = dict(
                recorded_at=now(),
                acceptable_asset_ids=value["acceptable"],
                language_valid=value["language_valid"],
                independent_case=value["independent_case"],
                notes=value["notes"],
                independence_evidence_ids=evidence,
                **{"class": value["cls"]},
            )
        write(args.output, out)
        left = sum(i["judgment"] is None for i in out["items"])
        print(json.dumps(dict(batch=name, left=left, ledger=ledger.total)))


def cmd_adjudicate(args) -> None:
    catalog = read(args.root / "catalog.json")
    names = handles(catalog)
    worksheet = read(args.worksheet)
    out = read(args.output) if args.output.exists() else worksheet
    prompt = (PROMPTS / "adjudicator.txt").read_text(encoding="utf-8")
    listing = catalog_listing(catalog)
    ledger = Ledger(args.ledger, args.cap)
    evidence = ["adjudication:machine-adjudicator-template"]

    def final(item, value, reason):
        include = value["language_valid"] and value["independent_case"]
        item["adjudication"] = dict(
            adjudicator_id="machine-adjudicator",
            recorded_at=now(),
            acceptable_asset_ids=value["acceptable"],
            language_valid=value["language_valid"],
            independent_case=value["independent_case"],
            disposition="include" if include else "exclude",
            reason=reason,
            evidence_ids=evidence,
            **{"class": value["cls"]},
        )

    for item in out["items"]:
        if item["adjudication"] is None and item["agrees"]["joint"]:
            left = item["judgments"][0]
            final(
                item,
                dict(
                    cls=left["class"],
                    acceptable=left["acceptable_asset_ids"],
                    language_valid=left["language_valid"],
                    independent_case=left["independent_case"],
                ),
                "Both raw judgments agree on every answer; the adjudication rule adopts them.",
            )
    todo = [i for i in out["items"] if i["adjudication"] is None]
    cases = {c["id"]: c for c in read(args.root / "cases.json")["cases"]}
    for start in range(0, len(todo), args.batch):
        batch = todo[start : start + args.batch]
        request = []
        for item in batch:
            case = cases[item["case_id"]]
            request.append(
                dict(
                    case_id=item["case_id"],
                    lane=case["lane"],
                    request=case["task_text"],
                    judgments=[
                        {
                            "class": j["class"],
                            "acceptable": [names[a] for a in j["acceptable_asset_ids"]],
                            "language_valid": j["language_valid"],
                            "independent_case": j["independent_case"],
                            "notes": j["notes"],
                        }
                        for j in item["judgments"]
                    ],
                )
            )
        messages = [
            dict(role="system", content=prompt + "\n\nCATALOG\n" + listing),
            dict(role="user", content=json.dumps(request, ensure_ascii=False)),
        ]
        name = f"machine-adjudicator-{start // args.batch + 1:03d}"
        answer = chat(ledger, "machine-adjudicator", messages, args.log, name)
        try:
            rows = {r["case_id"]: r for r in answer_json(answer)["adjudications"]}
        except (ValueError, KeyError, TypeError):
            print("unparsed", name, file=sys.stderr)
            continue
        for item in batch:
            row = rows.get(item["case_id"])
            if row is None or not str(row.get("reason", "")).strip():
                continue
            try:
                value = _classes(row, names)
            except ValueError:
                continue
            final(item, value, str(row["reason"]).strip()[:2000])
        write(args.output, out)
        left = sum(i["adjudication"] is None for i in out["items"])
        print(json.dumps(dict(batch=name, left=left, ledger=ledger.total)))
    write(args.output, out)


# --- lock ---------------------------------------------------------------------------------


def cmd_lock(args) -> None:
    root = args.root
    catalog, cases, labels = (read(root / f"{n}.json") for n in ("catalog", "cases", "labels"))
    lock = dict(
        schema_version=1,
        dataset_id=DATASET,
        release=RELEASE,
        purpose=catalog["purpose"],
        state="frozen",
        frozen_at=args.frozen_at,
        catalog=dict(path="catalog.json", digest=canonical_digest(catalog)),
        cases=dict(path="cases.json", digest=canonical_digest(cases)),
        labels=dict(path="labels.json", digest=canonical_digest(labels)),
        related_locks=[],
        evidence=[
            dict(id=e["id"], sha256=e["sha256"], bytes=e["bytes"]) for e in catalog["evidence"]
        ],
        derivation=dict(
            revision=args.revision,
            config_digest=canonical_digest(read(root / "cohort-plan.json")),
            evidence_ids=["derivation:cohort-plan"],
        ),
        unicode_version=unicodedata.unidata_version,
        qualification_evidence_ids=["access_control:protocol"],
    )
    lock["digest"] = canonical_digest(lock)
    write(root / "lock.json", lock)
    print(json.dumps(dict(lock=lock["digest"])))


def cmd_log(args) -> None:
    """Private drafting evidence holding original request and response bytes.

    UTF-8 strings preserve the exact JSON body bytes, alongside their hashes.
    Requests contain restricted catalog excerpts: keep this log private. The
    corpus registers it as restricted evidence for the offline resolver.
    """
    template = (PROMPTS / args.template).read_bytes()
    calls = []
    args.copy_to.mkdir(parents=True, exist_ok=True)
    for request in sorted(args.log.glob("*.request.json")):
        name = request.name.removesuffix(".request.json")
        response = args.log / f"{name}.response.json"
        payload = response.read_bytes()
        sent = request.read_bytes()
        body = json.loads(payload)
        served = body.get("model")
        created = datetime.fromtimestamp(body.get("created", 0), UTC).strftime("%Y-%m-%d")
        (args.copy_to / response.name).write_bytes(payload)
        calls.append(
            dict(
                name=name,
                request_sha256=sha256(sent),
                request_bytes=len(sent),
                request_utf8=sent.decode("utf-8"),
                response_sha256=sha256(payload),
                response_bytes=len(payload),
                response_utf8=payload.decode("utf-8"),
                served_model=served,
                served_on=created,
            )
        )
    write(
        args.output,
        dict(
            format="relaymind.owner_run_call_log.v1",
            role=args.role,
            model_id=MODEL,
            sampling=SAMPLING,
            template=args.template,
            template_sha256=sha256(template),
            served_models=sorted({c["served_model"] for c in calls}),
            dates=sorted({c["served_on"] for c in calls}),
            calls=calls,
            rejected=[
                dict(zip(("item", "reason", "response"), entry.split(":"), strict=True))
                for argument in args.rejected or []
                for entry in argument.split()
            ],
        ),
    )
    print(json.dumps(dict(calls=len(calls))))


HOST_MODEL = "glm-5.3-flash"
PROVIDER = "external-openai-compatible"
OMITTED = "omitted (secret, never recorded)"


def host_configs(catalog: dict) -> dict[str, dict]:
    """The execution-effective documents each arm uses; secrets are omitted, never hashed."""
    model = dict(id=HOST_MODEL, name=HOST_MODEL, reasoning=False, input=["text"])
    native = {
        "models": {
            "providers": {
                "external": {
                    "api": "openai-completions",
                    "models": [model],
                    "omitted_secret_fields": ["apiKey", "baseUrl"],
                }
            }
        },
        "tools": {"exec": {"mode": "deny"}},
    }
    plugin = {"baseUrl": "http://127.0.0.1:8087/v1", "executionMode": "external_only"}
    provider_only = {
        "plugins": {"entries": {"relaymind": {"enabled": True, "config": plugin}}},
        "tools": {"exec": {"mode": "deny"}},
    }
    recommendations = {
        "plugins": {
            "entries": {
                "relaymind": {
                    "enabled": True,
                    "config": dict(
                        plugin,
                        recommendations=dict(
                            bindings=[
                                dict(
                                    agentId="main",
                                    userId="owner",
                                    workspaceId="owner-run-260",
                                    omitted_secret_fields=["actionTokenEnv"],
                                )
                            ],
                            skills=[],
                        ),
                    ),
                }
            }
        },
        "tools": {"exec": {"mode": "deny"}},
    }
    return {
        "provider": dict(
            provider=PROVIDER,
            api="openai-completions",
            model=HOST_MODEL,
            omitted_secret_fields=["api_key", "base_url"],
        ),
        "sampling": dict(
            source="pinned host and gateway defaults; no sampling override is configured",
            temperature=None,
            reasoning=False,
        ),
        "host-native": native,
        "host-provider_only": provider_only,
        "host-recommendations": recommendations,
        "recommendations": dict(
            enabled=True,
            user_id="owner",
            workspace_id="owner-run-260",
            total_ms=500,
            primary_ms=150,
            fallback=True,
            primary="lexical",
            catalog_dataset=catalog["dataset_id"],
        ),
        "budget": dict(
            format="relaymind.owner_run_task_budget.v1",
            host_turn_timeout_s=600,
            retries=0,
            note="the pinned host's own per-turn limits apply unchanged",
        ),
    }


def cmd_host(args) -> None:
    """HostFreezeV1 and AssetHostTasksV1 over the frozen release (spec 12.6.5)."""
    root, out = args.root, args.output
    catalog = read(root / "catalog.json")
    lock = read(root / "lock.json")
    plan = read(root / "cohort-plan.json")
    records = {a["record"]["asset_id"]: a["record"] for a in catalog["assets"]}
    configs = host_configs(catalog)
    digests = {}
    for name, value in configs.items():
        write(out / "configs" / f"{name}.json", value)
        digests[name] = canonical_digest(value)
    inventory = read(args.native_inventory)
    write(out / "configs" / "native-inventory.json", inventory)
    rubric_ids, tasks, policies, bindings = [], [], [], {}
    permissions = ["host_exec:deny", "mutation:none"]
    for item in plan["host"]:
        rubric = dict(
            format="relaymind.owner_run_rubric.v1",
            task_id=item["item_id"],
            criteria=item["rubric"],
        )
        write(out / "rubrics" / f"{item['item_id']}.json", rubric)
        rubric_id = f"rubric:{item['item_id']}"
        rubric_ids.append(rubric_id)
        record = records[item["target"]]
        binding = dict(
            asset_id=record["asset_id"],
            content_hash=record["content_hash"],
            schema_hash=record["identity"].get("schema_hash"),
        )
        bindings[record["asset_id"]] = binding
        policies.append(
            dict(task_id=item["item_id"], permissions=permissions, budgets_digest=digests["budget"])
        )
        tasks.append(
            dict(
                id=item["item_id"],
                lane=item["lane"],
                family_ids=[f"intent:{item['item_id']}"],
                prompt_case_id=item["case_id"],
                setup_evidence_ids=[],
                success_rubric_evidence_id=rubric_id,
                permissions_digest=canonical_digest(permissions),
                budgets_digest=digests["budget"],
                asset_bindings=[binding],
                mutation=False,
            )
        )
    package = args.package.read_bytes()
    arms = ("native", "provider_only", "recommendations")
    freeze = dict(
        schema_version=1,
        corpus_lock_digest=canonical_digest(lock),
        configurations=[
            dict(
                arm=arm,
                provider=PROVIDER,
                model=HOST_MODEL,
                provider_config_digest=digests["provider"],
                sampling_config_digest=digests["sampling"],
                host_config_digest=digests["host-" + arm],
                recommendation_config_digest=digests["recommendations"]
                if arm == "recommendations"
                else None,
            )
            for arm in arms
        ],
        task_policies=sorted(policies, key=lambda p: p["task_id"].encode()),
        native_inventory_digest=canonical_digest(inventory),
        packages=[
            dict(
                name="@relaymind/openclaw-provider",
                version="0.1.0",
                sha256=sha256(package),
                bytes=len(package),
                evidence_id="package:relaymind-openclaw-provider-0.1.0",
            )
        ],
        host=dict(
            name="openclaw",
            version="2026.10.7",
            sdk_version="2026.10.7",
            distribution_evidence_ids=["host-distribution:openclaw-2026.10.7"],
        ),
        asset_bindings=[bindings[k] for k in sorted(bindings, key=str.encode)],
        rubric_evidence_ids=sorted(rubric_ids, key=str.encode),
    )
    write(out / "host-freeze.json", freeze)
    document = dict(
        schema_version=1,
        kind="acceptance",
        corpus_lock_digest=canonical_digest(lock),
        seed=args.seed,
        freeze_digest=canonical_digest(freeze),
        tasks=sorted(tasks, key=lambda t: t["id"].encode()),
    )
    write(out / "host-tasks.json", document)
    print(json.dumps(dict(tasks=len(tasks), freeze=canonical_digest(freeze))))


def cmd_episodes(args) -> None:
    """AssetEpisodesV1 over the frozen release and the prepared EpisodeFreezeV1."""
    root = args.root
    catalog, lock = read(root / "catalog.json"), read(root / "lock.json")
    plan = read(root / "cohort-plan.json")
    freeze = read(args.freeze)
    records = {a["record"]["asset_id"]: a["record"] for a in catalog["assets"]}
    episodes = []
    for item in plan["episodes"]:
        record = records[item["target"]]
        ids = item["case_ids"]
        episodes.append(
            dict(
                id=item["item_id"],
                intent_family_ids=[f"intent:{item['item_id']}"],
                corrections=[
                    dict(
                        case_id=case_id,
                        target_asset_id=record["asset_id"],
                        content_hash=record["content_hash"],
                        schema_hash=record["identity"].get("schema_hash"),
                    )
                    for case_id in ids[:3]
                ],
                paraphrase_case_ids=sorted(ids[3:]),
                control_case_ids=[item["control"]],
                reset_probe_case_ids=[item["reset"]],
            )
        )
    document = dict(
        schema_version=1,
        dataset_lock_digest=canonical_digest(lock),
        config_digest=canonical_digest(freeze),
        episodes=sorted(episodes, key=lambda e: e["id"].encode()),
    )
    write(args.output, document)
    print(json.dumps(dict(episodes=len(episodes))))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("catalog")
    p.add_argument("--built", type=Path, required=True)
    p.add_argument("--root", type=Path, required=True)
    p = sub.add_parser("plan")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    for name in ("draft", "annotate", "adjudicate"):
        p = sub.add_parser(name)
        p.add_argument("--root", type=Path, required=True)
        p.add_argument("--output", type=Path, required=True)
        p.add_argument("--log", type=Path, required=True)
        p.add_argument("--ledger", type=Path, required=True)
        p.add_argument("--cap", type=int, default=5_000_000)
        p.add_argument("--batch", type=int, default=10)
        p.add_argument("--max-batches", type=int, default=0)
        if name == "draft":
            p.add_argument("--plan", type=Path, required=True)
        if name == "annotate":
            p.add_argument("--template", type=Path, required=True)
        if name == "adjudicate":
            p.add_argument("--worksheet", type=Path, required=True)
    p = sub.add_parser("cases")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--drafts", type=Path, required=True)
    p.add_argument("--drafting-log", type=Path, required=True)
    p = sub.add_parser("lock")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--frozen-at", required=True)
    p.add_argument("--revision", required=True)
    p = sub.add_parser("host")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--native-inventory", type=Path, required=True)
    p.add_argument("--package", type=Path, required=True)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("episodes")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--freeze", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("log")
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--template", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--copy-to", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--rejected", nargs="*")
    args = parser.parse_args()
    globals()["cmd_" + args.command](args)


if __name__ == "__main__":
    main()
