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
from typing import Any


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
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
    summary = {
        "predictions": len(records),
        "gold_positive": len(positive_keys),
        "gold_negative": len(negative_keys),
        "gold_tp": gold_tp,
        "gold_fp": gold_fp,
        "gold_fn": gold_fn,
        "decisions": dict(decision_counts),
        "reasons": dict(reason_counts),
        "relation_types": dict(relation_type_counts),
        "needs_review": needs_review,
        "rule_audit_pass_rate": accepted / decided if decided else 0.0,
        "rejection_rate": rejected / len(records) if records else 0.0,
        "decided_share": decided / len(records) if records else 0.0,
    }
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
            },
        },
        "summary": summary,
        "by_source_file": by_source_file,
        "by_relation_type": by_relation_type,
        "items": records,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    write_json(args.output / "audit.json", report)
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
            "review_status": "machine_source_rule_audit",
        },
    )
    write_csv(args.output / "review.csv", records)
    (args.output / "report.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
