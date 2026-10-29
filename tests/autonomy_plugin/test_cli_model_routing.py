import io

import pytest
import yaml

from plugins.autonomy import cli_transport, tick
from plugins.autonomy.config import _resolve_provider_details, resolve_autonomy_model


class _Proc:
    def __init__(self, cmd, **_kwargs):
        self.cmd = cmd
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


def test_cli_command_includes_explicit_model_and_provider(monkeypatch):
    seen = {}

    def popen(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Proc(cmd, **kwargs)

    monkeypatch.setattr(cli_transport.subprocess, "Popen", popen)
    gateway = cli_transport.CliGateway(profile="example", model="worker-model", provider="worker-provider")
    gateway.start_run("goal", "run-1", "prompt")
    assert seen["cmd"][:5] == ["hermes", "-p", "example", "chat", "--model"]
    assert seen["cmd"][5:9] == ["worker-model", "--provider", "worker-provider", "--query-file"]


def test_cli_command_inherit_keeps_original_shape(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli_transport.subprocess, "Popen", lambda cmd, **kwargs: seen.setdefault("proc", _Proc(cmd, **kwargs)))
    gateway = cli_transport.CliGateway(profile="example")
    gateway.start_run("goal", "run-2", "prompt")
    assert seen["proc"].cmd[:4] == ["hermes", "-p", "example", "chat"]
    assert "--model" not in seen["proc"].cmd
    assert "--provider" not in seen["proc"].cmd


def _run_tick(monkeypatch, tmp_path, configured_model):
    profile = tmp_path / "example"
    profile.mkdir()
    (profile / "config.yaml").write_text(yaml.safe_dump({
        "model": {"default": "main-model", "provider": "main-provider"},
        "autonomy": {"transport": "cli", "model": configured_model, "data_dir": str(profile / "data")},
    }), encoding="utf-8")
    seen = {}

    class FakeCli:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    class FakeRunner:
        def __init__(self, *args, **kwargs):
            pass

        def tick(self):
            return {"status": "ok"}

    monkeypatch.setattr(cli_transport, "CliGateway", FakeCli)
    monkeypatch.setattr(tick, "Runner", FakeRunner)
    assert tick.main(["--hermes-home", str(profile)], hermes_home=str(profile)) == 0
    return seen


@pytest.mark.parametrize(
    ("configured", "model", "provider"),
    [
        (None, None, None),
        ("worker-model", "worker-model", None),
        ({"model": "worker-model", "provider": "worker-provider"}, "worker-model", "worker-provider"),
    ],
)
def test_tick_passes_only_explicit_model_routing(monkeypatch, tmp_path, configured, model, provider):
    seen = _run_tick(monkeypatch, tmp_path, configured)
    assert seen["model"] == model
    assert seen["provider"] == provider


def test_custom_providers_list_resolves_own_key_env():
    raw = {
        "model": {"default": "main-model", "provider": "main-provider"},
        "custom_providers": [{
            "name": "worker-provider",
            "base_url": "https://example.invalid/v1",
            "key_env": "HERMES_EXAMPLE_KEY_WORKER",
            "model": "worker-model",
        }],
        "autonomy": {"model": {"model": "worker-model", "provider": "worker-provider"}},
    }
    assert _resolve_provider_details(raw, "worker-provider") == (
        "worker-provider", "https://example.invalid/v1", "HERMES_EXAMPLE_KEY_WORKER"
    )
    resolved = resolve_autonomy_model(raw)
    assert resolved["provider"] == "worker-provider"
    assert resolved["api_key_env"] == "HERMES_EXAMPLE_KEY_WORKER"
