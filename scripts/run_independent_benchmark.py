#!/usr/bin/env python3
"""Run the frozen independent microbenchmarks and score them after prediction."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clang_pipeline.clang_ast import read_json, write_json
from clang_pipeline.pipeline import run_pipeline
from clang_pipeline.repo_agent import _relativize_json_paths, generic_compile_commands
from scripts.export_validation import export_graph, save
from validation.benchmark import evaluate, markdown


BASE = ROOT / "validation/independent/fixtures"
RESULTS = ROOT / "validation/independent/results"
PROJECTS = ("callgraph", "resources", "sync")
THRESHOLDS = {
    "callgraph": {"precision": 0.90, "recall": 0.90},
    "resources": {"precision": 0.90, "recall": 0.80},
    "sync": {"precision": 0.70, "recall": 0.70},
    "micro_total": {"precision": 0.85, "recall": 0.80},
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_frozen(project: str) -> dict:
    project_dir = BASE / project
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"{project}: manifest.json is missing; freeze the benchmark first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "frozen":
        raise ValueError(f"{project}: benchmark is not frozen")
    if sha256(project_dir / "ground-truth.json") != manifest["gold_sha256"]:
        raise ValueError(f"{project}: Gold changed after freeze")
    if sha256(project_dir / "SPEC.md") != manifest["spec_sha256"]:
        raise ValueError(f"{project}: specification changed after freeze")
    source_root = project_dir / "source"
    actual_sources = {
        path.relative_to(project_dir).as_posix(): sha256(path)
        for path in sorted(source_root.rglob("*"))
        if path.is_file()
    }
    if actual_sources != manifest["source_sha256"]:
        raise ValueError(f"{project}: source files changed after freeze")
    return manifest


def reset_output(path: Path) -> None:
    resolved = path.resolve()
    if RESULTS.resolve() not in resolved.parents:
        raise ValueError(f"refusing to remove output outside {RESULTS}: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def run_project(project: str) -> dict:
    manifest = verify_frozen(project)
    project_dir = BASE / project
    source_root = project_dir / "source"
    output = RESULTS / project
    reset_output(output)
    predictions = output / "predictions"
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    with tempfile.TemporaryDirectory(prefix=f"independent-{project}-") as temp_dir:
        workspace = Path(temp_dir) / "workspace"
        run_pipeline(
            source_root=source_root,
            workspace=workspace,
            run_id=f"independent_{project}_v1",
            build_profile="independent-micro-v1",
            compile_commands=generic_compile_commands(source_root),
            repository=manifest["repository"],
            commit=manifest["commit"],
        )
        graph = _relativize_json_paths(
            read_json(workspace / "07-report" / "graph.json"),
            source_root,
        )
        docs = export_graph(
            graph,
            native=True,
            source_root=source_root,
        )
        save(docs, predictions)
        write_json(output / "graph.json", graph)

    # Gold is deliberately read only after analysis and prediction export finish.
    if sha256(project_dir / "ground-truth.json") != manifest["gold_sha256"]:
        raise ValueError(f"{project}: Gold changed during analysis")
    truth = json.loads((project_dir / "ground-truth.json").read_text(encoding="utf-8"))
    report = evaluate(truth, docs)
    category = truth["annotation_scope"]["categories"][0]
    metrics = report["categories"][category]
    denominator = metrics["tp"] + metrics["tn"] + metrics["fp"] + metrics["fn"]
    labelled_accuracy = (
        (metrics["tp"] + metrics["tn"]) / denominator if denominator else None
    )
    run_record = {
        "schema_version": "1.0",
        "project_id": project,
        "status": "succeeded",
        "protocol": manifest["protocol"],
        "frozen_at": manifest["frozen_at"],
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "gold_sha256": manifest["gold_sha256"],
        "target_category": category,
        "metrics": metrics,
        "labelled_accuracy": labelled_accuracy,
        "predictions": "predictions",
        "graph": "graph.json",
    }
    write_json(output / "report.json", report)
    (output / "report.md").write_text(markdown(report), encoding="utf-8")
    write_json(output / "run.json", run_record)
    return run_record


def suite_report(records: list[dict]) -> dict:
    categories = {}
    total = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for record in records:
        category = record["target_category"]
        metrics = record["metrics"]
        categories[record["project_id"]] = {
            "category": category,
            "tp": metrics["tp"],
            "fp": metrics["fp"],
            "fn": metrics["fn"],
            "tn": metrics["tn"],
            "precision": metrics["precision"],
            "recall": metrics["recall"],
            "f1": metrics["f1"],
            "labelled_accuracy": record["labelled_accuracy"],
        }
        for key in total:
            total[key] += int(metrics.get(key) or 0)
    denominator = total["tp"] + total["fp"]
    recall_denominator = total["tp"] + total["fn"]
    accuracy_denominator = total["tp"] + total["tn"] + total["fp"] + total["fn"]
    precision = total["tp"] / denominator if denominator else None
    recall = total["tp"] / recall_denominator if recall_denominator else None
    f1 = (
        2 * total["tp"] / (2 * total["tp"] + total["fp"] + total["fn"])
        if 2 * total["tp"] + total["fp"] + total["fn"]
        else None
    )
    return {
        "schema_version": "1.0",
        "protocol": "specification_first_closed_world",
        "scope": "independent synthetic C microbenchmarks; not libuv or Redis",
        "projects": categories,
        "micro_totals": {
            **total,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "labelled_accuracy": (
                (total["tp"] + total["tn"]) / accuracy_denominator
                if accuracy_denominator
                else None
            ),
        },
    }


def build_gate(summary: dict) -> dict:
    checks = []
    for project, minimums in THRESHOLDS.items():
        if project == "micro_total" and set(summary["projects"]) != set(PROJECTS):
            continue
        if project != "micro_total" and project not in summary["projects"]:
            continue
        metrics = (
            summary["micro_totals"]
            if project == "micro_total"
            else summary["projects"][project]
        )
        for metric, minimum in minimums.items():
            actual = metrics.get(metric)
            checks.append(
                {
                    "name": f"{project}.{metric}",
                    "status": "passed" if actual is not None and actual >= minimum else "failed",
                    "actual": actual,
                    "minimum": minimum,
                }
            )
    return {
        "schema_version": "1.0",
        "status": "failed" if any(item["status"] == "failed" for item in checks) else "passed",
        "checks": checks,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=PROJECTS, action="append")
    args = parser.parse_args()
    selected = args.project or list(PROJECTS)
    records = [run_project(project) for project in selected]
    summary = suite_report(records)
    RESULTS.mkdir(parents=True, exist_ok=True)
    write_json(RESULTS / "summary.json", summary)
    gate = build_gate(summary)
    write_json(RESULTS / "gate.json", gate)
    lines = [
        "# Independent microbenchmark results",
        "",
        summary["scope"],
        "",
        "| Project | Category | TP | FP | FN | TN | Precision | Recall | F1 | Labelled accuracy |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, metrics in summary["projects"].items():
        fmt = lambda value: "N/A" if value is None else f"{value:.2%}"
        lines.append(
            "| {name} | {category} | {tp} | {fp} | {fn} | {tn} | {p} | {r} | {f} | {a} |".format(
                name=name,
                category=metrics["category"],
                tp=metrics["tp"],
                fp=metrics["fp"],
                fn=metrics["fn"],
                tn=metrics["tn"],
                p=fmt(metrics["precision"]),
                r=fmt(metrics["recall"]),
                f=fmt(metrics["f1"]),
                a=fmt(metrics["labelled_accuracy"]),
            )
        )
    totals = summary["micro_totals"]
    fmt = lambda value: "N/A" if value is None else f"{value:.2%}"
    lines.extend(
        [
            "",
            "## Micro total",
            "",
            "```text",
            f"TP={totals['tp']} FP={totals['fp']} FN={totals['fn']} TN={totals['tn']}",
            f"Precision={fmt(totals['precision'])}",
            f"Recall={fmt(totals['recall'])}",
            f"F1={fmt(totals['f1'])}",
            f"Labelled accuracy={fmt(totals['labelled_accuracy'])}",
            "```",
            "",
            f"Gate status: `{gate['status']}`.",
            "",
            "These results cover only the frozen synthetic microbenchmarks.",
            "The labels are AI-authored; human review remains pending.",
        ]
    )
    (RESULTS / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if gate["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
