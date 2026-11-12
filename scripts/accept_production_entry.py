"""Real HTTP -> durable queue -> Docker -> S3 acceptance. No mocks or skips."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://github.com/cbitosc/CBIT-Hacktoberfest-Hackathon-Hello-World'
COMMIT = '4125ced214965f0bd3715fb696d68ee1017116f8'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def validate_bundle(raw, result):
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        def read(name):
            member = archive.getmember('analysis/' + name)
            require(member.isfile() and member.size < 32 * 1024 * 1024, name)
            return archive.extractfile(member).read()

        manifest = json.loads(read('manifest.json'))
        graph = json.loads(read('graph.json'))
        require(any(n.get('name') == 'main' for n in graph['nodes']), 'missing real main function')
        for document in (result, manifest):
            require(document['repository_url'] == URL and document['commit'] == COMMIT,
                    'result/manifest snapshot mismatch')
        documents = [(graph, graph['meta'])]
        for name in ('call-chains', 'resource-flow', 'sync-relations', 'race-analysis'):
            document = json.loads(read('external/' + name + '.json'))
            documents.append((document, document['source']))
        for document, source in documents:
            require(document['run_id'] == result['run_id'], 'run_id mismatch')
            require(source['commit'] == COMMIT and source['repository'] == manifest['repository'],
                    'graph/export snapshot mismatch')
            require(source['build_profile'] == 'safe-generic', 'unsafe profile')
        require(read('visualization.html') and read('report.md'), 'missing rendered outputs')
        require(read('trace/tool-trace.jsonl'), 'missing analysis trace')
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    evidence = {'status': 'failed', 'repository_url': URL, 'commit': COMMIT, 'checks': []}
    children, logs = [], []
    try:
        import boto3
        require(os.environ.get('CRA_OBJECT_BACKEND') == 's3', 'real S3 backend required')
        image = os.environ['CRA_SANDBOX_TEST_IMAGE']
        require('@sha256:' in image, 'immutable image required')
        evidence['image'] = image
        env = dict(os.environ, CRA_SANDBOX_IMAGE=image, CRA_MAX_WORKERS='0',
                   CRA_JOB_ROOT=str(output / 'jobs'), CRA_JOB_RETENTION_SECONDS='30',
                   CRA_JOB_CLEANUP_INTERVAL_SECONDS='1', CRA_JOB_MAX_ATTEMPTS='1',
                   CRA_JOB_TIMEOUT_SECONDS='180')
        for key in ('CRA_SANDBOX_MODE', 'CRA_ENV', 'CRA_ALLOW_LOCAL_REPOSITORIES',
                    'CRA_JOB_DATABASE', 'CRA_OBJECT_ROOT'):
            env.pop(key, None)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        endpoint = '/api/v1/agent/analyze'

        def request(path, body=None, key=None):
            headers = {'Content-Type': 'application/json'}
            if key:
                headers['Idempotency-Key'] = key
            req = Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
            try:
                response = urlopen(req, timeout=15)
            except HTTPError as error:
                response = error
            with response:
                return response.status, response.read()

        def start(module, *arguments):
            log = (output / f'{len(children)}-{module.rsplit(".", 1)[-1]}.log').open('wb')
            logs.append(log)
            process = subprocess.Popen([sys.executable, '-m', module, *arguments], cwd=ROOT,
                                       env=env, stdout=log, stderr=subprocess.STDOUT)
            children.append(process)
            return process

        def stop(process):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)

        def api():
            process = start('clang_pipeline.agent_server', '--port', str(port))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                require(process.poll() is None, 'API exited; inspect logs')
                try:
                    if request('/health')[0] == 200:
                        return process
                except (URLError, TimeoutError):
                    pass
                time.sleep(.2)
            raise TimeoutError('API readiness')

        server = api()
        require(request(endpoint, {'repository_url': str(ROOT)})[0] == 400, 'local source accepted')
        require(request('/api/v1/agent/ask', {'question': 'test'})[0] == 403, 'host ask enabled')
        body = {'repository_url': URL, 'commit': COMMIT}
        code, raw = request(endpoint, body, 'production-acceptance')
        require(code == 202, raw.decode())
        job = json.loads(raw)['job_id']
        evidence['job_id'] = job
        path = endpoint + '/' + job
        require(json.loads(request(path)[1])['status'] == 'queued', 'API unexpectedly executes jobs')
        stop(server)
        server = api()
        require(json.loads(request(path)[1])['status'] == 'queued', 'queue lost on API restart')
        require(json.loads(request(endpoint, body, 'production-acceptance')[1])['job_id'] == job,
                'idempotency lost on restart')
        require(request(endpoint, {**body, 'commit': 'a' * 40}, 'production-acceptance')[0] == 409,
                'idempotency conflict accepted')
        evidence['checks'].append('queued restart and idempotency')
        start('clang_pipeline.job_worker', '--workers', '1')
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            state = json.loads(request(path)[1])
            if state['status'] == 'succeeded':
                break
            require(state['status'] in ('queued', 'running'), json.dumps(state.get('error')))
            time.sleep(.5)
        else:
            raise TimeoutError('analysis')
        client = boto3.client('s3', endpoint_url=env['CRA_S3_ENDPOINT_URL'])
        bucket, prefix = env['CRA_S3_BUCKET'], env.get('CRA_S3_PREFIX', 'recode').strip('/')
        hashes = {}
        downloads = {}
        for name in ('result', 'artifacts'):
            descriptor = state['objects'][name]
            require(descriptor['backend'] == 's3', 'wrong backend')
            code, raw = request(path + '/' + name)
            require(code == 200 and len(raw) == descriptor['size'], 'download failed')
            key = '/'.join(filter(None, (prefix, descriptor['key'])))
            stored = client.get_object(Bucket=bucket, Key=key)['Body']
            try:
                require(stored.read() == raw, 'HTTP download differs from S3')
            finally:
                stored.close()
            hashes[name] = hashlib.sha256(raw).hexdigest()
            downloads[name] = raw
        validate_bundle(downloads['artifacts'], json.loads(downloads['result']))
        evidence.update(hashes=hashes, checks=evidence['checks'] + ['real Docker analysis and S3 artifact identity'])
        stop(server)
        server = api()
        require(json.loads(request(path)[1])['status'] == 'succeeded', 'completed state lost')
        for name, digest in hashes.items():
            code, raw = request(path + '/' + name)
            require(code == 200 and hashlib.sha256(raw).hexdigest() == digest, 'restart download changed')
        evidence['checks'].append('completed restart and byte-identical downloads')
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            if json.loads(request(path)[1])['status'] == 'expired':
                break
            time.sleep(.5)
        else:
            raise TimeoutError('retention cleanup')
        for name in hashes:
            require(request(path + '/' + name)[0] == 404, 'expired download still available')
        listed = client.list_objects_v2(Bucket=bucket, Prefix='/'.join(filter(None, (prefix, job))) + '/')
        require(not listed.get('Contents'), 'expired S3 objects remain')
        require(json.loads(request(endpoint, body, 'production-acceptance')[1])['status'] == 'expired',
                'expired idempotent job re-executed')
        evidence['checks'].append('retention deletes S3 objects and preserves idempotency tombstone')
        evidence['status'] = 'passed'
    except Exception as exc:
        evidence['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        for log in logs:
            log.close()
        (output / 'evidence.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
