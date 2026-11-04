from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from clang_pipeline.agent_jobs import AnalysisJobManager, IdempotencyConflict, JobError
from clang_pipeline.execution import ClangStageError
from clang_pipeline.object_store import FileObjectStore, S3ObjectStore

URL = "https://example.com/org/repo.git"


def runner(url, *, output_dir, **kwargs):
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "visualization.html").write_text("<h1>report</h1>", encoding="utf-8")
    return {"status": "succeeded", "repository_url": url, "output_dir": str(output_dir)}


def wait_for(manager, job_id, statuses=("succeeded", "failed", "cancelled", "timed_out"), timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager.get(job_id)
        if state["status"] in statuses:
            return state
        time.sleep(0.01)
    raise AssertionError(f"job did not reach {statuses}: {state}")


class DurableJobTests(unittest.TestCase):
    def test_pending_and_completed_jobs_survive_restart(self):
        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root, max_workers=0) as api:
                job = api.submit(URL, idempotency_key="restart")
            with AnalysisJobManager(root, max_workers=1, runner=runner) as worker:
                state = wait_for(worker, job["job_id"])
                self.assertEqual(state["status"], "succeeded")
            with AnalysisJobManager(root, max_workers=0) as api:
                again = api.submit(URL, idempotency_key="restart")
                self.assertEqual(again["job_id"], job["job_id"])
                self.assertEqual(again["status"], "succeeded")
                stream, _ = api.open_object(job["job_id"], "result")
                with stream:
                    self.assertEqual(json.load(stream)["repository_url"], URL)
                stream, _ = api.open_object(job["job_id"], "artifacts")
                with stream, tarfile.open(fileobj=io.BytesIO(stream.read()), mode="r:gz") as archive:
                    self.assertIn("visualization.html", archive.getnames())
                    self.assertIn("trace/tool-trace.jsonl", archive.getnames())

    def test_concurrent_idempotency_and_conflict(self):
        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root, max_workers=0) as a, AnalysisJobManager(root, max_workers=0) as b:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    jobs = list(pool.map(lambda i: (a if i % 2 else b).submit(URL, idempotency_key="same"), range(20)))
                self.assertEqual(len({j["job_id"] for j in jobs}), 1)
                with self.assertRaises(IdempotencyConflict):
                    a.submit(URL, commit="different", idempotency_key="same")
                self.assertNotEqual(a.submit(URL)["job_id"], a.submit(URL)["job_id"])

    def test_workers_do_not_execute_same_claim(self):
        calls = []
        lock = threading.Lock()

        def counted(url, **kwargs):
            with lock:
                calls.append(str(kwargs["output_dir"]))
            time.sleep(0.02)
            return runner(url, **kwargs)

        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root, max_workers=2, runner=counted) as a, AnalysisJobManager(root, max_workers=2, runner=counted) as b:
                jobs = [a.submit(URL) for _ in range(8)]
                for job in jobs:
                    self.assertEqual(wait_for(b, job["job_id"])["status"], "succeeded")
                self.assertEqual(len(calls), 8)
                self.assertEqual(len(set(calls)), 8)

    def test_retry_then_success_and_retry_budget(self):
        calls = []

        def flaky(url, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise ClangStageError("transient", retryable=True)
            return runner(url, **kwargs)

        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root, max_workers=1, runner=flaky, retry_delay_seconds=0.01) as manager:
                job = manager.submit(URL)
                state = wait_for(manager, job["job_id"])
                self.assertEqual((state["status"], state["attempts"]), ("succeeded", 3))
                self.assertEqual(sum(e["event"] == "retry_scheduled" for e in state["trace"]), 2)
            with AnalysisJobManager(root, max_workers=1, runner=lambda *a, **k: (_ for _ in ()).throw(ClangStageError("always", retryable=True)), max_attempts=2, retry_delay_seconds=0.01) as manager:
                state = wait_for(manager, manager.submit(URL)["job_id"])
                self.assertEqual((state["status"], state["attempts"]), ("failed", 2))

    def test_nonretryable_failure_is_not_retried(self):
        def broken(*args, **kwargs):
            raise ValueError("invalid input")
        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root, runner=broken) as manager:
                state = wait_for(manager, manager.submit(URL)["job_id"])
                self.assertEqual((state["status"], state["attempts"]), ("failed", 1))

    def test_crashed_process_is_recovered_after_lease(self):
        with tempfile.TemporaryDirectory() as root:
            marker = Path(root) / "started"
            code = '''import sys,time
from pathlib import Path
from clang_pipeline.agent_jobs import AnalysisJobManager
def runner(url, **kwargs):
    Path(sys.argv[2]).write_text("started")
    while True: time.sleep(.05)
m = AnalysisJobManager(sys.argv[1], max_workers=1, runner=runner, lease_seconds=.4)
j = m.submit("https://example.com/org/repo.git", idempotency_key="crash")
while True: time.sleep(.05)
'''
            child = subprocess.Popen([sys.executable, "-c", code, root, str(marker)], cwd=Path(__file__).resolve().parents[2])
            try:
                deadline = time.monotonic() + 8
                while not marker.exists() and time.monotonic() < deadline:
                    self.assertIsNone(child.poll())
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                child.kill()
                child.wait(timeout=5)
                with AnalysisJobManager(root, max_workers=1, runner=runner, lease_seconds=.4) as manager:
                    job = manager.submit(URL, idempotency_key="crash")
                    state = wait_for(manager, job["job_id"])
                    self.assertEqual((state["status"], state["attempts"]), ("succeeded", 2))
                    self.assertIn("recovered", [e["event"] for e in state["trace"]])
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_stale_claim_cannot_publish_and_attempts_are_isolated(self):
        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root, max_workers=0, lease_seconds=.05) as manager:
                job = manager.submit(URL)
                first, _, old_claim = manager.queue.claim("dead")
                time.sleep(.07)
                second, _, new_claim = manager.queue.claim("live")
                self.assertNotEqual(first["output_dir"], second["output_dir"])
                self.assertFalse(manager.queue.finish(job["job_id"], old_claim, "succeeded", result={"stale": True}))
                self.assertTrue(manager.queue.finish(job["job_id"], new_claim, "succeeded", result={"fresh": True}))
                self.assertEqual(manager.get(job["job_id"])["result"], {"fresh": True})

    def test_cross_process_cancellation_and_queued_cancellation(self):
        started = threading.Event()

        def blocking(url, *, cancel_token, **kwargs):
            started.set()
            while not cancel_token.is_cancelled:
                time.sleep(.01)
            cancel_token.check()

        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root, max_workers=1, runner=blocking) as worker, AnalysisJobManager(root, max_workers=0) as api:
                job = api.submit(URL)
                self.assertTrue(started.wait(3))
                pending = api.submit(URL)
                self.assertEqual(api.cancel(pending["job_id"])["status"], "cancelled")
                api.cancel(job["job_id"])
                self.assertEqual(wait_for(api, job["job_id"])["status"], "cancelled")

    def test_shutdown_requeues_work(self):
        started = threading.Event()

        def blocking(url, *, cancel_token, **kwargs):
            started.set()
            while not cancel_token.is_cancelled:
                time.sleep(.01)
            cancel_token.check()

        with tempfile.TemporaryDirectory() as root:
            manager = AnalysisJobManager(root, max_workers=1, runner=blocking)
            job = manager.submit(URL)
            self.assertTrue(started.wait(3))
            manager.close()
            with AnalysisJobManager(root, max_workers=1, runner=runner) as restarted:
                state = wait_for(restarted, job["job_id"])
                self.assertEqual((state["status"], state["attempts"]), ("succeeded", 2))

    def test_expiry_deletes_results_and_keeps_idempotency_tombstone(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as objects:
            with AnalysisJobManager(root, max_workers=1, runner=runner, retention_seconds=.1,
                                    object_store=FileObjectStore(objects)) as manager:
                job = manager.submit(URL, idempotency_key="expire")
                state = wait_for(manager, job["job_id"])
                self.assertTrue((Path(objects) / job["job_id"]).exists())
                time.sleep(.12)
                manager.cleanup()
                self.assertEqual(manager.get(job["job_id"])["status"], "expired")
                self.assertFalse(Path(state["job_dir"]).exists())
                self.assertFalse((Path(objects) / job["job_id"]).exists())
                self.assertEqual(manager.submit(URL, idempotency_key="expire")["status"], "expired")
                with self.assertRaises(JobError):
                    manager.open_object(job["job_id"], "result")
                self.assertEqual(manager.cleanup(), 0)

    def test_storage_failure_retries_and_does_not_publish_success(self):
        class BrokenStore(FileObjectStore):
            broken = True
            def put_file(self, key, path):
                if self.broken:
                    self.broken = False
                    raise RuntimeError("S3 unavailable")
                return super().put_file(key, path)
        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root, runner=runner, object_store=BrokenStore(Path(root) / "objects"), retry_delay_seconds=.01) as manager:
                state = wait_for(manager, manager.submit(URL)["job_id"])
                self.assertEqual((state["status"], state["attempts"]), ("succeeded", 2))

    def test_default_runner_uses_managed_checkout_without_duplicate_zip(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            with patch("clang_pipeline.repo_agent.run_repo_analysis", side_effect=runner) as mocked:
                with AnalysisJobManager(root, max_workers=1) as manager:
                    state = wait_for(manager, manager.submit(URL)["job_id"])
                    self.assertEqual(state["status"], "succeeded")
                    args = mocked.call_args.kwargs
                    self.assertEqual(args["work_root"], Path(state["output_dir"]) / "checkout")
                    self.assertFalse(args["archive"])

    def test_retry_backoff_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root, max_workers=0) as manager:
                job = manager.submit(URL)
                state, _, claim = manager.queue.claim("worker")
                manager.queue.finish(job["job_id"], claim, "failed",
                    error={"retryable": True, "message": "transient"}, retry_delay=.15)
            with AnalysisJobManager(root, max_workers=0) as restarted:
                self.assertIsNone(restarted.queue.claim("new-worker"))
                time.sleep(.17)
                state, _, claim = restarted.queue.claim("new-worker")
                self.assertEqual(state["attempts"], 2)
                restarted.queue.finish(job["job_id"], claim, "failed",
                    error={"retryable": False, "message": "done"})

    def test_expired_lease_exhaustion_and_pending_cancel(self):
        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root, max_workers=0, lease_seconds=.05, max_attempts=1) as manager:
                lost = manager.submit(URL)
                manager.queue.claim("lost")
                time.sleep(.07)
                self.assertIsNone(manager.queue.claim("replacement"))
                state = manager.get(lost["job_id"])
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["error"]["code"], "worker_lost")
                cancelled = manager.submit(URL)
                manager.queue.claim("lost-again")
                manager.cancel(cancelled["job_id"])
                time.sleep(.07)
                self.assertIsNone(manager.queue.claim("replacement"))
                self.assertEqual(manager.get(cancelled["job_id"])["status"], "cancelled")

    def test_cleanup_failure_is_retried(self):
        class Store(FileObjectStore):
            broken = True
            def delete_prefix(self, prefix):
                if self.broken:
                    self.broken = False
                    raise OSError("temporarily unavailable")
                super().delete_prefix(prefix)
        with tempfile.TemporaryDirectory() as root:
            with AnalysisJobManager(root, runner=runner, object_store=Store(Path(root) / "objects"), retention_seconds=.1) as manager:
                job = manager.submit(URL)
                wait_for(manager, job["job_id"])
                time.sleep(.12)
                with self.assertRaises(OSError):
                    manager.cleanup()
                self.assertEqual(manager.get(job["job_id"])["status"], "expiring")
                manager.cleanup()
                self.assertEqual(manager.get(job["job_id"])["status"], "expired")


