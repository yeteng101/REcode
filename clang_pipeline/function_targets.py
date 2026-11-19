"""Resolve function-pointer and C++ member targets from source-backed graph facts."""
from __future__ import annotations

import copy
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .clang_ast import evidence_id, stable_digest


@dataclass(frozen=True)
class ResolvedTarget:
    caller: str
    callee: str
    kind: str
    file: str
    line: int
    snippet: str
    reason: str
    confidence: float = 0.85


@dataclass
class MethodInfo:
    class_name: str
    method_name: str
    file: str
    line: int
    end_line: int
    kind: str = "method"


class FunctionTargetResolver:
    """Enrich a call graph with source-proven indirect and member targets.

    The resolver deliberately returns may-targets. A function-pointer field
    can have more than one compatible value, and every target keeps its source
    location as evidence.
    """

    def __init__(self, graph: dict[str, Any], source_root: str | Path):
        self.graph = copy.deepcopy(graph)
        self.source_root = Path(source_root).resolve()
        self._source_cache: dict[str, str] = {}
        self._line_cache: dict[str, list[str]] = {}
        self._node_by_name: dict[str, list[str]] = defaultdict(list)
        self._edge_keys: set[tuple[str, str, str]] = set()
        self._function_ranges: dict[tuple[str, str], tuple[int, int]] = {}
        self._method_info: dict[tuple[str, str], MethodInfo] = {}
        self._class_bases: dict[str, str] = {}
        self._added_nodes = 0
        self._added_edges = 0
        self._rebuild_indexes()

    def enrich(self) -> dict[str, Any]:
        self._resolve_c_struct_dispatch()
        self._resolve_c_function_pointer_returns()
        self._resolve_cpp_members_and_virtual_calls()
        return self.graph

    def _rebuild_indexes(self) -> None:
        self._node_by_name.clear()
        self._edge_keys.clear()
        for node in self.graph.get("nodes", []):
            node_id = str(node.get("id") or "")
            if not node_id:
                continue
            name = self._tail(str(node.get("name") or node_id))
            self._node_by_name[name].append(node_id)
            self._node_by_name[str(node.get("name") or "")].append(node_id)
        for edge in self.graph.get("edges", []):
            self._edge_keys.add(
                (
                    str(edge.get("source") or ""),
                    str(edge.get("target") or ""),
                    str(edge.get("kind") or ""),
                )
            )
        self._function_ranges.clear()
        by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for node in self.graph.get("nodes", []):
            if node.get("kind") != "function":
                continue
            file = self._normal_file(str(node.get("file") or ""))
            line = int(node.get("line") or 0)
            if file and line > 0:
                by_file[file].append(node)
        for file, nodes in by_file.items():
            lines = self._lines(file)
            nodes.sort(key=lambda item: int(item.get("line") or 0))
            for index, node in enumerate(nodes):
                start = int(node["line"])
                end = (
                    int(nodes[index + 1]["line"]) - 1
                    if index + 1 < len(nodes)
                    else len(lines)
                )
                self._function_ranges[(file, str(node.get("name") or ""))] = (
                    start,
                    max(start, end),
                )
        self._discover_source_function_ranges()

    def _discover_source_function_ranges(self) -> None:
        ignored = {
            "if",
            "for",
            "while",
            "switch",
            "catch",
            "return",
            "sizeof",
        }
        pattern = re.compile(
            r"(?m)^[ \t]*(?:[A-Za-z_~][\w:<>,\s*&]*?\s+)?"
            r"([A-Za-z_]\w*)\s*\([^;{}]*\)\s*"
            r"(?:const\s*)?(?:override\s*)?\{"
        )
        for file in self._iter_source_files():
            if Path(file).is_absolute() and not Path(file).is_relative_to(self.source_root):
                continue
            lines = self._lines(file)
            matches = []
            for match in pattern.finditer(self._read(file)):
                name = match.group(1)
                if name in ignored:
                    continue
                line = self._read(file).count("\n", 0, match.start()) + 1
                matches.append((line, name))
            for index, (line, name) in enumerate(matches):
                end = (
                    matches[index + 1][0] - 1
                    if index + 1 < len(matches)
                    else len(lines)
                )
                self._function_ranges.setdefault((file, name), (line, max(line, end)))

    def _normal_file(self, value: str) -> str:
        return value.replace("\\", "/").removeprefix("./")

    def _read(self, file: str) -> str:
        file = self._normal_file(file)
        if file in self._source_cache:
            return self._source_cache[file]
        candidates = [
            self.source_root / file,
            self.source_root.parent / file,
            Path(file),
            self.source_root / Path(file).name,
        ]
        text = ""
        for candidate in candidates:
            try:
                if candidate.is_file():
                    text = candidate.read_text(encoding="utf-8", errors="replace")
                    break
            except OSError:
                continue
        self._source_cache[file] = text
        self._line_cache[file] = text.splitlines()
        return text

    def _lines(self, file: str) -> list[str]:
        file = self._normal_file(file)
        if file not in self._line_cache:
            self._read(file)
        return self._line_cache.get(file, [])

    def _range_at(self, file: str, line: int) -> tuple[str, str] | None:
        file = self._normal_file(file)
        for (candidate_file, function), (start, end) in self._function_ranges.items():
            if candidate_file == file and start <= line <= end:
                return candidate_file, function
        return None

    def _function_node(self, file: str, function: str) -> str | None:
        file = self._normal_file(file)
        nodes = self._nodes()
        exact = [
            node_id
            for node_id in self._node_by_name.get(function, [])
            if self._normal_file(str(nodes.get(node_id, {}).get("file") or "")) == file
            and str(nodes.get(node_id, {}).get("name") or "") == function
        ]
        if exact:
            return exact[0]
        for node_id in self._node_by_name.get(function, []):
            node = nodes.get(node_id)
            if (
                node
                and self._normal_file(str(node.get("file") or "")) == file
                and self._tail(str(node.get("name") or "")) == self._tail(function)
            ):
                return node_id
        return None

    def _source_function_location(self, function: str) -> tuple[str, int] | None:
        pattern = re.compile(
            rf"\b{re.escape(self._tail(function))}\s*\([^;{{}}]*\)\s*\{{"
        )
        for file in self._iter_source_files():
            match = pattern.search(self._read(file))
            if match:
                line = self._read(file).count("\n", 0, match.start()) + 1
                return file, line
        return None

    def _nodes(self) -> dict[str, dict[str, Any]]:
        return {
            str(node.get("id") or ""): node
            for node in self.graph.get("nodes", [])
        }

    def _function_names(self) -> set[str]:
        return {
            self._tail(str(node.get("name") or ""))
            for node in self.graph.get("nodes", [])
            if str(node.get("kind") or "") == "function"
        }

    def _tail(self, value: str) -> str:
        return value.split(":")[-1].rsplit("::", 1)[-1]

    def _ensure_node(
        self,
        name: str,
        file: str,
        line: int,
        *,
        kind: str = "function",
    ) -> str:
        file = self._normal_file(file)
        nodes = self._nodes()
        for node in nodes.values():
            if (
                self._normal_file(str(node.get("file") or "")) == file
                and int(node.get("line") or 0) == line
                and self._tail(str(node.get("name") or "")) == self._tail(name)
            ):
                return str(node["id"])
        node_id = "fn:" + name
        if node_id in nodes:
            node_id += "_" + stable_digest([file, line, name])[:8]
        nodes[node_id] = {
            "id": node_id,
            "kind": kind,
            "name": name,
            "file": file,
            "line": line,
            "signature": "",
            "module": "",
        }
        self.graph.setdefault("nodes", []).append(nodes[node_id])
        self._node_by_name[self._tail(name)].append(node_id)
        self._node_by_name[name].append(node_id)
        self._added_nodes += 1
        return node_id

    def _add_target(
        self,
        caller: str,
        callee: str,
        file: str,
        line: int,
        *,
        kind: str,
        snippet: str,
        reason: str,
        confidence: float = 0.85,
        callee_file: str | None = None,
        callee_line: int | None = None,
    ) -> None:
        caller_id = self._function_node(file, caller)
        if caller_id is None:
            caller_location = self._source_function_location(caller)
            if caller_location is None:
                return
            caller_file, caller_line = caller_location
            caller_id = self._ensure_node(caller, caller_file, caller_line)
        callee_file = callee_file or file
        callee_line = callee_line or line
        callee_id = self._function_node(callee_file, callee)
        if callee_id is None:
            for candidate_id in self._node_by_name.get(callee, []):
                candidate = self._nodes().get(candidate_id)
                if (
                    candidate
                    and candidate.get("kind") == "function"
                    and str(candidate.get("name") or "") == callee
                ):
                    callee_id = candidate_id
                    break
        if callee_id is None:
            location = self._source_function_location(callee)
            if location:
                callee_file, callee_line = location
            callee_id = self._ensure_node(callee, callee_file, callee_line)
        if caller_id == callee_id:
            return
        edge_key = (caller_id, callee_id, kind)
        if edge_key in self._edge_keys:
            return
        ev_id = evidence_id(
            "resolver_target",
            file,
            line,
            f"{caller}->{callee}:{reason}",
        )
        evidence_ids = [ev_id]
        existing_evidence = {
            str(item.get("id") or "")
            for item in self.graph.get("evidence", [])
        }
        if ev_id not in existing_evidence:
            self.graph.setdefault("evidence", []).append(
                {
                    "id": ev_id,
                    "kind": "call_site",
                    "location": {
                        "file": file,
                        "line": line,
                        "snippet": snippet,
                    },
                    "role": "supports",
                }
            )
        edge_id = "e_" + stable_digest(
            [caller_id, callee_id, kind, file, line]
        )[:12]
        self.graph.setdefault("edges", []).append(
            {
                "id": edge_id,
                "source": caller_id,
                "target": callee_id,
                "kind": kind,
                "call_site": {
                    "file": file,
                    "line": line,
                    "column": 1,
                    "snippet": snippet,
                },
                "condition": None,
                "execution_context": None,
                "confidence": confidence,
                "evidence_ids": evidence_ids,
            }
        )
        self._edge_keys.add(edge_key)
        self._added_edges += 1

    def _iter_source_files(self) -> set[str]:
        files: set[str] = set()
        graph_files: set[Path] = set()
        for node in self.graph.get("nodes", []):
            if node.get("file"):
                file = self._normal_file(str(node["file"]))
                files.add(file)
                graph_files.add(Path(file))
        for edge in self.graph.get("edges", []):
            file = (edge.get("call_site") or {}).get("file")
            if file:
                file = self._normal_file(str(file))
                files.add(file)
                graph_files.add(Path(file))
        source_files = [
            path
            for path in self.source_root.rglob("*")
            if path.is_file() and path.suffix in {".c", ".cc", ".cpp", ".h", ".hpp"}
        ]
        if len(source_files) <= 200:
            selected = source_files
        else:
            selected = []
            directories = {
                (self.source_root / file).parent
                for file in graph_files
                if not file.is_absolute()
            }
            for directory in directories:
                if directory.is_dir():
                    selected.extend(
                        path
                        for path in directory.iterdir()
                        if path.is_file()
                        and path.suffix in {".c", ".cc", ".cpp", ".h", ".hpp"}
                    )
        for path in selected:
            try:
                files.add(path.relative_to(self.source_root).as_posix())
            except ValueError:
                continue
        return {file for file in files if self._read(file)}

    def _parse_struct_table(self) -> tuple[
        dict[str, list[str]],
        dict[str, tuple[str, dict[str, str]]],
    ]:
        fields: dict[str, list[str]] = {}
        instances: dict[str, tuple[str, dict[str, str]]] = {}
        files = self._iter_source_files()
        for file in files:
            text = self._read(file)
            for match in re.finditer(
                r"\bstruct\s+(\w+)\s*\{(?P<body>.*?)\}\s*;",
                text,
                re.S,
            ):
                body = match.group("body")
                field_names = []
                field_pattern = re.compile(
                    r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)"
                    r"|([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*;"
                )
                for line in body.splitlines():
                    for field in field_pattern.finditer(line):
                        field_names.append(field.group(1) or field.group(2))
                fields[match.group(1)] = field_names
        for file in files:
            text = self._read(file)
            for match in re.finditer(
                r"(?:static\s+)?struct\s+(\w+)\s+(\w+)\s*=\s*\{(?P<init>[^;]*)\}\s*;",
                text,
                re.S,
            ):
                struct_name, instance = match.group(1), match.group(2)
                values = self._split_top_level(match.group("init"))
                mapping: dict[str, str] = {}
                for index, value in enumerate(values):
                    if index >= len(fields.get(struct_name, [])):
                        break
                    value = value.strip().strip('"')
                    if re.fullmatch(r"[A-Za-z_]\w*", value):
                        mapping[fields[struct_name][index]] = value
                if mapping:
                    instances[instance] = (struct_name, mapping)
        return fields, instances

    def _split_top_level(self, value: str) -> list[str]:
        parts: list[str] = []
        current: list[str] = []
        depth = 0
        quote = ""
        for char in value:
            if quote:
                current.append(char)
                if char == quote:
                    quote = ""
                continue
            if char in {'"', "'"}:
                quote = char
                current.append(char)
            elif char in "({[":
                depth += 1
                current.append(char)
            elif char in ")}]":
                depth = max(0, depth - 1)
                current.append(char)
            elif char == "," and depth == 0:
                parts.append("".join(current).strip())
                current = []
            else:
                current.append(char)
        if current:
            parts.append("".join(current).strip())
        return parts

    def _resolve_c_struct_dispatch(self) -> None:
        fields, instances = self._parse_struct_table()
        function_names = self._function_names()
        returns: dict[str, set[str]] = defaultdict(set)
        for (file, function), (start, end) in self._function_ranges.items():
            for line_number in range(start, end + 1):
                lines = self._lines(file)
                if line_number > len(lines):
                    break
                line = lines[line_number - 1]
                match = re.search(
                    r"\breturn\s+&?\s*([A-Za-z_]\w*)\s*;",
                    line,
                )
                if not match:
                    continue
                value = match.group(1)
                if value in instances:
                    returns[function].add(value)
                elif value in function_names or self._source_function_location(value):
                    returns[function].add(value)

        variables: dict[tuple[str, str], set[str]] = {}
        for (file, function), (start, end) in self._function_ranges.items():
            lines = self._lines(file)
            for line_number in range(start, min(end, len(lines)) + 1):
                line = lines[line_number - 1]
                assignment = re.search(
                    r"\b(?:struct\s+\w+\s*\*\s*|[A-Za-z_]\w*)\s*"
                    r"([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\s*\(",
                    line,
                )
                if assignment:
                    variables[(function, assignment.group(1))] = set(
                        returns.get(assignment.group(2), set())
                    )

        for (file, function), (start, end) in self._function_ranges.items():
            lines = self._lines(file)
            for line_number in range(start, min(end, len(lines)) + 1):
                snippet = lines[line_number - 1].strip()
                for member_call in re.finditer(
                    r"\b([A-Za-z_]\w*)\s*->\s*([A-Za-z_]\w*)\s*\(",
                    snippet,
                ):
                    variable, field = member_call.group(1), member_call.group(2)
                    for instance in variables.get((function, variable), set()):
                        info = instances.get(instance)
                        if not info:
                            continue
                        target = info[1].get(field)
                        if target and (
                            target in function_names
                            or self._source_function_location(target)
                        ):
                            self._add_target(
                                function,
                                target,
                                file,
                                line_number,
                                kind="indirect_call",
                                snippet=snippet,
                                reason=f"{variable}->{field} from {instance}",
                            )
                direct_call = re.search(
                    r"\b([A-Za-z_]\w*)\s*\(",
                    snippet,
                )
                if direct_call:
                    variable = direct_call.group(1)
                    for target in variables.get((function, variable), set()):
                        if target and (
                            target in function_names
                            or self._source_function_location(target)
                        ):
                            self._add_target(
                                function,
                                target,
                                file,
                                line_number,
                                kind="indirect_call",
                                snippet=snippet,
                                reason=f"function pointer value in {variable}",
                            )

    def _resolve_c_function_pointer_returns(self) -> None:
        # The C callback return flow is handled by the C table resolver when
        # the returned function is stored in a function-pointer variable.
        return

    def _class_blocks(self, file: str) -> list[tuple[str, str, str, int]]:
        text = self._read(file)
        result = []
        pattern = re.compile(
            r"\bclass\s+([A-Za-z_]\w*)"
            r"(?:\s*:\s*(?:public|protected|private)\s+([A-Za-z_]\w*))?"
            r"\s*\{(?P<body>.*?)\n\};",
            re.S,
        )
        for match in pattern.finditer(text):
            body = match.group("body")
            class_name = match.group(1)
            base = match.group(2) or ""
            body_line = text.count("\n", 0, match.start("body")) + 1
            result.append((class_name, base, body, body_line))
        return result

    def _method_block_end(self, lines: list[str], start: int) -> int:
        depth = 0
        opened = False
        for index in range(start - 1, len(lines)):
            line = lines[index]
            for char in line:
                if char == "{":
                    depth += 1
                    opened = True
                elif char == "}":
                    depth -= 1
                    if opened and depth <= 0:
                        return index + 1
        return min(len(lines), start + 20)

    def _parse_methods(self) -> None:
        for file in self._iter_source_files():
            lines = self._lines(file)
            for class_name, base, body, body_line in self._class_blocks(file):
                if base:
                    self._class_bases[class_name] = base
                for local_line, line in enumerate(body.splitlines(), start=body_line):
                    constructor = re.match(
                        r"\s*(?:explicit\s+)?"
                        rf"({re.escape(class_name)})"
                        r"\s*\([^;{}]*\)\s*(?::[^{}]+)?\{",
                        line,
                    )
                    method = re.match(
                        r"\s*(?:virtual\s+)?"
                        r"(?:[A-Za-z_][\w:<>,\s*&]*?\s+)"
                        r"([A-Za-z_]\w*)\s*"
                        r"\([^;{}]*\)\s*(?:const\s*)?"
                        r"(?:override\s*)?(?::[^{}]+)?\{",
                        line,
                    )
                    if not constructor and not method:
                        continue
                    method_name = constructor.group(1) if constructor else method.group(1)
                    if not method_name:
                        continue
                    full_name = f"{class_name}::{method_name}"
                    end_line = self._method_block_end(lines, local_line)
                    self._method_info[(class_name, method_name)] = MethodInfo(
                        class_name,
                        method_name,
                        file,
                        local_line,
                        end_line,
                        "constructor" if constructor else "method",
                    )
                    self._function_ranges[(file, full_name)] = (local_line, end_line)
                    self._ensure_node(full_name, file, local_line)

    def _resolve_cpp_members_and_virtual_calls(self) -> None:
        self._parse_methods()
        if not self._method_info:
            return
        methods_by_class: dict[str, list[MethodInfo]] = defaultdict(list)
        for info in self._method_info.values():
            methods_by_class[info.class_name].append(info)
        derived: dict[str, list[str]] = defaultdict(list)
        for child, parent in self._class_bases.items():
            derived[parent].append(child)

        for (file, function), (start, end) in list(self._function_ranges.items()):
            lines = self._lines(file)
            if not lines or start > len(lines):
                continue
            type_vars: dict[str, str] = {}
            signature = lines[start - 1] if start <= len(lines) else ""
            for match in re.finditer(
                r"\b(?:const\s+)?([A-Za-z_]\w*)\s*[&*]\s*([A-Za-z_]\w*)",
                signature,
            ):
                type_vars[match.group(2)] = match.group(1)
            caller_is_method = "::" in function
            caller_class = function.split("::", 1)[0] if caller_is_method else ""
            for line_number in range(start, min(end, len(lines)) + 1):
                snippet = lines[line_number - 1].strip()
                for type_name, variable in re.findall(
                    r"\b([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*(?:[=;({])",
                    snippet,
                ):
                    type_vars[variable] = type_name
                constructor = re.search(
                    r"\b([A-Za-z_]\w*)\s+([A-Za-z_]\w*)\s*\(",
                    snippet,
                )
                if constructor and constructor.group(1) in methods_by_class:
                    self._add_target(
                        function,
                        f"{constructor.group(1)}::{constructor.group(1)}",
                        file,
                        line_number,
                        kind="direct_call",
                        snippet=snippet,
                        reason="object construction",
                        callee_line=self._method_info[
                            (constructor.group(1), constructor.group(1))
                        ].line
                        if (constructor.group(1), constructor.group(1)) in self._method_info
                        else line_number,
                    )
                member_call = re.search(
                    r"\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\(",
                    snippet,
                )
                if member_call:
                    variable, method = member_call.group(1), member_call.group(2)
                    type_name = type_vars.get(variable)
                    if type_name in methods_by_class:
                        self._add_method_targets(
                            function,
                            type_name,
                            method,
                            file,
                            line_number,
                            snippet,
                            derived,
                        )
                    elif type_name in derived:
                        for child in derived[type_name]:
                            self._add_method_targets(
                                function,
                                child,
                                method,
                                file,
                                line_number,
                                snippet,
                                derived,
                            )
                if caller_is_method and caller_class:
                    for target in self._function_names():
                        if target in {caller_class, function.split("::", 1)[-1]}:
                            continue
                        if re.search(rf"\b{re.escape(target)}\s*\(", snippet):
                            self._add_target(
                                function,
                                target,
                                file,
                                line_number,
                                kind="direct_call",
                                snippet=snippet,
                                reason="method body call",
                            )

    def _add_method_targets(
        self,
        caller: str,
        class_name: str,
        method: str,
        file: str,
        line: int,
        snippet: str,
        derived: dict[str, list[str]],
    ) -> None:
        info = self._method_info.get((class_name, method))
        if info:
            self._add_target(
                caller,
                f"{class_name}::{method}",
                file,
                line,
                kind="direct_call",
                snippet=snippet,
                reason=f"member call {class_name}::{method}",
                callee_line=info.line,
            )
        for child in derived.get(class_name, []):
            child_info = self._method_info.get((child, method))
            if child_info:
                self._add_target(
                    caller,
                    f"{child}::{method}",
                    file,
                    line,
                    kind="indirect_call",
                    snippet=snippet,
                    reason=f"virtual call through {class_name}",
                    confidence=0.8,
                    callee_file=child_info.file,
                    callee_line=child_info.line,
                )


def enrich_call_graph(
    graph: dict[str, Any],
    source_root: str | Path,
) -> dict[str, Any]:
    """Convenience entry point for callers that do not need the class."""
    return FunctionTargetResolver(graph, source_root).enrich()
