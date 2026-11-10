import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "seed_memory" / "seed_src"))
from hermes.memory.memory_dream import (
    causal_completion, ensure_candidate_table, organisation_diagnostics,
    semantic_review, should_dream, store_candidates, validate_edge,
)


def seed(i, day, summary, **kw):
    return SimpleNamespace(id=i, timestamp=f"2025-01-{day:02d}T00:00:00+00:00", summary=summary,
                           caused_by=kw.get("caused_by", []), led_to=kw.get("led_to", []),
                           access_count=kw.get("access_count", 0))


def test_candidates_are_isolated_from_active_tables():
    conn = sqlite3.connect(":memory:")
    conn.execute("create table seeds (id text primary key)")
    conn.execute("create table seed_edges (from_id text, to_id text)")
    receipt = causal_completion([seed("a", 1, "first"), seed("b", 2, "second")])
    assert store_candidates(conn, receipt) == 1
    assert conn.execute("select count(*) from dream_candidates").fetchone()[0] == 1
    assert conn.execute("select count(*) from seed_edges").fetchone()[0] == 0


def test_empty_state_does_not_spin():
    assert should_dream(in_window=True, new_seed_count=2, backlog=0) == (False, "dream_skipped_empty")
    assert should_dream(in_window=True, new_seed_count=3, backlog=20) == (False, "backlog_full")
    assert should_dream(in_window=False, new_seed_count=3, backlog=0) == (False, "outside_window")


def test_edge_validation_rejects_same_batch_and_bad_order():
    rows = [seed("a", 2, "later"), seed("b", 1, "earlier")]
    bad = {"from_id": "a", "to_id": "b", "edge_type": "led_to", "from_quote": "a", "to_quote": "b"}
    assert validate_edge(bad, rows)["valid"] is False
    assert len(causal_completion([seed("a", 1, "a"), seed("b", 2, "b")], {"a", "b"}).candidates) == 0


def test_semantic_review_flags_duplicate_and_missing_source():
    receipt = semantic_review([SimpleNamespace(id="1", statement="Same claim", source_seeds=[]),
                               SimpleNamespace(id="2", statement=" same   claim ", source_seeds=["s"])])
    flags = [f.payload["flag"] for f in receipt.flags]
    assert "no_source" in flags and "duplicate" in flags


def test_diagnostics_are_counts_only():
    receipt = organisation_diagnostics([seed("a", 1, "x")], [])
    assert receipt.diagnostics["zero_edge_seeds"] == 1
    assert receipt.diagnostics["expired_claims"] == 0
