from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from clang_pipeline.clang_ast import run_clang_ast
from clang_pipeline.memory_paths import analyze_memory_paths


SOURCE = """#include <stdlib.h>
struct Job { void *owned; void (*finish)(void *); };
static void destroy(void *p) {
    free(p);
}
extern void external_cleanup(void *);
int entry(int cancel) {
    void *p = malloc(16);
    if (!p) return -1;
    struct Job *job = calloc(1, sizeof(struct Job));
    if (!job) {
        free(p);
        return -1;
    }
    job->owned = p;
    job->finish = destroy;
    if (!cancel) job->finish(job->owned);
    free(job);
    return 0;
}
"""


OUTPUT_SOURCE = """#include <stdlib.h>
struct Result { void *contents; };
int entry(struct Result **out, int *count) {
    *out = calloc(1, sizeof(struct Result));
    *count = 0;
    if (*out == NULL) return -1;
    void *contents = malloc(16);
    if (!contents) {
        free(*out);
        *out = NULL;
        return -1;
    }
    (*out)->contents = contents;
    *count = 1;
    return 0;
}
"""


@unittest.skipUnless(shutil.which("clang"), "real Clang compiler required")
class MemoryPathTests(unittest.TestCase):
    def analyze(self, source):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "case.c"
            file.write_text(source, encoding="utf-8")
            unit = {"file": str(file), "directory": str(root),
                    "arguments": ["clang", "-std=c11", "-c", str(file)]}
            ast = run_clang_ast(unit, root)
            return analyze_memory_paths(root, ([unit], [(file.name, ast)], [], []), {}, ["entry"])

    def test_cancel_retains_owned_pointer_and_callback_releases_same_instance(self):
        result, evidence = self.analyze(SOURCE)
        normal, cancel = (next(trace for trace in result["traces"] if trace["arguments"] == [arg]
                               and trace["events"][-1]["location"]["snippet"] == "return 0;") for arg in (0, 1))
        roots = cancel["events"][-1]["live_roots"]
        self.assertEqual(1, len(roots))
        self.assertEqual([], normal["events"][-1]["live_roots"])
        self.assertTrue(any(event["kind"] == "release" and event["resource_instance_id"] == roots[0]
                            for event in normal["events"]))
        binding = next(event for event in normal["events"] if event["kind"] == "bind_field"
                       and event["value_kind"] == "function")
        callback = next(event for event in normal["events"] if event["kind"] == "call" and event["indirect"])
        self.assertEqual(binding["value"], callback["target_id"])
        self.assertEqual(binding["container"], callback["callback"]["container"])
        self.assertTrue(evidence)

    def test_external_callback_discards_unproved_root_argument(self):
        result, _ = self.analyze(SOURCE.replace("job->finish = destroy", "job->finish = external_cleanup"))
        self.assertTrue(any("unmodeled_call:external_cleanup" in gap for gap in result["gaps"]))
        self.assertFalse(any(trace["arguments"] == [0] for trace in result["traces"]))
        self.assertTrue(any(trace["arguments"] == [1] and trace["events"][-1]["live_roots"]
                            for trace in result["traces"]))

    def test_caller_output_owns_the_allocation_and_its_contents(self):
        result, _ = self.analyze(OUTPUT_SOURCE)
        self.assertEqual([], result["gaps"])
        success = next(trace for trace in result["traces"]
                       if trace["events"][-1]["location"]["snippet"] == "return 0;")
        self.assertEqual(["output_slot", "output_slot"], success["arguments"])
        self.assertEqual([], success["events"][-1]["live_roots"])
        self.assertTrue(any(event["kind"] == "bind_output" and event["slot"] == 0
                            for event in success["events"]))

    def test_cleared_output_loses_its_previous_ownership(self):
        source = OUTPUT_SOURCE.replace("*count = 1;", "*count = 1;\n    *out = NULL;")
        result, _ = self.analyze(source)
        success = next(trace for trace in result["traces"]
                       if trace["events"][-1]["location"]["snippet"] == "return 0;")
        bindings = [event["value"] for event in success["events"] if event["kind"] == "bind_output"]
        self.assertEqual(None, bindings[-1])
        self.assertEqual([bindings[0]], success["events"][-1]["live_roots"])

    def test_unknown_initial_output_value_is_not_assumed_null(self):
        source = OUTPUT_SOURCE.replace("*out = calloc", "if (*out) return 0;\n    *out = calloc")
        result, _ = self.analyze(source)
        self.assertEqual([], result["traces"])
        self.assertTrue(any("uninitialized_output_slot_read" in gap for gap in result["gaps"]))

    def test_bounded_loop_records_all_iterations(self):
        source = """int entry(int n) {
    int i;
    for (i = 0; i < n; i++) { }
    return 0;
}
"""
        result, _ = self.analyze(source)
        self.assertEqual([], result["gaps"])
        one = next(trace for trace in result["traces"] if trace["arguments"] == [1])
        self.assertEqual([True, False], [event["taken"] for event in one["events"] if event["kind"] == "branch"])

    def test_loop_iteration_limit_does_not_emit_a_proof(self):
        source = """int entry(void) {
    int i;
    for (i = 0; i < 17; i++) { }
    return 0;
}
"""
        result, _ = self.analyze(source)
        self.assertEqual([], result["traces"])
        self.assertTrue(any("loop_iteration_limit" in gap for gap in result["gaps"]))

    def test_array_elements_preserve_distinct_resource_bindings(self):
        source = """#include <stdlib.h>
struct Item { void *value; };
int entry(void) {
    struct Item *items = calloc(2, sizeof(struct Item));
    if (!items) return -1;
    void *first = malloc(8);
    if (!first) { free(items); return -1; }
    items[0].value = first;
    void *second = malloc(8);
    if (!second) { free(items[0].value); free(items); return -1; }
    items[1].value = second;
    free(items[0].value);
    free(items[1].value);
    free(items);
    return 0;
}
"""
        result, _ = self.analyze(source)
        self.assertEqual([], result["gaps"])
        trace = next(item for item in result["traces"] if item["events"][-1]["location"]["snippet"] == "return 0;")
        bindings = [event for event in trace["events"] if event["kind"] == "bind_array_field"]
        self.assertEqual([0, 1], [event["index"] for event in bindings])
        self.assertNotEqual(bindings[0]["value"], bindings[1]["value"])
        self.assertEqual([], trace["events"][-1]["live_roots"])
        invalid, _ = self.analyze(source.replace("items[1].value = second", "items[2].value = second"))
        self.assertEqual([], invalid["traces"])
        self.assertTrue(any("array_index_out_of_bounds" in gap for gap in invalid["gaps"]))

        alias_source = source.replace("items[1].value = second;", "struct Item *item = &items[1];\n    item->value = second;")
        aliased, _ = self.analyze(alias_source)
        self.assertEqual([], aliased["gaps"])
        success = next(item for item in aliased["traces"] if item["events"][-1]["location"]["snippet"] == "return 0;")
        self.assertEqual([0, 1], [event["index"] for event in success["events"] if event["kind"] == "bind_array_field"])

    def test_loop_break_and_continue_follow_control_flow(self):
        source = """int entry(int n) {
    int i;
    for (i = 0; i < 4; i++) {
        if (i == n) break;
        continue;
    }
    if (i == n) return 1;
    return 0;
}
"""
        result, _ = self.analyze(source)
        self.assertEqual([], result["gaps"])
        self.assertEqual(2, len(result["traces"]))
        self.assertTrue(all(trace["events"][-1]["location"]["snippet"] == "if (i == n) return 1;"
                            for trace in result["traces"]))

    def test_forward_goto_reaches_cleanup_and_backward_goto_is_a_gap(self):
        source = """#include <stdlib.h>
int entry(int fail) {
    void *p = malloc(8);
    if (!p) return -1;
    if (fail) goto cleanup;
    free(p);
    return 0;
cleanup:
    free(p);
    return -1;
}
"""
        result, _ = self.analyze(source)
        self.assertEqual([], result["gaps"])
        self.assertTrue(all(not trace["events"][-1]["live_roots"] for trace in result["traces"]))
        backward, _ = self.analyze("int entry(void) { again: goto again; return 0; }\n")
        self.assertEqual([], backward["traces"])
        self.assertTrue(any("unresolved_control_transfer" in gap for gap in backward["gaps"]))
