from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clang_pipeline.agent import AGENT_TOOLS, AgentContext, execute_tool
from clang_pipeline.tool_trace import ToolTraceRecorder


class AgentToolContractTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Path:
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "external").mkdir()
        (workspace / "resource.json").write_text(
            json.dumps(
                {
                    "status": "succeeded",
                    "items": [
                        {
                            "id": "resource-1",
                            "operations": [{"function": "load"}],
                        }
                    ],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        (workspace / "external" / "resource-flow.json").write_text(
            (workspace / "resource.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (workspace / "external" / "sync-relations.json").write_text(
            json.dumps(
                {
                    "status": "succeeded",
                    "items": [
                        {
                            "source": {"function": "load"},
                            "target": {"function": "store"},
                        }
                    ],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        (workspace / "external" / "race-analysis.json").write_text(
            json.dumps(
                {
                    "status": "succeeded",
                    "label": "unknown",
                    "reason": "insufficient evidence",
                    "items": [],
                    "warnings": [],
                }
            ),
            encoding="utf-8",
        )
        (workspace / "visualization.html").write_text("<html></html>", encoding="utf-8")
        return workspace

    def test_required_tool_names_and_schemas_exist(self) -> None:
        schemas = {
            item["function"]["name"]: item["function"]["parameters"]
            for item in AGENT_TOOLS
        }
        required = {
            "analyze_repo",
            "get_resource_flow",
            "get_sync_relations",
            "get_races",
            "get_visualization",
        }
        self.assertTrue(required <= set(schemas))
        self.assertEqual(
            {"repository_url", "output_dir"},
            set(schemas["analyze_repo"]["required"]),
        )

    def test_tool_calls_have_persistent_trace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = self._workspace(Path(temp_dir))
            trace = ToolTraceRecorder(
                workspace / "trace" / "tool-trace.jsonl",
                run_id="contract-test",
                metadata={"test": True},
            )
            ctx = AgentContext(workspace, run_id="contract-test", trace=trace)
            resource = json.loads(
                execute_tool(ctx, "get_resource_flow", {"function": "load"})
            )
            sync = json.loads(execute_tool(ctx, "get_sync_relations", {}))
            race = json.loads(execute_tool(ctx, "get_races", {}))
            visualization = json.loads(execute_tool(ctx, "get_visualization", {}))
            self.assertTrue(resource["items"])
            self.assertTrue(sync["items"])
            self.assertEqual("unknown", race["label"])
            self.assertIn("visualization.html", visualization["path"])
            summary = trace.finish("succeeded")
            rows = [
                json.loads(line)
                for line in (workspace / "trace" / "tool-trace.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(4, len(rows))
            for row in rows:
                self.assertIn("tool", row)
                self.assertIn("arguments", row)
                self.assertIn("status", row)
                self.assertIn("duration_ms", row)
                self.assertIn("result", row)
            self.assertIn("summary_path", summary)

    def test_analyze_repo_tool_is_routed_and_traced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = self._workspace(Path(temp_dir))
            trace = ToolTraceRecorder(
                workspace / "trace" / "tool-trace.jsonl",
                run_id="analyze-contract",
            )
            ctx = AgentContext(workspace, run_id="analyze-contract", trace=trace)
            with patch(
                "clang_pipeline.repo_agent.run_repo_analysis",
                return_value={"status": "succeeded", "output_dir": "/tmp/out"},
            ) as runner:
                result = json.loads(
                    execute_tool(
                        ctx,
                        "analyze_repo",
                        {
                            "repository_url": "https://example.com/repo.git",
                            "output_dir": "/tmp/out",
                        },
                    )
                )
            self.assertEqual("succeeded", result["status"])
            runner.assert_called_once()
            self.assertEqual(
                "analyze-contract",
                runner.call_args.kwargs.get("trace").run_id,
            )
            rows = [
                json.loads(line)
                for line in (workspace / "trace" / "tool-trace.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual("analyze_repo", rows[-1]["tool"])

    def test_missing_artifact_is_recorded_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            trace = ToolTraceRecorder(workspace / "trace.jsonl", run_id="missing")
            ctx = AgentContext(workspace, run_id="missing", trace=trace)
            result = json.loads(execute_tool(ctx, "get_races", {}))
            self.assertIn("error", result)
            rows = [
                json.loads(line)
                for line in (workspace / "trace.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual("error", rows[0]["status"])


if __name__ == "__main__":
    unittest.main()
