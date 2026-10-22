"""The cache.routing launcher setting (RM24, spec 0.26 sections 10 and 14.1)."""

import pytest

from helpers import REPO_ROOT, ScriptedAdapter
from launcher_helpers import write_config
from relaymind.launcher import LaunchError, build_app, load_config


def doubles():
    return {"local_adapter": ScriptedAdapter(turns=[]), "external_adapter": None}


def test_the_routing_cache_is_off_without_the_table(tmp_path):
    """RM24: off by default."""
    config = load_config(write_config(tmp_path))
    assert config.routing_cache is False
    built = build_app(config, **doubles())
    assert built.orchestrator.routing_cache is None


def test_routing_false_is_off(tmp_path):
    config = load_config(write_config(tmp_path, "\n[cache]\nrouting = false\n"))
    assert config.routing_cache is False


def test_routing_true_gives_the_orchestrator_a_cache(tmp_path):
    """RM24: the operator turns the cache on in the launcher file."""
    config = load_config(write_config(tmp_path, "\n[cache]\nrouting = true\n"))
    assert config.routing_cache is True
    built = build_app(config, **doubles())
    assert built.orchestrator.routing_cache is not None
    assert len(built.orchestrator.routing_cache) == 0


@pytest.mark.parametrize(
    "extra",
    [
        "\n[cache]\nrouting = 'yes'\n",
        "\n[cache]\nttl_seconds = 5\n",
        "\n[cache]\nmax_entries = 2\n",
    ],
)
def test_a_bad_cache_table_is_a_startup_error(tmp_path, extra):
    """Section 14.1: wrong types and unknown keys stop startup; the limits are fixed."""
    with pytest.raises(LaunchError):
        load_config(write_config(tmp_path, extra))


def test_the_example_file_documents_the_setting_and_leaves_it_off():
    """Section 14.1: the example exposes the setting, off."""
    text = (REPO_ROOT / "relaymind.example.toml").read_text(encoding="utf-8")
    assert "[cache]" in text and "routing = false" in text
    assert load_config(REPO_ROOT / "relaymind.example.toml").routing_cache is False
