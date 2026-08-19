import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from grapheng import AgentOS, AgentOSDistribution, ContractViolation
from grapheng.cli import main


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
    supported_commands = set(flags)
    calls = []

    def which(command):
        return f"/fake/{command}" if command in supported_commands else None

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
        self.assertEqual(4, report["diagnostic_schema_version"])
        self.assertEqual("ready", report["readiness"])
        self.assertEqual([], report["blocking_checks"])
        self.assertEqual([], report["next_actions"])
        self.assertTrue(report["ready_for_agent_execution"])
        self.assertEqual([], report["discovered_agents"])
        self.assertEqual(
            ["claude-code", "codex", "pi-agent", "opencode"],
            report["ready_executors"],
        )
        self.assertTrue(report["ready_for_orca"])
        self.assertEqual("pass", statuses["state:agent-os"])
        self.assertEqual("pass", statuses["runtime:filesystem"])
        self.assertEqual("pass", statuses["adapter:claude-code"])
        self.assertEqual("pass", statuses["adapter:codex"])
        self.assertEqual("pass", statuses["adapter:pi-agent"])
        self.assertEqual("pass", statuses["adapter:opencode"])
        self.assertEqual("pass", statuses["orchestration:orca"])
        codex = next(
            item for item in report["checks"] if item["check_id"] == "adapter:codex"
        )
        self.assertEqual("verified", codex["details"]["support_status"])
        self.assertEqual(
            "2026-08-18T08:58:53Z", codex["details"]["last_verified_at"]
        )
        opencode = next(
            item
            for item in report["checks"]
            if item["check_id"] == "adapter:opencode"
        )
        self.assertEqual("verified", opencode["details"]["support_status"])
        self.assertEqual("run-jsonl-v1", opencode["details"]["protocol"])
        self.assertEqual(
            "2026-08-19T08:22:08Z", opencode["details"]["last_verified_at"]
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
        action = next(
            item
            for item in report["next_actions"]
            if item["check_id"] == "adapter:codex"
        )
        self.assertEqual("certify_adapter", action["action_id"])
        self.assertEqual("recommended", action["priority"])
        self.assertIsInstance(action["command"], list)

    def test_setup_initializes_state_and_explains_missing_agents_without_model_calls(self):
        distribution = AgentOSDistribution(PROJECT_ROOT, which=lambda command: None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent-os"
            first = distribution.setup(root)
            second = distribution.setup(root)

        self.assertTrue(first["initialized"])
        self.assertFalse(second["initialized"])
        self.assertEqual(0, first["model_calls"])
        self.assertEqual("needs_agent", first["readiness"])
        self.assertFalse(first["ready_for_agent_execution"])
        self.assertEqual(
            "enable_agent_execution", first["next_actions"][0]["action_id"]
        )
        self.assertEqual("required", first["next_actions"][0]["priority"])
        self.assertEqual(
            "pass",
            next(
                item["status"]
                for item in first["checks"]
                if item["check_id"] == "state:agent-os"
            ),
        )

    def test_setup_discovers_unintegrated_agents_without_marking_them_ready(self):
        calls = []

        def which(command):
            return "/fake/openclaw" if command == "openclaw" else None

        def runner(command, timeout_seconds):
            calls.append(tuple(command))
            if command[-1] == "--version":
                return subprocess.CompletedProcess(
                    command, 0, stdout="openclaw 1.2.3", stderr=""
                )
            return subprocess.CompletedProcess(command, 0, stdout="usage", stderr="")

        distribution = AgentOSDistribution(
            PROJECT_ROOT, which=which, runner=runner
        )
        with tempfile.TemporaryDirectory() as directory:
            report = distribution.setup(Path(directory) / "agent-os")

        self.assertEqual([], report["ready_executors"])
        self.assertFalse(report["ready_for_agent_execution"])
        self.assertEqual("needs_agent", report["readiness"])
        self.assertEqual(1, len(report["discovered_agents"]))
        discovered = report["discovered_agents"][0]
        self.assertEqual("openclaw", discovered["agent_id"])
        self.assertEqual("installed_unverified", discovered["discovery_status"])
        self.assertEqual("adapter_not_available", discovered["integration_status"])
        self.assertEqual(
            [
                ("/fake/openclaw", "--help"),
                ("/fake/openclaw", "--version"),
            ],
            calls,
        )
        action = next(
            item
            for item in report["next_actions"]
            if item["check_id"] == "discovery:openclaw"
        )
        self.assertEqual("integrate_discovered_agent", action["action_id"])
        self.assertEqual("optional", action["priority"])

    def test_discovery_version_failure_never_promotes_agent(self):
        def which(command):
            return "/fake/hermes" if command == "hermes" else None

        def runner(command, timeout_seconds):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(command, 2, stdout="", stderr="bad")
            return subprocess.CompletedProcess(command, 0, stdout="usage", stderr="")

        distribution = AgentOSDistribution(
            PROJECT_ROOT, which=which, runner=runner
        )
        with tempfile.TemporaryDirectory() as directory:
            agent_os = AgentOS(Path(directory) / "agent-os")
            report = distribution.doctor(agent_os.root)

        self.assertEqual([], report["ready_executors"])
        self.assertEqual(
            "version_unknown",
            report["discovered_agents"][0]["discovery_status"],
        )

    def test_setup_does_not_initialize_an_existing_nonempty_directory(self):
        distribution = AgentOSDistribution(PROJECT_ROOT, which=lambda command: None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "unrelated"
            root.mkdir()
            marker = root / "keep.txt"
            marker.write_text("keep", encoding="utf-8")

            report = distribution.setup(root)

            self.assertEqual("keep", marker.read_text(encoding="utf-8"))
            self.assertFalse((root / "manifest.json").exists())

        self.assertFalse(report["initialized"])
        self.assertEqual("blocked", report["readiness"])
        action = next(
            item
            for item in report["next_actions"]
            if item["check_id"] == "state:agent-os"
        )
        self.assertEqual("restore_or_choose_state", action["action_id"])
        self.assertNotIn("command", action)

    def test_cli_setup_has_human_and_json_outputs(self):
        report = {
            "initialized": True,
            "root": "/tmp/agent-os",
            "readiness": "ready_with_warnings",
            "ready_executors": ["codex"],
            "discovered_agents": [
                {
                    "agent_id": "openclaw",
                    "display_name": "OpenClaw",
                    "discovery_status": "installed_unverified",
                }
            ],
            "ready_for_agent_execution": True,
            "ready_for_orca": False,
            "blocking_checks": [],
            "checks": [
                {
                    "check_id": "orchestration:orca",
                    "status": "warn",
                    "summary": "Optional command orca is not installed",
                    "details": {},
                }
            ],
            "next_actions": [
                {
                    "action_id": "install_adapter",
                    "check_id": "orchestration:orca",
                    "priority": "optional",
                    "summary": "Install orca if orchestration is needed.",
                    "command": ["agent-os", "setup", "--home", "/tmp/agent-os"],
                }
            ],
            "model_calls": 0,
        }
        distribution = Mock()
        distribution.setup.return_value = report
        human = io.StringIO()
        with patch(
            "grapheng.cli.AgentOSDistribution", return_value=distribution
        ), patch.object(
            sys, "argv", ["agent-os", "setup", "--home", "/tmp/agent-os"]
        ), contextlib.redirect_stdout(human):
            self.assertEqual(0, main())

        machine = io.StringIO()
        with patch(
            "grapheng.cli.AgentOSDistribution", return_value=distribution
        ), patch.object(
            sys,
            "argv",
            ["agent-os", "setup", "--home", "/tmp/agent-os", "--json"],
        ), contextlib.redirect_stdout(machine):
            self.assertEqual(0, main())

        self.assertIn("ready with warnings", human.getvalue())
        self.assertIn("0 model calls", human.getvalue())
        self.assertIn("Discovered but not integrated: OpenClaw", human.getvalue())
        self.assertIn("[optional] Install orca", human.getvalue())
        self.assertEqual(report, json.loads(machine.getvalue()))

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
        self.assertEqual(4, matrix["matrix_schema_version"])
        self.assertEqual(
            {
                "openclaw",
                "hermes",
                "aider",
                "gemini-cli",
                "github-copilot-cli",
            },
            {item["agent_id"] for item in matrix["discoverable_agents"]},
        )
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
