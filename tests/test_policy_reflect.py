import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "plugins"))
import datetime as dt
import json
import sqlite3
import time
from zoneinfo import ZoneInfo

import pytest

from autonomy import policy, policy_reflect as pr
from autonomy.db import AutonomyDB
from autonomy.budget import Budget
from autonomy.circuit import Circuit
from autonomy.runner import Runner

TZ = ZoneInfo("Asia/Shanghai")
GOOD = json.dumps({"condition": "terminal 执行时报 FileNotFoundError",
                   "action": "先用 search_files 确认路径存在再读写",
                   "tool_family": "terminal"}, ensure_ascii=False)
ERR = '{"success": false, "error": "FileNotFoundError: missing.txt"}'


def _at(h, m, day=6):
    return dt.datetime(2026, 11, day, h, m, tzinfo=TZ)


def _make_state(profile, sessions=2, per_session=2):
    profile.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(profile / "state.db")
    conn.execute("create table messages(id integer primary key, session_id text, role text, content text, "
                 "tool_name text, tool_calls text, timestamp real)")
    now = time.time()
    n = 0
    for s in range(sessions):
        sid = "sess%d" % s
        for _ in range(per_session):
            n += 1
            conn.execute("insert into messages(session_id,role,content,tool_name,tool_calls,timestamp) values(?,?,?,?,?,?)",
                         (sid, "assistant", "", None,
                          json.dumps([{"function": {"name": "terminal", "arguments": '{"cmd":"cat missing.txt"}'}}]),
                          now - 100 + n))
            conn.execute("insert into messages(session_id,role,content,tool_name,tool_calls,timestamp) values(?,?,?,?,?,?)",
                         (sid, "tool", ERR, "terminal", None, now - 99 + n))
    conn.commit()
    conn.close()


class _Llm:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def __call__(self, prompt):
        self.calls += 1
        v = self.outputs[min(self.calls - 1, len(self.outputs) - 1)]
        if isinstance(v, Exception):
            raise v
        return v


@pytest.fixture
def env(tmp_path):
    profile = tmp_path / "profile"
    data = tmp_path / "data"
    data.mkdir()
    _make_state(profile)
    db = AutonomyDB(tmp_path / "a.db")
    return profile, data, db


def _trig_a(sig="sig1", tool="terminal", exc="FileNotFoundError"):
    return {"channel": "A", "key": sig,
            "item": {"signature": sig, "tool": tool, "tool_family": "terminal", "exc_type": exc,
                     "last_line_norm": "FileNotFoundError: <q>", "count": 4, "sample_sessions": []}}


def test_validator_rejections(tmp_path):
    trig = _trig_a()
    ok = lambda o, a=(), r=(): pr.validate_rule(o, trig, list(a), list(r))
    assert ok(json.loads(GOOD))[0] is True
    assert ok({"condition": "terminal 报错", "action": "", "tool_family": "terminal"})[1] == "missing_field"
    assert ok({"condition": "terminal 报错", "action": "x"})[1] == "missing_field"
    assert ok({"condition": "terminal 报错", "action": "x", "tool_family": "weather"})[1] == "unknown_tool_family"
    assert ok({"condition": "terminal 报错", "action": "尝试不同方向", "tool_family": "terminal"})[1] == "banned_phrase"
    assert ok({"condition": "遇到问题时", "action": "先检查", "tool_family": "terminal"})[1] == "no_checkable_clue"
    assert ok({"condition": "terminal 报错", "action": "设置 OPENAI_API_KEY=sk-abcdefghijklmnop123456", "tool_family": "terminal"})[1] == "sensitive_content"
    active = [{"text": "当terminal 执行时报 FileNotFoundError（工具族 terminal）时，先用 search_files 确认路径存在再读写"}]
    assert ok(json.loads(GOOD), active)[1] == "duplicate"
    rej = ["当terminal 执行时报 FileNotFoundError（工具族 terminal）时，先用 search_files 确认路径存在再读写"]
    assert ok(json.loads(GOOD), [], rej)[1] == "duplicate"


