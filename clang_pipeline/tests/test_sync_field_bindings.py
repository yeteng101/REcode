from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from clang_pipeline.clang_ast import run_clang_ast
from clang_pipeline.semantic_sync import SemanticSyncAnalyzer


SOURCE = """struct Slot { int value; };
static struct Slot first, second;
extern void change(int **);

int confirmed(struct Slot *input) {
    int *alias = &first.value;
    int *copy = alias;
    *copy = 2;
    int a = *alias;
    int b = *(&second.value);
    _Atomic int *pending;
    pending = (_Atomic int *) &input->value;
    return a + b + __c11_atomic_load(pending, __ATOMIC_RELAXED);
}

int local(void) {
    struct Slot receiver = {8};
    return *(&receiver.value);
}

int uncertain(int flag) {
    int *conditional;
    if (flag) { conditional = &first.value; }
    int *reassigned = &first.value;
    reassigned = &second.value;
    int *escaped = &first.value;
    change(&escaped);
    int *different_type = (int *)(short *)&first.value;
    return *conditional + *reassigned + *escaped + *different_type;
}

int nested(struct Slot *input) {
    int *outer = &input->value;
    while (*outer) {
        int *inner = &second.value;
        return *inner;
    }
    return *outer;
}

int bypass(int flag) {
    int *p;
    if (flag) goto after;
    p = &first.value;
after:
    return *p;
}

int main(void) { return 0; }
"""


@unittest.skipUnless(shutil.which("clang"), "real Clang compiler required")
class SyncFieldBindingTests(unittest.TestCase):
    def analyze(self, *, duplicate=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "field.c"
            path.write_text(SOURCE, encoding="utf-8")
            unit = {"file": str(path), "directory": str(root), "arguments": ["clang", "-std=c11", "-c", str(path)]}
            asts = [(path.name, run_clang_ast(unit, root)) for _ in range(2 if duplicate else 1)]
            return SemanticSyncAnalyzer(root, [unit]).analyze(asts)

    def test_binding_follows_field_address_and_pointer_copy_without_merging_instances(self):
        result = self.analyze()
        bindings = result["field_bindings"]
        accesses = [row for row in bindings["accesses"] if row["function"] == "confirmed"]
        write, copied_read, distinct_read, atomic = accesses
        self.assertEqual(["write", "read", "read", "atomic_target"], [row["access"] for row in accesses])
        self.assertEqual([6, 7], [site["line"] for site in write["alias_steps"]])
        self.assertEqual(write["field_object_id"], copied_read["field_object_id"])
        self.assertNotEqual(write["field_object_id"], distinct_read["field_object_id"])
        self.assertEqual(1, len({row["field"]["id"] for row in accesses}))
        self.assertEqual("parameter_pointer", atomic["receiver"]["kind"])
        self.assertEqual("input", atomic["receiver"]["name"])
        self.assertEqual(12, atomic["address_formation"]["line"])
        self.assertEqual("load", atomic["operation"])
        self.assertNotIn("field_object_id", atomic)
        local = next(row for row in bindings["accesses"] if row["function"] == "local")
        self.assertEqual("automatic_storage", local["receiver"]["kind"])
        self.assertNotIn("field_object_id", local)
        evidence = {item["id"]: item for item in bindings["evidence"]}
        for row in bindings["accesses"]:
            sites = {tuple(evidence[key]["location"].items()) for key in row["evidence_ids"]}
            for location in [row["location"], row["field"]["declaration"], row["address_formation"], *row["alias_steps"]]:
                self.assertIn(tuple(location.items()), sites)
        self.assertEqual([], result["findings"][0]["shared_objects"])

    def test_conditional_reassigned_escaped_and_jump_bypassed_aliases_stay_unbound(self):
        bindings = self.analyze(duplicate=True)["field_bindings"]
        self.assertEqual(self.analyze()["field_bindings"], bindings)
        self.assertEqual(1, bindings["coverage"]["functions_skipped_for_jumps_or_asm"])
        self.assertEqual({"confirmed", "local", "nested"}, {row["function"] for row in bindings["accesses"]})
        nested = [row for row in bindings["accesses"] if row["function"] == "nested"]
        self.assertEqual(["input", "second", "input"], [row["receiver"]["name"] for row in nested])
