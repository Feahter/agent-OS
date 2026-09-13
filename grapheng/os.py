import json
import os
import shutil
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional, Tuple

from ._store import (
    atomic_json_write,
    read_json_object,
    sha256_hex,
)
from .console import ApprovalInbox
from .errors import ContractViolation
from .governance import ProviderGovernanceStore
from .learning import RSILoop
from .migrations import (
    BUNDLE_SCHEMA_VERSION,
    BundleMigrationRegistry,
    MigrationReport,
    default_bundle_migrations,
)
from .model import GraphSpec
from .optimization import RegressionSuite, RSIOptimizationLab
from .reuse import VerifiedArtifactCache, VerifiedReuseRecord
from .routing import PolicyRouter, ProviderPolicy

AGENT_OS_SCHEMA_VERSION = 1
AGENT_OS_DIRECTORIES = (
    "learning",
    "optimization",
    "reuse",
    "approvals",
    "routing",
)
_ROOT_KIND = "grapheng-agent-os-root"
_BUNDLE_KIND = "grapheng-agent-os-bundle"


def state_root_for_home(home: Path) -> Path:
    """Return the portable state root for one operational Agent OS home.

    Preview builds briefly initialized portable state directly in ``home``.
    Refuse that legacy layout instead of silently creating a second state tree;
    ``agent-os setup`` diagnoses it without modifying either location.
    """

    home = home.expanduser().absolute()
    legacy_manifest = home / "manifest.json"
    if legacy_manifest.exists() or legacy_manifest.is_symlink():
        raise ContractViolation(
            "legacy Agent OS state exists directly under the home; "
            "move or restore it into <home>/state before running tasks"
        )
    return home / "state"


def _allowed_path(relative: PurePosixPath) -> bool:
    parts = relative.parts
    if parts in (
        ("learning", "observations.jsonl"),
        ("learning", "quality-feedback.jsonl"),
        ("learning", "active.json"),
        ("learning", "history.json"),
        ("optimization", "active.json"),
        ("optimization", "history.json"),
        ("optimization", "canary.jsonl"),
        ("reuse", "events.jsonl"),
        ("approvals", "decisions.json"),
    ):
        return True
    return (
        len(parts) == 3
        and parts[0] in ("learning", "optimization", "reuse")
        and parts[1]
        in {
            "learning": ("candidates",),
            "optimization": ("candidates", "suites"),
            "reuse": ("entries",),
        }[parts[0]]
        and relative.suffix == ".json"
    )


