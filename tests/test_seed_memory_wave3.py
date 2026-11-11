"""Regression coverage for seed-memory wave 3 safety gates."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "seed_memory" / "seed_src"))
from hermes.memory import retrieval, store
from hermes.memory.schema import Emotion, Seed, SeedAccessLog


@pytest.fixture
def seed_db(tmp_path, monkeypatch):
    path = tmp_path / "seeds.db"
    monkeypatch.setattr(store, "DB_PATH", path)
    store.init_db(str(path))
    return path


def make_seed(identifier, *, ref=None, status="active", importance=0.6):
    return Seed(
        id=identifier,
        timestamp=datetime(2026, 11, 1, tzinfo=timezone.utc),
        summary="File example.py failed E404 on 2026-11-01",
        participants=["worker"],
        emotion=Emotion(primary="neutral", intensity=0.5),
        importance=importance,
        provenance_ref=ref,
        status=status,
    )


def test_production_access_log_and_query_time_decay_coexist(seed_db):
    first = make_seed("older", ref="event:old", importance=0.9)
    second = make_seed("recent", ref="event:new", importance=0.6)
    second.timestamp = datetime.now(timezone.utc)
    first.decay_rate = second.decay_rate = 0.05
    assert store.save_seeds([first, second]) == 2
    signed = SeedAccessLog(seed_id=second.id, writer="tester", session_id="session:1",
                           reason="retrieval", provenance="event:new")
    assert store.save_access_log(signed)
    with sqlite3.connect(seed_db) as conn:
        assert conn.execute("SELECT seed_id, writer, provenance FROM seed_access_log").fetchone() == (
            second.id, "tester", "event:new")
        names = {row[1] for row in conn.execute("PRAGMA index_list(seed_access_log)")}
    assert {"idx_seed_access_log_seed_id", "idx_seed_access_log_timestamp"} <= names
    assert store._compute_effective_importance(0.9, first.timestamp.isoformat(), 0.05,
                                                datetime.now(timezone.utc)) < 0.6
    assert store.get_ranked_seeds(limit=1)[0].id == second.id
    assert store.decay_due_seeds(dry_run=True)["dry_run"] is True


def test_touch_signed_access_log_uses_merkle_path(seed_db, monkeypatch):
    seed = make_seed("signed", ref="event:signed")
    store.save_seed(seed)
    observed = []

    def fake_signed_write_access_log(*, log, writer):
        observed.append((log, writer))
        return None

    monkeypatch.setattr(store, "import_module", lambda _: SimpleNamespace(
        signed_write_access_log=fake_signed_write_access_log
    ))
    store.touch_seed(seed.id, writer="tester", session_id="session:1",
                     reason="retrieval", provenance="event:signed")
    assert len(observed) == 1
    assert observed[0][0].seed_id == seed.id
    assert observed[0][0].session_id == "session:1"
    assert observed[0][1] == "tester"


def test_touch_unsigned_fallback_persists_and_warns(seed_db, monkeypatch, caplog):
    seed = make_seed("unsigned", ref="event:unsigned")
    store.save_seed(seed)
    module = f"{store.__package__}.signed_write"

    def missing_import(name):
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(store, "import_module", missing_import)
    store.touch_seed(seed.id, writer="tester", session_id="session:2", reason="recall")
    with sqlite3.connect(seed_db) as conn:
        row = conn.execute("SELECT seed_id, writer FROM seed_access_log").fetchone()
    assert row == (seed.id, "tester")
    assert "unsigned seed access log" in caplog.text
    assert module == "hermes.memory.signed_write"


def test_prefetch_read_only_and_actual_recall_touches(seed_db, monkeypatch):
    seed = make_seed("prefetch", ref="event:1")
    assert store.save_seed(seed)
    monkeypatch.setattr(retrieval, "_embed_query", lambda *_: None, raising=False)
    retrieval.retrieve("example.py", top_k=1, record_access=False)
    with sqlite3.connect(seed_db) as conn:
        before = conn.execute(
            "SELECT access_count, last_real_access FROM seeds WHERE id=?", (seed.id,)
        ).fetchone()
    assert before == (0, None)
    assert retrieval.retrieve("example.py", top_k=1)
    with sqlite3.connect(seed_db) as conn:
        after = conn.execute(
            "SELECT access_count, last_real_access FROM seeds WHERE id=?", (seed.id,)
        ).fetchone()
    assert after[0] >= 1
    assert after[1] is not None


def test_hash_and_provenance_ref_idempotency(seed_db):
    first = make_seed("first", ref="event:1", importance=0.4)
    duplicate = make_seed("different-id", ref="event:1", importance=0.9)
    distinct_ref = make_seed("distinct", ref="event:2")
    assert store.save_seed(first)
    assert not store.save_seed(duplicate)
    assert store.save_seeds([duplicate, distinct_ref]) == 1
    with sqlite3.connect(seed_db) as conn:
        rows = conn.execute(
            "SELECT id, provenance_ref, importance FROM seeds ORDER BY rowid"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "first"
    assert rows[0][1] == "event:1"
    assert rows[0][2] == pytest.approx(0.9)
    assert rows[1][1] == "event:2"


def test_decay_phase2_needs_natural_day_and_explicit_confirmation(seed_db, monkeypatch):
    store.save_seed(make_seed("phase2"))
    with pytest.raises(RuntimeError):
        store.decay_due_seeds(now=datetime(2026, 11, 28, 23, 59, tzinfo=timezone.utc),
                              dry_run=False, phase2_confirmed=True)
    with pytest.raises(RuntimeError):
        store.decay_due_seeds(now=datetime(2026, 11, 29, tzinfo=timezone.utc), dry_run=False)
    report = store.decay_due_seeds(now=datetime(2026, 11, 29, tzinfo=timezone.utc),
                                   dry_run=True)
    assert report["dry_run"] is True
    class AfterObservation(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 30, tzinfo=timezone.utc)

    monkeypatch.setattr(store, "datetime", AfterObservation)
    report = store.decay_due_seeds(now=datetime(2026, 11, 29, tzinfo=timezone.utc),
                                   dry_run=False, phase2_confirmed=True)
    assert report["dry_run"] is False


def test_filtered_counter_is_separate_and_revival_requires_offline_proposal(seed_db):
    store.record_filtered_by_contract("missing_ref")
    assert store.count_filtered_by_contract() == 1
    archived = make_seed("archived", ref="event:1", status="archived")
    store.save_seed(archived)
    assert store.get_all_seeds() == []
    assert len(store.get_all_seeds(include_archived=True)) == 1
    assert store.decay_due_seeds(now=datetime(2026, 11, 29, tzinfo=timezone.utc))[
        "dormant_candidates"
    ] == 0
    with pytest.raises(ValueError):
        store.propose_archived_revival(archived.id, offline_review_id=" ")
    assert store.propose_archived_revival(archived.id, offline_review_id="review:1")
    assert store.get_all_seeds() == []
    # A repeated write cannot implicitly reactivate an archived record.
    assert not store.save_seed(make_seed("retry", ref="event:1", status="active"))
    assert store.get_all_seeds() == []
    with sqlite3.connect(seed_db) as conn:
        assert conn.execute("SELECT status FROM seeds WHERE id=?", (archived.id,)).fetchone()[0] == "archived"
        assert conn.execute("SELECT COUNT(*) FROM seed_revival_proposals").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM seeds").fetchone()[0] == 1
