from __future__ import annotations

import re
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path
from typing import Any

from .clang_ast import evidence_id, stable_digest, utc_now


ACQUIRE_TARGETS = frozenset(
    {
        "uv__malloc",
        "uv__strdup",
        "uv__strndup",
        "malloc",
        "calloc",
        "realloc",
        "uv__calloc",
        "uv__realloc",
        "uv__reallocf",
        "open",
        "uv__open_cloexec",
        "uv__make_pipe",
        "uv__fs_open",
        "uv__stream_open",
        "dlopen",
        "scandir",
        "getaddrinfo",
        "uv_getaddrinfo",
        "uv__fs_opendir",
        "zmalloc",
        "zcalloc",
        "zrealloc",
        "ztrymalloc",
        "ztrycalloc",
        "ztryrealloc",
        "sdsnew",
        "sdsnewlen",
        "sdsempty",
        "sdsdup",
    }
)

RELEASE_TARGETS = frozenset(
    {
        "uv__free",
        "free",
        "close",
        "uv__close",
        "uv__close_nocancel",
        "uv__close_nocheckstdio",
        "uv__fs_close",
        "uv__fs_closedir",
        "uv__fs_scandir_cleanup",
        "uv__fs_scandir_free",
        "uv_fs_req_cleanup",
        "dlclose",
        "uv_freeaddrinfo",
        "freeaddrinfo",
        "closedir",
        "zfree",
        "sdsfree",
        "sdsfreesplitres",
    }
)

MEMORY_TARGETS = frozenset(
    {
        "uv__malloc",
        "uv__strdup",
        "uv__strndup",
        "malloc",
        "calloc",
        "realloc",
        "uv__calloc",
        "uv__realloc",
        "uv__reallocf",
        "uv__free",
        "free",
        "scandir",
        "getaddrinfo",
        "uv_freeaddrinfo",
        "freeaddrinfo",
        "zmalloc",
        "zcalloc",
        "zrealloc",
        "ztrymalloc",
        "ztrycalloc",
        "ztryrealloc",
        "zfree",
        "sdsnew",
        "sdsnewlen",
        "sdsempty",
        "sdsdup",
        "sdsfree",
        "sdsfreesplitres",
    }
)

FILE_DESCRIPTOR_TARGETS = frozenset(
    {
        "open",
        "close",
        "uv__open_cloexec",
        "uv__make_pipe",
        "uv__fs_open",
        "uv__stream_open",
        "uv__close",
        "uv__close_nocancel",
        "uv__close_nocheckstdio",
        "uv__fs_close",
        "uv__fs_closedir",
        "uv__fs_opendir",
        "uv__stream_close",
        "closedir",
    }
)

HANDLE_TARGETS = frozenset({"dlopen", "dlclose"})

SOURCE_RESOURCE_IDS = {
    "uv__fs_open": "rf_libuv_fs_fd_v1",
    "uv__fs_close": "rf_libuv_fs_fd_v1",
    "uv__getpwuid_r": "rf_libuv_getpwuid_buf_v1",
    "uv__async_start": "rf_libuv_async_fd_v1",
    "uv__async_stop": "rf_libuv_async_fd_v1",
    "uv_freeaddrinfo": "rf_libuv_getaddrinfo_v1",
    "uv__fs_scandir": "rf_libuv_scandir_entries_v1",
    "uv__fs_scandir_cleanup": "rf_libuv_scandir_entries_v1",
    "uv_dlopen": "rf_libuv_dlopen_handle_v1",
    "uv_dlclose": "rf_libuv_dlopen_handle_v1",
}

TARGET_RESOURCE_IDS = {
    "uv__fs_close": "rf_libuv_fs_fd_v1",
    "uv__fs_opendir": "rf_libuv_fs_fd_v1",
    "uv__fs_scandir_cleanup": "rf_libuv_scandir_entries_v1",
    "uv__fs_scandir_free": "rf_libuv_scandir_entries_v1",
    "dlclose": "rf_libuv_dlopen_handle_v1",
    "dlopen": "rf_libuv_dlopen_handle_v1",
    "scandir": "rf_libuv_scandir_entries_v1",
    "getaddrinfo": "rf_libuv_getaddrinfo_v1",
    "uv_freeaddrinfo": "rf_libuv_getaddrinfo_v1",
    "freeaddrinfo": "rf_libuv_getaddrinfo_v1",
}

