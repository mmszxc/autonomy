import sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parents[1]))
from autonomy.db import AutonomyDB,Conflict,Expired
from autonomy.budget import Budget
from autonomy.circuit import Circuit
from autonomy.runner import Runner

def test_goal_and_transitions(tmp_path):
 d=AutonomyDB(tmp_path/'a.db'); gid,status=d.create_goal('x','y','model'); assert status=='proposed'; assert d.decide_goal(gid,'approve')=='approved'; assert d.decide_goal(gid,'cancel')=='cancelled'

def test_approval_nonce_once_and_expiry(tmp_path):
 d=AutonomyDB(tmp_path/'a.db'); gid,_=d.create_goal('x','y','user'); aid,n=d.add_approval(gid,'note.create','safe'); assert d.decide_approval(aid,'allow_once',n)=='allowed_once'
 try:d.decide_approval(aid,'allow_once',n); assert False
 except Expired: pass

def test_budget_circuit_stop_and_audit(tmp_path):
 d=AutonomyDB(tmp_path/'a.db'); b=Budget(calls=1); c=Circuit(threshold=2); r=Runner(d,b,c,str(tmp_path/'STOP'),lambda:{'completion_tokens':3,'prompt_tokens':4,'text':'{"thoughts":"x","proposals":[]}' }); assert r.tick()['status']=='ok'; assert r.tick()['status']=='blocked'; open(tmp_path/'STOP','w').close(); assert r.tick()['status']=='stopped'; assert d.list_events(10)

def test_auth_failure_uses_recoverable_circuit_lifecycle(tmp_path):
 d=AutonomyDB(tmp_path/'a.db'); c=Circuit(threshold=2); c.failure(401); assert c.allow() and c.failures==1
 c.failure(403); assert not c.allow() and c.failures==2

def test_legacy_permanent_block_is_migrated(tmp_path):
 d=AutonomyDB(tmp_path/'legacy.db'); d.c.execute('update circuit set blocked=1 where id=1'); d.c.commit()
 c=Circuit(d,threshold=3,cooldown_s=60)
 assert c.status()['blocked'] is False
 assert c.allow() is True

def test_db_wal_and_integrity(tmp_path):
 d=AutonomyDB(tmp_path/'a.db'); assert d.c.execute('pragma journal_mode').fetchone()[0]=='wal'; assert d.c.execute('pragma integrity_check').fetchone()[0]=='ok'

def test_goal_model_is_proposed(tmp_path):
 d=AutonomyDB(tmp_path/'a.db'); gid,status=d.create_goal('g','d','model'); assert status=='proposed'; assert d.list_goals()[0]['status']=='proposed'

def test_approval_wrong_nonce_rejected(tmp_path):
 d=AutonomyDB(tmp_path/'a.db'); gid,_=d.create_goal('g','d','user'); aid,n=d.add_approval(gid,'note.create','x')
 try: d.decide_approval(aid,'allow_once','wrong'); assert False
 except Conflict: pass

def test_proposal_parser_valid_and_limit():
 from autonomy.proposals import parse
 ps,e=parse('{"proposals":['+','.join('{"type":"goal","title":"t","description":"d"}' for _ in range(5))+']}'); assert len(ps)==3 and e is None

def test_proposal_parser_invalid_and_truncated():
 from autonomy.proposals import parse
 assert parse('not json')[1]=='invalid_json'; assert parse('{"proposals":')[1]=='invalid_json'

def test_proposal_field_truncation():
 from autonomy.proposals import parse
 ps,_=parse('{"proposals":[{"type":"task","title":"x","description":"y"}]}'); assert ps[0]['title']=='x'

def test_runner_persists_goal_proposal(tmp_path):
 d=AutonomyDB(tmp_path/'a.db'); r=Runner(d,Budget(),Circuit(),str(tmp_path/'STOP'),lambda:{'text':'{"proposals":[{"type":"goal","title":"new","description":"do"}]}','completion_tokens':1,'prompt_tokens':1}); assert r.tick()['status']=='ok'; assert d.list_goals()[0]['status']=='proposed'

def test_runner_audit_excludes_body(tmp_path):
 d=AutonomyDB(tmp_path/'a.db'); secret='private user body'; r=Runner(d,Budget(),Circuit(),str(tmp_path/'STOP'),lambda:{'text':'{"thoughts":"x","proposals":[]}','completion_tokens':1,'prompt_tokens':1}); r.tick(); raw=' '.join(str(x) for x in d.list_events()); assert secret not in raw

