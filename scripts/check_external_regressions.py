#!/usr/bin/env python3
"""Fail when locked external-v1 or external-v2 metrics regress."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
V1_EXPECTED = ROOT / "validation/external/expected-metrics-v1.json"
V1_STATUS = ROOT / "validation/external/results/status.json"
V2_EXPECTED = ROOT / "validation/external-v2/expected-metrics-v2.json"
V2_SUMMARY = (
    ROOT / "validation/external-v2/results/callgraphbench/summary.json"
)
V2_SVCOMP = (
    ROOT / "validation/external-v2/results/svcomp/memsafety/report.json"
)
V2_JULIET = ROOT / "validation/external-v2/results/juliet/report.json"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def compare_metrics(
    actual: dict[str, Any],
    expected: dict[str, Any],
    *,
    location: str,
) -> list[str]:
    errors = []
    for key, wanted in expected.items():
        current = actual.get(key)
        if isinstance(wanted, float):
            if current is None or float(current) + 1e-12 < wanted:
                errors.append(
                    f"{location}.{key}: {current!r} < expected {wanted!r}"
                )
        elif current != wanted:
            errors.append(
                f"{location}.{key}: {current!r} != expected {wanted!r}"
            )
    return errors


def check_v1() -> list[str]:
    expected = read_json(V1_EXPECTED)
    status = read_json(V1_STATUS)
    actual_groups = status.get("groups", {})
    mapping = {
        "callgraphbench": actual_groups.get("call_graph", {}).get(
            "callgraphbench", {}
        ),
        "cgbench": actual_groups.get("call_graph", {}).get("cgbench", {}),
        "juliet": actual_groups.get("resource_flow", {}).get("juliet", {}),
        "svcomp_memsafety": actual_groups.get("resource_flow", {}).get(
            "svcomp_memsafety", {}
        ),
        "dataracebench": actual_groups.get("sync_relation", {}).get(
            "dataracebench", {}
        ),
        "svcomp_concurrency": actual_groups.get("sync_relation", {}).get(
            "svcomp_concurrency", {}
        ),
    }
    errors = []
    for name, metrics in expected["datasets"].items():
        errors.extend(
            compare_metrics(
                mapping.get(name, {}),
                metrics,
                location=f"external-v1.{name}",
            )
        )
    return errors


def check_v2() -> list[str]:
    expected = read_json(V2_EXPECTED)
    summary = read_json(V2_SUMMARY)
    errors = compare_metrics(
        summary.get("totals", {}),
        expected["datasets"]["callgraphbench"],
        location="external-v2.callgraphbench",
    )
    svcomp = read_json(V2_SVCOMP)
    errors.extend(
        compare_metrics(
            svcomp,
            expected["datasets"]["svcomp_memsafety"],
            location="external-v2.svcomp_memsafety",
        )
    )
    juliet = read_json(V2_JULIET)
    errors.extend(
        compare_metrics(
            juliet,
            expected["datasets"]["juliet_cwe401"],
            location="external-v2.juliet_cwe401",
        )
    )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    errors = check_v1() + check_v2()
    report = {
        "schema_version": "external-regression-check-v1",
        "status": "passed" if not errors else "failed",
        "errors": errors,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
