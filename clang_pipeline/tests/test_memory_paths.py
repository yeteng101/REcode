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


TABLE_SOURCE = """#include <stddef.h>
#include <stdlib.h>
struct allocator {
    void *(*allocate)(size_t);
    void (*release)(void *);
};
static struct allocator table = {malloc, free};
int entry(int fail) {
    void *p = table.allocate(8);
    if (!p) return -1;
    if (fail) return -2;
    table.release(p);
    return 0;
}
"""


CLEANUP_SOURCE = """#include <stdlib.h>
struct item {
    char *name;
    char *value;
};
void cleanup(struct item *items, int count) {
    int i;
    for (i = 0; i < count; i++)
        free(items[i].name);
    free(items);
}
"""


EXTERNAL_SOURCE = """#include <stddef.h>
extern char **environ;
int entry(void) {
    return environ[0] != NULL;
}
"""


CONTAINER_SOURCE = """#include <stdlib.h>
struct item {
    char *name;
    char *value;
};
int entry(void) {
    struct item *items = calloc(2, sizeof(struct item));
    if (!items) return -1;
    char *first = malloc(4);
    if (!first) {
        free(items);
        return -1;
    }
    items[0].name = first;
    char *second = malloc(4);
    if (!second) {
        free(items);
        return -1;
    }
    items[1].name = second;
    free(second);
    free(first);
    free(items);
    return 0;
}
"""


UNKNOWN_TABLE_SOURCE = """#include <stddef.h>
#include <stdlib.h>
struct allocator {
    void *(*allocate)(size_t);
    void (*release)(void *);
};
extern void *external_alloc(size_t);
static struct allocator table = {external_alloc, free};
int entry(int size) {
    void *p = table.allocate(size);
    if (!p) return -1;
    table.release(p);
    return 0;
}
"""


MUTABLE_TABLE_SOURCE = """#include <stddef.h>
#include <stdlib.h>
struct allocator {
    void *(*allocate)(size_t);
    void (*release)(void *);
};
static struct allocator table = {malloc, free};
void install(void *(*fn)(size_t)) {
    table.allocate = fn;
}
int entry(void) {
    void *p = table.allocate(8);
    if (!p) return -1;
    table.release(p);
    return 0;
}
"""


CONST_TABLE_SOURCE = """#include <stddef.h>
#include <stdlib.h>
struct allocator {
    void *(*allocate)(size_t);
    void (*release)(void *);
};
const struct allocator table = {malloc, free};
int entry(int fail) {
    void *p = table.allocate(8);
    if (!p) return -1;
    if (fail) return -2;
    table.release(p);
    return 0;
}
"""


REGION_SOURCE = """#include <stdlib.h>
struct item {
    char *name;
};
void adopt(struct item *slot, int take) {
    char *name = malloc(4);
    if (!name) return;
    slot->name = name;
    if (take) free(slot);
}
"""


ERRNO_FIRST_READ_SOURCE = """int *__errno_location(void);
int entry(void) {
    int saved = *__errno_location();
    return saved;
}
"""


ERRNO_WRITE_THEN_READ_SOURCE = """int *__errno_location(void);
int entry(int code) {
    *__errno_location() = code;
    return *__errno_location();
}
"""


HPR_STYLE_SOURCE = """#include <stddef.h>
#include <stdlib.h>
#include <string.h>
struct item {
    char *name;
    char *value;
};
extern char **environ;
void *uv__calloc(size_t count, size_t size);
char *uv__strdup(const char *s);
void uv__free(void *p);
int entry(struct item **out, int *count) {
    int i, j, cnt;
    *out = 0;
    *count = 0;
    for (i = 0; environ[i] != 0; i++);
    *out = uv__calloc(i, sizeof(struct item));
    if (out == 0) return -1;
    for (j = 0, cnt = 0; j < i; j++) {
        char *buf;
        char *ptr;
        if (environ[j] == 0) break;
        buf = uv__strdup(environ[j]);
        if (buf == 0) goto fail;
        ptr = strchr(buf, '=');
        if (ptr == 0) {
            uv__free(buf);
            continue;
        }
        *ptr = 0;
        (*out)[cnt].name = buf;
        (*out)[cnt].value = ptr + 1;
        cnt++;
    }
    *count = cnt;
    return 0;
fail:
    for (i = 0; i < cnt; i++) {
        struct item *envitem = &(*out)[cnt];
        uv__free(envitem->name);
    }
    uv__free(*out);
    *out = 0;
    *count = 0;
    return -2;
}
"""


