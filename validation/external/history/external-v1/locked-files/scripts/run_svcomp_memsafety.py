#!/usr/bin/env python3
"""Evaluate tri-state resource-leak analysis on SV-COMP MemSafety tasks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clang_pipeline.clang_ast import read_json, write_json
from clang_pipeline.evaluation_protocol import (
    prediction_envelope,
    validate_prediction_document,
)
from clang_pipeline.pipeline import run_pipeline
from clang_pipeline.repo_agent import _relativize_json_paths
from clang_pipeline.resource_flow import analyze_resource_flow


DATASET = ROOT / ".benchmark-cache/svcomp26"
CASES = ROOT / "validation/external/results/svcomp/cases.json"
SELECTION = ROOT / "validation/external/selections/svcomp-memsafety-v1.json"
OUTPUT = ROOT / "validation/external/results/svcomp/memsafety"


def compile_commands(repo: Path) -> list[dict]:
    commands = []
    for source in sorted(repo.iterdir()):
        if source.suffix not in {".c", ".i", ".cpp"}:
            continue
        cpp = source.suffix == ".cpp"
        compiler = "clang++" if cpp else "clang"
        commands.append(
            {
                "directory": str(repo),
                "file": str(source),
                "arguments": [
                    compiler,
                    "-x",
                    "c++" if cpp else "c",
                    "-std=c++17" if cpp else "-std=c11",
                    "-D__regparm__(x)=",
                    "-I",
                    str(repo),
                    str(source),
                ],
                "compiler": compiler,
                "defines": [],
            }
        )
    return commands


def select_cases(cases: list[dict], limit: int | None) -> list[dict]:
    if limit is None or limit >= len(cases):
        return cases
    positives = [case for case in cases if not case["expected_verdict"]]
    negatives = [case for case in cases if case["expected_verdict"]]
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
    tp = sum(row["expected_leak"] and row["status"] == "leak" for row in answered)
    fp = sum(not row["expected_leak"] and row["status"] == "leak" for row in answered)
    fn = sum(row["expected_leak"] and row["status"] == "no_leak" for row in answered)
    tn = sum(not row["expected_leak"] and row["status"] == "no_leak" for row in answered)
    unknown = len(rows) - len(answered)
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "unknown": unknown,
        "precision": tp / precision_denominator if precision_denominator else None,
        "recall": tp / recall_denominator if recall_denominator else None,
        "unknown_rate": unknown / len(rows) if rows else None,
        "coverage": len(answered) / len(rows) if rows else None,
        "accuracy": (tp + tn) / len(rows) if rows and unknown == 0 else None,
    }


def format_rate(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2%}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--selection", type=Path, default=SELECTION)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if not CASES.is_file() or not DATASET.is_dir():
        raise SystemExit("prepare SV-COMP cases first")
    source = json.loads(args.cases.read_text(encoding="utf-8"))
    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    selected_ids = {unit["case_id"] for unit in selection["units"]}
    cases = [
        case
        for case in source["groups"]["memsafety"]
        if case["id"] in selected_ids
    ]
    cases = select_cases(cases, args.limit)
    predictions = []
    rows = []
    skipped = []
    with tempfile.TemporaryDirectory(prefix="svcomp-memsafety-") as temp_dir:
        temp_root = Path(temp_dir)
        for index, case in enumerate(cases):
            task_dir = (DATASET / case["task_file"]).parent
            repo = temp_root / f"{index:03d}_{case['id']}"
            repo.mkdir(parents=True)
            for value in case["input_files"]:
                source_path = task_dir / value
                shutil.copyfile(source_path, repo / source_path.name)
            workspace = repo / ".workspace"
            try:
                run_pipeline(
                    source_root=repo,
                    workspace=workspace,
                    run_id=f"svcomp_{case['id']}",
                    build_profile="svcomp26-memsafety",
                    compile_commands=compile_commands(repo),
                    repository="sv-comp/2026/memsafety",
                    commit="7efe28dd29576b46927b7a34e8f742bd90966a75",
                )
                graph = _relativize_json_paths(
                    read_json(workspace / "07-report" / "graph.json"),
                    repo,
                )
                flow = analyze_resource_flow(graph, source_root=repo)
                predicted_violation = any(
                    path.get("termination_reason") == "release_not_found"
                    for item in flow.get("items", [])
                    for path in item.get("paths", [])
                )
                status = "leak" if predicted_violation else "no_leak"
                evidence = [
                    {
                        "id": entry["id"],
                        "file": entry["location"]["file"],
                        "line": entry["location"]["line"],
                        "snippet": entry["location"].get("snippet", ""),
                    }
                    for entry in flow.get("evidence", [])
                    if entry.get("location")
                ][:50]
                reason = (
                    "resource path ends without release"
                    if predicted_violation
                    else "no analyzed resource path ends without release"
                )
                confidence = 0.55 if predicted_violation else 0.4
            except Exception as exc:
                status = "unknown"
                evidence = []
                reason = str(exc).splitlines()[-1][:300]
                confidence = 0.0
                skipped.append({"id": case["id"], "reason": reason})
            identity = {
                "task_file": case["task_file"],
                "property_file": case["property"],
                "subproperty": case["subproperty"],
            }
            predictions.append(
                {
                    "id": case["id"],
                    "identity": identity,
                    "status": status,
                    "confidence": confidence,
                    "evidence": evidence,
                    "details": {
                        "reason": reason,
                        "input_files": case["input_files"],
                    },
                }
            )
            rows.append(
                {
                    "id": case["id"],
                    "expected_leak": not bool(case["expected_verdict"]),
                    "status": status,
                    "reason": reason,
                }
            )
    envelope = prediction_envelope(
        dataset="svcomp-memsafety",
        generator={
            "name": "resource-flow-native",
            "version": "1.0.0",
            "mode": "deterministic",
            "parameters": {
                "strategy": "resource-path-termination",
                "gold_exposed_to_analyzer": False,
            },
        },
        predictions=predictions,
    )
    validate_prediction_document(envelope, expected_dataset="svcomp-memsafety")
    metrics = score(rows)
    report = {
        "schema_version": "1.0",
        "dataset": "SV-COMP 2026 C.MemSafety",
        "selection_id": selection["selection_id"],
        "commit": selection["source"]["revision"],
        "property": "valid-memtrack",
        "metric": "tri-state memory-leak violation classification",
        **metrics,
        "cases": rows,
        "skipped_cases": skipped,
    }
    write_json(args.output / "predictions.json", envelope)
    write_json(args.output / "report.json", report)
    (args.output / "report.md").write_text(
        "\n".join(
            [
                "# SV-COMP MemSafety resource-flow baseline",
                "",
                "| TP | FP | FN | TN | Unknown | Precision | Recall | Coverage |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|",
                "| {tp} | {fp} | {fn} | {tn} | {unknown} | {p} | {r} | "
                "{coverage} |".format(
                    tp=metrics["tp"],
                    fp=metrics["fp"],
                    fn=metrics["fn"],
                    tn=metrics["tn"],
                    unknown=metrics["unknown"],
                    p=format_rate(metrics["precision"]),
                    r=format_rate(metrics["recall"]),
                    coverage=format_rate(metrics["coverage"]),
                ),
                "",
                "Only valid-memtrack tasks are evaluated. Unknown results remain",
                "in the prediction JSON and are excluded from binary metrics.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
