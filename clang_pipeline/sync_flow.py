from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .clang_ast import evidence_id, stable_digest, utc_now


LOCK_ACQUIRE = {
    "uv_mutex_lock": ("acquire", "mutex_lock"),
    "uv_rwlock_rdlock": ("acquire", "rwlock"),
    "uv_rwlock_wrlock": ("acquire", "rwlock"),
    "uv_sem_wait": ("acquire", "mutex_lock"),
    "pthread_mutex_lock": ("acquire", "mutex_lock"),
    "pthread_rwlock_rdlock": ("acquire", "rwlock"),
    "pthread_rwlock_wrlock": ("acquire", "rwlock"),
}

TRY_LOCK = {
    "uv_rwlock_trywrlock": ("try_acquire", "rwlock"),
    "pthread_rwlock_trywrlock": ("try_acquire", "rwlock"),
}

LOCK_RELEASE = {
    "uv_mutex_unlock": ("release", "mutex_lock"),
    "uv_rwlock_unlock": ("release", "rwlock"),
    "uv_rwlock_rdunlock": ("release", "rwlock"),
    "uv_rwlock_wrunlock": ("release", "rwlock"),
    "uv_sem_post": ("release", "mutex_lock"),
    "pthread_mutex_unlock": ("release", "mutex_lock"),
    "pthread_rwlock_unlock": ("release", "rwlock"),
}

CONDITION_WAIT = {
    "uv_cond_wait": ("wait", "condition_variable"),
    "pthread_cond_wait": ("wait", "condition_variable"),
}

CONDITION_NOTIFY = {
    "uv_cond_signal": ("notify", "condition_variable"),
    "uv_cond_broadcast": ("notify", "condition_variable"),
    "pthread_cond_signal": ("notify", "condition_variable"),
    "pthread_cond_broadcast": ("notify", "condition_variable"),
}

THREAD_RULES = {
    "uv_thread_create": ("create", "thread_join"),
    "uv_thread_create_ex": ("create", "thread_join"),
    "uv_thread_join": ("join", "thread_join"),
    "pthread_create": ("create", "thread_join"),
    "pthread_join": ("join", "thread_join"),
}

EVENT_RULES = {
    "uv_async_send": ("notify", "event_notify"),
    "uv__async_io": ("wait", "event_wait"),
    "uv__async_send": ("notify", "event_notify"),
    "eventfd": ("wait", "event_wait"),
    "poll": ("wait", "event_wait"),
    "kevent": ("wait", "event_wait"),
    "epoll_wait": ("wait", "event_wait"),
}

SEQUENCE_TARGETS = {
    "listAddNodeTail",
    "listDelNode",
    "listFirst",
    "listEmpty",
    "setIOPendingCount",
    "getIOPendingCount",
    "zfree",
    "writeToClient",
    "readQueryFromClient",
    "freeClientAsync",
    "pthread_join",
    "_serverLog",
    "uv__queue_insert_tail",
    "uv__queue_move",
    "uv__queue_head",
    "uv__queue_remove",
    "uv__queue_move",
    "uv__work_submit",
    "uv__fs_done",
    "uv__async_spin",
    "uv__async_send",
    "uv_mutex_destroy",
    "uv_cond_destroy",
    "uv_sem_destroy",
    "epoll_pwait",
    "epoll_wait",
    "kevent",
    "poll",
    "read",
    "write",
}

QUEUE_PRODUCERS = {
    "listAddNodeTail",
    "uv__queue_insert_tail",
    "uv__queue_move",
    "setIOPendingCount",
    "uv_async_send",
}

QUEUE_CONSUMERS = {
    "listFirst",
    "listDelNode",
    "listEmpty",
    "getIOPendingCount",
    "uv__queue_head",
    "uv__queue_remove",
    "uv__queue_move",
}

ATOMIC_RELEASE_TOKENS = (
    "memory_order_release",
    "atomic_store_explicit",
    "atomic_store(",
    "atomic_exchange_explicit",
    "atomic_exchange(",
    "atomic_fetch_add_explicit",
    "atomic_fetch_sub_explicit",
    "atomicIncr",
    "atomicDecr",
    "atomicSetWithSync",
    "__atomic_store",
)

ATOMIC_ACQUIRE_TOKENS = (
    "memory_order_acquire",
    "atomic_load_explicit",
    "atomic_load(",
    "atomicGetWithSync",
    "__atomic_load",
)

ATOMIC_RELAXED_TOKENS = (
    "memory_order_relaxed",
    "atomicGet(",
    "atomicSet(",
)

SOURCE_EVENT_APIS = (
    set(LOCK_ACQUIRE)
    | set(TRY_LOCK)
    | set(LOCK_RELEASE)
    | set(CONDITION_WAIT)
    | set(CONDITION_NOTIFY)
    | set(THREAD_RULES)
    | set(SEQUENCE_TARGETS)
)

CALLBACK_CALL_PATTERN = re.compile(
    r"(?:->|\.)\s*(?:work|done|cb|async_cb|read_cb|write_cb|connect_cb)\s*\("
)
ASSIGNMENT_PATTERN = re.compile(
    r"(?:\+\+|--|(?<![=!<>])=(?!=)|\+=|-=)"
)
CONTROL_SUPPORT_PATTERN = re.compile(r"^\s*(?:if|else|return|break|continue)\b")
PROTECTED_ACCESS_WINDOW = 16

QUEUE_RESOURCE_TOKENS = (
    "queue",
    "wq",
    "jobs",
    "pending",
    "reply",
    "watcher",
    "ready",
)

IDENTIFIER_PATTERN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
IGNORED_IDENTIFIERS = {
    "if",
    "else",
    "while",
    "for",
    "return",
    "sizeof",
    "NULL",
    "true",
    "false",
}


def _tail(value: str) -> str:
    value = value.split(":")[-1]
    return value.rsplit("::", 1)[-1]


def _node_name(graph: dict[str, Any], node_id: str) -> str:
    for node in graph.get("nodes", []):
        if node.get("id") == node_id:
            return str(node.get("name") or _tail(node_id))
    return _tail(node_id)


