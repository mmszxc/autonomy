#!/usr/bin/env python3
"""Read-only web viewer for Hermes autonomy thoughts and seed memory.

The program intentionally uses only the Python standard library.  It never
imports either plugin, opens SQLite databases with ``mode=ro``, and never
creates or mutates files under the selected Hermes profile.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urlsplit

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DEFAULT_PROFILE = "xiaozhi"
DEFAULT_LIMITS = {"calls": 500, "tokens": 100_000_000, "prompt": 100_000_000}
MAX_THOUGHT_BYTES = 4 * 1024 * 1024
MAX_BODY_CHARS = 200_000

_THOUGHT_HEADER = re.compile(
    r"^=== \[(?P<ts>[^]]+)\] run_id=(?P<run_id>\S+) status=(?P<status>\S+)"
    r"(?: mode=(?P<mode>\S+))?(?: model=(?P<model>\S+))?"
    r" \((?P<tokens>[^)]*)\) ===$"
)
_REDACTIONS = (
    (re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}"), "sk-***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}"), "Bearer ***"),
    (
        re.compile(r"(?i)(\bapi[_-]?key\s*[:=]\s*)[\"']?[^\s,;\"']{4,}[\"']?"),
        r"\1[已打码]",
    ),
    (
        re.compile(r"(?i)(\b(?:access[_-]?token|secret)\s*[:=]\s*)[\"']?[^\s,;\"']{4,}[\"']?"),
        r"\1[已打码]",
    ),
)


def redact_text(value: Any) -> str:
    """Mask common credential shapes without attempting to identify real keys."""
    text = "" if value is None else str(value)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def safe_text(value: Any) -> str:
    """Return redacted HTML text suitable for JSON consumed by the bundled UI."""
    return html.escape(redact_text(value), quote=True)


def _read_tail(path: Path, max_bytes: int = MAX_THOUGHT_BYTES) -> str:
    if not path.is_file() or path.stat().st_size == 0:
        return ""
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - max_bytes))
        text = handle.read().decode("utf-8", errors="replace")
    if size > max_bytes:
        start = text.find("=== [")
        text = text[start:] if start >= 0 else ""
    return text


def _finish_thought(entry: dict[str, Any]) -> dict[str, Any]:
    body = "\n".join(entry.pop("_body", [])).strip()
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n……[查看器已截断]"
    elapsed = re.search(r"elapsed\s*[=:]\s*([0-9.]+)", body, re.I)
    prefetch = re.search(
        r"(?:本轮预取记忆|预取记忆|prefetch(?:ed)?(?:_count)?)[^\n]{0,48}?"
        r"(?:count\s*[=: ]\s*|[=: ]\s*)([0-9]+)",
        body,
        re.I,
    )
    tool_lines = [
        line.strip()
        for line in body.splitlines()
        if re.search(r"tool|工具|seed_(?:search|add|update)|写入|文件", line, re.I)
    ][:100]
    summary = ""
    for marker in ("结论", "下一步", "未完成", "【结束原因】"):
        position = body.find(marker)
        if position >= 0:
            summary = body[position : position + 500].strip()
            break
    if not summary:
        nonempty = [line.strip() for line in body.splitlines() if line.strip()]
        summary = (nonempty[-1] if nonempty else "")[:500]
    entry.update(
        {
            "elapsed": float(elapsed.group(1)) if elapsed else None,
            "prefetch_count": int(prefetch.group(1)) if prefetch else None,
            "summary": safe_text(summary),
            "body": safe_text(body),
            "tools": [safe_text(line) for line in tool_lines],
        }
    )
    for key in ("ts", "run_id", "status", "mode", "model"):
        entry[key] = safe_text(entry.get(key) or "")
    return entry


def parse_thought_text(text: str) -> list[dict[str, Any]]:
    """Parse the append-only thought-log format used by autonomy."""
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        match = _THOUGHT_HEADER.match(line.strip())
        if match:
            if current is not None:
                entries.append(_finish_thought(current))
            current = match.groupdict()
            current["tokens"] = {
                key: int(value)
                for key, value in re.findall(
                    r"(prompt|completion|total)=([0-9]+)", match.group("tokens") or ""
                )
            }
            current["_body"] = []
        elif current is not None:
            current["_body"].append(line)
    if current is not None:
        entries.append(_finish_thought(current))
    return entries


def load_thoughts(log_path: Path, count: int = 20) -> list[dict[str, Any]]:
    """Read rotations oldest-first, then return the newest requested entries."""
    count = max(1, min(int(count), 100))
    paths = [Path(f"{log_path}.{number}") for number in (3, 2, 1)] + [log_path]
    entries: list[dict[str, Any]] = []
    for path in paths:
        text = _read_tail(path)
        if text:
            entries.extend(parse_thought_text(text))
    return list(reversed(entries[-count:]))


def _readonly_connection(path: Path) -> sqlite3.Connection:
    absolute = path.expanduser().resolve()
    uri = "file:" + quote(absolute.as_posix(), safe="/:~") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=2000")
    return connection


def _json_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return [str(value)]
    if isinstance(parsed, list):
        return [str(item) for item in parsed]
    return [str(parsed)]


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def load_seeds(
    db_path: Path, limit: int = 20, query: str = "", offset: int = 0
) -> dict[str, Any]:
    """Search and page seed rows using an immutable read-only connection."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    if not db_path.is_file():
        return {"items": [], "total": 0, "limit": limit, "offset": offset, "error": "种子库不存在"}
    try:
        with _readonly_connection(db_path) as connection:
            columns = _table_columns(connection, "seeds")
            if not columns:
                return {"items": [], "total": 0, "limit": limit, "offset": offset, "error": "种子表不存在"}
            wanted = [
                name
                for name in (
                    "id",
                    "timestamp",
                    "created_at",
                    "summary",
                    "emotion_primary",
                    "emotion_intensity",
                    "importance",
                    "caused_by",
                    "seeds_planted",
                )
                if name in columns
            ]
            if "summary" not in columns:
                return {"items": [], "total": 0, "limit": limit, "offset": offset, "error": "种子表缺少 summary"}
            search_columns = [name for name in ("summary", "emotion_primary", "caused_by", "seeds_planted") if name in columns]
            where = ""
            parameters: list[Any] = []
            query = query.strip()[:200]
            if query and search_columns:
                escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                where = " WHERE " + " OR ".join(f"{name} LIKE ? ESCAPE '\\'" for name in search_columns)
                parameters = [f"%{escaped}%"] * len(search_columns)
            total = int(connection.execute("SELECT COUNT(*) FROM seeds" + where, parameters).fetchone()[0])
            time_column = "timestamp" if "timestamp" in columns else ("created_at" if "created_at" in columns else "rowid")
            sql = (
                f"SELECT {', '.join(wanted)} FROM seeds{where} "
                f"ORDER BY {time_column} DESC LIMIT ? OFFSET ?"
            )
            rows = connection.execute(sql, [*parameters, limit, offset]).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return {"items": [], "total": 0, "limit": limit, "offset": offset, "error": safe_text(exc)}

    items: list[dict[str, Any]] = []
    for row in rows:
        raw = dict(row)
        timestamp = raw.get("timestamp") or raw.get("created_at") or ""
        items.append(
            {
                "id": safe_text(raw.get("id") or ""),
                "timestamp": safe_text(timestamp),
                "summary": safe_text(raw.get("summary") or ""),
                "emotion": safe_text(raw.get("emotion_primary") or ""),
                "emotion_intensity": raw.get("emotion_intensity"),
                "importance": raw.get("importance"),
                "caused_by": [safe_text(item) for item in _json_list(raw.get("caused_by"))],
                "seeds_planted": [safe_text(item) for item in _json_list(raw.get("seeds_planted"))],
            }
        )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if offset + limit < total else None,
    }


