#!/usr/bin/env python3
"""Verify frozen libuv holdout data, then run native extraction and scoring."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clang_pipeline.clang_ast import read_json, write_json
from clang_pipeline.repo_agent import _relativize_json_paths
from scripts.export_validation import export_graph
from validation.benchmark import evaluate, markdown


BASE = ROOT / "validation/holdouts/libuv-v1"
LIBUV = ROOT / "third_party/libuv"
GRAPH = ROOT / "demo/libuv/graph.json"
RESULTS = BASE / "results"
PROJECTS = ("callgraph", "resources", "sync")
THRESHOLDS = {
    "callgraph": {"precision": 0.90, "recall": 0.90},
    "resources": {"precision": 0.90, "recall": 0.90},
    "sync": {"precision": 0.90, "recall": 0.25},
    "micro_total": {"precision": 0.90, "recall": 0.85},
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_frozen(project: str) -> dict:
    project_dir = BASE / project
    manifest_path = project_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"{project}: manifest.json is missing; freeze the holdout first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "frozen":
        raise ValueError(f"{project}: holdout is not frozen")
    for label, expected in (
        ("ground-truth.json", manifest["gold_sha256"]),
        ("SPEC.md", manifest["spec_sha256"]),
    ):
        if sha256(project_dir / label) != expected:
            raise ValueError(f"{project}: {label} changed after freeze")
    for source, expected in manifest["source_sha256"].items():
        if sha256(LIBUV / source) != expected:
            raise ValueError(f"{project}: libuv source changed: {source}")
    if sha256(GRAPH) != manifest["input_graph_sha256"]:
        raise ValueError(f"{project}: input graph changed after freeze")
    commit = subprocess.run(
        ["git", "-C", str(LIBUV), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if commit != manifest["commit"]:
        raise ValueError(f"{project}: libuv commit mismatch: {commit}")
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
    output = RESULTS / project
    reset_output(output)
    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    graph = _relativize_json_paths(read_json(GRAPH), LIBUV)
    docs = export_graph(graph, native=True, source_root=LIBUV)

    if sha256(project_dir / "ground-truth.json") != manifest["gold_sha256"]:
        raise ValueError(f"{project}: Gold changed during analyzer execution")
    truth = json.loads((project_dir / "ground-truth.json").read_text(encoding="utf-8"))
    report = evaluate(truth, docs)
    category = truth["annotation_scope"]["categories"][0]
    metrics = report["categories"][category]
    denominator = metrics["tp"] + metrics["tn"] + metrics["fp"] + metrics["fn"]
    accuracy = (
        (metrics["tp"] + metrics["tn"]) / denominator if denominator else None
    )
    record = {
        "schema_version": "1.0",
        "project_id": project,
        "status": "succeeded",
        "protocol": manifest["protocol"],
        "frozen_at": manifest["frozen_at"],
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "gold_sha256": manifest["gold_sha256"],
        "input_graph_sha256": manifest["input_graph_sha256"],
        "target_category": category,
        "metrics": metrics,
        "labelled_accuracy": accuracy,
    }
    write_json(output / "report.json", report)
    (output / "report.md").write_text(markdown(report), encoding="utf-8")
    write_json(output / "run.json", record)
    return record


def suite_report(records: list[dict]) -> dict:
    projects = {}
    total = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for record in records:
        metrics = record["metrics"]
        projects[record["project_id"]] = {
            "category": record["target_category"],
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
    precision_denominator = total["tp"] + total["fp"]
    recall_denominator = total["tp"] + total["fn"]
    accuracy_denominator = total["tp"] + total["tn"] + total["fp"] + total["fn"]
    precision = (
        total["tp"] / precision_denominator if precision_denominator else None
    )
    recall = total["tp"] / recall_denominator if recall_denominator else None
    f1_denominator = 2 * total["tp"] + total["fp"] + total["fn"]
    return {
        "schema_version": "1.0",
        "protocol": "post_synthetic_freeze_closed_world",
        "scope": "pinned libuv v1.50.0 holdout; labels frozen before native extraction",
        "projects": projects,
        "micro_totals": {
            **total,
            "precision": precision,
            "recall": recall,
            "f1": 2 * total["tp"] / f1_denominator if f1_denominator else None,
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
    fmt = lambda value: "N/A" if value is None else f"{value:.2%}"
    lines = [
        "# libuv post-freeze holdout results",
        "",
        summary["scope"],
        "",
        "| Project | Category | TP | FP | FN | TN | Precision | Recall | F1 | Labelled accuracy |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, metrics in summary["projects"].items():
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
    lines.extend(
        [
            "",
            "## Holdout total",
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
            "Human review of this new holdout remains pending.",
        ]
    )
    (RESULTS / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if gate["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