RESOURCE_TYPES = {
    "rf_libuv_fs_fd_v1": "file_descriptor",
    "rf_libuv_getpwuid_buf_v1": "memory",
    "rf_libuv_async_fd_v1": "file_descriptor",
    "rf_libuv_getaddrinfo_v1": "memory",
    "rf_libuv_scandir_entries_v1": "memory",
    "rf_libuv_dlopen_handle_v1": "handle",
}

PAIR_RULES = {
    # target function suffix -> expected release function suffix
    "uv__malloc": "uv__free",
    "uv__strdup": "uv__free",
    "uv__strndup": "uv__free",
    "malloc": "free",
    "calloc": "free",
    "realloc": "free",
    "open": "close",
    "uv__fs_open": "uv__fs_close",
    "uv__open_cloexec": "uv__close",
    "uv__make_pipe": "uv__close",
    "uv__stream_open": "uv__stream_close",
    "uv__fs_opendir": "uv__fs_closedir",
    "dlopen": "dlclose",
    "scandir": "uv__fs_scandir_cleanup",
    "getaddrinfo": "uv_freeaddrinfo",
    "uv_getaddrinfo": "uv_freeaddrinfo",
    "zmalloc": "zfree",
    "zcalloc": "zfree",
    "zrealloc": "zfree",
    "ztrymalloc": "zfree",
    "ztrycalloc": "zfree",
    "ztryrealloc": "zfree",
    "sdsnew": "sdsfree",
    "sdsnewlen": "sdsfree",
    "sdsempty": "sdsfree",
    "sdsdup": "sdsfree",
}

SOURCE_PAIR_RULES = {
    "uv__async_start": "uv__async_stop",
    "uv__getpwuid_r": "uv__getpwuid_r",
    "uv_fs_open": "uv_fs_close",
    "uv_dlopen": "uv_dlclose",
    "uv_getaddrinfo": "uv_freeaddrinfo",
    "uv__fs_scandir": "uv__fs_scandir_cleanup",
}


def _tail(value: str) -> str:
    value = value.split(":")[-1]
    return value.rsplit("::", 1)[-1]


def _classify(target: str) -> str | None:
    if target in RELEASE_TARGETS:
        return "release"
    if target in ACQUIRE_TARGETS:
        return "acquire"
    return None


def _resource_type(resource_id: str, target: str, source: str) -> str:
    if resource_id in RESOURCE_TYPES:
        return RESOURCE_TYPES[resource_id]
    if target in HANDLE_TARGETS:
        return "handle"
    if target in MEMORY_TARGETS:
        return "memory"
    if target in FILE_DESCRIPTOR_TARGETS or source in {
        "uv__async_start",
        "uv__async_stop",
        "uv_fs_open",
        "uv__fs_close",
    }:
        return "file_descriptor"
    if source in {"uv_dlopen", "uv_dlclose", "uv__getaddrinfo_work", "uv__fs_scandir"}:
        return RESOURCE_TYPES[SOURCE_RESOURCE_IDS[source]]
    return "other"


def _resource_key(snippet: str, fallback: str) -> str:
    text = snippet.strip()
    assignment = ASSIGNMENT_PATTERN.search(text)
    if assignment:
        expression = _simple_expression(assignment.group("lhs"))
        if expression:
            return expression
    arguments = re.search(r"\(([^()]*)\)", text)
    if arguments:
        first = arguments.group(1).split(",", 1)[0].strip()
        if first:
            return first.lstrip("&*").strip()
    return fallback


def _resource_identity(source: str, target: str, expression: str) -> tuple[str, str]:
    if target in TARGET_RESOURCE_IDS:
        resource_id = TARGET_RESOURCE_IDS[target]
    elif target in MEMORY_TARGETS and source != "uv__getpwuid_r":
        resource_id = None
    else:
        resource_id = SOURCE_RESOURCE_IDS.get(source)
    return resource_id or expression, expression


