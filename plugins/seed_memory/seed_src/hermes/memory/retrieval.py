"""Seed retrieval with embedding/FTS tracks and reciprocal-rank fusion."""
# xiaoai 专用，部署时不要同步到 xiaozhi。

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import store as memory_store
from .schema import Seed
from .store import get_all_seeds, get_ranked_seeds, touch_seed, update_embedding

_DEFAULT_MODEL_PATH = (
    Path(__file__).resolve().parent.parent
    / "model"
    / "paraphrase-multilingual-MiniLM-L12-v2"
)
_model_path = Path(os.environ.get("HERMES_SEED_MODEL_PATH", _DEFAULT_MODEL_PATH))
_tokenizer = None
_model = None
_model_unavailable_reason: Optional[str] = None


def configure(model_path: str | os.PathLike[str] | None = None) -> None:
    """Configure the local embedding model and clear the lazy-load cache."""
    global _model_path, _tokenizer, _model, _model_unavailable_reason
    if model_path is not None:
        _model_path = Path(model_path).expanduser().resolve()
    _tokenizer = None
    _model = None
    _model_unavailable_reason = None


def embedding_status() -> dict:
    return {
        "available": _model is not None,
        "path": str(_model_path),
        "reason": _model_unavailable_reason,
    }


def _has_local_model(path: Path) -> bool:
    weights = list(path.glob("*.safetensors")) + list(path.glob("pytorch_model*.bin"))
    return path.is_dir() and (path / "config.json").is_file() and bool(weights)


def _load_model() -> bool:
    """Load only local files. Missing/incomplete models never trigger download."""
    global _tokenizer, _model, _model_unavailable_reason
    if _model is not None and _tokenizer is not None:
        return True
    if _model_unavailable_reason is not None:
        return False
    if not _has_local_model(_model_path):
        _model_unavailable_reason = f"local embedding model incomplete or missing: {_model_path}"
        return False
    try:
        from transformers import AutoModel, AutoTokenizer

        _tokenizer = AutoTokenizer.from_pretrained(
            str(_model_path), local_files_only=True
        )
        _model = AutoModel.from_pretrained(str(_model_path), local_files_only=True)
        _model.eval()
        return True
    except Exception as exc:  # retrieval must fail open
        _tokenizer = None
        _model = None
        _model_unavailable_reason = f"{type(exc).__name__}: {exc}"
        return False


def embed(text: str) -> Optional[np.ndarray]:
    if not _load_model():
        return None
    import torch

    inputs = _tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=512,
        padding=True,
    )
    with torch.no_grad():
        outputs = _model(**inputs)
    mask = inputs["attention_mask"].unsqueeze(-1)
    vector = (outputs.last_hidden_state * mask).sum(1) / mask.sum(1).clamp(min=1)
    array = vector[0].cpu().numpy().astype(np.float32)
    norm = np.linalg.norm(array)
    return array / norm if norm else array


def _valid_embedding(value: object, dimension: int | None = None) -> bool:
    try:
        array = np.asarray(value, dtype=np.float32)
        return (
            array.ndim == 1
            and array.size > 0
            and (dimension is None or array.size == dimension)
            and bool(np.isfinite(array).all())
        )
    except (TypeError, ValueError):
        return False


def _seed_text(seed: Seed) -> str:
    return " ".join([
        seed.summary,
        seed.emotion.primary,
        seed.emotion.note,
        " ".join(seed.seeds_planted),
    ])


