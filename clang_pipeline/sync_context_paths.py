"""Source call paths and bounded forwarding for C thread API arguments."""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass

from .clang_ast import is_fptr_qual_type, iter_nodes, unwrap_expr
from .sync_field_bindings import SKIP_EXPRESSIONS, UNSUPPORTED_JUMPS, _assignment, _initializer, _pointee, _reference, _site, _type


MAX_FORWARD_STEPS = 4
MAX_FORWARD_STATES = 256
MAX_PATH_STEPS = 8
MAX_PATH_NODES = 2048
MAX_DISPATCH_PATHS = 4


@dataclass(frozen=True)
class Origin:
    kind: str
    identity: object
    adapter_sites: tuple = ()


@dataclass(eq=False)
class Call:
    caller: tuple
    target: tuple | None
    name: str
    location: dict
    position: int | None
    callbacks: dict
    handles: dict


def location_key(location):
    return location["file"], location["line"], location["column"]


def _failure_guard(node):
    if node.get("kind") != "IfStmt" or len(node.get("inner", [])) != 2:
        return False
    condition, failure = node["inner"]
    if any(child.get("kind") == "CallExpr" for child in iter_nodes(condition)):
        return False
    if failure.get("kind") == "CompoundStmt":
        statements = failure.get("inner", [])
        failure = statements[-1] if statements else {}
    if failure.get("kind") != "CallExpr" or not failure.get("inner"):
        return False
    direct = unwrap_expr(failure["inner"][0])
    return (direct or {}).get("referencedDecl", {}).get("name") in {"abort", "__builtin_trap"}


def _linear_call(node):
    """Recognize direct statements and the first operation in a do/while(0) wrapper."""
    if node.get("kind") == "DoStmt":
        parts = node.get("inner", [])
        if len(parts) != 2 or parts[1].get("kind") != "IntegerLiteral" or parts[1].get("value") != "0":
            return None
        children = parts[0].get("inner", [])
        if parts[0].get("kind") != "CompoundStmt" or not children:
            return None
        if not all(_failure_guard(statement) for statement in children[1:]):
            return None
        node = children[0]
    if node.get("kind") == "DeclStmt" and len(node.get("inner", [])) == 1:
        node = _initializer(node["inner"][0])
    elif _assignment(node) and _reference(node["inner"][0]):
        node = node["inner"][1]
    node = unwrap_expr(node)
    return node if node and node.get("kind") == "CallExpr" else None


def _member_base(expression):
    node = unwrap_expr(expression)
    if node and node.get("kind") == "MemberExpr" and not node.get("isArrow") and len(node.get("inner", [])) == 1:
        return _reference(node["inner"][0]), node.get("referencedMemberDecl")
    return None, None


def _callback_expr(expression):
    while expression and len(expression.get("inner", [])) == 1:
        kind, cast = expression.get("kind"), expression.get("castKind")
        if kind == "ParenExpr" or (kind == "ImplicitCastExpr" and cast in {"LValueToRValue", "FunctionToPointerDecay", "NoOp"}):
            expression = expression["inner"][0]
        elif kind == "UnaryOperator" and expression.get("opcode") == "&":
            expression = expression["inner"][0]
        else:
            break
    return expression


