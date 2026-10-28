import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "plugins"))

from autonomy.transcript import extract_run_transcript


SESSIONS_SQL = """
CREATE TABLE sessions (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, session_key TEXT, workspace_path TEXT,
 workspace_key TEXT, started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
 message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
 model TEXT, provider TEXT, title TEXT, metadata TEXT, created_at REAL NOT NULL,
 updated_at REAL NOT NULL, input_tokens INTEGER DEFAULT 0,
 output_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
 usage_json TEXT, is_archived INTEGER DEFAULT 0, archived_at REAL
)
"""
MESSAGES_SQL = """
CREATE TABLE messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
 content TEXT, tool_calls TEXT, tool_call_id TEXT, name TEXT, token_count INTEGER,
 timestamp REAL NOT NULL, model TEXT, provider TEXT, finish_reason TEXT,
 reasoning TEXT, reasoning_content TEXT, reasoning_signature TEXT,
 tool_name TEXT, tool_input TEXT, tool_context TEXT, tool_result_summary TEXT,
 FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
)
"""


def make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SESSIONS_SQL + ";" + MESSAGES_SQL)
    return conn


def add_session(conn, session_id="s1", started=101.0, ended=109.0, messages=4, tools=1):
    conn.execute(
        "INSERT INTO sessions (id,source,started_at,ended_at,message_count,"
        "tool_call_count,title,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, "cli", started, ended, messages, tools, "autonomy", started, ended),
    )


def add_message(conn, session_id, role, content, ts, **values):
    columns = ["session_id", "role", "content", "timestamp"] + list(values)
    params = [session_id, role, content, ts] + list(values.values())
    conn.execute(
        "INSERT INTO messages (%s) VALUES (%s)"
        % (",".join(columns), ",".join("?" for _ in params)),
        params,
    )


def test_extracts_assistant_text_and_tool_call_result(tmp_path):
    db = tmp_path / "state.db"
    conn = make_db(db)
    add_session(conn)
    add_message(conn, "s1", "user", "think", 101.1)
    calls = [{"id": "call-1", "type": "function", "function": {
        "name": "read", "arguments": json.dumps({"path": "记忆.txt"}, ensure_ascii=False)
    }}]
    add_message(conn, "s1", "assistant", "先查看记忆。", 102.0, tool_calls=json.dumps(calls))
    add_message(conn, "s1", "tool", "中文结果", 103.0, tool_call_id="call-1", tool_name="read")
    add_message(conn, "s1", "assistant", "已经完成。", 104.0)
    conn.commit()
    conn.close()

    result = extract_run_transcript(db, 100, 110)
    assert result["session_id"] == "s1"
    assert result["message_count"] == 4
    assert result["tool_call_count"] == 1
    assert result["assistant_texts"] == ["先查看记忆。", "已经完成。"]
    assert result["tool_calls"] == [{
        "name": "read", "args_preview": '{"path":"记忆.txt"}',
        "result_preview": "中文结果",
    }]
    assert "【助手】先查看记忆。" in result["text"]
    assert "【工具】read" in result["text"]


def test_selects_session_started_nearest_run_start(tmp_path):
    db = tmp_path / "state.db"
    conn = make_db(db)
    add_session(conn, "unrelated", 105, 109, 1, 0)
    add_session(conn, "target", 101, 108, 1, 0)
    add_message(conn, "unrelated", "assistant", "wrong", 106)
    add_message(conn, "target", "assistant", "right", 102)
    conn.commit()
    conn.close()

    result = extract_run_transcript(db, 100, 110)
    assert result["session_id"] == "target"
    assert result["assistant_texts"] == ["right"]


def test_redacts_secret_keys_assignments_and_sk_tokens(tmp_path):
    db = tmp_path / "state.db"
    conn = make_db(db)
    add_session(conn, messages=3)
    calls = [{"id": "c", "function": {"name": "exec", "arguments": json.dumps({
        "api_key": "fakekey", "command": "password=fakepass sk-abcdefghijkl"
    })}}]
    add_message(conn, "s1", "assistant", "token=faketoken", 102, tool_calls=json.dumps(calls))
    add_message(conn, "s1", "tool", "secret: fakesecret", 103, tool_call_id="c", tool_name="exec")
    conn.commit()
    conn.close()

    result = extract_run_transcript(db, 100, 110)
    rendered = json.dumps(result, ensure_ascii=False)
    for secret in ("fakekey", "fakepass", "sk-abcdefghijkl", "faketoken", "fakesecret"):
        assert secret not in rendered
    assert "[REDACTED]" in rendered


def test_preview_and_whole_transcript_truncation_are_marked(tmp_path):
    db = tmp_path / "state.db"
    conn = make_db(db)
    add_session(conn, messages=2)
    calls = [{"id": "c", "function": {"name": "write", "arguments": "x" * 80}}]
    add_message(conn, "s1", "assistant", "中文" * 80, 102, tool_calls=json.dumps(calls))
    add_message(conn, "s1", "tool", "y" * 80, 103, tool_call_id="c", tool_name="write")
    conn.commit()
    conn.close()

    result = extract_run_transcript(
        db, 100, 110, max_chars=80, tool_arg_chars=30, tool_result_chars=30
    )
    assert result["truncated"] is True
    assert len(result["text"]) <= 80
    assert result["text"].endswith("…[transcript truncated]")
    assert result["tool_calls"][0]["args_preview"].endswith("…[truncated]")
    assert result["tool_calls"][0]["result_preview"].endswith("…[truncated]")


def test_missing_database_and_schema_degrade_without_raising(tmp_path):
    missing = extract_run_transcript(tmp_path / "missing.db", 1, 2)
    assert missing["text"] == ""
    assert "not found" in missing["error"]

    broken_db = tmp_path / "broken.db"
    sqlite3.connect(broken_db).execute("CREATE TABLE other (id INTEGER)").connection.close()
    broken = extract_run_transcript(broken_db, 1, 2)
    assert broken["text"] == ""
    assert "schema" in broken["error"]


def test_no_matching_session_degrades_without_raising(tmp_path):
    db = tmp_path / "state.db"
    conn = make_db(db)
    add_session(conn, started=10, ended=20)
    conn.commit()
    conn.close()
    result = extract_run_transcript(db, 100, 110)
    assert result == {"error": "no session found in run time window", "text": ""}
