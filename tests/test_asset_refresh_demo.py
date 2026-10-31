"""RM34/RM38/RM43 AC25/AC29/AC34: real demo has no shortened-clock path."""

import importlib.util
from pathlib import Path

import pytest

from relaymind.assets.settings import (
    AssetSettings,
    RecommendationSettings,
    RefreshSettings,
    SourceConfig,
)
from relaymind.launcher import LauncherConfig
from test_asset_remote_contracts import PROFILE


def api():
    spec = importlib.util.spec_from_file_location(
        "refresh_demo",
        Path(__file__).resolve().parents[1] / "scripts/probes/run_asset_refresh_demo.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config(tmp_path):
    return LauncherConfig(
        store_path=tmp_path / "db",
        assets=AssetSettings(
            (SourceConfig("public", "qualified_remote", None, PROFILE),),
            RefreshSettings(remote_seconds=300),
        ),
        recommendations=RecommendationSettings(
            enabled=True, user_id="demo", workspace_id="disposable"
        ),
    )


def test_demo_requires_real_explicit_300_second_remote_config_and_disposable_store(tmp_path):
    from dataclasses import replace

    module = api()
    module.validate_config(config(tmp_path), tmp_path)
    for changed in [
        replace(config(tmp_path), assets=AssetSettings()),
        replace(
            config(tmp_path),
            assets=replace(config(tmp_path).assets, refresh=RefreshSettings(remote_seconds=3600)),
        ),
        replace(config(tmp_path), recommendations=RecommendationSettings()),
        replace(config(tmp_path), store_path=tmp_path.parent / "outside"),
    ]:
        with pytest.raises(ValueError):
            module.validate_config(changed, tmp_path)
    (tmp_path / "db").touch()
    with pytest.raises(ValueError):
        module.validate_config(config(tmp_path), tmp_path)


def test_demo_has_no_arbitrary_url_or_fake_clock_arguments():
    module = api()
    for flag in ["--url", "--fake-clock", "--interval", "--qualified"]:
        with pytest.raises(SystemExit):
            module.arguments(["--config", "input", "--output", "capture", flag, "x"])
