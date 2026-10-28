"""SQLite persistence for emotional seed memory.

The database location is configurable so the domain layer can be reused by the
Hermes Agent plugin without writing into the source tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import List, Optional

import numpy as np

from .schema import Emotion, Seed

_DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "seeds.db"
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
                decay_rate REAL DEFAULT 0.05
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
                      "access_count", "last_accessed", "content_hash", "caused_by", "led_to", "decay_rate"]
            values = [seed.id, seed.timestamp.isoformat(), seed.summary,
                      json.dumps(seed.participants, ensure_ascii=False), seed.emotion.primary,
                      seed.emotion.intensity, json.dumps(seed.emotion.secondary, ensure_ascii=False),
                      seed.emotion.note, json.dumps(seed.value_aligned, ensure_ascii=False),
                      json.dumps(seed.value_conflicted, ensure_ascii=False),
                      json.dumps(seed.seeds_planted, ensure_ascii=False), seed.importance,
                      embedding_blob, seed.access_count, None, _seed_hash(seed),
                      json.dumps(seed.caused_by, ensure_ascii=False), json.dumps(seed.led_to, ensure_ascii=False),
                      seed.decay_rate]
            if "created_at" in columns:
                fields.append("created_at"); values.append(seed.timestamp.isoformat())
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO seeds ({', '.join(fields)}) VALUES ({', '.join(['?'] * len(fields))})",
                values,
            )
            inserted += int(cursor.rowcount > 0)
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
            "caused_by", "led_to", "decay_rate"
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
        ]
        if "created_at" in columns:
            fields.append("created_at")
            values.append(seed.timestamp.isoformat())

        placeholders = ", ".join(["?"] * len(fields))
        sql = f"INSERT OR IGNORE INTO seeds ({', '.join(fields)}) VALUES ({placeholders})"
        cursor = conn.execute(sql, values)
        return cursor.rowcount > 0


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
            "UPDATE seeds SET access_count=access_count+1 WHERE id=?",
            (seed_id,),
        )


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
        decay_rate=row["decay_rate"] if row["decay_rate"] is not None else 0.05,
    )
