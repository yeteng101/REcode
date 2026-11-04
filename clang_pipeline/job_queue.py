from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .clang_ast import utc_now

TERMINAL = {"succeeded", "failed", "cancelled", "timed_out"}


class JobError(RuntimeError):
    pass


class IdempotencyConflict(JobError):
    pass


def event(state: dict[str, Any], name: str, message: str) -> None:
    state["trace"].append({"event": name, "timestamp": utc_now(), "message": message})


class JobQueue:
    """SQLite WAL queue. Every state transition uses an immediate transaction.

    A claim token fences stale workers; leases recover crashed processes. The DB
    must reside on a local persistent volume, not an NFS/SMB share.
    """

    def __init__(self, path: Path, *, lease_seconds: float, retention_seconds: float):
        self.path = path
        self.lease_seconds = lease_seconds
        self.retention_seconds = retention_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise RuntimeError(f"unsupported job database schema version: {version}")
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE,
                fingerprint TEXT NOT NULL, request TEXT NOT NULL,
                state TEXT NOT NULL, status TEXT NOT NULL,
                available_at REAL NOT NULL, lease_until REAL,
                owner TEXT, claim TEXT, expires_at REAL
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, available_at)")
            db.execute("CREATE INDEX IF NOT EXISTS jobs_expiry ON jobs(status, expires_at)")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            # journal_mode must be set outside a write transaction.
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _save(self, db, state, *, available_at=None, lease_until=None,
              owner=None, claim=None, expires_at=None):
        db.execute("""UPDATE jobs SET state=?, status=?, available_at=?,
            lease_until=?, owner=?, claim=?, expires_at=? WHERE job_id=?""",
            (json.dumps(state), state["status"], available_at or time.time(),
             lease_until, owner, claim, expires_at, state["job_id"]))

    def submit(self, state, request, fingerprint, key):
        with self.transaction() as db:
            if key is not None:
                row = db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
                if row:
                    if row["fingerprint"] != fingerprint:
                        raise IdempotencyConflict("idempotency key already used for a different request")
                    return json.loads(row["state"])
            db.execute("""INSERT INTO jobs(job_id,idempotency_key,fingerprint,request,state,
                status,available_at) VALUES(?,?,?,?,?,'queued',?)""",
                (state["job_id"], key, fingerprint, json.dumps(request), json.dumps(state), time.time()))
            return state

    def get(self, job_id):
        with self.transaction() as db:
            row = db.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobError(f"job not found: {job_id}")
            return json.loads(row["state"])

    def _terminal(self, db, state):
        state["finished_at"] = utc_now()
        state["expires_at"] = time.time() + self.retention_seconds
        self._save(db, state, expires_at=state["expires_at"])

    def claim(self, owner):
        with self.transaction() as db:
            now = time.time()
            stale = db.execute("SELECT * FROM jobs WHERE status IN ('running','cancelling') AND lease_until<=?", (now,)).fetchall()
            for row in stale:
                state = json.loads(row["state"])
                event(state, "recovered", "worker lease expired")
                if state["cancel_requested"]:
                    state["status"] = "cancelled"
                    self._terminal(db, state)
                elif state["attempts"] >= state["max_attempts"]:
                    state["status"] = "failed"
                    state["error"] = {"code": "worker_lost", "message": "worker lease expired; attempts exhausted", "retryable": False}
                    self._terminal(db, state)
                else:
                    state["status"] = "queued"
                    self._save(db, state, available_at=now)
            row = db.execute("SELECT * FROM jobs WHERE status='queued' AND available_at<=? ORDER BY available_at,job_id LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            state = json.loads(row["state"])
            state.update(status="running", started_at=utc_now(), finished_at=None, error=None)
            state["attempts"] += 1
            claim = uuid.uuid4().hex
            output = Path(state["job_dir"]) / f"attempt-{state['attempts']}-{claim}"
            state.update(output_dir=str(output), tool_trace_path=str(output / "trace/tool-trace.jsonl"),
                         tool_trace_summary_path=str(output / "trace/tool-trace-summary.json"))
            event(state, "started", f"attempt {state['attempts']} started")
            self._save(db, state, lease_until=now + self.lease_seconds, owner=owner, claim=claim)
            return state, json.loads(row["request"]), claim

    def heartbeat(self, job_id, claim):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=? AND claim=? AND lease_until>?", (job_id, claim, time.time())).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET lease_until=? WHERE job_id=?", (time.time() + self.lease_seconds, job_id))
            return json.loads(row["state"])

    def finish(self, job_id, claim, status, *, result=None, error=None,
               summary=None, objects=None, retry_delay=0):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=? AND claim=? AND lease_until>?", (job_id, claim, time.time())).fetchone()
            if row is None:
                return False
            state = json.loads(row["state"])
            if state["cancel_requested"]:
                if status == "succeeded":
                    objects = {}  # A late cancellation must not expose a success bundle.
                status, result = "cancelled", None
            state.update(status=status, result=result, error=error, tool_trace=summary)
            state["objects"] = objects or {}
            if status in {"failed", "timed_out"} and error and error.get("retryable") and state["attempts"] < state["max_attempts"]:
                state["status"] = "queued"
                event(state, "retry_scheduled", f"retry in {retry_delay:g}s: {error['message']}")
                self._save(db, state, available_at=time.time() + retry_delay)
            else:
                event(state, "completed" if status == "succeeded" else status,
                      "analysis completed" if status == "succeeded" else (error or {}).get("message", status))
                self._terminal(db, state)
            return True

    def cancel(self, job_id, reason):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobError(f"job not found: {job_id}")
            state = json.loads(row["state"])
            if state["status"] in TERMINAL | {"expired", "expiring"}:
                return state
            state.update(cancel_requested=True, cancel_reason=reason)
            event(state, "cancel_requested", reason)
            if state["status"] == "queued":
                state["status"] = "cancelled"
                self._terminal(db, state)
            else:
                state["status"] = "cancelling"
                self._save(db, state, lease_until=row["lease_until"], owner=row["owner"], claim=row["claim"])
            return state

    def release(self, owner):
        with self.transaction() as db:
            for row in db.execute("SELECT * FROM jobs WHERE owner=?", (owner,)).fetchall():
                state = json.loads(row["state"])
                event(state, "recovered", "worker stopped; execution returned to queue")
                if state["cancel_requested"]:
                    state["status"] = "cancelled"
                    self._terminal(db, state)
                elif state["attempts"] >= state["max_attempts"]:
                    state["status"] = "failed"
                    state["error"] = {"code": "worker_stopped", "message": "worker stopped; attempts exhausted", "retryable": False}
                    self._terminal(db, state)
                else:
                    state["status"] = "queued"
                    self._save(db, state)

    def expire(self, delete):
        # Keep tombstones (including idempotency keys) so old requests cannot
        # silently execute again. Deletion is repeatable after partial failures.
        with self.transaction() as db:
            rows = db.execute("SELECT * FROM jobs WHERE expires_at<=? AND status IN ('succeeded','failed','cancelled','timed_out','expiring')", (time.time(),)).fetchall()
            for row in rows:
                state = json.loads(row["state"])
                state["status"] = "expiring"
                self._save(db, state, expires_at=row["expires_at"])
        count = 0
        for row in rows:
            state = json.loads(row["state"])
            delete(state)
            with self.transaction() as db:
                state.update(status="expired", result=None, objects={}, tool_trace=None,
                             tool_trace_path=None, tool_trace_summary_path=None,
                             output_dir=None, error=None)
                state["trace"] = []
                event(state, "expired", "result objects and working files deleted")
                self._save(db, state)
                db.execute("UPDATE jobs SET request='{}' WHERE job_id=?", (state["job_id"],))
                count += 1
        return count
