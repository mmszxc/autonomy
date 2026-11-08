"""Versioned SQLite schema for P0 evidence and memory contracts."""
from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 1

BASE_DDL = """
CREATE TABLE IF NOT EXISTS goals(id TEXT PRIMARY KEY,title TEXT NOT NULL,description TEXT NOT NULL,status TEXT NOT NULL,risk TEXT NOT NULL DEFAULT 'low',budget_calls INTEGER NOT NULL DEFAULT 24,used_calls INTEGER NOT NULL DEFAULT 0,budget_seconds REAL NOT NULL DEFAULT 300,stop_reason TEXT,created_by TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,goal_id TEXT NOT NULL,parent_id TEXT,title TEXT,status TEXT,risk TEXT,created_at REAL);
CREATE TABLE IF NOT EXISTS approvals(approval_id TEXT PRIMARY KEY,goal_id TEXT,tool TEXT,args_hash TEXT,args_len INTEGER,side_effects TEXT,budget TEXT,expires_at REAL,nonce_hash TEXT,state TEXT NOT NULL DEFAULT 'pending',decided_at REAL);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,ts REAL,run_id TEXT,goal_id TEXT,kind TEXT,tool TEXT,approval_id TEXT,args_hash TEXT,result_hash TEXT,exit_reason TEXT,args_len INTEGER,result_len INTEGER,summary TEXT);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT,updated_at REAL);
CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY,calls INTEGER NOT NULL DEFAULT 0,tokens INTEGER NOT NULL DEFAULT 0,prompt INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS circuit(id INTEGER PRIMARY KEY CHECK(id=1),failures INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'closed',blocked INTEGER NOT NULL DEFAULT 0,opened_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS policy_events(id INTEGER PRIMARY KEY,ts TEXT,profile TEXT,rule_id TEXT,kind TEXT,reason TEXT,payload_json TEXT);
CREATE TABLE IF NOT EXISTS last_tick(id INTEGER PRIMARY KEY CHECK(id=1),status TEXT,started_at REAL,finished_at REAL,summary TEXT);
"""

