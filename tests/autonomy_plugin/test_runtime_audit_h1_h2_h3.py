"""Runtime audit regressions: redaction, truthful last_tick, verified auth only."""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins'))
from autonomy import runner
from autonomy.budget import Budget
from autonomy.circuit import Circuit
from autonomy.db import AutonomyDB


def make_runner(tmp_path, gateway=None, budget=None, circuit=None):
    db = AutonomyDB(tmp_path / 'autonomy.db')
    db.create_goal('g', 'goal')
    instance = runner.Runner(
        db, budget or Budget(db, calls=20, tokens=100, prompt=100),
        circuit or Circuit(db), gateway=gateway, mode='parallel',
        lock_path=str(tmp_path / 'lock'), stop_file=str(tmp_path / 'STOP'),
        thought_log_path=str(tmp_path / 'thoughts.log'), hermes_home=str(tmp_path),
    )
    return instance, db


def tick_status(db):
    return db.c.execute('SELECT status FROM last_tick WHERE id=1').fetchone()[0]


def events(db, kind):
    rows = db.c.execute('SELECT exit_reason,summary FROM events WHERE kind=?', (kind,)).fetchall()
    return [tuple(row) for row in rows]


def test_redactor_handles_common_credential_shapes():
    source = ('api_key=firstsecret token: secondsecret secret=thirdsecret '
              'password: fourthsecret Authorization: Bearer fifthsecret')
    redacted = runner._redact_diagnostic(source)
    for secret in ('firstsecret', 'secondsecret', 'thirdsecret', 'fourthsecret', 'fifthsecret'):
        assert secret not in redacted
    assert '<redacted>' in redacted


def test_gateway_failed_event_and_partial_receipt_are_redacted(tmp_path, monkeypatch):
    secret = 'top-secret-gateway-token'
    class Gateway:
        def start_run(self, *_): return {'run_id': 'remote-1'}
        def get_run(self, *_):
            return {'status': 'failed', 'output': 'api_key=' + secret,
                    'total_tokens': 1}
    instance, db = make_runner(tmp_path, Gateway())
    monkeypatch.setattr(instance, '_extract_transcript', lambda *_: {})
    outcome = instance.tick()
    assert outcome['status'] == 'failed', outcome
    assert events(db, 'run_failed')
    assert secret not in str(events(db, 'run_failed'))
    assert secret not in (tmp_path / 'thoughts.log').read_text(encoding='utf-8')
    assert '<redacted>' in str(events(db, 'run_failed'))
    assert '<redacted>' in (tmp_path / 'thoughts.log').read_text(encoding='utf-8')


def test_exception_tick_event_and_partial_are_redacted(tmp_path):
    secret = 'top-secret-exception-token'
    class Gateway:
        def chat(self, *_args, **_kwargs):
            raise RuntimeError('Authorization: Bearer ' + secret)
    instance, db = make_runner(tmp_path, Gateway())
    result = instance.tick()
    assert result['status'] == 'failed'
    assert secret not in str(events(db, 'tick'))
    assert secret not in str(events(db, 'partial'))
    assert '<redacted>' in str(events(db, 'tick'))


def test_busy_does_not_touch_existing_running_tick(tmp_path, monkeypatch):
    instance, db = make_runner(tmp_path)
    db.c.execute('INSERT OR REPLACE INTO last_tick VALUES(1,?,?,?,?)',
                 ('running', 123.0, None, 'first runner'))
    db.c.commit()
    monkeypatch.setattr(instance, '_acquire_lock', lambda _file: False)
    assert instance.tick()['status'] == 'busy'
    assert tuple(db.c.execute('SELECT status,started_at,summary FROM last_tick WHERE id=1').fetchone()) == ('running', 123.0, 'first runner')


def test_stop_and_auth_early_returns_keep_truthful_tick_state(tmp_path):
    instance, db = make_runner(tmp_path)
    (tmp_path / 'STOP').touch()
    assert instance.tick()['status'] == tick_status(db) == 'stopped'
    (tmp_path / 'STOP').unlink()
    (tmp_path / 'lock.auth_failed').write_text('401', encoding='utf-8')
    assert instance.tick()['status'] == tick_status(db) == 'auth_failed'
    assert events(db, 'auth_dispatch_blocked')


@pytest.mark.parametrize('reason', ['budget', 'circuit'])
def test_blocked_records_reason_and_last_tick(tmp_path, reason):
    instance, db = make_runner(tmp_path)
    instance.budget = SimpleNamespace(allow=lambda: reason != 'budget')
    instance.circuit = SimpleNamespace(allow=lambda: reason != 'circuit')
    assert instance.tick()['status'] == tick_status(db) == 'blocked'
    assert events(db, 'run_blocked')[-1][0] == reason


def test_text_403_does_not_latch_auth_or_stop_next_tick(tmp_path, monkeypatch):
    class Gateway:
        def __init__(self): self.calls = 0
        def start_run(self, *_):
            self.calls += 1
            return {'run_id': 'remote-%d' % self.calls}
        def get_run(self, *_): return {'status': 'failed', 'output': '403 Forbidden'}
    gateway = Gateway()
    instance, db = make_runner(tmp_path, gateway)
    monkeypatch.setattr(instance, '_extract_transcript', lambda *_: {})
    assert instance.tick()['status'] == 'failed'
    assert not (tmp_path / 'lock.auth_failed').exists()
    assert events(db, 'auth_text_unverified')
    instance.tick()
    assert gateway.calls == 2
    assert not (tmp_path / 'lock.auth_failed').exists()


def test_structured_gateway_401_latches_and_next_tick_short_circuits(tmp_path, monkeypatch):
    class Gateway:
        def __init__(self): self.calls = 0
        def start_run(self, *_):
            self.calls += 1
            return {'run_id': 'remote-%d' % self.calls}
        def get_run(self, *_):
            return {'status': 'failed', 'http_status': 401, 'output': 'error'}
    gateway = Gateway()
    instance, db = make_runner(tmp_path, gateway)
    monkeypatch.setattr(instance, '_extract_transcript', lambda *_: {})
    assert instance.tick()['status'] == 'failed'
    assert (tmp_path / 'lock.auth_failed').read_text(encoding='utf-8') == '401'
    assert instance.tick()['status'] == tick_status(db) == 'auth_failed'
    assert gateway.calls == 1
