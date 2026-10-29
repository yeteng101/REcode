from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from clang_pipeline.agent_jobs import AnalysisJobManager, JobError


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

    def test_local_paths_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with AnalysisJobManager(
                Path(temp_dir),
                allow_local_repositories=False,
            ) as manager:
                with self.assertRaises(JobError):
                    manager.submit(str(Path(temp_dir) / "repo"))


if __name__ == "__main__":
    unittest.main()
