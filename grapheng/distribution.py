import fcntl
import json
import math
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ._store import (
    read_json_object,
    sha256_hex,
)
from .adapters import (
    ClaudeCodeExecutor,
    CodexExecutor,
    OpenCodeExecutor,
    PiAgentExecutor,
)
from .errors import ContractViolation
from .governance import PROVIDER_GOVERNANCE_SCHEMA_VERSION
from .merge import CONTROLLED_MERGE_SCHEMA_VERSION
from .migrations import BUNDLE_SCHEMA_VERSION, default_bundle_migrations
from .orca_state import ORCA_COORDINATOR_SCHEMA_VERSION
from .os import AGENT_OS_SCHEMA_VERSION, AgentOS

RELEASE_SCHEMA_VERSION = 1
DIAGNOSTIC_SCHEMA_VERSION = 4
COMPATIBILITY_MATRIX_VERSION = 4
_RELEASE_KIND = "grapheng-agent-os-release"
_MINIMUM_PYTHON = (3, 9)
_SOURCE_FILES = (
    Path("README.md"),
    Path(".workflow/agent-os/plan.md"),
    Path(".workflow/agent-os/results.md"),
    Path("grapheng/compatibility-evidence.json"),
)
_SOURCE_GLOBS = (
    (Path("grapheng"), "*.py"),
    (Path("tests"), "*.py"),
    (Path("tests/fixtures"), "*.py"),
    (Path("examples"), "*.json"),
)
_REQUIRED_RELEASE_PATHS = (
    PurePosixPath("README.md"),
    PurePosixPath("COMPATIBILITY.json"),
    PurePosixPath("RESTORE.md"),
    PurePosixPath("grapheng/__init__.py"),
    PurePosixPath("grapheng/cli.py"),
    PurePosixPath("grapheng/distribution.py"),
    PurePosixPath("state.bundle/manifest.json"),
)
_COMPATIBILITY_EVIDENCE_PATH = Path("grapheng/compatibility-evidence.json")
_INSTALLED_RUNTIME_FILES = (
    Path("grapheng/__init__.py"),
    Path("grapheng/cli.py"),
    Path("grapheng/distribution.py"),
    _COMPATIBILITY_EVIDENCE_PATH,
)
_COMPATIBILITY_EVIDENCE_SCHEMA_VERSION = 1
_VERSION_PATTERN = re.compile(
    r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)"
)


