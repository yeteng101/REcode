from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .security import extract_regular_tar, normalize_commit_sha


def main():
    if sys.argv[1] == "import":
        extract_regular_tar(sys.stdin.buffer, Path('/work/repository'),
                            max_files=int(sys.argv[2]), max_bytes=int(sys.argv[3]), strip_root=True)
        return
    if sys.argv[1] != "analyze":
        raise ValueError('unsupported sandbox operation')
    request = json.load(sys.stdin)
    request['commit'] = normalize_commit_sha(request.get('commit'))
    # No repository instructions, shell, model credentials, CMake or supplied
    # compile_commands are used. The pipeline generates compiler arguments.
    from .repo_agent import run_repo_analysis
    from .execution import AnalysisError
    try:
        identity = None
        if request.get('commit'):
            identity = {'repository_url': request['repository_url'], 'commit': request['commit']}
        result = run_repo_analysis('/work/repository', output_dir='/work/output',
            work_root='/work/checkout', archive=False, safe_mode=True, build_profile='safe-generic',
            entry_symbols=request.get('entry_symbols'), optimized_ir=bool(request.get('optimized_ir')),
            snapshot_identity=identity,
            max_repository_files=request['max_repository_files'],
            max_repository_bytes=request['max_repository_bytes'])
        result['repository_url'] = request['repository_url']
        result['commit'] = request.get('commit') or 'archive-ref'
        Path('/work/output/sandbox-result.json').write_text(json.dumps(result), encoding='utf-8')
    except Exception as exc:
        Path('/work/output').mkdir(parents=True, exist_ok=True)
        error = exc.to_dict() if isinstance(exc, AnalysisError) else {
            'code': 'sandbox_analysis_failed', 'message': str(exc), 'retryable': False}
        Path('/work/output/sandbox-error.json').write_text(json.dumps(error), encoding='utf-8')
        raise


if __name__ == '__main__':
    main()
