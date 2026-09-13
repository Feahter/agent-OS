"""Structured, prompt-free telemetry for Agent OS.

Agent OS runs a background resident, several concurrent workers and a bounded
repair loop. Without a durable trace, a failed run leaves only whatever the
caller happened to print. This module provides that trace.

Two sinks are written for every event:

* the stdlib ``logging`` hierarchy under the ``grapheng`` logger, so an
  embedding application can route Agent OS output wherever it likes, and
* an append-only JSONL journal under ``<home>/runtime/logs/`` that survives
  process death and can be replayed after a crash.

Events are deliberately prompt-free. Only identifiers, states, digests,
durations and counters are recorded. Free-form agent text and model prompts
never enter the journal, so it stays safe to attach to a bug report.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from ._store import append_jsonl, sha256_hex
from .errors import ContractViolation

TELEMETRY_SCHEMA_VERSION = 1

_ENV_HOME = "AGENT_OS_HOME"
_ENV_DISABLE = "AGENT_OS_DISABLE_TELEMETRY"
_MAX_FIELD_CHARS = 512
#: Field names owned by the envelope. A caller that reuses one would silently
#: overwrite it, so the collision is rejected instead.
RESERVED_FIELDS = frozenset({"schema_version", "ts", "kind", "pid"})
#: Free-form failure text can contain prompts, model output, paths or provider
#: messages. Persist only a stable fingerprint and length for correlation.
SENSITIVE_FIELDS = frozenset(
    {"detail", "error", "message", "objective", "prompt", "reason", "response", "stderr", "stdout", "text"}
)
_DERIVED_SENSITIVE_FIELDS = frozenset(
    f"{field}_{suffix}"
    for field in SENSITIVE_FIELDS
    for suffix in ("chars", "sha256")
)

logger = logging.getLogger("grapheng")


def _default_home() -> Path:
    configured = os.environ.get(_ENV_HOME)
    return Path(configured).expanduser() if configured else Path.home() / ".agent-os"


def _sensitive_fingerprint(value: Any) -> tuple:
    if isinstance(value, Path):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        try:
            text = repr(value)
        except Exception:  # pragma: no cover - repr should not fail
            text = "<unrepresentable>"
    return sha256_hex(text.encode("utf-8")), len(text)


def _bounded(value: Any) -> Any:
    """Keep a field small and JSON-safe without losing its head and tail."""

    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, Path):
        value = str(value)
    if not isinstance(value, str):
        try:
            return _bounded(repr(value))
        except Exception:  # pragma: no cover - repr should not fail
            return "<unrepresentable>"
    if len(value) <= _MAX_FIELD_CHARS:
        return value
    head = _MAX_FIELD_CHARS // 2
    tail = _MAX_FIELD_CHARS - head
    return f"{value[:head]}...{value[-tail:]}"


class EventJournal:
    """Append-only, date-partitioned JSONL journal of runtime events."""

    def __init__(
        self,
        home: Optional[Path] = None,
        clock=time.time,
        enabled: Optional[bool] = None,
    ):
        self.home = (home or _default_home()).expanduser()
        self.root = self.home / "runtime" / "logs"
        self._clock = clock
        self._lock = threading.Lock()
        if enabled is None:
            enabled = os.environ.get(_ENV_DISABLE, "").strip().lower() not in (
                "1",
                "true",
                "yes",
            )
        self.enabled = enabled

    def path_for(self, moment: float) -> Path:
        day = time.strftime("%Y-%m-%d", time.gmtime(moment))
        return self.root / f"events-{day}.jsonl"

    def emit(self, event: str, /, level: int = logging.INFO, **fields: Any) -> None:
        """Record one event in both the logging hierarchy and the journal.

        ``event`` is positional-only so callers can pass a ``kind`` field
        without colliding with the event name.
        """

        moment = self._clock()
        payload: Dict[str, Any] = {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "ts": moment,
            "kind": event,
            "pid": os.getpid(),
        }
        reserved = (RESERVED_FIELDS | _DERIVED_SENSITIVE_FIELDS).intersection(fields)
        if reserved:
            raise ContractViolation(
                "telemetry field names are reserved: " + ", ".join(sorted(reserved))
            )
        for key in sorted(fields):
            value = fields[key]
            if value is None:
                continue
            if key in SENSITIVE_FIELDS:
                digest, chars = _sensitive_fingerprint(value)
                payload[f"{key}_sha256"] = digest
                payload[f"{key}_chars"] = chars
                continue
            payload[key] = _bounded(value)
        logger.log(level, "%s %s", event, payload, extra={"grapheng_event": payload})
        if not self.enabled:
            return
        try:
            with self._lock:
                append_jsonl(self.path_for(moment), payload, label="telemetry event")
        except Exception:
            # Telemetry must never change a task result. A journal write that
            # fails is reported through logging only.
            logger.debug("telemetry journal write failed for %s", event, exc_info=True)

    def read(self, moment: Optional[float] = None) -> tuple:
        """Return the events recorded for one UTC day, newest last."""

        import json

        path = self.path_for(moment if moment is not None else self._clock())
        if not path.exists():
            return ()
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return tuple(events)


_journal_lock = threading.Lock()
_journal: Optional[EventJournal] = None


def journal() -> EventJournal:
    """Return the process-wide journal, creating it on first use."""

    global _journal
    with _journal_lock:
        if _journal is None:
            _journal = EventJournal()
        return _journal


def configure(home: Optional[Path] = None, enabled: Optional[bool] = None) -> EventJournal:
    """Point the process-wide journal at ``home``."""

    global _journal
    with _journal_lock:
        _journal = EventJournal(home=home, enabled=enabled)
        return _journal


def emit(event: str, /, level: int = logging.INFO, **fields: Any) -> None:
    """Record one runtime event through the process-wide journal."""

    journal().emit(event, level=level, **fields)


def emit_failure(event: str, /, **fields: Any) -> None:
    """Record one runtime failure event."""

    emit(event, level=logging.WARNING, **fields)


def snapshot(home: Optional[Path] = None) -> Mapping[str, Any]:
    """Summarize today's journal for operations views."""

    active = EventJournal(home=home) if home is not None else journal()
    events = active.read()
    counts: Dict[str, int] = {}
    for event in events:
        kind = str(event.get("kind", "unknown"))
        counts[kind] = counts.get(kind, 0) + 1
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "enabled": active.enabled,
        "path": str(active.path_for(time.time())),
        "total": len(events),
        "counts": dict(sorted(counts.items())),
    }
