"""In-memory, prompt-free aggregation for Resident hot-path telemetry."""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Dict, Mapping

_DURATIONS = frozenset(
    {
        "serve_once",
        "refresh_waiting",
        "claim",
        "settle",
        "control",
        "status",
        "center",
        "queue_lock",
    }
)
_COUNTERS = frozenset(
    {
        "queue_writes",
        "telemetry_flushes",
        "waiting_inspects",
        "waiting_probe_failures",
        "handler_failures",
        "terminal_jobs_archived",
    }
)
_GAUGES = frozenset({"queue_bytes", "terminal_jobs", "active_jobs"})


class ResidentHotPathMetrics:
    """Aggregate bounded distributions without doing I/O on the hot path."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_samples: int = 2048,
    ):
        if isinstance(max_samples, bool) or not isinstance(max_samples, int) or max_samples < 1:
            raise ValueError("max_samples must be a positive integer")
        self._clock = clock
        self._max_samples = max_samples
        self._window_started_at = float(clock())
        self._durations: Dict[str, list[float]] = {}
        self._duration_counts: Dict[str, int] = {}
        self._counters = dict.fromkeys(_COUNTERS, 0)
        self._gauges = dict.fromkeys(_GAUGES, 0)
        self._lock = threading.Lock()

    def observe(self, name: str, seconds: float) -> None:
        if name not in _DURATIONS:
            raise ValueError(f"unknown Resident duration metric: {name}")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
        ):
            raise ValueError("Resident duration must be finite")
        if seconds < 0:
            raise ValueError("Resident duration must be non-negative")
        with self._lock:
            samples = self._durations.setdefault(name, [])
            samples.append(float(seconds))
            del samples[: -self._max_samples]
            self._duration_counts[name] = self._duration_counts.get(name, 0) + 1

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in _COUNTERS:
            raise ValueError(f"unknown Resident counter metric: {name}")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("Resident counter increment must be non-negative")
        with self._lock:
            self._counters[name] += amount

    def gauge(self, name: str, value: int) -> None:
        if name not in _GAUGES:
            raise ValueError(f"unknown Resident gauge metric: {name}")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Resident gauge must be non-negative")
        with self._lock:
            self._gauges[name] = value

    def snapshot(self, *, reset: bool = False) -> Mapping[str, Any]:
        observed_at = float(self._clock())
        with self._lock:
            durations = {
                name: self._distribution(
                    samples,
                    self._duration_counts.get(name, len(samples)),
                )
                for name, samples in self._durations.items()
                if samples
            }
            value = {
                "window_seconds": max(0.0, observed_at - self._window_started_at),
                "durations": durations,
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
            }
            if reset:
                self._window_started_at = observed_at
                self._durations.clear()
                self._duration_counts.clear()
                self._counters = dict.fromkeys(_COUNTERS, 0)
            return value

    @staticmethod
    def _distribution(samples: list[float], count: int) -> Mapping[str, Any]:
        ordered = sorted(samples)
        return {
            "count": count,
            "samples_retained": len(ordered),
            "p50_seconds": ResidentHotPathMetrics._percentile(ordered, 0.50),
            "p95_seconds": ResidentHotPathMetrics._percentile(ordered, 0.95),
            "max_seconds": ordered[-1],
        }

    @staticmethod
    def _percentile(ordered: list[float], fraction: float) -> float:
        rank = max(1, math.ceil(len(ordered) * fraction))
        return ordered[rank - 1]
