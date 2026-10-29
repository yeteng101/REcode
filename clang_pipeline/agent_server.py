from __future__ import annotations

import argparse
import json
import os
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .agent import AGENT_TOOLS, AgentContext
from .agent_runner import ask_question
from .agent_jobs import AnalysisJobManager, JobError
from .llm_bridge import LLMNotConfigured
from .tool_trace import ToolTraceRecorder


ROOT = Path(__file__).resolve().parents[1]
_JOB_MANAGER: AnalysisJobManager | None = None


def job_manager() -> AnalysisJobManager:
    global _JOB_MANAGER
    if _JOB_MANAGER is None:
        root = Path(
            os.environ.get(
                "CRA_JOB_ROOT",
                str(ROOT / "artifacts" / "agent-jobs"),
            )
        )
        allow_local = os.environ.get("CRA_ALLOW_LOCAL_REPOSITORIES", "1") == "1"
        _JOB_MANAGER = AnalysisJobManager(
            root,
            max_workers=max(1, int(os.environ.get("CRA_MAX_WORKERS", "2"))),
            allow_local_repositories=allow_local,
            job_timeout_seconds=float(
                os.environ.get("CRA_JOB_TIMEOUT_SECONDS", "1800")
            ),
            max_repository_files=int(
                os.environ.get("CRA_MAX_REPOSITORY_FILES", "20000")
            ),
            max_repository_bytes=int(
                os.environ.get("CRA_MAX_REPOSITORY_BYTES", str(1024 * 1024 * 1024))
            ),
        )
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

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 2_000_000:
            raise ValueError("请求体必须为 1 到 2000000 字节")
        return json.loads(self.rfile.read(length))

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._write_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "code-reverse-agent",
                    "version": "0.4.0",
                    "job_root": str(job_manager().output_root),
                },
            )
            return
        if path == "/api/v1/agent/tools":
            self._write_json(HTTPStatus.OK, {"tools": AGENT_TOOLS})
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
                )
                self._write_json(HTTPStatus.ACCEPTED, result)
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
        try:
            body = self._read_json()
            if not isinstance(body, dict) or not str(body.get("question", "")).strip():
                raise ValueError("question 不能为空")
            workspace = Path(body.get("workspace") or default_workspace()).resolve()
            run_id = str(body.get("run_id") or "run_libuv_1.50.0")
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
            result = ask_question(
                str(body["question"]).strip(),
                ctx,
                backend=backend,
                max_steps=max_steps,
                model=body.get("model"),
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
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="代码逆向 Agent 外部接口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AgentHandler)
    print(f"Agent Server: http://{args.host}:{args.port}")
    print("POST /api/v1/agent/ask  (自然语言提问)")
    print("POST /api/v1/agent/analyze  (仓库 URL -> 分析任务)")
    print("GET  /api/v1/agent/analyze/{job_id}")
    print("POST /api/v1/agent/analyze/{job_id}/cancel")
    print("GET  /api/v1/agent/tools")
    print("GET  /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
