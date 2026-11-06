"""Read-only half-hour health probe for the two production Hermes profiles.

The default collector makes one SSH connection to the approved host and only reads
systemd state, logs, queue metadata, and SQLite databases in mode=ro.  It never
restarts services or changes remote files.  ``--snapshot`` evaluates an offline
JSON fixture and is used for threshold tests and dry runs.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HOST = "192.168.10.222"
PROFILES = ("xiaozhi", "xiaoai")
DEFAULT_KEY = Path("F:/shuzishengming/id_rsa")
DEFAULT_STATE = Path("F:/shuzishengming/.monitor/state.json")

# Kept self-contained so a scheduled run needs only one SSH process.  Output is
# deliberately metadata-only: no message, seed, prompt, or environment content.
REMOTE_PROBE = r'''
import datetime,json,os,re,sqlite3,subprocess,time
from pathlib import Path
UTC=datetime.timezone.utc
now=time.time()
def cmd(*args):
 try:return subprocess.run(args,text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=15).stdout.strip()
 except Exception:return ""
def recent_log_counts(profile):
 text=cmd("journalctl","--user","-u",profile+"-autonomy.service","--since","35 minutes ago","--no-pager","-o","cat")
 low=text.lower()
 return {"traceback":low.count("traceback"),"failed":len(re.findall(r"\bfailed\b",low)),"timeout":len(re.findall(r"timeout|timed out",low)),"exit143":len(re.findall(r"(?:status=143|code=143|signal=term)",low))}
def worker_counts(path):
 try:
  with path.open("rb") as f:
   size=f.seek(0,2);f.seek(max(0,size-2*1024*1024));lines=f.read().decode("utf-8","replace").splitlines()
 except OSError:return {"nameerror":0,"invalid_json":0}
 cutoff=datetime.datetime.now(UTC)-datetime.timedelta(minutes=35); current=None; n=i=0
 for line in lines:
  m=re.match(r"(\d{4}-\d\d-\d\d[T ][0-9:.+-]+)",line)
  if m:
   try:
    current=datetime.datetime.fromisoformat(m.group(1).replace("Z","+00:00"));current=current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
   except ValueError:current=None
  if current is None or current<cutoff:continue
  low=line.lower();n += int("nameerror" in low and "data" in low);i += int("invalid_json" in low)
 return {"nameerror":n,"invalid_json":i}
def ticks(path):
 try:
  with path.open("rb") as f:
   size=f.seek(0,2);f.seek(max(0,size-4*1024*1024));text=f.read().decode("utf-8","replace")
 except OSError:return []
 pattern=re.compile(r"^=== \[(?P<ts>[^]]+)\] run_id=(?P<run>\S+) status=(?P<status>\S+)[^\n]*===\n(?P<body>.*?)(?=^=== \[|\Z)",re.M|re.S);out=[]
 for m in pattern.finditer(text):
  body=m.group("body");elapsed=re.search(r"elapsed[=:]([0-9.]+)",body);tools=re.search(r"tools[=:]([0-9]+)",body);prefetch=re.search(r"(?:prefetch|count)[=:]([0-9]+)",body)
  try:ts=datetime.datetime.fromisoformat(m.group("ts").replace("Z","+00:00"));ts=ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC);epoch=ts.timestamp()
  except ValueError:epoch=0
  out.append({"ts":epoch,"run_id":m.group("run"),"status":m.group("status"),"elapsed":float(elapsed.group(1)) if elapsed else None,"tools":int(tools.group(1)) if tools else None,"prefetch":int(prefetch.group(1)) if prefetch else None})
 return out[-6:]
def queue_meta(base):
 pending=[]
 try:pending=[p for p in base.glob("*.json") if p.is_file()]
 except OSError:pass
 failed=[]
 try:
  for p in sorted((base/"_failed").glob("*.json")):
   err=""
   try:err=str(json.loads(p.read_text(encoding="utf-8")).get("last_error", ""))[:80]
   except Exception:err="unreadable_json"
   failed.append({"name":p.name,"error":re.sub(r"[\r\n\t]+"," ",err)})
 except OSError:pass
 oldest=max((now-p.stat().st_mtime for p in pending),default=0)
 return {"pending":len(pending),"oldest_age_s":max(0,oldest),"failed_count":len(failed),"failed":failed}
def policy_meta(home):
 db=home/"autonomy_data"/"autonomy.db";out={"supported":False,"db_error":False,"events_60m":{}}
 if not db.is_file():return out
 try:
  con=sqlite3.connect("file:"+str(db)+"?mode=ro",uri=True);tables={r[0] for r in con.execute("select name from sqlite_master where type='table'")}
  if "policy_events" in tables:
   out["supported"]=True
   # Policy timestamps are local ISO strings; a 2-hour bounded count is enough for trend/health.
   cols={r[1] for r in con.execute("pragma table_info(policy_events)")};name_col="event" if "event" in cols else "kind"
   rows=con.execute("select "+name_col+",count(*) from policy_events where ts>=datetime('now','localtime','-2 hours') group by "+name_col).fetchall();out["events_60m"]={str(k):int(v) for k,v in rows}
  con.close()
 except Exception:out["db_error"]=True
 active=home/"autonomy_data"/"policy"/"active.json"
 if active.exists():
  try:json.loads(active.read_text(encoding="utf-8"));out["active_json_valid"]=True
  except Exception:out["active_json_valid"]=False
 return out
result={"collected_at":now,"profiles":{}}
for profile in ("xiaozhi","xiaoai"):
 home=Path("/home/hermes/.hermes/profiles")/profile;service=cmd("systemctl","--user","is-active",profile+"-autonomy.service");timer=cmd("systemctl","--user","is-active",profile+"-autonomy.timer")
 active_age=0
 if service=="active":
  entered=cmd("systemctl","--user","show",profile+"-autonomy.service","-p","ActiveEnterTimestampMonotonic","--value")
  try:active_age=max(0,float(Path("/proc/uptime").read_text().split()[0])-float(entered)/1000000)
  except Exception:active_age=None
 ts=ticks(home/"autonomy_data"/"thoughts.log");logs=recent_log_counts(profile);logs.update(worker_counts(home/"seed_data"/"seed_queue"/"worker.log"))
 result["profiles"][profile]={"timer":timer,"service":service,"service_active_age_s":active_age,"ticks":ts,"last_tick_age_s":max(0,now-ts[-1]["ts"]) if ts and ts[-1]["ts"] else None,"logs":logs,"queue":queue_meta(home/"seed_data"/"seed_queue"),"policy":policy_meta(home)}
print(json.dumps(result,ensure_ascii=False,separators=(",",":")))
'''


def collect(key: Path = DEFAULT_KEY) -> dict[str, Any]:
    if not key.is_file():
        raise RuntimeError(f"SSH key is missing: {key}")
    encoded = __import__("base64").b64encode(REMOTE_PROBE.encode()).decode()
    remote = f"python3 -I -c \"import base64;exec(base64.b64decode('{encoded}'))\""
    proc = subprocess.run(
        ["ssh", "-i", str(key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", f"hermes@{HOST}", remote],
        text=True, capture_output=True, timeout=90, check=False,
    )
    if proc.returncode:
        raise RuntimeError(f"probe SSH failed with exit code {proc.returncode}")
    return json.loads(proc.stdout)


def _issue(level: str, profile: str, code: str, detail: str) -> dict[str, str]:
    return {"level": level, "profile": profile, "code": code, "detail": detail}


def evaluate(snapshot: dict[str, Any], previous: dict[str, Any] | None = None) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    previous = previous or {}
    old_profiles = previous.get("profiles", {})
    for profile in PROFILES:
        data = snapshot.get("profiles", {}).get(profile)
        if not data:
            issues.append(_issue("CRIT", profile, "PROFILE_MISSING", "profile probe result is absent")); continue
        if data.get("timer") != "active": issues.append(_issue("CRIT", profile, "TIMER_INACTIVE", str(data.get("timer"))))
        if data.get("service") == "failed": issues.append(_issue("CRIT", profile, "SERVICE_FAILED", "systemd service is failed"))
        active_age = data.get("service_active_age_s")
        if data.get("service") == "active" and (active_age is None or active_age > 600):
            issues.append(_issue("CRIT", profile, "SERVICE_STUCK", f"active_age_s={active_age}"))
        age = data.get("last_tick_age_s")
        if age is None or age > 2700: issues.append(_issue("CRIT", profile, "TICK_STALE", f"age_s={age}"))
        elif age > 1800: issues.append(_issue("WARN", profile, "TICK_LATE", f"age_s={age:.0f}"))
        logs = data.get("logs", {})
        if logs.get("traceback", 0) or logs.get("nameerror", 0): issues.append(_issue("CRIT", profile, "NEW_EXCEPTION", f"traceback={logs.get('traceback',0)} nameerror={logs.get('nameerror',0)}"))
        if logs.get("failed", 0) or logs.get("timeout", 0) or logs.get("exit143", 0): issues.append(_issue("WARN", profile, "SERVICE_ERRORS", f"failed={logs.get('failed',0)} timeout={logs.get('timeout',0)} exit143={logs.get('exit143',0)}"))
        invalid = int(logs.get("invalid_json", 0));
        if invalid: issues.append(_issue("CRIT" if invalid >= 3 else "WARN", profile, "INVALID_JSON", f"count_35m={invalid}"))
        queue = data.get("queue", {}); pending=int(queue.get("pending",0)); oldest=float(queue.get("oldest_age_s",0) or 0)
        if pending > 20 or oldest > 7200: issues.append(_issue("CRIT", profile, "QUEUE_BACKLOG", f"pending={pending} oldest_s={oldest:.0f}"))
        elif pending > 5 or oldest > 3600: issues.append(_issue("WARN", profile, "QUEUE_BACKLOG", f"pending={pending} oldest_s={oldest:.0f}"))
        old_failed=int(old_profiles.get(profile,{}).get("queue",{}).get("failed_count",queue.get("failed_count",0)))
        if int(queue.get("failed_count",0)) > old_failed: issues.append(_issue("CRIT", profile, "FAILED_QUEUE_GREW", f"{old_failed}->{queue.get('failed_count')}"))
        ticks=data.get("ticks",[]); bad=[t for t in ticks if t.get("status") in {"failed","blocked","error"}]
        if len(bad)>=2: issues.append(_issue("CRIT", profile, "TICK_FAILURES", f"bad_recent={len(bad)}"))
        elif bad: issues.append(_issue("WARN", profile, "TICK_FAILURE", f"status={bad[-1].get('status')}"))
        for t in ticks:
            if (t.get("elapsed") or 0)>480: issues.append(_issue("WARN",profile,"TICK_SLOW",f"run={t.get('run_id')} elapsed={t.get('elapsed')}"));break
        short_zero=[t for t in ticks if t.get("tools")==0 and (t.get("elapsed") or 10**9)<60]
        consecutive=0
        for t in reversed(ticks):
            if t.get("tools")==0 and (t.get("elapsed") or 10**9)<60: consecutive+=1
            else: break
        if consecutive>=4: issues.append(_issue("CRIT",profile,"ZERO_TOOL_LOOP",f"consecutive={consecutive}"))
        elif consecutive>=2 or (ticks and len(short_zero)/len(ticks)>=.5): issues.append(_issue("WARN",profile,"ZERO_TOOL_LOOP",f"consecutive={consecutive} recent={len(short_zero)}/{len(ticks)}"))
        if sum(1 for t in ticks[-2:] if t.get("prefetch")==0)>=2: issues.append(_issue("WARN",profile,"PREFETCH_ZERO","last two ticks have zero prefetch"))
        if any((t.get("tools") or 0)>100 for t in ticks): issues.append(_issue("WARN",profile,"TOOL_RUNAWAY","a recent tick used over 100 tools"))
        policy=data.get("policy",{})
        if policy.get("db_error") or policy.get("active_json_valid") is False: issues.append(_issue("CRIT",profile,"POLICY_STATE_INVALID",f"db_error={policy.get('db_error')} active_json_valid={policy.get('active_json_valid')}"))
    return issues


def render(snapshot: dict[str, Any], issues: list[dict[str, str]]) -> dict[str, Any]:
    severity = "CRIT" if any(x["level"]=="CRIT" for x in issues) else "WARN" if issues else "OK"
    return {"severity": severity, "collected_at": snapshot.get("collected_at"), "issues": issues,
            "incident_template": None if not issues else {"status":"OPEN","evidence":issues,"allowed_action":"diagnose read-only; coordinate before any online change","verification":"two consecutive clean monitor runs"}}


def main(argv: list[str] | None = None) -> int:
    parser=argparse.ArgumentParser();parser.add_argument("--snapshot",type=Path);parser.add_argument("--state",type=Path,default=DEFAULT_STATE);parser.add_argument("--key",type=Path,default=DEFAULT_KEY);parser.add_argument("--no-state",action="store_true")
    args=parser.parse_args(argv)
    snapshot=json.loads(args.snapshot.read_text(encoding="utf-8")) if args.snapshot else collect(args.key)
    previous=None
    if not args.no_state and args.state.is_file():
        try:previous=json.loads(args.state.read_text(encoding="utf-8"))
        except (OSError,ValueError):previous=None
    issues=evaluate(snapshot,previous);print(json.dumps(render(snapshot,issues),ensure_ascii=False,indent=2))
    if not args.no_state:
        args.state.parent.mkdir(parents=True,exist_ok=True);tmp=args.state.with_suffix(".tmp");tmp.write_text(json.dumps(snapshot,ensure_ascii=False),encoding="utf-8");os.replace(tmp,args.state)
    return 2 if any(x["level"]=="CRIT" for x in issues) else 1 if issues else 0

if __name__ == "__main__": raise SystemExit(main())
