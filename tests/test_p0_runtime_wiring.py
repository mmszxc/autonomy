import hashlib
import os
import time

import pytest

from plugins.autonomy.budget import Budget, BudgetLedger
from plugins.autonomy.circuit import Circuit
from plugins.autonomy.cli_transport import capture_process_evidence
from plugins.autonomy.db import AutonomyDB
from plugins.autonomy.event_log import EventLog
from plugins.autonomy.runner import Runner


class TerminalGateway:
    def __init__(self, state):
        self.state = dict(state)

    def start_run(self, *_args, **_kwargs):
        return {"run_id": "gateway-run"}

    def get_run(self, _run_id):
        return dict(self.state)

    def stop_run(self, _run_id):
        return None


class PreemptGateway(TerminalGateway):
    def __init__(self, state, preempt_file):
        super().__init__(state)
        self.preempt_file = preempt_file
        self.stopped = False

    def start_run(self, *_args, **_kwargs):
        self.preempt_file.write_text("preempt", encoding="utf-8")
        future_ns = time.time_ns() + 1_000_000
        os.utime(self.preempt_file, ns=(future_ns, future_ns))
        return {"run_id": "gateway-run"}

    def stop_run(self, _run_id):
        self.stopped = True


def process_state(*, failed=False, timed_out=False, oom=False):
    stdout = b'{"proposals": []}'
    stderr = b"worker failed" if failed else b""
    exit_code = 137 if oom else (124 if timed_out else (1 if failed else 0))
    return {
        "status": "failed" if failed or timed_out or oom else "completed",
        "session_id": "session-1",
        "output": stdout.decode(),
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        "evidence": {
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "stdout_bytes": len(stdout),
            "stderr_bytes": len(stderr),
            "stdout_head": stdout.decode(),
            "stdout_tail": stdout.decode(),
            "stderr_head": stderr.decode(),
            "stderr_tail": stderr.decode(),
            "stdout_truncated": False,
            "stderr_truncated": False,
            "exit_code": exit_code,
            "signal_number": 9 if oom else None,
            "timed_out": timed_out,
            "oom": oom,
            "buffer_path": None,
        },
    }


def build_runner(tmp_path, state, rotation_extra_keywords=None):
    db = AutonomyDB(tmp_path / "autonomy.db")
    runner = Runner(
        db,
        Budget(db),
        Circuit(db),
        stop_file=str(tmp_path / "STOP"),
        gateway=TerminalGateway(state),
        lock_path=str(tmp_path / "tick.lock"),
        workspace=str(tmp_path / "workspace"),
        p0_mode="observe_only",
        event_log=EventLog(db),
        budget_ledger=BudgetLedger(weekly_units=70, db=db),
        rotation_extra_keywords=rotation_extra_keywords,
    )
    runner._extract_transcript = lambda *_args: {
        "session_id": "session-1",
        "text": "used a tool and observed the result",
        "tool_call_count": 1,
    }
    return runner, db


@pytest.mark.parametrize(
    "state,source,expected_kind",
    [
        (process_state(), "workspace", "action_completed"),
        (process_state(failed=True), "none", "action_failed"),
        (process_state(timed_out=True), "none", "action_timeout"),
        (process_state(oom=True), "none", "action_oom"),
    ],
)
def test_complete_tick_persists_terminal_evidence_receipt_and_budget(tmp_path, state, source, expected_kind):
    runner, db = build_runner(tmp_path, state)
    runner._l0_detect_change = lambda *_args: source

    result = runner.tick()

    receipt_kinds = [row[0] for row in db.c.execute("select kind from receipts order by ts,id")]
    assert expected_kind in receipt_kinds
    assert db.c.execute("select count(*) from evidence").fetchone()[0] >= 2
    assert db.c.execute("select count(*) from budget_ledger where bucket='normal'").fetchone()[0] == 1
    if expected_kind == "action_completed":
        assert result["status"] == "ok"
    else:
        assert result["status"] == "failed"


