"""Seed retrieval with lazy, offline embeddings and a safe fallback."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np

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


def _fallback(limit: int) -> List[Seed]:
    seeds = get_ranked_seeds(limit)
    for seed in seeds:
        touch_seed(seed.id)
    return seeds


def retrieve(query: str, top_k: int = 5) -> List[Seed]:
    """Retrieve semantically relevant seeds or degrade to importance+recency."""
    top_k = max(1, int(top_k))
    all_seeds = get_all_seeds()
    if not all_seeds:
        return []

    query_embedding = embed(query)
    if not _valid_embedding(query_embedding):
        return _fallback(top_k)
    query_embedding = np.asarray(query_embedding, dtype=np.float32)

    now = datetime.now(timezone.utc)
    scored = []
    for seed in all_seeds:
        if seed.embedding is None:
            seed.embedding = embed(_seed_text(seed))
            if seed.embedding is not None and _valid_embedding(seed.embedding, query_embedding.size):
                update_embedding(seed.id, seed.embedding)
            else:
                seed.embedding = None
        if not _valid_embedding(seed.embedding, query_embedding.size):
            continue
        seed_embedding = np.asarray(seed.embedding, dtype=np.float32)

        semantic = float(np.dot(query_embedding, seed_embedding))
        created = seed.timestamp
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        days = max(0, (now - created).days)
        recency = 1.0 / (1.0 + days / 30.0)
        score = semantic * 0.65 + seed.importance * 0.25 + recency * 0.10
        scored.append((score, seed))

    if not scored:
        return _fallback(top_k)

    scored.sort(key=lambda item: item[0], reverse=True)
    results = [seed for _, seed in scored[:top_k]]
    for seed in results:
        touch_seed(seed.id)
    return results
