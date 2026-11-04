from __future__ import annotations

import argparse
import json
import hmac
import re
import os
import shutil
import signal
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from .agent import AGENT_TOOLS, AgentContext
from .agent_runner import ask_question
from .agent_jobs import AnalysisJobManager, IdempotencyConflict, JobError
from .object_store import FileObjectStore, S3ObjectStore
from .llm_bridge import LLMNotConfigured
from .tool_trace import ToolTraceRecorder
from .sandbox import DockerSandboxRunner, SandboxConfig
from .security import SecurityError


ROOT = Path(__file__).resolve().parents[1]
_JOB_MANAGER: AnalysisJobManager | None = None
_JOB_MANAGER_LOCK = threading.Lock()


def configured_job_manager(*, max_workers: int | None = None) -> AnalysisJobManager:
    root = Path(os.environ.get("CRA_JOB_ROOT", str(ROOT / "artifacts" / "agent-jobs")))
    backend = os.environ.get("CRA_OBJECT_BACKEND", "file")
    if backend == "file":
        store = FileObjectStore(os.environ.get("CRA_OBJECT_ROOT", str(root / "objects")))
    elif backend == "s3":
        store = S3ObjectStore(os.environ.get("CRA_S3_BUCKET", ""),
                              prefix=os.environ.get("CRA_S3_PREFIX", "recode"),
                              endpoint_url=os.environ.get("CRA_S3_ENDPOINT_URL"))
    else:
        raise ValueError("CRA_OBJECT_BACKEND must be file or s3")
    mode = os.environ.get("CRA_SANDBOX_MODE", "docker")
    if mode == "docker":
        runner = DockerSandboxRunner(SandboxConfig(
            image=os.environ.get("CRA_SANDBOX_IMAGE", ""),
            cpus=float(os.environ.get("CRA_SANDBOX_CPUS", "2")),
            memory_mb=int(os.environ.get("CRA_SANDBOX_MEMORY_MB", "2048")),
            disk_mb=int(os.environ.get("CRA_SANDBOX_DISK_MB", "1024")),
            pids=int(os.environ.get("CRA_SANDBOX_PIDS", "128")),
            timeout_seconds=float(os.environ.get("CRA_JOB_TIMEOUT_SECONDS", "1800"))))
    elif mode == "development" and os.environ.get("CRA_ENV") == "development":
        runner = None
    else:
        raise ValueError("host execution requires CRA_SANDBOX_MODE=development and CRA_ENV=development")
    return AnalysisJobManager(
        root, runner=runner,
        database_path=os.environ.get("CRA_JOB_DATABASE"), object_store=store,
        max_workers=int(os.environ.get("CRA_MAX_WORKERS", "2")) if max_workers is None else max_workers,
        allow_local_repositories=os.environ.get("CRA_ALLOW_LOCAL_REPOSITORIES", "0") == "1",
        job_timeout_seconds=float(os.environ.get("CRA_JOB_TIMEOUT_SECONDS", "1800")),
        max_repository_files=int(os.environ.get("CRA_MAX_REPOSITORY_FILES", "20000")),
        max_repository_bytes=int(os.environ.get("CRA_MAX_REPOSITORY_BYTES", str(1024 * 1024 * 1024))),
        max_attempts=int(os.environ.get("CRA_JOB_MAX_ATTEMPTS", "3")),
        retry_delay_seconds=float(os.environ.get("CRA_JOB_RETRY_DELAY_SECONDS", "5")),
        lease_seconds=float(os.environ.get("CRA_JOB_LEASE_SECONDS", "30")),
        retention_seconds=float(os.environ.get("CRA_JOB_RETENTION_SECONDS", str(7 * 86400))),
        cleanup_interval_seconds=float(os.environ.get("CRA_JOB_CLEANUP_INTERVAL_SECONDS", "60")),
    )


def job_manager() -> AnalysisJobManager:
    global _JOB_MANAGER
    with _JOB_MANAGER_LOCK:
        if _JOB_MANAGER is None:
            _JOB_MANAGER = configured_job_manager()
        return _JOB_MANAGER


def default_workspace() -> Path:
    candidates = [
        ROOT / "demo" / "libuv",
        ROOT / "demo" / "run_clang_demo",
    ]
    for candidate in candidates:
        if (candidate / "graph.json").exists():
            return candidate
    return candidates[0]


