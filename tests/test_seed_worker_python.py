import importlib.util
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace


PLUGIN = Path(__file__).resolve().parents[1] / 'plugins' / 'seed_memory' / '__init__.py'


def _module(monkeypatch):
    agent = types.ModuleType('agent')
    memory_provider = types.ModuleType('agent.memory_provider')
    memory_provider.MemoryProvider = type('MemoryProvider', (), {})
    memory_provider.RecallStatus = type('RecallStatus', (), {})
    monkeypatch.setitem(sys.modules, 'agent', agent)
    monkeypatch.setitem(sys.modules, 'agent.memory_provider', memory_provider)
    spec = importlib.util.spec_from_file_location('seed_worker_python_test', PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_memory_worker_uses_system_python(monkeypatch, tmp_path):
    seed_memory = _module(monkeypatch)
    queue_dir = tmp_path / 'queue'
    queue_dir.mkdir()
    (queue_dir / 'session_test.json').write_text('{}', encoding='utf-8')
    hermes_home = tmp_path / 'xiaoling'
    hermes_home.mkdir()
    provider = seed_memory.SeedMemoryProvider.__new__(seed_memory.SeedMemoryProvider)
    provider._queue_dir = queue_dir
    provider._hermes_home = hermes_home
    provider._worker_proc = None
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(seed_memory, '_worker_python_executable', lambda: '/usr/bin/python3')

    provider._launch_worker_if_needed()

    command = commands[0]
    worker_index = command.index('/usr/bin/python3')
    assert command[0] == 'systemd-run'
    assert command[worker_index + 1].endswith('seed_memory\\worker.py') or command[worker_index + 1].endswith('seed_memory/worker.py')
    assert command[worker_index + 2] == '--once'
