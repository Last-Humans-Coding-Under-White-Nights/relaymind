"""RM16: documentation links keep implementation and acceptance status reachable."""

import re
from pathlib import Path


def test_roadmap_heading_links_resolve():
    """RM16: changing a roadmap heading must not strand documentation links."""
    docs = Path(__file__).resolve().parents[1] / "docs"
    roadmap = (docs / "ROADMAP.md").read_text(encoding="utf-8")
    headings = re.findall(r"^#{1,6} (.+)$", roadmap, re.MULTILINE)
    anchors = set()
    for heading in headings:
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        anchor = slug
        suffix = 0
        while anchor in anchors:
            suffix += 1
            anchor = f"{slug}-{suffix}"
        anchors.add(anchor)

    broken = []
    for path in sorted(docs.rglob("*.md")):
        for anchor in re.findall(r"ROADMAP\.md#([^\s)\]>]+)", path.read_text(encoding="utf-8")):
            if anchor not in anchors:
                broken.append(f"{path.relative_to(docs)}: ROADMAP.md#{anchor}")
    assert not broken, "Broken roadmap links:\n" + "\n".join(broken)
