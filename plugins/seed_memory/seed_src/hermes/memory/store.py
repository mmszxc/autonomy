"""SQLite persistence for emotional seed memory.

The database location is configurable so the domain layer can be reused by the
Hermes Agent plugin without writing into the source tree.
"""

from __future__ import annotations

import hashlib
import math
import re
import uuid
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from .schema import Emotion, Seed, SemanticMemory, ProgramMemory

_SOURCE_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "seeds.db"
# In an installed profile this module lives under
# <profile>/plugins/seed_memory/seed_src/hermes/memory/. Prefer the profile's
# canonical database even when HERMES_SEED_DB_PATH was not exported. Source
# checkouts retain the historical package-local fallback used by tests.
_PROFILE_ROOT = Path(__file__).resolve().parents[5]
_PROFILE_DB_PATH = _PROFILE_ROOT / "seed_data" / "seeds" / "seeds.db"
_DEFAULT_DB_PATH = (
    _PROFILE_DB_PATH if _PROFILE_DB_PATH.parent.is_dir() else _SOURCE_DB_PATH
)
DB_PATH = Path(os.environ.get("HERMES_SEED_DB_PATH", _DEFAULT_DB_PATH))


def configure(db_path: str | os.PathLike[str]) -> Path:
    """Set the database used by subsequent store operations."""
    global DB_PATH
    DB_PATH = Path(db_path).expanduser().resolve()
    return DB_PATH


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _embedding_blob(embedding: object) -> bytes | None:
    if embedding is None:
        return None
    array = np.asarray(embedding, dtype=np.float32)
    if array.ndim != 1 or not array.size or not np.isfinite(array).all():
        raise ValueError("embedding must be a finite, non-empty 1-D vector")
    return array.tobytes()


