#!/usr/bin/env python3
"""Audit every synchronization prediction originating in a closed file scope."""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clang_pipeline.sync_flow import (
    QUEUE_CONSUMERS,
    QUEUE_PRODUCERS,
    collect_sync_events,
)
from clang_pipeline.source_context import order_decision


SCOPE_FILES = (
    "third_party/libuv/src/threadpool.c",
    "third_party/libuv/src/unix/async.c",
    "third_party/libuv/src/unix/thread.c",
    "third_party/libuv/src/unix/linux.c",
)

QUEUE_PRODUCERS = (
    "uv__queue_insert_tail",
    "listAddNodeTail",
    "setIOPendingCount",
)
QUEUE_CONSUMERS = (
    "uv__queue_head",
    "uv__queue_remove",
    "uv__queue_move",
    "listFirst",
    "listDelNode",
    "getIOPendingCount",
)
ATOMIC_TOKENS = (
    "atomic_",
    "__atomic_",
    "atomicIncr",
    "atomicDecr",
    "atomicGet",
    "atomicSet",
)
EVENT_NOTIFY_TOKENS = (
    "uv_async_send",
    "uv__async_send",
    "write(",
)
EVENT_WAIT_TOKENS = (
    "read(",
    "poll(",
    "epoll_wait",
    "epoll_pwait",
    "kevent",
    "uv__async_io",
)
CALLBACK_FRAGMENTS = (
    "->work(",
    "->done(",
    "->cb(",
    "->async_cb(",
    "->read_cb(",
    "->write_cb(",
    "->connect_cb(",
)
EVENT_CONSUMER_FUNCTIONS = {
    "uv__async_io",
    "uv__io_poll",
    "uv__inotify_read",
    "uv__poll_io_uring",
}
SAME_FUNCTION_WINDOW = 60


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize(text: Any) -> str:
    return " ".join(str(text or "").split())


def canonical_resource(text: str) -> str:
    value = normalize(text).strip("&*() ")
    value = value.replace("w->loop->", "loop->")
    value = value.replace("_mutex", "")
    value = value.replace("->", ".")
    return value


