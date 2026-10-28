import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "plugins"))

from autonomy.prompt import build_tick_prompt, load_extra_rules


def test_empty_rules_add_no_section_or_internal_path():
    prompt = build_tick_prompt([], [], extra_rules=[])
    assert "【附加约束】" not in prompt
    assert "你的工作目录是" not in prompt
    assert "/opt/" not in prompt


def test_workspace_and_rules_are_rendered_in_order():
    prompt = build_tick_prompt(
        [],
        [],
        workspace="/home/example/workspace",
        extra_rules=["只读生产数据", "不要停止共享网关"],
    )
    assert "你的工作目录是 /home/example/workspace" in prompt
    assert "【附加约束】\n- 只读生产数据\n- 不要停止共享网关" in prompt
    assert prompt.index("只读生产数据") < prompt.index("不要停止共享网关")


def test_load_extra_rules_missing_and_invalid_utf8_degrade(tmp_path):
    assert load_extra_rules(tmp_path) == []
    (tmp_path / "extra_rules.txt").write_bytes(b"valid\n\xffinvalid")
    assert load_extra_rules(tmp_path) == []
    assert load_extra_rules(None) == []


def test_load_extra_rules_reads_nonempty_utf8_lines(tmp_path):
    (tmp_path / "extra_rules.txt").write_text(
        "  第一条  \n\n第二条\n", encoding="utf-8"
    )
    rules = load_extra_rules(tmp_path)
    assert rules == ["第一条", "第二条"]
    prompt = build_tick_prompt([], [], extra_rules=rules)
    assert "【附加约束】\n- 第一条\n- 第二条" in prompt
    assert "/opt/" not in prompt


def test_public_prompt_source_has_no_private_opt_path():
    source = (ROOT / "plugins" / "autonomy" / "prompt.py").read_text(
        encoding="utf-8"
    )
    assert "/opt/" not in source
