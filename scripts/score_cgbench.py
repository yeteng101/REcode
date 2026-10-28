#!/usr/bin/env python3
"""Score external function-pointer target predictions against CG-Bench."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "validation/external/results/cgbench/cases.json"
DEFAULT_REPORT = ROOT / "validation/external/results/cgbench/report.json"


def normalize(predictions: dict) -> dict[str, set[str]]:
    rows = predictions.get("predictions", predictions)
    if isinstance(rows, list):
        return {
            str(item["callsite"]): set(map(str, item.get("targets", [])))
            for item in rows
        }
    if isinstance(rows, dict):
        return {
            str(callsite): set(map(str, targets))
            for callsite, targets in rows.items()
        }
    raise ValueError("predictions must be a list or object")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    if not CASES.is_file():
        parser.error("run scripts/extract_cgbench.py first")
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    predicted = normalize(
        json.loads(args.predictions.read_text(encoding="utf-8"))
    )
    tp = fp = fn = 0
    per_case = []
    for case in cases:
        expected = set(case["targets"])
        actual = predicted.get(case["callsite"], set())
        case_tp = len(expected & actual)
        case_fp = len(actual - expected)
        case_fn = len(expected - actual)
        tp += case_tp
        fp += case_fp
        fn += case_fn
        per_case.append(
            {
                "id": case["id"],
                "callsite": case["callsite"],
                "tp": case_tp,
                "fp": case_fp,
                "fn": case_fn,
            }
        )
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    f1_denominator = 2 * tp + fp + fn
    report = {
        "schema_version": "1.0",
        "dataset": "CG-Bench",
        "commit": "b277b7329f62fdddc6d4a341057d045bdf69def2",
        "totals": {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": (
                tp / precision_denominator if precision_denominator else None
            ),
            "recall": tp / recall_denominator if recall_denominator else None,
            "f1": 2 * tp / f1_denominator if f1_denominator else None,
        },
        "cases": per_case,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["totals"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
