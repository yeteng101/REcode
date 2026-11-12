"""Compile all host-selected C translation units and run the resulting executable."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import uuid


CONTAINER_DRIVER = """import json, subprocess, sys
settings=json.loads(sys.argv[1])
command=['gcc','-std=gnu11','-O1','-pthread','-I/source',*['/source/'+p for p in settings['sources']],'-o','/work/application']
build=subprocess.run(command,capture_output=True,text=True,timeout=60)
result={'build_exit':build.returncode,'build_stdout':build.stdout,'build_stderr':build.stderr,'smoke_exit':None}
if build.returncode == 0 and settings['mode'] == 'smoke':
    smoke=subprocess.run(['/work/application'],capture_output=True,text=True,timeout=10)
    result.update(smoke_exit=smoke.returncode,smoke_stdout=smoke.stdout,smoke_stderr=smoke.stderr)
print(json.dumps(result))
"""


def run(repository, image, sources, mode, expected_exit):
    root = repository.resolve(strict=True)
    def git(*args):
        return subprocess.check_output(["git", "-c", "core.autocrlf=false", "-C", str(root), *args], stderr=subprocess.PIPE, timeout=30)
    selected = []
    for source in sources:
        path = PurePosixPath(source)
        if path.is_absolute() or ".." in path.parts or "\\" in source or ":" in source or path.suffix != ".c":
            raise ValueError("build source must be a repository-relative C path")
        resolved = (root / source).resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file() or (root / source).is_symlink():
            raise ValueError("build source escapes the selected repository")
        selected.append(path.as_posix())
    before_head = git("rev-parse", "HEAD").decode().strip()
    before_diff = hashlib.sha256(git("diff", "--binary", "HEAD", "--")).hexdigest()
    image_id = subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True, timeout=30).strip()
    settings = json.dumps({"sources": selected, "mode": mode})
    name = "cbug-c-build-" + uuid.uuid4().hex
    argv = ["docker", "run", "--rm", "--name", name, "--pull=never", "--network", "none", "--read-only",
        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--memory", "1g", "--cpus", "2",
        "--tmpfs", "/tmp:rw,size=128m,mode=1777", "--tmpfs", "/work:rw,exec,size=128m,mode=1777",
        "--mount", "type=bind,source=" + str(root) + ",target=/source,readonly",
        image_id, "python3", "-c", CONTAINER_DRIVER, settings]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
    if proc.returncode:
        raise RuntimeError("C verification container failed: " + proc.stderr[-3000:])
    data = json.loads(proc.stdout)
    head_after = git("rev-parse", "HEAD").decode().strip()
    diff_after = hashlib.sha256(git("diff", "--binary", "HEAD", "--")).hexdigest()
    if before_head != head_after or before_diff != diff_after:
        raise RuntimeError("source version or patch changed during C verification")
    code = 20 if data["build_exit"] != 0 else 21 if mode == "smoke" and data["smoke_exit"] != expected_exit else 0
    return {"schema_version": "c-source-docker-verification/0.1", "status": "passed" if code == 0 else "failed",
        "repository": str(root), "head_sha": before_head, "head_sha_after": head_after, "diff_unchanged": True,
        "image_id": image_id, "sources": selected, "mode": mode, "expected_smoke_exit": expected_exit,
        "exit_code": code, "argv": argv, **data}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--image", required=True)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--mode", choices=("build", "smoke"), required=True)
    parser.add_argument("--expected-exit", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.repository, args.image, args.source, args.mode, args.expected_exit)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        result = {"schema_version": "c-source-docker-verification/0.1", "status": "failed", "exit_code": 90, "errors": [str(exc)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
