"""Stage 0, observe-only memory dreaming.

This module deliberately does not import or call ``store`` mutators.  Dream output is
written only to ``dream_candidates``; a later approval step may apply candidates.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

ALLOWED_EDGE_TYPES = frozenset({"caused_by", "led_to"})


@dataclass(frozen=True)
class DreamCandidate:
    kind: str
    source_ids: tuple[str, ...]
    payload: Mapping[str, Any]
    reason: str
    validation: Mapping[str, Any]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_record(self) -> dict[str, Any]:
        return {"kind": self.kind, "source_ids": list(self.source_ids),
                "payload": dict(self.payload), "reason": self.reason,
                "validation": dict(self.validation), "created_at": self.created_at}


@dataclass(frozen=True)
class DreamReceipt:
    job: str
    status: str
    candidates: tuple[DreamCandidate, ...] = ()
    flags: tuple[DreamCandidate, ...] = ()
    diagnostics: Mapping[str, int] = field(default_factory=dict)
    reason: str = ""


def ensure_candidate_table(conn: sqlite3.Connection) -> None:
    """Create the isolated candidate table; never creates/updates active memory tables."""
    conn.execute("""CREATE TABLE IF NOT EXISTS dream_candidates (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_ids TEXT NOT NULL,
        payload TEXT NOT NULL, reason TEXT NOT NULL, validation TEXT NOT NULL,
        created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
    )""")
    conn.commit()


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _ts(item: Any) -> datetime:
    raw = _value(item, "timestamp")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def validate_edge(candidate: Mapping[str, Any], seeds: Iterable[Any], batch_ids: set[str] | None = None) -> dict[str, Any]:
    """Script-side validation; model confidence is intentionally ignored."""
    rows = {_value(s, "id"): s for s in seeds}
    a, b = candidate.get("from_id"), candidate.get("to_id")
    edge_type = candidate.get("edge_type")
    checks = {
        "ids_exist": bool(a and b and a in rows and b in rows),
        "edge_type_allowed": edge_type in ALLOWED_EDGE_TYPES,
        "temporal_order": bool(a in rows and b in rows and _ts(rows[a]) < _ts(rows[b])),
        "not_same_batch": not (batch_ids and a in batch_ids and b in batch_ids),
        "quotes_present": bool(candidate.get("from_quote") and candidate.get("to_quote")),
    }
    checks["valid"] = all(checks.values())
    return checks


def causal_completion(seeds: Sequence[Any], batch_ids: set[str] | None = None, limit: int = 20) -> DreamReceipt:
    """Suggest temporal causal links only; does not write seed_edges."""
    rows = [s for s in seeds if not _value(s, "caused_by", []) and not _value(s, "led_to", [])]
    out: list[DreamCandidate] = []
    for earlier in rows:
        for later in rows:
            a, b = _value(earlier, "id"), _value(later, "id")
            if not a or a == b or _ts(earlier) >= _ts(later):
                continue
            summary_a, summary_b = str(_value(earlier, "summary", "")), str(_value(later, "summary", ""))
            proposal = {"from_id": a, "to_id": b, "edge_type": "led_to",
                        "from_quote": summary_a, "to_quote": summary_b}
            validation = validate_edge(proposal, rows, batch_ids)
            if validation["valid"]:
                out.append(DreamCandidate("causal_edge", (a, b), proposal,
                    "both seeds have no existing causal links", validation))
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break
    return DreamReceipt("causal_completion", "ok", candidates=tuple(out))


def semantic_review(memories: Sequence[Any]) -> DreamReceipt:
    """Flag duplicate/contradictory/source-less semantic memories, without rewriting them."""
    flags: list[DreamCandidate] = []
    norm: dict[str, list[Any]] = {}
    for memory in memories:
        statement = str(_value(memory, "statement", "")).strip()
        key = " ".join(statement.lower().split())
        if key:
            norm.setdefault(key, []).append(memory)
        if not _value(memory, "source_seeds", []):
            mid = _value(memory, "id", "")
            flags.append(DreamCandidate("semantic_flag", (mid,), {"flag": "no_source", "id": mid}, "semantic memory has no source", {"valid": True}))
    for key, group in norm.items():
        if len(group) > 1:
            ids = tuple(_value(m, "id", "") for m in group)
            flags.append(DreamCandidate("semantic_flag", ids, {"flag": "duplicate", "ids": ids, "statement": key}, "normalised statements repeat", {"valid": True}))
    return DreamReceipt("semantic_review", "ok", flags=tuple(flags))


def organisation_diagnostics(seeds: Sequence[Any], memories: Sequence[Any] = ()) -> DreamReceipt:
    zero_edges = sum(not _value(s, "caused_by", []) and not _value(s, "led_to", []) for s in seeds)
    no_reader = sum(not _value(s, "access_count", 0) for s in seeds)
    claims = [str(_value(m, "statement", "")).strip().lower() for m in memories]
    counts: dict[str, int] = {}
    for claim in claims:
        if claim: counts[claim] = counts.get(claim, 0) + 1
    return DreamReceipt("organisation_diagnostics", "ok", diagnostics={
        "seed_count": len(seeds), "no_reader_seeds": no_reader,
        "zero_edge_seeds": zero_edges, "duplicate_claim_clusters": sum(v > 1 for v in counts.values()),
        "expired_claims": 0,  # expiry policy is intentionally not guessed in Stage 0
    })


def should_dream(*, in_window: bool, new_seed_count: int, backlog: int,
                 threshold: int = 3, backlog_limit: int = 20) -> tuple[bool, str]:
    if not in_window: return False, "outside_window"
    if new_seed_count < threshold: return False, "dream_skipped_empty"
    if backlog >= backlog_limit: return False, "backlog_full"
    return True, "eligible"


def store_candidates(conn: sqlite3.Connection, receipt: DreamReceipt, *, max_failures: int = 3, max_successes: int = 3) -> int:
    """Persist receipt candidates only; active seeds/semantic/edges are untouched."""
    ensure_candidate_table(conn)
    stored = 0
    for candidate in (*receipt.candidates, *receipt.flags):
        record = candidate.as_record()
        cid = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        conn.execute("INSERT OR IGNORE INTO dream_candidates VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                     (cid, candidate.kind, json.dumps(record["source_ids"]), json.dumps(record["payload"], ensure_ascii=False), candidate.reason, json.dumps(record["validation"]), candidate.created_at))
        stored += 1
    conn.commit()
    return stored
