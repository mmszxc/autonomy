"""End-to-end smoke test for ``tick.main`` with a local fake gateway."""
from __future__ import annotations

from plugins.autonomy import tick


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
