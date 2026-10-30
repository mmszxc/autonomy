"""Contract tests for the xiaoai-derived seed_memory source bundle.

The source bundle is intentionally imported in a clean subprocess.  The main
pytest process also has the repository's legacy ``hermes`` package on sys.path;
using a fresh cwd and PYTHONPATH proves that the plugin-local seed_src files,
not that legacy package, implement the master behaviour.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "plugins" / "seed_memory" / "seed_src"


def test_master_seed_source_preserves_recent_causality_and_backlinks(tmp_path: Path):
    script = textwrap.dedent(
        """
        import json
        import os

        from hermes.emotion import tagger
        from hermes.memory import store
        from hermes.memory.schema import Emotion, Seed

        db_path = os.path.join(os.environ["TEST_TMP"], "seeds.db")
        store.configure(db_path)
        store.init_db()
        parent = Seed(
            summary="先前的共同决定",
            participants=["user"],
            emotion=Emotion(primary="calm", intensity=0.6),
        )
        assert store.save_seeds([parent]) == 1
        recent = store.list_recent_seeds(limit=10)
        assert any(row["id"] == parent.id for row in recent)

        captured = []
        tagger.embed = lambda _text: [0.1, 0.2]
        def llm(prompt):
            captured.append(prompt)
            return json.dumps([{
                "summary": "后续行动",
                "participants": ["user"],
                "emotion": {"primary": "hope", "intensity": 0.8},
                "caused_by": [parent.id],
                "led_to": [],
            }], ensure_ascii=False)

        children = tagger.extract_seeds(
            [{"role": "user", "content": "请继续执行"}],
            llm,
            persist=False,
            recent_seeds=recent,
        )
        assert len(children) == 1
        child = children[0]
        assert child.caused_by == [parent.id]
        assert parent.id in captured[0], "recent real seed IDs must be exposed to the tagger"

        assert store.save_seeds([child]) == 1
        assert store.backlink_led_to([child.id], {child.id: child.caused_by}) == 1
        rows = {seed.id: seed for seed in store.get_all_seeds()}
        assert rows[parent.id].led_to == [child.id]
        assert rows[child.id].caused_by == [parent.id]
        """
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT)
    env["TEST_TMP"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
