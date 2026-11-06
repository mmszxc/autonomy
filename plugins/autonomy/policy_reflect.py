"""Nightly policy reflection (T5): distil one advisory rule from failures/inefficiency.

Runs only inside the local 03:00-03:39 window (Asia/Shanghai), at most once per
local day, and only from the tick entry in runner.py.  Every public entry
degrades (returns a status dict) instead of raising, so a tick can never be
broken by this module.  Stdlib only; the model call is injected by the caller
(runner passes the existing gateway.chat wrapper).

Public API:
  pick_trigger(sigs, inefficiency, active_rules, rejected_keys=()) -> dict | None
  find_program_memory(profile_home, terms) -> dict | None
  build_reflection_input(trigger, profile_home, state_db, autonomy_db, active_rules) -> (text, pm)
  validate_rule(obj, trigger, active_rules, rejected_texts=()) -> (ok, reason, text)
  generate_rule(trigger, input_text, active_rules, rejected_texts, llm_call, trigger_tool=None) -> dict
  run_policy_reflection(profile_home, data_dir, db, now=None, llm_call=None, profile="", run_id=None) -> dict
"""
from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import json
import logging
import re
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from . import policy as P

REFLECT_TZ = "Asia/Shanghai"
REFLECT_HOUR = 3
# window 03:00-03:39 local; a 02:40 tick may hold the run lock until 03:20
REFLECT_MINUTE_MAX = 39
MAX_SAMPLES = 3
SAMPLE_MAX_CHARS = 800
INPUT_MAX_CHARS = 6000
MAX_ATTEMPTS = 3                 # 1 initial + 2 rewrites
SIM_THRESHOLD = 0.8
REJECT_COOLDOWN_S = 7 * 86400
BANNED_PHRASES = ("尝试不同方向", "提高产出", "更努力", "多思考")
KNOWN_FAMILIES = frozenset(P._FAMILY.values())
ERROR_CLUES = ("error", "exception", "traceback", "timeout", "exit_", "permission", "denied",
               "not found", "报错", "错误", "超时", "失败")
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------- redaction

_RE_ENV_LINE = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?[A-Z][A-Z0-9_]{2,}[ \t]*=[^\n]*$")
_RE_KV_SECRET = re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|authorization)\b([ \t]*[:=][ \t]*)\S+")
_RE_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+")
_RE_PREFIX_TOKEN = re.compile(r"\b(?:sk|ghp|gho|xox[bpa]|AKIA)[-_A-Za-z0-9]{12,}\b")
_RE_LONG_RANDOM = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")


def redact(text):
    """Drop suspected secrets, tokens and .env assignment lines. Never raises."""
    s = str(text or "")
    s = _RE_ENV_LINE.sub("[REDACTED_ENV_LINE]", s)
    s = _RE_KV_SECRET.sub(lambda m: m.group(1) + m.group(2) + "[REDACTED]", s)
    s = _RE_BEARER.sub("Bearer [REDACTED]", s)
    s = _RE_PREFIX_TOKEN.sub("[REDACTED]", s)
    s = _RE_LONG_RANDOM.sub("[REDACTED]", s)
    return s

# ---------------------------------------------------------------- time window


def _local(now):
    tz = ZoneInfo(REFLECT_TZ)
    if now is None:
        return dt.datetime.now(tz)
    if isinstance(now, dt.datetime):
        d = now if now.tzinfo else now.replace(tzinfo=tz)
        return d.astimezone(tz)
    return dt.datetime.fromtimestamp(float(now), tz)


def in_reflection_window(now=None):
    try:
        loc = _local(now)
        return loc.hour == REFLECT_HOUR and loc.minute <= REFLECT_MINUTE_MAX
    except Exception:
        return False

# ---------------------------------------------------------------- trigger selection


