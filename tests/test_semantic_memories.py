"""Tests for semantic_memories activation:
  1. init_db creates semantic_memories table (empty DB)
  2. save_semantic_memory / get_all_semantic_memories round-trip
  3. merge=True reinforces existing row instead of inserting duplicate
  4. extract_seeds with semantic_distill=True writes semantic memories from seeds_planted
  5. extract_seeds with semantic_distill=False does NOT write semantic memories
  6. Idempotent: running init_db on existing DB with seeds rows does not corrupt data
"""
from __future__ import annotations

import os
import sys
import json
from pathlib import Path
import textwrap
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "plugins" / "seed_memory" / "seed_src"


def _run(script: str, tmp_path: Path) -> tuple[str, str, int]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT)
    env.pop("HERMES_SEED_DB_PATH", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout, result.stderr, result.returncode


# ---------------------------------------------------------------------------
# 1. init_db creates semantic_memories table
# ---------------------------------------------------------------------------

def test_init_db_creates_semantic_memories_table(tmp_path):
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os, sqlite3
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store
        store.init_db({str(db)!r})
        conn = sqlite3.connect({str(db)!r})
        tables = {{r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}}
        assert "semantic_memories" in tables, f"missing table, got {{tables}}"
        cols = {{r[1] for r in conn.execute("PRAGMA table_info(semantic_memories)").fetchall()}}
        required = {{"id","timestamp","category","statement","confidence","evidence_count","source_seeds"}}
        missing = required - cols
        assert not missing, f"missing columns: {{missing}}"
        print("OK")
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}\nstdout={stdout}"
    assert "OK" in stdout


# ---------------------------------------------------------------------------
# 2. save + get_all round-trip
# ---------------------------------------------------------------------------

def test_save_and_get_semantic_memories(tmp_path):
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store
        from hermes.memory.schema import SemanticMemory
        store.init_db({str(db)!r})
        mem = SemanticMemory(
            category="world_model",
            statement="好奇心是探索的源动力",
            confidence=0.7,
            evidence_count=1,
            source_seeds=["seed_fake"],
        )
        saved_id = store.save_semantic_memory(mem, merge=False)
        all_mems = store.get_all_semantic_memories()
        assert len(all_mems) == 1, f"expected 1 got {{len(all_mems)}}"
        assert all_mems[0].statement == "好奇心是探索的源动力"
        assert abs(all_mems[0].confidence - 0.7) < 0.01
        print("OK id=" + saved_id)
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}\nstdout={stdout}"
    assert "OK" in stdout


# ---------------------------------------------------------------------------
# 3. merge=True reinforces existing row
# ---------------------------------------------------------------------------

def test_save_semantic_memory_merge_reinforces(tmp_path):
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store
        from hermes.memory.schema import SemanticMemory
        store.init_db({str(db)!r})
        m1 = SemanticMemory(statement="共情是力量", confidence=0.5, evidence_count=1, source_seeds=["s1"])
        store.save_semantic_memory(m1, merge=True)
        m2 = SemanticMemory(statement="共情是力量", confidence=0.5, evidence_count=1, source_seeds=["s2"])
        store.save_semantic_memory(m2, merge=True)
        all_mems = store.get_all_semantic_memories()
        assert len(all_mems) == 1, f"merge failed: got {{len(all_mems)}} rows"
        assert all_mems[0].evidence_count == 2, f"expected evidence_count=2 got {{all_mems[0].evidence_count}}"
        print("OK evidence=" + str(all_mems[0].evidence_count))
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}\nstdout={stdout}"
    assert "OK" in stdout


# ---------------------------------------------------------------------------
# 4. extract_seeds with semantic_distill=True writes to semantic_memories
# ---------------------------------------------------------------------------

