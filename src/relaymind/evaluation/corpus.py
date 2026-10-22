"""The frozen correctness corpus (spec 0.28 section 12.2, #32; RM25).

The corpus is one versioned JSON file (``evaluation/corpus.v1.json``)
with the sandbox seeds its cases name (``tests/fixtures/sandbox/``). It
is frozen by the SHA-256 digest of the canonical JSON (RM07) of the
corpus and every seed it names; the digest is kept in a lock file, and a
runner refuses a corpus or seed that no longer matches it. Development
examples (the skill examples and the live harness prompts) are never
corpus cases.
"""

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from relaymind.canonical import canonical_digest
from relaymind.schema_util import check_closed_fields, check_schema_version
from relaymind.tools.sandbox import DEMO_TOOL_NAMES, MUTATE_PERMISSION, READ_PERMISSION

CLASSES = ("simple", "complex", "ambiguous", "mutation", "adversarial")
ENGLISH_CLASS_SIZES = {"simple": 12, "complex": 10, "ambiguous": 6, "mutation": 6, "adversarial": 6}
MODES = ("auto", "external_only", "local_only")
LANGUAGES = ("en", "zh")
ROUTES = ("local", "external", "clarify")
OUTCOMES = ("completed", "failed", "cancelled")
PERMISSIONS = (READ_PERMISSION, MUTATE_PERMISSION)
DECISIONS = ("approve", "deny")
# An argument pattern value that accepts any value.
ANY_VALUE = "*"

_DOCUMENT_FIELDS = (
    "schema_version",
    "corpus",
    "version",
    "frozen_on",
    "note",
    "chinese_review",
    "cases",
)
# The JSON fields of a case.
_CASE_FIELDS = (
    "id",
    "class",
    "language",
    "best_effort",
    "english_case",
    "prompt",
    "fixture",
    "permissions",
    "expected_route",
    "required_calls",
    "allowed_calls",
    "clarification_answer",
    "confirmation",
    "expected_outcomes",
    "required_evidence",
    "forbidden_evidence",
    "expected_databases",
    "expect_clarification",
    "rubric",
)


class CorpusError(ValueError):
    """The corpus breaks the section 12.2 case contract or its frozen digest."""


@dataclass(frozen=True)
class Case:
    """One corpus case (section 12.2). Call patterns are {"tool", "arguments"} objects."""

    case_id: str
    case_class: str
    language: str
    best_effort: bool
    english_case: str | None
    prompt: str
    fixture: str
    permissions: tuple[str, ...]
    expected_route: str
    required_calls: tuple[tuple[dict[str, Any], ...], ...]
    allowed_calls: tuple[dict[str, Any], ...]
    clarification_answer: str | None
    confirmation: str | None
    expected_outcomes: tuple[str, ...]
    required_evidence: tuple[tuple[str, ...], ...]
    forbidden_evidence: tuple[str, ...]
    expected_databases: tuple[str, ...] | None
    expect_clarification: bool
    rubric: str

    @property
    def acceptable_calls(self) -> list[dict[str, Any]]:
        """Every call the case accepts: the required groups' calls and the allowed ones."""
        return [p for group in self.required_calls for p in group] + list(self.allowed_calls)


@dataclass(frozen=True)
class Corpus:
    document: dict[str, Any]
    cases: tuple[Case, ...]
    seeds: dict[str, dict[str, Any]]
    digest: str

    def case(self, case_id: str) -> Case:
        return next(case for case in self.cases if case.case_id == case_id)


def corpus_digest(document: Mapping[str, Any], seeds: Mapping[str, Any]) -> str:
    """The frozen digest: canonical JSON of the corpus and of every seed it names."""
    return canonical_digest({"corpus": document, "seeds": dict(seeds)})


def load_corpus(path: Path, *, seed_dir: Path) -> Corpus:
    return parse_corpus(json.loads(Path(path).read_text("utf-8")), seed_dir=seed_dir)


