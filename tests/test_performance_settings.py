"""The performance options and the draft guards (RM31, RM32, spec 0.32 section 14.1).

Every default is the behavior before spec 0.31 and the latency baseline
of section 12. An option the gateway cannot honor yet is refused, never
accepted and ignored.
"""

import dataclasses

import pytest

from relaymind.orchestrator.performance import (
    PerformanceSettings,
    draft_in_language,
    holds_tool_call_json,
    non_default_answer_options,
)
from relaymind.orchestrator.route_decision import Language


def test_the_defaults_are_the_baseline_configuration():
    """Section 14.1: final, one, no prefetch, buffered, no early headers."""
    settings = PerformanceSettings()
    assert settings.answer_pass == "final"
    assert settings.calls_per_generation == "one"
    assert settings.prefetch is False
    assert settings.answer_stream == "buffered"
    assert settings.early_headers is False


def test_the_json_object_is_what_run_accepted_records():
    """Section 8 (spec 0.32): the five options in force, as plain JSON values."""
    assert PerformanceSettings().to_json_object() == {
        "answer_pass": "final",
        "answer_stream": "buffered",
        "calls_per_generation": "one",
        "prefetch": False,
        "early_headers": False,
    }
    chosen = PerformanceSettings(
        answer_pass="draft", calls_per_generation="many", prefetch=True, early_headers=True
    )
    assert chosen.to_json_object() == {
        "answer_pass": "draft",
        "answer_stream": "buffered",
        "calls_per_generation": "many",
        "prefetch": True,
        "early_headers": True,
    }


def test_the_settings_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        PerformanceSettings().answer_pass = "draft"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("answer_pass", "later"),
        ("answer_pass", ""),
        ("answer_pass", None),
        ("answer_pass", True),
        ("calls_per_generation", "two"),
        ("calls_per_generation", 1),
        ("answer_stream", "stream"),
        ("answer_stream", None),
        ("prefetch", "yes"),
        ("prefetch", 1),
        ("prefetch", None),
        ("early_headers", "true"),
        ("early_headers", 0),
        ("early_headers", None),
    ],
)
def test_a_bad_value_names_the_option(field, value):
    """Section 14.1: a wrong value or type stops startup with a ValueError naming the key."""
    with pytest.raises(ValueError, match=field):
        PerformanceSettings(**{field: value})


def test_live_answer_streaming_is_refused_until_it_exists():
    """RM32: answer_stream live is a spec value, but no code forwards a gated stream yet.

    Accepting it would be an option that silently does nothing.
    """
    with pytest.raises(ValueError, match="answer_stream.*live.*not implemented"):
        PerformanceSettings(answer_stream="live")


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("staging-api is healthy.", Language.EN, True),
        ("staging-api 的状态是 healthy。", Language.EN, False),
        ("staging-api 的状态是 healthy。", Language.ZH, True),
        ("staging-api is healthy.", Language.ZH, False),
    ],
)
def test_the_draft_must_be_in_the_run_language(text, language, expected):
    """RM31: a Chinese run needs Chinese text, an English run needs none (identifiers.has_cjk)."""
    assert draft_in_language(text, language) is expected


@pytest.mark.parametrize(
    ("text", "language", "expected"),
    [
        ("staging-api is healthy.", "en", True),
        ("staging-api 的状态是 healthy。", "en", False),
        ("staging-api 的状态是 healthy。", "zh", True),
        ("staging-api is healthy.", "zh", False),
    ],
)
def test_the_draft_language_compares_by_value(text, language, expected):
    """RM31: a plain "zh" string is the Chinese run, as the Language member is."""
    assert draft_in_language(text, language) is expected


def test_an_unknown_draft_language_is_refused():
    """RM31: a language outside spec section 1 is an error, never read as English."""
    with pytest.raises(ValueError):
        draft_in_language("staging-api is healthy.", "fr")


@pytest.mark.parametrize(
    "text",
    [
        '{"name": "get_service_health", "arguments": {"service": "staging-api"}}',
        '{"tool_name": "get_service_health", "parameters": {"service": "x"}}',
        '{"function": "get_service_health", "arguments": {}}',
        '<tool_call>{"name": "x"}</tool_call>',
        "ok </TOOL_CALL>",
    ],
)
def test_tool_call_json_in_a_draft_is_found(text):
    """RM31: the draft holds no tool-call JSON or tag."""
    assert holds_tool_call_json(text)


@pytest.mark.parametrize(
    "text",
    [
        "staging-api is healthy (checked at 2026-10-20T02:58:00Z).",
        'The "name" of the service is staging-api.',
        'The service has "arguments" for the tool.',
    ],
)
def test_ordinary_answers_are_not_tool_call_json(text):
    assert not holds_tool_call_json(text)


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({}, {}),
        ({"temperature": 1}, {}),
        ({"temperature": 1.0, "top_p": 1}, {}),
        ({"temperature": 0.2}, {"temperature": 0.2}),
        ({"top_p": 0.9, "temperature": 1}, {"top_p": 0.9}),
        # max_completion_tokens has no API default: any value sent is non-default.
        ({"max_completion_tokens": 32768}, {"max_completion_tokens": 32768}),
    ],
)
def test_only_non_default_answer_options_count(record, expected):
    """RM31, section 7.1: temperature 1 and top_p 1 are the API defaults."""
    assert non_default_answer_options(record) == expected
