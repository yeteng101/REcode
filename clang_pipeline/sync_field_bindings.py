"""Bounded, intraprocedural C pointer-to-field provenance."""
from __future__ import annotations

import re
from collections import Counter

from .clang_ast import iter_nodes, stable_digest
from .sync_field_calls import field_object_id
from .sync_source_observations import atomic_opcode, expression_access


MAX_BINDINGS = 512
MAX_PER_FILE = 64
MAX_ALIAS_STEPS = 8
UNSUPPORTED_JUMPS = {"GotoStmt", "IndirectGotoStmt", "LabelStmt", "SwitchStmt", "GCCAsmStmt", "MSAsmStmt"}
SKIP_EXPRESSIONS = {"StmtExpr", "UnaryExprOrTypeTraitExpr", "GenericSelectionExpr", "ChooseExpr", "TypeTraitExpr"}
POINTER_QUALIFIERS = re.compile(r"\b(?:const|volatile|restrict)\b")


def _type(node):
    info = node.get("type", {})
    return info.get("desugaredQualType") or info.get("qualType", "")


def _pointee(node):
    spelling = POINTER_QUALIFIERS.sub("", _type(node)).strip()
    if spelling.count("*") != 1 or not spelling.endswith("*"):
        return None
    spelling = " ".join(spelling[:-1].split())
    if spelling.startswith("_Atomic(") and spelling.endswith(")"):
        return spelling[len("_Atomic("):-1].strip()
    return spelling


def _pointer_expression(node):
    while len(node.get("inner", [])) == 1:
        child = node["inner"][0]
        kind = node.get("kind")
        cast = node.get("castKind")
        if kind == "ParenExpr" or (kind == "ImplicitCastExpr" and cast == "LValueToRValue"):
            node = child
        elif kind in {"ImplicitCastExpr", "CStyleCastExpr"} and cast in {"NoOp", "BitCast"}:
            if _pointee(node) is None or _pointee(node) != _pointee(child):
                break
            node = child
        else:
            break
    return node


def _reference(node):
    node = _pointer_expression(node)
    if node.get("kind") == "DeclRefExpr":
        return node.get("referencedDecl", {}).get("id")
    return None


def _assignment(node):
    return node.get("kind") == "BinaryOperator" and node.get("opcode") == "=" and len(node.get("inner", [])) == 2


def _initializer(node):
    if node.get("kind") == "VarDecl" and node.get("init"):
        return next((child for child in node.get("inner", []) if not child.get("kind", "").endswith("Attr")), None)
    return None


def _site(location):
    return {key: location[key] for key in ("file", "line", "column")}


