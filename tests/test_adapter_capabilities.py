"""Adapter capability record and explicit capability checks.

Covers RM18 and spec section 6: every adapter exposes a capability
record, and the orchestrator rejects unsupported combinations
explicitly. Issue #1.
"""

import dataclasses

import pytest

from relaymind.adapters import AdapterCapabilities, UnsupportedCapabilityError


def make_capabilities(**overrides):
    base = dict(
        text_streaming=True,
        tool_calling=True,
        structured_routing_output=True,
        context_limit=32768,
        reasoning_controls=("enable_thinking",),
        usage_reporting=True,
        tokenizer_available=True,
    )
    base.update(overrides)
    return AdapterCapabilities(**base)


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


def test_require_context_rejects_oversized_input():
    caps = make_capabilities(context_limit=2048)
    caps.require_context(2048)
    with pytest.raises(UnsupportedCapabilityError):
        caps.require_context(2049)
