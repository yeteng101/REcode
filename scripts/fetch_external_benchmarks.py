#!/usr/bin/env python3
"""Fetch pinned public benchmarks into the git-ignored local cache."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "validation/external/registry.json"


def run(command: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_head(path: Path) -> str:
    return run(["git", "-C", str(path), "rev-parse", "HEAD"])


def fetch_git(destination: Path, source: dict) -> None:
    expected = str(source["commit"])
    if destination.exists():
        if git_head(destination) != expected:
            raise ValueError(
                f"{destination}: cached commit differs from pinned {expected}"
            )
        return
    if destination.parent.exists():
        shutil.rmtree(destination.parent / (destination.name + ".tmp"), ignore_errors=True)
    temporary = destination.parent / (destination.name + ".tmp")
    temporary.mkdir(parents=True)
    run(["git", "init"], cwd=temporary)
    run(["git", "remote", "add", "origin", str(source["repo"])], cwd=temporary)
    sparse_paths = list(source.get("sparse_paths") or [])
    if sparse_paths:
        run(["git", "sparse-checkout", "init", "--cone"], cwd=temporary)
        run(["git", "sparse-checkout", "set", *sparse_paths], cwd=temporary)
    run(
        [
            "git",
            "fetch",
            "--depth",
            "1",
            "--filter=blob:none",
            "origin",
            expected,
        ],
        cwd=temporary,
    )
    run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=temporary)
    if git_head(temporary) != expected:
        raise ValueError(f"fetched commit mismatch for {destination.name}")
    temporary.rename(destination)


def extract_archive(destination: Path, source: dict) -> None:
    expected = str(source["sha256"])
    if destination.exists():
        marker = destination / ".archive-sha256"
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() == expected:
            return
        raise ValueError(f"{destination}: cached archive does not match pinned hash")
    archive = destination.parent / (destination.name + ".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not archive.is_file():
        try:
            urllib.request.urlretrieve(str(source["url"]), archive)
        except Exception:
            archive.unlink(missing_ok=True)
            run(
                [
                    "curl",
                    "-L",
                    "--fail",
                    "--output",
                    str(archive),
                    str(source["url"]),
                ]
            )
    actual = sha256(archive)
    if actual != expected:
        archive.unlink(missing_ok=True)
        raise ValueError(f"archive SHA-256 mismatch: {actual} != {expected}")
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as bundle:
        root = destination.resolve()
        for member in bundle.infolist():
            target = (destination / member.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"unsafe archive member: {member.filename}")
        bundle.extractall(destination)
    (destination / ".archive-sha256").write_text(expected + "\n", encoding="utf-8")
    archive.unlink(missing_ok=True)


def load_registry() -> dict:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def select_benchmarks(registry: dict, groups: list[str], ids: list[str]) -> list[str]:
    selected: list[str] = []
    for group in groups:
        if group not in registry["groups"]:
            raise ValueError(f"unknown group: {group}")
        selected.extend(registry["groups"][group])
    selected.extend(ids)
    return list(dict.fromkeys(selected))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", action="append", default=[])
    parser.add_argument("--benchmark", action="append", default=[])
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    registry = load_registry()
    if args.list:
        print(json.dumps(registry, ensure_ascii=False, indent=2))
        return 0
    selected = select_benchmarks(registry, args.group, args.benchmark)
    if not selected:
        parser.error("select at least one --group or --benchmark")
    cache = ROOT / registry["cache_root"]
    cache.mkdir(parents=True, exist_ok=True)
    results = {}
    for benchmark_id in selected:
        if benchmark_id not in registry["benchmarks"]:
            raise ValueError(f"unknown benchmark: {benchmark_id}")
        item = registry["benchmarks"][benchmark_id]
        destination = cache / benchmark_id
        source = item["source"]
        if source["type"] == "git":
            fetch_git(destination, source)
            resolved = git_head(destination)
        elif source["type"] == "archive":
            extract_archive(destination, source)
            resolved = str(source["sha256"])
        else:
            raise ValueError(f"unsupported source type: {source['type']}")
        results[benchmark_id] = {
            "path": str(destination),
            "resolved": resolved,
            "license": item["license"],
            "homepage": item["homepage"],
        }
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
