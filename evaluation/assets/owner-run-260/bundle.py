"""Companion input bundles for grading the #260 reports (spec 0.60 section 12.6.5).

RM42, RM44; AC33, AC36. Writes the section 12.6.2 InputManifestV1 (with the S3
document kinds) next to a report, copies every redistributable document and
evidence file beside it, and writes a private OfflineResolverV1 for what may
not be published: the catalog, its originals and the runner/protocol
inventory preimages. The report's ``input_manifest`` reference is then set to
the written manifest. Nothing is fetched and no secret is read.
"""

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "src"))

from relaymind.canonical import canonical_digest, canonical_json  # noqa: E402


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def payload_of(value) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def inventory(kind: str) -> dict:
    if kind == "runner_inventory":
        paths = ["pyproject.toml", "scripts/asset_whole_task.py"]
        paths += [
            p.relative_to(REPO).as_posix()
            for p in (REPO / "src/relaymind").rglob("*.py")
            if "__pycache__" not in p.parts
        ]
    else:
        paths = ["docs/SPECIFICATION.md"]
        paths += [p.relative_to(REPO).as_posix() for p in (REPO / "src/relaymind").rglob("*.json")]
    files = []
    for path in sorted(set(paths)):
        data = (REPO / path).read_bytes()
        files.append(dict(path=path, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data)))
    return dict(schema_version=1, files=files)


def put(base: Path, relative: str, payload: bytes) -> None:
    (base / relative).parent.mkdir(parents=True, exist_ok=True)
    (base / relative).write_bytes(payload)


class Bundle:
    def __init__(self, out: Path, private: Path):
        self.out, self.private = out.absolute(), private.absolute()
        self.documents, self.evidence, self.resolver = [], {}, []

    def document(self, kind: str, value: dict, *, publish: bool = True, payload=None) -> None:
        payload = payload_of(value) if payload is None else payload
        digest = canonical_digest(value)
        name = (
            kind
            if kind in ("environment_lock", "runner_inventory", "protocol_inventory")
            else f"{kind}:{digest}"
        )
        if any(d["id"] == name for d in self.documents):
            return
        path = None
        relative = "docs/" + hashlib.sha256(name.encode()).hexdigest() + ".json"
        if publish:
            put(self.out, relative, payload)
            path = relative
        else:
            put(self.private, relative, payload)
            self.resolver.append(
                dict(id=name, hash_kind="canonical", hash=digest, bytes=len(payload), path=relative)
            )
        self.documents.append(
            dict(id=name, kind=kind, path=path, digest=digest, bytes=len(payload), format="json")
        )

    def file(self, evidence_id, kind, access, payload: bytes, *, publish: bool, relative=None):
        sha = hashlib.sha256(payload).hexdigest()
        if evidence_id in self.evidence:
            if self.evidence[evidence_id]["sha256"] != sha:
                raise SystemExit(f"conflicting evidence {evidence_id}")
            return
        relative = relative or ("evidence/" + hashlib.sha256(evidence_id.encode()).hexdigest())
        path = None
        if publish:
            put(self.out, relative, payload)
            path = relative
        else:
            put(self.private, relative, payload)
            self.resolver.append(
                dict(
                    id=evidence_id,
                    hash_kind="original",
                    hash=sha,
                    bytes=len(payload),
                    path=relative,
                )
            )
        self.evidence[evidence_id] = dict(
            id=evidence_id, kind=kind, access=access, path=path, sha256=sha, bytes=len(payload)
        )

    def corpus(self, release: Path) -> None:
        lock = read(release / "lock.json")
        self.document("lock", lock, payload=(release / "lock.json").read_bytes())
        for name in ("cases", "labels"):
            raw = (release / f"{name}.json").read_bytes()
            self.document(name, json.loads(raw), payload=raw)
        catalog_raw = (release / "catalog.json").read_bytes()
        catalog = json.loads(catalog_raw)
        self.document("catalog", catalog, publish=False, payload=catalog_raw)
        for item in catalog["evidence"]:
            data = (release / item["path"]).read_bytes()
            self.file(
                item["id"],
                item["kind"],
                item["access"],
                data,
                publish=item["access"] == "redistributable",
            )

    def inventories(self) -> None:
        for kind in ("runner_inventory", "protocol_inventory"):
            value = inventory(kind)
            self.document(kind, value)
            for item in value["files"]:
                relative = "originals/" + item["path"]
                target = self.private / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO / item["path"], target)
                self.resolver.append(
                    dict(
                        id="file:" + hashlib.sha256(item["path"].encode()).hexdigest(),
                        hash_kind="original",
                        hash=item["sha256"],
                        bytes=item["bytes"],
                        path=relative,
                    )
                )

    def write(self, report_path: Path, report: dict) -> None:
        documents = sorted(self.documents, key=lambda d: d["id"].encode())
        evidence = sorted(self.evidence.values(), key=lambda e: e["id"].encode())
        manifest = dict(
            schema_version=1, documents=documents, evidence=evidence, source_proof_evidence_ids=[]
        )
        data = payload_of(manifest)
        (self.out / "inputs.json").write_bytes(data)
        report["input_manifest"] = dict(
            path="inputs.json", digest=canonical_digest(manifest), bytes=len(data)
        )
        report_path.write_bytes(payload_of(report))
        entries = sorted(
            {(e["id"], e["hash_kind"], e["hash"]): e for e in self.resolver}.values(),
            key=lambda e: (e["id"], e["hash_kind"], e["hash"]),
        )
        (self.private / "resolver.json").write_bytes(
            payload_of(dict(schema_version=1, entries=entries))
        )