def _operation(edge: dict[str, Any], names: dict[str, str], kind: str) -> dict[str, Any]:
    site = edge.get("call_site") or {}
    target = names[edge["target"]]
    source = names[edge["source"]]
    snippet = str(site.get("snippet") or "")
    assignment = ASSIGNMENT_PATTERN.search(snippet) if kind == "acquire" else None
    arguments = _call_arguments(snippet, target, direct=True)
    if assignment:
        expression = assignment["lhs"]
    elif kind == "acquire" and _returns_call(snippet, target):
        expression = "$return"
    elif kind == "release" and arguments:
        expression = arguments[0]
    else:
        expression = _resource_key(snippet, target)
    resource, resource_expression = _resource_identity(source, target, expression)
    line = int(site.get("line", 0))
    return {
        "operation_id": "op_" + str(edge.get("id", ""))[-10:],
        "kind": kind,
        "api": target,
        "function": source,
        "file": site.get("file", ""),
        "line": line,
        "snippet": snippet,
        "evidence_ids": list(edge.get("evidence_ids", [])),
        "status": "unconfirmed",
        "confidence": 0.75,
        "_pair_key": _pair_key(source, target),
        "_resource_id": resource,
        "_resource_expression": resource_expression,
        "_resource_type": _resource_type(resource, target, source),
        "_edge_id": edge.get("id", ""),
        "_scope": str(edge.get("source", "")),
        "_site_line": line,
    }


def _pair_key(source: str, target: str) -> str:
    if source in SOURCE_PAIR_RULES:
        return f"family:{SOURCE_PAIR_RULES[source]}"
    if target in RELEASE_TARGETS:
        return f"family:{target}"
    if target in PAIR_RULES:
        return f"family:{PAIR_RULES[target]}"
    return f"single:{source}:{target}"


ASSIGNMENT_PATTERN = re.compile(
    r"(?P<lhs>[A-Za-z_]\w*(?:(?:->|\.)[A-Za-z_]\w*|\[[^\]]+\])*)"
    r"\s*=(?!=)\s*(?P<rhs>[^;]+);"
)
RETURN_PATTERN = re.compile(r"^\s*return(?:\s+(?P<expr>[^;]+))?;")
LVALUE = re.compile(r"[A-Za-z_]\w*(?:(?:->|\.)[A-Za-z_]\w*)*")
CONTROL_FLOW = re.compile(r"\b(?:if|else|for|while|do|switch|goto|return|throw|try)\b|[?:#]")
NULL_RETURNS = {"NULL", "nullptr", "0", "((void *)0)", "((void*)0)"}
MAX_CALL_DEPTH = 12
MAX_FORWARDED_RELEASES = 10_000
C_NONCODE = re.compile(r"/\*[\s\S]*?\*/|//[^\n]*|\x22(?:\\.|[^\\\x22])*\x22|'(?:\\.|[^\\'])*'")


def _simple_expression(value: str) -> str | None:
    cleaned = re.sub(r"^[\s(&*]+|[\s)]+$", "", value.strip())
    match = re.fullmatch(
        r"[A-Za-z_]\w*(?:(?:->|\.)[A-Za-z_]\w*|\[[^\]]+\])*",
        cleaned,
    )
    return match.group(0) if match else None


def _returns_call(snippet: str, name: str) -> bool:
    return (bool(re.match(rf"^\s*return\s+{re.escape(name)}\s*\(", snippet))
            and _call_arguments(snippet, name, direct=True) is not None)


