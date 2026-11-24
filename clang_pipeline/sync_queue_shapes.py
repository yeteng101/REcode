"""Structural summaries for a small set of C intrusive-list operations."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from .clang_ast import iter_nodes, stable_digest
from .sync_field_bindings import _assignment, _pointee, _pointer_expression, _reference, _site, _type
from .sync_field_calls import REDIRECTED_FUNCTION


@dataclass(frozen=True)
class Member:
    identity: str
    owner: str
    name: str
    declaration: dict
    embedded: str | None
    points_to: str | None


@dataclass(frozen=True)
class QueueOperation:
    kind: str
    source: int
    destination: int | None
    link: str
    definition: dict
    helper: dict | None = None


def _one_statement(node):
    if node.get("kind") != "CompoundStmt":
        return node
    children = node.get("inner", [])
    return children[0] if len(children) == 1 else {}


class QueueShapes:
    def __init__(self, analyzer, tu, ast):
        self.analyzer, self.tu = analyzer, tu
        self.records = defaultdict(set)
        self.typedefs = defaultdict(set)
        self.members = {}
        self.definitions = []
        self.operations = {}
        self.excluded = set()
        self.splits = {}
        self.initializers = {}
        self.empty_checks = {}
        declarations = []
        for node in iter_nodes(ast):
            kind = node.get("kind")
            if kind == "TypedefDecl" and node.get("name"):
                self.typedefs[node["name"]].add(_type(node))
            elif kind == "RecordDecl" and node.get("tagUsed") == "struct" and node.get("name") and node.get("completeDefinition"):
                location = self.location(node)
                if location:
                    record = stable_digest([node["name"], location])[:20]
                    self.records["struct " + node["name"]].add(record)
                    declarations.append((record, node))
            elif kind == "FunctionDecl":
                identity = analyzer.function_references[tu, node["id"]]
                children = node.get("inner", [])
                if any(child.get("kind") in REDIRECTED_FUNCTION for child in children):
                    self.excluded.add(identity)
                body = next((child for child in children if child.get("kind") == "CompoundStmt"), None)
                if body is not None and self.location(node):
                    params = [child for child in children if child.get("kind") == "ParmVarDecl"]
                    self.definitions.append((identity, node, params, body))

        for record, node in declarations:
            for field in node.get("inner", []):
                if field.get("kind") != "FieldDecl" or not field.get("name"):
                    continue
                location = self.location(field)
                if location:
                    identity = stable_digest([record, field["name"], location])[:20]
                    self.members[field["id"]] = Member(identity, record, field["name"], location,
                                                       self.record(_type(field)), self.record(_pointee(field)))

    def location(self, node):
        location = self.analyzer.location(node, self.tu)
        return _site(location) if location else None

    def record(self, spelling):
        seen = set()
        while spelling in self.typedefs and spelling not in seen:
            seen.add(spelling)
            names = self.typedefs[spelling]
            if len(names) != 1:
                return None
            spelling = next(iter(names))
        records = self.records.get(spelling, ())
        return next(iter(records)) if len(records) == 1 else None

    def path(self, expression, parameters, record):
        node = _pointer_expression(expression)
        links = []
        while node.get("kind") == "MemberExpr" and node.get("isArrow"):
            field = self.members.get(node.get("referencedMemberDecl"))
            if not field or field.owner != record or field.points_to != record:
                return None
            links.append(field.identity)
            node = _pointer_expression(node["inner"][0])
        index = parameters.get(_reference(node))
        return (index, *reversed(links)) if index is not None else None

    def summarize(self):
        definitions = Counter(identity for identity, _, _, _ in self.definitions)
        wrappers = []
        for identity, node, params, body in self.definitions:
            if identity in self.excluded or definitions[identity] != 1 or not 1 <= len(params) <= 3:
                continue
            record = self.record(_pointee(params[0]))
            if not record or any(self.record(_pointee(param)) != record for param in params):
                continue
            parameter_ids = {param["id"]: index for index, param in enumerate(params)}
            statements = body.get("inner", [])
            location = self.location(node)
            if len(statements) == 1 and statements[0].get("kind") == "ReturnStmt" and statements[0].get("inner"):
                value = _pointer_expression(statements[0]["inner"][0])
                path = self.path(value, parameter_ids, record)
                if path and len(path) == 2:
                    self.operations[identity] = QueueOperation("read", path[0], None, path[1], location)
                elif len(params) == 1 and value.get("kind") == "BinaryOperator" and value.get("opcode") == "==":
                    operands = [self.path(operand, parameter_ids, record) for operand in value.get("inner", [])]
                    if len(operands) == 2 and (0,) in operands:
                        link = next((operand[1] for operand in operands if operand and len(operand) == 2), None)
                        if link:
                            self.empty_checks[identity] = link
            elif statements and all(_assignment(statement) for statement in statements):
                writes = [tuple(self.path(operand, parameter_ids, record) for operand in statement["inner"])
                          for statement in statements]
                self._summarize_writes(identity, len(params), writes, location)
            elif len(params) == 2 and len(statements) == 1 and statements[0].get("kind") in {"CallExpr", "IfStmt"}:
                wrappers.append((identity, statements[0], parameter_ids, record, location))
        for identity, statement, parameters, record, location in wrappers:
            operation = self._transfer(statement, parameters, record, location)
            if operation:
                self.operations[identity] = operation

    def _summarize_writes(self, identity, count, writes, location):
        if any(left is None or right is None for left, right in writes):
            return
        if count == 1 and all(len(left) == 2 and right == (0,) for left, right in writes):
            self.initializers[identity] = {left[1] for left, _ in writes}
        if count == 2 and len(writes) == 1:
            left, right = writes[0]
            if len(left) == len(right) == 2 and left[1] == right[1] and left[0] != right[0]:
                self.operations[identity] = QueueOperation("transfer", right[0], left[0], left[1], location)

        # Insertion links an element into the root's chain before changing the root.
        root, item = writes[-1]
        if count == 2 and len(root) == 2 and len(item) == 1 and root[0] != item[0]:
            head, back = root
            element = item[0]
            if writes == [((element, back), (head, back)), (root, item)]:
                self.operations[identity] = QueueOperation("insert", head, element, back, location)
            elif len(writes) == 4:
                for left, right in writes[:2]:
                    if len(left) != 2 or left[0] != element or left[1] == back or right != (head,):
                        continue
                    forward = left[1]
                    expected = {((element, forward), (head,)), ((element, back), (head, back))}
                    if set(writes[:2]) == expected and writes[2] == ((element, back, forward), item):
                        self.operations[identity] = QueueOperation("insert", head, element, forward, location)

        if count == 3 and len(writes) == 6:
            (target, preserved), _, (selected, boundary), *_ = writes
            if len(target) != 2 or len(preserved) != 2 or len(selected) != 2 or len(boundary) != 1:
                return
            destination, back = target
            source = preserved[0]
            pivot = boundary[0]
            forward = selected[1]
            if len({destination, source, pivot}) != 3 or forward == back:
                return
            if writes == [((destination, back), (source, back)), ((destination, back, forward), (destination,)),
                          ((destination, forward), (pivot,)), ((source, back), (pivot, back)),
                          ((source, back, forward), (source,)), ((pivot, back), (destination,))]:
                self.splits[identity] = (source, pivot, destination, forward, back, location)

    def _call(self, node, parameters, record):
        node = _one_statement(node)
        if node.get("kind") != "CallExpr" or not node.get("inner"):
            return None, ()
        target, *args = node["inner"]
        return self.analyzer.function_target(target, self.tu), tuple(self.path(arg, parameters, record) for arg in args)

    def _transfer(self, statement, parameters, record, location):
        conditional = statement.get("kind") == "IfStmt"
        branches = statement.get("inner", [])[1:] if conditional else [statement]
        if conditional and (len(branches) != 2 or statement.get("hasInit")):
            return None
        calls = [self._call(branch, parameters, record) for branch in branches]
        for index, (target, arguments) in enumerate(calls):
            if target not in self.splits or len(arguments) != 3 or any(argument is None for argument in arguments):
                continue
            source_index, pivot_index, dest_index, forward, back, helper = self.splits[target]
            source, pivot, destination = arguments[source_index], arguments[pivot_index], arguments[dest_index]
            if len(source) != 1 or len(destination) != 1 or source == destination or pivot != (*source, forward):
                continue
            if conditional:
                other_target, other_arguments = calls[1 - index]
                check, checked_arguments = self._call(statement["inner"][0], parameters, record)
                if index != 1 or self.empty_checks.get(check) != forward or checked_arguments != (source,):
                    continue
                if other_arguments != (destination,) or not {forward, back} <= self.initializers.get(other_target, set()):
                    continue
            return QueueOperation("transfer", source[0], destination[0], forward, location, helper)
        return None
