import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "plugins"))
import json, os
from autonomy import policy
from autonomy.db import AutonomyDB
from autonomy.prompt import build_tick_prompt, load_extra_rules


def test_missing_and_corrupt(tmp_path):
    assert policy.load_active_rules(tmp_path) == []
    d = tmp_path / "policy"; d.mkdir()
    (d / "active.json").write_text("{nope", encoding="utf-8")
    assert policy.load_active_rules(tmp_path) == []
    (d / "active.json").write_text(json.dumps([{"id": "a"}, "x", {"id": "b", "text": "t"}]), encoding="utf-8")
    assert [r["id"] for r in policy.load_active_rules(tmp_path)] == ["b"]
    (d / "active.json").write_text(json.dumps([{"id": str(i), "text": "t"} for i in range(9)]), encoding="utf-8")
    assert len(policy.load_active_rules(tmp_path)) == 5
    assert policy.rules_for_prompt(None) == []


def test_limit_and_remove(tmp_path):
    for i in range(5):
        assert policy.add_rule(tmp_path, "r%d" % i, "txt %d" % i)["ok"]
    assert policy.add_rule(tmp_path, "r6", "x")["error"] == "limit_exceeded"
    assert policy.save_active_rules(tmp_path, [{}] * 6)["ok"] is False
    db = AutonomyDB(tmp_path / "a.db")
    res = policy.remove_rule(tmp_path, "r1", "bad", source="manual_rollback", db=db)
    assert res["ok"] and len(policy.load_candidate_rules(tmp_path)) == 4
    row = db.c.execute("select kind,rule_id,payload_json from policy_events").fetchone()
    assert row[0] == "rollback_manual" and row[1] == "r1" and "manual_rollback" in row[2]


def test_atomic_write_failure(tmp_path, monkeypatch):
    policy.add_rule(tmp_path, "a", "one")
    f = tmp_path / "policy" / "candidates.json"
    before = f.read_text(encoding="utf-8")

    def boom(*a):
        raise OSError("x")
    monkeypatch.setattr(os, "replace", boom)
    assert policy.save_active_rules(tmp_path, [{"id": "z", "text": "z"}])["ok"] is False
    assert f.read_text(encoding="utf-8") == before
    assert [p.name for p in (tmp_path / "policy").iterdir()] == ["candidates.json"]


def test_policy_event_degrade(tmp_path):
    db = AutonomyDB(tmp_path / "a.db")
    assert db.record_policy_event("r", "triggered", "why", {"a": 1}) is True
    db.c.execute("drop table policy_events"); db.c.commit()
    assert db.record_policy_event("r", "triggered") is False
    AutonomyDB(tmp_path / "a.db")  # idempotent init


def test_prompt_injection(tmp_path):
    (tmp_path / "extra_rules.txt").write_text("human rule\n", encoding="utf-8")
    policy.add_rule(tmp_path, "p1", "model rule")
    rules = load_extra_rules(tmp_path) + policy.rules_for_prompt(tmp_path)
    prompt = build_tick_prompt([], [], extra_rules=rules)
    seg = prompt.split("【附加约束】", 1)[1]
    assert "human rule" in seg and "[policy:p1] model rule" not in seg
    assert "[policy:p1]" not in seg
