"""T2: Bayesian confidence + failure counting for program_memories."""
from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "seed_memory" / "seed_src"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for name in [m for m in sys.modules if m == "hermes" or m.startswith("hermes.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(SOURCE_ROOT))
    from hermes.memory import store, retrieval
    from hermes.memory.schema import ProgramMemory
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "seeds.db")
    store.init_db()
    yield store, retrieval, ProgramMemory
    for name in [m for m in sys.modules if m == "hermes" or m.startswith("hermes.")]:
        sys.modules.pop(name, None)


def _pm(PM, **kw):
    base = dict(id="", timestamp=None, category="habit", pattern="when alpha zebra",
                trigger_context="ctx", behavior="do beta", domain_signature="d",
                confidence=0.5, evidence_count=1,
                writer="test-writer", owner="test-owner", provenance="l0:test")
    base.update(kw)
    return PM(**base)


def _row(store, pid):
    with store._connect() as c:
        return dict(c.execute("SELECT * FROM program_memories WHERE id=?", (pid,)).fetchone())


def test_reinforce_raises_confidence(env):
    store, _, PM = env
    pid = store.save_program_memory(_pm(PM))
    c0 = _row(store, pid)["confidence"]
    store.save_program_memory(_pm(PM)); store.save_program_memory(_pm(PM))
    assert _row(store, pid)["confidence"] > c0


def test_counter_example_lowers_confidence(env):
    store, _, PM = env
    pid = store.save_program_memory(_pm(PM))
    for _ in range(3):
        store.save_program_memory(_pm(PM))
    hi = _row(store, pid)["confidence"]
    store.record_program_memory_outcome(pid, True, "seed_x")
    lo = _row(store, pid)
    assert lo["confidence"] < hi and '"seed_x"' in lo["counter_seeds"]


def test_two_failures_filtered_by_retrieval(env):
    store, retrieval, PM = env
    pid = store.save_program_memory(_pm(PM))
    assert [e for e in retrieval.retrieve_program_evidence("alpha zebra", [])]
    store.record_program_memory_outcome(pid, True, "s1")
    store.record_program_memory_outcome(pid, True, "s2")
    assert _row(store, pid)["consecutive_failures"] == 2
    assert retrieval.retrieve_program_evidence("alpha zebra", []) == []


def test_reinforce_decrements_not_clears(env):
    store, _, PM = env
    pid = store.save_program_memory(_pm(PM))
    for s in ("a", "b", "c"):
        store.record_program_memory_outcome(pid, True, s)
    store.save_program_memory(_pm(PM))
    assert _row(store, pid)["consecutive_failures"] == 2
    store.update_program_memory_failure(pid, failed=False)  # legacy semantics: clear
    assert _row(store, pid)["consecutive_failures"] == 0


def test_manual_rollback_not_recorded(env):
    store, _, PM = env
    pid = store.save_program_memory(_pm(PM))
    before = _row(store, pid)
    r = store.record_program_memory_outcome(pid, True, "s1", source="manual_rollback")
    assert r["recorded"] is False
    assert _row(store, pid) == before


def test_legacy_rows_survive_and_keep_confidence(tmp_path, monkeypatch):
    for name in [m for m in sys.modules if m == "hermes" or m.startswith("hermes.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(SOURCE_ROOT))
    from hermes.memory import store
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE program_memories (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, "
                 "category TEXT, pattern TEXT, trigger_context TEXT, behavior TEXT, "
                 "confidence REAL, evidence_count INTEGER, source_seeds TEXT, last_reinforced TEXT, "
                 "access_count INTEGER DEFAULT 0, last_accessed TEXT, embedding BLOB)")
    for i in range(5):
        conn.execute("INSERT INTO program_memories (id,timestamp,category,pattern,trigger_context,behavior,"
                     "confidence,evidence_count,source_seeds) VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"p{i}", "2026-01-01T00:00:00+00:00", "habit", f"pat{i}", "c", f"beh{i}", 0.9, 3, "[]"))
    conn.commit(); conn.close()
    monkeypatch.setattr(store, "DB_PATH", db)
    store.init_db()
    with store._connect() as c:
        rows = c.execute("SELECT id, confidence FROM program_memories").fetchall()
    assert len(rows) == 5 and all(r["confidence"] == 0.9 for r in rows)
    store.record_program_memory_outcome("p0", True, "s")
    with store._connect() as c:
        assert c.execute("SELECT confidence FROM program_memories WHERE id='p0'").fetchone()[0] != 0.9
    for name in [m for m in sys.modules if m == "hermes" or m.startswith("hermes.")]:
        sys.modules.pop(name, None)


def test_duplicate_and_concurrent_counter_seed_not_duplicated(env):
    store, _, PM = env
    pid = store.save_program_memory(_pm(PM))
    results = []
    def go():
        results.append(store.record_program_memory_outcome(pid, True, "same"))
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    row = _row(store, pid)
    assert row["counter_seeds"] == '["same"]'
    assert row["consecutive_failures"] == 1
    assert sum(r["recorded"] for r in results) == 1
