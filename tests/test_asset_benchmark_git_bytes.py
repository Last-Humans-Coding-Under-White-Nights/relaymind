"""RM16/RM40 AC31: Windows newline conversion must preserve hash-bound bytes."""

import subprocess
from pathlib import Path


def test_autocrlf_checkout_preserves_frozen_inputs_and_compiled_inventory(tmp_path):
    """Exercise actual Git conversion, not a textual assertion about attributes."""
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "seed"
    checkout = tmp_path / "checkout"
    source.mkdir()

    def git(*args, cwd=source):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-q")
    git("config", "core.autocrlf", "false")
    files = {
        p: (root / p).read_bytes()
        for p in (
            ".gitattributes",
            "docs/SPECIFICATION.md",
            "pyproject.toml",
            "scripts/asset_selection_benchmark.py",
            "src/relaymind/selection/asset_http.py",
            "src/relaymind/selection/schemas/selection_request.v2.json",
            "tests/fixtures/asset-benchmark/originals/calendar.md",
        )
    }
    files["tests/fixtures/asset-corpus/originals/exact.txt"] = b"original-one\noriginal-two\n"
    files["evaluation/assets/matrix-development/diagnostic.json"] = b'{"original":"retained"}\n'
    for path, payload in files.items():
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    git("add", ".")
    git(
        "-c",
        "user.name=Byte fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "Freeze byte fixture",
    )
    git("clone", "--no-checkout", "--no-local", str(source), str(checkout), cwd=tmp_path)
    git("config", "core.autocrlf", "true", cwd=checkout)
    git("checkout", "HEAD", "--", ".", cwd=checkout)
    # Attributes itself is not a compiled inventory member; all actual inputs
    # below must preserve original bytes rather than rehash a converted checkout.
    mismatches = [
        path
        for path, payload in files.items()
        if path != ".gitattributes" and (checkout / path).read_bytes() != payload
    ]
    assert mismatches == []
