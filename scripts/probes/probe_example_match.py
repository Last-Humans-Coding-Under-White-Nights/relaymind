"""Example-matching selector spike, scored offline on the corpus prompts (#118).

RM28, spec 0.31 sections 12.1, 12.2, and 12.4. Throwaway instrumentation,
not production code: the production ``example_match`` selector is #132,
and the scoring functions here are pure so that it can port them.

The matcher indexes each demo skill as one document: its description and
the requests of its ``examples.json`` (English and Chinese). Identifiers
are masked first, with the enum values of the allowed tools' input
schemas as ``relaymind.orchestrator.identifiers`` reads them
(``staging-api`` and ``payments-api``), so a request about one service
matches the examples of another. Latin text becomes word tokens and
Chinese text character bigrams. The score is BM25 from the standard
library; each distinct query word counts once. The pick is the best
skill when its score and its margin to the second skill reach the
thresholds, and none otherwise.

The 52 corpus prompts are scored against the skill sidecar
(``evaluation/corpus.v1.skills.json``, frozen in the corpus lock):
precision, recall, and Jaccard (section 12.4) per language and class,
with the score and margin of right and wrong picks. With ``--export``
(a corpus export from ``scripts/run_corpus.py``) the picks of the local
router in that export, the first ``route.selected`` of each auto and
local_only run with mechanism ``native_function_calling``, are the
baseline, and the candidate thresholds are the pair that keeps the
English wrong picks at or below the router's and gets the most right
picks. The thresholds are chosen on the cases that score them, so the
result is not held out. No model is called.

Usage:
    python scripts/probes/probe_example_match.py \\
        --export docs/evidence/2026-10-22-corpus-apple-m1.json \\
        --out docs/evidence/<date>-example-match-<machine>.json
"""

import argparse
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from relaymind.evaluation.corpus import (
    CLASSES,
    LANGUAGES,
    Corpus,
    check_lock,
    demo_skill_names,
    load_corpus,
    load_skills_sidecar,
)
from relaymind.evaluation.environment import application_revision
from relaymind.evaluation.metrics import nearest_rank
from relaymind.orchestrator.identifiers import required_enum_identifiers
from relaymind.tools.sandbox import DEMO_TOOL_SCHEMAS

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "evaluation" / "corpus.v1.json"
LOCK = REPO_ROOT / "evaluation" / "corpus.v1.lock.json"
SIDECAR = REPO_ROOT / "evaluation" / "corpus.v1.skills.json"
SEED_DIR = REPO_ROOT / "tests" / "fixtures" / "sandbox"
SKILLS_ROOT = REPO_ROOT / "skills"

LABEL = "offline analysis of recorded traces, not a measured run"
KIND = "relaymind.probe.example_match"
# A masked identifier; no real word in the descriptions or requests.
PLACEHOLDER = "idmask"
K1, B = 1.5, 0.75
ROUTER_MODES = ("auto", "local_only")
ROUTER_MECHANISM = "native_function_calling"
_CJK = "㐀-䶿一-鿿豈-﫿"
_TOKEN = re.compile(rf"([{_CJK}]+)|([A-Za-z0-9]+)")
_CAVEATS = (
    "The thresholds are chosen on the same 52 cases that score them; the result is optimistic "
    "and is not held out.",
    "The router figures are the two runs per case in one export (auto and local_only, one "
    "machine). The gateway ran the router and the local skill, repair, and final calls at "
    "temperature 0, so the two runs are repeats of a near-deterministic call, not independent "
    "samples; a second run could still differ, because the runtime does not promise "
    "identical output across runs. They are not a distribution.",
    "Every corpus case expects one skill and a selector picks at most one, so the macro "
    "precision, recall, and Jaccard are equal on the corpus; the micro averages differ. The ops "
    "corpus cannot tell a good selector from a poor one, which is why section 12.4 adds the "
    "selection benchmarks.",
    "Chinese cases are best-effort versions of the simple English cases and have not been "
    "natively reviewed; they are reported apart.",
)


