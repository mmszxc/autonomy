"""Phase-1 decay maintenance tests.

Covers:
  1. Decay formula correctness (half-life, floor, exempt threshold)
  2. dry_run=True never modifies the DB (read-only contract)
  3. dry_run=False raises RuntimeError (write guard)
  4. Idempotent: calling twice gives same result
  5. Timezone correctness (UTC vs naive)
  6. Dormant-candidate detection (age + effective threshold)
  7. Exempt seeds are never marked dormant
  8. Empty DB returns zero-count report
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import textwrap
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "plugins" / "seed_memory" / "seed_src"


def _run(script: str, tmp_path: Path) -> tuple[str, str, int]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT)
    env.pop("HERMES_SEED_DB_PATH", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout, result.stderr, result.returncode


# ---------------------------------------------------------------------------
# helper: insert seed rows directly (bypasses embedding logic)
# ---------------------------------------------------------------------------

def _insert_seed(conn: sqlite3.Connection, seed_id: str, importance: float,
                 decay_rate: float, timestamp_iso: str,
                 last_accessed_iso: str | None = None,
                 last_real_access_iso: str | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO seeds "
        "(id, timestamp, summary, participants, emotion_primary, emotion_intensity, "
        "importance, decay_rate, last_accessed, last_real_access, access_count, shareable) "
        "VALUES (?, ?, 'test', '[]', 'neutral', 0.5, ?, ?, ?, ?, 0, 1)",
        (seed_id, timestamp_iso, importance, decay_rate,
         last_accessed_iso, last_real_access_iso),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. Decay formula: half-life
# ---------------------------------------------------------------------------

def test_decay_formula_half_life():
    """At t = ln(2)/decay_rate days, effective should be ~50% of importance."""
    script = textwrap.dedent("""
        from hermes.memory.store import _decay_effective_importance, _DECAY_FLOOR
        import math

        decay_rate = 0.05
        half_life = math.log(2) / decay_rate   # ~13.86 days
        importance = 0.8

        effective = _decay_effective_importance(importance, decay_rate, half_life)
        expected = max(_DECAY_FLOOR, importance * 0.5)
        assert abs(effective - expected) < 0.001, f"expected ~{expected:.4f} got {effective:.4f}"
        print("OK half_life=", round(half_life, 2), "effective=", round(effective, 4))
    """)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT)
    r = subprocess.run([sys.executable, "-c", script], env=env,
                       capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"
    assert "OK" in r.stdout


def test_decay_formula_floor():
    """Effective importance never drops below _DECAY_FLOOR."""
    script = textwrap.dedent("""
        from hermes.memory.store import _decay_effective_importance, _DECAY_FLOOR
        # Very long age
        effective = _decay_effective_importance(0.5, 0.05, 10000.0)
        assert effective >= _DECAY_FLOOR, f"floor violated: {effective}"
        assert effective <= 0.5
        print("OK floor=", effective)
    """)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT)
    r = subprocess.run([sys.executable, "-c", script], env=env,
                       capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert "OK" in r.stdout


def test_decay_formula_exempt_threshold():
    """Seeds born with importance >= _DECAY_EXEMPT_IMPORTANCE must be marked exempt."""
    script = textwrap.dedent(f"""
        import os, sqlite3, json
        from datetime import datetime, timedelta, timezone
        db = "seeds_exempt.db"
        os.environ["HERMES_SEED_DB_PATH"] = db
        from hermes.memory import store

        store.init_db(db)
        # Insert a high-importance seed
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO seeds "
            "(id, timestamp, summary, participants, emotion_primary, emotion_intensity, "
            "importance, decay_rate, access_count, shareable) "
            "VALUES (?, ?, 'exempt_test', '[]', 'joy', 0.9, ?, 0.05, 0, 1)",
            ("seed_exempt_01", old_ts, store._DECAY_EXEMPT_IMPORTANCE),
        )
        conn.commit()
        conn.close()

        report = store.decay_due_seeds(dry_run=True)
        assert report["exempt"] == 1, f"expected 1 exempt, got {{report['exempt']}}"
        entry = report["entries"][0]
        assert entry["is_exempt"] is True
        assert entry["is_dormant_candidate"] is False
        print("OK exempt=1")
    """)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SOURCE_ROOT)
    r = subprocess.run([sys.executable, "-c", script], cwd="/tmp",
                       env=env, capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stderr={r.stderr}\nstdout={r.stdout}"
    assert "OK" in r.stdout


# ---------------------------------------------------------------------------
# 2. dry_run=True never modifies the DB
# ---------------------------------------------------------------------------

def test_dry_run_does_not_modify_db(tmp_path):
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os, sqlite3
        from datetime import datetime, timedelta, timezone
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store

        store.init_db({str(db)!r})
        conn = sqlite3.connect({str(db)!r})
        old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO seeds "
            "(id, timestamp, summary, participants, emotion_primary, emotion_intensity, "
            "importance, decay_rate, access_count, shareable) "
            "VALUES (?, ?, 'decay_test', '[]', 'neutral', 0.5, 0.6, 0.05, 0, 1)",
            ("seed_decay_01", old_ts),
        )
        conn.commit()
        conn.close()

        # Record importance before
        conn_before = sqlite3.connect({str(db)!r})
        before = conn_before.execute("SELECT importance FROM seeds WHERE id='seed_decay_01'").fetchone()[0]
        conn_before.close()

        report = store.decay_due_seeds(dry_run=True)
        assert report["dry_run"] is True

        conn_after = sqlite3.connect({str(db)!r})
        after = conn_after.execute("SELECT importance FROM seeds WHERE id='seed_decay_01'").fetchone()[0]
        conn_after.close()

        assert before == after, f"DB was modified! before={{before}} after={{after}}"
        print("OK importance unchanged=", after)
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}\nstdout={stdout}"
    assert "OK" in stdout


# ---------------------------------------------------------------------------
# 3. dry_run=False raises RuntimeError
# ---------------------------------------------------------------------------

def test_dry_run_false_raises(tmp_path):
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store
        store.init_db({str(db)!r})
        try:
            store.decay_due_seeds(dry_run=False)
            print("FAIL no exception raised")
        except RuntimeError as exc:
            assert "Phase 2" in str(exc) or "dry_run=False" in str(exc)
            print("OK raised RuntimeError:", str(exc)[:60])
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}"
    assert "OK" in stdout
    assert "FAIL" not in stdout


# ---------------------------------------------------------------------------
# 4. Idempotent: calling twice gives same result
# ---------------------------------------------------------------------------

def test_decay_due_seeds_idempotent(tmp_path):
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os, json
        from datetime import datetime, timedelta, timezone
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store

        store.init_db({str(db)!r})
        import sqlite3
        conn = sqlite3.connect({str(db)!r})
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        for i in range(3):
            conn.execute(
                "INSERT OR IGNORE INTO seeds "
                "(id, timestamp, summary, participants, emotion_primary, emotion_intensity, "
                "importance, decay_rate, access_count, shareable) "
                "VALUES (?, ?, 'test', '[]', 'neutral', 0.5, 0.5, 0.05, 0, 1)",
                (f"seed_idem_{{i:02d}}", old_ts),
            )
        conn.commit(); conn.close()

        now = datetime.now(timezone.utc)
        r1 = store.decay_due_seeds(now=now, dry_run=True)
        r2 = store.decay_due_seeds(now=now, dry_run=True)

        assert r1["total"] == r2["total"]
        assert r1["dormant_candidates"] == r2["dormant_candidates"]
        ids1 = {{e["id"] for e in r1["entries"]}}
        ids2 = {{e["id"] for e in r2["entries"]}}
        assert ids1 == ids2
        # effective values must be identical (same now)
        eff1 = {{e["id"]: e["effective"] for e in r1["entries"]}}
        eff2 = {{e["id"]: e["effective"] for e in r2["entries"]}}
        assert eff1 == eff2
        print("OK idempotent total=", r1["total"])
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}\nstdout={stdout}"
    assert "OK" in stdout


# ---------------------------------------------------------------------------
# 5. Timezone correctness
# ---------------------------------------------------------------------------

def test_decay_timezone_aware_vs_naive(tmp_path):
    """UTC-aware and naive timestamps both produce correct age calculation."""
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os
        from datetime import datetime, timedelta, timezone
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store

        store.init_db({str(db)!r})
        import sqlite3
        conn = sqlite3.connect({str(db)!r})
        # Store a naive timestamp (no tzinfo in ISO string)
        naive_ts = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "INSERT OR IGNORE INTO seeds "
            "(id, timestamp, summary, participants, emotion_primary, emotion_intensity, "
            "importance, decay_rate, access_count, shareable) "
            "VALUES ('seed_naive_01', ?, 'naive_tz_test', '[]', 'neutral', 0.5, 0.6, 0.05, 0, 1)",
            (naive_ts,),
        )
        conn.commit(); conn.close()

        now = datetime.now(timezone.utc)
        report = store.decay_due_seeds(now=now, dry_run=True)
        assert report["total"] == 1
        entry = report["entries"][0]
        # 5 days at decay_rate=0.05: effective = 0.6 * exp(-0.05*5) ~= 0.470
        import math
        expected = max(0.05, 0.6 * math.exp(-0.05 * entry["age_days"]))
        assert abs(entry["effective"] - expected) < 0.001, f"{{entry['effective']}} != {{expected}}"
        print("OK tz age_days=", entry["age_days"], "effective=", entry["effective"])
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}\nstdout={stdout}"
    assert "OK" in stdout


# ---------------------------------------------------------------------------
# 6. Dormant-candidate detection
# ---------------------------------------------------------------------------

def test_dormant_candidate_detected(tmp_path):
    """Seed with effective < 0.15 AND age_real >= 7d should be dormant candidate."""
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os, math
        from datetime import datetime, timedelta, timezone
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store

        store.init_db({str(db)!r})
        import sqlite3
        conn = sqlite3.connect({str(db)!r})
        # 60 days old, low importance — will definitely decay below threshold
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO seeds "
            "(id, timestamp, summary, participants, emotion_primary, emotion_intensity, "
            "importance, decay_rate, last_real_access, access_count, shareable) "
            "VALUES ('seed_dormant_01', ?, 'dormant', '[]', 'neutral', 0.5, 0.3, 0.1, ?, 0, 1)",
            (old_ts, old_ts),
        )
        conn.commit(); conn.close()

        report = store.decay_due_seeds(dry_run=True)
        assert report["dormant_candidates"] == 1, f"expected 1, got {{report['dormant_candidates']}}"
        entry = report["entries"][0]
        assert entry["is_dormant_candidate"] is True
        assert entry["effective"] < store._DECAY_DORMANT_THRESHOLD
        print("OK dormant effective=", entry["effective"])
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}\nstdout={stdout}"
    assert "OK" in stdout


# ---------------------------------------------------------------------------
# 7. Exempt seed is never dormant candidate
# ---------------------------------------------------------------------------

def test_exempt_seed_never_dormant(tmp_path):
    """High-importance (exempt) seeds must NOT appear as dormant candidates."""
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os
        from datetime import datetime, timedelta, timezone
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store

        store.init_db({str(db)!r})
        import sqlite3
        conn = sqlite3.connect({str(db)!r})
        old_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO seeds "
            "(id, timestamp, summary, participants, emotion_primary, emotion_intensity, "
            "importance, decay_rate, last_real_access, access_count, shareable) "
            "VALUES ('seed_hi_01', ?, 'exempt', '[]', 'joy', 0.9, ?, 0.05, ?, 0, 1)",
            (old_ts, store._DECAY_EXEMPT_IMPORTANCE, old_ts),
        )
        conn.commit(); conn.close()

        report = store.decay_due_seeds(dry_run=True)
        assert report["dormant_candidates"] == 0, f"exempt seed marked dormant!"
        assert report["exempt"] == 1
        entry = report["entries"][0]
        assert entry["is_exempt"] is True
        assert entry["is_dormant_candidate"] is False
        print("OK exempt never dormant")
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}\nstdout={stdout}"
    assert "OK" in stdout


# ---------------------------------------------------------------------------
# 8. Empty DB returns zero-count report
# ---------------------------------------------------------------------------

def test_empty_db_returns_zero_report(tmp_path):
    db = tmp_path / "seeds.db"
    script = textwrap.dedent(f"""
        import os
        os.environ["HERMES_SEED_DB_PATH"] = {str(db)!r}
        from hermes.memory import store
        store.init_db({str(db)!r})
        report = store.decay_due_seeds(dry_run=True)
        assert report["total"] == 0
        assert report["dormant_candidates"] == 0
        assert report["entries"] == []
        assert report["dry_run"] is True
        print("OK empty total=0")
    """)
    stdout, stderr, rc = _run(script, tmp_path)
    assert rc == 0, f"rc={rc}\nstderr={stderr}\nstdout={stdout}"
    assert "OK" in stdout
