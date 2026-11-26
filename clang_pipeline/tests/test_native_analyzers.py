from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clang_pipeline.resource_flow import ResourceEvent, analyze_resource_flow
from clang_pipeline.sync_flow import analyze_sync_relations


ROOT = Path(__file__).resolve().parents[2]
LIBUV_GRAPH = ROOT / "demo" / "libuv" / "graph.json"
REDIS_GRAPH = ROOT / "demo" / "redis" / "graph.json"


@unittest.skipUnless(LIBUV_GRAPH.exists(), "demo/libuv graph required")
class NativeAnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = json.loads(LIBUV_GRAPH.read_text(encoding="utf-8"))

    def test_resource_flow_has_native_operations_and_evidence(self) -> None:
        result = analyze_resource_flow(self.graph)
        self.assertEqual("resource_flow", result["result_type"])
        self.assertEqual("analyzer", result["source"]["origin"])
        self.assertTrue(result["items"])
        for item in result["items"]:
            self.assertTrue(item["evidence_ids"])
            self.assertEqual("unconfirmed", item["status"])
            for operation in item["operations"]:
                self.assertTrue(operation["evidence_ids"])
                self.assertIn("status", operation)
                self.assertIsInstance(operation["confidence"], (int, float))

    def test_resource_event_keeps_normalized_lifecycle_fields(self) -> None:
        event = ResourceEvent(
            kind="borrow",
            resource_id="rf_test_v1",
            resource_type="memory",
            expression="buffer",
            function="load",
            file="src/load.c",
            line=12,
            condition="non-owning call",
            owner="caller",
            evidence_ids=["ev-load"],
            api="inspect",
        )
        operation = event.as_operation()
        self.assertEqual("hold", operation["kind"])
        self.assertEqual("rf_test_v1", operation["_resource_id"])
        self.assertEqual("buffer", operation["expression"])
        self.assertEqual("non-owning call", operation["condition"])

    @unittest.skipUnless(
        (ROOT.parent / "third_party/libuv").is_dir(),
        "libuv source checkout required",
    )
    def test_resource_source_events_cover_libuv_lifecycle(self) -> None:
        result = analyze_resource_flow(self.graph, source_root=ROOT.parent)
        operations = {
            (
                operation["function"],
                operation["line"],
                operation["kind"],
            )
            for item in result["items"]
            for operation in item.get("operations", [])
        }
        self.assertTrue(
            {
                ("uv__async_start", 306, "acquire"),
                ("uv__async_start", 313, "transfer"),
                ("uv_fs_open", 2028, "transfer"),
                ("uv__getaddrinfo_done", 134, "transfer"),
                ("uv__fs_scandir", 600, "transfer"),
                ("uv__fs_scandir_cleanup", 728, "release"),
            }.issubset(operations)
        )

    def test_resource_flow_uses_exact_apis_and_stable_resource_ids(self) -> None:
        graph = {
            "run_id": "resource-exact-test",
            "meta": {
                "repository": "example/repo",
                "commit": "a" * 40,
            },
            "nodes": [
                {"id": "fn:uv__getpwuid_r", "kind": "function", "name": "uv__getpwuid_r"},
                {"id": "fn:uv__malloc", "kind": "function", "name": "uv__malloc"},
                {"id": "fn:uv__free", "kind": "function", "name": "uv__free"},
                {"id": "fn:uv__free_client", "kind": "function", "name": "uv__free_client"},
                {"id": "fn:uv__open_context", "kind": "function", "name": "uv__open_context"},
            ],
            "edges": [
                {
                    "id": "acquire",
                    "source": "fn:uv__getpwuid_r",
                    "target": "fn:uv__malloc",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/core.c",
                        "line": 10,
                        "snippet": "buf = uv__malloc(bufsize);",
                    },
                    "evidence_ids": ["ev-acquire"],
                },
                {
                    "id": "release",
                    "source": "fn:uv__getpwuid_r",
                    "target": "fn:uv__free",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/core.c",
                        "line": 20,
                        "snippet": "uv__free(buf);",
                    },
                    "evidence_ids": ["ev-release"],
                },
                {
                    "id": "false-release",
                    "source": "fn:uv__getpwuid_r",
                    "target": "fn:uv__free_client",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/core.c",
                        "line": 30,
                        "snippet": "uv__free_client(client);",
                    },
                    "evidence_ids": ["ev-false-release"],
                },
                {
                    "id": "false-acquire",
                    "source": "fn:uv__getpwuid_r",
                    "target": "fn:uv__open_context",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/core.c",
                        "line": 40,
                        "snippet": "uv__open_context(ctx);",
                    },
                    "evidence_ids": ["ev-false-acquire"],
                },
            ],
            "evidence": [
                {"id": "ev-acquire", "file": "src/core.c", "line": 10},
                {"id": "ev-release", "file": "src/core.c", "line": 20},
                {"id": "ev-false-release", "file": "src/core.c", "line": 30},
                {"id": "ev-false-acquire", "file": "src/core.c", "line": 40},
            ],
        }
        result = analyze_resource_flow(graph)
        self.assertEqual(1, len(result["items"]))
        item = result["items"][0]
        self.assertEqual("rf_libuv_getpwuid_buf_v1", item["resource"])
        self.assertEqual("buf", item["resource_expression"])
        self.assertEqual({"acquire", "release"}, {
            operation["kind"] for operation in item["operations"]
        })

    def test_resource_lifecycle_state_classifies_leak_candidates(self) -> None:
        graph = {
            "run_id": "resource-state-test",
            "meta": {"repository": "fixture/repo", "commit": "a" * 40},
            "nodes": [
                {"id": "fn:worker", "kind": "function", "name": "worker"},
                {"id": "fn:malloc", "kind": "external_function", "name": "malloc"},
                {"id": "fn:free", "kind": "external_function", "name": "free"},
            ],
            "edges": [
                {
                    "id": "acquire",
                    "source": "fn:worker",
                    "target": "fn:malloc",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/state.c",
                        "line": 10,
                        "snippet": "buffer = malloc(32);",
                    },
                    "evidence_ids": ["ev-acquire"],
                },
                {
                    "id": "release",
                    "source": "fn:worker",
                    "target": "fn:free",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/state.c",
                        "line": 20,
                        "snippet": "free(buffer);",
                    },
                    "evidence_ids": ["ev-release"],
                },
            ],
            "evidence": [
                {"id": "ev-acquire", "file": "src/state.c", "line": 10},
                {"id": "ev-release", "file": "src/state.c", "line": 20},
            ],
        }
        released = analyze_resource_flow(graph)
        self.assertTrue(released["items"])
        self.assertEqual(
            "no_leak",
            released["items"][0]["classification"],
        )
        self.assertEqual(
            1,
            sum(
                path["classification"] == "no_leak"
                for path in released["items"][0]["paths"]
            ),
        )
        del graph["edges"][1]
        leaking = analyze_resource_flow(graph)
        self.assertTrue(leaking["items"])
        self.assertEqual(
            "possible_leak",
            leaking["items"][0]["classification"],
        )

    def test_resource_pointer_arithmetic_release_matches_allocation(self) -> None:
        graph = {
            "run_id": "resource-pointer-state-test",
            "meta": {"repository": "fixture/repo", "commit": "a" * 40},
            "nodes": [
                {"id": "fn:worker", "kind": "function", "name": "worker"},
                {"id": "fn:malloc", "kind": "external_function", "name": "malloc"},
                {"id": "fn:free", "kind": "external_function", "name": "free"},
            ],
            "edges": [
                {
                    "id": "acquire",
                    "source": "fn:worker",
                    "target": "fn:malloc",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/pointer.c",
                        "line": 10,
                        "snippet": "buffer = malloc(32);",
                    },
                    "evidence_ids": ["ev-acquire"],
                },
                {
                    "id": "release",
                    "source": "fn:worker",
                    "target": "fn:free",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/pointer.c",
                        "line": 20,
                        "snippet": "free(buffer - 1);",
                    },
                    "evidence_ids": ["ev-release"],
                },
            ],
            "evidence": [
                {"id": "ev-acquire", "file": "src/pointer.c", "line": 10},
                {"id": "ev-release", "file": "src/pointer.c", "line": 20},
            ],
        }
        result = analyze_resource_flow(graph)
        self.assertTrue(result["items"])
        self.assertEqual("no_leak", result["items"][0]["classification"])

    def test_multiple_release_calls_on_one_line_are_expanded(self) -> None:
        graph = {
            "run_id": "resource-multi-release-test",
            "meta": {"repository": "fixture/repo", "commit": "a" * 40},
            "nodes": [
                {"id": "fn:worker", "kind": "function", "name": "worker"},
                {"id": "fn:malloc", "kind": "external_function", "name": "malloc"},
                {"id": "fn:free", "kind": "external_function", "name": "free"},
            ],
            "edges": [
                {
                    "id": "acquire-a",
                    "source": "fn:worker",
                    "target": "fn:malloc",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/multi.c",
                        "line": 10,
                        "snippet": "a = malloc(16);",
                    },
                    "evidence_ids": ["ev-a"],
                },
                {
                    "id": "acquire-b",
                    "source": "fn:worker",
                    "target": "fn:malloc",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/multi.c",
                        "line": 11,
                        "snippet": "b = malloc(24);",
                    },
                    "evidence_ids": ["ev-b"],
                },
                {
                    "id": "release",
                    "source": "fn:worker",
                    "target": "fn:free",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/multi.c",
                        "line": 20,
                        "snippet": "{ free(a); free(b); }",
                    },
                    "evidence_ids": ["ev-release"],
                },
            ],
            "evidence": [
                {"id": "ev-a", "file": "src/multi.c", "line": 10},
                {"id": "ev-b", "file": "src/multi.c", "line": 11},
                {"id": "ev-release", "file": "src/multi.c", "line": 20},
            ],
        }
        result = analyze_resource_flow(graph)
        self.assertTrue(result["items"])
        self.assertTrue(all(
            item["classification"] == "no_leak"
            for item in result["items"]
        ))

    def test_sync_relations_have_native_relations_and_evidence(self) -> None:
        result = analyze_sync_relations(self.graph)
        self.assertEqual("sync_relation", result["result_type"])
        self.assertEqual("analyzer", result["source"]["origin"])
        self.assertTrue(result["items"])
        relation_types = {item["relation_type"] for item in result["items"]}
        self.assertIn("mutex_lock", relation_types)
        for item in result["items"]:
            self.assertEqual("unconfirmed", item["status"])
            self.assertTrue(item["evidence_ids"])
            self.assertIn(item["order"], {"happens_before", "happens_after", "concurrent", "unknown"})

    def test_source_scan_links_lock_to_protected_write_across_control_flow(self) -> None:
        graph = {
            "run_id": "sync-protected-access-test",
            "meta": {"repository": "fixture/repo", "commit": "a" * 40},
            "nodes": [
                {
                    "id": "fn:worker",
                    "kind": "function",
                    "name": "worker",
                    "file": "src/counter.c",
                    "line": 1,
                }
            ],
            "edges": [],
            "evidence": [],
        }
        source = """\
static unsigned int running;
static void worker(void) {
  uv_mutex_lock(&mutex);
  if (ready)
    running--;
  uv_mutex_unlock(&mutex);
}
"""
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "src" / "counter.c"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(source, encoding="utf-8")
            result = analyze_sync_relations(graph, source_root=directory)

        relation = next(
            (
                item
                for item in result["items"]
                if (
                    item["source"]["function"] == "worker"
                    and item["source"]["line"] == 3
                    and item["target"]["line"] == 5
                )
            ),
            None,
        )
        self.assertIsNotNone(relation)
        self.assertEqual("mutex_lock", relation["relation_type"])
        self.assertEqual("happens_before", relation["order"])
        self.assertEqual(2, len(relation["evidence_ids"]))

    def test_source_scan_covers_queue_rwlock_eventfd_and_atomic_evidence(self) -> None:
        graph = {
            "run_id": "sync-source-coverage-test",
            "meta": {"repository": "fixture/repo", "commit": "a" * 40},
            "nodes": [
                {
                    "id": "fn:post",
                    "kind": "function",
                    "name": "post",
                    "file": "src/sync.c",
                    "line": 2,
                },
                {
                    "id": "fn:worker",
                    "kind": "function",
                    "name": "worker",
                    "file": "src/sync.c",
                    "line": 7,
                },
                {
                    "id": "fn:uv_rwlock_wrunlock",
                    "kind": "function",
                    "name": "uv_rwlock_wrunlock",
                    "file": "src/sync.c",
                    "line": 12,
                },
                {
                    "id": "fn:uv_rwlock_rdlock",
                    "kind": "function",
                    "name": "uv_rwlock_rdlock",
                    "file": "src/sync.c",
                    "line": 15,
                },
                {
                    "id": "fn:uv_rwlock_wrlock",
                    "kind": "function",
                    "name": "uv_rwlock_wrlock",
                    "file": "src/sync.c",
                    "line": 18,
                },
                {
                    "id": "fn:uv_rwlock_trywrlock",
                    "kind": "function",
                    "name": "uv_rwlock_trywrlock",
                    "file": "src/sync.c",
                    "line": 21,
                },
                {
                    "id": "fn:async_sender",
                    "kind": "function",
                    "name": "async_sender",
                    "file": "src/sync.c",
                    "line": 25,
                },
                {
                    "id": "fn:async_reader",
                    "kind": "function",
                    "name": "async_reader",
                    "file": "src/sync.c",
                    "line": 29,
                },
            ],
            "edges": [],
            "evidence": [],
        }
        source = """\
int produced;
void post(void) {
  uv_mutex_lock(&queue_mutex);
  uv__queue_insert_tail(&work_queue, item);
  uv_mutex_unlock(&queue_mutex);
}
void worker(void) {
  uv_mutex_lock(&queue_mutex);
  item = uv__queue_head(&work_queue);
  uv_mutex_unlock(&queue_mutex);
}
void uv_rwlock_wrunlock(uv_rwlock_t* rwlock) {
  if (pthread_rwlock_unlock(rwlock)) return;
}
void uv_rwlock_rdlock(uv_rwlock_t* rwlock) {
  if (pthread_rwlock_rdlock(rwlock)) return;
}
void uv_rwlock_wrlock(uv_rwlock_t* rwlock) {
  if (pthread_rwlock_wrlock(rwlock)) return;
}
void uv_rwlock_trywrlock(uv_rwlock_t* rwlock) {
  err = pthread_rwlock_trywrlock(rwlock);
  if (err) return UV_EBUSY;
}
void async_sender(void) {
  if (atomic_exchange(&pending, 1) == 0)
    (void) write(event_fd, &one, sizeof(one));
}
void async_reader(void) {
  (void) read(event_fd, &one, sizeof(one));
  h->async_cb(h);
}
"""
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "src" / "sync.c"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(source, encoding="utf-8")
            result = analyze_sync_relations(graph, source_root=directory)

        queue_relation = next(
            (
                item
                for item in result["items"]
                if "cross-function queue publication" in str(item["condition"])
            ),
            None,
        )
        self.assertIsNotNone(queue_relation)
        self.assertGreaterEqual(len(queue_relation["evidence_ids"]), 4)

        self.assertTrue(any(
            item["condition"] == "rwlock writer release precedes successful acquisition"
            and item["target"]["function"] == "uv_rwlock_rdlock"
            for item in result["items"]
        ))
        self.assertTrue(any(
            item["order"] == "concurrent"
            and item["source"]["function"] == "uv_rwlock_rdlock"
            and item["target"]["function"] == "uv_rwlock_rdlock"
            for item in result["items"]
        ))
        self.assertTrue(any(
            item["order"] == "unknown"
            and item["source"]["function"] == "uv_rwlock_trywrlock"
            for item in result["items"]
        ))
        self.assertTrue(any(
            item["order"] == "happens_before"
            and item["source"]["snippet"] == "(void) write(event_fd, &one, sizeof(one));"
            and item["target"]["snippet"] == "(void) read(event_fd, &one, sizeof(one));"
            for item in result["items"]
        ))
        self.assertTrue(any(
            "atomic_exchange" in item["source"]["snippet"]
            for item in result["items"]
        ))

    def test_notification_callback_requires_queue_or_async_witness(self) -> None:
        graph = {
            "run_id": "sync-notification-witness-test",
            "meta": {"repository": "fixture/repo", "commit": "a" * 40},
            "nodes": [
                {
                    "id": "fn:post",
                    "kind": "function",
                    "name": "post",
                    "file": "src/notify.c",
                    "line": 1,
                },
                {
                    "id": "fn:worker",
                    "kind": "function",
                    "name": "worker",
                    "file": "src/notify.c",
                    "line": 7,
                },
                {
                    "id": "fn:signal_only",
                    "kind": "function",
                    "name": "signal_only",
                    "file": "src/notify.c",
                    "line": 13,
                },
                {
                    "id": "fn:unrelated",
                    "kind": "function",
                    "name": "unrelated",
                    "file": "src/notify.c",
                    "line": 16,
                },
            ],
            "edges": [],
            "evidence": [],
        }
        source = """\
void post(void) {
  uv_mutex_lock(&queue_mutex);
  uv__queue_insert_tail(&work_queue, item);
  uv_mutex_unlock(&queue_mutex);
  uv_cond_signal(&queue_ready);
}
void worker(void) {
  uv_mutex_lock(&queue_mutex);
  item = uv__queue_head(&work_queue);
  uv_mutex_unlock(&queue_mutex);
  w->work(w);
}
void signal_only(void) {
  uv_cond_signal(&queue_ready);
}
void unrelated(void) {
  h->async_cb(h);
}
"""
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "src" / "notify.c"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(source, encoding="utf-8")
            result = analyze_sync_relations(graph, source_root=directory)

        queue_notification = next(
            (
                item
                for item in result["items"]
                if (
                    item["source"]["function"] == "post"
                    and item["source"]["line"] == 5
                    and item["target"]["function"] == "worker"
                    and item["target"]["line"] == 11
                    and item["order"] == "unknown"
                )
            ),
            None,
        )
        self.assertIsNotNone(queue_notification)
        self.assertIn(
            "queue notification boundary",
            str(queue_notification["condition"]),
        )
        self.assertFalse(any(
            item["source"]["function"] == "signal_only"
            and item["target"]["function"] == "unrelated"
            for item in result["items"]
        ))
        self.assertFalse(any(
            "notification does not identify" in str(item.get("condition"))
            for item in result["items"]
        ))

    def test_wrapper_summary_propagates_lock_semantics(self) -> None:
        graph = {
            "run_id": "sync-wrapper-test",
            "meta": {"repository": "fixture/repo", "commit": "a" * 40},
            "nodes": [
                {
                    "id": "fn:my_lock",
                    "kind": "function",
                    "name": "my_lock",
                    "file": "src/wrapper.c",
                    "line": 1,
                },
                {
                    "id": "fn:my_unlock",
                    "kind": "function",
                    "name": "my_unlock",
                    "file": "src/wrapper.c",
                    "line": 5,
                },
                {
                    "id": "fn:worker",
                    "kind": "function",
                    "name": "worker",
                    "file": "src/wrapper.c",
                    "line": 9,
                },
            ],
            "edges": [],
            "evidence": [],
        }
        source = """\
void my_lock(uv_mutex_t* mutex) {
  uv_mutex_lock(mutex);
}

void my_unlock(uv_mutex_t* mutex) {
  uv_mutex_unlock(mutex);
}

void worker(void) {
  my_lock(&guard);
  counter++;
  my_unlock(&guard);
}
"""
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "src" / "wrapper.c"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(source, encoding="utf-8")
            result = analyze_sync_relations(graph, source_root=directory)

        self.assertTrue(any(
            item["source"]["function"] == "worker"
            and item["source"]["line"] == 10
            and item["target"]["function"] == "worker"
            and item["target"]["line"] == 12
            and item["relation_type"] == "mutex_lock"
            for item in result["items"]
        ))

    def test_local_pointer_alias_links_lock_resources(self) -> None:
        graph = {
            "run_id": "sync-alias-test",
            "meta": {"repository": "fixture/repo", "commit": "a" * 40},
            "nodes": [
                {
                    "id": "fn:worker",
                    "kind": "function",
                    "name": "worker",
                    "file": "src/alias.c",
                    "line": 1,
                }
            ],
            "edges": [],
            "evidence": [],
        }
        source = """\
void worker(void) {
  uv_mutex_t* local = &global_mutex;
  uv_mutex_lock(local);
  counter++;
  uv_mutex_unlock(&global_mutex);
}
"""
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "src" / "alias.c"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(source, encoding="utf-8")
            result = analyze_sync_relations(graph, source_root=directory)

        self.assertTrue(any(
            item["source"]["line"] == 3
            and item["target"]["line"] == 5
            and item["relation_type"] == "mutex_lock"
            for item in result["items"]
        ))

    def test_thread_create_propagates_thread_identity(self) -> None:
        graph = {
            "run_id": "sync-thread-identity-test",
            "meta": {"repository": "fixture/repo", "commit": "a" * 40},
            "nodes": [
                {
                    "id": "fn:main",
                    "kind": "function",
                    "name": "main",
                    "file": "src/thread.c",
                    "line": 1,
                },
                {
                    "id": "fn:worker",
                    "kind": "function",
                    "name": "worker",
                    "file": "src/thread.c",
                    "line": 6,
                },
                {
                    "id": "fn:uv_thread_create",
                    "kind": "external_function",
                    "name": "uv_thread_create",
                },
            ],
            "edges": [
                {
                    "id": "create",
                    "source": "fn:main",
                    "target": "fn:uv_thread_create",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/thread.c",
                        "line": 3,
                        "snippet": "uv_thread_create(&worker_thread, worker, NULL);",
                    },
                    "evidence_ids": ["ev-create"],
                }
            ],
            "evidence": [
                {
                    "id": "ev-create",
                    "file": "src/thread.c",
                    "line": 3,
                    "snippet": "uv_thread_create(&worker_thread, worker, NULL);",
                }
            ],
        }
        source = """\
void main(void) {
  uv_thread_t worker_thread;
  uv_thread_create(&worker_thread, worker, NULL);
}

void worker(void* arg) {
  uv_mutex_lock(&guard);
  counter++;
  uv_mutex_unlock(&guard);
}
"""
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "src" / "thread.c"
            source_path.parent.mkdir(parents=True)
            source_path.write_text(source, encoding="utf-8")
            result = analyze_sync_relations(graph, source_root=directory)

        relation = next(
            (
                item
                for item in result["items"]
                if item["source"]["function"] == "worker"
                and item["target"]["function"] == "worker"
                and item["relation_type"] == "mutex_lock"
            ),
            None,
        )
        self.assertIsNotNone(relation)
        self.assertEqual(
            "thread:worker_thread",
            relation["source"]["thread"],
        )

    @unittest.skipUnless(
        (ROOT.parent / "third_party/libuv").is_dir(),
        "libuv source checkout required",
    )
    def test_libuv_worker_gets_thread_handle_identity(self) -> None:
        result = analyze_sync_relations(
            self.graph,
            source_root=ROOT.parent,
        )
        self.assertTrue(any(
            "worker" in item["source"]["function"]
            and str(item["source"]["thread"]).startswith("thread:")
            for item in result["items"]
        ))

    def test_cross_function_queue_atomic_and_thread_join_relations(self) -> None:
        graph = {
            "run_id": "sync-enhancement-test",
            "meta": {"repository": "fixture/repo", "commit": "a" * 40},
            "nodes": [
                {"id": "fn:producer", "kind": "function", "name": "producer"},
                {"id": "fn:consumer", "kind": "function", "name": "consumer"},
                {"id": "fn:main", "kind": "function", "name": "main"},
                {"id": "fn:listAddNodeTail", "kind": "external_function", "name": "listAddNodeTail"},
                {"id": "fn:listFirst", "kind": "external_function", "name": "listFirst"},
                {"id": "fn:atomic_store_explicit", "kind": "external_function", "name": "atomic_store_explicit"},
                {"id": "fn:atomic_load_explicit", "kind": "external_function", "name": "atomic_load_explicit"},
                {"id": "fn:pthread_create", "kind": "external_function", "name": "pthread_create"},
                {"id": "fn:pthread_join", "kind": "external_function", "name": "pthread_join"},
            ],
            "edges": [
                {
                    "id": "queue-producer",
                    "source": "fn:producer",
                    "target": "fn:listAddNodeTail",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/queue.c",
                        "line": 10,
                        "snippet": "listAddNodeTail(queue, item);",
                    },
                    "evidence_ids": ["ev-queue-producer"],
                },
                {
                    "id": "queue-consumer",
                    "source": "fn:consumer",
                    "target": "fn:listFirst",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/queue.c",
                        "line": 20,
                        "snippet": "item = listFirst(queue);",
                    },
                    "evidence_ids": ["ev-queue-consumer"],
                },
                {
                    "id": "atomic-release",
                    "source": "fn:producer",
                    "target": "fn:atomic_store_explicit",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/atomic.c",
                        "line": 12,
                        "snippet": "atomic_store_explicit(&ready, 1, memory_order_release);",
                    },
                    "evidence_ids": ["ev-atomic-release"],
                },
                {
                    "id": "atomic-acquire",
                    "source": "fn:consumer",
                    "target": "fn:atomic_load_explicit",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/atomic.c",
                        "line": 22,
                        "snippet": "value = atomic_load_explicit(&ready, memory_order_acquire);",
                    },
                    "evidence_ids": ["ev-atomic-acquire"],
                },
                {
                    "id": "thread-create",
                    "source": "fn:main",
                    "target": "fn:pthread_create",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/thread.c",
                        "line": 5,
                        "snippet": "pthread_create(&worker, NULL, run, NULL);",
                    },
                    "evidence_ids": ["ev-thread-create"],
                },
                {
                    "id": "thread-join",
                    "source": "fn:main",
                    "target": "fn:pthread_join",
                    "kind": "direct_call",
                    "call_site": {
                        "file": "src/thread.c",
                        "line": 30,
                        "snippet": "pthread_join(worker, NULL);",
                    },
                    "evidence_ids": ["ev-thread-join"],
                },
            ],
            "evidence": [
                {"id": "ev-queue-producer", "file": "src/queue.c", "line": 10},
                {"id": "ev-queue-consumer", "file": "src/queue.c", "line": 20},
                {"id": "ev-atomic-release", "file": "src/atomic.c", "line": 12},
                {"id": "ev-atomic-acquire", "file": "src/atomic.c", "line": 22},
                {"id": "ev-thread-create", "file": "src/thread.c", "line": 5},
                {"id": "ev-thread-join", "file": "src/thread.c", "line": 30},
            ],
        }
        result = analyze_sync_relations(graph)
        relations = result["items"]
        self.assertTrue(any(
            "cross-function queue publication queue" in str(item.get("condition"))
            for item in relations
        ))
        self.assertTrue(any(
            item["relation_type"] == "atomic"
            and item["source"]["function"] == "producer"
            and item["target"]["function"] == "consumer"
            and item["order"] == "happens_before"
            for item in relations
        ))
        self.assertTrue(any(
            item["relation_type"] == "thread_join"
            and "thread handle worker" in str(item.get("condition"))
            for item in relations
        ))

    @unittest.skipUnless(REDIS_GRAPH.exists(), "demo/redis graph required")
    def test_redis_program_order_relations_are_emitted(self) -> None:
        graph = json.loads(REDIS_GRAPH.read_text(encoding="utf-8"))
        result = analyze_sync_relations(graph)
        endpoints = {
            (
                item["source"]["function"],
                item["source"]["line"],
                item["target"]["function"],
                item["target"]["line"],
                item["order"],
            )
            for item in result["items"]
        }
        self.assertTrue(
            {
                ("bioSubmitJob", 155, "bioSubmitJob", 156, "happens_before"),
                ("bioSubmitJob", 156, "bioSubmitJob", 158, "happens_before"),
                ("bioDrainWorker", 317, "bioDrainWorker", 319, "happens_before"),
                ("IOThreadMain", 4192, "IOThreadMain", 4193, "happens_before"),
            }
            <= endpoints
        )
        self.assertTrue(
            any(
                "cross-function queue publication" in str(item.get("condition"))
                and item["source"]["function"] == "bioSubmitJob"
                and item["target"]["function"] == "bioProcessBackgroundJobs"
                for item in result["items"]
            )
        )
        source_result = analyze_sync_relations(graph, source_root=ROOT)
        if not list(ROOT.glob("**/bio.c")):
            return
        self.assertTrue(
            any(
                item["relation_type"] == "atomic"
                and item["source"]["function"] == "setIOPendingCount"
                and item["target"]["function"] == "getIOPendingCount"
                for item in source_result["items"]
            )
        )


if __name__ == "__main__":
    unittest.main()
