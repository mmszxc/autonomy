from pathlib import Path
import sqlite3

from autonomy.runner import Runner


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "plugins" / "autonomy" / "runner.py"
FIXTURES = Path(__file__).with_name("fixtures")


def _lf(path):
    return path.read_text(encoding="utf-8").replace("\r", "")


def test_d1600_seed_reference_block_matches_live_fixture_byte_for_byte():
    source = _lf(RUNNER)
    expected = _lf(FIXTURES / "runner_d1600_block.py.txt")
    start = source.index('                has_recall = "recall_count" in columns\n')
    assert source[start:start + len(expected)] == expected


def test_d1420_refresh_method_matches_live_fixture_byte_for_byte():
    source = _lf(RUNNER)
    expected = _lf(FIXTURES / "runner_d1420_method.py.txt")
    start = source.index("    def _refresh_dormant_flag(self):\n")
    assert source[start:start + len(expected)] == expected


class _EventDB:
    def __init__(self):
        self.events = []

    def append_event(self, **event):
        self.events.append(event)


def _runner(home):
    runner = Runner.__new__(Runner)
    runner.hermes_home = home
    runner.db = _EventDB()
    return runner


def test_xiaoai_gate_executes_when_prompt_has_loader(tmp_path):
    prompt = tmp_path / "plugins" / "autonomy" / "prompt.py"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("def load_dormant_flag():\n    pass\n", encoding="utf-8")
    runner = _runner(tmp_path)
    assert runner._dormant_flag_enabled("run-xiaoai") is True
    assert runner.db.events == []


def test_xiaozhi_gate_skips_when_readable_prompt_has_no_loader(tmp_path):
    prompt = tmp_path / "plugins" / "autonomy" / "prompt.py"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("def build_tick_prompt():\n    pass\n", encoding="utf-8")
    runner = _runner(tmp_path)
    assert runner._dormant_flag_enabled("run-xiaozhi") is False
    assert runner.db.events == []


def test_gate_read_failure_executes_and_records_error(tmp_path):
    runner = _runner(tmp_path)
    assert runner._dormant_flag_enabled("run-missing") is True
    assert runner.db.events == [{
        "run_id": "run-missing",
        "kind": "dormant_flag_gate_error",
        "exit_reason": "FileNotFoundError",
        "summary": runner.db.events[0]["summary"],
    }]


def test_original_refresh_writes_live_counter_format(tmp_path):
    seed_dir = tmp_path / "seed_data" / "seeds"
    seed_dir.mkdir(parents=True)
    conn = sqlite3.connect(seed_dir / "seeds.db")
    conn.execute("CREATE TABLE seeds(id TEXT, access_count INTEGER, last_accessed TEXT)")
    conn.executemany("INSERT INTO seeds VALUES(?,?,?)", [
        ("s1", 0, None), ("s2", 1, None), ("s3", 0, "now")
    ])
    conn.commit()
    conn.close()
    runner = _runner(tmp_path)
    runner._refresh_dormant_flag()
    text = (seed_dir / "dormant_flag").read_text(encoding="utf-8")
    assert text.startswith("invalid=0,2,2 total=3 ts=")
