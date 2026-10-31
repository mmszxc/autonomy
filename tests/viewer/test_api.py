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
    assert api.route("/api/model-route", {})[0] == 200
    assert api.route("/missing", {})[0] == 404


def test_notes_to_team_missing_is_hidden(profile: Path) -> None:
    api = ReadOnlyApi("demo", profile)
    data = decode(api.route("/api/notes-to-team", {}))
    assert data == {"available": False, "content": "", "truncated": False}
    assert not (profile / "autonomy_data" / "notes_to_team.md").exists()


def test_notes_to_team_is_read_only_redacted_and_escaped(profile: Path) -> None:
    path = profile / "autonomy_data" / "notes_to_team.md"
    original = "请保留 <script>x</script> api_key=viewer-test-value"
    path.write_text(original, encoding="utf-8")
    data = decode(ReadOnlyApi("demo", profile).route("/api/notes-to-team", {}))
    assert data["available"] and not data["truncated"]
    assert "&lt;script&gt;" in data["content"]
    assert "api_key=[已打码]" in data["content"]
    assert "viewer-test-value" not in data["content"]
    assert path.read_text(encoding="utf-8") == original


def test_notes_to_team_returns_only_tail_1kb(profile: Path) -> None:
    path = profile / "autonomy_data" / "notes_to_team.md"
    path.write_bytes(b"PREFIX-SECRET\n" + b"x" * 1100 + "末尾留言".encode("utf-8"))
    data = decode(ReadOnlyApi("demo", profile).route("/api/notes-to-team", {}))
    assert data["available"] and data["truncated"]
    assert "PREFIX-SECRET" not in data["content"]
    assert "末尾留言" in data["content"]
