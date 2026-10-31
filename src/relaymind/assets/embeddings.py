"""Bounded segmented embedding inputs and deterministic gateway arithmetic (RM41)."""

import hashlib
import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from relaymind.canonical import canonical_digest


class EmbeddingError(ValueError):
    """A fixed safe refusal; never backend exception text or private input."""


class SegmentTokenizer(Protocol):
    def encode_segment(self, text: str) -> Sequence[int]: ...
    def special_tokens(self, tokens: Sequence[int]) -> Sequence[int]: ...


@dataclass(frozen=True)
class Representation:
    revision: str
    segments: tuple[tuple[str, str], ...]
    data: bytes

    @property
    def byte_digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def segmentation_digest(self) -> str:
        return canonical_digest(
            dict(
                representation_revision=self.revision,
                segments=[dict(field=f, value=v) for f, v in self.segments],
            )
        )

    def token_ids(self, tokenizer: SegmentTokenizer, max_tokens: int) -> tuple[int, ...]:
        if type(max_tokens) is not int or not 1 <= max_tokens <= 8192:
            raise EmbeddingError("invalid_token_limit")
        tokens = []
        for index, (_, value) in enumerate(self.segments):
            if index > 1:
                tokens.extend(tokenizer.encode_segment("\n"))
            if value:
                tokens.extend(tokenizer.encode_segment(value))
            if len(tokens) > max_tokens:
                raise EmbeddingError("input_overflow")
        tokens = tuple(tokenizer.special_tokens(tokens))
        if len(tokens) > max_tokens or any(type(t) is not int or t < 0 for t in tokens):
            raise EmbeddingError("invalid_tokenization")
        return tokens


def _represent(revision, prefix_field, prefix, fields):
    if revision not in ("asset_fields.v1", "task_text.v1") or not isinstance(prefix, str):
        raise EmbeddingError("invalid_representation")
    try:
        if len(prefix.encode("utf-8")) > 256:
            raise EmbeddingError("input_overflow")
        values = tuple((field, unicodedata.normalize("NFC", value)) for field, value in fields)
        data = (prefix + "\n".join(value for _, value in values)).encode("utf-8")
    except (UnicodeError, TypeError):
        raise EmbeddingError("invalid_representation") from None
    if len(data) > 16384:
        raise EmbeddingError("input_overflow")
    return Representation(revision, ((prefix_field, prefix), *values), data)


def represent_query(
    task_text: str, prefix: str, *, revision: str = "asset_fields.v1"
) -> Representation:
    return _represent(revision, "query_prefix", prefix, [("/task_text", task_text)])


def identity_keys(kind: str) -> tuple[str, ...]:
    """The public identity fields embedded for one asset kind, in fixed order."""
    return ("package_id",) if kind == "skill" else ("server_id", "tool_name", "exposed_name")


def represent_record(record: Mapping, prefix: str) -> Representation:
    return _represent(
        "asset_fields.v1",
        "document_prefix",
        prefix,
        [("/description", record["description"])]
        + [(f"/identity/{key}", record["identity"][key]) for key in identity_keys(record["kind"])],
    )


def normalize_vectors(
    vectors: Sequence, *, count: int, dimensions: int
) -> tuple[tuple[float, ...], ...]:
    if (
        type(count) is not int
        or not 1 <= count <= 25
        or type(dimensions) is not int
        or not 1 <= dimensions <= 4096
        or not isinstance(vectors, Sequence)
        or len(vectors) != count
    ):
        raise EmbeddingError("invalid_vectors")
    result = []
    for vector in vectors:
        if not isinstance(vector, Sequence) or len(vector) != dimensions:
            raise EmbeddingError("invalid_vectors")
        try:
            if any(type(x) not in (int, float) for x in vector):
                raise EmbeddingError("invalid_vectors")
            values = [float(x) for x in vector]
            if any(not math.isfinite(x) for x in values):
                raise EmbeddingError("invalid_vectors")
            maximum = max(abs(x) for x in values)
            if maximum == 0:
                raise EmbeddingError("invalid_vectors")
            scaled = [x / maximum for x in values]
            total = 0.0
            for x in scaled:
                total = total + x * x
            norm = math.sqrt(total)
            normalized = tuple(x / norm for x in scaled)
        except (OverflowError, TypeError, ValueError):
            raise EmbeddingError("invalid_vectors") from None
        result.append(normalized)
    return tuple(result)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        raise EmbeddingError("invalid_vectors")
    total = 0.0
    for a, b in zip(left, right, strict=True):
        total = total + a * b
    if not math.isfinite(total):
        raise EmbeddingError("invalid_vectors")
    return max(-1.0, min(1.0, total))
