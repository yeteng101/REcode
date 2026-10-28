#!/usr/bin/env python3
"""Build deterministic SV-COMP MemSafety and Concurrency case indexes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / ".benchmark-cache/svcomp26"
OUTPUT = ROOT / "validation/external/results/svcomp"


def parse_task(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    input_files = re.findall(r"^\s*input_files:\s*'([^']+)'\s*$", text, re.MULTILINE)
    if not input_files:
        block = re.search(
            r"^\s*input_files:\s*\[(.*?)\]\s*$",
            text,
            re.MULTILINE | re.DOTALL,
        )
        if block:
            input_files = re.findall(r"'([^']+)'|\"([^\"]+)\"", block.group(1))
            input_files = [left or right for left, right in input_files]
    blocks = re.split(r"^\s*-\s*property_file:\s*", text, flags=re.MULTILINE)[1:]
    properties = []
    for block in blocks:
        lines = block.splitlines()
        property_file = lines[0].strip().strip("'\"")
        body = "\n".join(lines[1:])
        verdict = re.search(r"^\s*expected_verdict:\s*(true|false)\s*$", body, re.MULTILINE)
        subproperty = re.search(r"^\s*subproperty:\s*(\S+)\s*$", body, re.MULTILINE)
        properties.append(
            {
                "property_file": property_file,
                "expected_verdict": (
                    verdict.group(1) == "true" if verdict else None
                ),
                "subproperty": subproperty.group(1) if subproperty else "",
            }
        )
    language = re.search(r"^\s*language:\s*(\S+)\s*$", text, re.MULTILINE)
    data_model = re.search(r"^\s*data_model:\s*(\S+)\s*$", text, re.MULTILINE)
    return {
        "task_file": path.relative_to(DATASET).as_posix(),
        "input_files": input_files,
        "properties": properties,
        "language": language.group(1) if language else "C",
        "data_model": data_model.group(1) if data_model else "",
    }


def collect(root: Path, property_name: str, subproperties: set[str], limit: int) -> list[dict]:
    cases = []
    for task in sorted(root.glob("*.yml")):
        parsed = parse_task(task)
        for prop in parsed["properties"]:
            if Path(prop["property_file"]).name != property_name:
                continue
            if prop["expected_verdict"] is None:
                continue
            if subproperties and prop["subproperty"] not in subproperties:
                continue
            if not parsed["input_files"] or not all(
                (DATASET / str(task.parent.relative_to(DATASET)) / value).is_file()
                for value in parsed["input_files"]
            ):
                continue
            cases.append(
                {
                    **parsed,
                    "id": task.stem.replace(".", "_"),
                    "property": prop["property_file"],
                    "subproperty": prop["subproperty"],
                    "expected_verdict": prop["expected_verdict"],
                }
            )
            break
    per_verdict = max(1, limit // 2)
    selected = []
    for verdict in (True, False):
        selected.extend(
            [case for case in cases if case["expected_verdict"] is verdict][:per_verdict]
        )
    remaining = [case for case in cases if case not in selected]
    selected.extend(remaining[: max(0, limit - len(selected))])
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not DATASET.is_dir():
        parser.error(
            "SV-COMP is not fetched; run scripts/fetch_external_benchmarks.py "
            "--benchmark svcomp26"
        )
    groups = {
        "memsafety": collect(
            DATASET / "c/memsafety",
            "valid-memsafety.prp",
            {"", "valid-memtrack", "valid-memcleanup"},
            args.limit,
        ),
        "concurrency": collect(
            DATASET / "c/pthread",
            "no-data-race.prp",
            set(),
            args.limit,
        ),
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": "1.0",
        "dataset": "SV-COMP 2026",
        "commit": "7efe28dd29576b46927b7a34e8f742bd90966a75",
        "license": "Varies by task; retain task provenance",
        "selection": {"limit_per_group": args.limit},
        "groups": groups,
    }
    (OUTPUT / "cases.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                group: len(cases)
                for group, cases in groups.items()
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
