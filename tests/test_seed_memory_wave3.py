"""Regression coverage for seed-memory wave 3 safety gates."""

from datetime import datetime, timezone
import importlib
import os
from pathlib import Path
import sqlite3
import sys
import types
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(ROOT / "plugins"))
sys.path.append(str(ROOT / "plugins" / "seed_memory" / "seed_src"))
if "agent.memory_provider" not in sys.modules:
    agent_module = types.ModuleType("agent")
    memory_provider_module = types.ModuleType("agent.memory_provider")
    memory_provider_module.MemoryProvider = type("MemoryProvider", (), {})
    memory_provider_module.RecallStatus = type("RecallStatus", (), {})
    agent_module.memory_provider = memory_provider_module
    sys.modules.setdefault("agent", agent_module)
    sys.modules["agent.memory_provider"] = memory_provider_module

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


@pytest.fixture
def mixed_last_accessed_db(seed_db):
    iso_value = "2026-11-10T09:30:00+00:00"
    epoch_value = 1794388936.3138785
    iso_seed = make_seed("iso-seed")
    epoch_seed = make_seed("epoch-seed")
    epoch_seed.summary = "A distinct historical seed"
    store.save_seeds([iso_seed, epoch_seed])
    with sqlite3.connect(seed_db) as conn:
        conn.execute(
            "UPDATE seeds SET last_accessed = ? WHERE id = ?",
            (iso_value, "iso-seed"),
        )
        conn.execute(
            "UPDATE seeds SET last_accessed = ? WHERE id = ?",
            (str(epoch_value), "epoch-seed"),
        )
    return iso_value, epoch_value


def test_row_to_seed_accepts_mixed_last_accessed_formats(mixed_last_accessed_db):
    iso_value, epoch_value = mixed_last_accessed_db

    seeds = {seed.id: seed for seed in store.get_all_seeds()}

    assert isinstance(seeds["iso-seed"], Seed)
    assert seeds["iso-seed"].last_accessed == datetime.fromisoformat(iso_value)
    assert isinstance(seeds["epoch-seed"], Seed)
    assert seeds["epoch-seed"].last_accessed == datetime.fromtimestamp(
        epoch_value, tz=timezone.utc
    )


def test_drain_accepts_both_session_and_timestamp_transcripts(tmp_path, monkeypatch):
    from types import ModuleType

    agent_module = ModuleType("agent")
    provider_module = ModuleType("agent.memory_provider")
    provider_module.MemoryProvider = object
    provider_module.RecallStatus = SimpleNamespace
    monkeypatch.setitem(sys.modules, "agent", agent_module)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", provider_module)
    import seed_memory

    provider = seed_memory.SeedMemoryProvider()
    provider._queue_dir = tmp_path
    provider._config = {"write_enabled": True}
    provider._user_active = lambda: False
    inspected = []

    def fake_lock(path):
        from contextlib import nullcontext
        inspected.append(path.name)
        return nullcontext()

    monkeypatch.setattr(seed_memory, "_session_lock", fake_lock)
    for name in ("session_test.json", "1794384000_" + "a" * 32 + ".json",
                 "stats.json", "random.json", "unfinished.json.tmp"):
        path = tmp_path / name
        path.write_text('{"created_at": 1794384000, "messages": []}', encoding="utf-8")
        os.utime(path, (1794384000, 1794384000))
    assert {p.name for p in provider._queue_transcripts()} == {
        "session_test.json", "1794384000_" + "a" * 32 + ".json"
    }
    provider._drain_queue()
    assert set(inspected) == {"session_test.json", "1794384000_" + "a" * 32 + ".json"}


def test_gray_mode_audit_fallback_preserves_fields(seed_db, caplog, monkeypatch):
    from types import ModuleType

    agent_module = ModuleType("agent")
    provider_module = ModuleType("agent.memory_provider")
    provider_module.MemoryProvider = object
    provider_module.RecallStatus = SimpleNamespace
    monkeypatch.setitem(sys.modules, "agent", agent_module)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", provider_module)
    import seed_memory

    provider = seed_memory.SeedMemoryProvider()
    provider._store = store
    provider._initialized = True
    provider._session_id = "sess:gray"
    provider._profile = "xiaomei"
    provider._config = {"write_enabled": False}
    result = provider.handle_tool_call("seed_add", {"summary": "refused", "emotion_primary": "neutral"})
    assert '"rejection_recorded": true' in result
    with sqlite3.connect(seed_db) as conn:
        row = conn.execute(
            "SELECT operation, writer, session_id, reason FROM seed_gray_mode_rejections"
        ).fetchone()
    assert row == ("seed_add", "hermes:seed_memory:tool:sess:gray", "sess:gray", "write_disabled")
    assert "without signature" in caplog.text


