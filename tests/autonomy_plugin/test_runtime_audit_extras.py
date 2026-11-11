"""Transport stop escalation and status slash command regressions."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins'))
from autonomy import cli_transport
from autonomy import cli


def test_stop_run_escalates_to_process_group_sigkill(monkeypatch):
    class Proc:
        pid = 56789
        returncode = -9
        def __init__(self): self.signals = []
        def poll(self): return None
        def wait(self, timeout):
            if len(self.signals) == 1:
                raise subprocess.TimeoutExpired('hermes', timeout)
            return self.returncode
    proc = Proc()
    transport = object.__new__(cli_transport.CliGateway)
    transport.runs = {'rid': (proc, '', '')}
    transport.stopped_runs = set()
    transport.get_run = lambda rid: {'status': 'stopped', 'run_id': rid}
    monkeypatch.setattr(cli_transport, 'os', SimpleNamespace(
        name='posix', killpg=lambda pid, sig: proc.signals.append(sig)
    ))
    monkeypatch.setattr(cli_transport, 'signal', SimpleNamespace(SIGTERM=15, SIGKILL=9))
    result = transport.stop_run('rid')
    assert proc.signals == [15, 9]
    assert result['status'] == 'stopped'


def test_slash_status_uses_cmd_status_usage_and_breaker(monkeypatch):
    import autonomy
    monkeypatch.setattr(autonomy, 'cmd_status', lambda: {
        'state': 'running', 'mode': 'parallel', 'autonomy_model': 'mini',
        'main_model': 'main', 'usage': {'tokens': 7, 'tokens_limit': 42},
        'breaker': {'open': False, 'failures': 1},
    })
    text = autonomy._handle_slash_command('status')
    assert '7/42' in text
    assert 'closed' in text