def parse_corpus(document: Mapping[str, Any], *, seed_dir: Path) -> Corpus:
    """Check the corpus against the section 12.2 contract and read its seeds."""
    _check_fields("corpus", document, _DOCUMENT_FIELDS)
    check_schema_version(document["schema_version"], 1, CorpusError)
    seeds: dict[str, dict[str, Any]] = {}
    cases = []
    for raw in document["cases"]:
        fixture = raw.get("fixture") if isinstance(raw, Mapping) else None
        if isinstance(fixture, str) and fixture not in seeds:
            path = Path(seed_dir) / f"{fixture}.json"
            if re.fullmatch(r"[a-z0-9-]+", fixture) and path.is_file():
                seeds[fixture] = json.loads(path.read_text("utf-8"))
        cases.append(_parse_case(raw, seeds))
    _check_set(cases)
    return Corpus(dict(document), tuple(cases), seeds, corpus_digest(document, seeds))


def check_lock(corpus: Corpus, lock_path: Path) -> None:
    """Refuse a corpus whose digest differs from the frozen one (section 12.2)."""
    lock = json.loads(Path(lock_path).read_text("utf-8"))
    if lock.get("sha256") != corpus.digest:
        raise CorpusError(
            f"corpus digest {corpus.digest} differs from the frozen digest {lock.get('sha256')} "
            f"in {Path(lock_path).name}; a changed corpus is a new version (section 12.2)"
        )


def lock_document(corpus: Corpus, corpus_name: str) -> dict[str, Any]:
    """The lock file content that freezes a corpus."""
    return {
        "corpus": corpus_name,
        "version": corpus.document["version"],
        "sha256": corpus.digest,
        "seeds": {name: canonical_digest(seed) for name, seed in sorted(corpus.seeds.items())},
        "frozen_on": corpus.document["frozen_on"],
    }


def normalize_prompt(text: str) -> str:
    return " ".join(text.split()).casefold()


def development_prompts(skills_root: Path, *, extra: Iterable[str] = ()) -> set[str]:
    """The normalized prompts of the development examples (skill examples and extra prompts)."""
    prompts = {normalize_prompt(text) for text in extra}
    for path in sorted(Path(skills_root).glob("*/examples.json")):
        for pair in json.loads(path.read_text("utf-8")).get("pairs", []):
            for language in LANGUAGES:
                request = pair.get(language, {}).get("request")
                if request:
                    prompts.add(normalize_prompt(request))
    return prompts


# -- checks -----------------------------------------------------------------------------


def _check_fields(where: str, data: object, fields: Sequence[str]) -> Mapping[str, Any]:
    try:
        return check_closed_fields(data, fields, CorpusError, kind=where)
    except CorpusError as exc:
        raise CorpusError(f"{where}: {exc}") from None


def _strings(where: str, value: object, choices=None) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CorpusError(f"{where} must be a list of strings")
    if choices is not None and (bad := sorted(set(value) - set(choices))):
        raise CorpusError(f"{where}: unknown value(s) {', '.join(bad)}")
    return tuple(value)


def _pattern(where: str, value: object) -> str:
    if not isinstance(value, str):
        raise CorpusError(f"{where}: a pattern must be a string")
    try:
        re.compile(value)
    except re.error as exc:
        raise CorpusError(f"{where}: invalid pattern {value!r}: {exc}") from None
    return value


def _patterns(where: str, value: object) -> tuple[str, ...]:
    """A list of regular expressions; a bare string is refused, never split into characters."""
    if not isinstance(value, list):
        raise CorpusError(f"{where} must be a list of strings")
    return tuple(_pattern(where, p) for p in value)


def _evidence_groups(where: str, value: object) -> tuple[tuple[str, ...], ...]:
    """Groups of alternative patterns: a list of lists of strings."""
    if not isinstance(value, list) or not all(isinstance(group, list) for group in value):
        raise CorpusError(f"{where} must be a list of lists of strings")
    return tuple(_patterns(where, group) for group in value)


