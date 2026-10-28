"""Read-only thinking log file manager for observing autonomous thoughts without Web UI."""
from __future__ import annotations

import datetime
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Dict, Optional


def resolve_thought_log_path(
    thought_log_path: Optional[str] = None,
    lock_path: Optional[str] = None,
    stop_file: Optional[str] = None,
) -> Path:
    if thought_log_path:
        return Path(thought_log_path)
    env_path = os.environ.get("AUTONOMY_THOUGHT_LOG")
    if env_path:
        return Path(env_path)
    if lock_path:
        return Path(lock_path).resolve().parent / "thoughts.log"
    if stop_file:
        return Path(stop_file).resolve().parent / "autonomy_data" / "thoughts.log"
    return Path("/var/lib/hermes/autonomy_data/thoughts.log")


def append_thought_log(
    log_path: str | Path,
    *,
    run_id: str,
    raw_text: str,
    summary: Optional[Dict[str, Any]] = None,
    mode: str = "dialog_first",
    model: str = "",
    usage: Optional[Dict[str, Any]] = None,
    status: str = "ok",
    ts: Optional[float] = None,
) -> Path:
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.fromtimestamp(
        ts if ts is not None else time.time(), datetime.timezone.utc
    ).isoformat()

    usage_info = usage or {}
    tokens_str = (
        f"prompt={usage_info.get('prompt_tokens', 0)} "
        f"completion={usage_info.get('completion_tokens', 0)} "
        f"total={usage_info.get('total_tokens', int(usage_info.get('prompt_tokens', 0)) + int(usage_info.get('completion_tokens', 0)))}"
    )

    header = (
        f"=== [{timestamp}] run_id={run_id} status={status} mode={mode}"
        + (f" model={model}" if model else "")
        + f" ({tokens_str}) ==="
    )
    body_lines = [header]
    text = (raw_text or "").strip()
    if text:
        body_lines.append(text)
    elif summary:
        body_lines.append(json.dumps(summary, ensure_ascii=False))
    body_lines.append("")
    entry = "\n".join(body_lines) + "\n"

    # Unlock read-only attribute temporarily so the plugin can append, then restore 0o444 (read-only).
    if path.exists():
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(entry)
        fh.flush()

    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass

    return path


def read_thought_log(log_path: str | Path, max_chars: int = 8000) -> str:
    path = Path(log_path)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if max_chars and len(text) > max_chars:
        return text[-max_chars:]
    return text
