"""Reciprocal-rank fusion for independent lexical and semantic rankings."""
from __future__ import annotations


def rrf(lexical: list[tuple[str, int]], semantic: list[tuple[str, int]], k_rrf: int = 60) -> list[tuple[str, float]]:
    scores = {}
    for ranking in (lexical, semantic):
        for identifier, rank in ranking:
            scores[identifier] = scores.get(identifier, 0.0) + 1 / (k_rrf + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class HybridSearch:
    def __init__(self, lexical, vector, embedder, k_rrf: int = 60):
        self.lexical, self.vector, self.embedder, self.k_rrf = lexical, vector, embedder, k_rrf

    def search(self, query: str, aliases: dict[str, list[str]], k: int = 24) -> tuple[list[str], dict]:
        lexical = self.lexical.search(query, k)
        semantic_error = None
        try:
            semantic = self.vector.search(self.embedder.embed([query])[0], k)
        except RuntimeError as exc:
            # Keep the documentation assistant usable before the optional local
            # embedding model has been installed.
            semantic, semantic_error = [], str(exc)
        fused = rrf(lexical, semantic, self.k_rrf)
        normalized = ' '.join(query.casefold().split())
        pinned = sorted(identifier for identifier, values in aliases.items()
                        if normalized and normalized in {' '.join(value.casefold().split()) for value in values})
        ordered = pinned + [identifier for identifier, _ in fused if identifier not in pinned]
        detail = {'lexical': lexical, 'semantic': semantic, 'fused': fused,
                  'pinned_aliases': pinned, 'semantic_error': semantic_error}
        return ordered[:k], detail
