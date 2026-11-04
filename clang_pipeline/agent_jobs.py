from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .clang_ast import utc_now
from .execution import AnalysisCancelled, AnalysisError, AnalysisTimedOut, CancellationToken
from .job_queue import IdempotencyConflict, JobError, JobQueue
from .object_store import FileObjectStore, ObjectStore
from .tool_trace import ToolTraceRecorder

AnalysisRunner = Callable[..., dict[str, Any]]
logger = logging.getLogger(__name__)


class AnalysisJobManager:
    """Durable at-least-once queue with renewable, fenced worker leases.

    max_workers=0 starts an API/maintenance process only. Other processes may
    run workers against the same local DB and persistent work/object volumes.
    """

    def __init__(self, output_root: str | Path, *, max_workers: int = 2,
                 allow_local_repositories: bool = True,
                 runner: AnalysisRunner | None = None,
                 job_timeout_seconds: float = 1800,
                 max_repository_files: int = 20000,
                 max_repository_bytes: int = 1024 * 1024 * 1024,
                 database_path: str | Path | None = None,
                 object_store: ObjectStore | None = None,
                 max_attempts: int = 3, retry_delay_seconds: float = 0.1,
                 lease_seconds: float = 30, retention_seconds: float = 7 * 86400,
                 poll_seconds: float = 0.05, cleanup_interval_seconds: float = 60) -> None:
        if not all(math.isfinite(value) for value in (job_timeout_seconds, lease_seconds,
                   retention_seconds, poll_seconds, cleanup_interval_seconds, retry_delay_seconds)):
            raise ValueError("timeouts and delays must be finite")
        if min(job_timeout_seconds, max_repository_files, max_repository_bytes,
               lease_seconds, retention_seconds, poll_seconds, cleanup_interval_seconds) <= 0:
            raise ValueError("timeouts, repository limits and retention must be positive")
        if max_workers < 0 or max_attempts < 1 or retry_delay_seconds < 0:
            raise ValueError("invalid worker, attempt or retry configuration")
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.allow_local_repositories = allow_local_repositories
        self.job_timeout_seconds = job_timeout_seconds
        self.max_repository_files = max_repository_files
        self.max_repository_bytes = max_repository_bytes
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.poll_seconds = poll_seconds
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self.queue = JobQueue(Path(database_path).resolve() if database_path else self.output_root / "jobs.sqlite3",
                              lease_seconds=lease_seconds, retention_seconds=retention_seconds)
        self.object_store = object_store or FileObjectStore(self.output_root / "objects")
        self._runner_is_default = runner is None
        self._runner = runner or self._default_runner
        self._owner = uuid.uuid4().hex
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._tokens: dict[str, CancellationToken] = {}
        self._closed = False
        self._threads = [threading.Thread(target=self._worker, name=f"cra-worker-{i}", daemon=True)
                         for i in range(max_workers)]
        self._threads.append(threading.Thread(target=self._maintenance, name="cra-job-cleanup", daemon=True))
        for thread in self._threads:
            thread.start()

    @staticmethod
    def _default_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        from .repo_agent import run_repo_analysis
        return run_repo_analysis(*args, **kwargs)

    def _validate_url(self, repository_url: str) -> None:
        parsed = urlparse(repository_url)
        local = parsed.scheme in {"", "file"} or (len(parsed.scheme) == 1 and os.name == "nt")
        if local and not self.allow_local_repositories:
            raise JobError("local repository paths are disabled")
        if not local and parsed.scheme not in {"http", "https", "ssh", "git"}:
            raise JobError(f"unsupported repository URL scheme: {parsed.scheme}")
        if not repository_url:
            raise JobError("repository_url cannot be empty")

    def submit(self, repository_url: str, *, branch: str | None = None,
               commit: str | None = None, build_profile: str | None = None,
               entry_symbols: list[str] | None = None, log_paths: list[str] | None = None,
               optimized_ir: bool = False, idempotency_key: str | None = None) -> dict[str, Any]:
        if self._stop.is_set():
            raise JobError("job manager is closed")
        repository_url = repository_url.strip()
        self._validate_url(repository_url)
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200):
            raise JobError("idempotency_key must be a nonempty string of at most 200 characters")
        for name, value in (("branch", branch), ("commit", commit), ("build_profile", build_profile)):
            if value is not None and not isinstance(value, str):
                raise JobError(f"{name} must be a string")
        for name, value in (("entry_symbols", entry_symbols), ("log_paths", log_paths)):
            if value is not None and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
                raise JobError(f"{name} must be a list of strings")
        options = dict(branch=branch, commit=commit, build_profile=build_profile,
                       entry_symbols=entry_symbols or [], log_paths=log_paths or [], optimized_ir=bool(optimized_ir))
        request = {"repository_url": repository_url, "options": options}
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        request["limits"] = dict(job_timeout_seconds=self.job_timeout_seconds,
                                 max_repository_files=self.max_repository_files,
                                 max_repository_bytes=self.max_repository_bytes,
                                 retry_delay_seconds=self.retry_delay_seconds)
        job_id = "job_" + uuid.uuid4().hex
        job_dir = self.output_root / job_id
        state = dict(job_id=job_id, status="queued", repository_url=repository_url,
                     job_dir=str(job_dir), output_dir=str(job_dir), created_at=utc_now(),
                     started_at=None, finished_at=None, expires_at=None,
                     error=None, cancel_requested=False, result=None, objects={},
                     attempts=0, max_attempts=self.max_attempts, tool_trace=None,
                     tool_trace_path=None, tool_trace_summary_path=None,
                     trace=[{"event": "accepted", "timestamp": utc_now(), "message": "analysis queued"}])
        accepted = self.queue.submit(state, request, fingerprint, idempotency_key)
        self._wake.set()
        return accepted

    def _worker(self):
        while not self._stop.is_set():
            try:
                claimed = self.queue.claim(self._owner)
                if claimed:
                    if self._stop.is_set():
                        self.queue.release(self._owner)
                        return
                    self._run(*claimed)
                    continue
            except Exception:
                logger.exception("job worker failed; lease will recover unfinished work")
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def _heartbeat(self, job_id, claim, token, done):
        interval = min(0.2, self.queue.lease_seconds / 3)
        while not done.wait(interval):
            try:
                state = self.queue.heartbeat(job_id, claim)
                if state is None or self._stop.is_set():
                    token.cancel("worker lease lost or manager stopped")
                    return
                if state["cancel_requested"]:
                    token.cancel(state.get("cancel_reason", "cancelled by caller"))
            except Exception:
                # Stop work on uncertain ownership. Publication is independently fenced.
                logger.exception("cannot renew job lease")
                token.cancel("cannot renew worker lease")
                return

    def _put_object(self, key, path):
        try:
            return self.object_store.put_file(key, path)
        except Exception as exc:
            raise OSError(f"object upload failed: {type(exc).__name__}: {exc}") from exc

    def _persist(self, state, claim, payload):
        output = Path(state["output_dir"])
        result_path = output / "result.json"
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        prefix = f"{state['job_id']}/{claim}"
        with tempfile.TemporaryDirectory(prefix="cra-package-") as temporary:
            bundle = Path(temporary) / "artifacts.tar.gz"
            with tarfile.open(bundle, "w:gz") as archive:
                # Do not follow repository-created links or archive build workspaces.
                for directory, dirs, files in os.walk(output, followlinks=False):
                    dirs[:] = sorted(d for d in dirs if d not in {"workspace", ".git", "checkout"} and not (Path(directory) / d).is_symlink())
                    for name in sorted(files):
                        path = Path(directory) / name
                        if not path.is_symlink() and path.is_file():
                            archive.add(path, arcname=path.relative_to(output).as_posix(), recursive=False)
            objects = {"artifacts": self._put_object(prefix + "/artifacts.tar.gz", bundle)}
        objects["result"] = self._put_object(prefix + "/result.json", result_path)
        return objects

    def _run(self, state, request, claim):
        job_id = state["job_id"]
        token = CancellationToken()
        with self._lock:
            self._tokens[job_id] = token
        done = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(job_id, claim, token, done), daemon=True)
        heartbeat.start()
        limits = request["limits"]
        timer = threading.Timer(limits["job_timeout_seconds"], token.cancel,
                                kwargs={"reason": f"analysis timed out after {limits['job_timeout_seconds']:g}s", "kind": "timeout"})
        timer.daemon = True
        timer.start()
        trace = None
        summary = None
        objects = {}
        result = None
        error = None
        status = "failed"
        try:
            trace = ToolTraceRecorder(state["tool_trace_path"], run_id=job_id,
                                      metadata={"attempt": state["attempts"], "repository_url": request["repository_url"]})
            token.check("job-start")
            args = dict(output_dir=Path(state["output_dir"]), **request["options"], trace=trace, cancel_token=token,
                        max_repository_files=limits["max_repository_files"], max_repository_bytes=limits["max_repository_bytes"])
            if self._runner_is_default:
                args.update(work_root=Path(state["output_dir"]) / "checkout", archive=False)
                result = self._runner(request["repository_url"], **args)
            else:
                with trace.span("analyze_repo", {"repository_url": request["repository_url"], "options": request["options"]}) as span:
                    result = self._runner(request["repository_url"], **args)
                    span.set_result(result)
            token.check("job-finalize")
            summary = trace.finish("succeeded")
            result.update(tool_trace=summary, tool_trace_path=str(trace.path), tool_trace_summary_path=str(trace.summary_path))
            objects = self._persist(state, claim, result)
            token.check("job-publish")
            status = "succeeded"
        except Exception as exc:
            if token.is_cancelled:
                try:
                    token.check("job")
                except AnalysisError as cancellation:
                    exc = cancellation
            status = "timed_out" if isinstance(exc, AnalysisTimedOut) else "cancelled" if isinstance(exc, AnalysisCancelled) else "failed"
            error = exc.to_dict() if isinstance(exc, AnalysisError) else {
                "type": type(exc).__name__, "code": "object_storage_error" if isinstance(exc, OSError) else "internal_error",
                "message": str(exc), "retryable": isinstance(exc, OSError)}
            result = None
            if trace is not None:
                try:
                    summary = trace.finish(status)
                    objects = self._persist(state, claim, {"status": status, "error": error, "tool_trace": summary})
                except Exception:
                    logger.exception("could not persist failure artifacts for %s", job_id)
        finally:
            try:
                if not self._stop.is_set():
                    self.queue.finish(job_id, claim, status, result=result, error=error, summary=summary, objects=objects,
                                      retry_delay=min(3600, limits["retry_delay_seconds"] * 2 ** min(state["attempts"] - 1, 16)))
            finally:
                timer.cancel()
                done.set()
                heartbeat.join(timeout=2)
                with self._lock:
                    self._tokens.pop(job_id, None)

    def get(self, job_id: str) -> dict[str, Any]:
        return self.queue.get(job_id)

    def cancel(self, job_id: str, reason: str = "cancelled by caller") -> dict[str, Any]:
        state = self.queue.cancel(job_id, reason)
        with self._lock:
            token = self._tokens.get(job_id)
            if token:
                token.cancel(reason)
        self._wake.set()
        return state

    def open_object(self, job_id: str, name: str):
        state = self.get(job_id)
        if state["status"] not in {"succeeded", "failed", "cancelled", "timed_out"} or name not in state["objects"]:
            raise JobError("result object is unavailable or expired")
        descriptor = state["objects"][name]
        return self.object_store.open(descriptor["key"]), descriptor

    def cleanup(self) -> int:
        def delete(state):
            job_id = state["job_id"]
            if not re.fullmatch(r"job_[0-9a-f]{32}", job_id):
                raise JobError("invalid job id in cleanup")
            job_dir = self.output_root / job_id
            if job_dir.is_symlink() or job_dir.resolve().parent != self.output_root:
                raise JobError("unsafe job directory in cleanup")
            self.object_store.delete_prefix(job_id)
            if job_dir.exists():
                try:
                    shutil.rmtree(job_dir)
                except FileNotFoundError:
                    pass
        return self.queue.expire(delete)

    def _maintenance(self):
        while not self._stop.is_set():
            try:
                self.cleanup()
            except Exception:
                logger.exception("job cleanup failed; will retry")
            self._stop.wait(self.cleanup_interval_seconds)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            tokens = list(self._tokens.values())
        self._wake.set()
        self.queue.release(self._owner)
        for token in tokens:
            token.cancel("job manager stopped; task returned to durable queue")
        deadline = time.monotonic() + 5
        for thread in self._threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))

    def __enter__(self) -> "AnalysisJobManager":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