@dataclass(frozen=True)
class DiagnosticCheck:
    check_id: str
    status: str
    summary: str
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.status not in ("pass", "warn", "fail"):
            raise ContractViolation("diagnostic status must be pass, warn, or fail")

    def to_dict(self) -> Mapping[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _CommandProbe:
    probe_id: str
    command: str
    protocol: str
    help_arguments: Tuple[str, ...]
    required_help_flags: Tuple[str, ...]
    features: Tuple[str, ...]
    role: str = "agent"


@dataclass(frozen=True)
class _DiscoveryProbe:
    agent_id: str
    display_name: str
    commands: Tuple[str, ...]
    homepage: str
    help_arguments: Tuple[str, ...] = ("--help",)
    version_arguments: Tuple[str, ...] = ("--version",)


_COMMAND_PROBES = (
    _CommandProbe(
        "claude-code",
        "claude",
        "json-envelope-v1",
        ("--help",),
        (
            "--print",
            "--output-format",
            "--json-schema",
            "--no-session-persistence",
            "--permission-mode",
            "--tools",
            "--max-budget-usd",
        ),
        ClaudeCodeExecutor(("claude",), safe_mode_flag=True).capabilities.features,
    ),
    _CommandProbe(
        "codex",
        "codex",
        "exec-jsonl-v1",
        ("exec", "--help"),
        ("--json", "--ephemeral", "--sandbox", "--output-schema"),
        CodexExecutor(("codex",)).capabilities.features,
    ),
    _CommandProbe(
        "pi-agent",
        "pi",
        "message-end-jsonl-v1",
        ("--help",),
        (
            "--mode",
            "--print",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--tools",
        ),
        PiAgentExecutor(("pi",)).capabilities.features,
    ),
    _CommandProbe(
        "opencode",
        "opencode",
        "run-jsonl-v1",
        ("run", "--help"),
        ("--format", "--model", "--pure"),
        OpenCodeExecutor(("opencode",)).capabilities.features,
    ),
    _CommandProbe(
        "orca",
        "orca",
        "orca-json-command-v1",
        ("orchestration", "--help"),
        ("run-create", "task-create", "worker-start"),
        (
            "workspace_isolation",
            "worker_lifecycle",
            "delivery_ack",
            "question_reply",
        ),
        role="orchestration",
    ),
)

_DISCOVERY_PROBES = (
    _DiscoveryProbe(
        "openclaw",
        "OpenClaw",
        ("openclaw",),
        "https://openclaw.ai/",
    ),
    _DiscoveryProbe(
        "hermes",
        "Hermes Agent",
        ("hermes",),
        "https://github.com/NousResearch/hermes-agent",
    ),
    _DiscoveryProbe(
        "aider",
        "Aider",
        ("aider",),
        "https://aider.chat/",
    ),
    _DiscoveryProbe(
        "gemini-cli",
        "Gemini CLI",
        ("gemini",),
        "https://github.com/google-gemini/gemini-cli",
    ),
    _DiscoveryProbe(
        "github-copilot-cli",
        "GitHub Copilot CLI",
        ("copilot",),
        "https://github.com/github/copilot-cli",
    ),
)


ProbeRunner = Callable[[Sequence[str], int], subprocess.CompletedProcess]
Which = Callable[[str], Optional[str]]


class AgentOSDistribution:
    """Owns compatibility, diagnostics, rehearsal, and portable release assembly."""

    def __init__(
        self,
        source_root: Optional[Path] = None,
        which: Which = shutil.which,
        runner: Optional[ProbeRunner] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.source_root = (
            source_root.resolve()
            if source_root is not None
            else Path(__file__).resolve().parents[1]
        )
        self._which = which
        self._runner = runner or self._subprocess_probe
        self._clock = clock

    def compatibility_matrix(self) -> Mapping[str, Any]:
        migrations = default_bundle_migrations()
        evidence = self._compatibility_evidence()
        return {
            "matrix_schema_version": COMPATIBILITY_MATRIX_VERSION,
            "runtime": {
                "python_minimum": ".".join(str(item) for item in _MINIMUM_PYTHON),
                "python_running": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "third_party_runtime_dependencies": [],
                "platform_requirements": [
                    "POSIX advisory file locks",
                    "atomic same-filesystem directory replacement",
                ],
            },
            "state": {
                "root_schema_version": AGENT_OS_SCHEMA_VERSION,
                "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
                "importable_bundle_schema_versions": list(
                    migrations.compatible_source_versions(BUNDLE_SCHEMA_VERSION)
                ),
                "provider_governance_schema_version": PROVIDER_GOVERNANCE_SCHEMA_VERSION,
                "orca_coordinator_schema_version": ORCA_COORDINATOR_SCHEMA_VERSION,
                "controlled_merge_schema_version": CONTROLLED_MERGE_SCHEMA_VERSION,
            },
            "adapters": [
                {
                    "executor_id": probe.probe_id,
                    "command": probe.command,
                    "role": probe.role,
                    "protocol": probe.protocol,
                    "features": list(probe.features),
                    "help_arguments": list(probe.help_arguments),
                    "required_help_flags": list(probe.required_help_flags),
                    "diagnostic_side_effect": "help/version only; no model or Orca object creation",
                    "support": {
                        "policy": "exact version and platform with matching protocol evidence",
                        "verified_versions": evidence["adapters"][probe.probe_id],
                        "verified_platforms": [evidence["platform"]],
                        "last_verified_at": max(
                            item["validated_at"]
                            for item in evidence["adapters"][probe.probe_id]
                        ),
                    },
                }
                for probe in _COMMAND_PROBES
            ],
            "discoverable_agents": [
                {
                    "agent_id": probe.agent_id,
                    "display_name": probe.display_name,
                    "commands": list(probe.commands),
                    "homepage": probe.homepage,
                    "integration_status": "discovery_only",
                    "diagnostic_side_effect": "help/version only; no model calls",
                }
                for probe in _DISCOVERY_PROBES
            ],
            "evidence": {
                "schema_version": evidence["schema_version"],
                "verification_scope": evidence["verification_scope"],
                "platform": evidence["platform"],
            },
            "portability": {
                "included": [
                    "runtime source",
                    "tests and fake protocol fixtures",
                    "examples",
                    "portable Agent OS state bundle",
                    "compatibility matrix",
                    "restore checklist",
                ],
                "excluded": [
                    "credentials",
                    "worktrees",
                    "runtime artifacts",
                    "raw prompts and inputs",
                    "agent product sessions",
                    "unverified results",
                    "active leases",
                    "routing governance runtime",
                    "publication receipts",
                    "controlled merge runtime",
                ],
            },
        }

    def certified_adapter_commands(self) -> Mapping[str, str]:
        commands = {}
        for probe in _COMMAND_PROBES:
            if probe.role != "agent":
                continue
            check = self._command_check(probe)
            path = check.details.get("path")
            if check.status == "pass" and isinstance(path, str):
                commands[probe.probe_id] = path
        return commands

    def doctor(self, agent_os_root: Path) -> Mapping[str, Any]:
        """Diagnose one portable state root supplied to the low-level interface."""

        root = agent_os_root.expanduser().absolute()
        return self._diagnostic_report(
            root,
            ("agent-os", "agent-os", "doctor", "--root", str(root)),
        )

    def _diagnostic_report(
        self, agent_os_root: Path, rerun: Sequence[str]
    ) -> Mapping[str, Any]:
        checks = [self._python_check(), self._source_check()]
        checks.append(self._filesystem_check(agent_os_root))
        checks.append(self._state_check(agent_os_root))
        checks.extend(self._command_check(probe) for probe in _COMMAND_PROBES)
        discovery_checks = [
            self._discovery_check(probe) for probe in _DISCOVERY_PROBES
        ]
        checks.extend(discovery_checks)
        statuses = {item.check_id: item.status for item in checks}
        ready_executors = [
            probe.probe_id
            for probe in _COMMAND_PROBES
            if probe.role == "agent"
            and statuses.get(f"adapter:{probe.probe_id}") == "pass"
        ]
        blocking_checks = [
            item.check_id
            for item in checks
            if item.status == "fail"
            and not item.check_id.startswith(("adapter:", "orchestration:"))
        ]
        if blocking_checks:
            readiness = "blocked"
        elif not ready_executors:
            readiness = "needs_agent"
        elif any(item.status != "pass" for item in checks):
            readiness = "ready_with_warnings"
        else:
            readiness = "ready"
        return {
            "diagnostic_schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "observed_at": self._clock(),
            "root": str(agent_os_root.resolve()),
            "healthy": all(item.status != "fail" for item in checks),
            "readiness": readiness,
            "blocking_checks": blocking_checks,
            "ready_for_agent_execution": bool(ready_executors),
            "ready_executors": ready_executors,
            "discovered_agents": [
                dict(item.details)
                for item in discovery_checks
                if item.details["discovery_status"] != "not_installed"
            ],
            "ready_for_orca": statuses.get("orchestration:orca") == "pass",
            "checks": [item.to_dict() for item in checks],
            "next_actions": self._next_actions(
                agent_os_root, checks, bool(ready_executors), rerun
            ),
            "compatibility": self.compatibility_matrix(),
        }

    def setup(self, home: Path) -> Mapping[str, Any]:
        """Initialize ``<home>/state`` and return a no-model first-use diagnostic.

        Operational data may already exist under ``home/runtime`` or
        ``home/tasks``. Portable policy and learning state always lives below
        ``home/state`` so it can be exported without task prompts or logs.
        """

        home = home.expanduser().absolute()
        state_root = home / "state"
        initialized = False
        if (
            not home.is_symlink()
            and (not home.exists() or home.is_dir())
            and not (home / "manifest.json").exists()
            and not (home / "manifest.json").is_symlink()
            and not state_root.is_symlink()
            and (
                not state_root.exists()
                or (state_root.is_dir() and not any(state_root.iterdir()))
            )
        ):
            AgentOS(state_root)
            initialized = True
        report = self._diagnostic_report(
            state_root,
            ("agent-os", "setup", "--home", str(home.resolve())),
        )
        return {**report, "initialized": initialized, "model_calls": 0}

    def rehearse(
        self, agent_os_root: Path, bundle: Optional[Path] = None
    ) -> Mapping[str, Any]:
        source = self._open_existing(agent_os_root)
        with tempfile.TemporaryDirectory(
            prefix=f".{agent_os_root.name}.rehearsal-",
            dir=str(agent_os_root.parent),
        ) as directory:
            rehearsal_root = Path(directory)
            source_before = source.export_bundle(
                rehearsal_root / "source-before.bundle"
            )
            source_before_digest = self._portable_state_digest(source_before)
            backup = bundle
            if backup is None:
                backup = source_before
            input_digest = self._portable_state_digest(backup)
            restored = AgentOS(rehearsal_root / "restored")
            imported = restored.import_bundle(backup)
            restored_bundle = restored.export_bundle(
                rehearsal_root / "restored.bundle"
            )
            restored_digest = self._portable_state_digest(restored_bundle)
            confirmed = AgentOS(rehearsal_root / "confirmed")
            confirmed.import_bundle(restored_bundle)
            confirmed_bundle = confirmed.export_bundle(
                rehearsal_root / "confirmed.bundle"
            )
            confirmed_digest = self._portable_state_digest(confirmed_bundle)
            if restored_digest != confirmed_digest:
                raise ContractViolation(
                    "Agent OS rehearsal did not produce a stable restored state"
                )
            if not imported["migration"]["steps"] and input_digest != restored_digest:
                raise ContractViolation("Agent OS rehearsal changed a current bundle")
            source_after = source.export_bundle(rehearsal_root / "source-after.bundle")
            source_unchanged = (
                self._portable_state_digest(source_after) == source_before_digest
            )
            if not source_unchanged:
                raise ContractViolation("Agent OS rehearsal modified source state")
            restored_status = restored.status()
        return {
            "verified": True,
            "input_state_digest": input_digest,
            "portable_state_digest": restored_digest,
            "root_schema_version": restored_status["schema_version"],
            "bundle_schema_version": restored_status["bundle_schema_version"],
            "imported_files": imported["imported_files"],
            "migration": imported["migration"],
            "source_unchanged": source_unchanged,
        }

    def create_release(
        self, agent_os_root: Path, output: Path
    ) -> Mapping[str, Any]:
        source = self._open_existing(agent_os_root)
        if output.exists() or output.is_symlink():
            raise ContractViolation(f"Agent OS release target already exists: {output}")
        try:
            release_sources = self._runtime_sources()
        except ContractViolation as error:
            raise ContractViolation(
                "Agent OS release requires a complete source checkout: " + str(error)
            ) from error
        output.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.", dir=str(output.parent))
        )
        try:
            self._copy_runtime(staging, release_sources)
            self._write_json(
                staging / "COMPATIBILITY.json", self.compatibility_matrix()
            )
            (staging / "RESTORE.md").write_text(
                _RESTORE_GUIDE, encoding="utf-8"
            )
            state_bundle = source.export_bundle(staging / "state.bundle")
            records = self._release_records(staging)
            release_id = self._digest_json(
                {
                    "release_schema_version": RELEASE_SCHEMA_VERSION,
                    "files": records,
                }
            )
            self._write_json(
                staging / "release-manifest.json",
                {
                    "kind": _RELEASE_KIND,
                    "schema_version": RELEASE_SCHEMA_VERSION,
                    "release_id": release_id,
                    "created_at": self._clock(),
                    "state_digest": self._portable_state_digest(state_bundle),
                    "files": records,
                },
            )
            verification = self.verify_release(staging)
            os.replace(str(staging), str(output))
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return {
            **verification,
            "release": str(output.resolve()),
        }

    def verify_release(self, release: Path) -> Mapping[str, Any]:
        if not release.is_dir() or release.is_symlink():
            raise ContractViolation("Agent OS release must be a real directory")
        manifest = read_json_object(release / "release-manifest.json", label="release JSON")
        if (
            manifest.get("kind") != _RELEASE_KIND
            or manifest.get("schema_version") != RELEASE_SCHEMA_VERSION
        ):
            raise ContractViolation("unsupported Agent OS release manifest")
        declared = self._declared_release_files(manifest)
        actual = self._release_files(release)
        if set(declared) != set(actual):
            raise ContractViolation("Agent OS release files do not match its manifest")
        for relative, metadata in declared.items():
            data = actual[relative].read_bytes()
            if len(data) != metadata["size"] or sha256_hex(data) != metadata["sha256"]:
                raise ContractViolation(
                    f"Agent OS release checksum mismatch: {relative.as_posix()}"
                )
        for relative in _REQUIRED_RELEASE_PATHS:
            if relative not in actual:
                raise ContractViolation(
                    f"Agent OS release is missing runtime source: {relative.as_posix()}"
                )
        expected_release_id = self._digest_json(
            {
                "release_schema_version": RELEASE_SCHEMA_VERSION,
                "files": manifest["files"],
            }
        )
        if manifest.get("release_id") != expected_release_id:
            raise ContractViolation("Agent OS release identity does not match its files")
        compatibility = read_json_object(release / "COMPATIBILITY.json", label="release JSON")
        state = compatibility.get("state")
        if (
            not isinstance(state, dict)
            or state.get("root_schema_version") != AGENT_OS_SCHEMA_VERSION
            or state.get("bundle_schema_version") != BUNDLE_SCHEMA_VERSION
        ):
            raise ContractViolation("Agent OS release compatibility matrix is unsupported")
        state_bundle = release / "state.bundle"
        source_digest = self._portable_state_digest(state_bundle)
        if manifest.get("state_digest") != source_digest:
            raise ContractViolation("Agent OS release state digest mismatch")
        with tempfile.TemporaryDirectory(
            prefix=f".{release.name}.verify-"
        ) as directory:
            root = Path(directory)
            restored = AgentOS(root / "restored")
            imported = restored.import_bundle(state_bundle)
            round_trip = restored.export_bundle(root / "round-trip.bundle")
            restored_digest = self._portable_state_digest(round_trip)
        if source_digest != restored_digest:
            raise ContractViolation("Agent OS release restore rehearsal diverged")
        return {
            "verified": True,
            "release_id": manifest.get("release_id"),
            "release_schema_version": RELEASE_SCHEMA_VERSION,
            "files": len(declared),
            "state_digest": source_digest,
            "restored_files": imported["imported_files"],
        }

    def _python_check(self) -> DiagnosticCheck:
        running = (sys.version_info.major, sys.version_info.minor)
        status = "pass" if running >= _MINIMUM_PYTHON else "fail"
        return DiagnosticCheck(
            "runtime:python",
            status,
            "Python runtime is compatible" if status == "pass" else "Python runtime is too old",
            {
                "minimum": ".".join(str(item) for item in _MINIMUM_PYTHON),
                "running": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            },
        )

    def _source_check(self) -> DiagnosticCheck:
        try:
            sources = self._installed_runtime_sources()
        except ContractViolation as error:
            return DiagnosticCheck(
                "runtime:source",
                "fail",
                "Installed runtime layout is incomplete",
                {"error": str(error)},
            )
        return DiagnosticCheck(
            "runtime:source",
            "pass",
            "Installed runtime layout is complete",
            {"files": len(sources)},
        )

    def _filesystem_check(self, agent_os_root: Path) -> DiagnosticCheck:
        parent = agent_os_root.parent
        if not parent.is_dir():
            return DiagnosticCheck(
                "runtime:filesystem",
                "fail",
                "Agent OS parent directory does not exist",
                {"parent": str(parent)},
            )
        directory: Optional[Path] = None
        try:
            directory = Path(
                tempfile.mkdtemp(prefix=".grapheng-doctor-", dir=str(parent))
            )
            source = directory / "source"
            target = directory / "target"
            source.write_text("probe", encoding="utf-8")
            with (directory / "lock").open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            os.replace(str(source), str(target))
            if target.read_text(encoding="utf-8") != "probe":
                raise OSError("atomic replacement did not preserve data")
        except OSError as error:
            return DiagnosticCheck(
                "runtime:filesystem",
                "fail",
                "Filesystem lacks required lock or atomic replacement behavior",
                {"error": str(error)},
            )
        finally:
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
        return DiagnosticCheck(
            "runtime:filesystem",
            "pass",
            "Filesystem supports POSIX locks and atomic replacement",
            {},
        )

    def _state_check(self, agent_os_root: Path) -> DiagnosticCheck:
        try:
            status = self._open_existing(agent_os_root).status()
        except (ContractViolation, KeyError, OSError, TypeError, ValueError) as error:
            return DiagnosticCheck(
                "state:agent-os",
                "fail",
                "Agent OS state is unavailable or invalid",
                {"error": str(error)},
            )
        return DiagnosticCheck(
            "state:agent-os",
            "pass",
            "Agent OS state and schemas are readable",
            {
                "root_schema_version": status["schema_version"],
                "bundle_schema_version": status["bundle_schema_version"],
                "reuse_entries": status["reuse"]["entries"]["valid"],
            },
        )

    def _command_check(self, probe: _CommandProbe) -> DiagnosticCheck:
        prefix = "adapter" if probe.role == "agent" else "orchestration"
        path = self._which(probe.command)
        if path is None:
            return DiagnosticCheck(
                f"{prefix}:{probe.probe_id}",
                "warn",
                f"Optional command {probe.command} is not installed",
                {"command": probe.command, "protocol": probe.protocol},
            )
        try:
            help_result = self._runner((path, *probe.help_arguments), 10)
            version_result = self._runner((path, "--version"), 10)
        except (OSError, subprocess.SubprocessError) as error:
            return DiagnosticCheck(
                f"{prefix}:{probe.probe_id}",
                "fail",
                f"Command {probe.command} could not be inspected",
                {"command": probe.command, "error": str(error)},
            )
        help_text = f"{help_result.stdout or ''}\n{help_result.stderr or ''}"
        missing = [
            flag for flag in probe.required_help_flags if flag not in help_text
        ]
        version = self._installed_version(probe, path, version_result)
        evidence = self._compatibility_evidence()
        verified = evidence["adapters"][probe.probe_id]
        verified_platforms = [evidence["platform"]]
        running_platform = {
            "architecture": platform.machine(),
            "operating_system": platform.system(),
        }
        verified_versions = [item["version"] for item in verified]
        last_verified_at = max(item["validated_at"] for item in verified)
        if help_result.returncode != 0 or missing:
            return DiagnosticCheck(
                f"{prefix}:{probe.probe_id}",
                "fail",
                f"Command {probe.command} does not satisfy the Adapter protocol",
                {
                    "command": probe.command,
                    "path": path,
                    "protocol": probe.protocol,
                    "help_exit_code": help_result.returncode,
                    "missing_help_flags": missing,
                    "version": version,
                    "support_status": "protocol_mismatch",
                    "verified_versions": verified_versions,
                    "running_platform": running_platform,
                    "verified_platforms": verified_platforms,
                    "last_verified_at": last_verified_at,
                },
            )
        if version not in verified_versions:
            support_status = "unverified_version"
        elif running_platform not in verified_platforms:
            support_status = "unverified_platform"
        else:
            support_status = "verified"
        version_status = (
            "pass"
            if version_result.returncode == 0 and support_status == "verified"
            else "warn"
        )
        if version_status == "pass":
            summary = f"Command {probe.command} satisfies the verified Adapter protocol"
        elif support_status == "verified":
            summary = f"Command {probe.command} matches a verified version but its version probe failed"
        elif support_status == "unverified_platform":
            summary = f"Command {probe.command} matches a verified version on an unverified platform"
        else:
            summary = f"Command {probe.command} matches the protocol but its version is not verified"
        return DiagnosticCheck(
            f"{prefix}:{probe.probe_id}",
            version_status,
            summary,
            {
                "command": probe.command,
                "path": path,
                "protocol": probe.protocol,
                "version": version,
                "support_status": support_status,
                "verified_versions": verified_versions,
                "running_platform": running_platform,
                "verified_platforms": verified_platforms,
                "last_verified_at": last_verified_at,
            },
        )

    def _discovery_check(self, probe: _DiscoveryProbe) -> DiagnosticCheck:
        resolved = next(
            (
                (command, path)
                for command in probe.commands
                if (path := self._which(command)) is not None
            ),
            None,
        )
        base_details = {
            "agent_id": probe.agent_id,
            "display_name": probe.display_name,
            "command_candidates": list(probe.commands),
            "homepage": probe.homepage,
            "integration_status": "adapter_not_available",
        }
        if resolved is None:
            return DiagnosticCheck(
                f"discovery:{probe.agent_id}",
                "pass",
                f"{probe.display_name} is not installed",
                {**base_details, "discovery_status": "not_installed"},
            )
        command, path = resolved
        installed_details = {**base_details, "command": command, "path": path}
        try:
            help_result = self._runner((path, *probe.help_arguments), 10)
            version_result = self._runner((path, *probe.version_arguments), 10)
        except (OSError, subprocess.SubprocessError) as error:
            return DiagnosticCheck(
                f"discovery:{probe.agent_id}",
                "warn",
                f"{probe.display_name} was found but could not be safely inspected",
                {
                    **installed_details,
                    "discovery_status": "inspection_failed",
                    "version": None,
                    "error": str(error),
                },
            )
        version_text = "\n".join(
            (
                version_result.stdout or "",
                version_result.stderr or "",
            )
        )
        match = _VERSION_PATTERN.search(version_text)
        version = match.group(1) if match is not None else None
        if help_result.returncode != 0:
            discovery_status = "help_probe_failed"
            summary = f"{probe.display_name} was found but its help probe failed"
        elif version_result.returncode != 0 or version is None:
            discovery_status = "version_unknown"
            summary = f"{probe.display_name} was found but its version is unknown"
        else:
            discovery_status = "installed_unverified"
            summary = (
                f"{probe.display_name} was found, but no certified Agent OS Adapter exists"
            )
        return DiagnosticCheck(
            f"discovery:{probe.agent_id}",
            "warn",
            summary,
            {
                **installed_details,
                "discovery_status": discovery_status,
                "version": version,
                "help_exit_code": help_result.returncode,
                "version_exit_code": version_result.returncode,
            },
        )

    def _next_actions(
        self,
        agent_os_root: Path,
        checks: Sequence[DiagnosticCheck],
        ready_for_agent_execution: bool,
        rerun: Sequence[str],
    ) -> Sequence[Mapping[str, Any]]:
        rerun = list(rerun)
        actions: List[Mapping[str, Any]] = []
        for check in checks:
            if check.status == "pass":
                continue
            if check.check_id == "state:agent-os" and agent_os_root.exists():
                actions.append(
                    {
                        "action_id": "restore_or_choose_state",
                        "check_id": check.check_id,
                        "priority": "required",
                        "summary": (
                            "Restore a valid Agent OS state, or choose a new empty "
                            "directory with --home; existing files were not modified."
                        ),
                    }
                )
                continue
            action = self._action_for_check(
                check,
                rerun,
                ready_for_agent_execution,
            )
            if action is not None:
                actions.append(action)
        if not ready_for_agent_execution:
            actions.append(
                {
                    "action_id": "enable_agent_execution",
                    "check_id": "agent-execution",
                    "priority": "required",
                    "summary": (
                        "Install or repair at least one supported coding Agent, "
                        "then rerun setup."
                    ),
                    "command": rerun,
                }
            )
        priority_order = {"required": 0, "recommended": 1, "optional": 2}
        return sorted(actions, key=lambda item: priority_order[item["priority"]])

    @staticmethod
    def _action_for_check(
        check: DiagnosticCheck,
        rerun: Sequence[str],
        ready_for_agent_execution: bool,
    ) -> Optional[Mapping[str, Any]]:
        check_id = check.check_id
        if check_id == "runtime:python":
            return {
                "action_id": "upgrade_python",
                "check_id": check_id,
                "priority": "required",
                "summary": "Install Python 3.9 or newer, then rerun setup.",
                "command": list(rerun),
            }
        if check_id == "runtime:source":
            return {
                "action_id": "repair_installation",
                "check_id": check_id,
                "priority": "required",
                "summary": "Reinstall Agent OS from a complete release, then rerun setup.",
                "command": list(rerun),
            }
        if check_id == "runtime:filesystem":
            return {
                "action_id": "choose_supported_filesystem",
                "check_id": check_id,
                "priority": "required",
                "summary": (
                    "Move Agent OS state to a local filesystem with POSIX locks and "
                    "atomic replacement."
                ),
                "command": list(rerun),
            }
        if check_id == "state:agent-os":
            return {
                "action_id": "initialize_state",
                "check_id": check_id,
                "priority": "required",
                "summary": "Initialize or restore the Agent OS state directory.",
                "command": list(rerun),
            }
        if check_id.startswith("discovery:"):
            target = check.details["display_name"]
            return {
                "action_id": "integrate_discovered_agent",
                "check_id": check_id,
                "priority": "optional",
                "summary": (
                    f"{target} is installed but not yet available to Agent OS; "
                    "add and certify an Adapter before enabling it for execution."
                ),
            }
        if not check_id.startswith(("adapter:", "orchestration:")):
            return None
        target = check_id.split(":", 1)[1]
        role = "Orca" if check_id.startswith("orchestration:") else "coding Agent"
        priority = (
            "optional"
            if check_id.startswith("orchestration:") or ready_for_agent_execution
            else "recommended"
        )
        support_status = check.details.get("support_status")
        if support_status in ("unverified_version", "unverified_platform"):
            summary = (
                f"{target} matches the protocol but is not certified here; use a "
                f"verified version/platform or add compatibility evidence before enabling it."
            )
            action_id = "certify_adapter"
        elif support_status == "protocol_mismatch" or check.status == "fail":
            summary = (
                f"Repair or change the installed {target} version until its required "
                "protocol flags are available."
            )
            action_id = "repair_adapter"
        else:
            summary = (
                f"Install {target} from its official instructions if this {role} is needed."
            )
            action_id = "install_adapter"
        return {
            "action_id": action_id,
            "check_id": check_id,
            "priority": priority,
            "summary": summary,
            "command": list(rerun),
        }

    def _compatibility_evidence(self) -> Mapping[str, Any]:
        path = self.source_root / _COMPATIBILITY_EVIDENCE_PATH
        evidence = read_json_object(path, label="release JSON")
        adapters = evidence.get("adapters")
        evidence_platform = evidence.get("platform")
        if (
            evidence.get("schema_version")
            != _COMPATIBILITY_EVIDENCE_SCHEMA_VERSION
            or not isinstance(evidence.get("verification_scope"), str)
            or not isinstance(evidence_platform, dict)
            or set(evidence_platform) != {"architecture", "operating_system"}
            or not all(
                isinstance(value, str) and value.strip()
                for value in evidence_platform.values()
            )
            or not isinstance(adapters, dict)
        ):
            raise ContractViolation("invalid compatibility evidence envelope")
        expected_ids = {probe.probe_id for probe in _COMMAND_PROBES}
        if set(adapters) != expected_ids:
            raise ContractViolation("compatibility evidence Adapter set is incomplete")
        for probe in _COMMAND_PROBES:
            records = adapters[probe.probe_id]
            if not isinstance(records, list) or not records:
                raise ContractViolation(
                    f"compatibility evidence is missing versions for {probe.probe_id}"
                )
            versions = set()
            for record in records:
                if (
                    not isinstance(record, dict)
                    or record.get("protocol") != probe.protocol
                    or not isinstance(record.get("version"), str)
                    or not record["version"].strip()
                    or not isinstance(record.get("validated_at"), str)
                    or not record["validated_at"].strip()
                    or not isinstance(record.get("version_source"), str)
                    or not record["version_source"].strip()
                    or record["version"] in versions
                ):
                    raise ContractViolation(
                        f"invalid compatibility evidence for {probe.probe_id}"
                    )
                canary = record.get("runtime_canary")
                if canary is not None:
                    self._validate_runtime_canary(canary, probe.probe_id)
                versions.add(record["version"])
        return evidence

    @staticmethod
    def _validate_runtime_canary(value: Any, executor_id: str) -> None:
        required = {
            "cost_usd",
            "max_cost_usd",
            "model_call",
            "prompt_or_response_persisted",
            "structured_output",
            "success",
            "tokens_used",
            "tools",
            "validated_at",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ContractViolation(
                f"invalid runtime canary evidence for {executor_id}"
            )
        cost = value["cost_usd"]
        maximum = value["max_cost_usd"]
        tokens = value["tokens_used"]
        tools = value["tools"]
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or cost < 0
            or isinstance(maximum, bool)
            or not isinstance(maximum, (int, float))
            or not math.isfinite(maximum)
            or maximum <= 0
            or cost > maximum
            or isinstance(tokens, bool)
            or not isinstance(tokens, int)
            or tokens < 0
            or not isinstance(tools, list)
            or not tools
            or any(not isinstance(tool, str) or not tool for tool in tools)
            or value["model_call"] is not True
            or value["prompt_or_response_persisted"] is not False
            or value["structured_output"] is not True
            or value["success"] is not True
            or not isinstance(value["validated_at"], str)
            or not value["validated_at"].strip()
        ):
            raise ContractViolation(
                f"invalid runtime canary evidence for {executor_id}"
            )

    @classmethod
    def _installed_version(
        cls,
        probe: _CommandProbe,
        path: str,
        result: subprocess.CompletedProcess,
    ) -> Optional[str]:
        text = f"{result.stdout or ''}\n{result.stderr or ''}"
        match = _VERSION_PATTERN.search(text)
        if match is not None:
            return match.group(1)
        if probe.probe_id != "orca":
            return None
        return cls._macos_bundle_version(Path(path))

    @staticmethod
    def _macos_bundle_version(command: Path) -> Optional[str]:
        try:
            resolved = command.resolve(strict=True)
        except OSError:
            return None
        app = next(
            (parent for parent in resolved.parents if parent.suffix == ".app"),
            None,
        )
        if app is None:
            return None
        try:
            with (app / "Contents" / "Info.plist").open("rb") as handle:
                value = plistlib.load(handle).get("CFBundleShortVersionString")
        except (OSError, plistlib.InvalidFileException):
            return None
        return value if isinstance(value, str) and value.strip() else None

    def _copy_runtime(
        self, destination: Path, sources: Optional[Sequence[Path]] = None
    ) -> None:
        for source in sources if sources is not None else self._runtime_sources():
            relative = source.relative_to(self.source_root)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    def _installed_runtime_sources(self) -> Tuple[Path, ...]:
        if not self.source_root.is_dir() or self.source_root.is_symlink():
            raise ContractViolation("Graph Engineering install root must be a real directory")
        sources = []
        for relative in _INSTALLED_RUNTIME_FILES:
            source = self.source_root / relative
            if not source.is_file() or source.is_symlink():
                raise ContractViolation(f"missing installed runtime file: {relative.as_posix()}")
            sources.append(source)
        return tuple(sources)

    def _runtime_sources(self) -> Tuple[Path, ...]:
        if not self.source_root.is_dir() or self.source_root.is_symlink():
            raise ContractViolation("Graph Engineering source root must be a real directory")
        sources = []
        for relative in _SOURCE_FILES:
            source = self.source_root / relative
            if not source.is_file() or source.is_symlink():
                raise ContractViolation(f"missing runtime source: {relative.as_posix()}")
            sources.append(source)
        for directory, pattern in _SOURCE_GLOBS:
            root = self.source_root / directory
            matches = sorted(root.glob(pattern))
            if not matches:
                raise ContractViolation(f"missing runtime sources under {directory.as_posix()}")
            for source in matches:
                if not source.is_file() or source.is_symlink():
                    raise ContractViolation(
                        f"runtime source must be a regular file: {source.name}"
                    )
                sources.append(source)
        return tuple(sorted(set(sources)))

    @staticmethod
    def _open_existing(root: Path) -> AgentOS:
        if (
            not root.is_dir()
            or root.is_symlink()
            or not (root / "manifest.json").is_file()
        ):
            raise ContractViolation("Agent OS root does not exist or has no manifest")
        return AgentOS(root)

    @staticmethod
    def _subprocess_probe(
        command: Sequence[str], timeout_seconds: int
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

    @classmethod
    def _release_records(cls, root: Path) -> Sequence[Mapping[str, Any]]:
        return [
            {
                "path": relative.as_posix(),
                "sha256": sha256_hex(path.read_bytes()),
                "size": path.stat().st_size,
            }
            for relative, path in sorted(
                cls._release_files(root).items(),
                key=lambda item: item[0].as_posix(),
            )
        ]

    @staticmethod
    def _release_files(root: Path) -> Mapping[PurePosixPath, Path]:
        result: Dict[PurePosixPath, Path] = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ContractViolation("Agent OS release cannot contain symlinks")
            if not path.is_file():
                continue
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if relative == PurePosixPath("release-manifest.json"):
                continue
            result[relative] = path
        return result

    @staticmethod
    def _declared_release_files(
        manifest: Mapping[str, Any]
    ) -> Mapping[PurePosixPath, Mapping[str, Any]]:
        files = manifest.get("files")
        if not isinstance(files, list):
            raise ContractViolation("Agent OS release files must be an array")
        result = {}
        for item in files:
            if not isinstance(item, dict):
                raise ContractViolation("Agent OS release file record must be an object")
            raw_path = item.get("path")
            digest = item.get("sha256")
            size = item.get("size")
            if not isinstance(raw_path, str) or "\\" in raw_path:
                raise ContractViolation("Agent OS release path must be a POSIX string")
            relative = PurePosixPath(raw_path)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or raw_path != relative.as_posix()
                or relative == PurePosixPath("release-manifest.json")
                or relative in result
            ):
                raise ContractViolation(f"unsafe Agent OS release path: {raw_path}")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise ContractViolation("invalid Agent OS release file metadata")
            result[relative] = {"sha256": digest, "size": size}
        return result

    @classmethod
    def _portable_state_digest(cls, bundle: Path) -> str:
        manifest = read_json_object(bundle / "manifest.json", label="release JSON")
        records = manifest.get("files")
        if not isinstance(records, list):
            raise ContractViolation("Agent OS bundle files must be an array")
        identity = []
        for item in records:
            if not isinstance(item, dict):
                raise ContractViolation("Agent OS bundle file record must be an object")
            identity.append(
                {
                    "path": item.get("path"),
                    "sha256": item.get("sha256"),
                    "size": item.get("size"),
                }
            )
        return cls._digest_json(sorted(identity, key=lambda item: str(item["path"])))

    @classmethod
    def _digest_json(cls, value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256_hex(encoded)

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        path.write_text(data, encoding="utf-8")

_RESTORE_GUIDE = """# Agent OS 发行包恢复清单

本目录包含运行时源码、测试、示例、兼容矩阵和经校验的 `state.bundle`。
它不包含凭据、worktree、运行 Artifact、原始 prompt/input、活动 lease 或待合并状态。

1. 使用 Python 3.9 或更高版本。
2. 在目标机器单独安装并授权 Claude Code、Codex、Pi Agent 和可选 Orca。
3. 先验证发行包，再导入到全新状态目录。

```bash
PYTHONPATH=. python3 -m grapheng.cli agent-os verify-release --release .
PYTHONPATH=. python3 -m grapheng.cli agent-os import --root /path/to/new-agent-os --bundle ./state.bundle
PYTHONPATH=. python3 -m grapheng.cli agent-os doctor --root /path/to/new-agent-os --source-root .
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

4. 检查 `COMPATIBILITY.json`，确认本机 CLI 帮助中仍包含 Adapter 所需参数。
5. 先用只读、低成本、有 Reality Anchor 的任务灰度。
6. 确认费用、权限、审批、验证和恢复策略后，再开放写工具与 Orca worker。
"""
