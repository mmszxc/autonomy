"""Local/remote autonomy soak harness. Local mode uses an in-process OpenAI-compatible fake gateway."""
from __future__ import annotations
import argparse, http.server, json, os, socketserver, subprocess, sys, tempfile, threading, time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "hermes_home" / "plugins"
if str(PLUGIN) not in sys.path: sys.path.insert(0, str(PLUGIN))
from autonomy.db import AutonomyDB
from autonomy.budget import Budget
from autonomy.circuit import Circuit
from autonomy.runner import Runner
from autonomy.gateway_client import GatewayClient
from autonomy.cli import cmd_stop, cmd_resume, cmd_status
from autonomy.config import determine_mode

@dataclass
class FakeState:
    delay: float = 0.0
    fail: bool = False
    text: str = '{"thoughts":"local soak","did":"none","result":"ok","next":"tick"}'

class _Handler(http.server.BaseHTTPRequestHandler):
    state = FakeState()
    def do_POST(self):
        if self.path != "/v1/chat/completions": self.send_error(404); return
        if self.state.delay: time.sleep(self.state.delay)
        if self.state.fail: self.send_error(503, "injected failure"); return
        body = json.dumps({"choices":[{"message":{"content":self.state.text}}],"usage":{"prompt_tokens":2,"completion_tokens":3,"total_tokens":5}}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_PATCH(self):
        if self.path.startswith("/api/sessions/"):
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b"{}")
        else: self.send_error(404)
    def log_message(self, *_): pass

class LocalGateway:
    """Expose only chat/end so Runner uses the OpenAI-compatible endpoint."""
    def __init__(self, client): self.client = client
    def chat(self, *args, **kwargs): return self.client.chat(*args, **kwargs)
    def end(self, *args, **kwargs): return self.client.end(*args, **kwargs)

class FakeGateway:
    def __init__(self):
        self.http = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler); self.http.daemon_threads = True
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True); self.thread.start()
        self.state = _Handler.state; self.url = "http://127.0.0.1:%d" % self.http.server_address[1]
    def close(self): self.http.shutdown(); self.http.server_close()

@dataclass
class Context:
    root: Path
    db: AutonomyDB
    gateway: FakeGateway
    runner: Runner
    paths: dict


def _ctx():
    root = Path(tempfile.mkdtemp(prefix="autonomy-soak-")); data = root / "autonomy_data"; data.mkdir()
    db = AutonomyDB(data / "autonomy.db"); db.create_goal("soak", "local test goal", created_by="user")
    gw = FakeGateway(); paths = {"db": data/"autonomy.db", "stop": data/"STOP_AUTONOMY", "activity": data/"LAST_USER_ACTIVITY", "lock": data/"run.lock", "thought": data/"thoughts.log"}
    runner = Runner(db, Budget(db, calls=1000, tokens=100000, prompt=100000), Circuit(db, threshold=5, cooldown_s=0.2), stop_file=str(paths["stop"]), gateway=LocalGateway(GatewayClient(base_url=gw.url, timeout=2)), lock_path=str(paths["lock"]), activity_file=str(paths["activity"]), quiet_s=0.1, poll_s=0.01, mode="parallel", thought_log_path=str(paths["thought"]))
    return Context(root, db, gw, runner, paths)

def s1(ctx):
    out=[ctx.runner.tick() for _ in range(3)]; return all(x.get("status")=="ok" for x in out), "statuses="+str([x.get("status") for x in out])
def s2(ctx):
    ctx.db.append_event(kind="note", summary="long-context " + "x"*8000); out=ctx.runner.tick(); return out.get("status")=="ok", "status="+out.get("status","")
def s3(ctx):
    before=ctx.paths["thought"].stat().st_size if ctx.paths["thought"].exists() else 0; out=ctx.runner.tick(); after=ctx.paths["thought"].stat().st_size if ctx.paths["thought"].exists() else 0; return out.get("status")=="ok" and after>before, "thought_log_bytes=%d->%d"%(before,after)
def s4(ctx):
    ctx.gateway.state.delay=.2; results=[]
    # Model two independent processes: distinct SQLite connections, shared lock.
    runners=[]
    for _ in range(2):
        db=AutonomyDB(ctx.paths["db"])
        runners.append(Runner(db, Budget(db,calls=1000,tokens=100000,prompt=100000), Circuit(db,threshold=5,cooldown_s=1), stop_file=str(ctx.paths["stop"]), gateway=LocalGateway(GatewayClient(base_url=ctx.gateway.url,timeout=2)), lock_path=str(ctx.paths["lock"]), activity_file=str(ctx.paths["activity"]), quiet_s=.1, mode="parallel", thought_log_path=str(ctx.paths["thought"])))
    ts=[threading.Thread(target=lambda r=r: results.append(r.tick().get("status"))) for r in runners]
    [t.start() for t in ts]; [t.join() for t in ts]; ctx.gateway.state.delay=0
    return "busy" in results and "ok" in results, "statuses="+str(results)
