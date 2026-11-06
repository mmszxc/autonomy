import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "plugins"))
import json
import sqlite3
import time

import pytest

from autonomy import policy, policy_eval
from autonomy.db import AutonomyDB

NOW = time.time()
DAY = 86400
OK = json.dumps({"output": "ok", "exit_code": 0, "error": None})
BAD = json.dumps({"output": "boom", "exit_code": 1, "error": None})


def _state(home, rows):
    """rows: (sid, tool, content, ts) -> role=tool messages (id order = insertion order)."""
    home.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(home / "state.db"))
    c.execute("create table sessions(id text primary key, started_at real)")
    c.execute("create table messages(id integer primary key, session_id text, role text, content text,"
              " tool_calls text, tool_name text, timestamp real)")
    for sid, tool, content, ts in rows:
        c.execute("insert or ignore into sessions values(?,?)", (sid, ts))
        c.execute("insert into messages(session_id,role,content,tool_name,timestamp) values(?,?,?,?,?)",
                  (sid, "tool", content, tool, ts))
    c.commit()
    c.close()


def _autonomy(home, events):
    """events: (ts, run_id, kind, exit_reason, summary)."""
    (home / "autonomy_data").mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(home / "autonomy_data" / "autonomy.db"))
    c.execute("create table events(id integer primary key, ts real, run_id text, goal_id text, kind text,"
              " tool text, approval_id text, args_hash text, result_hash text, exit_reason text,"
              " args_len int, result_len int, summary text)")
    for ts, rid, kind, er, summ in events:
        c.execute("insert into events(ts,run_id,kind,exit_reason,summary) values(?,?,?,?,?)",
                  (ts, rid, kind, er, summ))
    c.commit()
    c.close()


def _rule(rid, family="terminal", created=None, baseline=None, ttl_days=7, ttl_opp=30, pm="pm1"):
    return {"id": rid, "text": "rule " + rid, "channel": "A", "tool_family": family, "condition": "",
            "source_pm_id": pm, "source_signature": "", "created_at": created if created is not None else NOW - 2 * DAY,
            "created_run": "", "ttl_opportunities": ttl_opp, "ttl_days": ttl_days, "status": "candidate",
            "baseline": baseline or {"fail_rate": 0.0, "attempts_per_tick": 0.0, "l0_changed_rate": 0.0},
            "stats": {"opportunities": 0, "fails": 0, "reuse_sessions": []}}


def _write_rules(data_dir, rules):
    (data_dir / "policy").mkdir(parents=True, exist_ok=True)
    (data_dir / "policy" / "candidates.json").write_text(json.dumps(rules), encoding="utf-8")


def _active_ids(data_dir):
    return [r["id"] for r in policy.load_candidate_rules(data_dir)]


def _home(tmp_path, rows, events=(), name="prof"):
    home = tmp_path / name
    _state(home, rows)
    _autonomy(home, list(events))
    return home


def _ctx(home):
    return {"now": NOW, "state_db": policy.open_state_db(home), "autonomy_db": policy.open_autonomy_db(home)}


# ---------------------------------------------------------------- veto

def test_no_veto_below_min_opportunities(tmp_path):
    base = NOW - 2 * DAY
    rows = [("s%d" % i, "terminal", BAD, base + 100 * (i + 1)) for i in range(7)]  # 7 sessions, all failing
    home = _home(tmp_path, rows)
    res = policy_eval.veto_check(_rule("r1"), _ctx(home))
    assert res["vetoed"] is False
    assert res["metrics"]["opportunities"] == 7
    assert res["reason"].startswith("opportunities<8")


def test_fail_rate_signal_alone(tmp_path):
    base = NOW - 2 * DAY
    rows = [("s%d" % i, "terminal", BAD if i < 4 else OK, base + 100 * (i + 1)) for i in range(10)]
    home = _home(tmp_path, rows)
    res = policy_eval.veto_check(_rule("r1"), _ctx(home))
    assert res["vetoed"] is True and res["reason"] == "fail_rate_up"
    assert res["metrics"]["post_fails"] == 4