def test_extract_seeds_distills_seeds_planted(tmp_path):
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os, json
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store
        from hermes.emotion import tagger
        store.init_db({str(db)!r})

        llm_response = json.dumps([{{
            "summary": "与创造者深度交流，感受到被理解的共鸣",
            "participants": ["creator"],
            "emotion": {{"primary": "共鸣", "intensity": 0.85, "secondary": [], "note": "感受到彼此理解"}},
            "value_aligned": ["真诚沟通"],
            "value_conflicted": [],
            "seeds_planted": ["真诚沟通能够超越语言的隔阂", "被理解是深度连接的基础"],
            "caused_by": [],
            "led_to": [],
            "importance": 0.8,
            "shareable": 1,
        }}])

        seeds = tagger.extract_seeds(
            [{{"role": "user", "content": "你好"}}, {{"role": "hermes", "content": "你好"}}],
            llm_call=lambda _: llm_response,
            persist=True,
            session_id="test-session",
            semantic_distill=True,
        )
        assert len(seeds) == 1, f"expected 1 seed got {{len(seeds)}}"

        all_mems = store.get_all_semantic_memories()
        statements = {{m.statement for m in all_mems}}
        assert "真诚沟通能够超越语言的隔阂" in statements, f"missing statement, got {{statements}}"
        assert "被理解是深度连接的基础" in statements, f"missing statement, got {{statements}}"
        assert len(all_mems) == 2, f"expected 2 semantic memories, got {{len(all_mems)}}"
        for m in all_mems:
            assert seeds[0].id in m.source_seeds, f"source_seeds not linked"
        print("OK count=" + str(len(all_mems)))
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}\nstdout={stdout}"
    assert "OK" in stdout


# ---------------------------------------------------------------------------
# 5. extract_seeds with semantic_distill=False does NOT write
# ---------------------------------------------------------------------------

def test_extract_seeds_no_distill_when_disabled(tmp_path):
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os, json
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store
        from hermes.emotion import tagger
        store.init_db({str(db)!r})

        llm_response = json.dumps([{{
            "summary": "普通的一次对话",
            "participants": ["creator"],
            "emotion": {{"primary": "平静", "intensity": 0.3, "secondary": [], "note": ""}},
            "value_aligned": [],
            "value_conflicted": [],
            "seeds_planted": ["这是一条不应被提炼的结论"],
            "caused_by": [],
            "led_to": [],
            "importance": 0.3,
            "shareable": 1,
        }}])

        tagger.extract_seeds(
            [{{"role": "user", "content": "hi"}}, {{"role": "hermes", "content": "hi"}}],
            llm_call=lambda _: llm_response,
            persist=True,
            session_id="no-distill",
            semantic_distill=False,
        )
        all_mems = store.get_all_semantic_memories()
        assert len(all_mems) == 0, f"should be 0, got {{len(all_mems)}}"
        print("OK no_distill")
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}\nstdout={stdout}"
    assert "OK" in stdout


# ---------------------------------------------------------------------------
# 6. init_db is idempotent on existing DB (does not corrupt seeds rows)
# ---------------------------------------------------------------------------

def test_init_db_idempotent_on_existing_db(tmp_path):
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os, json
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store
        from hermes.memory.schema import Seed, Emotion

        # First init + insert a seed
        store.init_db({str(db)!r})
        seed = Seed(
            summary="测试种子",
            participants=["tester"],
            emotion=Emotion(primary="好奇", intensity=0.6),
            seeds_planted=["好奇心驱动探索"],
        )
        store.save_seed(seed)

        # Second init_db call must not corrupt existing data
        store.init_db({str(db)!r})

        seeds = store.get_all_seeds()
        assert len(seeds) == 1, f"expected 1 seed, got {{len(seeds)}}"
        assert seeds[0].summary == "测试种子"

        # semantic_memories table must still exist
        import sqlite3
        conn = sqlite3.connect({str(db)!r})
        tables = {{r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}}
        assert "semantic_memories" in tables
        print("OK seeds=" + str(len(seeds)))
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}\nstdout={stdout}"
    assert "OK" in stdout
