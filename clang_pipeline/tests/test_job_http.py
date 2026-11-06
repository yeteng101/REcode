from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from clang_pipeline import agent_server
from clang_pipeline.agent_jobs import AnalysisJobManager
from clang_pipeline.tests.test_durable_jobs import URL, runner, wait_for


class JobHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = AnalysisJobManager(self.temp.name, max_workers=1, runner=runner)
        self.patcher = patch.object(agent_server, "_JOB_MANAGER", self.manager)
        self.patcher.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), agent_server.AgentHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/api/v1/agent/analyze"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.patcher.stop()
        self.manager.close()
        self.temp.cleanup()

    def request(self, suffix="", body=None, headers=None):
        request = Request(self.base + suffix, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), response.headers

    def test_idempotency_conflict_status_and_downloads(self):
        status, raw, _ = self.request(body={"repository_url": URL}, headers={"Idempotency-Key": "http-key"})
        self.assertEqual(status, 202)
        job = json.loads(raw)
        status, again, _ = self.request(body={"repository_url": URL}, headers={"Idempotency-Key": "http-key"})
        self.assertEqual(json.loads(again)["job_id"], job["job_id"])
        status, _, _ = self.request(body={"repository_url": URL, "commit": "different"}, headers={"Idempotency-Key": "http-key"})
        self.assertEqual(status, 409)
        wait_for(self.manager, job["job_id"])
        status, raw, _ = self.request("/" + job["job_id"])
        self.assertEqual((status, json.loads(raw)["status"]), (200, "succeeded"))
        status, raw, headers = self.request("/" + job["job_id"] + "/result")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["repository_url"], URL)
        self.assertEqual(int(headers["Content-Length"]), len(raw))
        status, raw, headers = self.request("/" + job["job_id"] + "/artifacts")
        self.assertEqual(status, 200)
        self.assertEqual(raw[:2], b"\x1f\x8b")
        self.assertEqual(headers["Content-Type"], "application/gzip")

    def test_missing_object_and_bad_options(self):
        self.assertEqual(self.request("/job_missing/result")[0], 404)
        self.assertEqual(self.request(body={"repository_url": URL, "entry_symbols": "not-a-list"})[0], 400)

    def test_configured_api_defaults_disable_local_paths(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict("os.environ", {"CRA_JOB_ROOT": root, "CRA_MAX_WORKERS": "0", "CRA_SANDBOX_IMAGE": "recode/sandbox@sha256:" + "a" * 64}, clear=True):
                with agent_server.configured_job_manager() as manager:
                    self.assertFalse(manager.allow_local_repositories)
                    self.assertEqual(
                        manager.queue.path,
                        (Path(root) / "jobs.sqlite3").resolve(),
                    )


if __name__ == "__main__":
    unittest.main()
