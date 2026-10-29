from __future__ import annotations

import json
from pathlib import Path

from tools.viewer.viewer_app.api import ReadOnlyApi
from tools.viewer.viewer_app.sources import status


def decode(response: tuple[int, str, bytes]) -> dict:
    assert response[1].startswith("application/json")
    return json.loads(response[2])


def test_api_routes_pagination_filter_and_status(profile: Path, monkeypatch) -> None:
    monkeypatch.setattr(status, "unit_state", lambda unit: "active")
    api = ReadOnlyApi("xiaozhi", profile)
    thought_data = decode(api.route("/api/thoughts", {"n": ["1"]}))
    seed_data = decode(
        api.route("/api/seeds", {"limit": ["1"], "emotion": ["坚定"], "sort": ["importance"]})
    )
    status_data = decode(api.route("/api/status", {}))
    assert len(thought_data["items"]) == 1
    assert seed_data["total"] == 1 and seed_data["items"][0]["id"] == "s2"
    assert status_data["seed_total"] == 2
    assert api.route("/missing", {})[0] == 404
