"""Independent local-schema, execution, frozen-test and Git-patch acceptance audit."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "runs/joint-full-e2e-20261028-v3/audit-deps"), str(ROOT / "v2-concurrency"), str(ROOT / "v2-common")]
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT202012
from patching.behavior import validate_certificate, validate_behavior_plan
from patching.test_tasks import assert_artifact, git_guard
from context_bundle import require_context_bundle
from recode_artifacts import RecodeArtifactsClient


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def deny(uri):
    raise NoSuchResource(ref=uri)


schemas = [load(p) for folder in (ROOT / "v2-concurrency/schemas", ROOT.parent / "REcode/schemas") for p in folder.glob("*.json")]
registry = Registry(retrieve=deny).with_resources((s["$id"], DRAFT202012.create_resource(s)) for s in schemas if "$id" in s)
evidence = ROOT / "docs/project-change-records/2026-11-11-recode-full-flow-final-evidence"
evidence.mkdir(exist_ok=False)
captured, checked, rows = [], [], []

image = "sha256:bc75917d21dd90e38fb15ae9e56fb026034b2556abcb1b92401e8843b2e343ec"
hash_program = "import hashlib,json,pathlib; print(json.dumps({name:hashlib.sha256(pathlib.Path('/opt/recode/clang_pipeline',name).read_bytes()).hexdigest() for name in ['semantic_sync.py','native_export.py','race_detector.py']}))"
image_hashes = json.loads(subprocess.check_output([
    "docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only",
    image, "python3", "-c", hash_program], text=True))
for filename, digest in image_hashes.items():
    assert sha(ROOT.parent / "REcode/clang_pipeline" / filename) == digest


def capture(path, relative):
    target = evidence / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, target)
    captured.append({"source": str(Path(path).resolve()), "path": relative, "sha256": sha(target)})


def check(path):
    value = load(path)
    schema_path = ROOT / "v2-concurrency/schemas" / (value["schema_version"].split("/")[0] + ".schema.json")
    Draft202012Validator(load(schema_path), registry=registry).validate(value)
    checked.append({"path": str(path), "sha256": sha(path), "schema": str(schema_path)})


for name, label in (("recode-full-final-missing-20261111", "missing"), ("recode-full-final-distinct-20261111", "distinct")):
    run = ROOT / "runs" / name
    state_path = run / "workflow/workflow-state.json"
    state = load(state_path)
    result_path = Path(state["result_path"])
    result = load(result_path)
    assert state["phase"] == result["status"] == "completed" and state["next_action"] is None
    assert len(result["items"]) == 1 and result["items"][0]["status"] == "verified"
    assert result["coverage"]["complete"] is True and not result["errors"]
    candidate_id = state["candidate_ids"][0]
    assert state["patch_attempts_used"][candidate_id] == state["test_attempts_used"][candidate_id] == 1
    current = state["artifacts"][candidate_id]
    request_path = Path(current["current_patch_request"])
    request = load(request_path)
    receipt = current["behavior_test"]
    artifact = validate_certificate(receipt, candidate_id, state["repository"])
    validate_behavior_plan(receipt, request["verification_plan"], request["verification_scope"])
    assert_artifact(artifact)
    validation = load(receipt["validation_path"])
    codes = {}
    for version in validation["versions"]:
        execution_path = Path(version["result_path"])
        execution = load(execution_path)
        expected = 42 if version["version"] == "defective" else 0
        assert execution["stable"] is True and len(execution["executions"]) == 3
        assert all(e["exit_code"] == expected for e in execution["executions"])
        assert execution["test_artifact_sha256"] == artifact["artifact_sha256"]
        codes[version["version"]] = [e["exit_code"] for e in execution["executions"]]
        capture(execution_path, label + "/" + version["version"] + "-executions.json")
    verification_path = request_path.parent / "verification-result.json"
    verification = load(verification_path)
    assert verification["status"] == "passed" and verification["diff_unchanged"] is True
    assert all(c["status"] == "passed" and c["exit_code"] == 0 for c in verification["checks"])
    generated_check = next(c for c in verification["checks"] if c["name"] == "generated-defect-regression")
    fixed_path = Path(generated_check["argv"][generated_check["argv"].index("--output") + 1])
    fixed = load(fixed_path)
    assert fixed["stable"] is True and len(fixed["executions"]) == 3
    assert all(e["exit_code"] == 0 and e["driver_unchanged"] and e["diff_unchanged"] for e in fixed["executions"])
    assert fixed["test_artifact_sha256"] == artifact["artifact_sha256"]
    assert fixed["driver_sha256"] == artifact["files"]["driver.c"]
    patch = Path(result["items"][0]["patch"]["patch_path"])
    expected_patch_sha = result["items"][0]["patch"]["diff_sha256"]
    assert sha(patch) == expected_patch_sha
    actual_diff = subprocess.check_output(["git", "-c", "core.autocrlf=false", "-C", request["workspace"]["path"],
        "diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"], stderr=subprocess.PIPE)
    assert hashlib.sha256(actual_diff).hexdigest() == expected_patch_sha
    original = run / "repository"
    guard = git_guard(original)
    assert guard["head_sha"] == state["repository"]["head_sha"]
    assert guard["diff_sha256"] == guard["status_sha256"] == hashlib.sha256(b"").hexdigest()
    subprocess.run(["git", "-c", "core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol", "-C", str(original),
                    "apply", "--check", str(patch)], check=True, capture_output=True)
    bundle = require_context_bundle(load(run / "http/concurrency-context-bundle.json"), expected_domain="concurrency")
    cache = next((run / "http/recode-artifacts").iterdir())
    client = RecodeArtifactsClient(cache)
    for filename in ("call-chains", "resource-flow", "sync-relations", "race-analysis"):
        native_path = cache / "external" / (filename + ".json")
        native_schema = ROOT.parent / "REcode/schemas" / (filename + ".schema.json")
        Draft202012Validator(load(native_schema), registry=registry).validate(load(native_path))
        checked.append({"path": str(native_path), "sha256": sha(native_path), "schema": str(native_schema)})
        capture(native_path, label + "/native/" + native_path.name)
    assert bundle["sync_facts"]["analysis_context"]["graph_snapshot_id"] == client.snapshot_id
    assert bundle["sync_facts"]["status"] == "succeeded" and bundle["call_facts"]["status"] == "succeeded"
    recheck = load(run / "post-repair-first-group/summary.json")
    assert recheck["status"] == "passed" and recheck["candidate_count"] == 0 and recheck["coverage"]["complete"]
    assert recheck["patch_sha256"] == expected_patch_sha and recheck["tree_matches_verified_worktree"]
    schema_paths = [state_path, result_path, request_path, verification_path, run / "review.json",
        Path(current["current_test_request"]), Path(receipt["artifact_path"]), Path(receipt["validation_path"]),
        run / "test-worker-report.json", run / "patch-worker-report.json",
        Path(artifact["workspace"]) / "test-case.json"]
    for path in schema_paths:
        check(path)
    for path, filename in ((state_path, "workflow-state.json"), (result_path, "run-result.json"),
        (run / "summary.json", "first-group-summary.json"), (run / "review-input.json", "review-input.json"),
        (run / "review.json", "review.json"), (verification_path, "verification-result.json"),
        (request_path, "patch-request.json"), (Path(receipt["artifact_path"]), "test-artifact.json"),
        (Path(receipt["validation_path"]), "test-validation.json"), (fixed_path, "fixed-executions.json"),
        (Path(receipt["host_review_path"]), "host-review.json"), (run / "test-role-adapter.json", "test-role-adapter.json"),
        (Path(artifact["workspace"]) / "driver.c", "driver.c"), (Path(artifact["workspace"]) / "test-case.json", "test-case.json"),
        (patch, "repair.patch"), (run / "post-repair-first-group/summary.json", "post-repair-first-group.json"),
        (cache / "external/race-analysis.json", "native-race-analysis.json")):
        capture(path, label + "/" + filename)
    for path, filename in ((run / "http/concurrency-context-bundle.json", "context-bundle.json"),
                           (run / "test-worker-report.json", "test-worker-report.json"),
                           (run / "patch-worker-report.json", "patch-worker-report.json"),
                           (Path(current["current_test_request"]), "test-request.json")):
        capture(path, label + "/" + filename)
    if label == "distinct":
        capture(run / "test-worker-interruption.json", label + "/test-worker-interruption.json")
    for path in sorted((run / "mcp").rglob("*.json")):
        capture(path, label + "/mcp/" + path.relative_to(run / "mcp").as_posix())
    bundle_path = evidence / label / "repository.bundle"
    subprocess.run(["git", "-C", str(original), "bundle", "create", str(bundle_path), "--all"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(original), "bundle", "verify", str(bundle_path)], check=True, capture_output=True)
    captured.append({"source": str(original), "path": label + "/repository.bundle", "sha256": sha(bundle_path)})
    rows.append({"case": label, "original_head": guard["head_sha"], "first_group_candidates": 1,
        "review": "probable/defect", "normal_exit_codes": codes["normal"], "original_exit_codes": codes["defective"],
        "repaired_exit_codes": [e["exit_code"] for e in fixed["executions"]], "patch_status": "verified",
        "workflow_status": "completed", "post_repair_candidates": 0, "diff_sha256": expected_patch_sha,
        "test_artifact_sha256": artifact["artifact_sha256"], "patch_attempts": 1, "test_attempts": 1,
        "test_harness_role": "worker_with_current_project_profile", "checks": {c["name"]: c["status"] for c in verification["checks"]}})
    rows[-1]["test_report_origin"] = load(run / "test-role-adapter.json")["report_origin"]

framework_path = ROOT / "runs/recode-full-flow-framework-final-20261111/result.json"
framework = load(framework_path)
assert framework["status"] == "passed" and framework["test_count"] == 314
capture(framework_path, "framework-result.json")
capture(ROOT / "runs/recode-full-e2e-20261111/registered-test-role-refusal.json", "registered-test-role-refusal.json")
for name in ("v2-common/pr_adapter.py", "v2-common/test_pr_adapter.py", "v2-concurrency/patching/test_tasks.py",
    "v2-concurrency/patching/pipeline.py", "v2-concurrency/patching/verifier.py",
    "v2-concurrency/scripts/run_generated_redis_test.py", "v2-concurrency/scripts/run_redis_protocol_regression.py",
    "v2-concurrency/scripts/run_c_docker_verification.py", "v2-concurrency/schemas/lock-race-test-policy.schema.json",
    "v2-concurrency/schemas/lock-race-test-case.schema.json", ".codex/agents/lock-race-test-worker.toml",
    "tests/concurrency/test_patch_crlf.py", "tests/concurrency/test_test_authoring.py",
    "tests/concurrency/test_strict_verification_defaults.py", "v2-concurrency/impact/lock_scope.py",
    "v2-concurrency/scripts/test_audit_improvements.py"):
    capture(ROOT / name, "sources/" + name)
for filename in image_hashes:
    capture(ROOT.parent / "REcode/clang_pipeline" / filename, "sources/REcode/clang_pipeline/" + filename)
capture(ROOT / "runs/recode-semantic-regression-20261111/upstream-full-04.log", "upstream-full-known-failures.log")
capture(Path(__file__), "audit.py")
summary = {"status": "passed", "cases": rows, "schema_objects": len(checked), "framework_tests": 314,
    "evidence_files": len(captured), "registered_generic_test_role": "not_run_cached_profile_refused",
    "registered_reviewer_and_patch_roles": "used", "upstream_large_repository_validation": "not_run",
    "scope": "Two constructed four-file C PRs, same counter-defect family, real first-group local HTTP/native Docker extraction and stdio MCP; bounded stress and complete three-unit build/smoke.",
    "remote_schema_retrieval": "disabled", "image_id": image, "image_source_sha256": image_hashes,
    "upstream_regression": {"tests": 127, "passed": 123, "failed": 2, "skipped": 2,
                            "source": "prior stage run on identical analyzer code"},
    "test_worker_interruption": "distinct worker saved complete files before safety-filter interruption; host inspected and recovered report with explicit provenance"}
(Path(__file__).parent / "full-flow-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(evidence / "manifest.json").write_text(json.dumps({"files": captured, "summary": summary, "schema_checks": checked}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
assert all(sha(evidence / entry["path"]) == entry["sha256"] for entry in captured)
print(json.dumps(summary, ensure_ascii=False))
