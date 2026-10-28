#!/usr/bin/env python3
"""Evaluate resource-flow leak detection on SV-COMP MemSafety tasks."""
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


DATASET = ROOT / ".benchmark-cache/svcomp26"
CASES = ROOT / "validation/external/results/svcomp/cases.json"
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


def main() -> int:
    if not CASES.is_file() or not DATASET.is_dir():
        raise SystemExit("prepare SV-COMP cases first")
    source = json.loads(CASES.read_text(encoding="utf-8"))
    cases = source["groups"]["memsafety"]
    rows = []
    tp = fp = tn = fn = 0
    with tempfile.TemporaryDirectory(prefix="svcomp-memsafety-") as temp_dir:
        temp_root = Path(temp_dir)
        for index, case in enumerate(cases):
            task_dir = (DATASET / case["task_file"]).parent
            repo = temp_root / f"{index:03d}_{case['id']}"
            repo.mkdir(parents=True)
            for value in case["input_files"]:
                source_path = task_dir / value
                destination = repo / source_path.name
                shutil.copyfile(source_path, destination)
            workspace = repo / ".workspace"
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
            expected_violation = not case["expected_verdict"]
            if expected_violation and predicted_violation:
                tp += 1
            elif expected_violation:
                fn += 1
            elif predicted_violation:
                fp += 1
            else:
                tn += 1
            rows.append(
                {
                    "id": case["id"],
                    "expected_violation": expected_violation,
                    "predicted_violation": predicted_violation,
                    "resource_items": len(flow.get("items", [])),
                }
            )
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    report = {
        "schema_version": "1.0",
        "dataset": "SV-COMP 2026 C.MemSafety",
        "commit": "7efe28dd29576b46927b7a34e8f742bd90966a75",
        "property": "valid-memtrack",
        "metric": "binary memory-leak violation classification",
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": tp / precision_denominator if precision_denominator else None,
        "recall": tp / recall_denominator if recall_denominator else None,
        "accuracy": (tp + tn) / len(rows) if rows else None,
        "cases": rows,
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    write_json(OUTPUT / "report.json", report)
    lines = [
        "# SV-COMP MemSafety resource-flow baseline",
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
        "Only the official `valid-memtrack` subset is evaluated.",
    ]
    (OUTPUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