def _read_budget_limits(config_path: Path) -> dict[str, int]:
    """Read only the three known scalar keys without requiring a YAML package."""
    limits = dict(DEFAULT_LIMITS)
    environment = {
        "calls": "AUTONOMY_DAILY_CALLS",
        "tokens": "AUTONOMY_DAILY_TOKENS",
        "prompt": "AUTONOMY_DAILY_PROMPT_TOKENS",
    }
    yaml_keys = {"daily_calls": "calls", "daily_tokens": "tokens", "daily_prompt_tokens": "prompt"}
    if config_path.is_file():
        try:
            for line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
                match = re.match(r"^\s+(daily_calls|daily_tokens|daily_prompt_tokens)\s*:\s*([0-9]+)\s*(?:#.*)?$", line)
                if match:
                    limits[yaml_keys[match.group(1)]] = int(match.group(2))
        except OSError:
            pass
    for key, variable in environment.items():
        raw = os.environ.get(variable)
        if raw and raw.isdigit():
            limits[key] = int(raw)
    return limits


def _unit_status(unit: str) -> str:
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
        value = result.stdout.strip()
        return value if value else "inactive"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _timer_status(profile: str) -> str:
    return _unit_status(f"{profile}-autonomy.timer")


def _runner_status(profile: str) -> str:
    return _unit_status(f"{profile}-autonomy.service")


