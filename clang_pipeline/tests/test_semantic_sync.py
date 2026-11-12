from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from clang_pipeline.clang_ast import run_clang_ast
from clang_pipeline.semantic_sync import SemanticSyncAnalyzer


SOURCE = """#include <pthread.h>
int counter;
pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
pthread_mutex_t other = PTHREAD_MUTEX_INITIALIZER;
void *writer_a(void *arg) {
    pthread_mutex_lock(&mutex);
    counter++;
    pthread_mutex_unlock(&mutex);
    return 0;
}
void *writer_b(void *arg) {
    pthread_mutex_lock(&mutex);
    counter++;
    pthread_mutex_unlock(&mutex);
    return 0;
}
int main(int argc, char **argv) {
    pthread_t a, b;
    if (pthread_create(&a, 0, writer_a, 0) != 0) return 1;
    if (pthread_create(&b, 0, writer_b, 0) != 0) return 1;
    if (pthread_join(a, 0) != 0) return 1;
    if (pthread_join(b, 0) != 0) return 1;
    return counter;
}
"""


@unittest.skipUnless(shutil.which("clang"), "real Clang compiler required")
class SemanticSyncTests(unittest.TestCase):
    def analyze(self, source=SOURCE, files=None, **limits):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = {"workers.c": source, **(files or {})}
            for name, content in sources.items():
                (root / name).write_text(content, encoding="utf-8")
            asts, units = [], []
            for name in sources:
                if not name.endswith((".c", ".cpp")):
                    continue
                unit = {"file": str(root / name), "directory": str(root),
                        "arguments": ["clang++" if name.endswith(".cpp") else "clang", "-pthread", "-I", str(root), "-c", str(root / name)]}
                units.append(unit)
                asts.append((name, run_clang_ast(unit, root)))
            analyzer = SemanticSyncAnalyzer(root, units, **limits)
            return analyzer.analyze(asts)

    def accesses(self, result, name="counter"):
        return [a for obj in result["findings"][0]["shared_objects"] if obj["name"] == name for a in obj["accesses"]]

    def worker_accesses(self, result):
        return [a for a in self.accesses(result) if a["function"].startswith("writer")]

    def test_common_lock_is_ast_bound_and_held_at_both_writes(self):
        result = self.analyze()
        self.assertEqual({"complete": True, "gaps": []}, result["coverage"])
        writes = self.worker_accesses(result)
        self.assertEqual(2, len(writes))
        self.assertTrue(all(len(a["must_lockset"]) == 1 for a in writes))
        self.assertEqual(writes[0]["must_lockset"], writes[1]["must_lockset"])
        first, second = (a["execution_context"] for a in writes)
        self.assertIn(second["context_id"], first["may_run_concurrently_with"])
        main_reads = [a for a in self.accesses(result) if a["function"] == "main"]
        self.assertTrue(main_reads)
        self.assertTrue(all(first["context_id"] in a["execution_context"]["cannot_run_concurrently_with"] for a in main_reads))

    def test_distinct_locks_remain_distinct(self):
        before, worker = SOURCE.split("void *writer_b", 1)
        result = self.analyze(before + "void *writer_b" + worker.replace("&mutex", "&other"))
        self.assertTrue(result["coverage"]["complete"])
        writes = self.worker_accesses(result)
        self.assertNotEqual(writes[0]["must_lockset"], writes[1]["must_lockset"])

    def test_missing_lock_has_empty_must_set(self):
        before, worker = SOURCE.split("void *writer_b", 1)
        worker = worker.replace("    pthread_mutex_lock(&mutex);\n", "").replace("    pthread_mutex_unlock(&mutex);\n", "")
        result = self.analyze(before + "void *writer_b" + worker)
        self.assertTrue(result["coverage"]["complete"])
        write = next(a for a in self.worker_accesses(result) if a["function"] == "writer_b")
        self.assertEqual([], write["must_lockset"])

    def test_conditional_protection_intersects_paths(self):
        source = SOURCE.replace("    pthread_mutex_lock(&mutex);", "    if (arg) pthread_mutex_lock(&mutex);", 1)
        source = source.replace("    pthread_mutex_unlock(&mutex);", "    if (arg) pthread_mutex_unlock(&mutex);", 1)
        # A non-null argument is not supplied, so the lock is skipped on the actual path.
        result = self.analyze(source)
        write = next(a for a in self.worker_accesses(result) if a["function"] == "writer_a")
        self.assertEqual([], write["must_lockset"])

    def test_shadowed_local_does_not_become_global_access(self):
        source = SOURCE.replace("void *writer_a(void *arg) {", "void *writer_a(void *arg) {\n    int counter = 0;")
        result = self.analyze(source)
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(["writer_b"], [a["function"] for a in self.worker_accesses(result)])

    def test_simple_pointer_alias_uses_target_declaration(self):
        source = SOURCE.replace("    counter++;", "    int *alias = &counter;\n    (*alias)++;", 1)
        result = self.analyze(source)
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(2, len(self.worker_accesses(result)))
        self.assertEqual(1, len(result["findings"][0]["shared_objects"]))

    def test_lock_alias_and_parameter_alias_preserve_identity(self):
        source = SOURCE.replace("    counter++;", "    update(&counter);", 1)
        source = source.replace("void *writer_a", "void update(int *value) { (*value)++; }\nvoid *writer_a", 1)
        source = source.replace("    pthread_mutex_lock(&mutex);", "    pthread_mutex_t *lock_alias = &mutex;\n    pthread_mutex_lock(lock_alias);", 1)
        result = self.analyze(source)
        self.assertTrue(result["coverage"]["complete"], result["coverage"])
        helper = next(a for a in self.accesses(result) if a["function"] == "update")
        self.assertEqual(1, len(helper["must_lockset"]))

    def test_caller_local_update_through_output_parameter_is_preserved(self):
        source = SOURCE.replace("void *writer_a", "void clear(int *value) { *value = 0; }\nvoid *writer_a", 1)
        source = source.replace("    counter++;", "    int flag = 1;\n    clear(&flag);\n    if (flag) counter++;", 1)
        result = self.analyze(source)
        self.assertTrue(result["coverage"]["complete"], result["coverage"])
        self.assertEqual(["writer_b"], [a["function"] for a in self.worker_accesses(result)])

    def test_same_named_static_globals_in_two_files_remain_distinct(self):
        source = SOURCE.replace("int counter;", "static int counter;")
        source = source.replace("void *writer_b(void *arg) {\n    pthread_mutex_lock(&mutex);\n    counter++;\n    pthread_mutex_unlock(&mutex);\n    return 0;\n}", "void *writer_b(void *arg);")
        result = self.analyze(source, {"other.c": "static int counter;\nvoid *writer_b(void *arg) { counter++; return 0; }\n"})
        self.assertTrue(result["coverage"]["complete"], result["coverage"])
        objects = [o for o in result["findings"][0]["shared_objects"] if o["name"] == "counter"]
        self.assertEqual(2, len(objects))
        self.assertEqual(2, len({o["object_id"] for o in objects}))

    def test_cross_file_extern_and_lock_propagate_through_helper(self):
        source = SOURCE.replace("    counter++;", "    update();", 1)
        source = source.replace("void *writer_a", "void update(void);\nvoid *writer_a", 1)
        result = self.analyze(source, {"helper.c": "extern int counter;\nvoid update(void) { counter++; }\n"})
        self.assertTrue(result["coverage"]["complete"])
        helper = next(a for a in self.accesses(result) if a["function"] == "update")
        self.assertEqual("helper.c", helper["location"]["file"])
        self.assertEqual(1, len(helper["must_lockset"]))
        self.assertEqual(1, len(result["findings"][0]["shared_objects"]))

    def test_repeated_entry_at_two_creation_sites_is_concurrent(self):
        result = self.analyze(SOURCE.replace("0, writer_b, 0", "0, writer_a, 0"))
        self.assertTrue(result["coverage"]["complete"])
        writes = self.worker_accesses(result)
        self.assertEqual(2, len(writes))
        first, second = (a["execution_context"] for a in writes)
        self.assertNotEqual(first["context_id"], second["context_id"])
        self.assertIn(second["context_id"], first["may_run_concurrently_with"])

    def test_successfully_joined_workers_do_not_overlap(self):
        source = SOURCE.replace("    if (pthread_create(&b, 0, writer_b, 0) != 0) return 1;\n    if (pthread_join(a, 0) != 0) return 1;",
                                "    if (pthread_join(a, 0) != 0) return 1;\n    if (pthread_create(&b, 0, writer_b, 0) != 0) return 1;")
        result = self.analyze(source)
        self.assertTrue(result["coverage"]["complete"])
        first, second = (a["execution_context"] for a in self.worker_accesses(result))
        self.assertIn(second["context_id"], first["cannot_run_concurrently_with"])
        self.assertNotIn(second["context_id"], first["may_run_concurrently_with"])

    def test_unsupported_loop_and_unresolved_callback_keep_gaps(self):
        for source in (SOURCE.replace("    counter++;", "    while (arg) counter++;", 1),
                       SOURCE.replace("    counter++;", "    ((void (*)(void))arg)();", 1)):
            result = self.analyze(source)
            self.assertFalse(result["coverage"]["complete"])
            self.assertTrue(result["coverage"]["gaps"])
            self.assertTrue(all(a["must_lockset"] == [] for a in self.accesses(result)))

    def test_budget_exhaustion_retains_gap_and_clears_must_proofs(self):
        result = self.analyze(max_paths=1)
        self.assertFalse(result["coverage"]["complete"])
        self.assertIn("path_budget_exhausted", result["coverage"]["gaps"])
        self.assertTrue(all(a["must_lockset"] == [] for a in self.accesses(result)))

    def test_uninitialized_lock_cannot_be_certified(self):
        result = self.analyze(SOURCE.replace("mutex = PTHREAD_MUTEX_INITIALIZER", "mutex"))
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(any("lock_initialization_unproved" in gap for gap in result["coverage"]["gaps"]))

    def test_unknown_helper_cannot_preserve_common_lock_proof(self):
        source = SOURCE.replace("void *writer_a", "extern void opaque(void);\nvoid *writer_a", 1)
        source = source.replace("    counter++;", "    opaque();\n    counter++;", 1)
        result = self.analyze(source)
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(all(a["must_lockset"] == [] for a in self.accesses(result)))

    def test_unsequenced_call_arguments_are_explicitly_unsupported(self):
        source = SOURCE.replace("void *writer_a", "int value(void) { return 0; }\nvoid pair(int a, int b) {}\nvoid *writer_a", 1)
        source = source.replace("    counter++;", "    pair(value(), counter);\n    counter++;", 1)
        result = self.analyze(source)
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(any("unsequenced_call_arguments" in gap for gap in result["coverage"]["gaps"]))

    def test_checked_default_lock_initialization(self):
        source = SOURCE.replace("mutex = PTHREAD_MUTEX_INITIALIZER", "mutex")
        source = source.replace("    pthread_t a, b;", "    pthread_t a, b;\n    if (pthread_mutex_init(&mutex, 0) != 0) return 1;")
        result = self.analyze(source)
        self.assertTrue(result["coverage"]["complete"], result["coverage"])
        self.assertTrue(all(a["must_lockset"] for a in self.worker_accesses(result)))

    def test_compound_assignment_condition_does_not_inherit_rhs_value(self):
        source = SOURCE.replace("    counter++;", "    if (counter += 1) { counter++; } else { counter--; }", 1)
        result = self.analyze(source)
        self.assertTrue(result["coverage"]["complete"], result["coverage"])
        self.assertEqual(3, len([a for a in self.worker_accesses(result) if a["function"] == "writer_a"]))

    def test_cpp_implicit_execution_is_not_certified_by_c_model(self):
        result = self.analyze(files={"extra.cpp": "struct Object { Object() {} };\nObject instance;\n"})
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(any("C_plus_plus" in gap for gap in result["coverage"]["gaps"]))

    def test_thread_captured_stack_storage_remains_a_gap(self):
        source = SOURCE.replace("    pthread_t a, b;", "    pthread_t a, b;\n    int local = 0;")
        source = source.replace("0, writer_a, 0", "0, writer_a, &local")
        result = self.analyze(source)
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(any("thread_argument_escapes_local_storage" in gap for gap in result["coverage"]["gaps"]))

    def test_nested_lock_order_is_an_explicit_gap(self):
        source = SOURCE.replace("    counter++;", "    pthread_mutex_lock(&other);\n    counter++;\n    pthread_mutex_unlock(&other);", 1)
        result = self.analyze(source)
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(any("nested_lock_order_analysis_unavailable" in gap for gap in result["coverage"]["gaps"]))

    def test_pthread_join_output_is_a_shared_write(self):
        source = """#include <pthread.h>
void *shared_result;
void *finished(void *arg) { return 0; }
void *reader(void *arg) { return shared_result; }
int main(void) {
    pthread_t a, b;
    if (pthread_create(&a, 0, finished, 0) != 0) return 1;
    if (pthread_create(&b, 0, reader, 0) != 0) return 1;
    if (pthread_join(a, &shared_result) != 0) return 1;
    if (pthread_join(b, 0) != 0) return 1;
    return 0;
}
"""
        result = self.analyze(source)
        self.assertTrue(result["coverage"]["complete"], result["coverage"])
        accesses = self.accesses(result, "shared_result")
        writes = [a for a in accesses if a["kind"] == "write" and a["function"] == "main"]
        reads = [a for a in accesses if a["kind"] == "read" and a["function"] == "reader"]
        self.assertTrue(writes)
        self.assertTrue(reads)
        reader_context = reads[0]["execution_context"]["context_id"]
        self.assertTrue(any(reader_context in a["execution_context"]["may_run_concurrently_with"] for a in writes))
        self.assertTrue(all("pthread_join" in a["location"]["snippet"] for a in writes))

    def test_thrd_join_invalidates_old_local_result_before_branching(self):
        source = """#include <threads.h>
int counter;
int worker(void *arg) { return 1; }
int main(void) {
    thrd_t t;
    int result = 0;
    if (thrd_create(&t, worker, 0) != 0) return 1;
    if (thrd_join(t, &result) != 0) return 1;
    if (result) counter = 1;
    return 0;
}
"""
        result = self.analyze(source)
        self.assertTrue(result["coverage"]["complete"], result["coverage"])
        self.assertTrue(any(a["kind"] == "write" for a in self.accesses(result)))

    def test_pthread_join_invalidates_old_local_pointer(self):
        source = """#include <pthread.h>
int counter;
void *worker(void *arg) { return 0; }
int main(void) {
    pthread_t t;
    void *result = 0;
    if (pthread_create(&t, 0, worker, 0) != 0) return 1;
    if (pthread_join(t, &result) != 0) return 1;
    if (result) counter = 1;
    return 0;
}
"""
        result = self.analyze(source)
        self.assertTrue(result["coverage"]["complete"], result["coverage"])
        self.assertTrue(any(a["kind"] == "write" for a in self.accesses(result)))

    def test_unresolved_join_output_retains_a_gap(self):
        source = """#include <threads.h>
int counter;
int worker(void *arg) { return 1; }
int main(void) {
    thrd_t t;
    int *output;
    if (thrd_create(&t, worker, 0) != 0) return 1;
    if (thrd_join(t, output) != 0) return 1;
    counter = 1;
    return 0;
}
"""
        result = self.analyze(source)
        self.assertFalse(result["coverage"]["complete"])
        self.assertTrue(any("unresolved_join_output" in gap for gap in result["coverage"]["gaps"]))


if __name__ == "__main__":
    unittest.main()
