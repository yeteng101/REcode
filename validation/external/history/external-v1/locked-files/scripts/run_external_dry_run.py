#!/usr/bin/env python3
"""Run a small end-to-end validation of every external benchmark adapter."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "validation/external/results/dry-run"


def run(command: list[str], *, timeout: int = 1200) -> dict:
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
            f"command failed: {' '.join(command)}\n"
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return {
        "command": command,
        "stdout_tail": completed.stdout.strip().splitlines()[-5:],
    }


def require(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"dry-run output missing: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output_root.resolve()
    output.mkdir(parents=True, exist_ok=True)
    python = sys.executable
    steps = []
    steps.append(run([python, "scripts/build_external_selections.py"]))
    steps.append(run([python, "scripts/check_external_protocol.py"]))

    cgbench = output / "cgbench"
    steps.append(
        run(
            [
                python,
                "scripts/run_cgbench_agent.py",
                "--limit",
                "6",
                "--output",
                str(cgbench / "predictions.json"),
            ]
        )
    )
    steps.append(
        run(
            [
                python,
                "scripts/score_cgbench.py",
                "--predictions",
                str(cgbench / "predictions.json"),
                "--output",
                str(cgbench / "report.json"),
            ]
        )
    )
    require(cgbench / "predictions.json")
    require(cgbench / "report.json")

    callgraph = output / "callgraphbench"
    steps.append(
        run(
            [
                python,
                "scripts/run_callgraphbench.py",
                "--limit",
                "1",
                "--output",
                str(callgraph),
            ]
        )
    )
    require(callgraph / "predictions.json")

    juliet = output / "juliet"
    steps.append(
        run(
            [
                python,
                "scripts/run_juliet_resource.py",
                "--limit",
                "1",
                "--output",
                str(juliet),
            ]
        )
    )
    require(juliet / "predictions.json")

    memsafety = output / "svcomp-memsafety"
    steps.append(
        run(
            [
                python,
                "scripts/run_svcomp_memsafety.py",
                "--limit",
                "2",
                "--output",
                str(memsafety),
            ]
        )
    )
    require(memsafety / "predictions.json")

    datarace = output / "dataracebench"
    steps.append(
        run(
            [
                python,
                "scripts/run_dataracebench.py",
                "--limit",
                "4",
                "--output",
                str(datarace),
            ]
        )
    )
    require(datarace / "predictions.json")

    concurrency = output / "svcomp-concurrency"
    steps.append(
        run(
            [
                python,
                "scripts/run_svcomp_concurrency.py",
                "--limit",
                "4",
                "--output",
                str(concurrency),
            ]
        )
    )
    require(concurrency / "predictions.json")

    summary = {
        "schema_version": "external-dry-run-v1",
        "protocol_id": "external-v1",
        "status": "succeeded",
        "output_root": str(output),
        "benchmarks": {
            "callgraphbench": {"prediction": "callgraphbench/predictions.json"},
            "cgbench": {"prediction": "cgbench/predictions.json"},
            "juliet-cwe401": {"prediction": "juliet/predictions.json"},
            "svcomp-memsafety": {"prediction": "svcomp-memsafety/predictions.json"},
            "dataracebench": {"prediction": "dataracebench/predictions.json"},
            "svcomp-concurrency": {"prediction": "svcomp-concurrency/predictions.json"},
        },
        "steps": steps,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "succeeded", "path": str(output / "summary.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
