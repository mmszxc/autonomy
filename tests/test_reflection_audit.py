from pathlib import Path
import os
import pytest

from autonomy.config import validate_reflection_profile
from autonomy.policy_reflect import extract_final_json, redact


def _tree(tmp_path, config="platform_toolsets:\n  cli: []\n"):
    root = tmp_path / ".hermes"
    source = root / "profiles" / "XiaoAI"
    reflect = root / "profiles" / "reflect"
    source.mkdir(parents=True)
    reflect.mkdir()
    (reflect / "config.yaml").write_text(config, encoding="utf-8")
    return root, source, reflect


@pytest.mark.parametrize("name", ["-bad", "../bad", "bad/name", "bad name", "."])
def test_profile_name_adversarial(tmp_path, name):
    root, _, _ = _tree(tmp_path)
    assert not validate_reflection_profile(root, "XiaoAI", name)[0]


def test_profile_name_source_comparison_casefold(tmp_path):
    root, _, _ = _tree(tmp_path)
    assert validate_reflection_profile(root, "XiaoAI", "xiaoai")[1] == "profile_not_isolated"


@pytest.mark.parametrize("extra,reason", [
    ("memory:\n  provider: seed_memory\n", "memory_configured"),
    ("context:\n  engine: token_budget\n", "context_engine_enabled"),
    ("context_engine: token_budget\n", "context_engine_enabled"),
])
def test_profile_rejects_memory_and_both_context_spellings(tmp_path, extra, reason):
    root, _, _ = _tree(tmp_path, "platform_toolsets:\n  cli: []\n" + extra)
    assert validate_reflection_profile(root, "XiaoAI", "reflect")[1] == reason


def test_default_root_profiles_path(tmp_path):
    root, _, reflect = _tree(tmp_path)
    ok, _, actual = validate_reflection_profile(root, "XiaoAI", "reflect")
    assert ok and actual.resolve() == reflect.resolve()


@pytest.mark.skipif(os.name == "nt" and not hasattr(os, "symlink"), reason="symlink unavailable")
def test_profile_rejects_config_and_dangling_plugin_symlinks(tmp_path):
    root, _, reflect = _tree(tmp_path)
    cfg = reflect / "config.yaml"
    target = reflect / "real.yaml"
    cfg.replace(target)
    try:
        cfg.symlink_to(target)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    assert validate_reflection_profile(root, "XiaoAI", "reflect")[1] == "config_symlink"
    cfg.unlink()
    target.replace(cfg)
    plugins = reflect / "plugins"
    plugins.symlink_to(reflect / "missing", target_is_directory=True)
    assert validate_reflection_profile(root, "XiaoAI", "reflect")[1] == "plugins_symlink"


def test_extract_json_skips_nested_and_keeps_fence_inside_string():
    root = '{"condition":"outer","action":{"condition":"inner","action":"x","tool_family":"bad"},"tool_family":"files"}'
    assert extract_final_json(root)["condition"] == "outer"
    fenced = '```json\n{"condition":"c ``` x","action":"a","tool_family":"files"}\n```'
    assert extract_final_json(fenced)["condition"] == "c ``` x"
    assert extract_final_json(root + '\n{"condition":"incomplete"}')["condition"] == "outer"


def test_redact_json_url_private_key_and_bearer():
    raw = '{"password":"p@ss", "api_key": "sk-secretvalue123456", "token":"abc"} https://user:pass@example/a Authorization: Bearer abc.def-123\n-----BEGIN PRIVATE KEY-----\nSECRET\n-----END PRIVATE KEY-----'
    cleaned = redact(raw)
    for secret in ("p@ss", "secretvalue", '"abc"', "user:pass", "abc.def-123", "SECRET"):
        assert secret not in cleaned
    assert cleaned.count("[REDACTED]") >= 4