# -- tokens and masking (pure) -----------------------------------------------------------


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens for Latin text, character bigrams for Chinese text."""
    tokens: list[str] = []
    for chinese, word in _TOKEN.findall(text):
        if chinese:
            tokens += [chinese[i : i + 2] for i in range(len(chinese) - 1)] or [chinese]
        else:
            tokens.append(word.lower())
    return tokens


def mask_identifiers(text: str, identifiers: Collection[str]) -> str:
    """Replace each identifier that stands alone as a token (the rule of identifiers.mentions)."""
    names = sorted(set(identifiers), key=len, reverse=True)
    if not names:
        return text
    alternatives = "|".join(re.escape(name) for name in names)
    pattern = (
        rf"(?<![A-Za-z0-9_-])(?<![A-Za-z0-9][.:/])(?:{alternatives})"
        rf"(?![A-Za-z0-9_-])(?![.:/][A-Za-z0-9])"
    )
    return re.sub(pattern, PLACEHOLDER, text)


# -- BM25 (pure) -------------------------------------------------------------------------


@dataclass(frozen=True)
class Index:
    """BM25 statistics of the skill documents, built from masked tokens."""

    identifiers: frozenset[str]
    k1: float
    b: float
    terms: Mapping[str, Counter]
    lengths: Mapping[str, int]
    average_length: float
    idf: Mapping[str, float]


def build_index(
    documents: Mapping[str, str], *, identifiers: Collection[str], k1: float = K1, b: float = B
) -> Index:
    """Index one text per skill. idf is ln(1 + (N - n + 0.5) / (n + 0.5)), never negative."""
    masked = frozenset(identifiers)
    terms = {
        name: Counter(tokenize(mask_identifiers(text, masked))) for name, text in documents.items()
    }
    lengths = {name: sum(counter.values()) for name, counter in terms.items()}
    count = len(terms)
    document_frequency = Counter(term for counter in terms.values() for term in counter)
    idf = {
        term: math.log(1 + (count - n + 0.5) / (n + 0.5)) for term, n in document_frequency.items()
    }
    average = sum(lengths.values()) / count if count else 0.0
    return Index(masked, k1, b, terms, lengths, average, idf)


def score_query(index: Index, query: str) -> dict[str, float]:
    """The BM25 score of the query against every skill document; unknown words add nothing.

    The sum runs over the sorted distinct words, so the last bits of a score
    do not depend on the hash seed of the process.
    """
    words = sorted(set(tokenize(mask_identifiers(query, index.identifiers))))
    scores = {}
    for name, counter in index.terms.items():
        norm = 1.0
        if index.average_length:
            norm = 1 - index.b + index.b * index.lengths[name] / index.average_length
        total = 0.0
        for word in words:
            frequency = counter.get(word, 0)
            if frequency:
                total += (
                    index.idf[word] * frequency * (index.k1 + 1) / (frequency + index.k1 * norm)
                )
        scores[name] = total
    return scores


# -- the decision (pure) -----------------------------------------------------------------


class Selection(NamedTuple):
    """The best skill and its numbers; skill is None when the thresholds are not reached."""

    skill: str | None
    top: str | None
    score: float
    second: float
    margin: float


def choose(scores: Mapping[str, float], *, min_score: float, min_margin: float) -> Selection:
    """The best skill when its score and its margin to the second reach the thresholds."""
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if not ranked or ranked[0][1] <= 0:
        return Selection(None, None, 0.0, 0.0, 0.0)
    top, score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = score - second
    reached = score >= min_score and margin >= min_margin
    return Selection(top if reached else None, top, score, second, margin)


# -- precision, recall, Jaccard (pure; spec section 12.4) ---------------------------------


def set_metrics(predicted: Collection[str], expected: Collection[str]) -> dict[str, float]:
    """Precision, recall, and Jaccard of one case; each is 0 for an empty predicted set."""
    p, e = set(predicted), set(expected)
    shared = len(p & e)
    if not p:
        return {"precision": 0.0, "recall": 0.0, "jaccard": 0.0}
    return {
        "precision": shared / len(p),
        "recall": shared / len(e) if e else 0.0,
        "jaccard": shared / len(p | e),
    }


def summarize_sets(pairs: Sequence[tuple[Collection[str], Collection[str]]]) -> dict[str, Any]:
    """Macro and micro averages over cases, and the picks: right, wrong, and none."""
    cases = len(pairs)
    picks = sum(1 for p, _ in pairs if p)
    right = sum(1 for p, e in pairs if p and set(p) == set(e))
    result: dict[str, Any] = {
        "cases": cases,
        "picks": picks,
        "right": right,
        "wrong": picks - right,
        "none": cases - picks,
        "macro": None,
        "micro": None,
    }
    if not cases:
        return result
    each = [set_metrics(p, e) for p, e in pairs]
    result["macro"] = {key: sum(m[key] for m in each) / cases for key in each[0]}
    shared = sum(len(set(p) & set(e)) for p, e in pairs)
    predicted = sum(len(set(p)) for p, _ in pairs)
    expected = sum(len(set(e)) for _, e in pairs)
    union = sum(len(set(p) | set(e)) for p, e in pairs)
    result["micro"] = {
        "precision": shared / predicted if predicted else 0.0,
        "recall": shared / expected if expected else 0.0,
        "jaccard": shared / union if union else 0.0,
    }
    return result


# -- thresholds (pure) -------------------------------------------------------------------

Rows = Sequence[tuple[str | None, Mapping[str, float]]]


def _grid(rows: Rows) -> list[dict[str, Any]]:
    """Every pair of observed top scores and margins, and what each pair picks."""
    tops = [choose(scores, min_score=0.0, min_margin=0.0) for _, scores in rows]
    scores = sorted({0.0, *(t.score for t in tops), max((t.score for t in tops), default=0) + 1})
    margins = sorted({0.0, *(t.margin for t in tops), max((t.margin for t in tops), default=0) + 1})
    points = []
    for min_score in scores:
        for min_margin in margins:
            right = wrong = 0
            for (expected, _), top in zip(rows, tops, strict=True):
                if top.top is not None and top.score >= min_score and top.margin >= min_margin:
                    if top.top == expected:
                        right += 1
                    else:
                        wrong += 1
            points.append(
                {
                    "min_score": min_score,
                    "min_margin": min_margin,
                    "right": right,
                    "wrong": wrong,
                    "none": len(rows) - right - wrong,
                }
            )
    return points


def _preference(point: Mapping[str, Any]) -> tuple:
    """Most right picks, then fewest wrong, then the most conservative thresholds."""
    return (point["right"], -point["wrong"], point["min_margin"], point["min_score"])


def candidate_thresholds(rows: Rows, *, max_wrong: int) -> dict[str, Any] | None:
    """The thresholds with the most right picks among those with at most max_wrong wrong picks."""
    if not rows:
        return None
    return max((point for point in _grid(rows) if point["wrong"] <= max_wrong), key=_preference)


def frontier(rows: Rows) -> list[dict[str, Any]]:
    """The best right picks at each number of wrong picks, keeping only strict improvements."""
    best: dict[int, dict[str, Any]] = {}
    for point in _grid(rows):
        if point["wrong"] not in best or _preference(point) > _preference(best[point["wrong"]]):
            best[point["wrong"]] = point
    result, seen = [], -1
    for wrong in sorted(best):
        if best[wrong]["right"] > seen:
            result.append(best[wrong])
            seen = best[wrong]["right"]
    return result


# -- inputs ------------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillDoc:
    """One demo skill as the matcher sees it."""

    name: str
    description: str
    requests: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    pairs: int

    @property
    def text(self) -> str:
        return "\n".join([self.description, *self.requests])


def load_catalog(skills_root: Path = SKILLS_ROOT) -> list[SkillDoc]:
    """The demo skill packages: description, example requests in both languages, allowed tools."""
    catalog = []
    for skill_md in sorted(Path(skills_root).glob("*/SKILL.md")):
        package = skill_md.parent
        front = skill_md.read_text("utf-8").split("---")[1]
        match = re.search(r"^description:\s*(.+)$", front, re.MULTILINE)
        manifest = json.loads((package / "skill.manifest.json").read_text("utf-8"))
        examples = json.loads((package / "examples.json").read_text("utf-8"))
        requests = tuple(
            pair[language]["request"]
            for pair in examples["pairs"]
            for language in LANGUAGES
            if pair.get(language, {}).get("request")
        )
        catalog.append(
            SkillDoc(
                package.name,
                match.group(1).strip() if match else "",
                requests,
                tuple(manifest["allowed_tools"]),
                len(examples["pairs"]),
            )
        )
    return catalog


def catalog_identifiers(catalog: Sequence[SkillDoc]) -> frozenset[str]:
    """The enum identifier values of the allowed tools' input schemas (the masked IDs)."""
    found: set[str] = set()
    for skill in catalog:
        for tool in skill.allowed_tools:
            for values in required_enum_identifiers(
                DEMO_TOOL_SCHEMAS[tool]["input_schema"]
            ).values():
                found.update(values)
    return frozenset(found)


