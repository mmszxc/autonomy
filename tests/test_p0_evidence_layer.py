import hashlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "plugins"))

from autonomy.budget import BudgetLedger, ResourceSnapshot
from autonomy.circuit import Circuit
from autonomy.config import P0Config
from autonomy.db import AutonomyDB, ImmutableEvidence
from autonomy.evidence_challenge import ChallengeResult, challenge_pending, reconcile
from autonomy.event_log import EventLog, EvidenceStateError
from autonomy.schema import SCHEMA_VERSION, downgrade, migrate
from autonomy.cli_transport import capture_process_evidence
from autonomy.validation import (
    FrozenValidator,
    StopRule,
    ValidationError,
    non_inferior,
)


def test_schema_up_and_down_migration():
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert {
        "receipts", "evidence", "claims", "tombstones", "quarantines",
        "experiments", "baselines", "schema_meta",
    } <= tables
    assert conn.execute("select version from schema_meta").fetchone()[0] == SCHEMA_VERSION
    downgrade(conn)
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    assert "receipts" not in tables and "goals" in tables


def test_db_evidence_is_append_only(tmp_path):
    db = AutonomyDB(tmp_path / "a.db")
    rid = db.append_receipt(kind="tool", payload_hash="a" * 64, status="unverified")
    eid = db.append_evidence(
        receipt_id=rid, stream="stdout", digest="b" * 64, byte_count=3,
        head=b"abc", tail=b"abc", status="unverified",
    )
    with pytest.raises((sqlite3.IntegrityError, ImmutableEvidence)):
        db.c.execute("update evidence set digest=? where id=?", ("c" * 64, eid))
    assert db.get_evidence(eid)["digest"] == "b" * 64


def test_p0_config_is_frozen_and_disk_thresholds():
    cfg = P0Config.from_mapping({"validator_hashes": {"safety": "a" * 64}})
    assert cfg.t1_free_bytes(100 * 1024**3) == 5 * 1024**3
    assert cfg.t1_free_bytes(1024 * 1024**3) == 10 * 1024**3
    assert cfg.pool_cap_for_startup_free_space(20 * 1024**3) == 2 * 1024**3
    assert cfg.t2_free_bytes == 1024**3
    with pytest.raises((AttributeError, TypeError)):
        cfg.buffer_ttl_s = 1


def test_capture_evidence_limits_and_redacts(tmp_path):
    secret = b"token=super-secret-value"
    out = b"a" * 300 + secret + b"z" * 300
    ev = capture_process_evidence(out, b"password=hunter2", 0, buffer_dir=tmp_path)
    assert ev["stdout_bytes"] == len(out)
    assert ev["stdout_sha256"] == hashlib.sha256(out).hexdigest()
    assert len(ev["stdout_head"]) <= 256 and len(ev["stdout_tail"]) <= 256
    rendered = repr(ev)
    assert "super-secret-value" not in rendered and "hunter2" not in rendered
    assert ev["permanent_bytes"] <= 1536
    assert Path(ev["buffer_path"]).stat().st_size <= 5 * 1024**2


def test_capture_marks_timeout_signal_oom_and_truncation(tmp_path):
    ev = capture_process_evidence(
        b"x" * (5 * 1024**2 + 10), b"", -9, signal_number=9,
        timed_out=True, oom=True, buffer_dir=tmp_path,
    )
    assert ev["truncated"] and ev["timed_out"] and ev["oom"]
    assert ev["signal"] == 9 and ev["exit_code"] == -9


def test_event_log_checkpoint_and_state_machine(tmp_path):
    db = AutonomyDB(tmp_path / "a.db")
    log = EventLog(db)
    rid = log.append_receipt("tool", "a" * 64)
    eid = log.append_evidence(rid, "stdout", b"hello")
    assert log.transition(eid, "verified") == "verified"
    with pytest.raises(EvidenceStateError):
        log.transition(eid, "unverified")
    cp = log.checkpoint()
    assert cp["row_count"] == 1 and len(cp["root_hash"]) == 64
    same = log.checkpoint()
    assert same["root_hash"] == cp["root_hash"] and same["row_count"] == 1
    log.append_evidence(rid, "stderr", b"next")
    newer = log.checkpoint()
    assert newer["row_count"] == 2 and newer["root_hash"] != cp["root_hash"]


