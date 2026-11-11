"""CLI commands for `hermes autonomy {init,stop,resume,status,tick}`."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .budget import Budget
from .circuit import Circuit
from .config import (
    determine_mode,
    get_autonomy_section,
    load_raw_config,
    resolve_autonomy_model,
    resolve_hermes_home,
    resolve_main_model,
    resolve_paths,
)
from .db import AutonomyDB
from .memory_bridge import get_configured_memory_provider
from .tick import main as tick_main
from .wizard import run_init_wizard


def _utf8_stdio() -> None:
    """Best-effort UTF-8 output without breaking embedded/test callers."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def cmd_stop(hermes_home: Optional[str | Path] = None) -> Dict[str, Any]:
    """Create STOP_AUTONOMY file to pause autonomous thinking."""
    raw_cfg = load_raw_config(hermes_home)
    paths = resolve_paths(raw_cfg, hermes_home=hermes_home)
    stop_path = Path(paths["stop_file"])
    stop_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path.write_text(f"stopped_at={time.time():.6f}\n", encoding="utf-8")
    return {
        "ok": True,
        "action": "stop",
        "stopped": True,
        "stop_file": str(stop_path),
    }


def cmd_resume(hermes_home: Optional[str | Path] = None) -> Dict[str, Any]:
    """Delete STOP_AUTONOMY file to resume autonomous thinking."""
    raw_cfg = load_raw_config(hermes_home)
    paths = resolve_paths(raw_cfg, hermes_home=hermes_home)
    stop_path = Path(paths["stop_file"])
    existed = stop_path.exists()
    if existed:
        stop_path.unlink(missing_ok=True)
    return {
        "ok": True,
        "action": "resume",
        "stopped": False,
        "removed_stop_file": existed,
        "stop_file": str(stop_path),
    }


_THOUGHT_HEADER = re.compile(r"^=== \[(?P<ts>[^]]+)\] run_id=(?P<run_id>\S+) status=(?P<status>\S+)(?: mode=(?P<mode>\S+))?(?: model=(?P<model>\S+))? \((?P<tokens>[^)]*)\) ===$")


def parse_thought_entries(path: str | Path, max_bytes: int = 4 * 1024 * 1024) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return []
    with p.open("rb") as fh:
        size = p.stat().st_size
        fh.seek(max(0, size - max_bytes))
        text = fh.read().decode("utf-8", errors="replace")
    start = text.find("=== [")
    if start > 0:
        text = text[start:]
    entries: List[Dict[str, Any]] = []
    current = None
    for line in text.splitlines():
        match = _THOUGHT_HEADER.match(line.strip())
        if match:
            if current is not None:
                current["body"] = "\n".join(current.pop("_body", [])).strip()
                entries.append(current)
            current = match.groupdict()
            current["_body"] = []
            tokens = dict(re.findall(r"(prompt|completion|total)=([0-9]+)", match.group("tokens") or ""))
            current["tokens"] = {k: int(v) for k, v in tokens.items()}
            continue
        if current is not None:
            current["_body"].append(line)
    if current is not None:
        current["body"] = "\n".join(current.pop("_body", [])).strip()
        entries.append(current)
    return entries


def _thought_local_time(ts: str) -> str:
    try:
        value = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return value.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S CST")
    except ValueError:
        return ts


def _seed_details(raw_cfg, hermes_home, seed_ids):
    if not seed_ids:
        return []
    memory = raw_cfg.get("memory") or {}
    cfg = memory.get("seed_memory") or {}
    db_path = Path(cfg.get("db_path") or Path(hermes_home or resolve_hermes_home()) / "seeds" / "seeds.db")
    if not db_path.is_file():
        return []
    import sqlite3
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path.as_posix(), uri=True)
        con.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in seed_ids)
        rows = []
        for row in con.execute("select id,summary,emotion_primary,emotion_intensity,importance,seeds_planted from seeds where id in (%s)" % placeholders, seed_ids):
            item = dict(row)
            try:
                item["seeds_planted"] = json.loads(item.get("seeds_planted") or "[]")
            except (TypeError, ValueError):
                pass
            rows.append(item)
        con.close()
        return rows
    except (OSError, sqlite3.Error):
        return []




