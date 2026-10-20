"""Launch settings from a manifest become llama-server arguments.

Covers RM18 / spec section 6: launch settings are manifest data, owned
in one place; the live smoke test derives its server argv and adapter
config from them instead of hand-copied constants. Issue #38.
"""

from collections import OrderedDict
from types import MappingProxyType

import pytest

from helpers import PINNED_MANIFEST_PATH
from relaymind.adapters.manifest import (
    ManifestError,
    launch_adapter_settings,
    launch_settings_argv,
    load_manifest,
)


def test_settings_map_to_flags_values_and_json():
    argv = launch_settings_argv(
        {
            "ctx_size": 8192,
            "jinja": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }
    )
    assert argv == [
        "--ctx-size",
        "8192",
        "--jinja",
        "--chat-template-kwargs",
        '{"enable_thinking":false}',
    ]


def test_false_boolean_settings_render_as_negated_flags():
    # A manifest must be able to turn off options that default to on.
    assert launch_settings_argv({"mmap": False, "warmup": False, "ctx_size": 1024}) == [
        "--no-mmap",
        "--no-warmup",
        "--ctx-size",
        "1024",
    ]


def test_any_mapping_type_is_rendered_as_compact_json():
    for value in (MappingProxyType({"a": 1, "b": [1, 2]}), OrderedDict(a=1, b=[1, 2])):
        assert launch_settings_argv({"chat_template_kwargs": value}) == [
            "--chat-template-kwargs",
            '{"a":1,"b":[1,2]}',
        ]


def test_floats_and_strings_are_passed_as_text():
    assert launch_settings_argv({"temp": 0.7, "cache_type_k": "q8_0"}) == [
        "--temp",
        "0.7",
        "--cache-type-k",
        "q8_0",
    ]


@pytest.mark.parametrize("value", [None, [1, 2], ("a",), {1, 2}, b"x"])
def test_values_without_a_defined_rendering_are_rejected(value):
    with pytest.raises(ManifestError, match="ctx_size"):
        launch_settings_argv({"ctx_size": value})


def test_mapping_values_that_are_not_json_are_rejected():
    with pytest.raises(ManifestError, match="chat_template_kwargs"):
        launch_settings_argv({"chat_template_kwargs": {"x": object()}})


@pytest.mark.parametrize(
    "key",
    [
        "host",
        "port",
        "model",
        "m",
        "hf_repo",
        "hf",
        "hfr",
        "hf_file",
        "hff",
        "model_url",
        "mu",
        "docker_repo",
        "dr",
        # Other weights llama-server loads beside the pinned artifact.
        "spec_draft_model",
        "model_draft",
        "md",
        "spec_draft_hf",
        "hf_repo_draft",
        "hfd",
        "hfrd",
        "mmproj",
        "mm",
        "mmproj_url",
        "mmu",
        "lora",
        "lora_scaled",
        "control_vector",
        "control_vector_scaled",
        "models_dir",
        "models_preset",
        # A replacement for the chat template the manifest hashed.
        "chat_template",
        "chat_template_file",
    ],
)
def test_harness_owned_keys_are_rejected(key):
    # The harness owns the bind address and the artifact path; a manifest
    # must not override them, or pick a model other than the pinned
    # artifact that verify_artifact hashed (RM18, spec section 6).
    with pytest.raises(ManifestError, match="reserved"):
        launch_settings_argv({key: "x"})


@pytest.mark.parametrize(
    "key",
    ["c", "", "ctx size", "-ctx", "ctx_", "--ctx", "no_mmap", "no-mmap", "CtxSize", "ctx-size", 7],
)
def test_keys_that_do_not_render_as_valid_long_flags_are_rejected(key):
    with pytest.raises(ManifestError):
        launch_settings_argv({key: 1})


def test_one_flag_has_one_spelling():
    # RM18: keys are snake_case only, so "ctx_size" and "ctx-size" cannot
    # both reach the server and disagree with the adapter's ctx_size.
    with pytest.raises(ManifestError, match="ctx-size"):
        launch_settings_argv({"ctx_size": 8192, "ctx-size": 512})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value):
    # RM18: NaN and Infinity have no text or JSON form llama-server parses.
    with pytest.raises(ManifestError, match="temp"):
        launch_settings_argv({"temp": value})
    with pytest.raises(ManifestError, match="chat_template_kwargs"):
        launch_settings_argv({"chat_template_kwargs": {"t": value}})


@pytest.mark.parametrize("value", ["enable_thinking=false", 123, True])
def test_chat_template_kwargs_must_be_an_object(value):
    # RM18: the adapter reads these kwargs as a mapping, so any other
    # value fails when the manifest is made, not when the suite runs.
    with pytest.raises(ManifestError, match="chat_template_kwargs"):
        launch_settings_argv({"chat_template_kwargs": value})


def test_pinned_manifest_produces_the_qualified_argv():
    # The exact launch line the artifact was qualified with (S2, S3):
    # drift between the manifest and what tests launch is the failure
    # mode the manifest exists to prevent.
    manifest = load_manifest(PINNED_MANIFEST_PATH)
    assert launch_settings_argv(manifest.launch_settings) == [
        "--ctx-size",
        "8192",
        "--jinja",
        "--chat-template-kwargs",
        '{"enable_thinking":false}',
    ]


def test_launch_adapter_settings_come_from_the_pinned_manifest():
    manifest = load_manifest(PINNED_MANIFEST_PATH)
    assert launch_adapter_settings(manifest.launch_settings) == (
        8192,
        {"enable_thinking": False},
    )


def test_launch_adapter_settings_default_template_kwargs_to_empty():
    assert launch_adapter_settings({"ctx_size": 4096}) == (4096, {})


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"ctx_size": "8192"},
        {"ctx_size": True},
        {"ctx_size": 0},
        {"ctx_size": 8192, "chat_template_kwargs": ["enable_thinking"]},
        {"ctx_size": 8192, "chat_template_kwargs": None},
        # Hyphen spellings would launch the server but be missed here.
        {"ctx-size": 8192},
        {"ctx_size": 8192, "chat-template-kwargs": {"enable_thinking": False}},
        # The adapter only uses settings the server argv also accepts.
        {"ctx_size": 8192, "model": "x"},
    ],
)
def test_launch_adapter_settings_reject_unusable_settings(settings):
    with pytest.raises(ManifestError):
        launch_adapter_settings(settings)
