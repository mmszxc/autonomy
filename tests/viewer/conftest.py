from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    home = tmp_path / "profile"
    autonomy = home / "autonomy_data"
    autonomy.mkdir(parents=True)
    (autonomy / "thoughts.log.1").write_text(
        "=== [2026-10-29T08:00:00Z] run_id=old status=ok mode=parallel model=test "
        "(prompt=10 completion=20 total=30) ===\n旧轮次 <script>alert(1)</script> sk-viewertest0000\n",
        encoding="utf-8",
    )
    (autonomy / "thoughts.log").write_text(
        "=== [2026-10-29T09:00:00Z] run_id=new status=partial mode=parallel model=test "
        "(prompt=100 completion=200 total=300) ===\n【本轮预取记忆】prefetch count=5\n"
        "【遥测】elapsed=12.5\ntool seed_search api_key=viewer-test-value\n"
        "结论：继续只读验证 Bearer viewer-test-value\n",
        encoding="utf-8",
    )
    seed_db = home / "seed_data" / "seeds" / "seeds.db"
    seed_db.parent.mkdir(parents=True)
    with sqlite3.connect(seed_db) as connection:
        connection.execute(
            "CREATE TABLE seeds(id TEXT PRIMARY KEY,timestamp TEXT,summary TEXT,emotion_primary TEXT,"
            "emotion_intensity REAL,importance REAL,caused_by TEXT,seeds_planted TEXT)"
        )
        connection.executemany(
            "INSERT INTO seeds VALUES (?,?,?,?,?,?,?,?)",
            [
                ("s2", "2026-10-29T09:00:00Z", "后来决定调用工具", "坚定", .8, .9, '["s1"]', "[]"),
                ("s1", "2026-10-29T08:00:00Z", "<script>alert(1)</script>", "好奇", .7, .75, "[]", "[]"),
            ],
        )
    state_db = autonomy / "autonomy.db"
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    with sqlite3.connect(state_db) as connection:
        connection.execute("CREATE TABLE usage(day TEXT PRIMARY KEY,calls INTEGER,tokens INTEGER,prompt INTEGER)")
        connection.execute("INSERT INTO usage VALUES (?,?,?,?)", (today, 7, 1234, 900))
        connection.execute("CREATE TABLE circuit(id INTEGER PRIMARY KEY,failures INTEGER,state TEXT,blocked INTEGER)")
        connection.execute("INSERT INTO circuit VALUES (1,2,'open',0)")
    queue = home / "seed_data" / "seed_queue"
    queue.mkdir(parents=True)
    (queue / "one.json").write_text("{}", encoding="utf-8")
    (home / "config.yaml").write_text(
        "autonomy:\n  limits:\n    daily_tokens: 100000000\n    daily_calls: 500\n"
        "    daily_prompt_tokens: 100000000\n",
        encoding="utf-8",
    )
    return home
