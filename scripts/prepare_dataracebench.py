#!/usr/bin/env python3
"""Build a deterministic DataRaceBench case index from official filenames."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / ".benchmark-cache/dataracebench"
OUTPUT = ROOT / "validation/external/results/dataracebench"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--per-label", type=int, default=12)
    args = parser.parse_args()
    root = DATASET / "micro-benchmarks"
    if not root.is_dir():
        parser.error(
            "DataRaceBench is not fetched; run scripts/fetch_external_benchmarks.py "
            "--benchmark dataracebench"
        )
    rows = []
    for source in sorted(root.rglob("*")):
        if source.suffix not in {".c", ".cc", ".cpp", ".cxx"}:
            continue
        if re.search(r"-yes$", source.stem):
            expected_race = True
        elif re.search(r"-no$", source.stem):
            expected_race = False
        else:
            continue
        rows.append(
            {
                "id": source.stem,
                "source_file": source.relative_to(DATASET).as_posix(),
                "expected_race": expected_race,
            }
        )
    selected = (
        [row for row in rows if row["expected_race"]][: args.per_label]
        + [row for row in rows if not row["expected_race"]][: args.per_label]
    )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": "1.0",
        "dataset": "DataRaceBench",
        "commit": "7d07c5d637fa80f1fd7c239bdc34ae53094badc2",
        "license": "BSD (see LICENSE.txt and LICENSE.OSU.txt)",
        "selection": {"per_label": args.per_label},
        "available": {
            "race": sum(row["expected_race"] for row in rows),
            "no_race": sum(not row["expected_race"] for row in rows),
        },
        "cases": selected,
    }
    (OUTPUT / "cases.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"cases": len(selected), "path": str(OUTPUT / "cases.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