def test_fail_rate_needs_absolute_count(tmp_path):
    base = NOW - 2 * DAY
    # 10 sessions, 1 failing -> rate 0.1 < 0.15 threshold, not vetoed
    rows = [("s%d" % i, "terminal", BAD if i == 0 else OK, base + 100 * (i + 1)) for i in range(10)]
    home = _home(tmp_path, rows)
    assert policy_eval.veto_check(_rule("r1"), _ctx(home))["vetoed"] is False


def test_avoidance_signal_alone(tmp_path):
    base = NOW - 2 * DAY
    rows = [("s%d" % i, "terminal", OK, base + 100 * (i + 1)) for i in range(10)]
    events = [(base + 1000 + i, "run%d" % i, "transcript", "ok", "session=s%d" % i) for i in range(10)]
    home = _home(tmp_path, rows, events)
    # baseline 10 attempts/tick; post is 1 attempt over 10 ticks = 1.0 < 5.0
    rule = _rule("r1", baseline={"fail_rate": 0.0, "attempts_per_tick": 10.0, "l0_changed_rate": 0.0})
    res = policy_eval.veto_check(rule, _ctx(home))
    assert res["vetoed"] is True and res["reason"] == "avoidance"


def test_l0_changed_drop_alone(tmp_path):
    base = NOW - 2 * DAY
    rows = [("s%d" % i, "terminal", OK, base + 100 * (i + 1)) for i in range(10)]
    events = [(base + 1000 + i, "r%d" % i, "l0_check", "no_change", "x") for i in range(4)]
    home = _home(tmp_path, rows, events)
    rule = _rule("r1", baseline={"fail_rate": 0.0, "attempts_per_tick": 0.0, "l0_changed_rate": 0.5})
    res = policy_eval.veto_check(rule, _ctx(home))
    assert res["vetoed"] is True and res["reason"] == "l0_changed_drop"


def test_healthy_rule_not_vetoed(tmp_path):
    base = NOW - 2 * DAY
    rows = [("s%d" % i, "terminal", OK, base + 100 * (i + 1)) for i in range(10)]
    home = _home(tmp_path, rows)
    res = policy_eval.veto_check(_rule("r1"), _ctx(home))
    assert res["vetoed"] is False and res["reason"] == "none"


def test_veto_records_outcome_and_event(tmp_path):
    base = NOW - 2 * DAY
    rows = [("s%d" % i, "terminal", BAD, base + 100 * (i + 1)) for i in range(10)]
    home = _home(tmp_path, rows)
    data = home / "autonomy_data"
    _write_rules(data, [_rule("r1", pm="pm-xyz")])
    db = AutonomyDB(data / "autonomy.db")
    calls = []

    def fake(pm_id, failed, counter_seed_id=None, source="auto"):
        calls.append((pm_id, failed, counter_seed_id, source))
        return {"found": True, "recorded": True, "reason": "ok"}

    summ = policy_eval.evaluate_rules(home, data, db, now=NOW, outcome_fn=fake)
    assert summ["ok"] is True
    assert calls == [("pm-xyz", True, "r1", "auto")]
    assert _active_ids(data) == []
    row = db.c.execute("select kind, rule_id from policy_events").fetchone()
    assert tuple(row) == ("vetoed", "r1")


def test_veto_survives_outcome_exception(tmp_path):
    base = NOW - 2 * DAY
    rows = [("s%d" % i, "terminal", BAD, base + 100 * (i + 1)) for i in range(10)]
    home = _home(tmp_path, rows)
    data = home / "autonomy_data"
    _write_rules(data, [_rule("r1")])
    db = AutonomyDB(data / "autonomy.db")

    def boom(*a, **k):
        raise RuntimeError("store down")

    summ = policy_eval.evaluate_rules(home, data, db, now=NOW, outcome_fn=boom)
    assert summ["ok"] is True and _active_ids(data) == []
    assert db.c.execute("select kind from policy_events").fetchone()[0] == "vetoed"


# ---------------------------------------------------------------- expiry