class FunctionBindings:
    def __init__(self, analyzer, tu, unit, function, fields, add, direct_calls):
        self.analyzer = analyzer
        self.tu = tu
        self.identity = (unit, analyzer.function_references[tu, function["id"]])
        self.function = function
        self.fields = fields
        self.add = add
        self.direct_calls = direct_calls
        self.parameters = [node for node in function.get("inner", []) if node.get("kind") == "ParmVarDecl"]
        self.locals = {parameter["id"]: parameter for parameter in self.parameters}
        self.writes = Counter()
        self.escaped = set()

    def location(self, node):
        location = self.analyzer.location(node, self.tu)
        return _site(location) if location else None

    def collect(self, body):
        # A unique definition can enter nested blocks only when its storage
        # cannot be modified by an indirect write or a jump around the definition.
        for node in iter_nodes(body):
            kind = node.get("kind")
            if kind in UNSUPPORTED_JUMPS:
                return False
            if kind == "VarDecl":
                self.locals[node["id"]] = node
                if _initializer(node) is not None:
                    self.writes[node["id"]] += 1
            elif _assignment(node) or kind == "CompoundAssignOperator" or (
                kind == "UnaryOperator" and node.get("opcode") in {"++", "--"}
            ):
                target = _reference(node["inner"][0])
                if target:
                    self.writes[target] += 1
            elif kind == "UnaryOperator" and node.get("opcode") == "&":
                target = _reference(node["inner"][0])
                if target:
                    self.escaped.add(target)
        origins = {}
        for index, parameter in enumerate(self.parameters):
            identity = parameter["id"]
            if not self.stable_pointer(identity, 0):
                continue
            location = self.location(parameter)
            if location and parameter.get("name"):
                origins[identity] = {"kind": "parameter_pointer", "name": parameter["name"],
                                     "parameter_index": index, "declaration": location, "alias_steps": []}
        self.visit(body, {}, origins)
        return True

    def storage(self, identity):
        local = self.locals.get(identity)
        if local and local.get("storageClass") not in {"extern", "static"}:
            declaration = self.location(local)
            if declaration:
                return {"kind": "automatic_storage", "name": local["name"], "declaration": declaration}
        global_id = self.analyzer.global_references.get((self.tu, identity))
        obj = self.analyzer.objects.get(global_id)
        if obj and global_id not in self.analyzer.global_binding_exclusions:
            return {"kind": "global_storage", "name": obj["name"],
                    "declaration": _site(obj["declaration"]), "object_id": global_id}
        return None

    def origin(self, expression, origins):
        node = _pointer_expression(expression)
        if not _pointee(node):
            return None
        if node.get("kind") == "UnaryOperator" and node.get("opcode") == "&":
            identity = _reference(node["inner"][0])
            storage = self.storage(identity) if identity else None
            return dict(storage, alias_steps=[]) if storage else None
        return origins.get(_reference(node))

    def field_address(self, node, origins):
        children = node.get("inner", [])
        if node.get("kind") != "MemberExpr" or len(children) != 1:
            return None
        field = self.fields.get(node.get("referencedMemberDecl"))
        base_id = _reference(children[0])
        capture = self.location(node)
        if not field or not base_id or not capture:
            return None

        local = self.locals.get(base_id)
        origin = None
        if node.get("isArrow"):
            if not local or local.get("storageClass") in {"extern", "static"}:
                return None
            kind = "parameter_pointer" if local.get("kind") == "ParmVarDecl" else "local_pointer"
            declaration = self.location(local)
            if not declaration:
                return None
            receiver = {"kind": kind, "name": local["name"], "declaration": declaration, "capture": capture}
            origin = origins.get(base_id)
        else:
            receiver = self.storage(base_id)
            if not receiver:
                return None
        return field, receiver, capture, (), origin

    def address(self, expression, aliases, origins):
        node = _pointer_expression(expression)
        if node.get("kind") == "UnaryOperator" and node.get("opcode") == "&":
            member = node["inner"][0]
            while member.get("kind") == "ParenExpr":
                member = member["inner"][0]
            return self.field_address(member, origins)
        return aliases.get(_reference(node))

    def stable_pointer(self, identity, writes):
        local = self.locals.get(identity)
        return (local and local.get("storageClass") not in {"extern", "static"}
                and identity not in self.escaped and self.writes[identity] == writes
                and "volatile" not in _type(local).split() and _pointee(local))

    def define(self, identity, expression, statement, aliases, origins):
        if not self.stable_pointer(identity, 1) or self.locals[identity].get("kind") != "VarDecl":
            return
        location = self.location(statement)
        if not location:
            return
        binding = self.address(expression, aliases, origins)
        if binding and len(binding[3]) < MAX_ALIAS_STEPS:
            field, receiver, capture, steps, origin = binding
            aliases[identity] = (field, receiver, capture, (*steps, location), origin)
        source = self.origin(expression, origins)
        if source and len(source["alias_steps"]) < MAX_ALIAS_STEPS:
            origins[identity] = dict(source, alias_steps=[*source["alias_steps"], location])

    def record_call(self, children, location, origins):
        target = self.analyzer.function_target(children[0], self.tu)
        if not target:
            return
        arguments = []
        for index, expression in enumerate(children[1:]):
            source = self.origin(expression, origins)
            argument_location = self.location(expression)
            if source and argument_location:
                arguments.append((index, argument_location, source))
        if arguments:
            self.direct_calls.calls.append((self.identity, target, location, arguments))

    def visit(self, node, aliases, origins, parents=None):
        kind = node.get("kind")
        children = node.get("inner", [])
        if kind in SKIP_EXPRESSIONS:
            return
        if kind == "CompoundStmt":
            # Definitions in a conditional or loop body stay within that body.
            aliases = dict(aliases)
            origins = dict(origins)
            for statement in children:
                if statement.get("kind") == "DeclStmt":
                    for declaration in statement.get("inner", []):
                        self.visit(declaration, aliases, origins, (statement, (node, parents)))
                        initializer = _initializer(declaration)
                        if initializer is not None:
                            self.define(declaration["id"], initializer, declaration, aliases, origins)
                else:
                    self.visit(statement, aliases, origins, (node, parents))
                    if _assignment(statement):
                        left, right = statement["inner"]
                        self.define(_reference(left), right, statement, aliases, origins)
            return

        location = self.location(node) if kind in {"AtomicExpr", "UnaryOperator", "CallExpr"} else None
        if location and children:
            if kind == "AtomicExpr":
                _, operation = atomic_opcode(node, location, self.analyzer.sources)
                if operation:
                    self.record(children[0], aliases, origins, location, "atomic_target", operation)
            elif kind == "CallExpr":
                self.record_call(children, location, origins)
            elif node.get("opcode") == "*":
                access = expression_access(node, parents)
                if access in {"read", "write", "read_write"}:
                    self.record(children[0], aliases, origins, location, access)
        for child in children:
            self.visit(child, aliases, origins, (node, parents))

    def record(self, expression, aliases, origins, location, access, operation=None):
        binding = self.address(expression, aliases, origins)
        if not binding:
            return
        field, receiver, capture, steps, origin = binding
        item = {"function": self.function["name"], "location": location, "access": access,
                "field": field, "receiver": receiver, "address_formation": capture, "alias_steps": list(steps)}
        if receiver["kind"] == "global_storage":
            item["field_object_id"] = field_object_id(receiver["object_id"], field["id"])
        if operation:
            item["operation"] = operation
        access_id = self.add(item)
        if access_id and origin and origin["kind"] == "parameter_pointer":
            self.direct_calls.seeds.append((access_id, self.identity, origin))