def cmd_seed_show(seed_id: str, hermes_home=None):
    """Query a seed by ID and return details without its embedding blob."""
    import sqlite3

    home = resolve_hermes_home(hermes_home)
    env_path = os.environ.get("HERMES_SEED_DB_PATH", "")
    candidates = [
        Path(env_path) if env_path else None,
        Path(home) / "seed_data" / "seeds" / "seeds.db",
        Path(home) / "plugins" / "seed_memory" / "seed_src" / "hermes" / "data" / "seeds.db",
    ]
    db_path = next((p for p in candidates if p is not None and p.is_file()), None)
    if db_path is None:
        return {"ok": False, "not_found": True, "seed_id": seed_id}
    try:
        with sqlite3.connect("file:%s?mode=ro" % db_path.as_posix(), uri=True) as con:
            con.row_factory = sqlite3.Row
            columns = {row[1] for row in con.execute("PRAGMA table_info(seeds)")}
            selected = [
                "id", "timestamp", "summary", "emotion_primary",
                "emotion_intensity", "importance", "access_count",
                "caused_by", "led_to",
            ]
            if "recall_count" in columns:
                selected.append("recall_count")
            row = con.execute(
                "SELECT %s FROM seeds WHERE id=?" % ", ".join(selected),
                (seed_id,),
            ).fetchone()
    except (OSError, sqlite3.Error) as exc:
        return {"ok": False, "error": str(exc), "seed_id": seed_id}
    if row is None:
        return {"ok": False, "not_found": True, "seed_id": seed_id}

    result = {"ok": True}
    for key in selected:
        value = row[key]
        if key in ("caused_by", "led_to"):
            try:
                value = json.loads(value or "[]")
            except (TypeError, ValueError):
                pass
        result[key] = value
    return result

def cmd_log(hermes_home: Optional[str | Path] = None, count: int = 3, full: bool = False, as_json: bool = False, seeds: bool = False, tools: bool = False) -> Dict[str, Any]:
    raw_cfg = load_raw_config(hermes_home)
    path = resolve_paths(raw_cfg, hermes_home=hermes_home)["thought_log"]
    # Archives are newest-first by suffix (.1, .2 ...); combine chronologically
    # so `-n` remains meaningful across controlled rotation.
    paths = [Path(path).with_name(Path(path).name + ".%d" % i) for i in range(3, 0, -1)] + [Path(path)]
    entries = []
    for candidate in paths:
        entries.extend(parse_thought_entries(candidate))
    selected = entries[-max(0, int(count)):]
    for entry in selected:
        body = entry.get("body", "")
        elapsed = re.search(r"elapsed[=:]([0-9.]+)", body)
        prefetch = re.search(r"count[=:]([0-9]+)", body)
        reason = re.search(r"(?:结束原因|reason)[：:= ]+([^\n]+)", body)
        seed_ids = re.findall(r"seed_id[=: ]+([A-Za-z0-9_.:-]+)", body)
        conclusion = next((body[pos:pos + 400] for marker in ("结论", "下一步", "未完成") if (pos := body.find(marker)) >= 0), body[-400:])
        entry.update({"local_time": _thought_local_time(entry.get("ts", "")), "elapsed": float(elapsed.group(1)) if elapsed else None, "prefetch_count": int(prefetch.group(1)) if prefetch else None, "end_reason": reason.group(1).strip() if reason else None, "result_summary": conclusion.strip(), "seed_ids": list(dict.fromkeys(seed_ids))})
        if seeds:
            entry["seed_details"] = _seed_details(raw_cfg, hermes_home, entry["seed_ids"])
        if tools:
            entry["tools"] = [line for line in body.splitlines() if re.search(r"tool|seed_search|seed_add|写入|文件", line, re.I)]
        if not full:
            entry["body"] = body[:600]
    return {"path": str(path), "count": len(selected), "entries": selected}


