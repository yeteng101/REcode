"""Annotate client object evidence with conditional C execution contexts."""
from __future__ import annotations

from collections import defaultdict

from .clang_ast import stable_digest
from .sync_context_paths import MAX_DISPATCH_PATHS, collect_context_paths


MAX_CONTEXTS = 128


def _window(create, dispatch, joins):
    if not dispatch or create.position is None:
        return None
    start = dispatch[0]
    if start.caller != create.caller or start.position != create.position + 1:
        return None
    return next((chain for chain in joins if chain[0].caller == start.caller and chain[0].position == start.position + 1), None)


def collect_execution_contexts(asts, analyzer, direct_calls, field_bindings, create_apis, join_apis):
    client_bindings = field_bindings["client_objects"]["bindings"]
    result = {"schema_version": "sync-execution-contexts/1", "bindings": [],
              "coverage": {"scope": "source-level thread entry and resolved direct dispatch paths",
                           "limits_hit": [], "max_bindings": MAX_CONTEXTS}}
    if not client_bindings:
        return result
    paths = collect_context_paths(asts, analyzer, direct_calls, create_apis, join_apis)
    starts = []
    joins = defaultdict(list)
    for kind, handle, entry, chain in paths.thread_events():
        if kind == "join":
            joins[handle.identity].append(chain)
        else:
            starts.append((handle, entry, chain))

    direct = {row["id"]: row for row in field_bindings["direct_calls"]["bindings"]}
    accesses = {row["id"]: row for row in field_bindings["accesses"]}
    evidence, bindings = {}, {}
    for relation in client_bindings:
        if not relation["registration_calls"]:
            continue
        peer = direct[relation["direct_binding_id"]]
        reader = accesses[relation["queue_access_id"]]
        targets = paths.functions[reader["location"]["file"], reader["function"]]
        sender_roots = {call.caller for call in paths.matching_calls(peer["calls"][0])}
        if not targets or not sender_roots:
            continue
        for registration in paths.matching_calls(relation["registration_calls"][0]):
            dispatch_candidates = paths.paths(registration.caller, targets, MAX_DISPATCH_PATHS)
            if not dispatch_candidates:
                continue
            for handle, entry, creation in starts:
                sender_candidates = paths.paths(entry.identity, sender_roots, 1)
                if not sender_candidates:
                    continue
                sender = sender_candidates[0]
                window = next(((route[0], join) for route in dispatch_candidates
                               if (join := _window(creation[0], route, joins[handle.identity]))), None)
                item = {"client_binding_id": relation["id"],
                        "worker": {"entry": direct_calls.names[entry.identity],
                                   "creation": [paths.describe(step) for step in creation],
                                   "adapter_locations": list(entry.adapter_sites),
                                   "calls": [paths.describe(step) for step in sender] + peer["calls"]},
                        "consumer": {"root": direct_calls.names[registration.caller],
                                     "call_paths": [[paths.describe(step) for step in route]
                                                    for route in dispatch_candidates]},
                        "overlap": "unknown"}
                if window:
                    dispatch, wait = window
                    item["thread_window"] = {"status": "conditional_start_dispatch_join",
                                             "dispatch": paths.describe(dispatch),
                                             "join": [paths.describe(step) for step in wait]}
                identity = "sync_context_" + stable_digest(item)[:20]
                if identity in bindings:
                    continue
                if len(bindings) >= MAX_CONTEXTS:
                    paths.limits.add("binding_count")
                    break
                sites = [("thread_start", step.location) for step in creation]
                sites.extend(("thread_callback_adapter", site) for site in entry.adapter_sites)
                sites.extend(("worker_call", step.location) for step in sender)
                sites.extend(("dispatch_call", step.location) for route in dispatch_candidates for step in route)
                if window:
                    sites.extend(("thread_join", step.location) for step in wait)
                ids = list(relation["evidence_ids"])
                for kind, site in sites:
                    key = "ev_ast_execution_context_" + stable_digest([kind, site])[:20]
                    evidence[key] = {"id": key, "kind": kind, "location": site, "role": "supports"}
                    ids.append(key)
                bindings[identity] = dict(item, id=identity, evidence_ids=list(dict.fromkeys(ids)))
    field_bindings["evidence"].extend(evidence.values())
    result["bindings"] = list(bindings.values())
    result["coverage"]["limits_hit"] = sorted(paths.limits)
    return result
