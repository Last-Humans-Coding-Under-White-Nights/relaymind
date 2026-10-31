"""RM12/RM43 AC34: checkout cannot change the original pinned rights bytes."""

import hashlib
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "path", ["LICENSE", "docs/evidence/2026-10-30-github-skill-source/LICENSE"]
)
def test_windows_git_checkout_preserves_captured_license_bytes(path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=true",
            "cat-file",
            "--filters",
            "--path=" + path,
            "HEAD:LICENSE",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    assert len(result.stdout) == 1079
    assert b"\r\n" not in result.stdout
    assert (
        hashlib.sha256(result.stdout).hexdigest()
        == "a0df745636ded7b39d42761f4e5000fb0718c04e98be6b004bf00efdcd79509a"
    )
    assert result.stdout == (root / path).read_bytes()