def identity_key(item: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    source = item["source"]
    target = item["target"]
    return (
        str(item["order"]),
        (
            source.get("function", ""),
            target.get("function", ""),
            source.get("file", ""),
            source.get("line"),
            target.get("file", ""),
            target.get("line"),
        ),
    )


def ground_truth_key(case: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    return str(case["kind"]), tuple(case["identity"])


def evidence_anchor(item: dict[str, Any]) -> tuple[str, int | None, str]:
    item = item.get("location", item)
    return (
        str(item.get("file", "")).replace("\\", "/"),
        item.get("line"),
        normalize(item.get("snippet", "")),
    )


def endpoint_anchor(item: dict[str, Any]) -> tuple[str, int | None, str]:
    return evidence_anchor(item)


def has_token(snippet: str, tokens: tuple[str, ...]) -> bool:
    lowered = snippet.lower()
    return any(token.lower() in lowered for token in tokens)


def queue_resource(snippet: str) -> str | None:
    for api in QUEUE_PRODUCERS + QUEUE_CONSUMERS:
        match = re.search(rf"\b{re.escape(api)}\s*\(\s*&?([^,\s)]+)", snippet)
        if match:
            return canonical_resource(match.group(1))
    return None


def atomic_resource(snippet: str) -> str | None:
    match = re.search(
        r"\b(?:atomic_[A-Za-z_]+|__atomic_[A-Za-z_]+)\s*\(\s*&?([^,\s)]+)",
        snippet,
    )
    return canonical_resource(match.group(1)) if match else None


def source_line(path: Path, line: int) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if line < 1 or line > len(lines):
        return None
    return lines[line - 1].strip()


def validate_endpoint(
    endpoint: dict[str, Any],
    evidence_anchors: set[tuple[str, int | None, str]],
    source_root: Path,
) -> tuple[bool, str]:
    anchor = endpoint_anchor(endpoint)
    if anchor not in evidence_anchors:
        return False, "endpoint is not backed by one of the item's evidence anchors"
    file = str(endpoint.get("file") or "")
    line = endpoint.get("line")
    if not isinstance(line, int):
        return False, "endpoint line is not an integer"
    actual = source_line(source_root / file, line)
    if actual is None:
        return False, "source line is missing or out of range"
    if normalize(actual) != normalize(endpoint.get("snippet")):
        return False, "source snippet does not match the pinned checkout"
    return True, "source anchor verified"


def event_location(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "function": event.get("function", ""),
        "thread": event.get("thread"),
        "file": event.get("file", ""),
        "line": event.get("line"),
        "snippet": event.get("snippet", ""),
        "held_locks": list(event.get("held_locks", [])),
        "evidence_ids": list(event.get("evidence_ids", [])),
    }


def candidate_key(
    source: dict[str, Any],
    target: dict[str, Any],
    order: str,
) -> tuple[str, tuple[Any, ...]]:
    return (
        order,
        (
            source.get("function", ""),
            target.get("function", ""),
            source.get("file", ""),
            source.get("line"),
            target.get("file", ""),
            target.get("line"),
        ),
    )


def add_candidate(
    candidates: dict[tuple[str, tuple[Any, ...]], dict[str, Any]],
    *,
    source: dict[str, Any],
    target: dict[str, Any],
    order: str,
    origin: str,
    condition: str,
    support_evidence_ids: list[str] | None = None,
) -> None:
    source_location = event_location(source) if "operation_id" in source else source
    target_location = event_location(target) if "operation_id" in target else target
    key = candidate_key(source_location, target_location, order)
    evidence_ids = list(
        dict.fromkeys(
            list(source_location.get("evidence_ids", []))
            + list(target_location.get("evidence_ids", []))
            + list(support_evidence_ids or [])
        )
    )
    existing = candidates.get(key)
    if existing is not None:
        existing["origins"] = sorted(set(existing["origins"]) | {origin})
        existing["evidence_ids"] = list(
            dict.fromkeys(existing["evidence_ids"] + evidence_ids)
        )
        return
    digest = hashlib.sha256(
        json.dumps([order, key[1]], separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    candidates[key] = {
        "id": "universe_" + digest,
        "relation_type": source.get("relation_type", "happens_before"),
        "source": source_location,
        "target": target_location,
        "order": order,
        "condition": condition,
        "origins": [origin],
        "evidence_ids": evidence_ids,
        "expected_exists": True,
    }


def is_same_function_candidate(candidate: dict[str, Any]) -> bool:
    source = candidate["source"]
    target = candidate["target"]
    return (
        source.get("file") == target.get("file")
        and source.get("function") == target.get("function")
        and candidate.get("order") == "happens_before"
    )


def build_generated_universe(
    graph: dict[str, Any],
    source_root: Path,
    gold_cases: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    operations, atomic_operations, sequence_operations, _ = collect_sync_events(
        graph,
        source_root=source_root,
    )
    in_scope = lambda event: str(event.get("file") or "") in SCOPE_FILES
    operations = [event for event in operations if event.get("file")]
    atomic_operations = [event for event in atomic_operations if event.get("file")]
    sequence_operations = [event for event in sequence_operations if event.get("file")]
    events = operations + atomic_operations + sequence_operations
    candidates: dict[tuple[str, tuple[Any, ...]], dict[str, Any]] = {}

    grouped_events: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        if in_scope(event):
            grouped_events[(event["file"], event["function"])].append(event)
    for (_, function), group in grouped_events.items():
        group.sort(key=lambda event: (event["line"], event["operation_id"]))
        last_line = -1
        previous = None
        for event in group:
            line = int(event.get("line") or 0)
            if line <= last_line:
                continue
            if (
                previous is not None
                and line > int(previous.get("line") or 0)
                and line - int(previous.get("line") or 0) <= SAME_FUNCTION_WINDOW
            ):
                order, order_condition = order_decision(
                    previous.get("_control_regions", ()),
                    int(previous.get("line") or 0),
                    line,
                )
                add_candidate(
                    candidates,
                    source=previous,
                    target=event,
                    order=order,
                    origin="same_function_adjacent",
                    condition=(
                        f"same-thread event order {function}:"
                        f"{previous.get('line')}->{line}; {order_condition}"
                    ),
                )
            previous = event
            last_line = line

    lock_groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for operation in operations:
        if (
            in_scope(operation)
            and operation.get("relation_type") in {"mutex_lock", "rwlock"}
        ):
            lock_groups[
                (
                    operation.get("file", ""),
                    operation.get("function", ""),
                    operation.get("relation_type", ""),
                    operation.get("resource", ""),
                )
            ].append(operation)
    for (_, function, _, resource), group in lock_groups.items():
        group.sort(key=lambda operation: operation["line"])
        releases = [
            operation for operation in group if operation.get("kind") == "release"
        ]
        for acquire in (
            operation
            for operation in group
            if operation.get("kind") == "acquire"
        ):
            release = next(
                (
                    operation
                    for operation in releases
                    if operation["line"] >= acquire["line"]
                ),
                None,
            )
            if release is not None:
                order, order_condition = order_decision(
                    acquire.get("_control_regions", ()),
                    int(acquire.get("line") or 0),
                    int(release.get("line") or 0),
                )
                add_candidate(
                    candidates,
                    source=acquire,
                    target=release,
                    order=order,
                    origin="same_function_lock_pair",
                    condition=(
                        f"same lock/resource {function}:{resource}; "
                        f"{order_condition}"
                    ),
                )

    queue_producers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    queue_consumers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    queue_function_links: dict[
        tuple[str, str], list[dict[str, Any]]
    ] = defaultdict(list)
    for event in sequence_operations:
        if not in_scope(event):
            continue
        resource = canonical_resource(str(event.get("resource") or ""))
        if len(resource) < 2:
            continue
        if not any(
            token in resource.lower()
            for token in ("queue", "wq", "jobs", "pending", "reply", "watcher", "ready")
        ):
            continue
        if event.get("api") in QUEUE_PRODUCERS:
            queue_producers[resource].append(event)
        if event.get("api") in QUEUE_CONSUMERS:
            if (
                event.get("api") == "uv__queue_head"
                and not re.search(
                    r"(?<![=!<>])=(?!=)",
                    str(event.get("snippet") or ""),
                )
            ):
                continue
            queue_consumers[resource].append(event)
    for resource in sorted(set(queue_producers) & set(queue_consumers)):
        for producer in queue_producers[resource]:
            for consumer in queue_consumers[resource]:
                if producer["function"] == consumer["function"]:
                    continue
                if (
                    resource == "wq"
                    and "worker" not in consumer["function"].lower()
                ):
                    continue
                producer_releases = [
                    event
                    for event in operations
                    if (
                        event.get("function") == producer["function"]
                        and event.get("kind") == "release"
                        and producer["line"] <= event.get("line", 0) <= producer["line"] + 12
                    )
                ]
                consumer_acquires = [
                    event
                    for event in operations
                    if (
                        event.get("function") == consumer["function"]
                        and event.get("kind") == "acquire"
                        and event.get("line", 0) <= consumer["line"]
                    )
                ]
                support = [
                    evidence_id
                    for event in (
                        producer_releases[-1:]
                        + consumer_acquires[-1:]
                    )
                    for evidence_id in event.get("evidence_ids", [])
                ]
                order = "happens_before" if producer_releases and consumer_acquires else "unknown"
                source_endpoint = producer_releases[-1] if producer_releases else producer
                add_candidate(
                    candidates,
                    source=source_endpoint,
                    target=consumer,
                    order=order,
                    origin="queue_producer_consumer",
                    condition=f"queue {resource}: {producer['function']} -> {consumer['function']}",
                    support_evidence_ids=support,
                )
                queue_function_links[
                    (producer["function"], consumer["function"])
                ].append(
                    {
                        "resource": resource,
                        "source": producer,
                        "target": consumer,
                    }
                )

    by_atomic_resource: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in atomic_operations:
        if in_scope(event):
            by_atomic_resource[canonical_resource(str(event.get("resource") or ""))].append(
                event
            )
    for resource, group in by_atomic_resource.items():
        if not resource or any(character.isspace() for character in resource):
            continue
        releases = [
            event
            for event in group
            if event.get("signature_kind") == "atomic"
            or (
                event.get("access") in {"write", "read_write"}
                and (
                    event.get("kind") == "release"
                    or event.get("semantics") in {"release", "seq_cst"}
                )
            )
        ]
        acquires = [
            event
            for event in group
            if event.get("access") in {"read", "read_write"}
            and (
                event.get("kind") == "acquire"
                or event.get("semantics") in {"acquire", "seq_cst"}
            )
        ]
        for release in releases:
            for acquire in acquires:
                if (
                    release["file"] == acquire["file"]
                    and acquire["line"] <= release["line"]
                ):
                    continue
                if release["function"] == acquire["function"] and release["line"] >= acquire["line"]:
                    continue
                add_candidate(
                    candidates,
                    source=release,
                    target=acquire,
                    order="happens_before",
                    origin="atomic_publish",
                    condition=f"atomic {resource} release/acquire",
                )
        relaxed_writes = [
            event
            for event in group
            if (
                event.get("semantics") == "relaxed"
                and event.get("access") in {"write", "read_write"}
            )
        ]
        for source in relaxed_writes:
            for target in group:
                if (
                    source["operation_id"] == target["operation_id"]
                    or source.get("file") != target.get("file")
                    or (
                        source.get("function") == target.get("function")
                        and int(source.get("line") or 0) >= int(target.get("line") or 0)
                    )
                ):
                    continue
                add_candidate(
                    candidates,
                    source=source,
                    target=target,
                    order="unknown",
                    origin="atomic_relaxed",
                    condition=f"atomic {resource} relaxed does not publish",
                )
                break

    by_thread_handle: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in operations:
        if in_scope(event) and event.get("relation_type") == "thread_join":
            by_thread_handle[event.get("resource", "")].append(event)
    for handle, group in by_thread_handle.items():
        creates = [event for event in group if event.get("kind") == "create"]
        joins = [event for event in group if event.get("kind") == "join"]
        for create in creates:
            for join in joins:
                add_candidate(
                    candidates,
                    source=create,
                    target=join,
                    order="happens_before",
                    origin="thread_join",
                    condition=f"thread handle {handle} is joined",
                )

    condition_notifies = [
        event
        for event in operations
        if (
            in_scope(event)
            and
            event.get("relation_type") == "condition_variable"
            and event.get("kind") == "notify"
        )
    ]
    condition_waits = [
        event
        for event in operations
        if (
            in_scope(event)
            and
            event.get("relation_type") == "condition_variable"
            and event.get("kind") == "wait"
        )
    ]
    for notify in condition_notifies:
        for wait in condition_waits:
            if "cond_wait" not in str(wait.get("snippet") or ""):
                continue
            if "cond_wait" not in str(wait.get("function") or ""):
                continue
            if (
                notify.get("function") == wait.get("function")
                and int(notify.get("line") or 0) >= int(wait.get("line") or 0)
            ):
                continue
            if (
                notify.get("resource")
                and notify.get("file") == wait.get("file")
                and canonical_resource(str(notify["resource"]))
                == canonical_resource(str(wait.get("resource") or ""))
            ):
                add_candidate(
                    candidates,
                    source=notify,
                    target=wait,
                    order=(
                        "happens_before"
                        if notify["function"] == wait["function"]
                        else "unknown"
                    ),
                    origin="condition_notify_wait",
                    condition=f"condition resource {notify['resource']}",
                )

    event_notifies = [
        event
        for event in operations
        if in_scope(event) and event.get("relation_type") == "event_notify"
    ]
    event_waits = [
        event for event in operations if event.get("relation_type") == "event_wait"
    ]
    for notify in event_notifies:
        for wait in event_waits:
            same_resource = (
                notify.get("resource")
                and canonical_resource(str(notify["resource"]))
                == canonical_resource(str(wait.get("resource") or ""))
            )
            async_pair = (
                "async" in notify.get("function", "").lower()
                and "async" in wait.get("function", "").lower()
            )
            consumer_wait = has_token(
                str(wait.get("snippet") or ""),
                EVENT_WAIT_TOKENS,
            )
            if (
                notify.get("file") == wait.get("file")
                and wait.get("function") in EVENT_CONSUMER_FUNCTIONS
                and consumer_wait
                and (same_resource or async_pair)
            ):
                add_candidate(
                    candidates,
                    source=notify,
                    target=wait,
                    order="happens_before",
                    origin="event_notify_wait",
                    condition="event notification/wait witness",
                )

    rwlock_releases = [
        event
        for event in operations
        if (
            in_scope(event)
            and
            event.get("relation_type") == "rwlock"
            and event.get("kind") == "release"
            and "wrunlock" in (
                str(event.get("api") or "") + str(event.get("function") or "")
            )
        )
    ]
    rwlock_acquires = [
        event
        for event in operations
        if (
            in_scope(event)
            and
            event.get("relation_type") == "rwlock"
            and event.get("kind") == "acquire"
            and any(
                token in str(event.get("api") or "")
                for token in ("rdlock", "wrlock")
            )
        )
    ]
    for release in rwlock_releases:
        for acquire in rwlock_acquires:
            if release["function"] == acquire["function"]:
                continue
            add_candidate(
                candidates,
                source=release,
                target=acquire,
                order="happens_before",
                origin="rwlock_release_acquire",
                condition="rwlock writer release precedes acquisition",
            )

    callbacks = [
        event for event in sequence_operations if event.get("kind") == "callback"
    ]
    notify_events = condition_notifies + event_notifies
    for notify in notify_events:
        for (producer_function, consumer_function), links in queue_function_links.items():
            if producer_function != notify["function"]:
                continue
            for callback in callbacks:
                if callback["function"] != consumer_function:
                    continue
                support = [
                    evidence_id
                    for link in links
                    for event in (link["source"], link["target"])
                    for evidence_id in event.get("evidence_ids", [])
                ]
                add_candidate(
                    candidates,
                    source=notify,
                    target=callback,
                    order="unknown",
                    origin="queue_notification_callback",
                    condition=(
                        f"queue notification callback {consumer_function}; "
                        "specific item remains unknown"
                    ),
                    support_evidence_ids=support,
                )

    async_callbacks = [
        callback
        for callback in callbacks
        if (
            callback.get("function") == "uv__async_io"
            and "async_cb" in str(callback.get("snippet") or "")
        )
    ]
    for event in atomic_operations:
        if (
            not in_scope(event)
            or
            event.get("access") != "read_write"
            or "async" not in event.get("function", "").lower()
        ):
            continue
        for callback in async_callbacks:
            if callback.get("file") != event.get("file"):
                continue
            add_candidate(
                candidates,
                source=event,
                target=callback,
                order=(
                    "happens_before"
                    if event.get("function") == callback.get("function")
                    else "unknown"
                ),
                origin="async_atomic_callback",
                condition="async atomic notification/callback boundary",
            )

    for gold_case in gold_cases:
        if not gold_case.get("expected_exists"):
            continue
        source_evidence = gold_case.get("evidence", [])
        if len(source_evidence) < 2:
            continue
        identity = gold_case["identity"]
        source_item = next(
            (
                item
                for item in source_evidence
                if item.get("file") == identity[2]
                and item.get("line") == identity[3]
            ),
            source_evidence[0],
        )
        target_item = next(
            (
                item
                for item in source_evidence
                if item.get("file") == identity[4]
                and item.get("line") == identity[5]
            ),
            source_evidence[1],
        )
        source = {
            "function": identity[0],
            "file": identity[2],
            "line": identity[3],
            "snippet": source_item.get("snippet", ""),
            "evidence_ids": [source_item.get("id", "")],
        }
        target = {
            "function": identity[1],
            "file": identity[4],
            "line": identity[5],
            "snippet": target_item.get("snippet", ""),
            "evidence_ids": [target_item.get("id", "")],
        }
        add_candidate(
            candidates,
            source=source,
            target=target,
            order=gold_case["kind"],
            origin="gold",
            condition=gold_case.get("condition", "frozen Gold"),
        )

    values = sorted(
        candidates.values(),
        key=lambda item: (
            item["source"].get("file", ""),
            item["source"].get("line", 0),
            item["target"].get("file", ""),
            item["target"].get("line", 0),
            item["order"],
        ),
    )
    counts = Counter(
        origin
        for candidate in values
        for origin in candidate.get("origins", [])
    )
    return values, {
        "total": len(values),
        "by_origin": dict(counts),
        "same_function": sum(is_same_function_candidate(item) for item in values),
    }


def evaluate_universe(
    candidates: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    accepted_keys = {
        (
            str(record["order"]),
            (
                record["source_function"],
                record["target_function"],
                record["source_file"],
                record["source_line"],
                record["target_file"],
                record["target_line"],
            ),
        )
        for record in records
        if record["decision"] == "accepted"
    }
    adjacency: dict[tuple[str, str], dict[int, set[int]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for record in records:
        if (
            record["decision"] == "accepted"
            and record["order"] == "happens_before"
            and record["source_file"] == record["target_file"]
            and record["source_function"] == record["target_function"]
        ):
            adjacency[(record["source_file"], record["source_function"])][
                record["source_line"]
            ].add(record["target_line"])

    def reachable(key: tuple[str, str], start: int, goal: int) -> bool:
        graph = adjacency.get(key, {})
        stack = [start]
        seen = set()
        while stack:
            current = stack.pop()
            if current == goal:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(graph.get(current, ()))
        return False

    covered = []
    missing = []
    for candidate in candidates:
        key = candidate_key(
            candidate["source"],
            candidate["target"],
            candidate["order"],
        )
        hit = key in accepted_keys
        if (
            not hit
            and is_same_function_candidate(candidate)
        ):
            hit = reachable(
                (
                    candidate["source"]["file"],
                    candidate["source"]["function"],
                ),
                int(candidate["source"]["line"]),
                int(candidate["target"]["line"]),
            )
        candidate["covered"] = hit
        if hit:
            covered.append(candidate)
        else:
            missing.append(candidate)
    return {
        "total": len(candidates),
        "covered": len(covered),
        "missing": len(missing),
        "recall": len(covered) / len(candidates) if candidates else None,
        "missing_candidates": missing,
    }


def classify_prediction(
    item: dict[str, Any],
    *,
    evidence_ok: bool,
    evidence_reason: str,
    gold_state: str,
) -> tuple[str, str]:
    if gold_state == "positive":
        return "accepted", "matches a frozen positive Gold identity and order"
    if gold_state == "negative":
        return "rejected", "matches a frozen negative Gold identity and order"
    if gold_state == "wrong_kind":
        return "rejected", "annotated identity has a different required order"
    if not evidence_ok:
        return "rejected", evidence_reason

    source = item["source"]
    target = item["target"]
    order = str(item.get("order") or "")
    condition = normalize(item.get("condition"))
    source_snippet = normalize(source.get("snippet"))
    target_snippet = normalize(target.get("snippet"))
    same_file = source.get("file") == target.get("file")
    same_function = (
        same_file and source.get("function") == target.get("function")
    )
    source_line_value = int(source.get("line") or 0)
    target_line_value = int(target.get("line") or 0)

    if same_function and order == "happens_before":
        if any(
            token in condition
            for token in (
                "mutually exclusive control-flow",
                "loop back-edge",
            )
        ):
            return "rejected", "control-flow evidence contradicts claimed order"
        if 0 < source_line_value < target_line_value:
            return "accepted", "same-function source order is consistent"
        if source_line_value == target_line_value:
            return "needs_review", "same-line ordering requires expression-level proof"
        return "rejected", "same-function line order contradicts the claimed direction"

    if same_function and order == "concurrent":
        if any(
            token in condition
            for token in (
                "two successful shared read locks",
                "distinct worker callbacks",
                "may overlap",
            )
        ):
            return "accepted", "specific same-function concurrency boundary"
        return "needs_review", "same-function concurrency lacks a specific boundary"

    if same_function and order == "unknown":
        if any(
            token in condition
            for token in (
                "failed try-lock",
                "relaxed",
                "does not identify",
                "not establish",
                "mutually exclusive control-flow",
                "loop back-edge",
            )
        ):
            return "accepted", "specific same-function unknown boundary"
        return "needs_review", "same-function unknown reason is not specific"

    if "cross-function queue publication" in condition:
        producer_resources = {
            resource
            for value in (source_snippet, target_snippet)
            if (resource := queue_resource(value))
        }
        producer_endpoint = has_token(source_snippet, QUEUE_PRODUCERS) or "unlock" in source_snippet
        consumer_endpoint = has_token(target_snippet, QUEUE_CONSUMERS)
        if producer_endpoint and consumer_endpoint and producer_resources:
            return "accepted", "queue producer, consumer and protected queue witness agree"
        return "needs_review", "queue publication lacks a producer/consumer witness pair"

    if condition.startswith("queue notification boundary "):
        if len(item.get("evidence_ids", [])) >= 3 and any(
            fragment in target_snippet
            for fragment in CALLBACK_FRAGMENTS
        ):
            return "accepted", "queue publication supplies the notification/callback witness"
        return "needs_review", "queue notification lacks a queue witness or callback endpoint"

    if condition == "semaphore post/wait handshake":
        if "sem_post" in source_snippet and "sem_wait" in target_snippet:
            return "accepted", "semaphore post/wait direction verified"
        return "rejected", "semaphore endpoints do not match post then wait"

    if condition.startswith("thread handle ") and condition.endswith(" is joined"):
        if "thread_create" in source_snippet and "thread_join" in target_snippet:
            return "accepted", "thread create/join direction verified"
        return "needs_review", "thread create/join instruction pairing needs instance proof"

    if condition == "rwlock writer release precedes successful acquisition":
        if "unlock" in source_snippet and any(
            token in target_snippet
            for token in ("rdlock", "wrlock")
        ):
            return "accepted", "rwlock release/acquire direction verified"
        return "rejected", "rwlock endpoints do not match release then acquisition"

    if condition.startswith("atomic ") and "release/acquire" in condition:
        source_resource = atomic_resource(source_snippet)
        target_resource = atomic_resource(target_snippet)
        if (
            source_resource
            and target_resource
            and canonical_resource(source_resource) == canonical_resource(target_resource)
        ):
            return "accepted", "atomic release/acquire uses the same resource token"
        return "needs_review", "atomic publication lacks matching resource tokens"

    if condition.startswith("event notification ") and "causally precedes" in condition:
        notify = has_token(source_snippet, EVENT_NOTIFY_TOKENS)
        wait = has_token(target_snippet, EVENT_WAIT_TOKENS)
        async_pair = (
            "async" in str(source.get("function", "")).lower()
            and "async" in str(target.get("function", "")).lower()
        )
        if notify and wait and async_pair:
            return "accepted", "async notification/wait endpoint direction verified"
        return "needs_review", "event pair lacks a same-resource or async witness"

    if condition == "same event loop wakeup must be consumed":
        if has_token(source_snippet, EVENT_NOTIFY_TOKENS) and has_token(
            target_snippet,
            EVENT_WAIT_TOKENS,
        ):
            return "accepted", "event-loop notify/wait pair is a specific unknown boundary"
        return "rejected", "event-loop pair does not contain notify then wait endpoints"

    if condition == "async notification may be coalesced and does not identify one callback instance":
        if (
            same_file
            and has_token(source_snippet, ATOMIC_TOKENS)
            and "async" in str(source.get("function", "")).lower()
            and "async" in str(target.get("function", "")).lower()
            and "async_cb" in target_snippet
        ):
            return "accepted", "same async path supplies the callback-boundary witness"
        return "needs_review", "async callback boundary lacks a same-resource witness"

    if condition.startswith("visible notification for shared resource "):
        if "cond_" in source_snippet and "cond_wait" in target_snippet:
            return "accepted", "condition notification/wait endpoints share the declared resource"
        return "rejected", "condition notification target is not a wait operation"

    if condition.startswith("condition notification/wait resource "):
        if "cond_" in source_snippet and "cond_wait" in target_snippet:
            return "accepted", "condition notification/wait resource verified"
        return "rejected", "condition notification target is not a wait operation"

    if condition == "two successful shared read locks may overlap":
        if "rdlock" in source_snippet and "rdlock" in target_snippet:
            return "accepted", "reader/reader overlap boundary verified"
        return "rejected", "reader overlap endpoints are not both read locks"

    if condition == "failed try-lock does not acquire the lock":
        if "trywrlock" in source_snippet and "wrlock" in target_snippet:
            return "accepted", "failed try-lock unknown boundary verified"
        return "rejected", "try-lock unknown endpoints are not trywrlock/wrlock"

    if condition == "distinct worker callbacks may overlap outside the queue lock":
        if "work" in source_snippet and "work" in target_snippet:
            return "accepted", "worker callback overlap boundary verified"
        return "needs_review", "worker overlap endpoints are not clear callbacks"

    if (
        (condition.startswith("atomic relaxed ") and "no acquire/release" in condition)
        or condition == "relaxed read does not establish publication"
    ):
        if (
            has_token(source_snippet, ATOMIC_TOKENS)
            and same_file
            and "async" in str(source.get("function", "")).lower()
            and "async" in str(target.get("function", "")).lower()
        ):
            return "accepted", "same async path has a specific relaxed-atomic unknown boundary"
        return "rejected", "relaxed atomic boundary lacks a shared async/resource witness"

    if "notification does not identify" in condition:
        return "rejected", "generic notification/callback pair has no shared resource witness"

    return "needs_review", f"no independent rule for condition: {condition or '<empty>'}"


def write_csv(path: Path, items: list[dict[str, Any]]) -> None:
    fields = [
        "id",
        "decision",
        "reason",
        "source_file",
        "source_function",
        "source_line",
        "target_file",
        "target_function",
        "target_line",
        "relation_type",
        "order",
        "condition",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(items)


def markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    gate = report.get("gate") or {}
    lines = [
        "# Libuv synchronization closed-world audit",
        "",
        "This is an independent source-rule audit of predictions originating in "
        "the selected files. It is not a human-confirmed expansion of Gold.",
        "",
        "## Scope",
        "",
        f"- Repository: `{report['scope']['repository']}`",
        f"- Commit: `{report['scope']['commit']}`",
        f"- Build profile: `{report['scope']['build_profile']}`",
        f"- Source files: {len(report['scope']['source_files'])}",
        f"- In-scope predictions: {summary['predictions']}",
        f"- Audit gate: {gate.get('status', 'not_configured')}",
        "",
        "## Gold regression",
        "",
        f"- TP: {summary['gold_tp']}",
        f"- FP: {summary['gold_fp']}",
        f"- FN: {summary['gold_fn']}",
        "",
        "## Prediction-side audit",
        "",
        "| Decision | Count | Share |",
        "|---|---:|---:|",
    ]
    for decision in ("accepted", "rejected", "needs_review"):
        count = summary["decisions"].get(decision, 0)
        share = count / summary["predictions"] if summary["predictions"] else 0
        lines.append(f"| {decision} | {count} | {share:.2%} |")
    if summary.get("universe_total") is not None:
        universe_recall = summary.get("universe_recall")
        lines += [
            "",
            "## Candidate universe",
            "",
            f"- Total: {summary['universe_total']}",
            f"- Covered: {summary['universe_covered']}",
            f"- Missing: {summary['universe_missing']}",
            "- Recall: "
            + (
                "N/A"
                if universe_recall is None
                else f"{universe_recall:.2%}"
            ),
            f"- Same-function candidates: {summary['universe_same_function']}",
        ]
    lines += [
        "",
        f"- Rule-audit pass rate: {summary['rule_audit_pass_rate']:.2%}",
        f"- Decided share: {summary['decided_share']:.2%}",
        f"- Unresolved after source rules: {summary['needs_review']}",
        "",
        "## By source file",
        "",
        "| File | Accepted | Rejected | Needs review |",
        "|---|---:|---:|---:|",
    ]
    for file, counts in report["by_source_file"].items():
        lines.append(
            f"| `{file}` | {counts.get('accepted', 0)} | "
            f"{counts.get('rejected', 0)} | {counts.get('needs_review', 0)} |"
        )
    lines += [
        "",
        "## By relation type",
        "",
        "| Relation type | Accepted | Rejected | Needs review |",
        "|---|---:|---:|---:|",
    ]
    for relation_type, counts in report["by_relation_type"].items():
        lines.append(
            f"| `{relation_type}` | {counts.get('accepted', 0)} | "
            f"{counts.get('rejected', 0)} | {counts.get('needs_review', 0)} |"
        )
    lines += [
        "",
        "## Audit reasons",
        "",
        "| Reason | Count |",
        "|---|---:|",
    ]
    for reason, count in sorted(
        summary["reasons"].items(),
        key=lambda item: (-item[1], item[0]),
    ):
        lines.append(f"| {reason} | {count} |")
    lines += [
        "",
        "## Gate checks",
        "",
        "| Check | Status | Actual | Requirement |",
        "|---|---|---:|---:|",
    ]
    for check in gate.get("checks", []):
        lines.append(
            f"| {check['name']} | {check['status']} | "
            f"{check['actual']} | {check['requirement']} |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        "`accepted` means an independent local source rule supports the emitted "
        "claim. `rejected` means the emitted endpoint/direction/resource pairing "
        "contradicts the rule. `needs_review` means the available source facts "
        "do not prove or refute the claim. Only the frozen Gold result is a "
        "confirmed regression metric; the prediction-side audit must not be "
        "described as human-confirmed precision.",
        "",
    ]
    return "\n".join(lines)


def build_gate(
    summary: dict[str, Any],
    *,
    max_rejected: int | None,
    max_needs_review: int | None,
    min_accepted: int | None,
    min_gold_precision: float | None,
    min_gold_recall: float | None,
    min_universe_recall: float | None,
    require_zero_generic_notification: bool,
) -> dict[str, Any]:
    checks = []

    def add_check(name: str, actual: Any, requirement: str, passed: bool) -> None:
        checks.append(
            {
                "name": name,
                "status": "passed" if passed else "failed",
                "actual": actual,
                "requirement": requirement,
            }
        )

    rejected = int(summary["decisions"].get("rejected", 0))
    if max_rejected is not None:
        add_check(
            "rejected",
            rejected,
            f"<= {max_rejected}",
            rejected <= max_rejected,
        )

    needs_review = int(summary.get("needs_review", 0))
    if max_needs_review is not None:
        add_check(
            "needs_review",
            needs_review,
            f"<= {max_needs_review}",
            needs_review <= max_needs_review,
        )

    accepted = int(summary["decisions"].get("accepted", 0))
    if min_accepted is not None:
        add_check(
            "accepted",
            accepted,
            f">= {min_accepted}",
            accepted >= min_accepted,
        )

    gold_tp = int(summary["gold_tp"])
    gold_fp = int(summary["gold_fp"])
    gold_fn = int(summary["gold_fn"])
    gold_precision = gold_tp / (gold_tp + gold_fp) if gold_tp + gold_fp else 1.0
    gold_recall = gold_tp / (gold_tp + gold_fn) if gold_tp + gold_fn else 1.0
    if min_gold_precision is not None:
        add_check(
            "gold_precision",
            gold_precision,
            f">= {min_gold_precision}",
            gold_precision >= min_gold_precision,
        )
    if min_gold_recall is not None:
        add_check(
            "gold_recall",
            gold_recall,
            f">= {min_gold_recall}",
            gold_recall >= min_gold_recall,
        )

    universe_recall = summary.get("universe_recall")
    if min_universe_recall is not None:
        actual = universe_recall if universe_recall is not None else 0.0
        add_check(
            "universe_recall",
            actual,
            f">= {min_universe_recall}",
            actual >= min_universe_recall,
        )

    if require_zero_generic_notification:
        generic = int(
            summary["reasons"].get(
                "generic notification/callback pair has no shared resource witness",
                0,
            )
        )
        add_check(
            "generic_notification_pairs",
            generic,
            "== 0",
            generic == 0,
        )

    return {
        "status": (
            "passed"
            if all(item["status"] == "passed" for item in checks)
            else "failed"
        ),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--graph", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-rejected", type=int)
    parser.add_argument("--max-needs-review", type=int)
    parser.add_argument("--min-accepted", type=int)
    parser.add_argument("--min-gold-precision", type=float)
    parser.add_argument("--min-gold-recall", type=float)
    parser.add_argument("--min-universe-recall", type=float)
    parser.add_argument(
        "--require-zero-generic-notification",
        action="store_true",
    )
    args = parser.parse_args()

    predictions = read_json(args.predictions)
    truth = read_json(args.ground_truth)
    evidence = {item["id"]: item for item in predictions.get("evidence", [])}

    gold_cases = [
        case
        for case in truth["cases"]
        if (
            case["category"] == "sync_relation"
            and case["identity"][2] in SCOPE_FILES
        )
    ]
    positive_by_identity: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    negative_by_identity: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    for case in gold_cases:
        target = positive_by_identity if case["expected_exists"] else negative_by_identity
        target[tuple(case["identity"])].add(str(case["kind"]))
    positive_keys = {
        ground_truth_key(case)
        for case in gold_cases
        if case["expected_exists"]
    }
    negative_keys = {
        ground_truth_key(case)
        for case in gold_cases
        if not case["expected_exists"]
    }

    records: list[dict[str, Any]] = []
    predicted_positive_keys: set[tuple[str, tuple[Any, ...]]] = set()
    for item in predictions["items"]:
        source_file = str(item["source"].get("file") or "").replace("\\", "/")
        if source_file not in SCOPE_FILES:
            continue
        key = identity_key(item)
        predicted_positive_keys.add(key)
        identity = key[1]
        if key in positive_keys:
            gold_state = "positive"
        elif key in negative_keys:
            gold_state = "negative"
        elif identity in positive_by_identity or identity in negative_by_identity:
            gold_state = "wrong_kind"
        else:
            gold_state = "none"

        evidence_anchors = {
            evidence_anchor(evidence[evidence_id])
            for evidence_id in item.get("evidence_ids", [])
            if evidence_id in evidence
        }
        source_ok, source_reason = validate_endpoint(
            item["source"],
            evidence_anchors,
            args.source_root,
        )
        target_ok, target_reason = validate_endpoint(
            item["target"],
            evidence_anchors,
            args.source_root,
        )
        evidence_ok = source_ok and target_ok
        evidence_reason = source_reason if not source_ok else target_reason
        decision, reason = classify_prediction(
            item,
            evidence_ok=evidence_ok,
            evidence_reason=evidence_reason,
            gold_state=gold_state,
        )
        records.append(
            {
                "id": item["id"],
                "decision": decision,
                "reason": reason,
                "source_file": source_file,
                "source_function": item["source"].get("function", ""),
                "source_line": item["source"].get("line"),
                "target_file": item["target"].get("file", ""),
                "target_function": item["target"].get("function", ""),
                "target_line": item["target"].get("line"),
                "relation_type": item.get("relation_type", ""),
                "order": item.get("order", ""),
                "condition": item.get("condition", ""),
                "gold_state": gold_state,
                "evidence_ok": evidence_ok,
            }
        )

    gold_tp = len(positive_keys & predicted_positive_keys)
    gold_fp = len(predicted_positive_keys & negative_keys)
    gold_fn = len(positive_keys - predicted_positive_keys)
    decision_counts = Counter(record["decision"] for record in records)
    reason_counts = Counter(record["reason"] for record in records)
    relation_type_counts = Counter(record["relation_type"] for record in records)
    by_source_file: dict[str, dict[str, int]] = {}
    for file in SCOPE_FILES:
        by_source_file[file] = dict(
            Counter(
                record["decision"]
                for record in records
                if record["source_file"] == file
            )
        )
    relation_types = sorted({record["relation_type"] for record in records})
    by_relation_type = {
        relation_type: dict(
            Counter(
                record["decision"]
                for record in records
                if record["relation_type"] == relation_type
            )
        )
        for relation_type in relation_types
    }

    accepted = decision_counts.get("accepted", 0)
    rejected = decision_counts.get("rejected", 0)
    needs_review = decision_counts.get("needs_review", 0)
    decided = accepted + rejected
    gold_precision = gold_tp / (gold_tp + gold_fp) if gold_tp + gold_fp else 1.0
    gold_recall = gold_tp / (gold_tp + gold_fn) if gold_tp + gold_fn else 1.0
    universe_candidates: list[dict[str, Any]] = []
    universe_evaluation: dict[str, Any] = {}
    universe_info: dict[str, Any] = {}
    if args.graph is not None:
        graph = read_json(args.graph)
        universe_candidates, universe_info = build_generated_universe(
            graph,
            args.source_root,
            gold_cases,
        )
        universe_evaluation = evaluate_universe(universe_candidates, records)
    summary = {
        "predictions": len(records),
        "gold_positive": len(positive_keys),
        "gold_negative": len(negative_keys),
        "gold_tp": gold_tp,
        "gold_fp": gold_fp,
        "gold_fn": gold_fn,
        "gold_precision": gold_precision,
        "gold_recall": gold_recall,
        "decisions": dict(decision_counts),
        "reasons": dict(reason_counts),
        "relation_types": dict(relation_type_counts),
        "needs_review": needs_review,
        "rule_audit_pass_rate": accepted / decided if decided else 0.0,
        "rejection_rate": rejected / len(records) if records else 0.0,
        "decided_share": decided / len(records) if records else 0.0,
        "universe_total": (
            universe_evaluation.get("total") if args.graph is not None else None
        ),
        "universe_covered": universe_evaluation.get("covered"),
        "universe_missing": universe_evaluation.get("missing"),
        "universe_recall": universe_evaluation.get("recall"),
        "universe_same_function": universe_info.get("same_function"),
    }
    gate = build_gate(
        summary,
        max_rejected=args.max_rejected,
        max_needs_review=args.max_needs_review,
        min_accepted=args.min_accepted,
        min_gold_precision=args.min_gold_precision,
        min_gold_recall=args.min_gold_recall,
        min_universe_recall=args.min_universe_recall,
        require_zero_generic_notification=args.require_zero_generic_notification,
    )
    summary["gate_status"] = gate["status"]
    report = {
        "schema_version": "libuv-sync-closed-world-audit-v1",
        "generated_at": predictions.get("generated_at")
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "scope": {
            "repository": truth["repository"],
            "commit": truth["commit"],
            "build_profile": predictions.get("source", {}).get("build_profile", ""),
            "source_files": list(SCOPE_FILES),
            "selection": (
                "all sync predictions whose source endpoint is in the selected files"
            ),
            "review_status": "machine_source_rule_audit",
            "hashes": {
                "predictions_sha256": sha256(args.predictions),
                "ground_truth_sha256": sha256(args.ground_truth),
                "graph_sha256": sha256(args.graph) if args.graph is not None else "",
            },
        },
        "summary": summary,
        "gate": gate,
        "by_source_file": by_source_file,
        "by_relation_type": by_relation_type,
        "items": records,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    write_json(args.output / "audit.json", report)
    write_json(args.output / "gate.json", gate)
    if args.graph is not None:
        write_json(
            args.output / "universe.json",
            {
                "schema_version": "libuv-sync-candidate-universe-v1",
                "selection_id": "libuv-sync-closed-world-v1",
                "graph_sha256": sha256(args.graph),
                "summary": {
                    **universe_info,
                    "covered": universe_evaluation.get("covered"),
                    "missing": universe_evaluation.get("missing"),
                    "recall": universe_evaluation.get("recall"),
                },
                "candidates": universe_candidates,
                "missing_candidates": universe_evaluation.get(
                    "missing_candidates",
                    [],
                ),
            },
        )
    write_json(
        args.output / "selection.json",
        {
            "schema_version": "libuv-sync-closed-world-selection-v1",
            "selection_id": "libuv-sync-closed-world-v1",
            "repository": truth["repository"],
            "commit": truth["commit"],
            "category": "sync_relation",
            "source_files": list(SCOPE_FILES),
            "prediction_selection": (
                "all predictions whose source.file is listed in source_files"
            ),
            "gold": str(args.ground_truth),
            "predictions": str(args.predictions),
            "predictions_sha256": sha256(args.predictions),
            "ground_truth_sha256": sha256(args.ground_truth),
            "graph_sha256": sha256(args.graph) if args.graph is not None else "",
            "review_status": "machine_source_rule_audit",
        },
    )
    write_csv(args.output / "review.csv", records)
    (args.output / "report.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if gate["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
