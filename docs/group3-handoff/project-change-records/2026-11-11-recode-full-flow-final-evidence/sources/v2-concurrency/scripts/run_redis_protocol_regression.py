"""Host-approved, isolated runtime checks of actual Redis BIO source (not full BIO integration)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

DRIVERS = {"condition": "bio_drain_protocol.c", "deadlock": "bio_deadlock_protocol.c",
           "counter": "bio_counter_protocol.c", "counter-read": "bio_counter_read_protocol.c"}
ASSETS = Path(__file__).resolve().parents[1] / "docker" / "redis-regressions"


def verify(repository: Path, image: str, scenario: str, *, timeout_seconds: int = 60) -> dict:
    if scenario not in DRIVERS:
        raise ValueError("unsupported regression scenario")
    return verify_driver(repository, image, ASSETS / DRIVERS[scenario], scenario=scenario, timeout_seconds=timeout_seconds)


def verify_driver(repository: Path, image: str, driver_path: Path, *, scenario: str, timeout_seconds: int = 60,
                  template: str = "redis_bio_c") -> dict:
    """Execute one host-captured C test using a fixed command/container template."""
    import re
    driver_path = driver_path.resolve(strict=True)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.c", driver_path.name):
        raise ValueError("unsafe C driver filename")
    if not 1 <= timeout_seconds <= 300:
        raise ValueError("timeout must be in [1, 300]")
    if template not in {"redis_bio_c", "c_source"}:
        raise ValueError("unsupported fixed C execution template")
    repo = repository.resolve(strict=True)
    def git(*argv):
        return subprocess.check_output(["git", "-c", "core.autocrlf=false", "-C", str(repo), *argv], timeout=30)
    head = git("rev-parse", "HEAD").decode().strip()
    before = hashlib.sha256(git("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--")).hexdigest()
    image_id = subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                                       text=True, timeout=30).strip()
    name = "cbug-protocol-" + uuid.uuid4().hex
    driver_hash = hashlib.sha256(driver_path.read_bytes()).hexdigest()
    includes = ("-I/source/src -I/source/deps/hiredis -I/source/deps/linenoise -I/source/deps/lua/src -I/source/deps/hdr_histogram "
                if template == "redis_bio_c" else "-I/source ")
    command = ("gcc -std=gnu11 -O1 -pthread -ffunction-sections -fdata-sections " + includes +
               f"/regressions/{driver_path.name} -Wl,--gc-sections -o /work/protocol "
               "&& timeout 10s /work/protocol")
    argv = ["docker", "run", "--name", name, "--rm", "--network", "none", "--read-only",
            "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--tmpfs", "/work:rw,exec,size=64m,mode=1777",
            "--mount", f"type=bind,source={repo},target=/source,readonly",
            "--mount", f"type=bind,source={driver_path.parent},target=/regressions,readonly",
            image_id, "bash", "-lc", command]
    started = time.perf_counter()
    timed_out = False
    try:
        proc = subprocess.run(argv, text=True, encoding="utf-8", errors="replace",
                              capture_output=True, timeout=timeout_seconds)
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        code = None
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
    finally:
        # Delete only this invocation's UUID-named container, including on timeout.
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
    after = hashlib.sha256(git("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--")).hexdigest()
    head_after = git("rev-parse", "HEAD").decode().strip()
    driver_unchanged = hashlib.sha256(driver_path.read_bytes()).hexdigest() == driver_hash
    unchanged = before == after and head == head_after and driver_unchanged
    return {"schema_version": "redis-protocol-regression/0.1", "scenario": scenario,
            "scope": ("actual_C_source_behavior_under_generated_driver" if template == "c_source" else {"counter": "actual_bioPendingJobsOfType_mutex_entry_not_exhaustive_race_detection",
                       "counter-read": "actual_query_mutex_or_atomicGet_not_writer_protocol_or_exhaustive_race_detection"}
                      .get(scenario, "actual_bioDrainWorker_controlled_queue_not_full_BIO_integration")),
            "repository": str(repo), "head_sha": head, "head_sha_after": head_after,
            "image_id": image_id, "argv": argv, "exit_code": code, "timed_out": timed_out,
            "driver_sha256": driver_hash, "driver_unchanged": driver_unchanged,
            "status": "passed" if code == 0 and unchanged and not timed_out else "failed",
            "diff_before_sha256": before, "diff_after_sha256": after, "diff_unchanged": unchanged,
            "seconds": round(time.perf_counter() - started, 3), "stdout": stdout, "stderr": stderr}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--scenario", required=True, choices=sorted(DRIVERS))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    args = parser.parse_args()
    try:
        result = verify(args.repository, args.image, args.scenario, timeout_seconds=args.timeout_seconds)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result = {"schema_version": "redis-protocol-regression/0.1", "status": "failed", "errors": [str(exc)]}
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