LIBUV_ALLOCATOR_SOURCE = """#include <stdlib.h>
#include <string.h>
static struct {
    void *(*local_malloc)(size_t);
    void *(*local_calloc)(size_t, size_t);
    void (*local_free)(void *);
} uv__allocator = { malloc, calloc, free };
static void *uv__malloc(size_t size) {
    return uv__allocator.local_malloc(size);
}
void *uv__calloc(size_t count, size_t size) {
    return uv__allocator.local_calloc(count, size);
}
char *uv__strdup(const char *s) {
    size_t len = strlen(s) + 1;
    char *m = uv__malloc(len);
    if (!m) return NULL;
    return memcpy(m, s, len);
}
void uv__free(void *p) {
    uv__allocator.local_free(p);
}
"""


POINTER_WRITE_SOURCE = """#include <stddef.h>
#include <stdlib.h>
struct allocator {
    void *(*allocate)(size_t);
    void (*release)(void *);
};
static struct allocator table = {malloc, free};
void install(void *(*fn)(size_t)) {
    void *(**slot)(size_t) = &table.allocate;
    *slot = fn;
}
int entry(void) {
    void *p = table.allocate(8);
    if (!p) return -1;
    table.release(p);
    return 0;
}
"""


TABLE_DEFINITION_UNIT = """#include <stddef.h>
#include <stdlib.h>
struct allocator {
    void *(*allocate)(size_t);
    void (*release)(void *);
};
struct allocator table = {malloc, free};
"""


TABLE_WRITER_UNIT = """#include <stddef.h>
struct allocator {
    void *(*allocate)(size_t);
    void (*release)(void *);
};
extern struct allocator table;
void install(void *(*fn)(size_t)) {
    table.allocate = fn;
}
"""


TABLE_READER_UNIT = """#include <stddef.h>
struct allocator {
    void *(*allocate)(size_t);
    void (*release)(void *);
};
extern struct allocator table;
int entry(void) {
    void *p = table.allocate(8);
    if (!p) return -1;
    table.release(p);
    return 0;
}
"""


NULL_WRITE_SOURCE = """#include <stdlib.h>
struct item { char *name; };
int entry(void) {
    struct item *p = malloc(sizeof(struct item));
    if (!p) {
        p->name = 0;
        return -1;
    }
    free(p);
    return 0;
}
"""


GUARDED_NULL_SOURCE = """#include <stdlib.h>
struct item { char *name; };
int entry(void) {
    struct item *p = malloc(sizeof(struct item));
    if (!p) return -1;
    p->name = 0;
    free(p);
    return 0;
}
"""


OTHER_NULL_SOURCE = """#include <stdlib.h>
struct item { char *name; };
int entry(void) {
    struct item *p = malloc(sizeof(struct item));
    struct item *q = malloc(sizeof(struct item));
    if (!p) {
        if (q) q->name = 0;
        return -1;
    }
    free(p);
    free(q);
    return 0;
}
"""


UNBOUND_NULL_SOURCE = """#include <stdlib.h>
struct item { char *name; };
int entry(void) {
    ((struct item *)malloc(sizeof(struct item)))->name = 0;
    return 0;
}
"""


COMPARED_NULL_SOURCE = """#include <stdlib.h>
struct item { char *name; };
int entry(void) {
    struct item *p = malloc(sizeof(struct item));
    if (p == NULL) {
        p->name = 0;
        return -1;
    }
    free(p);
    return 0;
}
"""