def caveats(catalog: Sequence[SkillDoc]) -> list[str]:
    """The caveats of the report; the example pair count of each skill is read from the catalog."""
    pairs = ", ".join(f"{skill.name} {skill.pairs}" for skill in catalog)
    index = (
        f"Each skill has only a few example pairs ({pairs}), so the index is tiny and its word "
        "statistics are coarse."
    )
    return [index, *_CAVEATS]


class CorpusSkills(NamedTuple):
    corpus: Corpus
    expected: dict[str, str | None]


def load_corpus_and_sidecar() -> CorpusSkills:
    """The frozen corpus and its skill sidecar, refused when either differs from the lock."""
    corpus = load_corpus(CORPUS, seed_dir=SEED_DIR)
    check_lock(corpus, LOCK)
    expected = load_skills_sidecar(SIDECAR, corpus, skill_names=demo_skill_names(SKILLS_ROOT))
    return CorpusSkills(corpus, expected)


def router_picks(document: Mapping[str, Any]) -> dict[tuple[str, str], str | None]:
    """The local router's pick in each auto and local_only run of an export.

    The pick is the skill of the run's first route.selected with mechanism
    native_function_calling; None when the run has none (the routing output
    was rejected before a decision).
    """
    picks: dict[tuple[str, str], str | None] = {}
    for grade in document["grades"]:
        if grade["mode"] not in ROUTER_MODES:
            continue
        pick = None
        for event in document["traces"].get(grade["run_id"], []):
            payload = event["payload"]
            if event["event_type"] == "route.selected" and payload.get("mechanism") == (
                ROUTER_MECHANISM
            ):
                pick = (payload.get("decision") or {}).get("skill_id")
                break
        picks[(grade["case_id"], grade["mode"])] = pick
    return picks


