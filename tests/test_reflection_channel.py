from pathlib import Path
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from autonomy.cli_transport import (
    CliGateway, ReflectionCallError, build_reflection_command,
)
from autonomy.config import validate_reflection_profile
from autonomy.policy_reflect import extract_final_json, redact
from autonomy.runner import Runner


def _profile(tmp_path, config, *, plugins=False):
    source = tmp_path / "profiles" / "xiaoai"
    source.mkdir(parents=True)
    reflect = source.parent / "xiaoai-reflect"
    reflect.mkdir()
    (reflect / "config.yaml").write_text(config, encoding="utf-8")
    if plugins:
        p = reflect / "plugins"
        p.mkdir()
        (p / "unsafe.py").write_text("", encoding="utf-8")
    return source


_ZERO_TOOL_CONFIG = "platform_toolsets:\n  cli: []\nagent:\n  disabled_toolsets: [x_search]\n"


def test_reflection_profile_safe_only_when_tools_explicitly_empty(tmp_path):
    source = _profile(tmp_path, _ZERO_TOOL_CONFIG)
    assert validate_reflection_profile(source, "xiaoai", "xiaoai-reflect")[:2] == (True, "")


@pytest.mark.parametrize("config,reason", [
    ("{}\n", "toolsets_not_explicitly_empty"),
    ("platform_toolsets:\n  cli: [web]\n", "toolsets_not_explicitly_empty"),
    (_ZERO_TOOL_CONFIG + "mcp_servers:\n  x: {}\n", "mcp_servers_configured"),
    (_ZERO_TOOL_CONFIG + "context_engine:\n  enabled: true\n", "context_engine_enabled"),
    (_ZERO_TOOL_CONFIG + "plugins: [memory]\n", "plugins_configured"),
])
def test_reflection_profile_rejects_unsafe_config(tmp_path, config, reason):
    source = _profile(tmp_path, config)
    assert validate_reflection_profile(source, "xiaoai", "xiaoai-reflect")[1] == reason


def test_reflection_profile_rejects_plugin_directory(tmp_path):
    source = _profile(tmp_path, _ZERO_TOOL_CONFIG, plugins=True)
    assert validate_reflection_profile(source, "xiaoai", "xiaoai-reflect")[1] == "plugin_directory_not_empty"


def test_reflection_profile_missing_same_and_unreadable_fail_closed(tmp_path):
    source = tmp_path / "profiles" / "xiaoai"
    source.mkdir(parents=True)
    assert validate_reflection_profile(source, "xiaoai", None)[1] == "profile_not_configured"
    assert validate_reflection_profile(source, "xiaoai", "xiaoai")[1] == "profile_not_isolated"
    assert validate_reflection_profile(source, "xiaoai", "missing")[1] == "profile_unreadable"


def _gateway():
    return SimpleNamespace(
        hermes_bin="hermes", model="m", provider="p", run_budget=120,
    )


def test_reflection_command_is_unique_oneshot_and_has_no_toolset_flag():
    first = build_reflection_command(_gateway(), "run-1", "xiaoai-reflect", "/tmp/prompt")
    second = build_reflection_command(_gateway(), "run-1", "xiaoai-reflect", "/tmp/prompt")
    assert first != second
    assert first[:4] == ["hermes", "-p", "xiaoai-reflect", "chat"]
    assert "--no-save" in first and "--oneshot" in first and "--quiet" in first
    assert "--prompt-file" not in first and first[-1] == "/tmp/prompt"
    assert "-t" not in first and "--toolsets" not in first
    supported = {line.strip() for line in (Path(__file__).parent / "fixtures" / "hermes_chat_supported_options.txt").read_text().splitlines() if line.startswith("-")}
    assert {arg for arg in first if arg.startswith("-")} <= supported
    sid = first[first.index("--session-id") + 1]
    assert sid.startswith("autonomy-reflect-run-1-")


class _FakePopen:
    def __init__(self, cmd, **kwargs):
        self.cmd, self.kwargs, self.returncode, self.pid = cmd, kwargs, 0, 123
    def communicate(self, timeout=None):
        return '{"ok":true}\n', ""
    def poll(self):
        return self.returncode


def test_run_reflection_uses_fake_subprocess(monkeypatch):
    gateway = CliGateway(profile="xiaoai", model="m", provider="p", run_budget=10, hermes_bin="hermes")
    seen = {}
    def factory(cmd, **kwargs):
        seen.update(cmd=cmd, kwargs=kwargs)
        return _FakePopen(cmd, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", factory)
    result = gateway.run_reflection("prompt with 'quotes'\nand lines", "r", reflection_profile="xiaoai-reflect")
    assert result["text"] == '{"ok":true}'
    assert seen["cmd"][-1] == "prompt with 'quotes'\nand lines"
    assert "--prompt-file" not in seen["cmd"]


def test_run_reflection_classifies_timeout_and_kills_group(monkeypatch):
    gateway = CliGateway(profile="xiaoai", model="m", provider="p", run_budget=10, hermes_bin="hermes")
    class Timed(_FakePopen):
        calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("x", 1)
            self.returncode = -9
            return "", ""
    killed = []
    monkeypatch.setattr(subprocess, "Popen", Timed)
    monkeypatch.setattr(CliGateway, "_kill_process_group", lambda self, proc: killed.append(proc.pid))
    with pytest.raises(ReflectionCallError, match="timed out") as exc:
        gateway.run_reflection("p", "r", reflection_profile="xiaoai-reflect")
    assert exc.value.reason == "reflection_timeout" and killed == [123]


def test_extract_final_json_ignores_ansi_fences_and_earlier_objects():
    raw = '\x1b[32mprogress {"noise":1}\x1b[0m\r\n```json\n' \
          '{"condition":"c","action":"a","tool_family":"files"}\n```'
    assert extract_final_json(raw) == {
        "condition": "c", "action": "a", "tool_family": "files"
    }
    assert extract_final_json('{"condition":"c"}') is None
    assert extract_final_json("not json") is None


class _Counter:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.consumed = self.failed = self.succeeded = 0

    def allow(self):
        return self.allowed

    def consume(self):
        self.consumed += 1

    def failure(self):
        self.failed += 1

    def success(self):
        self.succeeded += 1


def test_runner_reflection_call_consumes_budget_and_updates_circuit(monkeypatch):
    monkeypatch.setattr("autonomy.runner.validate_reflection_profile", lambda *a: (True, "", Path("reflect")))
    runner = Runner.__new__(Runner)
    runner.hermes_home = Path("source")
    runner._profile_name = lambda: "xiaoai"
    runner.reflection_profile_safe = True
    runner.reflection_profile = "xiaoai-reflect"
    runner.budget = _Counter()
    runner.circuit = _Counter()
    runner.gateway = SimpleNamespace(
        run_reflection=lambda prompt, run, reflection_profile: {"text": "answer", "usage": {"calls": 1}}
    )
    runner._last_reflection_usage = {}
    assert runner._policy_llm_call("run")("prompt") == "answer"
    assert runner.budget.consumed == 1 and runner.circuit.succeeded == 1