def s5(ctx):
    cfg={"model":{"default":"main"},"autonomy":{"model":"other"}}; p=determine_mode(cfg); cfg["autonomy"]["model"]="main"; d=determine_mode(cfg)
    ctx.paths["activity"].write_text(str(time.time()),encoding="utf-8")
    class AsyncGateway:
        def start_run(self,*_): return {"run_id":"fake-run"}
        def get_run(self,*_): return {"status":"completed","output":"ok","usage":{"total_tokens":1}}
        def stop_run(self,*_): return {"status":"stopped"}
    def make(mode,suffix):
        db=AutonomyDB(ctx.paths["db"]); return Runner(db,Budget(db,calls=1000,tokens=100000,prompt=100000),Circuit(db,threshold=5,cooldown_s=1),stop_file=str(ctx.paths["stop"]),gateway=AsyncGateway(),lock_path=str(ctx.root/(suffix+".lock")),activity_file=str(ctx.paths["activity"]),quiet_s=60,mode=mode,thought_log_path=str(ctx.paths["thought"]))
    dialog_status=make("dialog_first","dialog").tick().get("status"); parallel_status=make("parallel","parallel").tick().get("status"); ctx.paths["activity"].unlink(missing_ok=True)
    ok=p=="parallel" and d=="dialog_first" and dialog_status in ("yielded_user_recent","yielded_user_active","preempted") and parallel_status=="ok"
    return ok, "different=%s same=%s activity_dialog=%s activity_parallel=%s"%(p,d,dialog_status,parallel_status)
def s6(ctx):
    h=str(ctx.root); a=cmd_stop(h); st=cmd_status(h); b=cmd_resume(h); return a["stopped"] and st["stopped"] and not b["stopped"], "stop=%s resume=%s"%(st["state"],b["stopped"])
def s7(ctx):
    db=AutonomyDB(ctx.root/"circuit.db"); db.create_goal("circuit","failure injection",created_by="user")
    r=Runner(db,Budget(db,calls=100,tokens=10000,prompt=10000),Circuit(db,threshold=3,cooldown_s=.1),stop_file=str(ctx.root/"NO_STOP"),gateway=LocalGateway(GatewayClient(base_url=ctx.gateway.url,timeout=2)),lock_path=str(ctx.root/"circuit.lock"),mode="parallel")
    ctx.gateway.state.fail=True; rs=[r.tick().get("status") for _ in range(3)]; blocked=r.tick().get("status")=="blocked"; time.sleep(.15); ctx.gateway.state.fail=False; half_open=r.tick().get("status")
    bdb=AutonomyDB(ctx.root/"budget.db"); bdb.create_goal("budget","budget injection",created_by="user"); br=Runner(bdb,Budget(bdb,calls=1,tokens=10000,prompt=10000),Circuit(bdb,threshold=5,cooldown_s=1),stop_file=str(ctx.root/"NO_STOP2"),gateway=LocalGateway(GatewayClient(base_url=ctx.gateway.url,timeout=2)),lock_path=str(ctx.root/"budget.lock"),mode="parallel"); first=br.tick().get("status"); budget_blocked=br.tick().get("status")=="blocked"
    ok=blocked and half_open=="ok" and first=="ok" and budget_blocked
    return ok, "failures=%s blocked=%s half_open=%s budget_first=%s budget_blocked=%s"%(rs,blocked,half_open,first,budget_blocked)
def s8(ctx):
    # Isolate injected unreachable gateway from previous circuit/STOP scenarios.
    isolated=AutonomyDB(ctx.root/"unreachable.db"); isolated.create_goal("bad", "unreachable", created_by="user")
    bad=Runner(isolated, Budget(isolated,calls=100,tokens=10000,prompt=10000), Circuit(isolated,threshold=5,cooldown_s=1), stop_file=str(ctx.root/"NO_STOP"), gateway=LocalGateway(GatewayClient(base_url="http://127.0.0.1:1",timeout=.1)), lock_path=str(ctx.root/"bad.lock"), mode="parallel"); out=bad.tick(); return out.get("status")=="failed", "status="+out.get("status","")
def s9(ctx):
    cmd_stop(str(ctx.root)); db2=AutonomyDB(ctx.paths["db"]); st=cmd_status(str(ctx.root)); return st["stopped"] and db2.path.exists(), "stopped=%s db=%s"%(st["stopped"],db2.path)
def s10(ctx):
    return False, "SKIPPED: memory provider API is not available in local plugin fixture"

SCENARIOS={"S1":s1,"S2":s2,"S3":s3,"S4":s4,"S5":s5,"S6":s6,"S7":s7,"S8":s8,"S9":s9,"S10":s10}

def run_all(mode="local", interval=0.0):
    if mode == "remote": raise NotImplementedError("remote execution is intentionally not run in this phase")
    ctx=_ctx(); rows=[]
    try:
        for name, fn in SCENARIOS.items():
            started=time.monotonic()
            try: ok, evidence=fn(ctx); result="PASS" if ok else ("SKIPPED" if evidence.startswith("SKIPPED:") else "FAIL")
            except Exception as exc: result="FAIL"; evidence="exception=%s: %s"%(type(exc).__name__,exc)
            rows.append((name,result,evidence,time.monotonic()-started)); print("%s %s evidence=%s"%(name,result,evidence), flush=True)
            if interval: time.sleep(interval)
    finally: ctx.gateway.close()
    report=ROOT/"docs"/"soak_report.md"; report.parent.mkdir(exist_ok=True)
    with report.open("a",encoding="utf-8") as f:
        f.write("\n## %s\n"%time.strftime("%Y-%m-%d %H:%M:%S")); f.write("| 时间 | 场景 | 结果 | 证据 | 耗时(s) |\n|---|---|---|---|---|\n")
        for n,r,e,d in rows: f.write("| %s | %s | %s | %s | %.3f |\n"%(time.strftime("%H:%M:%S"),n,r,e.replace("|","/"),d))
    return rows

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument("--mode",choices=("local","remote"),default="local"); p.add_argument("--interval",type=float,default=0); a=p.parse_args(argv)
    try: rows=run_all(a.mode,a.interval)
    except NotImplementedError as e: print("SKIPPED",e); return 0
    return 0 if all(r[1] in ("PASS","SKIPPED") for r in rows) else 1
if __name__ == "__main__": raise SystemExit(main())
