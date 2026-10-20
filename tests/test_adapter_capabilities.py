"""Adapter capability record and explicit capability checks.

Covers RM18 and spec section 6: every adapter exposes a capability
record, and the orchestrator rejects unsupported combinations
explicitly. Issue #1.
"""

import dataclasses

import pytest

from helpers import make_capabilities
from relaymind.adapters import UnsupportedCapabilityError


def test_capability_record_is_immutable():
    caps = make_capabilities()
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.tool_calling = False


def test_require_passes_when_capabilities_present():
    caps = make_capabilities()
    caps.require("tool_calling", "text_streaming")


def test_require_raises_and_names_each_missing_capability():
    caps = make_capabilities(tool_calling=False, usage_reporting=False)
    with pytest.raises(UnsupportedCapabilityError) as exc:
        caps.require("tool_calling", "usage_reporting", "text_streaming")
    assert "tool_calling" in str(exc.value)
    assert "usage_reporting" in str(exc.value)
    assert "text_streaming" not in str(exc.value)


def test_require_rejects_unknown_capability_name():
    caps = make_capabilities()
    with pytest.raises(ValueError):
        caps.require("warp_speed")


def test_require_rejects_non_boolean_field_names():
    # context_limit and reasoning_controls are not flags; naming them in
    # require() must be an error, never a silent pass.
    caps = make_capabilities()
    with pytest.raises(ValueError):
        caps.require("context_limit")
    with pytest.raises(ValueError):
        caps.require("reasoning_controls")


def test_require_reasoning_control_checks_the_supported_list():
    caps = make_capabilities(reasoning_controls=("enable_thinking",))
    caps.require_reasoning_control("enable_thinking")
    with pytest.raises(UnsupportedCapabilityError) as exc:
        caps.require_reasoning_control("reasoning_effort")
    assert "reasoning_effort" in str(exc.value)


def test_require_context_rejects_oversized_input():
    caps = make_capabilities(context_limit=2048)
    caps.require_context(2048)
    with pytest.raises(UnsupportedCapabilityError):
        caps.require_context(2049)


def test_require_context_rejects_negative_token_count():
    caps = make_capabilities()
    with pytest.raises(ValueError):
        caps.require_context(-1)


def test_context_limit_must_be_positive():
    with pytest.raises(ValueError):
        make_capabilities(context_limit=0)


def test_reasoning_controls_reject_a_bare_string():
    with pytest.raises(TypeError):
        make_capabilities(reasoning_controls="enable_thinking")
