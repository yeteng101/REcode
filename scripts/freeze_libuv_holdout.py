#!/usr/bin/env python3
"""Freeze post-synthetic libuv holdout labels before scoring."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "validation/holdouts/libuv-v1"
LIBUV = ROOT / "third_party/libuv"
GRAPH = ROOT / "demo/libuv/graph.json"
PROJECTS = {
    "callgraph": ("src/unix/dl.c",),
    "resources": ("src/unix/dl.c",),
    "sync": ("src/unix/thread.c",),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_project(project: str, sources: tuple[str, ...]) -> dict:
    project_dir = BASE / project
    truth_path = project_dir / "ground-truth.json"
    spec_path = project_dir / "SPEC.md"
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    positives = sum(case["expected_exists"] is True for case in truth["cases"])
    negatives = len(truth["cases"]) - positives
    manifest = {
        "schema_version": "1.0",
        "project_id": project,
        "status": "frozen",
        "frozen_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "protocol": "post_synthetic_freeze_closed_world",
        "repository": truth["repository"],
        "commit": truth["commit"],
        "gold_sha256": sha256(truth_path),
        "spec_sha256": sha256(spec_path),
        "source_sha256": {
            source: sha256(LIBUV / source)
            for source in sources
        },
        "input_graph": "demo/libuv/graph.json",
        "input_graph_sha256": sha256(GRAPH),
        "label_counts": {
            "positive": positives,
            "negative": negatives,
        },
    }
    (project_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    for project, sources in PROJECTS.items():
        print(json.dumps(freeze_project(project, sources), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
