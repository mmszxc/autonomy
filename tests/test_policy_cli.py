import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / "plugins"))
import json
import sqlite3

import pytest

from autonomy import cli, policy
from autonomy.db import AutonomyDB


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "profile"
    data = h / "autonomy_data"
    data.mkdir(parents=True)
    AutonomyDB(data / "autonomy.db")  # creates policy_events and events tables
    policy.add_rule(data, "pr-aaaa000001", "当 terminal 报 FileNotFoundError 时先确认路径",
                    channel="A", tool_family="terminal", ttl_days=7, ttl_opportunities=30)
    policy.add_rule(data, "pr-bbbb000002", "先检查目录 " * 60, channel="B")
    return h


def _run(argv, capsys):
    rc = cli.main(argv)
    return rc, capsys.readouterr().out


def test_help_mentions_policy_subcommands(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "policy" in out and "rollback" in out and "history" in out


def test_show_lists_rules_and_inspection_fields(home, capsys):
    rc, out = _run(["policy", "show", "--hermes-home", str(home)], capsys)
    assert rc == 0
    assert "pr-aaaa000001" in out and "pr-bbbb000002" in out
    assert "候选数" in out and "映射覆盖率" in out and "今日反思触发次数" in out
    line = [ln for ln in out.splitlines() if ln.strip().startswith("text: 先检查目录")][0]
    assert len(line.strip()[len("text: "):]) <= 200  # rule text truncated to 200 chars
    assert "API_KEY" not in out and ".env" not in out


def test_show_json(home, capsys):
    rc, out = _run(["policy", "show", "--json", "--hermes-home", str(home)], capsys)
    assert rc == 0
    obj = json.loads(out)
    assert {r["id"] for r in obj["rules"]} == {"pr-aaaa000001", "pr-bbbb000002"}
    assert obj["summary"]["ok"] is True and obj["summary"]["candidates"] == 0


def test_history_limit(home, capsys):
    db = AutonomyDB(home / "autonomy_data" / "autonomy.db")
    for i in range(5):
        db.record_policy_event("pr-x%d" % i, "generated", "r%d" % i, {}, "profile")
    rc, out = _run(["policy", "history", "--limit", "2", "--hermes-home", str(home)], capsys)
    assert rc == 0
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 2 and "pr-x4" in lines[0] and "pr-x3" in lines[1]


def test_rollback_removes_rule_and_records_no_failure(home, capsys):
    rc, out = _run(["policy", "rollback", "pr-aaaa000001", "--hermes-home", str(home)], capsys)
    assert rc == 0 and "rolled back" in out
    ids = [r["id"] for r in policy.load_candidate_rules(home / "autonomy_data")]
    assert ids == ["pr-bbbb000002"]
    conn = sqlite3.connect(str(home / "autonomy_data" / "autonomy.db"))
    kinds = [r[0] for r in conn.execute("select kind from policy_events where rule_id='pr-aaaa000001'")]
    conn.close()
    assert kinds == ["rollback_manual"]


def test_rollback_missing_rule_fails_without_event(home, capsys):
    rc, out = _run(["policy", "rollback", "pr-nope", "--hermes-home", str(home)], capsys)
    assert rc == 1 and "rollback failed" in out
    conn = sqlite3.connect(str(home / "autonomy_data" / "autonomy.db"))
    assert conn.execute("select count(*) from policy_events").fetchone()[0] == 0
    conn.close()
