"""Flat, persistent NumPy cosine-similarity index for a small catalog."""
from __future__ import annotations

from pathlib import Path
import numpy as np


class VectorIndex:
    def __init__(self, path: Path):
        self.path, self.ids, self.vectors, self.hashes = path, [], np.empty((0, 0), dtype='float32'), {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        data = np.load(self.path, allow_pickle=False)
        self.ids = data['ids'].astype(str).tolist()
        self.vectors = data['vectors'].astype('float32')
        self.hashes = dict(zip(self.ids, data['hashes'].astype(str).tolist()))

    def update(self, entries: list[dict], embeddings: dict[str, np.ndarray], hashes: dict[str, str]) -> None:
        existing = {identifier: vector for identifier, vector in zip(self.ids, self.vectors)}
        existing.update(embeddings)
        ordered = [entry['source_id'] for entry in entries]
        self.ids = ordered
        self.vectors = np.vstack([existing[identifier] for identifier in ordered]).astype('float32') if ordered else np.empty((0, 0), dtype='float32')
        self.hashes = hashes
        self.path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(self.path, ids=np.asarray(self.ids), vectors=self.vectors,
                            hashes=np.asarray([hashes[identifier] for identifier in self.ids]))

    def search(self, query_vector: np.ndarray, k: int = 24) -> list[tuple[str, int]]:
        if not self.ids:
            return []
        # Embedder normalizes vectors, so dot product is cosine similarity.
        scores = self.vectors @ query_vector
        order = sorted(range(len(self.ids)), key=lambda index: (-scores[index], self.ids[index]))
        return [(self.ids[index], rank) for rank, index in enumerate(order[:k], 1)]
