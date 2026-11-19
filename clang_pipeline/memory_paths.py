"""Bounded C heap-lifetime witnesses. An unsupported effect invalidates a witness."""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path

from .clang_ast import stable_digest, unwrap_expr
from .semantic_sync import SemanticSyncAnalyzer


VERSION = "recode-memory-path/1"
MAX_TRACE_EVENTS = 192
MAX_TRACES = 128
MODELED_CASTS = {"NoOp", "LValueToRValue", "BitCast", "FunctionToPointerDecay",
                 "ArrayToPointerDecay", "NullToPointer", "ToVoid", "IntegralCast"}


class UnsupportedPath(ValueError):
    pass


@dataclass(frozen=True)
class Reference:
    kind: str
    key: str


@dataclass
class Allocation:
    size: int | str
    fields: dict = field(default_factory=dict)
    data: bytes | None = None
    released: bool = False


@dataclass
class Frame:
    values: dict
    returned: bool = False
    result: object = None
    exit: dict | None = None


@dataclass
class State:
    frames: list = field(default_factory=list)
    heap: dict = field(default_factory=dict)
    events: list = field(default_factory=list)

    @property
    def frame(self):
        return self.frames[-1]


def _type(node):
    types = node.get("type", {})
    return types.get("desugaredQualType", types.get("qualType", ""))


def _record_type(value):
    value = value.removeprefix("const ").strip()
    if value.startswith("struct ") and value[7:].isidentifier():
        return value
    raise UnsupportedPath("unmodeled_record_type")


