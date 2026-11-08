"""Independent evidence hash challenge job.

The job is intentionally separate from tick: it only reads evidence buffers and
writes challenge results.  One row per evidence id makes reruns idempotent.
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from .db import AutonomyDB
from .schema import migrate


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


def _hash_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            count += len(block)
    return digest.hexdigest(), count


def run_challenges(
    conn: sqlite3.Connection,
    *,
    now: Optional[float] = None,
    hasher: Callable[[str], tuple[str, int]] = _hash_file,
) -> dict[str, int]:
    """Challenge every pending evidence row, returning status counts.

    ``evidence_challenges.evidence_id`` is the idempotency key.  A late run is
    recorded as ``overdue`` and does not silently claim verification.
    """
    now = time.time() if now is None else float(now)
    migrate(conn)
    rows = conn.execute(
        """SELECT id,digest,byte_count,buffer_path,challenge_deadline
           FROM evidence WHERE status='unverified'
           ORDER BY id"""
    ).fetchall()
    counts = {"verified": 0, "mismatched": 0, "unavailable": 0, "overdue": 0}
    for evidence_id, expected, expected_bytes, buffer_path, deadline in rows:
        observed_digest = observed_bytes = error = None
        status = "unavailable"
        try:
            if not buffer_path:
                raise FileNotFoundError("evidence buffer_path is empty")
            observed_digest, observed_bytes = hasher(buffer_path)
            status = "verified" if observed_digest == expected and observed_bytes == expected_bytes else "mismatched"
            if status == "verified":
                conn.execute("UPDATE evidence SET status='verified', verified_at=? WHERE id=?", (now, evidence_id))
            else:
                conn.execute("UPDATE evidence SET status='mismatched' WHERE id=?", (evidence_id,))
        except (OSError, ValueError) as exc:
            error = str(exc)
        if now > float(deadline):
            challenge_status = "overdue"
        else:
            challenge_status = status
        conn.execute(
            """INSERT INTO evidence_challenges
               (evidence_id,challenged_at,deadline,status,observed_digest,observed_bytes,error)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(evidence_id) DO UPDATE SET
                 challenged_at=excluded.challenged_at, deadline=excluded.deadline,
                 status=excluded.status, observed_digest=excluded.observed_digest,
                 observed_bytes=excluded.observed_bytes, error=excluded.error""",
            (evidence_id, now, deadline, challenge_status, observed_digest, observed_bytes, error),
        )
        counts[challenge_status] += 1
    conn.commit()
    return counts


def challenge_pending(db: AutonomyDB, source: Callable[[dict], bytes], *, now: Optional[float] = None) -> ChallengeResult:
    """Compatibility API used by the in-process reconciliation path."""
    now = time.time() if now is None else now
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
            continue
        db.transition_evidence(row["id"], target, verified_at=now)
        verified += target == "verified"; mismatched += target == "mismatched"
    total = db.c.execute("select count(*) from evidence").fetchone()[0]
    pending = db.c.execute("select count(*) from evidence where status='unverified'").fetchone()[0]
    return ChallengeResult(len(rows), int(verified), int(mismatched), overdue, pending / max(1, total))


def reconcile(event_ids: Iterable[str], receipt_ids: Iterable[str], snapshot_ids: Iterable[str]) -> dict:
    event, receipt, snapshot = set(event_ids), set(receipt_ids), set(snapshot_ids)
    union = event | receipt | snapshot
    agreed = event & receipt & snapshot
    return {"total": len(union), "agreed": len(agreed), "difference_ratio": (len(union) - len(agreed)) / max(1, len(union)), "event_only": sorted(event - receipt - snapshot), "receipt_only": sorted(receipt - event - snapshot), "snapshot_only": sorted(snapshot - event - receipt)}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="challenge pending autonomy evidence")
    parser.add_argument("--db", required=True, help="autonomy SQLite database")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    try:
        run_challenges(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
