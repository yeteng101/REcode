from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from clang_pipeline.clang_ast import run_clang_ast
from clang_pipeline.semantic_sync import SemanticSyncAnalyzer


SOURCE = """#include <stddef.h>
struct Link { struct Link *next; };
struct Entry { int value; struct Link link; struct Link spare; };
static struct Link left, right, empty;
static struct Entry first, second;
static void attach(struct Link *root, struct Link *element) {
    element->next = root->next;
    root->next = element;
}
static void copy_queue(struct Link *from, struct Link *to) { to->next = from->next; }
static struct Link *front(const struct Link *root) { return root->next; }
static struct Link *opaque(struct Link *root) { return root; }
extern void replace(struct Link *);
#define entry_from(p, t, f) ((t *)((char *)(p) - offsetof(t, f)))
#define wrong_from(p, t, f) ((t *)((char *)(p) - offsetof(t, spare)))

void register_entries(void) {
    attach(&left, &first.link);
    attach(&right, &second.link);
}
int read_left(void) {
    struct Link temp;
    copy_queue(&left, &temp);
    struct Link *selected = front(&temp);
    struct Entry *owner = entry_from(selected, struct Entry, link);
    return *(&owner->value);
}
int read_right(void) {
    struct Link *selected = front(&right);
    struct Entry *owner = (struct Entry *)((char *)selected - __builtin_offsetof(struct Entry, link));
    return *(&owner->value);
}
int read_empty(void) {
    struct Link *selected = front(&empty);
    struct Entry *owner = entry_from(selected, struct Entry, link);
    return *(&owner->value);
}
int wrong_member(void) {
    struct Link *selected = front(&left);
    struct Entry *owner = entry_from(selected, struct Entry, spare);
    return *(&owner->value);
}
int wrong_macro(void) {
    struct Link *selected = front(&left);
    struct Entry *owner = wrong_from(selected, struct Entry, link);
    return *(&owner->value);
}
int unmodeled(void) {
    struct Link *selected = opaque(&left);
    struct Entry *owner = entry_from(selected, struct Entry, link);
    return *(&owner->value);
}
int overwritten(void) {
    struct Link temp;
    copy_queue(&left, &temp);
    replace(&temp);
    struct Link *selected = front(&temp);
    struct Entry *owner = entry_from(selected, struct Entry, link);
    return *(&owner->value);
}
int overwritten_inside_block(void) {
    struct Link temp;
    copy_queue(&left, &temp);
    { temp.next = 0; }
    struct Link *selected = front(&temp);
    struct Entry *owner = entry_from(selected, struct Entry, link);
    return *(&owner->value);
}
int main(void) { return 0; }
"""


@unittest.skipUnless(shutil.which("clang"), "real Clang compiler required")
class SyncQueueProvenanceTests(unittest.TestCase):
    def analyze(self, *, duplicate=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "queue.c"
            path.write_text(SOURCE, encoding="utf-8")
            unit = {"file": str(path), "directory": str(root), "arguments": ["clang", "-std=c11", "-c", str(path)]}
            asts = [(path.name, run_clang_ast(unit, root)) for _ in range(2 if duplicate else 1)]
            return SemanticSyncAnalyzer(root, [unit]).analyze(asts)["field_bindings"]

    def test_embedded_registration_follows_transfer_and_distinguishes_same_type_queues(self):
        facts = self.analyze()
        accesses = {item["id"]: item for item in facts["accesses"]}
        rows = {accesses[item["access_id"]]["function"]: item for item in facts["queue_provenance"]["bindings"]}
        self.assertEqual({"read_left", "read_right"}, set(rows))
        self.assertEqual("first", rows["read_left"]["registration"]["receiver"]["name"])
        self.assertEqual("second", rows["read_right"]["registration"]["receiver"]["name"])
        self.assertEqual(1, len(rows["read_left"]["transfers"]))
        self.assertEqual([], rows["read_right"]["transfers"])
        self.assertEqual({"possible_registration_source"}, {item["status"] for item in rows.values()})
        evidence = {item["id"]: item for item in facts["evidence"]}
        for row in rows.values():
            self.assertEqual("global_storage", row["queue_identity"])
            sites = [evidence[key]["location"] for key in row["evidence_ids"]]
            self.assertIn(row["registration"]["call"], sites)
            self.assertIn(row["head"]["call"], sites)
            self.assertIn(row["owner_recovery"]["location"], sites)
        self.assertEqual(facts, self.analyze(duplicate=True))