class ContextPaths:
    def __init__(self, direct_calls, analyzer, create_apis, join_apis):
        self.direct_calls, self.analyzer = direct_calls, analyzer
        self.create_apis, self.join_apis = create_apis, join_apis
        self.sites = defaultdict(list)
        self.functions = defaultdict(set)
        self.outgoing = defaultdict(list)
        self.incoming = defaultdict(list)
        self.primitives = []
        self.limits = set()

    def add(self, caller, target, location, position, callbacks, handles):
        resolved = self.direct_calls.resolve(caller, target)
        name = self.analyzer.call_nodes[target]["name"]
        call = Call(caller, resolved, name, location, position, callbacks, handles)
        if resolved:
            self.outgoing[caller].append(call)
            self.incoming[resolved].append(call)
            self.sites[location_key(location)].append(call)
        elif target not in self.direct_calls.excluded and target not in self.direct_calls.definitions:
            if name in self.create_apis and 0 in handles and self.create_apis[name] in callbacks:
                self.primitives.append(("create", call, handles[0], callbacks[self.create_apis[name]]))
            elif name in self.join_apis and 0 in handles:
                self.primitives.append(("join", call, handles[0], None))

    def paths(self, start, targets, maximum):
        pending = deque([(start, (), (start,))])
        seen = {(start,)}
        depths = {start: 0}
        matches = []
        while pending:
            function, steps, nodes = pending.popleft()
            if matches and len(steps) > len(matches[0]):
                break
            if function in targets:
                matches.append(steps)
                if len(matches) >= maximum:
                    break
                continue
            if len(steps) >= MAX_PATH_STEPS:
                self.limits.add("call_depth")
                continue
            for call in self.outgoing[function]:
                next_depth = len(steps) + 1
                if depths.get(call.target, next_depth) < next_depth:
                    continue
                lineage = (*nodes, call.target)
                if lineage in seen:
                    continue
                if len(seen) >= MAX_PATH_NODES:
                    self.limits.add("path_nodes")
                    break
                seen.add(lineage)
                depths[call.target] = next_depth
                pending.append((call.target, (*steps, call), lineage))
        return matches

    def matching_calls(self, edge):
        for call in self.sites[location_key(edge["location"])]:
            if self.direct_calls.names[call.caller] == edge["caller"] and call.name == edge["callee"]:
                yield call

    def thread_events(self):
        events = []
        for kind, primitive, handle, entry in self.primitives:
            pending = [(handle, entry, (primitive,), frozenset({primitive.caller}))]
            states = 0
            while pending:
                handle, entry, chain, visited = pending.pop()
                states += 1
                if states > MAX_FORWARD_STATES:
                    self.limits.add("thread_forward_states")
                    break
                known_entry = (kind == "join" or entry and entry.kind == "function")
                if known_entry and handle and handle.kind == "storage":
                    events.append((kind, handle, entry, chain))
                missing = (handle is not None and handle.kind == "parameter") or (entry is not None and entry.kind == "parameter")
                if not missing:
                    continue
                if len(chain) >= MAX_FORWARD_STEPS:
                    self.limits.add("thread_forward_depth")
                    continue
                for caller in self.incoming[chain[0].caller]:
                    if caller.caller in visited:
                        continue
                    param_handle = self._forward(handle, caller.handles)
                    param_entry = self._forward(entry, caller.callbacks)
                    if param_handle and (kind == "join" or param_entry):
                        pending.append((param_handle, param_entry, (caller, *chain), visited | {caller.caller}))
        return events

    @staticmethod
    def _forward(origin, arguments):
        if origin is None or origin.kind != "parameter":
            return origin
        resolved = arguments.get(origin.identity)
        if resolved:
            return Origin(resolved.kind, resolved.identity, (*resolved.adapter_sites, *origin.adapter_sites))
        return None

    def describe(self, call):
        return {"caller": self.direct_calls.names[call.caller], "callee": call.name, "location": call.location}


