"""Build bounded race candidates from a first-group sync_flow response.

This is a dependency-free contract prototype, not a replacement for the final
AST/CFG, path-sensitive lockset, or execution-context analyses.  It exists so
the member-B pipeline can be exercised locally before the real first-group
backend is available.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence

from shared_facts import merge_shared_findings


WRITE_KINDS = {"write", "delete", "free", "invalidate"}
LIFETIME_KINDS = {"delete", "free", "invalidate"}


class LoadedResponse(dict):
    """JSON object that retains the exact input-byte digest for provenance."""

    def __init__(self, value: Dict[str, Any], source_sha256: str) -> None:
        super().__init__(value)
        self.source_sha256 = source_sha256


def load_response(path: str | Path) -> Dict[str, Any]:
    with open(path, "rb") as handle:
        raw = handle.read()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("sync_flow input must be a JSON object")
    return LoadedResponse(value, hashlib.sha256(raw).hexdigest())


def _safe_relative_path(value: str) -> bool:
    if not value or "\\" in value or "\x00" in value:
        return False
    if value.startswith("/") or value.startswith("//"):
        return False
    if re.match(r"^[A-Za-z]:", value):
        return False
    return ".." not in PurePosixPath(value).parts


def _location(raw: Dict[str, Any]) -> Dict[str, Any]:
    file_name = str(raw.get("file") or "")
    if not _safe_relative_path(file_name):
        raise ValueError("unsafe repository-relative path: {!r}".format(file_name))
    start = raw.get("start_line", raw.get("line"))
    end = raw.get("end_line", start)
    if not isinstance(start, int) or start < 1:
        raise ValueError("location.start_line must be a positive integer")
    if not isinstance(end, int) or end < start:
        raise ValueError("location.end_line must be >= start_line")
    return {"file": file_name, "start_line": start, "end_line": end}


def _validated_evidence(items: Any) -> List[Dict[str, Any]]:
    if not isinstance(items, list):
        raise ValueError("evidence must be an array")
    result: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("each evidence record must be an object")
        evidence_id = str(item.get("id") or "")
        if not evidence_id or evidence_id in seen:
            raise ValueError("evidence IDs must be non-empty and unique")
        seen.add(evidence_id)
        if not item.get("kind"):
            raise ValueError("evidence {!r} has no kind".format(evidence_id))
        location = item.get("location")
        if not isinstance(location, dict):
            raise ValueError("evidence {!r} has no location".format(evidence_id))
        file_name = str(location.get("file") or "")
        if not _safe_relative_path(file_name):
            raise ValueError("unsafe evidence repository-relative path: {!r}".format(file_name))
        line = location.get("line", location.get("start_line"))
        if not isinstance(line, int) or line < 1:
            raise ValueError("evidence location.line must be a positive integer")
        snippet = item.get("snippet")
        if snippet is not None:
            if not isinstance(snippet, str):
                raise ValueError("evidence snippet must be a string")
            expected_hash = hashlib.sha256(snippet.encode("utf-8")).hexdigest()
            if item.get("snippet_sha256") != expected_hash:
                raise ValueError("evidence {!r} has an invalid snippet hash".format(evidence_id))
        result.append(dict(item))
    return result


def _lock_id(item: Any) -> Optional[str]:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        value = item.get("lock_id") or item.get("id")
        return str(value) if value else None
    return None


def _lock_mode(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("mode") or "unknown")
    return "unknown"


def _lock_map(items: Sequence[Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for item in items:
        identity = _lock_id(item)
        if identity:
            mode = _lock_mode(item)
            result[identity] = mode if identity not in result or result[identity] == mode else 'conflicting'
    return result


def _normalized_lockset(access: Dict[str, Any], key: str) -> List[Any]:
    value = access.get(key)
    if isinstance(value, list):
        return value

    # The older first-group draft exposed only held_locks.  It is unsafe to
    # promote those locks to must-locks unless the producer states the
    # semantics explicitly.
    if key == "may_lockset" and isinstance(access.get("held_locks"), list):
        return list(access["held_locks"])
    if (
        key == "must_lockset"
        and access.get("lockset_semantics") == "must"
        and isinstance(access.get("held_locks"), list)
    ):
        return list(access["held_locks"])
    return []


def _normalize_access(access: Dict[str, Any]) -> Dict[str, Any]:
    evidence_ids = list(dict.fromkeys(access.get("evidence_ids") or []))
    if not evidence_ids:
        raise ValueError("access {!r} has no evidence_ids".format(access.get("access_id")))
    kind = str(access.get("kind") or "")
    if kind not in {"read", "write", "delete", "free", "invalidate"}:
        raise ValueError("unsupported access kind: {!r}".format(kind))
    result = {
        "access_id": str(access.get("access_id") or ""),
        "kind": kind,
        "function": str(access.get("function") or ""),
        "location": _location(access.get("location") or {}),
        "execution_context": dict(access.get("execution_context") or {}),
        "must_lockset": _normalized_lockset(access, "must_lockset"),
        "may_lockset": _normalized_lockset(access, "may_lockset"),
        "other_synchronization": list(access.get("other_synchronization") or []),
        "confidence": float(access.get("confidence", 1.0)),
        "evidence_ids": evidence_ids,
    }
    if access.get('source_finding_ids'):
        result['source_finding_ids'] = sorted(set(access['source_finding_ids']))
    return result


def _context_overlap_analysis(a: Dict[str, Any], b: Dict[str, Any]) -> tuple[Optional[bool], List[str]]:
    context_a = a.get("execution_context") or {}
    context_b = b.get("execution_context") or {}

    id_a = context_a.get("context_id")
    id_b = context_b.get("context_id")
    non_reentrant = context_a.get('reentrant') is False and context_b.get('reentrant') is False
    loop_a = context_a.get("event_loop")
    loop_b = context_b.get("event_loop")
    explicit_a = set(context_a.get("may_run_concurrently_with") or [])
    explicit_b = set(context_b.get("may_run_concurrently_with") or [])
    excluded_a = set(context_a.get("cannot_run_concurrently_with") or [])
    excluded_b = set(context_b.get("cannot_run_concurrently_with") or [])
    thread_a = context_a.get("thread")
    thread_b = context_b.get("thread")
    known_threads = bool(thread_a and thread_b
        and not str(thread_a).lower().startswith(("unknown", "unresolved"))
        and not str(thread_b).lower().startswith(("unknown", "unresolved")))
    positive = bool((id_b and id_b in explicit_a) or (id_a and id_a in explicit_b)
                    or (known_threads and thread_a != thread_b))
    # A thread label may name a worker family rather than one physical thread.
    negative = (non_reentrant and bool((id_a and id_a == id_b) or (loop_a and loop_a == loop_b))) or bool(
        (id_b and id_b in excluded_a) or (id_a and id_a in excluded_b))
    if positive and negative:
        return None, ['contradictory_concurrency_facts']
    if negative:
        return False, []
    gaps = []
    serialization_a, serialization_b = context_a.get('serialized_by'), context_b.get('serialized_by')
    if serialization_a and serialization_a == serialization_b:
        gaps.append('unproven_serialization_hint')
    if positive:
        return True, gaps
    return None, gaps + ['concurrency_relation_unknown']


def _contexts_may_overlap(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[bool]:
    return _context_overlap_analysis(a, b)[0]


def _rwlock_mode_protects(access_kind: str, mode: str) -> bool:
    if access_kind in WRITE_KINDS:
        return mode in {"write", "exclusive"}
    return mode in {"read", "write", "shared", "exclusive"}


def _common_effective_locks(
    access_a: Dict[str, Any],
    access_b: Dict[str, Any],
    lock_kinds: Dict[str, str],
) -> List[str]:
    map_a = _lock_map(access_a.get("must_lockset") or [])
    map_b = _lock_map(access_b.get("must_lockset") or [])
    common = sorted(set(map_a).intersection(map_b))
    effective: List[str] = []
    for lock_id in common:
        kind = lock_kinds.get(lock_id)
        if kind == "rwlock":
            if not _rwlock_mode_protects(access_a["kind"], map_a[lock_id]):
                continue
            if not _rwlock_mode_protects(access_b["kind"], map_b[lock_id]):
                continue
        elif kind in {'mutex', 'recursive_mutex', 'spinlock'}:
            if any(mode not in {'unknown', 'exclusive', 'write'} for mode in (map_a[lock_id], map_b[lock_id])):
                continue
        else:
            continue
        effective.append(lock_id)
    return effective


def _shared_other_synchronization(a: Dict[str, Any], b: Dict[str, Any]) -> List[str]:
    def key(item):
        return item if isinstance(item, str) else json.dumps(item, sort_keys=True)
    sync_a = set(key(item) for item in a.get("other_synchronization") or [])
    sync_b = set(key(item) for item in b.get("other_synchronization") or [])
    return sorted(sync_a.intersection(sync_b))


def _protection_gaps(a, b, lock_kinds):
    """Hints are preserved, but are not an access-pair happens-before proof."""
    gaps = []
    if a.get('other_synchronization') or b.get('other_synchronization'):
        gaps.append('unproven_other_synchronization')
    map_a, map_b = _lock_map(a.get('must_lockset') or []), _lock_map(b.get('must_lockset') or [])
    common = set(map_a) & set(map_b)
    for identity in sorted(common):
        kind = lock_kinds.get(identity)
        modes = (map_a[identity], map_b[identity])
        if kind not in {'rwlock', 'mutex', 'recursive_mutex', 'spinlock'}:
            gaps.append('unknown_common_lock_kind:' + identity)
        elif 'conflicting' in modes:
            gaps.append('conflicting_common_lock_mode:' + identity)
        elif kind == 'rwlock' and any(mode not in {'read', 'shared', 'write', 'exclusive'} for mode in modes):
            gaps.append('unknown_common_lock_mode:' + identity)
        elif kind != 'rwlock' and any(mode not in {'unknown', 'exclusive', 'write'} for mode in modes):
            gaps.append('inconsistent_common_lock_mode:' + identity)
    return gaps


def _coverage_with_gaps(coverage, gaps):
    result = copy.deepcopy(coverage)
    if gaps:
        result['complete'] = False
        result['reason'] = result.get('reason') or gaps[0]
        result['unresolved'] = sorted(set(result.get('unresolved') or []) | set(gaps))
    return result


def _coverage(response: Dict[str, Any]) -> Dict[str, Any]:
    raw = response.get("coverage") or {}
    complete = bool(raw.get("complete")) and response.get("status") == "succeeded"
    reason = raw.get("reason")
    stopped = list(raw.get("stopped_because") or [])
    unresolved = []
    if reason:
        unresolved.append(str(reason))
    unresolved.extend(str(item) for item in stopped)
    if response.get("status") == "partial" and not unresolved:
        unresolved.append("first_group_partial")
    return {
        "complete": complete,
        "reason": None if complete else (str(reason) if reason else "incomplete_sync_flow"),
        "unresolved": list(dict.fromkeys(unresolved)),
    }


def _repository(response: Dict[str, Any]) -> Dict[str, Any]:
    context = response.get("analysis_context") or {}
    repository = context.get("repository")
    if isinstance(repository, dict):
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


def _provenance(response: Dict[str, Any]) -> Dict[str, Any]:
    context = response.get("analysis_context") or {}
    analyzer = context.get("analyzer") or {}
    analyzer_name = str(analyzer.get("name") or "unknown")
    warning_text = " ".join(str(item) for item in response.get("warnings") or [])
    is_fixture = "fixture" in analyzer_name.lower() or "development fixture" in warning_text.lower()
    response_digest = getattr(response, "source_sha256", None)
    if response_digest is None:
        response_digest = hashlib.sha256(
            json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
    formal_evidence = not is_fixture and analyzer.get("formal_evidence", True) is True
    return {
        "source_kind": "fixture" if is_fixture else "first_group",
        "first_group_schema_version": str(response.get("schema_version") or ""),
        "first_group_run_id": (
            str(response.get("run_id")) if response.get("run_id") is not None else None
        ),
        "source_response_sha256": response_digest,
        "graph_snapshot_id": context.get("graph_snapshot_id"),
        "analyzer": dict(analyzer),
        "formal_evidence": formal_evidence,
        "review_confidence_cap": 1.0 if formal_evidence else 0.6,
    }


def _change_context(response: Dict[str, Any], repository: Dict[str, Any]) -> Dict[str, Any]:
    raw = response.get("change_context")
    if not isinstance(raw, dict):
        raw = {}
    changed_locations = list(raw.get("changed_locations") or [])
    diff_evidence_ids = list(dict.fromkeys(raw.get("diff_evidence_ids") or []))
    status = str(raw.get("status") or ("complete" if changed_locations else "unavailable"))
    result = {
        "status": status,
        "base_sha": raw.get("base_sha", repository.get("base_sha")),
        "head_sha": str(raw.get("head_sha") or repository.get("head_sha") or ""),
        "changed_locations": changed_locations,
        "diff_evidence_ids": diff_evidence_ids,
        "reason": raw.get("reason") or (
            None if status == "complete" else "sync_flow_input_does_not_include_pr_diff"
        ),
    }
    for key in (
        "merge_base_sha",
        "base_is_ancestor_of_head",
        "changed_files",
        "diff_sha256",
    ):
        if key in raw:
            result[key] = copy.deepcopy(raw[key])
    return result


def _stable_candidate_id(
    task_id: str,
    kind: str,
    object_id: str,
    access_a_id: str,
    access_b_id: str,
) -> str:
    access_ids = sorted((access_a_id, access_b_id))
    canonical = "\x1f".join((task_id, kind, object_id, access_ids[0], access_ids[1]))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return "race-{}".format(digest)


def _candidate_evidence(
    evidence_index: Dict[str, Dict[str, Any]], *collections: Sequence[Any]
) -> List[str]:
    evidence_ids = list(
        dict.fromkeys(
            str(item)
            for collection in collections
            for item in collection
            if item
        )
    )
    missing = [item for item in evidence_ids if item not in evidence_index]
    if missing:
        raise ValueError(
            "candidate references missing evidence: {}".format(", ".join(missing))
        )
    return evidence_ids


def _lock_operation_access(
    operation: Dict[str, Any],
    *,
    execution_context: Dict[str, Any],
    held_lock_id: Optional[str] = None,
) -> Dict[str, Any]:
    evidence_ids = list(dict.fromkeys(operation.get("evidence_ids") or []))
    if not evidence_ids:
        raise ValueError("lock operation has no evidence_ids")
    held = [] if held_lock_id is None else [{"lock_id": held_lock_id, "mode": "exclusive"}]
    return {
        "access_id": str(operation.get("operation_id") or ""),
        "kind": "acquire",
        "function": str(operation.get("function") or ""),
        "location": _location(operation.get("location") or {}),
        "execution_context": dict(operation.get("execution_context") or execution_context),
        "must_lockset": held,
        "may_lockset": list(held),
        "other_synchronization": [],
        "confidence": float(operation.get("confidence", 1.0)),
        "evidence_ids": evidence_ids,
    }


def _attach_impact_scope(
    candidate: Dict[str, Any], impact_scope: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    if impact_scope is None:
        return candidate
    candidate["impact_scope"] = {
        "schema_version": impact_scope.get("schema_version"),
        "roots": list(impact_scope.get("roots") or [])[:64],
        "delivery": "host_scope_reference",
        "coverage": copy.deepcopy(impact_scope.get("coverage") or {}),
        "stats": copy.deepcopy(impact_scope.get("stats") or {}),
    }
    return candidate


def _local_impact_evidence(evidence_index, finding, *sites):
    files = {site.get('file') for site in sites if isinstance(site, dict)}
    return [key for key in finding.get('impact_evidence_ids') or []
            if (evidence_index.get(key, {}).get('location') or {}).get('file') in files]


def _bounded_lock_cycles(edges: Sequence[Dict[str, Any]], *, max_steps: int = 10000,
                         max_cycles: int = 64, max_depth: int = 8):
    """Iterative bounded simple-cycle search; retain parallel, evidence-distinct paths.

    A budget/depth stop is a coverage gap, never proof that the graph is acyclic.
    Canonical JSON ordering makes the result independent of producer edge order.
    """
    adjacency = {}
    unique_edges = {json.dumps(item, sort_keys=True): item for item in edges}
    for _, edge in sorted(unique_edges.items()):
        source, target = edge.get("from_lock_id"), edge.get("to_lock_id")
        if source and target and source != target:
            adjacency.setdefault(source, []).append(edge)
    cycles, steps, truncated = [], 0, False
    for start in sorted(adjacency):
        stack = [(start, [start], [])]
        while stack:
            node, nodes, path = stack.pop()
            for edge in reversed(adjacency.get(node, [])):
                steps += 1
                if steps > max_steps:
                    return cycles, True
                target = edge["to_lock_id"]
                if target == start:
                    cycles.append(path + [edge])
                    if len(cycles) >= max_cycles:
                        return cycles, True
                elif target > start and target not in nodes:
                    if len(nodes) >= max_depth:
                        truncated = True
                    else:
                        stack.append((target, nodes + [target], path + [edge]))
    return cycles, truncated


def _edge_access(edge: Dict[str, Any], locks: Dict[str, Any]) -> Dict[str, Any]:
    """Use the supplied observed site, not a guessed indirect-call target."""
    location = edge.get("observed_at") or {}
    function = str(edge.get("function") or "")
    if not function:
        for operation in (locks.get(edge["to_lock_id"]) or {}).get("operations") or []:
            if operation.get("location") == location:
                function = str(operation.get("function") or "")
                break
    identity = hashlib.sha256(json.dumps(edge, sort_keys=True).encode()).hexdigest()[:20]
    return _lock_operation_access({
        "operation_id": "order-site-" + identity, "function": function,
        "location": location, "evidence_ids": edge.get("evidence_ids") or [],
        "confidence": edge.get("confidence", 1.0),
    }, execution_context=edge.get("execution_context") or {}, held_lock_id=edge["from_lock_id"])


def _join_lock_findings(findings: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge canonical lock identities within one version/profile-bound response.

    Shared-object joining is handled separately. Conflicting locks fail closed;
    a name or a similar declaration is never used as an alias heuristic.
    """
    def canonical(value):
        if isinstance(value, dict):
            return {key: canonical(child) for key, child in value.items()}
        if isinstance(value, list):
            return sorted([canonical(child) for child in value], key=lambda child: json.dumps(child, sort_keys=True))
        return value
    result, locks, orders, operation_identities = [], {}, {}, {}
    confidence = 1.0
    impact_ids = []
    order_gaps = set()
    for raw in sorted(findings, key=lambda item: json.dumps(canonical(item), sort_keys=True)):
        if not isinstance(raw, dict) or raw.get("kind") != "sync_flow":
            continue
        finding = dict(raw)
        finding["lock_order_edges"] = []
        result.append(finding)
        if not raw.get("lock_order_edges"):
            continue
        origin = str(raw.get("id") or hashlib.sha256(json.dumps(canonical(raw), sort_keys=True).encode()).hexdigest())
        order_gaps.update(raw.get('join_gaps') or [])
        if raw.get('complete') is False or (raw.get('coverage') or {}).get('complete') is False:
            order_gaps.add('incomplete_source_flow:' + origin)
        confidence = min(confidence, float(raw.get("confidence", 1.0)))
        impact_ids.extend(raw.get("impact_evidence_ids") or [])
        for lock in raw.get("locks") or []:
            identity = str(lock.get("lock_id") or "")
            if not identity:
                raise ValueError("lock-order finding contains a lock without canonical identity")
            for operation in lock.get("operations") or []:
                operation_id = operation.get("operation_id")
                if not operation_id:
                    continue
                signature = {"lock_id": identity, **{key: operation.get(key) for key in
                             ("kind", "api", "function", "location")}}
                if operation_id in operation_identities and operation_identities[operation_id] != signature:
                    raise ValueError("conflicting canonical lock operation: " + str(operation_id))
                operation_identities[operation_id] = signature
            if identity not in locks:
                locks[identity] = copy.deepcopy(lock)
                continue
            previous = locks[identity]
            for field in ("kind", "declaration"):
                if previous.get(field) and lock.get(field) and previous[field] != lock[field]:
                    raise ValueError("conflicting canonical lock identity: " + identity)
            operations = {json.dumps(op, sort_keys=True): op for op in
                          (previous.get("operations") or []) + (lock.get("operations") or [])}
            previous["operations"] = [operations[key] for key in sorted(operations)]
            previous["evidence_ids"] = sorted(set(previous.get("evidence_ids") or []) | set(lock.get("evidence_ids") or []))
        for edge in raw.get("lock_order_edges") or []:
            key = json.dumps(edge, sort_keys=True)
            entry = orders.setdefault(key, copy.deepcopy(edge))
            entry["source_finding_ids"] = sorted(set(entry.get("source_finding_ids") or []) | {origin})
    if orders:
        result.append({"id": "joined-lock-order", "kind": "sync_flow", "shared_objects": [],
                       "locks": [locks[key] for key in sorted(locks)],
                       "lock_order_edges": [orders[key] for key in sorted(orders)],
                       "impact_evidence_ids": sorted(set(impact_ids)), "confidence": confidence,
                       "join_gaps": sorted(order_gaps)})
    return result