def pick_trigger(sigs, inefficiency, active_rules, rejected_keys=()):
    """Pick the most worth-handling trigger: A (failure signature, by count) before B.

    Excludes signatures already covered by an active rule and recently rejected keys.
    Returns {"channel","key","item"} or None.
    """
    taken = {r.get("source_signature") for r in (active_rules or [])}
    excluded = taken | set(rejected_keys or ())
    cands = [s for s in (sigs or []) if isinstance(s, dict) and s.get("triggerable")
             and s.get("signature") not in excluded]
    if cands:
        best = max(cands, key=lambda s: s.get("count", 0))
        return {"channel": "A", "key": best["signature"], "item": best}
    ineff = inefficiency or {}
    for name in ("no_change", "seed_search_repeat"):
        item = ineff.get(name)
        if not isinstance(item, dict) or not item.get("triggered"):
            continue
        kind = item.get("kind") or name
        key = "B:" + kind
        if key in excluded:
            continue
        return {"channel": "B", "key": key, "item": dict(item, kind=kind)}
    return None

# ---------------------------------------------------------------- program memory


def find_program_memory(profile_home, terms):
    """Best program_memory whose text mentions any term, with +/- seed summaries. None if absent."""
    conn = None
    try:
        conn = P._open_ro(Path(profile_home) / "seed_data" / "seeds" / "seeds.db")
        if conn is None:
            return None
        for term in [t for t in terms if t]:
            rows = P._q(conn,
                        "select id, pattern, behavior, confidence, source_seeds, counter_seeds from program_memories "
                        "where lower(pattern||' '||trigger_context||' '||behavior) like ? "
                        "order by confidence desc limit 1", ("%" + str(term).lower() + "%",))
            if not rows:
                continue
            pid, pattern, behavior, conf, src, cnt = rows[0]
            pos_ids = _json_ids(src)[:2]
            neg_ids = _json_ids(cnt)[:2]
            return {"id": pid, "pattern": redact(pattern or "")[:200], "behavior": redact(behavior or "")[:200],
                    "confidence": conf,
                    "positive": _seed_summaries(conn, pos_ids), "negative": _seed_summaries(conn, neg_ids)}
        return None
    except Exception:
        return None
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _json_ids(raw):
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in v if x] if isinstance(v, list) else []
    except Exception:
        return []


def _seed_summaries(conn, ids):
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = P._q(conn, "select id, summary from seeds where id in (%s)" % marks, list(ids))
    return [redact(str(s or ""))[:120] for _id, s in rows]

# ---------------------------------------------------------------- sample assembly


def _session_fragment(state_db, sid, tool=None, limit=SAMPLE_MAX_CHARS):
    rows = P._q(state_db, "select role, tool_name, tool_calls, content from messages where session_id=? order by timestamp",
                (sid,))
    parts = []
    for role, tname, tc, content in rows:
        if role == "assistant" and tc and (tool is None or tool in str(tc)):
            try:
                calls = json.loads(tc)
            except Exception:
                calls = []
            for c in calls if isinstance(calls, list) else []:
                fn = (c or {}).get("function") or {}
                if tool is None or fn.get("name") == tool:
                    parts.append("调用 %s 参数: %s" % (fn.get("name", ""), str(fn.get("arguments", ""))[:300]))
        elif role == "tool" and (tool is None or tname == tool) and P.classify_result(content) is not None:
            parts.append("报错结果: %s" % str(content)[:400])
    return redact("\n".join(parts))[:limit]


def _sample_sessions(trig, adb, sdb):
    it = trig["item"]
    if trig["channel"] == "A":
        return list(it.get("sample_sessions") or [])[:MAX_SAMPLES], it.get("tool") or None
    if it.get("kind") == "seed_search_repeat":
        return [x.get("session_id") for x in it.get("items", []) if x.get("session_id")][:MAX_SAMPLES], "seed_search"
    run_map = P.map_runs(adb, sdb).get("run_to_session", {}) if adb is not None else {}
    sids = []
    for h in it.get("recent") or []:
        sid = run_map.get(h.get("run_id"))
        if sid and sid not in sids:
            sids.append(sid)
    return sids[:MAX_SAMPLES], None


