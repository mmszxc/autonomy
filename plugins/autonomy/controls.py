"""自主思考开关：STOP_AUTONOMY 标记文件。

约定：停止标记固定叫 STOP_AUTONOMY，放在 data_dir 下。
文件存在 = 自主思考被停止；删除 = 恢复。只用 Python 标准库，兼容 Python 3.10。
data_dir 参数接受 str 或 pathlib.Path。
"""

from __future__ import annotations

from pathlib import Path

# 停止标记文件名（与 autonomy 侧使用的停止文件同名）
STOP_FILENAME = "STOP_AUTONOMY"


def _marker_path(data_dir) -> Path:
    """把 data_dir 归一化成 Path，并返回停止标记文件的路径。"""
    return Path(data_dir) / STOP_FILENAME


def stop(data_dir) -> Path:
    """停止自主思考：确保 data_dir 存在，创建 STOP_AUTONOMY。

    已存在时不报错（touch 不会清空已有内容，只刷新 mtime）。
    返回标记文件的路径。
    """
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / STOP_FILENAME
    marker.touch()
    return marker


def resume(data_dir) -> bool:
    """恢复自主思考：标记文件存在则删除并返回 True，不存在返回 False。

    不会因为文件不存在或目录不存在而抛异常。
    """
    marker = _marker_path(data_dir)
    try:
        marker.unlink()
    except FileNotFoundError:
        return False
    return True


def is_stopped(data_dir) -> bool:
    """标记文件是否存在。目录不存在时返回 False，不报错。"""
    return _marker_path(data_dir).is_file()
