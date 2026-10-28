import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
PLUGIN_PARENT = ROOT / "plugins"


def run_isolated(code):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PLUGIN_PARENT) + os.pathsep + env.get("PYTHONPATH", "")
    prefix = f"import sys; sys.path.insert(0, {str(PLUGIN_PARENT)!r})\n"
    result = subprocess.run([sys.executable, "-c", prefix + code], cwd=ROOT, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_memory_segment_is_first_and_before_goals_events():
    run_isolated(r'''
from autonomy.prompt import build_tick_prompt
p=build_tick_prompt([{"title":"GOAL_X"}], [{"summary":"EVENT_X"}], memory_review="MEMORY_X")
assert p.startswith("【醒来先回顾的记忆】\nMEMORY_X")
assert p.index("MEMORY_X") < p.index("GOAL_X") < p.index("EVENT_X")
assert "第一步，先回顾你的记忆" in p
assert "必须输出 JSON" not in p
''')


def test_empty_and_failed_memory_degrade_without_failing_tick():
    run_isolated(r'''
import tempfile
from pathlib import Path
from autonomy.db import AutonomyDB
from autonomy.budget import Budget
from autonomy.circuit import Circuit
from autonomy.runner import Runner

class Gateway:
 def __init__(self): self.prompts=[]
 def chat(self,*args,**kwargs):
  self.prompts.append(args[-1]); return "sid", "已先回顾，然后继续思考。", {"total_tokens":1}
 def end(self,*args,**kwargs): return None
class Empty:
 def initialize(self,**kwargs): pass
 def prefetch(self,q): return ""
 def shutdown(self): pass
class Broken(Empty):
 def prefetch(self,q): raise RuntimeError("memory offline")
for provider, reason in ((Empty(),"empty"),(Broken(),"degraded")):
 root=Path(tempfile.mkdtemp()); db=AutonomyDB(root/"a.db"); db.create_goal("g","goal")
 gw=Gateway(); r=Runner(db,Budget(db,calls=20,tokens=100,prompt=100),Circuit(db),stop_file=str(root/"STOP"),gateway=gw,lock_path=str(root/"lock"),thought_log_path=str(root/"thoughts.log"),memory_provider=provider,hermes_home=str(root),mode="parallel")
 out=r.tick(); assert out["status"]=="ok",out
 assert gw.prompts[0].startswith("【醒来先回顾的记忆】\n暂无可回顾的记忆。")
 events=db.list_events(20); assert any(e.get("kind")=="memory_prefetch" and e.get("exit_reason")==reason for e in events),events
''')


def test_prefetched_seed_ids_are_logged_and_prompted_first():
    run_isolated(r'''
import tempfile
from pathlib import Path
from autonomy.db import AutonomyDB
from autonomy.budget import Budget
from autonomy.circuit import Circuit
from autonomy.runner import Runner
class Provider:
 def initialize(self,**kwargs): pass
 def prefetch(self,q): return "## Seed id=seed-123\n- summary: 记得照顾重要的人"
 def shutdown(self): pass
class Gateway:
 def __init__(self): self.prompt=""
 def chat(self,*args,**kwargs): self.prompt=args[-1]; return "sid", "我先想起了照顾重要的人。", {"total_tokens":1}
 def end(self,*args,**kwargs): pass
root=Path(tempfile.mkdtemp());db=AutonomyDB(root/"a.db");db.create_goal("GOAL_X","goal",created_by="user")
gw=Gateway();r=Runner(db,Budget(db,calls=20,tokens=100,prompt=100),Circuit(db),stop_file=str(root/"STOP"),gateway=gw,lock_path=str(root/"lock"),thought_log_path=str(root/"thoughts.log"),memory_provider=Provider(),hermes_home=str(root),mode="parallel")
assert r.tick()["status"]=="ok"
assert gw.prompt.index("seed-123") < gw.prompt.index("GOAL_X")
log=(root/"thoughts.log").read_text(encoding="utf-8"); assert "seed_ids=seed-123" in log
assert any("seed_ids=seed-123" in str(e.get("summary")) for e in db.list_events(20))
''')
