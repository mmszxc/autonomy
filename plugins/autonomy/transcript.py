"""Read a Hermes one-shot session transcript from ``state.db``.

Hermes v0.22.5 stores sessions in ``sessions`` (notably ``id``, ``source``,
``title``, ``started_at``, ``ended_at``, ``message_count`` and
``tool_call_count``) and ordered records in ``messages`` (``session_id``,
``role``, ``content``, ``tool_calls``, ``tool_call_id``, ``tool_name`` and
``timestamp``).  An assistant row's ``tool_calls`` is an OpenAI-style JSON
list; the corresponding ``role=tool`` row carries the result in ``content``.

``hermes chat --oneshot`` has no option for choosing a new session id.  It can
emit stream JSON, resume an existing id, or put its id in the prompt, but a new
one-shot invocation creates the id internally.  Consequently a tick is matched
to sessions overlapping ``[start_ts, end_ts]``.  A session created inside the
window is preferred, then the session whose start is closest to ``start_ts``.
Callers should pass the tight gateway-call window to exclude unrelated chats.
The database is always opened read-only and failures degrade to an error dict.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

_SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|access[_-]?token|token|secret|password|passwd)")
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|passwd)"
    r"(\s*[:=]\s*)([^\s,;\]\}\"']+|\"[^\"]*\"|'[^']*')"
)
_SK_RE = re.compile(r"(?i)\bsk-[a-z0-9_-]{12,}\b")
_REDACTED = "[REDACTED]"


def _error(message: str) -> Dict[str, Any]:
    return {"error": message, "text": ""}


def _redact_obj(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (_REDACTED if _SECRET_KEY_RE.search(str(key)) else _redact_obj(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_obj(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_obj(item) for item in value)
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: Any) -> str:
    text = str(value or "")
    text = _SK_RE.sub(_REDACTED, text)

    def replace(match: re.Match[str]) -> str:
        return match.group(1) + match.group(2) + _REDACTED

    return _SECRET_ASSIGN_RE.sub(replace, text)


def _preview(value: Any, limit: int) -> str:
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(_redact_obj(value), ensure_ascii=False, separators=(",", ":"))
    else:
        raw = str(value or "")
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            text = _redact_text(raw)
        else:
            text = json.dumps(_redact_obj(parsed), ensure_ascii=False, separators=(",", ":"))
    text = " ".join(text.split())
    limit = max(0, int(limit))
    if len(text) <= limit:
        return text
    marker = "…[truncated]"
    return text[: max(0, limit - len(marker))] + marker


def _decode_tool_calls(raw: Any) -> List[Dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(value, dict):
        value = [value]
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute('PRAGMA table_info("%s")' % table)}


def extract_run_transcript(
    state_db_path: Any,
    start_ts: float,
    end_ts: float,
    *,
    max_chars: int = 20000,
    tool_arg_chars: int = 200,
    tool_result_chars: int = 200,
) -> Dict[str, Any]:
    """Extract one autonomous run's assistant text and tool summaries.

    The returned mapping contains ``session_id``, stored message/tool counts,
    ``assistant_texts``, ``tool_calls``, assembled ``text`` and ``truncated``.
    Missing files, incompatible schemas and unmatched windows return
    ``{"error": ..., "text": ""}`` instead of raising.
    """
    path = Path(state_db_path).expanduser()
    if not path.is_file():
        return _error("state database not found: %s" % path)
    try:
        start = float(start_ts)
        end = float(end_ts)
    except (TypeError, ValueError):
        return _error("invalid run time window")
    if end < start:
        return _error("invalid run time window: end precedes start")

    conn: Optional[sqlite3.Connection] = None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=2000")
        session_required = {
            "id", "started_at", "ended_at", "message_count", "tool_call_count"
        }
        message_required = {
            "session_id", "role", "content", "tool_calls", "tool_call_id",
            "tool_name", "timestamp",
        }
        if not session_required.issubset(_table_columns(conn, "sessions")):
            return _error("incompatible state database: sessions schema")
        if not message_required.issubset(_table_columns(conn, "messages")):
            return _error("incompatible state database: messages schema")

        candidates = list(conn.execute(
            "SELECT id, started_at, ended_at, message_count, tool_call_count "
            "FROM sessions WHERE started_at <= ? "
            "AND COALESCE(ended_at, started_at) >= ?",
            (end, start),
        ))
        if not candidates:
            return _error("no session found in run time window")
        candidates.sort(key=lambda row: (
            0 if start <= float(row["started_at"]) <= end else 1,
            abs(float(row["started_at"]) - start),
            -int(row["tool_call_count"] or 0),
        ))
        session = candidates[0]
        session_id = str(session["id"])
        messages = list(conn.execute(
            "SELECT rowid AS _rowid, role, content, tool_calls, tool_call_id, "
            "tool_name, timestamp FROM messages WHERE session_id=? "
            "ORDER BY timestamp, _rowid",
            (session_id,),
        ))

        results_by_id: Dict[str, str] = {}
        unmatched_results: List[sqlite3.Row] = []
        for message in messages:
            if str(message["role"] or "") != "tool":
                continue
            call_id = str(message["tool_call_id"] or "")
            if call_id:
                results_by_id[call_id] = str(message["content"] or "")
            else:
                unmatched_results.append(message)

        assistant_texts: List[str] = []
        tools: List[Dict[str, str]] = []
        lines: List[str] = []
        unmatched_index = 0
        for message in messages:
            if str(message["role"] or "") != "assistant":
                continue
            content = _redact_text(message["content"] or "").strip()
            if content:
                assistant_texts.append(content)
                lines.append("【助手】" + content)
            for call in _decode_tool_calls(message["tool_calls"]):
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = str(function.get("name") or call.get("name") or "unknown")
                arguments = function.get("arguments", call.get("arguments", ""))
                call_id = str(call.get("id") or "")
                result = results_by_id.get(call_id, "") if call_id else ""
                if not result and unmatched_index < len(unmatched_results):
                    fallback = unmatched_results[unmatched_index]
                    fallback_name = str(fallback["tool_name"] or "")
                    if not fallback_name or fallback_name == name:
                        result = str(fallback["content"] or "")
                        unmatched_index += 1
                item = {
                    "name": _redact_text(name),
                    "args_preview": _preview(arguments, tool_arg_chars),
                    "result_preview": _preview(result, tool_result_chars),
                }
                tools.append(item)
                line = "【工具】%s args=%s" % (item["name"], item["args_preview"] or "-")
                if item["result_preview"]:
                    line += " result=" + item["result_preview"]
                lines.append(line)

        text = "\n".join(lines)
        limit = max(0, int(max_chars))
        truncated = len(text) > limit
        if truncated:
            marker = "\n…[transcript truncated]"
            text = text[: max(0, limit - len(marker))] + marker
        return {
            "session_id": session_id,
            "message_count": int(session["message_count"] or len(messages)),
            "tool_call_count": int(session["tool_call_count"] or len(tools)),
            "assistant_texts": assistant_texts,
            "tool_calls": tools,
            "text": text,
            "truncated": truncated,
        }
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        return _error("transcript extraction failed: %s" % type(exc).__name__)
    finally:
        if conn is not None:
            conn.close()
