import hashlib
import sqlite3
from pathlib import Path

from plugins.autonomy.evidence_challenge import run_challenges
from plugins.autonomy.schema import migrate


def _fixture(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    now = 1_000_000.0
    conn.execute("insert into receipts values(?,?,?,?,?,?,?,?)", ("r1", now, "run", "p", "unverified", "run", "goal", "{}"))
    stdout, stderr = b"good", b"diagnostic"
    frame = tmp_path / "capture.raw"
    frame.write_bytes(b"STDOUT\n" + stdout + b"\nSTDERR\n" + stderr)
    for stream, data in (("stdout", stdout), ("stderr", stderr)):
        expected = hashlib.sha256(data).hexdigest()
        conn.execute("insert into evidence(receipt_id,stream,digest,byte_count,buffer_path,status,created_at,challenge_deadline) values(?,?,?,?,?,?,?,?)", ("r1", stream, expected, len(data), str(frame), "unverified", now, now + 21600))
    conn.commit()
    return conn, now, frame


def test_healthy_framed_verifies_both_and_is_idempotent(tmp_path):
    conn, now, _ = _fixture(tmp_path)
    assert run_challenges(conn, now=now) == {"verified": 2, "mismatched": 0, "unavailable": 0, "overdue": 0}
    assert conn.execute("select count(*) from evidence_challenges").fetchone()[0] == 2
    assert conn.execute("select count(*) from evidence where status='verified'").fetchone()[0] == 2
    assert run_challenges(conn, now=now + 1) == {"verified": 0, "mismatched": 0, "unavailable": 0, "overdue": 0}
    assert conn.execute("select count(*) from evidence_challenges").fetchone()[0] == 2
    assert conn.execute("select count(*) from last_tick").fetchone()[0] == 0


def test_tamper_stdout_only_mismatches_stdout(tmp_path):
    conn, now, frame = _fixture(tmp_path)
    frame.write_bytes(b"STDOUT\nBAD\nSTDERR\ndiagnostic")
    result = run_challenges(conn, now=now)
    assert result == {"verified": 1, "mismatched": 1, "unavailable": 0, "overdue": 0}
    assert conn.execute("select status from evidence where stream='stdout'").fetchone()[0] == "mismatched"
    assert conn.execute("select status from evidence where stream='stderr'").fetchone()[0] == "verified"


def test_truncated_or_ambiguous_frame_is_unavailable(tmp_path):
    conn, now, frame = _fixture(tmp_path)
    frame.write_bytes(b"STDOUT\ntruncated")
    result = run_challenges(conn, now=now)
    assert result["unavailable"] == 2
    assert conn.execute("select count(*) from evidence where status='unverified'").fetchone()[0] == 2


def test_missing_buffer_is_expired_and_not_mismatched(tmp_path):
    conn, now, frame = _fixture(tmp_path)
    frame.unlink()
    result = run_challenges(conn, now=now)
    assert result["unavailable"] == 2
    assert conn.execute("select count(*) from evidence where status='expired'").fetchone()[0] == 2
    assert conn.execute("select count(*) from evidence_challenges where status='unavailable'").fetchone()[0] == 2


def test_deadline_is_fail_closed(tmp_path):
    conn, now, _ = _fixture(tmp_path)
    result = run_challenges(conn, now=now + 21601)
    assert result["overdue"] == 2
    assert conn.execute("select count(*) from evidence_challenges where status='overdue'").fetchone()[0] == 2
