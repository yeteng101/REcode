#!/usr/bin/env python3
"""Build the source-backed Redis target-repository v2 Gold set."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CALLS = ROOT / "validation/redis/call-chains.json"
SYNC = ROOT / "validation/redis/sync-relations.json"
RESOURCES = ROOT / "validation/redis/resource-flow-v2.json"
SOURCE_ROOT = ROOT.parent / "third_party/redis"
OUTPUT = ROOT / "validation/benchmarks/redis/ground-truth-v2.json"
SOURCE_CHECK = ROOT / "validation/benchmarks/redis/source-check-v2.json"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sync_cases(data: dict[str, Any]) -> list[dict[str, Any]]:
    cases = []
    for sample in data["samples"]:
        evidence = sample["evidence"]
        by_id = {item["id"]: item for item in evidence}
        source = by_id[sample["source"]["evidence_ids"][0]]
        target = by_id[sample["target"]["evidence_ids"][0]]
        base = {
            "id": sample["id"],
            "category": "sync_relation",
            "identity": [
                sample["source"]["function"],
                sample["target"]["function"],
                source["file"],
                source["line"],
                target["file"],
                target["line"],
            ],
            "kind": sample["order"],
            "expected_exists": True,
            "evidence": evidence,
            "notes": sample["reason"],
            "condition": sample["condition"],
        }
        cases.append(base)
        if sample["claim_order"] != sample["order"]:
            cases.append(
                {
                    **base,
                    "id": sample["id"] + "_rejected_claim",
                    "kind": sample["claim_order"],
                    "expected_exists": False,
                }
            )
    return cases


def verify_source_evidence(
    cases: list[dict[str, Any]],
) -> tuple[list[str], dict[str, str]]:
    errors: list[str] = []
    hashes: dict[str, str] = {}
    cache: dict[str, list[str]] = {}
    for case in cases:
        for evidence in case.get("evidence", []):
            file = str(evidence.get("file", ""))
            path = ROOT.parent / file
            if not path.is_file():
                errors.append(f"{case['id']}: missing source {file}")
                continue
            lines = cache.setdefault(
                file,
                path.read_text(encoding="utf-8", errors="replace").splitlines(),
            )
            line = int(evidence.get("line") or 0)
            if line < 1 or line > len(lines):
                errors.append(f"{case['id']}: evidence line out of range {file}:{line}")
                continue
            if " ".join(lines[line - 1].split()) != " ".join(
                str(evidence.get("snippet", "")).split()
            ):
                errors.append(f"{case['id']}: evidence snippet mismatch {file}:{line}")
            raw = path.read_bytes().replace(b"\r\n", b"\n")
            hashes[file] = hashlib.sha256(raw).hexdigest()
    return errors, hashes


def build() -> dict[str, Any]:
    calls = read_json(CALLS)
    sync = read_json(SYNC)
    resources = read_json(RESOURCES)
    if calls["commit"] != resources["commit"] or sync["commit"] != resources["commit"]:
        raise ValueError("Redis annotation commits do not match")

    call_cases = list(calls["cases"])
    resource_cases = []
    for resource in resources["resources"]:
        for operation in resource["operations"]:
            location = operation["location"]
            resource_cases.append(
                {
                    "id": f"{resource['id']}_{operation['operation_id']}",
                    "category": "resource_flow",
                    "identity": [
                        operation["function"],
                        location["file"],
                        location["line"],
                    ],
                    "kind": operation["kind"],
                    "expected_exists": True,
                    "evidence": [location],
                    "notes": f"Redis resource operation from {resource['id']}",
                }
            )
    cases = call_cases + resource_cases + sync_cases(sync)
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate Redis v2 Gold case IDs")
    errors, hashes = verify_source_evidence(cases)
    if errors:
        raise ValueError("\n".join(errors[:20]))

    truth = {
        "schema_version": "1.0",
        "dataset_id": "redis-7.2.4-target-v2",
        "repository": "redis/redis",
        "commit": resources["commit"],
        "identity_profile": "source_anchor_v1",
        "review": {
            "human_review": "pending",
            "human_confirmed_at": "",
            "method": "source-reviewed v1 call/sync annotations plus v2 resource facts",
            "limitation": (
                "Redis target v2 resource annotations are source-backed but "
                "resource-level human review is pending. Fixes or new facts "
                "require a new Gold version."
            ),
        },
        "supersedes": "redis-7.2.4-baseline-v1",
        "annotation_scope": {
            "mode": "open_world",
            "paths": ["third_party/redis/src"],
            "categories": ["call_chain", "resource_flow", "sync_relation"],
        },
        "cases": cases,
    }
    source_check = {
        "schema_version": "1.0",
        "dataset_id": truth["dataset_id"],
        "commit": truth["commit"],
        "source_root": "third_party/redis",
        "files": hashes,
        "cases": len(cases),
        "errors": [],
        "status": "verified",
    }
    return {"truth": truth, "source_check": source_check}


def main() -> int:
    result = build()
    write_json(OUTPUT, result["truth"])
    write_json(SOURCE_CHECK, result["source_check"])
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "source_check": str(SOURCE_CHECK),
                "cases": len(result["truth"]["cases"]),
                "call_chain": sum(
                    case["category"] == "call_chain"
                    for case in result["truth"]["cases"]
                ),
                "resource_flow": sum(
                    case["category"] == "resource_flow"
                    for case in result["truth"]["cases"]
                ),
                "sync_relation": sum(
                    case["category"] == "sync_relation"
                    for case in result["truth"]["cases"]
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
