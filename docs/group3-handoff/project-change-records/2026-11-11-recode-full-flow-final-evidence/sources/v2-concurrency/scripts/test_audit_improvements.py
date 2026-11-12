"""Deterministic regressions for audit-discovered coverage and cycle gaps."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "prototype"), str(ROOT), str(ROOT / "scripts")]
from analyze_sync_flow import build_candidates, _bounded_lock_cycles, _contexts_may_overlap
from coordinator.pipeline import prepare_reviews, collect_reviews, validate_review, PipelineError
from patching.report import aggregate_run_result
from run_redis_protocol_regression import verify
import make_redis_docker_plan
from check_agent_profiles import check
from run_redis_docker_verification import _stage, MAX_OUTPUT_CHARS
from impact.lock_scope import apply_lock_scope
from validate_local import _fixture_review


def response(pairs):
    fixture = json.loads((ROOT / "fixtures/LK-001-race/sync-flow.json").read_text(encoding="utf-8"))
    locks = sorted({item for pair in pairs for item in pair})
    edges = [{"from_lock_id": a, "to_lock_id": b,
              "observed_at": {"file": "src/worker.c", "line": i + 1},
              "function": "worker_" + str(i), "blocking": True,
              "execution_context": {"context_id": "ctx" + str(i), "thread": "thread" + str(i)},
              "evidence_ids": ["cycle-" + str(i)]} for i, (a, b) in enumerate(pairs)]
    fixture["evidence"] = [{"id": "cycle-" + str(i), "kind": "lock_operation",
                             "location": {"file": "src/worker.c", "line": i + 1}}
                            for i in range(len(pairs))]
    fixture["findings"] = [{"kind": "sync_flow", "shared_objects": [], "locks": [
        {"lock_id": lock, "kind": "mutex"} for lock in locks], "lock_order_edges": edges}]
    fixture["coverage"] = {"complete": True}
    fixture["status"] = "succeeded"
    return fixture


class AuditImprovementTests(unittest.TestCase):
    def test_two_and_three_lock_cycles(self):
        for pairs in ([('a', 'b'), ('b', 'a')], [('a', 'b'), ('b', 'c'), ('c', 'a')]):
            result = build_candidates(response(pairs))
            self.assertEqual(len(result["candidates"]), 1)
            candidate = result["candidates"][0]
            self.assertEqual(candidate["kind"], "lock_order_deadlock")
            self.assertEqual(len(candidate["lock_order"]["cycle_edges"]), len(pairs))

    def test_lock_only_finding_survives_scope_filter(self):
        raw = response([('a', 'b'), ('b', 'a')])
        scope = {'locks': raw['findings'][0]['locks'],
                 'lock_order_edges': raw['findings'][0]['lock_order_edges'],
                 'coverage': {'complete': True}, 'evidence_ids': []}
        scoped = apply_lock_scope(raw, raw, scope)
        self.assertEqual(len(scoped['findings']), 1)
        self.assertEqual(len(build_candidates(scoped)['candidates']), 1)

    def test_head_only_producer_retains_host_base_binding_for_patch_contract(self):
        raw = response([('a', 'b'), ('b', 'a')])
        repository = copy.deepcopy(raw['analysis_context']['repository'])
        raw['analysis_context']['repository'].pop('base_sha', None)
        raw['analysis_context']['repository']['commit_sha'] = repository['head_sha']
        raw['analysis_context']['repository'].pop('head_sha', None)
        scope = {'repository': repository, 'locks': raw['findings'][0]['locks'],
                 'lock_order_edges': raw['findings'][0]['lock_order_edges'],
                 'coverage': {'complete': True}, 'evidence_ids': []}
        scoped = apply_lock_scope(raw, raw, scope)
        self.assertEqual(repository['base_sha'], scoped['analysis_context']['repository']['base_sha'])
        self.assertEqual(repository['head_sha'], scoped['analysis_context']['repository']['head_sha'])
        self.assertEqual(repository['base_sha'], build_candidates(scoped)['repository']['base_sha'])
        self.assertNotIn('base_sha', raw['analysis_context']['repository'])

    def test_edge_order_does_not_change_candidate(self):
        raw = response([('a', 'b'), ('b', 'a')])
        original = build_candidates(raw)["candidates"]
        raw["findings"][0]["lock_order_edges"].reverse()
        self.assertEqual(original, build_candidates(raw)["candidates"])

    def cycle_review(self):
        raw = response([('a', 'b'), ('b', 'a')])
        head = raw['analysis_context']['repository']['head_sha']
        request = prepare_reviews(raw, source_units=[{'path': 'src/worker.c', 'revision': head,
                                                      'content': '/* test source */'}])['review_requests'][0]
        review = _fixture_review(request)
        review['same_object'] = None  # A cycle spans distinct locks, not one data object.
        review['lock_order'] = {'applicable': True, 'reachable_cycle': True,
                                'blocking': True, 'may_overlap': True}
        return request, review

    def test_multilock_review_accepts_inapplicable_same_object_gate(self):
        request, review = self.cycle_review()
        validate_review(request, review)

    def test_deadlock_requires_blocking_and_overlap(self):
        for field in ('blocking', 'reachable_cycle', 'may_overlap'):
            request, review = self.cycle_review()
            review['lock_order'][field] = None
            with self.assertRaises(PipelineError):
                validate_review(request, review)

    def test_access_context_cannot_be_silently_rewritten(self):
        request, review = self.cycle_review()
        review['access_a']['execution_context']['thread'] = 'invented'
        with self.assertRaises(PipelineError):
            validate_review(request, review)

    def test_serialized_cycle_is_excluded(self):
        raw = response([('a', 'b'), ('b', 'a')])
        for edge in raw["findings"][0]["lock_order_edges"]:
            edge["execution_context"] = {"context_id": "same", "reentrant": False}
        self.assertEqual(build_candidates(raw)["candidates"], [])

    def test_unknown_caller_and_blocking_stay_unresolved(self):
        raw = response([('a', 'b'), ('b', 'a')])
        edge = raw["findings"][0]["lock_order_edges"][0]
        edge["execution_context"]["thread"] = "unknown_external_caller"
        del edge["function"]
        del edge["blocking"]
        candidate = build_candidates(raw)["candidates"][0]
        self.assertEqual(candidate["candidate_status"], "needs_more_context")
        self.assertIsNone(candidate["may_concurrent"])
        self.assertEqual(candidate["access_a"]["function"], "")

    def test_large_dag_does_not_recurse(self):
        raw = response([(f'{i:04d}', f'{i+1:04d}') for i in range(2000)])
        cycles, truncated = _bounded_lock_cycles(raw["findings"][0]["lock_order_edges"])
        self.assertEqual(cycles, [])
        self.assertTrue(truncated)

    def test_budget_degrades_coverage(self):
        raw = response([('a', 'b'), ('b', 'a')])
        with patch('analyze_sync_flow._bounded_lock_cycles', return_value=([], True)):
            state = prepare_reviews(raw)
        self.assertEqual(state["status"], "partial")
        self.assertIn("lock_order_cycle_search_budget", state["coverage"]["unresolved"])

    def test_incomplete_zero_candidates_remain_partial_through_aggregation(self):
        raw = response([('a', 'b')])
        raw["coverage"] = {"complete": False, "reason": "missing_callback"}
        state = prepare_reviews(raw)
        self.assertEqual(state["status"], "partial")
        result = collect_reviews(state, [])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(aggregate_run_result(result, {}, {})["status"], "partial")

    def test_complete_zero_candidates_remain_completed(self):
        state = prepare_reviews(response([('a', 'b')]))
        self.assertEqual(state["status"], "completed")
        self.assertEqual(aggregate_run_result(collect_reviews(state, []), {}, {})["status"], "completed")

    def test_invalid_scenario_never_calls_docker(self):
        with patch('subprocess.run') as run:
            with self.assertRaises(ValueError):
                verify(Path('.'), 'image', 'arbitrary-shell')
            run.assert_not_called()

    def test_long_build_logs_are_preserved_outside_bounded_summary(self):
        text = 'begin\n' + ('middle' * MAX_OUTPUT_CHARS) + '\nend'
        with tempfile.TemporaryDirectory() as temporary:
            with patch('run_redis_docker_verification._run',
                       return_value=subprocess.CompletedProcess([], 0, text, 'warning')):
                result = _stage('test:unit/info', ['approved-command'], timeout=1,
                                log_directory=Path(temporary))
            self.assertTrue(result['output_truncated'])
            self.assertEqual(Path(result['stdout_log']).read_text(encoding='utf-8'), text)
            self.assertLessEqual(len(result['stdout']), MAX_OUTPUT_CHARS)

    def test_runtime_failure_is_not_passed(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch('subprocess.check_output', side_effect=[b'a'*40, b'diff', 'sha256:image', b'diff', b'a'*40]), \
                 patch('subprocess.run', side_effect=[subprocess.CompletedProcess([], 42, 'failure', ''),
                                                     subprocess.CompletedProcess([], 0)]):
                result = verify(Path(temporary), 'image', 'condition')
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['diff_unchanged'])

    def test_runtime_timeout_cleans_only_own_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch('subprocess.check_output', side_effect=[b'a'*40, b'diff', 'sha256:image', b'diff', b'a'*40]), \
                 patch('subprocess.run', side_effect=[subprocess.TimeoutExpired('docker', 60, b'output'),
                                                     subprocess.CompletedProcess([], 0)]) as run:
                result = verify(Path(temporary), 'image', 'condition')
                launched_name = run.call_args_list[0].args[0][3]
                self.assertEqual(run.call_args_list[1].args[0], ['docker', 'rm', '-f', launched_name])
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['timed_out'])

    def test_duplicate_edges_do_not_duplicate_candidates(self):
        raw = response([('a', 'b'), ('b', 'a')])
        raw['findings'][0]['lock_order_edges'] *= 2
        self.assertEqual(len(build_candidates(raw)['candidates']), 1)

    def test_opt_in_plan_and_full_log_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'plan.json'
            with patch('sys.argv', ['plan', '--image', 'sha256:test', '--output', str(path),
                                    '--regression', 'condition', '--regression', 'deadlock']):
                self.assertEqual(make_redis_docker_plan.main(), 0)
            plan = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(len(plan['checks']), 4)
            self.assertTrue(all('--output' in item['argv'] for item in plan['checks'][1:]))

    def test_missing_workspace_profiles_report_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(check(Path(temporary))['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
