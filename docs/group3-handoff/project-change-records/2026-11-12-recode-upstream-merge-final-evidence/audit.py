"""Audit the merged repository and fresh producer-to-Reviewer regression artifacts."""
import hashlib
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

RUN = Path(__file__).resolve().parent
ROOT = RUN.parents[1]
RECODE = ROOT.parent / "REcode"
sys.path[:0] = [str(ROOT / "runs/joint-full-e2e-20261028-v3/audit-deps"), str(ROOT / "v2-common")]
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT202012
from context_bundle import require_context_bundle
from first_group_protocol import require_first_group_response
from recode_artifacts import RecodeArtifactsClient, validate_source_checkout

def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def git(*arguments):
    return subprocess.check_output(["git", "-C", str(RECODE), *arguments], text=True).strip()

def deny(uri):
    raise NoSuchResource(ref=uri)

schemas = [load(path) for folder in (ROOT / "v2-concurrency/schemas", RECODE / "schemas") for path in folder.glob("*.json")]
registry = Registry(retrieve=deny).with_resources((schema["$id"], DRAFT202012.create_resource(schema)) for schema in schemas if "$id" in schema)
checked, captured, variants = [], [], []
evidence = ROOT / "docs/project-change-records/2026-11-12-recode-upstream-merge-final-evidence"
evidence.mkdir(exist_ok=False)

def capture(source, name):
    target = evidence / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    captured.append({"source": str(Path(source).resolve()), "path": name, "sha256": sha(target)})

def check(path, schema_path=None):
    value = load(path)
    if schema_path is None:
        schema_path = ROOT / "v2-concurrency/schemas" / (value["schema_version"].split("/")[0] + ".schema.json")
    Draft202012Validator(load(schema_path), registry=registry).validate(value)
    checked.append({"path": str(path), "sha256": sha(path), "schema": str(schema_path)})

merge = load(RUN / "merge-record.json")
assert git("rev-parse", "HEAD") == merge["merged_head"] == merge["upstream_head"]
for item in merge["local_files"]:
    assert sha(RECODE / item["path"]) == item["sha256"], item["path"]
changed = git("diff", "--name-only", merge["old_head"], merge["merged_head"]).splitlines()
assert len(changed) == 5 and all(not name.startswith("clang_pipeline/") for name in changed)

image = subprocess.check_output(["docker", "image", "inspect", "cbug/recode-semantic:20261112-merged", "--format", "{{.Id}}"], text=True).strip()
names = ["native_export.py", "race_detector.py", "repo_agent.py", "sandbox.py", "sandbox_entry.py", "stages.py", "concurrency_facts.py", "semantic_sync.py"]
program = "import hashlib,json,pathlib; names=" + repr(names) + "; print(json.dumps({name:hashlib.sha256(pathlib.Path('/opt/recode/clang_pipeline',name).read_bytes()).hexdigest() for name in names}))"
image_hashes = json.loads(subprocess.check_output(["docker", "run", "--rm", "--pull=never", "--network", "none", "--read-only", image, "python3", "-c", program], text=True))
for name, digest in image_hashes.items():
    assert sha(RECODE / "clang_pipeline" / name) == digest
    capture(RECODE / "clang_pipeline" / name, "sources/REcode/" + name)

for variant, expected in (("common", 0), ("missing", 1), ("distinct", 1)):
    run = RUN / variant
    summary = load(run / "summary.json")
    assert summary["status"] == "passed" and summary["recode_commit"] == merge["merged_head"]
    assert summary["image_id"] == image and summary["analyzer_code"] == "built_image"
    assert summary["analysis_invocations"] == 1
    validate_source_checkout(load(run / "review-input.json"))
    context = require_context_bundle(load(run / "http/concurrency-context-bundle.json"), expected_domain="concurrency")
    for field in ("call_facts", "sync_facts"):
        facts = require_first_group_response(context[field], expected_commit=summary["head_sha"])
        assert facts["status"] == "succeeded" and facts["coverage"]["complete"] is True
    cache = next((run / "http/recode-artifacts").iterdir())
    client = RecodeArtifactsClient(cache)
    assert client.snapshot_id == context["sync_facts"]["analysis_context"]["graph_snapshot_id"]
    for name in ("call-chains", "resource-flow", "sync-relations", "race-analysis"):
        path = cache / "external" / (name + ".json")
        check(path, RECODE / "schemas" / (name + ".schema.json"))
        capture(path, variant + "/native/" + path.name)
    coordinator = run / "concurrency"
    paths = [coordinator / name for name in ("candidate-bundle.json", "lock-impact-scope.json", "coordinator-state.json", "coordinator-result.json")]
    paths.extend(sorted((coordinator / "review-requests").glob("*.json")))
    paths.extend(sorted((coordinator / "review-results").glob("*.json")))
    for path in paths:
        check(path)
        capture(path, variant + "/concurrency/" + path.relative_to(coordinator).as_posix())
    result = load(coordinator / "coordinator-result.json")
    assert result["status"] == "completed" and result["coverage"]["complete"] is True
    assert result["candidate_count"] == result["reviewed_count"] == expected
    for review in result["reviews"]:
        assert review["status"] == "probable" and review["verdict"] == "defect"
        assert review["repository"]["base_sha"] == summary["base_sha"]
        assert review["repository"]["head_sha"] == summary["head_sha"]
        assert review["patch"]["status"] == "not_generated" and review["verification"]["status"] == "not_run"
    for path, name in ((run / "summary.json", "summary.json"), (run / "review-input.json", "review-input.json"),
                       (run / "http/concurrency-context-bundle.json", "context-bundle.json"),
                       (run / "http/coordination-report.json", "coordination-report.json"),
                       (run / "native-analysis.log", "native-analysis.log")):
        capture(path, variant + "/" + name)
    variants.append({"variant": variant, "head_sha": summary["head_sha"], "candidate_count": expected,
                     "reviewed_count": result["reviewed_count"], "coverage_complete": True,
                     "reviews": [{"verdict": review["verdict"], "status": review["status"]} for review in result["reviews"]]})

