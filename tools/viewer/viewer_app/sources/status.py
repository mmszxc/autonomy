"""Read-only runtime telemetry for the HUD."""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from ..redact import safe_text
from .sqlite_ro import connect

DEFAULT_LIMITS = {"calls": 500, "tokens": 100_000_000, "prompt": 100_000_000}


def _limits(config_path: Path) -> dict[str, int]:
    limits = dict(DEFAULT_LIMITS)
    keys = {"daily_calls": "calls", "daily_tokens": "tokens", "daily_prompt_tokens": "prompt"}
    if config_path.is_file():
        try:
            for line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
                match = re.match(r"^\s+(daily_calls|daily_tokens|daily_prompt_tokens)\s*:\s*([0-9]+)", line)
                if match:
                    limits[keys[match.group(1)]] = int(match.group(2))
        except OSError:
            pass
    environment = {
        "calls": "AUTONOMY_DAILY_CALLS",
        "tokens": "AUTONOMY_DAILY_TOKENS",
        "prompt": "AUTONOMY_DAILY_PROMPT_TOKENS",
    }
    for key, variable in environment.items():
        value = os.getenv(variable, "")
        if value.isdigit():
            limits[key] = int(value)
    return limits


def unit_state(unit: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout.strip() or "inactive"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _autonomy(home: Path) -> tuple[dict[str, int], dict[str, Any], list[str]]:
    usage = {"calls": 0, "tokens": 0, "prompt": 0}
    circuit: dict[str, Any] = {"state": "unknown", "failures": 0, "blocked": False}
    errors: list[str] = []
    directory = home / "autonomy_data"
    database = next(
        (path for path in (directory / "autonomy.db", directory / "state.db") if path.is_file()),
        directory / "autonomy.db",
    )
    if not database.is_file():
        return usage, circuit, ["autonomy 状态库不存在"]
    try:
        with connect(database) as connection:
            day = dt.datetime.now(dt.timezone.utc).date().isoformat()
            row = connection.execute("SELECT calls,tokens,prompt FROM usage WHERE day=?", (day,)).fetchone()
            if row:
                usage = {"calls": int(row[0]), "tokens": int(row[1]), "prompt": int(row[2])}
            row = connection.execute("SELECT failures,state,blocked FROM circuit WHERE id=1").fetchone()
            if row:
                circuit = {"failures": int(row[0]), "state": safe_text(row[1]), "blocked": bool(row[2])}
    except (OSError, sqlite3.Error):
        errors.append("autonomy 状态读取失败")
    return usage, circuit, errors


def load(home: Path, profile: str) -> dict[str, Any]:
    usage, circuit, errors = _autonomy(home)
    seed_total = 0
    seed_db = home / "seed_data" / "seeds" / "seeds.db"
    if seed_db.is_file():
        try:
            with connect(seed_db) as connection:
                seed_total = int(connection.execute("SELECT COUNT(*) FROM seeds").fetchone()[0])
        except (OSError, sqlite3.Error):
            errors.append("种子总数读取失败")
    queue = home / "seed_data" / "seed_queue"
    try:
        backlog = sum(path.is_file() for path in queue.iterdir()) if queue.is_dir() else 0
    except OSError:
        backlog = 0
        errors.append("队列读取失败")
    limits = _limits(home / "config.yaml")
    return {
        "timer": safe_text(unit_state(f"{profile}-autonomy.timer")),
        "runner": safe_text(unit_state(f"{profile}-autonomy.service")),
        "budget": {key: {"used": usage[key], "limit": limits[key]} for key in usage},
        "circuit": circuit,
        "seed_total": seed_total,
        "queue_backlog": backlog,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "errors": errors,
    }
