"""activity.py 的单元测试：只用 tmp_path + os.utime 控制时间，不用 sleep。"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "plugins" / "autonomy" / "activity.py"

sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("autonomy_activity_test", MODULE_PATH)
assert spec is not None and spec.loader is not None
activity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activity)


def _set_mtime_ns(path: Path, ns: int) -> int:
    """把 mtime 精确设定到纳秒，并回读实际生效的 st_mtime_ns。"""
    ts = ns / 1_000_000_000
    os.utime(path, (ts, ts))
    return path.stat().st_mtime_ns


# ---------------------------------------------------------------- 用户活动

def test_is_user_recent_when_file_missing(tmp_path):
    assert activity.is_user_recent(tmp_path, 300) is False


def test_is_user_recent_when_data_dir_missing(tmp_path):
    missing = tmp_path / "nope" / "deeper"
    assert activity.is_user_recent(missing, 300) is False
    assert not missing.exists()  # 只读查询不应创建目录


def test_touch_creates_dir_and_file_then_recent(tmp_path):
    data_dir = tmp_path / "autonomy_data" / "sub"  # 目录尚不存在
    activity.touch_user_activity(str(data_dir))  # 接受 str
    marker = data_dir / "LAST_USER_ACTIVITY"
    assert marker.is_file()
    assert activity.is_user_recent(data_dir, 300) is True  # now 缺省 = time.time()


def test_not_recent_after_quiet_window(tmp_path):
    data_dir = tmp_path / "d"
    activity.touch_user_activity(data_dir)
    now = time.time()
    mtime_ns = _set_mtime_ns(data_dir / "LAST_USER_ACTIVITY", int((now - 400) * 1e9))
    mtime = mtime_ns / 1_000_000_000
    assert activity.is_user_recent(data_dir, 300, now=mtime + 400.0) is False


def test_quiet_boundary_equal_is_false(tmp_path):
    data_dir = tmp_path / "d"
    activity.touch_user_activity(data_dir)
    mtime_ns = _set_mtime_ns(data_dir / "LAST_USER_ACTIVITY", int((time.time() - 999) * 1e9))
    mtime = mtime_ns / 1_000_000_000
    quiet = 300.0
    assert activity.is_user_recent(data_dir, quiet, now=mtime + quiet) is False  # 等于 -> False
    assert activity.is_user_recent(data_dir, quiet, now=mtime + quiet - 1.0) is True
    assert activity.is_user_recent(data_dir, quiet, now=mtime + quiet + 1.0) is False


def test_touch_updates_mtime(tmp_path):
    data_dir = tmp_path / "d"
    activity.touch_user_activity(data_dir)
    marker = data_dir / "LAST_USER_ACTIVITY"
    first_ns = _set_mtime_ns(marker, int((time.time() - 500) * 1e9))
    assert activity.is_user_recent(data_dir, 300) is False
    activity.touch_user_activity(str(data_dir))
    second_ns = marker.stat().st_mtime_ns
    assert second_ns > first_ns
    assert activity.is_user_recent(data_dir, 300) is True
    # 内容始终为空，mtime 就是唯一状态
    assert marker.read_bytes() == b""


def test_quiet_zero_never_recent(tmp_path):
    data_dir = tmp_path / "d"
    activity.touch_user_activity(data_dir)
    mtime_ns = _set_mtime_ns(data_dir / "LAST_USER_ACTIVITY", int(time.time() * 1e9))
    mtime = mtime_ns / 1_000_000_000
    assert activity.is_user_recent(data_dir, 0, now=mtime) is False


# ---------------------------------------------------------------- 抢占标记

def test_preempt_since_when_file_missing(tmp_path):
    assert activity.preempt_since(tmp_path, 0) is False


def test_preempt_since_true_for_recent_write(tmp_path):
    data_dir = tmp_path / "d"
    activity.write_preempt(str(data_dir))
    assert (data_dir / "PREEMPT_NOW").is_file()
    since_ns = (time.time() - 60) * 1_000_000_000
    assert activity.preempt_since(data_dir, int(since_ns)) is True


def test_preempt_since_false_for_older_marker(tmp_path):
    data_dir = tmp_path / "d"
    activity.write_preempt(data_dir)
    marker_ns = _set_mtime_ns(data_dir / "PREEMPT_NOW", int((time.time() - 600) * 1e9))
    since_ns = int((time.time() - 60) * 1e9)
    assert activity.preempt_since(data_dir, since_ns) is False
    # 恰好相等（>= 边界）算命中；更早的 since 也算命中
    assert activity.preempt_since(data_dir, marker_ns) is True
    assert activity.preempt_since(data_dir, marker_ns - 1) is True
    assert activity.preempt_since(data_dir, marker_ns + 1) is False


def test_write_preempt_updates_mtime(tmp_path):
    data_dir = tmp_path / "d"
    activity.write_preempt(data_dir)
    marker = data_dir / "PREEMPT_NOW"
    first_ns = _set_mtime_ns(marker, int((time.time() - 600) * 1e9))
    old_since = int((time.time() - 30) * 1e9)
    assert activity.preempt_since(data_dir, old_since) is False
    activity.write_preempt(str(data_dir))
    second_ns = marker.stat().st_mtime_ns
    assert second_ns > first_ns
    assert activity.preempt_since(data_dir, old_since) is True


def test_markers_are_side_by_side_and_independent(tmp_path):
    data_dir = tmp_path / "d"
    activity.touch_user_activity(data_dir)
    activity.write_preempt(data_dir)
    assert sorted(p.name for p in data_dir.iterdir()) == ["LAST_USER_ACTIVITY", "PREEMPT_NOW"]
    assert activity.LAST_USER_ACTIVITY == "LAST_USER_ACTIVITY"
    assert activity.PREEMPT_NOW == "PREEMPT_NOW"
    # 写抢占不应刷新用户活动时间戳，反之亦然
    act_ns = (data_dir / "LAST_USER_ACTIVITY").stat().st_mtime_ns
    os.utime(data_dir / "LAST_USER_ACTIVITY", (1000.0, 1000.0))
    activity.write_preempt(data_dir)
    assert (data_dir / "LAST_USER_ACTIVITY").stat().st_mtime_ns == int(1000 * 1e9)
    assert act_ns > 0
