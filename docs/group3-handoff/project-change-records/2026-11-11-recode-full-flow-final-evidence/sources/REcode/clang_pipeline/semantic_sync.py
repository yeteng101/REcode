"""Bounded, interprocedural synchronization facts from compiler JSON ASTs.

Object references use compiler declaration IDs. Structured paths retain separate
lock states and thread lifetimes. Unsupported constructs and exhausted budgets
are exported as gaps, and prevent negative concurrency/protection proofs.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .clang_ast import read_json, run_clang_ast, stable_digest, unwrap_expr


CREATE_APIS = {"pthread_create": 2, "thrd_create": 1}
JOIN_APIS = {"pthread_join", "thrd_join"}
ACQUIRE_APIS = {"pthread_mutex_lock": "exclusive", "pthread_rwlock_rdlock": "read", "pthread_rwlock_wrlock": "write"}
RELEASE_APIS = {"pthread_mutex_unlock", "pthread_rwlock_unlock"}
TERMINATE_APIS = {"abort", "exit", "_Exit", "_exit", "__assert_fail"}
ALLOCATION_APIS = {"malloc", "calloc", "realloc", "free"}
WRAPPERS = {"ImplicitCastExpr", "CStyleCastExpr", "ParenExpr", "ExprWithCleanups", "ConstantExpr", "FullExpr"}
NONZERO = ("nonzero",)


@dataclass
class PathState:
    environment: dict = field(default_factory=dict)
    held: dict = field(default_factory=dict)
    initialized: set = field(default_factory=set)
    active: set = field(default_factory=set)
    instance: str = "main"
    phase: tuple = ()
    returned: bool = False
    result: Any = None
    terminated: bool = False

    def clone(self):
        return copy.deepcopy(self)


class SemanticSyncAnalyzer:
    def __init__(self, source_root: Path, units: list[dict], *, max_paths=64, max_call_depth=12, max_steps=50000):
        self.root = source_root.resolve()
        self.units = units
        self.max_paths, self.max_call_depth, self.max_steps = max_paths, max_call_depth, max_steps
        self.steps = 0
        self.gaps = set()
        self.call_gaps = set()
        self.locations = {}
        self.sources = {}
        self.declarations = {}
        self.functions = {}
        self.function_references = {}
        self.global_references = {}
        self.objects = {}
        self.locks = {}
        self.evidence = {}
        self.accesses = {}
        self.spawns = {}
        self.contexts = set()
        self.overlaps = set()
        self.thread_events = {}
        self.call_nodes = {}
        self.call_edges = {}
        self.translation_units = []

    def gap(self, reason, node=None, tu=None):
        location = self.location(node, tu) if node and tu else None
        suffix = (":" + location["file"] + ":" + str(location["line"])) if location else ""
        self.gaps.add(reason + suffix)

    def location(self, node, tu):
        return self.locations.get((tu, id(node)))

    def locate_tree(self, node, tu, inherited):
        loc = node.get("loc") or (node.get("range") or {}).get("begin") or {}
        loc = loc.get("expansionLoc", loc)
        filename = loc.get("file", inherited)
        path = Path(filename)
        if not path.is_absolute():
            path = self.root / path
        try:
            relative = path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            relative = None
        if relative and ("offset" in loc or loc.get("line")):
            try:
                if relative not in self.sources:
                    self.sources[relative] = path.read_bytes()
                data = self.sources[relative]
                line = loc.get("line") or data.count(b"\n", 0, loc.get("offset", 0)) + 1
                lines = data.decode("utf-8", errors="replace").splitlines()
                self.locations[(tu, id(node))] = {"file": relative, "line": line,
                    "column": loc.get("col", 1), "snippet": lines[line - 1].strip() if 0 < line <= len(lines) else ""}
            except OSError:
                pass
        current = filename
        for child in node.get("inner", []):
            # Clang elides repeated source file names across sibling declarations.
            child_loc = child.get("loc") or (child.get("range") or {}).get("begin") or {}
            child_loc = child_loc.get("expansionLoc", child_loc)
            current = child_loc.get("file", current)
            self.locate_tree(child, tu, current)

    def evidence_for(self, kind, location, detail=""):
        identity = "ev_ast_" + stable_digest([kind, location, detail])[:16]
        self.evidence[identity] = {"id": identity, "kind": kind, "location": location, "role": "supports"}
        return [identity]

    def canonical(self, node, tu, prefix):
        name = node.get("mangledName") or node.get("name", "")
        linkage = ["internal", tu, name] if node.get("storageClass") == "static" else ["external", name]
        return prefix + stable_digest(linkage)[:20]

    def index(self, asts):
        globals_by_key = {}
        for tu, ast in asts:
            self.locate_tree(ast, tu, tu)
            self.index_declarations(ast, tu, globals_by_key)
        for key, declarations in globals_by_key.items():
            definitions = {(tu, self.location(node, tu)["file"], self.location(node, tu)["line"]): (tu, node)
                           for tu, node in declarations if node.get("storageClass") != "extern" or node.get("inner")}
            # External definitions included through a header refer to one source
            # declaration. Static header variables remain TU-specific by key.
            sites = {(file, line) for _, file, line in definitions}
            if len(sites) != 1:
                continue
            tu, node = next(iter(definitions.values()))
            location = self.location(node, tu)
            type_name = node.get("type", {}).get("qualType", "")
            obj = {"object_id": key, "name": node["name"], "type": type_name,
                   "canonical_type": node.get("type", {}).get("desugaredQualType", type_name),
                   "declaration": location, "identity_confidence": 1.0,
                   "evidence_ids": self.evidence_for("ast_object_declaration", location, key)}
            self.objects[key] = obj
            if type_name in {"pthread_mutex_t", "pthread_rwlock_t"}:
                source = self.source_fragment(node, tu)
                initializer = "PTHREAD_MUTEX_INITIALIZER" if type_name == "pthread_mutex_t" else "PTHREAD_RWLOCK_INITIALIZER"
                self.locks[key] = {"lock_id": key, "name": node["name"],
                    "kind": "mutex" if type_name == "pthread_mutex_t" else "rwlock",
                    "declaration": location, "statically_initialized": initializer in source,
                    "operations": [], "evidence_ids": obj["evidence_ids"]}
        for tu, ast in asts:
            self.index_calls(ast, tu, None)

    def index_declarations(self, node, tu, globals_by_key, owner=None):
        kind = node.get("kind")
        if kind in {"FunctionDecl", "CXXMethodDecl"}:
            key = self.canonical(node, tu, "fn_ast_")
            self.function_references[(tu, node.get("id"))] = key
            location = self.location(node, tu)
            body = next((c for c in node.get("inner", []) if c.get("kind") == "CompoundStmt"), None)
            if body is not None and location:
                if key in self.functions and self.functions[key]["location"] != location:
                    self.call_gaps.add("ambiguous_function_definition:" + key)
                self.functions[key] = {"node": node, "body": body, "tu": tu, "location": location,
                    "name": node.get("name", ""), "parameters": [c for c in node.get("inner", []) if c.get("kind") == "ParmVarDecl"]}
            self.call_nodes.setdefault(key, {"id": key, "name": node.get("name", ""), "kind": "external"})
            if body is not None and location:
                self.call_nodes[key].update(kind="function", **location)
            owner = key
        elif kind == "VarDecl" and owner is None:
            key = self.canonical(node, tu, "object_ast_")
            self.global_references[(tu, node.get("id"))] = key
            if self.location(node, tu):
                globals_by_key.setdefault(key, []).append((tu, node))
            if self.location(node, tu) and any(c.get("kind") in {"AliasAttr", "WeakAttr"} for c in node.get("inner", [])):
                self.gap("aliased_or_weak_storage_unsupported", node, tu)
        elif kind == "VarDecl" and node.get("storageClass") == "static" and self.location(node, tu):
            self.gap("static_local_storage_unsupported", node, tu)
        for child in node.get("inner", []):
            if child.get("kind") in {"ConstructorAttr", "DestructorAttr", "AliasAttr", "IFuncAttr"} and self.location(node, tu):
                self.gap("implicit_entry_or_symbol_alias_unsupported", node, tu)
            self.index_declarations(child, tu, globals_by_key, owner)

    def source_fragment(self, node, tu):
        location = self.location(node, tu)
        if not location:
            return ""
        bounds = node.get("range") or {}
        begin, end = bounds.get("begin", {}), bounds.get("end", {})
        begin, end = begin.get("expansionLoc", begin), end.get("expansionLoc", end)
        data = self.sources[location["file"]]
        return data[begin.get("offset", 0):end.get("offset", 0) + end.get("tokLen", 1)].decode("utf-8", errors="replace")

    def function_target(self, callee, tu):
        callee = unwrap_expr(callee)
        if callee and callee.get("kind") == "UnaryOperator" and callee.get("opcode") == "&":
            callee = unwrap_expr(callee.get("inner", [None])[0])
        reference = (callee or {}).get("referencedDecl") or {}
        if reference.get("kind") in {"FunctionDecl", "CXXMethodDecl"}:
            return self.function_references.get((tu, reference.get("id")))
        return None

    def index_calls(self, node, tu, owner):
        if node.get("kind") in {"FunctionDecl", "CXXMethodDecl"}:
            owner = self.function_references.get((tu, node.get("id")))
        if node.get("kind") == "CallExpr" and owner in self.functions:
            location = self.location(node, tu)
            target = self.function_target(node.get("inner", [None])[0], tu)
            if target is None:
                self.call_gaps.add("unresolved_call_target:" + str(location))
            elif location:
                key = "ast_call_" + stable_digest([owner, target, location])[:16]
                self.call_edges[key] = {"id": key, "source": owner, "target": target, "kind": "direct_call",
                    "call_site": location, "confidence": 1.0,
                    "evidence_ids": self.evidence_for("ast_call_site", location, target)}
        for child in node.get("inner", []):
            self.index_calls(child, tu, owner)

    def bounded(self, paths):
        if len(paths) > self.max_paths:
            self.gap("path_budget_exhausted")
        return paths[:self.max_paths]

    def context(self, state):
        identity = state.instance
        if identity == "main":
            identity += ":" + stable_digest(state.phase)[:12]
        self.contexts.add(identity)
        for active in state.active:
            if active != identity:
                self.overlaps.add(tuple(sorted((identity, active))))
        return identity

    def lvalue(self, node, state, tu):
        node = unwrap_expr(node)
        if not node:
            return None
        if node.get("kind") == "DeclRefExpr":
            reference = node.get("referencedDecl") or {}
            identity = (tu, reference.get("id"))
            key = self.global_references.get(identity)
            if key:
                if key not in self.objects:
                    self.gap("global_definition_ambiguous_or_unavailable", node, tu)
                    return None
                return ("global", key)
            if reference.get("kind") in {"VarDecl", "ParmVarDecl"}:
                return ("local", identity)
        if node.get("kind") == "UnaryOperator" and node.get("opcode") == "*":
            child = unwrap_expr(node.get("inner", [None])[0])
            if child and child.get("kind") == "DeclRefExpr":
                target = self.lvalue(child, state, tu)
                value = state.environment.get(target[1]) if target and target[0] == "local" else None
                if isinstance(value, tuple) and value[0] == "address":
                    return value[1]
        self.gap("unresolved_memory_reference", node, tu)
        return None

    def emit(self, node, target, kind, state, tu, stack):
        if not target or target[0] != "global":
            return
        obj = self.objects[target[1]]
        if target[1] in self.locks:
            self.gap("lock_object_used_as_payload", node, tu)
            return
        canonical_type = obj["canonical_type"]
        if "_Atomic" in canonical_type or "[" in canonical_type or canonical_type.startswith(("struct ", "union ")):
            self.gap("aggregate_or_atomic_payload_unsupported", node, tu)
        location = self.location(node, tu)
        if not location:
            self.gap("access_location_unavailable")
            return
        context = self.context(state)
        key = "ast_access_" + stable_digest([target, location, kind, context, stack])[:20]
        lockset = {identity: mode for identity, mode in state.held.items()}
        ids = set(obj["evidence_ids"] + self.evidence_for("ast_shared_access", location, key))
        for lock in lockset:
            ids.update(self.locks[lock]["evidence_ids"])
        existing = self.accesses.get(key)
        if existing:
            existing["must"] = {lock: mode for lock, mode in existing["must"].items() if lockset.get(lock) == mode}
            existing["may"].update(lockset)
            existing["evidence_ids"] = sorted(set(existing["evidence_ids"]) | ids)
        else:
            function = self.functions[stack[-1]]["name"]
            self.accesses[key] = {"access_id": key, "object_id": target[1], "kind": kind, "function": function,
                "location": location, "context_id": context, "must": lockset, "may": dict(lockset),
                "confidence": 1.0, "evidence_ids": sorted(ids)}

    def statement(self, node, states, tu, stack):
        self.steps += 1
        if self.steps > self.max_steps:
            self.gap("step_budget_exhausted")
            return states
        result = []
        for state in states:
            if state.returned or state.terminated:
                result.append(state)
                continue
            kind = node.get("kind")
            children = node.get("inner", [])
            if kind in {"CompoundStmt", "DeclStmt"}:
                paths = [state]
                for child in children:
                    paths = self.statement(child, paths, tu, stack)
                result.extend(paths)
            elif kind == "IfStmt" and len(children) in {2, 3} and not node.get("hasInit"):
                for branch, condition in self.evaluate(children[0], state, tu, stack):
                    choices = (True, False) if condition is None else (bool(condition),)
                    for choice in choices:
                        paths = [branch.clone()]
                        if choice or len(children) == 3:
                            paths = self.statement(children[1 if choice else 2], paths, tu, stack)
                        result.extend(paths)
            elif kind == "ReturnStmt":
                values = self.evaluate(children[0], state, tu, stack) if children else [(state, None)]
                for path, value in values:
                    path.returned, path.result = True, value
                    result.append(path)
            elif kind in {"ForStmt", "WhileStmt", "DoStmt", "SwitchStmt", "GotoStmt", "IndirectGotoStmt", "LabelStmt", "BreakStmt", "ContinueStmt", "CXXTryStmt"}:
                self.gap("unsupported_control_flow:" + kind, node, tu)
                state.held.clear()
                result.append(state)
            else:
                result.extend(path for path, _ in self.evaluate(node, state, tu, stack))
        return self.bounded(result)

    def evaluate(self, node, state, tu, stack):
        kind = node.get("kind")
        children = node.get("inner", [])
        if kind in WRAPPERS and len(children) == 1:
            return self.evaluate(children[0], state, tu, stack)
        if kind in {"IntegerLiteral", "CharacterLiteral", "CXXBoolLiteralExpr"}:
            try:
                return [(state, int(node.get("value", 0)))]
            except (ValueError, TypeError):
                return [(state, None)]
        if kind in {"StringLiteral", "FloatingLiteral", "GNUNullExpr", "CXXNullPtrLiteralExpr", "UnaryExprOrTypeTraitExpr", "NullStmt"}:
            return [(state, None)]
        if kind == "DeclRefExpr":
            function = self.function_target(node, tu)
            if function:
                return [(state, ("function", function))]
            target = self.lvalue(node, state, tu)
            self.emit(node, target, "read", state, tu, stack)
            value = state.environment.get(target[1]) if target and target[0] == "local" else None
            return [(state, value)]
        if kind == "VarDecl":
            if node.get("storageClass") == "static":
                self.gap("static_local_storage_unsupported", node, tu)
            values = self.evaluate(children[-1], state, tu, stack) if children else [(state, None)]
            for path, value in values:
                path.environment[(tu, node.get("id"))] = value
            return values
        if kind == "UnaryOperator":
            opcode = node.get("opcode")
            if opcode == "&":
                function = self.function_target(node, tu)
                if function:
                    return [(state, ("function", function))]
                return [(state, ("address", self.lvalue(children[0], state, tu)))]
            if opcode in {"++", "--"}:
                target = self.lvalue(children[0], state, tu)
                self.emit(node, target, "write", state, tu, stack)
                if target and target[0] == "local":
                    state.environment[target[1]] = None
                return [(state, None)]
            if opcode == "*":
                target = self.lvalue(node, state, tu)
                self.emit(node, target, "read", state, tu, stack)
                return [(state, None)]
            values = self.evaluate(children[0], state, tu, stack)
            return [(path, int(not value) if opcode == "!" and value is not None else None) for path, value in values]
        if kind in {"BinaryOperator", "CompoundAssignOperator"} and len(children) == 2:
            opcode = node.get("opcode", "")
            if opcode not in {",", "&&", "||", "=", "!=", "=="} and any(self.has_side_effect(child) for child in children):
                self.gap("unsequenced_expression_effects", node, tu)
            if opcode in {"!=", "=="} and all(unwrap_expr(child).get("kind") != "IntegerLiteral" for child in children) and any(self.has_side_effect(child) for child in children):
                self.gap("unsequenced_expression_effects", node, tu)
            if opcode == "=" and unwrap_expr(children[0]).get("kind") != "DeclRefExpr" and self.has_side_effect(children[1]):
                self.gap("unsequenced_indirect_assignment", node, tu)
            if opcode == "=" or kind == "CompoundAssignOperator":
                values = self.evaluate(children[1], state, tu, stack)
                for path, value in values:
                    target = self.lvalue(children[0], path, tu)
                    self.emit(node, target, "write", path, tu, stack)
                    if target and target[0] == "local":
                        path.environment[target[1]] = value if opcode == "=" else None
                return [(path, None) for path, _ in values] if kind == "CompoundAssignOperator" else values
            result = []
            for left_state, left in self.evaluate(children[0], state, tu, stack):
                if opcode in {"&&", "||"}:
                    choices = (True, False) if left is None else (bool(left),)
                    for choice in choices:
                        branch = left_state.clone()
                        if choice == (opcode == "||"):
                            result.append((branch, int(choice)))
                        else:
                            result.extend((p, int(bool(v)) if v is not None else None)
                                          for p, v in self.evaluate(children[1], branch, tu, stack))
                    continue
                for path, right in self.evaluate(children[1], left_state, tu, stack):
                    value = None
                    if opcode == ",":
                        value = right
                    elif opcode in {"==", "!="} and left is not None and right is not None:
                        if left == NONZERO or right == NONZERO:
                            other = right if left == NONZERO else left
                            equal = False if other == 0 else None
                        else:
                            equal = left == right
                        value = None if equal is None else int(equal if opcode == "==" else not equal)
                    result.append((path, value))
            return self.bounded(result)
        if kind == "CallExpr":
            return self.call(node, state, tu, stack)
        if kind == "ConditionalOperator" and len(children) == 3:
            result = []
            for path, condition in self.evaluate(children[0], state, tu, stack):
                for choice in ((True, False) if condition is None else (bool(condition),)):
                    result.extend(self.evaluate(children[1 if choice else 2], path.clone(), tu, stack))
            return self.bounded(result)
        self.gap("unsupported_expression:" + str(kind), node, tu)
        state.held.clear()
        return [(state, None)]

    def invoke(self, function, state, arguments, stack):
        if function in stack or len(stack) >= self.max_call_depth:
            self.gap("recursive_or_deep_call:" + function)
            state.held.clear()
            return [(state, None)]
        record = self.functions[function]
        saved = dict(state.environment)
        for parameter, value in zip(record["parameters"], arguments):
            state.environment[(record["tu"], parameter.get("id"))] = value
        paths = self.statement(record["body"], [state], record["tu"], [*stack, function])
        result = []
        for path in paths:
            value = path.result if path.returned else None
            path.environment = {key: path.environment.get(key, value) for key, value in saved.items()}
            path.returned, path.result = False, None
            result.append((path, value))
        return result

    def has_side_effect(self, node):
        return (node.get("kind") in {"CallExpr", "CompoundAssignOperator"}
                or node.get("kind") == "UnaryOperator" and node.get("opcode") in {"++", "--"}
                or node.get("kind") == "BinaryOperator" and node.get("opcode") == "="
                or any(self.has_side_effect(child) for child in node.get("inner", [])))

    def call(self, node, state, tu, stack):
        children = node.get("inner", [])
        target = self.function_target(children[0], tu) if children else None
        if target is None:
            self.gap("unresolved_call_target", node, tu)
            state.held.clear()
            return [(state, None)]
        arguments = [(state, [])]
        if len(children) > 2 and any(self.has_side_effect(child) for child in children[1:]):
            self.gap("unsequenced_call_arguments", node, tu)
        for expression in children[1:]:
            next_arguments = []
            for path, values in arguments:
                next_arguments.extend((p, values + [value]) for p, value in self.evaluate(expression, path, tu, stack))
            arguments = self.bounded(next_arguments)
        name = self.call_nodes[target]["name"]
        result = []
        for path, values in arguments:
            if target in self.functions:
                result.extend(self.invoke(target, path, values, stack))
            elif name in CREATE_APIS or name in JOIN_APIS:
                result.extend(self.thread_call(name, node, values, path, tu, stack))
            elif name in ACQUIRE_APIS or name in RELEASE_APIS or name in {"pthread_mutex_init", "pthread_rwlock_init"}:
                result.extend(self.lock_call(name, node, values, path, tu, stack))
            elif name in TERMINATE_APIS:
                path.terminated = True
                result.append((path, None))
            elif name in ALLOCATION_APIS and not any(isinstance(v, tuple) and v[0] == "address" for v in values):
                result.append((path, None))
            else:
                self.gap("unmodeled_external_call:" + name, node, tu)
                path.held.clear()
                result.append((path, None))
        return self.bounded(result)

    def lock_call(self, name, node, values, state, tu, stack):
        value = values[0] if values else None
        target = value[1] if isinstance(value, tuple) and value[0] == "address" else None
        lock_id = target[1] if target and target[0] == "global" else None
        if lock_id not in self.locks:
            self.gap("unresolved_lock_identity", node, tu)
            state.held.clear()
            return [(state, None)]
        location = self.location(node, tu)
        lock = self.locks[lock_id]
        expected_kind = "rwlock" if name.startswith("pthread_rwlock_") else "mutex"
        if lock["kind"] != expected_kind:
            self.gap("lock_api_type_mismatch", node, tu)
            state.held.clear()
            return [(state, None)]
        kind = "acquire" if name in ACQUIRE_APIS else "release" if name in RELEASE_APIS else "initialize"
        operation_id = "ast_lock_op_" + stable_digest([lock_id, location, name])[:16]
        operation = {"operation_id": operation_id, "kind": kind, "api": name,
            "function": self.functions[stack[-1]]["name"], "location": location,
            "evidence_ids": self.evidence_for("ast_lock_operation", location, operation_id)}
        if not any(op["operation_id"] == operation_id for op in lock["operations"]):
            lock["operations"].append(operation)
        lock["evidence_ids"] = sorted(set(lock["evidence_ids"] + operation["evidence_ids"]))
        if kind == "initialize":
            if len(values) < 2 or values[1] != 0:
                self.gap("nondefault_lock_attributes", node, tu)
                return [(state, None)]
            success, failure = state.clone(), state.clone()
            success.initialized.add(lock_id)
            return [(success, 0), (failure, NONZERO)]
        if lock_id not in state.initialized:
            self.gap("lock_initialization_unproved", node, tu)
            return [(state, None)]
        if kind == "acquire":
            if state.held and lock_id not in state.held:
                self.gap("nested_lock_order_analysis_unavailable", node, tu)
            if lock_id in state.held:
                self.gap("reentrant_lock_acquisition", node, tu)
                state.held.pop(lock_id, None)
            else:
                state.held[lock_id] = ACQUIRE_APIS[name]
        elif lock_id not in state.held:
            self.gap("unlock_without_proven_ownership", node, tu)
        else:
            state.held.pop(lock_id)
        return [(state, 0)]

    def thread_call(self, name, node, values, state, tu, stack):
        location = self.location(node, tu)
        event_id = "ast_thread_" + stable_digest([name, location, stack, state.phase])[:16]
        evidence = self.evidence_for("ast_thread_event", location, event_id)
        self.thread_events[event_id] = {"id": event_id, "api": name, "location": location,
                                       "function": self.functions[stack[-1]]["name"], "evidence_ids": evidence}
        if state.instance != "main":
            self.gap("nested_thread_creation_or_join", node, tu)
            return [(state, None)]
        success, failure = state.clone(), state.clone()
        success.phase += (event_id + ":success",)
        failure.phase += (event_id + ":failure",)
        if name in CREATE_APIS:
            entry_index = CREATE_APIS[name]
            entry = values[entry_index] if len(values) > entry_index else None
            handle = values[0][1] if values and isinstance(values[0], tuple) and values[0][0] == "address" else None
            if not handle or handle[0] != "local" or not isinstance(entry, tuple) or entry[0] != "function" or entry[1] not in self.functions:
                self.gap("unresolved_thread_entry_or_handle", node, tu)
                return [(state, None)]
            instance = "thread:" + event_id
            argument = values[-1]
            if isinstance(argument, tuple) and argument[0] == "address" and (not argument[1] or argument[1][0] != "global"):
                self.gap("thread_argument_escapes_local_storage", node, tu)
            if instance in success.active:
                self.gap("repeated_thread_creation_site", node, tu)
            for active in success.active:
                self.overlaps.add(tuple(sorted((active, instance))))
            spawn = {"function": entry[1], "argument": argument,
                "initialized": set(success.initialized), "evidence_ids": evidence,
                "creation": location}
            if instance in self.spawns:
                previous = self.spawns[instance]
                spawn["initialized"] &= previous["initialized"]
                if previous["function"] != spawn["function"] or previous["argument"] != spawn["argument"]:
                    self.gap("ambiguous_thread_entry_or_argument", node, tu)
            self.spawns[instance] = spawn
            success.environment[handle[1]] = ("thread_handle", instance)
            failure.environment[handle[1]] = None
            success.active.add(instance)
        else:
            handle = values[0] if values else None
            if not isinstance(handle, tuple) or handle[0] != "thread_handle":
                self.gap("unresolved_join_handle", node, tu)
                return [(state, None)]
            success.active.discard(handle[1])
        return [(success, 0), (failure, NONZERO)]

    def analyze(self, asts):
        for tu, _ in asts:
            if not tu.endswith(".c"):
                self.gap("C_plus_plus_implicit_execution_unsupported:" + tu)
        for unit in self.units:
            arguments = [str(arg) for arg in unit.get("arguments", [])]
            if arguments and (Path(arguments[0]).name.endswith("++") or any("c++" in arg for arg in arguments if arg.startswith(("-std=", "-x")))):
                self.gap("C_plus_plus_compilation_unsupported")
        self.index(asts)
        mains = [key for key, value in self.functions.items() if value["name"] == "main"]
        if len(mains) != 1:
            self.gap("unique_main_entry_unavailable")
        else:
            initial = PathState(initialized={key for key, value in self.locks.items() if value["statically_initialized"]})
            self.invoke(mains[0], initial, [], [])
            for instance, spawn in list(self.spawns.items()):
                worker = PathState(instance=instance, initialized=set(spawn["initialized"]))
                paths = self.invoke(spawn["function"], worker, [spawn["argument"]], [])
                if any(path.held for path, _ in paths):
                    self.gap("thread_returns_holding_lock:" + instance)
        complete = not self.gaps
        public_objects = []
        for key, obj in self.objects.items():
            accesses = []
            for access in self.accesses.values():
                if access["object_id"] != key:
                    continue
                access = copy.deepcopy(access)
                context_id = access.pop("context_id")
                possible = [other for other in self.contexts if other != context_id and tuple(sorted((context_id, other))) in self.overlaps]
                excluded = [other for other in self.contexts if other != context_id and other not in possible] if complete else []
                creation = self.spawns.get(context_id)
                access["execution_context"] = {"context_id": context_id, "reentrant": False,
                    "may_run_concurrently_with": sorted(possible), "cannot_run_concurrently_with": sorted(excluded)}
                if creation:
                    access["execution_context"]["creation_site"] = creation["creation"]
                    access["evidence_ids"] = sorted(set(access["evidence_ids"] + creation["evidence_ids"]))
                must, may = access.pop("must"), access.pop("may")
                # Incomplete paths can contain unvisited unlocks and aliases.
                access["must_lockset"] = [{"lock_id": lock, "mode": mode} for lock, mode in sorted(must.items())] if complete else []
                access["may_lockset"] = [{"lock_id": lock, "mode": mode} for lock, mode in sorted(may.items())]
                access["other_synchronization"] = []
                accesses.append(access)
            if accesses:
                public_objects.append(dict(obj, accesses=accesses))
        finding = {"id": "recode-ast-sync", "kind": "sync_flow", "shared_objects": public_objects,
                   "locks": list(self.locks.values()), "lock_order_edges": [], "confidence": 1.0,
                   "callbacks_and_async_boundaries": list(self.thread_events.values()),
                   "evidence_ids": sorted(self.evidence)}
        return {"schema_version": "bounded-ast-sync/1", "analysis_basis": "clang-json-ast-paths",
                "scope": "Closed C executable, main and directly created threads, global scalar objects and default POSIX locks",
                "findings": [finding], "evidence": list(self.evidence.values()),
                "call_graph": {"nodes": list(self.call_nodes.values()), "edges": list(self.call_edges.values())},
                "coverage": {"complete": complete, "gaps": sorted(self.gaps)},
                "call_coverage": {"complete": not self.call_gaps, "gaps": sorted(self.call_gaps)},
                "translation_units": self.translation_units,
                "limits": {"max_paths": self.max_paths, "max_call_depth": self.max_call_depth, "max_steps": self.max_steps},
                "api_model": "POSIX default initialized nonrecursive mutex and bounded rwlock operations; thread API success/failure paths"}


def analyze_semantic_sync(workspace: str | Path, source_root: str | Path) -> dict:
    workspace, root = Path(workspace), Path(source_root)
    units = read_json(workspace / "compile_commands.json")
    analyzer = SemanticSyncAnalyzer(root, units)
    manifest_path = workspace / "01-index/ast/manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    paths = {file: workspace / "01-index/ast" / (stem + ".ast.json") for stem, file in manifest.items()}
    asts = []
    for unit in units:
        filename = str(unit["file"])
        path = Path(filename)
        if not path.is_absolute():
            path = Path(unit.get("directory", str(root))) / path
        try:
            relative = path.resolve().relative_to(root.resolve()).as_posix()
            cached = paths.get(relative)
            ast = read_json(cached) if cached and cached.exists() else None
            # Compact libclang ASTs omit storage classes and some operators.
            if ast is None or not str(ast.get("id", "")).startswith("0x"):
                ast = run_clang_ast(unit, root)
            asts.append((relative, ast))
            analyzer.translation_units.append({"file": relative, "ast_sha256": stable_digest(ast),
                "arguments_sha256": stable_digest(unit.get("arguments", unit.get("command", "")))})
        except Exception as exc:
            analyzer.gap("translation_unit_failed:" + filename + ":" + type(exc).__name__)
            analyzer.call_gaps.add("translation_unit_failed:" + filename)
    if not units:
        analyzer.gap("compile_commands_unavailable")
        analyzer.call_gaps.add("compile_commands_unavailable")
    return analyzer.analyze(asts)
