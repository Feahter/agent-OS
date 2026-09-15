import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKER = PROJECT_ROOT / "scripts" / "check_reliability_report.py"
ADAPTERS = {
    "codex": "exec-jsonl-v1",
    "claude-code": "json-envelope-v1",
    "pi-agent": "message-end-jsonl-v1",
    "opencode": "run-jsonl-v1",
    "orca": "orca-json-command-v1",
}
CRASH_POINTS = {
    "before_write",
    "after_write",
    "before_output_parse",
    "after_output_parse",
    "orca_message",
    "orca_merge",
    "orca_cleanup",
}


class ReliabilityGateTests(unittest.TestCase):
    def _run(self, root: Path, canary):
        report = {
            "schema_version": 1,
            "iterations": 100,
            "passed": True,
            "duplicate_effects": 0,
            "manual_interventions": 0,
            "terminal_divergences": 0,
            "real_adapter_canary": canary,
        }
        path = root / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(CHECKER), str(path)],
            capture_output=True,
            text=True,
            check=False,
        )

    def _complete_canary(self, root: Path):
        evidence = root / "evidence"
        evidence.mkdir()
        adapter_results = {}
        for adapter, protocol in ADAPTERS.items():
            path = evidence / f"adapter-{adapter}.json"
            path.write_text("{}", encoding="utf-8")
            adapter_results[adapter] = {
                "version": "1.2.3",
                "protocol": protocol,
                "passed": True,
                "evidence_path": str(path.relative_to(root)),
            }
        crash_results = {}
        for crash_point in CRASH_POINTS:
            path = evidence / f"crash-{crash_point}.json"
            path.write_text("{}", encoding="utf-8")
            crash_results[crash_point] = {
                "passed": True,
                "effect_identity": f"effect-{crash_point}",
                "receipt_before": "prepared",
                "receipt_after": "completed",
                "external_terminal": "observed",
                "retry_count": 0,
                "human_action_required": False,
                "evidence_path": str(path.relative_to(root)),
            }
        return {
            "performed": True,
            "source_commit": "a" * 40,
            "dirty_worktree": False,
            "platform": {"operating_system": "Darwin", "architecture": "arm64"},
            "started_at": "2026-09-15T00:00:00Z",
            "finished_at": "2026-09-15T01:00:00Z",
            "operator": "release-operator",
            "authorization": {
                "model_calls": True,
                "maximum_cost_usd": 5.0,
                "reference": "release-approval-1",
            },
            "adapter_results": adapter_results,
            "crash_results": crash_results,
            "duplicate_effects": 0,
            "terminal_divergences": 0,
        }

    def test_performed_flag_and_name_lists_cannot_forge_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self._run(
                Path(directory),
                {
                    "performed": True,
                    "adapters": sorted(ADAPTERS),
                    "crash_points": sorted(CRASH_POINTS),
                    "duplicate_effects": 0,
                    "terminal_divergences": 0,
                },
            )

        self.assertEqual(1, result.returncode)
        self.assertIn("identity", result.stderr)

    def test_complete_canary_requires_existing_contained_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canary = self._complete_canary(root)
            canary["crash_results"]["after_write"]["evidence_path"] = (
                "../outside.json"
            )
            result = self._run(root, canary)

        self.assertEqual(1, result.returncode)
        self.assertIn("evidence", result.stderr)

    def test_complete_auditable_canary_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self._run(root, self._complete_canary(root))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("release gate passed", result.stdout)

    def test_user_approved_opencode_waiver_is_explicit_and_auditable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canary = self._complete_canary(root)
            canary["adapter_results"].pop("opencode")
            canary["waivers"] = {
                "opencode": {
                    "approved_by": "user",
                    "approved_at": "2026-09-15T03:00:00Z",
                    "reason": "User directed this canary to skip OpenCode",
                    "approval_reference": "user-scope-change-2026-09-15",
                }
            }
            result = self._run(root, canary)

        self.assertEqual(0, result.returncode, result.stderr)

    def test_opencode_waiver_without_audit_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canary = self._complete_canary(root)
            canary["adapter_results"].pop("opencode")
            canary["waivers"] = {"opencode": {"approved_by": "user"}}
            result = self._run(root, canary)

        self.assertEqual(1, result.returncode)
        self.assertIn("waiver", result.stderr)


if __name__ == "__main__":
    unittest.main()
