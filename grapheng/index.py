"""Local sqlite index for expensive per-reference projections.

``agent-os center`` used to re-read every task's JSON on every invocation:
each job handler discovers all references it has ever created, then inspects
and describes each one before the result is sorted and truncated to the display
limit. That is O(tasks) file reads per call, and it grows without bound.

This module memoizes those projections in a sqlite database (stdlib, so the
zero-dependency constraint holds). A cached entry is reused only while the
fingerprint of its source files - path, size and modification time - is
unchanged, so a stale projection can never be served for a task that moved on.

The index is a cache, never a source of truth. Every sqlite failure is
swallowed and falls back to recomputation, because a corrupt cache must not be
able to break a task listing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Tuple

from . import telemetry
from ._store import sha256_hex

PROJECTION_INDEX_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projections (
    scope TEXT NOT NULL,
    reference TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    payload TEXT NOT NULL,
    observed_at REAL NOT NULL,
    PRIMARY KEY (scope, reference)
);
CREATE INDEX IF NOT EXISTS projections_observed
    ON projections (observed_at);
"""


def fingerprint(sources: Sequence[Path]) -> Optional[str]:
    """Return a digest of the source files backing one projection.

    A declared source that does not exist yet is fingerprinted as absent rather
    than disabling the cache. Task state is written incrementally - ``task.json``
    first, ``status.json`` and ``report.json`` later - so bailing out on the
    first missing file would mean never caching anything in practice. Creating
    the file changes the digest, which invalidates the entry.

    ``None`` means the projection is not cacheable: no sources were declared, or
    a source could not be stat-ed for a reason other than being absent.
    """

    if not sources:
        return None
    parts = []
    for source in sorted(sources, key=str):
        try:
            stat = source.stat()
        except FileNotFoundError:
            parts.append(f"{source}:absent")
            continue
        except OSError:
            return None
        parts.append(f"{source}:{stat.st_size}:{stat.st_mtime_ns}")
    return sha256_hex("\n".join(parts).encode("utf-8"))


class ProjectionIndex:
    """Fingerprint-validated cache of JSON-serializable projections."""

    def __init__(self, path: Path, clock: Optional[Callable[[], float]] = None):
        self.path = Path(path)
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._connection: Optional[sqlite3.Connection] = None
        self.hits = 0
        self.misses = 0
        self.bypasses = 0

    def _connect(self) -> Optional[sqlite3.Connection]:
        if self._connection is not None:
            return self._connection
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self.path), timeout=5.0, check_same_thread=False
            )
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(_SCHEMA)
            connection.commit()
        except (sqlite3.Error, OSError) as error:
            telemetry.emit_failure("index.unavailable", detail=str(error))
            return None
        self._connection = connection
        return connection

    def resolve(
        self,
        scope: str,
        reference: str,
        sources: Sequence[Path],
        compute: Callable[[], Any],
    ) -> Any:
        """Return the cached projection, or compute and cache a fresh one."""

        digest = fingerprint(sources)
        if digest is None:
            self.bypasses += 1
            return compute()
        cached = self._read(scope, reference, digest)
        if cached is not None:
            self.hits += 1
            return cached
        value = compute()
        self.misses += 1
        self._write(scope, reference, digest, value)
        return value

    def _read(self, scope: str, reference: str, digest: str) -> Any:
        connection = self._connect()
        if connection is None:
            return None
        try:
            with self._lock:
                row = connection.execute(
                    "SELECT payload FROM projections "
                    "WHERE scope = ? AND reference = ? AND fingerprint = ?",
                    (scope, reference, digest),
                ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

    def _write(self, scope: str, reference: str, digest: str, value: Any) -> None:
        connection = self._connect()
        if connection is None:
            return
        try:
            payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            # Not every projection is JSON-serializable; those simply stay
            # uncached instead of failing the caller.
            return
        try:
            with self._lock:
                connection.execute(
                    "INSERT INTO projections "
                    "(scope, reference, fingerprint, payload, observed_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(scope, reference) DO UPDATE SET "
                    "fingerprint = excluded.fingerprint, "
                    "payload = excluded.payload, "
                    "observed_at = excluded.observed_at",
                    (scope, reference, digest, payload, self._clock()),
                )
                connection.commit()
        except sqlite3.Error as error:
            telemetry.emit_failure("index.write_failed", scope=scope, detail=str(error))

    def forget(self, scope: str, reference: str) -> None:
        """Drop one cached projection."""

        connection = self._connect()
        if connection is None:
            return
        try:
            with self._lock:
                connection.execute(
                    "DELETE FROM projections WHERE scope = ? AND reference = ?",
                    (scope, reference),
                )
                connection.commit()
        except sqlite3.Error:
            return

    def prune(self, scope: str, keep: Iterable[str]) -> int:
        """Remove cached projections for references that no longer exist."""

        connection = self._connect()
        if connection is None:
            return 0
        retained = tuple(keep)
        try:
            with self._lock:
                rows = connection.execute(
                    "SELECT reference FROM projections WHERE scope = ?", (scope,)
                ).fetchall()
                stale = [row[0] for row in rows if row[0] not in retained]
                connection.executemany(
                    "DELETE FROM projections WHERE scope = ? AND reference = ?",
                    [(scope, reference) for reference in stale],
                )
                connection.commit()
        except sqlite3.Error:
            return 0
        return len(stale)

    def stats(self) -> Mapping[str, Any]:
        connection = self._connect()
        total = 0
        if connection is not None:
            try:
                with self._lock:
                    total = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM projections"
                        ).fetchone()[0]
                    )
            except sqlite3.Error:
                total = 0
        return {
            "schema_version": PROJECTION_INDEX_SCHEMA_VERSION,
            "path": str(self.path),
            "entries": total,
            "hits": self.hits,
            "misses": self.misses,
            "bypasses": self.bypasses,
            "available": connection is not None,
        }

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                try:
                    self._connection.close()
                finally:
                    self._connection = None


def index_sources(handler: Any, reference: str) -> Tuple[Path, ...]:
    """Ask a job handler which files back one reference's projection.

    Handlers opt in by exposing ``projection_sources``. One that does not is
    simply never cached, so adding a handler cannot introduce staleness.
    """

    resolver = getattr(handler, "projection_sources", None)
    if not callable(resolver):
        return ()
    try:
        return tuple(Path(item) for item in resolver(reference))
    except Exception:
        return ()
