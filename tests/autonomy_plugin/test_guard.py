"""guard.py 的单元测试：纯函数，直接喂数值，不碰文件系统。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins" / "autonomy" / "guard.py"

sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("autonomy_guard_test", MODULE_PATH)
assert spec is not None and spec.loader is not None
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

NOW = 1_000_000.0


def check(**kwargs):
    base = dict(
        tokens_used=1000,
        daily_tokens=1_000_000,
        failures=0,
        threshold=3,
        circuit_opened_at=None,
        cooldown_s=1800,
        now=NOW,
    )
    base.update(kwargs)
    return guard.check_guard(**base)


def test_state_constants():
    assert (guard.STATE_OK, guard.STATE_HALF_OPEN, guard.STATE_CIRCUIT_OPEN, guard.STATE_BUDGET) == (
        "ok",
        "half_open",
        "circuit_open",
        "budget",
    )


def test_normal():
    assert check() == (True, "ok")


def test_budget_exactly_used_up():
    assert check(tokens_used=1_000_000, daily_tokens=1_000_000) == (False, "budget")


def test_budget_exceeded():
    assert check(tokens_used=1_500_000, daily_tokens=1_000_000) == (False, "budget")


def test_budget_just_below():
    assert check(tokens_used=999_999, daily_tokens=1_000_000) == (True, "ok")


def test_daily_tokens_zero_means_unlimited():
    assert check(tokens_used=99_999_999, daily_tokens=0) == (True, "ok")


def test_negative_daily_tokens_means_unlimited():
    assert check(tokens_used=99_999_999, daily_tokens=-1) == (True, "ok")


def test_circuit_open_while_in_cooldown():
    assert check(failures=3, circuit_opened_at=NOW - 600) == (False, "circuit_open")


def test_circuit_open_cooldown_boundary_equal_is_over():
    # now - opened_at == cooldown_s -> 冷却已结束，进入 half_open
    assert check(failures=5, circuit_opened_at=NOW - 1800) == (True, "half_open")
    # 差一点点仍在冷却
    assert check(failures=5, circuit_opened_at=NOW - 1799.999) == (False, "circuit_open")


def test_circuit_open_in_cooldown_ignores_budget_order():
    # 冷却中优先于预算：即便预算已满也报 circuit_open
    assert check(failures=9, daily_tokens=100, tokens_used=100, circuit_opened_at=NOW - 10) == (
        False,
        "circuit_open",
    )


def test_half_open_after_cooldown():
    assert check(failures=3, circuit_opened_at=NOW - 3600) == (True, "half_open")


def test_half_open_but_budget_full():
    assert check(failures=4, tokens_used=2_000_000, daily_tokens=2_000_000, circuit_opened_at=NOW - 7200) == (
        False,
        "budget",
    )


def test_half_open_with_unlimited_budget():
    assert check(failures=4, daily_tokens=0, tokens_used=99, circuit_opened_at=NOW - 7200) == (True, "half_open")


def test_failures_below_threshold_ok():
    assert check(failures=2, threshold=3, circuit_opened_at=NOW - 5) == (True, "ok")


def test_failures_below_threshold_still_checks_budget():
    assert check(failures=0, tokens_used=1_000_000, daily_tokens=1_000_000) == (False, "budget")


def test_opened_at_none_is_not_tripped():
    # 失败够多但从未打开过熔断 -> 走预算/正常分支
    assert check(failures=99, threshold=3, circuit_opened_at=None) == (True, "ok")
    assert check(failures=99, threshold=3, circuit_opened_at=None, tokens_used=1_000_000) == (False, "budget")


def test_pure_function_no_file_access(tmp_path, monkeypatch):
    # 纯函数：不应读写任何文件
    (tmp_path / "LAST_USER_ACTIVITY").write_text("keep", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    assert check(failures=3, circuit_opened_at=NOW) == (False, "circuit_open")
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert guard.__file__ == str(MODULE_PATH)
