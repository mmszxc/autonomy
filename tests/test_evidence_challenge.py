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
    good = tmp_path / "good.raw"; good.write_bytes(b"good")
    bad = tmp_path / "bad.raw"; bad.write_bytes(b"bad")
    for path, deadline in ((good, now + 21600), (bad, now + 21600)):
        data = path.read_bytes()
        expected = hashlib.sha256(data if path == good else b"other").hexdigest()
        conn.execute("insert into evidence(receipt_id,stream,digest,byte_count,buffer_path,status,created_at,challenge_deadline) values(?,?,?,?,?,?,?,?)", ("r1", str(path), expected, len(data), str(path), "unverified", now, deadline))
    conn.commit()
    return conn, now


def test_challenge_fixture_status_deadline_and_idempotence(tmp_path):
    conn, now = _fixture(tmp_path)
    result = run_challenges(conn, now=now)
    assert result == {"verified": 1, "mismatched": 1, "unavailable": 0, "overdue": 0}
    assert conn.execute("select count(*) from evidence_challenges").fetchone()[0] == 2
    assert conn.execute("select count(*) from evidence where status='verified'").fetchone()[0] == 1
    assert conn.execute("select count(*) from evidence where status='mismatched'").fetchone()[0] == 1
    # A second run finds no pending rows and cannot duplicate challenge records.
    assert run_challenges(conn, now=now + 1) == {"verified": 0, "mismatched": 0, "unavailable": 0, "overdue": 0}
    assert conn.execute("select count(*) from evidence_challenges").fetchone()[0] == 2
    assert conn.execute("select count(*) from last_tick").fetchone()[0] == 0


def test_challenge_deadline_is_fail_closed(tmp_path):
    conn, now = _fixture(tmp_path)
    result = run_challenges(conn, now=now + 21601)
    assert result["overdue"] == 2
    assert conn.execute("select count(*) from evidence_challenges where status='overdue'").fetchone()[0] == 2
