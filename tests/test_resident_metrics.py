import tempfile
import unittest
from pathlib import Path

from grapheng import ResidentCoordinator
from grapheng.resident_metrics import ResidentHotPathMetrics


class ResidentHotPathMetricsTests(unittest.TestCase):
    def test_snapshot_reports_bounded_latency_distributions_and_io_counts(self):
        clock = iter((10.0, 70.0, 70.0))
        metrics = ResidentHotPathMetrics(clock=lambda: next(clock), max_samples=4)
        for duration in (0.01, 0.02, 0.03, 0.04, 99.0):
            metrics.observe("control", duration)
        metrics.increment("queue_writes", 3)
        metrics.increment("waiting_inspects", 2)
        metrics.increment("waiting_probe_failures")
        metrics.gauge("queue_bytes", 1024)
        metrics.gauge("terminal_jobs", 7)

        snapshot = metrics.snapshot(reset=True)

        self.assertEqual(5, snapshot["durations"]["control"]["count"])
        self.assertEqual(0.03, snapshot["durations"]["control"]["p50_seconds"])
        self.assertEqual(99.0, snapshot["durations"]["control"]["p95_seconds"])
        self.assertEqual(3, snapshot["counters"]["queue_writes"])
        self.assertEqual(2, snapshot["counters"]["waiting_inspects"])
        self.assertEqual(1, snapshot["counters"]["waiting_probe_failures"])
        self.assertEqual(1024, snapshot["gauges"]["queue_bytes"])
        self.assertEqual(7, snapshot["gauges"]["terminal_jobs"])
        self.assertEqual(60.0, snapshot["window_seconds"])

        after_reset = metrics.snapshot()
        self.assertEqual({}, after_reset["durations"])
        self.assertTrue(all(value == 0 for value in after_reset["counters"].values()))
        self.assertEqual(snapshot["gauges"], after_reset["gauges"])

    def test_metric_names_and_values_are_closed_and_finite(self):
        metrics = ResidentHotPathMetrics()

        with self.assertRaisesRegex(ValueError, "duration metric"):
            metrics.observe("workspace_path", 1.0)
        with self.assertRaisesRegex(ValueError, "counter metric"):
            metrics.increment("prompt_bytes")
        with self.assertRaisesRegex(ValueError, "finite"):
            metrics.observe("claim", float("inf"))
        with self.assertRaisesRegex(ValueError, "non-negative"):
            metrics.increment("queue_writes", -1)

    def test_resident_records_scheduler_boundaries_without_payload_content(self):
        class Tasks:
            def reconcile_outbox(self, resident):
                return None

        class Job:
            def inspect(self, reference):
                return "queued"

            def execute(self, reference, control_probe):
                return {"phase": "succeeded"}

            def record_failure(self, reference, failure):
                raise AssertionError(failure)

        with tempfile.TemporaryDirectory() as directory:
            coordinator = ResidentCoordinator(
                Path(directory),
                task_module_factory=Tasks,
                job_handlers={"graph": Job()},
                clock=lambda: 100.0,
            )
            coordinator.schedule("graph", "metrics-job")
            coordinator.serve_once()
            snapshot = coordinator._hot_path_snapshot()

        self.assertTrue(
            {"serve_once", "refresh_waiting", "claim", "settle"}.issubset(
                snapshot["durations"]
            )
        )
        self.assertGreaterEqual(snapshot["counters"]["queue_writes"], 3)
        self.assertEqual(1, snapshot["gauges"]["terminal_jobs"])
        self.assertEqual(0, snapshot["gauges"]["active_jobs"])
        self.assertNotIn("metrics-job", str(snapshot))


if __name__ == "__main__":
    unittest.main()
