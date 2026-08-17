import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .adapters import ClaudeCodeExecutor, CodexExecutor, PiAgentExecutor
from .coordinator import ORCA_COORDINATOR_SCHEMA_VERSION
from .errors import ContractViolation
from .governance import PROVIDER_GOVERNANCE_SCHEMA_VERSION
from .merge import CONTROLLED_MERGE_SCHEMA_VERSION
from .migrations import BUNDLE_SCHEMA_VERSION, default_bundle_migrations
from .os import AGENT_OS_SCHEMA_VERSION, AgentOS


RELEASE_SCHEMA_VERSION = 1
DIAGNOSTIC_SCHEMA_VERSION = 1
COMPATIBILITY_MATRIX_VERSION = 1
_RELEASE_KIND = "grapheng-agent-os-release"
_MINIMUM_PYTHON = (3, 9)
_SOURCE_FILES = (
    Path("README.md"),
    Path(".workflow/agent-os/plan.md"),
    Path(".workflow/agent-os/results.md"),
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
            "--safe-mode",
            "--permission-mode",
            "--tools",
            "--max-budget-usd",
        ),
        ClaudeCodeExecutor(("claude",)).capabilities.features,
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
                }
                for probe in _COMMAND_PROBES
            ],
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
                    "unverified results",
                    "active leases",
                    "routing governance runtime",
                    "publication receipts",
                    "controlled merge runtime",
                ],
            },
        }

    def doctor(self, agent_os_root: Path) -> Mapping[str, Any]:
        checks = [self._python_check(), self._source_check()]
        checks.append(self._filesystem_check(agent_os_root))
        checks.append(self._state_check(agent_os_root))
        checks.extend(self._command_check(probe) for probe in _COMMAND_PROBES)
        statuses = {item.check_id: item.status for item in checks}
        return {
            "diagnostic_schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "healthy": all(item.status != "fail" for item in checks),
            "ready_for_agent_execution": any(
                statuses.get(f"adapter:{probe.probe_id}") == "pass"
                for probe in _COMMAND_PROBES
                if probe.role == "agent"
            ),
            "ready_for_orca": statuses.get("orchestration:orca") == "pass",
            "checks": [item.to_dict() for item in checks],
            "compatibility": self.compatibility_matrix(),
        }

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
        output.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.", dir=str(output.parent))
        )
        try:
            self._copy_runtime(staging)
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
        manifest = self._read_json(release / "release-manifest.json")
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
            if len(data) != metadata["size"] or self._sha256(data) != metadata["sha256"]:
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
        compatibility = self._read_json(release / "COMPATIBILITY.json")
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
            sources = self._runtime_sources()
        except ContractViolation as error:
            return DiagnosticCheck(
                "runtime:source",
                "fail",
                "Runtime source layout is incomplete",
                {"error": str(error)},
            )
        return DiagnosticCheck(
            "runtime:source",
            "pass",
            "Runtime source layout is complete",
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
        version = (version_result.stdout or version_result.stderr or "").strip()
        version = version.splitlines()[0][:200] if version else None
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
                },
            )
        version_status = "pass" if version_result.returncode == 0 else "warn"
        return DiagnosticCheck(
            f"{prefix}:{probe.probe_id}",
            version_status,
            f"Command {probe.command} satisfies the declared protocol",
            {
                "command": probe.command,
                "path": path,
                "protocol": probe.protocol,
                "version": version,
            },
        )

    def _copy_runtime(self, destination: Path) -> None:
        for source in self._runtime_sources():
            relative = source.relative_to(self.source_root)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

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
                "sha256": cls._sha256(path.read_bytes()),
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
        manifest = cls._read_json(bundle / "manifest.json")
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

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @classmethod
    def _digest_json(cls, value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return cls._sha256(encoded)

    @staticmethod
    def _write_json(path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        path.write_text(data, encoding="utf-8")

    @staticmethod
    def _read_json(path: Path) -> Mapping[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(f"invalid release JSON {path.name}: {error}") from error
        if not isinstance(value, dict):
            raise ContractViolation(f"release JSON {path.name} must be an object")
        return value


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
