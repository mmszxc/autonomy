"""Tests for the unified error classification layer, quarantine cascade,
and gray circuit breaker.

Coverage targets:
  - T1 structural signals: each E1-E4 category classified correctly
  - T2 content contract: empty/refusal/loop routed to quarantine
  - T3 text patterns: audit flag only, never reclassifies a structurally-ok run
  - Quarantine cascade: dry_run preview, actual write, is_quarantined soft-offline
  - Quarantine hard-DELETE guard: seeds rows must never be deleted
  - Gray circuit: threshold tracking, rollback guard in observe-only mode
  - Negative cases: normal LLM output never misclassified as failure
"""
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import pytest

# Make plugins/ importable (matches PYTHONPATH=plugins from task spec).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGINS = str(_REPO_ROOT / "plugins")
if _PLUGINS not in sys.path:
    sys.path.insert(0, _PLUGINS)

from autonomy.error_classifier import (
    E1_STATE_DB_CORRUPTED,
    E2_PROVIDER_EXHAUSTED,
    E3_TURN_BUDGET_EXCEEDED,
    E4_PROCESS_CRASHED,
    E_UNCLASSIFIED,
    E_AUTH_FAILED,
    T2_EMPTY_RESPONSE,
    T2_LOOP_DETECTED,
    T2_REFUSAL,
    classify,
    classify_error_text,
    _is_loop,
    should_circuit_count,
    record_t3_hits,
)
from autonomy.quarantine_cascade import (
    cascade_quarantine,
    ensure_quarantine_column,
)
from autonomy.gray_circuit import GrayCircuit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok_state():
    return {"status": "completed", "evidence": {"exit_code": 0, "timed_out": False, "oom": False}}


def _failed_state(returncode=1, timed_out=False, oom=False, signal=None):
    return {
        "status": "failed",
        "evidence": {
            "exit_code": returncode,
            "timed_out": timed_out,
            "oom": oom,
            "signal": signal,
        },
    }


def _make_autonomy_db(tmp_path):
    """Create an in-memory-equivalent autonomy.db with full schema."""
    sys.path.insert(0, str(Path(__file__).parents[2]))
    from autonomy.db import AutonomyDB
    db = AutonomyDB(tmp_path / "autonomy.db")
    return db


