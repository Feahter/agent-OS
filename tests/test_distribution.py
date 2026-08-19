import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grapheng import AgentOS, AgentOSDistribution, ContractViolation


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def fake_probe_environment(distribution):
    matrix = distribution.compatibility_matrix()
    flags = {
        item["command"]: item["required_help_flags"]
        for item in matrix["adapters"]
    }
    versions = {
        item["command"]: item["support"]["verified_versions"][0]["version"]
        for item in matrix["adapters"]
    }
    calls = []

    def which(command):
        return f"/fake/{command}"

    def runner(command, timeout_seconds):
        calls.append(tuple(command))
        name = Path(command[0]).name
        if command[-1] == "--version":
            return subprocess.CompletedProcess(
                command, 0, stdout=f"{name} {versions[name]}\n", stderr=""
            )
        return subprocess.CompletedProcess(
            command, 0, stdout=" ".join(flags[name]), stderr=""
        )

    return which, runner, calls


class DistributionTests(unittest.TestCase):
    def test_doctor_checks_state_filesystem_and_adapter_protocols_without_execution(self):
        base = AgentOSDistribution(PROJECT_ROOT)
        which, runner, calls = fake_probe_environment(base)
        distribution = AgentOSDistribution(
            PROJECT_ROOT, which=which, runner=runner
        )
        with patch("platform.machine", return_value="arm64"), patch(
            "platform.system", return_value="Darwin"
        ), tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent_os = AgentOS(root / "agent-os")
            report = distribution.doctor(agent_os.root)

        statuses = {
            item["check_id"]: item["status"] for item in report["checks"]
        }
        self.assertTrue(report["healthy"])
        self.assertTrue(report["ready_for_agent_execution"])
        self.assertEqual(
            ["claude-code", "codex", "pi-agent"], report["ready_executors"]
        )
        self.assertTrue(report["ready_for_orca"])
        self.assertEqual("pass", statuses["state:agent-os"])
        self.assertEqual("pass", statuses["runtime:filesystem"])
        self.assertEqual("pass", statuses["adapter:claude-code"])
        self.assertEqual("pass", statuses["adapter:codex"])
        self.assertEqual("pass", statuses["adapter:pi-agent"])
        self.assertEqual("pass", statuses["orchestration:orca"])
        codex = next(
            item for item in report["checks"] if item["check_id"] == "adapter:codex"
        )
        self.assertEqual("verified", codex["details"]["support_status"])
        self.assertEqual(
            "2026-08-18T08:58:53Z", codex["details"]["last_verified_at"]
        )
        self.assertTrue(
            all("--help" in command or command[-1] == "--version" for command in calls)
        )

    def test_doctor_fails_closed_when_installed_command_drifts_from_protocol(self):
        def which(command):
            return "/fake/codex" if command == "codex" else None

        def runner(command, timeout_seconds):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(
                    command, 0, stdout="codex 1.0", stderr=""
                )
            return subprocess.CompletedProcess(
                command, 0, stdout="--json --sandbox", stderr=""
            )

        distribution = AgentOSDistribution(
            PROJECT_ROOT, which=which, runner=runner
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent_os = AgentOS(root / "agent-os")
            report = distribution.doctor(agent_os.root)

        codex = next(
            item for item in report["checks"] if item["check_id"] == "adapter:codex"
        )
        self.assertFalse(report["healthy"])
        self.assertFalse(report["ready_for_agent_execution"])
        self.assertEqual([], report["ready_executors"])
        self.assertEqual("fail", codex["status"])
        self.assertEqual(
            ["--ephemeral", "--output-schema"],
            codex["details"]["missing_help_flags"],
        )
        self.assertEqual("protocol_mismatch", codex["details"]["support_status"])

    def test_doctor_does_not_certify_an_unverified_version(self):
        base = AgentOSDistribution(PROJECT_ROOT)
        flags = next(
            item["required_help_flags"]
            for item in base.compatibility_matrix()["adapters"]
            if item["executor_id"] == "codex"
        )

        def which(command):
            return "/fake/codex" if command == "codex" else None

        def runner(command, timeout_seconds):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(
                    command, 0, stdout="codex 9.9.9", stderr=""
                )
            return subprocess.CompletedProcess(
                command, 0, stdout=" ".join(flags), stderr=""
            )

        distribution = AgentOSDistribution(
            PROJECT_ROOT, which=which, runner=runner
        )
        with tempfile.TemporaryDirectory() as directory:
            agent_os = AgentOS(Path(directory) / "agent-os")
            report = distribution.doctor(agent_os.root)

        codex = next(
            item for item in report["checks"] if item["check_id"] == "adapter:codex"
        )
        self.assertTrue(report["healthy"])
        self.assertFalse(report["ready_for_agent_execution"])
        self.assertEqual("warn", codex["status"])
        self.assertEqual("unverified_version", codex["details"]["support_status"])

    def test_doctor_does_not_certify_versions_on_an_unverified_platform(self):
        base = AgentOSDistribution(PROJECT_ROOT)
        which, runner, _ = fake_probe_environment(base)
        distribution = AgentOSDistribution(
            PROJECT_ROOT, which=which, runner=runner
        )
        with patch("platform.machine", return_value="x86_64"), patch(
            "platform.system", return_value="Darwin"
        ), tempfile.TemporaryDirectory() as directory:
            agent_os = AgentOS(Path(directory) / "agent-os")
            report = distribution.doctor(agent_os.root)

        codex = next(
            item for item in report["checks"] if item["check_id"] == "adapter:codex"
        )
        self.assertTrue(report["healthy"])
        self.assertFalse(report["ready_for_agent_execution"])
        self.assertEqual([], report["ready_executors"])
        self.assertEqual("warn", codex["status"])
        self.assertEqual("unverified_platform", codex["details"]["support_status"])
        self.assertEqual(
            {"architecture": "x86_64", "operating_system": "Darwin"},
            codex["details"]["running_platform"],
        )
        self.assertEqual(
            [{"architecture": "arm64", "operating_system": "Darwin"}],
            codex["details"]["verified_platforms"],
        )

    def test_doctor_fails_closed_on_probe_timeout_and_process_crash(self):
        def which(command):
            return "/fake/codex" if command == "codex" else None

        for failure in ("timeout", "crash"):
            with self.subTest(failure=failure):
                def runner(command, timeout_seconds):
                    if command[-1] == "--version":
                        return subprocess.CompletedProcess(
                            command, 0, stdout="codex 0.148.0-alpha.9", stderr=""
                        )
                    if failure == "timeout":
                        raise subprocess.TimeoutExpired(command, timeout_seconds)
                    return subprocess.CompletedProcess(
                        command, -9, stdout="", stderr="terminated"
                    )

                distribution = AgentOSDistribution(
                    PROJECT_ROOT, which=which, runner=runner
                )
                with tempfile.TemporaryDirectory() as directory:
                    agent_os = AgentOS(Path(directory) / "agent-os")
                    report = distribution.doctor(agent_os.root)

                codex = next(
                    item
                    for item in report["checks"]
                    if item["check_id"] == "adapter:codex"
                )
                self.assertFalse(report["healthy"])
                self.assertEqual("fail", codex["status"])

    def test_compatibility_matrix_and_backup_restore_rehearsal(self):
        distribution = AgentOSDistribution(PROJECT_ROOT)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent_os = AgentOS(root / "agent-os")
            (agent_os.learning_root / "history.json").write_text(
                "[]", encoding="utf-8"
            )
            before = (agent_os.learning_root / "history.json").read_bytes()

            matrix = distribution.compatibility_matrix()
            rehearsal = distribution.rehearse(agent_os.root)
            legacy = agent_os.export_bundle(root / "legacy.bundle")
            legacy_manifest_path = legacy / "manifest.json"
            legacy_manifest = json.loads(
                legacy_manifest_path.read_text(encoding="utf-8")
            )
            legacy_manifest["schema_version"] = 1
            legacy_manifest.pop("state_schema_version")
            legacy_manifest.pop("migration_history")
            legacy_manifest_path.write_text(
                json.dumps(legacy_manifest, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            migration_rehearsal = distribution.rehearse(agent_os.root, legacy)
            after = (agent_os.learning_root / "history.json").read_bytes()

        self.assertEqual([1, 2], matrix["state"]["importable_bundle_schema_versions"])
        self.assertEqual([], matrix["runtime"]["third_party_runtime_dependencies"])
        self.assertEqual(2, matrix["matrix_schema_version"])
        self.assertEqual(
            "help/version protocol inspection only; no model calls or Orca object creation",
            matrix["evidence"]["verification_scope"],
        )
        self.assertTrue(
            all(item["support"]["verified_versions"] for item in matrix["adapters"])
        )
        self.assertTrue(rehearsal["verified"])
        self.assertTrue(rehearsal["source_unchanged"])
        self.assertEqual(1, rehearsal["imported_files"])
        self.assertEqual(
            "bundle-v1-to-v2",
            migration_rehearsal["migration"]["steps"][0]["converter_id"],
        )
        self.assertTrue(migration_rehearsal["source_unchanged"])
        self.assertEqual(before, after)

    def test_release_is_self_contained_rehearsed_and_tamper_evident(self):
        distribution = AgentOSDistribution(PROJECT_ROOT, clock=lambda: 100.0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent_os = AgentOS(root / "agent-os", clock=lambda: 50.0)
            (agent_os.learning_root / "history.json").write_text(
                "[]", encoding="utf-8"
            )
            release = root / "release"

            created = distribution.create_release(agent_os.root, release)
            verified = distribution.verify_release(release)
            manifest_text = (release / "release-manifest.json").read_text(
                encoding="utf-8"
            )
            compatibility = json.loads(
                (release / "COMPATIBILITY.json").read_text(encoding="utf-8")
            )
            restore_guide = (release / "RESTORE.md").read_text(encoding="utf-8")
            self.assertTrue((release / "grapheng" / "cli.py").is_file())
            self.assertTrue((release / "tests" / "test_distribution.py").is_file())
            self.assertTrue(
                (release / "grapheng" / "compatibility-evidence.json").is_file()
            )
            self.assertTrue((release / "state.bundle" / "manifest.json").is_file())

            manifest_path = release / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["release_id"] = "0" * 64
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ContractViolation, "identity"):
                distribution.verify_release(release)
            manifest["release_id"] = created["release_id"]
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            (release / "grapheng" / "agents.py").write_text(
                "tampered", encoding="utf-8"
            )
            with self.assertRaisesRegex(ContractViolation, "checksum mismatch"):
                distribution.verify_release(release)

        self.assertTrue(created["verified"])
        self.assertEqual(created["release_id"], verified["release_id"])
        self.assertNotIn(str(root), manifest_text)
        self.assertEqual(2, compatibility["state"]["bundle_schema_version"])
        self.assertIn("verify-release", restore_guide)
        self.assertGreater(created["files"], 10)

    def test_release_refuses_to_overwrite_existing_target(self):
        distribution = AgentOSDistribution(PROJECT_ROOT)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent_os = AgentOS(root / "agent-os")
            release = root / "release"
            release.mkdir()

            with self.assertRaisesRegex(ContractViolation, "already exists"):
                distribution.create_release(agent_os.root, release)


if __name__ == "__main__":
    unittest.main()
