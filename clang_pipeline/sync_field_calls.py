"""Forward source-proven receiver origins along resolved C direct calls."""
from __future__ import annotations

from collections import Counter, defaultdict

from .clang_ast import stable_digest


MAX_CALL_DEPTH = 4
MAX_CALL_BINDINGS = 256
MAX_CHAINS_PER_ACCESS = 16
REDIRECTED_FUNCTION = {"AliasAttr", "WeakAttr", "WeakRefAttr", "WeakImportAttr", "IFuncAttr", "AsmLabelAttr"}


def field_object_id(object_id, field_id):
    return "sync_field_" + stable_digest([object_id, field_id])[:20]


class DirectFieldCalls:
    def __init__(self):
        self.definitions = defaultdict(list)
        self.names = {}
        self.excluded = set()
        self.calls = []
        self.seeds = []

    def declare(self, unit, identity, name, definition, attrs):
        if attrs & REDIRECTED_FUNCTION:
            self.excluded.add(identity)
        if definition:
            key = (unit, identity)
            self.definitions[identity].append(key)
            self.names[key] = name

    def collect(self, accesses, evidence):
        incoming = defaultdict(list)
        for caller, target, location, arguments in self.calls:
            if target in self.excluded:
                continue
            definitions = self.definitions.get(target, [])
            local = [definition for definition in definitions if definition[0] == caller[0]]
            choices = local or definitions
            if len(choices) != 1:
                continue
            callee = choices[0]
            for index, argument_location, origin in arguments:
                incoming[callee, index].append((caller, location, argument_location, origin))

        records = {}
        counts = Counter()
        limits_hit = set()

        def add_evidence(kind, location):
            identity = "ev_ast_field_call_" + stable_digest([kind, location])[:20]
            evidence[identity] = {"id": identity, "kind": kind, "location": location, "role": "supports"}
            return identity

        for access_id, callee, receiver_origin in self.seeds:
            access = accesses[access_id]
            stack = [(callee, receiver_origin["parameter_index"], (), frozenset({callee}))]
            while stack:
                function, index, suffix, visited = stack.pop()
                for caller, location, argument_location, origin in incoming[function, index]:
                    if caller in visited:
                        limits_hit.add("cycle")
                        continue
                    if len(suffix) >= MAX_CALL_DEPTH:
                        limits_hit.add("call_depth")
                        continue
                    segment = {"caller": self.names[caller], "callee": self.names[function],
                               "location": location, "argument_location": argument_location,
                               "parameter_index": index, "source": origin}
                    chain = (segment, *suffix)
                    key = "sync_field_call_" + stable_digest([access_id, receiver_origin, chain])[:20]
                    if key not in records and (len(records) >= MAX_CALL_BINDINGS or counts[access_id] >= MAX_CHAINS_PER_ACCESS):
                        limits_hit.add("binding_count")
                        break
                    if origin["kind"] == "parameter_pointer":
                        stack.append((caller, origin["parameter_index"], chain, visited | {caller}))
                    if key in records:
                        continue
                    ids = list(access["evidence_ids"])
                    for site in receiver_origin["alias_steps"]:
                        ids.append(add_evidence("receiver_pointer_copy", site))
                    for edge in chain:
                        source = edge["source"]
                        ids.append(add_evidence("direct_call", edge["location"]))
                        ids.append(add_evidence("call_argument", edge["argument_location"]))
                        ids.append(add_evidence("argument_origin", source["declaration"]))
                        for site in source["alias_steps"]:
                            ids.append(add_evidence("receiver_pointer_copy", site))
                    item = {"id": key, "access_id": access_id, "receiver_parameter": receiver_origin,
                            "origin": origin, "calls": list(chain), "evidence_ids": list(dict.fromkeys(ids))}
                    if origin["kind"] == "global_storage":
                        item["field_object_id"] = field_object_id(origin["object_id"], access["field"]["id"])
                    records[key] = item
                    counts[access_id] += 1
        return {"bindings": list(records.values()),
                "coverage": {"scope": "receiver provenance conditional on reaching the resolved C direct calls",
                             "limits_hit": sorted(limits_hit),
                             "limits": {"call_depth": MAX_CALL_DEPTH, "bindings": MAX_CALL_BINDINGS,
                                        "chains_per_access": MAX_CHAINS_PER_ACCESS}}}