def _trigger_desc(trig):
    it = trig["item"]
    if trig["channel"] == "A":
        return ("A通道：工具 %s（族 %s）错误签名 %s，异常类型 %s，规范化错误 %s，近14天出现 %d 次"
                % (it.get("tool"), it.get("tool_family"), it.get("signature"), it.get("exc_type"),
                   redact(it.get("last_line_norm", ""))[:200], it.get("count", 0)))
    if it.get("kind") == "seed_search_repeat":
        return "B通道：seed_search 同一查询重复（seed_search_repeat），涉及 %d 个会话" % it.get("sessions_affected", 0)
    return "B通道：连续无外部状态变化（l0_no_change），触发 %d 次，最长连续 %d 轮" % (
        it.get("events_ge3", 0), it.get("max_streak", 0))


def _trigger_terms(trig):
    it = trig["item"]
    if trig["channel"] == "A":
        return [it.get("tool"), it.get("exc_type")]
    if it.get("kind") == "seed_search_repeat":
        return ["seed_search"]
    return []


def build_reflection_input(trigger, profile_home, state_db, autonomy_db, active_rules):
    """Assemble the redacted reflection prompt. Returns (text, program_memory_or_None)."""
    sids, tool = _sample_sessions(trigger, autonomy_db, state_db)
    pm = find_program_memory(profile_home, _trigger_terms(trigger))
    parts = ["【触发】" + _trigger_desc(trigger)]
    for sid in sids:
        frag = _session_fragment(state_db, sid, tool)
        if frag:
            parts.append("【样例会话 %s】\n%s" % (str(sid)[:16], frag))
    if pm:
        parts.append("【程序记忆】模式: %s；行为: %s；置信度 %.2f\n正例: %s\n反例: %s" % (
            pm["pattern"], pm["behavior"], float(pm.get("confidence") or 0),
            "；".join(pm["positive"]) or "无", "；".join(pm["negative"]) or "无"))
    rules_txt = "\n".join("- " + " ".join(r.get("text", "").split()) for r in (active_rules or [])) or "无"
    parts.append("【现有生效规则（避免重复）】\n" + rules_txt)
    parts.append("【输出要求】只输出一个 JSON 对象：{\"condition\": \"...\", \"action\": \"...\", \"tool_family\": \"...\"}。"
                 "condition 必须包含具体工具名或错误类型关键词；action 是可执行的具体做法；"
                 "tool_family 取值之一：" + ", ".join(sorted(KNOWN_FAMILIES)) + "。这是建议，不是强制命令。")
    return redact("\n\n".join(parts))[:INPUT_MAX_CHARS], pm

# ---------------------------------------------------------------- validation

_RE_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _parse_json(raw):
    if not isinstance(raw, str):
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
    except Exception:
        return None
    return v if isinstance(v, dict) else None