def cmd_status(hermes_home: Optional[str | Path] = None) -> Dict[str, Any]:
    """Inspect running state, mode, today's budget usage, and circuit breaker state."""
    home = resolve_hermes_home(hermes_home)
    raw_cfg = load_raw_config(home)
    auto_cfg = get_autonomy_section(raw_cfg)
    paths = resolve_paths(raw_cfg, hermes_home=home)
    mode = determine_mode(raw_cfg, hermes_home=home)
    main_model = resolve_main_model(raw_cfg)
    auto_model = resolve_autonomy_model(raw_cfg)
    memory_provider = get_configured_memory_provider(home)

    stop_path = Path(paths["stop_file"])
    stopped = stop_path.exists()

    db = AutonomyDB(paths["db_path"])
    limits = auto_cfg.get("limits") if isinstance(auto_cfg.get("limits"), dict) else {}
    # Legacy nested config remains a read fallback, normalized by config.py.
    budget_cfg = dict(auto_cfg.get("budget") if isinstance(auto_cfg.get("budget"), dict) else {})
    budget_cfg.update({k: v for k, v in (auto_cfg.get("limits") or {}).items() if k in ("daily_tokens", "daily_calls", "daily_prompt_tokens")})
    circuit_cfg = auto_cfg.get("circuit") if isinstance(auto_cfg.get("circuit"), dict) else {}
    res_cfg = auto_cfg.get("resources") if isinstance(auto_cfg.get("resources"), dict) else {}
    daily_tokens = limits.get("daily_tokens", budget_cfg.get("daily_tokens"))
    threshold = limits.get("circuit_threshold", circuit_cfg.get("failure_threshold", 3))
    cooldown = limits.get("circuit_cooldown_s", circuit_cfg.get("cooldown_seconds", 1800))
    memory_max = limits.get("memory_max", res_cfg.get("memory_max_mb", 1024))

    budget = Budget(
        db,
        calls=os.environ.get("AUTONOMY_DAILY_CALLS") or budget_cfg.get("daily_calls"),
        tokens=os.environ.get("AUTONOMY_DAILY_TOKENS") or daily_tokens,
        prompt=os.environ.get("AUTONOMY_DAILY_PROMPT_TOKENS") or budget_cfg.get("daily_prompt_tokens"),
    )
    circuit = Circuit(
        db,
        threshold=int(
            os.environ.get("AUTONOMY_CIRCUIT_THRESHOLD")
            or threshold
            or 3
        ),
        cooldown_s=os.environ.get("AUTONOMY_CIRCUIT_COOLDOWN_S") or cooldown,
    )

    last_row = db.c.execute(
        "select status, started_at, finished_at, summary from last_tick where id=1"
    ).fetchone()
    last_tick = (
        {
            "status": last_row[0],
            "started_at": last_row[1],
            "finished_at": last_row[2],
            "summary": last_row[3],
        }
        if last_row
        else None
    )

    circuit_stat = circuit.status()
    circuit_stat["threshold"] = circuit.threshold
    circuit_stat["cooldown_seconds"] = circuit.cooldown
    circuit_stat["allow"] = circuit.allow()

    budget_stat = budget.status()
    budget_stat["allow"] = budget.allow()

    if stopped:
        run_state = "stopped"
    elif not circuit_stat["allow"] or not budget_stat["allow"]:
        run_state = "blocked"
    elif last_tick and last_tick.get("status") == "running":
        run_state = "running"
    else:
        run_state = "idle"

    return {
        "state": run_state,
        "stopped": stopped,
        "stop_file": str(stop_path),
        "mode": mode,
        "transport": (raw_cfg.get("autonomy") or {}).get("transport", "auto"),
        "main_model": main_model.get("model"),
        "autonomy_model": auto_model.get("model"),
        "inherit_model": bool(auto_model.get("inherit", True)),
        "memory_provider": memory_provider,
        "limits": {
            "daily_tokens": daily_tokens,
            "circuit_threshold": int(threshold),
            "circuit_cooldown_s": float(cooldown),
            "memory_max": memory_max,
        },
        "usage": budget_stat,
        "breaker": circuit_stat,
        "data_dir": str(Path(paths["db_path"]).parent),
        "quiet_s": auto_cfg.get("quiet_s", res_cfg.get("quiet_seconds", 300)),
        "last_tick": last_tick,
        "recent_runs": db.recent_runs(10),
        "thought_log": paths["thought_log"],
        "pending_resume": Path(paths["pending_resume"]).exists(),
    }