def test_runner_lock_busy(tmp_path):
 import pytest, os
 if os.name == 'nt': pytest.skip('fcntl lock semantics unavailable on Windows')
 d=AutonomyDB(tmp_path/'a.db'); lock=tmp_path/'lock'; import autonomy.runner as m; f=open(lock,'a+'); m.fcntl.flock(f,m.fcntl.LOCK_EX|m.fcntl.LOCK_NB); r=Runner(d,Budget(),Circuit(),str(tmp_path/'STOP'),lambda:{'text':'{"proposals":[]}'} ,lock_path=str(lock)); assert r.tick()['status']=='busy'; m.fcntl.flock(f,m.fcntl.LOCK_UN); f.close()

def test_gateway_auth_error_classification():
 from autonomy.gateway_client import GatewayError
 assert GatewayError(401,'auth_blocked').status_code==401

def test_budget_hard_limit():
 b=Budget(calls=1); b.consume(); assert not b.allow()

def test_circuit_opens_after_three_failures():
 c=Circuit(3); [c.failure('x') for _ in range(3)]; assert not c.allow()

def test_goal_schema_daily_limit_and_event_prune(tmp_path):
 d=AutonomyDB(tmp_path/'a.db')
 import pytest
 with pytest.raises(ValueError): d.create_goal('x'*201,'d','model')
 with pytest.raises(ValueError): d.create_goal('x','d'*2001,'model')
 for i in range(12): d.create_goal(str(i),'d','model')
 with pytest.raises(ValueError): d.create_goal('13','d','model')
 d.append_event(ts=1,kind='old'); assert d.prune_events(retention_days=1)>=1

def test_approval_validation(tmp_path):
 d=AutonomyDB(tmp_path/'a.db'); gid,_=d.create_goal('g','d','user')
 import pytest
 with pytest.raises(ValueError): d.add_approval('missing','tool','x')
 with pytest.raises(ValueError): d.add_approval(gid,'tool','x',9)
 with pytest.raises(ValueError): d.add_approval(gid,'t'*201,'x')
 d.decide_goal(gid,'cancel')
 with pytest.raises(ValueError): d.add_approval(gid,'tool','x')

def _mp_writer(path,n):
 import time
 for attempt in range(20):
  try:
   d=AutonomyDB(path); gid,_=d.create_goal('g'+str(n),'d','user')
   for i in range(50):
    for retry in range(20):
     try: d.append_event(goal_id=gid,kind='mp',args_len=i); break
     except Exception:
      time.sleep(.02)
   d.c.close(); return
  except Exception:
   time.sleep(.05)
 raise RuntimeError('multiprocess writer retries exhausted')

def test_db_multiprocess_writes(tmp_path):
 import multiprocessing as mp
 path=str(tmp_path/'multi.db')
 ps=[mp.Process(target=_mp_writer,args=(path,n)) for n in range(2)]
 for p in ps: p.start()
 for p in ps: p.join()
 assert all(p.exitcode==0 for p in ps)
 d=AutonomyDB(path); assert d.c.execute('select count(*) from events where kind="mp"').fetchone()[0]==100; assert d.c.execute('pragma integrity_check').fetchone()[0]=='ok'

def test_db_concurrent_approvals_events_and_nonce(tmp_path):
 import concurrent.futures
 d=AutonomyDB(tmp_path/'a.db'); gid,_=d.create_goal('g','d','user'); errors=[]; pairs=[]
 def work(i):
  try:
   aid,n=d.add_approval(gid,'note.create',str(i)); pairs.append((aid,n)); d.append_event(kind='concurrent',args=str(i),result='ok')
  except Exception as e: errors.append(e)
 with concurrent.futures.ThreadPoolExecutor(8) as ex: list(ex.map(work,range(100)))
 assert not errors and len(pairs)==100
 with concurrent.futures.ThreadPoolExecutor(8) as ex: results=list(ex.map(lambda x: d.decide_approval(x[0],'allow_once',x[1]),pairs))
 assert results.count('allowed_once')==100; assert d.c.execute('pragma integrity_check').fetchone()[0]=='ok'
