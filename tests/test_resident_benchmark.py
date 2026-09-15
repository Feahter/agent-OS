import unittest

from scripts.benchmark_resident_hot_path import run_benchmarks


class ResidentHotPathBenchmarkTests(unittest.TestCase):
    def test_offline_benchmark_reports_required_dimensions(self):
        report = run_benchmarks((10,), samples=2)

        self.assertEqual("resident_hot_path_offline", report["kind"])
        result = report["results"][0]
        self.assertEqual(10, result["jobs"])
        self.assertEqual(5, result["terminal_jobs"])
        self.assertGreater(result["queue_bytes"], 0)
        self.assertEqual(
            {"inspect", "control", "task_center", "queue_lock_wait"},
            set(result["operations"]),
        )
        for operation in ("inspect", "control", "task_center"):
            self.assertEqual(2, result["operations"][operation]["samples"])
            self.assertGreaterEqual(result["operations"][operation]["p95_ms"], 0)
