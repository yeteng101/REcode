#!/usr/bin/env python3
"""Evaluate per-function CWE-401 memory-leak predictions on NIST Juliet."""
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


DATASET = ROOT / ".benchmark-cache/juliet-c-cpp-1.3"
CASES = ROOT / "validation/external/results/juliet/cases.json"
SELECTION = ROOT / "validation/external/selections/juliet-cwe401-v1.json"
OUTPUT = ROOT / "validation/external/results/juliet"


def compile_commands(repo: Path, source: Path, language: str) -> list[dict]:
    compiler = "clang++" if language == "cpp" else "clang"
    return [
        {
            "directory": str(repo),
            "file": str(source),
            "arguments": [
                compiler,
                "-x",
                "c++" if language == "cpp" else "c",
                "-std=c++17" if language == "cpp" else "-std=c11",
                "-I",
                str(repo / "testcasesupport"),
                str(source),
            ],
            "compiler": compiler,
            "defines": [],
        }
    ]


def analyze_case(case: dict, temp_root: Path, index: int) -> tuple[set[str], list[dict]]:
    source_path = DATASET / case["source_file"]
    repo = temp_root / f"{index:03d}_{source_path.stem}"
    support = repo / "testcasesupport"
    shutil.copytree(
        DATASET / case["language"].capitalize() / "testcasesupport",
        support,
    )
    copied_source = repo / source_path.name
    shutil.copyfile(source_path, copied_source)
    workspace = repo / ".workspace"
    run_pipeline(
        source_root=repo,
        workspace=workspace,
        run_id=f"juliet_{case['id']}",
        build_profile="nist-juliet-cwe401",
        compile_commands=compile_commands(repo, copied_source, case["language"]),
        repository="nist/juliet-c-cpp-1.3",
        commit="ada9d7e1c323d283446df3f55bdee0d00bda1fed786785fe98764d58688f38eb",
    )
    graph = _relativize_json_paths(
        read_json(workspace / "07-report" / "graph.json"),
        repo,
    )
    flow = analyze_resource_flow(graph, source_root=repo)
    incomplete = {
        str(operation.get("function") or "")
        for item in flow.get("items", [])
        for path in item.get("paths", [])
        if path.get("termination_reason") == "release_not_found"
        for operation in item.get("operations", [])
    }
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
    return incomplete, evidence


def select_cases(cases: list[dict], limit: int | None) -> list[dict]:
    if limit is None or limit >= len(cases):
        return cases
    positives = [
        case
        for case in cases
        if case["labels"]["defect_prefixed_functions"]
    ]
    negatives = [
        case
        for case in cases
        if case["labels"]["clean_marked_functions"]
    ]
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
        raise SystemExit("prepare Juliet cases first")
    source = json.loads(args.cases.read_text(encoding="utf-8"))
    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    selected_ids = {unit["case_id"] for unit in selection["units"]}
    cases = [
        case
        for case in source["cases"]
        if case["id"] in selected_ids and case["cwe"] == "CWE401"
    ]
    cases = select_cases(cases, args.limit)
    predictions = []
    rows = []
    skipped = []
    with tempfile.TemporaryDirectory(prefix="juliet-resource-") as temp_dir:
        temp_root = Path(temp_dir)
        for index, case in enumerate(cases):
            try:
                incomplete_functions, evidence = analyze_case(case, temp_root, index)
                analysis_error = ""
            except Exception as exc:
                incomplete_functions = set()
                evidence = []
                analysis_error = str(exc)
                skipped.append(
                    {
                        "case": case["id"],
                        "reason": analysis_error.splitlines()[-1][:300],
                    }
                )
            labels = case["labels"]
            for function, expected_leak in [
                *(
                    (function, True)
                    for function in labels["defect_prefixed_functions"]
                ),
                *(
                    (function, False)
                    for function in labels["clean_marked_functions"]
                ),
            ]:
                if analysis_error:
                    status = "unknown"
                elif function in incomplete_functions:
                    status = "leak"
                else:
                    status = "no_leak"
                identity = {
                    "case_id": case["id"],
                    "source_file": case["source_file"],
                    "function": function,
                }
                predictions.append(
                    {
                        "id": f"{case['id']}::{function}",
                        "identity": identity,
                        "status": status,
                        "confidence": 0.55 if status == "leak" else 0.45 if status == "no_leak" else 0.0,
                        "evidence": evidence if status == "leak" else [],
                        "details": {
                            "analysis_error": analysis_error,
                        },
                    }
                )
                rows.append(
                    {
                        "case": case["id"],
                        "source_file": case["source_file"],
                        "function": function,
                        "expected_leak": expected_leak,
                        "status": status,
                    }
                )
    envelope = prediction_envelope(
        dataset="juliet-cwe401",
        generator={
            "name": "resource-flow-native",
            "version": "1.0.0",
            "mode": "deterministic",
            "parameters": {
                "scope": "CWE-401 function classification",
                "gold_exposed_to_analyzer": False,
            },
        },
        predictions=predictions,
    )
    validate_prediction_document(envelope, expected_dataset="juliet-cwe401")
    metrics = score(rows)
    report = {
        "schema_version": "1.0",
        "dataset": "NIST Juliet C/C++ 1.3",
        "selection_id": selection["selection_id"],
        "official_sha256": selection["source"]["revision"],
        "scope": "CWE-401 memory leak, function-level classification",
        "metric": "tri-state function-level leak classification",
        **metrics,
        "cases": rows,
        "skipped_cases": skipped,
    }
    write_json(args.output / "predictions.json", envelope)
    write_json(args.output / "report.json", report)
    (args.output / "report.md").write_text(
        "\n".join(
            [
                "# NIST Juliet CWE-401 leak baseline",
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
                "Only CWE401_Memory_Leak is included. Unknown results remain in",
                "the prediction file and are excluded from binary metrics.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
