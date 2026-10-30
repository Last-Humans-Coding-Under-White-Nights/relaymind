#!/usr/bin/env python3
"""RM40/AC31: reproduce an explicitly supplied same-source local release."""

import argparse
import json
import sys
from pathlib import Path

from relaymind.evaluation.asset_corpus_io import AssetCorpusError
from relaymind.evaluation.asset_derivation import derive_matrix_export


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = derive_matrix_export(args.input, args.lock, args.output)
    except AssetCorpusError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "reproduced",
                "qualification": result.assessment.status,
                "qualification_reasons": list(result.assessment.reasons),
                "lock_digest": result.lock_digest,
                "output_paths": [str(p) for p in result.output_paths],
                "excluded_original_ids": [e["original_id"] for e in result.exclusions],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
