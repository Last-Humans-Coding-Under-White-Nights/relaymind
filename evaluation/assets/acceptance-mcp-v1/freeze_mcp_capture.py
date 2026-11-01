"""Freeze a tools/list capture into the acceptance MCP tool set (spec 0.66 section 12.6.6).

usage: python freeze_mcp_capture.py CAPTURE_DIR SERVERS_JSON OUT_DIR CAPTURED_AT

Reads ``capture.raw.json`` and the originals/licenses written by
``capture_mcp_tools.py`` and writes into OUT_DIR:

- ``originals/`` and ``licenses/`` (exact bytes, unchanged);
- ``capture.v1.json``: servers (pinned package, launch arguments with local
  paths replaced by placeholders, serverInfo, license evidence), every captured
  tool with its original's SHA-256 and canonical ``schema_hash``, and the
  selection of exactly 100 tools by a fixed rule;
- ``lock.v1.json``: the canonical digest of ``capture.v1.json`` and the
  SHA-256 and length of every file it references.

Selection rule: order every captured tool by SHA-256 of the UTF-8 text
``<server_id>/<tool_name>`` and keep the first 100; the rest are recorded as
excluded with reason ``beyond_quota``. No tool is edited, translated or made up.
"""

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

from relaymind.canonical import canonical_digest

QUOTA = 100


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _placeholder(arg: str) -> str:
    arg = re.sub(r"^.*/node_modules/", "<npm>/node_modules/", arg)
    arg = re.sub(r"^.*/bin/node$", "<node>", arg)
    arg = re.sub(r"^.*/py/bin/python$", "<python>", arg)
    arg = re.sub(r"^.*/mcp/(fsroot|gitrepo)$", r"<\1>", arg)
    return arg


def main(capture_dir: str, servers_json: str, out_dir: str, captured_at: str) -> int:
    capture_dir, out = Path(capture_dir), Path(out_dir)
    captured = json.loads((capture_dir / "capture.raw.json").read_text(encoding="utf-8"))
    launches = {s["id"]: s for s in json.loads(Path(servers_json).read_text(encoding="utf-8"))}
    for name in ("originals", "licenses"):
        shutil.copytree(capture_dir / name, out / name, dirs_exist_ok=False)
    servers, tools = [], []
    for server in captured:
        launch = launches[server["id"]]
        servers.append(
            dict(
                id=server["id"],
                package=server["package"],
                launch=[_placeholder(a) for a in launch["argv"]],
                source_url=server["source_url"],
                license=server["license"],
                server_info=server["server_info"],
                protocol_version=server["protocol_version"],
                originals=server["originals"],
            )
        )
        digests = {o["path"]: o["sha256"] for o in server["originals"]}
        for tool in server["tools"]:
            tools.append(
                dict(
                    server_id=server["id"],
                    name=tool["name"],
                    original=tool["original"],
                    original_sha256=digests[tool["original"]],
                    schema_hash=tool["schema_hash"],
                    product_schema_check=tool["product_schema_check"],
                    origin="genuine_mcp",
                )
            )
    key = lambda t: _sha(f"{t['server_id']}/{t['name']}".encode())  # noqa: E731
    ordered = sorted(tools, key=key)
    if len(ordered) < QUOTA or not all(t["product_schema_check"] for t in ordered[:QUOTA]):
        print("fewer than 100 admissible tools", file=sys.stderr)
        return 2
    selected = [f"{t['server_id']}/{t['name']}" for t in ordered[:QUOTA]]
    excluded = [
        dict(tool=f"{t['server_id']}/{t['name']}", reason="beyond_quota") for t in ordered[QUOTA:]
    ]
    document = dict(
        kind="relaymind-acceptance-mcp-capture",
        version=1,
        captured_at=captured_at,
        capture_machine_role="linux_host",
        capture_tool=dict(
            path="capture_mcp_tools.py",
            sha256=_sha((out / "capture_mcp_tools.py").read_bytes()),
        ),
        protocol_version_requested="2025-06-18",
        servers=servers,
        tools=sorted(tools, key=lambda t: (t["server_id"], t["name"])),
        selection=dict(
            quota=QUOTA,
            rule="sha256(utf8(server_id + '/' + tool_name)) ascending, first 100",
            selected=sorted(selected),
            excluded=sorted(excluded, key=lambda e: e["tool"]),
        ),
    )
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    (out / "capture.v1.json").write_text(text, encoding="utf-8")
    files = sorted(
        {o["path"] for s in servers for o in s["originals"]}
        | {s["license"]["path"] for s in servers}
        | {"capture_mcp_tools.py", "freeze_mcp_capture.py"}
    )
    lock = dict(
        kind="relaymind-acceptance-mcp-capture-lock",
        version=1,
        document=dict(path="capture.v1.json", digest=canonical_digest(document)),
        files=[
            dict(path=p, sha256=_sha((out / p).read_bytes()), bytes=(out / p).stat().st_size)
            for p in files
        ],
    )
    (out / "lock.v1.json").write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            dict(tools=len(tools), selected=len(selected), digest=lock["document"]["digest"])
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
