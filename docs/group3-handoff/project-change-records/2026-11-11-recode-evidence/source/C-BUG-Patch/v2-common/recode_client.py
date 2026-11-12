"""REcode job submission, bounded polling and native artifact download."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

from first_group_client import FirstGroupClient, FirstGroupClientError
from first_group_protocol import require_first_group_request
from recode_artifacts import RecodeArtifactsClient, digest
from source_loader import normalize_relative_path


class RecodeClient(FirstGroupClient):
    def __init__(self, base_url: str, *, cache_directory: str | Path, timeout: int = 30,
                 job_timeout: float = 1800, poll_interval: float = 0.5,
                 job_id: str | None = None) -> None:
        super().__init__(base_url, timeout=timeout)
        if job_timeout <= 0 or poll_interval <= 0:
            raise FirstGroupClientError("REcode timeouts must be positive")
        if job_id is not None and not re.fullmatch(r"job_[0-9a-f]{32}", job_id):
            raise FirstGroupClientError("invalid REcode job_id")
        self.cache_directory = Path(cache_directory).resolve()
        self.job_timeout = job_timeout
        self.poll_interval = poll_interval
        self.job_id = job_id
        self._bundles = {}

    def _download(self, path: str) -> bytes:
        request = urllib.request.Request(self.base_url + path, headers={"Accept": "application/gzip"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(128 * 1024 * 1024 + 1)
                if len(raw) > 128 * 1024 * 1024:
                    raise FirstGroupClientError("REcode archive exceeds 128 MiB")
                return raw
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FirstGroupClientError("REcode artifact download failed: " + str(exc)) from exc

    def _capture(self, raw: bytes, result: dict, job_id: str) -> RecodeArtifactsClient:
        allowed = {"manifest.json", "graph.json", "external/call-chains.json", "external/resource-flow.json",
                   "external/sync-relations.json", "external/race-analysis.json"}
        files = {}
        total = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
                for count, member in enumerate(archive, start=1):
                    if count > 20000:
                        raise FirstGroupClientError("REcode archive contains too many entries")
                    name = normalize_relative_path(member.name.rstrip("/"))
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise FirstGroupClientError("REcode archive contains a link or special file")
                    total += member.size
                    if total > 256 * 1024 * 1024:
                        raise FirstGroupClientError("REcode expanded archive exceeds 256 MiB")
                    # The production sandbox exports under analysis/, while
                    # CLI bundles and development jobs use the archive root.
                    if name.startswith("analysis/"):
                        name = name.removeprefix("analysis/")
                    if name not in allowed:
                        continue
                    if name in files:
                        raise FirstGroupClientError("REcode archive has a duplicate artifact")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise FirstGroupClientError("REcode archive artifact is unreadable")
                    files[name] = stream.read()
        except (tarfile.TarError, ValueError, OSError) as exc:
            raise FirstGroupClientError("invalid REcode artifact archive: " + str(exc)) from exc
        if not {"manifest.json", "graph.json"} <= files.keys():
            raise FirstGroupClientError("REcode archive is missing its manifest/graph")
        manifest = json.loads(files["manifest.json"])
        for field in ("commit", "repository", "build_profile", "run_id"):
            if result.get(field) != manifest.get(field):
                raise FirstGroupClientError("REcode result/archive binding mismatch: " + field)
        self.cache_directory.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=job_id + "-", dir=self.cache_directory))
        for relative, contents in files.items():
            path = directory / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        (directory / "transport.json").write_text(json.dumps({"job_id": job_id,
            "base_url": self.base_url, "archive_sha256": hashlib.sha256(raw).hexdigest(),
            "result": result}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return RecodeArtifactsClient(directory)

    def _acquire(self, request: dict) -> RecodeArtifactsClient:
        job_id = self.job_id
        if job_id is None:
            repository = request["repository"]
            name = repository["name"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
                raise FirstGroupClientError("REcode submission requires a GitHub owner/repository name")
            body = {"repository_url": "https://github.com/" + name + ".git",
                    "commit": repository["commit_sha"],
                    "build_profile": (request.get("analysis") or {}).get("build_profile"),
                    "idempotency_key": "cbug-recode-" + digest({"repository": name,
                        "commit": repository["commit_sha"], "profile": (request.get("analysis") or {}).get("build_profile")})}
            status, accepted = self._request_json("POST", "/api/v1/agent/analyze", body)
            if status != 202 or not isinstance(accepted, dict):
                raise FirstGroupClientError("invalid REcode job acceptance")
            job_id = accepted.get("job_id")
            if not isinstance(job_id, str) or not re.fullmatch(r"job_[0-9a-f]{32}", job_id):
                raise FirstGroupClientError("invalid REcode accepted job id")
        deadline = time.monotonic() + self.job_timeout
        path = "/api/v1/agent/analyze/" + job_id
        while time.monotonic() < deadline:
            _, state = self._request_json("GET", path)
            status = state.get("status") if isinstance(state, dict) else None
            if status == "succeeded":
                _, result = self._request_json("GET", path + "/result")
                if not isinstance(result, dict) or result.get("status") != "succeeded":
                    raise FirstGroupClientError("REcode successful job has no successful result")
                return self._capture(self._download(path + "/artifacts"), result, job_id)
            if status in {"failed", "cancelled", "timed_out", "expiring", "expired"}:
                raise FirstGroupClientError("REcode job ended with " + status + ": " + str(state.get("error")))
            if status not in {"queued", "running", "cancelling"}:
                raise FirstGroupClientError("unexpected REcode job status: " + str(status))
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
        raise FirstGroupClientError("REcode job polling timed out: " + job_id)

    def query(self, request: dict) -> dict:
        require_first_group_request(request)
        key = digest({"repository": request["repository"], "profile": (request.get("analysis") or {}).get("build_profile")})
        if key not in self._bundles:
            self._bundles[key] = self._acquire(request)
        result = self._bundles[key].query(request)
        result["analysis_context"]["recode"]["transport"] = "job_http"
        return result
