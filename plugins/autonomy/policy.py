"""Policy-revision data layer (v1): read-only, offline, stdlib only.

Public API (stable for T3/T4/T5):
  open_state_db(profile_home) -> sqlite3.Connection | None
  open_autonomy_db(profile_home) -> sqlite3.Connection | None
  map_runs(autonomy_db, state_db=None, days=14) -> {"mapped","total","ratio","run_to_session"}
  normalize_error(text) -> str
  tool_family(tool_name) -> str
  failure_signatures(state_db, days=14, min_trigger=3) -> [signature dicts]
  inefficiency_triggers(autonomy_db, state_db=None, days=14) -> {"no_change","seed_search_repeat","total"}
  opportunities(state_db, tool_family, since_ts) -> {"family","sessions","attempts","fails","fail_rate"}
  baseline(autonomy_db, state_db, tool_family, until_ts, days=14) -> {...}
  build_report(profile_home) -> dict      (CLI: --profile-home P --report)

Every function degrades (returns None / empty structure) instead of raising,
so a tick can never be broken by this module.
"""
import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

WINDOW_DAYS = 14
TRIGGER_MIN_COUNT = 3

# ---------------------------------------------------------------- connections


def _open_ro(path):
    try:
        p = Path(path)
        if not p.is_file():
            return None
        conn = sqlite3.connect("file:%s?mode=ro" % p.resolve().as_posix(), uri=True, timeout=3.0)
        conn.execute("pragma busy_timeout=3000")
        conn.execute("select 1 from sqlite_master limit 1").fetchall()
        return conn
    except Exception:
        return None


def open_state_db(profile_home):
    """Read-only handle on <profile_home>/state.db, or None if missing/locked."""
    return _open_ro(Path(profile_home) / "state.db")


def open_autonomy_db(profile_home):
    return _open_ro(Path(profile_home) / "autonomy_data" / "autonomy.db")


def _q(conn, sql, args=()):
    """Run a query; return [] on any sqlite error (locked, missing table...)."""
    if conn is None:
        return []
    try:
        return conn.execute(sql, args).fetchall()
    except Exception:
        return []


def _since(days):
    return time.time() - days * 86400

# ---------------------------------------------------------------- tool family

_FAMILY = {
    "terminal": "terminal", "process": "terminal", "bash": "terminal",
    "execute_code": "execute_code",
    "write_file": "write_file", "patch": "write_file",
    "read_file": "read_file", "search_files": "read_file",
    "seed_search": "seed_search", "seed_memory": "seed_search",
    "memory": "memory", "session_search": "memory",
    "web_search": "web", "web_extract": "web",
    "browser_exec": "browser", "browser_vault": "browser",
    "skill_manage": "skill", "skill_view": "skill", "skills_list": "skill",
    "delegate_task": "delegate",
    "vision_analyze": "vision",
}


def tool_family(tool_name):
    n = (tool_name or "").strip().lower()
    if not n:
        return "other"
    if n in _FAMILY:
        return _FAMILY[n]
    if n.startswith("browser"):
        return "browser"
    if n.startswith("seed_"):
        return "seed_search"
    if n.startswith("skill"):
        return "skill"
    return "other"


def _family_members(family):
    return sorted(k for k, v in _FAMILY.items() if v == family)

# ---------------------------------------------------------------- normalise

_RE_TS = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?\b")
_RE_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_RE_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F-]{4,}\b")
_RE_LONGHEX = re.compile(r"\b[0-9a-fA-F]{12,}\b")
_RE_PID = re.compile(r"\b(pid|PID|process)[ =:]*\d+")
_RE_WINPATH = re.compile(r"\b[A-Za-z]:\\[^\s'\"]*")
_RE_PATH = re.compile(r"(?<![\w.])(?:~|\.{0,2})?(?:/[\w.@+\-~]+){2,}/?")
_RE_QUOTED = re.compile(r"""'[^'\n]{0,200}'|"[^"\n]{0,200}\"""")
_RE_NUM = re.compile(r"(?<![A-Za-z_])\d+(\.\d+)?")
_RE_WS = re.compile(r"\s+")


