"""JSON API routing over read-only data sources."""

from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from typing import Any

from .config import resolve_paths
from .redact import safe_text
from .sources import model_route, seeds, status, thoughts


def _integer(query: dict[str, list[str]], name: str, default: int, low: int, high: int) -> int:
    try:
        value = int((query.get(name) or [str(default)])[0])
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


def json_response(payload: Any, code: int = HTTPStatus.OK) -> tuple[int, str, bytes]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return code, "application/json; charset=utf-8", body


class ReadOnlyApi:
    def __init__(self, profile: str, home: Path) -> None:
        self.profile = profile
        self.home = home
        paths = resolve_paths(home)
        self.paths = paths
        self.autonomy_db = paths["autonomy_db"]
        self.thought_log = paths["thought_log"]
        self.seed_db = paths["seed_db"]
        self.queue = paths["queue"]
        self.notes_to_team = paths["data_dir"] / "notes_to_team.md"

    def _load_notes_to_team(self, limit: int = 1024) -> dict[str, Any]:
        try:
            size = self.notes_to_team.stat().st_size
            with self.notes_to_team.open("rb") as handle:
                if size > limit:
                    handle.seek(-limit, 2)
                content = handle.read(limit).decode("utf-8", errors="replace")
        except OSError:
            return {"available": False, "content": "", "truncated": False}
        return {
            "available": True,
            "content": safe_text(content),
            "truncated": size > limit,
        }

    def route(self, path: str, query: dict[str, list[str]]) -> tuple[int, str, bytes]:
        if path == "/api/thoughts":
            count = _integer(query, "n", 20, 1, 100)
            return json_response({"items": thoughts.load(self.thought_log, count), "count": count})
        if path == "/api/seeds":
            return json_response(
                seeds.load(
                    self.seed_db,
                    _integer(query, "limit", 20, 1, 100),
                    (query.get("q") or [""])[0],
                    _integer(query, "offset", 0, 0, 1_000_000),
                    (query.get("emotion") or [""])[0],
                    (query.get("sort") or ["time"])[0],
                )
            )
        if path == "/api/status":
            return json_response(status.load(self.home, self.profile, self.paths))
        if path == "/api/notes-to-team":
            return json_response(self._load_notes_to_team())
        if path == "/api/model-route":
            return json_response(model_route.load(self.home, self.profile))
        return json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)