def validate_rule(obj, trigger, active_rules, rejected_texts=()):
    """Return (ok, reason, text). Reasons: not_object, missing_field, unknown_tool_family,
    no_checkable_clue, banned_phrase, sensitive_content, duplicate."""
    if not isinstance(obj, dict):
        return False, "not_object", ""
    vals = {}
    for k in ("condition", "action", "tool_family"):
        v = obj.get(k)
        if not isinstance(v, str) or not v.strip():
            return False, "missing_field", ""
        vals[k] = v.strip()
    cond, act, fam = vals["condition"], vals["action"], vals["tool_family"]
    if fam not in KNOWN_FAMILIES:
        return False, "unknown_tool_family", ""
    text = "当%s（工具族 %s）时，%s" % (cond, fam, act)
    tool_names = set(P._FAMILY) | ({trigger["item"].get("tool")} if trigger["channel"] == "A" else set())
    tool_names.discard(None)
    tokens = {t.lower() for t in _RE_IDENT.findall(cond)}
    low_cond = cond.lower()
    clue = bool(tokens & {t.lower() for t in tool_names}) or any(c in low_cond for c in ERROR_CLUES)
    if trigger["channel"] == "A":
        exc = str(trigger["item"].get("exc_type") or "").lower()
        clue = clue or (len(exc) >= 4 and exc in low_cond)
    if not clue:
        return False, "no_checkable_clue", ""
    if any(b in text for b in BANNED_PHRASES):
        return False, "banned_phrase", ""
    if redact(text) != text:
        return False, "sensitive_content", ""
    norm = " ".join(text.split())
    others = [r.get("text", "") for r in (active_rules or [])] + [t for t in (rejected_texts or []) if t]
    for other in others:
        if difflib.SequenceMatcher(None, norm, " ".join(str(other).split())).ratio() >= SIM_THRESHOLD:
            return False, "duplicate", ""
    return True, "", text


def generate_rule(trigger, input_text, active_rules, rejected_texts, llm_call, trigger_tool=None):
    """Ask the model and validate; up to MAX_ATTEMPTS. Never raises.

    Returns {"status": "ok"|"rejected_format"|"model_error", "attempts", "reason", "text", "parsed": {...}}.
    """
    if llm_call is None:
        return {"status": "model_error", "attempts": 0, "reason": "no_model", "text": "", "parsed": {}}
    reason, last_text, last_parsed = "", "", {}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = input_text if attempt == 1 else (
            input_text + "\n\n【上次输出不合格：%s。请重新只输出一个合格的 JSON 对象。】" % reason)
        try:
            raw = llm_call(prompt)
        except Exception as exc:
            return {"status": "model_error", "attempts": attempt, "reason": type(exc).__name__,
                    "text": "", "parsed": {}}
        obj = _parse_json(raw)
        if obj is None:
            reason = "not_json"
            continue
        last_parsed = {k: obj.get(k) for k in ("condition", "action", "tool_family") if isinstance(obj.get(k), str)}
        ok, reason, text = validate_rule(obj, trigger, active_rules, rejected_texts)
        if ok:
            return {"status": "ok", "attempts": attempt, "reason": "", "text": text, "parsed": last_parsed}
        last_text = "当%s（工具族 %s）时，%s" % (last_parsed.get("condition", ""), last_parsed.get("tool_family", ""),
                                             last_parsed.get("action", "")) if last_parsed else ""
    return {"status": "rejected_format", "attempts": MAX_ATTEMPTS, "reason": reason, "text": last_text[:300],
            "parsed": last_parsed}

# ---------------------------------------------------------------- entry point


def _recent_rejected(db, now_ts):
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now_ts - REJECT_COOLDOWN_S))
    try:
        with db.lock:
            rows = db.c.execute("select payload_json from policy_events where kind='rejected_format' and ts>=?",
                                (cutoff,)).fetchall()
    except Exception:
        return [], []
    keys, texts = [], []
    for (pj,) in rows:
        try:
            p = json.loads(pj or "{}")
        except Exception:
            continue
        if p.get("source_signature"):
            keys.append(p["source_signature"])
        if p.get("text"):
            texts.append(p["text"])
    return keys, texts


def _already_ran_today(db, loc):
    day_start = loc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    try:
        with db.lock:
            return db.c.execute("select 1 from events where kind='policy_reflect' and ts>=? limit 1",
                                (day_start,)).fetchone() is not None
    except Exception:
        return True  # cannot prove it has not run: do not risk a double run