def normalize_error(text):
    """Strip jittery parts (timestamps, pids, addresses, paths, quoted values, numbers)."""
    s = str(text or "")
    s = _RE_TS.sub("<ts>", s)
    s = _RE_PID.sub("pid", s)
    s = _RE_HEX.sub("<addr>", s)
    s = _RE_UUID.sub("<id>", s)
    s = _RE_LONGHEX.sub("<id>", s)
    s = _RE_WINPATH.sub("<path>", s)
    s = _RE_PATH.sub("<path>", s)
    s = _RE_QUOTED.sub("<q>", s)
    s = _RE_NUM.sub("<n>", s)
    return _RE_WS.sub(" ", s).strip()[:300]

# ---------------------------------------------------------------- real errors

_RE_EXC = re.compile(r"^\s*((?:[A-Za-z_][\w]*\.)*[A-Za-z_]\w*(?:Error|Exception|Exit|Interrupt|Warning|Timeout|Refused|Denied))\b\s*:?")
_RE_EXC_LOOSE = re.compile(r"^\s*((?:[A-Za-z_][\w]*\.)*[A-Z]\w+):\s")

# SQL prefilter: only rows that could be real errors are JSON-parsed.
_PREFILTER = (
    "(content LIKE '%Traceback%' OR content LIKE '%\"success\": false%' OR content LIKE '%\"success\":false%'"
    " OR content LIKE '%\"error\": \"%' OR content LIKE '%\"error\":\"%'"
    " OR (content LIKE '%\"exit_code\":%' AND content NOT LIKE '%\"exit_code\": 0%' AND content NOT LIKE '%\"exit_code\":0%'))"
)


def _last_nonempty(text):
    for ln in reversed(str(text).splitlines()):
        if ln.strip():
            return ln.strip()
    return ""


def classify_result(content):
    """Return None if not a real error, else (exc_type, last_line).

    Real error: success=false, non-empty error, exit_code != 0, or Traceback.
    error=null / empty is NOT an error.
    """
    if not content:
        return None
    text = content if isinstance(content, str) else str(content)
    obj = None
    if text.lstrip().startswith("{"):
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
    reasons = []
    body = text
    err_txt = ""
    if isinstance(obj, dict):
        if obj.get("success") is False:
            reasons.append("success_false")
        e = obj.get("error")
        if e not in (None, "", [], {}):
            reasons.append("error_field")
            err_txt = e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)
        ec = obj.get("exit_code")
        if isinstance(ec, int) and not isinstance(ec, bool) and ec != 0:
            reasons.append("exit_%d" % ec)
        out = obj.get("output")
        body = out if isinstance(out, str) else text
    has_tb = "Traceback (most recent call last)" in body or "Traceback (most recent call last)" in text
    if has_tb:
        reasons.append("traceback")
    if not reasons:
        return None
    # exception type + last line
    last_line = ""
    exc = ""
    if has_tb:
        seg = body.rsplit("Traceback (most recent call last)", 1)[-1] if "Traceback (most recent call last)" in body \
            else text.rsplit("Traceback (most recent call last)", 1)[-1]
        # the exception line is the first non-indented line after the frames
        for ln in seg.splitlines()[1:]:
            if ln.strip() and not ln.startswith((" ", "\t")):
                m = _RE_EXC.match(ln) or _RE_EXC_LOOSE.match(ln)
                if m:
                    exc, last_line = m.group(1), ln.strip()
                    break
        if not last_line:
            last_line = _last_nonempty(seg)
            m = _RE_EXC.match(last_line) or _RE_EXC_LOOSE.match(last_line)
            exc = m.group(1) if m else "Traceback"
    else:
        last_line = _last_nonempty(err_txt) or _last_nonempty(body)
        m = _RE_EXC.match(last_line) or _RE_EXC_LOOSE.match(last_line)
        exc = m.group(1) if m else reasons[0]
    return exc.split(".")[-1], last_line[:300]


