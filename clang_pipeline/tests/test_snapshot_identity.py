import tempfile
import json
import shutil
import tarfile
import io
import unittest
from pathlib import Path

from clang_pipeline.repo_agent import run_repo_analysis


class SnapshotIdentityTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('clang'), 'requires real Clang')
    def test_real_pipeline_binds_all_exports_and_normalizes_sha(self):
        from scripts.accept_production_entry import COMMIT, URL, validate_bundle
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            (source / 'test.c').write_text('int helper(void){return 1;} int main(void){return helper();}')
            result = run_repo_analysis(str(source), output_dir=root / 'output',
                work_root=root / 'checkout', archive=False, safe_mode=True,
                build_profile='safe-generic', snapshot_identity={
                    'repository_url': URL, 'commit': COMMIT.upper()})
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode='w:gz') as archive:
                for path in (root / 'output').iterdir():
                    if path.name != 'workspace':
                        archive.add(path, arcname='analysis/' + path.name)
            validate_bundle(raw.getvalue(), result)
            with self.assertRaisesRegex(AssertionError, 'snapshot mismatch'):
                validate_bundle(raw.getvalue(), {**result, 'commit': '0' * 40})

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
