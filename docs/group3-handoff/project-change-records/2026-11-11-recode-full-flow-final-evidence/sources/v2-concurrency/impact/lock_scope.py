"""Build a bounded lock/race blast radius from existing first-group responses.

This module does not parse C/C++ and does not change the first-group JSON
contract.  It joins ``get_call_chains`` and ``get_sync_flows`` facts, starts
from the first-group query roots that were derived from the PR diff, expands
through call edges, shared-object accesses, locks, and asynchronous boundaries,
and emits an auditable internal scope for the deterministic candidate builder.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict, deque
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

from prototype.shared_facts import merge_shared_findings


class ImpactScopeError(ValueError):
    """Raised when first-group facts cannot be safely joined."""


def _safe_path(value: str) -> bool:
    return bool(
        value
        and "\\" not in value
        and not value.startswith(("/", "//"))
        and ":" not in value.split("/", 1)[0]
        and ".." not in PurePosixPath(value).parts
    )


def _location(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    file_name = str(raw.get("file") or "")
    start = raw.get("start_line", raw.get("line"))
    end = raw.get("end_line", start)
    if not _safe_path(file_name) or not isinstance(start, int) or start < 1:
        return None
    if not isinstance(end, int) or end < start:
        return None
    return {"file": file_name, "start_line": start, "end_line": end}


def _overlaps(location: Any, changed_locations: Iterable[Mapping[str, Any]]) -> bool:
    normalized = _location(location)
    if normalized is None:
        return False
    for changed in changed_locations:
        changed_normalized = _location(changed)
        if changed_normalized is None or changed_normalized["file"] != normalized["file"]:
            continue
        if (
            normalized["start_line"] <= changed_normalized["end_line"]
            and changed_normalized["start_line"] <= normalized["end_line"]
        ):
            return True
    return False


def _context(response: Mapping[str, Any]) -> Mapping[str, Any]:
    value = response.get("analysis_context")
    if not isinstance(value, Mapping):
        raise ImpactScopeError("first-group response has no analysis_context")
    return value


def _binding(response: Mapping[str, Any]) -> dict[str, Any]:
    context = _context(response)
    repository = context.get("repository")
    if isinstance(repository, Mapping):
        name = repository.get("name") or repository.get("repository")
        base_sha = repository.get("base_sha")
        head_sha = repository.get("head_sha") or repository.get("commit_sha")
    else:
        name = repository
        base_sha = context.get("base_sha")
        head_sha = context.get("head_sha") or context.get("commit_sha")
    return {
        "name": str(name or ""),
        "base_sha": base_sha,
        "head_sha": str(head_sha or ""),
        "build_profile": str(context.get("build_profile") or ""),
        "graph_snapshot_id": context.get("graph_snapshot_id"),
    }


def _validate_binding(
    call_chains: Mapping[str, Any],
    sync_flows: Mapping[str, Any],
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    if call_chains.get("schema_version") != "1.0":
        raise ImpactScopeError("unsupported call-chain schema_version")
    if sync_flows.get("schema_version") != "1.0":
        raise ImpactScopeError("unsupported sync-flow schema_version")
    if call_chains.get("status") not in {"succeeded", "partial"}:
        raise ImpactScopeError("call-chain response did not succeed")
    if sync_flows.get("status") not in {"succeeded", "partial"}:
        raise ImpactScopeError("sync-flow response did not succeed")

    call_binding = _binding(call_chains)
    sync_binding = _binding(sync_flows)
    repository = snapshot.get("repository")
    if not isinstance(repository, Mapping):
        raise ImpactScopeError("repository snapshot has no repository binding")
    expected = {
        "name": str(repository.get("name") or ""),
        "base_sha": repository.get("base_sha"),
        "head_sha": str(repository.get("head_sha") or ""),
        "build_profile": str(repository.get("build_profile") or ""),
    }
    for label, actual in (("call-chain", call_binding), ("sync-flow", sync_binding)):
        for field in ("name", "base_sha", "head_sha", "build_profile"):
            if actual.get(field) and actual.get(field) != expected.get(field):
                raise ImpactScopeError(
                    "{} {} does not match repository snapshot".format(label, field)
                )
    call_graph = call_binding.get("graph_snapshot_id")
    sync_graph = sync_binding.get("graph_snapshot_id")
    if call_graph and sync_graph and call_graph != sync_graph:
        raise ImpactScopeError("first-group responses use different graph snapshots")
    expected["graph_snapshot_id"] = sync_graph or call_graph
    return expected


def _evidence_index(*responses: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for response in responses:
        for item in response.get("evidence") or []:
            if not isinstance(item, Mapping) or not item.get("id"):
                raise ImpactScopeError("first-group evidence must have a non-empty id")
            evidence_id = str(item["id"])
            copied = copy.deepcopy(dict(item))
            previous = result.get(evidence_id)
            if previous is not None and previous != copied:
                raise ImpactScopeError("conflicting evidence id: {}".format(evidence_id))
            snippet = copied.get("snippet")
            if snippet is None and isinstance(copied.get("location"), Mapping):
                snippet = copied["location"].get("snippet")
            snippet_hash = copied.get("snippet_sha256")
            if snippet_hash is None and isinstance(copied.get("location"), Mapping):
                snippet_hash = copied["location"].get("snippet_sha256")
            if snippet is not None:
                if not isinstance(snippet, str):
                    raise ImpactScopeError("evidence snippet must be a string")
                actual_hash = hashlib.sha256(snippet.encode("utf-8")).hexdigest()
                if snippet_hash != actual_hash:
                    raise ImpactScopeError("evidence {} has an invalid snippet hash".format(evidence_id))
            result[evidence_id] = copied
    return result


def _response_digest(response: Mapping[str, Any]) -> str:
    preserved = getattr(response, "source_sha256", None)
    if isinstance(preserved, str) and len(preserved) == 64:
        return preserved
    encoded = json.dumps(
        response, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_ids(value: Any) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            references = node.get("evidence_ids")
            if isinstance(references, list):
                found.extend(str(item) for item in references if item)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return list(dict.fromkeys(found))


def _coverage_component(response: Mapping[str, Any], kind: str) -> dict[str, Any]:
    raw = response.get("coverage")
    coverage = raw if isinstance(raw, Mapping) else {}
    complete = response.get("status") == "succeeded" and coverage.get("complete") is True
    unresolved: list[str] = []
    if coverage.get("reason"):
        unresolved.append("{}:{}".format(kind, coverage["reason"]))
    for reason in coverage.get("stopped_because") or []:
        unresolved.append("{}:{}".format(kind, reason))
    target_resolution = response.get("target_resolution")
    if isinstance(target_resolution, Mapping):
        for target in target_resolution.get("unresolved_targets") or []:
            unresolved.append("{}:unresolved_target:{}".format(kind, target))
    for finding in response.get("findings") or []:
        if isinstance(finding, Mapping) and finding.get("complete") is False:
            unresolved.append(
                "{}:{}".format(kind, finding.get("termination_reason") or "incomplete_finding")
            )
    if not complete and not unresolved:
        unresolved.append("{}:partial".format(kind))
    return {
        "complete": complete,
        "status": response.get("status"),
        "unresolved": list(dict.fromkeys(unresolved)),
    }


def _lock_ids(access: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for key in ("must_lockset", "may_lockset", "held_locks"):
        for item in access.get(key) or []:
            if isinstance(item, Mapping):
                lock_id = item.get("lock_id") or item.get("id")
            else:
                lock_id = item
            if lock_id:
                result.add(str(lock_id))
    return result


def build_lock_scope(
    snapshot: Mapping[str, Any],
    call_chains: Mapping[str, Any],
    sync_flows: Mapping[str, Any],
    *,
    max_depth: int = 12,
) -> dict[str, Any]:
    """Join existing first-group responses into a deterministic impact scope."""

    if max_depth < 0:
        raise ImpactScopeError("max_depth must be non-negative")
    repository = _validate_binding(call_chains, sync_flows, snapshot)
    evidence = _evidence_index(call_chains, sync_flows)
    change_context = sync_flows.get("change_context")
    if not isinstance(change_context, Mapping):
        raise ImpactScopeError("sync-flow must be enriched with Git change_context first")
    changed_locations = list(change_context.get("changed_locations") or [])

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    roots: set[str] = set()
    adjacency: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for finding in call_chains.get("findings") or []:
        if not isinstance(finding, Mapping) or finding.get("kind") != "call_chain":
            continue
        root = finding.get("root_node_id")
        if root:
            roots.add(str(root))
        for node in finding.get("nodes") or []:
            if not isinstance(node, Mapping) or not node.get("node_id"):
                continue
            node_id = str(node["node_id"])
            copied = copy.deepcopy(dict(node))
            if node_id in nodes and nodes[node_id] != copied:
                raise ImpactScopeError("conflicting call node id: {}".format(node_id))
            nodes[node_id] = copied
        for edge in finding.get("edges") or []:
            if not isinstance(edge, Mapping):
                continue
            source = str(edge.get("source_node_id") or "")
            target = str(edge.get("target_node_id") or "")
            if not source or not target:
                continue
            edge_id = str(edge.get("edge_id") or "{}->{}".format(source, target))
            copied = copy.deepcopy(dict(edge))
            edges[edge_id] = copied
            adjacency[source].append((target, edge_id))
            adjacency[target].append((source, edge_id))

    reasons: dict[str, set[str]] = defaultdict(set)
    queue: deque[tuple[str, int]] = deque()
    distance: dict[str, int] = {}
    depth_frontier: set[tuple[str, str, str]] = set()
    for node_id in sorted(roots):
        if node_id in nodes:
            distance[node_id] = 0
            reasons[node_id].add("first_group_query_root")
            queue.append((node_id, 0))
    for node_id, node in nodes.items():
        if _overlaps(node.get("location"), changed_locations):
            reasons[node_id].add("overlaps_pr_change")
            if node_id not in distance:
                distance[node_id] = 0
                queue.append((node_id, 0))

    def expand_calls() -> None:
        while queue:
            node_id, depth = queue.popleft()
            if depth >= max_depth:
                for neighbor, edge_id in adjacency.get(node_id, []):
                    depth_frontier.add((node_id, neighbor, edge_id))
                continue
            for neighbor, _edge_id in adjacency.get(node_id, []):
                candidate_depth = depth + 1
                if neighbor not in distance or candidate_depth < distance[neighbor]:
                    distance[neighbor] = candidate_depth
                    reasons[neighbor].add("connected_call_path")
                    queue.append((neighbor, candidate_depth))

    expand_calls()

    selected_objects: dict[str, dict[str, Any]] = {}
    selected_locks: dict[str, dict[str, Any]] = {}
    selected_boundaries: dict[str, dict[str, Any]] = {}
    selected_conditions: dict[str, dict[str, Any]] = {}
    selected_lock_orders: dict[str, dict[str, Any]] = {}
    access_functions: set[str] = set()
    context_ids: set[str] = set()
    try:
        joined_findings, _ = merge_shared_findings(sync_flows.get('findings') or [])
    except ValueError as exc:
        raise ImpactScopeError(str(exc)) from exc

    while True:
        before = (len(distance), len(selected_objects), len(access_functions), len(context_ids),
                  len(selected_locks), len(selected_lock_orders))
        selected_names = {
            str(nodes[node_id].get("name") or "") for node_id in distance if node_id in nodes
        }
        selected_names.update(access_functions)
        for finding in joined_findings:
            if not isinstance(finding, Mapping) or finding.get("kind") != "sync_flow":
                continue
            for shared_object in finding.get("shared_objects") or []:
                if not isinstance(shared_object, Mapping) or not shared_object.get("object_id"):
                    continue
                accesses = [item for item in shared_object.get("accesses") or [] if isinstance(item, Mapping)]
                relevant = any(
                    str(access.get("function") or "") in selected_names
                    or _overlaps(access.get("location"), changed_locations)
                    for access in accesses
                )
                if not relevant:
                    continue
                object_id = str(shared_object["object_id"])
                selected_objects[object_id] = copy.deepcopy(dict(shared_object))
                for access in accesses:
                    function_name = str(access.get("function") or "")
                    if function_name:
                        access_functions.add(function_name)
                    execution_context = access.get("execution_context")
                    if isinstance(execution_context, Mapping) and execution_context.get("context_id"):
                        context_ids.add(str(execution_context["context_id"]))
            referenced_locks: set[str] = set()
            for shared_object in selected_objects.values():
                for access in shared_object.get("accesses") or []:
                    if isinstance(access, Mapping):
                        referenced_locks.update(_lock_ids(access))
            for lock in finding.get("locks") or []:
                if not isinstance(lock, Mapping) or not lock.get("lock_id"):
                    continue
                lock_id = str(lock["lock_id"])
                operation_functions = {
                    str(operation.get("function") or "")
                    for operation in lock.get("operations") or []
                    if isinstance(operation, Mapping)
                }
                if lock_id in referenced_locks or operation_functions.intersection(selected_names):
                    selected_locks[lock_id] = copy.deepcopy(dict(lock))
            for boundary in finding.get("callbacks_and_async_boundaries") or []:
                if not isinstance(boundary, Mapping):
                    continue
                source = str(boundary.get("source_context") or "")
                target = str(boundary.get("target_context") or "")
                if source in context_ids or target in context_ids:
                    boundary_id = str(
                        boundary.get("boundary_id") or "{}:{}:{}".format(source, target, boundary.get("kind"))
                    )
                    selected_boundaries[boundary_id] = copy.deepcopy(dict(boundary))
                    if source:
                        context_ids.add(source)
                    if target:
                        context_ids.add(target)
            for condition in finding.get("condition_variables") or []:
                if not isinstance(condition, Mapping):
                    continue
                predicate_object = str(condition.get("predicate_object_id") or "")
                condition_functions = {
                    str(node.get("function") or "")
                    for node in [condition.get("wait"), *(condition.get("signals") or [])]
                    if isinstance(node, Mapping)
                }
                if predicate_object in selected_objects or condition_functions.intersection(selected_names):
                    condition_id = str(
                        condition.get("condition_id")
                        or "condition:{}".format(len(selected_conditions) + 1)
                    )
                    selected_conditions[condition_id] = copy.deepcopy(dict(condition))
            for order in finding.get("lock_order_edges") or []:
                if not isinstance(order, Mapping):
                    continue
                from_lock = str(order.get("from_lock_id") or "")
                to_lock = str(order.get("to_lock_id") or "")
                if (
                    from_lock in selected_locks
                    or to_lock in selected_locks
                    or _overlaps(order.get("observed_at"), changed_locations)
                ):
                    edge_id = str(
                        order.get("edge_id")
                        or "lock-order:{}:{}".format(from_lock, to_lock)
                    )
                    selected_lock_orders[edge_id] = copy.deepcopy(dict(order))
                    # Follow lock identities across an order path even without a
                    # shared-data object. Do not invent indirect-call targets.
                    for lock in finding.get("locks") or []:
                        if isinstance(lock, Mapping) and lock.get("lock_id") in {from_lock, to_lock}:
                            selected_locks[str(lock["lock_id"])] = copy.deepcopy(dict(lock))

        for node_id, node in nodes.items():
            if str(node.get("name") or "") in access_functions and node_id not in distance:
                distance[node_id] = 0
                reasons[node_id].add("accesses_selected_shared_object")
                queue.append((node_id, 0))
        expand_calls()
        after = (len(distance), len(selected_objects), len(access_functions), len(context_ids),
                 len(selected_locks), len(selected_lock_orders))
        if after == before:
            break

    selected_edges = {
        edge_id: edge
        for edge_id, edge in edges.items()
        if str(edge.get("source_node_id")) in distance
        and str(edge.get("target_node_id")) in distance
    }
    functions = []
    for node_id in sorted(distance, key=lambda item: (distance[item], item)):
        node = nodes[node_id]
        functions.append(
            {
                "node_id": node_id,
                "name": str(node.get("name") or ""),
                "qualified_name": node.get("qualified_name"),
                "location": copy.deepcopy(node.get("location")),
                "distance": distance[node_id],
                "reasons": sorted(reasons[node_id]),
                "evidence_ids": list(dict.fromkeys(node.get("evidence_ids") or [])),
            }
        )
    known_function_names = {item["name"] for item in functions}
    for function_name in sorted(access_functions.difference(known_function_names)):
        functions.append(
            {
                "node_id": None,
                "name": function_name,
                "qualified_name": None,
                "location": None,
                "distance": None,
                "reasons": ["accesses_selected_shared_object"],
                "evidence_ids": [],
            }
        )

    selected_payload = {
        "functions": functions,
        "call_edges": list(selected_edges.values()),
        "shared_objects": list(selected_objects.values()),
        "locks": list(selected_locks.values()),
        "async_boundaries": list(selected_boundaries.values()),
        "condition_variables": list(selected_conditions.values()),
        "lock_order_edges": list(selected_lock_orders.values()),
    }
    evidence_ids = _evidence_ids(selected_payload)
    evidence_ids.extend(str(item) for item in change_context.get("diff_evidence_ids") or [])
    evidence_ids = list(dict.fromkeys(evidence_ids))
    missing_evidence = [item for item in evidence_ids if item not in evidence]
    if missing_evidence:
        raise ImpactScopeError(
            "impact scope references missing evidence: {}".format(", ".join(missing_evidence))
        )

    call_coverage = _coverage_component(call_chains, "call_chains")
    sync_coverage = _coverage_component(sync_flows, "sync_flows")
    unresolved = list(dict.fromkeys(call_coverage["unresolved"] + sync_coverage["unresolved"]))
    cut_edges = sorted(edge for _, target, edge in depth_frontier if target not in distance)
    if cut_edges:
        unresolved.append("impact:local_call_depth_limit")
    has_lock_scope = bool(selected_locks and selected_lock_orders)
    if not selected_objects and not has_lock_scope:
        unresolved.append("impact:no_shared_object_reached_from_change_roots")
    complete = (call_coverage["complete"] and sync_coverage["complete"]
                and bool(selected_objects or has_lock_scope) and not cut_edges)
    if not complete and not unresolved:
        unresolved.append("impact:incomplete")

    return {
        "schema_version": "lock-race-impact-scope/0.1",
        "repository": repository,
        "source_runs": {
            "call_chains": {
                "run_id": call_chains.get("run_id"),
                "response_sha256": _response_digest(call_chains),
            },
            "sync_flows": {
                "run_id": sync_flows.get("run_id"),
                "response_sha256": _response_digest(sync_flows),
            },
        },
        "change_context": copy.deepcopy(dict(change_context)),
        "roots": sorted(roots),
        **selected_payload,
        "context_ids": sorted(context_ids),
        "evidence_ids": evidence_ids,
        "coverage": {
            "complete": complete,
            "reason": None if complete else unresolved[0],
            "unresolved": unresolved,
            "local_limits": {"max_depth": max_depth, "cut_call_edge_count": len(cut_edges),
                             "cut_call_edge_ids": cut_edges[:128],
                             "cut_edge_ids_sha256": hashlib.sha256(json.dumps(cut_edges).encode()).hexdigest()},
            "components": {
                "call_chains": call_coverage,
                "sync_flows": sync_coverage,
            },
        },
        "stats": {
            "root_count": len(roots),
            "function_count": len(functions),
            "call_edge_count": len(selected_edges),
            "shared_object_count": len(selected_objects),
            "shared_access_count": sum(
                len(item.get("accesses") or []) for item in selected_objects.values()
            ),
            "lock_count": len(selected_locks),
            "async_boundary_count": len(selected_boundaries),
            "condition_variable_count": len(selected_conditions),
            "lock_order_edge_count": len(selected_lock_orders),
        },
    }


def apply_lock_scope(
    sync_flows: Mapping[str, Any],
    call_chains: Mapping[str, Any],
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a first-group-shaped sync response restricted to the impact scope."""

    result = copy.deepcopy(dict(sync_flows))
    selected_object_ids = {
        str(item.get("object_id"))
        for item in scope.get("shared_objects") or []
        if isinstance(item, Mapping) and item.get("object_id")
    }
    selected_lock_ids = {
        str(item.get("lock_id"))
        for item in scope.get("locks") or []
        if isinstance(item, Mapping) and item.get("lock_id")
    }
    selected_boundary_ids = {
        str(item.get("boundary_id"))
        for item in scope.get("async_boundaries") or []
        if isinstance(item, Mapping) and item.get("boundary_id")
    }
    selected_condition_ids = {
        str(item.get("condition_id"))
        for item in scope.get("condition_variables") or []
        if isinstance(item, Mapping) and item.get("condition_id")
    }
    selected_order_ids = {
        str(item.get("edge_id"))
        for item in scope.get("lock_order_edges") or []
        if isinstance(item, Mapping) and item.get("edge_id")
    }
    filtered_findings: list[dict[str, Any]] = []
    for raw_finding in result.get("findings") or []:
        if not isinstance(raw_finding, Mapping) or raw_finding.get("kind") != "sync_flow":
            continue
        finding = copy.deepcopy(dict(raw_finding))
        finding["shared_objects"] = [
            item
            for item in finding.get("shared_objects") or []
            if isinstance(item, Mapping) and str(item.get("object_id")) in selected_object_ids
        ]
        finding["locks"] = [
            item
            for item in finding.get("locks") or []
            if isinstance(item, Mapping) and str(item.get("lock_id")) in selected_lock_ids
        ]
        finding["callbacks_and_async_boundaries"] = [
            item
            for item in finding.get("callbacks_and_async_boundaries") or []
            if isinstance(item, Mapping)
            and (
                not item.get("boundary_id")
                or str(item.get("boundary_id")) in selected_boundary_ids
            )
        ]
        finding["condition_variables"] = [
            item
            for item in finding.get("condition_variables") or []
            if isinstance(item, Mapping)
            and (
                not item.get("condition_id")
                or str(item.get("condition_id")) in selected_condition_ids
            )
        ]
        finding["lock_order_edges"] = [
            item
            for item in finding.get("lock_order_edges") or []
            if isinstance(item, Mapping)
            and (not item.get("edge_id") or str(item.get("edge_id")) in selected_order_ids)
        ]
        # A selected mutex/rwlock may be declared in its own flow with no
        # access or order edge. Dropping it loses the kind needed for filtering.
        if not finding["shared_objects"] and not finding["locks"]:
            continue
        finding["impact_evidence_ids"] = list(scope.get("evidence_ids") or [])
        filtered_findings.append(finding)
    result["findings"] = filtered_findings

    merged_evidence = _evidence_index(result, call_chains)
    result["evidence"] = [merged_evidence[item] for item in sorted(merged_evidence)]
    scope_coverage = scope.get("coverage") or {}
    result["coverage"] = {
        "complete": scope_coverage.get("complete") is True,
        "reason": scope_coverage.get("reason"),
        "stopped_because": list(scope_coverage.get("unresolved") or []),
        "components": copy.deepcopy(scope_coverage.get("components") or {}),
    }
    warnings = list(dict.fromkeys(
        [str(item) for item in (sync_flows.get("warnings") or [])]
        + [str(item) for item in (call_chains.get("warnings") or [])]
    ))
    if scope_coverage.get("complete") is not True:
        warnings.append("Lock/race impact scope is incomplete; candidates require more context.")
    result["warnings"] = list(dict.fromkeys(warnings))
    result["lock_impact_scope"] = copy.deepcopy(dict(scope))
    # The host snapshot supplies PR base metadata even when the producer query
    # was head-only. Existing nonempty bindings were checked while building scope.
    repository = result.get("analysis_context", {}).get("repository")
    if isinstance(repository, dict):
        for field in ("base_sha", "head_sha"):
            if not repository.get(field) and (scope.get("repository") or {}).get(field):
                repository[field] = scope["repository"][field]
    return result


def canonical_digest(value: Mapping[str, Any]) -> str:
    """Return a stable digest for an internal impact artifact."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
