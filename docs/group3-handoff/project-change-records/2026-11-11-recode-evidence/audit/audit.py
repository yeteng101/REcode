"""Read-only binding and closed-local schema audit of REcode integration results."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
RECODE = ROOT.parent / "REcode"
sys.path[:0] = [str(ROOT / "runs/joint-full-e2e-20261028-v3/audit-deps"), str(ROOT / "v2-common")]
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT202012
from context_bundle import require_context_bundle
from first_group_protocol import require_first_group_response
from recode_artifacts import RecodeArtifactsClient, validate_source_checkout

load = lambda p: json.loads(p.read_text(encoding="utf-8"))
def deny(uri):
    raise NoSuchResource(ref=uri)

schemas = [load(p) for folder in (ROOT / "v2-concurrency/schemas", RECODE / "schemas") for p in folder.glob("*.json")]
registry = Registry(retrieve=deny).with_resources((s["$id"], DRAFT202012.create_resource(s)) for s in schemas if "$id" in s)
checked = []
def check(path, schema_path):
    schema = load(schema_path)
    Draft202012Validator(schema, registry=registry).validate(load(path))
    checked.append({"path": str(path), "schema": str(schema_path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

small = ROOT / "runs/recode-integration-auto-20261111"
production = ROOT / "runs/recode-production-service-20261111-08"
context = ROOT / "runs/recode-production-context-20261111"
assert load(small / "summary.json")["status"] == load(production / "summary.json")["status"] == "passed"
for run, common in ((small, "http"), (context, "common-auto")):
    review = load(run / ("review-input.json" if run == small else "review-input-auto.json"))
    assert "requested_routes" not in review
    coordination = load(run / common / "coordination-report.json")
    assert coordination["routing"]["routes"] == ["memory", "concurrency"]
    for domain in ("memory", "concurrency"):
        bundle = require_context_bundle(load(run / common / (domain + "-context-bundle.json")), expected_domain=domain)
        assert bundle["coverage"]["complete"] is False
        if run == context:
            assert (run / common / (domain + "-context-bundle.json")).read_bytes() == (run / "common-acceptance" / (domain + "-context-bundle.json")).read_bytes()
        assert bundle["call_facts"]["analysis_context"]["graph_snapshot_id"] == bundle[("resource_facts" if domain == "memory" else "sync_facts")]["analysis_context"]["graph_snapshot_id"]
        for facts in (bundle["call_facts"], bundle[("resource_facts" if domain == "memory" else "sync_facts")]):
            require_first_group_response(facts, expected_commit=bundle["repository"]["commit_sha"])
            assert facts["analysis_context"]["analyzer"]["formal_evidence"] is False

validate_source_checkout(load(context / "review-input.json"))
cache = next((production / "artifacts").iterdir())
client = RecodeArtifactsClient(cache)
assert client.manifest["commit"] == "e6e3b4222715cea71c150f0dd824ed990e7f567d"
for name in ("call-chains", "resource-flow", "sync-relations"):
    check(cache / "external" / (name + ".json"), RECODE / "schemas" / (name + ".schema.json"))

for path in [small / "concurrency/candidate-bundle.json", small / "concurrency/coordinator-state.json", *sorted((small / "concurrency/review-requests").glob("*.json")),
             context / "concurrency-acceptance/candidate-bundle.json", context / "concurrency-acceptance/coordinator-state.json"]:
    version = load(path)["schema_version"].split("/")[0]
    check(path, ROOT / "v2-concurrency/schemas" / (version + ".schema.json"))
bundle = load(small / "concurrency/candidate-bundle.json")
assert len(bundle["candidates"]) == 3
assert bundle["provenance"]["source_kind"] == "first_group" and bundle["provenance"]["formal_evidence"] is False
assert all(c["candidate_status"] == "needs_more_context" and c["shared_object"]["declaration"] is None for c in bundle["candidates"])
framework = load(ROOT / "runs/recode-integration-framework-routing-final-20261111/result.json")
assert framework["status"] == "passed" and framework["test_count"] == 287
result = {"status": "passed", "schema_objects": len(checked), "checked": checked,
          "small_candidates": 3, "small_candidate_status": "needs_more_context", "framework_tests": 287,
          "recode_snapshot": client.snapshot_id, "remote_schema_retrieval": "disabled"}
(Path(__file__).parent / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({k: v for k, v in result.items() if k != "checked"}))
