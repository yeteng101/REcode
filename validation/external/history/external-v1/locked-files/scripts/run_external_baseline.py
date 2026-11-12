#!/usr/bin/env python3
"""Run the complete fixed external-v1 baseline in the prescribed order."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "validation/external/history/external-v1-baseline.json"
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
    if not args.skip_dry_run:
        run([python, "scripts/run_external_dry_run.py"])
    run([python, "scripts/run_callgraphbench.py"])
    run([python, "scripts/run_cgbench_agent.py"])
    run(
        [
            python,
            "scripts/score_cgbench.py",
            "--predictions",
            "validation/external/results/cgbench/predictions.json",
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
    payload = {
        "schema_version": "external-baseline-v1",
        "protocol_id": "external-v1",
        "status": "locked" if lock_digest else "candidate",
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
        "status_report": "validation/external/results/status.json",
        "reports": {
            "callgraphbench": "validation/external/results/callgraphbench/summary.json",
            "cgbench": "validation/external/results/cgbench/report.json",
            "juliet": "validation/external/results/juliet/report.json",
            "svcomp_memsafety": "validation/external/results/svcomp/memsafety/report.json",
            "dataracebench": "validation/external/results/dataracebench/report.json",
            "svcomp_concurrency": "validation/external/results/svcomp/concurrency/report.json",
        },
        "predictions": {
            "callgraphbench": "validation/external/results/callgraphbench/predictions.json",
            "cgbench": "validation/external/results/cgbench/predictions.json",
            "juliet": "validation/external/results/juliet/predictions.json",
            "svcomp_memsafety": "validation/external/results/svcomp/memsafety/predictions.json",
            "dataracebench": "validation/external/results/dataracebench/predictions.json",
            "svcomp_concurrency": "validation/external/results/svcomp/concurrency/predictions.json",
        },
    }
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "candidate", "path": str(HISTORY)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
