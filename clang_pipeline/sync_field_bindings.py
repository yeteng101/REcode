"""Bounded, intraprocedural C pointer-to-field provenance."""
from __future__ import annotations

import re
from collections import Counter

from .clang_ast import iter_nodes, stable_digest
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
    def __init__(self, analyzer, tu, function, fields, add):
        self.analyzer = analyzer
        self.tu = tu
        self.function = function
        self.fields = fields
        self.add = add
        self.locals = {p["id"]: p for p in function.get("inner", []) if p.get("kind") == "ParmVarDecl"}
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
        self.visit(body, {})
        return True

    def field_address(self, node):
        children = node.get("inner", [])
        if node.get("kind") != "MemberExpr" or len(children) != 1:
            return None
        field = self.fields.get(node.get("referencedMemberDecl"))
        base_id = _reference(children[0])
        capture = self.location(node)
        if not field or not base_id or not capture:
            return None

        local = self.locals.get(base_id)
        if node.get("isArrow"):
            if not local or local.get("storageClass") in {"extern", "static"}:
                return None
            kind = "parameter_pointer" if local.get("kind") == "ParmVarDecl" else "local_pointer"
            declaration = self.location(local)
            if not declaration:
                return None
            receiver = {"kind": kind, "name": local["name"], "declaration": declaration, "capture": capture}
        elif local and local.get("storageClass") not in {"extern", "static"}:
            declaration = self.location(local)
            if not declaration:
                return None
            receiver = {"kind": "automatic_storage", "name": local["name"], "declaration": declaration}
        else:
            global_id = self.analyzer.global_references.get((self.tu, base_id))
            obj = self.analyzer.objects.get(global_id)
            if not obj or global_id in self.analyzer.global_binding_exclusions:
                return None
            receiver = {"kind": "global_storage", "name": obj["name"],
                        "declaration": _site(obj["declaration"]), "object_id": global_id}
        return field, receiver, capture, ()

    def address(self, expression, aliases):
        node = _pointer_expression(expression)
        if node.get("kind") == "UnaryOperator" and node.get("opcode") == "&":
            member = node["inner"][0]
            while member.get("kind") == "ParenExpr":
                member = member["inner"][0]
            return self.field_address(member)
        return aliases.get(_reference(node))

    def define(self, identity, expression, statement, aliases):
        local = self.locals.get(identity)
        if (not local or local.get("kind") != "VarDecl" or local.get("storageClass") in {"extern", "static"}
                or identity in self.escaped or self.writes[identity] != 1
                or "volatile" in _type(local).split() or not _pointee(local)):
            return
        binding = self.address(expression, aliases)
        location = self.location(statement)
        if binding and location and len(binding[3]) < MAX_ALIAS_STEPS:
            field, receiver, capture, steps = binding
            aliases[identity] = (field, receiver, capture, (*steps, location))

    def visit(self, node, aliases, parents=None):
        kind = node.get("kind")
        children = node.get("inner", [])
        if kind in SKIP_EXPRESSIONS:
            return
        if kind == "CompoundStmt":
            # Definitions in a conditional or loop body stay within that body.
            aliases = dict(aliases)
            for statement in children:
                if statement.get("kind") == "DeclStmt":
                    for declaration in statement.get("inner", []):
                        self.visit(declaration, aliases, (statement, (node, parents)))
                        initializer = _initializer(declaration)
                        if initializer is not None:
                            self.define(declaration["id"], initializer, declaration, aliases)
                else:
                    self.visit(statement, aliases, (node, parents))
                    if _assignment(statement):
                        left, right = statement["inner"]
                        self.define(_reference(left), right, statement, aliases)
            return

        location = self.location(node) if kind in {"AtomicExpr", "UnaryOperator"} else None
        if location and children:
            if kind == "AtomicExpr":
                _, operation = atomic_opcode(node, location, self.analyzer.sources)
                if operation:
                    self.record(children[0], aliases, location, "atomic_target", operation)
            elif node.get("opcode") == "*":
                access = expression_access(node, parents)
                if access in {"read", "write", "read_write"}:
                    self.record(children[0], aliases, location, access)
        for child in children:
            self.visit(child, aliases, (node, parents))

    def record(self, expression, aliases, location, access, operation=None):
        binding = self.address(expression, aliases)
        if not binding:
            return
        field, receiver, capture, steps = binding
        item = {"function": self.function["name"], "location": location, "access": access,
                "field": field, "receiver": receiver, "address_formation": capture, "alias_steps": list(steps)}
        if receiver["kind"] == "global_storage":
            item["field_object_id"] = "sync_field_" + stable_digest([receiver["object_id"], field["id"]])[:20]
        if operation:
            item["operation"] = operation
        self.add(item)


def collect_field_bindings(asts, analyzer):
    records, evidence = {}, {}
    counts = Counter()
    skipped = set()
    truncated = False

    def add(item):
        nonlocal truncated
        identity = "sync_field_access_" + stable_digest(item)[:20]
        if identity in records:
            return
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

    for tu, ast in asts:
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
            elif node.get("kind") == "FunctionDecl" and analyzer.location(node, tu):
                body = next((child for child in node.get("inner", []) if child.get("kind") == "CompoundStmt"), None)
                if body is not None:
                    functions.append((node, body))
        if fields:
            for function, body in functions:
                if not FunctionBindings(analyzer, tu, function, fields, add).collect(body):
                    location = analyzer.location(function, tu)
                    skipped.add((location["file"], location["line"], function["name"]))
    return {"schema_version": "sync-field-bindings/1", "analysis_basis": "intraprocedural-clang-json",
            "accesses": list(records.values()), "evidence": list(evidence.values()),
            "coverage": {"scope": "single-definition local pointers in structured C function bodies",
                         "functions_skipped_for_jumps_or_asm": len(skipped), "truncated": truncated,
                         "limits": {"accesses": MAX_BINDINGS, "per_file": MAX_PER_FILE, "alias_steps": MAX_ALIAS_STEPS}}}
