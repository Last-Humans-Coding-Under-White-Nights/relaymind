"""RM34/AC25: closed catalog contracts and non-executable, safe projections."""

import copy
import hashlib
import json

import pytest

from relaymind.canonical import canonical_digest
from relaymind.redaction import Redactor


def record(**changes):
    value = {
        "schema_version": 1,
        "asset_id": "example:hello",
        "kind": "skill",
        "source_id": "example",
        "source_locator": "https://example.invalid/hello",
        "source_version": None,
        "captured_at": "2026-10-29T00:00:00Z",
        "content_hash": hashlib.sha256(b"hello").hexdigest(),
        "description": "Greeting skill",
        "language_tags": ["en", "zh-CN"],
        "dependencies": [],
        "availability": "unknown",
        "stale": False,
        "identity": {"package_id": "Hello World", "package_version": None},
    }
    value.update(changes)
    return value


def api():
    # Import here so the first RED reports the missing implementation as test failures.
    from relaymind.assets import catalog

    return catalog


def test_closed_record_and_frozen_copy():
    c = api()
    value = record()
    got = c.validate_asset_record(value)
    value["identity"]["package_id"] = "changed"
    assert got.to_json_object()["identity"]["package_id"] == "Hello World"
    with pytest.raises((TypeError, AttributeError)):
        got.identity["package_id"] = "changed"
    for key in record():
        value = record()
        del value[key]
        with pytest.raises(c.AssetValidationError):
            c.validate_asset_record(value)
    with pytest.raises(c.AssetValidationError):
        c.validate_asset_record(record(body="hidden instructions"))


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": 2},
        {"captured_at": "2026-10-29T01:00:00+01:00"},
        {"captured_at": "yesterday"},
        {"content_hash": "A" * 64},
        {"content_hash": "a" * 63},
        {"kind": "workflow"},
        {"availability": "installed"},
        {"stale": 0},
        {"source_version": ""},
        {"identity": {"package_id": "x"}},
        {"identity": {"package_id": "x", "package_version": None, "tool_name": "x"}},
        {"language_tags": ["en", "en"]},
        {"language_tags": ["中文"]},
        {"language_tags": ["a"] * 33},
        {"dependencies": ["x"] * 65},
        {"description": "中" * 2731},
        {"asset_id": "中" * 86},
        {"source_locator": "x" * 2049},
        {"description": "x\ud800"},
        {"description": "x\x00"},
        {"description": "x\x85"},
        {"description": "<script>alert(1)"},
        {"description": "<!-- unsafe"},
        {"description": "<?unsafe"},
        {"description": "</unsafe"},
        {"description": "same\n same \nsame\nsame"},
    ],
)
def test_invalid_record_rejected_without_echo(changes):
    c = api()
    with pytest.raises(c.AssetValidationError) as caught:
        c.validate_asset_record(record(**changes))
    assert "script" not in str(caught.value)


def test_mcp_schema_hash_and_offline_reference_boundary():
    c = api()
    schema = {
        "type": "object",
        "$defs": {"n": {"type": "string"}},
        "properties": {"password": {"$ref": "#/$defs/n"}},
    }
    identity = {
        "server_id": "real-server",
        "tool_name": "lookup",
        "exposed_name": "a__b__c",
        "input_schema": schema,
        "schema_hash": canonical_digest(schema),
    }
    value = record(kind="mcp_tool", identity=identity)
    got = c.validate_asset_record(value)
    assert got.identity["server_id"] == "real-server"
    assert c.project_asset(got, body=None, redactor=Redactor(), secrets=()).record == got
    identity["schema_hash"] = "0" * 64
    with pytest.raises(c.AssetValidationError):
        c.validate_asset_record(value)
    identity["input_schema"] = {"$ref": "https://example.invalid/secret"}
    identity["schema_hash"] = canonical_digest(identity["input_schema"])
    with pytest.raises(c.AssetValidationError) as caught:
        c.validate_asset_record(value)
    assert "example.invalid" not in str(caught.value)
    identity["input_schema"] = {"type": "imaginary"}
    identity["schema_hash"] = canonical_digest(identity["input_schema"])
    with pytest.raises(c.AssetValidationError):
        c.validate_asset_record(value)


def test_projection_normalizes_redacts_and_preserves_original_hash():
    c = api()
    value = record(description="Cafe\u0301 credential-987")
    original = copy.deepcopy(value)
    doc = c.project_asset(
        c.validate_asset_record(value),
        body="Use credential-987\nCafe\u0301",
        redactor=Redactor(),
        secrets={"credential-987"},
    )
    assert doc.record.description == "Café [redacted]"
    assert doc.body == "Use [redacted]\nCafé"
    assert doc.record.content_hash == original["content_hash"]
    assert value == original
    assert "body" not in doc.record.to_json_object()
    assert "credential-987" not in json.dumps(doc.to_json_object())
    with pytest.raises(c.AssetValidationError):
        c.project_asset(
            c.validate_asset_record(record(asset_id="credential-987")),
            body=None,
            redactor=Redactor(),
            secrets={"credential-987"},
        )
    with pytest.raises(c.AssetValidationError):
        c.project_asset(
            c.validate_asset_record(record()),
            body="x" * (256 * 1024 + 1),
            redactor=Redactor(),
            secrets=(),
        )


