import importlib.util
import json
import logging
import os
import sqlite3
import sys

import pytest
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
    p._hermes_home = tmp_path / ".hermes" / "profiles" / "test"
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


@pytest.mark.parametrize("error_factory", [
    lambda: type("InternalServerError", (Exception,), {})(),
    ConnectionError,
    lambda: TimeoutError("tagger request timed out"),
])
def test_transient_tagger_errors_stay_queued_and_count_retries(monkeypatch, tmp_path, error_factory):
    mod = _module(monkeypatch)
    p, queue = _provider(mod, tmp_path)
    path = queue / "session_transient.json"
    path.write_text(json.dumps({
        "session_id": "transient",
        "status": "ended",
        "created_at": 0,
        "updated_at": 0,
        "messages": [{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}],
    }), encoding="utf-8")

    class Store:
        DB_PATH = tmp_path / "seeds.db"
        def list_recent_seeds(self, limit=15): return []
    class Tagger:
        def extract_seeds(self, *args, **kwargs): raise error_factory()
    p._store = Store()
    p._tagger = Tagger()
    p._llm_call = lambda _prompt: "[]"
    p._writes_enabled = lambda: True
    p._user_active = lambda: False
    p._normalise_messages = lambda messages: messages or []
    p._chunk_messages = lambda messages, _size: [messages]
    p._queue_max_age_days = 99_999
    p._config = {"max_files": 6, "max_duration_s": 540, "max_retries": 3, "retry_backoff_s": 1, "tagger_retries": 0}

    p._drain_queue()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["last_error"] in {"InternalServerError", "ConnectionError", "TimeoutError"}
    assert payload["retries"] == 1
    assert payload["transient_failures"] == 1
    assert payload["next_retry_at"] > 0
    assert not (queue / "_failed").exists()


def test_tagger_retry_uses_bounded_backoff_and_jitter(monkeypatch, tmp_path):
    mod = _module(monkeypatch); p, _ = _provider(mod, tmp_path)
    calls, sleeps = [], []
    class Tagger:
        def extract_seeds(self, *args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise type("InternalServerError", (Exception,), {})()
            return ["seed"]
    p._tagger = Tagger(); p._llm_call = lambda _prompt: "[]"
    p._config = {"tagger_retries": 3}
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    monkeypatch.setattr(mod.random, "uniform", lambda _a, _b: 0.25)
    assert p._extract_seeds_with_retry([], "session", []) == ["seed"]
    assert len(calls) == 3
    assert sleeps == [2.25, 6.25]
    assert sum(sleeps) < 40


def test_tagger_timeout_is_transient(monkeypatch):
    mod = _module(monkeypatch)
    assert mod._is_transient_tagger_error(TimeoutError("tagger request timed out"))
