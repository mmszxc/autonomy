import os
import subprocess
import sys
from pathlib import Path

import yaml

from plugins.autonomy.budget import Budget
from plugins.autonomy.circuit import Circuit
from plugins.autonomy.config import P0Config, load_p0_mode, resolve_p0_mode
from plugins.autonomy.db import AutonomyDB
from plugins.autonomy.runner import Runner


REPO_ROOT = Path(__file__).resolve().parents[1]


class _DB:
    pass


class _Policy:
    def __init__(self):
        self.calls = 0

    def rules_for_prompt(self, _goal):
        self.calls += 1
        return ["candidate must not leak"]


def _runner(mode):
    runner = Runner.__new__(Runner)
    runner.p0_mode = mode
    runner.circuit = Circuit()
    runner.policy = _Policy()
    return runner


def test_p0_mode_defaults_and_invalid_values_fail_closed():
    assert resolve_p0_mode({}) == ("observe_only", "missing")
    assert resolve_p0_mode({"autonomy": {"p0": {"mode": "bogus"}}}) == (
        "observe_only",
        "invalid",
    )
    assert resolve_p0_mode({"autonomy": {"p0": {"mode": "normal"}}}) == (
        "normal",
        "configured",
    )
    assert P0Config.from_mapping({}).mode == "observe_only"


def test_observe_only_is_higher_priority_than_mutable_circuit():
    runner = _runner("observe_only")
    runner.circuit.unfreeze_learning(True)
    assert runner._learning_allowed() is False
    assert runner._promotion_allowed() is False
    assert runner._policy_rules_for_prompt("goal") == []
    assert runner.policy.calls == 0


def test_normal_mode_still_uses_circuit_and_promoted_policy():
    runner = _runner("normal")
    assert runner._learning_allowed() is True
    assert runner._promotion_allowed() is True
    assert runner._policy_rules_for_prompt("goal") == ["candidate must not leak"]
    assert runner.policy.calls == 1


def test_observe_only_keeps_normal_action_and_l0_audit(tmp_path):
    db = AutonomyDB(tmp_path / "autonomy.db")
    circuit = Circuit()
    runner = Runner(
        db,
        Budget(),
        circuit,
        str(tmp_path / "STOP"),
        lambda: {
            "text": '{"thoughts":"observed and acted","proposals":[]}',
            "completion_tokens": 1,
            "prompt_tokens": 1,
        },
        lock_path=str(tmp_path / "tick.lock"),
        p0_mode="observe_only",
    )
    result = runner.tick()
    assert result["status"] == "ok"
    assert circuit.allow_normal() is True
    assert runner._learning_allowed() is False
    assert runner._l0_get_counter() >= 1
    assert db.c.execute("SELECT status FROM last_tick WHERE id=1").fetchone()[0] == "ok"


def test_two_fresh_timer_processes_keep_persisted_observe_only_gate(tmp_path):
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "config.yaml").write_text(
        yaml.safe_dump({"autonomy": {"p0": {"mode": "observe_only"}}}),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    command = [
        sys.executable,
        "-m",
        "plugins.autonomy.tick",
        "--dry-run",
        "--hermes-home",
        str(home),
    ]
    first = subprocess.run(command, cwd=REPO_ROOT, env=env, text=True, capture_output=True)
    second = subprocess.run(command, cwd=REPO_ROOT, env=env, text=True, capture_output=True)

    assert first.returncode == second.returncode == 0
    assert "p0_mode=observe_only" in first.stderr
    assert "p0_mode=observe_only" in second.stderr
    assert load_p0_mode(home) == ("observe_only", "configured")


def test_missing_mode_logs_fail_closed_in_fresh_process(tmp_path):
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "config.yaml").write_text("autonomy: {}\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "plugins.autonomy.tick", "--dry-run", "--hermes-home", str(home)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
    assert "p0_mode=observe_only" in result.stderr
    assert "reason=missing" in result.stderr