def _seed_hash(seed: Seed) -> str:
    """Stable semantic hash used to make repeated extraction idempotent."""
    payload = {
        "summary": seed.summary.strip(),
        "participants": sorted(seed.participants),
        "emotion": {
            "primary": seed.emotion.primary.strip(),
            "secondary": sorted(seed.emotion.secondary),
            "note": seed.emotion.note.strip(),
        },
        "value_aligned": sorted(seed.value_aligned),
        "value_conflicted": sorted(seed.value_conflicted),
        "seeds_planted": sorted(seed.seeds_planted),
        "caused_by": sorted(seed.caused_by),
        "led_to": sorted(seed.led_to),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def init_db(db_path: str | os.PathLike[str] | None = None) -> None:
    if db_path is not None:
        configure(db_path)
    with _connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS seeds (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                summary TEXT NOT NULL,
                participants TEXT NOT NULL,
                emotion_primary TEXT NOT NULL,
                emotion_intensity REAL NOT NULL,
                emotion_secondary TEXT,
                emotion_note TEXT,
                value_aligned TEXT,
                value_conflicted TEXT,
                seeds_planted TEXT,
                importance REAL NOT NULL,
                embedding BLOB,
                access_count INTEGER DEFAULT 0,
                last_accessed TEXT,
                content_hash TEXT,
                caused_by TEXT,
                led_to TEXT,
                decay_rate REAL DEFAULT 0.05,
                recall_count INTEGER DEFAULT 0,
                shareable INTEGER DEFAULT 1
            )"""
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(seeds)")}
        migrations = {
            "content_hash": "TEXT",
            "timestamp": "TEXT",
            "embedding": "BLOB",
            "last_accessed": "TEXT",
            "emotion_secondary": "TEXT",
            "emotion_note": "TEXT",
            "value_aligned": "TEXT",
            "value_conflicted": "TEXT",
            "caused_by": "TEXT",
            "led_to": "TEXT",
            "decay_rate": "REAL DEFAULT 0.05",
            "recall_count": "INTEGER DEFAULT 0",
            "shareable": "INTEGER DEFAULT 1",
            "last_real_access": "TEXT",
            "last_reinforced": "TEXT",
        }
        for column, sql_type in migrations.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE seeds ADD COLUMN {column} {sql_type}")
        if "created_at" in columns:
            conn.execute(
                "UPDATE seeds SET timestamp=created_at "
                "WHERE timestamp IS NULL OR timestamp=''"
            )
        legacy_copies = {
            "vector": "embedding",
            "secondary_emotions": "emotion_secondary",
            "note": "emotion_note",
            "values_aligned": "value_aligned",
            "values_conflicted": "value_conflicted",
        }
        for old_column, new_column in legacy_copies.items():
            if old_column in columns:
                conn.execute(
                    f"UPDATE seeds SET {new_column}={old_column} "
                    f"WHERE {new_column} IS NULL"
                )
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_seeds_content_hash "
                "ON seeds(content_hash) WHERE content_hash IS NOT NULL"
            )
        except sqlite3.IntegrityError:
            # A legacy DB may contain duplicate hashes. Retain the oldest row
            # for each hash, then make future writes idempotent.
            conn.execute(
                "DELETE FROM seeds WHERE rowid NOT IN "
                "(SELECT MIN(rowid) FROM seeds WHERE content_hash IS NOT NULL GROUP BY content_hash) "
                "AND content_hash IS NOT NULL"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_seeds_content_hash "
                "ON seeds(content_hash) WHERE content_hash IS NOT NULL"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_seeds_importance_timestamp "
            "ON seeds(importance DESC, timestamp DESC)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS seed_edges (
                from_id TEXT,
                to_id TEXT,
                edge_type TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                created_at TEXT NOT NULL,
                PRIMARY KEY (from_id, to_id, edge_type)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS seed_expand_requests (
                id TEXT PRIMARY KEY,
                seed_id TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                processed_at TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS program_memories (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'habit',
                pattern TEXT NOT NULL DEFAULT '',
                trigger_context TEXT NOT NULL DEFAULT '',
                behavior TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.5,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                source_seeds TEXT NOT NULL DEFAULT '[]',
                last_reinforced TEXT,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed TEXT,
                embedding TEXT,
                normalised_pattern TEXT,
                normalised_behavior TEXT,
                domain_signature TEXT NOT NULL DEFAULT '',
                counter_seeds TEXT NOT NULL DEFAULT '[]',
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                writer TEXT NOT NULL DEFAULT '',
                owner TEXT NOT NULL DEFAULT '',
                provenance TEXT NOT NULL DEFAULT ''
            )"""
        )
        program_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(program_memories)")
        }
        for column, declaration in {
            "domain_signature": "TEXT DEFAULT ''",
            "counter_seeds": "TEXT DEFAULT '[]'",
            "consecutive_failures": "INTEGER DEFAULT 0",
            "normalised_pattern": "TEXT",
            "normalised_behavior": "TEXT",
            "writer": "TEXT DEFAULT ''",
            "owner": "TEXT DEFAULT ''",
            "provenance": "TEXT DEFAULT ''",
        }.items():
            if column not in program_columns:
                conn.execute(
                    f"ALTER TABLE program_memories ADD COLUMN {column} {declaration}"
                )

        conn.execute(
            """CREATE TABLE IF NOT EXISTS semantic_memories (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'value',
                statement TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.5,
                evidence_count INTEGER NOT NULL DEFAULT 0,
                source_seeds TEXT NOT NULL DEFAULT '[]',
                last_reinforced TEXT,
                access_count INTEGER NOT NULL DEFAULT 0,
                last_accessed TEXT,
                embedding TEXT,
                normalised_statement TEXT
            )"""
        )
        sem_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(semantic_memories)")
        }
        for column, declaration in {
            "normalised_statement": "TEXT",
            "embedding": "TEXT",
        }.items():
            if column not in sem_columns:
                conn.execute(
                    f"ALTER TABLE semantic_memories ADD COLUMN {column} {declaration}"
                )

        fts_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='seeds_fts'"
        ).fetchone()
        if fts_exists is None:
            try:
                conn.execute(
                    "CREATE VIRTUAL TABLE seeds_fts USING "
                    "fts5(summary, tokenize=trigram, content=seeds, content_rowid=rowid)"
                )
            except sqlite3.OperationalError:
                # SQLite versions before trigram support still have unicode61.
                conn.execute(
                    "CREATE VIRTUAL TABLE seeds_fts USING "
                    "fts5(summary, tokenize=unicode61, content=seeds, content_rowid=rowid)"
                )
            conn.execute(
                "INSERT OR IGNORE INTO seeds_fts(seeds_fts) VALUES('rebuild')"
            )

        # Migrate causal links already stored in the legacy JSON columns.
        now_iso = datetime.now(timezone.utc).isoformat()
        for row in conn.execute("SELECT id, caused_by, led_to FROM seeds"):
            for parent_id in _json_list(row["caused_by"]):
                conn.execute(
                    "INSERT OR IGNORE INTO seed_edges "
                    "(from_id, to_id, edge_type, weight, created_at) "
                    "VALUES (?, ?, 'caused_by', 1.0, ?)",
                    (parent_id, row["id"], now_iso),
                )
            for child_id in _json_list(row["led_to"]):
                conn.execute(
                    "INSERT OR IGNORE INTO seed_edges "
                    "(from_id, to_id, edge_type, weight, created_at) "
                    "VALUES (?, ?, 'led_to', 1.0, ?)",
                    (row["id"], child_id, now_iso),
                )


