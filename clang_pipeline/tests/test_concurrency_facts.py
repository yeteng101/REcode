from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clang_pipeline.race_detector import analyze_race_source


SOURCE = """int counter;
pthread_mutex_t first = PTHREAD_MUTEX_INITIALIZER;
pthread_mutex_t second = PTHREAD_MUTEX_INITIALIZER;
void *writer(void *arg) {
    pthread_mutex_lock(&first);
    counter++;
    pthread_mutex_unlock(&first);
    return 0;
}
void *reader(void *arg) {
    pthread_mutex_lock(&second);
    counter++;
    pthread_mutex_unlock(&second);
    return 0;
}
int main(void) {
    pthread_t a, b;
    pthread_create(&a, 0, writer, 0);
    pthread_create(&b, 0, reader, 0);
    pthread_join(a, 0);
    pthread_join(b, 0);
    return 0;
}
"""


class ConcurrencyFactsTests(unittest.TestCase):
    def analyze(self, source=SOURCE, extra=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "workers.c").write_text(source, encoding="utf-8")
            for name, content in (extra or {}).items():
                (root / name).write_text(content, encoding="utf-8")
            return analyze_race_source(root)

    def facts(self, result):
        return next(f for f in result["concurrency_facts"] if any(o["name"] == "counter" for o in f["objects"]))

    def test_declarations_contexts_and_distinct_locks_have_source_evidence(self):
        result = self.analyze()
        facts = self.facts(result)
        obj = next(o for o in facts["objects"] if o["name"] == "counter")
        self.assertEqual({"file": "workers.c", "line": 1, "snippet": "int counter;"}, obj["declaration"])
        self.assertEqual("int", obj["type"])
        self.assertEqual(4, len(facts["thread_events"]))
        creates = [e for e in facts["thread_events"] if e["kind"] == "create"]
        self.assertEqual({"writer", "reader"}, {e["entry_function"] for e in creates})
        acquired = [o for o in facts["lock_operations"] if o["kind"] == "acquire"]
        self.assertEqual({"&first", "&second"}, {o["lock_expression"] for o in acquired})
        self.assertEqual(2, len({o["declaration_candidate_id"] for o in acquired}))
        evidence = {e["id"] for e in result["evidence"]}
        for item in facts["objects"] + facts["thread_events"] + facts["lock_operations"]:
            self.assertTrue(set(item["evidence_ids"]) <= evidence)
        bound = [b for b in facts["access_bindings"] if b["declaration_candidate_id"] == obj["object_id"]]
        self.assertEqual(2, len(bound))
        self.assertTrue(all(b["execution_context"]["creation_site_ids"] for b in bound))

    def test_conditional_lock_and_common_lock_cannot_claim_must_protection(self):
        for source in (SOURCE.replace("&second", "&first"), SOURCE.replace(
                "pthread_mutex_lock(&first);", "if (arg) pthread_mutex_lock(&first);")):
            with self.subTest(source=source):
                facts = self.facts(self.analyze(source))
                self.assertTrue(facts["lock_operations"])
                self.assertTrue(all(b["must_lockset"] == [] and b["must_lockset_status"] == "unknown"
                                    for b in facts["access_bindings"]))

    def test_shadowed_global_is_unbound(self):
        result = self.analyze(SOURCE.replace("void *writer(void *arg) {", "void *writer(void *arg) {\n    int counter = 0;"))
        raw = {a["id"]: a for r in result["items"] for a in r["accesses"]}
        bindings = [b for b in self.facts(result)["access_bindings"]
                    if raw[b["access_id"]]["function"] == "writer" and raw[b["access_id"]]["memory_location"] == "counter"]
        self.assertTrue(bindings)
        self.assertTrue(all(b["declaration_candidate_id"] is None for b in bindings))

    def test_same_static_name_in_other_file_has_distinct_identity(self):
        result = self.analyze(SOURCE.replace("int counter;", "static int counter;"), {"other.c": "static int counter;\n"})
        objects = [o for f in result["concurrency_facts"] for o in f["objects"] if o["name"] == "counter"]
        self.assertEqual(2, len({o["object_id"] for o in objects}))
        self.assertTrue(all(o["linkage"] == "internal" for o in objects))

    def test_join_separated_lifetimes_are_observed_without_serialization_claim(self):
        source = SOURCE.replace("    pthread_create(&b, 0, reader, 0);\n    pthread_join(a, 0);",
                                "    pthread_join(a, 0);\n    pthread_create(&b, 0, reader, 0);")
        facts = self.facts(self.analyze(source))
        self.assertEqual(["create", "join", "create", "join"], [e["kind"] for e in facts["thread_events"]])
        self.assertTrue(all(b["execution_context"]["overlap"] == "unknown" for b in facts["access_bindings"]))

    def test_unresolved_callback_keeps_unknown(self):
        facts = self.facts(self.analyze(SOURCE.replace("0, writer, 0", "0, table[0], 0")))
        self.assertIsNone(next(e for e in facts["thread_events"] if e["kind"] == "create")["entry_function"])

    def test_c11_create_uses_second_argument_as_entry(self):
        source = SOURCE.replace("pthread_create(&a, 0, writer, 0)", "thrd_create(&a, writer, 0)")
        result = self.analyze(source)
        facts = self.facts(result)
        event = next(e for e in facts["thread_events"] if e["api"] == "thrd_create")
        self.assertEqual("writer", event["entry_function"])
        self.assertTrue(any(a["function"] == "writer" for r in result["items"] for a in r["accesses"]))

    def test_api_in_comment_is_ignored_and_source_line_preserved(self):
        result = self.analyze("/* pthread_create(&z, 0, fake, 0); */\n" + SOURCE)
        facts = self.facts(result)
        self.assertEqual(4, len(facts["thread_events"]))
        self.assertEqual(2, next(o for o in facts["objects"] if o["name"] == "counter")["declaration"]["line"])


if __name__ == "__main__":
    unittest.main()
