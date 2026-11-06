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


def test_receipted_correct_no_action_is_recognised():
    runner = Runner.__new__(Runner)
    runner.db = _DB([(1,)])
    assert runner._has_correct_no_action_receipt("run") is True
