#!/usr/bin/env python3
"""Run a source-synchronization baseline on the pinned DataRaceBench subset."""
from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / ".benchmark-cache/dataracebench"
CASES = ROOT / "validation/external/results/dataracebench/cases.json"
OUTPUT = ROOT / "validation/external/results/dataracebench"
SYNC_PATTERNS = (
    r"#\s*pragma\s+omp\s+critical\b",
    r"#\s*pragma\s+omp\s+atomic\b",
    r"#\s*pragma\s+omp\s+barrier\b",
    r"#\s*pragma\s+omp\s+ordered\b",
    r"#\s*pragma\s+omp\s+taskwait\b",
    r"#\s*pragma\s+omp\s+taskgroup\b",
    r"#\s*pragma\s+omp\s+single\b",
    r"#\s*pragma\s+omp\s+master\b",
    r"\bomp_(?:set|unset|init|destroy)_(?:nested_)?lock\s*\(",
)


def main() -> int:
    if not CASES.is_file():
        raise SystemExit("prepare DataRaceBench cases first")
    data = json.loads(CASES.read_text(encoding="utf-8"))
    rows = []
    tp = fp = tn = fn = 0
    for case in data["cases"]:
        source = (DATASET / case["source_file"]).read_text(
            encoding="utf-8",
            errors="replace",
        )
        synchronized = any(re.search(pattern, source) for pattern in SYNC_PATTERNS)
        predicted_race = not synchronized
        expected_race = bool(case["expected_race"])
        if expected_race and predicted_race:
            tp += 1
        elif expected_race:
            fn += 1
        elif predicted_race:
            fp += 1
        else:
            tn += 1
        rows.append(
            {
                "id": case["id"],
                "expected_race": expected_race,
                "predicted_race": predicted_race,
                "synchronization_marker": synchronized,
            }
        )
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    report = {
        "schema_version": "1.0",
        "dataset": "DataRaceBench",
        "commit": "7d07c5d637fa80f1fd7c239bdc34ae53094badc2",
        "metric": "source synchronization-presence baseline",
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": tp / precision_denominator if precision_denominator else None,
        "recall": tp / recall_denominator if recall_denominator else None,
        "accuracy": (tp + tn) / len(rows) if rows else None,
        "cases": rows,
    }
    (OUTPUT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (OUTPUT / "report.md").write_text(
        "\n".join(
            [
                "# DataRaceBench synchronization baseline",
                "",
                "| TP | FP | FN | TN | Precision | Recall | Accuracy |",
                "|---:|---:|---:|---:|---:|---:|---:|",
                "| {tp} | {fp} | {fn} | {tn} | {p} | {r} | {a} |".format(
                    tp=tp,
                    fp=fp,
                    fn=fn,
                    tn=tn,
                    p="N/A" if report["precision"] is None else f"{report['precision']:.2%}",
                    r="N/A" if report["recall"] is None else f"{report['recall']:.2%}",
                    a="N/A" if report["accuracy"] is None else f"{report['accuracy']:.2%}",
                ),
                "",
                "This source-only baseline predicts `race` when no explicit OpenMP",
                "synchronization marker is present. It is not a full race detector.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