def load_status(profile_home: Path, profile: str) -> dict[str, Any]:
    limits = _read_budget_limits(profile_home / "config.yaml")
    usage = {"calls": 0, "tokens": 0, "prompt": 0}
    circuit = {"state": "unknown", "failures": 0, "blocked": False}
    errors: list[str] = []
    autonomy_dir = profile_home / "autonomy_data"
    state_db = next(
        (candidate for candidate in (autonomy_dir / "autonomy.db", autonomy_dir / "state.db") if candidate.is_file()),
        autonomy_dir / "autonomy.db",
    )
    if state_db.is_file():
        try:
            with _readonly_connection(state_db) as connection:
                day = dt.datetime.now(dt.timezone.utc).date().isoformat()
                row = connection.execute("SELECT calls,tokens,prompt FROM usage WHERE day=?", (day,)).fetchone()
                if row:
                    usage = {"calls": int(row[0]), "tokens": int(row[1]), "prompt": int(row[2])}
                row = connection.execute("SELECT failures,state,blocked FROM circuit WHERE id=1").fetchone()
                if row:
                    circuit = {"failures": int(row[0]), "state": safe_text(row[1]), "blocked": bool(row[2])}
        except (OSError, sqlite3.Error) as exc:
            errors.append(safe_text(exc))
    else:
        errors.append("autonomy 状态库不存在")

    seeds_db = profile_home / "seed_data" / "seeds" / "seeds.db"
    seed_total = 0
    if seeds_db.is_file():
        try:
            with _readonly_connection(seeds_db) as connection:
                seed_total = int(connection.execute("SELECT COUNT(*) FROM seeds").fetchone()[0])
        except (OSError, sqlite3.Error) as exc:
            errors.append(safe_text(exc))
    queue_dir = profile_home / "seed_data" / "seed_queue"
    try:
        queue_backlog = sum(1 for path in queue_dir.iterdir() if path.is_file()) if queue_dir.is_dir() else 0
    except OSError as exc:
        queue_backlog = 0
        errors.append(safe_text(exc))
    return {
        "timer": safe_text(_timer_status(profile)),
        "runner": safe_text(_runner_status(profile)),
        "budget": {key: {"used": usage[key], "limit": limits[key]} for key in ("calls", "tokens", "prompt")},
        "circuit": circuit,
        "seed_total": seed_total,
        "queue_backlog": queue_backlog,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "errors": errors,
    }


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>XIAOZHI · 自主意识监控</title>
<style>
:root{color-scheme:dark;--void:#050b14;--cyan:#00e5ff;--ice:#8ff7ff;--amber:#ffb547;--magenta:#ff3cac;--panel:rgba(7,25,42,.68);--line:rgba(0,229,255,.3);--text:#dffcff;--muted:#79a9b7;--danger:#ff657a;--ease:cubic-bezier(.2,.8,.2,1)}
*{box-sizing:border-box}html{background:var(--void)}body{margin:0;min-height:100vh;overflow-x:hidden;background:radial-gradient(circle at 50% -20%,#12314a 0,transparent 45%),linear-gradient(rgba(0,229,255,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(0,229,255,.035) 1px,transparent 1px),var(--void);background-size:auto,32px 32px,32px 32px,auto;color:var(--text);font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}body:after{content:"";position:fixed;inset:-100% 0;z-index:20;pointer-events:none;background:linear-gradient(transparent 48%,rgba(0,229,255,.045) 50%,transparent 52%);animation:scan 11s linear infinite}#stars{position:fixed;inset:0;width:100%;height:100%;z-index:-1;opacity:.7}.hud{position:sticky;top:0;z-index:10;padding:14px clamp(12px,3vw,42px);background:linear-gradient(180deg,rgba(5,11,20,.98),rgba(5,11,20,.8));border-bottom:1px solid var(--line);box-shadow:0 0 30px rgba(0,229,255,.08);backdrop-filter:blur(14px)}.hud-top{display:flex;align-items:center;justify-content:space-between;gap:18px}.brand{min-width:0}.eyebrow{color:var(--cyan);font:10px/1.2 ui-monospace,Consolas,monospace;letter-spacing:.34em;text-shadow:0 0 9px var(--cyan)}h1{font-size:clamp(18px,2.5vw,30px);letter-spacing:.08em;margin:3px 0;text-shadow:0 0 14px rgba(0,229,255,.65)}.clock{text-align:right;font:700 clamp(18px,2.5vw,30px)/1 ui-monospace,Consolas,monospace;color:var(--ice);text-shadow:0 0 12px var(--cyan)}.clock small{display:block;margin-top:7px;font:10px/1 system-ui;color:var(--muted);letter-spacing:.16em}.telemetry{display:grid;grid-template-columns:repeat(5,minmax(100px,1fr));gap:8px;margin-top:12px}.telemetry-cell{position:relative;min-height:64px;padding:9px 10px 8px 54px;overflow:hidden;border:1px solid rgba(0,229,255,.2);clip-path:polygon(0 0,calc(100% - 9px) 0,100% 9px,100% 100%,9px 100%,0 calc(100% - 9px));background:linear-gradient(135deg,rgba(0,229,255,.07),rgba(4,18,31,.72))}.telemetry-cell:before{content:"";position:absolute;left:12px;top:15px;width:24px;height:24px;border-radius:50%;border:2px solid var(--cyan);box-shadow:0 0 10px var(--cyan),inset 0 0 7px rgba(0,229,255,.6)}.telemetry-cell.warn:before{border-color:var(--amber);box-shadow:0 0 10px var(--amber)}.telemetry-cell .label{color:var(--muted);font-size:10px;letter-spacing:.16em}.telemetry-cell strong{display:block;margin-top:3px;color:var(--ice);font:700 15px/1.2 ui-monospace,Consolas,monospace}.gauge{--p:0;position:absolute;left:9px;top:12px;width:32px;height:32px;border-radius:50%;background:conic-gradient(var(--cyan) calc(var(--p)*1%),rgba(0,229,255,.1) 0);filter:drop-shadow(0 0 5px var(--cyan))}.gauge:after{content:"";position:absolute;inset:5px;border-radius:50%;background:#071523}.seed-count{animation:digitPulse 2.6s ease-in-out infinite}.core-stage{height:190px;display:grid;place-items:center;position:relative}.core{--core:var(--cyan);position:relative;width:148px;height:148px;border-radius:50%;display:grid;place-items:center;filter:drop-shadow(0 0 14px var(--core));transition:filter .5s}.core:before,.core:after,.ring{content:"";position:absolute;border-radius:50%;border:1px solid var(--core)}.core:before{inset:0;border-width:2px;border-style:dashed;animation:spin 18s linear infinite}.core:after{inset:16px;border-color:color-mix(in srgb,var(--core) 68%,transparent);border-width:3px;border-left-color:transparent;animation:spinReverse 9s linear infinite}.ring{inset:35px;border-width:1px;box-shadow:0 0 20px var(--core),inset 0 0 22px rgba(0,229,255,.18);animation:breathe 3.4s ease-in-out infinite}.core-dot{width:30px;height:30px;border-radius:50%;background:radial-gradient(circle,#fff 0,var(--core) 24%,transparent 70%);box-shadow:0 0 24px var(--core)}.core-label{position:absolute;top:calc(50% + 82px);font:10px ui-monospace,Consolas,monospace;letter-spacing:.24em;color:var(--core);text-shadow:0 0 8px var(--core)}.core.thinking{--core:var(--amber)}.core.thinking:before{animation-duration:3s}.core.thinking:after{animation-duration:1.8s}.core.thinking .ring{animation-duration:1s}.layout{display:grid;grid-template-columns:minmax(0,1.12fr) minmax(330px,.88fr);gap:18px;padding:0 clamp(12px,3vw,42px) 24px}.panel-head{display:flex;justify-content:space-between;align-items:end;margin-bottom:11px;border-bottom:1px solid var(--line);padding:0 4px 8px}.panel-head h2{margin:0;color:var(--ice);font-size:16px;letter-spacing:.15em}.panel-code{color:var(--cyan);font:10px ui-monospace,Consolas,monospace}.glass{position:relative;margin-bottom:11px;padding:14px;border:1px solid rgba(0,229,255,.24);border-radius:3px;background:linear-gradient(135deg,rgba(13,39,58,.7),rgba(5,14,25,.62));box-shadow:inset 0 0 22px rgba(0,229,255,.025),0 5px 28px rgba(0,0,0,.3);backdrop-filter:blur(12px);transition:transform .25s var(--ease),border-color .25s,box-shadow .25s}.glass:before,.glass:after{content:"";position:absolute;width:14px;height:14px;border-color:var(--cyan)}.glass:before{left:-1px;top:-1px;border-left:2px solid;border-top:2px solid}.glass:after{right:-1px;bottom:-1px;border-right:2px solid;border-bottom:2px solid}.glass:hover{transform:translateY(-2px);border-color:rgba(0,229,255,.55);box-shadow:0 0 22px rgba(0,229,255,.13)}.glass.arrive{animation:arrive .7s var(--ease),flash 1s ease}.thought-top,.seed-top{display:flex;justify-content:space-between;align-items:center;gap:8px}.status-chip{padding:2px 8px;border:1px solid var(--cyan);border-radius:999px;color:var(--cyan);font:10px ui-monospace,Consolas,monospace;letter-spacing:.12em;text-shadow:0 0 6px var(--cyan)}.status-chip.partial,.status-chip.error{color:var(--amber);border-color:var(--amber);text-shadow:0 0 6px var(--amber)}.meta{color:var(--muted);font:11px ui-monospace,Consolas,monospace;display:flex;gap:12px;flex-wrap:wrap}.summary{margin:10px 0;white-space:pre-wrap;color:#ccecf2}.metric-row{display:grid;grid-template-columns:1fr auto;align-items:center;gap:8px;margin:7px 0}.duration{height:3px;background:rgba(0,229,255,.1);overflow:hidden}.duration i{display:block;height:100%;width:var(--duration);background:linear-gradient(90deg,var(--cyan),var(--amber));box-shadow:0 0 7px var(--cyan)}details summary{cursor:pointer;color:var(--cyan);font:11px ui-monospace,Consolas,monospace;letter-spacing:.08em}.terminal{position:relative;max-height:520px;overflow:auto;margin-top:10px;padding:14px;border:1px solid rgba(0,229,255,.18);background:rgba(0,6,12,.9);white-space:pre-wrap;color:#a9f7de;font:12px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;text-shadow:0 0 5px rgba(0,229,255,.25)}.terminal:before{content:"READ_ONLY // TRANSCRIPT";display:block;color:var(--muted);font-size:9px;margin-bottom:8px}.tools{color:var(--amber);border-left:2px solid var(--amber);padding-left:9px}.search-row,.pager{display:flex;gap:8px;margin-bottom:11px}.search-row input{min-width:0;flex:1;background:rgba(0,10,19,.78);color:var(--ice);border:1px solid var(--line);border-radius:2px;padding:10px 12px;outline:none}.search-row input:focus{border-color:var(--cyan);box-shadow:0 0 12px rgba(0,229,255,.18)}button{color:var(--cyan);background:rgba(0,229,255,.07);border:1px solid rgba(0,229,255,.45);padding:8px 13px;clip-path:polygon(7px 0,100% 0,100% calc(100% - 7px),calc(100% - 7px) 100%,0 100%,0 7px);cursor:pointer}button:hover{background:rgba(0,229,255,.16);box-shadow:0 0 12px rgba(0,229,255,.24)}button:disabled{opacity:.3;cursor:not-allowed}.seed{--intensity:.4;--size:1;transform-origin:center}.seed:hover{transform:translateY(-2px) scale(calc(1 + var(--size)*.018))}.seed-node{width:calc(9px + var(--size)*4px);height:calc(9px + var(--size)*4px);flex:none;border-radius:50%;background:var(--cyan);opacity:calc(.45 + var(--intensity)*.55);box-shadow:0 0 calc(7px + var(--intensity)*18px) var(--cyan)}.seed-node.peak{background:var(--magenta);box-shadow:0 0 calc(9px + var(--intensity)*22px) var(--magenta)}.emotion{color:var(--cyan);font:11px ui-monospace,Consolas,monospace}.seed .seed-top>div{display:flex;align-items:center;gap:9px}.empty,.error{padding:18px;border:1px dashed rgba(0,229,255,.25);color:var(--muted)}.error{color:var(--danger);border-color:var(--danger)}.pager{align-items:center}.page{color:var(--muted);font:11px ui-monospace,Consolas,monospace}.foot{text-align:center;padding:6px 12px 20px;color:var(--muted);font:9px ui-monospace,Consolas,monospace;letter-spacing:.16em}
@keyframes scan{to{transform:translateY(50%)}}@keyframes spin{to{transform:rotate(360deg)}}@keyframes spinReverse{to{transform:rotate(-360deg)}}@keyframes breathe{50%{transform:scale(.88);opacity:.52}}@keyframes arrive{from{opacity:0;transform:translateY(18px)}}@keyframes flash{20%{box-shadow:0 0 30px rgba(0,229,255,.45)}}@keyframes digitPulse{50%{text-shadow:0 0 16px var(--cyan);transform:scale(1.05)}}
@media(max-width:900px){.telemetry{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}.core-stage{height:165px}.hud{position:relative}}@media(max-width:520px){.hud-top{align-items:flex-end}.eyebrow{letter-spacing:.18em}.telemetry-cell{min-height:57px}.core{width:122px;height:122px}.core-label{top:calc(50% + 70px)}.layout{padding-inline:9px}.glass{padding:11px}.clock small{display:none}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.001ms!important;animation-iteration-count:1!important;scroll-behavior:auto!important}#stars{display:none}.glass{transition:none}}
</style></head><body>
<canvas id="stars" aria-hidden="true"></canvas>
<header class="hud"><div class="hud-top"><div class="brand"><div class="eyebrow">JARVIS // READ-ONLY LINK</div><h1>XIAOZHI · 自主意识监控</h1></div><div id="clock" class="clock">--:--:--<small>LOCAL SYSTEM TIME</small></div></div><div class="telemetry"><div id="timerCell" class="telemetry-cell"><span class="label">AUTONOMY TIMER</span><strong id="timerValue">同步中</strong></div><div id="circuitCell" class="telemetry-cell"><span class="label">CIRCUIT</span><strong id="circuitValue">同步中</strong></div><div id="queueCell" class="telemetry-cell"><span class="label">QUEUE</span><strong id="queueValue">--</strong></div><div class="telemetry-cell"><span id="budgetGauge" class="gauge"></span><span class="label">DAILY BUDGET</span><strong id="budgetValue">--</strong></div><div class="telemetry-cell"><span class="label">MEMORY SEEDS</span><strong id="seedCount" class="seed-count">0</strong></div></div></header>
<div class="core-stage"><div id="core" class="core" role="img" aria-label="自主意识核心状态"><span class="ring"></span><span class="core-dot"></span><span id="coreLabel" class="core-label">CORE // IDLE</span></div></div>
<main class="layout"><section><div class="panel-head"><h2>思考流</h2><span class="panel-code">THOUGHT STREAM / 01</span></div><div id="thoughts"></div></section><section><div class="panel-head"><h2>种子星图</h2><span class="panel-code">MEMORY CONSTELLATION / 02</span></div><div class="search-row"><input id="search" maxlength="200" aria-label="搜索种子" placeholder="搜索内容、情绪或因果"><button id="searchBtn">扫描</button></div><div id="seeds"></div><div class="pager"><button id="prev">上一页</button><button id="next">下一页</button><span id="page" class="page"></span></div></section></main>
<div class="foot">SECURE READ-ONLY TELEMETRY · 每 15 秒同步 · 无模型连接</div>
<script>
const token=new URLSearchParams(location.search).get('token')||'',limit=20,reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;let offset=0,lastThoughtIds=new Set,lastSeedIds=new Set;const thoughtNodes=new Map,seedNodes=new Map;
function api(path,params={}){const p=new URLSearchParams(params);if(token)p.set('token',token);return fetch(path+'?'+p,{cache:'no-store'}).then(r=>{if(!r.ok)throw Error('HTTP '+r.status);return r.json()})}
function decode(s){const t=document.createElement('textarea');t.innerHTML=s||'';return t.value}function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=decode(String(text));if(cls)n.className=cls;return n}function setText(id,value){const n=document.getElementById(id),v=decode(String(value));if(n.textContent!==v)n.textContent=v}function setWarn(id,warn){document.getElementById(id).classList.toggle('warn',warn)}
function clock(){const n=new Date;setText('clock',n.toLocaleTimeString('zh-CN',{hour12:false}));const small=el('small',n.toLocaleDateString('zh-CN')+' · LOCAL SYSTEM TIME');document.getElementById('clock').append(small)}clock();setInterval(clock,1000);
function animateNumber(node,to){const from=Number(node.dataset.value||0);node.dataset.value=to;if(reduced||from===to){node.textContent=to;return}const start=performance.now();function frame(now){const p=Math.min(1,(now-start)/650);node.textContent=Math.round(from+(to-from)*(1-Math.pow(1-p,3)));if(p<1)requestAnimationFrame(frame)}requestAnimationFrame(frame)}
function updateCore(active){const core=document.getElementById('core');core.classList.toggle('thinking',active);setText('coreLabel',active?'CORE // THINKING':'CORE // IDLE')}
async function loadStatus(){try{const s=await api('/api/status'),timer=s.timer,circuit=s.circuit.state,queue=s.queue_backlog,calls=s.budget.calls,tokens=s.budget.tokens,pct=Math.min(100,Math.max(calls.used/calls.limit*100,tokens.used/tokens.limit*100)||0);setText('timerValue',timer.toUpperCase());setWarn('timerCell',timer!=='active');setText('circuitValue',`${circuit.toUpperCase()} / ${s.circuit.failures}`);setWarn('circuitCell',circuit!=='closed');setText('queueValue',`${queue} PENDING`);setWarn('queueCell',queue>0);setText('budgetValue',`${calls.used}/${calls.limit} · ${pct.toFixed(1)}%`);document.getElementById('budgetGauge').style.setProperty('--p',pct);animateNumber(document.getElementById('seedCount'),s.seed_total);updateCore(s.runner==='active'||s.runner==='activating')}catch(e){setText('timerValue','LINK ERROR');setWarn('timerCell',true)}}
function typeTerminal(pre,text){if(pre.dataset.done)return;pre.dataset.done='1';if(reduced){pre.textContent=text;return}const lines=text.split('\n');let i=0;pre.textContent='';function step(){pre.textContent+=(i?'\n':'')+lines[i++];pre.scrollTop=pre.scrollHeight;if(i<lines.length)requestAnimationFrame(step)}if(lines.length)requestAnimationFrame(step)}
function thoughtCard(x,isNew){const c=el('article',undefined,'glass thought'+(isNew?' arrive':''));c.dataset.key=x.run_id;const top=el('div',undefined,'thought-top'),chip=el('span',x.status,'status-chip '+x.status),time=el('span',x.ts,'meta');top.append(chip,time);const metric=el('div',undefined,'metric-row'),bar=el('span',undefined,'duration'),fill=el('i'),elapsed=x.elapsed==null?0:Number(x.elapsed);bar.style.setProperty('--duration',Math.min(100,elapsed/6)+'%');bar.append(fill);metric.append(bar,el('span',x.elapsed==null?'耗时未知':`${x.elapsed}s · 预取 ${x.prefetch_count??'-'}`,'meta'));const details=document.createElement('details'),label=el('summary','展开神经链路 / 工具调用'),terminal=el('pre','', 'terminal'),tools=x.tools.length?el('pre',x.tools.join('\n'),'terminal tools'):null;details.append(label,terminal);if(tools)details.append(tools);details.addEventListener('toggle',()=>{if(details.open)typeTerminal(terminal,decode(x.body))});c.append(top,metric,el('div',x.summary,'summary'),details);return c}
function syncThoughts(items){const root=document.getElementById('thoughts'),ids=new Set(items.map(x=>x.run_id));if(items.length)root.querySelectorAll('.empty,.error').forEach(n=>n.remove());for(const [id,node] of thoughtNodes)if(!ids.has(id)){node.remove();thoughtNodes.delete(id)}for(const x of items){let node=thoughtNodes.get(x.run_id);if(!node){node=thoughtCard(x,!lastThoughtIds.has(x.run_id));thoughtNodes.set(x.run_id,node)}root.append(node)}lastThoughtIds=ids;if(!items.length&&!root.querySelector('.empty'))root.append(el('div','暂无自主思考记录','empty'))}
async function loadThoughts(){try{const data=await api('/api/thoughts',{n:20});syncThoughts(data.items)}catch(e){document.getElementById('thoughts').replaceChildren(el('div','思考链路读取失败：'+e,'error'))}}
function seedCard(x,isNew){const intensity=Math.max(0,Math.min(1,Number(x.emotion_intensity)||0)),importance=Math.max(0,Math.min(1,Number(x.importance)||0)),c=el('article',undefined,'glass seed'+(isNew?' arrive':''));c.dataset.key=x.id;c.style.setProperty('--intensity',intensity);c.style.setProperty('--size',importance);const top=el('div',undefined,'seed-top'),left=document.createElement('div'),dot=el('span',undefined,'seed-node'+(intensity>=.75?' peak':''));left.append(dot,el('span',`${x.emotion||'中性'} / ${(intensity*100).toFixed(0)}%`,'emotion'));top.append(left,el('span',x.timestamp,'meta'));c.append(top,el('div',x.summary,'summary'),el('div',`重要度 ${(importance*100).toFixed(0)}% · 因果 ${x.caused_by.join(', ')||'无'}`,'meta'));return c}
function syncSeeds(data){const root=document.getElementById('seeds'),ids=new Set(data.items.map(x=>x.id));if(data.items.length)root.querySelectorAll('.empty,.error').forEach(n=>n.remove());for(const [id,node] of seedNodes)if(!ids.has(id)){node.remove();seedNodes.delete(id)}for(const x of data.items){let node=seedNodes.get(x.id);if(!node){node=seedCard(x,!lastSeedIds.has(x.id));seedNodes.set(x.id,node)}root.append(node)}lastSeedIds=ids;if(!data.items.length)root.replaceChildren(el('div',data.error||'未发现匹配种子','empty'));document.getElementById('prev').disabled=offset===0;document.getElementById('next').disabled=data.next_offset==null;setText('page',`${data.total} 条 · ${data.total?offset+1:0}-${Math.min(offset+limit,data.total)}`)}
async function loadSeeds(reset=false){if(reset)offset=0;try{syncSeeds(await api('/api/seeds',{limit,q:document.getElementById('search').value,offset}))}catch(e){document.getElementById('seeds').replaceChildren(el('div','种子星图读取失败：'+e,'error'))}}
document.getElementById('searchBtn').onclick=()=>loadSeeds(true);document.getElementById('search').onkeydown=e=>{if(e.key==='Enter')loadSeeds(true)};document.getElementById('prev').onclick=()=>{offset=Math.max(0,offset-limit);loadSeeds()};document.getElementById('next').onclick=()=>{offset+=limit;loadSeeds()};
function starfield(){if(reduced)return;const c=document.getElementById('stars'),x=c.getContext('2d'),stars=Array.from({length:44},()=>({x:Math.random(),y:Math.random(),r:.3+Math.random()*1.1,s:.00003+Math.random()*.00007}));let last=0,running=!document.hidden;function size(){const d=Math.min(devicePixelRatio||1,1.5);c.width=innerWidth*d;c.height=innerHeight*d;x.setTransform(d,0,0,d,0,0)}function draw(now){if(!running)return;if(now-last>50){last=now;x.clearRect(0,0,innerWidth,innerHeight);for(const p of stars){p.y=(p.y+p.s*(now%100))%1;x.beginPath();x.arc(p.x*innerWidth,p.y*innerHeight,p.r,0,Math.PI*2);x.fillStyle='rgba(0,229,255,.55)';x.fill()}}requestAnimationFrame(draw)}addEventListener('resize',size,{passive:true});document.addEventListener('visibilitychange',()=>{running=!document.hidden;if(running)requestAnimationFrame(draw)});size();requestAnimationFrame(draw)}starfield();
async function refresh(){await Promise.all([loadStatus(),loadThoughts(),loadSeeds()])}refresh();setInterval(refresh,15000);
</script></body></html>"""


class ViewerApplication:
    def __init__(self, profile: str, profile_home: Path, token: str = "") -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", profile):
            raise ValueError("invalid profile name")
        self.profile = profile
        self.profile_home = profile_home.expanduser().resolve()
        self.token = token
        self.thought_log = self.profile_home / "autonomy_data" / "thoughts.log"
        self.seeds_db = self.profile_home / "seed_data" / "seeds" / "seeds.db"

    def authorized(self, authorization: str, query: dict[str, list[str]]) -> bool:
        if not self.token:
            return True
        bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
        supplied = bearer or (query.get("token") or [""])[0]
        return secrets.compare_digest(supplied, self.token)

    def route(self, path: str, query: dict[str, list[str]]) -> tuple[int, str, bytes]:
        if path == "/":
            return HTTPStatus.OK, "text/html; charset=utf-8", INDEX_HTML.encode("utf-8")
        if path == "/api/thoughts":
            count = _integer_parameter(query, "n", 20, 1, 100)
            payload = {"items": load_thoughts(self.thought_log, count), "count": count}
            return _json_response(payload)
        if path == "/api/seeds":
            limit = _integer_parameter(query, "limit", 20, 1, 100)
            offset = _integer_parameter(query, "offset", 0, 0, 1_000_000)
            search = (query.get("q") or [""])[0]
            return _json_response(load_seeds(self.seeds_db, limit, search, offset))
        if path == "/api/status":
            return _json_response(load_status(self.profile_home, self.profile))
        return _json_response({"error": "not found"}, HTTPStatus.NOT_FOUND)


def _integer_parameter(
    query: dict[str, list[str]], name: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int((query.get(name) or [str(default)])[0])
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _json_response(payload: Any, status: int = HTTPStatus.OK) -> tuple[int, str, bytes]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return status, "application/json; charset=utf-8", body


def make_handler(application: ViewerApplication) -> type[BaseHTTPRequestHandler]:
    class ViewerHandler(BaseHTTPRequestHandler):
        server_version = "HermesReadOnlyViewer/1.0"

        def do_GET(self) -> None:  # noqa: N802 - http.server callback name
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if not application.authorized(self.headers.get("Authorization", ""), query):
                self._send(*_json_response({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED))
                return
            self._send(*application.route(parsed.path, query))

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(int(status))
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'",
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, message: str, *args: Any) -> None:
            # Never put a ?token= query value into journal output.
            clean_path = urlsplit(self.path).path
            sys.stderr.write(f"[viewer] {self.client_address[0]} {self.command} {clean_path}\n")

    return ViewerHandler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes 小智自主思考与种子只读查看器")
    parser.add_argument("--profile", default=os.getenv("HERMES_VIEWER_PROFILE", DEFAULT_PROFILE))
    parser.add_argument("--profile-home", default=os.getenv("HERMES_VIEWER_HOME"))
    parser.add_argument("--host", default=os.getenv("HERMES_VIEWER_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("HERMES_VIEWER_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--token", default=os.getenv("HERMES_VIEWER_TOKEN", ""))
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    profile_home = (
        Path(args.profile_home).expanduser()
        if args.profile_home
        else Path.home() / ".hermes" / "profiles" / args.profile
    )
    if not profile_home.is_dir():
        raise SystemExit(f"profile directory does not exist: {profile_home}")
    application = ViewerApplication(args.profile, profile_home, args.token)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    server.daemon_threads = True
    print(f"Hermes read-only viewer listening on http://{args.host}:{args.port}", flush=True)
    if not args.token:
        print("WARNING: no token configured; expose this port only to a trusted LAN", file=sys.stderr, flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
