import tempfile
import unittest
from pathlib import Path

from clang_pipeline.repo_agent import run_repo_analysis


class SnapshotIdentityTests(unittest.TestCase):
    def test_snapshot_binding_requires_safe_archive_mode(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "safe_mode"):
                run_repo_analysis(root, output_dir=Path(root) / "output", snapshot_identity={
                    "repository_url": "https://github.com/org/repo", "commit": "a" * 40})

    def test_snapshot_binding_rejects_mutable_commit(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "full commit"):
                run_repo_analysis(root, output_dir=Path(root) / "output", safe_mode=True, snapshot_identity={
                    "repository_url": "https://github.com/org/repo", "commit": "main"})
