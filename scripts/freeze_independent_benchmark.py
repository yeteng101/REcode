#!/usr/bin/env python3
"""Freeze source and Gold hashes before any prediction is generated."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "validation/independent/fixtures"
PROJECTS = ("callgraph", "resources", "sync")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files(project_dir: Path) -> dict[str, str]:
    source_root = project_dir / "source"
    return {
        path.relative_to(project_dir).as_posix(): sha256(path)
        for path in sorted(source_root.rglob("*"))
        if path.is_file()
    }


def freeze_project(project: str) -> dict:
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
        "protocol": "specification_first_closed_world",
        "repository": truth["repository"],
        "commit": truth["commit"],
        "gold_sha256": sha256(truth_path),
        "spec_sha256": sha256(spec_path),
        "source_sha256": source_files(project_dir),
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
    for project in PROJECTS:
        print(json.dumps(freeze_project(project), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
