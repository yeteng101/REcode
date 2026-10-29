#!/usr/bin/env python3
"""Create or verify the immutable external-v1 protocol lock."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "validation/external/protocol-lock.json"
LOCKED_FILES = (
    "validation/external/protocol-v1.json",
    "validation/external/prediction.schema.json",
    "validation/external/registry.json",
    "validation/external/prompts/cgbench-target-agent-v1.txt",
    "validation/external/selections/callgraphbench-v1.json",
    "validation/external/selections/cgbench-v1.json",
    "validation/external/selections/juliet-cwe401-v1.json",
    "validation/external/selections/svcomp-memsafety-v1.json",
    "validation/external/selections/svcomp-concurrency-v1.json",
    "validation/external/selections/dataracebench-v1.json",
    "scripts/run_cgbench_agent.py",
    "scripts/run_callgraphbench.py",
    "scripts/score_cgbench.py",
    "scripts/run_juliet_resource.py",
    "scripts/run_svcomp_memsafety.py",
    "scripts/run_dataracebench.py",
    "scripts/run_svcomp_concurrency.py",
    "scripts/build_external_selections.py",
    "scripts/check_external_protocol.py",
    "scripts/run_external_dry_run.py",
    "scripts/run_external_baseline.py",
    "scripts/update_external_status.py",
    "clang_pipeline/evaluation_protocol.py",
    "clang_pipeline/race_detector.py",
    "clang_pipeline/resource_flow.py",
    "clang_pipeline/sync_flow.py",
    "schemas/race-analysis.schema.json",
)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_lock() -> dict[str, Any]:
    missing = [path for path in LOCKED_FILES if not (ROOT / path).is_file()]
    if missing:
        raise SystemExit("cannot lock missing files:\n" + "\n".join(missing))
    protocol = read_json(ROOT / "validation/external/protocol-v1.json")
    hashes = {path: sha256_path(ROOT / path) for path in LOCKED_FILES}
    return {
        "schema_version": "external-protocol-lock-v1",
        "protocol_id": protocol["protocol_id"],
        "status": "locked",
        "hash_algorithm": "sha256",
        "files": hashes,
        "lock_digest": hashlib.sha256(
            json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def verify_lock() -> list[str]:
    if not LOCK_PATH.is_file():
        return ["protocol-lock.json does not exist"]
    lock = read_json(LOCK_PATH)
    errors = []
    if lock.get("schema_version") != "external-protocol-lock-v1":
        errors.append("invalid lock schema")
    if lock.get("protocol_id") != "external-v1":
        errors.append("lock protocol_id is not external-v1")
    files = lock.get("files", {})
    if set(files) != set(LOCKED_FILES):
        errors.append("locked file list differs from freezer")
    for path in LOCKED_FILES:
        target = ROOT / path
        if not target.is_file():
            errors.append(f"missing locked file: {path}")
        elif files.get(path) != sha256_path(target):
            errors.append(f"hash mismatch: {path}")
    expected_digest = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if lock.get("lock_digest") != expected_digest:
        errors.append("lock_digest mismatch")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.write:
        lock = build_lock()
        LOCK_PATH.write_text(
            json.dumps(lock, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "status": "written",
                    "path": str(LOCK_PATH.relative_to(ROOT)),
                    "lock_digest": lock["lock_digest"],
                    "files": len(lock["files"]),
                },
                indent=2,
            )
        )
        return 0
    errors = verify_lock()
    if errors:
        raise SystemExit("\n".join(errors))
    print(json.dumps({"status": "ok", "path": str(LOCK_PATH.relative_to(ROOT))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
