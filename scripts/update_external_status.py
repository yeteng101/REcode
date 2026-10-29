#!/usr/bin/env python3
"""Aggregate external benchmark reports into status.json."""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "validation/external/results"
OUTPUT = RESULTS / "status.json"


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    callgraph = read(RESULTS / "callgraphbench/summary.json")
    cgbench = read(RESULTS / "cgbench/report.json")
    juliet = read(RESULTS / "juliet/report.json")
    memsafety = read(RESULTS / "svcomp/memsafety/report.json")
    datarace = read(RESULTS / "dataracebench/report.json")
    concurrency = read(RESULTS / "svcomp/concurrency/report.json")
    status = {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "protocol_id": "external-v1",
        "groups": {
            "call_graph": {
                "datasets": ["CallGraphBench", "CG-Bench"],
                "status": "locked_baseline_complete",
                "callgraphbench": {
                    "status": "baseline_complete",
                    "cases": len(callgraph.get("cases", {})),
                    **callgraph["totals"],
                },
                "cgbench": {
                    "status": "agent_baseline_complete",
                    **cgbench["totals"],
                },
            },
            "resource_flow": {
                "datasets": ["NIST Juliet C/C++ 1.3", "SV-COMP 2026 C.MemSafety"],
                "status": "locked_baseline_complete",
                "juliet": {
                    "status": "baseline_complete",
                    "scope": juliet["scope"],
                    "tp": juliet["tp"],
                    "fp": juliet["fp"],
                    "fn": juliet["fn"],
                    "tn": juliet["tn"],
                    "unknown": juliet["unknown"],
                    "precision": juliet["precision"],
                    "recall": juliet["recall"],
                    "coverage": juliet["coverage"],
                },
                "svcomp_memsafety": {
                    "status": "baseline_complete",
                    "property": memsafety["property"],
                    "tp": memsafety["tp"],
                    "fp": memsafety["fp"],
                    "fn": memsafety["fn"],
                    "tn": memsafety["tn"],
                    "unknown": memsafety["unknown"],
                    "precision": memsafety["precision"],
                    "recall": memsafety["recall"],
                    "coverage": memsafety["coverage"],
                },
            },
            "sync_relation": {
                "datasets": ["DataRaceBench", "SV-COMP 2026 C.Concurrency"],
                "status": "locked_baseline_complete",
                "dataracebench": {
                    "status": "native_tristate_baseline_complete",
                    "metric": datarace["metric"],
                    "cases": len(datarace["cases"]),
                    "tp": datarace["tp"],
                    "fp": datarace["fp"],
                    "fn": datarace["fn"],
                    "tn": datarace["tn"],
                    "unknown": datarace["unknown"],
                    "race_precision": datarace["race_precision"],
                    "race_recall": datarace["race_recall"],
                    "false_race_rate": datarace["false_race_rate"],
                    "no_race_recall": datarace["no_race_recall"],
                    "coverage": datarace["coverage"],
                },
                "svcomp_concurrency": {
                    "status": "native_tristate_baseline_complete",
                    "property": concurrency["property"],
                    "cases": len(concurrency["cases"]),
                    "tp": concurrency["tp"],
                    "fp": concurrency["fp"],
                    "fn": concurrency["fn"],
                    "tn": concurrency["tn"],
                    "unknown": concurrency["unknown"],
                    "race_precision": concurrency["race_precision"],
                    "race_recall": concurrency["race_recall"],
                    "false_race_rate": concurrency["false_race_rate"],
                    "no_race_recall": concurrency["no_race_recall"],
                    "coverage": concurrency["coverage"],
                },
            },
        },
    }
    OUTPUT.write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "updated", "path": str(OUTPUT)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
