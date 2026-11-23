"""Tiny local vector store: embeddings cached in SQLite, cosine search in numpy.

With ~50 facts and ~15 Q&A entries a vector database adds nothing but install weight, so this keeps
everything in data/vectors.db. Each vector is keyed by (model, sha1 of text), so changed text re-embeds.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from . import llm, sqldb
from .config import DATA


class Store:
    def __init__(self, cfg: dict, path: Path | None = None):
        self.cfg = cfg
        self.model = llm.model(cfg, "embed")
        self.path = path or DATA / "vectors.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqldb.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.execute("CREATE TABLE IF NOT EXISTS vec (model TEXT, key TEXT, dim INTEGER, v BLOB, "
                          "PRIMARY KEY (model, key))")

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def vectors(self, texts: list[str]) -> np.ndarray:
        keys = [self._key(t) for t in texts]
        have: dict[str, np.ndarray] = {}
        for k in set(keys):
            row = self.conn.execute("SELECT v FROM vec WHERE model=? AND key=?", (self.model, k)).fetchone()
            if row:
                have[k] = np.frombuffer(row[0], dtype=np.float32)
        todo = [(k, t) for k, t in zip(keys, texts) if k not in have]
        seen: set[str] = set()
        todo = [(k, t) for k, t in todo if not (k in seen or seen.add(k))]
        for i in range(0, len(todo), 32):
            chunk = todo[i:i + 32]
            embs = llm.embed(self.cfg, [t for _, t in chunk])
            for (k, _), e in zip(chunk, embs):
                v = np.asarray(e, dtype=np.float32)
                v = v / (np.linalg.norm(v) or 1.0)
                have[k] = v
            with self.conn:  # commit each chunk: never hold the write lock while waiting for the next embeddings
                self.conn.executemany("INSERT OR REPLACE INTO vec VALUES (?,?,?,?)",
                                      [(self.model, k, len(have[k]), have[k].tobytes()) for k, _ in chunk])
        return np.vstack([have[k] for k in keys]) if keys else np.zeros((0, 1), dtype=np.float32)

    def search(self, queries: list[str], docs: list[str], k: int = 3) -> list[list[tuple[int, float]]]:
        """For each query, the top-k (doc index, cosine) pairs."""
        if not queries or not docs:
            return [[] for _ in queries]
        q, d = self.vectors(queries), self.vectors(docs)
        sims = q @ d.T
        out = []
        for row in sims:
            idx = np.argsort(-row)[:k]
            out.append([(int(i), float(row[i])) for i in idx])
        return out
