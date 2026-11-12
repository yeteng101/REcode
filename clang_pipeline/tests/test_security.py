from __future__ import annotations

import io
import json
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from clang_pipeline.agent import AgentContext, AgentToolError, SYSTEM_PROMPT
from clang_pipeline.execution import CancellationToken
from clang_pipeline.sandbox import DockerSandboxRunner, SandboxConfig, SandboxUnavailable
from clang_pipeline.security import SecurityError, extract_regular_tar, github_archive_url, public_host
from clang_pipeline.tool_trace import ToolTraceRecorder, _sanitize

IMAGE = 'recode/sandbox@sha256:' + 'a' * 64


def archive(entries):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as bundle:
        for name, data, kind in entries:
            entry = tarfile.TarInfo(name)
            if kind == 'link':
                entry.type = tarfile.SYMTYPE
                entry.linkname = '/etc/passwd'
            else:
                entry.size = len(data)
            bundle.addfile(entry, io.BytesIO(data) if kind != 'link' else None)
    stream.seek(0)
    return stream


class SecurityTests(unittest.TestCase):
    def test_sandbox_runner_normalizes_commit_before_submission_validation(self):
        runner = DockerSandboxRunner(SandboxConfig(IMAGE))
        with patch.object(runner, 'validate_submission', side_effect=ValueError('stop before execution')) as validate:
            with self.assertRaisesRegex(ValueError, 'stop before execution'):
                runner('https://github.com/a/b', output_dir='unused', cancel_token=CancellationToken(),
                       trace=None, max_repository_files=10, max_repository_bytes=1000, commit='A' * 40)
        self.assertEqual('a' * 40, validate.call_args.args[1]['commit'])

    def test_uppercase_commit_archive_uses_canonical_lowercase(self):
        commit = 'ABCDEF0123456789' * 2 + 'ABCDEF01'
        self.assertEqual(
            'https://codeload.github.com/a/repo/tar.gz/' + commit.lower(),
            github_archive_url('https://github.com/a/repo.git', commit=commit))

    def test_github_only_and_ref_validation(self):
        self.assertEqual(github_archive_url('https://github.com/a/repo.git', 'feature/test'),
                         'https://codeload.github.com/a/repo/tar.gz/feature%2Ftest')
        for url in ('file:///etc/passwd', 'https://127.0.0.1/a/b', 'https://github.com.evil/a/b',
                    'https://user:pass@github.com/a/b', 'https://github.com:444/a/b',
                    'https://github.com/a/../b', 'https://github.com/a/b?token=secret',
                    'ssh://github.com/a/b', 'https://github.com/-/..'):
            with self.subTest(url=url), self.assertRaises((SecurityError, ValueError)):
                github_archive_url(url)
        for ref in ('--upload-pack=evil', '../bad', 'x;curl evil', '/secret'):
            with self.assertRaises(SecurityError):
                github_archive_url('https://github.com/a/b', branch=ref)
        with self.assertRaises(SecurityError):
            github_archive_url('https://github.com/a/b', commit='short')

    def test_private_endpoint_dns_is_denied(self):
        with patch('socket.getaddrinfo', return_value=[(None,None,None,None,('127.0.0.1',443))]):
            with self.assertRaises(SecurityError):
                public_host('codeload.github.com')

    def test_archive_escape_links_and_expansion_limits(self):
        for name, kind in (('../secret','file'), ('/etc/passwd','file'), ('x\\secret','file'),
                           ('C:/secret','file'), ('repo/link','link')):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                with self.assertRaises(SecurityError):
                    extract_regular_tar(archive([(name,b'data',kind)]), Path(root))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(SecurityError):
                extract_regular_tar(archive([('a',b'12345','file')]), Path(root), max_bytes=4)
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(SecurityError):
                extract_regular_tar(archive([('a',b'1','file'),('b',b'2','file')]), Path(root), max_files=1)

    def test_repository_instruction_and_secret_files_not_imported(self):
        entries = [(name, b'data','file') for name in ('repo/main.c','repo/AGENTS.md','repo/.env','repo/.ssh/key','repo/Gold/answers.json')]
        with tempfile.TemporaryDirectory() as root:
            extract_regular_tar(archive(entries), Path(root), strip_root=True)
            self.assertEqual([p.name for p in Path(root).rglob('*')], ['main.c'])

    def test_source_tool_cannot_escape_repository(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root) / 'repo'
            repo.mkdir()
            secret = Path(root) / 'secret.txt'
            secret.write_text('secret')
            ctx = AgentContext(repo, repo_root=repo)
            with self.assertRaises(AgentToolError):
                ctx.source_snippet(str(secret), 1)
            with self.assertRaises(AgentToolError):
                ctx.source_snippet('../secret.txt', 1)
        self.assertIn('不可信数据', SYSTEM_PROMPT)

    def test_safe_build_never_uses_repository_commands(self):
        from clang_pipeline.repo_agent import build_compile_commands
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            (repo / 'main.c').write_text('int main(){return 0;}')
            (repo / 'compile_commands.json').write_text('[{"command":"sh -c evil"}]')
            (repo / 'CMakeLists.txt').write_text('execute_process(COMMAND evil)')
            with patch('clang_pipeline.repo_agent._run_cmake_command', side_effect=AssertionError('must not run')):
                commands = build_compile_commands(repo, safe_mode=True)
            self.assertEqual(commands[0]['arguments'][0], 'clang')
            self.assertNotIn('command', commands[0])

    def test_container_policy_and_no_host_fallback(self):
        with self.assertRaises(ValueError):
            SandboxConfig('recode:latest')
        runner = DockerSandboxRunner(SandboxConfig(IMAGE))
        command = runner.container_command('test')
        for required in ('--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true','--user=65534:65534','--pids-limit','--memory-swap'):
            self.assertIn(required, command)
        self.assertFalse(any(item in {'-v','--volume','--mount','--privileged'} for item in command))
        with tempfile.TemporaryDirectory() as root:
            trace = ToolTraceRecorder(Path(root)/'trace.jsonl', run_id='test')
            with patch('shutil.which', return_value=None):
                with self.assertRaises(SandboxUnavailable):
                    runner('https://github.com/a/b', output_dir=root, cancel_token=CancellationToken(),
                           trace=trace, max_repository_files=10, max_repository_bytes=1000)
        with self.assertRaises(SecurityError):
            runner.validate_submission('https://github.com/a/b', {'log_paths':['/etc/passwd']})

    def test_command_cancellation_terminates_client_process(self):
        import sys,time
        runner = DockerSandboxRunner(SandboxConfig(IMAGE))
        token = CancellationToken()
        timer = threading.Timer(.1, token.cancel)
        timer.start()
        try:
            from clang_pipeline.execution import AnalysisCancelled
            with self.assertRaises(AnalysisCancelled):
                runner._command([sys.executable,'-c','import time;time.sleep(30)'],token,time.monotonic()+5)
        finally:
            timer.cancel()

    def test_skipped_entries_still_count_toward_expansion_quota(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(SecurityError):
                extract_regular_tar(archive([('repo/.env',b'x'*20,'file')]),Path(root),max_bytes=10,strip_root=True)

    def test_secret_redaction(self):
        data = _sanitize({'password':'secret', 'url':'https://u:secret@example.com/a',
                          'text':'Authorization: Bearer secret API_KEY=secret'})
        self.assertNotIn('secret', json.dumps(data))


if __name__ == '__main__':
    unittest.main()
