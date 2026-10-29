"""JSON API routing over read-only data sources."""

from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from typing import Any

from .sources import seeds, status, thoughts


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
        self.thought_log = home / "autonomy_data" / "thoughts.log"
        self.seed_db = home / "seed_data" / "seeds" / "seeds.db"

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
            return json_response(status.load(self.home, self.profile))
        return json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)
