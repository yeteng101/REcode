import json
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clang_pipeline.repo_agent import run_repo_analysis


class SnapshotIdentityTests(unittest.TestCase):
    def test_sandbox_entry_keeps_uppercase_request_canonical_in_output(self):
        from clang_pipeline import sandbox_entry
        request = {'repository_url': 'https://github.com/org/repo', 'commit': 'A' * 40,
                   'max_repository_files': 10, 'max_repository_bytes': 1000}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sandbox-result.json'
            with patch('sys.argv', ['sandbox_entry', 'analyze']), \
                 patch('sys.stdin', io.StringIO(json.dumps(request))), \
                 patch.object(sandbox_entry, 'Path', return_value=output), \
                 patch('clang_pipeline.repo_agent.run_repo_analysis', return_value={'status': 'succeeded', 'commit': 'a' * 40}) as analyze:
                sandbox_entry.main()
            self.assertEqual('a' * 40, analyze.call_args.kwargs['snapshot_identity']['commit'])
            self.assertEqual('a' * 40, json.loads(output.read_text(encoding='utf-8'))['commit'])

    @unittest.skipUnless(shutil.which('clang') and shutil.which('clang++'), 'real Clang compiler required')
    def test_uppercase_snapshot_is_canonical_in_result_graph_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'main.c').write_text('int main(void) { return 0; }\n', encoding='utf-8')
            result = run_repo_analysis(str(source), output_dir=root / 'output', archive=False, safe_mode=True,
                snapshot_identity={'repository_url': 'https://github.com/org/repo', 'commit': 'A' * 40})
            manifest = json.loads((root / 'output/manifest.json').read_text(encoding='utf-8'))
            graph = json.loads((root / 'output/graph.json').read_text(encoding='utf-8'))
            self.assertEqual('a' * 40, result['commit'])
            self.assertEqual('a' * 40, manifest['commit'])
            self.assertEqual('a' * 40, graph['meta']['commit'])

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