def test_challenge_verifies_all_pending_and_reports_mismatch(tmp_path):
    db = AutonomyDB(tmp_path / "a.db")
    log = EventLog(db)
    rid = log.append_receipt("tool", "a" * 64)
    good = log.append_evidence(rid, "stdout", b"good")
    bad = log.append_evidence(rid, "stderr", b"expected")
    sources = {good: b"good", bad: b"different"}
    result = challenge_pending(db, lambda row: sources[row["id"]], now=10_000)
    assert isinstance(result, ChallengeResult)
    assert result.checked == 2 and result.verified == 1 and result.mismatched == 1


def test_three_way_reconciliation_reports_missing_ids():
    result = reconcile(["a", "b"], ["a", "c"], ["a", "d"])
    assert result["agreed"] == 1 and result["difference_ratio"] == 0.75
    assert result["event_only"] == ["b"] and result["receipt_only"] == ["c"]


def test_budget_weekly_buckets_and_daily_alarm():
    ledger = BudgetLedger(weekly_units=100)
    ledger.consume("normal", 20, at=1_700_000_000)
    ledger.consume("memory", 9, at=1_700_000_000)
    ledger.consume("evolution", 11, at=1_700_000_000)
    status = ledger.status(at=1_700_000_000)
    assert status["memory"]["within_cap"]
    assert not status["evolution"]["within_cap"]
    assert status["daily_warning"]


def test_resource_snapshot_watermarks():
    snap = ResourceSnapshot(
        filesystem_size=100 * 1024**3, free_bytes=4 * 1024**3,
        evidence_pool_bytes=9 * 1024**3, evidence_pool_cap=10 * 1024**3,
    )
    assert snap.pool_level == "warn"
    assert snap.free_space_level == "freeze"


def test_circuit_separates_normal_from_learning_and_requests_rollback():
    c = Circuit(threshold=2)
    c.freeze_learning("audit_mismatch", rollback=True)
    assert c.allow_normal() and not c.allow_learning() and not c.allow_promotion()
    assert c.rollback_requested()
    c.unfreeze_learning("operator-confirmed")
    assert c.allow_learning()


def test_frozen_validator_hash_author_and_mutation_gate():
    source = b"validator-v1"
    validator = FrozenValidator(
        name="safety", expected_hash=hashlib.sha256(source).hexdigest(),
        author="reviewer", proposer="agent", source=source,
    )
    assert validator.verify_source()
    with pytest.raises(ValidationError):
        FrozenValidator("x", hashlib.sha256(source).hexdigest(), "same", "same", source)
    assert validator.accept_mutations([True] * 20)
    assert not validator.accept_mutations([True] * 19 + [False])


def test_non_inferiority_and_stop_rule():
    assert non_inferior(candidate=0.98, control=0.99, margin=0.03, posterior=0.96)
    assert not non_inferior(candidate=0.90, control=0.99, margin=0.03, posterior=0.99)
    stop = StopRule(safety_violations=0, cost_increase=0.10, min_posterior=0.95)
    assert stop.should_stop(safety_violations=1, cost_increase=0, posterior=1)
    assert not stop.should_stop(safety_violations=0, cost_increase=0.05, posterior=0.96)


MUTATION_CASES = [
    ("exit_zero_to_nonzero", {"exit_code": 1}),
    ("exit_nonzero_to_zero", {"exit_code": 0}),
    ("stdout_truncated", {"truncated": True}),
    ("stderr_truncated", {"truncated": True}),
    ("stdout_invalid_utf8", {"stdout": b"\xff\xfe"}),
    ("stderr_invalid_utf8", {"stderr": b"\x80"}),
    ("bearer_secret", {"stdout": b"Authorization: Bearer abc"}),
    ("password_secret", {"stderr": b"password=hunter2"}),
    ("api_key_secret", {"stdout": b"api_key=abc"}),
    ("token_secret", {"stdout": b"token=abc"}),
    ("timeout", {"timed_out": True}),
    ("sigterm", {"signal_number": 15}),
    ("sigkill", {"signal_number": 9}),
    ("oom", {"oom": True}),
    ("empty_stdout", {"stdout": b""}),
    ("empty_stderr", {"stderr": b""}),
    ("large_stdout", {"stdout": b"x" * (5 * 1024**2 + 1)}),
    ("large_stderr", {"stderr": b"x" * (5 * 1024**2 + 1)}),
    ("digest_mismatch", {"force_digest": "0" * 64}),
    ("receipt_missing", {"receipt_missing": True}),
]


@pytest.mark.parametrize("name,mutation", MUTATION_CASES)
def test_mutation_fixture_spec_is_complete(name, mutation):
    assert name and mutation
    assert len(MUTATION_CASES) == 20
