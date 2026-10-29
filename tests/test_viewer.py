from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tools.viewer import viewer


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_seed_db(path: Path) -> None:
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE seeds (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                summary TEXT NOT NULL,
                emotion_primary TEXT NOT NULL,
                emotion_intensity REAL NOT NULL,
                importance REAL NOT NULL,
                caused_by TEXT,
                seeds_planted TEXT
            )"""
        )
        connection.executemany(
            "INSERT INTO seeds VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    "s2",
                    "2026-10-29T09:00:00Z",
                    "后来决定调用工具 Bearer top-secret-token",
                    "坚定",
                    0.8,
                    0.9,
                    '["s1"]',
                    '["继续验证"]',
                ),
                (
                    "s1",
                    "2026-10-29T08:00:00Z",
                    "<script>alert(1)</script> 初始观察 api_key=abcd1234",
                    "好奇",
                    0.7,
                    0.75,
                    "[]",
                    "[]",
                ),
            ],
        )


def _make_state_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE usage(day TEXT PRIMARY KEY,calls INTEGER,tokens INTEGER,prompt INTEGER)")
        connection.execute("INSERT INTO usage VALUES (?,?,?,?)", (today, 7, 1234, 900))
        connection.execute(
            "CREATE TABLE circuit(id INTEGER PRIMARY KEY,failures INTEGER,state TEXT,blocked INTEGER)"
        )
        connection.execute("INSERT INTO circuit VALUES (1,2,'open',0)")


def _make_profile(tmp_path: Path) -> Path:
    home = tmp_path / "profile"
    autonomy = home / "autonomy_data"
    autonomy.mkdir(parents=True)
    (autonomy / "thoughts.log.1").write_text(
        "=== [2026-10-29T08:00:00Z] run_id=old status=ok mode=parallel model=test "
        "(prompt=10 completion=20 total=30) ===\n"
        "旧轮次 <script>alert(1)</script> sk-abcdefghijk\n",
        encoding="utf-8",
    )
    (autonomy / "thoughts.log").write_text(
        "=== [2026-10-29T09:00:00Z] run_id=new status=partial mode=parallel model=test "
        "(prompt=100 completion=200 total=300) ===\n"
        "【本轮预取记忆】prefetch count=5\n"
        "【遥测】elapsed=12.5\n"
        "tool seed_search api_key=abcd1234\n"
        "结论：继续只读验证 Bearer abcdefghijkl\n",
        encoding="utf-8",
    )
    _make_seed_db(home / "seed_data" / "seeds" / "seeds.db")
    _make_state_db(autonomy / "autonomy.db")
    queue = home / "seed_data" / "seed_queue"
    queue.mkdir(parents=True)
    (queue / "one.json").write_text("{}", encoding="utf-8")
    (queue / "two.json").write_text("{}", encoding="utf-8")
    (home / "config.yaml").write_text(
        "autonomy:\n  limits:\n    daily_tokens: 100000000\n    daily_calls: 500\n"
        "    daily_prompt_tokens: 100000000\n",
        encoding="utf-8",
    )
    return home


def test_thought_parser_reads_rotations_redacts_and_escapes(tmp_path: Path) -> None:
    home = _make_profile(tmp_path)
    items = viewer.load_thoughts(home / "autonomy_data" / "thoughts.log", 10)

    assert [item["run_id"] for item in items] == ["new", "old"]
    newest = items[0]
    assert newest["elapsed"] == 12.5
    assert newest["prefetch_count"] == 5
    assert newest["tokens"] == {"prompt": 100, "completion": 200, "total": 300}
    assert "api_key=[已打码]" in newest["body"]
    assert "Bearer ***" in newest["body"]
    assert newest["tools"]
    assert "&lt;script&gt;" in items[1]["body"]
    assert "sk-***" in items[1]["body"]
    assert "<script>" not in items[1]["body"]


def test_seed_search_pagination_xss_and_database_remain_read_only(tmp_path: Path) -> None:
    home = _make_profile(tmp_path)
    database = home / "seed_data" / "seeds" / "seeds.db"
    before = _digest(database)
    before_files = sorted(path.relative_to(home) for path in home.rglob("*"))

    first = viewer.load_seeds(database, limit=1, query="", offset=0)
    second = viewer.load_seeds(database, limit=1, query="初始", offset=1)
    injection = viewer.load_seeds(database, limit=20, query="%' OR 1=1 --", offset=0)

    assert first["total"] == 2
    assert first["items"][0]["id"] == "s2"
    assert "Bearer ***" in first["items"][0]["summary"]
    # Search is applied before pagination; this single match is at offset zero.
    assert second["items"] == []
    match = viewer.load_seeds(database, limit=20, query="初始", offset=0)
    assert match["total"] == 1
    assert "&lt;script&gt;" in match["items"][0]["summary"]
    assert "api_key=[已打码]" in match["items"][0]["summary"]
    assert injection["total"] == 0
    assert _digest(database) == before
    assert sorted(path.relative_to(home) for path in home.rglob("*")) == before_files


def test_status_uses_read_only_state_and_reports_timer_budget_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _make_profile(tmp_path)
    state_db = home / "autonomy_data" / "autonomy.db"
    seed_db = home / "seed_data" / "seeds" / "seeds.db"
    before = (_digest(state_db), _digest(seed_db))
    monkeypatch.setattr(viewer, "_timer_status", lambda profile: "active")

    result = viewer.load_status(home, "xiaozhi")

    assert result["timer"] == "active"
    assert result["budget"]["calls"] == {"used": 7, "limit": 500}
    assert result["budget"]["tokens"] == {"used": 1234, "limit": 100_000_000}
    assert result["circuit"] == {"failures": 2, "state": "open", "blocked": False}
    assert result["seed_total"] == 2
    assert result["queue_backlog"] == 2
    assert (_digest(state_db), _digest(seed_db)) == before


def test_http_api_token_and_security_headers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _make_profile(tmp_path)
    monkeypatch.setattr(viewer, "_timer_status", lambda profile: "inactive")
    application = viewer.ViewerApplication("xiaozhi", home, token="viewer-passphrase")
    server = viewer.ThreadingHTTPServer(("127.0.0.1", 0), viewer.make_handler(application))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/api/thoughts?n=1", timeout=3)
        assert error.value.code == 401

        request = urllib.request.Request(base + "/api/thoughts?n=1")
        request.add_header("Authorization", "Bearer viewer-passphrase")
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.load(response)
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert payload["items"][0]["run_id"] == "new"

        with urllib.request.urlopen(base + "/api/seeds?limit=1&token=viewer-passphrase", timeout=3) as response:
            seeds = json.load(response)
        assert seeds["total"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_default_paths_and_cli_are_zero_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    parser = viewer.build_parser()
    args = parser.parse_args(["--profile", "xiaozhi"])
    assert args.host == "0.0.0.0"
    assert args.port == 8765
    assert args.profile == "xiaozhi"
    assert viewer.safe_text('<img src=x onerror="alert(1)">') == "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"
