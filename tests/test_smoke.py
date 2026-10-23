"""Scaffold smoke test.

Keeps CI meaningful before the first feature lands. Real tests reference
requirement IDs from docs/SPECIFICATION.md (RM01-RM32, AC01-AC23).
"""

import relaymind


def test_package_imports_and_has_version():
    assert relaymind.__version__ == "0.1.0"
