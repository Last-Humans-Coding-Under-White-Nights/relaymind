"""Identifier provenance shared by the router and the loop (RM05, section 5).

RM05: resource identifiers come from the request, established context,
or tool results, never from a guess. Spec 0.6 section 5: only user turns
and tool results count; the gateway's own question is not provenance.
"""

import pytest

from helpers import make_demo_tools
from relaymind.adapters import Message, Role
from relaymind.orchestrator import identifiers
from relaymind.tools import ToolResult, ToolStatus


def test_provenance_is_user_turns_and_tool_results_only():
    messages = (
        Message(role=Role.SYSTEM, content="system names sys-db"),
        Message(role=Role.USER, content="check staging-api"),
        Message(role=Role.ASSISTANT, content="Options: payments-api"),
    )
    result = ToolResult(
        operation_id="op_1",
        tool_name="list_demo_databases",
        status=ToolStatus.SUCCEEDED,
        data={"databases": [{"database_id": "demo-orders-db"}]},
        error=None,
        observed_at="2026-10-20T03:00:00Z",
        duration_ms=1,
        source_ref="tool:list_demo_databases/op_1",
        truncated=False,
    )
    text = identifiers.provenance_text(messages, [result])
    assert identifiers.mentions(text, "staging-api")
    assert identifiers.mentions(text, "demo-orders-db")
    assert not identifiers.mentions(text, "payments-api")
    assert not identifiers.mentions(text, "sys-db")


def test_a_value_inside_a_longer_id_is_not_a_mention():
    assert not identifiers.mentions("staging-api-2 is up", "staging-api")


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"enum": ["staging-api", "payments-api"]}, True),
        ({"type": "string", "pattern": "^[a-z0-9-]+$"}, True),
        ({"enum": [15, 60, 1440]}, False),
        ({"type": "integer", "minimum": 0}, False),
        ({"type": "string"}, False),
    ],
)
def test_identifier_like_properties(spec, expected):
    assert identifiers.is_identifier_property(spec) is expected


def test_every_identifier_like_argument_needs_provenance():
    schema = {
        "properties": {
            "database_id": {"type": "string", "pattern": "^[a-z0-9-]+$"},
            "expected_revision": {"type": "integer"},
        }
    }
    arguments = {"database_id": "demo-billing-db", "expected_revision": 3}
    assert identifiers.unsupported_identifiers(arguments, schema, "demo-orders-db") == [
        "database_id"
    ]
    assert identifiers.unsupported_identifiers(arguments, schema, "demo-billing-db") == []


def test_the_label_is_the_schema_title_or_the_name():
    schema = {"properties": {"service": {"title": "service ID"}, "window_minutes": {}}}
    assert identifiers.label(schema, "service") == "service ID"
    assert identifiers.label(schema, "window_minutes") == "window_minutes"


def test_the_label_follows_the_language():
    """Section 5 and 12.2: the clarification names the missing input in the
    run's language, from the schema's x-relaymind-label (#55). A language
    without a label falls back to the title, then the name."""
    schema = {
        "properties": {
            "service": {"title": "service", "x-relaymind-label": {"en": "service", "zh": "服务"}},
            "database_id": {"title": "database"},
            "other": {"x-relaymind-label": {"zh": " "}},
        }
    }
    assert identifiers.label(schema, "service", "zh") == "服务"
    assert identifiers.label(schema, "service", "en") == "service"
    assert identifiers.label(schema, "database_id", "zh") == "database"
    assert identifiers.label(schema, "other", "zh") == "other"


def test_the_published_demo_labels_are_used(tmp_path):
    """The demo tool schemas carry the bilingual labels (#55)."""
    schema = make_demo_tools(tmp_path).get("get_service_health").declaration.input_schema
    assert identifiers.label(schema, "service", "zh") == "服务"
    assert identifiers.label(schema, "service", "en") == "service"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("staging-api", True),
        ("demo-orders-db, 3。", True),
        ("yes", True),
        ("好的", False),
        ("yes please", False),
    ],
)
def test_identifier_only_text(text, expected):
    assert identifiers.is_identifier_only(text) is expected


# --- Review fixes: single-word replies, separators, labels across schemas


@pytest.mark.parametrize("reply", ["auth", "redis", "server1", "db1", " orders. "])
def test_a_single_bare_word_is_identifier_only(reply):
    """Section 5 (spec 0.6): the run language is kept across a resume, so a
    one-word reply such as auth or redis, which may be a resource name,
    is not language evidence."""
    assert identifiers.is_identifier_only(reply) is True


@pytest.mark.parametrize(
    "text",
    ["staging-api.backup", "staging-api:2", "staging-api/v2", "v2/staging-api", "eu.staging-api"],
)
def test_a_value_joined_to_a_longer_token_by_a_separator_is_not_a_mention(text):
    """RM05: ., :, and / join identifier parts, so staging-api inside
    staging-api.backup names another resource."""
    assert not identifiers.mentions(text, "staging-api")


@pytest.mark.parametrize(
    "text",
    ["Check staging-api.", "staging-api: is it up?", "(staging-api)", "staging-api/ payments-api"],
)
def test_punctuation_after_a_value_still_counts_as_a_mention(text):
    """RM05: sentence punctuation after an ID still names it."""
    assert identifiers.mentions(text, "staging-api")


def test_the_label_is_taken_from_any_schema_that_has_one():
    """Section 5: the clarification label comes from the schemas in the
    run's language; a first schema without a label does not hide the
    label another allowed tool declares."""
    bare = {"properties": {"service": {"enum": ["staging-api"]}}}
    titled = {"properties": {"service": {"enum": ["staging-api"], "title": "service ID"}}}
    labelled = {
        "properties": {"service": {"enum": ["staging-api"], "x-relaymind-label": {"zh": "服务"}}}
    }
    assert identifiers.label_in([bare, titled, labelled], "service", "zh") == "服务"
    assert identifiers.label_in([bare, titled, labelled], "service", "en") == "service ID"
    assert identifiers.label_in([bare], "service", "zh") == "service"
    assert identifiers.label_in([], "service", "en") == "service"