def test_preempted_tick_persists_interrupted_receipt_evidence_and_one_budget_entry(tmp_path):
    runner, db = build_runner(tmp_path, process_state())
    preempt_file = tmp_path / "preempt"
    gateway = PreemptGateway(process_state(), preempt_file)
    runner.gateway = gateway
    runner.preempt_file = str(preempt_file)
    runner.pending_resume = str(tmp_path / "pending-resume")

    result = runner.tick()

    row = db.c.execute(
        "select run_id,goal_id,status from receipts where kind='action_interrupted'"
    ).fetchone()
    assert result["status"] == "preempted"
    assert gateway.stopped is True
    assert row is not None
    assert row[0] == result["run_id"]
    assert row[1] == "default"
    assert row[2] == "verified"
    assert db.c.execute(
        "select count(*) from evidence e join receipts r on r.id=e.receipt_id "
        "where r.kind='action_interrupted'"
    ).fetchone()[0] == 2
    assert db.c.execute(
        "select count(*) from budget_ledger where bucket='normal'"
    ).fetchone()[0] == 1


def test_complete_tick_records_bound_no_action_receipt(tmp_path):
    runner, db = build_runner(tmp_path, process_state())
    runner._l0_detect_change = lambda *_args: "none"

    result = runner.tick()

    row = db.c.execute(
        "select run_id,goal_id,status from receipts where kind='no_action'"
    ).fetchone()
    assert result["status"] == "ok"
    assert row is not None
    assert row[0] == result["run_id"]
    assert row[1] == "default"
    assert row[2] == "verified"
    assert db.c.execute(
        "select count(*) from evidence e join receipts r on r.id=e.receipt_id where r.kind='no_action'"
    ).fetchone()[0] == 2
    assert db.c.execute("select count(*) from budget_ledger").fetchone()[0] == 1


def test_profile_rotation_keywords_merge_and_cover_online_business_set(tmp_path):
    extras = [
        "光伏、半导体、政策、行业、A股、市场日报",
        "龙哥、投资、不亏钱、空仓、选股、政策意志",
        "外部文件、磁盘痕迹、真实动作、探针、反爬、Cloudflare",
    ]
    runner, _db = build_runner(tmp_path, process_state(), extras)

    queries = runner._rotation_queries()
    joined = "\n".join(queries)
    assert len(queries) == len(set(queries))
    for phrase in (
        "我正在形成的对世界的理解",
        "我感受到的困惑和未解答的问题",
        "我做过的最困难的选择",
        "CrowdStrike",
        "国际安全",
        "数字货币",
        "宏观经济",
        "边界代谢",
        "批量吞并",
        "休眠信号",
        "增量强化",
        "dormant",
        "access_count",
        "last_reinforced",
        "caused_by",
        "种子表",
        "光伏",
        "半导体",
        "政策",
        "行业",
        "A股",
        "市场日报",
        "龙哥",
        "投资",
        "不亏钱",
        "空仓",
        "选股",
        "政策意志",
        "外部文件",
        "磁盘痕迹",
        "真实动作",
        "探针",
        "反爬",
        "Cloudflare",
    ):
        assert phrase in joined


def test_raw_buffer_respects_t2_but_keeps_permanent_evidence(tmp_path, monkeypatch):
    class Usage:
        total = 100 * 1024**3
        used = 99 * 1024**3
        free = 1024**3

    monkeypatch.setattr("plugins.autonomy.cli_transport.shutil.disk_usage", lambda _path: Usage())
    evidence = capture_process_evidence(
        b"stdout", b"stderr", 0,
        buffer_dir=tmp_path / "raw",
        min_free_bytes=1024**3,
    )

    assert evidence["buffer_path"] is None
    assert evidence["stdout_sha256"] == hashlib.sha256(b"stdout").hexdigest()
    assert evidence["stderr_sha256"] == hashlib.sha256(b"stderr").hexdigest()
