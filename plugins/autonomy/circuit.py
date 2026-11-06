import os,time
class Circuit:
 def __init__(self,db=None,threshold=3,cooldown_s=None,clock=None):
  if db is not None and not hasattr(db,'c'): threshold,db=db,None
  self.db=db; self._failures=0; self._state='closed'; self._blocked=False; self._opened=0; self.threshold=threshold; self.cooldown=float(cooldown_s or os.getenv('AUTONOMY_CIRCUIT_COOLDOWN_S',1800)); self.clock=clock or time.time
  self._learning_frozen=False; self._promotion_frozen=False; self._rollback=False; self._freeze_reason=None
  # Upgrade the legacy permanent-block bit to a recoverable half-open path.
  if self.db:
   row=self._row()
   if row[2]:
    self.db.c.execute("update circuit set failures=?,state='open',opened_at=0,blocked=0 where id=1",(max(row[0],self.threshold),)); self.db.c.commit()
 @property
 def failures(self): return self._row()[0] if self.db else self._failures
 def _row(self): return self.db.c.execute('select failures,state,blocked,opened_at from circuit where id=1').fetchone()
 def allow(self):
  if not self.db:
   if self._blocked:return False
   if self._state=='open' and self.clock()-self._opened>=self.cooldown: self._state='half_open'; return True
   return self._state!='open'
  f,s,b,o=self._row()
  if b:return False
  if s=='open' and self.clock()-o>=self.cooldown:
   self.db.c.execute("update circuit set state='half_open' where id=1"); self.db.c.commit(); return True
  return s!='open'
 def success(self):
  if self.db:self.db.c.execute("update circuit set failures=0,state='closed',opened_at=0 where id=1"); self.db.c.commit()
 def failure(self,status=None):
  # Auth/config HTTP errors are recoverable after credentials/routes change.
  # Treat them like all other failures and use open/cooldown/half-open.
  if not self.db:
   self._failures+=1
   if self._failures>=self.threshold: self._state='open'; self._opened=self.clock()
   return
  r=self._row(); n=r[0]+1; self.db.c.execute("update circuit set failures=?,state=?,opened_at=? where id=1",(n,'open' if n>=self.threshold else r[1],self.clock() if n>=self.threshold else r[3]))
  self.db.c.commit()
 def reset(self):
  if self.db:
   self.db.c.execute("update circuit set failures=0,state='closed',blocked=0,opened_at=0 where id=1"); self.db.c.commit()
  else:self._failures=0; self._state='closed'; self._blocked=False; self._opened=0
 def status(self):
  r=self._row() if self.db else (self._failures,self._state,self._blocked,self._opened)
  return {'failures':r[0],'state':r[1],'blocked':bool(r[2]),'opened_at':r[3],'learning_frozen':self._learning_frozen,'promotion_frozen':self._promotion_frozen,'freeze_reason':self._freeze_reason,'rollback_requested':self._rollback}
 def allow_normal(self): return self.allow()
 def allow_learning(self): return self.allow() and not self._learning_frozen
 def allow_promotion(self): return self.allow_learning() and not self._promotion_frozen
 def freeze_learning(self,reason,rollback=False):
  self._learning_frozen=True; self._promotion_frozen=True; self._freeze_reason=str(reason); self._rollback=self._rollback or bool(rollback)
 def unfreeze_learning(self,operator_confirmation):
  if not operator_confirmation: raise ValueError('operator confirmation required')
  self._learning_frozen=False; self._promotion_frozen=False; self._freeze_reason=None
 def rollback_requested(self): return self._rollback
 def acknowledge_rollback(self): self._rollback=False
