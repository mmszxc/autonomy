"""Measured chassis status and optional daily journal support.

All host state reads degrade to ``未知``. Journal failures are isolated by the
Runner and never block an autonomy tick.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

_DEFAULT = {"enabled": True, "window_local": "21:00-23:59", "tz": "Asia/Shanghai", "reflection_max_chars": 800, "inject_max_chars": 400}
_EXTERNAL_ERROR = re.compile(r"\b5\d\d\b|timeout|timed out|gateway|connection|rate.?limit", re.I)
_PATH_RE = re.compile(r"(?:/[^\s'\"`]+|[A-Za-z]:\\[^\s'\"`]+)")
_REFLECTION_RE = re.compile(r"【今日体悟】\s*(.*?)(?=\n【|\Z)", re.S)


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def _unknown(fn):
    try:
        return fn()
    except Exception:
        return "未知"


def build_chassis_status(hermes_home, budget, autonomy_db=None) -> str:
    """Return a <=150-char measured status line; each component degrades alone."""
    home = Path(hermes_home)
    seed_db = home / "seed_data/seeds/seeds.db"
    queue = home / "seed_data/seed_queue"

    def seeds():
        c = _ro(seed_db)
        try:
            total = int(c.execute("select count(*) from seeds").fetchone()[0])
        finally:
            c.close()
        delta = "未知"
        if autonomy_db is not None:
            row = autonomy_db.c.execute("select value from kv where key='chassis_seed_count'").fetchone()
            if row:
                delta = total - int(row[0])
            autonomy_db.c.execute("insert into kv(key,value,updated_at) values('chassis_seed_count',?,?) on conflict(key) do update set value=excluded.value,updated_at=excluded.updated_at", (str(total), __import__('time').time()))
            autonomy_db.c.commit()
        return f"可读/总{total}/增{delta}"

    backlog = _unknown(lambda: len(list(queue.glob("*.json"))))
    failed = _unknown(lambda: len(list((queue / "_failed").glob("*.json"))))

    def tagger():
        d = json.loads((queue / "tagger_health.json").read_text(encoding="utf-8"))
        return "%s/连败%s/探测%s" % (d.get("state", "未知"), d.get("consecutive_failures", "未知"), d.get("next_probe_at", "未知"))

    def remaining():
        s = budget.status()
        return "次%d/tok%d" % (max(0, int(s["calls_limit"]) - int(s["calls"])), max(0, int(s["tokens_limit"]) - int(s["tokens"])))

    fallback = ""
    state_db = home / "state.db"
    try:
        c = _ro(state_db)
        row = c.execute("select model_config from sessions where source='oneshot' order by started_at desc limit 1").fetchone()
        c.close()
        if row and row[0]:
            cfg = json.loads(row[0])
            if "fallback_active" in cfg:
                fallback = "，上轮备用=" + ("是" if cfg.get("fallback_active") else "否")
    except Exception:
        pass
    line = "【底盘状态（代码实测）】种子%s，队列%s，失败%s，标注%s，预算%s%s。" % (
        _unknown(seeds), backlog, failed, _unknown(tagger), _unknown(remaining), fallback)
    return line[:150]


class JournalManager:
    def __init__(self, hermes_home, autonomy_db, config=None, now=None, plugin_file=None):
        self.home = Path(hermes_home)
        self.db = autonomy_db
        self.cfg = dict(_DEFAULT)
        if isinstance(config, dict):
            self.cfg.update(config)
        self.now_fn = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self.dir = Path(getattr(autonomy_db, "path", self.home / "autonomy_data/autonomy.db")).parent / "journal"
        self.state_db = self.home / "state.db"
        pf = Path(plugin_file or __file__)
        try:
            self.version = hashlib.sha256(pf.read_bytes()).hexdigest()[:16]
        except Exception:
            self.version = "unknown"
        self.pending_path = None

    def _local_now(self):
        value = self.now_fn()
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(ZoneInfo(str(self.cfg["tz"])))

    def _bounds(self, day):
        tz = ZoneInfo(str(self.cfg["tz"]))
        a = dt.datetime.combine(day, dt.time(), tzinfo=tz)
        b = a + dt.timedelta(days=1)
        return a.timestamp(), b.timestamp()

    def in_window(self):
        now = self._local_now()
        lo, hi = str(self.cfg["window_local"]).split("-", 1)
        parse = lambda x: dt.time.fromisoformat(x)
        return parse(lo) <= now.time().replace(tzinfo=None) <= parse(hi)

    def _sessions(self, day):
        a, b = self._bounds(day)
        c = _ro(self.state_db)
        try:
            rows = c.execute("select id,started_at,ended_at from sessions where source='oneshot' and started_at>=? and started_at<? order by started_at", (a, b)).fetchall()
        finally:
            c.close()
        return rows

    @staticmethod
    def _calls(raw):
        try:
            value = json.loads(raw or "[]")
            return value if isinstance(value, list) else []
        except Exception:
            return []

    @staticmethod
    def _call(call):
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(fn.get("name") or call.get("name") or "unknown")
        args = fn.get("arguments", call.get("arguments", ""))
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        return name, args, str(call.get("id") or "")

    def _facts(self, day, sessions):
        ids = [str(x[0]) for x in sessions]
        tools, paths, indexed = Counter(), [], []
        if ids:
            c = _ro(self.state_db)
            c.row_factory = sqlite3.Row
            try:
                marks = ",".join("?" for _ in ids)
                rows = c.execute(f"select session_id,role,tool_calls,tool_call_id,tool_name,content,timestamp from messages where session_id in ({marks}) order by timestamp,rowid", ids).fetchall()
                results = {str(r["tool_call_id"]): r for r in rows if r["role"] == "tool" and r["tool_call_id"]}
                for r in rows:
                    if r["role"] != "assistant":
                        continue
                    for call in self._calls(r["tool_calls"]):
                        name, args, cid = self._call(call)
                        tools[name] += 1
                        found = _PATH_RE.findall(args)
                        is_write = name.lower() in {"write_file", "edit_file", "patch", "apply_patch", "create_file"} or bool(re.search(r">>|(?<![<>])>(?![>&])|write_text|write_bytes|open\([^\n]{0,100}['\"](?:w|a|x)", args, re.I))
                        if is_write:
                            for p in found:
                                if p not in paths and len(paths) < 20:
                                    paths.append(p.rstrip(",.;)"))
                        rr = results.get(cid)
                        result = str(rr["content"] or "") if rr else ""
                        status = "ok"
                        m = re.search(r"(?:exit[_ ]?code|returncode)[=: ]+(-?\d+)", result, re.I)
                        exit_code = int(m.group(1)) if m else None
                        if exit_code not in (None, 0):
                            status = "failed"
                        duration = max(0.0, float(rr["timestamp"]) - float(r["timestamp"])) if rr and rr["timestamp"] and r["timestamp"] else None
                        indexed.append((float(r["timestamp"] or 0), str(r["session_id"]), name, found[0] if found else None, exit_code, status, duration))
            finally:
                c.close()
        a, b = self._bounds(day)
        ev = self.db.c.execute("select exit_reason,summary from events where ts>=? and ts<? and kind='tick'", (a, b)).fetchall()
        success = sum(str(x[0]) == "ok" for x in ev)
        failures = len(ev) - success
        controlled = []
        for reason, summary in ev:
            text = "%s: %s" % (reason or "", summary or "")
            if reason != "ok" and not _EXTERNAL_ERROR.search(text):
                controlled.append(text[:160])
        return {"rounds": len(sessions), "success": success, "failures": failures, "tools": tools, "paths": paths, "errors": controlled[:5], "index": indexed}

    def _ensure_index(self, rows):
        self.db.c.execute("""create table if not exists event_index(
            id integer primary key autoincrement, event_time real not null, round_id text not null,
            tool_name text not null, target_path text, exit_code integer, status text,
            duration_ms integer, chassis_version text not null,
            unique(round_id,event_time,tool_name,target_path))""")
        self.db.c.executemany("insert or ignore into event_index(event_time,round_id,tool_name,target_path,exit_code,status,duration_ms,chassis_version) values(?,?,?,?,?,?,?,?)", [
            (ts, sid, tool, path, code, status, int(duration * 1000) if duration is not None else None, self.version)
            for ts, sid, tool, path, code, status, duration in rows
        ])
        self.db.c.commit()

    def _continuation(self, day, facts):
        previous = self.dir / ((day - dt.timedelta(days=1)).isoformat() + ".md")
        if not previous.exists():
            return "昨日无日记"
        text = previous.read_text(encoding="utf-8")
        pending = " ".join(line for line in text.splitlines() if "未完成" in line or "没做完" in line)
        if not pending:
            return "昨日未记录未完成项"
        haystack = " ".join(list(facts["tools"].keys()) + facts["paths"])
        tokens = [x for x in re.findall(r"[A-Za-z0-9_.\-/]{4,}|[\u4e00-\u9fff]{4,}", pending) if x not in {"未完成续接", "还有什么没做完"}]
        hits = [x for x in tokens if x in haystack]
        return "已出现：" + "、".join(hits[:5]) if hits else "今日工具/路径未见同项"

    def _summary(self, facts, continuation):
        tools = "、".join("%s×%d" % x for x in facts["tools"].most_common()) or "无"
        paths = "、".join(facts["paths"]) or "无"
        errors = "；".join(facts["errors"]) or "无"
        return "轮次：%d（成功%d/失败%d）\n工具：%s\n写过路径：%s\n可控错误：%s\n昨日未完成续接：%s" % (facts["rounds"], facts["success"], facts["failures"], tools, paths, errors, continuation)

    def prepare(self):
        """Return (yesterday injection, optional reflection invitation)."""
        if not self.cfg.get("enabled", True):
            return "", ""
        now = self._local_now()
        self.dir.mkdir(parents=True, exist_ok=True)
        yesterday = self.dir / ((now.date() - dt.timedelta(days=1)).isoformat() + ".md")
        injection = ""
        if yesterday.exists():
            injection = "【昨日日记】\n" + yesterday.read_text(encoding="utf-8")[:int(self.cfg["inject_max_chars"])]
        invitation = ""
        today = self.dir / (now.date().isoformat() + ".md")
        if self.in_window() and not today.exists():
            sessions = self._sessions(now.date())
            if sessions:
                facts = self._facts(now.date(), sessions)
                summary = self._summary(facts, self._continuation(now.date(), facts))
                today.write_text("# %s 日记骨架\n\n%s\n" % (now.date().isoformat(), summary), encoding="utf-8")
                self._ensure_index(facts["index"])
                self.pending_path = today
                invitation = "【今晚日记邀请】\n%s\n可以不写。若愿意，请在【今日体悟】标题下回答：今天什么最重要？我有什么变化？还有什么没做完？" % summary[:400]
        return injection, invitation

    def finalize(self, output):
        if not self.pending_path:
            return
        raw = str(output or "")
        match = _REFLECTION_RE.search(raw)
        if match:
            text = match.group(1).strip()
        else:
            # Models occasionally answer the invitation with a natural label
            # instead of reproducing the exact requested heading.
            marker = next((x for x in ("本轮感受：", "独处心情：", "本轮情绪：") if x in raw), "")
            text = raw.split(marker, 1)[1].strip() if marker else ""
        text = text[:int(self.cfg["reflection_max_chars"])]
        if text:
            with self.pending_path.open("a", encoding="utf-8") as fh:
                fh.write("\n【今日体悟】\n" + text + "\n")
