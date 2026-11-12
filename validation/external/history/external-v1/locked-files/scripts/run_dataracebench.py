#!/usr/bin/env python3
"""Evaluate tri-state native race analysis on the pinned DataRaceBench subset."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clang_pipeline.evaluation_protocol import (
    prediction_envelope,
    validate_prediction_document,
    write_json,
)
from clang_pipeline.race_detector import analyze_race_file


DATASET = ROOT / ".benchmark-cache/dataracebench"
CASES = ROOT / "validation/external/results/dataracebench/cases.json"
SELECTION = ROOT / "validation/external/selections/dataracebench-v1.json"
OUTPUT = ROOT / "validation/external/results/dataracebench"


def select_cases(cases: list[dict], limit: int | None) -> list[dict]:
    if limit is None or limit >= len(cases):
        return cases
    positives = [case for case in cases if case["expected_race"]]
    negatives = [case for case in cases if not case["expected_race"]]
    selected = []
    for index in range(max(len(positives), len(negatives))):
        if index < len(positives):
            selected.append(positives[index])
        if index < len(negatives):
            selected.append(negatives[index])
        if len(selected) >= limit:
            break
    return selected[:limit]


def score(rows: list[dict]) -> dict:
    answered = [row for row in rows if row["status"] != "unknown"]
    tp = sum(row["expected_race"] and row["status"] == "race" for row in answered)
    fp = sum(not row["expected_race"] and row["status"] == "race" for row in answered)
    fn = sum(row["expected_race"] and row["status"] == "no_race" for row in answered)
    tn = sum(not row["expected_race"] and row["status"] == "no_race" for row in answered)
    unknown = len(rows) - len(answered)
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "unknown": unknown,
        "race_precision": tp / precision_denominator if precision_denominator else None,
        "race_recall": tp / recall_denominator if recall_denominator else None,
        "false_race_rate": fp / (fp + tn) if fp + tn else None,
        "no_race_recall": tn / (tn + fp) if tn + fp else None,
        "unknown_rate": unknown / len(rows) if rows else None,
        "coverage": len(answered) / len(rows) if rows else None,
        "accuracy": (
            (tp + tn) / len(rows)
            if rows and unknown == 0
            else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--selection", type=Path, default=SELECTION)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if not CASES.is_file() or not DATASET.is_dir():
        raise SystemExit("prepare DataRaceBench cases first")
    source = json.loads(args.cases.read_text(encoding="utf-8"))
    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    selected_ids = {unit["case_id"] for unit in selection["units"]}
    cases = [
        dict(case, selected_id=case["id"])
        for case in source["cases"]
        if case["id"] in selected_ids
    ]
    cases = select_cases(cases, args.limit)
    predictions = []
    rows = []
    for case in cases:
        source_path = DATASET / case["source_file"]
        try:
            analysis = analyze_race_file(source_path)
            status = analysis["label"]
            evidence = [
                {
                    "id": item["id"],
                    "file": item["location"]["file"],
                    "line": item["location"]["line"],
                    "snippet": item["location"].get("snippet", ""),
                }
                for item in analysis.get("evidence", [])
            ][:50]
            reason = analysis.get("reason", "")
            confidence = float(analysis.get("confidence", 0.0))
        except Exception as exc:
            status = "unknown"
            evidence = []
            reason = str(exc)
            confidence = 0.0
        predictions.append(
            {
                "id": case["id"],
                "identity": {"case_id": case["id"]},
                "status": status,
                "confidence": confidence,
                "evidence": evidence,
                "details": {
                    "reason": reason,
                    "source_file": case["source_file"],
                },
            }
        )
        rows.append(
            {
                "id": case["id"],
                "expected_race": bool(case["expected_race"]),
                "status": status,
                "reason": reason,
            }
        )
    envelope = prediction_envelope(
        dataset="dataracebench",
        generator={
            "name": "race-detector-native",
            "version": "1.0.0",
            "mode": "deterministic",
            "parameters": {
                "strategy": "openmp-index-and-synchronization",
                "gold_exposed_to_analyzer": False,
            },
        },
        predictions=predictions,
    )
    validate_prediction_document(envelope, expected_dataset="dataracebench")
    metrics = score(rows)
    report = {
        "schema_version": "1.0",
        "dataset": "DataRaceBench",
        "selection_id": selection["selection_id"],
        "commit": selection["source"]["revision"],
        "metric": "tri-state data-race classification",
        **metrics,
        "cases": rows,
    }
    write_json(args.output / "predictions.json", envelope)
    write_json(args.output / "report.json", report)
    (args.output / "report.md").write_text(
        "\n".join(
            [
                "# DataRaceBench tri-state race baseline",
                "",
                "| TP | FP | FN | TN | Unknown | Race Precision | Race Recall | "
                "False Race Rate | No-race Recall | Coverage |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
                "| {tp} | {fp} | {fn} | {tn} | {unknown} | {p} | {r} | "
                "{frr} | {nrr} | {coverage} |".format(
                    tp=metrics["tp"],
                    fp=metrics["fp"],
                    fn=metrics["fn"],
                    tn=metrics["tn"],
                    unknown=metrics["unknown"],
                    p=format_rate(metrics["race_precision"]),
                    r=format_rate(metrics["race_recall"]),
                    frr=format_rate(metrics["false_race_rate"]),
                    nrr=format_rate(metrics["no_race_recall"]),
                    coverage=format_rate(metrics["coverage"]),
                ),
                "",
                "Unknown is excluded from the binary confusion matrix and is not",
                "converted to no-race. Accuracy is intentionally omitted unless",
                "the unknown count is zero.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


def format_rate(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


if __name__ == "__main__":
    raise SystemExit(main())