class AgentHandler(BaseHTTPRequestHandler):
    server_version = "CodeReverseAgentServer/0.4"

    def send_response(self, code, message=None):
        super().send_response(code, message)
        try:
            job_manager().observability.audit("http." + self.command.lower(), urlparse(self.path).path, str(code))
        except Exception:
            import logging
            logging.getLogger(__name__).exception("HTTP audit failed")

    def _admin_allowed(self):
        token = os.environ.get("CRA_ADMIN_TOKEN", "")
        if token:
            expected = "Bearer " + token
            return hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode())
        return self.client_address[0] in {"127.0.0.1", "::1"}

    def _write_bytes(self, status, payload, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._write_bytes(status, encoded, "application/json; charset=utf-8")

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 2_000_000:
            raise ValueError("请求体必须为 1 到 2000000 字节")
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/dashboard", "/dashboard.js", "/dashboard.css"}:
            name = {"/dashboard": "dashboard.html", "/dashboard.js": "dashboard.js", "/dashboard.css": "dashboard.css"}[path]
            content_type = "text/html" if name.endswith("html") else "text/javascript" if name.endswith("js") else "text/css"
            self._write_bytes(200, (Path(__file__).parent / "web" / name).read_bytes(), content_type + "; charset=utf-8")
            return
        if path in {"/metrics", "/api/v1/observability"} or path.endswith("/trace"):
            if not self._admin_allowed():
                self._write_json(401, {"error": {"code": "admin_access_required"}})
                return
            try:
                telemetry = job_manager().observability
                if path == "/metrics":
                    self._write_bytes(200, telemetry.prometheus().encode(), "text/plain; version=0.0.4")
                elif path == "/api/v1/observability":
                    self._write_json(200, telemetry.snapshot())
                else:
                    parts = path.strip("/").split("/")
                    if len(parts) != 6 or parts[:4] != ["api","v1","agent","analyze"]:
                        raise JobError("trace not found")
                    query = parse_qs(urlparse(self.path).query)
                    offset = max(0, int(query.get("offset", ["0"])[0]))
                    self._write_json(200, telemetry.trace(parts[4], offset=offset))
            except (JobError, ValueError) as exc:
                self._write_json(404, {"error": {"code": "not_found", "message": str(exc)}})
            return
        if path == "/health":
            self._write_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "code-reverse-agent",
                    "version": "0.4.0",
                    "sandbox": os.environ.get("CRA_SANDBOX_MODE", "docker"),
                },
            )
            return
        if path == "/api/v1/agent/tools":
            self._write_json(HTTPStatus.OK, {"tools": AGENT_TOOLS})
            return
        parts = path.strip("/").split("/")
        if len(parts) == 6 and parts[:4] == ["api", "v1", "agent", "analyze"] and parts[5] in {"result", "artifacts"}:
            try:
                stream, descriptor = job_manager().open_object(parts[4], parts[5])
            except (JobError, FileNotFoundError) as exc:
                self._write_json(HTTPStatus.NOT_FOUND, {"error": {"code": "not_found", "message": str(exc)}})
                return
            except Exception:
                self._write_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": {"code": "object_store_unavailable"}})
                return
            try:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json" if parts[5] == "result" else "application/gzip")
                self.send_header("Content-Length", str(descriptor["size"]))
                filename = "result.json" if parts[5] == "result" else "artifacts.tar.gz"
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.end_headers()
                shutil.copyfileobj(stream, self.wfile)
            finally:
                stream.close()
            return
        if path.startswith("/api/v1/agent/analyze/"):
            job_id = path.rsplit("/", 1)[-1]
            try:
                self._write_json(HTTPStatus.OK, job_manager().get(job_id))
            except JobError as exc:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"status": "failed", "error": {"code": "not_found", "message": str(exc)}},
                )
            return
        self._write_json(
            HTTPStatus.NOT_FOUND,
            {"status": "failed", "error": {"code": "not_found", "message": "接口不存在"}},
        )

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/api/v1/agent/analyze/") and path.endswith("/cancel"):
            job_id = path.split("/")[-2]
            try:
                self._write_json(HTTPStatus.ACCEPTED, job_manager().cancel(job_id))
            except JobError as exc:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"status": "failed", "error": {"code": "not_found", "message": str(exc)}},
                )
            return
        if path == "/api/v1/agent/analyze":
            try:
                body = self._read_json()
                if not isinstance(body, dict) or not str(body.get("repository_url", "")).strip():
                    raise ValueError("repository_url 不能为空")
                result = job_manager().submit(
                    str(body["repository_url"]),
                    branch=body.get("branch"),
                    commit=body.get("commit"),
                    build_profile=body.get("build_profile"),
                    entry_symbols=body.get("entry_symbols"),
                    log_paths=body.get("logs"),
                    optimized_ir=bool(body.get("optimized_ir")),
                    idempotency_key=self.headers.get("Idempotency-Key") or body.get("idempotency_key"),
                )
                self._write_json(HTTPStatus.ACCEPTED, result)
            except IdempotencyConflict as exc:
                self._write_json(HTTPStatus.CONFLICT, {"status": "failed", "error": {"code": "idempotency_conflict", "message": str(exc)}})
            except SecurityError as exc:
                self._write_json(403, {"status": "failed", "error": exc.to_dict()})
            except (ValueError, JobError) as exc:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"status": "failed", "error": {"code": "bad_request", "message": str(exc)}},
                )
            except Exception as exc:
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"status": "failed", "error": {"code": "internal_error", "message": str(exc)}},
                )
            return
        if path != "/api/v1/agent/ask":
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"status": "failed", "error": {"code": "not_found", "message": "接口不存在"}},
            )
            return
        if os.environ.get("CRA_SANDBOX_MODE", "docker") != "development" or os.environ.get("CRA_ENV") != "development":
            self._write_json(403, {"error": {"code": "host_ask_disabled", "message": "host workspace/model tools are disabled in production"}})
            return
        try:
            body = self._read_json()
            if not isinstance(body, dict) or not str(body.get("question", "")).strip():
                raise ValueError("question 不能为空")
            workspace = Path(body.get("workspace") or default_workspace()).resolve()
            run_id = str(body.get("run_id") or "run_libuv_1.50.0")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
                raise ValueError("invalid run_id")
            max_steps = int(body.get("max_steps", 6))
            backend = str(body.get("backend") or "auto")
            focus = body.get("focus") or []
            ctx = AgentContext(
                workspace,
                repo_root=ROOT,
                run_id=run_id,
                trace=ToolTraceRecorder(
                    Path(
                        os.environ.get(
                            "CRA_TRACE_ROOT",
                            str(ROOT / "artifacts" / "agent-traces"),
                        )
                    )
                    / f"{run_id}_{uuid.uuid4().hex[:12]}.jsonl",
                    run_id=run_id,
                    metadata={"workspace": str(workspace)},
                ),
            )
            with job_manager().observability.usage_scope():
                result = ask_question(
                    str(body["question"]).strip(), ctx, backend=backend,
                    max_steps=max_steps, model=body.get("model"),
                    focus=focus if isinstance(focus, list) else None,
                )
            result["tool_trace"] = str(ctx.trace.path) if ctx.trace is not None else ""
            if ctx.trace is not None:
                ctx.trace.finish(result.get("status", "succeeded"))
            self._write_json(HTTPStatus.OK, result)
        except LLMNotConfigured as exc:
            self._write_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "status": "failed",
                    "error": {"code": "llm_not_configured", "message": str(exc)},
                },
            )
        except Exception as exc:
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"status": "failed", "error": {"code": "bad_request", "message": str(exc)}},
            )

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # Structured request audit replaces raw URL logging (may contain secrets).


def main() -> None:
    parser = argparse.ArgumentParser(description="代码逆向 Agent 外部接口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    job_manager()  # Start recovery and workers even before the first request.
    server = ThreadingHTTPServer((args.host, args.port), AgentHandler)
    print(f"Agent Server: http://{args.host}:{args.port}")
    print("POST /api/v1/agent/ask  (自然语言提问)")
    print("POST /api/v1/agent/analyze  (仓库 URL -> 分析任务)")
    print("GET  /api/v1/agent/analyze/{job_id}")
    print("POST /api/v1/agent/analyze/{job_id}/cancel")
    print("GET  /api/v1/agent/tools")
    print("GET  /health")
    def stop_server(*_args):
        threading.Thread(target=server.shutdown, daemon=True).start()

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if _JOB_MANAGER is not None:
            _JOB_MANAGER.close()


if __name__ == "__main__":
    main()
