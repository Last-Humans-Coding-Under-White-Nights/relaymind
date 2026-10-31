"""RM39/AC35 bounded disposable MCP capability tier, never production activation."""

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
FIXTURE = Path(__file__).resolve().parent


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GRADE = load("mcp_use_grade", FIXTURE / "grade.py")


def blocked_capture(reason: str) -> dict:
    """Fixed reasons separate unsupported composition from a failed host launch."""
    if reason not in {"preflight_only", "exact_route_unqualified", "person_setup_unavailable"}:
        raise ValueError("unknown capture reason")
    return dict(
        kind="openclaw-mcp-use-qualification",
        requirements=["RM39", "AC35"],
        status="blocked",
        reason=reason,
        mcp_use=dict(
            exact_route=dict(
                status="unsupported", reason="no_qualified_exact_use_receipt_resolver"
            ),
            approval_driver="automated_ui",
            human_approval="not_run",
            approvals=[
                dict(name=name, status="not_run", reason="exact_route_unqualified")
                for name in ("allow-once", "deny")
            ],
            negative_controls=[
                dict(name=name, status="not_run", reason="exact_route_unqualified")
                for name in GRADE.NEGATIVE_CASES
            ],
            provider_baseline=dict(status="not_run", reason="exact_route_unqualified"),
            inventories=[],
            server_events=[],
            sandbox_files={},
        ),
    )


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    if path.stat().st_size > 1048576:
        raise ValueError("capture_bound")
    return [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def composition_type(ordinary):
    """Reuse the installed tarball, default registration and real browser login."""

    class McpUseComposition(ordinary.OrdinaryComposition):
        def configure(self, config, work, origin):
            super().configure(config, work, origin)
            self.mcp_work = work
            self.inventory_file = work / "mcp-inventory.jsonl"
            self.inventory_file.write_bytes(b"")
            self.inventory_file.chmod(0o600)
            original = ROOT / "integrations/openclaw/test/host-person-qualification"
            paths = config["plugins"]["load"]["paths"]
            paths[paths.index(str(original))] = str(FIXTURE)
            config["plugins"]["entries"]["relaymind-person-qualification"]["config"][
                "inventoryFile"
            ] = str(self.inventory_file)
            servers = {}
            for index, name in enumerate(("alpha.one", "alpha?one")):
                control = work / f"mcp-control-{index}.json"
                ordinary.private_json(control, dict(server=name, revision=1))
                servers[name] = dict(
                    command="node",
                    args=[
                        str(FIXTURE / "server.mjs"),
                        str(control),
                        str(work / f"mcp-events-{index}.jsonl"),
                        str(work / f"mcp-sandbox-{index}.jsonl"),
                    ],
                    connectionTimeoutMs=5000,
                    requestTimeoutMs=5000,
                )
            config["mcp"] = dict(servers=servers)

        def prepare_host(self, host, environment, work, config_path):
            super().prepare_host(host, environment, work, config_path)
            # Host CLI owns both real stdio processes. No harness MCP client.
            with (work / "private-mcp-probe.log").open("wb") as log:
                result = subprocess.run(
                    host + ["mcp", "probe", "--json"],
                    env=environment,
                    cwd=work,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=30,
                )
            self.probe_exit = result.returncode

        def source_paths(self):
            return super().source_paths() + [
                FIXTURE / name
                for name in (
                    "qualify.py",
                    "grade.py",
                    "approval_ui.py",
                    "probe.mjs",
                    "server.mjs",
                    "package.json",
                    "openclaw.plugin.json",
                )
            ]

        def persons_ready(self, rpc, pages, sessions, capture):
            # Inventory dispatch is observed inside the inherited authenticated person RPC.
            self.collect(capture)
            super().persons_ready(rpc, pages, sessions, capture)
            self.collect(capture)

        def collect(self, capture):
            value = capture.setdefault(
                "mcp_use", blocked_capture("exact_route_unqualified")["mcp_use"]
            )
            value["inventories"] = read_jsonl(self.inventory_file)
            value["probe_exit_code"] = self.probe_exit
            value["server_events"] = [
                event
                for index in range(2)
                for event in read_jsonl(self.mcp_work / f"mcp-events-{index}.jsonl")
            ]
            value["sandbox_files"] = {
                name: read_jsonl(self.mcp_work / f"mcp-sandbox-{index}.jsonl")
                for index, name in enumerate(("alpha.one", "alpha?one"))
            }

        def finalize(self, capture):
            super().finalize(capture)
            self.collect(capture)

    return McpUseComposition


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tarball", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args(argv)
    capture = blocked_capture("preflight_only")
    # Pin before launching anything. Regrading after the trial reopens these
    # originals, so a source change during the run cannot be silently renewed.
    inventory = {}
    for name in sorted(GRADE.required_sources()):
        path = ROOT / name
        if path.is_file():
            inventory[name] = dict(path=name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    if not args.preflight_only:
        if args.tarball is None or args.work_dir is None:
            parser.error("--tarball and --work-dir are required for a host trial")
        ordinary = load(
            "mcp_use_ordinary",
            ROOT / "integrations/openclaw/test/host-ordinary-qualification/qualify.py",
        )
        ordinary.OrdinaryComposition = composition_type(ordinary)
        code = ordinary.main(
            [
                "--tarball",
                str(args.tarball),
                "--work-dir",
                str(args.work_dir),
                "--out",
                str(args.out),
            ]
        )
        original = json.loads(args.out.read_bytes())
        capture = blocked_capture(
            "exact_route_unqualified" if "mcp_use" in original else "person_setup_unavailable"
        )
        capture["ordinary_capture"] = original
        if "mcp_use" in original:
            capture["mcp_use"] = original.pop("mcp_use")
        else:
            capture["person_setup_reason"] = original.get("reason", "qualification_failed")
        capture["ordinary_exit_code"] = code
    for row in (
        capture.get("ordinary_capture", {}).get("environment", {}).get("fixture_plugin_sources", [])
    ):
        inventory.setdefault(row["path"], row)
    capture["source_inventory"] = list(inventory.values())
    capture["checks"] = GRADE.grade_capture(capture)
    capture["functional_exact_use"] = GRADE.mcp_use_pass(capture)
    capture["status"] = "passed" if capture["functional_exact_use"] else "blocked"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(capture, indent=2) + "\n", encoding="utf-8")
    args.out.chmod(0o600)
    # Capability refusal is data; setup/programming errors still raise.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
