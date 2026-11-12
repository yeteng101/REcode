"""Regressions for cross-flow pairing and evidence-based exclusion decisions."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'prototype')]
from analyze_sync_flow import build_candidates
from coordinator.pipeline import prepare_reviews, validate_review, PipelineError
from impact.lock_scope import build_lock_scope, apply_lock_scope, ImpactScopeError
from validate_local import _fixture_review


def raw_response():
    return json.loads((ROOT/'fixtures/LK-001-race/sync-flow.json').read_text(encoding='utf-8'))


def split_response():
    raw = raw_response()
    flow = raw['findings'][0]
    a, b = copy.deepcopy(flow), copy.deepcopy(flow)
    a['id'], b['id'] = 'reader-flow', 'writer-flow'
    a['shared_objects'][0]['accesses'] = [a['shared_objects'][0]['accesses'][0]]
    b['shared_objects'][0]['accesses'] = [b['shared_objects'][0]['accesses'][1]]
    raw['findings'] = [a,b]
    return raw


def accesses(raw):
    return raw['findings'][0]['shared_objects'][0]['accesses']


def scope_for(raw):
    raw = copy.deepcopy(raw)
    raw['change_context'] = {'changed_locations':[{'file':'src/command.c','start_line':51,'end_line':51}]}
    calls = {'schema_version':'1.0','status':'succeeded','analysis_context':raw['analysis_context'],
             'coverage':{'complete':True},'evidence':[], 'findings':[{'kind':'call_chain','root_node_id':'read',
             'nodes':[{'node_id':'read','name':'command_read_counter',
                       'location':{'file':'src/command.c','line':51}}], 'edges':[]}]}
    context = raw['analysis_context']
    snapshot = {'repository':dict(context['repository'],build_profile=context['build_profile'])}
    return raw, calls, build_lock_scope(snapshot,calls,raw)


class CrossFlowPairingTests(unittest.TestCase):
    def test_scope_schema_describes_emitted_local_limits(self):
        _, _, scope = scope_for(raw_response())
        schema = json.loads((ROOT / 'schemas/lock-race-impact-scope.schema.json').read_text(encoding='utf-8'))
        coverage_schema = schema['properties']['coverage']
        self.assertFalse(coverage_schema['additionalProperties'])
        self.assertTrue(set(scope['coverage']) <= set(coverage_schema['properties']))
        limits_schema = coverage_schema['properties']['local_limits']
        self.assertFalse(limits_schema['additionalProperties'])
        self.assertEqual(set(scope['coverage']['local_limits']), set(limits_schema['required']))
        self.assertEqual(set(limits_schema['required']), set(limits_schema['properties']))
        properties = limits_schema['properties']
        self.assertEqual({'type': 'integer', 'minimum': 0}, properties['max_depth'])
        self.assertEqual({'type': 'integer', 'minimum': 0}, properties['cut_call_edge_count'])
        self.assertEqual(128, properties['cut_call_edge_ids']['maxItems'])
        self.assertTrue(properties['cut_call_edge_ids']['uniqueItems'])
        self.assertEqual('^[0-9a-f]{64}$', properties['cut_edge_ids_sha256']['pattern'])

    def test_split_and_combined_have_the_same_candidate_id(self):
        combined = build_candidates(raw_response())['candidates']
        raw = split_response()
        before = copy.deepcopy(raw)
        split = build_candidates(raw)['candidates']
        self.assertEqual(len(split), 1)
        self.assertEqual(split[0]['candidate_id'], combined[0]['candidate_id'])
        self.assertEqual(split[0]['shared_object']['source_finding_ids'], ['reader-flow','writer-flow'])
        sources = {access['access_id']:access['source_finding_ids']
                   for access in (split[0]['access_a'],split[0]['access_b'])}
        self.assertEqual(sources['access-client-read'], ['reader-flow'])
        self.assertEqual(sources['access-worker-write'], ['writer-flow'])
        self.assertEqual(raw, before)

    def test_flow_and_access_order_do_not_change_candidates(self):
        raw = split_response()
        expected = build_candidates(raw)['candidates']
        raw['findings'].reverse()
        self.assertEqual(expected, build_candidates(raw)['candidates'])

    def test_scope_follows_all_accesses_of_the_selected_object(self):
        raw,calls,scope = scope_for(split_response())
        self.assertTrue(scope['coverage']['complete'])
        self.assertEqual(scope['stats']['shared_access_count'], 2)
        self.assertEqual({item['name'] for item in scope['functions']},
                         {'command_read_counter','worker_update_counter'})
        self.assertEqual(scope['context_ids'], ['ctx-client-main','ctx-worker'])
        self.assertEqual(len(build_candidates(apply_lock_scope(raw,calls,scope))['candidates']), 1)

    def test_same_display_name_does_not_join_distinct_object_ids(self):
        raw = split_response()
        raw['findings'][1]['shared_objects'][0]['object_id'] = 'different-object'
        self.assertEqual(build_candidates(raw)['candidates'], [])

    def test_equivalent_declaration_line_shapes_are_accepted(self):
        raw = split_response()
        obj = raw['findings'][1]['shared_objects'][0]
        obj['declaration'] = {'file':'src/state.h','line':12}
        self.assertEqual(len(build_candidates(raw)['candidates']), 1)

    def test_conflicting_object_identity_fails_in_both_stages(self):
        raw = split_response()
        raw['findings'][1]['shared_objects'][0]['declaration']['start_line'] = 15
        raw['findings'][1]['shared_objects'][0]['declaration']['end_line'] = 15
        with self.assertRaisesRegex(ValueError,'shared object identity'):
            build_candidates(raw)
        with self.assertRaisesRegex(ImpactScopeError,'shared object identity'):
            scope_for(raw)

    def test_conflicting_access_context_is_not_union_merged(self):
        raw = raw_response()
        second = copy.deepcopy(raw['findings'][0])
        second['id'] = 'different-view'
        second['shared_objects'][0]['accesses'][0]['execution_context']['thread'] = 'other'
        raw['findings'].append(second)
        with self.assertRaisesRegex(ValueError,'shared access'):
            build_candidates(raw)

    def test_conflicting_access_must_locks_are_not_union_merged(self):
        raw = raw_response()
        second = copy.deepcopy(raw['findings'][0])
        second['shared_objects'][0]['accesses'][0]['must_lockset'] = ['invented-lock']
        raw['findings'].append(second)
        with self.assertRaisesRegex(ValueError,'shared access'):
            build_candidates(raw)

    def test_exact_duplicate_accesses_do_not_consume_pair_budget(self):
        raw = split_response()
        raw['findings'] *= 4
        result = build_candidates(raw, max_access_pairs=1)
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['discovery_budget']['examined_access_pairs'], 1)
        self.assertEqual(result['discovery_budget']['shared_object_join']['duplicate_accesses_removed'], 6)
        self.assertTrue(result['coverage']['complete'])

    def test_repeated_access_evidence_is_preserved(self):
        raw = split_response()
        extra = copy.deepcopy(raw['findings'][0])
        extra['id'] = 'another-observation'
        extra['shared_objects'][0]['accesses'][0]['evidence_ids'].append('extra')
        raw['evidence'].append({'id':'extra','kind':'shared_access','location':{'file':'src/command.c','line':51}})
        raw['findings'].append(extra)
        candidate = build_candidates(raw)['candidates'][0]
        self.assertIn('extra',candidate['evidence_ids'])

    def test_low_identity_confidence_is_not_upgraded(self):
        raw = split_response()
        raw['findings'][1]['shared_objects'][0]['identity_confidence'] = 0.4
        candidate = build_candidates(raw)['candidates'][0]
        self.assertEqual(candidate['candidate_status'], 'needs_more_context')
        self.assertIn('low_object_identity_confidence',candidate['coverage']['unresolved'])

    def test_incomplete_source_flow_is_not_promoted(self):
        raw = split_response()
        raw['findings'][1]['complete'] = False
        candidate = build_candidates(raw)['candidates'][0]
        self.assertIn('incomplete_source_flow:writer-flow',candidate['coverage']['unresolved'])

    def test_merged_access_budget_reports_cross_flow_backlog(self):
        raw = split_response()
        for index in range(3):
            flow = copy.deepcopy(raw['findings'][1])
            flow['id'] = f'writer-{index}'
            access = flow['shared_objects'][0]['accesses'][0]
            access['access_id'] = f'extra-{index}'
            access['execution_context'] = {'context_id':f'extra-{index}', 'thread':f'thread-{index}'}
            raw['findings'].append(flow)
        result = build_candidates(raw,max_access_pairs=2)
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['discovery_budget']['deferred_pair_ranges'][0]['remaining_pair_count'], 8)

    def test_common_lock_still_suppresses_split_healthy_pair(self):
        raw = split_response()
        raw['findings'][0]['shared_objects'][0]['accesses'][0]['must_lockset'] = [{'lock_id':'lock:state','mode':'exclusive'}]
        self.assertEqual(build_candidates(raw)['candidates'], [])

    def test_separate_lock_metadata_flow_survives_scope_filter(self):
        raw = split_response()
        lock = raw['findings'][0]['locks'][0]
        for flow in raw['findings']:
            flow['locks'] = []
            for access in flow['shared_objects'][0]['accesses']:
                access['must_lockset'] = [{'lock_id':'lock:state','mode':'exclusive'}]
        raw['findings'].append({'id':'lock-metadata','kind':'sync_flow','shared_objects':[],
                                'locks':[lock],'lock_order_edges':[]})
        enriched,calls,scope = scope_for(raw)
        scoped = apply_lock_scope(enriched,calls,scope)
        self.assertTrue(any(flow.get('id')=='lock-metadata' for flow in scoped['findings']))
        self.assertEqual(build_candidates(scoped)['candidates'], [])

    def test_condition_predicate_pair_can_be_split_across_flows(self):
        raw = split_response()
        raw['findings'][0]['condition_variables'] = [{'condition_id':'cv',
             'predicate_object_id':'state:server_state', 'wait':{'guard_kind':'if','evidence_ids':['ev-read']},
             'evidence_ids':['ev-read']}]
        kinds = {candidate['kind'] for candidate in build_candidates(raw)['candidates']}
        self.assertEqual(kinds, {'data_race','condition_variable_protocol'})

    def test_incomplete_join_is_preserved_for_condition_candidate(self):
        raw = split_response()
        raw['findings'][1]['complete'] = False
        raw['findings'][0]['condition_variables'] = [{'condition_id':'cv',
             'predicate_object_id':'state:server_state', 'wait':{'guard_kind':'if','evidence_ids':['ev-read']},
             'evidence_ids':['ev-read']}]
        candidates = build_candidates(raw)['candidates']
        self.assertTrue(all(candidate['candidate_status']=='needs_more_context' for candidate in candidates))

    def test_pair_identity_is_invariant_under_arbitrary_flow_partitions(self):
        raw = raw_response()
        obj = raw['findings'][0]['shared_objects'][0]
        template = obj['accesses'][1]
        obj['accesses'] = [dict(copy.deepcopy(template), access_id=str(index), must_lockset=[], may_lockset=[],
                          execution_context={'context_id':str(index),'thread':'t'+str(index)})
                          for index in range(6)]
        expected = {item['candidate_id'] for item in build_candidates(raw)['candidates']}
        self.assertEqual(len(expected), 15)
        for partitions in (([0,2,4],[1,3,5]), ([0],[1],[2],[3],[4],[5]), ([5,4],[3,2],[1,0])):
            split = copy.deepcopy(raw)
            split['findings'] = []
            for index,partition in enumerate(partitions):
                flow = copy.deepcopy(raw['findings'][0])
                flow['id'] = 'part-'+str(index)
                flow['shared_objects'][0]['accesses'] = [copy.deepcopy(obj['accesses'][item]) for item in partition]
                split['findings'].append(flow)
            self.assertEqual(expected, {item['candidate_id'] for item in build_candidates(split)['candidates']})


class ConservativeExclusionTests(unittest.TestCase):
    def candidate(self, raw, gap=None):
        result = build_candidates(raw)
        self.assertEqual(len(result['candidates']), 1)
        candidate = result['candidates'][0]
        self.assertEqual(candidate['candidate_status'], 'needs_more_context')
        self.assertFalse(candidate['coverage']['complete'])
        if gap:
            self.assertIn(gap,candidate['coverage']['unresolved'])
        return candidate

    def test_missing_reentrancy_for_same_context_is_unknown(self):
        raw = raw_response()
        for access in accesses(raw):
            access['execution_context'] = {'context_id':'same','thread':'main'}
        self.candidate(raw,'concurrency_relation_unknown')

    def test_missing_reentrancy_for_same_event_loop_is_unknown(self):
        raw = raw_response()
        for index,access in enumerate(accesses(raw)):
            access['execution_context'] = {'context_id':str(index),'event_loop':'loop'}
        self.candidate(raw,'concurrency_relation_unknown')

    def test_shared_thread_label_is_not_a_thread_instance_proof(self):
        raw = raw_response()
        for index,access in enumerate(accesses(raw)):
            access['execution_context'] = {'context_id':str(index),'thread':'worker_pool','reentrant':False}
        self.candidate(raw,'concurrency_relation_unknown')

    def test_explicit_non_reentrant_same_context_is_still_excluded(self):
        raw = raw_response()
        for access in accesses(raw):
            access['execution_context'] = {'context_id':'same','reentrant':False}
        self.assertEqual(build_candidates(raw)['candidates'], [])

    def test_explicit_non_reentrant_same_event_loop_is_still_excluded(self):
        raw = raw_response()
        for index,access in enumerate(accesses(raw)):
            access['execution_context'] = {'context_id':str(index),'event_loop':'loop','reentrant':False}
        self.assertEqual(build_candidates(raw)['candidates'], [])

    def test_conflicting_concurrency_evidence_is_not_silently_excluded(self):
        raw = raw_response()
        for index,access in enumerate(accesses(raw)):
            access['execution_context'] = {'context_id':'same','thread':str(index),'reentrant':False}
        self.candidate(raw,'contradictory_concurrency_facts')

    def test_conflicting_context_is_retained_even_with_a_common_lock(self):
        raw = raw_response()
        for index,access in enumerate(accesses(raw)):
            access['execution_context'] = {'context_id':'same','thread':str(index),'reentrant':False}
            access['must_lockset'] = [{'lock_id':'lock:state','mode':'exclusive'}]
        self.candidate(raw,'contradictory_concurrency_facts')

    def test_non_boolean_reentrancy_does_not_become_proof(self):
        for marker in (None,0,'false'):
            with self.subTest(marker=marker):
                raw = raw_response()
                for access in accesses(raw):
                    access['execution_context'] = {'context_id':'same','reentrant':marker}
                self.candidate(raw,'concurrency_relation_unknown')

    def test_explicit_overlap_beats_no_implicit_same_context_assumption(self):
        raw = raw_response()
        for access in accesses(raw):
            access['execution_context'] = {'context_id':'same','may_run_concurrently_with':['same']}
        candidate = build_candidates(raw)['candidates'][0]
        self.assertTrue(candidate['may_concurrent'])
        self.assertEqual(candidate['candidate_status'],'ready_for_review')

    def test_serialization_string_does_not_prove_pairwise_ordering(self):
        raw = raw_response()
        for access in accesses(raw):
            access['execution_context']['serialized_by'] = 'thread_join:some-task'
        self.candidate(raw,'unproven_serialization_hint')

    def test_common_atomic_label_does_not_prove_payload_publication(self):
        raw = raw_response()
        for access in accesses(raw):
            access['other_synchronization'] = ['atomic:ready']
        candidate = self.candidate(raw,'unproven_other_synchronization')
        self.assertEqual(candidate['other_synchronization'],['atomic:ready'])

    def test_one_sided_sync_hint_is_preserved_as_uncertain(self):
        raw = raw_response()
        accesses(raw)[0]['other_synchronization'] = ['acquire:ready']
        self.candidate(raw,'unproven_other_synchronization')

    def test_unknown_common_lock_kind_does_not_prove_protection(self):
        raw = raw_response()
        raw['findings'][0]['locks'] = []
        for access in accesses(raw):
            access['must_lockset'] = ['lock:state']
        self.candidate(raw,'unknown_common_lock_kind:lock:state')

    def test_conflicting_must_lock_modes_do_not_prove_protection(self):
        raw = raw_response()
        for access in accesses(raw):
            access['must_lockset'] = [{'lock_id':'lock:state','mode':'shared'}, {'lock_id':'lock:state','mode':'exclusive'}]
        self.candidate(raw,'conflicting_common_lock_mode:lock:state')

    def test_unknown_rwlock_mode_is_not_assumed_safe(self):
        raw = raw_response()
        raw['findings'][0]['locks'][0]['kind'] = 'rwlock'
        for access in accesses(raw):
            access['must_lockset'] = ['lock:state']
        self.candidate(raw,'unknown_common_lock_mode:lock:state')

    def test_valid_rwlock_modes_still_protect_split_pair(self):
        raw = raw_response()
        raw['findings'][0]['locks'][0]['kind'] = 'rwlock'
        accesses(raw)[0]['must_lockset'] = [{'lock_id':'lock:state','mode':'read'}]
        accesses(raw)[1]['must_lockset'] = [{'lock_id':'lock:state','mode':'write'}]
        self.assertEqual(build_candidates(raw)['candidates'], [])

    def test_read_lock_cannot_protect_write(self):
        raw = raw_response()
        raw['findings'][0]['locks'][0]['kind'] = 'rwlock'
        for access in accesses(raw):
            access['must_lockset'] = [{'lock_id':'lock:state','mode':'read'}]
        self.assertEqual(build_candidates(raw)['candidates'][0]['candidate_status'], 'ready_for_review')

    def test_may_locks_are_not_promoted_to_must(self):
        raw = raw_response()
        for access in accesses(raw):
            access['must_lockset'] = []
            access['may_lockset'] = [{'lock_id':'lock:state','mode':'exclusive'}]
        self.assertEqual(len(build_candidates(raw)['candidates']), 1)

    def test_uncertain_sync_cannot_be_submitted_as_a_defect(self):
        raw = raw_response()
        for access in accesses(raw):
            access['other_synchronization'] = ['atomic:ready']
        request = prepare_reviews(raw)['review_requests'][0]
        review = _fixture_review(request)
        review.update(status='probable',verdict='defect')
        with self.assertRaisesRegex(PipelineError,'incomplete'):
            validate_review(request, review)

    def test_lock_cycle_serialization_hint_is_also_kept_uncertain(self):
        from test_audit_improvements import response
        raw = response([('a','b'),('b','a')])
        for edge in raw['findings'][0]['lock_order_edges']:
            edge['execution_context']['serialized_by'] = 'join:task'
        self.candidate(raw,'unproven_serialization_hint')


if __name__ == '__main__':
    unittest.main()
