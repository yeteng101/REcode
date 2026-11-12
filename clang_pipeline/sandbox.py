from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path

from .execution import AnalysisError, RepositoryCheckoutError
from .security import SecurityError, extract_regular_tar, github_archive_url, normalize_commit_sha, public_host


class SandboxUnavailable(AnalysisError):
    code = 'sandbox_unavailable'


@dataclass(frozen=True)
class SandboxConfig:
    image: str
    cpus: float = 2
    memory_mb: int = 2048
    disk_mb: int = 1024
    pids: int = 128
    timeout_seconds: float = 1800
    max_download_bytes: int = 128 * 1024 * 1024

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9_./:-]+@sha256:[0-9a-f]{64}', self.image):
            raise ValueError('CRA_SANDBOX_IMAGE must be an immutable image@sha256:digest')
        if not all(math.isfinite(v) and v > 0 for v in (self.cpus, self.memory_mb, self.disk_mb, self.pids, self.timeout_seconds, self.max_download_bytes)):
            raise ValueError('sandbox limits must be finite and positive')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SecurityError('repository redirects are disabled')


class DockerSandboxRunner:
    """No host mounts, network, credentials, model tools or repository commands.

    The Docker daemon is trusted infrastructure. Containers require a Linux
    daemon supporting cgroups, tmpfs, seccomp and unprivileged users.
    """
    def __init__(self, config: SandboxConfig):
        self.config = config

    def validate_submission(self, url, options):
        github_archive_url(url, options.get('branch'), options.get('commit'))
        if options.get('log_paths') or options.get('build_profile') not in {None, 'safe-generic'}:
            raise SecurityError('sandbox forbids host logs and custom build profiles')

    def container_command(self, name):
        c = self.config
        return ['docker', 'create', '--pull=never', '--name', name,
                '--network=none', '--read-only', '--user=65534:65534',
                '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                '--cpus', str(c.cpus), '--memory', f'{c.memory_mb}m',
                '--memory-swap', f'{c.memory_mb}m', '--pids-limit', str(c.pids),
                '--ulimit', 'nofile=256:256', '--ulimit', 'core=0:0',
                '--tmpfs', f'/work:rw,noexec,nosuid,nodev,size={c.disk_mb}m,mode=1777',
                '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777',
                '--env', 'HOME=/tmp', '--env', 'PYTHONDONTWRITEBYTECODE=1',
                '--label', 'recode.sandbox=true', self.config.image,
                'python3', '-c', f'import time; time.sleep({c.timeout_seconds + 30:g})']

    def _command(self, args, token, deadline, *, incoming=None, outgoing=None, output_limit=1024 * 1024):
        with tempfile.TemporaryFile() as fallback_stdout, tempfile.TemporaryFile() as stderr:
            stdout = outgoing if outgoing is not None else fallback_stdout
            process = subprocess.Popen(args, stdin=incoming or subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, shell=False)
            try:
                while process.poll() is None:
                    token.check('sandbox')
                    if time.monotonic() >= deadline:
                        token.cancel('sandbox deadline exceeded', kind='timeout')
                        token.check('sandbox')
                    if stdout.tell() + stderr.tell() > output_limit:
                        raise SecurityError('sandbox command output quota exceeded')
                    time.sleep(.05)
                if stdout.tell() + stderr.tell() > output_limit:
                    raise SecurityError('sandbox command output quota exceeded')
                stdout.seek(0)
                stderr.seek(0)
                return process.returncode, stdout.read() if outgoing is None else b'', stderr.read()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)

    def __call__(self, repository_url, *, output_dir, cancel_token, trace,
                 max_repository_files, max_repository_bytes, **options):
        options = dict(options, commit=normalize_commit_sha(options.get('commit')))
        self.validate_submission(repository_url, options)
        if not shutil.which('docker'):
            raise SandboxUnavailable('Docker is required; host execution fallback is disabled')
        output_dir = Path(output_dir)
        archive_url = github_archive_url(repository_url, options.get('branch'), options.get('commit'))
        deadline = time.monotonic() + self.config.timeout_seconds
        import uuid
        name = 'recode-' + uuid.uuid4().hex
        created = False
        try:
            with tempfile.TemporaryFile() as repository:
                with trace.span('sandbox.fetch', {'host': 'codeload.github.com'}) as span:
                    public_host('codeload.github.com')
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                    total = 0
                    with opener.open(archive_url, timeout=20) as response:
                        while True:
                            cancel_token.check('sandbox-download')
                            if time.monotonic() >= deadline:
                                raise SecurityError('repository download deadline exceeded')
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > self.config.max_download_bytes:
                                raise SecurityError('repository download quota exceeded')
                            repository.write(chunk)
                    span.set_result({'download_bytes': total})
                repository.seek(0)
                code, _, error = self._command(self.container_command(name), cancel_token, deadline)
                if code:
                    raise SandboxUnavailable('cannot create sandbox: ' + error.decode(errors='replace')[:1000])
                created = True
                code, _, _ = self._command(['docker', 'start', name], cancel_token, deadline)
                if code:
                    raise SandboxUnavailable('cannot start sandbox')
                with trace.span('sandbox.import', {}) as span:
                    code, _, error = self._command(['docker', 'exec', '-i', name, 'python3', '-m',
                        'clang_pipeline.sandbox_entry', 'import', str(max_repository_files), str(max_repository_bytes)],
                        cancel_token, deadline, incoming=repository)
                    if code:
                        raise SecurityError('sandbox rejected archive: ' + error.decode(errors='replace')[:1000])
                    span.set_result({'status': 'imported'})
            request = dict(repository_url=repository_url, commit=options.get('commit'),
                           entry_symbols=options.get('entry_symbols'), optimized_ir=options.get('optimized_ir'),
                           max_repository_files=max_repository_files, max_repository_bytes=max_repository_bytes)
            with tempfile.TemporaryFile() as incoming, trace.span('sandbox.analyze', {}) as span:
                incoming.write(json.dumps(request).encode())
                incoming.seek(0)
                code, _, error = self._command(['docker', 'exec', '-i', name, 'python3', '-m',
                    'clang_pipeline.sandbox_entry', 'analyze'], cancel_token, deadline,
                    incoming=incoming, output_limit=8 * 1024 * 1024)
                # Export only after the analysis process exits. No host path is mounted.
                # Stream the bounded tar transport to disk, not host RAM.
                with tempfile.TemporaryFile() as exported_archive:
                    # Read through the running container. Docker's archive API
                    # may not expose files in runtime tmpfs mounts.
                    export = "\n".join([
                        "import sys, tarfile",
                        "from pathlib import Path",
                        "with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:",
                        "    for path in sorted(Path('/work/output').iterdir()):",
                        "        if path.name not in {'workspace', 'checkout', '.git'}:",
                        "            archive.add(path, arcname=path.name)",
                    ])
                    copy_code, _, copy_error = self._command(['docker', 'exec', name, 'python3', '-c', export],
                        cancel_token, deadline, outgoing=exported_archive,
                        output_limit=self.config.disk_mb * 1024 * 1024)
                    if copy_code:
                        detail = error.decode(errors='replace') if code else copy_error.decode(errors='replace')
                        raise AnalysisError('sandbox analysis/output unavailable: ' + detail[-2000:])
                    # Keep host transport Trace separate from the inner analysis Trace.
                    exported = output_dir / 'analysis'
                    extract_regular_tar(exported_archive, exported,
                        max_files=max_repository_files * 10, max_bytes=self.config.disk_mb * 1024 * 1024)
                inner_error = exported / 'sandbox-error.json'
                if code or inner_error.exists():
                    failure = json.loads(inner_error.read_text()) if inner_error.exists() else {}
                    raise AnalysisError(failure.get('message', error.decode(errors='replace')[:1000]),
                                        retryable=bool(failure.get('retryable')))
                result_file = exported / 'sandbox-result.json'
                if result_file.stat().st_size > 4 * 1024 * 1024:
                    raise SecurityError('sandbox result JSON exceeds quota')
                result = json.loads(result_file.read_text())
                if options.get('commit'):
                    expected_commit = options['commit']
                    manifest = json.loads((exported / 'manifest.json').read_text())
                    graph = json.loads((exported / 'graph.json').read_text())
                    if any(value != expected_commit for value in (
                            result.get('commit'), manifest.get('commit'), graph.get('meta', {}).get('commit'))):
                        raise AnalysisError('sandbox result commit binding mismatch', retryable=False)
                span.set_result({'status': 'succeeded'})
            stats_code, stats_raw, _ = self._command(['docker', 'stats', '--no-stream', '--format', '{{json .}}', name],
                                                       cancel_token, deadline)
            resources = {'cpus_limit': self.config.cpus, 'memory_limit_mb': self.config.memory_mb,
                         'disk_limit_mb': self.config.disk_mb, 'pids_limit': self.config.pids}
            if stats_code == 0:
                try:
                    sample = json.loads(stats_raw)
                    resources.update(cpu_percent=sample.get('CPUPerc'), memory=sample.get('MemUsage'),
                                     pids=sample.get('PIDs'), block_io=sample.get('BlockIO'),
                                     memory_percent=float(str(sample.get('MemPerc','0%')).rstrip('%')), sample='post-analysis')
                except (ValueError, TypeError):
                    pass
            result['resources'] = resources
            result['sandbox'] = {'image': self.config.image, 'network': 'none', 'mode': 'safe-generic'}
            result['output_dir'] = str(exported)
            return result
        except urllib.error.HTTPError as exc:
            raise RepositoryCheckoutError(f"repository archive request failed: HTTP {exc.code}",
                                          retryable=exc.code == 429 or exc.code >= 500) from exc
        except urllib.error.URLError as exc:
            raise RepositoryCheckoutError("repository archive network request failed", retryable=True) from exc
        except FileNotFoundError as exc:
            if exc.filename in {'docker', 'docker.exe'}:
                raise SandboxUnavailable('Docker executable is unavailable') from exc
            raise AnalysisError('sandbox artifact unavailable: ' + str(exc)) from exc
        finally:
            if created:
                # Forced removal terminates every descendant, including compiler processes.
                try:
                    subprocess.run(['docker', 'rm', '-f', name], timeout=15, capture_output=True, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    import logging
                    logging.getLogger(__name__).exception('sandbox cleanup failed: %s', name)