class AgentOS:
    """Owns portable Agent OS state behind one versioned root interface."""

    def __init__(
        self,
        root: Path,
        clock=time.time,
        migrations: Optional[BundleMigrationRegistry] = None,
    ):
        if root.is_symlink():
            raise ContractViolation("Agent OS root cannot be a symlink")
        self.root = root
        self._clock = clock
        self._migrations = migrations or default_bundle_migrations()
        self.root.mkdir(parents=True, exist_ok=True)
        manifest_path = self.root / "manifest.json"
        if manifest_path.exists():
            manifest = read_json_object(manifest_path, label="Agent OS state")
            if (
                manifest.get("kind") != _ROOT_KIND
                or manifest.get("schema_version") != AGENT_OS_SCHEMA_VERSION
            ):
                raise ContractViolation("unsupported Agent OS root manifest")
        else:
            atomic_json_write(
                manifest_path,
                {
                    "kind": _ROOT_KIND,
                    "schema_version": AGENT_OS_SCHEMA_VERSION,
                    "created_at": self._clock(),
                },
            )
        for name in AGENT_OS_DIRECTORIES:
            (self.root / name).mkdir(exist_ok=True)

    @property
    def learning_root(self) -> Path:
        return self.root / "learning"

    @property
    def optimization_root(self) -> Path:
        return self.root / "optimization"

    @property
    def reuse_root(self) -> Path:
        return self.root / "reuse"

    @property
    def approvals_root(self) -> Path:
        return self.root / "approvals"

    @property
    def routing_root(self) -> Path:
        return self.root / "routing"

    def governance_store(self) -> ProviderGovernanceStore:
        return ProviderGovernanceStore(self.routing_root, clock=self._clock)

    def router(
        self, provider_policies: Optional[Mapping[str, ProviderPolicy]] = None
    ) -> PolicyRouter:
        return RSILoop(self.learning_root).router(
            provider_policies, governance=self.governance_store()
        )

    def reuse_store(self) -> VerifiedArtifactCache:
        return VerifiedArtifactCache(self.reuse_root)

    def approval_inbox(self, control_root: Path) -> ApprovalInbox:
        return ApprovalInbox(control_root, self.approvals_root)

    def apply(self, graph: GraphSpec, rollout_key: Optional[str] = None) -> GraphSpec:
        return RSIOptimizationLab(self.optimization_root).apply(graph, rollout_key)

    def status(self) -> Mapping[str, Any]:
        loop = RSILoop(self.learning_root)
        active_policy = loop.active_policy()
        lab = RSIOptimizationLab(self.optimization_root)
        active_candidates = lab.active_candidates()
        decisions = self._approval_decisions()
        root_manifest = read_json_object(self.root / "manifest.json", label="Agent OS state")
        return {
            "schema_version": AGENT_OS_SCHEMA_VERSION,
            "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
            "root": str(self.root.resolve()),
            "last_import": root_manifest.get("last_import"),
            "learning": {
                "observations": len(loop.journal.read()),
                "quality_feedback": len(loop.feedback_journal.read()),
                "candidates": len(loop.candidates()),
                "active_policy": (
                    active_policy.version if active_policy is not None else None
                ),
            },
            "optimization": {
                "candidates": len(lab.candidates()),
                "active": {
                    kind: candidate.candidate_id
                    for kind, candidate in sorted(active_candidates.items())
                },
            },
            "reuse": self.reuse_store().status(),
            "routing": self.governance_store().status(),
            "approvals": {
                "runs": len(decisions),
                "decisions": sum(
                    len(items) for items in decisions.values() if isinstance(items, dict)
                ),
            },
        }

    def export_bundle(self, output: Path) -> Path:
        if output.exists():
            raise ContractViolation(f"Agent OS export target already exists: {output}")
        self._validate_state(self.root)
        files = self._state_files()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{output.name}.", dir=str(output.parent))
        )
        try:
            records = []
            for relative, source in files:
                data = source.read_bytes()
                target = temporary / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                records.append(
                    {
                        "path": relative.as_posix(),
                        "sha256": sha256_hex(data),
                        "size": len(data),
                    }
                )
            atomic_json_write(
                temporary / "manifest.json",
                {
                    "kind": _BUNDLE_KIND,
                    "schema_version": BUNDLE_SCHEMA_VERSION,
                    "state_schema_version": AGENT_OS_SCHEMA_VERSION,
                    "exported_at": self._clock(),
                    "files": records,
                    "migration_history": [],
                    "excludes": [
                        "credentials",
                        "worktrees",
                        "runtime-artifacts",
                        "raw-prompts",
                        "raw-inputs",
                        "unverified-results",
                        "runtime-leases",
                        "publication-receipts",
                        "routing-governance-runtime",
                        "controlled-merge-runtime",
                    ],
                },
            )
            os.replace(str(temporary), str(output))
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return output

    def import_bundle(self, bundle: Path) -> Mapping[str, Any]:
        if not bundle.is_dir() or bundle.is_symlink():
            raise ContractViolation("Agent OS import bundle must be a real directory")
        self._assert_import_target_empty()
        source_manifest = read_json_object(bundle / "manifest.json", label="Agent OS state")
        source_version = self._migrations.source_version(
            source_manifest, BUNDLE_SCHEMA_VERSION
        )
        declared = self._declared_files(source_manifest)
        actual = self._bundle_files(bundle)
        if set(declared) != set(actual):
            raise ContractViolation("Agent OS bundle files do not match its manifest")

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{self.root.name}.import-", dir=str(self.root.parent)
            )
        )
        try:
            for relative, metadata in declared.items():
                source = bundle / relative
                if source.is_symlink():
                    raise ContractViolation("Agent OS bundle cannot contain symlinks")
                data = source.read_bytes()
                if len(data) != metadata["size"] or sha256_hex(data) != metadata["sha256"]:
                    raise ContractViolation(
                        f"Agent OS bundle checksum mismatch: {relative.as_posix()}"
                    )
                self._validate_serialized(relative, data)
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            migrated_manifest, report = self._migrations.migrate(
                staging, source_manifest, BUNDLE_SCHEMA_VERSION
            )
            migrated_manifest = self._rebuild_bundle_manifest(
                staging, migrated_manifest
            )
            self._validate_migrated_manifest(migrated_manifest)
            self._validate_state(staging)
            imported_files = len(self._declared_files(migrated_manifest))
            audit = self._migration_audit(report, imported_files)
            current_root_manifest = read_json_object(self.root / "manifest.json", label="Agent OS state")
            final_root_manifest = dict(current_root_manifest)
            final_root_manifest["last_import"] = {
                **audit,
                "imported_at": self._clock(),
            }
            atomic_json_write(staging / "manifest.json", final_root_manifest)
            for name in AGENT_OS_DIRECTORIES:
                (staging / name).mkdir(exist_ok=True)
            self._assert_import_target_empty()
            self._commit_import(staging)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return {
            "schema_version": AGENT_OS_SCHEMA_VERSION,
            "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
            "source_bundle_schema_version": source_version,
            "imported_files": imported_files,
            "migration": audit,
        }

    def _assert_import_target_empty(self) -> None:
        if self._state_files():
            raise ContractViolation("Agent OS import target must have no existing state")
        if self.governance_store().has_runtime_state():
            raise ContractViolation(
                "Agent OS import target has active provider governance state"
            )
        flight_status = self.reuse_store().singleflight.status()
        active = sum(
            flight_status[name] for name in ("running", "completed", "failed")
        )
        if active or flight_status["invalid"]:
            raise ContractViolation(
                "Agent OS import target has active or invalid single-flight state"
            )

    @staticmethod
    def _migration_audit(
        report: MigrationReport, imported_files: int
    ) -> Mapping[str, Any]:
        return {
            **report.to_dict(),
            "imported_files": imported_files,
        }

    @classmethod
    def _rebuild_bundle_manifest(
        cls, staging: Path, manifest: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        records = []
        for relative, path in sorted(
            cls._bundle_files(staging).items(), key=lambda item: item[0].as_posix()
        ):
            data = path.read_bytes()
            cls._validate_serialized(relative, data)
            records.append(
                {
                    "path": relative.as_posix(),
                    "sha256": sha256_hex(data),
                    "size": len(data),
                }
            )
        rebuilt = dict(manifest)
        rebuilt["files"] = records
        return rebuilt

    @staticmethod
    def _validate_migrated_manifest(manifest: Mapping[str, Any]) -> None:
        if (
            manifest.get("kind") != _BUNDLE_KIND
            or manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION
        ):
            raise ContractViolation("bundle migration did not reach the current schema")
        if manifest.get("state_schema_version") != AGENT_OS_SCHEMA_VERSION:
            raise ContractViolation("unsupported Agent OS state schema in bundle")
        AgentOS._declared_files(manifest)

    def _commit_import(self, staging: Path) -> None:
        backup = Path(
            tempfile.mkdtemp(
                prefix=f".{self.root.name}.backup-", dir=str(self.root.parent)
            )
        )
        backup.rmdir()
        old_moved = False
        try:
            self._replace_directory(self.root, backup)
            old_moved = True
            self._replace_directory(staging, self.root)
        except OSError as error:
            if old_moved and backup.exists() and not self.root.exists():
                try:
                    self._replace_directory(backup, self.root)
                except OSError as restore_error:
                    raise ContractViolation(
                        "Agent OS import commit and target restoration both failed"
                    ) from restore_error
            raise ContractViolation("Agent OS import directory commit failed") from error
        else:
            shutil.rmtree(backup, ignore_errors=True)

    @staticmethod
    def _replace_directory(source: Path, target: Path) -> None:
        os.replace(str(source), str(target))

    def _state_files(self) -> Tuple[Tuple[PurePosixPath, Path], ...]:
        result = []
        for path in sorted(self.root.rglob("*")):
            if path.is_symlink():
                raise ContractViolation("Agent OS state cannot contain symlinks")
            if path.is_dir():
                continue
            relative = PurePosixPath(path.relative_to(self.root).as_posix())
            if relative == PurePosixPath("manifest.json"):
                continue
            if relative.parts[:2] == ("reuse", "flights"):
                continue
            if relative in (
                PurePosixPath("routing/state.json"),
                PurePosixPath("routing/state.lock"),
            ):
                continue
            if not _allowed_path(relative):
                raise ContractViolation(
                    f"Agent OS state contains unmanaged file: {relative.as_posix()}"
                )
            result.append((relative, path))
        return tuple(result)

    @staticmethod
    def _bundle_files(bundle: Path) -> Mapping[PurePosixPath, Path]:
        result = {}
        for path in sorted(bundle.rglob("*")):
            if path.is_symlink():
                raise ContractViolation("Agent OS bundle cannot contain symlinks")
            if path.is_dir():
                continue
            relative = PurePosixPath(path.relative_to(bundle).as_posix())
            if relative == PurePosixPath("manifest.json"):
                continue
            if not _allowed_path(relative):
                raise ContractViolation(
                    f"Agent OS bundle contains unmanaged file: {relative.as_posix()}"
                )
            result[relative] = path
        return result

    @staticmethod
    def _declared_files(
        manifest: Mapping[str, Any]
    ) -> Mapping[PurePosixPath, Mapping[str, Any]]:
        files = manifest.get("files")
        if not isinstance(files, list):
            raise ContractViolation("Agent OS bundle files must be an array")
        result = {}
        for item in files:
            if not isinstance(item, dict):
                raise ContractViolation("Agent OS bundle file record must be an object")
            raw_path = item.get("path")
            if not isinstance(raw_path, str):
                raise ContractViolation("Agent OS bundle path must be a string")
            relative = PurePosixPath(raw_path)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not _allowed_path(relative)
            ):
                raise ContractViolation(f"unsafe Agent OS bundle path: {raw_path}")
            if relative in result:
                raise ContractViolation(f"duplicate Agent OS bundle path: {raw_path}")
            digest = item.get("sha256")
            size = item.get("size")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise ContractViolation("invalid Agent OS bundle file metadata")
            result[relative] = {"sha256": digest, "size": size}
        return result

    @staticmethod
    def _validate_serialized(relative: PurePosixPath, data: bytes) -> None:
        try:
            text = data.decode("utf-8")
            if relative.suffix == ".json":
                json.loads(text)
            else:
                for line in text.splitlines():
                    if line.strip():
                        json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractViolation(
                f"invalid Agent OS serialized state {relative.as_posix()}: {error}"
            ) from error

    @staticmethod
    def _validate_state(root: Path) -> None:
        loop = RSILoop(root / "learning")
        loop.journal.read()
        loop.feedback_journal.read()
        loop.candidates()
        loop.active_policy()
        lab = RSIOptimizationLab(root / "optimization")
        lab.candidates()
        lab.active_candidates()
        for path in (root / "optimization" / "suites").glob("*.json"):
            RegressionSuite.from_dict(json.loads(path.read_text(encoding="utf-8")))
        for path in (root / "reuse" / "entries").glob("*.json"):
            VerifiedReuseRecord.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        VerifiedArtifactCache(root / "reuse").status()
        ProviderGovernanceStore(root / "routing").status()
        decisions_path = root / "approvals" / "decisions.json"
        if decisions_path.exists() and not isinstance(
            json.loads(decisions_path.read_text(encoding="utf-8")), dict
        ):
            raise ContractViolation("Agent OS approval decisions must be an object")

    def _approval_decisions(self) -> Mapping[str, Any]:
        path = self.approvals_root / "decisions.json"
        if not path.exists():
            return {}
        value = read_json_object(path, label="Agent OS state")
        if not isinstance(value, dict):
            raise ContractViolation("Agent OS approval decisions must be an object")
        return value
