"""Reviewed exact-identity local activation authority (RM41)."""

from relaymind.assets.embedding_manifest import EmbeddingManifest
from relaymind.assets.embeddings import EmbeddingError

# Rights, original bytes and actual Linux runtime/device controls were reviewed
# for these exact two identities. This is not a quality or platform-wide claim.
# A config/environment/local JSON cannot populate this registry.
QUALIFIED_IDENTITIES: frozenset[tuple] = frozenset(
    {
        (
            "420a2fcd5564ade1d81216a8c85947267300d5cc09a1e888d4bcd5361b7b13fa",
            "8fc6b9e9717fc20ba6130237f03f3a4aef8a656f45650b3638575296ba5eda2f",
            "onnx_e5_cpu",
            "1.31.1",
            "f3543633fa2f66713cc90563a862af952d40f55cfc70b0d95cf25f4deba07eb1",
            "cpu:Linux:x86_64:Intel Xeon Processor (Skylake, IBRS)",
        ),
        (
            "420a2fcd5564ade1d81216a8c85947267300d5cc09a1e888d4bcd5361b7b13fa",
            "7b3cb420944f5b933526ffa862221b41baecdc30e8a3e99f91dc6ea201eea121",
            "onnx_e5_cpu",
            "1.31.1",
            "f3543633fa2f66713cc90563a862af952d40f55cfc70b0d95cf25f4deba07eb1",
            "cpu:Linux:x86_64:Intel Xeon Processor (Skylake, IBRS)",
        ),
    }
)


def qualification_key(manifest: EmbeddingManifest, semantic_digest: str) -> tuple:
    raw = manifest.data
    return (
        manifest.manifest_digest,
        semantic_digest,
        raw["runtime"]["backend"],
        raw["runtime"]["version"],
        raw["runtime"]["build"],
        raw["device"],
    )


def require_qualified(manifest: EmbeddingManifest, semantic_digest: str) -> None:
    if qualification_key(manifest, semantic_digest) not in QUALIFIED_IDENTITIES:
        raise EmbeddingError("unqualified_artifact")
