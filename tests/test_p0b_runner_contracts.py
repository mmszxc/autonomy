from plugins.autonomy.cli_transport import CliGateway
from plugins.autonomy.db import AutonomyDB
from plugins.autonomy.runner import Runner


class _Cursor:
    def __init__(self, rows=()): self.rows = rows
    def execute(self, *args, **kwargs): return self
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None


class _DB:
    def __init__(self, rows=()): self.c = _Cursor(rows)


def test_empty_or_zero_tool_fast_turn_cannot_get_surprise_reward():
    runner = Runner.__new__(Runner)
    runner.db = _DB([("elapsed=100", "tools=2")] * 6)
    assert runner._surprise_importance("r", 1, 0, has_receipt=False) == 0.2
    assert runner._surprise_importance("r", 1, 4, has_receipt=False) == 0.2


def test_l0_change_reports_evidence_source():
    runner = Runner.__new__(Runner)
    runner._l0_has_git_change = lambda started: True
    assert runner._l0_detect_change(0, -1) == "git"
    runner._l0_has_git_change = lambda started: False
    runner._l0_snapshot_recall = lambda: 3
    assert runner._l0_detect_change(0, 2) == "recall"
    runner._l0_snapshot_recall = lambda: 2
    assert runner._l0_detect_change(0, 2) == "none"


def test_receipted_correct_no_action_is_recognised(tmp_path):
    runner = Runner.__new__(Runner)
    runner.db = AutonomyDB(tmp_path / "autonomy.db")
    receipt_id = runner.db.append_receipt(
        "no_action", "payload-sha", status="verified", run_id="run", goal_id="goal"
    )
    runner.db.append_evidence(
        receipt_id, "decision", "evidence-sha", 0, status="verified"
    )
    assert runner._has_correct_no_action_receipt("run", "goal") is True


def test_unbound_verified_no_change_receipt_cannot_exempt_idle_run(tmp_path):
    runner = Runner.__new__(Runner)
    runner.db = AutonomyDB(tmp_path / "autonomy.db")
    # Same run id is insufficient: wrong kind/goal and no verified evidence binding.
    runner.db.append_receipt(
        "no_change", "fake-sha", status="verified", run_id="run", goal_id="other-goal"
    )
    assert runner._has_correct_no_action_receipt("run", "goal") is False


def test_failed_evidence_caps_surprise_even_with_transcript_and_tools():
    runner = Runner.__new__(Runner)
    runner.db = _DB([("elapsed=100", "tools=2")] * 6)
    transcript = {"session_id": "s", "text": "used a tool", "tool_call_count": 2}

    nonzero = {"status": "failed", "evidence": {"exit_code": 7}}
    oom = {"status": "failed", "evidence": {"exit_code": -9, "oom": True}}
    for failed_state in (nonzero, oom):
        has_receipt = runner._has_successful_run_receipt(failed_state, transcript)
        assert has_receipt is False
        assert runner._surprise_importance("r", 1, 2, has_receipt=has_receipt) == 0.2


def test_cli_nonzero_exit_with_stdout_stays_failed():
    class _Process:
        returncode = 7
        pid = 123

        @staticmethod
        def poll():
            return 7

    gateway = CliGateway()
    gateway.runs["cli-run"] = (_Process(), ["session output"], [""])
    result = gateway.get_run("cli-run")
    assert result["status"] == "failed"
    assert result["output"] == "session output"
    assert result["evidence"]["exit_code"] == 7


def test_l0_access_delta_is_a_coarse_evidence_source():
    runner = Runner.__new__(Runner)
    runner._l0_has_git_change = lambda started: False
    runner._l0_snapshot_recall = lambda: 7
    runner._l0_snapshot_access = lambda: 105
    assert runner._l0_detect_change(0, 7, 100) == "access"
    runner._l0_snapshot_access = lambda: 103
    assert runner._l0_detect_change(0, 7, 100) == "none"


def test_prefetch_rotation_includes_external_world_escape_queries():
    import inspect
    source = inspect.getsource(Runner._prefetch_memory)
    assert "CrowdStrike" in source
    assert "数字货币" in source
