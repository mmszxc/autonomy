"""Read-only search, filtering and pagination for memory seeds."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..redact import safe_text
from .sqlite_ro import connect

_FIELDS = (
    "id",
    "timestamp",
    "created_at",
    "summary",
    "emotion_primary",
    "emotion_intensity",
    "importance",
    "caused_by",
    "seeds_planted",
)


def _columns(connection: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in connection.execute("PRAGMA table_info(seeds)")}


def _json_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    try:
        result = json.loads(str(value))
    except (TypeError, ValueError):
        return [str(value)]
    return [str(item) for item in result] if isinstance(result, list) else [str(result)]


def _conditions(columns: set[str], query: str, emotion: str) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if query:
        searchable = [name for name in ("id", "summary", "emotion_primary", "caused_by", "seeds_planted") if name in columns]
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(" + " OR ".join(f"{name} LIKE ? ESCAPE '\\'" for name in searchable) + ")")
        parameters.extend([f"%{escaped}%"] * len(searchable))
    if emotion and "emotion_primary" in columns:
        clauses.append("emotion_primary = ?")
        parameters.append(emotion)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), parameters


def load(
    db_path: Path,
    limit: int = 20,
    query: str = "",
    offset: int = 0,
    emotion: str = "",
    sort: str = "time",
) -> dict[str, Any]:
    limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
    empty = {"items": [], "total": 0, "limit": limit, "offset": offset}
    if not db_path.is_file():
        return {**empty, "emotions": [], "error": "种子库不存在"}
    try:
        with connect(db_path) as connection:
            columns = _columns(connection)
            if "summary" not in columns:
                return {**empty, "emotions": [], "error": "种子表格式不受支持"}
            selected = [name for name in _FIELDS if name in columns]
            where, parameters = _conditions(columns, query.strip()[:200], emotion.strip()[:80])
            total = int(connection.execute("SELECT COUNT(*) FROM seeds" + where, parameters).fetchone()[0])
            timestamp = "timestamp" if "timestamp" in columns else ("created_at" if "created_at" in columns else "rowid")
            order = f"importance DESC, {timestamp} DESC" if sort == "importance" and "importance" in columns else f"{timestamp} DESC"
            rows = connection.execute(
                f"SELECT {', '.join(selected)} FROM seeds{where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            ).fetchall()
            emotions = []
            if "emotion_primary" in columns:
                emotions = [
                    {"name": safe_text(row[0] or "中性"), "count": int(row[1])}
                    for row in connection.execute(
                        "SELECT emotion_primary,COUNT(*) FROM seeds GROUP BY emotion_primary ORDER BY COUNT(*) DESC"
                    )
                ]
    except (OSError, sqlite3.Error):
        return {**empty, "emotions": [], "error": "种子库读取失败"}

    items = []
    for row in rows:
        raw = dict(row)
        items.append(
            {
                "id": safe_text(raw.get("id") or ""),
                "timestamp": safe_text(raw.get("timestamp") or raw.get("created_at") or ""),
                "summary": safe_text(raw.get("summary") or ""),
                "emotion": safe_text(raw.get("emotion_primary") or "中性"),
                "emotion_intensity": raw.get("emotion_intensity"),
                "importance": raw.get("importance"),
                "caused_by": [safe_text(item) for item in _json_list(raw.get("caused_by"))],
                "seeds_planted": [safe_text(item) for item in _json_list(raw.get("seeds_planted"))],
            }
        )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if offset + limit < total else None,
        "emotions": emotions,
        "sort": "importance" if sort == "importance" else "time",
    }