class FunctionCalls:
    def __init__(self, paths, key, tu, node, callback_unions):
        self.paths, self.key, self.tu = paths, key, tu
        self.analyzer = paths.analyzer
        parameters = [child for child in node.get("inner", []) if child.get("kind") == "ParmVarDecl"]
        self.parameters = {parameter["id"]: index for index, parameter in enumerate(parameters)}
        self.locals = {param["id"]: param for param in parameters}
        self.unions = callback_unions
        self.writes = Counter()
        self.escaped = set()
        self.positions = {}
        self.adapters_supported = True

    def collect(self, body):
        for node in iter_nodes(body):
            kind = node.get("kind")
            if kind in UNSUPPORTED_JUMPS:
                self.adapters_supported = False
            if kind == "VarDecl":
                self.locals[node["id"]] = node
                if _initializer(node) is not None:
                    self.writes[node["id"]] += 1
            elif _assignment(node) or kind == "CompoundAssignOperator" or (
                kind == "UnaryOperator" and node.get("opcode") in {"++", "--"}
            ):
                self.writes[self._base(node["inner"][0])] += 1
            elif kind == "UnaryOperator" and node.get("opcode") == "&":
                self.escaped.add(self._base(node["inner"][0]))
        if self.adapters_supported:
            for position, statement in enumerate(body.get("inner", [])):
                call = _linear_call(statement)
                if call:
                    self.positions[id(call)] = position
        self.visit(body, {})

    @staticmethod
    def _base(expression):
        member, _ = _member_base(expression)
        return member or _reference(expression)

    def _parameter(self, identity):
        if identity in self.parameters and identity not in self.escaped and not self.writes[identity]:
            return Origin("parameter", self.parameters[identity])
        return None

    def _callback(self, expression, adapters):
        node = _callback_expr(expression)
        if not node:
            return None
        if node.get("kind") == "DeclRefExpr":
            target = self.analyzer.function_target(node, self.tu)
            resolved = self.paths.direct_calls.resolve(self.key, target)
            if resolved:
                return Origin("function", resolved)
            if is_fptr_qual_type(node.get("type")):
                return self._parameter(_reference(node))
        base, field = _member_base(node)
        adapter = adapters.get(base)
        if adapter and self.unions.get(field) == adapter[0]:
            group, source = adapter
            location = self.analyzer.location(node, self.tu)
            if location:
                return Origin(source.kind, source.identity, (*source.adapter_sites, _site(location)))
        return None

    def _handle(self, expression):
        node = unwrap_expr(expression)
        if not node:
            return None
        if node.get("kind") == "UnaryOperator" and node.get("opcode") == "*":
            return self._parameter(_reference(node["inner"][0]))
        addressed = node.get("kind") == "UnaryOperator" and node.get("opcode") == "&"
        if addressed:
            node = unwrap_expr(node["inner"][0])
        if not node or node.get("kind") != "DeclRefExpr" or is_fptr_qual_type(node.get("type")):
            return None
        identity = _reference(node)
        if identity in self.parameters:
            return self._parameter(identity) if not addressed else None
        if not addressed and _pointee(node):
            return None
        obj = self.analyzer.global_references.get((self.tu, identity))
        if obj in self.analyzer.objects and obj not in self.analyzer.global_binding_exclusions:
            return Origin("storage", ("global", obj))
        local = self.locals.get(identity)
        if local and local.get("storageClass") not in {"extern", "static"}:
            location = self.analyzer.location(local, self.tu)
            if location:
                return Origin("storage", ("local", self.key, location_key(location)))
        return None

    def _adapter(self, statement, adapters):
        if not self.adapters_supported or not _assignment(statement):
            return
        left, right = statement["inner"]
        base, field = _member_base(left)
        member_group = self.unions.get(field)
        local = self.locals.get(base)
        if (not member_group or not local or local.get("kind") != "VarDecl" or local.get("storageClass") in {"extern", "static"}
                or self.writes[base] != 1 or base in self.escaped or "volatile" in _type(local)):
            return
        source = self._callback(right, adapters)
        location = self.analyzer.location(statement, self.tu)
        if source and location:
            adapters[base] = (member_group, Origin(source.kind, source.identity, (*source.adapter_sites, _site(location))))

    def visit(self, node, adapters):
        kind = node.get("kind")
        if kind in SKIP_EXPRESSIONS:
            return
        children = node.get("inner", [])
        if kind == "CompoundStmt":
            adapters = dict(adapters)
            for statement in children:
                self.visit(statement, adapters)
                self._adapter(statement, adapters)
            return
        if kind == "CallExpr" and children:
            target = self.analyzer.function_target(children[0], self.tu)
            location = self.analyzer.location(node, self.tu)
            if target and location:
                callbacks, handles = {}, {}
                for index, arg in enumerate(children[1:]):
                    if is_fptr_qual_type(arg.get("type")):
                        value = self._callback(arg, adapters)
                        if value:
                            callbacks[index] = value
                    elif (value := self._handle(arg)) is not None:
                        handles[index] = value
                self.paths.add(self.key, target, _site(location), self.positions.get(id(node)), callbacks, handles)
        for child in children:
            self.visit(child, adapters)


def _callback_unions(ast):
    groups = {}
    supported = {"void(*)(void*)", "void*(*)(void*)"}
    for node in iter_nodes(ast):
        if node.get("kind") != "RecordDecl" or node.get("tagUsed") != "union":
            continue
        fields = [child for child in node.get("inner", []) if child.get("kind") == "FieldDecl"]
        if len(fields) == 2 and {"".join(_type(child).split()) for child in fields} == supported:
            for child in fields:
                groups[child["id"]] = node["id"]
    return groups


def collect_context_paths(asts, analyzer, direct_calls, create_apis, join_apis):
    paths = ContextPaths(direct_calls, analyzer, create_apis, join_apis)
    for index, (tu, ast) in enumerate(asts):
        if not tu.endswith(".c"):
            continue
        unions = _callback_unions(ast)
        for node in iter_nodes(ast):
            if node.get("kind") != "FunctionDecl":
                continue
            identity = analyzer.function_references[tu, node["id"]]
            key = (index, identity)
            body = next((child for child in node.get("inner", []) if child.get("kind") == "CompoundStmt"), None)
            if key not in direct_calls.names or body is None:
                continue
            location = analyzer.location(node, tu)
            if not location:
                continue
            paths.functions[location["file"], node["name"]].add(key)
            FunctionCalls(paths, key, tu, node, unions).collect(body)
    return paths
