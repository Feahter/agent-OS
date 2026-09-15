#!/usr/bin/env python3
"""Benchmark Resident queue control paths without invoking an Agent."""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from grapheng._store import atomic_json_write
from grapheng.resident import RESIDENT_QUEUE_SCHEMA_VERSION, ResidentCoordinator


class _SyntheticJob:
    def inspect(self, reference: str) -> str:
        return "queued"

    def execute(self, reference: str, control_probe: Callable[[], str]) -> str:
        raise AssertionError("the Resident hot-path benchmark never executes jobs")

    def record_failure(self, reference: str, failure: str) -> None:
        raise AssertionError("the Resident hot-path benchmark never records failures")


class _EmptyTasks:
    pass


def _distribution(samples: Sequence[float]) -> Mapping[str, float]:
    ordered = sorted(samples)
    return {
        "samples": len(ordered),
        "p50_ms": statistics.median(ordered) * 1000.0,
        "p95_ms": ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] * 1000.0,
        "max_ms": ordered[-1] * 1000.0,
    }


def _measure(operation: Callable[[int], Any], samples: int) -> Mapping[str, float]:
    durations = []
    for index in range(samples):
        started = time.perf_counter()
        operation(index)
        durations.append(time.perf_counter() - started)
    return _distribution(durations)


def _queue(size: int) -> Mapping[str, Any]:
    items = {}
    active = max(1, size // 2)
    for index in range(size):
        reference = f"job-{index:08d}"
        job_id = f"graph:{reference}"
        items[job_id] = {
            "job_id": job_id,
            "kind": "graph",
            "reference": reference,
            "priority": 0,
            "sequence": index + 1,
            "state": "queued" if index < active else "succeeded",
            "requested_action": None,
            "submitted_at": 1.0,
            "updated_at": 1.0,
            "attempts": 0 if index < active else 1,
            "error": None,
            "probe_failures": 0,
            "next_probe_at": None,
            "enqueue_intent_id": None,
            "last_control_intent_id": None,
        }
    return {
        "schema_version": RESIDENT_QUEUE_SCHEMA_VERSION,
        "next_sequence": size + 1,
        "updated_at": 1.0,
        "items": items,
    }


def benchmark_size(size: int, samples: int) -> Mapping[str, Any]:
    if size < 1 or samples < 2:
        raise ValueError("benchmark size must be positive and samples must be at least two")
    with tempfile.TemporaryDirectory(prefix="agent-os-resident-benchmark-") as raw_root:
        root = Path(raw_root)
        handler = _SyntheticJob()
        coordinator = ResidentCoordinator(
            root,
            task_module_factory=_EmptyTasks,
            job_handlers={"graph": handler, "orca": handler},
        )
        try:
            atomic_json_write(coordinator.queue_path, _queue(size))
            active = max(1, size // 2)

            inspect = _measure(
                lambda index: coordinator.inspect_job(
                    "graph", f"job-{index % size:08d}"
                ),
                samples,
            )
            control = _measure(
                lambda index: coordinator.request_job(
                    "graph",
                    f"job-{index % active:08d}",
                    "reprioritize",
                    -1 if index % 2 else 1,
                ),
                samples,
            )
            center = _measure(
                lambda _index: coordinator.task_center(limit=200),
                samples,
            )
            metrics = coordinator._hot_path_snapshot()
            queue_bytes = coordinator.queue_path.stat().st_size
        finally:
            coordinator.projections.close()
    target_ms = 100.0 if size == 100 else 250.0 if size == 1000 else None
    return {
        "jobs": size,
        "active_jobs": active,
        "terminal_jobs": size - active,
        "queue_bytes": queue_bytes,
        "operations": {
            "inspect": inspect,
            "control": control,
            "task_center": center,
            "queue_lock_wait": {
                key.replace("_seconds", "_ms"): value * 1000.0
                if key.endswith("_seconds")
                else value
                for key, value in metrics["durations"]["queue_lock"].items()
            },
        },
        "control_status_target_ms": target_ms,
        "control_status_target_met": (
            None
            if target_ms is None
            else inspect["p95_ms"] < target_ms and control["p95_ms"] < target_ms
        ),
    }


def run_benchmarks(sizes: Sequence[int], samples: int) -> Mapping[str, Any]:
    results = [benchmark_size(size, samples) for size in sizes]
    return {
        "schema_version": 1,
        "kind": "resident_hot_path_offline",
        "generated_at": time.time(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "samples_per_operation": samples,
        "results": results,
        "all_defined_targets_met": all(
            result["control_status_target_met"] is not False for result in results
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=(10, 100, 1000))
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    report = run_benchmarks(args.sizes, args.samples)
    if args.output is not None:
        atomic_json_write(args.output, report, label="Resident hot-path benchmark")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["all_defined_targets_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
