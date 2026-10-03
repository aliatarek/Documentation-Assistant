"""Small in-memory BM25 lexical index."""
from __future__ import annotations

from difflib import get_close_matches

from rank_bm25 import BM25Okapi

from .search_records import tokenize


class LexicalIndex:
    def __init__(self):
        self.ids: list[str] = []
        self.index = None
        self.vocabulary: set[str] = set()

    def rebuild(self, entries: list[dict]) -> None:
        self.ids = [entry['source_id'] for entry in entries]
        documents = [tokenize(entry['text']) for entry in entries]
        self.vocabulary = {token for document in documents for token in document}
        self.index = BM25Okapi(documents) if entries else None

    def expanded_query_tokens(self, query: str) -> list[str]:
        """Preserve query tokens and add close catalog words for ordinary typos.

        This is deliberately conservative: exact matches are untouched, short
        tokens (for example, CIF or ISIC) are never guessed, and the original
        user wording remains in the query. It improves lexical recall without
        claiming that a typo correction is a documentation fact.
        """
        tokens = tokenize(query)
        expanded = list(tokens)
        for token in tokens:
            if token in self.vocabulary or len(token) < 5:
                continue
            match = get_close_matches(token, self.vocabulary, n=1, cutoff=0.82)
            if match and match[0] not in expanded:
                expanded.append(match[0])
        return expanded

    def search(self, query: str, k: int = 24) -> list[tuple[str, int]]:
        if not self.index:
            return []
        scores = self.index.get_scores(self.expanded_query_tokens(query))
        order = sorted(range(len(self.ids)), key=lambda index: (-scores[index], self.ids[index]))
        return [(self.ids[index], rank) for rank, index in enumerate(order[:k], 1) if scores[index] > 0]