def _call(where: str, value: object) -> dict[str, Any]:
    _check_fields(where, value, ("tool", "arguments"))
    if value["tool"] not in DEMO_TOOL_NAMES:
        raise CorpusError(f"{where}: unknown tool {value['tool']!r}")
    if not isinstance(value["arguments"], Mapping):
        raise CorpusError(f"{where}: arguments must be an object")
    return {"tool": value["tool"], "arguments": dict(value["arguments"])}


def _parse_case(raw: object, seeds: Mapping[str, Any]) -> Case:
    _check_fields("case", raw, _CASE_FIELDS)
    where = f"case {raw['id']}"
    if raw["class"] not in CLASSES:
        raise CorpusError(f"{where}: class must be one of {', '.join(CLASSES)}")
    if raw["language"] not in LANGUAGES:
        raise CorpusError(f"{where}: language must be one of {', '.join(LANGUAGES)}")
    if raw["fixture"] not in seeds:
        raise CorpusError(f"{where}: fixture {raw['fixture']!r} names no seed file")
    if raw["expected_route"] not in ROUTES:
        raise CorpusError(f"{where}: expected_route must be one of {', '.join(ROUTES)}")
    if raw["confirmation"] not in (None, *DECISIONS):
        raise CorpusError(f"{where}: confirmation must be approve, deny, or null")
    for name in ("prompt", "rubric"):
        if not isinstance(raw[name], str) or not raw[name].strip():
            raise CorpusError(f"{where}: {name} must be a non-blank string")
    permissions = _strings(f"{where} permissions", raw["permissions"], PERMISSIONS)
    if not permissions:
        raise CorpusError(f"{where}: permissions must name at least one permission")
    outcomes = _strings(f"{where} expected_outcomes", raw["expected_outcomes"], OUTCOMES)
    if not outcomes:
        raise CorpusError(f"{where}: expected_outcomes must name at least one outcome")
    databases = raw["expected_databases"]
    return Case(
        case_id=raw["id"],
        case_class=raw["class"],
        language=raw["language"],
        best_effort=bool(raw["best_effort"]),
        english_case=raw["english_case"],
        prompt=raw["prompt"],
        fixture=raw["fixture"],
        permissions=permissions,
        expected_route=raw["expected_route"],
        required_calls=tuple(
            tuple(_call(f"{where} required_calls", c) for c in group)
            for group in raw["required_calls"]
        ),
        allowed_calls=tuple(_call(f"{where} allowed_calls", c) for c in raw["allowed_calls"]),
        clarification_answer=raw["clarification_answer"],
        confirmation=raw["confirmation"],
        expected_outcomes=outcomes,
        required_evidence=_evidence_groups(f"{where} required_evidence", raw["required_evidence"]),
        forbidden_evidence=_patterns(f"{where} forbidden_evidence", raw["forbidden_evidence"]),
        expected_databases=None
        if databases is None
        else _strings(f"{where} expected_databases", databases),
        expect_clarification=bool(raw["expect_clarification"]),
        rubric=raw["rubric"],
    )


def _check_set(cases: list[Case]) -> None:
    """Unique IDs, the section 12.2 English class sizes, and best-effort Chinese versions."""
    by_id: dict[str, Case] = {}
    for case in cases:
        if case.case_id in by_id:
            raise CorpusError(f"duplicate case id {case.case_id}")
        by_id[case.case_id] = case
    english = [case for case in cases if case.language == "en"]
    for name, size in ENGLISH_CLASS_SIZES.items():
        count = sum(1 for case in english if case.case_class == name)
        if count != size:
            raise CorpusError(
                f"the corpus has {count} English {name} cases; section 12.2 needs {size}"
            )
    for case in cases:
        if case.language == "en":
            if case.best_effort or case.english_case is not None:
                raise CorpusError(f"case {case.case_id}: an English case is not best-effort")
            continue
        english_case = by_id.get(case.english_case or "")
        if english_case is None or english_case.language != "en":
            raise CorpusError(f"case {case.case_id}: english_case must name an English case")
        if not case.best_effort or english_case.case_class != case.case_class:
            raise CorpusError(
                f"case {case.case_id}: a Chinese case is a best-effort version of its english_case"
            )
