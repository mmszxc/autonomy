from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "plugins" / "seed_memory" / "seed_src"
sys.path.insert(0, str(SOURCE_ROOT))


def test_reader_gate_threshold_and_provenance(tmp_path, monkeypatch):
    from hermes.memory import store

    db = tmp_path / "seeds.db"
    monkeypatch.setenv("HERMES_SEED_DB_PATH", str(db))
    monkeypatch.setattr(store, "DB_PATH", db)
    store.init_db(str(db))
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO seeds (id, timestamp, summary, participants, emotion_primary, emotion_intensity, importance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("seed-1", "2026-01-01T00:00:00+00:00", "source", "[]", "neutral", 0.0, 0.8),
        )
    single = {"confidence": 2 / 3, "evidence_count": 1, "source_seeds": ["seed-1"]}
    assert not store.semantic_memory_readable(single)
    good = {"confidence": 0.75, "evidence_count": 2, "source_seeds": ["seed-1", "seed-1b"]}
    assert store.semantic_memory_readable(good, source_seed_ids={"seed-1", "seed-1b"})
    assert not store.semantic_memory_readable({**good, "confidence": 0.59})
    assert not store.semantic_memory_readable({**good, "source_seeds": ["missing"]})


def test_semantic_schema_embedding_is_blob(tmp_path, monkeypatch):
    import sqlite3
    from hermes.memory import store

    db = tmp_path / "schema.db"
    monkeypatch.setenv("HERMES_SEED_DB_PATH", str(db))
    monkeypatch.setattr(store, "DB_PATH", db)
    store.init_db(str(db))
    with sqlite3.connect(db) as conn:
        kind = conn.execute(
            "SELECT type FROM pragma_table_info('semantic_memories') WHERE name='embedding'"
        ).fetchone()[0]
    assert kind.upper() == "BLOB"
