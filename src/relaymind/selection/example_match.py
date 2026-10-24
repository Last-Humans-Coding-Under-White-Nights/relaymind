"""example_match: deterministic matching of a request to the offered skills (RM28).

Spec 0.31 RM28 and 0.32 section 14.1. No model call, standard library
only. The method is the one the offline spike of #118 scored
(``scripts/probes/probe_example_match.py``; ``docs/EVIDENCE.md``):

- Each offered skill is one document: its description and the example
  requests of its ``examples.json``, in both languages, as the
  SelectionRequest catalog carries them. The index is rebuilt when the
  catalog's skill IDs or content hashes change, and reused otherwise.
- Identifiers are masked first: the enum values of the required
  identifier arguments of the skills' allowed tools (``staging-api`` and
  ``payments-api`` in the demo), so a request about one service matches
  the examples of another, and changing only the service name never
  changes the pick.
- Latin text becomes lowercase word tokens, Chinese text character
  bigrams. The raw score is BM25 (k1 1.5, b 0.75); each distinct query
  word counts once, summed in sorted order, so the result does not
  depend on the hash seed of the process.

Normalization to [0, 1] (section 14.1). The raw score of a skill is
divided by the sum of the inverse document frequencies of the distinct
query words, a word no skill text holds counting with the largest idf
the index can give, and capped at 1. A skill scores 1 when every query
word appears in its text as often as in a document of average length,
and a request full of words no skill knows scores near 0. The score is
reported as the result's confidence: a selector-reported number, never a
calibrated probability (RM04).

The pick is the unique best skill whose score is at least
example_match_score and whose lead over the second-best skill is at
least example_match_margin (the second-best score is 0 for a one-skill
catalog); a tie for the top score, or a score of 0, is none. The
alternatives are the other skills that also reach example_match_score,
best first; the gateway ignores them for admission.

The default thresholds (0.5 and 0.1) are provisional product choices,
not accuracy claims. The #118 spike measured candidate thresholds on the
raw scale (score 2.8164, margin 0.1268) on the corpus that tuned them;
they are evidence, not this module's defaults.
"""

import json
import math
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from relaymind.orchestrator.identifiers import required_enum_identifiers
from relaymind.selection.contract import (
    SCHEMA_VERSION,
    SelectionContext,
    SelectionRequest,
)
from relaymind.tools import ToolRegistry

__all__ = [
    "EXAMPLE_MATCH",
    "EXAMPLE_MATCH_VERSION",
    "EXAMPLES_RESOURCE",
    "ExampleMatchSelector",
    "Match",
    "example_requests",
    "tokenize",
]

EXAMPLE_MATCH = "example_match"
EXAMPLE_MATCH_VERSION = "1"
EXAMPLES_RESOURCE = "examples.json"
K1, B = 1.5, 0.75
# A masked identifier; no real word in the descriptions or requests.
PLACEHOLDER = "idmask"
LANGUAGES = ("en", "zh")
_CJK = "㐀-䶿一-鿿豈-﫿"
_TOKEN = re.compile(rf"([{_CJK}]+)|([A-Za-z0-9]+)")


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


