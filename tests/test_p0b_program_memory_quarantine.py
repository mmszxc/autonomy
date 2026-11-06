import sys
from pathlib import Path

import pytest

from plugins.autonomy import policy_reflect

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "seed_memory" / "seed_src"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for name in [m for m in sys.modules if m == "hermes" or m.startswith("hermes.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(SOURCE_ROOT))
    from hermes.memory import retrieval, store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "memory.db")
    store.init_db()
    yield store, retrieval


def test_legacy_program_memory_without_provenance_is_not_retrieved(env):
    store, retrieval = env
    with store._connect() as conn:
        conn.execute("""INSERT INTO program_memories
            (id, timestamp, domain_signature, pattern, trigger_context, behavior, confidence,
             evidence_count, source_seeds, counter_seeds, consecutive_failures,
             writer, owner, provenance)
            VALUES ('legacy', '2026-01-01T00:00:00', 'ops', 'deploy safely', 'deploy', 'unsafe legacy',
                    .9, 4, '[]', '[]', 0, '', '', '')""")
    assert retrieval.retrieve_program_evidence("deploy", [], limit=3) == []


def test_legacy_program_memory_without_provenance_is_not_used_by_reflection(env, monkeypatch, tmp_path):
    store, _retrieval = env
    with store._connect() as conn:
        conn.execute("""INSERT INTO program_memories
            (id, timestamp, domain_signature, pattern, trigger_context, behavior, confidence,
             evidence_count, source_seeds, counter_seeds, consecutive_failures,
             writer, owner, provenance)
            VALUES ('legacy-reflect', '2026-01-01T00:00:00', 'ops', 'deploy safely', 'deploy',
                    'untrusted legacy behavior', .9, 4, '[]', '[]', 0, '', '', '')""")
    monkeypatch.setattr(policy_reflect.P, "_open_ro", lambda _path: store._connect())
    assert policy_reflect.find_program_memory(tmp_path, ["deploy"]) is None
