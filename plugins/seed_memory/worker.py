"""Detached seed queue worker.

Run with ``python -m seed_memory.worker --once``.  The provider remains the
single owner of queue/db path resolution; this worker only selects the profile
and drains one queue batch under an inter-process lock.
"""
import argparse
import fcntl
import importlib
import logging
import os
import sys
from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PLUGIN_DIR.parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout, force=True)
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()
    mod = importlib.import_module("seed_memory")
    provider = mod.SeedMemoryProvider()
    provider.initialize("detached-worker", hermes_home=str(home), start_worker=False)
    queue_dir = Path(getattr(provider, "_queue_dir"))
    queue_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = queue_dir / "worker.lock"
    with open(lock_path, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        provider._drain_queue()
    return 0


def _cli_entry():
    rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(int(rc or 0))


if __name__ == "__main__":
    _cli_entry()
