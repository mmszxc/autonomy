from __future__ import annotations
import hashlib, json, secrets, sqlite3, time, threading
from pathlib import Path

def locked(fn):
 def wrapped(self,*a,**kw):
  with self.lock: return fn(self,*a,**kw)
 wrapped.__name__=fn.__name__; return wrapped

TRANSITIONS={'draft':{'proposed'},'proposed':{'approved','cancelled'},'approved':{'active','paused','cancelled'},'active':{'paused','completed','failed','cancelled'},'paused':{'active','cancelled'},'completed':set(),'failed':set(),'cancelled':set()}
class Conflict(Exception): pass
class Expired(Exception): pass
class AutonomyDB:
 def __init__(self,path):
  self.lock=threading.RLock(); self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self.c=sqlite3.connect(self.path,timeout=10,check_same_thread=False); self.c.row_factory=sqlite3.Row; self.c.execute('pragma journal_mode=wal'); self.c.execute('pragma busy_timeout=10000'); self._init()
 def _init(self):
  self.c.executescript('''create table if not exists goals(id text primary key,title text not null,description text not null,status text not null,risk text not null default 'low',budget_calls integer not null default 24,used_calls integer not null default 0,budget_seconds real not null default 300,stop_reason text,created_by text not null,created_at real not null,updated_at real not null); create table if not exists tasks(id text primary key,goal_id text not null,parent_id text,title text,status text,risk text,created_at real); create table if not exists approvals(approval_id text primary key,goal_id text,tool text,args_hash text, args_len integer,side_effects text,budget text,expires_at real,nonce_hash text,state text not null default 'pending',decided_at real); create table if not exists events(id integer primary key,ts real,run_id text,goal_id text,kind text,tool text,approval_id text,args_hash text,result_hash text,exit_reason text,args_len integer,result_len integer);''');
  cols={r[1] for r in self.c.execute('pragma table_info(events)')}
  if 'summary' not in cols: self.c.execute('alter table events add column summary text')
  self.c.execute("create table if not exists usage(day text primary key,calls integer not null default 0,tokens integer not null default 0,prompt integer not null default 0)")
  self.c.execute("create table if not exists circuit(id integer primary key check(id=1),failures integer not null default 0,state text not null default 'closed',blocked integer not null default 0,opened_at real not null default 0)")
  self.c.execute("insert or ignore into circuit(id) values(1)")
  self.c.execute("create table if not exists last_tick(id integer primary key check(id=1),status text,started_at real,finished_at real,summary text)")
  self.c.commit()
 @locked
 def list_goals(self): return [dict(r) for r in self.c.execute('select * from goals order by created_at desc')]
 @locked
 def create_goal(self,title,description,created_by='model'):
  if not isinstance(title,str) or not isinstance(description,str) or not title.strip() or len(title)>200 or len(description)>2000: raise ValueError('invalid goal title/description')
  now=time.time(); day_start=now-(now%86400)
  if created_by=='model' and self.c.execute('select count(*) from goals where created_by=? and created_at>=?',('model',day_start)).fetchone()[0] >= 12: raise ValueError('daily model proposal limit reached')
  status='approved' if created_by=='user' else 'proposed'; import uuid; gid=uuid.uuid4().hex; self.c.execute('insert into goals values(?,?,?,?,?,?,?,?,?,?,?,?)',(gid,title,description,status,'low',24,0,300,None,created_by,now,now,)); self.c.commit(); return gid,status
 @locked
 def decide_goal(self,gid,action):
  row=self.c.execute('select status from goals where id=?',(gid,)).fetchone();
  if not row: raise KeyError(gid)
  target={'approve':'approved','pause':'paused','resume':'active','cancel':'cancelled'}[action];
  if target not in TRANSITIONS[row['status']]: raise Conflict('invalid transition')
  self.c.execute('update goals set status=?,updated_at=? where id=?',(target,time.time(),gid)); self.c.commit(); return target
 @locked
 def list_pending_approvals(self): return [dict(r) for r in self.c.execute('select * from approvals where state="pending" and expires_at>?',(time.time(),))]
 @locked
 def add_approval(self,goal_id,tool,args,expires_s=300):
  row=self.c.execute('select status from goals where id=?',(goal_id,)).fetchone()
  if not row or row['status'] in {'completed','failed','cancelled'}: raise ValueError('goal missing or terminated')
  if not isinstance(expires_s,(int,float)) or not 10 <= expires_s <= 3600: raise ValueError('expires_s must be 10..3600')
  if not isinstance(tool,str) or not 1 <= len(tool) <= 200: raise ValueError('invalid tool length')
  if not isinstance(args,str) or len(args)>20000: raise ValueError('invalid args length')
  aid=secrets.token_hex(12); nonce=secrets.token_urlsafe(24); now=time.time(); self.c.execute('insert into approvals values(?,?,?,?,?,?,?,?,?,?,?)',(aid,goal_id,tool,hashlib.sha256(args.encode()).hexdigest(),len(args),'','',now+expires_s,hashlib.sha256(nonce.encode()).hexdigest(),'pending',None)); self.c.commit(); return aid,nonce
 @locked
 def decide_approval(self,aid,decision,nonce):
  r=self.c.execute('select * from approvals where approval_id=?',(aid,)).fetchone();
  if not r: raise KeyError(aid)
  if r['state']!='pending' or r['expires_at']<=time.time(): raise Expired(aid)
  if not secrets.compare_digest(r['nonce_hash'],hashlib.sha256(nonce.encode()).hexdigest()): raise Conflict('nonce replay/invalid')
  state={'allow_once':'allowed_once','allow_goal':'allowed_goal','deny':'denied'}[decision]; self.c.execute('update approvals set state=?,decided_at=? where approval_id=?',(state,time.time(),aid)); self.c.commit(); return state
 @locked
 def status(self):
  u=self.c.execute("select calls,tokens,prompt from usage where day=date('now')").fetchone(); c=self.c.execute('select failures,state,blocked,opened_at from circuit where id=1').fetchone()
  return {'pending_approvals':len(self.list_pending_approvals()),'daily_calls':u[0] if u else 0,'daily_tokens':u[1] if u else 0,'daily_prompt_tokens':u[2] if u else 0,'circuit_state':c[1] if c else 'closed','circuit_failures':c[0] if c else 0,'circuit_blocked':bool(c[2]) if c else False}
 @locked
 def reset_circuit(self):
  """Clear legacy/open breaker state after caller has backed up the DB."""
  self.c.execute("update circuit set failures=0,state='closed',blocked=0,opened_at=0 where id=1")
  self.c.commit()
  return True
 @locked
 def append_event(self,**kw):
  vals={k:kw.get(k) for k in ('ts','run_id','goal_id','kind','tool','approval_id','args_hash','result_hash','exit_reason','args_len','result_len','summary')}; vals['ts']=vals['ts'] or time.time(); self.c.execute('insert into events(ts,run_id,goal_id,kind,tool,approval_id,args_hash,result_hash,exit_reason,args_len,result_len,summary) values(:ts,:run_id,:goal_id,:kind,:tool,:approval_id,:args_hash,:result_hash,:exit_reason,:args_len,:result_len,:summary)',vals); self.c.commit()
 @locked
 def list_events(self,limit=50): return [dict(r) for r in self.c.execute('select * from events order by id desc limit ?',(min(limit,500),))]
 @locked
 def recent_runs(self,limit=10):
  rows=self.c.execute("select ts,run_id,kind,exit_reason,summary from events where kind in ('tick','run_complete','run_timeout','run_preempted') order by id desc limit ?",(min(limit,50),)).fetchall()
  return [dict(r) for r in rows]
 @locked
 def prune_events(self,retention_days=30,max_rows=10000):
  cutoff=time.time()-max(1,retention_days)*86400
  self.c.execute('delete from events where ts < ?', (cutoff,))
  self.c.execute('delete from events where id not in (select id from events order by id desc limit ?)', (max(1,max_rows),))
  removed=self.c.total_changes; self.c.commit(); return removed