class MemoryPaths(SemanticSyncAnalyzer):
    """Reuse compiler declarations and source indexing from the synchronization analyzer."""

    def _step(self):
        self.steps += 1
        if self.steps > self.max_steps:
            raise UnsupportedPath("step_limit")

    def _bounded(self, paths):
        if len(paths) > self.max_paths:
            raise UnsupportedPath("path_limit")
        return paths

    def _site(self, node, tu):
        location = self.location(node, tu)
        if not location or not location["snippet"]:
            raise UnsupportedPath("source_site_unavailable")
        return dict(location)

    def _record(self, state, kind, node, tu, **data):
        if len(state.events) >= MAX_TRACE_EVENTS:
            raise UnsupportedPath("trace_event_limit")
        location = self._site(node, tu)
        event = {"kind": kind, "location": location, **data}
        event["evidence_ids"] = self.evidence_for("ast_memory_" + kind, location, stable_digest(data))
        state.events.append(event)

    def _allocation(self, state, value):
        if not isinstance(value, Reference) or value.kind != "heap" or value.key not in state.heap:
            raise UnsupportedPath("unbound_heap_reference")
        allocation = state.heap[value.key]
        if allocation.released:
            raise UnsupportedPath("access_after_release")
        return allocation

    def _member(self, node, state, tu):
        base = node.get("inner", [])
        if len(base) != 1 or not node.get("isArrow") or not node.get("referencedMemberDecl"):
            raise UnsupportedPath("unmodeled_member")
        for path, value in self._evaluate(base[0], state, tu):
            allocation = self._allocation(path, value)
            # QualType can retain a typedef. The pointee's canonical type is
            # also available on the record declaration in this TU.
            if not isinstance(allocation.size, str) or not self._matches_field(node, tu, allocation.size):
                raise UnsupportedPath("allocation_type_mismatch")
            yield path, value, allocation

    def _matches_field(self, node, tu, record):
        return self.member_records.get((tu, node["referencedMemberDecl"])) == record

    def _index_fields(self, node, tu, record=None):
        if node.get("kind") == "RecordDecl":
            record = "struct " + node["name"] if node.get("name") and node.get("tagUsed") == "struct" else None
        if node.get("kind") == "FieldDecl" and record:
            self.member_records[(tu, node["id"])] = record
        if node.get("kind") == "TypedefDecl":
            canonical = node.get("type", {}).get("desugaredQualType", "")
            if canonical.startswith("struct ") and canonical[7:].isidentifier():
                self.record_aliases[(tu, node.get("name"))] = canonical
        for child in node.get("inner", []):
            self._index_fields(child, tu, record)

    def _write(self, left, state, value, node, tu):
        left = unwrap_expr(left)
        if left.get("kind") == "DeclRefExpr":
            key = (left.get("referencedDecl") or {}).get("id")
            if key not in state.frame.values:
                raise UnsupportedPath("write_outside_frame")
            state.frame.values[key] = value
            return [(state, value)]
        if left.get("kind") == "MemberExpr":
            result = []
            for path, owner, allocation in self._member(left, state, tu):
                name = left["name"]
                allocation.fields[name] = value
                self._record(path, "bind_field", node, tu, container=owner.key, field=name,
                             value_kind=value.kind if isinstance(value, Reference) else "scalar",
                             value=value.key if isinstance(value, Reference) else None)
                result.append((path, value))
            return result
        raise UnsupportedPath("unmodeled_assignment_target")

    def _evaluate(self, node, state, tu):
        self._step()
        kind, children = node.get("kind"), node.get("inner", [])
        if kind in {"ImplicitCastExpr", "CStyleCastExpr"} and len(children) == 1:
            cast = node.get("castKind")
            if cast not in MODELED_CASTS:
                raise UnsupportedPath("unmodeled_cast")
            values = self._evaluate(children[0], state, tu)
            if cast == "ToVoid":
                return [(path, None) for path, _ in values]
            if cast == "IntegralCast" and any(type(value) is not int or not 0 <= value <= 127 for _, value in values):
                raise UnsupportedPath("unmodeled_integer_conversion")
            return values
        if kind in {"ParenExpr", "ConstantExpr", "FullExpr"} and len(children) == 1:
            return self._evaluate(children[0], state, tu)
        if kind in {"IntegerLiteral", "CharacterLiteral"}:
            return [(state, int(node["value"]))]
        if kind == "GNUNullExpr":
            return [(state, 0)]
        if kind == "StringLiteral":
            value = ast.literal_eval(node["value"])
            if isinstance(value, str) and value.isascii() and "\x00" not in value:
                return [(state, value.encode("ascii") + b"\x00")]
            raise UnsupportedPath("unmodeled_string_literal")
        if kind == "DeclRefExpr":
            function = self.function_target(node, tu)
            if function:
                return [(state, Reference("function", function))]
            key = (node.get("referencedDecl") or {}).get("id")
            if key in state.frame.values and state.frame.values[key] is not None:
                return [(state, state.frame.values[key])]
            raise UnsupportedPath("unbound_local_value")
        if kind == "UnaryExprOrTypeTraitExpr" and node.get("name") == "sizeof":
            types = node.get("argType") or (children[0].get("type", {}) if children else {})
            value = types.get("desugaredQualType", types.get("qualType", ""))
            return [(state, _record_type(self.record_aliases.get((tu, value), value)))]
        if kind == "MemberExpr":
            result = []
            for path, _, allocation in self._member(node, state, tu):
                if node["name"] not in allocation.fields:
                    raise UnsupportedPath("unmodeled_field_read")
                result.append((path, allocation.fields[node["name"]]))
            return result
        if kind == "UnaryOperator" and len(children) == 1:
            result = []
            for path, value in self._evaluate(children[0], state, tu):
                if node.get("opcode") == "!":
                    result.append((path, int(not self._truth(value))))
                elif node.get("opcode") == "-" and type(value) is int and 0 <= value <= 127:
                    result.append((path, -value))
                else:
                    raise UnsupportedPath("unmodeled_unary_operator")
            return result
        if kind == "BinaryOperator" and len(children) == 2:
            return self._binary(node, children, state, tu)
        if kind == "ConditionalOperator" and len(children) == 3:
            result = []
            for path, condition in self._evaluate(children[0], state, tu):
                choice = self._truth(condition)
                self._record(path, "branch", children[0], tu, taken=choice)
                result.extend(self._evaluate(children[1 if choice else 2], path, tu))
            return self._bounded(result)
        if kind == "ArraySubscriptExpr" and len(children) == 2:
            result = []
            for path, value in self._evaluate(children[0], state, tu):
                for evaluated, index in self._evaluate(children[1], path, tu):
                    data = self._allocation(evaluated, value).data
                    if data is None or type(index) is not int or not 0 <= index < len(data):
                        raise UnsupportedPath("unmodeled_array_read")
                    result.append((evaluated, data[index]))
            return self._bounded(result)
        if kind == "CallExpr":
            return self._call(node, children, state, tu)
        raise UnsupportedPath("unmodeled_expression:" + str(kind))

    @staticmethod
    def _truth(value):
        if type(value) is int or isinstance(value, Reference):
            return bool(value)
        raise UnsupportedPath("undetermined_condition")

    def _binary(self, node, children, state, tu):
        op = node.get("opcode")
        left, right = children
        if op == "=":
            target = unwrap_expr(left)
            base = target.get("inner", [])
            stable_lhs = target.get("kind") == "DeclRefExpr" or (
                target.get("kind") == "MemberExpr" and len(base) == 1 and unwrap_expr(base[0]).get("kind") == "DeclRefExpr"
            )
            if not stable_lhs and self.has_side_effect(right):
                raise UnsupportedPath("unsequenced_indirect_assignment")
            return self._bounded([result for path, value in self._evaluate(right, state, tu)
                                  for result in self._write(left, path, value, node, tu)])
        if op not in {"&&", "||"} and (self.has_side_effect(left) or self.has_side_effect(right)):
            raise UnsupportedPath("unsequenced_binary_effect")
        results = []
        for path, a in self._evaluate(left, state, tu):
            if op in {"&&", "||"}:
                if self._truth(a) == (op == "||"):
                    results.append((path, int(op == "||")))
                else:
                    results.extend((next_path, int(self._truth(b))) for next_path, b in self._evaluate(right, path, tu))
                continue
            for next_path, b in self._evaluate(right, path, tu):
                if type(a) is int and type(b) is int and op in {"+", ">", "==", "!="}:
                    value = {"+": lambda: a + b, ">": lambda: int(a > b),
                             "==": lambda: int(a == b), "!=": lambda: int(a != b)}[op]()
                    if not -(2**31) <= value < 2**31:
                        raise UnsupportedPath("unmodeled_integer_range")
                    results.append((next_path, value))
                else:
                    raise UnsupportedPath("unmodeled_binary_operator")
        return self._bounded(results)

    def _statement(self, node, state, tu):
        self._step()
        if state.frame.returned:
            return [state]
        kind, children = node.get("kind"), node.get("inner", [])
        if kind in {"CompoundStmt", "DeclStmt"}:
            paths = [state]
            for child in children:
                paths = self._bounded([out for path in paths for out in self._statement(child, path, tu)])
            return paths
        if kind == "VarDecl" and node.get("storageClass") not in {"static", "extern"}:
            if children and node.get("init") != "c":
                raise UnsupportedPath("unmodeled_initializer")
            paths = self._evaluate(children[-1], state, tu) if children else [(state, None)]
            for path, value in paths:
                path.frame.values[node["id"]] = value
            return [path for path, _ in paths]
        if kind == "IfStmt" and not node.get("hasInit") and len(children) in {2, 3}:
            result = []
            for path, value in self._evaluate(children[0], state, tu):
                choice = self._truth(value)
                self._record(path, "branch", children[0], tu, taken=choice)
                result.extend(self._statement(children[1 if choice else 2], path, tu)
                              if choice or len(children) == 3 else [path])
            return self._bounded(result)
        if kind == "ReturnStmt":
            paths = self._evaluate(children[0], state, tu) if children else [(state, None)]
            for path, value in paths:
                path.frame.returned, path.frame.result = True, value
                path.frame.exit = self._site(node, tu)
            return [path for path, _ in paths]
        if kind == "NullStmt":
            return [state]
        return [path for path, _ in self._evaluate(node, state, tu)]

    def _invoke(self, key, state, args):
        record = self.functions[key]
        if len(state.frames) >= self.max_call_depth or len(args) != len(record["parameters"]):
            raise UnsupportedPath("call_boundary_unmodeled")
        state.frames.append(Frame({param["id"]: value for param, value in zip(record["parameters"], args)}))
        result = []
        for path in self._statement(record["body"], state, record["tu"]):
            frame = path.frames.pop()
            if not frame.returned and not record["node"].get("type", {}).get("qualType", "").startswith("void ("):
                raise UnsupportedPath("non_void_fallthrough")
            result.append((path, frame))
        return self._bounded(result)

    def _call(self, node, children, state, tu):
        if not children or (len(children) > 2 and any(self.has_side_effect(arg) for arg in children[1:])):
            raise UnsupportedPath("unsequenced_call_arguments")
        direct = self.function_target(children[0], tu)
        result = []
        for path, target in self._evaluate(children[0], state, tu):
            if not isinstance(target, Reference) or target.kind != "function":
                raise UnsupportedPath("unresolved_call_target")
            args = [(path, [])]
            for child in children[1:]:
                args = self._bounded([(evaluated, [*values, value]) for current, values in args
                                      for evaluated, value in self._evaluate(child, current, tu)])
            name = self.call_nodes[target.key]["name"]
            for current, arguments in args:
                if target.key in self.functions:
                    record = self.functions[target.key]
                    callback = None
                    if direct is None:
                        callee = unwrap_expr(children[0])
                        base = callee.get("inner", [])
                        if callee.get("kind") != "MemberExpr" or len(base) != 1 or self.has_side_effect(base[0]):
                            raise UnsupportedPath("indirect_call_not_bound_to_heap_field")
                        bindings = list(self._member(callee, current, tu))
                        if len(bindings) != 1 or bindings[0][2].fields.get(callee["name"]) != target:
                            raise UnsupportedPath("indirect_call_field_does_not_match_target")
                        callback = {"container": bindings[0][1].key, "field": callee["name"]}
                    self._record(current, "call", node, tu, target=name, indirect=direct is None,
                                 definition=record["location"], target_id=target.key, callback=callback)
                    result.extend((evaluated, frame.result) for evaluated, frame in self._invoke(target.key, current, arguments))
                elif direct and name in {"malloc", "calloc", "free", "strlen", "memcpy"}:
                    result.extend(self._builtin(name, node, current, tu, arguments))
                else:
                    raise UnsupportedPath("unmodeled_call:" + name)
        return self._bounded(result)

    def _builtin(self, name, node, state, tu, args):
        if name in {"malloc", "calloc"}:
            if name == "malloc" and len(args) == 1:
                size = args[0]
            elif name == "calloc" and len(args) == 2 and args[0] == 1:
                size = args[1]
            else:
                raise UnsupportedPath("unmodeled_allocation_size")
            if not (type(size) is int and size > 0 or isinstance(size, str) and size.startswith("struct ")):
                raise UnsupportedPath("unmodeled_allocation_size")
            site = self._site(node, tu)
            identity = "ast_heap_" + stable_digest([site, len(state.events), len(state.frames)])[:20]
            success = copy.deepcopy(state)
            success.heap[identity] = Allocation(size)
            self._record(success, "acquire", node, tu, resource_instance_id=identity, api=name)
            self._record(state, "allocation_failure", node, tu, api=name)
            return [(success, Reference("heap", identity)), (state, 0)]
        if name == "free" and len(args) == 1:
            if args[0] != 0:
                allocation = self._allocation(state, args[0])
                for member in allocation.fields.values():
                    if isinstance(member, Reference) and member.kind == "heap" and not state.heap[member.key].released:
                        self._record(state, "release_container", node, tu, resource_instance_id=member.key, container=args[0].key)
                allocation.released = True
                self._record(state, "release", node, tu, resource_instance_id=args[0].key, api=name)
            return [(state, None)]
        if name == "strlen" and len(args) == 1 and isinstance(args[0], bytes):
            return [(state, len(args[0]) - 1)]
        if name == "memcpy" and len(args) == 3 and isinstance(args[1], bytes):
            allocation = self._allocation(state, args[0])
            if type(allocation.size) is int and type(args[2]) is int and 0 <= args[2] <= min(allocation.size, len(args[1])):
                allocation.data = args[1][:args[2]]
                return [(state, args[0])]
        raise UnsupportedPath("unmodeled_builtin:" + name)

    def prove(self, asts, binding, entries):
        self.member_records, self.record_aliases = {}, {}
        self.index(asts)
        for tu, ast_data in asts:
            self._index_fields(ast_data, tu)
        proofs, gaps = [], []
        for entry in sorted(set(entries)):
            matches = [key for key, record in self.functions.items() if record["name"] == entry]
            if len(matches) != 1:
                gaps.append("unsupported_entry:" + entry)
                continue
            key = matches[0]
            record = self.functions[key]
            params = record["parameters"]
            if len(params) > 2 or any(_type(param) != "int" for param in params):
                gaps.append("unsupported_entry_parameters:" + entry)
                continue
            for args in product((0, 1), repeat=len(params)):
                try:
                    witnessed = []
                    for path, frame in self._invoke(key, State(), args):
                        if type(frame.result) is not int or not frame.exit:
                            raise UnsupportedPath("entry_exit_unmodeled")
                        self._record_exit(path, frame.exit)
                        witnessed.append({"entry": record["location"], "arguments": list(args), "events": path.events})
                    if len(proofs) + len(witnessed) > MAX_TRACES:
                        raise UnsupportedPath("trace_count_limit")
                    proofs.extend(witnessed)
                except (UnsupportedPath, RecursionError) as exc:
                    gaps.append(f"{entry}:{args}:{exc}")
        return {"schema_version": VERSION, "basis": "clang-json-ast-paths", "binding": binding,
                "traces": proofs, "gaps": sorted(set(gaps))}

    def _record_exit(self, path, location):
        if len(path.events) >= MAX_TRACE_EVENTS:
            raise UnsupportedPath("trace_event_limit")
        roots = sorted(ref.key for allocation in path.heap.values() if not allocation.released
                       for ref in allocation.fields.values() if isinstance(ref, Reference) and ref.kind == "heap")
        live = sorted(key for key, allocation in path.heap.items() if not allocation.released)
        # Heap allocations reached through another leaked allocation are
        # indirect losses. Report the owning allocation as the leak witness.
        data = {"live_roots": sorted(set(live) - set(roots))}
        path.events.append({"kind": "exit", "location": location, **data,
                            "evidence_ids": self.evidence_for("ast_memory_exit", location, stable_digest(data))})


def analyze_memory_paths(source_root, prepared, binding, entries):
    units, asts, _, errors = prepared
    if errors or not asts or any(not name.endswith(".c") for name, _ in asts):
        return None, []
    if any(any("c++" in arg for arg in unit.get("arguments", []) if arg.startswith(("-std=", "-x")))
           for unit in units):
        return None, []
    analyzer = MemoryPaths(Path(source_root), units, max_steps=12000)
    result = analyzer.prove(asts, binding, entries)
    if analyzer.gaps or any(reason.startswith("ambiguous_function_definition:") for reason in analyzer.call_gaps):
        return None, []
    ids = {identity for trace in result["traces"] for ev in trace["events"] for identity in ev["evidence_ids"]}
    return result, [analyzer.evidence[identity] for identity in sorted(ids)]
