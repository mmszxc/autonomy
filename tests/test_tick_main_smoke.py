"""End-to-end smoke test for ``tick.main`` with a local fake gateway."""
from __future__ import annotations

import hashlib
import sqlite3

from plugins.autonomy import cli_transport, tick


def test_tick_main_smoke_with_fake_gateway(monkeypatch, tmp_path):
    """One complete tick must reach the fake LLM and finish successfully."""
    data_dir = tmp_path / "data"
    thought_log = data_dir / "thoughts.log"
    raw_cfg = {
        "model": {"default": "fake-model"},
        "autonomy": {
            "model": None,
            "transport": "http",
            "data_dir": str(data_dir),
            "thought_log": str(thought_log),
            "quiet_s": 0,
            "limits": {
                "daily_tokens": 10_000,
                "daily_calls": 10,
                "daily_prompt_tokens": 10_000,
                "circuit_threshold": 3,
                "circuit_cooldown_s": 60,
                "memory_max": "1G",
            },
        },
    }
    called = {"count": 0}

    class FakeGateway:
        def __init__(self, *args, **kwargs):
            pass

        def chat(self, goal, run_id, session_id, messages, prompt):
            called["count"] += 1
            assert "独处" in prompt
            return "fake-session", "smoke ok", {
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
            }

        def end(self, session_id):
            assert session_id == "fake-session"

    monkeypatch.setattr(tick, "load_raw_config", lambda *_a, **_kw: raw_cfg)
    monkeypatch.setattr(tick, "GatewayClient", FakeGateway)
    monkeypatch.setenv("AUTONOMY_MIN_QUIET_START", "0")
    monkeypatch.delenv("AUTONOMY_ACTIVE_HOURS", raising=False)

    result = tick.main([], hermes_home=str(tmp_path / "hermes"))

    assert result == 0
    assert called["count"] == 1
    assert thought_log.exists()
    assert "smoke ok" in thought_log.read_text(encoding="utf-8")


def test_tick_main_cli_action_failure_returns_one_and_persists_real_db(monkeypatch, tmp_path):
    """The pre-P0 contract propagates a terminal CLI failure as process exit 1."""
    data_dir = tmp_path / "data"
    db_path = data_dir / "autonomy.db"
    stdout = b"session_id=failing-session"
    stderr = b"action failed"
    raw_cfg = {
        "model": {"default": "fake-model"},
        "autonomy": {
            "transport": "cli",
            "data_dir": str(data_dir),
            "paths": {"db_path": str(db_path)},
            "p0": {"mode": "observe_only"},
            "limits": {
                "daily_tokens": 10_000,
                "daily_calls": 10,
                "daily_prompt_tokens": 10_000,
                "max_turns": 3,
                "run_max_seconds": 30,
            },
        },
    }

    class FailedCliGateway:
        def __init__(self, *args, **kwargs):
            pass

        def start_run(self, *_args, **_kwargs):
            return {"run_id": "failed-action"}

        def get_run(self, _run_id):
            return {
                "status": "failed",
                "session_id": "failing-session",
                "output": stdout.decode(),
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                "evidence": {
                    "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                    "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                    "stdout_bytes": len(stdout),
                    "stderr_bytes": len(stderr),
                    "stdout_head": stdout.decode(),
                    "stdout_tail": stdout.decode(),
                    "stderr_head": stderr.decode(),
                    "stderr_tail": stderr.decode(),
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "exit_code": 1,
                    "signal_number": None,
                    "timed_out": False,
                    "oom": False,
                    "buffer_path": None,
                },
            }

        def stop_run(self, _run_id):
            return None

    monkeypatch.setattr(tick, "load_raw_config", lambda *_a, **_kw: raw_cfg)
    monkeypatch.setattr(cli_transport, "CliGateway", FailedCliGateway)
    monkeypatch.setenv("AUTONOMY_DB_PATH", str(db_path))
    monkeypatch.setenv("AUTONOMY_MIN_QUIET_START", "0")
    monkeypatch.delenv("AUTONOMY_ACTIVE_HOURS", raising=False)

    exit_code = tick.main([], hermes_home=str(tmp_path / "hermes"))

    assert exit_code == 1
    with sqlite3.connect(db_path) as conn:
        receipt = conn.execute(
            "select id,status from receipts where kind='action_failed'"
        ).fetchone()
        assert receipt is not None
        assert receipt[1] == "verified"
        assert conn.execute(
            "select count(*) from evidence where receipt_id=?", (receipt[0],)
        ).fetchone()[0] == 2
        assert conn.execute(
            "select count(*) from budget_ledger where bucket='normal'"
        ).fetchone()[0] == 1
