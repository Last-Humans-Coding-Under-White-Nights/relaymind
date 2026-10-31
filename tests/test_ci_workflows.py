"""RM16: CI workflow files must parse before they can produce platform evidence."""

import re
from collections import Counter
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
TOP_LEVEL_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(?:\s|$)", re.MULTILINE)


def test_workflows_have_unique_top_level_keys():
    """A repeated permissions, concurrency, or other top-level key invalidates YAML."""
    paths = sorted((*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")))
    assert paths
    for path in paths:
        counts = Counter(TOP_LEVEL_KEY.findall(path.read_text(encoding="utf-8")))
        duplicates = {key: count for key, count in counts.items() if count > 1}
        assert not duplicates, f"{path.name}: duplicate top-level keys {duplicates}"