def _connect_ro() -> sqlite3.Connection:
    """Open the configured seed database without permitting writes."""
    # Import here because store.configure() may replace DB_PATH after this module
    # has itself been imported.
    from .store import DB_PATH

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fts_retrieve(
    query: str, top_n: int, conn: sqlite3.Connection
) -> List[Tuple[int, Seed]]:
    """Return 1-based FTS ranks, or [] when the FTS table is unavailable."""
    terms = re.findall(r"\w+", query, flags=re.UNICODE)[:10]
    if not terms or top_n <= 0:
        return []
    # Quoting every token keeps punctuation/operators in user input from
    # changing the MATCH grammar. Double quotes are escaped for FTS5.
    match_expression = " OR ".join(
        '"' + term.replace('"', '""') + '"' for term in terms
    )
    sql = """SELECT s.*
             FROM seeds AS s
             JOIN seeds_fts ON seeds_fts.rowid = s.rowid
             WHERE seeds_fts MATCH ? AND s.status != 'archived'
             ORDER BY seeds_fts.rank
             LIMIT ?"""
    try:
        rows = conn.execute(sql, (match_expression, int(top_n))).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(rank, memory_store._row_to_seed(row)) for rank, row in enumerate(rows, 1)]


def retrieve_program_evidence(
    query: str, recalled_seeds: List[Seed], limit: int = 3
) -> List[Dict[str, Any]]:
    """Return active program memories with one positive and negative example."""
    if limit <= 0:
        return []
    try:
        # Unlike immutable seed retrieval, program memories are updated online
        # (including breaker counters), so this reader must observe the WAL.
        from .store import DB_PATH

        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(program_memories)")
        }
        # Legacy program memories predate provenance fields. They are quarantined
        # read-only: absence of a trusted writer/L0 provenance means no injection.
        required = {"source_seeds", "counter_seeds", "consecutive_failures", "writer", "owner", "provenance"}
        if not required.issubset(columns):
            return []
        rows = conn.execute(
            """SELECT * FROM program_memories
               WHERE COALESCE(consecutive_failures, 0) < 2
                 AND COALESCE(writer, '') <> ''
                 AND COALESCE(owner, '') <> ''
                 AND COALESCE(provenance, '') <> ''
               ORDER BY confidence DESC, evidence_count DESC"""
        ).fetchall()
        recalled_ids = {seed.id for seed in recalled_seeds}
        terms = [term.casefold() for term in re.findall(r"\w+", query)[:10]]
        ranked: List[Tuple[int, sqlite3.Row, List[str], List[str]]] = []
        for row in rows:
            positive_ids = [str(item) for item in memory_store._json_list(row["source_seeds"])]
            counter_ids = [str(item) for item in memory_store._json_list(row["counter_seeds"])]
            searchable = " ".join(
                str(row[key] or "")
                for key in ("domain_signature", "pattern", "trigger_context", "behavior")
                if key in columns
            ).casefold()
            score = 10 * len(recalled_ids.intersection(positive_ids + counter_ids))
            score += sum(1 for term in terms if term in searchable)
            if score:
                ranked.append((score, row, positive_ids, counter_ids))
        ranked.sort(
            key=lambda item: (-item[0], -float(item[1]["confidence"] or 0), item[1]["id"])
        )
        selected = ranked[:limit]
        evidence_ids = {
            seed_id
            for _, _, positive, counter in selected
            for seed_id in positive + counter
        }
        seed_by_id: Dict[str, Seed] = {}
        if evidence_ids:
            placeholders = ",".join("?" for _ in evidence_ids)
            seed_rows = conn.execute(
                f"SELECT * FROM seeds WHERE id IN ({placeholders}) AND status != 'archived'",
                tuple(evidence_ids),
            ).fetchall()
            seed_by_id = {
                row["id"]: memory_store._row_to_seed(row) for row in seed_rows
            }
        return [
            {
                "id": row["id"],
                "domain_signature": row["domain_signature"] or "",
                "pattern": row["pattern"] or "",
                "trigger_context": row["trigger_context"] or "",
                "behavior": row["behavior"] or "",
                "confidence": float(row["confidence"] or 0),
                "positive_seed": next(
                    (seed_by_id[item] for item in positive if item in seed_by_id), None
                ),
                "counter_seed": next(
                    (seed_by_id[item] for item in counter if item in seed_by_id), None
                ),
            }
            for _, row, positive, counter in selected
        ]
    except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
        return []
    finally:
        if "conn" in locals():
            conn.close()


