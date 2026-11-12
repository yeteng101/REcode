#!/usr/bin/env python3
"""Run a new baseline bound to the active frozen analyzer revision."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from clang_pipeline.evaluation_protocol import PROTOCOL_ID, RESULTS_ROOT

HISTORY = ROOT / "validation/external/history" / (PROTOCOL_ID + "-baseline.json")
LOCK = ROOT / "validation/external/protocol-lock.json"


def run(command: list[str], timeout: int = 3600) -> None:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"failed: {' '.join(command)}\n"
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    print(f"[baseline] {command[1]} ok")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-dry-run",
        action="store_true",
        help="run only the full fixed subset baseline",
    )
    args = parser.parse_args()
    python = sys.executable
    run([python, "scripts/build_external_selections.py"])
    run([python, "scripts/check_external_protocol.py"])
    run([python, "scripts/freeze_external_protocol.py", "--check"])
    if not args.skip_dry_run:
        run([python, "scripts/run_external_dry_run.py"])
    run([python, "scripts/run_callgraphbench.py"])
    run([python, "scripts/run_cgbench_agent.py"])
    run(
        [
            python,
            "scripts/score_cgbench.py",
            "--predictions",
            str(RESULTS_ROOT / "cgbench/predictions.json"),
        ]
    )
    run([python, "scripts/run_juliet_resource.py"])
    run([python, "scripts/run_svcomp_memsafety.py"])
    run([python, "scripts/run_dataracebench.py"])
    run([python, "scripts/run_svcomp_concurrency.py"])
    run([python, "scripts/update_external_status.py"])
    lock_digest = None
    if LOCK.is_file():
        lock_digest = json.loads(LOCK.read_text(encoding="utf-8")).get("lock_digest")
    result_prefix = RESULTS_ROOT.relative_to(ROOT).as_posix()
    payload = {
        "schema_version": "external-baseline-v1",
        "protocol_id": PROTOCOL_ID,
        "status": "completed",
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "protocol_lock": (
            {
                "path": "validation/external/protocol-lock.json",
                "lock_digest": lock_digest,
            }
            if lock_digest
            else None
        ),
        "status_report": (RESULTS_ROOT / "status.json").relative_to(ROOT).as_posix(),
        "reports": {
            "callgraphbench": f"{result_prefix}/callgraphbench/summary.json",
            "cgbench": f"{result_prefix}/cgbench/report.json",
            "juliet": f"{result_prefix}/juliet/report.json",
            "svcomp_memsafety": f"{result_prefix}/svcomp/memsafety/report.json",
            "dataracebench": f"{result_prefix}/dataracebench/report.json",
            "svcomp_concurrency": f"{result_prefix}/svcomp/concurrency/report.json",
        },
        "predictions": {
            "callgraphbench": f"{result_prefix}/callgraphbench/predictions.json",
            "cgbench": f"{result_prefix}/cgbench/predictions.json",
            "juliet": f"{result_prefix}/juliet/predictions.json",
            "svcomp_memsafety": f"{result_prefix}/svcomp/memsafety/predictions.json",
            "dataracebench": f"{result_prefix}/dataracebench/predictions.json",
            "svcomp_concurrency": f"{result_prefix}/svcomp/concurrency/predictions.json",
        },
    }
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "completed", "path": str(HISTORY)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
