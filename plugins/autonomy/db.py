from __future__ import annotations
import hashlib, json, secrets, sqlite3, time, threading
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from .schema import migrate

def locked(fn):
 def wrapped(self,*a,**kw):
  with self.lock: return fn(self,*a,**kw)
 wrapped.__name__=fn.__name__; return wrapped

TRANSITIONS={'draft':{'proposed'},'proposed':{'approved','cancelled'},'approved':{'active','paused','cancelled'},'active':{'paused','completed','failed','cancelled'},'paused':{'active','cancelled'},'completed':set(),'failed':set(),'cancelled':set()}
POLICY_EVENT_KINDS=('triggered','generated','rejected_format','vetoed','expired_no_opportunity','expired_neutral','promoted_eligible','promoted','rollback_manual')
class Conflict(Exception): pass
class Expired(Exception): pass
class ImmutableEvidence(Exception): pass
class AutonomyDB:
 def __init__(self,path):
  self.lock=threading.RLock(); self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self.c=sqlite3.connect(self.path,timeout=10,check_same_thread=False); self.c.row_factory=sqlite3.Row; self.c.execute('pragma journal_mode=wal'); self.c.execute('pragma busy_timeout=10000'); self._init()
 def _init(self):
  migrate(self.c)
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
 def record_policy_event(self,rule_id,kind,reason='',payload=None,profile=''):
  try:
   ts=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%dT%H:%M:%S')
   self.c.execute('insert into policy_events(ts,profile,rule_id,kind,reason,payload_json) values(?,?,?,?,?,?)',(ts,profile or '',rule_id,kind,(reason or '')[:500],json.dumps(payload or {},ensure_ascii=False,default=str)))
   self.c.commit(); return True
  except Exception:
   try: self.c.rollback()
   except Exception: pass
   return False
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
 @locked
 def append_receipt(self,kind,payload_hash,status='unverified',run_id=None,goal_id=None,payload=None,ts=None):
  import uuid
  rid=uuid.uuid4().hex
  self.c.execute('insert into receipts(id,ts,kind,payload_hash,status,run_id,goal_id,payload_json) values(?,?,?,?,?,?,?,?)',(rid,ts or time.time(),kind,payload_hash,status,run_id,goal_id,json.dumps(payload or {},ensure_ascii=False,sort_keys=True,default=str)))
  self.c.commit(); return rid
 @locked
 def append_evidence(self,receipt_id,stream,digest,byte_count,head=b'',tail=b'',buffer_path=None,truncated=False,exit_code=None,signal=None,timed_out=False,oom=False,status='unverified',created_at=None,challenge_deadline=None):
  created_at=created_at or time.time(); challenge_deadline=challenge_deadline or created_at+6*3600
  cur=self.c.execute('insert into evidence(receipt_id,stream,digest,byte_count,head,tail,buffer_path,truncated,exit_code,signal,timed_out,oom,status,created_at,challenge_deadline) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(receipt_id,stream,digest,int(byte_count),sqlite3.Binary(head),sqlite3.Binary(tail),buffer_path,int(bool(truncated)),exit_code,signal,int(bool(timed_out)),int(bool(oom)),status,created_at,challenge_deadline))
  self.c.commit(); return int(cur.lastrowid)
 @locked
 def get_evidence(self,evidence_id):
  row=self.c.execute('select * from evidence where id=?',(int(evidence_id),)).fetchone()
  if not row: raise KeyError(evidence_id)
  return dict(row)
 @locked
 def list_pending_evidence(self,limit=10000):
  return [dict(r) for r in self.c.execute("select * from evidence where status='unverified' order by id limit ?",(min(int(limit),100000),))]
 @locked
 def transition_evidence(self,evidence_id,target,verified_at=None):
  allowed={'unverified':{'verified','mismatched','expired'},'verified':set(),'mismatched':set(),'expired':set()}
  row=self.c.execute('select status from evidence where id=?',(int(evidence_id),)).fetchone()
  if not row: raise KeyError(evidence_id)
  if target not in allowed.get(row['status'],set()): raise ImmutableEvidence('%s -> %s' % (row['status'],target))
  self.c.execute('update evidence set status=?,verified_at=? where id=?',(target,verified_at or time.time(),int(evidence_id)))
  self.c.commit(); return target