limits_schema = load(ROOT / "v2-concurrency/schemas/lock-race-impact-scope.schema.json")["properties"]["coverage"]["properties"]["local_limits"]
limits_validator = Draft202012Validator(limits_schema)
limits = load(RUN / "common/concurrency/lock-impact-scope.json")["coverage"]["local_limits"]
assert limits_validator.is_valid(limits)
invalid_limits = [dict(limits, max_depth=-1), dict(limits, cut_call_edge_count="0"),
                  dict(limits, cut_call_edge_ids=["duplicate", "duplicate"]),
                  dict(limits, cut_edge_ids_sha256="invalid"), dict(limits, unknown_field=True)]
missing = copy.deepcopy(limits)
missing.pop("max_depth")
invalid_limits.append(missing)
assert all(not limits_validator.is_valid(value) for value in invalid_limits)

framework = load(RUN / "framework-final/result.json")
assert framework["status"] == "passed" and framework["test_count"] == 315
assert framework["offline"]["blocked_python_network_events"] == 0
upstream = load(RUN / "upstream-result.json")
assert upstream["tests"] == 127 and upstream["failures"] == 2 and upstream["errors"] == 0 and upstream["skipped"] == 2
assert set(upstream["failed_tests"]) == {"test_pipeline.ClangPipelineTests.test_external_function_calls_are_preserved", "test_pipeline.ClangPipelineTests.test_seven_stages_produce_evidence_graph"}
for source, name in ((RUN / "merge-record.json", "merge-record.json"), (RUN / "local-before.patch", "local-before.patch"),
                     (RUN / "upstream-update.json", "upstream-update.json"), (RUN / "upstream-commit.txt", "upstream-commit.txt"),
                     (RUN / "framework-final/result.json", "framework-result.json"), (RUN / "upstream-result.json", "upstream-result.json"),
                     (RUN / "upstream-full.log", "upstream-full.log"), (Path(__file__), "audit.py")):
    capture(source, name)
for source, name in ((RUN / "audit-initial-error.json", "audit-initial-error.json"),
                     (RUN / "impact-schema-before.json", "impact-schema-before.json"),
                     (ROOT / "v2-concurrency/schemas/lock-race-impact-scope.schema.json", "impact-schema-final.json"),
                     (ROOT / "v2-concurrency/scripts/test_race_pair_correctness.py", "sources/test_race_pair_correctness.py")):
    capture(source, name)
result = {"status": "passed", "merge_head": merge["merged_head"], "preserved_local_files": len(merge["local_files"]),
          "upstream_changed_files": changed, "variants": variants, "schema_objects": len(checked), "evidence_files": len(captured),
          "framework_tests": 315, "registered_reviewer_invocations": 2, "image_id": image, "image_source_sha256": image_hashes,
          "local_limits_invalid_payloads_rejected": len(invalid_limits),
          "upstream_suite": {"status": "failed", "tests": 127, "passed": 123, "failed": 2, "skipped": 2, "errors": 0},
          "scope": "Three four-file constructed C PRs, real HTTP/SQLite/object-store/native Docker extraction; two registered model reviews and deterministic result acceptance.",
          "patch_generation": "not_run", "large_repository_evaluation": "not_run", "production_download_runner": "not_run",
          "remote_schema_retrieval": "disabled"}
(RUN / "audit-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
(evidence / "manifest.json").write_text(json.dumps({"summary": result, "files": captured, "schema_checks": checked}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
assert len({entry["path"] for entry in captured}) == len(captured)
assert all(sha(evidence / entry["path"]) == entry["sha256"] for entry in captured)
print(json.dumps(result, ensure_ascii=False))
