"""Relate a possible queue registration to another access via caller evidence."""
from __future__ import annotations

from collections import Counter, defaultdict

from .clang_ast import stable_digest
from .sync_field_calls import field_object_id


MAX_CLIENT_BINDINGS = 256
MAX_PER_QUEUE_SOURCE = 16


def _sources(registration, calls, limits):
    receiver = registration["receiver"]
    if receiver["kind"] == "global_storage":
        yield receiver, ()
    elif receiver["kind"] == "parameter_member":
        yield from calls.registration_chains(registration["function"], registration["function_definition"],
                                             receiver["parameter_index"], limits)


def collect_client_bindings(field_bindings, direct_calls):
    accesses = {item["id"]: item for item in field_bindings["accesses"]}
    direct = defaultdict(list)
    for binding in field_bindings["direct_calls"]["bindings"]:
        if "field_object_id" in binding:
            direct[binding["field_object_id"]].append(binding)

    limits = set()
    records, evidence = {}, {}
    counts = Counter()
    for queue in field_bindings["queue_provenance"]["bindings"]:
        access = accesses[queue["access_id"]]
        for source, chain in _sources(queue["registration"], direct_calls, limits):
            object_id = field_object_id(source["object_id"], access["field"]["id"])
            for peer in direct.get(object_id, ()):
                if queue["access_id"] == peer["access_id"]:
                    continue
                identity = "sync_client_object_" + stable_digest([queue["id"], peer["id"], chain])[:20]
                if identity in records:
                    continue
                if len(records) >= MAX_CLIENT_BINDINGS or counts[queue["id"]] >= MAX_PER_QUEUE_SOURCE:
                    limits.add("binding_count")
                    break
                ids = [*queue["evidence_ids"], *peer["evidence_ids"]]
                sites = [("registered_object", source["declaration"])]
                for edge in chain:
                    sites.append(("registration_caller", edge["location"]))
                    sites.append(("registration_argument", edge["argument_location"]))
                    sites.append(("registration_origin", edge["source"]["declaration"]))
                    sites.extend(("registration_pointer_copy", site) for site in edge["source"]["alias_steps"])
                for kind, location in sites:
                    key = "ev_ast_client_binding_" + stable_digest([kind, location])[:20]
                    evidence[key] = {"id": key, "kind": kind, "location": location, "role": "supports"}
                    ids.append(key)
                records[identity] = {"id": identity, "status": "same_object_if_registration_source",
                                     "field_object_id": object_id, "object": source,
                                     "queue_provenance_id": queue["id"], "queue_access_id": queue["access_id"],
                                     "direct_binding_id": peer["id"], "direct_access_id": peer["access_id"],
                                     "registration_calls": list(chain), "evidence_ids": list(dict.fromkeys(ids))}
                counts[queue["id"]] += 1

    field_bindings["evidence"].extend(evidence.values())
    return {"schema_version": "sync-client-object-bindings/1", "bindings": list(records.values()),
            "coverage": {"scope": "global storage given the observed registration and queue source",
                         "limits_hit": sorted(limits), "limits": {"bindings": MAX_CLIENT_BINDINGS,
                                                                   "per_queue_source": MAX_PER_QUEUE_SOURCE}}}
