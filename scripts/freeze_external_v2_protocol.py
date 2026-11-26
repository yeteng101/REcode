#!/usr/bin/env python3
"""Create or verify the immutable external-v2 CallGraphBench lock."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "validation/external-v2/protocol-lock-v2.json"
LOCKED_FILES = (
    "validation/external-v2/protocol-v2.json",
    "validation/external-v2/selections/callgraphbench-v2.json",
    "validation/external/prediction.schema.json",
    "scripts/run_callgraphbench.py",
    "scripts/run_svcomp_memsafety.py",
    "scripts/run_juliet_resource.py",
    "clang_pipeline/function_targets.py",
    "clang_pipeline/resource_flow.py",
    "clang_pipeline/pipeline.py",
    "clang_pipeline/clang_ast.py",
    "validation/external-v2/results/callgraphbench/predictions.json",
    "validation/external-v2/results/callgraphbench/summary.json",
    "validation/external-v2/results/callgraphbench/report.md",
    "validation/external-v2/results/svcomp/memsafety/predictions.json",
    "validation/external-v2/results/svcomp/memsafety/report.json",
    "validation/external-v2/results/svcomp/memsafety/report.md",
    "validation/external-v2/results/juliet/predictions.json",
    "validation/external-v2/results/juliet/report.json",
    "validation/external-v2/results/juliet/report.md",
)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_lock() -> dict:
    missing = [path for path in LOCKED_FILES if not (ROOT / path).is_file()]
    if missing:
        raise SystemExit("cannot lock missing files:\n" + "\n".join(missing))
    files = {path: sha256_path(ROOT / path) for path in LOCKED_FILES}
    return {
        "schema_version": "external-protocol-lock-v2",
        "protocol_id": "external-v2",
        "status": "locked",
        "hash_algorithm": "sha256",
        "files": files,
        "lock_digest": hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def verify_lock() -> list[str]:
    if not LOCK_PATH.is_file():
        return ["protocol-lock-v2.json does not exist"]
    lock = read_json(LOCK_PATH)
    errors = []
    if lock.get("schema_version") != "external-protocol-lock-v2":
        errors.append("invalid lock schema")
    if lock.get("protocol_id") != "external-v2":
        errors.append("lock protocol_id is not external-v2")
    files = lock.get("files", {})
    if set(files) != set(LOCKED_FILES):
        errors.append("locked file list differs from freezer")
    for path in LOCKED_FILES:
        target = ROOT / path
        if not target.is_file():
            errors.append(f"missing locked file: {path}")
        elif files.get(path) != sha256_path(target):
            errors.append(f"hash mismatch: {path}")
    expected = hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if lock.get("lock_digest") != expected:
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
