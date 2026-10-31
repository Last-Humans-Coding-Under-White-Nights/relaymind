"""RM34/RM43 AC25/AC34: only the reviewed installed profile grants acquisition."""

import pytest

from relaymind.assets.settings import parse_asset_settings

PROFILE = "github.relaymind-skills.v1"


def source(**changes):
    return (
        dict(
            source_id="public",
            kind="qualified_remote",
            path=None,
            adapter_contract=PROFILE,
            secret_ref=None,
        )
        | changes
    )


def test_installed_public_profile_and_env_reference_are_closed(tmp_path):
    settings = parse_asset_settings(
        {"sources": [source(secret_ref="env:FIXTURE_TOKEN")]},
        {"remote_seconds": 300},
        config_dir=tmp_path,
    )
    assert settings.sources[0].adapter_contract == PROFILE and settings.sources[0].path is None
    from relaymind.assets.remote_contracts import resolve_remote_profile

    profile = resolve_remote_profile(PROFILE)
    assert profile.minimum_poll_seconds == 300
    assert (
        profile.rights_sha256 == "a0df745636ded7b39d42761f4e5000fb0718c04e98be6b004bf00efdcd79509a"
    )


@pytest.mark.parametrize(
    "changes",
    [
        dict(adapter_contract="https://example.test/"),
        dict(adapter_contract="arbitrary.module"),
        dict(path="/tmp/input"),
        dict(qualified=True),
        dict(url="https://api.github.com/"),
        dict(secret_ref="plaintext"),
        dict(secret_ref="env:a\nb"),
        dict(secret_ref="env:" + "x" * 129),
        dict(adapter_contract=None),
    ],
)
def test_caller_cannot_self_qualify_or_change_origin(tmp_path, changes):
    with pytest.raises(ValueError):
        parse_asset_settings({"sources": [source(**changes)]}, {}, config_dir=tmp_path)


def test_duplicate_profile_and_too_fast_polling_refuse(tmp_path):
    for records, refresh in [
        ([source(), source(source_id="renamed")], {}),
        ([source()], {"remote_seconds": 299}),
    ]:
        with pytest.raises(ValueError):
            parse_asset_settings({"sources": records}, refresh, config_dir=tmp_path)
