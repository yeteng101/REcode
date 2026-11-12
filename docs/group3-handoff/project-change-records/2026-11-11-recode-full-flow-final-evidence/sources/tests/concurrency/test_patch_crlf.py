"""CRLF source bytes remain intact while actual whitespace errors are rejected."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "v2-concurrency"))
from patching.pipeline import PatchPipelineError, capture_patch
from patching.plan import verification_plan_sha256
from patching.verifier import verify_patch


class PatchCrlfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "value.c").write_bytes(b"int value = 0;\r\n")
        self.git("add", "value.c")
        self.git("-c", "user.name=CRLF Test", "-c", "user.email=crlf@example.invalid", "commit", "-qm", "base")
        self.head = self.git("rev-parse", "HEAD").decode().strip()
        self.request = {"candidate_id": "case", "repository": {"head_sha": self.head},
            "workspace": {"path": str(self.repo), "expected_head_sha": self.head},
            "constraints": {"allowed_files": ["value.c"]}}
        self.report = {"schema_version": "lock-race-patch-worker-report/0.1", "candidate_id": "case",
            "status": "edited", "changed_files": ["value.c"], "summary": "Update value", "verification_status": "not_run"}

    def git(self, *arguments):
        return subprocess.check_output(["git", "-c", "core.autocrlf=false", "-C", str(self.repo), *arguments], stderr=subprocess.PIPE)

    def test_capture_accepts_crlf_without_rewriting_source_or_diff(self):
        data = b"int value = 1;\r\n"
        (self.repo / "value.c").write_bytes(data)
        result = capture_patch(self.request, self.report, self.root / "fix.patch")
        self.assertEqual(data, (self.repo / "value.c").read_bytes())
        self.assertEqual(self.git("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"),
                         Path(result["patch_path"]).read_bytes())
        self.assertEqual(hashlib.sha256(Path(result["patch_path"]).read_bytes()).hexdigest(), result["diff_sha256"])

    def test_capture_rejects_real_trailing_space_before_crlf(self):
        (self.repo / "value.c").write_bytes(b"int value = 1; \r\n")
        with self.assertRaisesRegex(PatchPipelineError, "diff --check"):
            capture_patch(self.request, self.report, self.root / "fix.patch")

    def test_verification_format_check_accepts_crlf_and_keeps_diff(self):
        (self.repo / "value.c").write_bytes(b"int value = 1;\r\n")
        captured = capture_patch(self.request, self.report, self.root / "fix.patch")
        plan = {"schema_version": "lock-race-verification-plan/0.1", "checks": [
            {"name": "format", "argv": ["git", "diff", "--check"], "timeout_seconds": 10}]}
        request = dict(self.request, verification_plan=plan, verification_plan_sha256=verification_plan_sha256(plan))
        result = verify_patch(request, captured)
        self.assertEqual("passed", result["status"])
        self.assertTrue(result["diff_unchanged"])
