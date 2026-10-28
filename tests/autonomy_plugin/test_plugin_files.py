"""原子6 的验收测试：plugin.yaml 与 config.example.yaml 的 schema，以及插件目录 .py 的守卫标记。

没有 PyYAML 时相关用例自动 skip（只用标准库 + pytest 也能跑其余用例）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = ROOT / "plugins" / "autonomy"
PLUGIN_YAML = PLUGIN_DIR / "plugin.yaml"
CONFIG_EXAMPLE_YAML = PLUGIN_DIR / "config.example.yaml"


def _load_yaml():
    try:
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover - 本机无 PyYAML 时跳过
        pytest.skip("需要 PyYAML 才能解析 yaml 文件，未安装则跳过")
    return yaml


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _comment_above(text: str, key: str) -> bool:
    """该键的上一行（跳过空行）是不是中文 # 注释。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            j = i - 1
            while j >= 0 and not lines[j].strip():
                j -= 1
            if j < 0:
                return False
            prev = lines[j].strip()
            return prev.startswith("#") and _has_cjk(prev)
    return False


# ---------------------------------------------------------------- 文件存在性

def test_two_yaml_files_exist_and_are_utf8():
    for path in (PLUGIN_YAML, CONFIG_EXAMPLE_YAML):
        assert path.is_file(), f"缺少 {path}"
        text = path.read_text(encoding="utf-8")  # 必须是合法 UTF-8
        assert text.strip()


def test_budget_status_exposes_env_or_config_source(monkeypatch):
    spec = importlib.util.spec_from_file_location("autonomy_plugin_budget", PLUGIN_DIR / "budget.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    for key in ("AUTONOMY_DAILY_CALLS", "AUTONOMY_DAILY_TOKENS", "AUTONOMY_DAILY_PROMPT_TOKENS"):
        monkeypatch.delenv(key, raising=False)
    assert module.Budget(tokens=30000000).daily_tokens_source == "config"
    assert module.Budget().daily_tokens_source == "default"
    monkeypatch.setenv("AUTONOMY_DAILY_TOKENS", "30000000")
    budget = module.Budget()
    assert budget.daily_tokens_source == "env"
    assert budget.tokens == 30000000


# ---------------------------------------------------------------- plugin.yaml

def test_plugin_yaml_manifest():
    yaml = _load_yaml()
    data = yaml.safe_load(PLUGIN_YAML.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data["name"] == "autonomy"
    assert data["version"] == "0.1.0"
    assert data["kind"] == "standalone"
    assert isinstance(data["description"], str)
    assert data["description"] == "自主思考大脑插件"


def test_plugin_yaml_has_no_secrets():
    text = PLUGIN_YAML.read_text(encoding="utf-8")
    lowered = text.lower()
    for marker in ("api_key", "api-key", "token: sk-", "bearer "):
        assert marker not in lowered


# ------------------------------------------------- config.example.yaml

def test_config_example_yaml_values():
    yaml = _load_yaml()
    text = CONFIG_EXAMPLE_YAML.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert isinstance(data, dict) and set(data) == {"autonomy"}
    cfg = data["autonomy"]

    assert cfg["model"] is None
    assert cfg["memory_provider"] is None
    assert cfg["in_process_loop"] is False
    assert cfg["data_dir"] == "/var/lib/hermes/autonomy_data"
    assert cfg["thought_log"] == "/var/lib/hermes/autonomy_data/thoughts.log"
    assert cfg["quiet_s"] == 300

    limits = cfg["limits"]
    assert limits["daily_tokens"] == 2000000
    assert limits["circuit_threshold"] == 3
    assert limits["circuit_cooldown_s"] == 1800
    assert limits["memory_max"] == "1G"


def test_config_example_yaml_key_order_and_chinese_comments():
    yaml = _load_yaml()
    text = CONFIG_EXAMPLE_YAML.read_text(encoding="utf-8")
    cfg = yaml.safe_load(text)["autonomy"]
    assert list(cfg.keys()) == [
        "model",
        "memory_provider",
        "in_process_loop",
        "data_dir",
        "thought_log",
        "quiet_s",
        "limits",
    ]
    assert list(cfg["limits"].keys()) == ["daily_tokens", "circuit_threshold", "circuit_cooldown_s", "memory_max"]
    for key in ("model", "memory_provider", "in_process_loop", "data_dir", "thought_log", "quiet_s", "limits"):
        assert _comment_above(text, key), f"{key} 上方缺少中文注释"
    for key in ("daily_tokens", "circuit_threshold", "circuit_cooldown_s", "memory_max"):
        assert _comment_above(text, key), f"limits.{key} 上方缺少中文注释"


def test_config_example_has_no_real_keys():
    text = CONFIG_EXAMPLE_YAML.read_text(encoding="utf-8")
    lowered = text.lower()
    for marker in ("sk-", "api_key", "password", "bearer "):
        assert marker not in lowered


# ------------------------------------------------- 插件目录 .py 守卫标记

def test_plugin_dir_py_init_has_no_memory_provider_marker():
    py_files = sorted(PLUGIN_DIR.glob("*.py"))
    if not py_files:
        pytest.skip("插件目录里还没有 .py 文件，跳过")
    init_py = PLUGIN_DIR / "__init__.py"
    if not init_py.is_file():
        pytest.skip("插件目录里还没有 __init__.py，跳过")
    source = init_py.read_text(encoding="utf-8")
    assert "register_memory_provider" not in source
    assert "MemoryProvider" not in source


def test_own_atoms_are_importable_and_free_of_memory_api(tmp_path):
    loaded = {}
    for name in ("activity", "guard"):
        module_path = PLUGIN_DIR / f"{name}.py"
        if not module_path.is_file():
            pytest.skip(f"{name}.py 尚未创建，跳过")
        spec = importlib.util.spec_from_file_location(f"atom6_{name}", module_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded[name] = module
        source = module_path.read_text(encoding="utf-8")
        assert "register_memory_provider" not in source
        assert "MemoryProvider" not in source
        assert "openviking" not in source.lower()

    if "guard" in loaded:
        assert loaded["guard"].check_guard(0, 0, 0, 3, None, 1800, 1.0) == (True, "ok")
    if "activity" in loaded:
        activity = loaded["activity"]
        activity.touch_user_activity(tmp_path)
        assert (tmp_path / "LAST_USER_ACTIVITY").is_file()
        assert activity.is_user_recent(tmp_path, 300) is True
