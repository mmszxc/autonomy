"""hermes_home/plugins/autonomy/mode.py 的测试。"""

import importlib.util
from pathlib import Path

# 直接按文件路径加载被测模块，避免依赖插件包 __init__.py（由其它任务维护，改动中）
PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "autonomy"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"autonomy_atom_{name}", PLUGIN_DIR / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mode = _load("mode")
MODE_DIALOG_FIRST = mode.MODE_DIALOG_FIRST
MODE_PARALLEL = mode.MODE_PARALLEL
decide_mode = mode.decide_mode

MAIN = "qwen38-flash-v100"


def test_constants():
    assert MODE_PARALLEL == "parallel"
    assert MODE_DIALOG_FIRST == "dialog_first"


def test_autonomy_none_is_dialog_first():
    assert decide_mode(MAIN, None) == MODE_DIALOG_FIRST


def test_autonomy_empty_is_dialog_first():
    assert decide_mode(MAIN, "") == MODE_DIALOG_FIRST


def test_autonomy_whitespace_only_is_dialog_first():
    assert decide_mode(MAIN, "   ") == MODE_DIALOG_FIRST
    assert decide_mode(MAIN, "\t \n") == MODE_DIALOG_FIRST


def test_identical_models_is_dialog_first():
    assert decide_mode(MAIN, MAIN) == MODE_DIALOG_FIRST


def test_case_difference_is_dialog_first():
    assert decide_mode("Qwen38-Flash-V100", "qwen38-flash-v100") == MODE_DIALOG_FIRST
    assert decide_mode("qwen38-flash-v100", "QWEN38-FLASH-V100") == MODE_DIALOG_FIRST


def test_surrounding_whitespace_is_dialog_first():
    assert decide_mode("  qwen38-flash-v100  ", "qwen38-flash-v100") == MODE_DIALOG_FIRST
    assert decide_mode("qwen38-flash-v100", "\n qwen38-flash-v100 \t") == MODE_DIALOG_FIRST
    assert decide_mode(" qwen38-flash-v100 ", " Qwen38-Flash-V100 ") == MODE_DIALOG_FIRST


def test_genuinely_different_models_are_parallel():
    assert decide_mode("qwen38-flash-v100", "qwen38-flash") == MODE_PARALLEL
    assert decide_mode("qwen38-flash", "qwen38-flash-v100") == MODE_PARALLEL
    assert decide_mode(MAIN, "deepseek-r1") == MODE_PARALLEL


def test_main_none_always_dialog_first():
    assert decide_mode(None, None) == MODE_DIALOG_FIRST
    assert decide_mode(None, "qwen38-flash") == MODE_DIALOG_FIRST


def test_main_empty_always_dialog_first():
    assert decide_mode("", "qwen38-flash") == MODE_DIALOG_FIRST
    assert decide_mode("   ", "qwen38-flash") == MODE_DIALOG_FIRST
    assert decide_mode("", "") == MODE_DIALOG_FIRST
    assert decide_mode("  ", "\t") == MODE_DIALOG_FIRST


def test_return_value_is_one_of_two_constants():
    for main in (MAIN, None, "", "  ", "other-model"):
        for autonomy in (MAIN, "other-model", None, "", "   "):
            mode = decide_mode(main, autonomy)
            assert mode in (MODE_PARALLEL, MODE_DIALOG_FIRST)
            assert isinstance(mode, str)