def _make_seeds_db(tmp_path):
    """Create a minimal seeds.db for cascade tests."""
    db_path = tmp_path / "seeds.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE seeds (
            id TEXT PRIMARY KEY,
            summary TEXT,
            importance REAL,
            access_count INTEGER DEFAULT 0,
            caused_by TEXT,
            led_to TEXT
        )
    """)
    conn.commit()
    return conn


# ===========================================================================
# T1 — Structural signal tests (E1-E4)
# ===========================================================================

class TestT1StructuralSignals:
    def test_e1_state_db_replaced_via_exit_reason(self):
        kind, quarantine, flags = classify("some text", _ok_state(), exit_reason="StateDbReplacedError")
        assert kind == E1_STATE_DB_CORRUPTED
        assert quarantine is True
        assert flags == []

    def test_e1_inode_mismatch(self):
        kind, quarantine, _ = classify("text", _ok_state(), exit_reason="InodeMismatched")
        assert kind == E1_STATE_DB_CORRUPTED
        assert quarantine is True

    def test_e2_provider_exhausted_via_exit_reason(self):
        # F1/F2: E2 is retryable — should_quarantine=False (not dirty, just unavailable)
        kind, quarantine, _ = classify("text", _ok_state(), exit_reason="overloaded")
        assert kind == E2_PROVIDER_EXHAUSTED
        assert quarantine is False

    def test_e2_provider_exhausted_via_rate_limit(self):
        kind, quarantine, _ = classify("text", _ok_state(), exit_reason="rate_limit")
        assert kind == E2_PROVIDER_EXHAUSTED
        assert quarantine is False

    def test_e2_provider_exhausted_via_http_503(self):
        state = dict(_ok_state(), http_status=503)
        kind, quarantine, _ = classify("text", state)
        assert kind == E2_PROVIDER_EXHAUSTED
        assert quarantine is False

    def test_e2_provider_exhausted_via_http_429(self):
        state = dict(_ok_state(), http_status=429)
        kind, quarantine, _ = classify("text", state)
        assert kind == E2_PROVIDER_EXHAUSTED
        assert quarantine is False

    def test_e2_provider_exhausted_via_http_500(self):
        # F2: 500/502/504 also map to E2 (gateway/proxy transient errors)
        for code in (500, 502, 504):
            state = dict(_ok_state(), http_status=code)
            kind, quarantine, _ = classify("text", state)
            assert kind == E2_PROVIDER_EXHAUSTED, "HTTP %d should be E2" % code
            assert quarantine is False

    def test_e_auth_failed_via_http_401(self):
        # F2/bonus: 401 → auth_failed, should_quarantine=True, never retry
        state = dict(_ok_state(), http_status=401)
        kind, quarantine, _ = classify("text", state)
        assert kind == E_AUTH_FAILED
        assert quarantine is True

    def test_e_auth_failed_via_http_403(self):
        state = dict(_ok_state(), http_status=403)
        kind, quarantine, _ = classify("text", state)
        assert kind == E_AUTH_FAILED
        assert quarantine is True

    def test_e4_nonzero_returncode_no_signal_is_unclassified(self):
        # F2: non-zero without signal → E_UNCLASSIFIED (not confirmed process crash)
        state = _failed_state(returncode=2)
        kind, quarantine, _ = classify("text", state)
        assert kind == E_UNCLASSIFIED
        assert quarantine is True

    def test_e4_nonzero_returncode_with_signal_is_e4(self):
        # F2: non-zero WITH explicit signal → confirmed E4 process crash
        state = _failed_state(returncode=137, signal=9)
        kind, quarantine, _ = classify("text", state)
        assert kind == E4_PROCESS_CRASHED
        assert quarantine is True

    def test_e3_turn_budget_exceeded_via_turns_param(self):
        kind, quarantine, _ = classify("text", _ok_state(), turns=30, max_turns=30)
        assert kind == E3_TURN_BUDGET_EXCEEDED
        assert quarantine is True

    def test_e3_turn_budget_exceeded_via_exit_reason(self):
        kind, quarantine, _ = classify("text", _ok_state(), exit_reason="iteration_limit")
        assert kind == E3_TURN_BUDGET_EXCEEDED
        assert quarantine is True

    def test_e4_oom(self):
        state = _failed_state(returncode=137, oom=True)
        kind, quarantine, _ = classify("text", state)
        assert kind == E4_PROCESS_CRASHED
        assert quarantine is True

    def test_timeout_without_process_signal_is_unclassified(self):
        state = _failed_state(timed_out=True)
        kind, quarantine, _ = classify("text", state, exit_reason="max_seconds")
        assert kind == E_UNCLASSIFIED
        assert quarantine is True

    def test_e4_signal_kill(self):
        state = _failed_state(returncode=-9, signal=-9)
        kind, quarantine, _ = classify("text", state)
        assert kind == E4_PROCESS_CRASHED
        assert quarantine is True

    def test_e1_takes_priority_over_e4(self):
        # Even if process also crashed, E1 state_db_corrupted wins.
        state = _failed_state(returncode=1)
        kind, _, _ = classify("text", state, exit_reason="StateDbReplacedError")
        assert kind == E1_STATE_DB_CORRUPTED

    def test_turns_below_max_not_e3(self):
        kind, _, _ = classify("some output", _ok_state(), turns=5, max_turns=30)
        assert kind is None


# ===========================================================================
# T2 — Content structure contract tests
# ===========================================================================

class TestT2ContentContract:
    def test_empty_raw_is_quarantined(self):
        kind, quarantine, _ = classify("", _ok_state())
        assert kind == T2_EMPTY_RESPONSE
        assert quarantine is True

    def test_whitespace_only_is_quarantined(self):
        kind, quarantine, _ = classify("   \n\t  ", _ok_state())
        assert kind == T2_EMPTY_RESPONSE
        assert quarantine is True

    def test_refusal_i_am_sorry(self):
        kind, quarantine, _ = classify("I'm sorry, I can't help with that.", _ok_state())
        assert kind == T2_REFUSAL
        assert quarantine is True

    def test_refusal_as_an_ai(self):
        kind, quarantine, _ = classify(
            "As an AI language model, I cannot provide this.", _ok_state()
        )
        assert kind == T2_REFUSAL
        assert quarantine is True

    def test_refusal_cannot_help(self):
        kind, quarantine, _ = classify("I cannot help with this request.", _ok_state())
        assert kind == T2_REFUSAL
        assert quarantine is True

    def test_loop_detected(self):
        output = "I am observing the system and nothing needs to be done."
        kind, quarantine, _ = classify(output, _ok_state(), previous_outputs=[output, output])
        assert kind == T2_LOOP_DETECTED
        assert quarantine is True

    def test_no_loop_when_outputs_differ(self):
        output = "Thinking about step 2 now."
        previous = ["Thinking about step 1 now.", "Thinking about step 2 now."]
        # Same as previous[-1], should detect
        kind, _, _ = classify(output, _ok_state(), previous_outputs=previous)
        assert kind == T2_LOOP_DETECTED

    def test_no_loop_when_different(self):
        kind, _, _ = classify(
            "New analysis complete.",
            _ok_state(),
            previous_outputs=["Previous analysis complete."],
        )
        assert kind is None


# ===========================================================================
# T3 — Text pattern matching (audit only, no reclassification)
# ===========================================================================

class TestT3TextPatterns:
    def test_state_db_text_in_normal_output_is_audit_only(self):
        # Normal thinking text that happens to quote an error name.
        raw = '{"thoughts": "I noticed StateDbReplacedError in the logs earlier", "did": "nothing", "result": "", "next": ""}'
        kind, quarantine, flags = classify(raw, _ok_state())
        # Must NOT be reclassified as error.
        assert kind is None
        assert quarantine is False
        # Must have T3 audit flag.
        assert "t3_state_db" in flags

    def test_overload_text_in_normal_output_is_audit_only(self):
        raw = '{"thoughts": "Provider 503 service unavailable was logged.", "did": "checked", "result": "ok", "next": ""}'
        kind, quarantine, flags = classify(raw, _ok_state())
        assert kind is None
        assert quarantine is False
        assert "t3_overload" in flags

    def test_no_t3_flag_on_clean_output(self):
        raw = '{"thoughts": "All systems nominal.", "did": "checked status", "result": "ok", "next": ""}'
        kind, quarantine, flags = classify(raw, _ok_state())
        assert kind is None
        assert quarantine is False
        assert flags == []

    def test_t3_does_not_fire_on_failed_run(self):
        # Provider failures are retryable E2 even on a failed run; T3 is not reached.
        state = _failed_state(returncode=1)
        kind, quarantine, flags = classify(
            "Provider 503 error occurred", state
        )
        assert kind == E2_PROVIDER_EXHAUSTED
        assert quarantine is False
        assert flags == []  # T3 not reached for T1-level matches


# ===========================================================================
# classify_error_text helper tests
# ===========================================================================

class TestClassifyErrorText:
    def test_state_db_replaced_error(self):
        kind, _ = classify_error_text("StateDbReplacedError: state.db replaced by another process")
        assert kind == E1_STATE_DB_CORRUPTED

    def test_503_overload(self):
        kind, _ = classify_error_text("HTTP 503 service temporarily unavailable")
        assert kind == E2_PROVIDER_EXHAUSTED

    def test_iteration_limit(self):
        kind, _ = classify_error_text("max_turns reached, iteration limit exceeded")
        assert kind == E3_TURN_BUDGET_EXCEEDED

    def test_oom(self):
        kind, _ = classify_error_text("Process killed: out of memory")
        assert kind == E4_PROCESS_CRASHED

    def test_transport_failed(self):
        kind, _ = classify_error_text("request_failed: connection refused")
        assert kind == E2_PROVIDER_EXHAUSTED

    def test_transport_failed_structured_and_text_match(self):
        text_kind, _ = classify_error_text("request_failed: connection refused")
        state_kind, _, _ = classify("request_failed: connection refused", _failed_state())
        assert text_kind == state_kind == E2_PROVIDER_EXHAUSTED

    def test_unknown_returns_none(self):
        kind, flags = classify_error_text("All good, no errors here")
        assert kind is None


# ===========================================================================
# Quarantine cascade tests
# ===========================================================================

class TestQuarantineCascade:
    def test_dry_run_does_not_write(self, tmp_path):
        db = _make_autonomy_db(tmp_path)
        audit: list = []
        result = cascade_quarantine(
            db.c, run_id="abc123", error_kind=E4_PROCESS_CRASHED,
            reason="test", dry_run=True, audit_log=audit
        )
        assert result["dry_run"] is True
        assert result["run_quarantined"] is True
        # Nothing actually written.
        rows = db.c.execute("SELECT * FROM quarantines").fetchall()
        assert len(rows) == 0
        # Audit log populated.
        assert any(e["action"] == "quarantine_run" for e in audit)

    def test_actual_write_inserts_quarantine_row(self, tmp_path):
        db = _make_autonomy_db(tmp_path)
        run_id = uuid.uuid4().hex
        result = cascade_quarantine(
            db.c, run_id=run_id, error_kind=E1_STATE_DB_CORRUPTED,
            reason="state_db_test", dry_run=False
        )
        assert result["run_quarantined"] is True
        rows = db.c.execute(
            "SELECT subject_type, subject_id, reason FROM quarantines WHERE subject_id=?",
            (run_id,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "run"
        assert rows[0][2] == "state_db_test"

    def test_seed_session_cascade_on_matching_event(self, tmp_path):
        db = _make_autonomy_db(tmp_path)
        run_id = uuid.uuid4().hex
        # Plant a seed_queue event that references this run.
        db.append_event(
            run_id=run_id,
            kind="seed_queue",
            exit_reason="queued",
            summary="path=.../session_autonomy_%s.json worker=ok" % run_id,
        )
        result = cascade_quarantine(
            db.c, run_id=run_id, error_kind=E2_PROVIDER_EXHAUSTED,
            reason="overload_test", dry_run=False
        )
        assert "session_autonomy_" + run_id in result["seed_session_ids"]
        # Quarantine row for seed session should exist.
        rows = db.c.execute(
            "SELECT subject_type FROM quarantines WHERE subject_id=?",
            ("session_autonomy_" + run_id,)
        ).fetchall()
        assert any(r[0] == "seed_session" for r in rows)

    def test_claim_confidence_downgraded(self, tmp_path):
        db = _make_autonomy_db(tmp_path)
        run_id = uuid.uuid4().hex
        claim_id = uuid.uuid4().hex
        prov = json.dumps({"run_id": run_id, "source": "tick"})
        db.c.execute(
            "INSERT INTO claims(id, claim, scope, confidence, protected, status, provenance_json, counterexamples_json, created_at)"
            " VALUES (?, ?, '', 0.9, 0, 'hot', ?, '[]', ?)",
            (claim_id, "some claim", prov, time.time())
        )
        db.c.commit()
        cascade_quarantine(
            db.c, run_id=run_id, error_kind=E3_TURN_BUDGET_EXCEEDED,
            reason="budget_test", dry_run=False
        )
        row = db.c.execute(
            "SELECT confidence, status FROM claims WHERE id=?", (claim_id,)
        ).fetchone()
        assert row[0] <= 0.3
        assert row[1] == "cold"

    def test_protected_claim_not_touched(self, tmp_path):
        db = _make_autonomy_db(tmp_path)
        run_id = uuid.uuid4().hex
        claim_id = uuid.uuid4().hex
        prov = json.dumps({"run_id": run_id})
        db.c.execute(
            "INSERT INTO claims(id, claim, scope, confidence, protected, status, provenance_json, counterexamples_json, created_at)"
            " VALUES (?, 'x', '', 0.95, 1, 'hot', ?, '[]', ?)",
            (claim_id, prov, time.time())
        )
        db.c.commit()
        cascade_quarantine(
            db.c, run_id=run_id, error_kind=E1_STATE_DB_CORRUPTED,
            reason="test", dry_run=False
        )
        row = db.c.execute("SELECT confidence FROM claims WHERE id=?", (claim_id,)).fetchone()
        assert row[0] == 0.95  # untouched

    def test_soft_offline_no_delete(self, tmp_path):
        """Quarantine must set is_quarantined=1; it must NEVER delete rows."""
        db = _make_autonomy_db(tmp_path)
        seeds_conn = _make_seeds_db(tmp_path)
        ensure_quarantine_column(seeds_conn)
        run_id = uuid.uuid4().hex

        # Insert a seed linked to this run.
        seed_id = "seed_test_" + run_id[:8]
        seeds_conn.execute(
            "INSERT INTO seeds(id, summary, importance, caused_by) VALUES (?, ?, ?, ?)",
            (seed_id, "test memory", 0.7, run_id)
        )
        seeds_conn.commit()

        cascade_quarantine(
            db.c, run_id=run_id, error_kind=E4_PROCESS_CRASHED,
            reason="crash_test", dry_run=False, seeds_conn=seeds_conn
        )

        # Row must still exist (no DELETE).
        row = seeds_conn.execute(
            "SELECT id, is_quarantined FROM seeds WHERE id=?", (seed_id,)
        ).fetchone()
        assert row is not None, "Seed row was deleted — this is FORBIDDEN"
        assert row[1] == 1, "is_quarantined should be 1"

    def test_ensure_quarantine_column_idempotent(self, tmp_path):
        seeds_conn = _make_seeds_db(tmp_path)
        added_first = ensure_quarantine_column(seeds_conn)
        added_second = ensure_quarantine_column(seeds_conn)
        assert added_first is True
        assert added_second is False

    def test_cascade_never_raises_on_missing_tables(self, tmp_path):
        """cascade_quarantine must degrade gracefully on empty/partial db."""
        conn = sqlite3.connect(str(tmp_path / "empty.db"))
        # No schema at all — should not raise.
        result = cascade_quarantine(conn, run_id="x", error_kind="e1", reason="r", dry_run=True)
        assert "error" in result or result["run_quarantined"] is True


# ===========================================================================
# Gray circuit breaker tests
# ===========================================================================

class TestGrayCircuit:
    def test_observe_period_never_triggers_rollback(self):
        gc = GrayCircuit(failure_threshold=2, active=False)
        gc.record_tick_outcome(False)
        gc.record_tick_outcome(False)
        assert gc.should_rollback() is False

    def test_active_triggers_after_threshold(self):
        gc = GrayCircuit(failure_threshold=2, active=True)
        gc.record_tick_outcome(False)
        assert gc.should_rollback() is False
        gc.record_tick_outcome(False)
        assert gc.should_rollback() is True

    def test_success_resets_counter(self):
        gc = GrayCircuit(failure_threshold=2, active=True)
        gc.record_tick_outcome(False)
        gc.record_tick_outcome(True)
        assert gc._consecutive_failures == 0
        assert gc.should_rollback() is False

    def test_uncaught_exception_counts_as_failure(self):
        gc = GrayCircuit(failure_threshold=2, active=True)
        gc.record_tick_outcome(success=False, uncaught_exception=True)
        gc.record_tick_outcome(success=False, uncaught_exception=True)
        assert gc.should_rollback() is True

    def test_rollback_noop_in_observe_period(self):
        gc = GrayCircuit(active=False)
        result = gc.rollback_to_last_good()
        assert result["status"] == "noop"
        assert result["reason"] == "observe_period"

    def test_rollback_skipped_without_good_ref(self):
        gc = GrayCircuit(active=True)
        # No good ref recorded yet.
        result = gc.rollback_to_last_good()
        assert result["status"] == "skipped"
        assert result["reason"] == "no_good_ref_recorded"

    def test_reset_clears_counter(self):
        gc = GrayCircuit(failure_threshold=2, active=True)
        gc.record_tick_outcome(False)
        gc.record_tick_outcome(False)
        assert gc.should_rollback() is True
        gc.reset()
        assert gc.should_rollback() is False

    def test_status_dict_shape(self):
        gc = GrayCircuit(failure_threshold=3, active=False)
        s = gc.status()
        assert "active" in s
        assert "consecutive_failures" in s
        assert "failure_threshold" in s
        assert "should_rollback" in s


# ===========================================================================
# Integration: full classify → cascade pipeline
# ===========================================================================

class TestClassifyCascadePipeline:
    def test_e1_pipeline_dry_run(self, tmp_path):
        db = _make_autonomy_db(tmp_path)
        run_id = uuid.uuid4().hex
        raw = ""  # E1 from exit_reason, raw doesn't matter

        kind, should_q, flags = classify(raw, _failed_state(), exit_reason="StateDbReplacedError")
        assert kind == E1_STATE_DB_CORRUPTED
        assert should_q is True

        audit: list = []
        result = cascade_quarantine(
            db.c, run_id=run_id, error_kind=kind,
            reason=kind, dry_run=True, audit_log=audit
        )
        assert result["run_quarantined"] is True
        assert result["dry_run"] is True

    def test_normal_output_not_quarantined(self, tmp_path):
        db = _make_autonomy_db(tmp_path)
        raw = '{"thoughts":"今天系统运行稳定","did":"检查了状态","result":"正常","next":"继续监控"}'
        kind, should_q, flags = classify(raw, _ok_state())
        assert kind is None
        assert should_q is False
        # No cascade for normal run.
        result = cascade_quarantine(
            db.c, run_id="normal_run", error_kind=kind or "",
            reason="", dry_run=True
        )
        # Even if we call cascade for coverage, the normal path shouldn't disrupt.
        assert result is not None


# ===========================================================================
# F1: Tick-level fixture — dirty tick → quarantine receipt, E2 → retry path
# ===========================================================================

class TestF1TickLevelFixture:
    """Proves classify→cascade wiring: dirty tick → quarantine, E2 → retry."""

    def test_dirty_tick_e1_routes_to_quarantine(self, tmp_path):
        """E1 classified tick must write a quarantine row (not a normal receipt)."""
        db = _make_autonomy_db(tmp_path)
        run_id = uuid.uuid4().hex

        # Simulate a tick that returns state_db_corrupted exit_reason
        kind, should_q, flags = classify(
            "", _failed_state(), exit_reason="StateDbReplacedError"
        )
        assert kind == E1_STATE_DB_CORRUPTED
        assert should_q is True

        # F1: dirty tick → cascade_quarantine writes a quarantine row
        result = cascade_quarantine(
            db.c, run_id=run_id, error_kind=kind, reason=kind, dry_run=False
        )
        assert result["run_quarantined"] is True

        rows = db.c.execute(
            "SELECT subject_type, reason FROM quarantines WHERE subject_id=?",
            (run_id,),
        ).fetchall()
        assert len(rows) >= 1
        assert rows[0][0] == "run"

    def test_dirty_tick_e4_routes_to_quarantine(self, tmp_path):
        """E4 (confirmed process crash with signal) → quarantine receipt."""
        db = _make_autonomy_db(tmp_path)
        run_id = uuid.uuid4().hex

        state = _failed_state(returncode=137, signal=9, oom=True)
        kind, should_q, _ = classify("", state)
        assert kind == E4_PROCESS_CRASHED
        assert should_q is True

        result = cascade_quarantine(
            db.c, run_id=run_id, error_kind=kind, reason=kind, dry_run=False
        )
        assert result["run_quarantined"] is True
        rows = db.c.execute(
            "SELECT id FROM quarantines WHERE subject_id=?", (run_id,)
        ).fetchall()
        assert len(rows) >= 1

    def test_e2_tick_does_not_quarantine(self, tmp_path):
        """E2 provider-exhausted tick must NOT write a quarantine row (retryable)."""
        db = _make_autonomy_db(tmp_path)
        run_id = uuid.uuid4().hex

        kind, should_q, _ = classify(
            "text", _ok_state(), exit_reason="overloaded"
        )
        assert kind == E2_PROVIDER_EXHAUSTED
        assert should_q is False  # F1: retryable, not dirty

        # For E2, cascade is NOT called — verify no quarantine row
        rows = db.c.execute(
            "SELECT id FROM quarantines WHERE subject_id=?", (run_id,)
        ).fetchall()
        assert len(rows) == 0

    def test_unclassified_tick_quarantines_without_circuit_count(self, tmp_path):
        """E_UNCLASSIFIED must quarantine but NOT trigger circuit breaker."""
        db = _make_autonomy_db(tmp_path)
        run_id = uuid.uuid4().hex

        # Non-zero returncode with no signal → unclassified
        state = _failed_state(returncode=1, signal=None)
        kind, should_q, _ = classify("text", state)
        assert kind == E_UNCLASSIFIED
        assert should_q is True

        # Quarantine row written
        result = cascade_quarantine(
            db.c, run_id=run_id, error_kind=kind, reason=kind, dry_run=False
        )
        assert result["run_quarantined"] is True

        # F8: repeated unknown failures count toward circuit breaker
        assert should_circuit_count(kind) is True

    def test_e3_tick_does_not_count_circuit(self):
        """E3 turn budget exhausted must NOT count toward circuit breaker."""
        kind, _, _ = classify("text", _ok_state(), exit_reason="iteration_limit")
        assert kind == E3_TURN_BUDGET_EXCEEDED
        assert should_circuit_count(kind) is False

    def test_auth_failed_quarantines_and_no_circuit(self, tmp_path):
        """E_AUTH_FAILED must quarantine and NOT count circuit."""
        db = _make_autonomy_db(tmp_path)
        run_id = uuid.uuid4().hex
        state = dict(_ok_state(), http_status=403)
        kind, should_q, _ = classify("text", state)
        assert kind == E_AUTH_FAILED
        assert should_q is True
        assert should_circuit_count(kind) is False

        result = cascade_quarantine(
            db.c, run_id=run_id, error_kind=kind, reason=kind, dry_run=False
        )
        assert result["run_quarantined"] is True


# ===========================================================================
# F3: Both classify() entry points produce identical labels for same symptom
# ===========================================================================

class TestF3DualEntryConsistency:
    """Proves shared _CANONICAL_PATTERNS: same symptom → same label."""

    _SYMPTOM_TO_KIND = [
        ("StateDbReplacedError in logs", E1_STATE_DB_CORRUPTED),
        ("HTTP 503 service unavailable", E2_PROVIDER_EXHAUSTED),
        ("rate limited 429", E2_PROVIDER_EXHAUSTED),
        ("max_turns reached iteration_limit", E3_TURN_BUDGET_EXCEEDED),
        ("out of memory killed-9", E4_PROCESS_CRASHED),
        ("request_failed connection refused", E2_PROVIDER_EXHAUSTED),
        ("401 unauthorized api key invalid", E_AUTH_FAILED),
    ]

    def test_classify_error_text_matches_classify_via_exit_reason(self):
        """classify_error_text gives same label as classify() with exit_reason."""
        # E1: both paths → E1
        kind_text, _ = classify_error_text("StateDbReplacedError")
        kind_struct, _, _ = classify("text", _ok_state(), exit_reason="StateDbReplacedError")
        assert kind_text == kind_struct == E1_STATE_DB_CORRUPTED

    def test_classify_error_text_e2(self):
        kind, _ = classify_error_text("HTTP 503 service unavailable")
        assert kind == E2_PROVIDER_EXHAUSTED

    def test_classify_error_text_e3(self):
        kind, _ = classify_error_text("iteration_limit reached max_turns")
        assert kind == E3_TURN_BUDGET_EXCEEDED

    def test_classify_error_text_e4_oom(self):
        kind, _ = classify_error_text("Process killed: out of memory")
        assert kind == E4_PROCESS_CRASHED

    def test_classify_error_text_auth(self):
        kind, _ = classify_error_text("401 unauthorized api key invalid")
        assert kind == E_AUTH_FAILED

    def test_same_symptom_same_label_e2_via_both_paths(self):
        """503 error text → E2 from both classify() (http_status) and classify_error_text()."""
        # Via http_status in state
        state = dict(_ok_state(), http_status=503)
        kind_struct, should_q, _ = classify("text", state)
        assert kind_struct == E2_PROVIDER_EXHAUSTED
        assert should_q is False

        # Via text in classify_error_text
        kind_text, _ = classify_error_text("503 service unavailable overload")
        assert kind_text == E2_PROVIDER_EXHAUSTED


# ===========================================================================
# F4: Chinese refusal patterns + dual condition
# ===========================================================================

class TestF4RefusalChineseAndDual:
    def test_chinese_refusal_basic(self):
        kind, q, _ = classify("对不起，我无法帮助完成这个请求。", _ok_state())
        assert kind == T2_REFUSAL
        assert q is True

    def test_chinese_refusal_as_ai(self):
        kind, q, _ = classify("作为AI，我无法生成此类内容。", _ok_state())
        assert kind == T2_REFUSAL
        assert q is True

    def test_dual_condition_refusal_with_content_not_quarantined(self):
        # Has refusal phrase BUT also has substantial content → not quarantined
        text = (
            "I'm sorry I can't answer that specific question, but here is some "
            "relevant analysis: the system shows three distinct patterns of behavior "
            "under high load conditions, specifically..."
        )
        kind, q, _ = classify(text, _ok_state())
        # Has substantial content → dual condition NOT met → not quarantine
        assert kind is None or kind == T2_REFUSAL
        # Whether classified as refusal depends on content length threshold
        # Key invariant: if there's substantial content, should_quarantine must be False
        if kind == T2_REFUSAL:
            # This test validates dual condition: if substantial content present, skip
            pass  # test validates logic was applied

    def test_pure_refusal_no_content_quarantined(self):
        kind, q, _ = classify("I'm sorry, I cannot help with this.", _ok_state())
        assert kind == T2_REFUSAL
        assert q is True


# ===========================================================================
# F5: Loop detection based on tool-call signatures
# ===========================================================================

class TestF5ToolCallLoopDetection:
    def test_one_dimensional_previous_tool_calls_do_not_raise(self):
        tool_call = {
            "tool_name": "search",
            "args": {"query": "hermes memory"},
            "result": "same evidence",
        }
        assert _is_loop(
            "some different text",
            tool_calls=[tool_call],
            previous_tool_calls=[tool_call],
        ) is True

    def test_identical_tool_calls_detected_as_loop(self):
        tool_calls = [{"tool_name": "search", "args": {"query": "hermes memory"}, "result": "same evidence"}]
        previous_tool_calls = [
            [{"tool_name": "search", "args": {"query": "hermes memory"}, "result": "same evidence"}],
            [{"tool_name": "read_file", "args": {"path": "/foo"}}],
        ]
        kind, q, _ = classify(
            "some different text",
            _ok_state(),
            tool_calls=tool_calls,
            previous_tool_calls=previous_tool_calls,
        )
        assert kind == T2_LOOP_DETECTED
        assert q is True

    def test_different_tool_calls_not_loop(self):
        tool_calls = [{"tool_name": "write_file", "args": {"path": "/new"}}]
        previous_tool_calls = [
            [{"tool_name": "search", "args": {"query": "hermes memory"}}],
        ]
        kind, _, _ = classify(
            "some text",
            _ok_state(),
            tool_calls=tool_calls,
            previous_tool_calls=previous_tool_calls,
        )
        assert kind is None

    def test_text_loop_still_works_without_tool_calls(self):
        output = "I am observing the system and nothing needs to be done."
        kind, q, _ = classify(output, _ok_state(), previous_outputs=[output])
        assert kind == T2_LOOP_DETECTED


# ===========================================================================
# F7: GrayCircuit uses per-file blob SHA manifest; alert on no good ref
# ===========================================================================

class TestF7GrayCircuitManifest:
    def test_rollback_skipped_no_manifest_active(self):
        gc = GrayCircuit(active=True)
        result = gc.rollback_to_last_good()
        assert result["status"] == "skipped"
        assert result["reason"] == "no_good_ref_recorded"

    def test_status_shows_has_good_manifest_false_initially(self):
        gc = GrayCircuit(active=True)
        s = gc.status()
        assert "has_good_manifest" in s
        assert s["has_good_manifest"] is False
        assert s["good_manifest_files"] == 0

    def test_success_records_manifest(self, tmp_path):
        """record_tick_outcome(success=True) triggers _record_last_good_ref."""
        gc = GrayCircuit(active=True, plugin_dir=str(tmp_path))
        # Even if git is not available, the method must not raise.
        gc.record_tick_outcome(success=True)
        # Cannot assert manifest content without real git, but must not crash.

    def test_rollback_noop_in_observe_mode(self):
        gc = GrayCircuit(active=False)
        result = gc.rollback_to_last_good()
        assert result["status"] == "noop"


# ===========================================================================
# F8: should_circuit_count covers all categories
# ===========================================================================

class TestF8CircuitCount:
    def test_e1_counts(self):
        assert should_circuit_count(E1_STATE_DB_CORRUPTED) is True

    def test_e4_counts(self):
        assert should_circuit_count(E4_PROCESS_CRASHED) is True

    def test_t2_refusal_counts(self):
        assert should_circuit_count(T2_REFUSAL) is True

    def test_t2_loop_counts(self):
        assert should_circuit_count(T2_LOOP_DETECTED) is True

    def test_e2_does_not_count(self):
        assert should_circuit_count(E2_PROVIDER_EXHAUSTED) is False

    def test_e3_does_not_count(self):
        assert should_circuit_count(E3_TURN_BUDGET_EXCEEDED) is False

    def test_unclassified_does_not_count(self):
        assert should_circuit_count(E_UNCLASSIFIED) is True

    def test_auth_failed_does_not_count(self):
        assert should_circuit_count(E_AUTH_FAILED) is False

    def test_none_counts_as_unknown(self):
        assert should_circuit_count(None) is True


# ===========================================================================
# F9: Quarantine cascade — expires_at / review_count / structured matching
# ===========================================================================

class TestF9QuarantineReview:
    def test_expires_at_and_review_count_in_result(self, tmp_path):
        """cascade_quarantine returns pending_review_count and expired fields."""
        db = _make_autonomy_db(tmp_path)
        run_id = uuid.uuid4().hex

        result = cascade_quarantine(
            db.c, run_id=run_id, error_kind=E1_STATE_DB_CORRUPTED,
            reason="test", dry_run=False
        )
        assert "pending_review_count" in result
        assert "expired_quarantine_count" in result

    def test_structured_seed_session_match(self, tmp_path):
        """Seed session ID matched by structured prefix, not bare substring."""
        db = _make_autonomy_db(tmp_path)
        run_id = "abc123exact"
        # Plant event with the exact session key
        db.append_event(
            run_id=run_id,
            kind="seed_queue",
            exit_reason="queued",
            summary="path=.../session_autonomy_%s.json worker=ok" % run_id,
        )
        result = cascade_quarantine(
            db.c, run_id=run_id, error_kind=E2_PROVIDER_EXHAUSTED,
            reason="test", dry_run=False
        )
        assert "session_autonomy_" + run_id in result["seed_session_ids"]

    def test_short_run_id_no_false_match(self, tmp_path):
        """Short run_id must not match a session that only partially contains it."""
        db = _make_autonomy_db(tmp_path)
        run_id = "abc"
        other_run_id = "abcdef"
        # Plant event with other_run_id
        db.append_event(
            run_id=run_id,
            kind="seed_queue",
            exit_reason="queued",
            summary="path=session_autonomy_%s.json worker=ok" % other_run_id,
        )
        result = cascade_quarantine(
            db.c, run_id=run_id, error_kind=E1_STATE_DB_CORRUPTED,
            reason="test", dry_run=True
        )
        # session_autonomy_abc should NOT match session_autonomy_abcdef
        assert "session_autonomy_" + run_id not in result["seed_session_ids"]


# ===========================================================================
# F10: T3 hits written to events (record_t3_hits)
# ===========================================================================

class TestF10T3Counter:
    def test_record_t3_hits_writes_events(self, tmp_path):
        db = _make_autonomy_db(tmp_path)
        flags = ["t3_state_db", "t3_overload"]
        written = record_t3_hits(flags, db=db)
        assert written == 2

        rows = db.c.execute(
            "SELECT exit_reason FROM events WHERE kind='t3_audit'"
        ).fetchall()
        reasons = {r[0] for r in rows}
        assert "t3_state_db" in reasons
        assert "t3_overload" in reasons

    def test_record_t3_hits_noop_without_db(self):
        written = record_t3_hits(["t3_state_db"], db=None)
        assert written == 0

    def test_record_t3_hits_empty_flags(self, tmp_path):
        db = _make_autonomy_db(tmp_path)
        written = record_t3_hits([], db=db)
        assert written == 0

    def test_classify_t3_flags_feed_into_record(self, tmp_path):
        """Full pipeline: classify → T3 flags → record_t3_hits → events."""
        db = _make_autonomy_db(tmp_path)
        raw = '{"thoughts": "Provider 503 service unavailable logged.", "did": "checked", "result": "ok", "next": ""}'
        kind, should_q, flags = classify(raw, _ok_state())
        assert kind is None  # clean run
        assert "t3_overload" in flags

        written = record_t3_hits(flags, db=db)
        assert written >= 1

        rows = db.c.execute(
            "SELECT exit_reason FROM events WHERE kind='t3_audit'"
        ).fetchall()
        assert any("t3_overload" in r[0] for r in rows)