def example_requests(text: str) -> list[str]:
    """The English and Chinese example requests of an examples.json text, in order.

    A pair without a request in a language, or a file of another shape,
    gives nothing for it; the registry checks the package, not this file's
    inner shape.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return []
    pairs = data.get("pairs") if isinstance(data, Mapping) else None
    if not isinstance(pairs, list):
        return []
    found: list[str] = []
    for pair in pairs:
        if not isinstance(pair, Mapping):
            continue
        for language in LANGUAGES:
            side = pair.get(language)
            request = side.get("request") if isinstance(side, Mapping) else None
            if isinstance(request, str) and request.strip():
                found.append(request)
    return found


@dataclass(frozen=True)
class _Index:
    """BM25 statistics of the skill documents, built from masked tokens."""

    identifiers: frozenset[str]
    terms: Mapping[str, Counter]
    lengths: Mapping[str, int]
    average_length: float
    idf: Mapping[str, float]
    unseen_idf: float


def _build_index(documents: Mapping[str, str], identifiers: Collection[str]) -> _Index:
    """Index one text per skill. idf is ln(1 + (N - n + 0.5) / (n + 0.5)), never negative."""
    masked = frozenset(identifiers)
    terms = {
        name: Counter(tokenize(mask_identifiers(text, masked))) for name, text in documents.items()
    }
    lengths = {name: sum(counter.values()) for name, counter in terms.items()}
    count = len(terms)
    frequency = Counter(term for counter in terms.values() for term in counter)
    idf = {term: math.log(1 + (count - n + 0.5) / (n + 0.5)) for term, n in frequency.items()}
    average = sum(lengths.values()) / count if count else 0.0
    return _Index(masked, terms, lengths, average, idf, math.log(1 + (count + 0.5) / 0.5))


def _scores(index: _Index, query: str) -> dict[str, float]:
    """The normalized score of the query against every skill document, in [0, 1]."""
    words = sorted(set(tokenize(mask_identifiers(query, index.identifiers))))
    ideal = sum(index.idf.get(word, index.unseen_idf) for word in words)
    scores: dict[str, float] = {}
    for name, counter in index.terms.items():
        norm = 1.0
        if index.average_length:
            norm = 1 - B + B * index.lengths[name] / index.average_length
        raw = 0.0
        for word in words:
            frequency = counter.get(word, 0)
            if frequency:
                raw += index.idf[word] * frequency * (K1 + 1) / (frequency + K1 * norm)
        scores[name] = min(1.0, raw / ideal) if ideal else 0.0
    return scores


@dataclass(frozen=True)
class Match:
    """The ranking of one request: every skill's score, best first, and the pick.

    skill is None when no unique best skill reaches the thresholds; top is
    the first skill by score, then by ID, so a tie reads the same every time.
    """

    ranked: tuple[tuple[str, float], ...]
    skill: str | None
    score: float
    margin: float

    @property
    def top(self) -> str | None:
        return self.ranked[0][0] if self.ranked and self.ranked[0][1] > 0 else None


class ExampleMatchSelector:
    """The deterministic example matcher (RM28). It makes no model call."""

    name = EXAMPLE_MATCH
    version = EXAMPLE_MATCH_VERSION
    model_based = False
    mechanism = EXAMPLE_MATCH

    def __init__(self, tools: ToolRegistry, *, score: float, margin: float) -> None:
        self.tools = tools
        self.min_score = score
        self.min_margin = margin
        self._index_key: tuple[tuple[str, str], ...] | None = None
        self._index: _Index | None = None
        # How many times the index was built; a test reads it (RM24: reuse by content hash).
        self.index_builds = 0

    def _identifiers(self, tool_names: Iterable[str]) -> frozenset[str]:
        found: set[str] = set()
        for name in sorted(set(tool_names)):
            if name not in self.tools.names():
                continue
            schema = self.tools.get(name).declaration.input_schema
            for values in required_enum_identifiers(schema).values():
                found.update(values)
        return frozenset(found)

    def index_for(self, catalog: Sequence[Mapping[str, Any]]) -> _Index:
        """The index of a catalog, rebuilt only when its IDs or content hashes change."""
        key = tuple((entry["id"], entry["content_hash"]) for entry in catalog)
        if self._index is None or key != self._index_key:
            documents = {
                entry["id"]: "\n".join([entry["description"], *entry["examples"]])
                for entry in catalog
            }
            tools = [tool for entry in catalog for tool in entry["allowed_tools"]]
            self._index = _build_index(documents, self._identifiers(tools))
            self._index_key = key
            self.index_builds += 1
        return self._index

    def match(self, request: SelectionRequest) -> Match:
        """Rank the offered skills for the request's user turns and pick one, or none."""
        query = "\n".join(
            turn["content"] for turn in request.conversation if turn["role"] == "user"
        )
        scores = _scores(self.index_for(request.catalog), query)
        ranked = tuple(sorted(scores.items(), key=lambda item: (-item[1], item[0])))
        if not ranked or ranked[0][1] <= 0:
            return Match(ranked, None, 0.0, 0.0)
        best = ranked[0][1]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = best - second
        unique = margin > 0
        chosen = unique and best >= self.min_score and margin >= self.min_margin
        return Match(ranked, ranked[0][0] if chosen else None, best, margin)

    async def select(self, request: SelectionRequest, context: SelectionContext) -> dict[str, Any]:
        """The SelectionResult of the match; the context is not used."""
        found = self.match(request)
        alternatives = [
            name
            for name, score in found.ranked
            if name != found.skill and score > 0 and score >= self.min_score
        ]
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "candidate": (
                {"kind": "none"}
                if found.skill is None
                else {"kind": "skill", "skill_id": found.skill}
            ),
            "mechanism": self.mechanism,
            "confidence": None if found.skill is None else found.score,
            "selector": {"name": self.name, "version": self.version},
            "model_calls": [],
        }
        if alternatives:
            result["alternatives"] = alternatives
        return result