def _reflect_core(profile_home, data_dir, db, llm_call, profile, now_ts):
    sdb = P.open_state_db(profile_home)
    adb = P.open_autonomy_db(profile_home)
    try:
        sigs = P.failure_signatures(sdb)
        ineff = P.inefficiency_triggers(adb, sdb)
        active = P.load_active_rules(data_dir)
        rej_keys, rej_texts = _recent_rejected(db, now_ts)
        trig = pick_trigger(sigs, ineff, active, rej_keys)
        if trig is None:
            return {"status": "no_trigger"}
        key = trig["key"]
        payload = {"channel": trig["channel"], "source_signature": key}
        db.record_policy_event(key, "triggered", "", payload, profile)
        if len(active) >= P.MAX_RULES:
            db.record_policy_event(key, "triggered", "limit", payload, profile)
            return {"status": "limit", "key": key}
        text_in, pm = build_reflection_input(trig, profile_home, sdb, adb, active)
        tool = trig["item"].get("tool") if trig["channel"] == "A" else (
            "seed_search" if trig["item"].get("kind") == "seed_search_repeat" else None)
        gen = generate_rule(trig, text_in, active, rej_texts, llm_call, tool)
        if gen["status"] != "ok":
            if gen["status"] == "rejected_format":
                db.record_policy_event(key, "rejected_format", gen["reason"],
                                       dict(payload, text=gen.get("text", "")), profile)
            return {"status": gen["status"], "key": key, "reason": gen.get("reason", "")}
        p = gen["parsed"]
        rule_id = "pr-" + hashlib.sha1(gen["text"].encode("utf-8")).hexdigest()[:10]
        res = P.add_rule(data_dir, rule_id, gen["text"], channel=trig["channel"],
                         tool_family=p.get("tool_family", ""), condition=p.get("condition", ""),
                         source_pm_id=(pm or {}).get("id", ""), source_signature=key,
                         created_run=profile or "")
        if not res.get("ok"):
            return {"status": "write_failed", "key": key, "reason": res.get("error", "")}
        db.record_policy_event(rule_id, "generated", "", dict(payload, attempts=gen["attempts"]), profile)
        return {"status": "generated", "key": key, "rule_id": rule_id}
    finally:
        for c in (sdb, adb):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass


def run_policy_reflection(profile_home, data_dir, db, now=None, llm_call=None, profile="", run_id=None,
                          has_chat=True):
    """Run the nightly reflection once per local day inside 03:00-03:39. Never raises.

    Returns {"executed": bool, "status": str, ...}. executed=True means the caller
    should treat this tick as the reflection run (skip normal prompt/L0/surprise).
    has_chat=False (CLI transport has no chat) records policy_reflect_skipped instead of
    running: no rule is produced and it is not counted as a success.
    """
    try:
        loc = _local(now)
        if not (loc.hour == REFLECT_HOUR and loc.minute <= REFLECT_MINUTE_MAX):
            return {"executed": False, "status": "outside_window"}
        if _already_ran_today(db, loc):
            return {"executed": False, "status": "already_ran"}
    except Exception as exc:
        return {"executed": False, "status": "error", "error": type(exc).__name__}
    now_ts = loc.timestamp()
    run = run_id or uuid.uuid4().hex
    if not has_chat:
        skip = {"status": "policy_reflect_skipped", "reason": "transport_no_chat"}
        try:
            db.append_event(ts=now_ts, run_id=run, kind="policy_reflect", exit_reason=skip["status"],
                            summary=json.dumps(skip, ensure_ascii=False))
        except Exception:
            pass
        return dict(skip, executed=True)
    try:
        res = _reflect_core(profile_home, data_dir, db, llm_call, profile, now_ts)
    except Exception as exc:
        res = {"status": "error", "error": type(exc).__name__}
    try:
        db.append_event(ts=now_ts, run_id=run, kind="policy_reflect", exit_reason=str(res.get("status")),
                        summary=json.dumps({k: v for k, v in res.items() if k != "text"}, ensure_ascii=False,
                                           default=str)[:1200])
    except Exception:
        pass
    return dict(res, executed=True)