# -- the analysis ------------------------------------------------------------------------


def _by_language_and_class(
    rows: Sequence[Mapping[str, Any]], key: str = "predicted"
) -> list[dict[str, Any]]:
    table = []
    for language in LANGUAGES:
        for case_class in CLASSES:
            subset = [
                r for r in rows if r["language"] == language and r["case_class"] == case_class
            ]
            if subset:
                pairs = [(r[key], r["expected_set"]) for r in subset]
                table.append(
                    {"language": language, "case_class": case_class, **summarize_sets(pairs)}
                )
    return table


def _by_language(rows: Sequence[Mapping[str, Any]], key: str = "predicted") -> dict[str, Any]:
    """One summary per language: Chinese results are never merged into English ones."""
    return {
        language: summarize_sets(
            [(r[key], r["expected_set"]) for r in rows if r["language"] == language]
        )
        for language in LANGUAGES
        if any(r["language"] == language for r in rows)
    }


def _spread(values: Sequence[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "min": round(min(values), 4),
        "p25": round(nearest_rank(values, 0.25), 4),
        "median": round(nearest_rank(values, 0.5), 4),
        "p75": round(nearest_rank(values, 0.75), 4),
        "max": round(max(values), 4),
    }


def _distribution(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "score": _spread([r["score"] for r in rows]),
        "margin": _spread([r["margin"] for r in rows]),
    }


def _router_sample(
    cases: Sequence[Mapping[str, Any]], picks: Mapping[tuple[str, str], str | None], mode: str
) -> dict[str, Any]:
    rows = [
        {**c, "predicted": {picks[(c["case_id"], mode)]} - {None}}
        for c in cases
        if (c["case_id"], mode) in picks
    ]
    return {
        "by_language": _by_language(rows),
        "by_language_and_class": _by_language_and_class(rows),
    }


def analyse(
    bundle: CorpusSkills,
    catalog: Sequence[SkillDoc],
    export: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Score the corpus prompts with the matcher, and compare with the router of an export."""
    index = build_index({s.name: s.text for s in catalog}, identifiers=catalog_identifiers(catalog))
    cases, rows = [], []
    for case in bundle.corpus.cases:
        scores = score_query(index, case.prompt)
        best = choose(scores, min_score=0.0, min_margin=0.0)
        expected = bundle.expected[case.case_id]
        top3 = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:3]
        cases.append(
            {
                "case_id": case.case_id,
                "case_class": case.case_class,
                "language": case.language,
                "expected": expected,
                "expected_set": {expected} - {None},
                "pick": best.top,
                "predicted": {best.top} - {None},
                "right": best.top == expected,
                "score": round(best.score, 4),
                "second": round(best.second, 4),
                "margin": round(best.margin, 4),
                "top3": [[name, round(score, 4)] for name, score in top3],
            }
        )
        rows.append((case.language, expected, scores))
    picks = [c for c in cases if c["pick"] is not None]
    analysis: dict[str, Any] = {
        "kind": KIND,
        "label": LABEL,
        "note": "A throwaway BM25 matcher over the demo skills' example requests and "
        "descriptions, scored on the corpus prompts against the skill sidecar. Nothing here "
        "is a measured run of the selector in the gateway.",
        "method": {
            "documents": {s.name: len(tokenize(s.text)) for s in catalog},
            "example_pairs": {s.name: s.pairs for s in catalog},
            "masked_identifiers": sorted(index.identifiers),
            "placeholder": PLACEHOLDER,
            "bm25": {"k1": K1, "b": B},
            "tokens": "word tokens for Latin text, character bigrams for Chinese text",
        },
        "source": {
            "corpus": {"sha256": bundle.corpus.digest},
            "sidecar": SIDECAR.name,
            "router_export": None if export is None else export.get("corpus"),
        },
        "caveats": caveats(catalog),
        "cases": [
            {k: v for k, v in c.items() if k not in ("predicted", "expected_set")} for c in cases
        ],
        "argmax": {
            "note": "The best skill of every prompt with no thresholds (every prompt with any "
            "word in common with the index is a pick).",
            "by_language": _by_language(cases),
            "by_language_and_class": _by_language_and_class(cases),
        },
        "distributions": {
            language: {
                "right": _distribution(
                    [c for c in picks if c["language"] == language and c["right"]]
                ),
                "wrong": _distribution(
                    [c for c in picks if c["language"] == language and not c["right"]]
                ),
            }
            for language in LANGUAGES
        },
        "router": None,
        "candidate": None,
    }
    if export is not None:
        analysis["router"], analysis["candidate"] = _against_router(cases, rows, export)
    return analysis


def _against_router(
    cases: Sequence[Mapping[str, Any]],
    rows: Sequence[tuple[str, str | None, Mapping[str, float]]],
    export: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    picks = router_picks(export)
    modes = [mode for mode in ROUTER_MODES if any(key[1] == mode for key in picks)]
    if not modes:
        raise SystemExit("the export has no auto or local_only run, so it has no router baseline")
    samples = {mode: _router_sample(cases, picks, mode) for mode in modes}
    english_wrong = {
        mode: sum(
            row["wrong"] for row in sample["by_language_and_class"] if row["language"] == "en"
        )
        for mode, sample in samples.items()
    }
    max_wrong = min(english_wrong.values())
    english_rows = [(expected, scores) for language, expected, scores in rows if language == "en"]
    best = candidate_thresholds(english_rows, max_wrong=max_wrong)
    if best is None:
        raise SystemExit("the corpus has no English case to choose thresholds on")
    thresholds = {"min_score": best["min_score"], "min_margin": best["min_margin"]}
    at_thresholds = []
    for case, (_, _, scores) in zip(cases, rows, strict=True):
        pick = choose(scores, **thresholds)
        at_thresholds.append(
            {**case, "predicted": {pick.skill} - {None}, "threshold_pick": pick.skill}
        )
    english = [r for r in at_thresholds if r["language"] == "en"]
    chinese = [r for r in at_thresholds if r["language"] == "zh"]
    candidate = {
        "language": "en",
        "rule": "the thresholds with the most right English picks among those whose English "
        "wrong picks are at or below the router's (the lowest of its samples)",
        "router_wrong_picks": english_wrong,
        "max_wrong": max_wrong,
        "thresholds": thresholds,
        "english": summarize_sets([(r["predicted"], r["expected_set"]) for r in english]),
        "english_by_class": _by_language_and_class(english),
        "chinese": summarize_sets([(r["predicted"], r["expected_set"]) for r in chinese]),
        "chinese_by_class": _by_language_and_class(chinese),
        "frontier": frontier(english_rows),
        "not_held_out": True,
    }
    both = [case for case in cases if all((case["case_id"], m) in picks for m in ROUTER_MODES)]
    differ = sum(1 for case in both if len({picks[(case["case_id"], m)] for m in ROUTER_MODES}) > 1)
    router = {"samples": samples, "cases_in_both": len(both), "samples_differ": differ}
    return router, candidate


# -- the report --------------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def _summary_row(label: str, row: Mapping[str, Any]) -> str:
    macro, micro = row["macro"] or {}, row["micro"] or {}
    return (
        f"| {label} | {row['cases']} | {row['picks']} | {row['right']} | {row['wrong']} "
        f"| {row['none']} | {_fmt(macro.get('precision'))} | {_fmt(macro.get('recall'))} "
        f"| {_fmt(macro.get('jaccard'))} | {_fmt(micro.get('precision'))} "
        f"| {_fmt(micro.get('recall'))} | {_fmt(micro.get('jaccard'))} |"
    )


_HEAD = (
    "| group | cases | picks | right | wrong | none | macro precision | macro recall "
    "| macro Jaccard | micro precision | micro recall | micro Jaccard |",
    "|---|---|---|---|---|---|---|---|---|---|---|---|",
)


def _table(
    title: str,
    rows: Sequence[Mapping[str, Any]],
    totals: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[str]:
    """Rows per language and class, then one total row per language (never both languages)."""
    lines = ["", f"### {title}", "", *_HEAD]
    lines += [_summary_row(f"{r['language']} {r['case_class']}", r) for r in rows]
    for language, total in (totals or {}).items():
        lines.append(_summary_row(f"{language} all", total))
    return lines


def render_markdown(analysis: Mapping[str, Any]) -> str:
    """The report: the matcher's picks, the router baseline, and the candidate thresholds."""
    lines = [f"# Example matcher on the corpus prompts: {LABEL}", "", analysis["note"], ""]
    method = analysis["method"]
    lines += [
        f"Masked identifiers: {', '.join(method['masked_identifiers'])}. BM25 k1 "
        f"{method['bm25']['k1']}, b {method['bm25']['b']}. Tokens: {method['tokens']}.",
        "",
        "## Best skill of every prompt, no thresholds",
        *_table(
            "Matcher",
            analysis["argmax"]["by_language_and_class"],
            analysis["argmax"]["by_language"],
        ),
        "",
        "### Score and margin of the picks",
        "",
        "| language | picks | count | score min | p25 | median | p75 | max | margin min | p25 "
        "| median | p75 | max |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for language, picks in analysis["distributions"].items():
        for kind, part in picks.items():
            s, m = part["score"] or {}, part["margin"] or {}
            lines.append(
                f"| {language} | {kind} | {part['count']} | "
                + " | ".join(_fmt(s.get(k)) for k in ("min", "p25", "median", "p75", "max"))
                + " | "
                + " | ".join(_fmt(m.get(k)) for k in ("min", "p25", "median", "p75", "max"))
                + " |"
            )
    if analysis["router"] is not None:
        router = analysis["router"]
        lines += [
            "",
            "## Local router in the export (the baseline)",
            "",
            f"The auto and local_only runs picked different skills for {router['samples_differ']} "
            f"of {router['cases_in_both']} cases that have both.",
        ]
        for mode, sample in router["samples"].items():
            lines += _table(
                f"Router, {mode}", sample["by_language_and_class"], sample["by_language"]
            )
        candidate = analysis["candidate"]
        t = candidate["thresholds"]
        lines += [
            "",
            "## Candidate thresholds",
            "",
            f"Rule: {candidate['rule']}. Router English wrong picks: "
            + ", ".join(f"{k} {v}" for k, v in candidate["router_wrong_picks"].items())
            + f"; limit {candidate['max_wrong']}. Thresholds: score at least "
            f"{t['min_score']:.4f}, margin at least {t['min_margin']:.4f}. Chosen on the cases "
            "that score them; not held out.",
            *_table(
                "English at the thresholds",
                candidate["english_by_class"],
                {"en": candidate["english"]},
            ),
            *_table(
                "Chinese at the thresholds",
                candidate["chinese_by_class"],
                {"zh": candidate["chinese"]},
            ),
            "",
            "### Right and wrong English picks along the frontier",
            "",
            "| wrong picks | right picks | score at least | margin at least |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {f['wrong']} | {f['right']} | {f['min_score']:.4f} | {f['min_margin']:.4f} |"
            for f in candidate["frontier"]
        ]
    lines += ["", "## Caveats", "", *[f"- {c}" for c in analysis["caveats"]], ""]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, help="the analysis (JSON); the report is .md")
    parser.add_argument(
        "--export", help="a corpus export (JSON) whose router picks are the baseline"
    )
    args = parser.parse_args(argv)
    export = json.loads(Path(args.export).read_text("utf-8")) if args.export else None
    application = application_revision(REPO_ROOT)
    analysis = analyse(load_corpus_and_sidecar(), load_catalog(), export=export)
    analysis["analysed_with"] = {"application": application}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    report = render_markdown(analysis)
    out.with_suffix(".md").write_text(report, encoding="utf-8")
    sys.stdout.write(report)


if __name__ == "__main__":
    main()
