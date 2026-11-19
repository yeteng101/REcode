from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clang_pipeline.resource_flow import analyze_resource_flow


class ResourceBindingTests(unittest.TestCase):
    def analyze(self, source, declarations, calls):
        """Use call locations and evidence in the same shape as the native graph."""
        lines = source.splitlines()
        nodes = [
            {"id": key, "kind": "function", "name": name, "file": "sample.c",
             "line": next(i for i, text in enumerate(lines, 1) if text == declaration)}
            for key, name, declaration in declarations
        ]
        known = {node["id"] for node in nodes}
        targets = sorted({target for _, target, _ in calls} - known)
        nodes.extend({"id": target, "name": target, "kind": "external_function"} for target in targets)
        edges, evidence = [], []
        for i, (caller, target, text) in enumerate(calls):
            line = next(i for i, value in enumerate(lines, 1) if value.strip() == text)
            site = {"file": "sample.c", "line": line, "snippet": text}
            evidence.append({"id": f"evidence-{i}", "location": site})
            edges.append({"id": f"call-{i}", "kind": "direct_call", "source": caller,
                          "target": target, "call_site": site, "evidence_ids": [f"evidence-{i}"]})
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "sample.c").write_text(source, encoding="utf-8")
            result = analyze_resource_flow({"nodes": nodes, "edges": edges, "evidence": evidence},
                                           source_root=directory)
        ids = {item["id"] for item in result["evidence"]}
        for item in result["items"]:
            operations = {op["operation_id"]: op for op in item["operations"]}
            for path in item["paths"]:
                self.assertLessEqual(set(path["evidence_ids"]), ids)
                self.assertEqual({item["resource_instance_id"]},
                    {operations[op_id]["resource_instance_id"] for op_id in path["operations"]})
        return result["items"]

    def test_factory_calls_and_forwarded_free_keep_independent_objects(self):
        source = """static void *allocate(void) {
    return calloc(1, 16);
}
static void dispose(void *job) {
    free(job);
}
static void cancel(void *job) {
    dispose(job);
}
void run(int canceled) {
    void *request = allocate();
    void *job = allocate();
    if (canceled) {
        cancel(job);
        return;
    }
    free(request);
}
"""
        declarations = [
            ("factory", "allocate", "static void *allocate(void) {"),
            ("dispose", "dispose", "static void dispose(void *job) {"),
            ("cancel", "cancel", "static void cancel(void *job) {"),
            ("run", "run", "void run(int canceled) {"),
        ]
        calls = [
            ("factory", "calloc", "return calloc(1, 16);"), ("dispose", "free", "free(job);"),
            ("cancel", "dispose", "dispose(job);"), ("run", "factory", "void *request = allocate();"),
            ("run", "factory", "void *job = allocate();"), ("run", "cancel", "cancel(job);"),
            ("run", "free", "free(request);"),
        ]
        items = self.analyze(source, declarations, calls)
        request, job = (next(item for item in items if item["owner"] == "run"
                             and item["resource_expression"] == name) for name in ("request", "job"))
        self.assertNotEqual(request["resource_instance_id"], job["resource_instance_id"])
        for item in (request, job):
            acquisition, transfer = item["operations"][:2]
            self.assertEqual(("acquire", "transfer"), (acquisition["kind"], transfer["kind"]))
            self.assertEqual(2, transfer["allocation_origin"]["line"])
            self.assertTrue(all(not path["complete"] for path in item["paths"]))
        request_releases = [op["snippet"] for op in request["operations"] if op["kind"] == "release"]
        self.assertEqual(["free(request);"], request_releases)
        job_release = next(op for op in job["operations"] if op["kind"] == "release")
        self.assertEqual("free(job);", job_release["snippet"])
        self.assertEqual(["cancel", "dispose"], [site["api"] for site in job_release["via_calls"]])
        self.assertTrue(any(path.get("exit_location", {}).get("snippet") == "return;" for path in request["paths"]))

    def test_unconditional_local_alias_release_has_a_proven_path(self):
        source = """void run(void) {
    void *owner = malloc(16);
    void *alias = owner;
    free(alias);
}
"""
        items = self.analyze(source, [("run", "run", "void run(void) {")],
                             [("run", "malloc", "void *owner = malloc(16);"),
                              ("run", "free", "free(alias);")])
        self.assertEqual(1, len(items))
        self.assertEqual("release_found", items[0]["paths"][0]["termination_reason"])
        self.assertTrue(items[0]["paths"][0]["complete"])

    def test_conditional_definition_is_not_reused_across_branches(self):
        source = """void run(int flag, void *other) {
    void *owner = other;
    if (flag) {
        owner = malloc(16);
    } else {
        free(owner);
    }
}
"""
        items = self.analyze(source, [("run", "run", "void run(int flag, void *other) {")],
                             [("run", "malloc", "owner = malloc(16);"), ("run", "free", "free(owner);")])
        self.assertEqual(2, len(items))
        self.assertTrue(all(len(item["operations"]) == 1 for item in items))
        self.assertTrue(all(not path["complete"] for item in items for path in item["paths"]))

    def test_reassigned_alias_cannot_bind_a_release_to_the_original_allocation(self):
        source = """void run(int replace, void *other) {
    void *owner = malloc(16);
    void *alias = owner;
    if (replace) {
        alias = other;
    }
    free(alias);
}
"""
        items = self.analyze(source, [("run", "run", "void run(int replace, void *other) {")],
                             [("run", "malloc", "void *owner = malloc(16);"), ("run", "free", "free(alias);")])
        self.assertEqual(2, len(items))
        acquire = next(item for item in items if item["resource_expression"] == "owner")
        self.assertEqual(["acquire"], [op["kind"] for op in acquire["operations"]])

    def test_derived_return_is_not_a_factory_summary(self):
        source = """static char *indirect(void) {
    return malloc(16) + 1;
}
void run(void) {
    char *ptr = indirect();
}
"""
        items = self.analyze(source,
                             [("factory", "indirect", "static char *indirect(void) {"),
                              ("run", "run", "void run(void) {")],
                             [("factory", "malloc", "return malloc(16) + 1;"),
                              ("run", "factory", "char *ptr = indirect();")])
        self.assertTrue(items)
        self.assertEqual({"indirect"}, {item["owner"] for item in items})