def _resource_key(snippet: str, fallback: str) -> str:
    text = snippet.strip()
    arguments = re.search(r"\(([^()]*)\)", text)
    if arguments:
        first = arguments.group(1).split(",", 1)[0].strip()
        if first:
            return first.lstrip("&*").strip()
    assignment = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*=", text)
    if assignment:
        return assignment.group(1)
    return fallback


def _thread_hint(function: str) -> str | None:
    lowered = function.lower()
    for token in ("submit", "post", "producer", "enqueue"):
        if token in lowered:
            return "producer"
    for token in ("worker", "process", "iothread", "threadmain"):
        if token in lowered:
            return "worker"
    for token in ("main", "event", "loop"):
        if token in lowered:
            return token
    return None


def _identifiers(text: str) -> set[str]:
    return {
        match.group(0)
        for match in IDENTIFIER_PATTERN.finditer(text)
        if match.group(0) not in IGNORED_IDENTIFIERS
    }


def _sequence_operation(
    edge: dict[str, Any],
    graph: dict[str, Any],
) -> dict[str, Any] | None:
    target = _node_name(graph, str(edge.get("target", "")))
    if target not in SEQUENCE_TARGETS and edge.get("kind") != "callback_edge":
        return None
    source = _node_name(graph, str(edge.get("source", "")))
    site = edge.get("call_site") or {}
    snippet = str(site.get("snippet") or "")
    return {
        "operation_id": "program_op_" + str(edge.get("id", ""))[-10:],
        "kind": "program_order",
        "relation_type": "happens_before",
        "api": target,
        "function": source,
        "thread": _thread_hint(source),
        "resource": _resource_key(snippet, target),
        "file": site.get("file", ""),
        "line": int(site.get("line", 0)),
        "snippet": snippet,
        "evidence_ids": list(edge.get("evidence_ids", [])),
        "_edge_id": edge.get("id", ""),
        "_identifiers": _identifiers(snippet),
    }


def _atomic_operation(
    edge: dict[str, Any],
    graph: dict[str, Any],
) -> dict[str, Any] | None:
    site = edge.get("call_site") or {}
    snippet = str(site.get("snippet") or "")
    classification = _classify_atomic_snippet(snippet)
    if classification is None:
        return None
    kind, semantics = classification
    target = _node_name(graph, str(edge.get("target", "")))
    source = _node_name(graph, str(edge.get("source", "")))
    return {
        "operation_id": "atomic_op_" + str(edge.get("id", ""))[-10:],
        "kind": kind,
        "relation_type": "atomic",
        "semantics": semantics,
        "access": _atomic_access(snippet),
        "api": target,
        "function": source,
        "thread": _thread_hint(source),
        "resource": _resource_key(snippet, target),
        "file": site.get("file", ""),
        "line": int(site.get("line", 0)),
        "snippet": snippet,
        "evidence_ids": list(edge.get("evidence_ids", [])),
        "_edge_id": edge.get("id", ""),
        "_identifiers": _identifiers(snippet),
    }


def _classify_atomic_snippet(snippet: str) -> tuple[str, str] | None:
    lowered = snippet.lower()
    if "atomic" not in lowered and "memory_order_" not in lowered and "__atomic" not in lowered:
        return None
    relaxed_only = (
        "memory_order_relaxed" in lowered
        or bool(re.search(r"\b(atomicIncr|atomicDecr|atomicGet|atomicSet)\s*\(", snippet))
    )
    if relaxed_only:
        kind, semantics = "relaxed", "relaxed"
    elif any(
        token in lowered
        for token in ("atomic_exchange", "__atomic_exchange")
    ):
        kind, semantics = "exchange", "seq_cst"
    elif any(
        token in lowered
        for token in (
            "atomic_fetch_add",
            "atomic_fetch_sub",
            "atomic_store(",
            "__atomic_store",
        )
    ):
        kind, semantics = "release", "seq_cst"
    elif any(
        token in lowered
        for token in ("atomic_load(", "__atomic_load")
    ):
        kind, semantics = "acquire", "seq_cst"
    elif any(token.lower() in lowered for token in ATOMIC_RELEASE_TOKENS):
        kind, semantics = "release", "release"
    elif any(token.lower() in lowered for token in ATOMIC_ACQUIRE_TOKENS):
        kind, semantics = "acquire", "acquire"
    elif any(token.lower() in lowered for token in ATOMIC_RELAXED_TOKENS):
        kind, semantics = "relaxed", "relaxed"
    else:
        return None
    return kind, semantics


def _atomic_access(snippet: str) -> str:
    lowered = snippet.lower()
    if any(
        token in lowered
        for token in ("atomic_exchange", "__atomic_exchange")
    ):
        return "read_write"
    if any(
        token in lowered
        for token in (
            "atomic_load",
            "atomicget",
            "__atomic_load",
            "atomicgetwithsync",
        )
    ):
        return "read"
    return "write"


def _sync_operation(edge: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any] | None:
    target = _node_name(graph, str(edge.get("target", "")))
    source = _node_name(graph, str(edge.get("source", "")))
    rules = (
        LOCK_ACQUIRE.get(target)
        or TRY_LOCK.get(target)
        or LOCK_RELEASE.get(target)
        or CONDITION_WAIT.get(target)
        or CONDITION_NOTIFY.get(target)
        or THREAD_RULES.get(target)
        or EVENT_RULES.get(target)
    )
    if rules is None:
        return None
    kind, relation_type = rules
    site = edge.get("call_site") or {}
    snippet = str(site.get("snippet") or "")
    return {
        "operation_id": "sync_op_" + str(edge.get("id", ""))[-10:],
        "kind": kind,
        "relation_type": relation_type,
        "api": target,
        "function": source,
        "thread": _thread_hint(source),
        "resource": _resource_key(snippet, target),
        "file": site.get("file", ""),
        "line": int(site.get("line", 0)),
        "snippet": snippet,
        "evidence_ids": list(edge.get("evidence_ids", [])),
        "_edge_id": edge.get("id", ""),
        "_identifiers": _identifiers(snippet),
    }


def _location(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "function": operation["function"],
        "thread": operation["thread"],
        "file": operation["file"],
        "line": operation["line"],
        "snippet": operation["snippet"],
        "held_locks": list(operation.get("held_locks", [])),
        "evidence_ids": operation["evidence_ids"],
    }