def collect_field_bindings(asts, analyzer, direct_calls):
    records, evidence = {}, {}
    counts = Counter()
    skipped = set()
    truncated = False

    def add(item):
        nonlocal truncated
        identity = "sync_field_access_" + stable_digest(item)[:20]
        if identity in records:
            return identity
        path = item["location"]["file"]
        if counts[path] >= MAX_PER_FILE or len(records) >= MAX_BINDINGS:
            truncated = True
            return
        sites = [("field_declaration", item["field"]["declaration"]), ("field_access", item["location"]),
                 ("receiver_declaration", item["receiver"]["declaration"]), ("address_formation", item["address_formation"])]
        sites.extend(("local_pointer_definition", location) for location in item["alias_steps"])
        ids = []
        for kind, location in sites:
            key = "ev_ast_field_binding_" + stable_digest([kind, location])[:20]
            evidence[key] = {"id": key, "kind": kind, "location": location, "role": "supports"}
            ids.append(key)
        records[identity] = dict(item, id=identity, evidence_ids=ids)
        counts[path] += 1
        return identity

    for unit, (tu, ast) in enumerate(asts):
        if not tu.endswith(".c"):
            continue
        fields = {}
        functions = []
        for node in iter_nodes(ast):
            if node.get("kind") == "RecordDecl" and node.get("tagUsed") == "struct":
                for child in node.get("inner", []):
                    if child.get("kind") != "FieldDecl" or not child.get("name") or child.get("isBitfield"):
                        continue
                    location = analyzer.location(child, tu)
                    if location:
                        field = {"name": child["name"], "type": _type(child), "declaration": _site(location)}
                        fields[child["id"]] = dict(field, id="sync_field_decl_" + stable_digest(field)[:20])
            elif node.get("kind") == "FunctionDecl":
                body = next((child for child in node.get("inner", []) if child.get("kind") == "CompoundStmt"), None)
                identity = analyzer.function_references[tu, node["id"]]
                location = analyzer.location(node, tu) if body is not None else None
                attrs = {child.get("kind") for child in node.get("inner", [])}
                direct_calls.declare(unit, identity, node.get("name"), _site(location) if location else None, attrs)
                if location:
                    functions.append((node, body))
        for function, body in functions:
            if not FunctionBindings(analyzer, tu, unit, function, fields, add, direct_calls).collect(body):
                location = analyzer.location(function, tu)
                skipped.add((location["file"], location["line"], function["name"]))
    direct_calls.build()
    call_facts = direct_calls.collect(records, evidence)
    return {"schema_version": "sync-field-bindings/1", "analysis_basis": "intraprocedural-clang-json",
            "accesses": list(records.values()), "evidence": list(evidence.values()),
            "direct_calls": call_facts,
            "coverage": {"scope": "single-definition local pointers in structured C function bodies",
                         "functions_skipped_for_jumps_or_asm": len(skipped), "truncated": truncated,
                         "limits": {"accesses": MAX_BINDINGS, "per_file": MAX_PER_FILE, "alias_steps": MAX_ALIAS_STEPS}}}
