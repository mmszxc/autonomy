import os,time,datetime
from dataclasses import dataclass
class Budget:
 def __init__(self,db=None,calls=None,tokens=None,prompt=None,clock=None):
  self.db=db; self._calls=0; self._tokens=0; self._prompt=0
  # CLI/tick resolve env before config; retain that precedence as observable
  # state so status can reveal when deployment environment overrides YAML.
  env_override=any(os.getenv(k) is not None for k in ('AUTONOMY_DAILY_CALLS','AUTONOMY_DAILY_TOKENS','AUTONOMY_DAILY_PROMPT_TOKENS'))
  explicit_config=any(value is not None for value in (calls,tokens,prompt))
  self.daily_tokens_source='env' if env_override else ('config' if explicit_config else 'default')
  self.calls=int(calls or os.getenv('AUTONOMY_DAILY_CALLS',24)); self.tokens=int(tokens or os.getenv('AUTONOMY_DAILY_TOKENS',72000)); self.prompt=int(prompt or os.getenv('AUTONOMY_DAILY_PROMPT_TOKENS',60000)); self.clock=clock or time.time
 def _day(self): return datetime.datetime.fromtimestamp(self.clock(),datetime.timezone.utc).date().isoformat()
 def _row(self): return self.db.c.execute('select calls,tokens,prompt from usage where day=?',(self._day(),)).fetchone()
 def allow(self):
  if not self.db: return self._calls<self.calls and self._tokens<self.tokens and self._prompt<self.prompt
  r=self._row(); return not r or (r[0]<self.calls and r[1]<self.tokens and r[2]<self.prompt)
 def consume(self,completion_tokens=0,prompt_tokens=0):
  if not self.db: self._calls+=1; self._tokens+=int(completion_tokens)+int(prompt_tokens); self._prompt+=int(prompt_tokens); return True
  day=self._day(); total=int(completion_tokens)+int(prompt_tokens)
  self.db.c.execute('insert into usage(day,calls,tokens,prompt) values(?,?,?,?) on conflict(day) do update set calls=calls+1,tokens=tokens+excluded.tokens,prompt=prompt+excluded.prompt',(day,1,total,int(prompt_tokens))); self.db.c.commit(); return True
 def status(self):
  r=self._row(); return {'calls':r[0] if r else 0,'tokens':r[1] if r else 0,'prompt':r[2] if r else 0,'calls_limit':self.calls,'tokens_limit':self.tokens,'prompt_limit':self.prompt,'source':'env' if self.daily_tokens_source == 'env' else 'config','daily_tokens_source':self.daily_tokens_source,'daily_tokens_effective':self.tokens}


class BudgetLedger:
 """Restart-independent weekly bucket accounting; tokens and tool calls map to units."""
 shares={'normal':0.80,'memory':0.10,'evolution':0.10}
 def __init__(self,weekly_units,db=None): self.weekly_units=float(weekly_units); self.db=db; self.rows=[]
 def consume(self,bucket,units,at=None,metadata=None):
  if bucket not in self.shares: raise ValueError(bucket)
  at=float(at or time.time()); row=(at,bucket,float(units),metadata or {}); self.rows.append(row)
  if self.db:
   dt=datetime.datetime.fromtimestamp(at,datetime.timezone.utc); week=f'{dt.isocalendar().year}-W{dt.isocalendar().week:02d}'
   self.db.c.execute('insert into budget_ledger(ts,week,day,bucket,units,metadata_json) values(?,?,?,?,?,?)',(at,week,dt.date().isoformat(),bucket,float(units),'{}')); self.db.c.commit()
 def status(self,at=None):
  at=float(at or time.time()); dt=datetime.datetime.fromtimestamp(at,datetime.timezone.utc); iso=dt.isocalendar(); day=dt.date()
  weekly={k:0.0 for k in self.shares}; daily=0.0
  for ts,bucket,units,_ in self.rows:
   stamp=datetime.datetime.fromtimestamp(ts,datetime.timezone.utc)
   if stamp.isocalendar()[:2]==iso[:2]: weekly[bucket]+=units
   if stamp.date()==day: daily+=units
  out={k:{'used':weekly[k],'limit':self.weekly_units*v,'within_cap': weekly[k] <= self.weekly_units*v} for k,v in self.shares.items()}
  out['daily_warning']=daily > self.weekly_units*0.30; out['daily_used']=daily
  return out


@dataclass(frozen=True)
class ResourceSnapshot:
 filesystem_size:int
 free_bytes:int
 evidence_pool_bytes:int
 evidence_pool_cap:int
 @property
 def pool_level(self):
  ratio=self.evidence_pool_bytes/max(1,self.evidence_pool_cap)
  return 'freeze' if ratio>=.95 else ('warn' if ratio>=.80 else 'ok')
 @property
 def free_space_level(self):
  t1=max(2*1024**3,min(int(self.filesystem_size*.05),10*1024**3))
  return 'critical' if self.free_bytes<1024**3 else ('freeze' if self.free_bytes<t1 else 'ok')
