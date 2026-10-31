"""RM34/RM40, AC25/AC31: selected-text benchmark inspection never installs a ZIP."""

import importlib
import io
import stat
import struct
import zipfile

import pytest


def inspect(payload):
    try:
        module = importlib.import_module("relaymind.evaluation.asset_archive")
    except ModuleNotFoundError:
        pytest.fail("RM40 bounded benchmark archive inspector is missing")
    return module.inspect_skill_archive(payload)


def archive(entries, *, compression=zipfile.ZIP_STORED):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=compression) as file:
        for name, value in entries:
            file.writestr(name, value)
    return out.getvalue()


def mutate_directory(payload, field, value):
    data = bytearray(payload)
    offset = data.index(b"PK\x01\x02")
    struct.pack_into("<I", data, offset + field, value)
    return bytes(data)


def test_rm40_ac31_reads_only_selected_ordinary_text_and_preserves_original_hash():
    import hashlib

    payload = archive(
        [
            ("SKILL.md", b"---\nname: actual-skill\n---\nOriginal body."),
            ("LICENSE", b"License text"),
            ("NOTICE.md", b"Attribution"),
            ("scripts/untrusted.py", b'raise RuntimeError("NEVER RUN")'),
        ]
    )
    result = inspect(payload)
    assert result.original_sha256 == hashlib.sha256(payload).hexdigest()
    assert result.selected["SKILL.md"].endswith(b"Original body.")
    assert result.selected["LICENSE"] == b"License text"
    assert result.selected["NOTICE.md"] == b"Attribution"
    assert "scripts/untrusted.py" not in result.selected
    assert result.entry_count == 4
    with pytest.raises(TypeError):
        result.selected["SKILL.md"] = b"replaced"


@pytest.mark.parametrize(
    "name",
    ["/escape", "../escape", "nested/../escape", "C:/escape", "nested\\escape", "./SKILL.md"],
)
def test_rm40_ac31_rejects_unsafe_names_even_on_opaque_members(name):
    # ZipInfo normalizes backslashes while writing on Windows. Put the unsafe
    # bytes in both ZIP headers so this always tests the inspector's boundary.
    if name == "nested\\escape":
        payload = archive([("SKILL.md", b"Original"), ("nested/escape", b"opaque")])
        assert payload.count(b"nested/escape") == 2
        payload = payload.replace(b"nested/escape", b"nested\\escape")
        assert payload.count(b"nested\\escape") == 2
    else:
        payload = archive([("SKILL.md", b"Original"), (name, b"opaque")])
    with pytest.raises(ValueError, match="unsafe_archive_path"):
        inspect(payload)


@pytest.mark.parametrize("names", [("a", "a"), ("a", "A"), ("caf\u00e9", "cafe\u0301")])
def test_rm40_ac31_rejects_duplicate_or_normalized_case_colliding_names(names):
    with (
        pytest.warns(UserWarning)
        if names[0] == names[1]
        else __import__("contextlib").nullcontext()
    ):
        payload = archive([("SKILL.md", b"Original"), *[(name, b"x") for name in names]])
    with pytest.raises(ValueError, match="duplicate_archive_path"):
        inspect(payload)


def test_rm40_ac31_rejects_nul_hidden_in_central_and_local_names():
    payload = archive([("SKILL.md", b"Original"), ("bad-name", b"x")])
    payload = payload.replace(b"bad-name", b"bad\x00name")
    with pytest.raises(ValueError, match="unsafe_archive_path"):
        inspect(payload)


@pytest.mark.parametrize("mode", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR])
def test_rm40_ac31_rejects_nonordinary_entries_before_reading(mode):
    entry = zipfile.ZipInfo("unsafe")
    entry.create_system = 3
    entry.external_attr = (mode | 0o777) << 16
    with pytest.raises(ValueError, match="unsafe_archive_entry"):
        inspect(archive([("SKILL.md", b"Original"), (entry, b"opaque")]))


def test_rm40_ac31_rejects_encrypted_archive_flags():
    payload = bytearray(archive([("SKILL.md", b"Original")]))
    payload[6] |= 1
    payload[payload.index(b"PK\x01\x02") + 8] |= 1
    with pytest.raises(ValueError, match="encrypted_archive"):
        inspect(bytes(payload))


