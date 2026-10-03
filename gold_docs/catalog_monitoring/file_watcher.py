"""Single-threaded YAML/SQL catalog polling watcher."""
from __future__ import annotations

import time
from pathlib import Path

from ..catalog import Catalog

class CatalogWatcher:
    """Single-threaded polling watcher run by HTTPServer.service_actions()."""
    def __init__(self, catalog: Catalog, roots: dict[str, Path], interval: float):
        self.catalog = catalog
        self.roots = roots
        self.interval = interval
        self._next_check = 0.0
        self._fingerprint = self.fingerprint()
        self.last_result = 'Initial catalog loaded'
        self.last_error = None

    def fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        files = []
        for layer, root in self.roots.items():
            for path in root.rglob('*'):
                if path.suffix not in {'.sql', '.yml', '.yaml'} or '__MACOSX' in path.parts:
                    continue
                # Normal editor saves change mtime. This avoids reading every file's
                # contents on every watcher tick; SHA-256 is used after reload for
                # catalog/entry versions, not as the continuous watcher mechanism.
                stat = path.stat()
                files.append((f'{layer}/{path.relative_to(root).as_posix()}', stat.st_mtime_ns, stat.st_size))
        return tuple(sorted(files))

    def check(self, force: bool = False) -> bool:
        if self.interval <= 0:
            return False
        now = time.monotonic()
        if not force and now < self._next_check:
            return False
        self._next_check = now + self.interval
        current = self.fingerprint()
        if current == self._fingerprint:
            return False
        # Record this filesystem state before parsing. An invalid save triggers one
        # failed reload; fixing the file changes the fingerprint and retries it.
        self._fingerprint = current
        try:
            version = self.catalog.refresh()['version']
            self.last_result, self.last_error = f'Automatically reloaded version {version}', None
            return True
        except Exception as exc:
            self.last_error = f'Automatic reload failed; previous catalog retained. {exc}'
            return False

    def status(self) -> dict:
        return {'enabled': self.interval > 0, 'interval_seconds': self.interval,
                'last_result': self.last_result, 'last_error': self.last_error}