class ObjectStoreTests(unittest.TestCase):
    def test_file_store_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            store = FileObjectStore(root)
            for key in ("../secret", "/secret", "a/../secret", "a\\secret", "a//b"):
                with self.assertRaises(ValueError):
                    store.open(key)

    def test_s3_round_trip_and_prefix_scoped_deletion(self):
        class FakeS3:
            def __init__(self):
                self.objects = {"other/job/keep": b"keep"}
            def upload_file(self, source, bucket, key):
                self.objects[key] = Path(source).read_bytes()
            def get_object(self, *, Bucket, Key):
                return {"Body": io.BytesIO(self.objects[Key])}
            def get_paginator(self, name):
                return self
            def paginate(self, *, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}
            def delete_objects(self, *, Bucket, Delete):
                for obj in Delete["Objects"]:
                    del self.objects[obj["Key"]]
                return {}
        client = FakeS3()
        store = S3ObjectStore("bucket", prefix="results", client=client)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "result.json"
            path.write_text('{"ok": true}', encoding="utf-8")
            store.put_file("job/result.json", path)
            store.put_file("job-other/result.json", path)
            with store.open("job/result.json") as stream:
                self.assertTrue(json.load(stream)["ok"])
            store.delete_prefix("job")
            self.assertEqual(set(client.objects), {"other/job/keep", "results/job-other/result.json"})


if __name__ == "__main__":
    unittest.main()
