"""Synchronize full BM25 and incremental persistent vector indexes."""
from __future__ import annotations

from .embedding_change_tracking import diff, hashes
from .search_records import entries


class Reindexer:
    def __init__(self, lexical, vector, embedder):
        self.lexical, self.vector, self.embedder = lexical, vector, embedder

    def reindex(self, snapshot: dict, aliases: dict[str, list[str]] | None = None) -> tuple[list[dict], tuple[set[str], set[str], set[str]]]:
        current_entries = entries(snapshot, aliases)
        current_hashes = hashes(current_entries)
        vector_entries, vector_hashes = current_entries, current_hashes
        added, changed, removed = diff(self.vector.hashes, current_hashes)
        needing_vectors = [entry for entry in current_entries if entry['source_id'] in added | changed]
        embedded = {}
        if needing_vectors:
            try:
                vectors = self.embedder.embed([entry['text'] for entry in needing_vectors])
                embedded = {entry['source_id']: vector for entry, vector in zip(needing_vectors, vectors)}
            except RuntimeError:
                # Preserve existing vectors, if any. New/changed entries remain
                # searchable through BM25 until BGE-M3 is installed.
                existing_ids = set(self.vector.ids)
                vector_entries = [entry for entry in current_entries if entry['source_id'] in existing_ids]
                vector_hashes = {identifier: value for identifier, value in current_hashes.items()
                                 if identifier in existing_ids}
        self.vector.update(vector_entries, embedded, vector_hashes)
        self.lexical.rebuild(current_entries)
        return current_entries, (added, changed, removed)
