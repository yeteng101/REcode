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
CALLER_INPUT = "caller_input"
CALLER_POINTER = "caller_pointer"
MODELED_CASTS = {"NoOp", "LValueToRValue", "BitCast", "FunctionToPointerDecay",
                 "ArrayToPointerDecay", "NullToPointer", "ToVoid", "IntegralCast"}
BUILTIN_CALLS = {"malloc", "calloc", "free", "strlen", "memcpy", "__errno_location"}
INTEGER_TYPES = {"char", "signed char", "unsigned char", "short", "short int", "unsigned short",
                 "int", "unsigned", "unsigned int", "long", "long int", "unsigned long",
                 "long long", "unsigned long long", "size_t", "ssize_t", "intptr_t", "uintptr_t",
                 "int8_t", "uint8_t", "int16_t", "uint16_t", "int32_t", "uint32_t",
                 "int64_t", "uint64_t", "bool", "_Bool"}


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
    input: bool = False


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
    cells: dict = field(default_factory=dict)

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
        if isinstance(value, Reference) and value.kind == "external":
            name = self.external_names.get(value.key, "caller_pointer")
            raise UnsupportedPath("external_value_extent_unmodeled:" + name)
        if not isinstance(value, Reference) or value.kind != "heap" or value.key not in state.heap:
            raise UnsupportedPath("unbound_heap_reference")
        allocation = state.heap[value.key]
        if allocation.released:
            raise UnsupportedPath("access_after_release")
        return allocation

    def _member_owners(self, node, state, tu):
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
        elif node.get("isArrow") or receiver.get("kind") in {"DeclRefExpr", "MemberExpr"}:
            # ``.`` on a static record selects a table entry; ``->`` selects a
            # field of an allocation. Both resolve through the same owner walk.
            owners = [(path, value, None) for path, value in self._evaluate(base[0], state, tu)]
        else:
            raise UnsupportedPath("unmodeled_member")
        for path, value, index in owners:
            if isinstance(value, Reference) and value.kind == "record":
                if index is not None:
                    raise UnsupportedPath("record_table_indexed")
                yield path, value, None, None
                continue
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

    def _member(self, node, state, tu):
        for path, value, allocation, index in self._member_owners(node, state, tu):
            if allocation is None:
                raise UnsupportedPath("record_table_assignment_unmodeled")
            yield path, value, allocation, index

    def _matches_field(self, node, tu, record):
        return self.member_records.get((tu, node["referencedMemberDecl"])) == record

    def _index_fields(self, node, tu, record=None, typedef=None):
        kind = node.get("kind")
        if kind == "RecordDecl":
            name = node.get("name")
            # Anonymous records keep a TU-local identity so typedef'd
            # tables such as ``static uv__allocator_t table = {...}`` resolve.
            record = "struct " + name if name and node.get("tagUsed") == "struct" else "anon_" + str(node.get("id"))
            if record.startswith("anon_"):
                location = self.location(node, tu)
                if location:
                    bounds = node.get("range") or {}
                    end = (bounds.get("end") or {}).get("expansionLoc", bounds.get("end") or {})
                    end_line = end.get("line", location["line"])
                    self.anonymous_sites[(tu, location["file"], location["line"])] = record
                    self.anonymous_ranges.setdefault((tu, location["file"]), []).append(
                        (location["line"], end_line, record))
            if typedef:
                self.record_aliases[(tu, typedef)] = record
        elif kind == "TypedefDecl":
            declaration = node.get("type", {})
            canonical = declaration.get("desugaredQualType") or declaration.get("qualType", "")
            qualified = declaration.get("qualType", "")
            name = node.get("name")
            if canonical.startswith("struct ") and canonical[7:].isidentifier():
                self.record_aliases[(tu, name)] = canonical
            elif qualified == "struct " + str(name):
                # Clang synthesises a tag from the typedef for anonymous
                # records but leaves the RecordDecl unnamed.
                location = self.location(node, tu)
                if location:
                    self.typedef_sites[(tu, name)] = (location["file"], location["line"])
            else:
                typedef = name
        if kind == "FieldDecl" and record:
            self.member_records[(tu, node["id"])] = record
            fields = self.record_fields.setdefault((tu, record), [])
            if node.get("name") and node["name"] not in fields:
                fields.append(node["name"])
                self.field_types[(tu, record, node["name"])] = _type(node)
        for child in node.get("inner", []):
            self._index_fields(child, tu, record, typedef)

    def _link_typedefs(self, tu):
        for (owner, name), (file, line) in self.typedef_sites.items():
            if owner != tu:
                continue
            record = self.anonymous_sites.get((tu, file, line))
            if record is None:
                spans = [item for item in self.anonymous_ranges.get((tu, file), [])
                         if item[0] <= line <= item[1]]
                record = min(spans, key=lambda item: item[1] - item[0])[2] if spans else None
            if record:
                self.record_aliases[(tu, name)] = record
                self.record_aliases[(tu, "struct " + name)] = record

    def scoped_gap(self, reason, node=None, tu=None, owner=None):
        if owner is None or owner not in self.functions:
            return self.gap(reason, node, tu)
        location = self.location(node, tu) if node and tu else None
        suffix = (":" + location["file"] + ":" + str(location["line"])) if location else ""
        self.function_gaps.setdefault(owner, set()).add(reason + suffix)

    def _table_target(self, node, tu):
        """Resolve a table access to ``(record id, field or None)``."""
        node = unwrap_expr(node)
        if node is None:
            return None
        if node.get("kind") == "MemberExpr":
            base = node.get("inner", [])
            if len(base) != 1:
                return None
            owner = self._table_target(base[0], tu)
            return (owner[0], str(node.get("name"))) if owner and owner[1] is None else None
        if node.get("kind") == "DeclRefExpr":
            declaration = node.get("referencedDecl") or {}
            if declaration.get("kind") == "VarDecl":
                record = self.global_records.get((tu, declaration.get("id")))
                if record:
                    return (record, None)
        return None

    def _scan_table_writes(self, node, tu):
        kind, children = node.get("kind"), node.get("inner", [])
        written = escaped = None
        if kind == "BinaryOperator" and node.get("opcode") == "=" and children:
            written = self._table_target(children[0], tu)
        elif kind == "UnaryOperator" and node.get("opcode") == "&" and children:
            # Taking the address of the table or one of its fields lets any
            # callee rebind the entry through the pointer.
            escaped = self._table_target(children[0], tu)
        location = self.location(node, tu)
        if written:
            self._mark_table(self.mutated_fields, self.mutated_tables, written, location)
        if escaped:
            self._mark_table(self.escaped_fields, self.escaped_tables, escaped, location)
        for child in children:
            self._scan_table_writes(child, tu)

    @staticmethod
    def _mark_table(fields, tables, target, location):
        record, field = target
        if field is None:
            tables.setdefault(record, location)
        else:
            fields.setdefault((record, field), location)

    def _link_extern_tables(self):
        """Bind ``extern`` declarations to the table defined in another unit."""
        for symbol, identity in self.table_symbols.items():
            table = self.records[identity]
            for tu, declaration_id, qualified in self.extern_decls.get(symbol, []):
                if self._record_of_type(tu, qualified) == table["record"]:
                    self.global_records[(tu, declaration_id)] = identity
                    self.external_globals.pop((tu, declaration_id), None)

    def _seal_tables(self):
        for identity, record in self.records.items():
            writes = {field: site for (record_id, field), site in self.mutated_fields.items()
                      if record_id == identity}
            escapes = {field: site for (record_id, field), site in self.escaped_fields.items()
                       if record_id == identity}
            # A table configured at runtime is not read-only as a whole, even
            # when only one field was observed.
            table_write = self.mutated_tables.get(identity) or self._first_site(writes.values())
            table_escape = self.escaped_tables.get(identity) or self._first_site(escapes.values())
            reasons = {}
            for field in record["fields"]:
                written = writes.get(field, table_write)
                escaped = escapes.get(field, table_escape)
                if written:
                    reasons[field] = "written_at:" + self._site_suffix(written)
                elif escaped:
                    reasons[field] = "address_taken:" + self._site_suffix(escaped)
                elif not record["const"] and not record["static"]:
                    reasons[field] = "external_linkage"
                else:
                    reasons[field] = None
            record["reasons"] = reasons

    @staticmethod
    def _first_site(sites):
        present = [site for site in sites if site]
        return min(present, key=lambda site: (str(site.get("file")), site.get("line") or 0)) if present else None

    @staticmethod
    def _site_suffix(location):
        if not location:
            return "unknown"
        return str(location.get("file")) + ":" + str(location.get("line"))

    @staticmethod
    def _instability(table, field, reason):
        return "table_stability_unverified:" + table["name"] + "." + field + ":" + reason

    def _record_of_type(self, tu, type_name):
        value = type_name.replace("const ", "").strip()
        for candidate in (value, self.record_aliases.get((tu, value))):
            if candidate and (tu, candidate) in self.record_fields:
                return candidate
        return None

    def _index_globals(self, node, tu):
        for child in node.get("inner", []):
            kind = child.get("kind")
            if kind in {"TranslationUnitDecl", "LinkageSpecDecl"}:
                self._index_globals(child, tu)
            elif kind == "VarDecl":
                self._index_global(child, tu)

    def _index_global(self, node, tu):
        key = (tu, node.get("id"))
        declaration = node.get("type", {})
        qualified = declaration.get("desugaredQualType", declaration.get("qualType", ""))
        symbol = self.canonical(node, tu, "object_ast_")
        initializer = next((child for child in node.get("inner", []) if child.get("kind") == "InitListExpr"), None)
        if initializer is None:
            if node.get("storageClass") != "extern":
                return
            self.extern_decls.setdefault(symbol, []).append((tu, node.get("id"), qualified))
            # A declaration without a definition keeps caller-provided
            # contents. It stays a named opaque pointer instead of aborting
            # the whole entry point.
            name = node.get("name", "")
            reference = Reference("external", "ast_external_" + stable_digest([tu, name])[:20])
            self.external_globals[key] = {"name": name, "type": qualified, "reference": reference}
            self.external_names[reference.key] = name
            return
        record = self._record_of_type(tu, qualified) or qualified
        fields = self.record_fields.get((tu, record))
        if not fields:
            return
        # Only records whose every member is a function pointer can describe
        # an allocator table; other aggregates stay uninterpreted globals.
        if not all("(*" in self.field_types.get((tu, record, name), "") for name in fields):
            return
        try:
            values = self._table_values(initializer, tu, fields)
        except UnsupportedPath as exc:
            self.gap("unmodeled_global_table:" + node.get("name", "") + ":" + str(exc), node, tu)
            return
        identity = "ast_record_" + stable_digest([tu, node.get("name")])[:20]
        self.records[identity] = {"id": identity, "name": node.get("name", ""), "record": record,
                                  "fields": values, "location": self.location(node, tu),
                                  "const": qualified.startswith("const "),
                                  "static": node.get("storageClass") == "static",
                                  "reasons": {}}
        self.global_records[key] = identity
        self.table_symbols[symbol] = identity

    def _table_values(self, initializer, tu, fields):
        entries = initializer.get("inner", [])
        if len(entries) != len(fields):
            raise UnsupportedPath("table_shape_unmodeled")
        return {name: self._static_value(entry, tu) for name, entry in zip(fields, entries)}

    def _static_value(self, node, tu):
        node = unwrap_expr(node)
        if node is None:
            raise UnsupportedPath("empty_table_entry")
        if node.get("kind") == "UnaryOperator" and node.get("opcode") == "&" and len(node.get("inner", [])) == 1:
            node = unwrap_expr(node["inner"][0])
        if node.get("kind") == "DeclRefExpr":
            name = (node.get("referencedDecl") or {}).get("name")
            target = self.function_target(node, tu)
            if target and target in self.functions:
                return Reference("function", target)
            if name in BUILTIN_CALLS:
                return Reference("builtin", name)
            if target:
                return Reference("function", target)
            raise UnsupportedPath("table_entry_unresolved:" + str(name))
        if node.get("kind") == "GNUNullExpr":
            return 0
        if node.get("kind") == "IntegerLiteral":
            return int(node["value"])
        raise UnsupportedPath("table_entry_unmodeled:" + str(node.get("kind")))

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
                if isinstance(reference, Reference) and reference.kind == "cell":
                    if type(value) is not int:
                        raise UnsupportedPath("unmodeled_cell_value")
                    path.cells[reference.key] = value
                    result.append((path, value))
                    continue
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
            if (tu, key) in self.global_records:
                return [(state, Reference("record", self.global_records[(tu, key)]))]
            external = self.external_globals.get((tu, key))
            if external:
                return [(state, external["reference"])]
            name = declaration.get("name")
            raise UnsupportedPath("unbound_value:" + name if isinstance(name, str) and name else "unbound_local_value")
        if kind == "UnaryExprOrTypeTraitExpr" and node.get("name") == "sizeof":
            types = node.get("argType") or (children[0].get("type", {}) if children else {})
            value = types.get("desugaredQualType", types.get("qualType", ""))
            return [(state, _record_type(self.record_aliases.get((tu, value), value)))]
        if kind == "MemberExpr":
            result = []
            for path, value, allocation, index in self._member_owners(node, state, tu):
                if allocation is None:
                    table = self.records.get(value.key)
                    field = node["name"]
                    if table is None or field not in table["fields"]:
                        raise UnsupportedPath("unmodeled_record_field:" + str(field))
                    reason = table["reasons"].get(field)
                    if reason:
                        # The binding is not verifiably read-only, so the
                        # initializer stays a candidate, not the target.
                        raise UnsupportedPath(self._instability(table, field, reason))
                    result.append((path, table["fields"][field]))
                    continue
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
                    if isinstance(value, Reference) and value.kind == "cell":
                        if value.key not in state.cells:
                            # The first read has no verified value; an
                            # assumption here would silently pick a branch.
                            raise UnsupportedPath("uninitialized_cell_read:" + value.key)
                        result.append((path, state.cells[value.key]))
                        continue
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
        if key in self.function_gaps:
            raise UnsupportedPath("index_gap_in_callee:" + sorted(self.function_gaps[key])[0])
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
            if not isinstance(target, Reference) or target.kind not in {"function", "builtin"}:
                raise UnsupportedPath("unresolved_call_target")
            args = [(path, [])]
            for child in children[1:]:
                args = self._bounded([(evaluated, [*values, value]) for current, values in args
                                      for evaluated, value in self._evaluate(child, current, tu)])
            name = target.key if target.kind == "builtin" else self.call_nodes[target.key]["name"]
            for current, arguments in args:
                callback = None if direct is not None else self._route(children[0], current, tu, target)
                builtin = target.kind == "builtin" or (
                    target.key not in self.functions and name in BUILTIN_CALLS)
                if builtin:
                    # A builtin reached through a function-pointer table keeps
                    # the table field as provenance for the release or acquire.
                    if direct is None:
                        self._record(current, "call", node, tu, target=name, indirect=True,
                                     definition=None, target_id=name, callback=callback)
                    result.extend(self._builtin(name, node, current, tu, arguments))
                elif target.key in self.functions:
                    self._record(current, "call", node, tu, target=name, indirect=direct is None,
                                 definition=self.functions[target.key]["location"],
                                 target_id=target.key, callback=callback)
                    result.extend((evaluated, frame.result) for evaluated, frame in self._invoke(target.key, current, arguments))
                else:
                    raise UnsupportedPath("unmodeled_call:" + name)
        return self._bounded(result)

    def _route(self, callee, state, tu, target):
        callee = unwrap_expr(callee)
        base = callee.get("inner", []) if callee else []
        if callee.get("kind") != "MemberExpr" or len(base) != 1 or self.has_side_effect(base[0]):
            raise UnsupportedPath("indirect_call_not_bound_to_container")
        bindings = list(self._member_owners(callee, state, tu))
        if len(bindings) != 1:
            raise UnsupportedPath("indirect_call_field_does_not_match_target")
        _, value, allocation, index = bindings[0]
        field = callee["name"]
        if allocation is None:
            table = self.records.get(value.key)
            if index is not None or table is None or table["fields"].get(field) != target:
                raise UnsupportedPath("indirect_call_field_does_not_match_target")
            return {"container": value.key, "field": field, "kind": "record"}
        if index is not None or allocation.fields.get((None, field)) != target:
            raise UnsupportedPath("indirect_call_field_does_not_match_target")
        return {"container": value.key, "field": field, "kind": "heap"}

    def _builtin(self, name, node, state, tu, args):
        if name == "__errno_location" and not args:
            # errno is thread-local scalar storage. The cell keeps read/write
            # pairing without claiming a heap object.
            return [(state, Reference("cell", "errno"))]
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
            if isinstance(args[0], Reference) and args[0].kind == "external":
                # Caller-provided memory: the release is evidence of the
                # cleanup site, and no witness owns the region.
                self._record(state, "release", node, tu, resource_instance_id=args[0].key,
                             api=name, external=True)
                return [(state, None)]
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

    def _parameter_kind(self, param_type, tu):
        value = param_type.replace("const ", "").strip()
        if "(" in value:
            return "unsupported"
        if value.endswith("**"):
            return "output"
        if value.endswith("*"):
            pointee = value[:-1].strip()
            if pointee == "int":
                return "output"
            if self._record_of_type(tu, pointee):
                return "input_record"
            return "external"
        if value in INTEGER_TYPES:
            return "integer"
        return "unsupported"

    def _end_site(self, node, tu):
        """Location of the closing brace, used for functions without ``return``."""
        bounds = node.get("range") or {}
        end = (bounds.get("end") or {})
        end = end.get("expansionLoc", end)
        location = self.location(node, tu)
        file = end.get("file") or (location or {}).get("file")
        if not file:
            raise UnsupportedPath("source_site_unavailable")
        line = end.get("line")
        data = self.sources.get(file)
        if not line and data is not None and end.get("offset") is not None:
            line = data.count(b"\n", 0, int(end["offset"])) + 1
        if not line or data is None:
            raise UnsupportedPath("source_site_unavailable")
        lines = data.decode("utf-8", errors="replace").splitlines()
        if not 0 < line <= len(lines):
            raise UnsupportedPath("source_site_unavailable")
        return {"file": file, "line": line, "column": end.get("col", 1), "snippet": lines[line - 1].strip()}

    def _caller_inputs(self, state, record, kinds, args):
        """Bind caller-provided regions declared by the entry signature."""
        values, regions = [], []
        for index, (kind, argument) in enumerate(zip(kinds, args)):
            if kind == "output":
                values.append(Reference("output", str(index)))
            elif kind == "input_record":
                param_type = _type(record["parameters"][index]).replace("const ", "").strip()
                pointee = self._record_of_type(record["tu"], param_type[:-1].strip())
                if pointee is None:
                    raise UnsupportedPath("caller_region_type_unmodeled")
                identity = "ast_input_" + stable_digest([record["location"], index])[:20]
                allocation = Allocation(pointee, count=1, input=True)
                for field in self.record_fields.get((record["tu"], pointee), []):
                    reference = Reference("external", identity + ":" + field)
                    allocation.fields[(None, field)] = reference
                    self.external_names[reference.key] = field
                state.heap[identity] = allocation
                values.append(Reference("heap", identity))
                regions.append((identity, index))
            elif kind == "external":
                reference = Reference("external",
                                      "ast_pointer_" + stable_digest([record["location"], index])[:20])
                self.external_names[reference.key] = record["parameters"][index].get("name") or "caller_pointer"
                values.append(reference)
            else:
                values.append(argument)
        return values, regions

    def _record_inputs(self, path, record, regions):
        if not regions:
            return
        location = self._site(record["node"], record["tu"])
        for identity, index in reversed(regions):
            if len(path.events) >= MAX_TRACE_EVENTS:
                raise UnsupportedPath("trace_event_limit")
            data = {"resource_instance_id": identity, "api": CALLER_INPUT, "argument": index}
            path.events.insert(0, {"kind": "caller_input", "location": location, **data,
                                   "evidence_ids": self.evidence_for("ast_memory_caller_input", location, stable_digest(data))})

    def _table_uses(self):
        """Fields of unstable tables that each indexed function reads."""
        uses = {}
        for key, record in self.functions.items():
            found, stack = set(), [record["body"]]
            while stack:
                node = stack.pop()
                if node.get("kind") == "MemberExpr" and len(node.get("inner", [])) == 1:
                    target = self._table_target(node["inner"][0], record["tu"])
                    if target and target[1] is None:
                        table = self.records[target[0]]
                        reason = table["reasons"].get(str(node.get("name")))
                        if reason:
                            found.add(self._instability(table, str(node.get("name")), reason))
                stack.extend(node.get("inner", []))
            if found:
                uses[key] = sorted(found)
        return uses

    def _reachable(self, key, adjacency):
        seen, frontier = {key}, [key]
        while frontier:
            for target in adjacency.get(frontier.pop(), ()):
                if target not in seen:
                    seen.add(target)
                    frontier.append(target)
        return seen

    def prove(self, asts, binding, entries):
        self.member_records, self.record_aliases = {}, {}
        self.record_fields, self.records, self.global_records, self.external_globals = {}, {}, {}, {}
        self.field_types = {}
        self.typedef_sites, self.anonymous_sites = {}, {}
        self.anonymous_ranges = {}
        self.external_names = {}
        self.mutated_fields, self.mutated_tables = {}, {}
        self.escaped_fields, self.escaped_tables = {}, {}
        self.table_symbols, self.extern_decls = {}, {}
        self.function_gaps = {}
        self.index(asts)
        for tu, ast_data in asts:
            self._index_fields(ast_data, tu)
            self._link_typedefs(tu)
            self._index_globals(ast_data, tu)
        self._link_extern_tables()
        for tu, ast_data in asts:
            self._scan_table_writes(ast_data, tu)
        self._seal_tables()
        proofs, gaps = [], []
        uses = self._table_uses()
        adjacency = {}
        for edge in self.call_edges.values():
            adjacency.setdefault(edge["source"], set()).add(edge["target"])
        for entry in sorted(set(entries)):
            matches = [key for key, record in self.functions.items() if record["name"] == entry]
            if len(matches) != 1:
                gaps.append("unsupported_entry:" + entry)
                continue
            key = matches[0]
            record = self.functions[key]
            param_types = [_type(param) for param in record["parameters"]]
            kinds = [self._parameter_kind(param_type, record["tu"]) for param_type in param_types]
            if len(param_types) > 2 or "unsupported" in kinds:
                gaps.append("unsupported_entry_parameters:" + entry)
                continue
            domains = [{"integer": (0, 1), "output": (OUTPUT_SLOT,),
                        "input_record": (CALLER_INPUT,), "external": (CALLER_POINTER,)}[kind]
                       for kind in kinds]
            reachable = self._reachable(key, adjacency)
            for found in sorted({gap for callee in reachable for gap in uses.get(callee, [])}):
                gaps.append(found)
            for args in product(*domains):
                slots = {str(i): OutputSlot(param_types[i][:-1].rstrip()) for i, kind in enumerate(kinds)
                         if kind == "output"}
                try:
                    state = State(output_slots=slots)
                    values, regions = self._caller_inputs(state, record, kinds, args)
                    void_entry = _type(record["node"]).startswith("void (")
                    witnessed = []
                    for path, frame in self._invoke(key, state, values):
                        if frame.exit is None:
                            if not void_entry:
                                raise UnsupportedPath("entry_exit_unmodeled")
                            frame.exit = self._end_site(record["body"], record["tu"])
                        if not (type(frame.result) is int or frame.result is None
                                or isinstance(frame.result, (Reference, bytes))):
                            raise UnsupportedPath("entry_exit_unmodeled")
                        self._record_inputs(path, record, regions)
                        self._record_exit(path, frame)
                        witnessed.append({"entry": record["location"], "arguments": list(args), "events": path.events})
                    if len(proofs) + len(witnessed) > MAX_TRACES:
                        raise UnsupportedPath("trace_count_limit")
                    proofs.extend(witnessed)
                except (UnsupportedPath, RecursionError) as exc:
                    gaps.append(f"{entry}:{args}:{exc}")
        return {"schema_version": VERSION, "basis": "clang-json-ast-paths", "binding": binding,
                "traces": proofs, "gaps": sorted(set(gaps))}

    def _record_exit(self, path, frame):
        if len(path.events) >= MAX_TRACE_EVENTS:
            raise UnsupportedPath("trace_event_limit")
        live = {key for key, allocation in path.heap.items()
                if not allocation.released and not allocation.input}
        escaped = {slot.value.key for slot in path.output_slots.values()
                   if isinstance(slot.value, Reference) and slot.value.kind == "heap" and slot.value.key in live}
        returned = set()
        if isinstance(frame.result, Reference) and frame.result.kind == "heap" and frame.result.key in live:
            returned.add(frame.result.key)
            escaped.add(frame.result.key)
        for key, allocation in path.heap.items():
            # Only a region the caller can still reach keeps its contents
            # owned by the caller; a released region leaves them behind.
            if allocation.input and not allocation.released:
                escaped.update(ref.key for ref in allocation.fields.values()
                               if isinstance(ref, Reference) and ref.kind == "heap" and ref.key in live)
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
        if returned:
            data["returned"] = sorted(returned)
        path.events.append({"kind": "exit", "location": frame.exit, **data,
                            "evidence_ids": self.evidence_for("ast_memory_exit", frame.exit, stable_digest(data))})


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
