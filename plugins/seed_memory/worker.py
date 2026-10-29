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
for vendor in (PLUGIN_DIR / "_vendor", PLUGIN_DIR / "_vendor_ml"):
    if vendor.is_dir() and str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
# Hermes CLI's bundled runtime owns ruamel.yaml and related runtime modules.
for runtime_site in (Path.home() / ".hermes" / "installs").glob("*/pm-runtime/generations/*/lib/python*/site-packages"):
    if runtime_site.is_dir() and str(runtime_site) not in sys.path:
        sys.path.insert(0, str(runtime_site))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout, force=True)
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()
    # Direct workers do not pass through the Hermes CLI profile bootstrap.
    # Load the profile's local .env without logging values, while preserving
    # explicitly supplied service environment variables.
    env_file = home / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key.replace("_", "").isalnum():
                os.environ.setdefault(key, value.strip().strip("\"'"))
    # Standalone tools Python needs the local Hermes Agent source for the
    # MemoryProvider base class imported by seed_memory.
    agent_root = Path.home() / ".hermes" / "hermes-agent"
    if agent_root.is_dir() and str(agent_root) not in sys.path:
        sys.path.insert(0, str(agent_root))
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