def build_candidates(response: Dict[str, Any], *, max_access_pairs: int = 10000,
                     max_race_candidates: int = 2000) -> Dict[str, Any]:
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1
           for value in (max_access_pairs, max_race_candidates)):
        raise ValueError("candidate budgets must be positive")
    evidence = _validated_evidence(response.get("evidence"))
    evidence_index = {item["id"]: item for item in evidence}
    coverage = _coverage(response)
    repository = _repository(response)
    provenance = _provenance(response)
    change_context = _change_context(response, repository)
    impact_scope = response.get("lock_impact_scope")
    if impact_scope is not None and not isinstance(impact_scope, dict):
        raise ValueError("lock_impact_scope must be an object")
    task_id = str(response.get("task_id") or "")
    candidates: List[Dict[str, Any]] = []
    warnings = [str(item) for item in response.get("warnings") or []]
    examined_pairs, race_count = 0, 0
    deferred_pair_ranges = []

    merged_findings, shared_join_stats = merge_shared_findings(response.get('findings') or [])
    for finding in _join_lock_findings(merged_findings):
        if finding.get("kind") != "sync_flow":
            continue
        lock_kinds = {
            str(lock.get("lock_id")): str(lock.get("kind") or "unknown")
            for lock in finding.get("locks") or []
            if isinstance(lock, dict) and lock.get("lock_id")
        }
        flow_confidence = float(finding.get("confidence", 1.0))
        finding_coverage = _coverage_with_gaps(coverage, list(finding.get('join_gaps') or []))
        for shared_object in finding.get("shared_objects") or []:
            identity_confidence = float(shared_object.get("identity_confidence", flow_confidence))
            accesses = sorted([_normalize_access(item) for item in shared_object.get("accesses") or []],
                              key=lambda item: json.dumps(item, sort_keys=True))
            object_examined = 0
            for access_a, access_b in itertools.combinations(accesses, 2):
                if examined_pairs >= max_access_pairs or race_count >= max_race_candidates:
                    deferred_pair_ranges.append({"finding_id": finding.get("id"),
                        "object_id": shared_object.get("object_id"), "access_count": len(accesses),
                        "examined_prefix_pairs": object_examined,
                        "remaining_pair_count": len(accesses) * (len(accesses) - 1) // 2 - object_examined,
                        "ordered_accesses_sha256": hashlib.sha256(json.dumps(accesses, sort_keys=True).encode()).hexdigest()})
                    break
                examined_pairs += 1
                object_examined += 1
                at_least_one_write = bool(
                    access_a["kind"] in WRITE_KINDS or access_b["kind"] in WRITE_KINDS
                )
                if not at_least_one_write:
                    continue
                may_concurrent, context_gaps = _context_overlap_analysis(access_a, access_b)
                if may_concurrent is False:
                    continue
                common_locks = _common_effective_locks(access_a, access_b, lock_kinds)
                other_sync = _shared_other_synchronization(access_a, access_b)
                if common_locks and 'contradictory_concurrency_facts' not in context_gaps:
                    continue
                candidate_coverage = _coverage_with_gaps(finding_coverage,
                    context_gaps + _protection_gaps(access_a, access_b, lock_kinds)
                    + (['object_declaration_unavailable'] if not shared_object.get('declaration') else [])
                    + (['low_object_identity_confidence'] if identity_confidence < 0.8 else []))

                evidence_ids = list(
                    dict.fromkeys(
                        list(shared_object.get("evidence_ids") or [])
                        + access_a["evidence_ids"]
                        + access_b["evidence_ids"]
                        + _local_impact_evidence(evidence_index, finding, access_a['location'], access_b['location'], shared_object.get('declaration'))
                    )
                )
                missing_evidence = [item for item in evidence_ids if item not in evidence_index]
                if missing_evidence:
                    raise ValueError(
                        "candidate references missing evidence: {}".format(", ".join(missing_evidence))
                    )

                incomplete = (
                    not candidate_coverage["complete"]
                    or may_concurrent is None
                    or identity_confidence < 0.8
                )
                kind = (
                    "concurrent_lifetime_access"
                    if access_a["kind"] in LIFETIME_KINDS
                    or access_b["kind"] in LIFETIME_KINDS
                    else "data_race"
                )
                confidence = min(
                    flow_confidence,
                    identity_confidence,
                    access_a["confidence"],
                    access_b["confidence"],
                    provenance["review_confidence_cap"],
                )
                if may_concurrent is None:
                    confidence = min(confidence, 0.6)
                candidate = {
                    "candidate_id": _stable_candidate_id(
                        task_id,
                        kind,
                        str(shared_object.get("object_id") or ""),
                        access_a["access_id"],
                        access_b["access_id"],
                    ),
                    "candidate_status": (
                        "needs_more_context" if incomplete else "ready_for_review"
                    ),
                    "domain": "lock",
                    "kind": kind,
                    "shared_object": {
                        "object_id": str(shared_object.get("object_id") or ""),
                        "name": str(shared_object.get("name") or ""),
                        "type": str(shared_object.get("type") or ""),
                        "identity_confidence": identity_confidence,
                        "declaration": _location(shared_object["declaration"]) if shared_object.get("declaration") else None,
                        "source_finding_ids": list(shared_object.get('source_finding_ids') or []),
                    },
                    "access_a": access_a,
                    "access_b": access_b,
                    "same_object": True,
                    "at_least_one_write": True,
                    "may_concurrent": may_concurrent,
                    "common_must_lockset": common_locks,
                    "other_synchronization": other_sync,
                    "confidence": confidence,
                    "evidence_ids": evidence_ids,
                    "coverage": candidate_coverage,
                }
                candidates.append(_attach_impact_scope(candidate, impact_scope))
                race_count += 1

        shared_by_id = {
            str(item.get("object_id")): item
            for item in finding.get("shared_objects") or []
            if isinstance(item, dict) and item.get("object_id")
        }
        for condition in finding.get("condition_variables") or []:
            if not isinstance(condition, dict):
                continue
            wait = condition.get("wait") or {}
            guard_kind = str(wait.get("guard_kind") or "unknown").lower()
            if guard_kind in {"while", "loop"}:
                continue
            shared_object = shared_by_id.get(str(condition.get("predicate_object_id") or ""))
            if shared_object is None:
                continue
            accesses = [_normalize_access(item) for item in shared_object.get("accesses") or []]
            reads = [item for item in accesses if item["kind"] == "read"]
            writes = [item for item in accesses if item["kind"] in WRITE_KINDS]
            if not reads or not writes:
                continue
            access_a, access_b = reads[0], writes[0]
            may_concurrent, context_gaps = _context_overlap_analysis(access_a, access_b)
            condition_coverage = _coverage_with_gaps(finding_coverage, context_gaps
                + (['object_declaration_unavailable'] if not shared_object.get('declaration') else []))
            common_locks = _common_effective_locks(access_a, access_b, lock_kinds)
            evidence_ids = _candidate_evidence(
                evidence_index,
                shared_object.get("evidence_ids") or [],
                access_a["evidence_ids"],
                access_b["evidence_ids"],
                condition.get("evidence_ids") or [],
                wait.get("evidence_ids") or [],
                _local_impact_evidence(evidence_index, finding, access_a['location'], access_b['location'], shared_object.get('declaration')),
            )
            identity_confidence = float(
                shared_object.get("identity_confidence", flow_confidence)
            )
            incomplete = (
                not condition_coverage["complete"]
                or guard_kind == "unknown"
                or identity_confidence < 0.8
            )
            confidence = min(
                flow_confidence,
                identity_confidence,
                access_a["confidence"],
                access_b["confidence"],
                provenance["review_confidence_cap"],
            )
            candidate = {
                "candidate_id": _stable_candidate_id(
                    task_id,
                    "condition_variable_protocol",
                    str(shared_object.get("object_id") or ""),
                    access_a["access_id"],
                    access_b["access_id"],
                ),
                "candidate_status": (
                    "needs_more_context" if incomplete else "ready_for_review"
                ),
                "domain": "lock",
                "kind": "condition_variable_protocol",
                "shared_object": {
                    "object_id": str(shared_object.get("object_id") or ""),
                    "name": str(shared_object.get("name") or ""),
                    "type": str(shared_object.get("type") or ""),
                    "identity_confidence": identity_confidence,
                    "declaration": _location(shared_object["declaration"]) if shared_object.get("declaration") else None,
                },
                "access_a": access_a,
                "access_b": access_b,
                "same_object": True,
                "at_least_one_write": True,
                "may_concurrent": may_concurrent,
                "common_must_lockset": common_locks,
                "other_synchronization": _shared_other_synchronization(access_a, access_b),
                "condition_variable": copy.deepcopy(condition),
                "confidence": confidence,
                "evidence_ids": evidence_ids,
                "coverage": condition_coverage,
            }
            candidates.append(_attach_impact_scope(candidate, impact_scope))

        locks_by_id = {
            str(item.get("lock_id")): item
            for item in finding.get("locks") or []
            if isinstance(item, dict) and item.get("lock_id")
        }
        cycles, truncated = _bounded_lock_cycles([
            edge for edge in finding.get("lock_order_edges") or [] if isinstance(edge, dict)
        ])
        if truncated:
            coverage["complete"] = False
            coverage["reason"] = "lock_order_cycle_search_budget"
            coverage["unresolved"].append("lock_order_cycle_search_budget")
            warnings.append("bounded lock-order search stopped; remaining cycles may be missing")
        for cycle in cycles:
            accesses = [_edge_access(edge, locks_by_id) for edge in cycle]
            decisions = [_context_overlap_analysis(a, b) for a, b in itertools.combinations(accesses, 2)]
            overlaps = [decision[0] for decision in decisions]
            cycle_coverage = _coverage_with_gaps(finding_coverage, [gap for _, gaps in decisions for gap in gaps])
            if overlaps and all(value is False for value in overlaps):
                continue
            lock_ids = [edge["from_lock_id"] for edge in cycle]
            evidence_ids = _candidate_evidence(evidence_index,
                *[edge.get("evidence_ids") or [] for edge in cycle],
                *[(locks_by_id.get(lock_id) or {}).get("evidence_ids") or [] for lock_id in lock_ids],
                _local_impact_evidence(evidence_index, finding, *(access['location'] for access in accesses)))
            known = (cycle_coverage["complete"] and all(value is True for value in overlaps)
                     and all(access["function"] for access in accesses)
                     and all(edge.get("blocking") is True for edge in cycle)
                     and all(lock_id in locks_by_id for lock_id in lock_ids))
            signature = json.dumps(cycle, sort_keys=True)
            candidate = {
                "candidate_id": _stable_candidate_id(task_id, "lock_order_deadlock",
                    "|".join(lock_ids), signature, signature),
                "candidate_status": "ready_for_review" if known else "needs_more_context",
                "domain": "lock", "kind": "lock_order_deadlock",
                "shared_object": {"object_id": "|".join(lock_ids), "name": "lock-order cycle",
                    "type": "lock_cycle", "identity_confidence": 1.0 if known else 0.6,
                    "declaration": accesses[0]["location"]},
                "access_a": accesses[0], "access_b": accesses[1],
                "same_object": None, "at_least_one_write": None,
                "may_concurrent": True if all(value is True for value in overlaps) else None,
                "common_must_lockset": [], "other_synchronization": [],
                "lock_order": {"self_edge": False, "cycle_lock_ids": lock_ids,
                    "cycle_edges": copy.deepcopy(cycle), "cycle_accesses": accesses},
                "confidence": min(flow_confidence, provenance["review_confidence_cap"],
                    *(access["confidence"] for access in accesses), 1.0 if known else 0.6),
                "evidence_ids": evidence_ids, "coverage": cycle_coverage,
            }
            candidates.append(_attach_impact_scope(candidate, impact_scope))
        for edge in finding.get("lock_order_edges") or []:
            if not isinstance(edge, dict):
                continue
            from_lock = str(edge.get("from_lock_id") or "")
            to_lock = str(edge.get("to_lock_id") or "")
            if not from_lock or from_lock != to_lock:
                continue
            lock = locks_by_id.get(from_lock)
            if lock is None or str(lock.get("kind") or "") in {"recursive_mutex", "reentrant"}:
                continue
            acquisitions = [
                item
                for item in lock.get("operations") or []
                if isinstance(item, dict) and item.get("kind") in {"acquire", "lock"}
            ]
            if len(acquisitions) < 2:
                continue
            edge_context = dict(edge.get("execution_context") or {})
            access_a = _lock_operation_access(
                acquisitions[0], execution_context=edge_context
            )
            access_b = _lock_operation_access(
                acquisitions[1],
                execution_context=edge_context,
                held_lock_id=from_lock,
            )
            evidence_ids = _candidate_evidence(
                evidence_index,
                lock.get("evidence_ids") or [],
                access_a["evidence_ids"],
                access_b["evidence_ids"],
                edge.get("evidence_ids") or [],
                _local_impact_evidence(evidence_index, finding, access_a['location'], access_b['location'], lock.get('declaration')),
            )
            confidence = min(
                flow_confidence,
                access_a["confidence"],
                access_b["confidence"],
                provenance["review_confidence_cap"],
            )
            candidate = {
                "candidate_id": _stable_candidate_id(
                    task_id,
                    "lock_order_deadlock",
                    from_lock,
                    access_a["access_id"],
                    access_b["access_id"],
                ),
                "candidate_status": (
                    "needs_more_context" if not finding_coverage["complete"] else "ready_for_review"
                ),
                "domain": "lock",
                "kind": "lock_order_deadlock",
                "shared_object": {
                    "object_id": from_lock,
                    "name": str(lock.get("name") or from_lock),
                    "type": str(lock.get("kind") or "lock"),
                    "identity_confidence": 1.0,
                    "declaration": _location(lock.get("declaration") or {}),
                },
                "access_a": access_a,
                "access_b": access_b,
                "same_object": True,
                "at_least_one_write": None,
                "may_concurrent": False,
                "common_must_lockset": [from_lock],
                "other_synchronization": [],
                "lock_order": {
                    "from_lock_id": from_lock,
                    "to_lock_id": to_lock,
                    "self_edge": True,
                    "observed_at": _location(edge.get("observed_at") or {}),
                    "execution_context": edge_context,
                },
                "confidence": confidence,
                "evidence_ids": evidence_ids,
                "coverage": dict(finding_coverage),
            }
            candidates.append(_attach_impact_scope(candidate, impact_scope))

    if "lock_order_cycle_search_budget" in coverage["unresolved"]:
        for candidate in candidates:
            candidate["coverage"] = copy.deepcopy(coverage)
            candidate["candidate_status"] = "needs_more_context"
    if deferred_pair_ranges:
        # Exhausted discovery is a RUN-level gap, not missing evidence for an
        # already materialized bounded candidate. Preserve each candidate's facts.
        coverage = copy.deepcopy(coverage)
        coverage["complete"] = False
        coverage["reason"] = "candidate_discovery_budget"
        coverage["unresolved"].append("candidate_discovery_budget")
        warnings.append("unexamined access pairs are deferred, not classified safe")
    unique_candidates = {}
    for candidate in candidates:
        key = candidate['candidate_id']
        previous = unique_candidates.get(key)
        if previous is not None and previous != candidate:
            raise ValueError('conflicting duplicate candidate identity: ' + key)
        unique_candidates[key] = candidate
    duplicate_count = len(candidates) - len(unique_candidates)
    candidates = list(unique_candidates.values())
    return {
        "schema_version": "race-candidates/0.1",
        "task_id": task_id,
        "repository": repository,
        "provenance": provenance,
        "change_context": change_context,
        "impact_scope": impact_scope,
        "candidates": candidates,
        "discovery_budget": {"max_access_pairs": max_access_pairs,
                             "max_race_candidates": max_race_candidates,
                             "examined_access_pairs": examined_pairs,
                             "exact_duplicates_removed": duplicate_count,
                             "shared_object_join": shared_join_stats,
                             "deferred_pair_ranges": deferred_pair_ranges},
        # Keep candidate packages self-contained.  A reviewer must be able to
        # resolve every evidence_id without depending on the producer's file
        # layout or making a second network request.
        "evidence": list(evidence),
        "coverage": coverage,
        "warnings": warnings,
        "errors": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build race candidates from a sync_flow fixture")
    parser.add_argument("input", help="Path to first-group sync_flow JSON")
    parser.add_argument("--output", help="Optional output JSON path")
    args = parser.parse_args()
    try:
        result = build_candidates(load_response(args.input))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 1
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered + "\n")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
