"""Contract regression fixtures for the REcode adapter, separate from native runs."""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "v2-common"))
from coordinator import build_first_group_request, route_specialties
from first_group_client import FirstGroupClientError
from first_group_protocol import FORBIDDEN_CONCLUSION_KEYS, _walk
from recode_artifacts import RecodeArtifactsClient
from recode_client import RecodeClient


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class RecodeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.meta = {"repository": "org/repo", "commit": "a" * 40, "build_profile": "test-clang"}
        self.manifest = dict(self.meta, run_id="native-test", status="succeeded", outputs={"graph": "graph.json"})
        self.graph = {"run_id": "native-test", "meta": self.meta,
            "nodes": [{"id": "fn:writer", "name": "writer", "kind": "function", "file": "state.c", "line": 2},
                      {"id": "fn:reader", "name": "reader", "kind": "function", "file": "state.c", "line": 5}],
            "edges": [{"id": "call", "source": "fn:writer", "target": "fn:reader", "kind": "direct_call",
                       "call_site": {"file": "state.c", "line": 3}, "confidence": 0.9, "evidence_ids": ["ev"]}],
            "evidence": [{"id": "ev", "kind": "call_site", "location": {"file": "state.c", "line": 3}}]}
        self.docs = {}
        for name, kind in (("call-chains", "call_chain"), ("resource-flow", "resource_flow"), ("sync-relations", "sync_relation"), ("race-analysis", "race_analysis")):
            self.docs[name] = {"schema_version": "1.0", "run_id": "native-test", "result_type": kind,
                               "status": "succeeded", "source": dict(self.meta, origin="analyzer"),
                               "items": [], "evidence": [], "warnings": []}
        self.docs["call-chains"]["origin"] = "analyzer"
        self.docs["call-chains"]["source"]["origin"] = "01-index/ast/*.ast.json"
        self.docs["race-analysis"].update(label="race", reason="This producer conclusion must be ignored")
        self.docs["race-analysis"]["items"] = [{"status": "race", "reason": "ignore region verdict", "accesses": [
            {"id": "read", "access": "read", "function": "reader", "memory_location": "counter",
             "file": "state.c", "line": 6, "evidence_ids": ["read-ev"]},
            {"id": "write", "access": "write", "function": "writer", "memory_location": "counter",
             "file": "state.c", "line": 3, "protected": True, "evidence_ids": ["write-ev"]}]}]
        self.docs["race-analysis"]["evidence"] = [
            {"id": "read-ev", "kind": "memory_access", "location": {"file": "state.c", "line": 6}},
            {"id": "write-ev", "kind": "memory_access", "location": {"file": "state.c", "line": 3}}]
        self.review = {"repository": {"name": "org/repo", "commit_sha": "a" * 40, "build_profile": "test-clang"},
                       "review_target": {"id": "pr-1"}, "diff": {"changed_files": ["state.c"]},
                       "changed_functions": [{"name": "writer", "file": "state.c", "start_line": 2}]}
        self.save()

    def save(self):
        write(self.root / "manifest.json", self.manifest)
        write(self.root / "graph.json", self.graph)
        for name, data in self.docs.items():
            write(self.root / "external" / (name + ".json"), data)

    def query(self, action="get_sync_flows"):
        return RecodeArtifactsClient(self.root).query(build_first_group_request(self.review, action))

    def test_native_top_level_origin_with_ast_provenance_is_accepted(self):
        result = self.query("get_call_chains")
        self.assertEqual("fn:writer", result["findings"][0]["root_node_id"])
        self.assertEqual(1, result["target_resolution"]["resolved_total"])
        self.assertEqual("fn:reader", result["findings"][0]["edges"][0]["target_node_id"])

    def test_classifier_verdicts_and_protection_hints_are_not_promoted(self):
        result = self.query()
        serialized = json.dumps(result)
        self.assertNotIn("ignore region verdict", serialized)
        self.assertNotIn("producer conclusion", serialized)
        self.assertFalse(set(k for k, _, _ in _walk(result["findings"])) & FORBIDDEN_CONCLUSION_KEYS)
        obj = result["findings"][0]["shared_objects"][0]
        self.assertIsNone(obj["declaration"])
        self.assertEqual(0.35, obj["identity_confidence"])
        self.assertTrue(all(a["execution_context"] == {} and a["must_lockset"] == [] for a in obj["accesses"]))
        self.assertFalse(result["coverage"]["complete"])

    def test_same_name_in_distinct_files_is_not_joined(self):
        raw = copy.deepcopy(self.docs["race-analysis"]["items"][0]["accesses"][0])
        raw.update(id="other-read", file="other.c")
        self.docs["race-analysis"]["items"][0]["accesses"].append(raw)
        self.save()
        self.assertEqual(2, len(self.query()["findings"][0]["shared_objects"]))

    def add_concurrency_observations(self):
        def located(identity, line, **fields):
            evidence = identity + "-ev"
            location = {"file": "state.c", "line": line}
            self.docs["race-analysis"]["evidence"].append(
                {"id": evidence, "kind": "source_fact", "location": copy.deepcopy(location)})
            return dict(id=identity, location=location, evidence_ids=[evidence], **fields)
        declaration = located("global-counter", 1, name="counter", type="int")
        declaration["object_id"] = declaration.pop("id")
        declaration["declaration"] = declaration.pop("location")
        event = located("create-writer", 10, kind="create", function="main", entry_function="writer",
                        api="pthread_create", handle_expression="&a")
        operation = located("lock-writer", 2, kind="acquire", function="writer", api="pthread_mutex_lock",
                            lock_expression="&mutex", declaration_candidate_id=None)
        binding = {"access_id": "write", "declaration_candidate_id": "global-counter",
            "execution_context": {"creation_site_ids": ["create-writer"]},
            "lock_operation_ids": ["lock-writer"], "coverage_gaps": ["path_sensitive_lockset_unavailable"]}
        self.docs["race-analysis"]["concurrency_facts"] = [{"schema_version": "1.0",
            "analysis_basis": "lexical_source_observations", "objects": [declaration],
            "thread_events": [event], "lock_operations": [operation], "access_bindings": [binding]}]
        return self.docs["race-analysis"]["concurrency_facts"][0]

    def test_native_observations_reach_context_with_evidence_and_partial_coverage(self):
        self.add_concurrency_observations()
        self.save()
        result = self.query()
        flow = result["findings"][0]
        obj = flow["shared_objects"][0]
        access = next(a for a in obj["accesses"] if a["access_id"] == "write")
        self.assertEqual({"file": "state.c", "line": 1}, obj["declaration"])
        self.assertEqual("int", obj["type"])
        self.assertEqual(0.65, obj["identity_confidence"])
        self.assertEqual("pthread_create", access["execution_context"]["creation_sites"][0]["api"])
        self.assertEqual("unknown", access["execution_context"]["overlap"])
        self.assertNotIn("thread", access["execution_context"])
        self.assertEqual([], access["must_lockset"])
        observation = access["other_synchronization"][-1]
        self.assertEqual("&mutex", observation["operations"][0]["lock_expression"])
        self.assertTrue({"global-counter-ev", "create-writer-ev", "lock-writer-ev"} <= set(access["evidence_ids"]))
        self.assertEqual(1, len(flow["source_observations"]["lock_operations"]))
        self.assertFalse(result["coverage"]["complete"])
        self.assertIn("recode_thread_instances_and_overlap_unproved", result["coverage"]["stopped_because"])

    def test_lexical_export_cannot_promote_thread_identity_or_must_lockset(self):
        facts = self.add_concurrency_observations()
        binding = facts["access_bindings"][0]
        binding.update(must_lockset=["forged-common-lock"], must_lockset_status="complete")
        binding["execution_context"].update(thread="physical-thread-1", reentrant=False, serialized_by="loop")
        facts["objects"][0]["identity_confidence"] = 1.0
        self.save()
        obj = self.query()["findings"][0]["shared_objects"][0]
        access = next(a for a in obj["accesses"] if a["access_id"] == "write")
        self.assertEqual(0.65, obj["identity_confidence"])
        self.assertEqual([], access["must_lockset"])
        self.assertNotIn("thread", access["execution_context"])
        self.assertNotIn("serialized_by", access["execution_context"])

    def test_unavailable_or_wrong_thread_reference_is_rejected(self):
        for event_id in ("unknown", "create-writer"):
            with self.subTest(event_id=event_id):
                facts = self.add_concurrency_observations()
                facts["access_bindings"][0]["execution_context"]["creation_site_ids"] = [event_id]
                facts["thread_events"][0]["entry_function"] = "other-entry"
                self.save()
                with self.assertRaisesRegex(FirstGroupClientError, "invalid thread creation"):
                    self.query()

    def test_mismatched_or_unbacked_declaration_is_rejected(self):
        facts = self.add_concurrency_observations()
        facts["objects"][0]["name"] = "different-object"
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "name or file mismatch"):
            self.query()
        facts["objects"][0]["name"] = "counter"
        facts["objects"][0]["declaration"]["line"] = 99
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "not backed"):
            self.query()

    def test_conflicting_concurrency_identity_is_rejected(self):
        facts = self.add_concurrency_observations()
        duplicate = copy.deepcopy(facts["lock_operations"][0])
        duplicate["lock_expression"] = "&different_mutex"
        facts["lock_operations"].append(duplicate)
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "conflicting concurrency fact"):
            self.query()

    def test_unknown_concurrency_export_version_is_rejected(self):
        facts = self.add_concurrency_observations()
        facts["schema_version"] = "999"
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "basis/version"):
            self.query()

    def add_semantic_facts(self):
        accesses = []
        for raw in self.docs["race-analysis"]["items"][0]["accesses"]:
            context_id = raw["function"]
            other = "writer" if context_id == "reader" else "reader"
            accesses.append({"access_id": raw["id"], "object_id": "ast-counter", "kind": raw["access"],
                "function": raw["function"], "location": {"file": raw["file"], "line": raw["line"]},
                "execution_context": {"context_id": context_id, "reentrant": False,
                    "may_run_concurrently_with": [other], "cannot_run_concurrently_with": []},
                "must_lockset": [], "may_lockset": [], "other_synchronization": [],
                "confidence": 1.0, "evidence_ids": raw["evidence_ids"]})
        obj = {"object_id": "ast-counter", "name": "counter", "type": "int",
               "identity_confidence": 1.0, "declaration": {"file": "state.c", "line": 1},
               "accesses": accesses, "evidence_ids": ["declaration-ev"]}
        self.docs["race-analysis"]["evidence"].append(
            {"id": "declaration-ev", "kind": "ast_object_declaration", "location": obj["declaration"]})
        semantic = {"schema_version": "bounded-ast-sync/1", "analysis_basis": "clang-json-ast-paths",
            "translation_units": [{"file": "state.c", "ast_sha256": "d" * 64, "arguments_sha256": "e" * 64}],
            "findings": [{"id": "ast-sync", "kind": "sync_flow", "shared_objects": [obj], "locks": [],
                          "lock_order_edges": [], "confidence": 1.0, "evidence_ids": ["read-ev", "write-ev", "declaration-ev"]}],
            "call_graph": copy.deepcopy(self.graph), "coverage": {"complete": True, "gaps": []},
            "call_coverage": {"complete": True, "gaps": []}}
        self.docs["race-analysis"]["semantic_sync"] = semantic
        return semantic

    def test_bounded_ast_facts_can_reach_complete_sync_and_call_responses(self):
        self.add_semantic_facts()
        self.save()
        for action in ("get_sync_flows", "get_call_chains"):
            result = self.query(action)
            self.assertEqual("succeeded", result["status"])
            self.assertTrue(result["coverage"]["complete"])
            self.assertTrue(result["analysis_context"]["analyzer"]["formal_evidence"])
        self.assertFalse(self.query("get_resource_flows")["coverage"]["complete"])

    def test_semantic_translation_unit_binding_is_required(self):
        semantic = self.add_semantic_facts()
        semantic["translation_units"][0]["ast_sha256"] = "missing"
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "SHA-256"):
            self.query()

    def test_semantic_complete_coverage_cannot_hide_gaps(self):
        semantic = self.add_semantic_facts()
        semantic["coverage"]["gaps"] = ["unsupported_control_flow"]
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "contradicts"):
            self.query()

    def test_incomplete_semantic_facts_preserve_partial(self):
        semantic = self.add_semantic_facts()
        semantic["coverage"] = {"complete": False, "gaps": ["unresolved_memory_reference"]}
        self.save()
        result = self.query()
        self.assertEqual("partial", result["status"])
        self.assertFalse(result["analysis_context"]["analyzer"]["formal_evidence"])
        self.assertIn("unresolved_memory_reference", result["coverage"]["stopped_because"])

    def test_incomplete_semantic_analysis_cannot_exclude_other_context(self):
        semantic = self.add_semantic_facts()
        semantic["coverage"] = {"complete": False, "gaps": ["path_budget_exhausted"]}
        access = semantic["findings"][0]["shared_objects"][0]["accesses"][0]
        access["execution_context"]["cannot_run_concurrently_with"] = ["writer"]
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "cannot certify"):
            self.query()

    def test_semantic_unknown_lock_identity_is_rejected(self):
        semantic = self.add_semantic_facts()
        semantic["findings"][0]["shared_objects"][0]["accesses"][0]["must_lockset"] = [{"lock_id": "missing", "mode": "exclusive"}]
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "unknown lock/mode"):
            self.query()

    def test_semantic_unknown_concurrency_reference_is_rejected(self):
        semantic = self.add_semantic_facts()
        semantic["findings"][0]["shared_objects"][0]["accesses"][0]["execution_context"]["may_run_concurrently_with"] = ["missing"]
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "unknown or contradictory"):
            self.query()

    def test_semantic_call_depth_truncation_is_reported(self):
        self.add_semantic_facts()
        self.save()
        request = build_first_group_request(self.review, "get_call_chains")
        request["analysis"]["max_depth"] = 0
        result = RecodeArtifactsClient(self.root).query(request)
        self.assertFalse(result["coverage"]["complete"])
        self.assertIn("recode_call_depth_truncated", result["coverage"]["stopped_because"])

    def test_cpp_keeps_original_graph_and_explicit_semantic_scope_gap(self):
        semantic = self.add_semantic_facts()
        semantic["coverage"] = {"complete": False, "gaps": ["C_plus_plus_implicit_execution_unsupported:state.cpp"]}
        semantic["call_graph"] = {"nodes": [], "edges": []}
        self.save()
        call = self.query("get_call_chains")
        self.assertEqual(1, call["target_resolution"]["resolved_total"])
        self.assertEqual("fn:reader", call["findings"][0]["edges"][0]["target_node_id"])
        self.assertIn("C_plus_plus_implicit_execution_unsupported:state.cpp", self.query()["coverage"]["stopped_because"])

    def test_unknown_access_context_routes_concurrency_alongside_memory(self):
        result = route_specialties(self.review, {}, {"findings": [{"kind": "resource_flow"}]}, self.query())
        self.assertEqual(["memory", "concurrency"], result["routes"])

    def test_observed_native_sync_relations_route_concurrency(self):
        result = route_specialties(self.review, {}, {"findings": [{"kind": "resource_flow"}]},
            {"findings": [{"kind": "sync_flow", "observed_relations": [{"relation_type": "mutex_lock"}]}]})
        self.assertEqual(["memory", "concurrency"], result["routes"])

    def test_wrong_head_profile_snapshot_and_repository_are_rejected(self):
        client = RecodeArtifactsClient(self.root)
        for field, value in (("commit_sha", "b" * 40), ("name", "other/repo"), ("graph_snapshot_id", "wrong")):
            with self.subTest(field=field):
                request = build_first_group_request(self.review, "get_call_chains")
                request["repository"][field] = value
                with self.assertRaises(FirstGroupClientError):
                    client.query(request)
        request = build_first_group_request(self.review, "get_call_chains")
        request["analysis"]["build_profile"] = "other-clang"
        with self.assertRaises(FirstGroupClientError):
            client.query(request)

    def test_mixed_export_versions_are_rejected(self):
        self.docs["sync-relations"]["source"]["commit"] = "b" * 40
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "binding mismatch"):
            RecodeArtifactsClient(self.root)

    def test_resource_flows_with_same_family_keep_distinct_identities(self):
        operation = {"operation_id": "allocation", "kind": "acquire", "function": "writer", "api": "malloc",
                     "file": "state.c", "line": 3, "evidence_ids": ["ev"]}
        self.docs["resource-flow"]["items"] = [
            {"id": "flow-one", "resource": "heap-family", "resource_type": "heap", "operations": [operation], "evidence_ids": ["ev"]},
            {"id": "flow-two", "resource": "heap-family", "resource_type": "heap", "operations": [operation], "evidence_ids": ["ev"]}]
        self.save()
        findings = self.query("get_resource_flows")["findings"]
        self.assertEqual({"flow-one", "flow-two"}, {f["resource_id"] for f in findings})
        self.assertEqual({"heap-family"}, {f["resource_family_id"] for f in findings})

    def test_reviewed_or_fixture_export_cannot_claim_native_origin(self):
        self.docs["resource-flow"]["source"]["origin"] = "reviewed"
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "native analyzer"):
            RecodeArtifactsClient(self.root)

    def test_mutation_after_capture_is_rejected(self):
        client = RecodeArtifactsClient(self.root)
        self.graph["edges"] = []
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "changed"):
            client.query(build_first_group_request(self.review, "get_sync_flows"))

    def test_missing_native_sync_is_explicit_partial(self):
        (self.root / "external/sync-relations.json").unlink()
        result = self.query()
        self.assertEqual([], result["findings"])
        self.assertIn("recode_export_missing:get_sync_flows", result["coverage"]["reason"])

    def test_unknown_evidence_is_rejected(self):
        self.docs["race-analysis"]["items"][0]["accesses"][0]["evidence_ids"] = ["unknown"]
        self.save()
        with self.assertRaisesRegex(FirstGroupClientError, "unknown source evidence"):
            self.query()

    def test_system_header_locations_are_omitted_with_coverage_record(self):
        self.graph["evidence"].append({"id": "system-ev", "kind": "call_site",
                                       "location": {"file": "/usr/include/example.h", "line": 7}})
        self.graph["edges"].append({"id": "system-edge", "source": "fn:writer", "target": "fn:reader",
            "kind": "direct_call", "call_site": {"file": "/usr/include/example.h", "line": 7}, "evidence_ids": ["system-ev"]})
        self.save()
        result = self.query("get_call_chains")
        self.assertEqual(["call"], [e["edge_id"] for e in result["findings"][0]["edges"]])
        self.assertIn("external_source_record:system-ev", result["coverage"]["omitted_record_ids"])
        self.assertIn("unavailable_call_fact:system-edge", result["coverage"]["omitted_record_ids"])
        self.assertNotIn("/usr/include", json.dumps(result))

    def test_production_analysis_prefix_archive_is_accepted(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as archive:
            for path in self.root.rglob("*.json"):
                data = path.read_bytes()
                member = tarfile.TarInfo("analysis/" + path.relative_to(self.root).as_posix())
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        client = RecodeClient("http://127.0.0.1:1", cache_directory=self.root / "cache")
        captured = client._capture(raw.getvalue(), self.manifest, "job_" + "a" * 32)
        self.assertEqual("native-test", captured.manifest["run_id"])

    def test_ambiguous_targets_remain_unresolved(self):
        self.graph["nodes"].append({"id": "fn:other-writer", "name": "writer", "kind": "function", "file": "state.c", "line": 20})
        self.save()
        result = self.query("get_call_chains")
        self.assertEqual([], result["findings"])
        self.assertEqual("ambiguous_native_target", result["target_resolution"]["unresolved_targets"][0]["reason"])

    def test_archive_traversal_and_links_are_rejected(self):
        client = RecodeClient("http://127.0.0.1:1", cache_directory=self.root / "cache")
        for name, link in (("../escape.json", False), ("graph.json", True)):
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w:gz") as archive:
                member = tarfile.TarInfo(name)
                if link:
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/outside"
                archive.addfile(member)
            with self.subTest(name=name), self.assertRaises(FirstGroupClientError):
                client._capture(raw.getvalue(), {}, "job_" + "a" * 32)

    def test_failed_job_stops_and_does_not_fetch_artifacts(self):
        client = RecodeClient("http://127.0.0.1:1", cache_directory=self.root / "cache", job_id="job_" + "a" * 32)
        with patch.object(client, "_request_json", return_value=(200, {"status": "failed", "error": {"code": "compile_error"}})), patch.object(client, "_download") as download:
            with self.assertRaisesRegex(FirstGroupClientError, "ended with failed"):
                client.query(build_first_group_request(self.review, "get_call_chains"))
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
