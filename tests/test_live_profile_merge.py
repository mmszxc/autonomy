import sqlite3

from autonomy.runner import Runner


def _seed_db(home):
    seed_dir = home / "seed_data" / "seeds"
    seed_dir.mkdir(parents=True)
    conn = sqlite3.connect(seed_dir / "seeds.db")
    for table in ("semantic_memories", "program_memories", "seeds"):
        conn.execute(
            f"CREATE TABLE {table}(id TEXT PRIMARY KEY, access_count INTEGER, "
            "last_accessed TEXT)"
        )
    conn.execute("INSERT INTO seeds VALUES('s1', 0, NULL)")
    conn.commit()
    conn.close()


def test_xiaoai_dormant_flag_refresh_is_enabled_by_loader(tmp_path):
    prompt = tmp_path / "plugins" / "autonomy" / "prompt.py"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("def load_dormant_flag():\n    pass\n", encoding="utf-8")
    _seed_db(tmp_path)
    runner = Runner.__new__(Runner)
    runner.hermes_home = tmp_path

    assert runner._refresh_dormant_flag() is True
    flag = (tmp_path / "seed_data" / "seeds" / "dormant_flag").read_text()
    assert "invalid=0,0,1 total=1" in flag


def test_xiaozhi_dormant_flag_refresh_stays_disabled_without_loader(tmp_path):
    prompt = tmp_path / "plugins" / "autonomy" / "prompt.py"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("def build_tick_prompt():\n    pass\n", encoding="utf-8")
    _seed_db(tmp_path)
    runner = Runner.__new__(Runner)
    runner.hermes_home = tmp_path

    assert runner._refresh_dormant_flag() is False
    assert not (tmp_path / "seed_data" / "seeds" / "dormant_flag").exists()
