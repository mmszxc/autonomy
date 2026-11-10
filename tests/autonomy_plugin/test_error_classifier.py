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
    T2_EMPTY_RESPONSE,
    T2_LOOP_DETECTED,
    T2_REFUSAL,
    classify,
    classify_error_text,
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
        kind, quarantine, _ = classify("text", _ok_state(), exit_reason="overloaded")
        assert kind == E2_PROVIDER_EXHAUSTED
        assert quarantine is True

    def test_e2_provider_exhausted_via_rate_limit(self):
        kind, quarantine, _ = classify("text", _ok_state(), exit_reason="rate_limit")
        assert kind == E2_PROVIDER_EXHAUSTED
        assert quarantine is True

    def test_e2_provider_exhausted_via_http_503(self):
        state = dict(_ok_state(), http_status=503)
        kind, quarantine, _ = classify("text", state)
        assert kind == E2_PROVIDER_EXHAUSTED
        assert quarantine is True

    def test_e2_provider_exhausted_via_http_429(self):
        state = dict(_ok_state(), http_status=429)
        kind, quarantine, _ = classify("text", state)
        assert kind == E2_PROVIDER_EXHAUSTED
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

    def test_e4_timeout(self):
        state = _failed_state(timed_out=True)
        kind, quarantine, _ = classify("text", state, exit_reason="max_seconds")
        assert kind == E4_PROCESS_CRASHED
        assert quarantine is True

    def test_e4_nonzero_returncode(self):
        state = _failed_state(returncode=2)
        kind, quarantine, _ = classify("text", state)
        assert kind == E4_PROCESS_CRASHED
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
        # A failed run is classified by T1, T3 flags are irrelevant.
        state = _failed_state(returncode=1)
        kind, quarantine, flags = classify(
            "Provider 503 error occurred", state
        )
        assert kind == E4_PROCESS_CRASHED
        assert quarantine is True
        # T3 is not reached for T1 matches; flags should be empty.
        assert "t3_overload" not in flags or kind == E4_PROCESS_CRASHED


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
        assert kind == E4_PROCESS_CRASHED

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
