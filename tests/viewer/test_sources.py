from __future__ import annotations

import hashlib
from pathlib import Path

from tools.viewer.viewer_app.config import resolve_paths
from tools.viewer.viewer_app.sources import seeds, status, thoughts


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_thought_rotations_metadata_and_masking(profile: Path) -> None:
    items = thoughts.load(profile / "autonomy_data" / "thoughts.log", 10)
    assert [item["run_id"] for item in items] == ["new", "old"]
    assert items[0]["elapsed"] == 12.5 and items[0]["prefetch_count"] == 5
    assert "api_key=[已打码]" in items[0]["body"] and "Bearer ***" in items[0]["body"]
    assert "&lt;script&gt;" in items[1]["body"] and "sk-***" in items[1]["body"]


def test_seed_search_filter_sort_and_read_only(profile: Path) -> None:
    database = profile / "seed_data" / "seeds" / "seeds.db"
    before = digest(database)
    result = seeds.load(database, limit=20, emotion="好奇", sort="importance")
    assert result["total"] == 1 and result["items"][0]["id"] == "s1"
    assert "&lt;script&gt;" in result["items"][0]["summary"]
    assert {row["name"] for row in result["emotions"]} == {"坚定", "好奇"}
    assert seeds.load(database, query="%' OR 1=1 --")["total"] == 0
    assert digest(database) == before


def test_runtime_status_is_read_only(profile: Path, monkeypatch) -> None:
    state_db = profile / "autonomy_data" / "autonomy.db"
    seed_db = profile / "seed_data" / "seeds" / "seeds.db"
    before = digest(state_db), digest(seed_db)
    monkeypatch.setattr(status, "unit_state", lambda unit: "active" if unit.endswith("timer") else "activating")
    result = status.load(profile, "xiaozhi")
    assert result["timer"] == "active" and result["runner"] == "activating"
    assert result["budget"]["calls"] == {"used": 7, "limit": 500}
    assert result["circuit"]["state"] == "open" and result["seed_total"] == 2
    assert (digest(state_db), digest(seed_db)) == before


def test_profile_paths_and_queue_counts(profile: Path, monkeypatch) -> None:
    config = profile / "config.yaml"
    config.write_text(
        "autonomy:\n  paths:\n    db_path: %s\n  thought_log: %s\n"
        "memory:\n  seed_memory:\n    queue_dir: %s\n" % (
            (profile / "autonomy_data" / "autonomy.db").as_posix(),
            (profile / "autonomy_data" / "thoughts.log").as_posix(),
            (profile / "seed_data" / "seed_queue").as_posix(),
        ), encoding="utf-8",
    )
    paths = resolve_paths(profile)
    queue = paths["queue"]
    (queue / "worker.log").write_text("diagnostic", encoding="utf-8")
    (queue / "worker.lock").write_text("", encoding="utf-8")
    (queue / "session.json").write_text("{}", encoding="utf-8")
    failed = queue / "_failed"; failed.mkdir()
    (failed / "poison.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(status, "unit_state", lambda unit: "inactive")
    result = status.load(profile, "xiaoai", paths)
    assert result["profile"] == "xiaoai"
    assert result["queue_backlog"] == len(list(queue.glob("*.json"))) == 2
    assert result["queue_failed"] == 1
