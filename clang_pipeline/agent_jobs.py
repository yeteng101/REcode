from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .clang_ast import utc_now


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
    ) -> None:
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.allow_local_repositories = allow_local_repositories
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._runner = runner or self._default_runner
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
        self._executor.submit(
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
        )
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
    ) -> None:
        with self._lock:
            state = self._jobs[job_id]
            state["status"] = "running"
            state["started_at"] = utc_now()
        self._append_trace(job_id, "started", "analysis started")
        try:
            result = self._runner(
                repository_url,
                output_dir=output_dir,
                branch=options.get("branch"),
                commit=options.get("commit"),
                build_profile=options.get("build_profile"),
                entry_symbols=options.get("entry_symbols"),
                log_paths=options.get("log_paths"),
                optimized_ir=bool(options.get("optimized_ir")),
            )
            with self._lock:
                state = self._jobs[job_id]
                state["status"] = "succeeded"
                state["result"] = result
                state["finished_at"] = utc_now()
            self._append_trace(job_id, "completed", "analysis completed")
        except Exception as exc:
            with self._lock:
                state = self._jobs[job_id]
                state["status"] = "failed"
                state["error"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
                state["finished_at"] = utc_now()
            self._append_trace(job_id, "failed", str(exc))

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._jobs.get(job_id)
            if state is None:
                raise JobError(f"job not found: {job_id}")
            return dict(state)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "AnalysisJobManager":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
