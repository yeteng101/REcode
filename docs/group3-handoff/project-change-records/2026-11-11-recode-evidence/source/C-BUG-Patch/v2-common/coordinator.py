#!/usr/bin/env python3
"""Shared PR fact collection, specialty routing, and ContextBundle assembly."""
from __future__ import annotations

import re
from typing import Any

from context_manager import ContextManager
from first_group_client import FirstGroupClient


MEMORY_SIGNALS = {
    "malloc", "calloc", "realloc", "free", "alloc", "release", "destroy",
    "ownership", "lifetime", "buffer", "resource", "cancel", "cleanup",
}
CONCURRENCY_SIGNALS = {
    "pthread", "mutex", "rwlock", "lock", "unlock", "atomic", "thread",
    "race", "concurrent", "shared", "condition", "callback", "async",
}


class CoordinatorError(ValueError):
    pass


def _signal_text(review: dict[str, Any], call_facts: dict[str, Any]) -> str:
    pieces: list[str] = []
    diff = review.get("diff")
    if isinstance(diff, dict) and isinstance(diff.get("text"), str):
        pieces.append(diff["text"])
    for function in review.get("changed_functions", []):
        if isinstance(function, dict):
            pieces.extend(str(function.get(field, "")) for field in ("name", "qualified_name", "file"))
    pieces.append(str(call_facts.get("findings", [])))
    return " ".join(pieces).lower()


