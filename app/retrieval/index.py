"""Hybrid retrieval: BM25 (exact terms, numbers, names) + dense embeddings (paraphrases),
merged with Reciprocal Rank Fusion. The corpus is ~130 chunks, so everything lives in memory."""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

log = logging.getLogger(__name__)

RRF_K = 60
_STOP = set("a an the of in on at to for and or is are was be by with from what which how much does do i my me it this that".split())

_embedder = None
_embedder_lock = threading.Lock()


def get_embedder(model_name: str, cache_dir: Path):
    """Load the ONNX embedding model once per process (~130 MB, CPU only)."""
    global _embedder
    with _embedder_lock:
        if _embedder is None:
            from fastembed import TextEmbedding
            _embedder = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir))
        return _embedder


def tokenize(text: str) -> list[str]:
    text = text.lower().replace(",", "")  # "18,995" -> "18995" so prices match either way
    return [t for t in re.findall(r"[a-z0-9$]+", text) if t not in _STOP]


@dataclass
class Hit:
    chunk: dict
    score: float       # fused RRF score
    dense_sim: float   # cosine similarity, used as the "is anything relevant?" signal


class KnowledgeIndex:
    def __init__(self, chunks: list[dict], vectors: np.ndarray, embedder):
        self.chunks = chunks
        self.vectors = vectors
        self.embedder = embedder
        self.bm25 = BM25Okapi([tokenize(c["text"]) for c in chunks])

    # ---------- persistence ----------
    @staticmethod
    def embed_passages(embedder, texts: list[str]) -> np.ndarray:
        vecs = np.array(list(embedder.passage_embed(texts)), dtype=np.float32)
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)

    @classmethod
    def load(cls, data_dir: Path, embedder) -> "KnowledgeIndex":
        chunks = json.loads((data_dir / "chunks.json").read_text(encoding="utf-8"))
        vectors = np.load(data_dir / "embeddings.npy")
        if len(chunks) != len(vectors):
            raise ValueError("chunks.json and embeddings.npy are out of sync; re-run ingestion")
        return cls(chunks, vectors, embedder)

    # ---------- search ----------
    def search(self, queries: str | list[str], k: int = 6) -> list[Hit]:
        """RRF over (dense, BM25) x every query variant, e.g. the rewritten query and the user's own words."""
        queries = [queries] if isinstance(queries, str) else [q for q in dict.fromkeys(queries) if q.strip()]
        qvs = np.array(list(self.embedder.query_embed(queries)), dtype=np.float32)
        qvs /= np.linalg.norm(qvs, axis=1, keepdims=True)
        dense_all = self.vectors @ qvs.T  # (chunks, queries)

        fused = np.zeros(len(self.chunks))
        for j, query in enumerate(queries):
            for scores in (dense_all[:, j], self.bm25.get_scores(tokenize(query))):
                order = np.argsort(-scores)
                ranks = np.empty_like(order)
                ranks[order] = np.arange(len(order))
                fused += 1.0 / (RRF_K + ranks + 1)

        best_dense = dense_all.max(axis=1)
        top = np.argsort(-fused)[:k]
        return [Hit(self.chunks[i], float(fused[i]), float(best_dense[i])) for i in top]
