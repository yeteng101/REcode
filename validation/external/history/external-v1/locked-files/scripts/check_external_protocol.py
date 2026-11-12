#!/usr/bin/env python3
"""Validate external benchmark selections and the candidate protocol."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "validation/external/protocol-v1.json"
SCHEMA = ROOT / "validation/external/prediction.schema.json"
REGISTRY = ROOT / "validation/external/registry.json"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_selection(
    name: str,
    path: Path,
    dataset_config: dict[str, Any],
) -> list[str]:
    errors = []
    if not path.is_file():
        return [f"{name}: missing selection {path.relative_to(ROOT)}"]
    data = read_json(path)
    if data.get("schema_version") != "selection-v1":
        errors.append(f"{name}: schema_version must be selection-v1")
    if data.get("dataset") != name:
        errors.append(f"{name}: dataset does not match protocol key")
    if data.get("identity_fields") != dataset_config["identity_fields"]:
        errors.append(f"{name}: identity_fields differ from protocol")
    cases = data.get("cases", [])
    units = data.get("units", [])
    if data.get("counts", {}).get("cases") != len(cases):
        errors.append(f"{name}: case count mismatch")
    if data.get("counts", {}).get("units") != len(units):
        errors.append(f"{name}: unit count mismatch")
    case_ids = [str(case.get("id", "")) for case in cases]
    if not all(case_ids) or len(case_ids) != len(set(case_ids)):
        errors.append(f"{name}: case IDs must be non-empty and unique")
    unit_ids = [str(unit.get("id", "")) for unit in units]
    if not all(unit_ids) or len(unit_ids) != len(set(unit_ids)):
        errors.append(f"{name}: unit IDs must be non-empty and unique")
    identities = [
        json.dumps(unit.get("identity"), sort_keys=True, ensure_ascii=False)
        for unit in units
    ]
    if len(identities) != len(set(identities)):
        errors.append(f"{name}: duplicate semantic identities")
    known_cases = set(case_ids)
    for unit in units:
        if str(unit.get("case_id", "")) not in known_cases:
            errors.append(f"{name}: unit references an unknown case")
            break
        missing = [
            field
            for field in data["identity_fields"]
            if field not in (unit.get("identity") or {})
        ]
        if missing:
            errors.append(f"{name}: identity missing fields {missing}")
            break
    return errors


def verify_selection_sources(name: str, data: dict[str, Any]) -> list[str]:
    errors = []
    if name == "callgraphbench":
        cache = ROOT / ".benchmark-cache/callgraphbench"
        for case in data["cases"]:
            path = cache / case["manifest"]
            if path.is_file() and sha256_path(path) != case["manifest_sha256"]:
                errors.append(f"{name}: manifest hash mismatch for {case['id']}")
    if name == "cgbench":
        parsed = ROOT / "validation/external/results/cgbench/cases.json"
        expected = str(data.get("source", {}).get("parsed_cases_sha256") or "")
        if parsed.is_file() and sha256_path(parsed) != expected:
            errors.append(f"{name}: parsed cases hash mismatch")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-missing-sources",
        action="store_true",
        help="skip local benchmark cache hash checks",
    )
    args = parser.parse_args()

    errors = []
    protocol = read_json(PROTOCOL)
    registry = read_json(REGISTRY)
    schema = read_json(SCHEMA)
    if protocol.get("protocol_id") != "external-v1":
        errors.append("protocol_id must be external-v1")
    if protocol.get("prediction_contract", {}).get("schema_version") != schema.get(
        "properties", {}
    ).get("schema_version", {}).get("const"):
        errors.append("protocol and prediction schema versions differ")
    for benchmark in registry["benchmarks"]:
        if benchmark not in {
            "callgraphbench",
            "cgbench",
            "juliet-c-cpp-1.3",
            "svcomp26",
            "dataracebench",
        }:
            errors.append(f"registry contains unknown benchmark {benchmark}")
    for name, config in protocol["datasets"].items():
        selection_path = ROOT / config["selection"]
        errors.extend(validate_selection(name, selection_path, config))
        if not args.allow_missing_sources and selection_path.is_file():
            errors.extend(verify_selection_sources(name, read_json(selection_path)))
    prompt = protocol["datasets"]["cgbench"].get("prompt")
    if prompt and not (ROOT / prompt).is_file():
        errors.append(f"CG-Bench prompt is missing: {prompt}")
    if errors:
        raise SystemExit("\n".join(errors))
    print(
        json.dumps(
            {
                "status": "ok",
                "protocol_id": protocol["protocol_id"],
                "datasets": {
                    name: read_json(ROOT / config["selection"])["counts"]
                    for name, config in protocol["datasets"].items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