def setup_cli_parser(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["init", "stop", "resume", "status", "log", "reset-circuit", "tick", "seed-show", "policy"],
        help="Autonomy command: init | stop | resume | status | tick | seed-show | policy",
    )
    subparser.add_argument("seed_id", nargs="?", default=None,
                           help="Seed ID for seed-show; sub-command for policy: show | history | rollback")
    subparser.add_argument("policy_args", nargs="*", default=[],
                           help="policy rollback: <rule_id>. show: list active rules + inspection fields. "
                                "history [--limit N]: most recent policy events (default 20, max 200). "
                                "rollback <rule_id>: remove an active rule manually (no failure record).")
    subparser.add_argument("--limit", type=int, default=20, help="policy history: number of events (max 200)")
    subparser.add_argument("--hermes-home", default=None, help="Override HERMES_HOME path")
    subparser.add_argument("--json", action="store_true", help="Output JSON")
    subparser.add_argument("--dry-run", action="store_true", help="Dry run for tick")
    subparser.add_argument("--resume-if-idle", action="store_true", help="Resume if idle for tick")
    subparser.add_argument("-n", "--count", type=int, default=3, help="Number of thought entries")
    subparser.add_argument("--full", action="store_true", help="Show full thought text")
    subparser.add_argument("--seeds", action="store_true", help="Show seed details from read-only SQLite")
    subparser.add_argument("--tools", action="store_true", help="Show tool/file summaries")


def cmd_reset_circuit(hermes_home: Optional[str | Path] = None, as_json: bool = False) -> Dict[str, Any]:
    """Reset the breaker, retaining a backup of the SQLite state first."""
    home = resolve_hermes_home(hermes_home)
    raw_cfg = load_raw_config(home)
    db_path = Path(resolve_paths(raw_cfg, hermes_home=home)["db_path"])
    backup = None
    if db_path.exists():
        backup = db_path.with_name(db_path.name + ".bak")
        shutil.copy2(db_path, backup)
    db = AutonomyDB(db_path)
    try:
        db.reset_circuit()
    finally:
        db.c.close()
    return {"ok": True, "action": "reset-circuit", "database": str(db_path), "backup": str(backup) if backup else None}


def _policy_paths(hermes_home):
    home = resolve_hermes_home(hermes_home)
    raw_cfg = load_raw_config(home)
    paths = resolve_paths(raw_cfg, hermes_home=home)
    db_path = os.environ.get("AUTONOMY_DB_PATH") or paths["db_path"]
    return home, Path(db_path).parent, db_path


def _policy_text(s: Any, limit: int = 200) -> str:
    from .policy_reflect import redact

    return " ".join(redact(str(s or "")).split())[:limit]


