"""Launch settings from a manifest become llama-server arguments.

Covers RM18 / spec section 6: launch settings are manifest data, owned
in one place; the live smoke test derives its server argv and adapter
config from them instead of hand-copied constants. Issue #38.
"""

from pathlib import Path

from relaymind.adapters.manifest import launch_settings_argv, load_manifest

PINNED_MANIFEST = (
    Path(__file__).resolve().parent.parent / "models" / "qwen3-0.6b-q8_0.manifest.json"
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
        '{"enable_thinking": false}',
    ]


def test_false_boolean_settings_are_omitted():
    assert launch_settings_argv({"jinja": False, "ctx_size": 1024}) == ["--ctx-size", "1024"]


def test_pinned_manifest_produces_the_qualified_argv():
    # The exact launch line the artifact was qualified with (S2, S3):
    # drift between the manifest and what tests launch is the failure
    # mode the manifest exists to prevent.
    manifest = load_manifest(PINNED_MANIFEST)
    assert launch_settings_argv(manifest.launch_settings) == [
        "--ctx-size",
        "8192",
        "--jinja",
        "--chat-template-kwargs",
        '{"enable_thinking": false}',
    ]
