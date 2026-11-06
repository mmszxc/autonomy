import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "plugins"))
import json
import sqlite3
import time

from autonomy import policy

TB = "Traceback (most recent call last):\n  File \"/tmp/x.py\", line 12, in <module>\n    f()\n%s"


def mk_state(path, msgs):
    c = sqlite3.connect(path)
    c.execute("create table sessions(id text primary key, started_at real)")
    c.execute("create table messages(id integer primary key, session_id text, role text, content text,"
              " tool_calls text, tool_name text, timestamp real)")
    for i, (sid, role, tool, content, ts) in enumerate(msgs):
        c.execute("insert or ignore into sessions values(?,?)", (sid, ts))
        c.execute("insert into messages(session_id,role,content,tool_name,timestamp) values(?,?,?,?,?)",
                  (sid, role, content, tool, ts))
    c.commit()
    c.close()
    return path


def out(text, code=0, error=None):
    return json.dumps({"output": text, "exit_code": code, "error": error})


def test_normalize_error():
    a = policy.normalize_error("KeyError: 'abc123' at /home/hermes/x/y.py line 42 pid=1234 0xdeadbeef 2026-11-01T10:00:00")
    b = policy.normalize_error("KeyError: 'zzz' at /home/other/p/q.py line 7 pid=99 0x1f 2026-11-02 11:11:11")
    assert a == b
    assert "1234" not in a and "/home" not in a


def test_tool_family():
    assert policy.tool_family("terminal") == "terminal"
    assert policy.tool_family("patch") == "write_file"
    assert policy.tool_family("browser_foo") == "browser"
    assert policy.tool_family("weird") == "other"
    assert policy.tool_family(None) == "other"


def test_real_error_judgement():
    c = policy.classify_result
    assert c(out("ok", 0, None)) is None
    assert c(json.dumps({"success": True, "error": None})) is None
    assert c(out("x", 1, None))[0] == "exit_1"
    assert c(json.dumps({"success": False}))[0] == "success_false"
    assert c(json.dumps({"error": "boom happened"}))[0] == "error_field"
    assert c(out(TB % "ValueError: bad 5", 0))[0] == "ValueError"


def test_signature_split_and_trigger(tmp_path):
    now = time.time()
    msgs = []
    for i in range(3):
        msgs.append(("s%d" % i, "tool", "terminal", out(TB % ("KeyError: 'k%d'" % i), 1), now - 100 - i))
    msgs.append(("s9", "tool", "terminal", out(TB % "ValueError: bad value 3", 1), now - 50))
    msgs.append(("s9", "tool", "terminal", out("fine", 0, None), now - 40))
    msgs.append(("s9", "tool", "read_file", json.dumps({"error": None, "content": "x"}), now - 30))
    msgs.append(("old", "tool", "terminal", out(TB % "OSError: old", 1), now - 40 * 86400))
    db = policy.open_state_db(_dir(tmp_path, msgs))
    sigs = policy.failure_signatures(db)
    assert len(sigs) == 2
    assert sigs[0]["exc_type"] == "KeyError" and sigs[0]["count"] == 3 and sigs[0]["triggerable"]
    assert sigs[1]["exc_type"] == "ValueError" and not sigs[1]["triggerable"]
    assert len(sigs[0]["sample_sessions"]) <= 3
    opp = policy.opportunities(db, "terminal", now - 86400)
    assert opp["attempts"] == 5 and opp["fails"] == 4 and opp["sessions"] == 4


def _dir(tmp_path, msgs):
    home = tmp_path / "prof"
    home.mkdir(exist_ok=True)
    mk_state(str(home / "state.db"), msgs)
    return home


def test_degrade_missing_and_locked(tmp_path):
    assert policy.open_state_db(tmp_path / "nope") is None
    assert policy.failure_signatures(None) == []
    assert policy.map_runs(None)["total"] == 0
    home = _dir(tmp_path, [("s", "tool", "terminal", out("x", 1), time.time())])
    lk = sqlite3.connect(str(home / "state.db"), isolation_level=None)
    lk.execute("begin exclusive")
    try:
        db = policy.open_state_db(home)  # ro open under exclusive lock: None or empty, never raises
        if db is not None:
            policy.failure_signatures(db)
        rep = policy.build_report(home)
        assert isinstance(rep, dict)
    finally:
        lk.execute("rollback")
        lk.close()


def test_map_runs_and_l0(tmp_path):
    home = tmp_path / "p"
    (home / "autonomy_data").mkdir(parents=True)
    c = sqlite3.connect(str(home / "autonomy_data" / "autonomy.db"))
    c.execute("create table events(id integer primary key, ts real, run_id text, goal_id text, kind text,"
              " tool text, approval_id text, args_hash text, result_hash text, exit_reason text,"
              " args_len int, result_len int, summary text)")
    now = time.time()
    for i in range(4):
        c.execute("insert into events(ts,run_id,kind,exit_reason,summary) values(?,?,?,?,?)",
                  (now - 10 * i, "r%d" % i, "transcript", "ok", "session=S%d messages=3" % i))
        c.execute("insert into events(ts,run_id,kind,exit_reason,summary) values(?,?,?,?,?)",
                  (now - 10 * i, "r%d" % i, "l0_check", "no_change", "consecutive_no_change=%d" % (4 - i)))
    c.execute("insert into events(ts,run_id,kind,exit_reason,summary) values(?,?,?,?,?)",
              (now, "rx", "transcript", "unavailable", "err"))
    c.commit()
    c.close()
    adb = policy.open_autonomy_db(home)
    m = policy.map_runs(adb)
    assert (m["mapped"], m["total"]) == (4, 5)
    t = policy.inefficiency_triggers(adb)
    assert t["no_change"]["events_ge3"] == 2 and t["no_change"]["triggered"]
    b = policy.baseline(adb, None, "terminal", now + 1)
    assert b["ticks"] == 5 and b["l0_changed_rate"] == 0.0