@unittest.skipUnless(shutil.which("clang"), "real Clang compiler required")
class MemoryPathTests(unittest.TestCase):
    def analyze(self, source, entry="entry"):
        return self.analyze_files({"case.c": source}, entry)

    def analyze_files(self, sources, entry="entry"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            units, asts = [], []
            for name, text in sources.items():
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(text, encoding="utf-8")
                unit = {"file": str(file), "directory": str(root),
                        "arguments": ["clang", "-std=c11", "-c", str(file)]}
                units.append(unit)
                asts.append((name, run_clang_ast(unit, root)))
            return analyze_memory_paths(root, (units, asts, [], []), {}, [entry])

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

    def test_allocator_table_binds_indirect_builtins_and_reports_leaks(self):
        result, evidence = self.analyze(TABLE_SOURCE)
        self.assertEqual([], result["gaps"])
        normal = next(trace for trace in result["traces"]
                      if trace["events"][-1]["location"]["snippet"] == "return 0;")
        failure = next(trace for trace in result["traces"]
                       if "return -2;" in trace["events"][-1]["location"]["snippet"])
        acquire = next(event for event in normal["events"] if event["kind"] == "acquire")
        release = next(event for event in normal["events"] if event["kind"] == "release")
        self.assertEqual(acquire["resource_instance_id"], release["resource_instance_id"])
        self.assertEqual([], normal["events"][-1]["live_roots"])
        # The error return keeps the same object live for the caller to see.
        self.assertEqual([acquire["resource_instance_id"]], failure["events"][-1]["live_roots"])
        call = next(event for event in normal["events"]
                    if event["kind"] == "call" and event.get("target") == "malloc")
        self.assertTrue(call["indirect"])
        self.assertEqual("allocate", call["callback"]["field"])
        self.assertEqual("record", call["callback"]["kind"])
        self.assertEqual(acquire["location"], call["location"])
        self.assertEqual(1, len(release["evidence_ids"]))
        ids = {item["id"] for item in evidence}
        self.assertTrue(all(set(event["evidence_ids"]) <= ids
                            for trace in result["traces"] for event in trace["events"]))

    def test_cleanup_entry_tracks_caller_region_until_release(self):
        result, _ = self.analyze(CLEANUP_SOURCE, entry="cleanup")
        self.assertEqual([], result["gaps"])
        self.assertEqual(2, len(result["traces"]))
        empty, one = sorted(result["traces"], key=lambda trace: trace["arguments"][1])
        self.assertEqual(["caller_input", 0], empty["arguments"])
        self.assertEqual("caller_input", empty["events"][0]["kind"])
        self.assertEqual("caller_input", empty["events"][0]["api"])
        self.assertEqual([], empty["events"][-1]["live_roots"])
        releases = [event for event in one["events"] if event["kind"] == "release"]
        self.assertEqual(2, len(releases))
        self.assertTrue(any(event.get("external") for event in releases))
        self.assertEqual([], one["events"][-1]["live_roots"])

    def test_external_global_uses_a_bounded_extent_assumption(self):
        result, _ = self.analyze(EXTERNAL_SOURCE)
        self.assertEqual(1, len(result["traces"]))
        self.assertTrue(any("external_value_extent_assumed:environ=2" in gap
                            for gap in result["gaps"]))
        self.assertEqual([], result["traces"][0]["events"][-1]["live_roots"])

    def test_wrapped_allocator_and_cleanup_index_expose_the_failure_leak(self):
        result, _ = self.analyze_files({"case.c": HPR_STYLE_SOURCE,
                                        "src/uv-common.c": LIBUV_ALLOCATOR_SOURCE})
        self.assertTrue(any("external_value_extent_assumed:environ=2" in gap
                            for gap in result["gaps"]))
        leaks = [trace for trace in result["traces"]
                 if trace["events"][-1]["live_roots"]]
        self.assertEqual(1, len(leaks))
        leak = leaks[0]
        self.assertTrue(any(event["kind"] == "release_container"
                            for event in leak["events"]))
        self.assertEqual(1, len(leak["events"][-1]["live_roots"]))

    def test_named_wrapper_outside_libuv_does_not_create_an_allocation(self):
        result, _ = self.analyze("""
int uv__strdup(int s) { return s; }
int entry(void) { uv__strdup(1); return 0; }
""")
        self.assertTrue(result["traces"])
        self.assertFalse(any(event["kind"] == "acquire"
                             for trace in result["traces"] for event in trace["events"]))

    def test_opaque_free_releases_modeled_caller_region(self):
        source = REGION_SOURCE.replace("void adopt(", "void uv__free(void *);\nvoid adopt(")
        source = source.replace("free(slot);", "uv__free(slot);")
        result, _ = self.analyze_files({"case.c": source,
                                        "src/uv-common.c": LIBUV_ALLOCATOR_SOURCE}, entry="adopt")
        trace = next(item for item in result["traces"] if item["events"][-1]["live_roots"])
        release = next(event for event in trace["events"] if event["kind"] == "release")
        self.assertTrue(release["resource_instance_id"].startswith("ast_input_"))
        self.assertNotIn("external", release)

    def test_unknown_allocator_entry_keeps_the_call_gap(self):
        result, _ = self.analyze(UNKNOWN_TABLE_SOURCE)
        self.assertEqual([], result["traces"])
        self.assertTrue(any("unmodeled_call:external_alloc" in gap for gap in result["gaps"]))

    def test_container_release_without_child_release_is_a_live_root(self):
        result, _ = self.analyze(CONTAINER_SOURCE)
        self.assertEqual([], result["gaps"])
        normal = next(trace for trace in result["traces"]
                      if trace["events"][-1]["location"]["snippet"] == "return 0;")
        self.assertEqual([], normal["events"][-1]["live_roots"])
        leaked = [trace for trace in result["traces"]
                  if trace["events"][-1]["live_roots"]]
        self.assertEqual(1, len(leaked))
        roots = leaked[0]["events"][-1]["live_roots"]
        acquire = next(event for event in leaked[0]["events"]
                       if event["kind"] == "acquire" and event["resource_instance_id"] in roots)
        self.assertEqual(1, len(roots))
        self.assertFalse(any(event["kind"] == "release" and event["resource_instance_id"] == roots[0]
                             for event in leaked[0]["events"]))
        self.assertTrue(any(event["kind"] == "release_container" and event["resource_instance_id"] == roots[0]
                            for event in leaked[0]["events"]))
        self.assertIn("first = malloc", acquire["location"]["snippet"])

    def test_mutable_table_entry_is_not_resolved_to_the_default(self):
        result, _ = self.analyze(MUTABLE_TABLE_SOURCE)
        self.assertEqual([], result["traces"])
        gap = next(item for item in result["gaps"] if "table_stability_unverified:table.allocate" in item)
        write_line = MUTABLE_TABLE_SOURCE.splitlines().index("    table.allocate = fn;") + 1
        self.assertIn(f"written_at:case.c:{write_line}", gap)
        self.assertFalse(any(event.get("target") == "malloc"
                             for trace in result["traces"] for event in trace["events"]))

    def test_pointer_write_to_table_entry_keeps_the_target_unverified(self):
        result, _ = self.analyze(POINTER_WRITE_SOURCE)
        self.assertEqual([], result["traces"])
        gap = next(item for item in result["gaps"]
                   if "table_stability_unverified:table.allocate" in item)
        escape_line = POINTER_WRITE_SOURCE.splitlines().index(
            "    void *(**slot)(size_t) = &table.allocate;") + 1
        self.assertIn(f"address_taken:case.c:{escape_line}", gap)
        self.assertFalse(any(event.get("target") == "malloc"
                             for trace in result["traces"] for event in trace["events"]))

    def test_extern_write_in_another_unit_keeps_the_target_unverified(self):
        result, _ = self.analyze_files({"table.c": TABLE_DEFINITION_UNIT,
                                        "install.c": TABLE_WRITER_UNIT,
                                        "use.c": TABLE_READER_UNIT})
        self.assertEqual([], result["traces"])
        gap = next(item for item in result["gaps"]
                   if "table_stability_unverified:table.allocate" in item)
        write_line = TABLE_WRITER_UNIT.splitlines().index("    table.allocate = fn;") + 1
        self.assertIn(f"install.c:{write_line}", gap)
        self.assertFalse(any(event.get("target") == "malloc"
                             for trace in result["traces"] for event in trace["events"]))

    def test_const_table_binding_stays_resolved(self):
        result, _ = self.analyze(CONST_TABLE_SOURCE)
        self.assertEqual([], result["gaps"])
        normal = next(trace for trace in result["traces"]
                      if trace["events"][-1]["location"]["snippet"] == "return 0;")
        call = next(event for event in normal["events"]
                    if event["kind"] == "call" and event.get("target") == "malloc")
        self.assertTrue(call["indirect"])
        self.assertEqual("record", call["callback"]["kind"])
        self.assertEqual([], normal["events"][-1]["live_roots"])

    def test_caller_region_release_moves_child_ownership(self):
        result, _ = self.analyze(REGION_SOURCE, entry="adopt")
        self.assertEqual([], result["gaps"])
        allocated = [trace for trace in result["traces"]
                     if any(event["kind"] == "acquire" for event in trace["events"])]
        self.assertEqual(2, len(allocated))
        kept, released = sorted(allocated, key=lambda trace: trace["arguments"][1])
        self.assertEqual([], kept["events"][-1]["live_roots"])
        self.assertEqual(1, len(released["events"][-1]["live_roots"]))
        self.assertTrue(any(event["kind"] == "release_container"
                            and event["resource_instance_id"] == released["events"][-1]["live_roots"][0]
                            for event in released["events"]))

    def test_errno_first_read_reports_unknown_state(self):
        result, _ = self.analyze(ERRNO_FIRST_READ_SOURCE)
        self.assertEqual([], result["traces"])
        self.assertTrue(any("uninitialized_cell_read:errno" in gap for gap in result["gaps"]))

    def test_errno_read_after_write_uses_the_written_value(self):
        result, _ = self.analyze(ERRNO_WRITE_THEN_READ_SOURCE)
        self.assertEqual([], result["gaps"])
        self.assertEqual(2, len(result["traces"]))

    def test_null_write_binds_allocation_binding_condition_and_write(self):
        result, evidence = self.analyze(NULL_WRITE_SOURCE)
        self.assertEqual(1, len(result["null_writes"]))
        record = result["null_writes"][0]
        self.assertRegex(record["null_write_id"], r"^ast_nullwrite_[0-9a-f]{20}$")
        self.assertEqual(record["pointer"]["null_id"], record["pointer"]["allocation"]["null_id"])
        self.assertEqual("malloc", record["pointer"]["allocation"]["api"])
        self.assertEqual("p", record["pointer"]["binding"]["declaration"])
        self.assertEqual([4, 5], [item["location"]["line"] for item in record["conditions"]])
        self.assertEqual(["allocation_failure", "branch"], [item["kind"] for item in record["conditions"]])
        self.assertTrue(all(item["taken"] is True for item in record["conditions"]))
        self.assertEqual("name", record["write"]["field"])
        self.assertEqual(6, record["write"]["location"]["line"])
        cited = {item["id"]: item for item in evidence}
        for key in (record["pointer"]["allocation"]["evidence_ids"]
                    + record["pointer"]["binding"]["evidence_ids"]
                    + [key for condition in record["conditions"] for key in condition["evidence_ids"]]
                    + record["write"]["evidence_ids"]):
            self.assertIn(key, cited)
        self.assertIn("ast_memory_null_allocation", {item["kind"] for item in evidence})
        self.assertIn("ast_memory_null_write", {item["kind"] for item in evidence})

    def test_guard_branch_prevents_null_write(self):
        result, _ = self.analyze(GUARDED_NULL_SOURCE)
        self.assertEqual([], result["null_writes"])
        self.assertTrue(result["traces"])
        self.assertFalse(any("null_pointer" in gap for gap in result["gaps"]))

    def test_write_through_another_pointer_is_not_null_write(self):
        result, _ = self.analyze(OTHER_NULL_SOURCE)
        self.assertEqual([], result["null_writes"])
        self.assertTrue(result["traces"])
        self.assertFalse(any("null_pointer" in gap for gap in result["gaps"]))

    def test_unbound_null_write_reports_binding_gap(self):
        result, _ = self.analyze(UNBOUND_NULL_SOURCE)
        self.assertEqual([], result["null_writes"])
        self.assertTrue(any("null_write_binding_unverified" in gap for gap in result["gaps"]))

    def test_compared_null_write_records_comparison_condition(self):
        result, _ = self.analyze(COMPARED_NULL_SOURCE)
        self.assertEqual(1, len(result["null_writes"]))
        comparison = next(item for item in result["null_writes"][0]["conditions"]
                          if item["kind"] == "comparison")
        self.assertEqual("==", comparison["operator"])
        self.assertTrue(comparison["taken"])
