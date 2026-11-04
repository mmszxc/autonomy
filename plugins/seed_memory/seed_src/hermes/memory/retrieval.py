"""Seed retrieval with embedding/FTS tracks and reciprocal-rank fusion."""

from __future__ import annotations

import concurrent.futures
import importlib.util
import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
             WHERE seeds_fts MATCH ?
             ORDER BY seeds_fts.rank
             LIMIT ?"""
    try:
        rows = conn.execute(sql, (match_expression, int(top_n))).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(rank, memory_store._row_to_seed(row)) for rank, row in enumerate(rows, 1)]


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


def retrieve(query: str, top_k: int = 5) -> List[Seed]:
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
                if seed.embedding is not None and _valid_embedding(
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

        ranked_ids = sorted(scores, key=lambda seed_id: (-scores[seed_id], seed_id))
        results = [candidates[seed_id] for seed_id in ranked_ids[:top_k]]
        for seed in results:
            touch_seed(seed.id)

    return _rerank_with_ext_hook(results, query)
