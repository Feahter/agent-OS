import importlib.util
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_soak_module():
    path = PROJECT_ROOT / "scripts" / "run_reliability_soak.py"
    spec = importlib.util.spec_from_file_location("run_reliability_soak", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


soak_module = load_soak_module()
run_soak = soak_module.run_soak


class ReliabilitySoakTests(unittest.TestCase):
    def test_pending_real_canary_fixes_protocol_and_crash_matrix(self):
        pending = soak_module.pending_real_canary(
            {
                "codex": {"version": "codex-cli 1.2.3"},
                "claude": {"version": "claude 2.3.4"},
                "pi": {"version": "pi 3.4.5"},
                "opencode": {"version": None},
                "orca": {"version": "4.5.6"},
            }
        )

        self.assertFalse(pending["performed"])
        self.assertEqual(
            {
                "codex",
                "claude-code",
                "pi-agent",
                "opencode",
                "orca",
            },
            set(pending["required_adapter_protocols"]),
        )
        self.assertEqual(
            {
                "before_write",
                "after_write",
                "before_output_parse",
                "after_output_parse",
                "orca_message",
                "orca_merge",
                "orca_cleanup",
            },
            set(pending["required_crash_points"]),
        )
        self.assertIsNone(pending["observed_versions"]["opencode"])

    def test_soak_recovers_expired_leases_and_committed_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            report = run_soak(3, Path(directory))

        self.assertTrue(report["passed"])
        self.assertEqual(3, report["iterations"])
        self.assertEqual(0, report["duplicate_effects"])
        self.assertEqual(0, report["manual_interventions"])
        self.assertEqual(0, report["terminal_divergences"])
        self.assertGreaterEqual(report["minimum_checkpoints_observed"], 3)
        self.assertEqual(1, report["maximum_effect_calls_per_iteration"])
        self.assertEqual(2, report["maximum_lease_generation"])


if __name__ == "__main__":
    unittest.main()
