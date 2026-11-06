import inspect
import json
from pathlib import Path

from plugins.autonomy import policy, policy_reflect


def test_candidate_is_not_rendered_or_written_to_production_rules(tmp_path):
    data = tmp_path / "autonomy_data"
    result = policy.add_rule(data, "candidate-1", "do not enter production")
    assert result["ok"]
    assert policy.rules_for_prompt(data) == []
    assert not (data / "policy" / "active.json").exists()
    stored = json.loads((data / "policy" / "candidates.json").read_text(encoding="utf-8"))
    assert stored[0]["status"] == "candidate"


def test_reflection_core_is_explicitly_candidate_only_offline_batch():
    source = inspect.getsource(policy_reflect._reflect_core)
    assert "P.load_candidate_rules(data_dir)" in source
    assert "P.load_active_rules(data_dir)" not in source


def test_only_approved_or_promoted_active_rules_are_rendered(tmp_path):
    data = tmp_path / "autonomy_data"
    policy.save_active_rules(data, [
        {"id": "candidate", "text": "unsafe", "status": "candidate"},
        {"id": "approved", "text": "approved text", "status": "approved"},
        {"id": "promoted", "text": "promoted text", "status": "promoted"},
    ])
    assert policy.rules_for_prompt(data) == [
        "[policy:approved] approved text", "[policy:promoted] promoted text"
    ]