def test_rewrites_twice_then_rejected_format_only(env):
    profile, data, db = env
    llm = _Llm(["not json", '{"condition":"x"}', '{"condition":"遇到问题","action":"多思考","tool_family":"terminal"}'])
    res = pr.run_policy_reflection(profile, data, db, _at(3, 5), llm_call=llm, profile="p")
    assert res["executed"] is True and res["status"] == "rejected_format"
    assert llm.calls == 3
    assert not (data / "policy" / "active.json").exists()
    kinds = [r[0] for r in db.c.execute("select kind from policy_events").fetchall()]
    assert "rejected_format" in kinds and "generated" not in kinds


def test_valid_rule_written_as_candidate(env):
    profile, data, db = env
    llm = _Llm([GOOD])
    res = pr.run_policy_reflection(profile, data, db, _at(3, 5), llm_call=llm, profile="p")
    assert res["status"] == "generated" and llm.calls == 1
    rules = policy.load_active_rules(data)
    assert len(rules) == 1
    r = rules[0]
    assert r["status"] == "candidate" and r["channel"] == "A" and r["source_signature"] == res["key"]
    assert r["tool_family"] == "terminal" and r["text"].startswith("当terminal 执行时报 FileNotFoundError")
    kinds = [x[0] for x in db.c.execute("select kind from policy_events order by id").fetchall()]
    assert kinds == ["triggered", "generated"]


def test_limit_does_not_generate(env):
    profile, data, db = env
    for i in range(5):
        assert policy.add_rule(data, "r%d" % i, "规则文本 %d" % i, source_signature="other%d" % i)["ok"]
    llm = _Llm([GOOD])
    res = pr.run_policy_reflection(profile, data, db, _at(3, 5), llm_call=llm, profile="p")
    assert res["status"] == "limit" and llm.calls == 0
    row = db.c.execute("select kind, reason from policy_events").fetchall()
    assert ("triggered", "limit") in [tuple(r) for r in row]
    assert len(policy.load_active_rules(data)) == 5


def test_window_and_once_per_day(env):
    profile, data, db = env
    llm = _Llm([GOOD])
    for h, m in ((2, 59), (3, 20), (4, 0), (15, 0)):
        assert pr.run_policy_reflection(profile, data, db, _at(h, m), llm_call=llm)["executed"] is False
    assert llm.calls == 0
    assert pr.run_policy_reflection(profile, data, db, _at(3, 0), llm_call=llm)["executed"] is True
    again = pr.run_policy_reflection(profile, data, db, _at(3, 10), llm_call=llm)
    assert again == {"executed": False, "status": "already_ran"}
    assert llm.calls == 1
    # next local day runs again
    assert pr.run_policy_reflection(profile, data, db, _at(3, 5, day=7), llm_call=llm)["executed"] is True


def test_model_error_degrades(env):
    profile, data, db = env
    llm = _Llm([RuntimeError("boom")])
    res = pr.run_policy_reflection(profile, data, db, _at(3, 5), llm_call=llm)
    assert res["executed"] is True and res["status"] == "model_error"
    assert not (data / "policy" / "active.json").exists()
    assert pr.run_policy_reflection(profile, data, db, _at(3, 5), llm_call=None)["status"] == "already_ran"


def test_no_model_and_no_trigger_degrade(tmp_path):
    db = AutonomyDB(tmp_path / "a.db")
    res = pr.run_policy_reflection(tmp_path / "missing", tmp_path, db, _at(3, 5), llm_call=_Llm([GOOD]))
    assert res["executed"] is True and res["status"] == "no_trigger"


def test_pick_trigger_prefers_a_and_excludes_covered():
    sigs = [{"signature": "a1", "triggerable": True, "count": 5}, {"signature": "a2", "triggerable": True, "count": 9},
            {"signature": "a3", "triggerable": False, "count": 50}]
    ineff = {"no_change": {"triggered": True, "kind": "l0_no_change"},
             "seed_search_repeat": {"triggered": True, "kind": "seed_search_repeat"}}
    t = pr.pick_trigger(sigs, ineff, [])
    assert t["channel"] == "A" and t["key"] == "a2"
    t = pr.pick_trigger(sigs, ineff, [{"source_signature": "a2"}], rejected_keys=["a1"])
    assert t["key"] == "B:l0_no_change"
    t = pr.pick_trigger(sigs, {"no_change": {"triggered": False}}, [{"source_signature": "a2"}], rejected_keys=["a1"])
    assert t is None


