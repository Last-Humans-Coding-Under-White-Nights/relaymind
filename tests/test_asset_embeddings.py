"""RM38/RM41, AC29/AC32: exact segmented identities and bounded numeric work."""

import math

import pytest

from relaymind.canonical import canonical_digest
from test_asset_catalog import record


def api():
    from relaymind.assets import embeddings

    return embeddings


class SegmentTokenizer:
    def encode_segment(self, text):
        return [len(text)] if text else []

    def special_tokens(self, tokens):
        return [101, *tokens, 102]


def test_lf_collision_preserves_bytes_but_refuses_vector_identity_reuse():
    a = api().represent_record(
        record(description="a\nb", identity={"package_id": "c", "package_version": None}), ""
    )
    b = api().represent_record(
        record(description="a", identity={"package_id": "b\nc", "package_version": None}), ""
    )
    assert a.data == b.data == b"a\nb\nc"
    assert (
        a.byte_digest
        == b.byte_digest
        == "ea7fb08b7a2dc4619ffb7c7bb38d95a2047935fa165d71b12efd3852a2e6d0cc"
    )
    assert a.segmentation_digest != b.segmentation_digest
    assert a.token_ids(SegmentTokenizer(), 10) == (101, 3, 1, 1, 102)
    assert b.token_ids(SegmentTokenizer(), 10) == (101, 1, 1, 3, 102)


def test_query_prefix_is_opaque_fields_are_nfc_and_empty_prefix_is_bound():
    value = api().represent_query("e\u0301中文", "e\u0301: ")
    assert value.data == "e\u0301: é中文".encode()
    assert value.segmentation_digest == canonical_digest(
        {
            "representation_revision": "asset_fields.v1",
            "segments": [
                {"field": "query_prefix", "value": "e\u0301: "},
                {"field": "/task_text", "value": "é中文"},
            ],
        }
    )
    assert (
        api().represent_query("é中文", "e\u0301: ").segmentation_digest == value.segmentation_digest
    )
    assert (
        api().represent_query("é中文", "e\u0301: ", revision="task_text.v1").segmentation_digest
        != value.segmentation_digest
    )
    with pytest.raises(api().EmbeddingError):
        value.token_ids(SegmentTokenizer(), 3)
    with pytest.raises(api().EmbeddingError):
        api().represent_query("界" * 6000, "")


@pytest.mark.parametrize(
    "vectors,dimensions",
    [
        ([], 2),
        ([[True, 0]], 2),
        ([[0, 0]], 2),
        ([[math.inf, 1]], 2),
        ([[1, math.nan]], 2),
        ([[1]], 2),
        ([[1, 2, 3]], 2),
        ([[[1], 2]], 2),
    ],
)
def test_invalid_vector_output_is_refused(vectors, dimensions):
    with pytest.raises(api().EmbeddingError):
        api().normalize_vectors(vectors, count=1, dimensions=dimensions)


def test_cosine_normalizes_without_overflow_and_keeps_dimension_order():
    a, b = api().normalize_vectors([[3e300, 4e300], [-4, 3]], count=2, dimensions=2)
    assert a == (0.6, 0.8)
    assert api().cosine(a, a) == 1.0
    assert abs(api().cosine(a, b)) < 1e-15
