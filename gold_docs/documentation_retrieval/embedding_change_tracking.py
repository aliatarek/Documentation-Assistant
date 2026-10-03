"""Per-entry hashes for incremental vector-index updates."""
from __future__ import annotations

from .search_records import content_hash


def hashes(entries: list[dict]) -> dict[str, str]:
    return {entry['source_id']: content_hash(entry) for entry in entries}


def diff(previous: dict[str, str], current: dict[str, str]) -> tuple[set[str], set[str], set[str]]:
    added = current.keys() - previous.keys()
    removed = previous.keys() - current.keys()
    changed = {key for key in current.keys() & previous.keys() if current[key] != previous[key]}
    return set(added), changed, set(removed)