def cmd_policy(sub: str, rest: List[str], hermes_home=None, as_json=False, limit=20) -> int:
    """`autonomy policy show|history|rollback`. Never prints .env/secret content; rule text <= 200 chars."""
    from . import policy as P
    from . import policy_eval as PE
    from . import policy_report as PR

    sub = (sub or "").lower()
    home, data_dir, db_path = _policy_paths(hermes_home)
    if sub == "show":
        rows = PE.rule_rows(home, data_dir)
        summary = PR.policy_summary(home, data_dir)
        for r in rows:
            r["text"] = _policy_text(r.get("text"))
        if as_json:
            print(json.dumps({"rules": rows, "summary": summary}, ensure_ascii=False, indent=2, default=str))
            return 0 if summary.get("ok") else 1
        print("规则 %d/%d" % (len(rows), P.MAX_RULES))
        print("%-16s %-10s %-7s %-14s %-6s %s" % ("id", "status", "channel", "ttl", "机会数", "否决指标"))
        for r in rows:
            vm = json.dumps(r.get("veto_metrics") or {}, ensure_ascii=False, default=str)[:120]
            print("%-16s %-10s %-7s %-14s %-6s %s %s" % (r["id"], r["status"], r["channel"], r["ttl"],
                                                       r["opportunities"], r["veto_reason"], vm))
            print("    text: %s" % r["text"])
        print("--- 巡检 ---")
        if not summary.get("ok"):
            print("巡检失败: %s" % summary.get("error"))
            return 1
        tty = sys.stdout.isatty()
        ratio = summary["opportunity_shortage_ratio"]
        shortage = "机会不足占比: %.1f%%" % (ratio * 100)
        if summary["opportunity_shortage_alert"]:
            shortage = ("[31m%s  [红色告警: >60%%][0m" % shortage) if tty else shortage + "  [红色告警: >60%]"
        for line in (
            "候选数: %d (active %d)" % (summary["candidates"], summary["active_rules"]),
            "promoted_eligible 数: %d" % summary["promoted_eligible"],
            "到期分类: expired_no_opportunity=%d expired_neutral=%d" % (
                summary["expired_no_opportunity"], summary["expired_neutral"]),
            "否决数 vetoed: %d" % summary["vetoed"],
            "rejected_format 数: %d" % summary["rejected_format"],
            "今日反思触发次数: %d" % summary["reflect_today"],
            "映射覆盖率: %s" % summary.get("mapping_coverage"),
            shortage,
        ):
            print(line)
        return 0
    if sub == "history":
        n = max(1, min(int(limit or 20), 200))
        conn = P._open_ro(db_path)
        if conn is None:
            print("policy history: 无法打开 %s" % db_path)
            return 1
        try:
            rows = conn.execute("select ts,rule_id,kind,reason from policy_events order by id desc limit ?",
                                (n,)).fetchall()
        except Exception as e:
            print("policy history: 查询失败 %s" % type(e).__name__)
            return 1
        finally:
            conn.close()
        if as_json:
            print(json.dumps([{"ts": a, "rule_id": b, "kind": c, "reason": _policy_text(d)} for a, b, c, d in rows],
                             ensure_ascii=False, indent=2))
            return 0
        for ts, rid, kind, reason in rows:
            print("%s  %-16s %-22s %s" % (ts, rid, kind, _policy_text(reason)))
        if not rows:
            print("(no policy events)")
        return 0
    if sub == "rollback":
        if len(rest) != 1:
            print("usage: autonomy policy rollback <rule_id>")
            return 2
        rule_id = rest[0]
        res = P.remove_rule(data_dir, rule_id, reason="manual rollback (cli)", source="manual_rollback",
                            db=AutonomyDB(db_path), profile=home.name)
        if not res.get("ok"):
            print("rollback failed: %s" % (res.get("error") or "unknown"))
            return 1
        print("rolled back %s" % rule_id)
        return 0
    print("usage: autonomy policy show | history [--limit N] | rollback <rule_id>")
    return 2


