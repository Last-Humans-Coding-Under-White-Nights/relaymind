#!/usr/bin/env python3
"""RM40/AC31: reproduce a pinned local MCP export; never acquire or execute tools."""

import argparse
import json
import sys
from pathlib import Path

from relaymind.assets.catalog import AssetValidationError
from relaymind.evaluation.asset_corpus_io import AssetCorpusError
from relaymind.evaluation.asset_derivation import derive_mcp_export


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = derive_mcp_export(args.input, args.lock, args.output)
    except (AssetCorpusError, AssetValidationError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "reproduced",
                "qualification": result.assessment.status,
                "qualification_reasons": list(result.assessment.reasons),
                "lock_digest": result.lock_digest,
                "mcp_tools": result.assessment.counts["mcp_tools"],
                "output_paths": [str(path) for path in result.output_paths],
                "excluded_original_ids": [item["original_id"] for item in result.exclusions],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
