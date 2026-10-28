"""hermes_home/plugins/autonomy/controls.py 的测试（pytest, tmp_path）。"""

import importlib.util
from pathlib import Path

# 直接按文件路径加载被测模块：插件包的 __init__.py 由别的任务维护，
# 它正在改动时会让 import 整包失败，原子测试应与它解耦。
PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "autonomy"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"autonomy_atom_{name}", PLUGIN_DIR / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


controls = _load("controls")

STOP_NAME = controls.STOP_FILENAME


def test_stop_creates_marker(tmp_path):
    marker = controls.stop(tmp_path)
    assert isinstance(marker, Path)
    assert marker == tmp_path / STOP_NAME
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8") == ""


def test_stop_is_idempotent(tmp_path):
    first = controls.stop(tmp_path)
    second = controls.stop(tmp_path)
    controls.stop(tmp_path)
    assert first == second
    assert second.is_file()
    assert controls.is_stopped(tmp_path) is True
    # 重复 stop 不会产生第二个文件
    assert sorted(p.name for p in tmp_path.iterdir()) == [STOP_NAME]


def test_resume_deletes_marker_and_returns_true(tmp_path):
    marker = controls.stop(tmp_path)
    assert controls.resume(tmp_path) is True
    assert not marker.exists()
    assert controls.is_stopped(tmp_path) is False


def test_resume_without_marker_returns_false(tmp_path):
    assert controls.is_stopped(tmp_path) is False
    assert controls.resume(tmp_path) is False
    # 不该顺手创建任何东西
    assert list(tmp_path.iterdir()) == []


def test_is_stopped_reflects_state_before_and_after(tmp_path):
    assert controls.is_stopped(tmp_path) is False
    controls.stop(tmp_path)
    assert controls.is_stopped(tmp_path) is True
    controls.resume(tmp_path)
    assert controls.is_stopped(tmp_path) is False


def test_stop_creates_missing_data_dir(tmp_path):
    nested = tmp_path / "nested" / "deep"
    assert not nested.exists()
    marker = controls.stop(nested)
    assert nested.is_dir()
    assert marker == nested / STOP_NAME
    assert marker.is_file()


def test_is_stopped_when_data_dir_missing(tmp_path):
    missing = tmp_path / "does" / "not" / "exist"
    assert controls.is_stopped(missing) is False
    assert controls.resume(missing) is False
    assert not missing.exists()


def test_data_dir_accepts_str_and_path(tmp_path):
    as_str = str(tmp_path)
    marker = controls.stop(as_str)
    assert marker == tmp_path / STOP_NAME
    assert controls.is_stopped(as_str) is True
    assert controls.is_stopped(tmp_path) is True
    assert controls.resume(as_str) is True
    assert controls.is_stopped(tmp_path) is False


def test_resume_twice(tmp_path):
    controls.stop(tmp_path)
    assert controls.resume(tmp_path) is True
    assert controls.resume(tmp_path) is False
