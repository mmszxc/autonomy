"""Regression tests for queue importance propagation in seed_memory."""

import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "seed_memory" / "__init__.py"


def _module(monkeypatch):
    agent = types.ModuleType("agent")
    memory_provider = types.ModuleType("agent.memory_provider")
    memory_provider.MemoryProvider = type("MemoryProvider", (), {})
    memory_provider.RecallStatus = type("RecallStatus", (), {})
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", memory_provider)
    spec = importlib.util.spec_from_file_location("seed_queue_importance_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _provider(module, tmp_path, importance_marker):
    queue = tmp_path / "queue"
    queue.mkdir()
    payload = {
        "session_id": "importance-regression",
        "status": "ended",
        "created_at": time.time(),
        "updated_at": time.time(),
        "messages": [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
        ],
    }
    if importance_marker is not _MISSING:
        payload["importance_override"] = importance_marker
    path = queue / "session_importance.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    class Store:
        def list_recent_seeds(self, limit=15):
            return []

    provider = object.__new__(module.SeedMemoryProvider)
    provider._queue_dir = queue
    provider._store = Store()
    provider._tagger = types.SimpleNamespace(LAST_RAW="")
    provider._writes_enabled = lambda: True
    provider._user_active = lambda: False
    provider._normalise_messages = lambda messages: messages or []
    provider._chunk_messages = lambda messages, _size: [messages]
    provider._queue_max_age_days = 30
    provider._config = {
        "max_files": 1,
        "max_duration_s": 30,
        "max_retries": 3,
        "retry_backoff_s": 1,
    }
    return provider, path


_MISSING = object()


@pytest.mark.parametrize(
    ("importance_marker", "expected"),
    [(0.73, 0.73), (_MISSING, None)],
    ids=["with-importance-override", "without-importance-override"],
)
def test_drain_truncation_retry_reads_importance_from_payload(
    monkeypatch, tmp_path, importance_marker, expected
):
    module = _module(monkeypatch)
    provider, path = _provider(module, tmp_path, importance_marker)
    calls = []

    def extract(_messages, _session_id, _recent_seeds, importance_override=None):
        calls.append(importance_override)
        return []

    provider._extract_seeds_with_retry = extract
    hermes = types.ModuleType("hermes")
    emotion = types.ModuleType("hermes.emotion")
    tagger = types.ModuleType("hermes.emotion.tagger")
    tagger.LAST_FINISH_REASON = "length"
    tagger.LAST_TRUNCATED = True
    emotion.tagger = tagger
    hermes.emotion = emotion
    monkeypatch.setitem(sys.modules, "hermes", hermes)
    monkeypatch.setitem(sys.modules, "hermes.emotion", emotion)
    monkeypatch.setitem(sys.modules, "hermes.emotion.tagger", tagger)

    provider._drain_queue()

    assert calls == [None, expected]
    queued = json.loads(path.read_text(encoding="utf-8"))
    assert queued["last_error"] == "parse_failed:truncated"


def test_tagger_transient_retry_preserves_importance_override(monkeypatch, tmp_path):
    module = _module(monkeypatch)
    provider = object.__new__(module.SeedMemoryProvider)
    seen = []

    class Tagger:
        def extract_seeds(self, *args, **kwargs):
            seen.append(kwargs["importance_override"])
            if len(seen) == 1:
                raise TimeoutError("tagger request timed out")
            return ["seed"]

    provider._tagger = Tagger()
    provider._llm_call = lambda _prompt: "[]"
    provider._config = {"tagger_retries": 1}
    monkeypatch.setattr(module.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(module.random, "uniform", lambda _a, _b: 0.0)

    result = provider._extract_seeds_with_retry(
        [], "importance-regression", [], importance_override=0.81
    )

    assert result == ["seed"]
    assert seen == [0.81, 0.81]