def test_snapshot_exact_shape_and_discovery_offer_limits():
    c = api()
    assets = [record(), record(asset_id="other:hello", source_id="other", stale=True)]
    sources = [
        dict(
            source_id=s,
            source_version=None,
            content_hash=None,
            captured_at=None,
            last_success_at=None,
            stale=True,
            complete=False,
            accepted_count=0,
            rejected_count=0,
            exclusions=[],
        )
        for s in ("example", "other")
    ]
    raw = dict(
        schema_version=1,
        catalog_revision=0,
        published_at="2026-10-29T00:00:00Z",
        sources=sources,
        assets=assets,
    )
    snapshot = c.validate_catalog_snapshot(raw)
    assert len(c.discovery_records(snapshot, frozenset({"skill"}))) == 2
    assert c.discovery_records(snapshot, frozenset({"mcp_tool"})) == ()
    for changes in (
        {"catalog_revision": True},
        {"catalog_revision": -1},
        {"assets": [assets[0], assets[0]]},
        {"sources": [sources[0], sources[0]]},
        {"host_snapshot_revision": None},
    ):
        with pytest.raises(c.AssetValidationError):
            c.validate_catalog_snapshot(raw | changes)
    many = [record(asset_id=f"example:{i}") for i in range(1001)]
    snapshot = c.validate_catalog_snapshot(raw | {"assets": many})
    with pytest.raises(c.AssetValidationError):
        c.discovery_records(snapshot, frozenset({"skill"}))
    # Full canonical offer, not merely descriptions, is subject to 32 MiB.
    schema = {"type": "object", "description": "x" * 64000}
    identity = dict(
        server_id="s",
        tool_name="t",
        exposed_name="s__t",
        input_schema=schema,
        schema_hash=canonical_digest(schema),
    )
    many = [record(asset_id=f"example:{i}", kind="mcp_tool", identity=identity) for i in range(530)]
    snapshot = c.validate_catalog_snapshot(raw | {"assets": many})
    with pytest.raises(c.AssetValidationError):
        c.discovery_records(snapshot, frozenset({"mcp_tool"}))


@pytest.mark.parametrize(
    "schema,secrets",
    [
        ({"type": "object", "description": "<script>untrusted"}, ()),
        (
            {"type": "object", "properties": {"credential-987": {"type": "string"}}},
            {"credential-987"},
        ),
    ],
)
def test_schema_text_is_safe_and_secret_property_names_never_persist(schema, secrets):
    c = api()
    identity = dict(
        server_id="s",
        tool_name="t",
        exposed_name="s__t",
        input_schema=schema,
        schema_hash=canonical_digest(schema),
    )
    with pytest.raises(c.AssetValidationError):
        c.project_asset(
            c.validate_asset_record(record(kind="mcp_tool", identity=identity)),
            body=None,
            redactor=Redactor(),
            secrets=secrets,
        )


def test_nfc_repetition_exact_utf8_boundaries_and_nonfinite_schema():
    c = api()
    assert c.validate_asset_record(record(description="中" * 2730)).description == "中" * 2730
    assert c.validate_asset_record(record(asset_id="中" * 85)).asset_id == "中" * 85
    assert (
        c.project_asset(
            c.validate_asset_record(record()),
            body="one\n\n\n\n two\n",
            redactor=Redactor(),
            secrets=(),
        ).body
        == "one\n\n\n\n two\n"
    )
    with pytest.raises(c.AssetValidationError):
        c.project_asset(
            c.validate_asset_record(record()),
            body="é\ne\u0301\né\ne\u0301",
            redactor=Redactor(),
            secrets=(),
        )
    schema = {"type": "number", "minimum": float("inf")}
    with pytest.raises(c.AssetValidationError):
        c.validate_asset_record(
            record(
                kind="mcp_tool",
                identity=dict(
                    server_id="s",
                    tool_name="t",
                    exposed_name="s__t",
                    input_schema=schema,
                    schema_hash="0" * 64,
                ),
            )
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"language_tags": ["en\n"]},
        {"content_hash": "a" * 64 + "\n"},
    ],
)
def test_record_and_snapshot_reject_non_whole_string_tags_and_hashes(changes):
    """RM34/AC25: exact tags/hashes are identical at record and snapshot boundaries."""
    c = api()
    with pytest.raises(c.AssetValidationError):
        c.validate_asset_record(record(**changes))
    value = dict(
        schema_version=1,
        catalog_revision=1,
        published_at="2026-10-29T00:00:00Z",
        sources=[
            dict(
                source_id="example",
                source_version=None,
                content_hash=None,
                captured_at=None,
                last_success_at=None,
                stale=False,
                complete=True,
                accepted_count=1,
                rejected_count=0,
                exclusions=[],
            )
        ],
        assets=[record(**changes)],
    )
    with pytest.raises(c.AssetValidationError):
        c.validate_catalog_snapshot(value)