def test_expiry_classification(tmp_path):
    base = NOW - 10 * DAY
    rows = []
    # A: write_file family, only 3 opportunities, created 10 days ago (ttl 7) -> expired_no_opportunity
    rows += [("a%d" % i, "patch", OK, base + 100 * (i + 1)) for i in range(3)]
    # B: terminal family, 10 healthy opportunities, created 10 days ago -> expired_neutral
    rows += [("b%d" % i, "terminal", OK, base + 100 * (i + 1)) for i in range(10)]
    # C: read_file family, 10 healthy opportunities, ttl_opportunities=5, created 1 day ago -> expired_neutral
    rows += [("c%d" % i, "read_file", OK, NOW - DAY + 100 * (i + 1)) for i in range(10)]
    home = _home(tmp_path, rows)
    data = home / "autonomy_data"
    _write_rules(data, [
        _rule("A", family="write_file", created=base, ttl_days=7),
        _rule("B", family="terminal", created=base, ttl_days=7),
        _rule("C", family="read_file", created=NOW - DAY, ttl_days=7, ttl_opp=5),
    ])
    db = AutonomyDB(data / "autonomy.db")
    policy_eval.evaluate_rules(home, data, db, now=NOW, outcome_fn=lambda *a, **k: {})
    kinds = dict(db.c.execute("select rule_id, kind from policy_events").fetchall())
    assert kinds == {"A": "expired_no_opportunity", "B": "expired_neutral", "C": "expired_neutral"}
    assert _active_ids(data) == []


def test_not_expired_no_action(tmp_path):
    base = NOW - 2 * DAY
    rows = [("s%d" % i, "terminal", OK, base + 100 * (i + 1)) for i in range(3)]
    home = _home(tmp_path, rows)
    data = home / "autonomy_data"
    _write_rules(data, [_rule("r1")])
    db = AutonomyDB(data / "autonomy.db")
    policy_eval.evaluate_rules(home, data, db, now=NOW, outcome_fn=lambda *a, **k: {})
    assert _active_ids(data) == ["r1"]
    assert db.c.execute("select count(*) from policy_events").fetchone()[0] == 0


# ---------------------------------------------------------------- reuse

def test_reuse_requires_distinct_session_and_later_nonread(tmp_path):
    base = NOW - 2 * DAY
    rows = [
        # S1: terminal then write_file -> counts
        ("S1", "terminal", OK, base + 10), ("S1", "write_file", OK, base + 20),
        # S2: terminal then read_file only -> no
        ("S2", "terminal", OK, base + 10), ("S2", "read_file", OK, base + 20),
        # S3: write_file before terminal (no later non-read after the family call) -> no
        ("S3", "write_file", OK, base + 10), ("S3", "terminal", OK, base + 20),
        # S4: several non-read calls after terminal -> counts once
        ("S4", "terminal", OK, base + 10), ("S4", "write_file", OK, base + 20), ("S4", "write_file", OK, base + 30),
    ]
    home = _home(tmp_path, rows)
    sdb = policy.open_state_db(home)
    got = policy_eval.reuse_sessions(sdb, _rule("r1", created=base), NOW)
    assert got == ["S1", "S4"]


def test_promoted_eligible_keeps_status_and_is_recorded_once(tmp_path):
    base = NOW - 2 * DAY
    rows = []
    for i in range(10):
        rows.append(("s%d" % i, "terminal", OK, base + 100 * (i + 1)))
        rows.append(("s%d" % i, "write_file", OK, base + 100 * (i + 1) + 1))
    home = _home(tmp_path, rows)
    data = home / "autonomy_data"
    _write_rules(data, [_rule("r1", created=base)])
    db = AutonomyDB(data / "autonomy.db")
    policy_eval.evaluate_rules(home, data, db, now=NOW, outcome_fn=lambda *a, **k: {})
    policy_eval.evaluate_rules(home, data, db, now=NOW, outcome_fn=lambda *a, **k: {})
    rules = policy.load_candidate_rules(data)
    assert len(rules) == 1 and rules[0]["status"] == "candidate"
    assert len(rules[0]["stats"]["reuse_sessions"]) == 10
    n = db.c.execute("select count(*) from policy_events where kind='promoted_eligible'").fetchone()[0]
    assert n == 1