@pytest.mark.parametrize(
    "kind", ["compressed", "entries", "expanded", "skill", "metadata", "ratio"]
)
def test_rm40_ac31_archive_limits_reject_without_truncating(kind):
    if kind == "compressed":
        payload = b"x" * (10 * 1024 * 1024 + 1)
    elif kind == "entries":
        payload = archive([("SKILL.md", b"x"), *[(f"opaque/{n}", b"") for n in range(1000)]])
    elif kind == "expanded":
        payload = mutate_directory(archive([("SKILL.md", b"x")]), 24, 20 * 1024 * 1024 + 1)
    elif kind == "skill":
        payload = archive([("SKILL.md", b"x" * (256 * 1024 + 1))])
    elif kind == "metadata":
        payload = archive([("SKILL.md", b"x"), ("LICENSE", b"x" * (4 * 1024 * 1024 + 1))])
    else:
        payload = archive([("SKILL.md", b"x" * 100000)], compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(ValueError, match="archive_limit"):
        inspect(payload)


def test_rm40_ac31_valid_deflate_and_ordinary_opaque_members():
    payload = archive(
        [("SKILL.md", bytes(range(256)) * 10), ("subdir/", b""), ("opaque.bin", b"not inspected")],
        compression=zipfile.ZIP_DEFLATED,
    )
    assert inspect(payload).selected["SKILL.md"] == bytes(range(256)) * 10


@pytest.mark.parametrize("kind", ["corrupt_crc", "truncated", "missing_skill", "inconsistent_size"])
def test_rm40_ac31_malformed_or_incomplete_archive_rejected(kind):
    payload = archive([("SKILL.md", b"Original body")])
    if kind == "corrupt_crc":
        payload = payload.replace(b"Original body", b"CORRUPT! body")
    elif kind == "truncated":
        payload = payload[:-10]
    elif kind == "missing_skill":
        payload = archive([("README.md", b"not a skill")])
    else:
        payload = mutate_directory(payload, 24, 1)
    with pytest.raises(ValueError):
        inspect(payload)


@pytest.mark.parametrize("entry", ["SKILL.md", "opaque.bin"])
@pytest.mark.parametrize(
    "offset,fmt,value", [(14, "I", 0), (18, "I", 1), (22, "I", 1), (8, "H", 8), (6, "H", 1)]
)
def test_rm40_ac31_rejects_local_header_contradictions_before_selected_reads(
    entry, offset, fmt, value
):
    """RM34/RM40: central-directory truth cannot hide contradictory local metadata."""
    payload = bytearray(archive([("SKILL.md", b"Original body"), ("opaque.bin", b"Opaque body")]))
    with zipfile.ZipFile(io.BytesIO(payload)) as file:
        at = file.getinfo(entry).header_offset
    struct.pack_into("<" + fmt, payload, at + offset, value)
    with pytest.raises(ValueError, match="malformed_archive|encrypted_archive"):
        inspect(bytes(payload))


def descriptor_archive(compression):
    class Unseekable(io.BytesIO):
        def seekable(self):
            return False

        def seek(self, *args):
            raise io.UnsupportedOperation

    out = Unseekable()
    with zipfile.ZipFile(out, "w", compression=compression) as file:
        file.writestr("SKILL.md", b"Original body")
        file.writestr("opaque.bin", b"Opaque body")
    return out.getvalue()


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_rm40_ac31_valid_deferred_crc_sizes_supported(compression):
    assert inspect(descriptor_archive(compression)).selected["SKILL.md"] == b"Original body"


@pytest.mark.parametrize("entry", ["SKILL.md", "opaque.bin"])
def test_rm40_ac31_corrupt_data_descriptor_is_rejected_even_for_opaque_entry(entry):
    payload = bytearray(descriptor_archive(zipfile.ZIP_STORED))
    with zipfile.ZipFile(io.BytesIO(payload)) as file:
        info = file.getinfo(entry)
        descriptor = info.header_offset + 30 + len(entry) + info.compress_size
    assert payload[descriptor : descriptor + 4] == b"PK\x07\x08"
    struct.pack_into("<I", payload, descriptor + 4, 0)
    with pytest.raises(ValueError, match="malformed_archive"):
        inspect(bytes(payload))