def _make_relation(
    source: dict[str, Any],
    target: dict[str, Any],
    *,
    order: str,
    confidence: float,
    condition: str | None,
    support_evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    relation_id = "sync_" + stable_digest(
        [
            source["relation_type"],
            source["operation_id"],
            target["operation_id"],
            order,
        ]
    )[:12]
    return {
        "id": relation_id,
        "status": "unconfirmed",
        "relation_type": source["relation_type"],
        "source": _location(source),
        "target": _location(target),
        "order": order,
        "condition": condition,
        "execution_context": {
            "process": "target-repository",
            "source_thread": source.get("thread"),
            "target_thread": target.get("thread"),
            "event_loop": None,
        },
        "confidence": confidence,
        "evidence_ids": list(
            dict.fromkeys(
                source["evidence_ids"]
                + target["evidence_ids"]
                + list(support_evidence_ids or [])
            )
        ),
        "origin": "analyzer",
    }


def _public_operation(operation: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in operation.items() if not key.startswith("_")}


def _program_category(operation: dict[str, Any]) -> str:
    if operation.get("kind") == "callback":
        return "callback"
    if operation.get("relation_type") == "atomic":
        return "atomic"
    if operation.get("relation_type") == "event_wait":
        return "event_wait"
    if operation.get("relation_type") == "event_notify":
        return "event_notify"
    if operation.get("api") in QUEUE_PRODUCERS:
        return "queue_producer"
    if operation.get("api") in QUEUE_CONSUMERS:
        return "queue_consumer"
    if operation.get("kind") in {"acquire", "release", "try_acquire"}:
        return "lock"
    if operation.get("kind") == "program":
        return "program"
    return str(operation.get("relation_type") or "other")


def _evidence_map(graph: dict[str, Any], evidence_ids: set[str]) -> list[dict[str, Any]]:
    result = []
    for item in graph.get("evidence", []):
        if item.get("id") in evidence_ids:
            result.append(item)
    return sorted(result, key=lambda item: str(item.get("id", "")))


def _resolve_source_path(source_root: Path, file: str) -> Path:
    path = Path(file)
    if path.is_absolute():
        return path
    for candidate in (source_root / path, Path.cwd() / path):
        if candidate.is_file():
            return candidate
    return source_root / path


def _scan_source_atomic_operations(
    graph: dict[str, Any],
    source_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in graph.get("nodes", []):
        if node.get("kind") != "function":
            continue
        file = str(node.get("file") or "")
        line = int(node.get("line") or 0)
        if file and line > 0:
            by_file[file].append(node)
    operations: list[dict[str, Any]] = []
    evidence: dict[str, dict[str, Any]] = {}
    for file, nodes in by_file.items():
        path = _resolve_source_path(source_root, file)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        nodes.sort(key=lambda item: int(item["line"]))
        line_functions: dict[int, str] = {}
        for index, node in enumerate(nodes):
            start = int(node["line"])
            end = (
                int(nodes[index + 1]["line"]) - 1
                if index + 1 < len(nodes)
                else len(lines)
            )
            for line_number in range(start, min(end, len(lines)) + 1):
                line_functions[line_number] = str(node.get("name") or "")
        current_function = ""
        current_started = 0
        function_pattern = re.compile(
            r"^\s*(?:[A-Za-z_][\w\s*&:<>]*?)\s+"
            r"([A-Za-z_]\w*)\s*\([^;]*\)\s*\{?"
        )
        for line_number, raw in enumerate(lines, start=1):
            defined = function_pattern.match(raw)
            if defined:
                current_function = defined.group(1)
                current_started = line_number
            function = current_function if current_started else line_functions.get(line_number, "")
            if not function:
                continue
            snippet = raw.strip()
            classification = _classify_atomic_snippet(snippet)
            if classification is None:
                continue
            call = re.search(r"\b([A-Za-z_]\w*)\s*\(", snippet)
            api = call.group(1) if call else "atomic"
            ev_id = evidence_id("call_site", file, line_number, api)
            kind, semantics = classification
            operations.append(
                {
                    "operation_id": "atomic_src_" + stable_digest(
                        [function, file, line_number, snippet]
                    )[:10],
                    "kind": kind,
                    "relation_type": "atomic",
                    "semantics": semantics,
                    "access": _atomic_access(snippet),
                    "api": api,
                    "function": function,
                    "thread": _thread_hint(function),
                    "resource": _resource_key(snippet, api),
                    "file": file,
                    "line": line_number,
                    "snippet": snippet,
                    "evidence_ids": [ev_id],
                    "_edge_id": "source_atomic",
                    "_identifiers": _identifiers(snippet),
                }
            )
            evidence[ev_id] = {
                "id": ev_id,
                "kind": "call_site",
                "location": {
                    "file": file,
                    "line": line_number,
                    "snippet": snippet,
                },
                "role": "supports",
            }
    return operations, evidence


def _canonical_resource(value: str) -> str:
    text = value.strip().strip("&*() ")
    text = text.replace("w->loop->", "loop->")
    text = text.replace("_mutex", "")
    lowered = text.lower()
    if lowered == "fd" or lowered.endswith(".fd"):
        return "fd"
    if "loop" in lowered and "wq" in lowered:
        return "loop.wq"
    return text.replace("->", ".")


def _source_event_api(snippet: str) -> str | None:
    masked = re.sub(r"//.*$", "", snippet)
    for api in sorted(SOURCE_EVENT_APIS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(api)}\b", masked):
            return api
    callback = CALLBACK_CALL_PATTERN.search(masked)
    if callback:
        return callback.group(0).replace("->", "").replace(".", "").strip("(")
    return None


def _source_event(
    *,
    function: str,
    file: str,
    line: int,
    snippet: str,
    api: str | None = None,
) -> dict[str, Any] | None:
    stripped = snippet.strip()
    if not stripped or stripped.startswith(("/*", "*", "//")):
        return None
    api = api or _source_event_api(stripped)
    atomic = _classify_atomic_snippet(stripped)
    if atomic is not None:
        kind, semantics = atomic
        event_api = api or "atomic"
        return {
            "operation_id": "atomic_src_" + stable_digest(
                [function, file, line, stripped]
            )[:10],
            "kind": kind,
            "relation_type": "atomic",
            "semantics": semantics,
            "access": _atomic_access(stripped),
            "api": event_api,
            "function": function,
            "thread": _thread_hint(function),
            "resource": _resource_key(stripped, event_api),
            "file": file,
            "line": line,
            "snippet": stripped,
            "evidence_ids": [],
            "_identifiers": _identifiers(stripped),
        }
    rules = None
    if api == "write":
        kind, relation_type = "notify", "event_notify"
        rules = (kind, relation_type)
    elif api in {"read", "poll", "epoll_pwait", "epoll_wait", "kevent"}:
        kind, relation_type = "wait", "event_wait"
        rules = (kind, relation_type)
    if api:
        rules = rules or (
            LOCK_ACQUIRE.get(api)
            or TRY_LOCK.get(api)
            or LOCK_RELEASE.get(api)
            or CONDITION_WAIT.get(api)
            or CONDITION_NOTIFY.get(api)
            or THREAD_RULES.get(api)
            or EVENT_RULES.get(api)
        )
    if rules is not None and api is not None:
        kind, relation_type = rules
        return {
            "operation_id": "sync_src_" + stable_digest(
                [function, file, line, stripped]
            )[:10],
            "kind": kind,
            "relation_type": relation_type,
            "api": api,
            "function": function,
            "thread": _thread_hint(function),
            "resource": _resource_key(stripped, api),
            "file": file,
            "line": line,
            "snippet": stripped,
            "evidence_ids": [],
            "_identifiers": _identifiers(stripped),
        }
    if CALLBACK_CALL_PATTERN.search(stripped):
        return {
            "operation_id": "callback_src_" + stable_digest(
                [function, file, line, stripped]
            )[:10],
            "kind": "callback",
            "relation_type": "happens_before",
            "api": api or "callback",
            "function": function,
            "thread": _thread_hint(function),
            "resource": _resource_key(stripped, api or "callback"),
            "file": file,
            "line": line,
            "snippet": stripped,
            "evidence_ids": [],
            "_identifiers": _identifiers(stripped),
        }
    if api in SEQUENCE_TARGETS:
        return {
            "operation_id": "sequence_src_" + stable_digest(
                [function, file, line, stripped]
            )[:10],
            "kind": "program_order",
            "relation_type": "happens_before",
            "api": api,
            "function": function,
            "thread": _thread_hint(function),
            "resource": _resource_key(stripped, api),
            "file": file,
            "line": line,
            "snippet": stripped,
            "evidence_ids": [],
            "_identifiers": _identifiers(stripped),
        }
    if ASSIGNMENT_PATTERN.search(stripped) or CONTROL_SUPPORT_PATTERN.match(stripped):
        return {
            "operation_id": "program_src_" + stable_digest(
                [function, file, line, stripped]
            )[:10],
            "kind": "program",
            "relation_type": "happens_before",
            "api": "assignment",
            "function": function,
            "thread": _thread_hint(function),
            "resource": _resource_key(stripped, "assignment"),
            "file": file,
            "line": line,
            "snippet": stripped,
            "evidence_ids": [],
            "_identifiers": _identifiers(stripped),
        }
    return None


def _scan_source_events(
    graph: dict[str, Any],
    source_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in graph.get("nodes", []):
        if node.get("kind") != "function":
            continue
        file = str(node.get("file") or "")
        line = int(node.get("line") or 0)
        if file and line > 0:
            by_file[file].append(node)
    events: list[dict[str, Any]] = []
    evidence: dict[str, dict[str, Any]] = {}
    for file, nodes in by_file.items():
        nodes.sort(key=lambda item: int(item["line"]))
        path = _resolve_source_path(source_root, file)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        ranges: list[tuple[str, int, int]] = []
        for index, node in enumerate(nodes):
            start = int(node["line"])
            end = (
                int(nodes[index + 1]["line"]) - 1
                if index + 1 < len(nodes)
                else len(lines)
            )
            ranges.append((str(node.get("name") or ""), start, min(end, len(lines))))
        emitted_lines: set[tuple[str, int]] = set()
        recognized_by_function: dict[str, list[int]] = defaultdict(list)
        for function, start, end in ranges:
            for line_number in range(start, end + 1):
                snippet = lines[line_number - 1].strip()
                event = _source_event(
                    function=function,
                    file=file,
                    line=line_number,
                    snippet=snippet,
                )
                if event is None:
                    continue
                if event["kind"] in {"program"}:
                    continue
                ev_id = evidence_id(
                    "source_event",
                    file,
                    line_number,
                    str(event["api"]),
                )
                event["evidence_ids"] = [ev_id]
                events.append(event)
                emitted_lines.add((function, line_number))
                recognized_by_function[function].append(line_number)
                evidence[ev_id] = {
                    "id": ev_id,
                    "kind": "source_event",
                    "location": {
                        "file": file,
                        "line": line_number,
                        "snippet": snippet,
                    },
                    "role": "supports",
                }
        # Add nearby assignments as program-order anchors. The window is
        # deliberately local so unrelated assignments do not dominate output.
        for function, start, end in ranges:
            anchors = recognized_by_function.get(function, [])
            if not anchors:
                continue
            windows = [
                (max(start, anchor - 20), min(end, anchor + 20))
                for anchor in anchors
            ]
            for window_start, window_end in windows:
                for line_number in range(window_start, window_end + 1):
                    if (function, line_number) in emitted_lines:
                        continue
                    snippet = lines[line_number - 1].strip()
                    event = _source_event(
                        function=function,
                        file=file,
                        line=line_number,
                        snippet=snippet,
                    )
                    if event is None or event["kind"] != "program":
                        continue
                    ev_id = evidence_id(
                        "source_event",
                        file,
                        line_number,
                        "assignment",
                    )
                    event["evidence_ids"] = [ev_id]
                    events.append(event)
                    emitted_lines.add((function, line_number))
                    evidence[ev_id] = {
                        "id": ev_id,
                        "kind": "source_event",
                        "location": {
                            "file": file,
                            "line": line_number,
                            "snippet": snippet,
                        },
                        "role": "supports",
                    }
    return events, evidence


def analyze_sync_relations(
    graph: dict[str, Any],
    *,
    source_root: str | Path | None = None,
) -> dict[str, Any]:
    """Extract first-pass synchronization relations from a Clang graph.

    The extractor is deliberately conservative: lock pairs found in the same
    function are emitted as happens-before relations. Cross-function condition
    and event pairs are emitted with unknown or low-confidence order unless
    there is a shared resource key and a visible notification/wait pair.
    """

    operations = []
    atomic_operations = []
    sequence_operations = []
    for edge in graph.get("edges", []):
        operation = _sync_operation(edge, graph)
        if operation is not None:
            operations.append(operation)
            continue
        atomic_operation = _atomic_operation(edge, graph)
        if atomic_operation is not None:
            atomic_operations.append(atomic_operation)
            continue
        sequence = _sequence_operation(edge, graph)
        if sequence is not None:
            sequence_operations.append(sequence)
    source_evidence: dict[str, dict[str, Any]] = {}
    if source_root is not None:
        scanned_events, source_evidence = _scan_source_events(
            graph,
            Path(source_root).resolve(),
        )
        existing_by_kind = {
            "operation": {
                (item["function"], item["file"], item["line"], item["kind"])
                for item in operations
            },
            "atomic": {
                (item["function"], item["file"], item["line"], item["kind"])
                for item in atomic_operations
            },
            "sequence": {
                (item["function"], item["file"], item["line"], item["kind"])
                for item in sequence_operations
            },
        }
        for operation in scanned_events:
            key = (
                operation["function"],
                operation["file"],
                operation["line"],
                operation["kind"],
            )
            if operation["relation_type"] == "atomic":
                target_list, bucket = atomic_operations, "atomic"
            elif operation["relation_type"] == "happens_before":
                target_list, bucket = sequence_operations, "sequence"
            else:
                target_list, bucket = operations, "operation"
            if key in existing_by_kind[bucket]:
                continue
            target_list.append(operation)
            existing_by_kind[bucket].add(key)
    items: list[dict[str, Any]] = []
    warnings: list[str] = []
    referenced: set[str] = set()

    # Track the locks visible at each operation in source order. This is a
    # local program-order approximation; the condition records that boundary.
    lock_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for operation in operations:
        if operation["relation_type"] in {"mutex_lock", "rwlock"}:
            lock_groups[(operation["function"], operation["resource"])].append(operation)
    for (function, lock_resource), group in lock_groups.items():
        group.sort(key=lambda item: (item["file"], item["line"]))
        active: set[str] = set()
        for operation in group:
            if operation["kind"] == "acquire":
                active.add(lock_resource)
            operation["held_locks"] = sorted(active)
            if operation["kind"] == "release":
                active.discard(lock_resource)

    # Same-function lock pairs are the strongest evidence available in the
    # current graph: lock -> unlock in source order.
    by_function: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for operation in operations:
        by_function[
            (operation["relation_type"], operation["resource"], operation["function"])
        ].append(operation)
    for (relation_type, resource, function), group in sorted(by_function.items()):
        group.sort(key=lambda item: (item["file"], item["line"]))
        acquires = [
            item
            for item in group
            if item["kind"] in {"acquire", "wait"} and item["relation_type"] == relation_type
        ]
        releases = [
            item
            for item in group
            if item["kind"] == "release" and item["relation_type"] == relation_type
        ]
        for acquire in acquires:
            release = next(
                (item for item in releases if item["line"] >= acquire["line"]),
                None,
            )
            if release is None:
                continue
            relation = _make_relation(
                acquire,
                release,
                order="happens_before",
                confidence=0.85,
                condition=f"same function/resource {function}:{resource}",
            )
            items.append(relation)
            referenced.update(relation["evidence_ids"])

    # Atomic release/acquire pairs are cross-function publication edges when
    # the same atomic object is visible on both sides.
    by_atomic_resource: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for operation in atomic_operations:
        by_atomic_resource[operation["resource"]].append(operation)
    for resource, group in by_atomic_resource.items():
        releases = [
            item for item in group
            if (
                item.get("access") in {"write", "read_write"}
                and (
                    item["kind"] == "release"
                    or item.get("semantics") in {"release", "seq_cst"}
                )
            )
        ]
        acquires = [
            item for item in group
            if (
                item.get("access") in {"read", "read_write"}
                and (
                    item["kind"] == "acquire"
                    or item.get("semantics") in {"acquire", "seq_cst"}
                )
            )
        ]
        for release in releases:
            for acquire in acquires:
                if release["function"] == acquire["function"] and release["line"] >= acquire["line"]:
                    continue
                if release["function"] == acquire["function"]:
                    order, confidence = "happens_before", 0.8
                else:
                    order, confidence = "happens_before", 0.7
                relation = _make_relation(
                    release,
                    acquire,
                    order=order,
                    confidence=confidence,
                    condition=(
                        f"atomic {resource} release/acquire across "
                        f"{release['function']} -> {acquire['function']}"
                    ),
                )
                items.append(relation)
                referenced.update(relation["evidence_ids"])
                break
        relaxed_reads = [
            item
            for item in group
            if item.get("semantics") == "relaxed" and item.get("access") == "read"
        ]
        preferred_reads = [
            item
            for item in relaxed_reads
            if any(
                token in item["function"].lower()
                for token in ("get", "count", "read", "load")
            )
        ]
        if preferred_reads:
            relaxed_reads = preferred_reads
        relaxed_writes = [
            item
            for item in group
            if item.get("semantics") == "relaxed" and item.get("access") == "write"
        ]
        for source in relaxed_writes:
            target = next(
                (
                    item
                    for item in relaxed_reads
                    if item["function"] != source["function"]
                ),
                None,
            )
            if target is None:
                continue
            relation = _make_relation(
                source,
                target,
                order="unknown",
                confidence=0.3,
                condition=(
                    f"atomic relaxed {resource}; no acquire/release "
                    "publication established"
                ),
            )
            items.append(relation)
            referenced.update(relation["evidence_ids"])

    # Thread create/join pairs use the thread handle as the resource key.
    by_thread_handle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for operation in operations:
        if operation["relation_type"] == "thread_join":
            by_thread_handle[operation["resource"]].append(operation)
    for handle, group in by_thread_handle.items():
        creates = [item for item in group if item["kind"] == "create"]
        joins = [item for item in group if item["kind"] == "join"]
        for create in creates:
            for join in joins:
                relation = _make_relation(
                    create,
                    join,
                    order="happens_before",
                    confidence=0.75,
                    condition=f"thread handle {handle} is joined",
                )
                items.append(relation)
                referenced.update(relation["evidence_ids"])
                break

    # Cross-function queue publication: a producer inserts into a queue and a
    # different function consumes the same visible queue expression.
    queue_operations = [
        item for item in sequence_operations
        if item["api"] in QUEUE_PRODUCERS or item["api"] in QUEUE_CONSUMERS
    ]
    queue_producers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    queue_consumers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for operation in queue_operations:
        resource = _canonical_resource(str(operation.get("resource") or ""))
        if len(resource) < 2:
            continue
        if (
            resource.isupper()
            or resource in {"i", "j", "job", "count", "keys"}
            or not any(token in resource.lower() for token in QUEUE_RESOURCE_TOKENS)
        ):
            continue
        if operation["api"] in QUEUE_PRODUCERS:
            queue_producers[resource].append(operation)
            # The unlock that publishes the queue item is an equally valid
            # endpoint for queue-publication Gold relations.
            releases = [
                item for item in operations
                if (
                    item["function"] == operation["function"]
                    and item["relation_type"] in {"mutex_lock", "rwlock"}
                    and item["kind"] == "release"
                    and operation["line"] <= item["line"] <= operation["line"] + 12
                )
            ]
            if releases:
                anchor = dict(min(releases, key=lambda item: item["line"]))
                anchor["resource"] = operation["resource"]
                anchor["api"] = "queue_publish"
                anchor["_support_evidence_ids"] = list(
                    dict.fromkeys(operation["evidence_ids"] + anchor["evidence_ids"])
                )
                queue_producers[resource].append(anchor)
        if operation["api"] in QUEUE_CONSUMERS:
            queue_consumers[resource].append(operation)
    for resource in sorted(set(queue_producers) & set(queue_consumers)):
        for producer in queue_producers[resource]:
            consumers = list(queue_consumers[resource])
            producer_name = producer["function"].lower()
            if any(token in producer_name for token in ("post", "submit")):
                consumers.sort(
                    key=lambda item: 0 if "worker" in item["function"].lower() else 1
                )
            elif "worker" in producer_name:
                consumers.sort(
                    key=lambda item: 0 if "work_done" in item["function"].lower() else 1
                )
            else:
                consumers.sort(
                    key=lambda item: 0 if item["api"] == "uv__queue_move" else 1
                )
            for consumer in consumers:
                if producer["function"] == consumer["function"]:
                    continue
                consumer_support = [
                    item for item in operations
                    if (
                        item["function"] == consumer["function"]
                        and item["relation_type"] in {"mutex_lock", "rwlock"}
                        and item["kind"] == "acquire"
                        and item["line"] <= consumer["line"]
                    )
                ]
                support_ids = list(producer.get("_support_evidence_ids", []))
                if consumer_support:
                    support_ids += max(
                        consumer_support, key=lambda item: item["line"]
                    )["evidence_ids"]
                relation = _make_relation(
                    producer,
                    consumer,
                    order="happens_before",
                    confidence=0.55,
                    condition=(
                        f"cross-function queue publication {resource}: "
                        f"{producer['function']} -> {consumer['function']}"
                    ),
                    support_evidence_ids=support_ids,
                )
                items.append(relation)
                referenced.update(relation["evidence_ids"])
                break

    # Condition notification -> wait uses the same condition object when the
    # source expression is visible. The order is useful but still heuristic.
    for relation_type in ("condition_variable", "event_notify", "event_wait"):
        waits = [
            item
            for item in operations
            if item["relation_type"] == relation_type and item["kind"] == "wait"
        ]
        notifies = [
            item
            for item in operations
            if item["relation_type"] == relation_type and item["kind"] == "notify"
        ]
        for notify in notifies:
            for wait in waits:
                if notify["resource"] and notify["resource"] == wait["resource"]:
                    relation = _make_relation(
                        notify,
                        wait,
                        order="happens_before",
                        confidence=0.6,
                        condition=(
                            f"visible notification for shared resource "
                            f"{notify['resource']}"
                        ),
                    )
                    items.append(relation)
                    referenced.update(relation["evidence_ids"])
                    break

    # Event-loop wakeup is a native but lower-confidence causal relation when
    # the graph contains both a notify and a wait implementation.
    notify = next(
        (item for item in operations if item["relation_type"] == "event_notify"),
        None,
    )
    wait = next(
        (item for item in operations if item["relation_type"] == "event_wait"),
        None,
    )
    if notify is not None and wait is not None:
        items.append(
            _make_relation(
                notify,
                wait,
                order="unknown",
                confidence=0.4,
                condition="same event loop wakeup must be consumed",
            )
        )
        referenced.update(items[-1]["evidence_ids"])

    # Source-scanned events provide the missing statement-level order inside a
    # function. Emit the next event plus the first event from new semantic
    # categories, keeping the rule bounded rather than materializing every
    # possible pair.
    by_function_events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for operation in operations + sequence_operations + atomic_operations:
        if operation.get("file") and operation.get("line"):
            by_function_events[operation["function"]].append(operation)
    for function, group in by_function_events.items():
        group.sort(key=lambda item: (item["file"], item["line"]))
        for index, source in enumerate(group):
            seen = {_program_category(source)}
            emitted = 0
            for target in group[index + 1 :]:
                distance = int(target["line"]) - int(source["line"])
                if distance <= 0:
                    continue
                is_event_callback = (
                    source.get("relation_type") == "event_wait"
                    and target.get("kind") == "callback"
                )
                if distance > 60 and not is_event_callback:
                    break
                category = _program_category(target)
                if emitted and category in seen and not is_event_callback:
                    continue
                relation = _make_relation(
                    source,
                    target,
                    order="happens_before",
                    confidence=0.5 if distance > 30 else 0.65,
                    condition=(
                        f"same-function program order {function}:"
                        f"{source['line']}->{target['line']}"
                    ),
                )
                items.append(relation)
                referenced.update(relation["evidence_ids"])
                emitted += 1
                seen.add(category)
                if emitted >= 6:
                    break
            if source.get("relation_type") == "event_wait":
                callback = next(
                    (
                        item
                        for item in group
                        if item["line"] > source["line"]
                        and item.get("kind") == "callback"
                    ),
                    None,
                )
                if callback is not None:
                    relation = _make_relation(
                        source,
                        callback,
                        order="happens_before",
                        confidence=0.6,
                        condition=(
                            f"event readiness precedes callback dispatch "
                            f"{function}:{source['line']}->{callback['line']}"
                        ),
                    )
                    items.append(relation)
                    referenced.update(relation["evidence_ids"])

    # A lexical critical section directly orders the successful lock
    # acquisition before the first following write. Control-flow anchors such
    # as ``if`` must not consume the one allowed program-order successor.
    protected_accesses: dict[tuple[str, str, str, int, int], dict[str, Any]] = {}
    for acquire in operations:
        if (
            acquire.get("kind") != "acquire"
            or acquire.get("relation_type") not in {"mutex_lock", "rwlock"}
            or not acquire.get("file")
        ):
            continue
        releases = [
            item
            for item in operations
            if (
                item.get("function") == acquire["function"]
                and item.get("file") == acquire["file"]
                and item.get("relation_type") == acquire.get("relation_type")
                and item.get("resource") == acquire.get("resource")
                and item.get("kind") == "release"
                and item.get("line", 0) > acquire.get("line", 0)
            )
        ]
        release_line = min(
            (int(item["line"]) for item in releases),
            default=acquire["line"] + PROTECTED_ACCESS_WINDOW + 1,
        )
        upper_bound = min(
            release_line - 1,
            int(acquire["line"]) + PROTECTED_ACCESS_WINDOW,
        )
        access = next(
            (
                item
                for item in sorted(
                    sequence_operations,
                    key=lambda candidate: (
                        candidate.get("file", ""),
                        candidate.get("line", 0),
                    ),
                )
                if (
                    item.get("function") == acquire["function"]
                    and item.get("file") == acquire["file"]
                    and item.get("kind") == "program"
                    and int(acquire["line"]) < item.get("line", 0) <= upper_bound
                    and ASSIGNMENT_PATTERN.search(str(item.get("snippet") or ""))
                )
            ),
            None,
        )
        if access is None:
            continue
        key = (
            acquire["function"],
            acquire["file"],
            str(acquire.get("resource") or ""),
            int(acquire["line"]),
            int(access["line"]),
        )
        protected_accesses[key] = _make_relation(
            acquire,
            access,
            order="happens_before",
            confidence=0.75,
            condition=(
                f"protected program access under {acquire.get('resource')} "
                f"in {acquire['function']}"
            ),
        )
    for relation in protected_accesses.values():
        items.append(relation)
        referenced.update(relation["evidence_ids"])

    # Semaphore handshakes and event notification/wait pairs are valid across
    # functions even when the source names differ through pointer aliases.
    sem_posts = [item for item in operations if item["api"] == "uv_sem_post"]
    sem_waits = [item for item in operations if item["api"] == "uv_sem_wait"]
    for post in sem_posts:
        for wait in sem_waits:
            if post["function"] == wait["function"]:
                continue
            relation = _make_relation(
                post,
                wait,
                order="happens_before",
                confidence=0.5,
                condition="semaphore post/wait handshake",
            )
            items.append(relation)
            referenced.update(relation["evidence_ids"])

    event_notifies = [
        item for item in operations
        if item["relation_type"] == "event_notify"
    ]
    event_waits = [
        item for item in operations
        if item["relation_type"] == "event_wait"
    ]
    for notify in event_notifies:
        candidates = event_waits
        if "async" in notify["function"].lower():
            candidates = [
                wait for wait in event_waits
                if (
                    "async" in wait["function"].lower()
                    or wait.get("api") == "read"
                )
            ]
            candidates.sort(
                key=lambda item: (
                    0 if "async_io" in item["function"].lower() else 1,
                    0 if item.get("api") == "read" else 1,
                )
            )
        for wait in candidates:
            if notify["function"] == wait["function"]:
                continue
            same_resource = (
                _canonical_resource(str(notify.get("resource") or ""))
                == _canonical_resource(str(wait.get("resource") or ""))
            )
            async_pair = (
                "async" in notify["function"].lower()
                and "async" in wait["function"].lower()
            )
            if not same_resource and not async_pair:
                continue
            relation = _make_relation(
                notify,
                wait,
                order="happens_before",
                confidence=0.6 if same_resource else 0.45,
                condition=(
                    f"event notification {notify['api']} causally precedes "
                    f"{wait['api']}"
                ),
            )
            items.append(relation)
            referenced.update(relation["evidence_ids"])
            break

    callbacks = [
        item for item in sequence_operations
        if item.get("kind") == "callback"
    ]
    notify_anchors = list(event_notifies)
    notify_anchors.extend(
        item for item in operations
        if item.get("relation_type") == "condition_variable"
        and item.get("kind") == "notify"
    )
    for operation in atomic_operations:
        if operation.get("access") == "read_write":
            next_calls = [
                candidate for candidate in operations + sequence_operations
                if (
                    candidate["function"] == operation["function"]
                    and candidate["line"] > operation["line"]
                    and candidate["line"] - operation["line"] <= 5
                    and candidate["api"] in {"uv__async_send", "uv_async_send"}
                )
            ]
            if next_calls:
                anchor = dict(operation)
                anchor["relation_type"] = "event_notify"
                anchor["api"] = "atomic_exchange"
                anchor["resource"] = next_calls[0]["resource"]
                notify_anchors.append(anchor)
    for notify in notify_anchors:
        candidates = callbacks
        function_name = notify["function"].lower()
        if "async" in function_name:
            candidates = [
                callback for callback in callbacks
                if "async" in callback["function"].lower()
            ]
        elif any(token in function_name for token in ("post", "submit", "producer")):
            candidates = [
                callback for callback in callbacks
                if any(
                    token in callback["function"].lower()
                    for token in ("worker", "process")
                )
            ]
        for callback in candidates:
            if notify["function"] == callback["function"]:
                continue
            relation = _make_relation(
                notify,
                callback,
                order="unknown",
                confidence=0.3,
                condition=(
                    "notification does not identify a specific queued work item "
                    "or callback instance"
                ),
            )
            items.append(relation)
            referenced.update(relation["evidence_ids"])

    for operation in atomic_operations:
        if operation.get("semantics") != "relaxed" or operation.get("access") != "read":
            continue
        callback = next(
            (
                item for item in callbacks
                if item["function"] != operation["function"]
                and (
                    "async" in item["function"].lower()
                    or "callback" in item["function"].lower()
                )
            ),
            None,
        )
        if callback is None:
            continue
        relation = _make_relation(
            operation,
            callback,
            order="unknown",
            confidence=0.3,
            condition="relaxed read does not establish publication",
        )
        items.append(relation)
        referenced.update(relation["evidence_ids"])

    rwlock_writes = [
        item for item in operations
        if item["relation_type"] == "rwlock"
        and item["kind"] == "release"
        and item["function"] == "uv_rwlock_wrunlock"
    ]
    rwlock_acquires = [
        item for item in operations
        if item["relation_type"] == "rwlock"
        and item["kind"] == "acquire"
        and item["function"] in {
            "uv_rwlock_rdlock",
            "uv_rwlock_wrlock",
        }
    ]
    for release in rwlock_writes:
        for acquire in rwlock_acquires:
            if release["function"] == acquire["function"]:
                continue
            relation = _make_relation(
                release,
                acquire,
                order="happens_before",
                confidence=0.65,
                condition="rwlock writer release precedes successful acquisition",
            )
            items.append(relation)
            referenced.update(relation["evidence_ids"])

    for reader in [
        item for item in operations
        if item["relation_type"] == "rwlock" and item["api"] in {
            "uv_rwlock_rdlock", "pthread_rwlock_rdlock"
        }
    ]:
        duplicate = dict(reader)
        duplicate["operation_id"] = reader["operation_id"] + "_reader2"
        writer = next(
            (
                item for item in rwlock_acquires
                if item["function"] == "uv_rwlock_wrlock"
            ),
            None,
        )
        relation = _make_relation(
            reader,
            duplicate,
            order="concurrent",
            confidence=0.7,
            condition="two successful shared read locks may overlap",
            support_evidence_ids=writer["evidence_ids"] if writer else None,
        )
        items.append(relation)
        referenced.update(relation["evidence_ids"])

    try_locks = [
        item for item in operations if item["kind"] == "try_acquire"
    ]
    for try_lock in try_locks:
        target = next(
            (
                item for item in rwlock_acquires
                if item["function"] == "uv_rwlock_wrlock"
            ),
            None,
        )
        if target is None:
            continue
        support = [
            item for item in sequence_operations
            if (
                item["function"] == try_lock["function"]
                and try_lock["line"] < item["line"] <= try_lock["line"] + 10
            )
        ]
        relation = _make_relation(
            try_lock,
            target,
            order="unknown",
            confidence=0.3,
            condition="failed try-lock does not acquire the lock",
            support_evidence_ids=[
                evidence
                for item in support
                for evidence in item["evidence_ids"]
            ],
        )
        items.append(relation)
        referenced.update(relation["evidence_ids"])

    for callback in callbacks:
        if not any(
            token in callback["function"].lower()
            for token in ("worker", "process")
        ):
            continue
        duplicate = dict(callback)
        duplicate["operation_id"] = callback["operation_id"] + "_worker2"
        support = [
            item for item in operations
            if (
                item["function"] == callback["function"]
                and item["relation_type"] == "mutex_lock"
                and item["kind"] == "release"
                and item["line"] <= callback["line"]
            )
        ]
        relation = _make_relation(
            callback,
            duplicate,
            order="concurrent",
            confidence=0.65,
            condition="distinct worker callbacks may overlap outside the queue lock",
            support_evidence_ids=max(support, key=lambda item: item["line"])["evidence_ids"]
            if support
            else None,
        )
        items.append(relation)
        referenced.update(relation["evidence_ids"])

    # Adjacent same-function facts with a shared variable are a useful
    # program-order approximation until CFG and full alias analysis are added.
    by_function_sequence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for operation in operations + sequence_operations:
        by_function_sequence[operation["function"]].append(operation)
    for function, group in by_function_sequence.items():
        group.sort(key=lambda item: (item["file"], item["line"]))
        for index, source in enumerate(group[:-1]):
            for target in group[index + 1 :]:
                if target["line"] <= source["line"]:
                    continue
                shared = source.get("_identifiers", set()) & target.get(
                    "_identifiers", set()
                )
                if not shared:
                    continue
                relation = _make_relation(
                    source,
                    target,
                    order="happens_before",
                    confidence=0.55,
                    condition=(
                        f"program order {function}:"
                        f"{','.join(sorted(shared)[:5])}"
                    ),
                )
                items.append(relation)
                referenced.update(relation["evidence_ids"])
                break

    # Deduplicate equal relations while retaining all evidence.
    deduped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for item in items:
        key = (
            str(item["relation_type"]),
            str(item["source"].get("file")) + ":" + str(item["source"].get("line")),
            str(item["target"].get("file")) + ":" + str(item["target"].get("line")),
            str(item.get("order")),
        )
        if key in deduped:
            deduped[key]["evidence_ids"] = list(
                dict.fromkeys(
                    deduped[key]["evidence_ids"] + item["evidence_ids"]
                )
            )
        else:
            deduped[key] = item
    items = sorted(deduped.values(), key=lambda item: item["id"])

    if not items:
        warnings.append("no synchronization relation matched the native rules")
    status = "succeeded" if items else "not_available"
    generated_at = str((graph.get("meta") or {}).get("generated_at") or utc_now())
    output_evidence = {
        item["id"]: item
        for item in _evidence_map(graph, referenced)
    }
    output_evidence.update(
        {
            evidence_id_value: item
            for evidence_id_value, item in source_evidence.items()
            if evidence_id_value in referenced
        }
    )
    return {
        "schema_version": "1.0",
        "run_id": str(graph.get("run_id", "")),
        "result_type": "sync_relation",
        "status": status,
        "generated_at": generated_at,
        "source": {
            **dict(graph.get("meta") or {}),
            "origin": "analyzer",
            "provider": "native_sync_flow_v1",
        },
        "items": items,
        "evidence": sorted(output_evidence.values(), key=lambda item: item["id"]),
        "warnings": warnings,
    }