def handle_cli(args: argparse.Namespace) -> int:
    _utf8_stdio()
    action = getattr(args, "action", "status") or "status"
    hermes_home = getattr(args, "hermes_home", None)
    as_json = bool(getattr(args, "json", False))

    if action == "policy":
        return cmd_policy(getattr(args, "seed_id", None) or "", list(getattr(args, "policy_args", None) or []),
                          hermes_home=hermes_home, as_json=as_json, limit=getattr(args, "limit", 20))

    if action == "init":
        res = run_init_wizard(hermes_home=hermes_home)
        if as_json:
            print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if action == "stop":
        res = cmd_stop(hermes_home=hermes_home)
        if as_json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print(f"[autonomy] 已停止自主思考 (创建 {res['stop_file']})")
        return 0

    if action == "resume":
        res = cmd_resume(hermes_home=hermes_home)
        if as_json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print(f"[autonomy] 已恢复自主思考 (删除 {res['stop_file']})")
        return 0

    if action == "reset-circuit":
        res = cmd_reset_circuit(hermes_home=hermes_home)
        if as_json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print(f"已重置熔断器: {res['database']}")
            if res.get("backup"):
                print(f"数据库备份: {res['backup']}")
        return 0

    if action == "log":
        res = cmd_log(hermes_home=hermes_home, count=getattr(args, "count", 3), full=getattr(args, "full", False), as_json=as_json, seeds=getattr(args, "seeds", False), tools=getattr(args, "tools", False))
        if as_json:
            print(json.dumps(res, ensure_ascii=False))
        elif not res["entries"]:
            print(f"暂无思考日志内容: {res['path']}")
        else:
            for entry in res["entries"]:
                tokens = entry.get("tokens", {})
                print("--- %s status=%s mode=%s model=%s elapsed=%s token=%s prefetch=%s reason=%s ---" % (entry.get("local_time"), entry.get("status"), entry.get("mode") or "-", entry.get("model") or "-", entry.get("elapsed") if entry.get("elapsed") is not None else "-", tokens.get("total", 0), entry.get("prefetch_count") if entry.get("prefetch_count") is not None else "-", entry.get("end_reason") or "-"))
                print("结果摘要: " + entry.get("result_summary", ""))
                if getattr(args, "seeds", False):
                    for seed in entry.get("seed_details", []):
                        print("种子 %s: 摘要=%s 情绪=%s/%s 重要度=%s 洞见=%s" % (seed.get("id"), seed.get("summary", ""), seed.get("emotion_primary", ""), seed.get("emotion_intensity", ""), seed.get("importance", ""), "; ".join(seed.get("seeds_planted", []) or [])))
                if getattr(args, "tools", False):
                    for line in entry.get("tools", []):
                        print("工具/文件: " + line)
                print(entry.get("body", ""))
        return 0

    if action == "status":
        res = cmd_status(hermes_home=hermes_home)
        if as_json:
            print(json.dumps(res, ensure_ascii=False))
        else:
            print(f"运行状态: {res['state']} (STOP_AUTONOMY={'存在' if res['stopped'] else '不存在'})")
            print(
                f"运行模式: {res['mode']} (主模型={res['main_model']}, 思考模型={res['autonomy_model']}, 继承={res['inherit_model']})\n传输方式: {res.get('transport', 'auto')}"
            )
            print(f"记忆插件: {res['memory_provider']}")
            b = res.get("usage") or res.get("budget") or {}
            print(
                f"今日预算: calls={b['calls']}/{b['calls_limit']}, tokens={b['tokens']}/{b['tokens_limit']}, prompt={b['prompt']}/{b['prompt_limit']} (允许运行={b['allow']})"
            )
            print(f"预算来源: {b.get('source') or b.get('daily_tokens_source') or 'config'}")
            c = res.get("breaker") or res.get("circuit") or {}
            print(
                f"熔断状态: state={c['state']}, failures={c['failures']}/{c['threshold']}, blocked={c['blocked']} (允许运行={c['allow']})"
            )
            print(f"只读思考日志: {res['thought_log']}")
            print("查看思考内容：hermes autonomy log -n 3")
            print("最近10轮:")
            for item in res.get("recent_runs", []):
                print(f"  {item.get('ts')} {item.get('exit_reason') or item.get('kind')} {item.get('summary', '')[:240]}")
        return 0

    if action == "tick":
        tick_args: List[str] = []
        if getattr(args, "dry_run", False):
            tick_args.append("--dry-run")
        if getattr(args, "resume_if_idle", False):
            tick_args.append("--resume-if-idle")
        return tick_main(tick_args, hermes_home=hermes_home)

    if action == "seed-show":
        seed_id_arg = getattr(args, "seed_id", None)
        if not seed_id_arg:
            print("usage: hermes autonomy seed-show <seed_id>", file=sys.stderr)
            return 1
        res = cmd_seed_show(seed_id_arg, hermes_home=hermes_home)
        if as_json:
            print(json.dumps(res, ensure_ascii=False))
        elif not res.get("ok"):
            if res.get("not_found"):
                print("seed not found: %s" % seed_id_arg)
            else:
                print("error: %s" % res.get("error", "unknown"), file=sys.stderr)
            return 1
        else:
            fields = [
                "id", "timestamp", "summary", "emotion_primary",
                "emotion_intensity", "importance", "access_count",
                "recall_count", "caused_by", "led_to",
            ]
            for field in fields:
                if field in res:
                    value = res[field]
                    if isinstance(value, (list, dict)):
                        value = json.dumps(value, ensure_ascii=False)
                    print("%s: %s" % (field, value))
        return 0

    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes autonomy")
    setup_cli_parser(parser)
    args = parser.parse_args(argv)
    return handle_cli(args)


if __name__ == "__main__":
    sys.exit(main())
