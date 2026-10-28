import json
from pathlib import Path

from plugins.autonomy.cli import cmd_log, parse_thought_entries


def _write(path: Path):
    path.write_text(
        "=== [2026-10-28T01:00:00+00:00] run_id=a status=ok mode=parallel model=qwen (prompt=10 completion=2 total=12) ===\n"
        "elapsed=3.5 prefetch count=2\nfirst thought\n\n"
        "=== [2026-10-28T02:00:00+00:00] run_id=b status=run_timeout mode=dialog_first model=qwen (prompt=20 completion=0 total=20) ===\n"
        "elapsed=1200 prefetch count=5\n结束原因：max_seconds\n" + "x" * 1000 + "\n",
        encoding="utf-8",
    )


def test_parse_multiple_entries_and_truncation(tmp_path, monkeypatch):
    path = tmp_path / "thoughts.log"
    _write(path)
    entries = parse_thought_entries(path)
    assert len(entries) == 2
    assert entries[1]["status"] == "run_timeout"
    (tmp_path / "config.yaml").write_text("autonomy:\n  thought_log: '%s'\n" % path.as_posix(), encoding="utf-8")
    result = cmd_log(tmp_path, count=1)
    assert result["count"] == 1
    assert len(result["entries"][0]["body"]) <= 600


def test_empty_log_and_json_shape(tmp_path, monkeypatch):
    path = tmp_path / "thoughts.log"
    path.write_text("", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("autonomy:\n  thought_log: '%s'\n" % path.as_posix(), encoding="utf-8")
    result = cmd_log(tmp_path, count=3, as_json=True)
    assert result["entries"] == []
