"""Append-only L0 event/evidence writer and checkpoint state machine."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from .db import AutonomyDB, ImmutableEvidence


class EvidenceStateError(ImmutableEvidence):
    pass


class EventLog:
    def __init__(self, db: AutonomyDB, challenge_sla_s: float = 6 * 3600):
        self.db = db
        self.challenge_sla_s = float(challenge_sla_s)

    def append_receipt(self, kind: str, payload_hash: str, **kwargs) -> str:
        return self.db.append_receipt(kind, payload_hash, **kwargs)

    def append_evidence(self, receipt_id: str, stream: str, data: bytes, **metadata) -> int:
        """Append evidence without re-hashing redacted snippets from streaming captures."""
        data = data if isinstance(data, bytes) else str(data).encode("utf-8", "replace")
        digest = str(metadata.pop("digest", hashlib.sha256(data).hexdigest()))
        byte_count = int(metadata.pop("byte_count", len(data)))
        metadata.setdefault("challenge_deadline", time.time() + self.challenge_sla_s)
        return self.db.append_evidence(
            receipt_id=receipt_id,
            stream=stream,
            digest=digest,
            byte_count=byte_count,
            head=metadata.pop("head", data[:256]),
            tail=metadata.pop("tail", data[-256:]),
            **metadata,
        )

    def transition(self, evidence_id: int, target: str) -> str:
        try:
            return self.db.transition_evidence(evidence_id, target)
        except ImmutableEvidence as exc:
            raise EvidenceStateError(str(exc)) from exc

    def checkpoint(self, now: Optional[float] = None) -> dict:
        previous = self.db.c.execute(
            "select row_count,last_evidence_id,root_hash from checkpoints order by id desc limit 1"
        ).fetchone()
        previous_count = int(previous["row_count"]) if previous else 0
        previous_id = int(previous["last_evidence_id"]) if previous else 0
        rows = self.db.c.execute(
            "select id,receipt_id,stream,digest,byte_count,status from evidence where id>? order by id",
            (previous_id,),
        ).fetchall()
        # Chain checkpoints, hashing only newly appended rows rather than all history.
        digest = hashlib.sha256((previous["root_hash"] if previous else "").encode())
        for row in rows:
            digest.update(json.dumps(dict(row), sort_keys=True, separators=(",", ":")).encode())
            digest.update(b"\n")
        result = {
            "created_at": now or time.time(),
            "row_count": previous_count + len(rows),
            "last_evidence_id": int(rows[-1]["id"]) if rows else previous_id,
            "root_hash": digest.hexdigest() if rows else (previous["root_hash"] if previous else hashlib.sha256(b"").hexdigest()),
        }
        self.db.c.execute(
            "insert into checkpoints(created_at,row_count,last_evidence_id,root_hash) values(?,?,?,?)",
            (result["created_at"], result["row_count"], result["last_evidence_id"], result["root_hash"]),
        )
        self.db.c.commit()
        return result

    @staticmethod
    def expire_buffers(root: Path, ttl_s: int = 24 * 3600, now: Optional[float] = None) -> int:
        now = now or time.time(); removed = 0
        if not Path(root).exists():
            return 0
        for path in Path(root).glob("evidence-*.bin"):
            if now - path.stat().st_mtime >= ttl_s:
                path.unlink(missing_ok=True); removed += 1
        return removed
