"""Local Git PR simulations for the shared acquisition boundary."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from acquire_github_pr import acquire
from pr_adapter import PRAdapterError, build_review_input
from review_input import require_review_input
from coordinator import build_first_group_request, route_specialties


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, encoding="utf-8").strip()


class PRAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Adapter Test")
        git(self.root, "config", "user.email", "adapter@example.invalid")
        (self.root / "review.c").write_text(
            "void change(void) {\n    return;\n}\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "base")
        self.base = git(self.root, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def review(self, source: str, routes: list[str] | None = None) -> dict:
        (self.root / "review.c").write_text(source, encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "PR head")
        head = git(self.root, "rev-parse", "HEAD")
        metadata = {"repository": {"name": "example/project"}, "base": {"sha": self.base},
                    "head": {"sha": head}, "number": 12,
                    "pr_url": "https://github.com/example/project/pull/12"}
        return build_review_input(metadata, self.root, "test", requested_routes=routes)

    def test_memory_pr(self) -> None:
        review = self.review("#include <stdlib.h>\nvoid change(void) {\n    void *buffer = malloc(8);\n    free(buffer);\n}\n")
        self.assertIs(require_review_input(review), review)
        self.assertEqual(review["changed_functions"][0]["name"], "change")
        self.assertEqual(route_specialties(review, {})["routes"], ["memory"])
        self.assertEqual(review["repository"]["commit_sha"], review["review_target"]["head_revision"])

    def test_concurrency_pr(self) -> None:
        review = self.review("#include <stdlib.h>\nvoid change(void) {\n    mutex_lock();\n    pthread_create();\n}\n")
        self.assertEqual(route_specialties(review, {})["routes"], ["concurrency"])

    def test_pointer_returning_thread_entry_is_located(self) -> None:
        review = self.review("void *worker(void *argument) {\n    return argument;\n}\n")
        self.assertEqual(["worker"], [f["name"] for f in review["changed_functions"]])
        self.assertEqual([], review["acquisition_coverage_gaps"])
        for action in ("get_call_chains", "get_resource_flows", "get_sync_flows"):
            request = build_first_group_request(review, action)
            self.assertEqual(self.base, request["repository"]["base_sha"])
            self.assertEqual(review["repository"]["commit_sha"], request["repository"]["head_sha"])

    def test_reference_returning_function_is_located(self) -> None:
        review = self.review("Widget &change(void) {\n    return shared;\n}\n")
        self.assertEqual(["change"], [f["name"] for f in review["changed_functions"]])

    def test_mixed_pr(self) -> None:
        review = self.review("#include <stdlib.h>\nvoid change(void) {\n    mutex_lock();\n    free(buffer);\n}\n")
        self.assertEqual(route_specialties(review, {})["routes"], ["memory", "concurrency"])

    def test_moved_head_rejected(self) -> None:
        self.review("#include <stdlib.h>\nvoid change(void) {\n    free(buffer);\n}\n")
        metadata = {"repository": {"name": "example/project"}, "base": {"sha": self.base},
                    "head": {"sha": self.base}, "number": 12,
                    "pr_url": "https://github.com/example/project/pull/12"}
        with self.assertRaisesRegex(PRAdapterError, "checkout HEAD differs"):
            build_review_input(metadata, self.root, "test")

    def test_unlocated_change_requires_verified_target(self) -> None:
        (self.root / "review.c").write_text(
            "int shared = 1;\nvoid change(void) {\n    return;\n}\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "global change")
        metadata = {"repository": {"name": "example/project"}, "base": {"sha": self.base},
                    "head": {"sha": git(self.root, "rev-parse", "HEAD")}, "number": 12,
                    "pr_url": "https://github.com/example/project/pull/12"}
        with self.assertRaisesRegex(PRAdapterError, "supply --changed-functions-json"):
            build_review_input(metadata, self.root, "test")
        explicit = [{"name": "change", "file": "review.c", "start_line": 2}]
        review = build_review_input(metadata, self.root, "test", explicit)
        self.assertTrue(review["acquisition_coverage_gaps"])

    def test_field_only_change_accepts_explicit_shared_object_target(self) -> None:
        (self.root / "review.c").write_text(
            "struct Shared { int counter; };\nvoid change(void) {\n    return;\n}\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "field change")
        metadata = {"repository": {"name": "example/project"}, "base": {"sha": self.base},
                    "head": {"sha": git(self.root, "rev-parse", "HEAD")}, "number": 12,
                    "pr_url": "https://github.com/example/project/pull/12"}
        targets = [{"target_id": "shared-counter", "type": "shared_object", "name": "Shared.counter",
                    "location": {"file": "review.c", "line": 1}, "role": "changed_shared_object"}]
        review = build_review_input(metadata, self.root, "test", explicit_targets=targets)
        self.assertEqual(review["changed_functions"], [])
        self.assertTrue(review["acquisition_coverage_gaps"])
        request = build_first_group_request(review, "get_sync_flows")
        self.assertEqual(request["targets"][0]["type"], "shared_object")
        self.assertEqual(request["repository"]["commit_sha"], metadata["head"]["sha"])

    def test_acquisition_emits_common_input(self) -> None:
        git(self.root, "branch", "main", self.base)
        review = self.review("void change(void) {\n    void *buffer = malloc(8);\n    free(buffer);\n}\n")
        head = review["repository"]["commit_sha"]
        git(self.root, "update-ref", "refs/pull/12/head", head)
        metadata = {"repository": {"name": "example/project", "clone_url": str(self.root)},
                    "base": {"sha": self.base, "ref": "main"},
                    "head": {"sha": head}, "number": 12,
                    "pr_url": "https://github.com/example/project/pull/12"}
        with patch("acquire_github_pr.fetch_pr_metadata", return_value=metadata):
            result = acquire(metadata["pr_url"], self.root / "mirror.git",
                             self.root / "workspace", "linux-test", token_env="")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["changed_functions"], 1)
        self.assertTrue(Path(result["review_input"]).is_file())


if __name__ == "__main__":
    unittest.main()