def _sync_seed_indexes(conn: sqlite3.Connection, seed: Seed) -> None:
    """Synchronize the FTS row and normalized causal edges for a new seed."""
    conn.execute(
        "INSERT OR REPLACE INTO seeds_fts(rowid, summary) "
        "SELECT rowid, summary FROM seeds WHERE id=?",
        (seed.id,),
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    for parent_id in seed.caused_by:
        conn.execute(
            "INSERT OR IGNORE INTO seed_edges "
            "(from_id, to_id, edge_type, weight, created_at) "
            "VALUES (?, ?, 'caused_by', 1.0, ?)",
            (parent_id, seed.id, now_iso),
        )
    for child_id in seed.led_to:
        conn.execute(
            "INSERT OR IGNORE INTO seed_edges "
            "(from_id, to_id, edge_type, weight, created_at) "
            "VALUES (?, ?, 'led_to', 1.0, ?)",
            (seed.id, child_id, now_iso),
        )


def save_seeds(seeds: List[Seed]) -> int:
    """Insert a batch in one transaction; duplicates are ignored."""
    if not seeds:
        return 0
    init_db()
    inserted = 0
    with _connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(seeds)")}
        for seed in seeds:
            embedding_blob = (
                _embedding_blob(seed.embedding)
            )
            fields = ["id", "timestamp", "summary", "participants", "emotion_primary",
                      "emotion_intensity", "emotion_secondary", "emotion_note", "value_aligned",
                      "value_conflicted", "seeds_planted", "importance", "embedding",
                      "access_count", "last_accessed", "content_hash", "caused_by", "led_to", "decay_rate",
                      "shareable"]
            values = [seed.id, seed.timestamp.isoformat(), seed.summary,
                      json.dumps(seed.participants, ensure_ascii=False), seed.emotion.primary,
                      seed.emotion.intensity, json.dumps(seed.emotion.secondary, ensure_ascii=False),
                      seed.emotion.note, json.dumps(seed.value_aligned, ensure_ascii=False),
                      json.dumps(seed.value_conflicted, ensure_ascii=False),
                      json.dumps(seed.seeds_planted, ensure_ascii=False), seed.importance,
                      embedding_blob, seed.access_count, None, _seed_hash(seed),
                      json.dumps(seed.caused_by, ensure_ascii=False), json.dumps(seed.led_to, ensure_ascii=False),
                      seed.decay_rate, int(seed.shareable)]
            if "created_at" in columns:
                fields.append("created_at"); values.append(seed.timestamp.isoformat())
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO seeds ({', '.join(fields)}) VALUES ({', '.join(['?'] * len(fields))})",
                values,
            )
            was_inserted = cursor.rowcount > 0
            inserted += int(was_inserted)
            if was_inserted:
                _sync_seed_indexes(conn, seed)
    return inserted


def save_seed(seed: Seed) -> bool:
    """Insert a seed once.

    Returns True when a row was inserted. Repeated extraction of semantically
    identical content is ignored via ``content_hash``.
    """
    init_db()
    embedding_blob = (
        _embedding_blob(seed.embedding)
    )
    with _connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(seeds)")}
        fields = [
            "id", "timestamp", "summary", "participants",
            "emotion_primary", "emotion_intensity", "emotion_secondary", "emotion_note",
            "value_aligned", "value_conflicted", "seeds_planted", "importance",
            "embedding", "access_count", "last_accessed", "content_hash",
            "caused_by", "led_to", "decay_rate", "shareable"
        ]
        values = [
            seed.id,
            seed.timestamp.isoformat(),
            seed.summary,
            json.dumps(seed.participants, ensure_ascii=False),
            seed.emotion.primary,
            seed.emotion.intensity,
            json.dumps(seed.emotion.secondary, ensure_ascii=False),
            seed.emotion.note,
            json.dumps(seed.value_aligned, ensure_ascii=False),
            json.dumps(seed.value_conflicted, ensure_ascii=False),
            json.dumps(seed.seeds_planted, ensure_ascii=False),
            seed.importance,
            embedding_blob,
            seed.access_count,
            None,
            _seed_hash(seed),
            json.dumps(seed.caused_by, ensure_ascii=False),
            json.dumps(seed.led_to, ensure_ascii=False),
            seed.decay_rate,
            int(seed.shareable),
        ]
        if "created_at" in columns:
            fields.append("created_at")
            values.append(seed.timestamp.isoformat())

        placeholders = ", ".join(["?"] * len(fields))
        sql = f"INSERT OR IGNORE INTO seeds ({', '.join(fields)}) VALUES ({placeholders})"
        cursor = conn.execute(sql, values)
        was_inserted = cursor.rowcount > 0
        if was_inserted:
            _sync_seed_indexes(conn, seed)
        return was_inserted


def update_embedding(seed_id: str, embedding: np.ndarray) -> None:
    """Update only the embedding, preserving access metadata."""
    blob = embedding.astype(np.float32).tobytes()
    with _connect() as conn:
        conn.execute("UPDATE seeds SET embedding=? WHERE id=?", (blob, seed_id))


def get_all_seeds() -> List[Seed]:
    init_db()
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM seeds ORDER BY timestamp DESC").fetchall()
    return [_row_to_seed(row) for row in rows]


