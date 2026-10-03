"""One locally loaded multilingual BGE-M3 sentence-transformer."""
from __future__ import annotations

import numpy as np


class Embedder:
    def __init__(self, model_name: str = 'BAAI/bge-m3', batch_size: int = 16):
        self.model_name, self.batch_size, self._model = model_name, batch_size, None

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError('Install sentence-transformers to enable BGE-M3 semantic retrieval.') from exc
            # Do not make a browser question wait for Hugging Face network retries.
            # The model must be downloaded deliberately during setup; hybrid search
            # falls back to BM25 if it is not available in the local cache yet.
            try:
                self._model = SentenceTransformer(self.model_name, local_files_only=True)
            except OSError as exc:
                raise RuntimeError(
                    'BGE-M3 is not installed locally. Semantic retrieval is unavailable; '
                    'the assistant is using BM25 keyword retrieval until it is installed.'
                ) from exc
        return self._model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                  show_progress_bar=False).astype('float32')
