"""Verify the configured REcode Docker service against a fixed public GitHub commit."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from coordinator import build_first_group_request
from recode_client import RecodeClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recode-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--repository", required=True, help="GitHub owner/repository")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--function", required=True)
    parser.add_argument("--file", required=True)
    args = parser.parse_args()
    run = args.output.resolve()
    run.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.recode_root.resolve()))
    from clang_pipeline import agent_server
    environment = {"CRA_JOB_ROOT": str(run / "jobs"), "CRA_OBJECT_ROOT": str(run / "objects"),
                   "CRA_SANDBOX_MODE": "docker", "CRA_SANDBOX_IMAGE": args.image,
                   "CRA_MAX_WORKERS": "1", "CRA_JOB_MAX_ATTEMPTS": "1", "CRA_JOB_TIMEOUT_SECONDS": "600"}
    with patch.dict(os.environ, environment):
        manager = agent_server.configured_job_manager()
    commands = []
    execute = manager._runner._command
    def record_command(argv, *arguments, **options):
        result = execute(argv, *arguments, **options)
        commands.append({"argv": argv, "exit_code": result[0],
                         "stdout_bytes": len(result[1]),
                         "stderr": result[2].decode("utf-8", errors="replace")[-3000:]})
        (run / "docker-commands.json").write_text(json.dumps(commands, indent=2), encoding="utf-8")
        return result
    manager._runner._command = record_command
    previous = agent_server._JOB_MANAGER
    agent_server._JOB_MANAGER = manager
    server = ThreadingHTTPServer(("127.0.0.1", 0), agent_server.AgentHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.perf_counter()
    review = {"repository": {"provider": "github", "name": args.repository, "commit_sha": args.commit,
                             "commit_role": "head", "build_profile": "safe-generic"},
              "review_target": {"id": "recode-service-acceptance"}, "diff": {"changed_files": [args.file]},
              "changed_functions": [{"name": args.function, "file": args.file, "start_line": 1}]}
    summary = {"status": "failed", "mode": "configured_production_docker_runner", "repository": args.repository,
               "commit": args.commit, "image": args.image}
    try:
        client = RecodeClient(f"http://127.0.0.1:{server.server_port}", cache_directory=run / "artifacts", job_timeout=660)
        responses = {}
        for action in ("get_call_chains", "get_resource_flows", "get_sync_flows"):
            response = client.query(build_first_group_request(review, action))
            (run / (action + ".json")).write_text(json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            responses[action] = {"status": response["status"], "findings": len(response["findings"]),
                                 "evidence": len(response["evidence"]), "resolved_targets": response["target_resolution"]["resolved_total"]}
        summary.update(status="passed", responses=responses, seconds=round(time.perf_counter() - started, 3),
                       fact_snapshots=len(client._bundles))
    except Exception as exc:
        summary["error"] = str(exc)
        raise
    finally:
        (run / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False))
        server.shutdown()
        server.server_close()
        thread.join()
        manager.close()
        agent_server._JOB_MANAGER = previous


if __name__ == "__main__":
    main()
