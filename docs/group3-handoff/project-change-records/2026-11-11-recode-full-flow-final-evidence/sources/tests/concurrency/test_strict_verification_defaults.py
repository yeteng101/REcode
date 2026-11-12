"""Strict creation defaults, explicit compatibility, and persisted recovery guards."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "v2-concurrency"))
import test_verification_scope as fixtures
from orchestrator import workflow
from mcp import server
from patching import pipeline
from patching.verifier import verify_patch


class StrictVerificationDefaultsTest(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.VerificationScopeTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        f = self.f
        self.coordinator = f.root / "coordinator"
        self.coordinator.mkdir()
        workflow._write(self.coordinator / "coordinator-state.json", {
            "repository": f.repository, "review_requests": [{"candidate_id": "C-1"}]})
        self.plan = f.root / "plan.json"
        workflow._write(self.plan, f.plan)
        self.scopes = f.root / "scopes"
        workflow._write(self.scopes / "C-1.json", f.scope)
        self.kw = dict(run_id="strict-default", coordinator_workspace=self.coordinator,
                       source_repository=f.repo, patch_root=f.root / "patches",
                       verification_plan_path=self.plan, workflow_directory=f.root / "workflow", test_strategy="legacy_optional")

    def test_missing_scope_is_rejected_before_creation(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "requires verification_scopes_directory"):
            workflow.create_run(**self.kw)
        self.assertFalse(self.kw["workflow_directory"].exists())
        self.assertFalse(self.kw["patch_root"].exists())

    def test_strict_is_persisted_and_resumable(self):
        state = workflow.create_run(**self.kw, verification_scopes_directory=self.scopes)
        self.assertEqual("strict", state["verification_mode"])
        self.assertEqual(state, workflow.get_run(state["state_path"]))

    def test_explicit_legacy_opt_in_is_persisted(self):
        state = workflow.create_run(**self.kw, allow_legacy_unscoped=True)
        self.assertEqual("legacy_unscoped", state["verification_mode"])
        self.assertNotIn("verification_scopes", state)

    def test_scope_and_legacy_flag_cannot_be_mixed(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "mutually exclusive"):
            workflow.create_run(**self.kw, verification_scopes_directory=self.scopes, allow_legacy_unscoped=True)

    def test_truthy_values_do_not_enable_legacy_mode(self):
        for bad in ("true", "false", 1, None):
            with self.subTest(bad=bad), self.assertRaisesRegex(workflow.WorkflowError, "must be a boolean"):
                server.call_tool("create_lock_race_run", dict(self.kw, allow_legacy_unscoped=bad))

    def test_incomplete_candidate_scopes_are_rejected(self):
        workflow._write(self.coordinator / "coordinator-state.json", {
            "repository": self.f.repository,
            "review_requests": [{"candidate_id": "C-1"}, {"candidate_id": "C-2"}]})
        with self.assertRaisesRegex(workflow.WorkflowError, "unable to read JSON"):
            workflow.create_run(**self.kw, verification_scopes_directory=self.scopes)
        self.assertFalse(self.kw["workflow_directory"].exists())

    def test_strict_state_cannot_silently_lose_scopes(self):
        state = workflow.create_run(**self.kw, verification_scopes_directory=self.scopes)
        state.pop("verification_scopes")
        workflow._write(Path(state["state_path"]), state)
        with self.assertRaisesRegex(workflow.WorkflowError, "scope for every candidate"):
            workflow.get_run(state["state_path"])

    def test_mode_less_historical_state_remains_readable_without_rewrite(self):
        state = workflow.create_run(**self.kw, allow_legacy_unscoped=True)
        state.pop("verification_mode")
        path = Path(state["state_path"])
        workflow._write(path, state)
        before = path.read_bytes()
        restored = workflow.get_run(path)
        self.assertNotIn("verification_mode", restored)
        self.assertEqual(before, path.read_bytes())

    def test_patch_preparation_rejects_unscoped_default_before_git(self):
        with patch.object(pipeline, "_git") as git:
            with self.assertRaisesRegex(pipeline.PatchPipelineError, "requires verification_scope"):
                pipeline.prepare_patch_request({}, {}, self.f.repo, self.f.root / "product", self.f.plan)
            git.assert_not_called()

    def test_patch_preparation_rejects_missing_base_before_git(self):
        request = {"candidate_id": "C-1", "repository": dict(self.f.repository, base_sha=None), "provenance": {}}
        review = {"status": "probable", "verdict": "defect"}
        with patch.object(pipeline, "validate_review"), patch.object(pipeline, "_git") as git:
            with self.assertRaisesRegex(pipeline.PatchPipelineError, "no full base SHA"):
                pipeline.prepare_patch_request(request, review, self.f.repo, self.f.root / "invalid-base", self.f.plan,
                                               allow_legacy_unscoped=True)
            git.assert_not_called()
        self.assertFalse((self.f.root / "invalid-base").exists())

    def test_new_patch_requests_persist_strict_or_explicit_legacy(self):
        f = self.f
        request = {"candidate_id": "C-1", "repository": f.repository, "provenance": {},
                   "evidence": [{"id": "ev-1", "location": {"file": "source.c"}}]}
        review = {"status": "probable", "verdict": "defect", "evidence_ids": ["ev-1"]}
        with patch.object(pipeline, "validate_review"):
            strict = pipeline.prepare_patch_request(request, review, f.repo, f.root / "product-strict", f.plan, f.scope, test_strategy="legacy_optional")
            legacy = pipeline.prepare_patch_request(request, review, f.repo, f.root / "product-legacy", f.plan,
                                                     allow_legacy_unscoped=True)
        self.assertEqual("strict", strict["verification_mode"])
        self.assertEqual("legacy_unscoped", legacy["verification_mode"])
        self.assertNotIn("verification_scope", legacy)

    def test_strict_request_missing_scope_is_rejected_before_execution(self):
        request = dict(self.f.request, verification_mode="strict")
        with self.assertRaisesRegex(pipeline.PatchPipelineError, "requires a verification scope"):
            verify_patch(request, self.f.captured)

    def test_explicit_legacy_request_cannot_hide_a_scope(self):
        request = dict(self.f.scoped_request(), verification_mode="legacy_unscoped")
        with self.assertRaisesRegex(pipeline.PatchPipelineError, "cannot contain a verification scope"):
            verify_patch(request, self.f.captured)

    def test_unknown_strict_failure_blocks_product_revision(self):
        self.f.helper.write_text("raise SystemExit(33)\n")
        self.f.refresh_helper_hash()
        request = dict(self.f.scoped_request(), verification_mode="strict")
        result = verify_patch(request, self.f.captured)
        self.assertFalse(result["diagnostics"]["patch_revision_allowed"])
        self.assertEqual("review_verification", result["diagnostics"]["next_action"])
        with self.assertRaisesRegex(pipeline.PatchPipelineError, "not patch revision"):
            pipeline.prepare_patch_revision_request(request, self.f.captured, result)

    def test_explicit_legacy_and_historical_requests_keep_old_semantics(self):
        self.f.helper.write_text("raise SystemExit(33)\n")
        for request in (self.f.request, dict(self.f.request, verification_mode="legacy_unscoped")):
            with self.subTest(mode=request.get("verification_mode", "historical")):
                result = verify_patch(request, self.f.captured)
                self.assertEqual("legacy_unscoped", result["diagnostics"]["mode"])
                self.assertTrue(result["diagnostics"]["patch_revision_allowed"])
                self.assertTrue(result["diagnostics"]["limitations"])

    def test_strict_revision_cannot_use_missing_or_legacy_diagnostics(self):
        self.f.helper.write_text("raise SystemExit(7)\n")
        self.f.refresh_helper_hash()
        request = dict(self.f.scoped_request(), verification_mode="strict")
        verified = verify_patch(request, self.f.captured)
        self.assertTrue(verified["diagnostics"]["patch_revision_allowed"])
        self.assertEqual(2, pipeline.prepare_patch_revision_request(request, self.f.captured, verified)["revision_feedback"]["attempt"])
        for bad in (None, {"mode": "legacy_unscoped", "patch_revision_allowed": True}, {}):
            changed = copy.deepcopy(verified)
            changed["diagnostics"] = bad
            with self.subTest(diagnostics=bad), self.assertRaisesRegex(pipeline.PatchPipelineError, "classified code-failure"):
                pipeline.prepare_patch_revision_request(request, self.f.captured, changed)

    def test_invalid_mode_values_are_structured_errors(self):
        for mode in (None, [], {}, "unknown"):
            with self.subTest(mode=mode), self.assertRaisesRegex(pipeline.PatchPipelineError, "mode is invalid"):
                pipeline.validate_verification_mode_binding(dict(self.f.request, verification_mode=mode))

    def test_mcp_default_rejects_missing_scope_and_advertises_boolean_opt_in(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "requires verification_scopes_directory"):
            server.call_tool("create_lock_race_run", self.kw)
        tool = next(t for t in server.TOOLS if t["name"] == "create_lock_race_run")
        self.assertEqual({"type": "boolean", "default": False},
                         tool["inputSchema"]["properties"]["allow_legacy_unscoped"])

    def test_cli_default_rejects_and_explicit_legacy_creates(self):
        argv = [sys.executable, str(ROOT / "v2-concurrency/scripts/run_lock_race_workflow.py"), "create"]
        for name, value in self.kw.items():
            cli_name = "verification-plan" if name == "verification_plan_path" else name.replace("_", "-")
            argv.extend(["--" + cli_name, str(value)])
        denied = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(2, denied.returncode, denied.stdout + denied.stderr)
        self.assertIn("requires verification_scopes_directory", denied.stdout)
        allowed = subprocess.run(argv + ["--allow-legacy-unscoped"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, allowed.returncode, allowed.stdout + allowed.stderr)
        self.assertEqual("legacy_unscoped", json.loads(allowed.stdout)["verification_mode"])


if __name__ == "__main__":
    unittest.main()
