import importlib.util
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_preflight_module():
    path = PROJECT_ROOT / "scripts" / "prepare_reliability_canary.py"
    spec = importlib.util.spec_from_file_location("prepare_reliability_canary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = load_preflight_module()


class ReliabilityPreflightTests(unittest.TestCase):
    def _evidence(self):
        return {
            "platform": {"operating_system": "Darwin", "architecture": "arm64"},
            "adapters": {
                adapter: [
                    {
                        "version": "1.2.3",
                        "protocol": protocol,
                    }
                ]
                for adapter, protocol in preflight.ADAPTER_PROTOCOLS.items()
            },
        }

    def _probes(self):
        return {
            probe_id: {"installed": True, "version": "tool 1.2.3"}
            for probe_id in preflight.TOOL_PROBE_IDS.values()
        }

    def _build(self, **overrides):
        values = {
            "tool_probes": self._probes(),
            "compatibility_evidence": self._evidence(),
            "source_commit": "a" * 40,
            "dirty_worktree": False,
            "operating_system": "Darwin",
            "architecture": "arm64",
            "operator": "release-operator",
            "model_calls_authorized": True,
            "maximum_cost_usd": 5.0,
            "authorization_reference": "approval-1",
            "observed_at": 1.0,
        }
        values.update(overrides)
        return preflight.build_preflight(**values)

    def test_ready_requires_clean_certified_authorized_environment(self):
        report = self._build()

        self.assertTrue(report["ready"])
        self.assertEqual([], report["blockers"])
        self.assertEqual(0, report["model_calls"])
        self.assertEqual(0, report["orca_objects_created"])
        self.assertTrue(all(item["certified"] for item in report["tools"].values()))

    def test_missing_drifted_dirty_and_unauthorized_environment_is_blocked(self):
        probes = self._probes()
        probes["opencode"] = {"installed": False, "version": None}
        probes["claude"] = {"installed": True, "version": "Claude Code 9.9.9"}

        report = self._build(
            tool_probes=probes,
            dirty_worktree=True,
            model_calls_authorized=False,
            maximum_cost_usd=None,
            authorization_reference="",
        )

        self.assertFalse(report["ready"])
        self.assertIn("dirty_worktree", report["blockers"])
        self.assertIn("model_call_authorization_missing", report["blockers"])
        self.assertIn("tool_missing:opencode", report["blockers"])
        self.assertIn(
            "tool_version_not_certified:claude-code:9.9.9", report["blockers"]
        )
        self.assertEqual(0, report["model_calls"])
        self.assertEqual(0, report["orca_objects_created"])

    def test_explicit_opencode_waiver_removes_only_that_tool_blocker(self):
        probes = self._probes()
        probes["opencode"] = {"installed": False, "version": None}

        report = self._build(
            tool_probes=probes,
            waivers={
                "opencode": {
                    "approved_by": "user",
                    "approved_at": "2026-09-15T03:00:00Z",
                    "reason": "User directed this canary to skip OpenCode",
                    "approval_reference": "user-scope-change-2026-09-15",
                }
            },
        )

        self.assertTrue(report["ready"])
        self.assertEqual([], report["blockers"])
        self.assertFalse(report["tools"]["opencode"]["certified"])
        self.assertIn("opencode", report["waivers"])


if __name__ == "__main__":
    unittest.main()
