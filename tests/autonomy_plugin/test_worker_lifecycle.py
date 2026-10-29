import ast
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict


_SOURCE = Path(__file__).parents[2] / "plugins" / "seed_memory" / "__init__.py"
_TREE = ast.parse(_SOURCE.read_text(encoding="utf-8"))
_NODE = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "_quarantine_queue_file")
_NAMESPACE = {"Path": Path, "Dict": Dict, "Any": Any, "json": json, "os": __import__("os"), "time": time}
exec(compile(ast.Module(body=[_NODE], type_ignores=[]), str(_SOURCE), "exec"), _NAMESPACE)
_quarantine_queue_file = _NAMESPACE["_quarantine_queue_file"]


def test_poison_file_moves_to_failed_with_last_error(tmp_path):
    queue = tmp_path / "seed_queue"
    queue.mkdir()
    source = queue / "session_bad.json"
    source.write_text(json.dumps({"retries": 4}), encoding="utf-8")
    target = _quarantine_queue_file(
        source,
        queue,
        {"retries": 4, "last_error": "TimeoutExpired"},
    )
    assert target.parent == queue / "_failed"
    assert not source.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["last_error"] == "TimeoutExpired"


def test_systemd_run_missing_is_detectable_for_fallback(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("systemd-run")

    monkeypatch.setattr(subprocess, "run", missing)
    try:
        subprocess.run(["systemd-run", "--user"], check=True)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing systemd-run must select the foreground fallback")
