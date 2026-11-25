from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from clang_pipeline.clang_ast import write_json
from clang_pipeline.native_export import run_native_export


SOURCE = """#include <stddef.h>
#include <stdlib.h>
struct allocator {
    void *(*allocate)(size_t);
    void (*release)(void *);
};
static struct allocator table = {malloc, free};
static void keep(void *p) {
    (void)p;
}
int entry(int fail) {
    void *p = table.allocate(8);
    if (!p) return -1;
    keep(p);
    if (fail) return -2;
    table.release(p);
    return 0;
}
"""

EVENT_KINDS = {"acquire", "acquire_array", "caller_input", "allocation_failure", "bind_field", "bind_array_field",
               "bind_output", "call", "branch", "release_container", "release", "exit"}
ARGUMENTS = {0, 1, "output_slot", "caller_input", "caller_pointer"}


@unittest.skipUnless(shutil.which("clang"), "real Clang compiler required")
class MemoryExportTests(unittest.TestCase):
    def assert_contract(self, payload):
        self.assertEqual({"schema_version", "basis", "binding", "traces", "tables", "gaps"}, set(payload))
        self.assertEqual("recode-memory-path/1", payload["schema_version"])
        self.assertEqual("clang-json-ast-paths", payload["basis"])
        self.assertEqual({"repository", "commit", "build_profile", "run_id"}, set(payload["binding"]))
        self.assertLessEqual(len(payload["traces"]), 128)
        self.assertLessEqual(len(payload["tables"]), 32)
        for table in payload["tables"]:
            self.assertEqual({"table_id", "name", "stability", "basis", "location", "fields",
                              "evidence_ids"}, set(table))
            self.assertIn(table["stability"], {"const", "read_only"})
            self.assertTrue(table["fields"])
            for target in table["fields"].values():
                self.assertTrue(target == "malloc" or target == "free" or target.startswith("fn_ast_"))
        for trace in payload["traces"]:
            self.assertEqual({"entry", "arguments", "events"}, set(trace))
            self.assertLessEqual(len(trace["arguments"]), 2)
            self.assertTrue(set(trace["arguments"]) <= ARGUMENTS)
            self.assertTrue(trace["events"])
            self.assertLessEqual(len(trace["events"]), 192)
            for event in trace["events"]:
                self.assertIn(event["kind"], EVENT_KINDS)
                self.assertTrue(event["evidence_ids"])
                self.assertTrue(event["location"]["snippet"])
            self.assertEqual("exit", trace["events"][-1]["kind"])

    def test_native_export_binds_proofs_to_source_and_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src" / "case.c"
            source.parent.mkdir(parents=True)
            source.write_text(SOURCE, encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            write_json(workspace / "graph.json", {
                "schema_version": "1.0",
                "run_id": "run_memory_export",
                "meta": {"repository": "example/table", "commit": "0" * 40,
                         "build_profile": "repository-auto"},
                "nodes": [], "edges": []})
            write_json(workspace / "compile_commands.json", [{
                "directory": str(root), "file": str(source),
                "arguments": ["clang", "-std=c11", "-c", str(source)]}])
            write_json(workspace / "request.json", {
                "run_id": "run_memory_export", "source_root": str(root),
                "memory_entry_symbols": ["entry"], "entry_symbols": []})

            result = run_native_export(workspace)
            self.assertEqual("succeeded", result["status"])
            payload = json.loads((workspace / "08-resource-flow" / "resource-flow.json").read_text(encoding="utf-8"))
            proofs = payload["memory_path_proofs"]
            self.assert_contract(proofs)
            self.assertEqual({"repository": "example/table", "commit": "0" * 40,
                              "build_profile": "repository-auto", "run_id": "run_memory_export"},
                             proofs["binding"])
            self.assertEqual([], proofs["gaps"])
            calls = [event for trace in proofs["traces"] for event in trace["events"]
                     if event["kind"] == "call"]
            self.assertTrue(any(event["callback"] is None and event["indirect"] is False for event in calls))
            self.assertTrue(any((event["callback"] or {}).get("kind") == "record" for event in calls))
            self.assertEqual(1, len(proofs["tables"]))
            self.assertEqual({"allocate": "malloc", "release": "free"}, proofs["tables"][0]["fields"])
            self.assertEqual("static_without_write_or_address_escape", proofs["tables"][0]["basis"])
            exits = {trace["events"][-1]["location"]["snippet"]: trace["events"][-1]
                     for trace in proofs["traces"]}
            self.assertEqual([], exits["return 0;"]["live_roots"])
            leaked = [event for snippet, event in exits.items() if "return -2;" in snippet]
            self.assertEqual(1, len(leaked))
            self.assertEqual(1, len(leaked[0]["live_roots"]))
            released = {event["resource_instance_id"] for trace in proofs["traces"]
                        for event in trace["events"] if event["kind"] == "release"}
            self.assertEqual(set(leaked[0]["live_roots"]), released)
            evidence = {item["id"]: item for item in payload["evidence"]}
            self.assertTrue(set(proofs["tables"][0]["evidence_ids"]) <= set(evidence))
            self.assertEqual("ast_memory_table_declaration",
                             evidence[proofs["tables"][0]["evidence_ids"][0]]["kind"])
            for trace in proofs["traces"]:
                self.assertEqual("src/case.c", trace["entry"]["file"])
                for event in trace["events"]:
                    self.assertTrue(set(event["evidence_ids"]) <= set(evidence))
                    self.assertEqual("src/case.c", evidence[event["evidence_ids"][0]]["location"]["file"])
