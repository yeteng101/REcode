"""Standalone durable queue worker (same host/volumes as the API process)."""
from __future__ import annotations

import argparse
import logging
import signal
import threading

from .agent_server import configured_job_manager


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--cleanup-only", action="store_true")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be positive")
    logging.basicConfig(level=logging.INFO)
    stopped = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: stopped.set())
    with configured_job_manager(max_workers=0 if args.cleanup_only else args.workers) as manager:
        if args.cleanup_only:
            manager.cleanup()
        else:
            stopped.wait()


if __name__ == "__main__":
    main()