def get_ranked_seeds(limit: int = 5) -> List[Seed]:
    """Embedding-free fallback: importance first, then recency."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT * FROM seeds
               ORDER BY importance DESC, timestamp DESC
               LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    return [_row_to_seed(row) for row in rows]


def touch_seed(seed_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE seeds SET access_count=access_count+1, "
            "last_accessed=?, last_real_access=? WHERE id=?",
            (
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                seed_id,
            ),
        )


def backlink_led_to(new_seed_ids: List[str], caused_by_map: Dict[str, List[str]]) -> int:
    """Reverse-link: for every seed that caused one of the new seeds, append
    the new seed id to its led_to list.  Returns the number of led_to edges
    written.

    This is the structural complement of caused_by.  The LLM can name what
    came before (caused_by) but cannot foresee what comes next (led_to), so
    led_to must be computed from caused_by after the fact, not extracted.
    """
    if not new_seed_ids or not caused_by_map:
        return 0
    new_set = set(new_seed_ids)
    edges = []
    for new_id in new_seed_ids:
        for parent_id in caused_by_map.get(new_id, []):
            if parent_id in new_set:
                continue  # same-batch cycles are noise, not causality
            edges.append((parent_id, new_id))
    if not edges:
        return 0
    written = 0
    with _connect() as conn:
        for parent_id, child_id in edges:
            row = conn.execute(
                "SELECT led_to FROM seeds WHERE id=?", (parent_id,)
            ).fetchone()
            if row is None:
                continue
            current = _json_list(row["led_to"])
            if child_id not in current:
                current.append(child_id)
                conn.execute(
                    "UPDATE seeds SET led_to=? WHERE id=?",
                    (json.dumps(current, ensure_ascii=False), parent_id),
                )
                written += 1
            conn.execute(
                "INSERT OR IGNORE INTO seed_edges "
                "(from_id, to_id, edge_type, weight, created_at) "
                "VALUES (?, ?, 'led_to', 1.0, ?)",
                (parent_id, child_id, datetime.now(timezone.utc).isoformat()),
            )
    return written


def list_recent_seeds(limit: int = 10) -> List[dict]:
    """Return the most recent seeds as plain dicts (no embeddings, no numpy).

    Used by the tagger to inject real seed IDs into the extraction prompt so the
    LLM can populate caused_by/led_to with references that actually exist.
    """
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, timestamp, summary, emotion_primary, emotion_intensity, "
            "importance, access_count FROM seeds "
            "ORDER BY timestamp DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "timestamp": row["timestamp"],
            "summary": row["summary"],
            "emotion_primary": row["emotion_primary"],
            "emotion_intensity": row["emotion_intensity"],
            "importance": row["importance"],
            "access_count": row["access_count"],
        }
        for row in rows
    ]


def _json_list(value: Optional[str]) -> list:
    return json.loads(value) if value else []


def _row_to_seed(row: sqlite3.Row) -> Seed:
    from datetime import datetime

    embedding = (
        np.frombuffer(row["embedding"], dtype=np.float32).copy()
        if row["embedding"] is not None
        else None
    )
    return Seed(
        id=row["id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        summary=row["summary"],
        participants=_json_list(row["participants"]),
        emotion=Emotion(
            primary=row["emotion_primary"],
            intensity=row["emotion_intensity"],
            secondary=_json_list(row["emotion_secondary"]),
            note=row["emotion_note"] or "",
        ),
        value_aligned=_json_list(row["value_aligned"]),
        value_conflicted=_json_list(row["value_conflicted"]),
        seeds_planted=_json_list(row["seeds_planted"]),
        caused_by=_json_list(row["caused_by"]),
        led_to=_json_list(row["led_to"]),
        importance=row["importance"],
        embedding=embedding,
        access_count=row["access_count"],
        shareable=int(row["shareable"] if row["shareable"] is not None else 1),
        decay_rate=row["decay_rate"] if row["decay_rate"] is not None else 0.05,
    )


def sanitize_causal_refs() -> dict:
    """Remove any caused_by/led_to entries that are not valid, existing seed IDs.

    Two-layer defense:
    1. Format check - strip anything that does not match seed_YYYYMMDD_HHMMSS_XXXXXX
    2. Existence check - strip IDs that are well-formed but don't exist in
       the database (the LLM occasionally hallucinates plausible-looking
       IDs with the same timestamp prefix but different hash).
    """
    seed_id_re = re.compile(r"^seed_\d{8}_\d{6}_[A-Za-z0-9]{6}$")
    cleaned = {"caused_by_removed": 0, "led_to_removed": 0, "seeds_fixed": 0}
    with _connect() as conn:
        real_ids = {row[0] for row in conn.execute("SELECT id FROM seeds").fetchall()}
        rows = conn.execute("SELECT id, caused_by FROM seeds WHERE caused_by != '[]'").fetchall()
        for row in rows:
            refs = _json_list(row["caused_by"])
            valid = [r for r in refs if isinstance(r, str) and seed_id_re.match(r) and r in real_ids]
            removed = len(refs) - len(valid)
            if removed:
                cleaned["caused_by_removed"] += removed
                cleaned["seeds_fixed"] += 1
                conn.execute("UPDATE seeds SET caused_by=? WHERE id=?",
                             (json.dumps(valid, ensure_ascii=False), row["id"]))
        rows = conn.execute("SELECT id, led_to FROM seeds WHERE led_to != '[]'").fetchall()
        for row in rows:
            refs = _json_list(row["led_to"])
            valid = [r for r in refs if isinstance(r, str) and seed_id_re.match(r) and r in real_ids]
            removed = len(refs) - len(valid)
            if removed:
                cleaned["led_to_removed"] += removed
                cleaned["seeds_fixed"] += 1
                conn.execute("UPDATE seeds SET led_to=? WHERE id=?",
                             (json.dumps(valid, ensure_ascii=False), row["id"]))
    return cleaned


def get_all_semantic_memories() -> List[SemanticMemory]:
    """Return all semantic memories (values, beliefs, world models)."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM semantic_memories ORDER BY confidence DESC, last_reinforced DESC"
        ).fetchall()
    return [_row_to_semantic(row) for row in rows]


