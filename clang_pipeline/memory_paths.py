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
MAX_LOOP_ITERATIONS = 16
OUTPUT_SLOT = "output_slot"
MODELED_CASTS = {"NoOp", "LValueToRValue", "BitCast", "FunctionToPointerDecay",
                 "ArrayToPointerDecay", "NullToPointer", "ToVoid", "IntegralCast"}


class UnsupportedPath(ValueError):
    pass


@dataclass(frozen=True)
class Reference:
    kind: str
    key: str
    index: int | None = None


@dataclass
class Allocation:
    size: int | str
    fields: dict = field(default_factory=dict)
    data: bytes | None = None
    released: bool = False
    count: int = 1


@dataclass
class Frame:
    values: dict
    returned: bool = False
    result: object = None
    exit: dict | None = None
    loop_control: str | None = None
    goto_label: str | None = None


@dataclass
class OutputSlot:
    pointee_type: str
    value: object = None


@dataclass
class State:
    frames: list = field(default_factory=list)
    heap: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    output_slots: dict[str, OutputSlot] = field(default_factory=dict)

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
        if len(base) != 1 or not node.get("referencedMemberDecl"):
            raise UnsupportedPath("unmodeled_member")
        receiver = unwrap_expr(base[0])
        if not node.get("isArrow") and receiver.get("kind") == "ArraySubscriptExpr":
            operands = receiver.get("inner", [])
            if len(operands) != 2 or any(self.has_side_effect(item) for item in operands):
                raise UnsupportedPath("unmodeled_array_member")
            owners = [(evaluated, value, index) for path, value in self._evaluate(operands[0], state, tu)
                      for evaluated, index in self._evaluate(operands[1], path, tu)]
        elif node.get("isArrow"):
            owners = [(path, value, None) for path, value in self._evaluate(base[0], state, tu)]
        else:
            raise UnsupportedPath("unmodeled_member")
        for path, value, index in owners:
            if isinstance(value, Reference) and value.kind == "element":
                if index is not None:
                    raise UnsupportedPath("nested_array_pointer_unmodeled")
                index = value.index
                value = Reference("heap", value.key)
            allocation = self._allocation(path, value)
            # QualType can retain a typedef. The pointee's canonical type is
            # also available on the record declaration in this TU.
            if not isinstance(allocation.size, str) or not self._matches_field(node, tu, allocation.size):
                raise UnsupportedPath("allocation_type_mismatch")
            if index is not None and (type(index) is not int or not 0 <= index < allocation.count):
                raise UnsupportedPath("array_index_out_of_bounds")
            if allocation.count == 1:
                index = None
            elif index is None:
                index = 0
            yield path, value, allocation, index

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

    @staticmethod
    def _output_slot(state, reference, pointee_type):
        if not isinstance(reference, Reference) or reference.kind != "output":
            raise UnsupportedPath("unbound_output_slot")
        slot = state.output_slots.get(reference.key)
        if slot is None or slot.pointee_type != pointee_type:
            raise UnsupportedPath("output_slot_type_mismatch")
        return slot

    def _write(self, left, state, value, node, tu):
        left = unwrap_expr(left)
        if left.get("kind") == "DeclRefExpr":
            key = (left.get("referencedDecl") or {}).get("id")
            if key not in state.frame.values:
                raise UnsupportedPath("write_outside_frame")
            state.frame.values[key] = value
            return [(state, value)]
        if left.get("kind") == "UnaryOperator" and left.get("opcode") == "*":
            result = []
            pointee_type = _type(left)
            for path, reference in self._evaluate(left["inner"][0], state, tu):
                slot = self._output_slot(path, reference, pointee_type)
                if pointee_type.endswith("*"):
                    if value != 0:
                        self._allocation(path, value)
                    self._record(path, "bind_output", node, tu, slot=int(reference.key),
                                 value=None if value == 0 else value.key)
                elif type(value) is not int:
                    raise UnsupportedPath("unmodeled_output_scalar")
                slot.value = value
                result.append((path, value))
            return self._bounded(result)
        if left.get("kind") == "MemberExpr":
            if isinstance(value, Reference) and value.kind in {"output", "element"}:
                raise UnsupportedPath("local_address_escapes")
            result = []
            for path, owner, allocation, index in self._member(left, state, tu):
                name = left["name"]
                allocation.fields[(index, name)] = value
                data = {"container": owner.key, "field": name,
                        "value_kind": value.kind if isinstance(value, Reference) else "scalar",
                        "value": value.key if isinstance(value, Reference) else None}
                if index is not None:
                    data["index"] = index
                self._record(path, "bind_array_field" if index is not None else "bind_field", node, tu, **data)
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
            declaration = node.get("referencedDecl") or {}
            key = declaration.get("id")
            if key in state.frame.values and state.frame.values[key] is not None:
                return [(state, state.frame.values[key])]
            name = declaration.get("name")
            raise UnsupportedPath("unbound_value:" + name if isinstance(name, str) and name else "unbound_local_value")
        if kind == "UnaryExprOrTypeTraitExpr" and node.get("name") == "sizeof":
            types = node.get("argType") or (children[0].get("type", {}) if children else {})
            value = types.get("desugaredQualType", types.get("qualType", ""))
            return [(state, _record_type(self.record_aliases.get((tu, value), value)))]
        if kind == "MemberExpr":
            result = []
            for path, _, allocation, index in self._member(node, state, tu):
                key = (index, node["name"])
                if key not in allocation.fields:
                    raise UnsupportedPath("unmodeled_field_read")
                result.append((path, allocation.fields[key]))
            return result
        if kind == "UnaryOperator" and len(children) == 1:
            if node.get("opcode") == "&":
                target = unwrap_expr(children[0])
                operands = target.get("inner", [])
                if target.get("kind") != "ArraySubscriptExpr" or len(operands) != 2 or any(
                    self.has_side_effect(item) for item in operands
                ):
                    raise UnsupportedPath("unmodeled_address_of")
                result = []
                for path, owner in self._evaluate(operands[0], state, tu):
                    for evaluated, index in self._evaluate(operands[1], path, tu):
                        allocation = self._allocation(evaluated, owner)
                        if type(index) is not int or not 0 <= index < allocation.count:
                            raise UnsupportedPath("array_index_out_of_bounds")
                        result.append((evaluated, Reference("element", owner.key, index)))
                return self._bounded(result)
            if node.get("opcode") in {"++", "--"}:
                target = unwrap_expr(children[0])
                if target.get("kind") != "DeclRefExpr":
                    raise UnsupportedPath("unmodeled_increment_target")
                key = (target.get("referencedDecl") or {}).get("id")
                previous = state.frame.values.get(key)
                if type(previous) is not int:
                    raise UnsupportedPath("unmodeled_increment_value")
                updated = previous + (1 if node["opcode"] == "++" else -1)
                if not -(2**31) <= updated < 2**31:
                    raise UnsupportedPath("unmodeled_integer_range")
                state.frame.values[key] = updated
                return [(state, previous if node.get("isPostfix") else updated)]
            result = []
            for path, value in self._evaluate(children[0], state, tu):
                if node.get("opcode") == "!":
                    result.append((path, int(not self._truth(value))))
                elif node.get("opcode") == "-" and type(value) is int and 0 <= value <= 127:
                    result.append((path, -value))
                elif node.get("opcode") == "*":
                    slot = self._output_slot(path, value, _type(node))
                    if slot.value is None:
                        raise UnsupportedPath("uninitialized_output_slot_read")
                    result.append((path, slot.value))
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
            if target.get("kind") == "UnaryOperator" and target.get("opcode") == "*" and len(base) == 1:
                address, call = unwrap_expr(base[0]), unwrap_expr(right)
                if address.get("kind") == "DeclRefExpr" and call.get("kind") == "CallExpr":
                    reference = state.frame.values.get((address.get("referencedDecl") or {}).get("id"))
                    # A call can update the passed slot but cannot change the
                    # caller's local pointer when its arguments have no effects.
                    stable_lhs = (isinstance(reference, Reference) and reference.kind == "output"
                                  and not any(self.has_side_effect(child) for child in call.get("inner", [])))
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
                if type(a) is int and type(b) is int and op in {"+", "<", ">", "==", "!="}:
                    value = {"+": lambda: a + b, "<": lambda: int(a < b), ">": lambda: int(a > b),
                             "==": lambda: int(a == b), "!=": lambda: int(a != b)}[op]()
                    if not -(2**31) <= value < 2**31:
                        raise UnsupportedPath("unmodeled_integer_range")
                    results.append((next_path, value))
                elif op in {"==", "!="} and (
                    (isinstance(a, Reference) and type(b) is int and b == 0)
                    or (isinstance(b, Reference) and type(a) is int and a == 0)
                ):
                    results.append((next_path, int(op == "!=")))
                else:
                    raise UnsupportedPath("unmodeled_binary_operator")
        return self._bounded(results)

    def _statement(self, node, state, tu):
        self._step()
        if state.frame.returned or state.frame.loop_control:
            return [state]
        kind, children = node.get("kind"), node.get("inner", [])
        if state.frame.goto_label:
            if kind == "LabelStmt" and node.get("declId") == state.frame.goto_label:
                state.frame.goto_label = None
            elif kind != "CompoundStmt":
                return [state]
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
        if kind == "ForStmt" and len(children) == 5:
            initializer, declaration, condition, increment, body = children
            if declaration.get("kind") or not condition.get("kind") or not increment.get("kind"):
                raise UnsupportedPath("unmodeled_for_shape")
            active = self._statement(initializer, state, tu) if initializer.get("kind") else [state]
            finished = []
            for iteration in range(MAX_LOOP_ITERATIONS + 1):
                next_paths = []
                for path in active:
                    if path.frame.returned:
                        finished.append(path)
                        continue
                    for tested, value in self._evaluate(condition, path, tu):
                        choice = self._truth(value)
                        self._record(tested, "branch", condition, tu, taken=choice)
                        if not choice:
                            finished.append(tested)
                        elif iteration == MAX_LOOP_ITERATIONS:
                            raise UnsupportedPath("loop_iteration_limit")
                        else:
                            for evaluated in self._statement(body, tested, tu):
                                if evaluated.frame.returned or evaluated.frame.goto_label:
                                    finished.append(evaluated)
                                elif evaluated.frame.loop_control == "break":
                                    evaluated.frame.loop_control = None
                                    finished.append(evaluated)
                                else:
                                    evaluated.frame.loop_control = None
                                    next_paths.extend(path for path, _ in self._evaluate(increment, evaluated, tu))
                active = self._bounded(next_paths)
                if not active:
                    return self._bounded(finished)
            raise UnsupportedPath("loop_iteration_limit")
        if kind == "ReturnStmt":
            paths = self._evaluate(children[0], state, tu) if children else [(state, None)]
            for path, value in paths:
                path.frame.returned, path.frame.result = True, value
                path.frame.exit = self._site(node, tu)
            return [path for path, _ in paths]
        if kind in {"BreakStmt", "ContinueStmt"}:
            state.frame.loop_control = "break" if kind == "BreakStmt" else "continue"
            return [state]
        if kind == "GotoStmt":
            label = node.get("targetLabelDeclId")
            if not isinstance(label, str) or not label:
                raise UnsupportedPath("unmodeled_goto_target")
            state.frame.goto_label = label
            return [state]
        if kind == "LabelStmt" and len(children) == 1:
            return self._statement(children[0], state, tu)
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
            if frame.goto_label or frame.loop_control:
                raise UnsupportedPath("unresolved_control_transfer")
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
                        if len(bindings) != 1 or bindings[0][2].fields.get((None, callee["name"])) != target:
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
            elif name == "calloc" and len(args) == 2 and type(args[0]) is int and 1 <= args[0] <= MAX_LOOP_ITERATIONS:
                size = args[1]
            else:
                raise UnsupportedPath("unmodeled_allocation_size")
            if not (type(size) is int and size > 0 or isinstance(size, str) and size.startswith("struct ")):
                raise UnsupportedPath("unmodeled_allocation_size")
            site = self._site(node, tu)
            identity = "ast_heap_" + stable_digest([site, len(state.events), len(state.frames)])[:20]
            success = copy.deepcopy(state)
            success.heap[identity] = Allocation(size, count=args[0] if name == "calloc" else 1)
            count = success.heap[identity].count
            if count > 1:
                self._record(success, "acquire_array", node, tu, resource_instance_id=identity, api=name, count=count)
            else:
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
            param_types = [_type(param) for param in record["parameters"]]
            if len(param_types) > 2 or any(
                param_type not in {"int", "int *"}
                and not (param_type.endswith(" **") and "(" not in param_type)
                for param_type in param_types
            ):
                gaps.append("unsupported_entry_parameters:" + entry)
                continue
            domains = [(0, 1) if param_type == "int" else (OUTPUT_SLOT,) for param_type in param_types]
            for args in product(*domains):
                slots = {str(i): OutputSlot(param_types[i][:-1].rstrip()) for i, value in enumerate(args)
                         if value == OUTPUT_SLOT}
                values = [Reference("output", str(i)) if value == OUTPUT_SLOT else value
                          for i, value in enumerate(args)]
                try:
                    witnessed = []
                    for path, frame in self._invoke(key, State(output_slots=slots), values):
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
        live = {key for key, allocation in path.heap.items() if not allocation.released}
        escaped = {slot.value.key for slot in path.output_slots.values()
                   if isinstance(slot.value, Reference) and slot.value.kind == "heap" and slot.value.key in live}
        frontier = list(escaped)
        while frontier:
            for ref in path.heap[frontier.pop()].fields.values():
                if isinstance(ref, Reference) and ref.kind == "heap" and ref.key in live and ref.key not in escaped:
                    escaped.add(ref.key)
                    frontier.append(ref.key)
        owned = live - escaped
        attached = {ref.key for owner in owned for ref in path.heap[owner].fields.values()
                    if isinstance(ref, Reference) and ref.kind == "heap"}
        data = {"live_roots": sorted(owned - attached)}
        path.events.append({"kind": "exit", "location": location, **data,
                            "evidence_ids": self.evidence_for("ast_memory_exit", location, stable_digest(data))})


def analyze_memory_paths(source_root, prepared, binding, entries):
    units, asts, _, errors = prepared
    no_paths = {"schema_version": VERSION, "basis": "clang-json-ast-paths", "binding": binding, "traces": []}
    if errors or not asts:
        return {**no_paths, "gaps": ["clang_ast_incomplete"]}, []
    if any(not name.endswith(".c") for name, _ in asts) or any(
        "c++" in arg for unit in units for arg in unit.get("arguments", []) if arg.startswith(("-std=", "-x"))
    ):
        return {**no_paths, "gaps": ["non_c_translation_unit"]}, []
    analyzer = MemoryPaths(Path(source_root), units, max_steps=12000)
    result = analyzer.prove(asts, binding, entries)
    index_gaps = analyzer.gaps | {gap for gap in analyzer.call_gaps if gap.startswith("ambiguous_function_definition:")}
    if index_gaps:
        gaps = sorted(set(result["gaps"]) | {f"ast_index_incomplete:{len(index_gaps)}"})
        return {**no_paths, "gaps": gaps}, []
    ids = {identity for trace in result["traces"] for ev in trace["events"] for identity in ev["evidence_ids"]}
    return result, [analyzer.evidence[identity] for identity in sorted(ids)]