def _sig(tool, exc, last_norm):
    return hashlib.sha1(("%s|%s|%s" % (tool, exc, last_norm)).encode("utf-8", "replace")).hexdigest()[:12]


def failure_signatures(state_db, days=WINDOW_DAYS, min_trigger=TRIGGER_MIN_COUNT, since_ts=None):
    """Aggregate real tool errors by hash(tool + exc_type + normalised last line)."""
    start = since_ts if since_ts is not None else _since(days)
    rows = _q(state_db,
              "select tool_name, content, session_id, timestamp from messages "
              "where role='tool' and timestamp>=? and " + _PREFILTER + " order by timestamp", (start,))
    agg = {}
    for tool, content, sid, ts in rows:
        r = classify_result(content)
        if r is None:
            continue
        exc, last = r
        norm = normalize_error(last)
        key = _sig(tool or "", exc, norm)
        d = agg.get(key)
        if d is None:
            d = agg[key] = {"signature": key, "tool": tool or "", "tool_family": tool_family(tool),
                            "exc_type": exc, "last_line_norm": norm, "count": 0,
                            "first_ts": ts, "last_ts": ts, "sample_sessions": [],
                            "sample_text": last[:200]}
        d["count"] += 1
        d["last_ts"] = max(d["last_ts"], ts)
        d["first_ts"] = min(d["first_ts"], ts)
        if sid and sid not in d["sample_sessions"] and len(d["sample_sessions"]) < 3:
            d["sample_sessions"].append(sid)
    out = sorted(agg.values(), key=lambda x: -x["count"])
    for d in out:
        d["triggerable"] = d["count"] >= min_trigger
    return out

# ---------------------------------------------------------------- run mapping

_RE_SESSION = re.compile(r"session=(\S+)")


def map_runs(autonomy_db, state_db=None, days=WINDOW_DAYS):
    """run_id -> session_id via transcript events. state_db (optional) verifies the session exists."""
    rows = _q(autonomy_db, "select run_id, summary, exit_reason from events where kind='transcript' and ts>=?",
              (_since(days),))
    total_runs, mapping = set(), {}
    for run_id, summary, _er in rows:
        if not run_id:
            continue
        total_runs.add(run_id)
        m = _RE_SESSION.search(summary or "")
        if m:
            mapping[run_id] = m.group(1).rstrip(",;")
    if state_db is not None and mapping:
        have = {r[0] for r in _q(state_db, "select id from sessions where started_at>=?", (_since(days + 1),))}
        if have:
            mapping = {k: v for k, v in mapping.items() if v in have}
    total = len(total_runs)
    return {"mapped": len(mapping), "total": total,
            "ratio": round(len(mapping) / total, 4) if total else 0.0,
            "run_to_session": mapping}

# ---------------------------------------------------------------- B channel

_RE_NOCHANGE = re.compile(r"consecutive_no_change=(\d+)")


def _no_change_triggers(autonomy_db, days):
    rows = _q(autonomy_db, "select ts, run_id, summary from events where kind='l0_check' and ts>=? order by ts",
              (_since(days),))
    series = []
    for ts, run_id, summary in rows:
        m = _RE_NOCHANGE.search(summary or "")
        if m:
            series.append((ts, run_id, int(m.group(1))))
    hits = [{"ts": ts, "run_id": rid, "consecutive_no_change": n} for ts, rid, n in series if n >= 3]
    streaks = sum(1 for ts, rid, n in series if n == 3)  # each streak reaches 3 exactly once
    return {"channel": "B", "kind": "l0_no_change", "events_ge3": len(hits), "streaks_reaching_3": streaks,
            "max_streak": max([n for _, _, n in series], default=0),
            "triggered": len(hits) >= 2, "recent": hits[-5:]}


