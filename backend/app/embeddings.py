"""Embedding backends.

* fastembed (default): BAAI/bge-small-en-v1.5 run locally via ONNX. Document text is
  NOT sent to any external embedding provider. The model weights are downloaded once
  from Hugging Face into MODEL_CACHE_DIR.
* hashing: dependency-free fallback using hashed word and character n-grams. It is a
  lexical similarity, NOT a semantic embedding; it exists only so the app can run when
  the model cannot be downloaded. The /api/health endpoint reports which is active.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading

import numpy as np

from .config import settings

log = logging.getLogger(__name__)

# bge models recommend this prefix for retrieval queries.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class HashingEmbedder:
    name = "hashing-ngram-512 (lexical fallback, not semantic)"
    dim = 512

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        t = text.lower()
        feats = re.findall(r"[a-z0-9]+", t)
        feats += [t[i:i + 4] for i in range(max(0, len(t) - 3))]
        for f in feats:
            h = int(hashlib.md5(f.encode()).hexdigest()[:8], 16)
            v[h % self.dim] += 1.0 if (h >> 9) & 1 else -1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return np.vstack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


class FastEmbedder:
    def __init__(self, model_name: str):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name, cache_dir=str(settings.model_cache_dir))
        self.name = f"fastembed:{model_name} (local)"
        self.dim = int(next(iter(self._model.embed(["dimension probe"]))).shape[0])
        self._is_bge = "bge" in model_name.lower()

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), np.float32)
        arr = np.array(list(self._model.embed(texts, batch_size=32)), dtype=np.float32)
        return arr / np.linalg.norm(arr, axis=1, keepdims=True).clip(min=1e-9)

    def embed_query(self, text: str) -> np.ndarray:
        q = (BGE_QUERY_PREFIX + text) if self._is_bge else text
        v = np.array(next(iter(self._model.embed([q]))), dtype=np.float32)
        return v / max(float(np.linalg.norm(v)), 1e-9)


_embedder = None
_lock = threading.Lock()


def get_embedder():
    global _embedder
    with _lock:
        if _embedder is None:
            if settings.embedding_provider == "fastembed":
                try:
                    _embedder = FastEmbedder(settings.embedding_model)
                except Exception as exc:  # model download blocked, package missing, ...
                    log.warning("fastembed unavailable (%s); falling back to hashing embedder", type(exc).__name__)
                    _embedder = HashingEmbedder()
            else:
                _embedder = HashingEmbedder()
        return _embedder
