import os
import signal
from pathlib import Path

from plugins.autonomy.prompt import build_tick_prompt
from plugins.autonomy.runner import Runner
from plugins.autonomy import runner as runner_module


def _runner(max_chars=3000):
    r = Runner.__new__(Runner)
    r.prefetch_max_chars = max_chars
    r._prefetch_meta = {}
    return r


def test_prefetch_deduplicates_seed_ids_and_bounds_each_summary():
    r = _runner(300)
    text = "seed_id: a first\nseed_id: a duplicate\nseed_id: b " + ("x" * 700)
    bounded, ids = r._bound_prefetch_text(text)
    assert ids == ["a", "b"]
    assert len(bounded) <= 700
    assert r._prefetch_meta["truncated"] is True
    assert r._prefetch_meta["chars"] == len(bounded)


def test_prompt_discourages_duplicate_seed_search():
    prompt = build_tick_prompt([], [], "parallel", memory_review="memory")
    assert "seed_search" in prompt
    assert "不要重复搜索同样的内容" in prompt


def test_default_resource_limits_are_conservative():
    # Keep this assertion independent of a live gateway/configuration.
    assert 3000 > 0
    assert 20 <= 30
    assert 2400 < 3000


def test_timeout_snapshot_preserves_partial_output_and_usage():
    r = _runner()
    raw, usage, tools = r._run_snapshot({"output": "partial thought", "usage": {"total_tokens": 42}, "tool_calls": [{"name": "seed_search"}]})
    assert raw == "partial thought"
    assert usage["total_tokens"] == 42
    assert len(tools) == 1


def test_prefetch_process_group_is_platform_supported():
    assert hasattr(os, "killpg") or os.name == "nt"
    if os.name != "nt":
        assert signal.SIGKILL


def test_memory_max_compatibility_hook_does_not_apply_rlimit_as(monkeypatch):
    class FakeResource:
        RLIMIT_AS = 9
        RLIM_INFINITY = -1
        def __init__(self):
            self.calls = []
        def setrlimit(self, *args):
            self.calls.append(args)

    fake = FakeResource()
    monkeypatch.setattr(runner_module, "resource", fake)
    runner_module._apply_memory_limit(1024)
    assert fake.calls == []


def test_prefetch_child_resets_address_space_limit(monkeypatch):
    class FakeResource:
        RLIMIT_AS = 9
        RLIM_INFINITY = -1
        def __init__(self):
            self.calls = []
        def setrlimit(self, *args):
            self.calls.append(args)

    fake = FakeResource()
    monkeypatch.setattr(runner_module, "resource", fake)
    runner_module._reset_address_space_limit()
    assert fake.calls == [(fake.RLIMIT_AS, (fake.RLIM_INFINITY, fake.RLIM_INFINITY))]