def _seed_search_repeats(state_db, days):
    rows = _q(state_db,
              "select session_id, tool_calls from messages where role='assistant' and timestamp>=? "
              "and tool_calls LIKE '%seed_search%'", (_since(days),))
    per = defaultdict(Counter)
    for sid, tc in rows:
        try:
            calls = json.loads(tc)
        except Exception:
            continue
        for c in calls if isinstance(calls, list) else []:
            fn = (c or {}).get("function") or {}
            args = fn.get("arguments") or ""
            if fn.get("name") == "seed_search" or "seed_search" in args:
                per[sid][normalize_error(args)] += 1
    reps = []
    for sid, cnt in per.items():
        for q, n in cnt.items():
            if n >= 2:
                reps.append({"session_id": sid, "query_norm": q[:120], "repeats": n})
    reps.sort(key=lambda x: -x["repeats"])
    return {"channel": "B", "kind": "seed_search_repeat", "sessions_affected": len({r["session_id"] for r in reps}),
            "triggered": bool(reps), "items": reps[:10]}


def inefficiency_triggers(autonomy_db, state_db=None, days=WINDOW_DAYS):
    nc = _no_change_triggers(autonomy_db, days)
    ss = _seed_search_repeats(state_db, days)
    return {"no_change": nc, "seed_search_repeat": ss,
            "total": int(nc["triggered"]) + int(ss["triggered"])}

# ---------------------------------------------------------------- opportunities / baseline


def opportunities(state_db, tool_family, since_ts):
    """Opportunity = sessions that called this tool family since since_ts."""
    if tool_family == "other":
        known = sorted(_FAMILY)
        cond, args = "tool_name NOT IN (%s)" % ",".join("?" * len(known)), known
    else:
        names = _family_members(tool_family)
        if not names:
            return {"family": tool_family, "sessions": 0, "attempts": 0, "fails": 0, "fail_rate": 0.0}
        cond, args = "tool_name IN (%s)" % ",".join("?" * len(names)), names
    rows = _q(state_db, "select count(distinct session_id), count(*) from messages where role='tool' and timestamp>=? and "
              + cond, [since_ts] + list(args))
    sessions, attempts = (rows[0] if rows else (0, 0))
    err_rows = _q(state_db, "select content from messages where role='tool' and timestamp>=? and " + cond
                  + " and " + _PREFILTER, [since_ts] + list(args))
    fails = sum(1 for (c,) in err_rows if classify_result(c) is not None)
    return {"family": tool_family, "sessions": sessions or 0, "attempts": attempts or 0, "fails": fails,
            "fail_rate": round(fails / attempts, 4) if attempts else 0.0}


def baseline(autonomy_db, state_db, tool_family, until_ts, days=WINDOW_DAYS):
    """Metrics for the `days` before until_ts (i.e. before a rule took effect)."""
    start = until_ts - days * 86400
    if tool_family == "other":
        known = sorted(_FAMILY)
        cond, args = "tool_name NOT IN (%s)" % ",".join("?" * len(known)), known
    else:
        names = _family_members(tool_family) or ["\0"]
        cond, args = "tool_name IN (%s)" % ",".join("?" * len(names)), names
    att = _q(state_db, "select count(*) from messages where role='tool' and timestamp>=? and timestamp<? and " + cond,
             [start, until_ts] + list(args))
    attempts = att[0][0] if att else 0
    errs = _q(state_db, "select content from messages where role='tool' and timestamp>=? and timestamp<? and " + cond
              + " and " + _PREFILTER, [start, until_ts] + list(args))
    fails = sum(1 for (c,) in errs if classify_result(c) is not None)
    ticks = _q(autonomy_db, "select count(distinct run_id) from events where kind='transcript' and ts>=? and ts<?",
               (start, until_ts))
    n_ticks = ticks[0][0] if ticks else 0
    l0 = _q(autonomy_db, "select exit_reason, count(*) from events where kind='l0_check' and ts>=? and ts<? group by 1",
            (start, until_ts))
    l0d = dict(l0)
    l0_total = sum(l0d.values())
    return {"family": tool_family, "since_ts": start, "until_ts": until_ts,
            "attempts": attempts, "fails": fails,
            "fail_rate": round(fails / attempts, 4) if attempts else 0.0,
            "ticks": n_ticks,
            "attempts_per_tick": round(attempts / n_ticks, 3) if n_ticks else 0.0,
            "l0_checks": l0_total,
            "l0_changed_rate": round(l0d.get("changed", 0) / l0_total, 4) if l0_total else 0.0}

