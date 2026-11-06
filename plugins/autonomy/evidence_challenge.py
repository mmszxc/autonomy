"""Hourly evidence verification and three-way reconciliation job."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Optional

from .db import AutonomyDB


@dataclass(frozen=True)
class ChallengeResult:
    checked: int
    verified: int
    mismatched: int
    overdue: int
    unverified_ratio: float

    @property
    def freeze_learning(self) -> bool:
        return self.mismatched > 0 or self.overdue > 0 or self.unverified_ratio > 0.05


def challenge_pending(db: AutonomyDB, source: Callable[[dict], bytes], *, now: Optional[float] = None) -> ChallengeResult:
    """Verify 100% of pending evidence; callers schedule this hourly."""
    now = now or time.time()
    rows = db.list_pending_evidence(limit=100000)
    verified = mismatched = overdue = 0
    for row in rows:
        if float(row["challenge_deadline"]) < now:
            overdue += 1
        try:
            raw = source(row)
            raw = raw if isinstance(raw, bytes) else str(raw).encode("utf-8", "replace")
            target = "verified" if hashlib.sha256(raw).hexdigest() == row["digest"] else "mismatched"
        except (FileNotFoundError, KeyError, OSError):
            # Source unavailable remains explicitly unverified; it is never silently accepted.
            continue
        db.transition_evidence(row["id"], target, verified_at=now)
        verified += target == "verified"; mismatched += target == "mismatched"
    total = db.c.execute("select count(*) from evidence").fetchone()[0]
    pending = db.c.execute("select count(*) from evidence where status='unverified'").fetchone()[0]
    return ChallengeResult(len(rows), int(verified), int(mismatched), overdue, pending / max(1, total))


def reconcile(event_ids: Iterable[str], receipt_ids: Iterable[str], snapshot_ids: Iterable[str]) -> dict:
    """Compare event table, CLI receipt and file snapshot identifiers."""
    event, receipt, snapshot = set(event_ids), set(receipt_ids), set(snapshot_ids)
    union = event | receipt | snapshot
    agreed = event & receipt & snapshot
    return {
        "total": len(union), "agreed": len(agreed),
        "difference_ratio": (len(union) - len(agreed)) / max(1, len(union)),
        "event_only": sorted(event - receipt - snapshot),
        "receipt_only": sorted(receipt - event - snapshot),
        "snapshot_only": sorted(snapshot - event - receipt),
    }
