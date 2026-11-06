"""Compatibility facade; persistence semantics live in event_log.py."""
import hashlib

from .event_log import EventLog


def digest(value): return hashlib.sha256(value.encode()).hexdigest()
def event(db,**kw):
 args=kw.pop('args',''); result=kw.pop('result',''); kw.update(args_hash=digest(args),result_hash=digest(result),args_len=len(args),result_len=len(result)); db.append_event(**kw)

def receipt(db, kind, payload_hash, **kw): return EventLog(db).append_receipt(kind, payload_hash, **kw)
def evidence(db, receipt_id, stream, data, **kw): return EventLog(db).append_evidence(receipt_id, stream, data, **kw)
def checkpoint(db, **kw): return EventLog(db).checkpoint(**kw)
