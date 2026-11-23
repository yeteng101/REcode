from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from argparse import Namespace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from clang_pipeline.cli import command_repo
from clang_pipeline.execution import (
    RepositoryCheckoutError,
    RepositoryNotFoundError,
    RepositoryTooLargeError,
)
from clang_pipeline.repo_agent import (
    build_compile_commands,
    _relativize_json_paths,
    checkout_repository,
    repository_identity,
    run_repo_analysis,
)


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "clang_pipeline" / "tests" / "fixtures"
SAMPLE = ROOT / "demo" / "sample"


class RepoAgentTests(unittest.TestCase):
    def test_repo_cli_validation_project_wiring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bundle"
            result = {
                "status": "succeeded",
                "repository": "fixture/repo",
                "commit": "f" * 40,
                "build_profile": "repository-auto",
                "source_dir": str(Path(tmp) / "source"),
                "output_dir": str(output),
                "pipeline_status": {},
            }
            args = Namespace(
                repo_url="https://example.com/fixture/repo.git",
                output=output,
                branch="",
                commit="",
                work_root="",
                entry=[],
                profile="",
                no_archive=True,
                validation_project="fixture",
                validation_name="Fixture",
                validation_repository="fixture/repo",
                validation_source_root="",
                validation_limit=50,
            )
            with patch("clang_pipeline.cli.run_repo_analysis", return_value=result) as analyze, \
                    patch("clang_pipeline.cli.scaffold_project", return_value={"status": "not_started"}) as scaffold, \
                    patch("clang_pipeline.cli.collect_candidates", return_value={"total_candidates": 0}) as collect, \
                    patch("sys.stdout", new=StringIO()):
                command_repo(args)
            analyze.assert_called_once()
            scaffold.assert_called_once_with(
                project_id="fixture",
                name="Fixture",
                repository="fixture/repo",
                repository_url="https://example.com/fixture/repo.git",
                commit="f" * 40,
                build_profile="repository-auto",
            )
            collect.assert_called_once_with(
                project_id="fixture",
                predictions_dir=output / "external",
                limit=50,
                source_root=result["source_dir"],
            )

    def test_repository_identity_keeps_owner_and_repo(self) -> None:
        self.assertEqual(
            repository_identity("https://github.com/libuv/libuv.git"),
            "libuv/libuv",
        )
        self.assertEqual(
            repository_identity("git@github.com:redis/redis.git"),
            "redis/redis",
        )
        self.assertEqual(
            repository_identity("/Users/andye/Documents/ChatGPT/8.18huawei/demo/sample"),
            "sample",
        )

    def test_remote_checkout_fetches_pinned_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()

            def git(*args: str) -> str:
                return subprocess.check_output(
                    [
                        "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "-c", "commit.gpgsign=false", *args,
                    ],
                    cwd=source,
                    text=True,
                ).strip()

            git("init", "--quiet", "--initial-branch=main")
            source_file = source / "main.c"
            source_file.write_text("int main(void) { return 0; }\n", encoding="utf-8")
            git("add", "main.c")
            git("commit", "--quiet", "-m", "Historical source")
            historical_sha = git("rev-parse", "HEAD")
            source_file.write_text("int main(void) { return 1; }\n", encoding="utf-8")
            git("commit", "--quiet", "-am", "Current source")
            tip_sha = git("rev-parse", "HEAD")

            remote = "https://example.invalid/fixture/source.git"
            with patch.dict(os.environ, {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{source.as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": remote,
            }):
                for branch in (None, "main"):
                    with self.subTest(branch=branch):
                        checkout, resolved, _ = checkout_repository(
                            remote, branch=branch, commit=historical_sha.upper(),
                            work_root=root / (branch or "default"),
                        )
                        self.assertEqual(historical_sha, resolved)
                        self.assertIn("return 0", (checkout / "main.c").read_text(encoding="utf-8"))
                        self.assertTrue((checkout / ".git" / "shallow").is_file())

                _, resolved, _ = checkout_repository(remote, work_root=root / "tip")
                self.assertEqual(tip_sha, resolved)

                failed_root = root / "missing"
                missing_sha = "f" * 40
                with self.assertRaises(RepositoryCheckoutError) as error:
                    checkout_repository(remote, commit=missing_sha, work_root=failed_root)
                self.assertEqual(missing_sha, error.exception.details["commit"])
                self.assertEqual([], list(failed_root.iterdir()))

    def test_relativizes_workspace_relative_source_paths(self) -> None:
        relative_file = (
            FIXTURE / "external_flow.c"
        ).relative_to(ROOT).as_posix()
        result = _relativize_json_paths(
            {"file": relative_file},
            FIXTURE,
        )
        self.assertEqual(result["file"], "external_flow.c")

    def test_file_url_produces_complete_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bundle"
            log_file = Path(tmp) / "runtime.log"
            log_file.write_text(
                "function=open_and_close event=started\n",
                encoding="utf-8",
            )
            result = run_repo_analysis(
                FIXTURE.resolve().as_uri(),
                output_dir=output,
                archive=True,
                log_paths=[log_file],
            )

            self.assertEqual("succeeded", result["status"])
            self.assertTrue(Path(result["source_dir"]).is_dir())
            self.assertTrue(result["build_profile"])
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["repository_url"], FIXTURE.resolve().as_uri())
            self.assertIn("visualization.html", manifest["outputs"].values())
            self.assertIn("external/call-chains.json", manifest["outputs"].values())
            self.assertIn("external/resource-flow.json", manifest["outputs"].values())
            self.assertIn("external/sync-relations.json", manifest["outputs"].values())
            self.assertIn("external/race-analysis.json", manifest["outputs"].values())
            self.assertIn("trace/tool-trace.jsonl", manifest["outputs"].values())
            self.assertTrue((output / "trace" / "tool-trace.jsonl").is_file())
            trace_rows = [
                json.loads(line)
                for line in (output / "trace" / "tool-trace.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertTrue(
                {"analyze_repo", "checkout_repository", "pipeline.01-index"}
                <= {row["tool"] for row in trace_rows}
            )
            for row in trace_rows:
                self.assertIn("arguments", row)
                self.assertIn("status", row)
                self.assertIn("duration_ms", row)
                self.assertIn("result", row)
            self.assertIn("external/knowledge-graph.json", manifest["outputs"].values())

            for path in (
                "visualization.html",
                "graph.json",
                "report.md",
                "external/call-chains.json",
                "external/resource-flow.json",
                "external/sync-relations.json",
                "external/race-analysis.json",
                "external/knowledge-graph.json",
                "knowledge/build-profile.json",
                "knowledge/log-facts.json",
                "knowledge/knowledge-graph.json",
            ):
                self.assertTrue((output / path).exists(), path)
            self.assertTrue(Path(result["archive"]).exists())

            calls = json.loads((output / "external/call-chains.json").read_text())
            resources = json.loads(
                (output / "external/resource-flow.json").read_text()
            )
            sync = json.loads(
                (output / "external/sync-relations.json").read_text()
            )
            races = json.loads(
                (output / "external/race-analysis.json").read_text()
            )
            knowledge = json.loads(
                (output / "external/knowledge-graph.json").read_text()
            )
            self.assertTrue(calls["items"])
            self.assertTrue(resources["items"])
            self.assertTrue(sync["items"])
            self.assertIn(races["label"], {"race", "no_race", "unknown"})
            self.assertGreater(knowledge["summary"]["log_events"], 0)
            for document in (calls, resources, sync):
                self.assertNotIn(
                    result["source_dir"],
                    json.dumps(document),
                )
            self.assertNotIn(
                result["source_dir"],
                (output / "visualization.html").read_text(encoding="utf-8"),
            )

    def test_cpp_repository_uses_the_same_url_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "cpp-bundle"
            result = run_repo_analysis(
                SAMPLE.resolve().as_uri(),
                output_dir=output,
                archive=False,
            )
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual("succeeded", result["status"])
            self.assertGreater(manifest["summary"]["nodes"], 0)
            self.assertGreater(manifest["summary"]["call_items"], 0)
            self.assertTrue((output / "visualization.html").exists())

    def test_missing_repository_returns_typed_error_and_error_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "missing-output"
            with self.assertRaises(RepositoryNotFoundError):
                run_repo_analysis(
                    (Path(tmp) / "does-not-exist").as_uri(),
                    output_dir=output,
                    archive=False,
                )
            self.assertTrue((output / "error.json").is_file())
            self.assertTrue((output / "trace" / "tool-trace-summary.json").is_file())

    def test_repository_file_limit_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "small"
            source.mkdir()
            (source / "main.c").write_text("int main(void) { return 0; }", encoding="utf-8")
            with self.assertRaises(RepositoryTooLargeError):
                run_repo_analysis(
                    source.as_uri(),
                    output_dir=Path(tmp) / "output",
                    archive=False,
                    max_repository_files=0,
                )

    def test_cmake_failure_falls_back_to_generic_compile_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / "CMakeLists.txt").write_text(
                "this is not valid cmake",
                encoding="utf-8",
            )
            (repo / "main.c").write_text("int main(void) { return 0; }", encoding="utf-8")
            commands = build_compile_commands(
                repo,
                log_path=Path(tmp) / "build.log",
            )
            self.assertEqual(1, len(commands))
            self.assertIn("main.c", commands[0]["file"])
            self.assertIn(
                "falling back to generic compile commands",
                (Path(tmp) / "build.log").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
