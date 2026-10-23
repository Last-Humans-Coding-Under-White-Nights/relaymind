"""Fetch and derive the pinned ToolRet sample (RM08, RM09, RM25, RM28, section 12.4).

Only row identities and hashes are checked in. PyArrow is imported when parquet
is read, so installing RelayMind without the bench extra remains supported.
"""

import argparse
import hashlib
import json
import sys
import urllib.request
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from relaymind.evaluation.selection_benchmark import (  # noqa: E402
    TOOLRET_DATASET,
    TOOLRET_REVISION,
    derive_toolret,
    normalize_toolret,
    verify_toolret,
)

SHARDS = tuple(f"train-{index:05d}-of-00006.parquet" for index in range(6))
DEFAULT_SEED = 20261023


def iter_rows(paths: Sequence[Path]) -> Iterator[dict[str, Any]]:
    """Read source shards in their declared order without loading the whole dataset."""
    try:
        import pyarrow.parquet as parquet
    except ImportError as exc:
        raise RuntimeError(
            "ToolRet derivation requires the optional bench extra (pyarrow)"
        ) from exc
    for path in paths:
        for batch in parquet.ParquetFile(path).iter_batches(batch_size=256):
            yield from batch.to_pylist()


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch_shards(cache_dir: Path, *, revision: str = TOOLRET_REVISION) -> list[Path]:
    """Fetch the exact revision into an operator cache; interrupted files are not reused."""
    if revision != TOOLRET_REVISION:
        raise ValueError("ToolRet revision differs from the pinned dataset revision")
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in SHARDS:
        path = cache_dir / name
        if not path.exists():
            url = f"https://huggingface.co/datasets/{TOOLRET_DATASET}/resolve/{revision}/ToolRet-Training-20w/{name}"
            temporary = path.with_suffix(".parquet.part")
            urllib.request.urlretrieve(url, temporary)
            temporary.replace(path)
        paths.append(path)
    return paths


def load_cases(lock_path: Path, cache_dir: Path, *, revision: str = TOOLRET_REVISION) -> list[dict]:
    """Read only the frozen sample, refusing changed revisions, shards, rows or normalization."""
    lock = json.loads(Path(lock_path).read_text())
    if lock["dataset"] != TOOLRET_DATASET or lock["revision"] != revision:
        raise ValueError("ToolRet dataset or revision differs from lock")
    paths = fetch_shards(Path(cache_dir), revision=revision)
    if "shards" in lock:
        actual = [{"name": path.name, "sha256": _digest(path)} for path in paths]
        if actual != lock["shards"]:
            raise ValueError("ToolRet source shard digest differs from lock")
    wanted = {case["row_index"] for case in lock["cases"]}
    cases = [
        normalize_toolret(row, index)
        for index, row in enumerate(iter_rows(paths))
        if index in wanted
    ]
    return verify_toolret(cases, lock, revision=revision)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cache-dir", type=Path, default=Path("/tmp/relaymind-toolret"))
    parser.add_argument(
        "--parquet",
        type=Path,
        nargs=6,
        help="the six already-downloaded pinned shards, in source order",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--lock", type=Path, default=Path("evaluation/tools.v1.lock.json"))
    parser.add_argument(
        "--verify", action="store_true", help="verify an existing lock; never replace it"
    )
    args = parser.parse_args()
    paths = args.parquet or fetch_shards(args.cache_dir)
    if args.verify:
        lock = json.loads(args.lock.read_text())
        if lock["revision"] != TOOLRET_REVISION or lock["dataset"] != TOOLRET_DATASET:
            raise ValueError("ToolRet dataset or revision differs from lock")
        wanted = {case["row_index"] for case in lock["cases"]}
        cases = [normalize_toolret(row, i) for i, row in enumerate(iter_rows(paths)) if i in wanted]
        verify_toolret(cases, lock, revision=TOOLRET_REVISION)
    else:
        cases, lock = derive_toolret(
            iter_rows(paths), revision=TOOLRET_REVISION, seed=args.seed, size=500
        )
        lock["shards"] = [
            {"name": name, "sha256": _digest(path)}
            for name, path in zip(SHARDS, paths, strict=True)
        ]
        args.lock.parent.mkdir(parents=True, exist_ok=True)
        args.lock.write_text(json.dumps(lock, indent=2) + "\n")
    print(f"Verified {len(cases)} ToolRet cases; source rows are not written")


if __name__ == "__main__":
    main()
