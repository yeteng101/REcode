#!/usr/bin/env python3
"""CLI that prepares specialty ContextBundles through the shared upper layer."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from context_manager import ContextManager
from coordinator import ReviewCoordinator
from first_group_client import FirstGroupClient
from review_input import require_review_input


ROOT = Path(__file__).resolve().parent


def _load_review(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("review request must be a JSON object")
    repository = value.get("repository")
    if not isinstance(repository, dict):
        raise ValueError("review.repository must be an object")
    repository_root = repository.get("root")
    if not isinstance(repository_root, str):
        raise ValueError("review.repository.root must be a string")
    root_path = Path(repository_root)
    if not root_path.is_absolute():
        root_path = (resolved.parent / root_path).resolve()
    repository["root"] = str(root_path)
    diff = value.get("diff")
    if not isinstance(diff, dict):
        raise ValueError("review.diff must be an object")
    diff_path = diff.get("path")
    if not isinstance(diff.get("text"), str) and isinstance(diff_path, str):
        artifact = Path(diff_path)
        if not artifact.is_absolute():
            artifact = (resolved.parent / artifact).resolve()
        diff["text"] = artifact.read_text(encoding="utf-8")
    return require_review_input(value)


def _load_registry(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("specialties"), dict):
        raise ValueError("specialty registry must contain a specialties object")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare routed specialty ContextBundles")
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--first-group-url", default="http://127.0.0.1:8765")
    parser.add_argument("--first-group-provider", choices=["contract", "recode"], default="contract")
    parser.add_argument("--recode-artifacts", type=Path, help="Use a fixed REcode manifest/export directory")
    parser.add_argument("--recode-job-id", help="Download an existing REcode job instead of submitting")
    parser.add_argument("--recode-job-timeout", type=float, default=1800)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--specialty-registry", type=Path, default=ROOT / "specialty_registry.json")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-context-chars", type=int, default=240_000)
    parser.add_argument("--max-file-chars", type=int, default=80_000)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    try:
        review = _load_review(args.review)
        registry_path = args.specialty_registry.resolve()
        registry = _load_registry(registry_path)
        if args.recode_artifacts and args.recode_job_id:
            raise ValueError("choose REcode artifacts or an existing job")
        if args.first_group_provider == "contract" and (args.recode_artifacts or args.recode_job_id):
            raise ValueError("REcode options require --first-group-provider recode")
        if args.first_group_provider == "recode":
            from recode_artifacts import validate_source_checkout
            validate_source_checkout(review)
        if args.recode_artifacts:
            from recode_artifacts import RecodeArtifactsClient
            client = RecodeArtifactsClient(args.recode_artifacts)
        elif args.first_group_provider == "recode":
            from recode_client import RecodeClient
            client = RecodeClient(args.first_group_url, cache_directory=output_dir / "recode-artifacts",
                                  timeout=args.timeout, job_timeout=args.recode_job_timeout, job_id=args.recode_job_id)
        else:
            client = FirstGroupClient(args.first_group_url, timeout=args.timeout)
        coordinator = ReviewCoordinator(
            client,
            ContextManager(max_chars=args.max_context_chars, max_file_chars=args.max_file_chars),
        )
        coordination = coordinator.prepare(review)
        output_dir.mkdir(parents=True, exist_ok=True)
        bundle_paths: dict[str, str] = {}
        handoffs: list[dict[str, Any]] = []
        for domain, bundle in coordination.pop("bundles").items():
            bundle_path = output_dir / f"{domain}-context-bundle.json"
            bundle_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            bundle_paths[domain] = str(bundle_path)
            specialty = registry["specialties"].get(domain, {})
            entrypoint = specialty.get("entrypoint") if isinstance(specialty, dict) else None
            entrypoint_path = Path(entrypoint) if isinstance(entrypoint, str) else None
            if entrypoint_path is not None and not entrypoint_path.is_absolute():
                entrypoint_path = (registry_path.parent / entrypoint_path).resolve()
            specialty_ready = (
                isinstance(specialty, dict)
                and specialty.get("status", "available") == "available"
                and entrypoint_path is not None
                and entrypoint_path.is_file()
                and isinstance(specialty.get("invocation"), str)
            )
            handoffs.append(
                {
                    "domain": domain,
                    "status": "prepared" if specialty_ready else "not_implemented",
                    "context_bundle": str(bundle_path),
                    "entrypoint": str(entrypoint_path) if entrypoint_path is not None else entrypoint,
                    "invocation": specialty.get("invocation"),
                }
            )
            if not specialty_ready:
                coordination.setdefault("route_errors", []).append(
                    {
                        "domain": domain,
                        "action": "specialty_handoff",
                        "message": "specialty entrypoint is not implemented or not registered",
                    }
                )
                if coordination.get("status") == "prepared":
                    coordination["status"] = "partial"
        report = {**coordination, "bundle_paths": bundle_paths, "handoffs": handoffs}
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
        report = {
            "schema_version": "specialty-coordination.v1",
            "status": "failed",
            "errors": [{"path": "$", "message": str(exc)}],
        }
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "coordination-report.json"
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    report_path.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    if report.get("status") == "prepared":
        return 0
    if report.get("status") == "partial":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
