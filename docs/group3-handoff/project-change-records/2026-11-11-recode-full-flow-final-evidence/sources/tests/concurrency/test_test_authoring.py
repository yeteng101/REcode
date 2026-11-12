from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "v2-concurrency"))
import test_verification_scope as fixtures
from patching.test_tasks import prepare_test_request, capture_test_artifact, assert_artifact, plan_with_generated_test
from patching.plan import verification_plan_sha256
from orchestrator import workflow


class TestAuthoringTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.VerificationScopeTests()
        self.fixture.setUp()
        f = self.fixture
        self.root = f.root
        self.source, self.normal = self.root / "source", self.root / "normal"
        for path in (self.source, self.normal):
            f.git("worktree", "add", "--detach", str(path), f.repository["head_sha"])
        self.policy = {"schema_version": "lock-race-test-policy/0.1", "normal_repository": str(self.normal),
                       "normal_head_sha": f.repository["head_sha"], "image_id": "sha256:" + "a" * 64}
        self.review_request = {"candidate_id": "C-1", "repository": f.repository, "source_units": [{"path": "src/bio.c"}]}
        self.review = {"candidate_id": "C-1", "verdict": "defect"}
        self.request = prepare_test_request(self.review_request, self.review, f.scope, f.assessment(), self.policy,
                                            self.source, self.root / "tests", product_workspace=f.repo)

    def tearDown(self): self.fixture.tearDown()

    def author(self):
        root = Path(self.request["workspace"]["path"])
        (root / "driver.c").write_text("int main(void) {return 0;}\n")
        case = {"schema_version": "lock-race-test-case/0.1", "candidate_id": "C-1", "template": "redis_bio_c",
                "supported_protocols": ["mutex", "atomic"], "covers": ["counter_read"],
                "limitations": ["Unit fixture only; not a discriminating test."], "rationale": "Test capture contract."}
        (root / "test-case.json").write_text(json.dumps(case))
        return {"schema_version": "lock-race-test-worker-report/0.1", "candidate_id": "C-1", "status": "authored",
                "changed_files": ["driver.c", "test-case.json"], "summary": "Authored test", "verification_status": "not_run"}

    def test_captures_real_files_and_rejects_later_tampering(self):
        artifact = capture_test_artifact(self.request, self.author())
        assert_artifact(artifact)
        (Path(artifact["workspace"]) / "driver.c").write_text("changed")
        with self.assertRaisesRegex(ValueError, "hash changed"):
            assert_artifact(artifact)

    def test_host_selected_generic_c_template_is_bound_to_artifact(self):
        policy = {"schema_version": "lock-race-test-policy/0.2", "image_id": self.policy["image_id"],
                  "repetitions": 3, "template": "c_source"}
        self.request = prepare_test_request(self.review_request, self.review, self.fixture.scope,
            self.fixture.assessment(), policy, self.source, self.root / "generic-tests")
        self.assertEqual("c_source", self.request["execution_template"]["name"])
        self.assertEqual(["/source"], self.request["execution_template"]["include_paths"])
        self.assertNotIn("src/atomicvar.h", self.request["constraints"]["readable_source_files"])
        report = self.author()
        case_path = Path(self.request["workspace"]["path"]) / "test-case.json"
        case = json.loads(case_path.read_text())
        case["template"] = "c_source"
        case_path.write_text(json.dumps(case))
        artifact = capture_test_artifact(self.request, report)
        assert_artifact(artifact)
        self.assertEqual("c_source", artifact["test_case"]["template"])

    def test_test_worker_cannot_switch_host_selected_template(self):
        report = self.author()
        case_path = Path(self.request["workspace"]["path"]) / "test-case.json"
        case = json.loads(case_path.read_text())
        case["template"] = "c_source"
        case_path.write_text(json.dumps(case))
        with self.assertRaisesRegex(ValueError, "fixed-template"):
            capture_test_artifact(self.request, report)

    def test_feedback_revision_preserves_both_test_snapshots(self):
        report = self.author()
        first = capture_test_artifact(self.request, report)
        workspace = Path(self.request["workspace"]["path"])
        revised = prepare_test_request(self.review_request, self.review, self.fixture.scope, self.fixture.assessment(),
            self.policy, self.source, workspace, attempt=2, feedback={"prior_artifact_files": first["files"]})
        (workspace / "driver.c").write_text("int main(void) {return 42;}\n")
        report["changed_files"] = ["driver.c"]
        second = capture_test_artifact(revised, report)
        self.assertNotEqual(first["workspace"], second["workspace"])
        assert_artifact(first)
        assert_artifact(second)

    def test_captured_coverage_metadata_cannot_diverge_from_hashed_case_file(self):
        artifact = capture_test_artifact(self.request, self.author())
        artifact["test_case"]["covers"].append("invented_coverage")
        with self.assertRaisesRegex(ValueError, "metadata differs"):
            assert_artifact(artifact)

    def test_existing_product_patch_is_bounded_and_hash_bound_for_test_review(self):
        patch = self.request["product_patch"]
        self.assertEqual(self.fixture.captured["diff_sha256"], patch["diff_sha256"])
        self.assertIn("+after", patch["current_diff"])
        self.assertFalse(patch["current_diff_truncated"])

    def test_product_edit_is_rejected(self):
        report = self.author()
        (self.fixture.repo / "source.c").write_text("unauthorized")
        with self.assertRaisesRegex(ValueError, "protected product"):
            capture_test_artifact(self.request, report)

    def test_arbitrary_test_runner_or_command_is_rejected(self):
        report = self.author()
        root = Path(self.request["workspace"]["path"])
        (root / "run.py").write_text("pass")
        with self.assertRaisesRegex(ValueError, "unapproved"):
            capture_test_artifact(self.request, report)
        (root / "run.py").unlink()
        case = json.loads((root / "test-case.json").read_text())
        case["argv"] = ["anything"]
        (root / "test-case.json").write_text(json.dumps(case))
        with self.assertRaisesRegex(ValueError, "fixed-template"):
            capture_test_artifact(self.request, report)

    def test_coverage_overclaim_or_wrong_candidate_is_rejected(self):
        report = self.author()
        self.request["verification_scope"]["required_coverage"] = ["counter_writers"]
        with self.assertRaisesRegex(ValueError, "requested protocol/coverage"):
            capture_test_artifact(self.request, report)
        report["candidate_id"] = "other"
        with self.assertRaisesRegex(ValueError, "invalid test-worker"):
            capture_test_artifact(self.request, report)

    def test_refusal_requires_no_edits(self):
        report = self.author()
        report.update(status="refused", changed_files=[])
        with self.assertRaisesRegex(ValueError, "preserve"):
            capture_test_artifact(self.request, report)

    def test_revision_refusal_preserves_first_test_and_has_independent_budget(self):
        report = self.author()
        artifact = capture_test_artifact(self.request, report)
        revised = prepare_test_request(self.review_request, self.review, self.fixture.scope, self.fixture.assessment(),
            self.policy, self.source, Path(self.request["workspace"]["path"]), attempt=2,
            feedback={"prior_artifact_files": artifact["files"]})
        report.update(status="refused", changed_files=[])
        self.assertEqual("refused", capture_test_artifact(revised, report)["status"])
        with self.assertRaisesRegex(ValueError, "one initial"):
            prepare_test_request(self.review_request, self.review, self.fixture.scope, {}, self.policy, self.source,
                                 self.root / "third", attempt=3)

    def test_plan_replacement_preserves_nondefect_checks_and_does_not_execute_model_argv(self):
        artifact = capture_test_artifact(self.request, self.author())
        path = self.root / "artifact.json"
        path.write_text(json.dumps(artifact))
        original = copy.deepcopy(self.fixture.plan)
        scope = copy.deepcopy(self.fixture.scope)
        check = {"name": "official", "argv": ["known-official-runner"], "timeout_seconds": 5}
        original["checks"].insert(0, check)
        declaration = dict(copy.deepcopy(scope["checks"][0]), name="official", purpose="official_regression", covers=[], supported_protocols=["any"])
        scope["checks"].insert(0, declaration)
        scope["verification_plan_sha256"] = verification_plan_sha256(original)
        plan, new_scope = plan_with_generated_test(original, scope, path, self.policy["image_id"], self.root / "logs")
        self.assertEqual(check, plan["checks"][0])
        self.assertEqual(["official", "generated-defect-regression"], [c["name"] for c in plan["checks"]])
        self.assertEqual("run_generated_redis_test.py", Path(plan["checks"][1]["argv"][1]).name)
        self.assertEqual(["counter_read"], new_scope["required_coverage"])
        self.assertEqual(2, len(original["checks"]))

    def make_workflow(self, *, mismatch=True):
        f = self.fixture
        coordinator = self.root / "coordinator"
        (coordinator / "review-requests").mkdir(parents=True)
        (coordinator / "review-requests/C-1.json").write_text(json.dumps(self.review_request))
        state = {"schema_version": "lock-race-coordinator-state/0.1", "review_requests": [self.review_request], "repository": f.repository}
        (coordinator / "coordinator-state.json").write_text(json.dumps(state))
        (self.root / "plan.json").write_text(json.dumps(f.plan))
        (self.root / "policy.json").write_text(json.dumps(self.policy))
        scopes = self.root / "scopes"
        scopes.mkdir()
        scope = copy.deepcopy(f.scope)
        scope["checks"][0]["supported_protocols"] = ["mutex"] if mismatch else ["mutex", "atomic"]
        (scopes / "C-1.json").write_text(json.dumps(scope))
        run = workflow.create_run(run_id="test-author", coordinator_workspace=coordinator, source_repository=self.source,
            patch_root=self.root / "patches", verification_plan_path=self.root / "plan.json", workflow_directory=self.root / "workflow",
            verification_scopes_directory=scopes, test_policy_path=self.root / "policy.json", test_strategy="legacy_optional")
        result = {"schema_version": "lock-race-coordinator-result/0.1", "status": "completed", "repository": f.repository,
                  "reviews": [self.review], "coverage": {"complete": True}}
        with patch.object(workflow, "validate_review"), patch.object(workflow, "collect_reviews", return_value=result):
            workflow.submit_reviewer_result(run["state_path"], self.review)
        run = workflow.run_until_agent_boundary(run["state_path"])
        self.request = json.loads(Path(run["agent_task"]["request_path"]).read_text())
        return run

    def test_applicable_scope_does_not_add_a_test_agent_call(self):
        request = {"candidate_id": "C-1", "repository": self.fixture.repository,
                   "workspace": {"path": str(self.root / "product")}}
        with patch.object(workflow, "prepare_patch_request", return_value=request):
            run = self.make_workflow(mismatch=False)
        self.assertEqual("invoke_lock_race_patch_worker", run["next_action"])
        self.assertEqual({}, run["test_attempts_used"])
        self.assertEqual({"C-1": 1}, run["patch_attempts_used"])

    def test_workflow_stops_at_new_model_boundary_and_rejects_stale_requests(self):
        run = self.make_workflow()
        self.assertEqual("invoke_lock_race_test_worker", run["next_action"])
        self.assertEqual({}, run["patch_attempts_used"])
        self.assertEqual(run, workflow.run_until_agent_boundary(run["state_path"]))
        report = self.author()
        path = Path(run["agent_task"]["request_path"])
        path.write_text(path.read_text().replace('"attempt": 1', '"attempt": 2'))
        with self.assertRaisesRegex(workflow.WorkflowError, "changed after dispatch"):
            workflow.submit_test_worker_result(run["state_path"], report)

    def test_discrimination_success_creates_new_plan_before_patch_worker(self):
        run = self.make_workflow()
        original_plan = (self.root / "plan.json").read_bytes()
        run = workflow.submit_test_worker_result(run["state_path"], self.author())
        self.assertEqual("validate_test", run["next_action"])
        validation = {"status": "passed", "candidate_id": "C-1", "versions": [], "retry_test_allowed": True}
        with patch.object(workflow, "discriminate_test", return_value=validation):
            run = workflow.run_deterministic_step(run["state_path"])
        self.assertEqual("prepare_patch_task", run["next_action"])
        self.assertEqual({}, run["patch_attempts_used"])
        self.assertEqual({"C-1": 1}, run["test_attempts_used"])
        self.assertEqual(original_plan, (self.root / "plan.json").read_bytes())
        self.assertIn("C-1", run["candidate_verification_plans"])

    def test_two_unsound_tests_stop_without_resetting_patch_budget(self):
        run = self.make_workflow()
        for attempt in (1, 2):
            self.request = json.loads(Path(run["agent_task"]["request_path"]).read_text())
            report = self.author()
            if attempt == 2:
                (Path(self.request["workspace"]["path"]) / "driver.c").write_text("int main(void) {return 0;} /* still unsound */\n")
                report["changed_files"] = ["driver.c"]
            run = workflow.submit_test_worker_result(run["state_path"], report)
            failure_path = self.root / ("failed-" + str(attempt) + ".json")
            failure_path.write_text(json.dumps({"exit_code": 0, "stdout": "not discriminating"}))
            failed = {"status": "failed", "versions": [{"matched": False, "version": "defective", "result_path": str(failure_path)}],
                      "retry_test_allowed": True}
            with patch.object(workflow, "discriminate_test", return_value=failed):
                run = workflow.run_deterministic_step(run["state_path"])
        run = workflow.run_until_agent_boundary(run["state_path"])
        self.assertEqual("partial", run["phase"])
        self.assertEqual({"C-1": 2}, run["test_attempts_used"])
        self.assertEqual({}, run["patch_attempts_used"])

    def test_host_semantic_finding_uses_one_revision_without_claiming_a_runtime_failure(self):
        run = self.make_workflow()
        run = workflow.submit_test_worker_result(run["state_path"], self.author(), ["Test reinitializes a potentially static-initialized product mutex."])
        with patch.object(workflow, "discriminate_test") as execute:
            run = workflow.run_deterministic_step(run["state_path"])
            execute.assert_not_called()
        self.assertEqual("invoke_lock_race_test_worker", run["next_action"])
        self.assertEqual({"C-1": 2}, run["test_attempts_used"])
        request = json.loads(Path(run["agent_task"]["request_path"]).read_text())
        self.assertEqual("not_run", request["feedback"]["validation"]["versions"][0]["status"])
        self.assertTrue(request["feedback"]["host_review"]["findings"])

if __name__ == "__main__": unittest.main()
