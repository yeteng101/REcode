from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from clang_pipeline.clang_ast import run_clang_ast
from clang_pipeline.semantic_sync import SemanticSyncAnalyzer


SOURCES = {
    "slot.h": "struct Slot { int value; };\n",
    "leaf.c": """#include "slot.h"
int leaf(int flag, struct Slot *receiver) {
    struct Slot *base = receiver;
    int *value = &base->value;
    return *value + flag;
}
__attribute__((weak)) int weak_leaf(struct Slot *receiver) {
    return *(&receiver->value);
}
""",
    "forward.c": """struct Slot;
int leaf(int flag, struct Slot *receiver);
int forward(struct Slot *param) { return leaf(0, param); }
""",
    "callers.c": """#include "slot.h"
static struct Slot left, right;
int forward(struct Slot *param);
int leaf(int flag, struct Slot *receiver);
int weak_leaf(struct Slot *receiver);
extern struct Slot *unknown(void);
extern void replace(struct Slot **);
int from_left(void) { struct Slot *copy = &left; return forward(copy); }
int from_right(void) { return forward(&right); }
int from_local(void) { struct Slot slot = {0}; return forward(&slot); }
int standalone(void) { return *(&left.value); }
int reassigned(struct Slot *p) { p = &right; return leaf(0, p); }
int escaped(struct Slot *p) { replace(&p); return leaf(0, p); }
int unresolved(void) { return leaf(0, unknown()); }
int indirect(void) { int (*fp)(int, struct Slot *) = leaf; return fp(0, &left); }
int weak(void) { return weak_leaf(&left); }
int main(void) { return 0; }
""",
}


@unittest.skipUnless(shutil.which("clang"), "real Clang compiler required")
class SyncFieldCallTests(unittest.TestCase):
    def analyze(self, *, duplicate_leaf=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, source in SOURCES.items():
                (root / name).write_text(source, encoding="utf-8")
            filenames = [name for name in SOURCES if name.endswith(".c")]
            if duplicate_leaf:
                filenames.append("leaf.c")
            units = [{"file": str(root / name), "directory": str(root),
                      "arguments": ["clang", "-std=c11", "-c", str(root / name)]} for name in filenames]
            asts = [(name, run_clang_ast(unit, root)) for name, unit in zip(filenames, units)]
            return SemanticSyncAnalyzer(root, units).analyze(asts)["field_bindings"]

    def test_resolves_receiver_through_two_c_files_and_preserves_object_identities(self):
        facts = self.analyze()
        calls = facts["direct_calls"]["bindings"]
        by_name = {row["origin"]["name"]: row for row in calls}
        self.assertEqual({"param", "left", "right", "slot"}, set(by_name))
        left, right = by_name["left"], by_name["right"]
        self.assertEqual([("from_left", "forward", 0), ("forward", "leaf", 1)],
                         [(edge["caller"], edge["callee"], edge["parameter_index"]) for edge in left["calls"]])
        self.assertEqual(["callers.c", "forward.c"], [edge["location"]["file"] for edge in left["calls"]])
        self.assertEqual([8], [location["line"] for location in left["origin"]["alias_steps"]])
        self.assertEqual([3], [location["line"] for location in left["receiver_parameter"]["alias_steps"]])
        existing = next(row for row in facts["accesses"] if row["function"] == "standalone")
        self.assertEqual(existing["field_object_id"], left["field_object_id"])
        self.assertNotEqual(left["field_object_id"], right["field_object_id"])
        self.assertNotIn("field_object_id", by_name["slot"])
        self.assertNotIn("field_object_id", by_name["param"])
        self.assertEqual("automatic_storage", by_name["slot"]["origin"]["kind"])
        evidence = {row["id"]: row for row in facts["evidence"]}
        for row in calls:
            locations = [evidence[key]["location"] for key in row["evidence_ids"]]
            for edge in row["calls"]:
                self.assertIn(edge["location"], locations)
                self.assertIn(edge["argument_location"], locations)

    def test_unknown_redirected_and_ambiguous_call_targets_are_unbound(self):
        facts = self.analyze()
        edges = [edge for row in facts["direct_calls"]["bindings"] for edge in row["calls"]]
        self.assertEqual({"from_left", "from_right", "from_local", "forward"}, {edge["caller"] for edge in edges})
        self.assertEqual([], self.analyze(duplicate_leaf=True)["direct_calls"]["bindings"])
