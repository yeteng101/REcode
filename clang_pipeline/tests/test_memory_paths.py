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
