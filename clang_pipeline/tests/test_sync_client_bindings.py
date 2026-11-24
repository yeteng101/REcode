from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from clang_pipeline.clang_ast import run_clang_ast
from clang_pipeline.semantic_sync import SemanticSyncAnalyzer


SOURCES = {
    "api.h": """struct Link { struct Link *next; };
struct Item { int value; struct Link member; };
void register_item(struct Item *item);
void use_item(struct Item *item);
""",
    "library.c": """#include <stddef.h>
#include "api.h"
static struct Link items;
static void put(struct Link *q, struct Link *e) { e->next = q->next; q->next = e; }
static struct Link *head(struct Link *q) { return q->next; }
void register_item(struct Item *item) { put(&items, &item->member); }
void use_item(struct Item *item) { int *value = &item->value; *value = 1; }
int read_item(void) {
    struct Link *link = head(&items);
    struct Item *item = (struct Item *)((char *)link - offsetof(struct Item, member));
    return *(&item->value);
}
""",
    "client.c": """#include "api.h"
static struct Item shared;
static struct Item independent;
static void register_wrapper(struct Item *item) { register_item(item); }
void configure(void) { register_wrapper(&shared); }
void use_shared(void) { use_item(&shared); }
void use_other(void) { use_item(&independent); }
""",
    "other.c": """#include "api.h"
static struct Item shared;
void use_different_shared(void) { use_item(&shared); }
""",
}


@unittest.skipUnless(shutil.which("clang"), "real Clang compiler required")
class SyncClientBindingTests(unittest.TestCase):
    def analyze(self, names):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in SOURCES.items():
                (root / name).write_text(source, encoding="utf-8")
            units = [{"file": str(root / name), "directory": str(root),
                      "arguments": ["clang", "-std=c11", "-c", str(root / name)]} for name in names]
            asts = [(name, run_clang_ast(unit, root)) for name, unit in zip(names, units)]
            return SemanticSyncAnalyzer(root, units).analyze(asts)

    def test_links_exact_storage_through_registration_and_excludes_lookalikes(self):
        library = self.analyze(["library.c"])["field_bindings"]
        self.assertEqual(1, len(library["queue_provenance"]["bindings"]))
        self.assertEqual([], library["client_objects"]["bindings"])
        result = self.analyze(["library.c", "client.c", "other.c"])
        self.assertEqual([], result["findings"][0]["shared_objects"])
        facts = result["field_bindings"]
        peer_calls = [row for row in facts["direct_calls"]["bindings"] if "field_object_id" in row]
        self.assertEqual(3, len({row["field_object_id"] for row in peer_calls}))
        binding, = facts["client_objects"]["bindings"]
        self.assertEqual("same_object_if_registration_source", binding["status"])
        self.assertEqual("shared", binding["object"]["name"])
        self.assertEqual("client.c", binding["object"]["declaration"]["file"])
        self.assertEqual([("configure", "register_wrapper"), ("register_wrapper", "register_item")],
                         [(edge["caller"], edge["callee"]) for edge in binding["registration_calls"]])
        matched = next(row for row in peer_calls if row["id"] == binding["direct_binding_id"])
        self.assertEqual("use_shared", matched["calls"][0]["caller"])
        self.assertEqual(matched["field_object_id"], binding["field_object_id"])
        queue, = facts["queue_provenance"]["bindings"]
        self.assertEqual(queue["id"], binding["queue_provenance_id"])
        evidence = {item["id"]: item for item in facts["evidence"]}
        locations = [evidence[key]["location"] for key in binding["evidence_ids"]]
        for edge in binding["registration_calls"]:
            self.assertIn(edge["argument_location"], locations)
        self.assertTrue(set(queue["evidence_ids"] + matched["evidence_ids"]) <= set(binding["evidence_ids"]))