def test_production_access_log_and_query_time_decay_coexist(seed_db):
    first = make_seed("older", ref="event:old", importance=0.9)
    second = make_seed("recent", ref="event:new", importance=0.6)
    second.summary = "File recent.py failed E505 on 2026-11-10"
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
    assert store._compute_effective_importance(first, datetime.now(timezone.utc)) < 0.6
    first.last_accessed = datetime.now(timezone.utc)
    assert store._compute_effective_importance(first, datetime.now(timezone.utc)) > 0.8
    assert store.get_ranked_seeds(limit=1)[0].id == second.id
    assert store.decay_due_seeds(dry_run=True)["dry_run"] is True


def test_retrieval_provenance_and_readonly_prefetch(seed_db, monkeypatch):
    seed = make_seed("retrieved", ref="event:search")
    assert store.save_seed(seed)
    monkeypatch.setattr(retrieval, "_embed_query", lambda *_: None, raising=False)
    assert retrieval.retrieve("example.py", top_k=1, record_access=False,
                              writer="tester", session_id="sess:1", reason="prefetch")
    with sqlite3.connect(seed_db) as conn:
        assert conn.execute("SELECT access_count FROM seeds WHERE id=?", (seed.id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM seed_access_log").fetchone()[0] == 0
    assert retrieval.retrieve("example.py", top_k=1, writer="tester", session_id="sess:1")
    with sqlite3.connect(seed_db) as conn:
        assert conn.execute("SELECT access_count FROM seeds WHERE id=?", (seed.id,)).fetchone()[0] == 1
        writer, session, provenance = conn.execute(
            "SELECT writer, session_id, provenance FROM seed_access_log"
        ).fetchone()
    assert writer in {"hermes:retrieval:fallback", "tester"}
    assert session == "sess:1"
    assert '"trigger":' in provenance


def test_optional_heelo_and_signed_exports_when_private_modules_present(monkeypatch):
    # These modules are production-only; the public checkout must still import.
    from hermes import memory

    assert hasattr(memory, "SeedAccessLog")
    assert "signed_write_seed" not in memory.__all__
    assert "Experience" not in memory.__all__
    fake_signed = SimpleNamespace(**{
        key: object() for key in (
            "init_signed_write_path", "signed_write_seed", "signed_write_access_log",
            "signed_write_semantic_memory", "signed_write_program_memory",
            "record_gray_mode_rejection", "get_gray_mode_rejections",
            "verify_merkle_chain", "verify_write_integrity", "export_merkle_proof",
            "get_merkle_tail", "get_public_key_pem", "SignedWriteResult", "GrayModeRejection",
        )
    })
    fake_heelo = SimpleNamespace(**{
        key: object() for key in (
            "Experience", "Observation", "Reflection", "Action", "Outcome",
            "ExecutionPrimitive", "HandoffPrimitive", "ObservabilityPrimitive",
            "serialize_heelo", "deserialize_heelo", "seed_to_experience",
            "experience_to_seed", "skill_to_execution_primitive",
        )
    })
    monkeypatch.setitem(sys.modules, "hermes.memory.signed_write", fake_signed)
    monkeypatch.setitem(sys.modules, "hermes.memory.heelo", fake_heelo)
    try:
        importlib.reload(memory)
        assert "signed_write_seed" in memory.__all__
        assert "Experience" in memory.__all__
    finally:
        monkeypatch.delitem(sys.modules, "hermes.memory.signed_write")
        monkeypatch.delitem(sys.modules, "hermes.memory.heelo")
        importlib.reload(memory)


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


def test_signed_access_failure_never_breaks_primary_touch(seed_db, monkeypatch):
    seed = make_seed("signed-failure", ref="event:signed-failure")
    assert store.save_seed(seed)

    def signing_failure(*, log, writer):
        raise RuntimeError("signing unavailable")

    monkeypatch.setattr(store, "import_module", lambda _: SimpleNamespace(
        signed_write_access_log=signing_failure
    ))
    store.touch_seed(seed.id, writer="tester", session_id="session:failure", reason="recall")
    with sqlite3.connect(seed_db) as conn:
        access_count, last_real_access = conn.execute(
            "SELECT access_count, last_real_access FROM seeds WHERE id=?", (seed.id,)
        ).fetchone()
    assert access_count == 1
    assert last_real_access is not None


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


def test_retrieval_fallback_preserves_access_context(seed_db):
    store.save_seeds([make_seed("fallback-context")])

    results = retrieval.retrieve(
        "", top_k=1, writer="caller:writer", session_id="session:fallback",
        reason="caller-reason",
    )

    assert [seed.id for seed in results] == ["fallback-context"]
    with sqlite3.connect(seed_db) as conn:
        audit = conn.execute(
            "SELECT writer, session_id, reason FROM seed_access_log"
        ).fetchone()
    assert audit == ("caller:writer", "session:fallback", "caller-reason")


def test_prefetch_records_audit_without_mutating_seed_access(seed_db, monkeypatch):
    from seed_memory import SeedMemoryProvider

    seed = make_seed("prefetch", ref="event:1")
    assert store.save_seed(seed)
    monkeypatch.setattr(retrieval, "_embed_query", lambda *_: None, raising=False)
    provider = SeedMemoryProvider()
    provider._initialized = True
    provider._store = store
    provider._retrieval = retrieval
    provider._config = {"prefetch_limit": 1}
    provider._session_id = "session:prefetch"
    assert provider.prefetch("example.py")
    with sqlite3.connect(seed_db) as conn:
        count, last_real_access = conn.execute(
            "SELECT access_count, last_real_access FROM seeds WHERE id=?", (seed.id,)
        ).fetchone()
        access_logs = conn.execute(
            "SELECT writer, session_id, reason FROM seed_access_log"
        ).fetchall()
    assert count == 0
    assert last_real_access is None
    assert access_logs == [
        ("hermes:seed_memory:prefetch", "session:prefetch", "prefetch")
    ]


def test_production_hash_dedup_ignores_ref_and_never_updates(seed_db):
    first = make_seed("first", ref="event:1", importance=0.4)
    duplicate = make_seed("different-id", ref="event:1", importance=0.9)
    distinct_ref = make_seed("distinct", ref="event:2")
    assert store.save_seed(first)
    assert not store.save_seed(duplicate)
    assert store.save_seeds([duplicate, distinct_ref]) == 0
    with sqlite3.connect(seed_db) as conn:
        rows = conn.execute("SELECT id, provenance_ref, importance FROM seeds").fetchall()
    assert rows == [("first", "event:1", 0.4)]


def test_legacy_duplicate_hash_migration_keeps_all_rows(seed_db, caplog):
    seed = make_seed("legacy-original", ref="event:legacy")
    assert store.save_seed(seed)
    with sqlite3.connect(seed_db) as conn:
        conn.execute("DROP INDEX idx_seeds_content_hash")
        conn.execute(
            "INSERT INTO seeds (id, timestamp, summary, participants, emotion_primary, "
            "emotion_intensity, importance, content_hash) SELECT ?, timestamp, summary, "
            "participants, emotion_primary, emotion_intensity, importance, content_hash "
            "FROM seeds WHERE id=?",
            ("legacy-duplicate", seed.id),
        )
    store.init_db(str(seed_db))
    with sqlite3.connect(seed_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM seeds").fetchone()[0] == 2
    assert "preserving rows without unique index" in caplog.text


def test_decay_phase1_is_report_only_and_phase2_remains_disabled(seed_db):
    store.save_seed(make_seed("phase2"))
    with pytest.raises(RuntimeError):
        store.decay_due_seeds(now=datetime(2026, 11, 29, tzinfo=timezone.utc), dry_run=False)
    report = store.decay_due_seeds(now=datetime(2026, 11, 29, tzinfo=timezone.utc), dry_run=True)
    assert report["dry_run"] is True


def test_filtered_counter_is_separate_and_revival_requires_offline_proposal(seed_db):
    store.record_filtered_by_contract("missing_ref")
    assert store.count_filtered_by_contract() == 1
    archived = make_seed("archived", ref="event:1", status="archived")
    store.save_seed(archived)
    assert len(store.get_all_seeds()) == 1
    assert store.get_all_seeds(include_archived=False) == []
    # No automatic archival isolation is enabled in the production baseline.
    assert store.decay_due_seeds(now=datetime(2026, 11, 29, tzinfo=timezone.utc))[
        "total"
    ] == 1
    with pytest.raises(ValueError):
        store.propose_archived_revival(archived.id, offline_review_id=" ")
    assert store.propose_archived_revival(archived.id, offline_review_id="review:1")
    assert len(store.get_all_seeds()) == 1
    # Repeated production-style content hash dedup cannot reactivate the row.
    assert not store.save_seed(make_seed("retry", ref="event:1", status="active"))
    assert store.get_all_seeds(include_archived=False) == []
    with sqlite3.connect(seed_db) as conn:
        assert conn.execute("SELECT status FROM seeds WHERE id=?", (archived.id,)).fetchone()[0] == "archived"
        assert conn.execute("SELECT COUNT(*) FROM seed_revival_proposals").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM seeds").fetchone()[0] == 1
