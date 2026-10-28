#!/usr/bin/env python3
"""Evaluate per-function memory-leak predictions on NIST Juliet CWE-401."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clang_pipeline.clang_ast import read_json, write_json
from clang_pipeline.pipeline import run_pipeline
from clang_pipeline.repo_agent import _relativize_json_paths
from clang_pipeline.resource_flow import analyze_resource_flow


DATASET = ROOT / ".benchmark-cache/juliet-c-cpp-1.3"
CASES = ROOT / "validation/external/results/juliet/cases.json"
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


def analyze_case(case: dict, temp_root: Path, index: int) -> set[str]:
    source_path = DATASET / case["source_file"]
    repo = temp_root / f"{index:03d}_{source_path.stem}"
    support = repo / "testcasesupport"
    shutil.copytree(DATASET / case["language"].capitalize() / "testcasesupport", support)
    copied_source = repo / source_path.name
    shutil.copyfile(source_path, copied_source)
    workspace = repo / ".workspace"
    run_pipeline(
        source_root=repo,
        workspace=workspace,
        run_id=f"juliet_{case['id']}",
        build_profile="nist-juliet-1.3",
        compile_commands=compile_commands(repo, copied_source, case["language"]),
        repository="nist/juliet-c-cpp-1.3",
        commit="ada9d7e1c323d283446df3f55bdee0d00bda1fed786785fe98764d58688f38eb",
    )
    graph = _relativize_json_paths(
        read_json(workspace / "07-report" / "graph.json"),
        repo,
    )
    flow = analyze_resource_flow(graph, source_root=repo)
    return {
        str(operation.get("function") or "")
        for item in flow.get("items", [])
        for path in item.get("paths", [])
        if path.get("termination_reason") == "release_not_found"
        for operation in item.get("operations", [])
    }


def main() -> int:
    if not CASES.is_file() or not DATASET.is_dir():
        raise SystemExit("prepare Juliet cases first")
    source_index = json.loads(CASES.read_text(encoding="utf-8"))
    function_rows = []
    skipped = []
    tp = fp = tn = fn = 0
    with tempfile.TemporaryDirectory(prefix="juliet-resource-") as temp_dir:
        temp_root = Path(temp_dir)
        for index, case in enumerate(source_index["cases"]):
            try:
                incomplete_functions = analyze_case(case, temp_root, index)
            except Exception as exc:
                skipped.append(
                    {
                        "case": case["id"],
                        "reason": str(exc).splitlines()[-1][:300],
                    }
                )
                continue
            labels = case["labels"]
            for function in labels["defect_prefixed_functions"]:
                expected = True
                predicted = function in incomplete_functions
                if expected and predicted:
                    tp += 1
                elif expected:
                    fn += 1
                function_rows.append(
                    {
                        "case": case["id"],
                        "function": function,
                        "expected_leak": expected,
                        "predicted_leak": predicted,
                    }
                )
            for function in labels["clean_marked_functions"]:
                expected = False
                predicted = function in incomplete_functions
                if predicted:
                    fp += 1
                else:
                    tn += 1
                function_rows.append(
                    {
                        "case": case["id"],
                        "function": function,
                        "expected_leak": expected,
                        "predicted_leak": predicted,
                    }
                )
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    report = {
        "schema_version": "1.0",
        "dataset": "NIST Juliet C/C++ 1.3",
        "official_sha256": source_index["official_sha256"],
        "scope": "CWE-401 memory leak, per-function classification",
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": tp / precision_denominator if precision_denominator else None,
        "recall": tp / recall_denominator if recall_denominator else None,
        "accuracy": (tp + tn) / len(function_rows) if function_rows else None,
        "cases": function_rows,
        "skipped_cases": skipped,
    }
    write_json(OUTPUT / "report.json", report)
    (OUTPUT / "report.md").write_text(
        "\n".join(
            [
                "# NIST Juliet CWE-401 resource-flow baseline",
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
                "Only CWE-401 memory-leak cases are evaluated; platform-specific",
                "cases that cannot be parsed on the current host are listed as skipped.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