# ---------------------------------------------------------------- rule store (T3)

MAX_RULES = 5
_log = logging.getLogger(__name__)
_REQUIRED = ("id", "text")


def _rules_path(data_dir):
    return Path(data_dir) / "policy" / "active.json"


def _norm_rule(r):
    if not isinstance(r, dict) or not all(isinstance(r.get(k), str) and r.get(k).strip() for k in _REQUIRED):
        return None
    r = dict(r)
    r.setdefault("channel", "A")
    r.setdefault("tool_family", "")
    r.setdefault("condition", "")
    r.setdefault("source_pm_id", "")
    r.setdefault("source_signature", "")
    r.setdefault("created_at", time.time())
    r.setdefault("created_run", "")
    r.setdefault("ttl_opportunities", 30)
    r.setdefault("ttl_days", 7)
    r.setdefault("status", "candidate")
    r.setdefault("baseline", {"fail_rate": 0.0, "attempts_per_tick": 0.0, "l0_changed_rate": 0.0})
    r.setdefault("stats", {"opportunities": 0, "fails": 0, "reuse_sessions": []})
    return r


def load_active_rules(data_dir):
    """Rules from active.json; [] on missing/corrupt/invalid. Never raises."""
    try:
        if not data_dir:
            return []
        p = _rules_path(data_dir)
        if not p.is_file():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        out = [x for x in (_norm_rule(r) for r in data) if x is not None]
        return out[:MAX_RULES]
    except Exception as e:
        _log.debug("policy: load_active_rules failed: %s", type(e).__name__)
        return []


def save_active_rules(data_dir, rules):
    """Atomic write. Returns {"ok": True} or {"ok": False, "error": ...}; never raises."""
    tmp = None
    try:
        if not isinstance(rules, list):
            return {"ok": False, "error": "rules_not_list"}
        if len(rules) > MAX_RULES:
            return {"ok": False, "error": "limit_exceeded", "max": MAX_RULES, "got": len(rules)}
        p = _rules_path(data_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name("%s.%d.tmp" % (p.name, os.getpid()))
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return {"ok": True}
    except Exception as e:
        try:
            if tmp is not None and Path(tmp).exists():
                Path(tmp).unlink()
        except Exception:
            pass
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:120])}


def add_rule(data_dir, rule_id, text, channel="A", tool_family="", condition="", source_pm_id="",
             source_signature="", created_run="", ttl_opportunities=30, ttl_days=7, baseline=None):
    """Append a candidate rule. Returns {"ok", "rule"|"error"}; 6th rule is rejected."""
    try:
        rules = load_active_rules(data_dir)
        if any(r["id"] == rule_id for r in rules):
            return {"ok": False, "error": "duplicate_id"}
        if len(rules) >= MAX_RULES:
            return {"ok": False, "error": "limit_exceeded", "max": MAX_RULES}
        r = _norm_rule({
            "id": rule_id, "text": text, "channel": channel, "tool_family": tool_family,
            "condition": condition, "source_pm_id": source_pm_id, "source_signature": source_signature,
            "created_at": time.time(), "created_run": created_run,
            "ttl_opportunities": ttl_opportunities, "ttl_days": ttl_days, "status": "candidate",
            "baseline": baseline or {"fail_rate": 0.0, "attempts_per_tick": 0.0, "l0_changed_rate": 0.0},
            "stats": {"opportunities": 0, "fails": 0, "reuse_sessions": []}})
        if r is None:
            return {"ok": False, "error": "invalid_rule"}
        res = save_active_rules(data_dir, rules + [r])
        if not res.get("ok"):
            return res
        return {"ok": True, "rule": r}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:120])}


