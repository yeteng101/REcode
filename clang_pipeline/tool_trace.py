from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .clang_ast import utc_now
from .execution import AnalysisError


TRACE_SCHEMA_VERSION = "tool-trace-v1"
MAX_ARGUMENT_TEXT = 2000
MAX_RESULT_PREVIEW = 1200
SECRET_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}


def _sanitize(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if depth > 8:
        return "<max-depth>"
    if any(secret in key.lower() for secret in SECRET_KEYS):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize(item, key=str(item_key), depth=depth + 1)
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, key=key, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        if len(value) <= MAX_ARGUMENT_TEXT:
            return value
        return value[:MAX_ARGUMENT_TEXT] + f"...<truncated {len(value) - MAX_ARGUMENT_TEXT} chars>"
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:MAX_ARGUMENT_TEXT]


def summarize_result(value: Any) -> dict[str, Any]:
    value = _sanitize(value)
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        payload = json.dumps(str(value), ensure_ascii=False)
    preview = payload[:MAX_RESULT_PREVIEW]
    if len(payload) > MAX_RESULT_PREVIEW:
        preview += "...<truncated>"
    return {
        "type": type(value).__name__,
        "bytes": len(payload.encode("utf-8")),
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "preview": preview,
        "truncated": len(payload) > MAX_RESULT_PREVIEW,
    }


class TraceSpan:
    def __init__(
        self,
        recorder: "ToolTraceRecorder",
        tool: str,
        arguments: dict[str, Any] | None,
    ) -> None:
        self.recorder = recorder
        self.tool = tool
        self.arguments = _sanitize(arguments or {})
        self.span_id = "span_" + uuid.uuid4().hex[:12]
        self.parent_span_id = recorder.current_span_id()
        self.started_at = utc_now()
        self.started_monotonic = time.monotonic()
        self.result: Any = None
        self.explicit_status: str | None = None
        self._entered = False

    def __enter__(self) -> "TraceSpan":
        self.recorder._push_span(self.span_id)
        self._entered = True
        return self

    def set_result(self, result: Any) -> None:
        self.result = result

    def set_status(self, status: str) -> None:
        self.explicit_status = status

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if not self._entered:
            return False
        self.recorder._pop_span(self.span_id)
        status = self.explicit_status or ("error" if exc is not None else "ok")
        error = None
        if exc is not None:
            if isinstance(exc, AnalysisError):
                error = exc.to_dict()
            else:
                error = {
                    "type": type(exc).__name__,
                    "code": "internal_error",
                    "message": str(exc),
                    "retryable": False,
                }
        duration_ms = round((time.monotonic() - self.started_monotonic) * 1000, 3)
        self.recorder.record(
            {
                "span_id": self.span_id,
                "parent_span_id": self.parent_span_id,
                "tool": self.tool,
                "arguments": self.arguments,
                "status": status,
                "started_at": self.started_at,
                "finished_at": utc_now(),
                "duration_ms": duration_ms,
                "result": summarize_result(self.result),
                "error": error,
            }
        )
        return False


class ToolTraceRecorder:
    """Writes durable JSONL traces for Agent tools and nested analysis steps."""

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path = self.path.with_name("tool-trace-summary.json")
        self.run_id = run_id
        self.trace_id = "trace_" + uuid.uuid4().hex[:16]
        self.metadata = _sanitize(metadata or {})
        self._lock = threading.RLock()
        self._local = threading.local()
        self._events: list[dict[str, Any]] = []
        self._status = "running"
        self.path.write_text("", encoding="utf-8")

    def current_span_id(self) -> str | None:
        stack = getattr(self._local, "stack", None) or []
        return stack[-1] if stack else None

    def _push_span(self, span_id: str) -> None:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = []
            self._local.stack = stack
        stack.append(span_id)

    def _pop_span(self, span_id: str) -> None:
        stack = getattr(self._local, "stack", None) or []
        if stack and stack[-1] == span_id:
            stack.pop()
        elif span_id in stack:
            stack.remove(span_id)

    def span(self, tool: str, arguments: dict[str, Any] | None = None) -> TraceSpan:
        return TraceSpan(self, tool, arguments)

    def record(self, event: dict[str, Any]) -> None:
        with self._lock:
            sequence = len(self._events) + 1
            record = {
                "schema_version": TRACE_SCHEMA_VERSION,
                "trace_id": self.trace_id,
                "run_id": self.run_id,
                "sequence": sequence,
                "timestamp": event.get("finished_at") or utc_now(),
                **event,
            }
            self._events.append(record)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._events]

    def finish(self, status: str) -> dict[str, Any]:
        with self._lock:
            self._status = status
            counts: dict[str, int] = {}
            for event in self._events:
                event_status = str(event.get("status") or "unknown")
                counts[event_status] = counts.get(event_status, 0) + 1
            summary = {
                "schema_version": TRACE_SCHEMA_VERSION,
                "trace_id": self.trace_id,
                "run_id": self.run_id,
                "status": status,
                "trace_path": str(self.path),
                "summary_path": str(self.summary_path),
                "event_count": len(self._events),
                "status_counts": counts,
                "metadata": self.metadata,
                "updated_at": utc_now(),
            }
            self.summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return summary

    @property
    def status(self) -> str:
        with self._lock:
            return self._status
