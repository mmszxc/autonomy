import os,time,datetime
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
