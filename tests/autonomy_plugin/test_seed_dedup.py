import importlib.util
import json
import logging
import os
import sqlite3
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = Path(os.environ.get("SEED_DEDUP_PLUGIN", ROOT / "plugins" / "seed_memory" / "__init__.py"))


def _module(monkeypatch):
    # Minimal public Agent contract; dedup itself must not require a full agent.
    agent = types.ModuleType("agent"); mp = types.ModuleType("agent.memory_provider")
    mp.MemoryProvider = type("MemoryProvider", (), {})
    mp.RecallStatus = type("RecallStatus", (), {})
    monkeypatch.setitem(sys.modules, "agent", agent); monkeypatch.setitem(sys.modules, "agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location("seed_dedup_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def _provider(mod, tmp_path, summaries=()):
    db = tmp_path / "seeds.db"; q = tmp_path / "queue"; q.mkdir()
    conn = sqlite3.connect(db); conn.execute("create table seeds(summary text)")
    conn.executemany("insert into seeds values(?)", [(x,) for x in summaries]); conn.commit(); conn.close()
    class Store: DB_PATH = db
    p = object.__new__(mod.SeedMemoryProvider); p._store = Store(); p._queue_dir = q
    return p, q


def _seeds(*values): return [types.SimpleNamespace(summary=x) for x in values]


def test_dedup_existing_batch_normalization_and_different_content(monkeypatch, tmp_path):
    mod = _module(monkeypatch); p, q = _provider(mod, tmp_path, ["Hello, World!"])
    kept, stats = p._dedup_seeds(_seeds(" hello world ", "NEW fact", "new FACT!!!", "another"))
    assert [x.summary for x in kept] == ["NEW fact", "another"]
    assert stats["duplicate"] == 2 and stats["inserted"] == 2
    assert json.loads((q / "stats.json").read_text())["duplicate"] == 2


def test_dedup_empty_or_missing_table_keeps_seeds(monkeypatch, tmp_path):
    mod = _module(monkeypatch); p, _ = _provider(mod, tmp_path)
    kept, stats = p._dedup_seeds(_seeds("one", "two"))
    assert len(kept) == 2 and stats["inserted"] == 2


def test_drain_ignores_operational_sidecars_in_all_counters(monkeypatch, tmp_path, caplog):
    mod = _module(monkeypatch)
    queue = tmp_path / "queue"; queue.mkdir()
    for name in ("stats.json", ".worker.unit", "worker.log", "session_x.json.lock"):
        (queue / name).write_text("{}", encoding="utf-8")
    p = object.__new__(mod.SeedMemoryProvider)
    p._queue_dir = queue
    p._config = {"max_files": 6, "max_duration_s": 540}
    p._writes_enabled = lambda: True
    p._user_active = lambda: False
    caplog.set_level(logging.INFO, logger=mod.logger.name)
    p._drain_queue()
    assert "queue_file_count=0" in caplog.text
    assert "drain summary inspected=0 skipped=0 processed=0 backlog=0" in caplog.text
