"""Wave 2 wiring: real quarantine storage, loop evidence, auth short circuit."""
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins'))
from autonomy.quarantine_cascade import _quarantine_subject, overdue_quarantine_reviews
from autonomy.error_classifier import T2_LOOP_DETECTED, classify
from autonomy import runner
from autonomy.db import AutonomyDB
from autonomy.budget import Budget
from autonomy.circuit import Circuit


def test_review_due_and_overdue_flag_never_deletes():
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE quarantines (id TEXT PRIMARY KEY, subject_type TEXT, subject_id TEXT, '
               'reason TEXT, status TEXT, expires_at REAL, review_count INTEGER, created_at REAL)')
    qid = _quarantine_subject(db, 'run', 'r1', 'failure')
    row = db.execute('SELECT reviewer,review_due_at,created_at FROM quarantines WHERE id=?', (qid,)).fetchone()
    assert row[0] is None and abs(row[1] - row[2] - 72 * 3600) < .01
    assert overdue_quarantine_reviews(db, row[1] - 1) == []
    assert [r['id'] for r in overdue_quarantine_reviews(db, row[1] + 1)] == [qid]
    assert db.execute('SELECT status FROM quarantines WHERE id=?', (qid,)).fetchone()[0] == 'review_overdue'
    assert db.execute('SELECT count(*) FROM quarantines').fetchone()[0] == 1


def test_normalized_repeated_tool_calls_without_new_result_are_loop():
    first = [{'name': 'search', 'args': {'query': 'x', 'request_id': 'random-a'}, 'result': 'same'}]
    again = [{'name': 'search', 'args': {'query': 'x', 'request_id': 'random-b'}, 'result': 'same'}]
    kind, _, _ = classify('tool operation finished', {'status': 'completed'}, tool_calls=again, previous_tool_calls=[first])
    assert kind == T2_LOOP_DETECTED
    again[0]['result'] = 'new document'
    kind, _, _ = classify('tool operation finished', {'status': 'completed'}, tool_calls=again, previous_tool_calls=[first])
    assert kind != T2_LOOP_DETECTED


def test_auth_flag_short_circuits_before_budget_or_gateway(tmp_path, monkeypatch):
    auth_flag = tmp_path / 'runner.lock.auth_failed'
    auth_flag.write_text('401', encoding='utf-8')
    attempts = []
    instance = object.__new__(runner.Runner)
    instance.lock_path = str(tmp_path / 'runner.lock')
    instance.stop_file = str(tmp_path / 'not-stopped')
    instance.db = None
    instance.budget = SimpleNamespace(allow=lambda: attempts.append('budget') or True)
    instance.circuit = SimpleNamespace(is_open=lambda: attempts.append('circuit') or False)
    instance.gateway = SimpleNamespace(run=lambda *a, **kw: attempts.append('gateway'))
    instance.memory_max_mb = 0
    monkeypatch.setattr(instance, '_acquire_lock', lambda f: True)
    monkeypatch.setattr(instance, '_release_lock', lambda f: None)
    result = instance.tick()
    assert result['status'] == 'auth_failed'
    assert attempts == []
    auth_flag.unlink()  # explicit operator reset after credentials are replaced


def test_401_failure_sets_flag_and_next_tick_uses_no_budget(tmp_path):
    class Unauthorized(Exception):
        status_code = 401

    db = AutonomyDB(tmp_path / 'autonomy.db')
    db.create_goal('g', 'goal')
    class Gateway:
        def __init__(self): self.calls = 0
        def chat(self, *args, **kwargs):
            self.calls += 1
            raise Unauthorized('unauthorized')
    gateway = Gateway()
    budget = Budget(db, calls=20, tokens=100, prompt=100)
    instance = runner.Runner(db, budget, Circuit(db), gateway=gateway,
                             lock_path=str(tmp_path / 'lock'),
                             stop_file=str(tmp_path / 'STOP'),
                             thought_log_path=str(tmp_path / 'thoughts.log'),
                             hermes_home=str(tmp_path), mode='parallel')
    first = instance.tick()
    assert first['status'] == 'failed', first
    flag = tmp_path / 'lock.auth_failed'
    assert flag.read_text(encoding='utf-8') == '401'
    before = db.c.execute('select count(*) from events').fetchone()[0]
    second = instance.tick()
    assert second['status'] == 'auth_failed'
    assert gateway.calls == 1
    events = db.c.execute('select kind from events').fetchall()
    assert any(row[0] == 'auth_dispatch_blocked' for row in events)
    assert len(events) == before + 1  # no gateway/LLM retry or budget use
    flag.unlink()  # operator reset after changing credentials


def test_gateway_failure_repeated_tools_is_classified_as_existing_loop(tmp_path, monkeypatch):
    db = AutonomyDB(tmp_path / 'autonomy.db')
    db.create_goal('g', 'goal')
    prior = {'tool_name': 'search', 'args_preview': '{"query":"x","request_id":"a"}',
             'result_preview': 'same evidence'}
    current = {'tool_name': 'search', 'args_preview': '{"query":"x","request_id":"b"}',
               'result_preview': 'same evidence'}
    class Gateway:
        def start_run(self, *_): return {'run_id': 'remote-1'}
        def get_run(self, *_):
            return {'status': 'failed', 'output': 'tool operation failed',
                    'tool_calls': [current], 'previous_tool_calls': [[prior]],
                    'total_tokens': 0}
    instance = runner.Runner(db, Budget(db, calls=20, tokens=100, prompt=100),
                             Circuit(db), gateway=Gateway(), mode='parallel',
                             lock_path=str(tmp_path / 'lock'), stop_file=str(tmp_path / 'STOP'),
                             thought_log_path=str(tmp_path / 'thoughts.log'),
                             hermes_home=str(tmp_path))
    monkeypatch.setattr(instance, '_extract_transcript', lambda *_: {})
    outcome = instance.tick()
    assert outcome['status'] == 'failed', outcome
    rows = db.c.execute("SELECT exit_reason FROM events WHERE kind='run_failed'").fetchall()
    assert rows and rows[-1][0] == T2_LOOP_DETECTED


def test_runner_overdue_check_emits_one_alert_and_preserves_quarantine(tmp_path):
    db = AutonomyDB(tmp_path / 'autonomy.db')
    qid = _quarantine_subject(db.c, 'run', 'old-run', 'failure')
    db.c.execute('UPDATE quarantines SET review_due_at=? WHERE id=?', (0, qid))
    db.c.commit()
    instance = object.__new__(runner.Runner)
    instance.db = db
    instance._check_overdue_quarantine_reviews('tick-1')
    assert any(row['kind'] == 'quarantine_review_overdue' for row in db.list_events(10))
    assert db.c.execute('select status from quarantines where id=?', (qid,)).fetchone()[0] == 'review_overdue'
    instance._check_overdue_quarantine_reviews('tick-2')
    assert len([row for row in db.list_events(10) if row['kind'] == 'quarantine_review_overdue']) == 1