def _fallback(limit: int) -> List[Seed]:
    seeds = get_ranked_seeds(limit)
    for seed in seeds:
        touch_seed(seed.id)
    return seeds


def _rerank_with_ext_hook(seeds: List[Seed], query: str) -> List[Seed]:
    """Allow a data-local hook to reorder only unprotected results."""
    if len(seeds) <= 3:
        return seeds

    hook_path = Path(memory_store.DB_PATH).parent.parent / "ext_hooks.py"
    if not hook_path.is_file():
        return seeds

    protected = seeds[:3]
    tail = seeds[3:]

    def run_hook() -> object:
        spec = importlib.util.spec_from_file_location("hermes_memory_ext_hooks", hook_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load extension hook: {hook_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rerank = getattr(module, "rerank")
        return rerank(list(tail), query)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(run_hook)
    try:
        reranked = future.result(timeout=2.0)
        if not isinstance(reranked, list):
            return seeds

        # A hook may change order, but cannot inject, duplicate, or remove seeds.
        tail_by_id = {seed.id: seed for seed in tail}
        reranked_ids = [
            seed.id
            for seed in reranked
            if isinstance(seed, Seed) and seed.id in tail_by_id
        ]
        if len(reranked_ids) != len(tail) or len(set(reranked_ids)) != len(tail):
            return seeds
        return protected + [tail_by_id[seed_id] for seed_id in reranked_ids]
    except Exception:
        return seeds
    finally:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)


def retrieve(query: str, top_k: int = 5, *, record_access: bool = True) -> List[Seed]:
    """Retrieve seeds by fusing cosine and FTS ranks with weighted RRF."""
    top_k = max(1, int(top_k))
    top_n = max(top_k * 3, 20)
    k_rrf = 10
    all_seeds = get_all_seeds()

    emb_ranked: List[Tuple[int, Seed]] = []
    query_embedding = embed(query)
    if _valid_embedding(query_embedding):
        query_embedding = np.asarray(query_embedding, dtype=np.float32)
        scored: List[Tuple[float, Seed]] = []
        for seed in all_seeds:
            if seed.embedding is None:
                seed.embedding = embed(_seed_text(seed))
                if record_access and seed.embedding is not None and _valid_embedding(
                    seed.embedding, query_embedding.size
                ):
                    update_embedding(seed.id, seed.embedding)
                else:
                    seed.embedding = None
            if not _valid_embedding(seed.embedding, query_embedding.size):
                continue
            similarity = float(
                np.dot(query_embedding, np.asarray(seed.embedding, dtype=np.float32))
            )
            scored.append((similarity, seed))
        scored.sort(key=lambda item: item[0], reverse=True)
        emb_ranked = [
            (rank, seed) for rank, (_, seed) in enumerate(scored[:top_n], 1)
        ]

    fts_ranked: List[Tuple[int, Seed]] = []
    try:
        with _connect_ro() as conn:
            fts_ranked = _fts_retrieve(query, top_n, conn)
    except (OSError, sqlite3.Error):
        fts_ranked = []

    if not emb_ranked and not fts_ranked:
        results = _fallback(top_k)
    else:
        scores: Dict[str, float] = {}
        candidates: Dict[str, Seed] = {}
        for rank, seed in emb_ranked:
            candidates[seed.id] = seed
            scores[seed.id] = scores.get(seed.id, 0.0) + 0.55 / (k_rrf + rank)
        for rank, seed in fts_ranked:
            candidates[seed.id] = seed
            scores[seed.id] = scores.get(seed.id, 0.0) + 0.45 / (k_rrf + rank)

        # Lift long-dormant seeds into the candidate pool BEFORE RRF truncation.
        # The ext_hook can only REORDER what retrieve() hands it; a seed that
        # never enters the top_n window can never be woken by the hook, no
        # matter how high it scores in ext_hooks._score().  This is the
        # caller's own pool expansion -- the hook's no-injection contract is
        # untouched; it still only reorders.
        injected = _inject_dormant_candidates(candidates, scores, all_seeds, top_k)

        ranked_ids = sorted(scores, key=lambda seed_id: (-scores[seed_id], seed_id))
        results = [candidates[seed_id] for seed_id in ranked_ids[:top_k]]
        # Injected dormant seeds carry a floor score and would be truncated
        # away here.  Reserve their slots so the injection is not a no-op:
        # they displace the lowest-scoring ordinary matches, not each other.
        if injected:
            injected_ids = {s.id for s in injected if s.id in candidates}
            # Make room first: drop the lowest-scoring ordinary results until
            # every injected seed fits.  If the pool is already smaller than
            # top_k (rare -- top_n < top_k), nothing is dropped.
            for sid in injected_ids:
                if sid in results:
                    continue
                while len(results) >= top_k:
                    ordinary = [
                        i for i, s in enumerate(results)
                        if s.id not in injected_ids
                    ]
                    if not ordinary:
                        break  # pool is all injected seeds; nothing to drop
                    results.pop(ordinary[-1])
                if len(results) < top_k:
                    results.append(candidates[sid])
    results = _rerank_with_ext_hook(results, query)
    if record_access:
        for seed in results:
            touch_seed(seed.id)
    return results


_DORMANT_INJECT_DAYS = 7.0
_DORMANT_INJECT_MAX = 5


def _dormant_seeds_from_db(
    all_seeds: List[Seed], top_k: int
) -> List[Seed]:
    """Return long-sleeping unretrieved seeds, capped and age-filtered.

    The Seed dataclass carries no last_real_access column (that lives only in
    the DB), so the age check is done here against the raw row rather than
    against the in-memory object.  Seeds already in the candidate pool are
    excluded by the caller.
    """
    if top_k <= 0:
        return []
    try:
        from .store import DB_PATH
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cutoff = datetime.now(timezone.utc) - timedelta(days=_DORMANT_INJECT_DAYS)
        rows = conn.execute(
            "SELECT id, access_count, last_real_access, timestamp, importance "
            "FROM seeds WHERE access_count = 0 AND status != 'archived' "
            "ORDER BY importance DESC, timestamp ASC"
        ).fetchall()
        conn.close()
    except (OSError, sqlite3.Error):
        return []

    now = datetime.now(timezone.utc)
    seed_by_id = {s.id: s for s in all_seeds}
    picked: List[Seed] = []
    for row in rows:
        if len(picked) >= _DORMANT_INJECT_MAX:
            break
        seed = seed_by_id.get(row["id"])
        if seed is None:
            continue
        ts = row["last_real_access"] or row["timestamp"]
        try:
            t = datetime.fromisoformat(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            age_days = (now - t).total_seconds() / 86400.0
        except Exception:
            age_days = _DORMANT_INJECT_DAYS  # unknown age: treat as eligible
        if age_days < _DORMANT_INJECT_DAYS:
            continue
        picked.append(seed)
    return picked


def _inject_dormant_candidates(
    candidates: Dict[str, Seed],
    scores: Dict[str, float],
    all_seeds: List[Seed],
    top_k: int,
) -> List[Seed]:
    """Add long-sleeping seeds to the RRF candidate pool so the ext_hook can reach them.

    Seeds already in the pool are left alone.  Only seeds that have never been
    retrieved (access_count == 0) AND have been sleeping for at least
    _DORMANT_INJECT_DAYS are added, with a floor score so they sit below genuine
    matches but above noise.  The cap keeps the pool bounded.

    Returns the seeds that were actually injected so the caller can reserve
    their slots in the final top_k truncation.
    """
    if top_k <= 0:
        return []
    floor = -float(top_k) - 1.0
    injected: List[Seed] = []
    for seed in _dormant_seeds_from_db(all_seeds, top_k):
        if seed.id in candidates:
            continue
        candidates[seed.id] = seed
        scores[seed.id] = floor
        injected.append(seed)
    return injected