def get_all_program_memories() -> List[ProgramMemory]:
    """Return all program memories (habits, heuristics, behaviors)."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM program_memories ORDER BY confidence DESC, last_reinforced DESC"
        ).fetchall()
    return [_row_to_program(row) for row in rows]


def _bayes_confidence(evidence_count: int, counter_count: int) -> float:
    """(e+1)/(e+c+2), clamped to [0.01, 0.99]."""
    e = max(0, int(evidence_count or 0))
    c = max(0, int(counter_count or 0))
    return min(0.99, max(0.01, (e + 1) / (e + c + 2)))


def record_program_memory_outcome(
    pm_id: str,
    failed: bool,
    counter_seed_id: str | None = None,
    source: str = "auto",
) -> Dict[str, Any]:
    """Atomically record an outcome for a program memory.

    failed=True: consecutive_failures+1, counter_seed_id appended to counter_seeds
    (deduplicated; a repeated counter_seed_id is a no-op so retries/concurrency
    never double count), confidence recomputed.
    failed=False: treated as reinforcement: evidence_count+1, consecutive_failures-1
    (floor 0), confidence recomputed.
    source="manual_rollback": nothing is recorded (human rollbacks must not pollute stats).
    Returns {"found", "recorded", "reason", "confidence", "consecutive_failures"}.
    """
    if source == "manual_rollback":
        return {"found": None, "recorded": False, "reason": "manual_rollback"}
    init_db()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT evidence_count, confidence, counter_seeds, consecutive_failures "
            "FROM program_memories WHERE id=?", (pm_id,)).fetchone()
        if row is None:
            conn.rollback()
            return {"found": False, "recorded": False, "reason": "not_found"}
        counters = _json_list(row["counter_seeds"])
        evidence = int(row["evidence_count"] or 0)
        failures = int(row["consecutive_failures"] or 0)
        if failed:
            if counter_seed_id is not None and str(counter_seed_id) in [str(c) for c in counters]:
                conn.rollback()
                return {"found": True, "recorded": False, "reason": "duplicate",
                        "confidence": row["confidence"], "consecutive_failures": failures}
            failures += 1
            if counter_seed_id is not None:
                counters.append(str(counter_seed_id))
        else:
            evidence += 1
            failures = max(0, failures - 1)
        conf = _bayes_confidence(evidence, len(counters))
        conn.execute(
            "UPDATE program_memories SET evidence_count=?, confidence=?, counter_seeds=?, "
            "consecutive_failures=? WHERE id=?",
            (evidence, conf, json.dumps(counters, ensure_ascii=False), failures, pm_id))
        conn.commit()
        return {"found": True, "recorded": True, "reason": "ok",
                "confidence": conf, "consecutive_failures": failures}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_program_memory_failure(memory_id: str, failed: bool = True) -> bool:
    """Increment the circuit breaker on failure, or reset it after success."""
    init_db()
    with _connect() as conn:
        if failed:
            cursor = conn.execute(
                "UPDATE program_memories SET consecutive_failures="
                "COALESCE(consecutive_failures, 0) + 1 WHERE id=?",
                (memory_id,),
            )
        else:
            cursor = conn.execute(
                "UPDATE program_memories SET consecutive_failures=0 WHERE id=?",
                (memory_id,),
            )
        return cursor.rowcount > 0


def _row_to_semantic(row: sqlite3.Row) -> SemanticMemory:
    return SemanticMemory(
        id=row["id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        category=row["category"],
        statement=row["statement"],
        confidence=row["confidence"],
        evidence_count=row["evidence_count"],
        source_seeds=_json_list(row["source_seeds"]),
        last_reinforced=datetime.fromisoformat(row["last_reinforced"]) if row["last_reinforced"] else None,
        access_count=row["access_count"],
        last_accessed=datetime.fromisoformat(row["last_accessed"]) if row["last_accessed"] else None,
        embedding=(
            np.frombuffer(row["embedding"], dtype=np.float32).copy()
            if row["embedding"] is not None else None
        ),
    )


def _row_to_program(row: sqlite3.Row) -> ProgramMemory:
    return ProgramMemory(
        id=row["id"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        category=row["category"],
        pattern=row["pattern"],
        trigger_context=row["trigger_context"],
        behavior=row["behavior"],
        domain_signature=row["domain_signature"] or "",
        confidence=row["confidence"],
        evidence_count=row["evidence_count"],
        source_seeds=_json_list(row["source_seeds"]),
        counter_seeds=_json_list(row["counter_seeds"]),
        consecutive_failures=row["consecutive_failures"] or 0,
        last_reinforced=datetime.fromisoformat(row["last_reinforced"]) if row["last_reinforced"] else None,
        access_count=row["access_count"],
        last_accessed=datetime.fromisoformat(row["last_accessed"]) if row["last_accessed"] else None,
        embedding=(
            np.frombuffer(row["embedding"], dtype=np.float32).copy()
            if row["embedding"] is not None else None
        ),
    )


def update_semantic_embedding(memory_id: str, embedding: np.ndarray) -> None:
    """Update only the embedding on a semantic memory, preserving metadata."""
    blob = embedding.astype(np.float32).tobytes()
    with _connect() as conn:
        conn.execute(
            "UPDATE semantic_memories SET embedding=? WHERE id=?",
            (blob, memory_id),
        )


def update_program_embedding(memory_id: str, embedding: np.ndarray) -> None:
    """Update only the embedding on a program memory, preserving metadata."""
    blob = embedding.astype(np.float32).tobytes()
    with _connect() as conn:
        conn.execute(
            "UPDATE program_memories SET embedding=? WHERE id=?",
            (blob, memory_id),
        )


def _normalise_statement(text: str) -> str:
    """Strip all whitespace and lowercase for merge-key comparison.

    Whitespace differences are noise -- two statements that differ only in
    spacing are the same thought. Removing all whitespace (not just
    collapsing) makes the merge key robust against LLM formatting variance.
    """
    return re.sub(r"\s+", "", (text or "").strip().lower())


def save_semantic_memory(
    memory: SemanticMemory,
    *,
    merge: bool = True,
) -> str:
    """Insert or merge a semantic memory.

    When merge is True (default), an existing memory with a similar
    statement is reinforced instead of creating a duplicate. The merge key
    is a normalised statement: lowercased, whitespace-collapsed. Matching
    rows get their evidence_count incremented, confidence re-weighted, and
    last_reinforced updated. source_seeds are unioned.

    Returns the id of the row that was written.
    """
    init_db()
    now = datetime.now(timezone.utc)
    norm = _normalise_statement(memory.statement)

    with _connect() as conn:
        if merge:
            row = conn.execute(
                "SELECT id, evidence_count, confidence, source_seeds "
                "FROM semantic_memories WHERE normalised_statement = ?",
                (norm,),
            ).fetchone()
            if row is not None:
                existing_seeds = _json_list(row["source_seeds"])
                new_seeds = [s for s in memory.source_seeds if s not in existing_seeds]
                merged_evidence = row["evidence_count"] + 1
                # Bayesian-smoothed, can fall again (no ratchet). Legacy rows keep their
                # stored confidence until first touched here/in outcome (no deploy-time jump).
                # Semantic memories have no counter evidence or breaker state.
                merged_confidence = _bayes_confidence(merged_evidence, 0)
                merged_sources = existing_seeds + new_seeds
                conn.execute(
                    "UPDATE semantic_memories SET evidence_count=?, confidence=?, "
                    "source_seeds=?, last_reinforced=? WHERE id=?",
                    (merged_evidence, merged_confidence,
                     json.dumps(merged_sources, ensure_ascii=False),
                     now.isoformat(), row["id"]),
                )
                return row["id"]

        memory.id = memory.id or f"sem_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        memory.timestamp = now
        memory.last_reinforced = now
        fields = ["id", "timestamp", "category", "statement", "confidence",
                  "evidence_count", "source_seeds", "last_reinforced",
                  "access_count", "last_accessed"]
        values = [memory.id, now.isoformat(), memory.category, memory.statement,
                  memory.confidence, memory.evidence_count,
                  json.dumps(memory.source_seeds, ensure_ascii=False),
                  now.isoformat(), memory.access_count,
                  None]
        conn.execute(
            f"INSERT OR IGNORE INTO semantic_memories ({', '.join(fields)}) "
            f"VALUES ({', '.join(['?'] * len(fields))})",
            values,
        )
        try:
            conn.execute(
                "ALTER TABLE semantic_memories ADD COLUMN normalised_statement TEXT"
            )
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "UPDATE semantic_memories SET normalised_statement=? WHERE id=?",
            (norm, memory.id),
        )
        return memory.id


def save_program_memory(
    memory: ProgramMemory,
    *,
    merge: bool = True,
) -> str:
    """Insert or merge a program memory.

    Merge key is the tuple (normalised pattern, normalised behavior). Matching
    rows get evidence_count incremented, confidence re-weighted, and
    last_reinforced updated. source_seeds are unioned.

    Returns the id of the row that was written.
    """
    init_db()
    now = datetime.now(timezone.utc)
    norm_pattern = _normalise_statement(memory.pattern)
    norm_behavior = _normalise_statement(memory.behavior)

    with _connect() as conn:
        if merge:
            row = conn.execute(
                "SELECT id, evidence_count, confidence, source_seeds, counter_seeds, "
                "consecutive_failures FROM program_memories "
                "WHERE normalised_pattern = ? AND normalised_behavior = ?",
                (norm_pattern, norm_behavior),
            ).fetchone()
            if row is not None:
                existing_seeds = _json_list(row["source_seeds"])
                new_seeds = [s for s in memory.source_seeds if s not in existing_seeds]
                existing_counters = _json_list(row["counter_seeds"])
                new_counters = [s for s in memory.counter_seeds if s not in existing_counters]
                merged_evidence = row["evidence_count"] + 1
                # Bayesian-smoothed, can fall again (no ratchet). Legacy rows keep their
                # stored confidence until first touched here/in outcome (no deploy-time jump).
                merged_confidence = _bayes_confidence(
                    merged_evidence, len(existing_counters) + len(new_counters))
                merged_failures = max(0, int(row["consecutive_failures"] or 0) - 1)
                conn.execute(
                    "UPDATE program_memories SET evidence_count=?, confidence=?, "
                    "source_seeds=?, counter_seeds=?, domain_signature=?, "
                    "consecutive_failures=?, last_reinforced=? WHERE id=?",
                    (merged_evidence, merged_confidence,
                     json.dumps(existing_seeds + new_seeds, ensure_ascii=False),
                     json.dumps(existing_counters + new_counters, ensure_ascii=False),
                     memory.domain_signature, merged_failures, now.isoformat(), row["id"]),
                )
                return row["id"]

        memory.id = memory.id or f"prog_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        memory.timestamp = now
        memory.last_reinforced = now
        fields = ["id", "timestamp", "category", "pattern", "trigger_context",
                  "behavior", "domain_signature", "confidence", "evidence_count",
                  "source_seeds", "counter_seeds", "consecutive_failures",
                  "writer", "owner", "provenance", "last_reinforced", "access_count", "last_accessed"]
        values = [memory.id, now.isoformat(), memory.category, memory.pattern,
                  memory.trigger_context, memory.behavior, memory.domain_signature,
                  memory.confidence, memory.evidence_count,
                  json.dumps(memory.source_seeds, ensure_ascii=False),
                  json.dumps(memory.counter_seeds, ensure_ascii=False),
                  memory.consecutive_failures, memory.writer, memory.owner, memory.provenance,
                  now.isoformat(), memory.access_count, None]
        conn.execute(
            f"INSERT OR IGNORE INTO program_memories ({', '.join(fields)}) "
            f"VALUES ({', '.join(['?'] * len(fields))})",
            values,
        )
        for col, val in (("normalised_pattern", norm_pattern), ("normalised_behavior", norm_behavior)):
            try:
                conn.execute(f"ALTER TABLE program_memories ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
            conn.execute(f"UPDATE program_memories SET {col}=? WHERE id=?", (val, memory.id))
        return memory.id


def touch_program_memory(program_id: str) -> None:
    """Increment access count and update last_accessed for a program memory."""
    with _connect() as conn:
        conn.execute(
            "UPDATE program_memories SET access_count=access_count+1, "
            "last_accessed=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), program_id),
        )


def touch_semantic_memory(semantic_id: str) -> None:
    """Increment access count and update last_accessed for a semantic memory."""
    with _connect() as conn:
        conn.execute(
            "UPDATE semantic_memories SET access_count=access_count+1, "
            "last_accessed=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), semantic_id),
        )


# ---------------------------------------------------------------------------
# Decay maintenance — Phase 1: dry-run only, never writes importance
# ---------------------------------------------------------------------------

# Half-life: a seed with decay_rate=0.05 reaches ~50% importance after ~14 days
# (ln(2)/0.05 ≈ 13.9 days).  Default rate 0.05 chosen so typical seeds stay
# retrievable for weeks and gradually yield to newer experience.
_DECAY_FLOOR: float = 0.05          # effective importance is never driven below this
_DECAY_DORMANT_THRESHOLD: float = 0.15  # candidate for dormant label if below this
_DECAY_EXEMPT_IMPORTANCE: float = 0.85  # seeds born with importance >= this are exempt
_DECAY_DORMANT_MIN_AGE_DAYS: float = 7.0  # must also be >=7d since last real access


def _decay_effective_importance(
    importance: float,
    decay_rate: float,
    age_days: float,
) -> float:
    """importance * exp(-decay_rate * age_days), clamped to [_DECAY_FLOOR, 1.0]."""
    effective = importance * math.exp(-decay_rate * min(age_days, 365.0))
    return max(_DECAY_FLOOR, min(1.0, effective))


def decay_due_seeds(
    now: "datetime | None" = None,
    *,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Compute time-decayed effective importance for every seed and return a report.

    Phase-1 contract: ``dry_run=True`` (the default) is the ONLY supported mode.
    ``dry_run=False`` is reserved for Phase 2 (post 14-day observe window) and
    raises ``RuntimeError`` to prevent accidental writes during the observe period.

    Parameters
    ----------
    now:
        Reference time for age calculation. Defaults to ``datetime.now(UTC)``.
    dry_run:
        Must be True in Phase 1. Set to False only after the 14-day observe
        window ends and Phase 2 is explicitly activated (see rollback design below).

    Returns
    -------
    A report dict with keys:
        total           — total seeds scanned
        exempt          — seeds born with importance >= _DECAY_EXEMPT_IMPORTANCE
        no_decay_needed — seeds whose effective importance >= _DECAY_DORMANT_THRESHOLD
        dormant_candidates — seeds whose effective < _DECAY_DORMANT_THRESHOLD AND
                             age_since_real_access >= _DECAY_DORMANT_MIN_AGE_DAYS
        decay_candidates  — seeds whose effective dropped below current importance
                            but are not yet dormant candidates
        distribution    — {"<0.05": N, "0.05-0.15": N, "0.15-0.50": N, "0.50+": N}
        dry_run         — echoed
        entries         — list of per-seed dicts (id, importance, effective, age_days,
                          age_real_days, is_exempt, is_dormant_candidate)

    Phase-2 write activation design
    --------------------------------
    When the 14-day observe period ends (2026-11-28), enable writes by:
      1. Set ``HERMES_DECAY_WRITE_ENABLED=1`` in the profile .env.
      2. The caller (autonomy tick maintenance slot) passes ``dry_run=False``.
      3. decay_due_seeds will UPDATE seeds SET importance=effective WHERE id=?
         for all non-exempt seeds whose effective < importance.
      4. Rollback: ``git revert HEAD~1`` or ``HERMES_DECAY_WRITE_ENABLED=`` (unset)
         re-disables writes. No DB migration needed: importance values already
         written can be left in place (they were computed deterministically).
    """
    if not dry_run:
        raise RuntimeError(
            "decay_due_seeds: dry_run=False is reserved for Phase 2 "
            "(post 2026-11-28 observe window). "
            "Set HERMES_DECAY_WRITE_ENABLED=1 and confirm with the team lead "
            "before enabling writes."
        )

    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    init_db()
    with _connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(seeds)")}
        has_real_access = "last_real_access" in columns
        select_cols = "id, importance, decay_rate, timestamp, last_accessed"
        if has_real_access:
            select_cols += ", last_real_access"
        rows = conn.execute(f"SELECT {select_cols} FROM seeds").fetchall()

    total = len(rows)
    exempt = 0
    no_decay_needed = 0
    dormant_candidates = 0
    decay_candidates = 0
    dist: Dict[str, int] = {"<0.05": 0, "0.05-0.15": 0, "0.15-0.50": 0, "0.50+": 0}
    entries: List[Dict[str, Any]] = []

    for row in rows:
        importance = float(row["importance"] or 0.5)
        decay_rate = float(row["decay_rate"] or 0.05)

        # Age since creation/last_accessed (for effective importance)
        baseline_ts = row["last_accessed"] or row["timestamp"]
        try:
            baseline = datetime.fromisoformat(baseline_ts)
            if baseline.tzinfo is None:
                baseline = baseline.replace(tzinfo=timezone.utc)
            age_days = (now - baseline).total_seconds() / 86400.0
        except Exception:
            age_days = 0.0

        # Age since last real (non-prefetch) access
        real_access_ts = None
        if has_real_access:
            real_access_ts = row["last_real_access"]
        age_real_days: float
        if real_access_ts:
            try:
                rt = datetime.fromisoformat(real_access_ts)
                if rt.tzinfo is None:
                    rt = rt.replace(tzinfo=timezone.utc)
                age_real_days = (now - rt).total_seconds() / 86400.0
            except Exception:
                age_real_days = age_days
        else:
            age_real_days = age_days

        is_exempt = importance >= _DECAY_EXEMPT_IMPORTANCE
        effective = _decay_effective_importance(importance, decay_rate, age_days)

        is_dormant_candidate = (
            not is_exempt
            and effective < _DECAY_DORMANT_THRESHOLD
            and age_real_days >= _DECAY_DORMANT_MIN_AGE_DAYS
        )

        # Bucketize for distribution
        if effective < _DECAY_FLOOR:
            dist["<0.05"] += 1
        elif effective < _DECAY_DORMANT_THRESHOLD:
            dist["0.05-0.15"] += 1
        elif effective < 0.50:
            dist["0.15-0.50"] += 1
        else:
            dist["0.50+"] += 1

        if is_exempt:
            exempt += 1
        elif is_dormant_candidate:
            dormant_candidates += 1
        elif effective < importance:
            decay_candidates += 1
        else:
            no_decay_needed += 1

        entries.append({
            "id": row["id"],
            "importance": importance,
            "effective": round(effective, 4),
            "age_days": round(age_days, 2),
            "age_real_days": round(age_real_days, 2),
            "is_exempt": is_exempt,
            "is_dormant_candidate": is_dormant_candidate,
        })

    return {
        "total": total,
        "exempt": exempt,
        "no_decay_needed": no_decay_needed,
        "dormant_candidates": dormant_candidates,
        "decay_candidates": decay_candidates,
        "distribution": dist,
        "dry_run": dry_run,
        "entries": entries,
    }
