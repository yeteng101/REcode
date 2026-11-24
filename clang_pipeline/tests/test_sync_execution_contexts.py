from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from clang_pipeline.clang_ast import run_clang_ast
from clang_pipeline.semantic_sync import SemanticSyncAnalyzer


SOURCE = """#include <pthread.h>
#include <stddef.h>
struct Link { struct Link *next; };
struct Item { int value; struct Link member; };
static struct Link items;
static struct Item payload;
static pthread_t task, other;
static void put(struct Link *q, struct Link *e) { e->next = q->next; q->next = e; }
static struct Link *head(struct Link *q) { return q->next; }
static void attach(struct Item *p) { put(&items, &p->member); }
static void publish(struct Item *p) { int *value = &p->value; *value = 3; }
static int consume(void) {
    struct Link *link = head(&items);
    struct Item *item = (struct Item *)((char *)link - offsetof(struct Item, member));
    return *(&item->value);
}
static int dispatch(void) { return consume(); }
static void worker(void *arg) { publish(&payload); }
extern void escape(void *);
static int start(pthread_t *tid, void (*receive)(void *)) {
    union { void (*input)(void *); void *(*output)(void *); } adapter;
    adapter.input = receive;
    ADAPTER_ESCAPE
    return pthread_create(tid, 0, adapter.output, 0);
}
static int create(pthread_t *tid, void (*receive)(void *)) { return start(tid, receive); }
static int wait_for(pthread_t *tid) { return pthread_join(*tid, 0); }
#define CHECK(x) do { int ret = (x); if (ret) __builtin_trap(); } while (0)
void exercise(void) {
    attach(&payload);
    SEQUENCE
}
"""


@unittest.skipUnless(shutil.which("clang"), "real Clang compiler required")
class SyncExecutionContextTests(unittest.TestCase):
    def analyze(self, sequence, escape=""):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "scenario.c"
            path.write_text(SOURCE.replace("SEQUENCE", sequence).replace("ADAPTER_ESCAPE", escape), encoding="utf-8")
            unit = {"file": str(path), "directory": str(root), "arguments": ["clang", "-std=c11", "-pthread", "-c", str(path)]}
            asts = [(path.name, run_clang_ast(unit, root))]
            return SemanticSyncAnalyzer(root, [unit]).analyze(asts)

    def test_traces_worker_and_dispatch_with_a_conditional_window(self):
        result = self.analyze("CHECK(create(&task, worker)); CHECK(dispatch()); CHECK(wait_for(&task));")
        facts = result["field_bindings"]
        context, = facts["execution_contexts"]["bindings"]
        self.assertEqual("unknown", context["overlap"])
        self.assertEqual("conditional_start_dispatch_join", context["thread_window"]["status"])
        self.assertEqual("worker", context["worker"]["entry"])
        self.assertEqual(["create", "start", "pthread_create"], [step["callee"] for step in context["worker"]["creation"]])
        self.assertEqual(["publish"], [step["callee"] for step in context["worker"]["calls"]])
        self.assertEqual(2, len(context["worker"]["adapter_locations"]))
        self.assertEqual([["dispatch", "consume"]],
                         [[step["callee"] for step in route] for route in context["consumer"]["call_paths"]])
        self.assertEqual(["wait_for", "pthread_join"], [step["callee"] for step in context["thread_window"]["join"]])
        self.assertEqual([], result["findings"][0]["shared_objects"])
        ids = {row["id"] for row in facts["evidence"]}
        self.assertTrue(set(context["evidence_ids"]) <= ids)

    def test_sequential_join_other_handle_and_unknown_adapter_stay_incomplete(self):
        sequences = (
            "create(&task, worker); wait_for(&task); dispatch(); wait_for(&task);",
            "create(&task, worker); dispatch(); wait_for(&other);",
            "if (dispatch()) create(&task, worker); dispatch(); wait_for(&task);",
            "do { create(&task, worker); wait_for(&task); } while (0); dispatch(); wait_for(&task);",
        )
        for sequence in sequences:
            with self.subTest(sequence=sequence):
                facts = self.analyze(sequence)["field_bindings"]
                context, = facts["execution_contexts"]["bindings"]
                self.assertEqual("unknown", context["overlap"])
                self.assertNotIn("thread_window", context)
        escaped = self.analyze("create(&task, worker); dispatch(); wait_for(&task);", "escape(&adapter);")
        self.assertEqual([], escaped["field_bindings"]["execution_contexts"]["bindings"])
