#!/usr/bin/env python3
"""Extract the pinned CG-Bench markdown examples with the official parser."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / ".benchmark-cache/cgbench"
OUTPUT = ROOT / "validation/external/results/cgbench"


def main() -> int:
    if not BENCHMARK.is_dir():
        raise SystemExit(
            "CG-Bench is not fetched; run scripts/fetch_external_benchmarks.py "
            "--benchmark cgbench"
        )
    completed = subprocess.run(
        [sys.executable, "extract_from_markdowns.py"],
        cwd=BENCHMARK,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise SystemExit(completed.stderr.strip() or completed.stdout.strip())
    source = BENCHMARK / "cgbench.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    cases = []
    for project, callsites in sorted(data.items()):
        if not isinstance(callsites, dict):
            continue
        for callsite, example in sorted(callsites.items()):
            if not isinstance(example, dict):
                continue
            targets = sorted(example.get("callees", {}).get("targets", {}))
            case_id = "cgbench_" + hashlib.sha256(
                f"{project}:{callsite}".encode("utf-8")
            ).hexdigest()[:12]
            cases.append(
                {
                    "id": case_id,
                    "project": project,
                    "callsite": callsite,
                    "fnptr": example.get("callsite", ""),
                    "type": example.get("type", ""),
                    "targets": targets,
                    "code_blocks": example.get("chain_summary", []),
                }
            )
    rows = len(cases)
    targets = sum(len(case["targets"]) for case in cases)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "cases.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "dataset": "CG-Bench",
                "commit": "b277b7329f62fdddc6d4a341057d045bdf69def2",
                "cases": cases,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    summary = {
        "schema_version": "1.0",
        "dataset": "CG-Bench",
        "commit": "b277b7329f62fdddc6d4a341057d045bdf69def2",
        "projects": sorted(data),
        "callsites": rows,
        "targets": targets,
        "evaluation_mode": "agent_or_external_predictions",
        "note": (
            "CG-Bench examples are extracted from real C/C++ projects and require "
            "function-pointer target predictions; no labels are inferred from analyzer output."
        ),
    }
    (OUTPUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
