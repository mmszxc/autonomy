"""用户活动与抢占标记文件（autonomy 插件的原子构件）。

两个标记文件都直接放在 data_dir 下，文件名固定：

- ``LAST_USER_ACTIVITY``：mtime = 最近一次用户活动的时间（谁在说话）。
- ``PREEMPT_NOW``：mtime = 最近一次请求「立刻暂停自主思考」的时间。

只依赖 Python 标准库，兼容 Python 3.10。不联网、不读配置、不碰其他文件。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, os.PathLike]

#: 用户活动标记文件名（固定）
LAST_USER_ACTIVITY = "LAST_USER_ACTIVITY"
#: 抢占请求标记文件名（固定）
PREEMPT_NOW = "PREEMPT_NOW"


def _dir(data_dir: PathLike) -> Path:
    """data_dir 可以是 str 或 Path，统一成 Path。"""
    return Path(data_dir)


def _activity_file(data_dir: PathLike) -> Path:
    return _dir(data_dir) / LAST_USER_ACTIVITY


def _preempt_file(data_dir: PathLike) -> Path:
    return _dir(data_dir) / PREEMPT_NOW


def _touch_now(path: Path) -> None:
    """创建文件（若不存在）并把 mtime 刷成当前时间；不覆盖已有内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    now = time.time()
    os.utime(path, (now, now))


def touch_user_activity(data_dir: PathLike) -> None:
    """记录「用户刚刚活动」：创建 data_dir，创建或刷新 LAST_USER_ACTIVITY 的 mtime。"""
    _touch_now(_activity_file(data_dir))


def write_preempt(data_dir: PathLike) -> None:
    """发出抢占信号：创建 data_dir，创建或刷新 PREEMPT_NOW 的 mtime。"""
    _touch_now(_preempt_file(data_dir))


def is_user_recent(data_dir: PathLike, quiet_s: float, now: Optional[float] = None) -> bool:
    """用户活动是否仍在静默窗口内。

    LAST_USER_ACTIVITY 存在且 (now - mtime) < quiet_s 时为 True；
    文件不存在为 False；恰好等于 quiet_s 视为已经不 recent。
    """
    try:
        mtime = _activity_file(data_dir).stat().st_mtime
    except OSError:
        return False
    if now is None:
        now = time.time()
    return (now - mtime) < float(quiet_s)


def preempt_since(data_dir: PathLike, since_ns: int) -> bool:
    """PREEMPT_NOW 是否在 since_ns（纳秒）之后被写过。

    用 st_mtime_ns 与 since_ns 比较，等号算命中；文件不存在为 False。
    """
    try:
        mtime_ns = _preempt_file(data_dir).stat().st_mtime_ns
    except OSError:
        return False
    return mtime_ns >= int(since_ns)