def add_extra_evidence(bundle: Bundle, extra: dict) -> None:
    """Further evidence files; one kept out of the published bundle is restricted."""
    for evidence_id, item in extra.items():
        publish = item.get("publish", True)
        bundle.file(
            evidence_id,
            item.get("kind", "original"),
            "redistributable" if publish else "restricted",
            Path(item["path"]).read_bytes(),
            publish=publish,
            relative=item.get("relative"),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("host", "adaptation"))
    parser.add_argument("--report", type=Path, required=True, help="the raw run report")
    parser.add_argument("--release", type=Path, required=True, help="the private release root")
    parser.add_argument("--root-input", type=Path, required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--configs", type=Path, nargs="+", required=True)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        required=True,
        help="directory the report's evidence paths are relative to",
    )
    parser.add_argument(
        "--extra-evidence",
        type=Path,
        help="JSON {evidence_id: {path, kind, publish, relative}} of further evidence files",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--private", type=Path, required=True)
    args = parser.parse_args()
    report = read(args.report)
    bundle = Bundle(args.out, args.private)
    args.out.mkdir(parents=True, exist_ok=True)
    root_kind, freeze_kind = (
        ("host_tasks", "host_freeze") if args.kind == "host" else ("episodes", "episode_freeze")
    )
    bundle.document(root_kind, read(args.root_input), payload=args.root_input.read_bytes())
    bundle.document(freeze_kind, read(args.freeze), payload=args.freeze.read_bytes())
    for path in args.configs:
        bundle.document("referenced_config", read(path), payload=path.read_bytes())
    bundle.document("environment_lock", report["environment"])
    bundle.corpus(args.release)
    bundle.inventories()
    for ref in report["evidence"]:
        data = (args.evidence_root / ref["path"]).read_bytes() if ref["path"] else None
        if data is None:
            raise SystemExit(f"unresolved evidence {ref['id']}")
        bundle.file(
            ref["id"], "original", "redistributable", data, publish=True, relative=ref["path"]
        )
    if args.extra_evidence:
        add_extra_evidence(bundle, read(args.extra_evidence))
    bundle.write(args.out / "report.json", report)
    print(json.dumps(dict(documents=len(bundle.documents), evidence=len(bundle.evidence))))


if __name__ == "__main__":
    main()