def test_input_is_redacted(env):
    profile, data, db = env
    conn = sqlite3.connect(profile / "state.db")
    conn.execute("insert into messages(session_id,role,content,tool_name,tool_calls,timestamp) values(?,?,?,?,?,?)",
                 ("sess0", "assistant", "", None, json.dumps([{"function": {"name": "terminal", "arguments":
                  'OPENAI_API_KEY=sk-SECRETVALUE123456789\\ntoken: SECRETTOKEN999 cmd'}}]), time.time()))
    conn.commit()
    conn.close()
    sdb = policy.open_state_db(profile)
    text, _pm = pr.build_reflection_input(_trig_a(), profile, sdb, None, [])
    assert "SECRETVALUE" not in text and "SECRETTOKEN" not in text
    assert "FileNotFoundError" in text
    assert len(text) <= pr.INPUT_MAX_CHARS


def test_fragment_capped_at_800():
    conn = sqlite3.connect(":memory:")
    conn.execute("create table messages(session_id text, role text, content text, tool_name text, tool_calls text, timestamp real)")
    conn.execute("insert into messages values('s','tool',?,'terminal',NULL,1)", (ERR + " " + "y" * 3000,))
    assert len(pr._session_fragment(conn, "s", "terminal")) <= pr.SAMPLE_MAX_CHARS


def test_window_helper():
    assert pr.in_reflection_window(_at(3, 0)) and pr.in_reflection_window(_at(3, 19))
    assert not pr.in_reflection_window(_at(3, 20)) and not pr.in_reflection_window(_at(2, 59))


def test_runner_reflection_slot_skips_l0_and_surprise(tmp_path, monkeypatch):
    db = AutonomyDB(tmp_path / "a.db")
    lock = tmp_path / "lock"
    r = Runner(db, Budget(), Circuit(), str(tmp_path / "STOP"),
               lambda: {"text": '{"thoughts":"x","proposals":[]}', "completion_tokens": 1, "prompt_tokens": 1},
               lock_path=str(lock))
    r._l0_set_counter(2)
    calls = []

    def fake(*a, **k):
        calls.append(k)
        return {"executed": True, "status": "no_trigger"}

    monkeypatch.setattr(pr, "run_policy_reflection", fake)
    out = r.tick()
    assert out["status"] == "policy_reflect"
    assert r._l0_get_counter() == 2
    kinds = {x["kind"] for x in db.list_events(50)}
    assert "tick" not in kinds and "l0_check" not in kinds
    assert calls and calls[0].get("run_id") == out["run_id"]


def test_runner_normal_tick_when_not_reflecting(tmp_path, monkeypatch):
    db = AutonomyDB(tmp_path / "a.db")
    r = Runner(db, Budget(), Circuit(), str(tmp_path / "STOP"),
               lambda: {"text": '{"thoughts":"x","proposals":[]}', "completion_tokens": 1, "prompt_tokens": 1},
               lock_path=str(tmp_path / "lock"))
    monkeypatch.setattr(pr, "run_policy_reflection", lambda *a, **k: {"executed": False, "status": "outside_window"})
    assert r.tick()["status"] == "ok"
    assert "tick" in {x["kind"] for x in db.list_events(50)}


def test_runner_reflection_exception_degrades(tmp_path, monkeypatch):
    db = AutonomyDB(tmp_path / "a.db")
    r = Runner(db, Budget(), Circuit(), str(tmp_path / "STOP"),
               lambda: {"text": '{"thoughts":"x","proposals":[]}', "completion_tokens": 1, "prompt_tokens": 1},
               lock_path=str(tmp_path / "lock"))

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(pr, "run_policy_reflection", boom)
    assert r.tick()["status"] == "ok"
