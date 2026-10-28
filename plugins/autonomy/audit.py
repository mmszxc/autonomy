import hashlib

def digest(value): return hashlib.sha256(value.encode()).hexdigest()
def event(db,**kw):
 args=kw.pop('args',''); result=kw.pop('result',''); kw.update(args_hash=digest(args),result_hash=digest(result),args_len=len(args),result_len=len(result)); db.append_event(**kw)