def test_reuse_below_threshold_not_promoted(tmp_path):
    base = NOW - 2 * DAY
    rows = []
    for i in range(10):
        rows.append(("s%d" % i, "terminal", OK, base + 100 * (i + 1)))
        if i < 2:  # only 2 sessions with a later non-read call
            rows.append(("s%d" % i, "write_file", OK, base + 100 * (i + 1) + 1))
    home = _home(tmp_path, rows)
    data = home / "autonomy_data"
    _write_rules(data, [_rule("r1", created=base)])
    db = AutonomyDB(data / "autonomy.db")
    policy_eval.evaluate_rules(home, data, db, now=NOW, outcome_fn=lambda *a, **k: {})
    assert db.c.execute("select count(*) from policy_events").fetchone()[0] == 0
    assert _active_ids(data) == ["r1"]


# ---------------------------------------------------------------- degrade / dry-run

def test_evaluate_never_raises(tmp_path, monkeypatch):
    home = _home(tmp_path, [])
    data = home / "autonomy_data"
    _write_rules(data, [_rule("r1")])
    db = AutonomyDB(data / "autonomy.db")

    def bad(*a, **k):
        raise RuntimeError("explode")

    monkeypatch.setattr(policy, "opportunities", bad)
    res = policy_eval.evaluate_rules(home, data, db, now=NOW, outcome_fn=lambda *a, **k: {})
    assert res["ok"] is True and [e["id"] for e in res["errors"]] == ["r1"]  # per-rule degrade
    assert _active_ids(data) == ["r1"]
    monkeypatch.setattr(policy_eval, "decide_rules", bad)  # whole-tick failure -> dict, no raise
    res = policy_eval.evaluate_rules(home, data, db, now=NOW, outcome_fn=lambda *a, **k: {})
    assert res["ok"] is False and "RuntimeError" in res["error"]
    assert _active_ids(data) == ["r1"]


def test_evaluate_bad_inputs_do_not_raise():
    assert policy_eval.evaluate_rules(None, None, None)["ok"] in (True, False)
    assert isinstance(policy_eval.evaluate_rules("/nonexistent/x", None, None), dict)


def test_one_rule_error_does_not_block_others(tmp_path, monkeypatch):
    base = NOW - 2 * DAY
    rows = [("s%d" % i, "terminal", BAD, base + 100 * (i + 1)) for i in range(10)]
    home = _home(tmp_path, rows)
    data = home / "autonomy_data"
    _write_rules(data, [_rule("bad"), _rule("good")])
    db = AutonomyDB(data / "autonomy.db")
    orig = policy_eval.veto_check

    def flaky(rule, ctx):
        if rule["id"] == "bad":
            raise ValueError("x")
        return orig(rule, ctx)

    monkeypatch.setattr(policy_eval, "veto_check", flaky)
    res = policy_eval.evaluate_rules(home, data, db, now=NOW, outcome_fn=lambda *a, **k: {})
    assert res["ok"] is True
    assert [e["id"] for e in res["errors"]] == ["bad"]
    assert _active_ids(data) == ["bad"]  # error rule untouched; good rule vetoed


def test_dry_run_is_read_only(tmp_path):
    base = NOW - 2 * DAY
    rows = [("s%d" % i, "terminal", BAD, base + 100 * (i + 1)) for i in range(10)]
    home = _home(tmp_path, rows)
    data = home / "autonomy_data"
    _write_rules(data, [_rule("r1")])
    db = AutonomyDB(data / "autonomy.db")
    before = (data / "policy" / "candidates.json").read_text(encoding="utf-8")
    rep = policy_eval.dry_run(home, now=NOW)
    assert rep["ok"] is True and [v["id"] for v in rep["vetoed"]] == ["r1"]
    assert (data / "policy" / "candidates.json").read_text(encoding="utf-8") == before
    assert db.c.execute("select count(*) from policy_events").fetchone()[0] == 0
