"""Trace possible C object provenance from an intrusive queue registration."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .clang_ast import iter_nodes, stable_digest
from .sync_field_bindings import (SKIP_EXPRESSIONS, UNSUPPORTED_JUMPS, _assignment, _initializer, _pointee,
                                 _pointer_expression, _reference, _site, _type)
from .sync_queue_offset import recovery_parts
from .sync_queue_shapes import Member, QueueShapes


MAX_RESULTS = 256
MAX_PER_ACCESS = 16
MAX_TRANSFERS = 3
MAX_REGISTRATIONS_PER_QUEUE = 32


@dataclass(frozen=True)
class Slot:
    key: tuple
    kind: str
    site: dict
    record: str


@dataclass(frozen=True)
class QueueRead:
    original: Slot
    link: str
    transfers: tuple
    head: dict
    local_registrations: tuple


@dataclass(frozen=True)
class Recovered:
    read: QueueRead
    record: str
    member: str
    site: dict


@dataclass
class Scope:
    values: dict = field(default_factory=dict)
    transfers: dict = field(default_factory=dict)
    registrations: dict = field(default_factory=dict)
    invalidated: set = field(default_factory=set)

    def branch(self):
        return Scope(dict(self.values), dict(self.transfers), dict(self.registrations))


def _address(expression):
    node = _pointer_expression(expression)
    if node.get("kind") == "UnaryOperator" and node.get("opcode") == "&" and len(node.get("inner", [])) == 1:
        return _pointer_expression(node["inner"][0])
    return None


def _registration_key(registration):
    return stable_digest(registration)[:20]


def _site_key(location):
    return location["file"], location["line"], location["column"]


class QueueFunction:
    def __init__(self, shapes, node, parameters, access_sites, add_registration, add_recovery, limits):
        self.shapes, self.function = shapes, node
        self.locals = {param["id"]: param for param in parameters}
        self.parameter_indices = {param["id"]: index for index, param in enumerate(parameters)}
        self.access_sites = access_sites
        self.add_registration, self.add_recovery = add_registration, add_recovery
        self.limits = limits
        self.writes = Counter()
        self.escaped = set()

    def collect(self, body):
        for node in iter_nodes(body):
            kind = node.get("kind")
            if kind in UNSUPPORTED_JUMPS:
                return
            if kind == "VarDecl":
                self.locals[node["id"]] = node
                if _initializer(node) is not None:
                    self.writes[node["id"]] += 1
            elif _assignment(node) or kind == "CompoundAssignOperator" or (
                kind == "UnaryOperator" and node.get("opcode") in {"++", "--"}
            ):
                self.writes[_reference(node["inner"][0])] += 1
            elif kind == "UnaryOperator" and node.get("opcode") == "&":
                self.escaped.add(_reference(node["inner"][0]))
        self.visit(body, Scope())

    def _member(self, node):
        if node.get("kind") != "MemberExpr" or len(node.get("inner", [])) != 1:
            return None, None
        member = self.shapes.members.get(node.get("referencedMemberDecl"))
        base = _reference(node["inner"][0])
        return member, base

    def _root(self, base, arrow):
        node = self.locals.get(base)
        if arrow:
            if node and node.get("kind") == "ParmVarDecl" and self.writes[base] == 0 and base not in self.escaped:
                location = self.shapes.location(node)
                if location:
                    return ("parameter", _site_key(location)), "parameter_member", location, node.get("name")
            return None
        if node and node.get("storageClass") not in {"extern", "static"}:
            location = self.shapes.location(node)
            if location:
                return ("automatic", _site_key(location)), "automatic_storage", location, node.get("name")
        analyzer = self.shapes.analyzer
        identity = analyzer.global_references.get((self.shapes.tu, base))
        obj = analyzer.objects.get(identity)
        if obj and identity not in analyzer.global_binding_exclusions:
            return ("global", identity), "global_storage", _site(obj["declaration"]), obj["name"]
        return None

    def _slot(self, expression):
        addressed = _address(expression)
        return self._addressed_slot(addressed) if addressed else None

    def _addressed_slot(self, addressed):
        location = self.shapes.location(addressed)
        if not location:
            return None
        member, base = self._member(addressed)
        if member:
            root = self._root(base, addressed.get("isArrow"))
            if not root or not member.embedded:
                return None
            identity, kind, _, _ = root
            if kind == "parameter_member":
                identity = ("parameter_member", member.owner)
            return Slot((*identity, member.identity), kind, location, member.embedded)
        base = _reference(addressed)
        root = self._root(base, False) if base else None
        record = self.shapes.record(_type(addressed))
        if root and record:
            identity, kind, _, _ = root
            return Slot(identity, kind, location, record)
        return None

    def _call(self, node):
        children = node.get("inner", [])
        if node.get("kind") != "CallExpr" or not children:
            return None, []
        target = self.shapes.analyzer.function_target(children[0], self.shapes.tu)
        return self.shapes.operations.get(target), children[1:]

    def _insert(self, scope, operation, slot, argument, location):
        addressed = _address(argument)
        if not addressed:
            return
        member, base = self._member(addressed)
        if not member or member.embedded != slot.record:
            return
        root = self._root(base, addressed.get("isArrow"))
        embedded_location = self.shapes.location(addressed)
        if not root or not embedded_location:
            return
        receiver_identity, receiver_kind, declaration, name = root
        receiver = {"kind": receiver_kind, "name": name, "declaration": declaration}
        if receiver_kind == "parameter_member":
            receiver["parameter_index"] = self.parameter_indices[base]
        if receiver_kind == "global_storage":
            receiver["object_id"] = receiver_identity[1]
        item = {"function": self.function["name"], "call": location, "definition": operation.definition,
                "function_definition": self.shapes.location(self.function),
                "queue": slot.site, "queue_storage": slot.kind, "embedded_address": embedded_location,
                "embedded_member": member.name, "member_declaration": member.declaration,
                "receiver": receiver}
        key = slot.key, operation.link, member.owner, member.name
        if slot.kind == "automatic_storage":
            registrations = scope.registrations.get(key, {})
            identity = _registration_key(item)
            if identity in registrations:
                return
            if len(registrations) >= MAX_REGISTRATIONS_PER_QUEUE:
                self.limits.add("registrations_per_queue")
                return
            scope.registrations[key] = {**registrations, identity: item}
        else:
            self.add_registration(key, item)

    def _invalidate(self, slot_key, scope):
        scope.invalidated.add(slot_key)
        for key in tuple(scope.transfers):
            if key[0] == slot_key:
                del scope.transfers[key]
        for key in tuple(scope.registrations):
            if key[0] == slot_key:
                del scope.registrations[key]

    def _call_effect(self, node, scope):
        operation, arguments = self._call(node)
        if not operation:
            for argument in arguments:
                slot = self._slot(argument)
                if slot:
                    self._invalidate(slot.key, scope)
            return
        location = self.shapes.location(node)
        if not location or operation.source >= len(arguments):
            return
        source = self._slot(arguments[operation.source])
        target = operation.destination
        if operation.kind == "insert" and source and target < len(arguments):
            self._insert(scope, operation, source, arguments[target], location)
        elif operation.kind == "transfer" and target < len(arguments):
            destination = self._slot(arguments[target])
            if not destination:
                return
            self._invalidate(destination.key, scope)
            if not source or source.record != destination.record or source.key == destination.key:
                return
            inherited = scope.transfers.get((source.key, operation.link))
            origin, hops, registrations = inherited or (source, (), self._local_registrations(source, operation.link, scope))
            if len(hops) >= MAX_TRANSFERS:
                self.limits.add("transfer_depth")
                return
            step = {"call": location, "source": source.site, "destination": destination.site,
                    "definition": operation.definition}
            if operation.helper:
                step["helper_definition"] = operation.helper
            scope.transfers[destination.key, operation.link] = (origin, (*hops, step), registrations)

    def _local_registrations(self, slot, link, scope):
        return tuple((owner, name, row) for (key, member_link, owner, name), rows in scope.registrations.items()
                     if key == slot.key and member_link == link for row in rows.values())

    def _value(self, expression, scope):
        node = _pointer_expression(expression)
        if node.get("kind") == "CallExpr":
            operation, arguments = self._call(node)
            if not operation or operation.kind != "read" or operation.source >= len(arguments):
                return None
            slot = self._slot(arguments[operation.source])
            call = self.shapes.location(node)
            if slot and call:
                inherited = scope.transfers.get((slot.key, operation.link))
                source, transfers, registrations = inherited or (slot, (), self._local_registrations(slot, operation.link, scope))
                head = {"call": call, "queue": slot.site, "definition": operation.definition}
                return QueueRead(source, operation.link, transfers, head, registrations)
            return None
        if node.get("kind") == "DeclRefExpr":
            return scope.values.get(_reference(node))
        recovered = recovery_parts(expression, self.shapes)
        if recovered:
            reference, owner, member, location = recovered
            pointer = scope.values.get(_reference(reference))
            if isinstance(pointer, QueueRead):
                return Recovered(pointer, owner, member, location)
        return None

    def _define(self, identity, expression, scope):
        node = self.locals.get(identity)
        if (not node or node.get("kind") != "VarDecl" or node.get("storageClass") in {"extern", "static"}
                or identity in self.escaped or self.writes[identity] != 1 or not _pointee(node)
                or "volatile" in _type(node).split()):
            return
        value = self._value(expression, scope)
        if value is not None:
            scope.values[identity] = value

    def _capture(self, node, scope):
        member, base = self._member(node)
        value = scope.values.get(base)
        if not node.get("isArrow") or not isinstance(member, Member) or not isinstance(value, Recovered):
            return
        if member.owner != value.record:
            return
        location = self.shapes.location(node)
        if location:
            key = self.function["name"], _site_key(location), member.name
            for access in self.access_sites.get(key, ()):
                if access["field"]["declaration"] == member.declaration:
                    self.add_recovery(access, value)

    def visit(self, node, scope):
        kind = node.get("kind")
        if kind in SKIP_EXPRESSIONS:
            return
        children = node.get("inner", [])
        if kind == "CompoundStmt":
            outer, scope = scope, scope.branch()
            for statement in children:
                if statement.get("kind") == "DeclStmt":
                    for declaration in statement.get("inner", []):
                        self.visit(declaration, scope)
                        initializer = _initializer(declaration)
                        if initializer is not None:
                            self._define(declaration["id"], initializer, scope)
                else:
                    self.visit(statement, scope)
                    if _assignment(statement):
                        left, right = statement["inner"]
                        self._define(_reference(left), right, scope)
                        self._write_effect(left, scope)
                    elif statement.get("kind") == "CallExpr":
                        self._call_effect(statement, scope)
            # A scoped write can kill a previous route even when its new value
            # is only modeled inside that scope.
            for key in scope.invalidated:
                self._invalidate(key, outer)
            return
        if kind == "MemberExpr":
            self._capture(node, scope)
        for child in children:
            self.visit(child, scope)

    def _write_effect(self, left, scope):
        left = _pointer_expression(left)
        slot = self._addressed_slot(left)
        if not slot and left.get("kind") == "MemberExpr" and not left.get("isArrow"):
            slot = self._addressed_slot(_pointer_expression(left["inner"][0]))
        if slot:
            self._invalidate(slot.key, scope)


def collect_queue_provenance(asts, analyzer, field_bindings):
    access_sites = defaultdict(list)
    for access in field_bindings["accesses"]:
        key = access["function"], _site_key(access["address_formation"]), access["field"]["name"]
        access_sites[key].append(access)
    registrations = defaultdict(dict)
    recoveries = []
    limits = set()

    def add_registration(key, item):
        rows = registrations[key]
        identity = _registration_key(item)
        if identity in rows:
            return
        if len(rows) >= MAX_REGISTRATIONS_PER_QUEUE:
            limits.add("registrations_per_queue")
            return
        rows[identity] = item

    def add_recovery(access, value):
        recoveries.append((access, value))

    if access_sites:
        for tu, ast in asts:
            if not tu.endswith(".c"):
                continue
            shapes = QueueShapes(analyzer, tu, ast)
            shapes.summarize()
            if shapes.operations:
                for _, function, parameters, body in shapes.definitions:
                    QueueFunction(shapes, function, parameters, access_sites, add_registration, add_recovery, limits).collect(body)

    results, evidence = {}, {}
    counts = Counter()
    for access, recovered in recoveries:
        read = recovered.read
        key = read.original.key, read.link, recovered.record, recovered.member
        candidates = list(registrations.get(key, {}).values())
        candidates.extend(item for owner, member, item in read.local_registrations
                          if owner == recovered.record and member == recovered.member)
        for source in candidates:
            item = {"access_id": access["id"], "status": "possible_registration_source", "registration": source,
                    "queue_identity": read.original.kind, "transfers": list(read.transfers), "head": read.head,
                    "owner_recovery": {"location": recovered.site, "member": recovered.member}}
            identity = "sync_queue_source_" + stable_digest(item)[:20]
            if identity in results:
                continue
            if len(results) >= MAX_RESULTS or counts[access["id"]] >= MAX_PER_ACCESS:
                limits.add("binding_count")
                continue
            sites = [("registration_" + kind, source[kind]) for kind in
                     ("call", "definition", "function_definition", "queue", "embedded_address", "member_declaration")]
            sites.append(("registered_receiver", source["receiver"]["declaration"]))
            for transfer in read.transfers:
                sites.extend(("transfer_" + role, location) for role, location in transfer.items())
            sites.extend(("head_" + role, location) for role, location in read.head.items())
            sites.append(("owner_recovery", recovered.site))
            ids = list(access["evidence_ids"])
            for kind, location in sites:
                evidence_id = "ev_ast_queue_" + stable_digest([kind, location])[:20]
                evidence[evidence_id] = {"id": evidence_id, "kind": kind, "location": location, "role": "supports"}
                ids.append(evidence_id)
            results[identity] = dict(item, id=identity, evidence_ids=list(dict.fromkeys(ids)))
            counts[access["id"]] += 1
    field_bindings["evidence"].extend(evidence.values())
    return {"schema_version": "sync-queue-provenance/1", "bindings": list(results.values()),
            "coverage": {"scope": "possible sources in recognized C intrusive-list operations",
                         "limits_hit": sorted(limits), "limits": {"results": MAX_RESULTS, "per_access": MAX_PER_ACCESS,
                                                                   "transfers": MAX_TRANSFERS, "registrations_per_queue": MAX_REGISTRATIONS_PER_QUEUE}}}
