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


def _timer_status(profile: str) -> str:
    unit = f"{profile}-autonomy.timer"
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
        "budget": {key: {"used": usage[key], "limit": limits[key]} for key in ("calls", "tokens", "prompt")},
        "circuit": circuit,
        "seed_total": seed_total,
        "queue_backlog": queue_backlog,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "errors": errors,
    }


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>小智 · 自主思考只读查看器</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--card:#151d31;--line:#2b3958;--text:#e8eefc;--muted:#9badca;--accent:#79b8ff;--ok:#68d391;--warn:#f6c177}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}header{padding:18px max(16px,3vw);border-bottom:1px solid var(--line);position:sticky;top:0;background:#0b1020ee;backdrop-filter:blur(10px);z-index:2}h1{font-size:21px;margin:0 0 12px}.status{display:grid;grid-template-columns:repeat(5,minmax(130px,1fr));gap:8px}.badge,.card{background:var(--card);border:1px solid var(--line);border-radius:10px}.badge{padding:8px 10px;color:var(--muted)}.badge b{display:block;color:var(--text);font-size:16px}.layout{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,.9fr);gap:16px;padding:16px max(16px,3vw)}section h2{font-size:18px;margin:0 0 10px}.card{padding:12px;margin-bottom:10px}.meta{color:var(--muted);font-size:13px;display:flex;gap:10px;flex-wrap:wrap}.summary{white-space:pre-wrap;margin:8px 0}.full{white-space:pre-wrap;max-height:520px;overflow:auto;background:#0d1425;padding:10px;border-radius:7px;color:#cbd7ee}.tools{color:var(--warn);white-space:pre-wrap}.row{display:flex;gap:8px;margin-bottom:10px}.row input{min-width:0;flex:1;background:#0d1425;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:9px}button{background:#24456f;color:white;border:1px solid #3a6598;border-radius:7px;padding:8px 12px;cursor:pointer}button:disabled{opacity:.45}.seed-title{display:flex;justify-content:space-between;gap:8px}.emotion{color:var(--accent)}.error{color:#ff9b9b}.foot{color:var(--muted);font-size:12px;text-align:center;padding:8px}details summary{cursor:pointer;color:var(--accent)}
@media(max-width:820px){.status{grid-template-columns:repeat(2,1fr)}.layout{grid-template-columns:1fr}header{position:static}}
</style></head><body>
<header><h1>小智 · 自主思考与种子（只读）</h1><div id="status" class="status"></div></header>
<main class="layout"><section><h2>最近自主思考</h2><div id="thoughts"></div></section><section><h2>记忆种子</h2><div class="row"><input id="search" maxlength="200" placeholder="搜索内容、情绪或因果"><button id="searchBtn">搜索</button></div><div id="seeds"></div><div class="row"><button id="prev">上一页</button><button id="next">下一页</button><span id="page" class="meta"></span></div></section></main>
<div class="foot">每 15 秒自动刷新 · 页面和 API 均为只读</div>
<script>
const token=new URLSearchParams(location.search).get('token')||'';let offset=0;const limit=20;
function api(path,params={}){const p=new URLSearchParams(params);if(token)p.set('token',token);return fetch(path+'?'+p,{cache:'no-store'}).then(r=>{if(!r.ok)throw Error('HTTP '+r.status);return r.json()})}
function decode(s){const t=document.createElement('textarea');t.innerHTML=s||'';return t.value}function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=decode(String(text));if(cls)n.className=cls;return n}
async function loadStatus(){const root=document.getElementById('status');try{const s=await api('/api/status');const values=[['定时器',s.timer],['预算',`${s.budget.calls.used}/${s.budget.calls.limit} 次 · ${s.budget.tokens.used}/${s.budget.tokens.limit} token`],['熔断',`${s.circuit.state} · ${s.circuit.failures}`],['种子总数',s.seed_total],['队列积压',s.queue_backlog]];root.replaceChildren(...values.map(([a,b])=>{const n=el('div',a,'badge');n.append(el('b',b));return n}))}catch(e){root.replaceChildren(el('div','状态读取失败：'+e,'error'))}}
async function loadThoughts(){const root=document.getElementById('thoughts');try{const data=await api('/api/thoughts',{n:20});root.replaceChildren(...data.items.map(x=>{const c=el('article',undefined,'card'),m=el('div',undefined,'meta');m.append(el('span',x.ts),el('span','状态 '+x.status),el('span',x.elapsed==null?'耗时未知':`耗时 ${x.elapsed}s`),el('span',x.prefetch_count==null?'预取未知':`预取 ${x.prefetch_count}`));c.append(m,el('div',x.summary,'summary'));const d=document.createElement('details');d.append(el('summary','查看完整过程 / 工具调用'),el('pre',x.body,'full'));if(x.tools.length)d.append(el('pre',x.tools.join('\n'),'tools'));c.append(d);return c}));if(!data.items.length)root.replaceChildren(el('div','暂无自主思考记录','card'))}catch(e){root.replaceChildren(el('div','思考读取失败：'+e,'error'))}}
async function loadSeeds(reset=false){if(reset)offset=0;const root=document.getElementById('seeds'),q=document.getElementById('search').value;try{const data=await api('/api/seeds',{limit,q,offset});root.replaceChildren(...data.items.map(x=>{const c=el('article',undefined,'card'),top=el('div',undefined,'seed-title');top.append(el('span',x.timestamp),el('span',`${x.emotion} ${x.emotion_intensity??''}`,'emotion'));c.append(top,el('div',x.summary,'summary'),el('div',`重要度：${x.importance??'-'} · 因果：${x.caused_by.join(', ')||'-'}`,'meta'));return c}));if(!data.items.length)root.replaceChildren(el('div',data.error||'没有匹配种子','card'));document.getElementById('prev').disabled=offset===0;document.getElementById('next').disabled=data.next_offset==null;document.getElementById('page').textContent=`${data.total} 条 · ${offset+1}-${Math.min(offset+limit,data.total)}`}catch(e){root.replaceChildren(el('div','种子读取失败：'+e,'error'))}}
document.getElementById('searchBtn').onclick=()=>loadSeeds(true);document.getElementById('search').onkeydown=e=>{if(e.key==='Enter')loadSeeds(true)};document.getElementById('prev').onclick=()=>{offset=Math.max(0,offset-limit);loadSeeds()};document.getElementById('next').onclick=()=>{offset+=limit;loadSeeds()};
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
