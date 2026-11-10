"""Quarantine cascade: when a dirty summary/seed is quarantined, propagate
untrusted status to derived artefacts (seed queue sessions, claims).

Design constraints (P0 observe period until 2026-11-14):
  - SOFT-OFFLINE ONLY: marks seeds with is_quarantined=1 to exclude from
    recall. Hard DELETE is strictly forbidden — it breaks primary-key
    sequences, foreign-key chains, and leaves evidence_id dangling.
  - dry_run=True (default) previews cascade targets without writing.
  - Never raises: all failures are logged to audit_log and swallowed.
  - Only touches: quarantines, claims (confidence downgrade), and the
    seeds table (is_quarantined flag when the column exists).
  - Receipts and evidence are immutable by schema trigger and are never
    touched here.

The is_quarantined column may not yet exist in older seeds.db schemas.
When absent, cascade_quarantine silently skips the soft-offline step and
records the skip in audit_log. Callers may run ``ensure_quarantine_column``
to add the column before first use (idempotent ALTER TABLE).
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional


def ensure_quarantine_column(seeds_conn) -> bool:
    """Add is_quarantined INTEGER column to seeds if absent (idempotent).

    Returns True if the column was added, False if it already existed.
    """
    cols = {row[1] for row in seeds_conn.execute("PRAGMA table_info(seeds)")}
    if "is_quarantined" in cols:
        return False
    seeds_conn.execute("ALTER TABLE seeds ADD COLUMN is_quarantined INTEGER NOT NULL DEFAULT 0")
    seeds_conn.commit()
    return True


def _quarantine_subject(
    conn,
    subject_type: str,
    subject_id: str,
    reason: str,
    expires_hours: float = 72.0,
) -> str:
    """Insert or ignore a quarantine row; return the new quarantine id."""
    qid = uuid.uuid4().hex
    now = time.time()
    expires_at = now + expires_hours * 3600
    conn.execute(
        "INSERT OR IGNORE INTO quarantines"
        "(id, subject_type, subject_id, reason, status, expires_at, review_count, created_at)"
        " VALUES (?, ?, ?, ?, 'pending_review', ?, 0, ?)",
        (qid, subject_type, subject_id, reason, expires_at, now),
    )
    return qid


def _derived_seed_session_ids(conn, run_id: str) -> List[str]:
    """Find seed queue session IDs created from this run via seed_queue events."""
    rows = conn.execute(
        "SELECT summary FROM events WHERE run_id=? AND kind='seed_queue'",
        (run_id,),
    ).fetchall()
    ids: List[str] = []
    session_key = "session_autonomy_" + run_id
    for row in rows:
        if session_key in (row[0] or ""):
            ids.append(session_key)
            break
    return ids


def _derived_claim_ids(conn, run_id: str) -> List[str]:
    """Find claims whose provenance_json references this run_id."""
    ids: List[str] = []
    try:
        rows = conn.execute("SELECT id, provenance_json FROM claims").fetchall()
    except Exception:
        return ids
    for row in rows:
        prov = row[1] or "{}"
        try:
            obj = json.loads(prov)
        except (ValueError, TypeError):
            obj = {}
        if run_id in str(obj):
            ids.append(row[0])
    return ids


def cascade_quarantine(
    conn,
    run_id: str,
    error_kind: str,
    reason: str,
    *,
    dry_run: bool = True,
    seeds_conn=None,
    audit_log: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Cascade quarantine from a dirty run to derived artefacts.

    Parameters
    ----------
    conn:
        Open sqlite3.Connection to autonomy.db.
    run_id:
        The run whose output was classified as dirty.
    error_kind:
        The error classification that triggered quarantine (E1-E4 or T2 label).
    reason:
        Human-readable quarantine reason string.
    dry_run:
        When True (default), preview only — no rows are written.
    seeds_conn:
        Optional open sqlite3.Connection to seeds.db. When provided and not
        dry_run, sets is_quarantined=1 on seeds whose caused_by/session traces
        back to this run. Soft-offline only — no DELETE.
    audit_log:
        Optional list to append audit entries to.

    Returns
    -------
    dict with keys:
        run_quarantined  — bool (or would-be for dry_run)
        seed_session_ids — list of seed session ids found
        claim_ids        — list of claim ids found
        seeds_soft_offlined — count of seeds marked is_quarantined=1
        dry_run          — echoed
        error            — str if an unexpected error occurred
    """
    if audit_log is None:
        audit_log = []

    result: Dict[str, Any] = {
        "run_quarantined": False,
        "seed_session_ids": [],
        "claim_ids": [],
        "seeds_soft_offlined": 0,
        "dry_run": dry_run,
    }

    try:
        # 1. Quarantine the run itself.
        result["run_quarantined"] = True
        if not dry_run:
            _quarantine_subject(conn, "run", run_id, reason)
        audit_log.append({
            "action": "quarantine_run",
            "run_id": run_id,
            "error_kind": error_kind,
            "dry_run": dry_run,
        })

        # 2. Find and quarantine derived seed queue sessions.
        try:
            seed_ids = _derived_seed_session_ids(conn, run_id)
            result["seed_session_ids"] = seed_ids
            for sid in seed_ids:
                audit_log.append({
                    "action": "quarantine_seed_session",
                    "subject_id": sid,
                    "run_id": run_id,
                    "dry_run": dry_run,
                })
                if not dry_run:
                    _quarantine_subject(
                        conn, "seed_session", sid,
                        "derived_from_dirty_run:%s" % error_kind,
                    )
        except Exception as exc:
            audit_log.append({"action": "seed_lookup_error", "error": str(exc)[:200]})

        # 3. Soft-offline seeds in seeds.db (is_quarantined=1, no DELETE).
        if seeds_conn is not None:
            try:
                cols = {row[1] for row in seeds_conn.execute("PRAGMA table_info(seeds)")}
                if "is_quarantined" not in cols:
                    audit_log.append({"action": "seeds_soft_offline_skip", "reason": "column_absent"})
                else:
                    # Seeds whose caused_by or led_to encodes this run_id.
                    # We use a text-match since the linkage is a JSON/text field.
                    rows = seeds_conn.execute(
                        "SELECT id FROM seeds WHERE caused_by LIKE ? OR led_to LIKE ?",
                        ("%" + run_id + "%", "%" + run_id + "%"),
                    ).fetchall()
                    seed_row_ids = [r[0] for r in rows]
                    result["seeds_soft_offlined"] = len(seed_row_ids)
                    for sid in seed_row_ids:
                        audit_log.append({
                            "action": "seeds_soft_offline",
                            "seed_id": sid,
                            "dry_run": dry_run,
                        })
                    if not dry_run and seed_row_ids:
                        placeholders = ",".join("?" * len(seed_row_ids))
                        seeds_conn.execute(
                            "UPDATE seeds SET is_quarantined=1 WHERE id IN (%s)" % placeholders,
                            seed_row_ids,
                        )
                        seeds_conn.commit()
            except Exception as exc:
                audit_log.append({"action": "seeds_soft_offline_error", "error": str(exc)[:200]})

        # 4. Find and downgrade-confidence derived claims (soft untrust).
        try:
            claim_ids = _derived_claim_ids(conn, run_id)
            result["claim_ids"] = claim_ids
            for cid in claim_ids:
                audit_log.append({
                    "action": "untrust_claim",
                    "claim_id": cid,
                    "run_id": run_id,
                    "dry_run": dry_run,
                })
                if not dry_run:
                    # Downgrade confidence without touching immutable fields.
                    # protected=1 claims are never touched.
                    conn.execute(
                        "UPDATE claims SET confidence=MIN(confidence,0.3), status='cold'"
                        " WHERE id=? AND protected=0",
                        (cid,),
                    )
        except Exception as exc:
            audit_log.append({"action": "claim_lookup_error", "error": str(exc)[:200]})

        if not dry_run:
            conn.commit()

    except Exception as exc:
        result["error"] = str(exc)[:400]
        audit_log.append({"action": "cascade_error", "error": str(exc)[:200]})

    return result
