from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clang_pipeline.clang_ast import run_clang_ast
from clang_pipeline.semantic_sync import SemanticSyncAnalyzer


SOURCE = """#include <stdatomic.h>
typedef struct { int count; _Atomic int flag; } Slot;
static int atomic_like(int value) { return value; }
static int inspect(Slot *slot, int *other, memory_order order) {
    slot->count = 1;
    slot->count += 2;
    int plain = (slot->count);
    int *address = &(slot->count);
    int expected = 0;
    __c11_atomic_store(&slot->flag, plain, memory_order_release);
    int loaded = __c11_atomic_load(&slot->flag, order);
    __c11_atomic_compare_exchange_weak(&slot->flag, &expected, loaded,
                                       memory_order_acq_rel, memory_order_acquire);
    __atomic_compare_exchange_n(other, &expected, loaded, 0, __ATOMIC_RELEASE, __ATOMIC_RELAXED);
    atomic_fetch_add(&slot->flag, 1);
    return atomic_like(*address);
}
int main(void) { return 0; }
"""


@unittest.skipUnless(shutil.which("clang"), "real Clang compiler required")
class SyncSourceObservationTests(unittest.TestCase):
    def analyze(self, *, duplicate=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.c"
            source.write_text(SOURCE, encoding="utf-8")
            unit = {"file": str(source), "directory": str(root),
                    "arguments": ["clang", "-std=c11", "-c", str(source)]}
            asts = [(source.name, run_clang_ast(unit, root))]
            if duplicate:
                asts.append((source.name, run_clang_ast(unit, root)))
            return SemanticSyncAnalyzer(root, [unit]).analyze(asts)

    def test_records_unreached_field_and_atomic_sites_without_affecting_path_facts(self):
        result = self.analyze()
        observations = result["source_observations"]
        self.assertEqual({"complete": True, "gaps": []}, result["coverage"])
        self.assertEqual([], result["findings"][0]["shared_objects"])
        fields = observations["member_accesses"]
        self.assertEqual([(5, "write"), (6, "read_write"), (7, "read"), (8, "address_taken")],
                         [(row["location"]["line"], row["access"]) for row in fields if row["member"] == "count"])
        self.assertEqual({"address_taken"}, {row["access"] for row in fields if row["member"] == "flag"})
        atomics = observations["atomic_operations"]
        self.assertEqual([("store", "release", None), ("load", None, None),
                          ("compare_exchange", "acq_rel", "acquire"), ("compare_exchange", "release", "relaxed")],
                         [(row["operation"], row["memory_order"], row.get("failure_memory_order")) for row in atomics[:4]])
        self.assertEqual(5, len(atomics))
        macro = atomics[-1]
        self.assertEqual(15, macro["location"]["line"])
        if macro["operator"]:
            self.assertEqual(("fetch_add", "seq_cst"), (macro["operation"], macro["memory_order"]))
        else:
            self.assertIsNone(macro["memory_order"])
        evidence = {item["id"]: item for item in observations["evidence"]}
        self.assertTrue(set(evidence).isdisjoint(result["findings"][0]["evidence_ids"]))
        self.assertEqual(len(fields) + len(atomics), len(evidence))
        for row in fields + atomics:
            self.assertEqual("inspect", row["function"])
            self.assertEqual("sample.c", row["location"]["file"])
            self.assertGreater(row["location"]["column"], 0)
            self.assertEqual(row["location"], evidence[row["evidence_ids"][0]]["location"])

    def test_repeated_compiles_deduplicate_and_budgets_are_reported_per_kind(self):
        single = self.analyze()["source_observations"]
        self.assertEqual(single, self.analyze(duplicate=True)["source_observations"])
        with patch("clang_pipeline.sync_source_observations.MAX_PER_FILE_PER_KIND", 3):
            with patch.dict("clang_pipeline.sync_source_observations.LIMITS", {"atomic_operations": 2}):
                observations = self.analyze(duplicate=True)["source_observations"]
        self.assertEqual(single["member_accesses"][:3], observations["member_accesses"])
        self.assertEqual(single["atomic_operations"][:2], observations["atomic_operations"])
        self.assertEqual(["atomic_operations", "member_accesses"], observations["coverage"]["truncated_kinds"])
