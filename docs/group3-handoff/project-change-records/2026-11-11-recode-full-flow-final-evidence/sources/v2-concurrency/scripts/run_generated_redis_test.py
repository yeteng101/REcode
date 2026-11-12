"""Fixed execution template for host-captured Concurrency tests, no model argv."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from patching.test_tasks import assert_artifact
from run_redis_protocol_regression import verify_driver


def run(repository: Path, image: str, artifact_path: Path) -> dict:
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert_artifact(artifact)
    executions = [verify_driver(repository, image, Path(artifact["workspace"]) / "driver.c", scenario="generated",
                                template=artifact["test_case"]["template"])
                  for _ in range(artifact.get("repetitions", 1))]
    result = dict(executions[0])
    if artifact.get("test_strategy") == "behavior_before_patch":
        codes = [e["exit_code"] for e in executions]
        stable = (len(set(codes)) == 1 and all(not e["timed_out"] and e["driver_unchanged"] and e["diff_unchanged"]
                  and e["driver_sha256"] == artifact["files"]["driver.c"] for e in executions))
        result.update(executions=executions, repetitions=artifact["repetitions"], stable=stable,
                      exit_code=codes[0] if stable else None,
                      status="passed" if stable and all(e["status"] == "passed" for e in executions) else "failed",
                      driver_unchanged=all(e["driver_unchanged"] for e in executions),
                      diff_unchanged=all(e["diff_unchanged"] for e in executions),
                      timed_out=any(e["timed_out"] for e in executions))
    result["schema_version"] = "lock-race-generated-test-execution/0.1"
    result["scope"] = artifact["test_case"]["limitations"]
    result["test_artifact_sha256"] = artifact["artifact_sha256"]
    result["candidate_id"] = artifact["candidate_id"]
    assert_artifact(artifact)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--test-artifact", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args.repository, args.image, args.test_artifact)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # A template/setup failure is never converted to a candidate assertion.
        result = {"schema_version": "lock-race-generated-test-execution/0.1", "status": "failed", "errors": [str(exc)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1

if __name__ == "__main__": raise SystemExit(main())