def route_specialties(
    review: dict[str, Any],
    call_facts: dict[str, Any],
    resource_facts: dict[str, Any] | None = None,
    sync_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    overrides = review.get("requested_routes")
    if isinstance(overrides, list) and overrides:
        routes = [item for item in overrides if item in {"memory", "concurrency"}]
        if not routes:
            raise CoordinatorError("requested_routes contains no supported specialty")
        return {
            "routes": list(dict.fromkeys(routes)),
            "confidence": 1.0,
            "reason": "explicit review requested_routes override",
            "run_both": len(set(routes)) > 1,
            "signals": {"memory": [], "concurrency": []},
        }

    # Prefer first-group structure over words in a diff. These are routing
    # signals only; neither response is a defect verdict.
    if resource_facts is not None or sync_facts is not None:
        resources = resource_facts.get("findings", []) if resource_facts else []
        syncs = sync_facts.get("findings", []) if sync_facts else []
        has_resource = bool(resources)
        has_concurrency = False
        has_lifetime_access = False
        for finding in syncs:
            if not isinstance(finding, dict):
                continue
            if finding.get("lock_order_edges") or finding.get("locks") or finding.get("observed_relations"):
                has_concurrency = True
            for obj in finding.get("shared_objects", []):
                accesses = obj.get("accesses", []) if isinstance(obj, dict) else []
                contexts = {
                    access.get("execution_context", {}).get("context_id")
                    for access in accesses if isinstance(access, dict)
                }
                contexts.discard(None)
                kinds = {access.get("kind") for access in accesses if isinstance(access, dict)}
                lifetime_access = bool(kinds.intersection({"delete", "free", "release"}))
                unknown_context = any(not access.get("execution_context", {}).get("context_id")
                                      for access in accesses if isinstance(access, dict))
                if len(contexts) > 1 or (len(accesses) > 1 and unknown_context):
                    has_concurrency = True
                    if lifetime_access:
                        has_lifetime_access = True
                elif len(contexts) == 1 and lifetime_access:
                    has_lifetime_access = True
        routes = []
        if has_resource or has_lifetime_access:
            routes.append("memory")
        if has_concurrency:
            routes.append("concurrency")
        # A missing domain response, or no positive structural signal, cannot
        # safely exclude either specialty.
        if resource_facts is None or sync_facts is None or not routes:
            routes = ["memory", "concurrency"]
        return {
            "routes": routes,
            "confidence": 0.8 if len(routes) == 1 else 0.5,
            "reason": "structured first-group resource and synchronization facts",
            "run_both": len(routes) > 1,
            "signals": {
                "memory": ["resource_flow"] if has_resource else (["lifetime_access"] if has_lifetime_access else []),
                "concurrency": ["sync_flow"] if has_concurrency else [],
            },
        }

    text = _signal_text(review, call_facts)
    words = set(re.findall(r"[a-z_][a-z0-9_]*", text))
    memory_hits = sorted(signal for signal in MEMORY_SIGNALS if signal in words or signal in text)
    concurrency_hits = sorted(signal for signal in CONCURRENCY_SIGNALS if signal in words or signal in text)
    memory_score = len(memory_hits)
    concurrency_score = len(concurrency_hits)

    if memory_score and concurrency_score:
        routes = ["memory", "concurrency"]
        confidence = min(0.95, 0.55 + 0.05 * (memory_score + concurrency_score))
        reason = "both memory-lifecycle and concurrency signals are present"
    elif memory_score >= 2:
        routes = ["memory"]
        confidence = min(0.95, 0.6 + 0.05 * memory_score)
        reason = "memory-lifecycle signals dominate"
    elif concurrency_score >= 2:
        routes = ["concurrency"]
        confidence = min(0.95, 0.6 + 0.05 * concurrency_score)
        reason = "concurrency signals dominate"
    else:
        routes = ["memory", "concurrency"]
        confidence = 0.35
        reason = "routing confidence is low; both specialties remain enabled"
    return {
        "routes": routes,
        "confidence": confidence,
        "reason": reason,
        "run_both": len(routes) > 1,
        "signals": {"memory": memory_hits, "concurrency": concurrency_hits},
    }


def build_first_group_request(review: dict[str, Any], action: str) -> dict[str, Any]:
    repository = review.get("repository")
    review_target = review.get("review_target")
    if not isinstance(repository, dict) or not isinstance(review_target, dict):
        raise CoordinatorError("review requires repository and review_target")
    targets = review.get("targets")
    if not isinstance(targets, list) or not targets:
        targets = []
        for function in review.get("changed_functions", []):
            if not isinstance(function, dict):
                continue
            targets.append(
                {
                    "target_id": f"changed-function-{len(targets) + 1}",
                    "type": "function",
                    "name": function.get("name"),
                    "qualified_name": function.get("qualified_name") or function.get("name"),
                    "location": {
                        "file": function.get("file"),
                        "line": function.get("start_line"),
                    },
                    "role": "changed_function",
                }
            )
    if not targets:
        raise CoordinatorError("review requires targets or changed_functions")
    normalized_targets: list[dict[str, Any]] = []
    for index, target in enumerate(targets):
        if not isinstance(target, dict):
            continue
        raw_location = target.get("location")
        location = dict(raw_location) if isinstance(raw_location, dict) else {
            "file": target.get("file"),
            "line": target.get("start_line"),
        }
        normalized_targets.append(
            {
                "target_id": target.get("target_id") or f"target-{index + 1}",
                "node_id": target.get("node_id"),
                "type": target.get("type") or target.get("kind") or "function",
                "name": target.get("name"),
                "qualified_name": target.get("qualified_name") or target.get("name"),
                "signature": target.get("signature"),
                "location": location,
                "role": target.get("role") or "changed_function",
            }
        )
    targets = normalized_targets
    request_repository = {
        key: value
        for key, value in repository.items()
        if key in {"provider", "name", "commit_sha", "commit_role", "base_sha", "head_sha", "graph_snapshot_id"}
    }
    request_repository["commit_role"] = "head"
    analysis: dict[str, Any] = {
        "direction": "both",
        "max_depth": review.get("max_depth", 12),
        "max_paths_per_target": review.get("max_paths_per_target", 50),
        "include_callbacks": True,
        "include_indirect_calls": True,
        "include_external_calls": False,
        "include_error_paths": True,
        "include_async_boundaries": True,
        "build_profile": repository.get("build_profile"),
    }
    request: dict[str, Any] = {
        "schema_version": "1.0",
        "task_id": f"{review_target['id']}:{action}",
        "action": action,
        "repository": request_repository,
        "targets": targets,
        "changed_files": review.get("diff", {}).get("changed_files", []),
        "analysis": analysis,
    }
    if action == "get_resource_flows":
        request["resource_families"] = review.get(
            "resource_families", ["heap", "file_descriptor", "async_request"]
        )
    elif action == "get_sync_flows":
        request["sync_families"] = review.get(
            "sync_families",
            ["mutex", "rwlock", "condition_variable", "atomic", "event_loop_serialization"],
        )
        analysis.update(
            {
                "include_thread_entries": True,
                "include_shared_accesses": True,
                "include_lock_order": True,
            }
        )
    return request


class ReviewCoordinator:
    def __init__(self, client: FirstGroupClient, context_manager: ContextManager) -> None:
        self.client = client
        self.context_manager = context_manager

    def prepare(self, review: dict[str, Any]) -> dict[str, Any]:
        call_request = build_first_group_request(review, "get_call_chains")
        call_facts = self.client.query(call_request)
        if call_facts.get("status") == "failed":
            raise CoordinatorError("first-group call-chain analysis failed")
        prefetched: dict[str, dict[str, Any]] = {}
        prefetch_errors: dict[str, str] = {}
        requested = review.get("requested_routes")
        for domain, action in (("memory", "get_resource_flows"), ("concurrency", "get_sync_flows")):
            if isinstance(requested, list) and requested and domain not in requested:
                continue
            try:
                facts = self.client.query(build_first_group_request(review, action))
                if facts.get("status") == "failed":
                    raise CoordinatorError(f"first-group {action} analysis failed")
                prefetched[domain] = facts
            except (RuntimeError, ValueError, KeyError) as exc:
                prefetch_errors[domain] = str(exc)
        routing = route_specialties(review, call_facts, prefetched.get("memory"), prefetched.get("concurrency"))
        bundles: dict[str, dict[str, Any]] = {}
        domain_fact_runs: dict[str, dict[str, Any]] = {}
        route_errors: list[dict[str, Any]] = []
        for domain in routing["routes"]:
            action = "get_resource_flows" if domain == "memory" else "get_sync_flows"
            try:
                if domain in prefetch_errors:
                    raise CoordinatorError(prefetch_errors[domain])
                domain_facts = prefetched[domain]
            except (RuntimeError, ValueError) as exc:
                domain_fact_runs[domain] = {
                    "action": action,
                    "status": "failed",
                }
                route_errors.append(
                    {
                        "domain": domain,
                        "action": action,
                        "message": str(exc),
                    }
                )
                continue
            domain_fact_runs[domain] = {
                "action": action,
                "run_id": domain_facts.get("run_id"),
                "status": domain_facts.get("status"),
            }
            try:
                bundles[domain] = self.context_manager.build_bundle(
                    review,
                    domain=domain,
                    call_facts=call_facts,
                    domain_facts=domain_facts,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                domain_fact_runs[domain]["status"] = "failed"
                route_errors.append(
                    {
                        "domain": domain,
                        "action": "build_context_bundle",
                        "message": str(exc),
                    }
                )
        status = "prepared"
        if route_errors:
            status = "partial" if bundles else "failed"
        return {
            "schema_version": "specialty-coordination.v1",
            "status": status,
            "review_target": review["review_target"],
            "routing": routing,
            "first_group": {
                "call_chain": {"run_id": call_facts.get("run_id"), "status": call_facts.get("status")},
                "specialty_facts": domain_fact_runs,
            },
            "route_errors": route_errors,
            "bundles": bundles,
        }