P0_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL, applied_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS receipts(
 id TEXT PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL, payload_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('unverified','verified','mismatched','expired')),
 run_id TEXT, goal_id TEXT, payload_json TEXT
);
CREATE TABLE IF NOT EXISTS evidence(
 id INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL REFERENCES receipts(id),
 stream TEXT NOT NULL, digest TEXT NOT NULL, byte_count INTEGER NOT NULL,
 head BLOB NOT NULL DEFAULT X'', tail BLOB NOT NULL DEFAULT X'', buffer_path TEXT,
 truncated INTEGER NOT NULL DEFAULT 0, exit_code INTEGER, signal INTEGER,
 timed_out INTEGER NOT NULL DEFAULT 0, oom INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL CHECK(status IN ('unverified','verified','mismatched','expired')),
 created_at REAL NOT NULL, verified_at REAL, challenge_deadline REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_status_deadline ON evidence(status,challenge_deadline);
CREATE TABLE IF NOT EXISTS evidence_challenges(
 evidence_id INTEGER PRIMARY KEY REFERENCES evidence(id), challenged_at REAL NOT NULL,
 deadline REAL NOT NULL, status TEXT NOT NULL CHECK(status IN ('verified','mismatched','unavailable','overdue')),
 observed_digest TEXT, observed_bytes INTEGER, error TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_challenges_status ON evidence_challenges(status);
CREATE TABLE IF NOT EXISTS claims(
 id TEXT PRIMARY KEY, claim TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL,
 protected INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'hot', provenance_json TEXT NOT NULL,
 counterexamples_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tombstones(
 id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id TEXT NOT NULL, reason TEXT NOT NULL,
 prior_status TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantines(
 id TEXT PRIMARY KEY, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, reason TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending_review', expires_at REAL NOT NULL, review_count INTEGER NOT NULL DEFAULT 0,
 created_at REAL NOT NULL, reviewed_at REAL
);
CREATE TABLE IF NOT EXISTS experiments(
 id TEXT PRIMARY KEY, goal_id TEXT, goal_class TEXT NOT NULL, arm TEXT NOT NULL,
 prompt_hash TEXT NOT NULL, memory_ids_json TEXT NOT NULL DEFAULT '[]', assigned_at REAL NOT NULL,
 outcome_json TEXT, completed_at REAL
);
CREATE TABLE IF NOT EXISTS baselines(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, version TEXT NOT NULL, metrics_json TEXT NOT NULL,
 frozen INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints(
 id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, row_count INTEGER NOT NULL,
 last_evidence_id INTEGER NOT NULL, root_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_ledger(
 id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, week TEXT NOT NULL, day TEXT NOT NULL,
 bucket TEXT NOT NULL, units REAL NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TRIGGER IF NOT EXISTS receipts_no_update BEFORE UPDATE ON receipts BEGIN SELECT RAISE(ABORT,'immutable receipt'); END;
CREATE TRIGGER IF NOT EXISTS receipts_no_delete BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT,'immutable receipt'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence WHEN
 NEW.receipt_id != OLD.receipt_id OR NEW.stream != OLD.stream OR NEW.digest != OLD.digest OR
 NEW.byte_count != OLD.byte_count OR NEW.head != OLD.head OR NEW.tail != OLD.tail OR
 COALESCE(NEW.buffer_path,'') != COALESCE(OLD.buffer_path,'') OR NEW.truncated != OLD.truncated OR
 COALESCE(NEW.exit_code,-999999) != COALESCE(OLD.exit_code,-999999) OR
 COALESCE(NEW.signal,-999999) != COALESCE(OLD.signal,-999999) OR
 NEW.timed_out != OLD.timed_out OR NEW.oom != OLD.oom OR NEW.created_at != OLD.created_at OR
 NEW.challenge_deadline != OLD.challenge_deadline
 BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
"""

P0_OBJECTS = (
    "receipts", "evidence", "claims", "tombstones", "quarantines", "experiments",
    "baselines", "checkpoints", "budget_ledger", "evidence_challenges", "schema_meta",
)


def migrate(conn: sqlite3.Connection) -> int:
    """Idempotently migrate a connection to the current schema."""
    conn.executescript(BASE_DDL)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    if "summary" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN summary TEXT")
    conn.executescript(P0_DDL)
    row = conn.execute("SELECT version FROM schema_meta ORDER BY applied_at DESC LIMIT 1").fetchone()
    if row is None or int(row[0]) != SCHEMA_VERSION:
        import time
        conn.execute("INSERT INTO schema_meta(version,applied_at) VALUES(?,?)", (SCHEMA_VERSION, time.time()))
    conn.execute("INSERT OR IGNORE INTO circuit(id) VALUES(1)")
    conn.commit()
    return SCHEMA_VERSION


def downgrade(conn: sqlite3.Connection, target: int = 0) -> int:
    """Remove only P0-owned objects; legacy autonomy tables are preserved."""
    if target != 0:
        raise ValueError("only downgrade target 0 is supported")
    conn.executescript("""
    DROP TRIGGER IF EXISTS evidence_no_delete;
    DROP TRIGGER IF EXISTS evidence_no_update;
    DROP TRIGGER IF EXISTS receipts_no_delete;
    DROP TRIGGER IF EXISTS receipts_no_update;
    DROP TABLE IF EXISTS evidence_challenges;
    DROP TABLE IF EXISTS budget_ledger;
    DROP TABLE IF EXISTS checkpoints;
    DROP TABLE IF EXISTS baselines;
    DROP TABLE IF EXISTS experiments;
    DROP TABLE IF EXISTS quarantines;
    DROP TABLE IF EXISTS tombstones;
    DROP TABLE IF EXISTS claims;
    DROP TABLE IF EXISTS evidence;
    DROP TABLE IF EXISTS receipts;
    DROP TABLE IF EXISTS schema_meta;
    """)
    conn.commit()
    return target