def remove_rule(data_dir, rule_id, reason="", source="auto", db=None, profile=""):
    """Remove a rule; if db (AutonomyDB) is given, record a policy_events row.

    source="manual_rollback" -> kind rollback_manual (T4 then calls
    record_program_memory_outcome(..., source="manual_rollback")). Event only; no store call here.
    source="expired" -> expired_neutral; anything else -> vetoed.
    Returns {"ok", "removed": rule|None, "source", "reason"}.
    """
    try:
        rules = load_active_rules(data_dir)
        hit = next((r for r in rules if r["id"] == rule_id), None)
        if hit is None:
            return {"ok": False, "error": "not_found", "removed": None}
        res = save_active_rules(data_dir, [r for r in rules if r["id"] != rule_id])
        if not res.get("ok"):
            return dict(res, removed=None)
        if db is not None:
            try:
                kind = "rollback_manual" if source == "manual_rollback" else (
                    "expired_neutral" if source == "expired" else "vetoed")
                db.record_policy_event(rule_id, kind, reason,
                                       {"source": source, "source_pm_id": hit.get("source_pm_id", "")}, profile)
            except Exception:
                pass
        return {"ok": True, "removed": hit, "source": source, "reason": reason}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:120]), "removed": None}


def rules_for_prompt(data_dir):
    """['[policy:<id>] text', ...] for candidate/promoted rules. [] on any failure."""
    try:
        return ["[policy:%s] %s" % (r["id"], " ".join(r["text"].split()))
                for r in load_active_rules(data_dir) if r.get("status") in ("candidate", "promoted")]
    except Exception:
        return []

# ---------------------------------------------------------------- report / CLI


def build_report(profile_home, days=WINDOW_DAYS):
    t0 = time.time()
    home = Path(profile_home)
    sdb, adb = open_state_db(home), open_autonomy_db(home)
    rep = {"profile_home": home.name, "state_db_open": sdb is not None, "autonomy_db_open": adb is not None}
    try:
        m = map_runs(adb, sdb, days)
        rep["coverage"] = {k: m[k] for k in ("mapped", "total", "ratio")}
        sigs = failure_signatures(sdb, days)
        rep["signature_count"] = len(sigs)
        rep["triggerable_count"] = sum(1 for s in sigs if s["triggerable"])
        rep["top_signatures"] = [{k: (s[k][:200] if isinstance(s[k], str) else s[k]) for k in
                                  ("signature", "tool", "tool_family", "exc_type", "last_line_norm", "count",
                                   "triggerable", "sample_text")} for s in sigs[:10]]
        tb = [s for s in sigs if s["tool"] == "terminal" and s["exc_type"] not in
              ("success_false", "error_field") and not s["exc_type"].startswith("exit_")]
        rep["terminal_traceback_signatures"] = {"distinct": len(tb), "total_count": sum(s["count"] for s in tb)}
        trig = inefficiency_triggers(adb, sdb, days)
        rep["b_channel"] = {"no_change_events_ge3": trig["no_change"]["events_ge3"],
                            "no_change_triggered": trig["no_change"]["triggered"],
                            "seed_search_repeat_sessions": trig["seed_search_repeat"]["sessions_affected"],
                            "triggered_total": trig["total"]}
    except Exception as e:  # never explode
        rep["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
    rep["elapsed_s"] = round(time.time() - t0, 3)
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(description="policy data-layer self check")
    ap.add_argument("--profile-home", required=True)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--days", type=int, default=WINDOW_DAYS)
    a = ap.parse_args(argv)
    print(json.dumps(build_report(a.profile_home, a.days), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
