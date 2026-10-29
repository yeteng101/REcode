from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .clang_ast import utc_now
from .execution import (
    AnalysisCancelled,
    AnalysisError,
    AnalysisTimedOut,
    CancellationToken,
)
from .tool_trace import ToolTraceRecorder


AnalysisRunner = Callable[..., dict[str, Any]]


class JobError(RuntimeError):
    pass


class AnalysisJobManager:
    """Small bounded job service for URL-to-analysis runs.

    The manager intentionally keeps the API contract independent from the
    analysis implementation so it can be backed by a queue in production.
    """

    def __init__(
        self,
        output_root: str | Path,
        *,
        max_workers: int = 2,
        allow_local_repositories: bool = True,
        runner: AnalysisRunner | None = None,
        job_timeout_seconds: float = 1800,
        max_repository_files: int = 20000,
        max_repository_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        if job_timeout_seconds <= 0:
            raise ValueError("job_timeout_seconds must be positive")
        if max_repository_files <= 0 or max_repository_bytes <= 0:
            raise ValueError("repository limits must be positive")
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.allow_local_repositories = allow_local_repositories
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, CancellationToken] = {}
        self._futures: dict[str, Any] = {}
        self._runner_is_default = runner is None
        self._runner = runner or self._default_runner
        self.job_timeout_seconds = job_timeout_seconds
        self.max_repository_files = max_repository_files
        self.max_repository_bytes = max_repository_bytes
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="cra-analysis",
        )

    @staticmethod
    def _default_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        from .repo_agent import run_repo_analysis

        return run_repo_analysis(*args, **kwargs)

    def _validate_url(self, repository_url: str) -> None:
        parsed = urlparse(repository_url)
        local = parsed.scheme in {"", "file"} or (
            len(parsed.scheme) == 1 and os.name == "nt"
        )
        if local and not self.allow_local_repositories:
            raise JobError("local repository paths are disabled")
        if parsed.scheme not in {"", "file", "http", "https", "ssh", "git"}:
            raise JobError(f"unsupported repository URL scheme: {parsed.scheme}")
        if not repository_url.strip():
            raise JobError("repository_url cannot be empty")

    def submit(
        self,
        repository_url: str,
        *,
        branch: str | None = None,
        commit: str | None = None,
        build_profile: str | None = None,
        entry_symbols: list[str] | None = None,
        log_paths: list[str] | None = None,
        optimized_ir: bool = False,
    ) -> dict[str, Any]:
        repository_url = repository_url.strip()
        self._validate_url(repository_url)
        job_id = "job_" + uuid.uuid4().hex[:16]
        output_dir = self.output_root / job_id
        state = {
            "job_id": job_id,
            "status": "queued",
            "repository_url": repository_url,
            "output_dir": str(output_dir),
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "cancel_requested": False,
            "tool_trace_path": str(output_dir / "trace" / "tool-trace.jsonl"),
            "tool_trace_summary_path": str(output_dir / "trace" / "tool-trace-summary.json"),
            "tool_trace": None,
            "trace": [
                {
                    "event": "accepted",
                    "timestamp": utc_now(),
                    "message": "analysis queued",
                }
            ],
            "result": None,
        }
        with self._lock:
            self._jobs[job_id] = state
            token = CancellationToken()
            self._tokens[job_id] = token
        future = self._executor.submit(
            self._run,
            job_id,
            repository_url,
            output_dir,
            {
                "branch": branch,
                "commit": commit,
                "build_profile": build_profile,
                "entry_symbols": entry_symbols,
                "log_paths": log_paths,
                "optimized_ir": optimized_ir,
            },
            token,
        )
        with self._lock:
            self._futures[job_id] = future
        return self.get(job_id)

    def _append_trace(self, job_id: str, event: str, message: str) -> None:
        with self._lock:
            state = self._jobs[job_id]
            state["trace"].append(
                {
                    "event": event,
                    "timestamp": utc_now(),
                    "message": message,
                }
            )

    def _run(
        self,
        job_id: str,
        repository_url: str,
        output_dir: Path,
        options: dict[str, Any],
        cancel_token: CancellationToken,
    ) -> None:
        with self._lock:
            state = self._jobs[job_id]
            state["status"] = "running"
            state["started_at"] = utc_now()
        self._append_trace(job_id, "started", "analysis started")
        trace = ToolTraceRecorder(
            output_dir / "trace" / "tool-trace.jsonl",
            run_id=job_id,
            metadata={
                "repository_url": repository_url,
                "options": {
                    key: value
                    for key, value in options.items()
                    if key not in {"log_paths"}
                },
            },
        )
        timer = threading.Timer(
            self.job_timeout_seconds,
            cancel_token.cancel,
            kwargs={
                "reason": f"analysis timed out after {self.job_timeout_seconds:g}s",
                "kind": "timeout",
            },
        )
        timer.daemon = True
        timer.start()
        try:
            runner_arguments = {
                "output_dir": output_dir,
                "branch": options.get("branch"),
                "commit": options.get("commit"),
                "build_profile": options.get("build_profile"),
                "entry_symbols": options.get("entry_symbols"),
                "log_paths": options.get("log_paths"),
                "optimized_ir": bool(options.get("optimized_ir")),
                "trace": trace,
                "cancel_token": cancel_token,
                "max_repository_files": self.max_repository_files,
                "max_repository_bytes": self.max_repository_bytes,
            }
            if self._runner_is_default:
                result = self._runner(repository_url, **runner_arguments)
            else:
                with trace.span(
                    "analyze_repo",
                    {
                        "repository_url": repository_url,
                        **{
                            key: value
                            for key, value in runner_arguments.items()
                            if key not in {"trace", "cancel_token"}
                        },
                    },
                ) as tool_span:
                    result = self._runner(repository_url, **runner_arguments)
                    tool_span.set_result(result)
            cancel_token.check("job-finalize")
            trace_summary = trace.finish("succeeded")
            result["tool_trace"] = trace_summary
            result["tool_trace_path"] = str(trace.path)
            result["tool_trace_summary_path"] = str(trace.summary_path)
            with self._lock:
                state = self._jobs[job_id]
                state["status"] = "succeeded"
                state["result"] = result
                state["tool_trace"] = trace_summary
                state["finished_at"] = utc_now()
            self._append_trace(job_id, "completed", "analysis completed")
        except Exception as exc:
            if cancel_token.is_cancelled:
                try:
                    cancel_token.check("job")
                except AnalysisError as cancel_exc:
                    exc = cancel_exc
            status = (
                "timed_out"
                if isinstance(exc, AnalysisTimedOut)
                else "cancelled"
                if isinstance(exc, AnalysisCancelled)
                else "failed"
            )
            trace_summary = trace.finish(status)
            error = (
                exc.to_dict()
                if isinstance(exc, AnalysisError)
                else {
                    "type": type(exc).__name__,
                    "code": "internal_error",
                    "message": str(exc),
                    "retryable": False,
                }
            )
            with self._lock:
                state = self._jobs[job_id]
                state["status"] = status
                state["error"] = error
                state["tool_trace"] = trace_summary
                state["finished_at"] = utc_now()
            self._append_trace(job_id, status, str(exc))
        finally:
            timer.cancel()

    def cancel(self, job_id: str, reason: str = "cancelled by caller") -> dict[str, Any]:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                raise JobError(f"job not found: {job_id}")
            if state["status"] in {"succeeded", "failed", "cancelled", "timed_out"}:
                return dict(state)
            token = self._tokens[job_id]
            future = self._futures.get(job_id)
            state["cancel_requested"] = True
            if state["status"] == "queued" and future is not None and future.cancel():
                state["status"] = "cancelled"
                state["finished_at"] = utc_now()
                token.cancel(reason)
                state["trace"].append(
                    {
                        "event": "cancelled",
                        "timestamp": utc_now(),
                        "message": reason,
                    }
                )
                return dict(state)
            state["status"] = "cancelling"
            token.cancel(reason)
        self._append_trace(job_id, "cancel_requested", reason)
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                raise JobError(f"job not found: {job_id}")
            return dict(state)

    def close(self) -> None:
        with self._lock:
            tokens = list(self._tokens.values())
        for token in tokens:
            token.cancel("job manager closed")
        self._executor.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "AnalysisJobManager":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
