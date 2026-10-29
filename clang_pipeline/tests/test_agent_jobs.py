from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from clang_pipeline.agent_jobs import AnalysisJobManager, JobError
from clang_pipeline.execution import ClangStageError


class AgentJobTests(unittest.TestCase):
    def test_job_runs_and_returns_artifacts(self) -> None:
        def runner(url: str, *, output_dir: Path, **options):
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "manifest.json").write_text("{}", encoding="utf-8")
            return {
                "status": "succeeded",
                "repository_url": url,
                "output_dir": str(output_dir),
                "commit": options.get("commit"),
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            with AnalysisJobManager(
                Path(temp_dir),
                max_workers=1,
                runner=runner,
            ) as manager:
                accepted = manager.submit(
                    "https://example.com/org/repo.git",
                    commit="a" * 40,
                )
                self.assertIn(accepted["status"], {"queued", "running", "succeeded"})
                for _ in range(100):
                    state = manager.get(accepted["job_id"])
                    if state["status"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual("succeeded", state["status"])
                self.assertEqual("a" * 40, state["result"]["commit"])
                self.assertTrue(Path(state["result"]["output_dir"], "manifest.json").is_file())
                self.assertIn("completed", [item["event"] for item in state["trace"]])
                trace_path = Path(state["tool_trace_path"])
                self.assertTrue(trace_path.is_file())
                self.assertGreater(trace_path.stat().st_size, 0)
                rows = [
                    json.loads(line)
                    for line in trace_path.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual("analyze_repo", rows[-1]["tool"])
                self.assertIn("arguments", rows[-1])
                self.assertIn("duration_ms", rows[-1])
                self.assertIn("result", rows[-1])

    def test_local_paths_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with AnalysisJobManager(
                Path(temp_dir),
                allow_local_repositories=False,
            ) as manager:
                with self.assertRaises(JobError):
                    manager.submit(str(Path(temp_dir) / "repo"))

    def test_running_job_can_be_cancelled(self) -> None:
        started = threading.Event()

        def runner(url: str, *, cancel_token, **options):
            started.set()
            while not cancel_token.is_cancelled:
                time.sleep(0.01)
            cancel_token.check("test")

        with tempfile.TemporaryDirectory() as temp_dir:
            with AnalysisJobManager(
                Path(temp_dir),
                max_workers=1,
                runner=runner,
            ) as manager:
                job = manager.submit("https://example.com/org/repo.git")
                self.assertTrue(started.wait(2))
                manager.cancel(job["job_id"], reason="test cancellation")
                for _ in range(100):
                    state = manager.get(job["job_id"])
                    if state["status"] == "cancelled":
                        break
                    time.sleep(0.01)
                self.assertEqual("cancelled", state["status"])
                self.assertTrue(Path(state["tool_trace_path"]).is_file())

    def test_job_timeout_marks_timed_out(self) -> None:
        def runner(url: str, *, cancel_token, **options):
            while not cancel_token.is_cancelled:
                time.sleep(0.01)
            cancel_token.check("test timeout")

        with tempfile.TemporaryDirectory() as temp_dir:
            with AnalysisJobManager(
                Path(temp_dir),
                max_workers=1,
                runner=runner,
                job_timeout_seconds=0.05,
            ) as manager:
                job = manager.submit("https://example.com/org/repo.git")
                for _ in range(100):
                    state = manager.get(job["job_id"])
                    if state["status"] == "timed_out":
                        break
                    time.sleep(0.01)
                self.assertEqual("timed_out", state["status"])
                self.assertEqual("analysis_timeout", state["error"]["code"])

    def test_clang_failure_is_exposed_with_code(self) -> None:
        def runner(url: str, **options):
            raise ClangStageError(
                "AST parsing failed",
                stage="01-index",
                retryable=True,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            with AnalysisJobManager(
                Path(temp_dir),
                max_workers=1,
                runner=runner,
            ) as manager:
                job = manager.submit("https://example.com/org/repo.git")
                for _ in range(100):
                    state = manager.get(job["job_id"])
                    if state["status"] == "failed":
                        break
                    time.sleep(0.01)
                self.assertEqual("clang_stage_failed", state["error"]["code"])
                self.assertTrue(state["error"]["retryable"])


if __name__ == "__main__":
    unittest.main()