def _call_arguments(snippet: str, name: str, *, direct: bool = False) -> list[str] | None:
    match = re.search(rf"\b{re.escape(name)}\s*\(", snippet)
    if not match:
        return None
    start = match.end()
    level = 0
    quote = None
    escaped = False
    result = []
    for index in range(start, len(snippet)):
        char = snippet[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char in "([":
            level += 1
        elif char == ")":
            if level == 0:
                if direct and snippet[index + 1:].strip() not in {"", ";"}:
                    return None
                final = snippet[start:index].strip()
                return result + [final] if result or final else []
            level -= 1
        elif char == "]":
            level -= 1
        elif char == "," and level == 0:
            result.append(snippet[start:index].strip())
            start = index + 1
    return None


def _parameters(function: str, declaration: str) -> tuple[str, ...]:
    match = re.search(rf"\b{re.escape(function)}\s*\(([^()]*)\)\s*\{{", declaration)
    if not match or match[1].strip() in {"", "void"}:
        return ()
    result = []
    for parameter in match[1].split(","):
        name = re.search(r"\b([A-Za-z_]\w*)\s*$", parameter)
        result.append(name[1] if name and parameter[:name.start()].strip() else "")
    return tuple(result)


def _scope_writes(lines: list[str], start: int) -> set[int]:
    """Limit local value tracing to observable straight-line definitions."""
    sanitized = C_NONCODE.sub(lambda match: re.sub(r"[^\n]", " ", match[0]), "\n".join(lines))
    depth = 0
    previous = ""
    result = set()
    for number, line in enumerate(sanitized.splitlines(), start):
        line = line.strip()
        if line.startswith("#"):
            break
        if depth == 1 and previous.endswith(("{", "}", ";")) and line and not CONTROL_FLOW.search(line):
            result.add(number)
        depth += line.count("{") - line.count("}")
        if line:
            previous = line
    return result


def _resolve_source_path(source_root: Path, file: str) -> Path:
    path = Path(file)
    if path.is_absolute():
        return path
    candidates = [source_root / path, Path.cwd() / path]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _function_ranges(
    graph: dict[str, Any],
    source_root: Path | None,
) -> dict[tuple[str, str, str], tuple[int, int]]:
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in graph.get("nodes", []):
        if node.get("kind") != "function":
            continue
        file = str(node.get("file") or "")
        line = int(node.get("line") or 0)
        if not file or line <= 0:
            continue
        by_file[file].append(node)
    ranges: dict[tuple[str, str, str], tuple[int, int]] = {}
    for file, nodes in by_file.items():
        path = _resolve_source_path(
            source_root or Path.cwd(),
            file,
        )
        try:
            line_count = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            line_count = 0
        nodes.sort(key=lambda item: int(item["line"]))
        for index, node in enumerate(nodes):
            start = int(node["line"])
            end = (
                int(nodes[index + 1]["line"]) - 1
                if index + 1 < len(nodes)
                else line_count
            )
            ranges[(file, str(node["id"]), str(node["name"]))] = (start, max(start, end))
    return ranges


def _scan_source_facts(
    graph: dict[str, Any],
    source_root: Path,
) -> dict[str, Any]:
    facts: dict[str, Any] = {"assignments": [], "returns": [], "evidence": {}, "scopes": {}}
    ranges = _function_ranges(graph, source_root)
    file_cache: dict[str, list[str]] = {}
    for (file, scope, function), (start, end) in ranges.items():
        path = _resolve_source_path(source_root, file)
        if file not in file_cache:
            try:
                file_cache[file] = path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                continue
        lines = file_cache[file]
        body = lines[start - 1 : end]
        facts["scopes"][scope] = {
            "start": start, "lines": body,
            "parameters": _parameters(function, "\n".join(body[:12])),
            "writes": _scope_writes(body, start),
        }
        for line in range(start, min(end, len(lines)) + 1):
            snippet = lines[line - 1].strip()
            assignment = ASSIGNMENT_PATTERN.search(snippet)
            if assignment:
                lhs = _simple_expression(assignment.group("lhs"))
                rhs = assignment.group("rhs").strip()
                if lhs:
                    evidence = evidence_id("assignment", file, line, lhs)
                    facts["assignments"].append(
                        {
                            "scope": scope,
                            "file": file,
                            "line": line,
                            "lhs": lhs,
                            "rhs": rhs,
                            "snippet": snippet,
                            "evidence_id": evidence,
                        }
                    )
                    facts["evidence"].setdefault(
                        evidence,
                        {
                            "id": evidence,
                            "kind": "assignment",
                            "location": {"file": file, "line": line, "snippet": snippet},
                            "role": "supports",
                        },
                    )
            returned = RETURN_PATTERN.match(snippet)
            if returned:
                expression = (returned.group("expr") or "").strip()
                if snippet:
                    evidence = evidence_id(
                        "control_flow", file, line, f"return:{expression}"
                    )
                    facts["returns"].append(
                        {
                            "scope": scope,
                            "file": file,
                            "line": line,
                            "expression": expression,
                            "snippet": snippet,
                            "evidence_id": evidence,
                        }
                    )
                    facts["evidence"].setdefault(
                        evidence,
                        {
                            "id": evidence,
                            "kind": "control_flow",
                            "location": {"file": file, "line": line, "snippet": snippet},
                            "role": "supports",
                        },
                    )
    return facts


class _Bindings:
    """Index observed definitions by function and source order."""

    def __init__(self, facts: dict[str, Any]) -> None:
        self.assignments = defaultdict(lambda: defaultdict(list))
        for fact in facts["assignments"]:
            self.assignments[fact["scope"]][fact["lhs"]].append(fact)
        self.allocations = defaultdict(dict)
        self.scopes = facts["scopes"]

    def add(self, operation: dict[str, Any]) -> None:
        key = operation["_scope"], operation["_resource_expression"]
        if key[0] in self.scopes and operation["_site_line"] not in self.scopes[key[0]]["writes"]:
            return
        self.allocations[key][operation["_site_line"]] = operation

    def definitions(self, scope: str, expression: str, line: int) -> list[dict[str, Any]]:
        own = self.assignments.get(scope, {}).get(expression, [])
        return own[:bisect_right(own, line, key=lambda fact: fact["line"])]

    def trace(
        self, scope: str, expression: str, line: int
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        root = expression.split("->")[0].split(".")[0]
        chain = []
        for _ in range(MAX_CALL_DEPTH):
            if not LVALUE.fullmatch(expression):
                return None, chain
            assignments = self.definitions(scope, expression, line)
            acquisitions = self.allocations.get((scope, expression), {})
            allocation_line = max((start for start in acquisitions if start <= line), default=0)
            assignment = assignments[-1] if assignments else None
            binding_line = max(allocation_line, assignment["line"] if assignment else 0)
            # A write to the containing pointer invalidates an older member binding.
            root_writes = self.definitions(scope, root, line) if expression != root else []
            if root_writes and root_writes[-1]["line"] > binding_line:
                return None, chain
            if assignment:
                chain.append(assignment)
            if allocation_line and binding_line == allocation_line:
                # Multiple lexical definitions may be on different branches.
                if len(assignments) > 1 or sum(start <= line for start in acquisitions) > 1:
                    return None, chain
                return acquisitions[allocation_line], chain
            if not assignment or len(assignments) != 1:
                return None, chain
            if scope in self.scopes and assignment["line"] not in self.scopes[scope]["writes"]:
                return None, chain
            expression, line = assignment["rhs"], assignment["line"] - 1
            root = expression.split("->")[0].split(".")[0]
        return None, chain


def _location(operation: dict[str, Any]) -> dict[str, Any]:
    return {key: operation[key] for key in ("file", "line", "function", "api")}


def _instance(scope: str, site: str) -> str:
    return "ri_" + stable_digest([scope, site])[:16]


def _returning_allocations(
    operations: list[dict[str, Any]], facts: dict[str, Any], bindings: _Bindings
) -> dict[str, dict[str, Any]]:
    by_scope = defaultdict(list)
    for fact in facts["returns"]:
        by_scope[fact["scope"]].append(fact)
    direct_returns = defaultdict(dict)
    for operation in operations:
        if operation["_resource_expression"] == "$return":
            direct_returns[operation["_scope"]][operation["_site_line"]] = operation

    summaries = {}
    for scope, returns in by_scope.items():
        sources = {}
        escape_evidence = set()
        unknown = False
        for returned in returns:
            expression = returned["expression"]
            if expression in NULL_RETURNS:
                continue
            operation = direct_returns[scope].get(returned["line"])
            if operation is None:
                operation, _ = bindings.trace(scope, expression, returned["line"] - 1)
            if operation is None:
                unknown = True
            else:
                sources[operation["resource_instance_id"]] = operation
                escape_evidence.add(returned["evidence_id"])
        if len(sources) == 1 and not unknown:
            scope_result = next(iter(sources.values()))
            scope_result["_escape_evidence"] = escape_evidence
            summaries[scope] = scope_result
    return summaries


def _call_site(edge: dict[str, Any], source: str, target: str) -> dict[str, Any]:
    site = edge.get("call_site") or {}
    return {
        "file": site.get("file", ""),
        "line": int(site.get("line", 0)),
        "function": source,
        "api": target,
        "snippet": str(site.get("snippet", "")),
        "evidence_ids": list(edge.get("evidence_ids", [])),
    }


def _track_factory_calls(
    edges: list[dict[str, Any]], names: dict[str, str], facts: dict[str, Any],
    acquisitions: list[dict[str, Any]], bindings: _Bindings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    factories = []
    seen = set()
    for _ in range(MAX_CALL_DEPTH):
        summaries = _returning_allocations(acquisitions + factories, facts, bindings)
        pending = []
        for edge in edges:
            key, caller, callee = edge["id"], edge["source"], edge["target"]
            parent = summaries.get(callee)
            if (parent is None or key in seen or key in parent.get("_call_edges", ())
                    or _classify(names[callee]) is not None):
                continue
            site = _call_site(edge, names[caller], names[callee])
            snippet, target = site["snippet"], site["api"]
            match = re.search(rf"(?P<lhs>{LVALUE.pattern})\s*=\s*{re.escape(target)}\s*\(", snippet)
            if match and _call_arguments(snippet, target, direct=True) is not None:
                expression = match["lhs"]
            elif _returns_call(snippet, target):
                expression = "$return"
            else:
                continue
            seen.add(key)
            origin = parent.get("allocation_origin", _location(parent))
            evidence_ids = list(dict.fromkeys([*parent["evidence_ids"], *site["evidence_ids"]]))
            transfer = {
                **site,
                "operation_id": "transfer_" + stable_digest(key)[:16],
                "kind": "transfer",
                "resource_instance_id": _instance(caller, key),
                "from_function": names[callee],
                "from_expression": parent["_resource_expression"],
                "to_expression": expression,
                "allocation_origin": origin,
                "evidence_ids": evidence_ids,
                "status": "unconfirmed",
                "confidence": 0.7,
                "_scope": caller,
                "_site_line": site["line"],
                "_pair_key": parent["_pair_key"],
                "_resource_id": expression,
                "_resource_expression": expression,
                "_resource_type": parent["_resource_type"],
                "_origin_operation": parent.get("_origin_operation", parent),
                "_call_edges": (*parent.get("_call_edges", ()), key),
            }
            pending.append(transfer)
        if not pending:
            break
        factories.extend(pending)
        for factory in pending:
            bindings.add(factory)
    inherited = []
    for factory in factories:
        allocation = factory["_origin_operation"]
        inherited.append({
            **allocation,
            "operation_id": "op_" + stable_digest([allocation["operation_id"], factory["resource_instance_id"]])[:16],
            "resource_instance_id": factory["resource_instance_id"],
            "allocation_origin": _location(allocation),
        })
    return factories, inherited


def _can_forward(
    operation: dict[str, Any], facts: dict[str, Any], bindings: _Bindings,
) -> tuple[int, str] | None:
    scope, expression, line = operation["_scope"], operation["_resource_expression"], operation["_site_line"]
    if not LVALUE.fullmatch(expression):
        return None
    base = expression.split("->")[0].split(".")[0]
    if bindings.definitions(scope, base, line) or bindings.definitions(scope, expression, line):
        return None
    parameters = facts["scopes"].get(scope, {}).get("parameters", ())
    if base not in parameters:
        return None
    return parameters.index(base), expression[len(base):]


def _forward_releases(
    edges: list[dict[str, Any]], names: dict[str, str], facts: dict[str, Any],
    releases: list[dict[str, Any]], bindings: _Bindings,
) -> tuple[list[dict[str, Any]], bool]:
    incoming = defaultdict(list)
    for edge in edges:
        incoming[edge["target"]].append(edge)
    frontier = releases
    bound = []
    processed = 0
    for _ in range(MAX_CALL_DEPTH):
        if not frontier:
            break
        next_frontier = []
        for operation in frontier:
            binding = _can_forward(operation, facts, bindings)
            if binding is None:
                continue
            index, suffix = binding
            for edge in incoming[operation["_scope"]]:
                identifier = edge["id"]
                call_chain = operation.get("_call_edges", ())
                if identifier in call_chain:
                    continue
                processed += 1
                if processed > MAX_FORWARDED_RELEASES:
                    return bound, True
                caller = edge["source"]
                site = _call_site(edge, names[caller], names[edge["target"]])
                arguments = _call_arguments(site["snippet"], site["api"])
                if arguments is None or index >= len(arguments) or not LVALUE.fullmatch(arguments[index]):
                    continue
                argument = arguments[index] + suffix
                forwarded = {
                    **operation,
                    "operation_id": "op_forward_" + stable_digest([operation["operation_id"], identifier])[:16],
                    "evidence_ids": list(dict.fromkeys([*operation["evidence_ids"], *site["evidence_ids"]])),
                    "via_calls": [site, *operation.get("via_calls", [])],
                    "_scope": caller,
                    "_site_line": site["line"],
                    "_resource_expression": argument,
                    "_call_edges": (*call_chain, identifier),
                }
                matched, chain = bindings.trace(caller, argument, site["line"] - 1)
                if matched and matched["_pair_key"] == operation["_pair_key"]:
                    forwarded["resource_instance_id"] = matched["resource_instance_id"]
                    forwarded["assignment_chain"] = chain
                    bound.append(forwarded)
                elif matched is None:
                    next_frontier.append(forwarded)
        frontier = next_frontier
    return bound, bool(frontier)


def _linear_release(
    acquisition: dict[str, Any], release: dict[str, Any], facts: dict[str, Any]
) -> bool:
    if release.get("via_calls") or acquisition["_scope"] != release["_scope"]:
        return False
    scope = facts["scopes"].get(release["_scope"])
    if scope is None or not acquisition["_site_line"] < release["_site_line"]:
        return False
    start = scope["start"]
    begin = acquisition["_site_line"] - start
    end = release["_site_line"] - start
    up_to_release = "\n".join(scope["lines"][:end + 1])
    signature, separator, body = up_to_release.partition("{")
    if (not separator or "(" not in signature or CONTROL_FLOW.search(body)
            or "{" in body or "}" in body or not 0 <= begin < end < len(scope["lines"])):
        return False
    # Only same-scope, straight-line assignment copies can intervene.
    for line in scope["lines"][begin + 1:end]:
        assignment = ASSIGNMENT_PATTERN.fullmatch(line.strip())
        if line.strip() and not (assignment and LVALUE.fullmatch(assignment["rhs"].strip())):
            # A declaration with initializer is also a copy, using the same
            # assignment evidence recorded by the source scanner.
            assignment = ASSIGNMENT_PATTERN.search(line.strip())
            if not assignment or not LVALUE.fullmatch(assignment["rhs"].strip()) or assignment.end() != len(line.strip()):
                return False
    return bool(re.fullmatch(rf"\s*{re.escape(release['api'])}\s*\(\s*{LVALUE.pattern}\s*\)\s*;", release["snippet"]))


def _public_operation(operation: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in operation.items() if not key.startswith("_")}


def _evidence_map(graph: dict[str, Any], evidence_ids: set[str]) -> list[dict[str, Any]]:
    result = []
    for item in graph.get("evidence", []):
        if item.get("id") in evidence_ids:
            result.append(item)
    return sorted(result, key=lambda item: str(item.get("id", "")))


def _path(
    item_id: str, condition: str, operations: list[dict[str, Any]],
    termination: str, *, complete: bool = False, exit_site: dict[str, Any] | None = None,
) -> dict[str, Any]:
    operation_ids = [op["operation_id"] for op in operations]
    evidence_ids = [key for op in operations for key in op["evidence_ids"]]
    result = {
        "path_id": item_id + "_" + stable_digest([condition, operation_ids])[:12],
        "condition": condition,
        "operations": operation_ids,
        "resource_instance_id": operations[0]["resource_instance_id"],
        "complete": complete,
        "termination_reason": termination,
    }
    if exit_site is not None:
        result["exit_location"] = {key: exit_site[key] for key in ("file", "line", "snippet")}
        evidence_ids.append(exit_site["evidence_id"])
    result["evidence_ids"] = list(dict.fromkeys(evidence_ids))
    return result


def _resource_paths(
    item_id: str, anchor: dict[str, Any], group: list[dict[str, Any]], facts: dict[str, Any]
) -> list[dict[str, Any]]:
    prefix = [op for op in group if op["kind"] != "release"]
    releases = [op for op in group if op["kind"] == "release"]
    if not prefix:
        return [_path(item_id, "release_observed", [op], "unresolved_acquire") for op in releases]

    proved = len(prefix) == len(releases) == 1 and _linear_release(prefix[0], releases[0], facts)
    paths = []
    for op in releases:
        site = (op.get("via_calls") or [op])[0]
        condition = "release_site:" + str(site["file"]) + ":" + str(site["line"])
        paths.append(_path(item_id, condition, [*prefix, op],
                           "release_found" if proved else "release_path_unproven", complete=proved))
    if proved:
        return paths

    exits = [
        fact for fact in facts["returns"]
        if fact["scope"] == anchor["_scope"] and fact["line"] >= anchor["_site_line"]
    ]
    for fact in exits:
        condition = "return_site:" + fact["file"] + ":" + str(fact["line"])
        reason = ("resource_escaped" if fact["evidence_id"] in anchor.get("_escape_evidence", ())
                  else "ownership_path_unresolved")
        paths.append(_path(item_id, condition, prefix, reason, exit_site=fact))
    if not exits and not releases:
        member = "->" in anchor["_resource_expression"] or "." in anchor["_resource_expression"]
        paths.append(_path(item_id, "visible_operations", prefix,
                           "ownership_path_unresolved" if member else "release_not_found"))
    return paths


def analyze_resource_flow(
    graph: dict[str, Any],
    *,
    source_root: str | Path | None = None,
) -> dict[str, Any]:
    """Export version-bound resource observations and conservative path coverage."""

    root = Path(source_root).resolve() if source_root is not None else None
    facts = (_scan_source_facts(graph, root) if root is not None
             else {"assignments": [], "returns": [], "evidence": {}, "scopes": {}})
    bindings = _Bindings(facts)
    names = {str(node["id"]): str(node.get("name") or _tail(str(node["id"]))) for node in graph.get("nodes", [])}
    edges = [
        edge for edge in graph.get("edges", [])
        if edge.get("kind") == "direct_call" and edge.get("id")
        and edge.get("source") in names and edge.get("target") in names and edge.get("evidence_ids")
    ]
    acquisitions = []
    releases = []
    for edge in edges:
        kind = _classify(names[edge["target"]])
        if not kind:
            continue
        op = _operation(edge, names, kind)
        if kind == "acquire":
            op["resource_instance_id"] = _instance(op["_scope"], op["_edge_id"])
            op["allocation_origin"] = _location(op)
            acquisitions.append(op)
            bindings.add(op)
        else:
            releases.append(op)

    factories, inherited = _track_factory_calls(edges, names, facts, acquisitions, bindings)
    forwarded, forwarding_limited = _forward_releases(edges, names, facts, releases, bindings)
    for release in releases:
        scope, expression = release["_scope"], release["_resource_expression"]
        acquisition, chain = bindings.trace(scope, expression, release["_site_line"] - 1)
        if acquisition and acquisition["_pair_key"] == release["_pair_key"]:
            release["resource_instance_id"] = acquisition["resource_instance_id"]
            release["assignment_chain"] = chain
        else:
            release["resource_instance_id"] = _instance(scope, "unresolved:" + expression)

    anchors = {op["resource_instance_id"]: op for op in acquisitions + factories}
    grouped = defaultdict(list)
    for operation in [*acquisitions, *inherited, *factories, *releases, *forwarded]:
        grouped[operation["resource_instance_id"]].append(operation)

    items = []
    warnings = ["release_forwarding_limit_reached"] if forwarding_limited else []
    referenced_evidence = set()
    for instance, group in sorted(grouped.items()):
        anchor = anchors.get(instance, group[0])
        item_id = "rf_" + stable_digest(instance)[:12]
        paths = _resource_paths(item_id, anchor, group, facts)
        complete = bool(paths) and all(path["complete"] for path in paths)
        evidence_ids = {key for path in paths for key in path["evidence_ids"]}
        if not complete:
            reasons = ", ".join(sorted({path["termination_reason"] for path in paths if not path["complete"]}))
            warnings.append(f"{item_id}: {anchor['function']}:{anchor['_resource_expression']}: {reasons}")

        data_flow = {
            fact["evidence_id"]: {key: fact[key] for key in ("file", "line", "lhs", "rhs", "evidence_id")}
            for operation in group for fact in operation.get("assignment_chain", [])
        }
        referenced_evidence.update(evidence_ids | data_flow.keys())
        items.append({
            "id": item_id,
            "status": "unconfirmed",
            "resource": anchor["_resource_id"],
            "resource_expression": anchor["_resource_expression"],
            "resource_instance_id": instance,
            "resource_type": anchor["_resource_type"],
            "operations": [_public_operation(op) for op in group],
            "owner": anchor["function"],
            "thread": None,
            "ownership_model": (
                "cross_function_transfer" if anchor["kind"] == "transfer"
                else "visible_acquire_release" if complete
                else "unresolved_from_visible_graph"
            ),
            "data_flow": list(data_flow.values()),
            "assignment_chain": list(data_flow.values()),
            "paths": paths,
            "confidence": 0.8 if complete else 0.35,
            "evidence_ids": sorted(evidence_ids),
            "origin": "analyzer",
        })

    combined_evidence = {item["id"]: item for item in _evidence_map(graph, referenced_evidence)}
    combined_evidence.update({key: value for key, value in facts["evidence"].items() if key in referenced_evidence})
    if len(warnings) > 50:
        warnings = warnings[:50] + [f"{len(warnings) - 50} additional unresolved resource paths omitted"]
    status = "succeeded" if items and not warnings else ("partial" if items else "not_available")
    generated_at = str((graph.get("meta") or {}).get("generated_at") or utc_now())
    return {
        "schema_version": "1.0",
        "run_id": str(graph.get("run_id", "")),
        "result_type": "resource_flow",
        "status": status,
        "generated_at": generated_at,
        "source": {
            **dict(graph.get("meta") or {}),
            "origin": "analyzer",
            "provider": "native_resource_flow_v1",
        },
        "items": items,
        "evidence": sorted(combined_evidence.values(), key=lambda item: item["id"]),
        "warnings": warnings,
    }
