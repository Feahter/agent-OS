import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .artifacts import ArtifactRecord
from .errors import ContractViolation
from .model import NodeSpec
from .orca import ChangeSetArtifact
from .publication import ArtifactVersion


CONTROLLED_MERGE_SCHEMA_VERSION = 1
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json_dict(value: Any) -> Dict[str, Any]:
    return json.loads(json.dumps(asdict(value), ensure_ascii=False, sort_keys=True))


def _artifact_dicts(records: Iterable[ArtifactRecord]) -> Tuple[Mapping[str, Any], ...]:
    return tuple(asdict(ArtifactVersion.from_record(record)) for record in records)


def _artifact_identity(value: Mapping[str, Any]) -> Tuple[str, str, int, str]:
    return (
        str(value["key"]),
        str(value["producer"]),
        int(value["version"]),
        str(value["checksum"]),
    )


def _extract(value: Any, path: Sequence[str]) -> Any:
    current = value
    for part in path:
        if not isinstance(current, dict) or part not in current:
            raise ContractViolation(
                f"merge verification result is missing path {'.'.join(path)}"
            )
        current = current[part]
    return current


@dataclass(frozen=True)
class MergeCandidate:
    schema_version: int
    candidate_id: str
    run_id: str
    source_node_id: str
    source_attempt: int
    verifier_node_id: str
    workspace_id: str
    source_workspace: str
    target_repository: str
    target_branch: str
    base_commit: str
    head_commit: str
    target_head: str
    files_modified: Tuple[str, ...]
    change_set_digest: str
    source_artifacts: Tuple[Mapping[str, Any], ...]

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MergeCandidate":
        candidate = cls(
            schema_version=int(value["schema_version"]),
            candidate_id=str(value["candidate_id"]),
            run_id=str(value["run_id"]),
            source_node_id=str(value["source_node_id"]),
            source_attempt=int(value["source_attempt"]),
            verifier_node_id=str(value["verifier_node_id"]),
            workspace_id=str(value["workspace_id"]),
            source_workspace=str(value["source_workspace"]),
            target_repository=str(value["target_repository"]),
            target_branch=str(value["target_branch"]),
            base_commit=str(value["base_commit"]),
            head_commit=str(value["head_commit"]),
            target_head=str(value["target_head"]),
            files_modified=tuple(str(item) for item in value["files_modified"]),
            change_set_digest=str(value["change_set_digest"]),
            source_artifacts=tuple(dict(item) for item in value["source_artifacts"]),
        )
        candidate.validate()
        return candidate

    def validate(self) -> None:
        if self.schema_version != CONTROLLED_MERGE_SCHEMA_VERSION:
            raise ContractViolation("unsupported controlled merge candidate schema")
        if self.source_attempt < 1:
            raise ContractViolation("controlled merge source attempt must be positive")
        scalar = (
            self.candidate_id,
            self.run_id,
            self.source_node_id,
            self.verifier_node_id,
            self.workspace_id,
            self.source_workspace,
            self.target_repository,
            self.target_branch,
            self.base_commit,
            self.head_commit,
            self.target_head,
            self.change_set_digest,
        )
        if any(not item for item in scalar):
            raise ContractViolation("controlled merge candidate is incomplete")
        _validate_files(self.files_modified)
        for artifact in self.source_artifacts:
            _artifact_identity(artifact)
        identity = self.to_dict()
        identity.pop("candidate_id")
        if _digest(identity) != self.candidate_id:
            raise ContractViolation("controlled merge candidate checksum mismatch")


@dataclass(frozen=True)
class MergeAuthorization:
    gate: str
    gate_resolution: str
    verifier_node_id: str
    verifier_attempt: int
    verification_id: str
    quality_score: float
    source_artifacts: Tuple[Mapping[str, Any], ...]
    verifier_inputs: Tuple[Mapping[str, Any], ...]
    verifier_outputs: Tuple[Mapping[str, Any], ...]

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)


@dataclass(frozen=True)
class MergeReceipt:
    schema_version: int
    status: str
    candidate_id: str
    target_branch: str
    target_before: str
    target_after: str
    source_head: str
    merge_commit: Optional[str]
    files_modified: Tuple[str, ...]
    authorization: Mapping[str, Any]
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return _json_dict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MergeReceipt":
        receipt = cls(
            schema_version=int(value["schema_version"]),
            status=str(value["status"]),
            candidate_id=str(value["candidate_id"]),
            target_branch=str(value["target_branch"]),
            target_before=str(value["target_before"]),
            target_after=str(value["target_after"]),
            source_head=str(value["source_head"]),
            merge_commit=(
                None if value.get("merge_commit") is None else str(value["merge_commit"])
            ),
            files_modified=tuple(str(item) for item in value["files_modified"]),
            authorization=dict(value.get("authorization", {})),
            reason=None if value.get("reason") is None else str(value["reason"]),
        )
        receipt.validate()
        return receipt

    def validate(self) -> None:
        if self.schema_version != CONTROLLED_MERGE_SCHEMA_VERSION:
            raise ContractViolation("unsupported controlled merge receipt schema")
        if self.status not in ("merged", "rejected"):
            raise ContractViolation("invalid controlled merge receipt status")
        if self.status == "merged" and not self.merge_commit:
            raise ContractViolation("merged receipt has no merge commit")
        if self.status == "rejected" and not self.reason:
            raise ContractViolation("rejected receipt has no reason")
        _validate_files(self.files_modified)


class ControlledGitMerger:
    """Validates and merges one immutable change-set behind a small interface."""

    def __init__(self, target_repository: Path, timeout_seconds: int = 30):
        if isinstance(timeout_seconds, bool) or timeout_seconds < 1:
            raise ContractViolation("Git merge timeout must be positive")
        self.target_repository = target_repository.resolve()
        self.timeout_seconds = timeout_seconds
        self._assert_repository(self.target_repository)

    def prepare(
        self,
        change_set: ChangeSetArtifact,
        source_workspace: Path,
        target_branch: str,
        run_id: str,
        source_node_id: str,
        source_attempt: int,
        verifier_node_id: str,
        source_outputs: Sequence[ArtifactRecord],
    ) -> MergeCandidate:
        if change_set.conflicts:
            raise ContractViolation("controlled merge change-set declares conflicts")
        if source_attempt < 1:
            raise ContractViolation("controlled merge source attempt must be positive")
        self._validate_revision(change_set.base_ref, "base_ref")
        self._validate_revision(change_set.head_ref, "head_ref")
        files = _validate_files(change_set.files_modified)
        source = source_workspace.resolve()
        self._assert_repository(source)
        self._validate_branch_name(target_branch)

        with self._repository_lock():
            self._require_same_repository(source)
            self._require_clean(source, "source")
            self._require_clean(self.target_repository, "target")
            self._require_target_branch(target_branch)
            base_commit = self._revision(source, change_set.base_ref)
            head_commit = self._revision(source, change_set.head_ref)
            source_head = self._revision(source, "HEAD")
            target_head = self._revision(self.target_repository, "HEAD")
            if source_head != head_commit:
                raise ContractViolation("change-set head_ref does not match source HEAD")
            if base_commit == head_commit:
                raise ContractViolation("change-set contains no commit")
            if not self._is_ancestor(source, base_commit, head_commit):
                raise ContractViolation("change-set head is not based on base_ref")
            if not self._is_ancestor(self.target_repository, base_commit, target_head):
                raise ContractViolation("target branch does not contain change-set base_ref")
            if self._is_ancestor(self.target_repository, head_commit, target_head):
                raise ContractViolation("change-set head is already present on target branch")
            actual_files = self._changed_files(source, base_commit, head_commit)
            if actual_files != files:
                raise ContractViolation(
                    "change-set files_modified does not match Git diff"
                )

        change_set_value = {
            "workspace_id": change_set.workspace_id,
            "base_ref": change_set.base_ref,
            "head_ref": change_set.head_ref,
            "files_modified": list(files),
            "patch_path": change_set.patch_path,
            "conflicts": list(change_set.conflicts),
        }
        identity = {
            "schema_version": CONTROLLED_MERGE_SCHEMA_VERSION,
            "run_id": run_id,
            "source_node_id": source_node_id,
            "source_attempt": source_attempt,
            "verifier_node_id": verifier_node_id,
            "workspace_id": change_set.workspace_id,
            "source_workspace": str(source),
            "target_repository": str(self.target_repository),
            "target_branch": target_branch,
            "base_commit": base_commit,
            "head_commit": head_commit,
            "target_head": target_head,
            "files_modified": files,
            "change_set_digest": _digest(change_set_value),
            "source_artifacts": _artifact_dicts(source_outputs),
        }
        candidate = MergeCandidate(
            candidate_id=_digest(identity),
            **identity,
        )
        candidate.validate()
        return candidate

    def merge(
        self,
        candidate: MergeCandidate,
        verifier: NodeSpec,
        verifier_attempt: int,
        gate_resolution: Optional[str],
        verifier_inputs: Sequence[ArtifactRecord],
        verifier_outputs: Sequence[ArtifactRecord],
    ) -> MergeReceipt:
        candidate.validate()
        authorization, rejection = self._authorization(
            candidate,
            verifier,
            verifier_attempt,
            gate_resolution,
            verifier_inputs,
            verifier_outputs,
        )
        if rejection is not None:
            return self._rejected(candidate, rejection, authorization)
        with self._repository_lock():
            preflight = self._preflight(candidate, authorization)
            if preflight is not None:
                return preflight
            message = self._merge_message(candidate, authorization)
            result = self._git(
                self.target_repository,
                "-c",
                "commit.gpgSign=false",
                "-c",
                "merge.autoStash=false",
                "merge",
                "--no-ff",
                "--no-edit",
                "--no-gpg-sign",
                "-m",
                message,
                candidate.head_commit,
                check=False,
            )
            if result.returncode != 0:
                if self._merge_in_progress():
                    aborted = self._git(
                        self.target_repository, "merge", "--abort", check=False
                    )
                    if aborted.returncode != 0:
                        raise ContractViolation(
                            "Git merge failed and the target could not be restored"
                        )
                if (
                    self._revision(self.target_repository, "HEAD")
                    != candidate.target_head
                    or not self._is_clean(self.target_repository)
                ):
                    raise ContractViolation(
                        "Git merge failure left an indeterminate target state"
                    )
                return self._rejected(
                    candidate, "git_merge_conflict_or_hook_rejection", authorization
                )
            return self._merged_receipt(candidate, authorization)

    def reconcile(
        self,
        candidate: MergeCandidate,
        verifier: NodeSpec,
        verifier_attempt: int,
        gate_resolution: Optional[str],
        verifier_inputs: Sequence[ArtifactRecord],
        verifier_outputs: Sequence[ArtifactRecord],
    ) -> Optional[MergeReceipt]:
        candidate.validate()
        authorization, rejection = self._authorization(
            candidate,
            verifier,
            verifier_attempt,
            gate_resolution,
            verifier_inputs,
            verifier_outputs,
        )
        if rejection is not None:
            return self._rejected(candidate, rejection, authorization)
        with self._repository_lock():
            current = self._revision(self.target_repository, "HEAD")
            if (
                current == candidate.target_head
                and not self._merge_in_progress()
                and self._is_clean(self.target_repository)
            ):
                return None
            if self._is_expected_merge(current, candidate, authorization):
                return self._merged_receipt(candidate, authorization)
            return self._rejected(
                candidate, "indeterminate_or_external_target_change", authorization
            )

    def _authorization(
        self,
        candidate: MergeCandidate,
        verifier: NodeSpec,
        verifier_attempt: int,
        gate_resolution: Optional[str],
        verifier_inputs: Sequence[ArtifactRecord],
        verifier_outputs: Sequence[ArtifactRecord],
    ) -> Tuple[Optional[MergeAuthorization], Optional[str]]:
        if verifier.id != candidate.verifier_node_id or verifier.verifier_for != candidate.source_node_id:
            return None, "verification_node_mismatch"
        config = verifier.verified_reuse
        if config is None or not verifier.reality_anchor:
            return None, "verification_contract_missing"
        if verifier.gate is None or gate_resolution != "approved":
            return None, "approval_gate_not_approved"
        if verifier_attempt < 1:
            return None, "verification_attempt_invalid"

        expected = {_artifact_identity(item) for item in candidate.source_artifacts}
        observed = {
            _artifact_identity(item)
            for item in _artifact_dicts(verifier_inputs)
        }
        if not expected or not expected.issubset(observed):
            return None, "verification_version_mismatch"
        decision = next(
            (
                record
                for record in verifier_outputs
                if record.key == config.decision_artifact
                and record.producer == verifier.id
            ),
            None,
        )
        if decision is None:
            return None, "verification_decision_missing"
        try:
            passed = _extract(decision.value, config.passed_path)
            quality = _extract(decision.value, config.quality_path)
        except ContractViolation:
            return None, "verification_decision_invalid"
        if passed is not True:
            return None, "verification_not_passed"
        if (
            isinstance(quality, bool)
            or not isinstance(quality, (int, float))
            or not math.isfinite(quality)
            or not 0 <= quality <= 1
        ):
            return None, "verification_quality_invalid"
        if float(quality) < config.minimum_quality_score:
            return None, "verification_quality_below_threshold"
        authorization = MergeAuthorization(
            gate=verifier.gate,
            gate_resolution=gate_resolution,
            verifier_node_id=verifier.id,
            verifier_attempt=verifier_attempt,
            verification_id=f"{candidate.run_id}:{verifier.id}:{verifier_attempt}",
            quality_score=float(quality),
            source_artifacts=candidate.source_artifacts,
            verifier_inputs=_artifact_dicts(verifier_inputs),
            verifier_outputs=_artifact_dicts(verifier_outputs),
        )
        return authorization, None

    def _preflight(
        self, candidate: MergeCandidate, authorization: MergeAuthorization
    ) -> Optional[MergeReceipt]:
        if Path(candidate.target_repository).resolve() != self.target_repository:
            return self._rejected(candidate, "target_repository_mismatch", authorization)
        self._require_target_branch(candidate.target_branch)
        if self._merge_in_progress() or not self._is_clean(self.target_repository):
            return self._rejected(candidate, "target_worktree_not_clean", authorization)
        current = self._revision(self.target_repository, "HEAD")
        if current != candidate.target_head:
            if self._is_expected_merge(current, candidate, authorization):
                return self._merged_receipt(candidate, authorization)
            return self._rejected(candidate, "target_head_drift", authorization)
        try:
            resolved_head = self._revision(
                self.target_repository, candidate.head_commit
            )
        except ContractViolation:
            return self._rejected(candidate, "source_commit_unavailable", authorization)
        if resolved_head != candidate.head_commit:
            return self._rejected(candidate, "source_commit_mismatch", authorization)
        actual_files = self._changed_files(
            self.target_repository, candidate.base_commit, candidate.head_commit
        )
        if actual_files != candidate.files_modified:
            return self._rejected(candidate, "change_set_digest_mismatch", authorization)
        return None

    def _merged_receipt(
        self, candidate: MergeCandidate, authorization: MergeAuthorization
    ) -> MergeReceipt:
        current = self._revision(self.target_repository, "HEAD")
        if not self._is_expected_merge(current, candidate, authorization):
            raise ContractViolation("Git merge result does not match controlled receipt")
        receipt = MergeReceipt(
            schema_version=CONTROLLED_MERGE_SCHEMA_VERSION,
            status="merged",
            candidate_id=candidate.candidate_id,
            target_branch=candidate.target_branch,
            target_before=candidate.target_head,
            target_after=current,
            source_head=candidate.head_commit,
            merge_commit=current,
            files_modified=candidate.files_modified,
            authorization=authorization.to_dict(),
        )
        receipt.validate()
        return receipt

    def _rejected(
        self,
        candidate: MergeCandidate,
        reason: str,
        authorization: Optional[MergeAuthorization],
    ) -> MergeReceipt:
        try:
            current = self._revision(self.target_repository, "HEAD")
        except ContractViolation:
            current = candidate.target_head
        receipt = MergeReceipt(
            schema_version=CONTROLLED_MERGE_SCHEMA_VERSION,
            status="rejected",
            candidate_id=candidate.candidate_id,
            target_branch=candidate.target_branch,
            target_before=candidate.target_head,
            target_after=current,
            source_head=candidate.head_commit,
            merge_commit=None,
            files_modified=candidate.files_modified,
            authorization=(authorization.to_dict() if authorization is not None else {}),
            reason=reason,
        )
        receipt.validate()
        return receipt

    def _is_expected_merge(
        self,
        revision: str,
        candidate: MergeCandidate,
        authorization: MergeAuthorization,
    ) -> bool:
        result = self._git(
            self.target_repository,
            "show",
            "-s",
            "--format=%P%x00%B",
            revision,
            check=False,
        )
        if result.returncode != 0:
            return False
        raw = self._decode(result.stdout)
        if "\x00" not in raw:
            return False
        parents, message = raw.split("\x00", 1)
        expected_parents = f"{candidate.target_head} {candidate.head_commit}"
        return (
            parents.strip() == expected_parents
            and message.strip() == self._merge_message(candidate, authorization)
            and not self._merge_in_progress()
            and self._is_clean(self.target_repository)
        )

    @staticmethod
    def _merge_message(
        candidate: MergeCandidate, authorization: MergeAuthorization
    ) -> str:
        return (
            f"Graph Engineering controlled merge {candidate.source_node_id}\n\n"
            f"Graph-Engineering-Candidate: {candidate.candidate_id}\n"
            f"Graph-Engineering-Verification: {authorization.verification_id}\n"
            f"Graph-Engineering-Gate: {authorization.gate}"
        )

    def _require_same_repository(self, source: Path) -> None:
        source_common = self._common_dir(source)
        target_common = self._common_dir(self.target_repository)
        if source_common != target_common:
            raise ContractViolation(
                "controlled merge source and target are not Git worktrees of one repository"
            )

    def _require_target_branch(self, target_branch: str) -> None:
        current = self._git_text(
            self.target_repository, "symbolic-ref", "--quiet", "--short", "HEAD"
        ).strip()
        if current != target_branch:
            raise ContractViolation(
                f"controlled merge target branch mismatch: expected {target_branch}"
            )

    def _validate_branch_name(self, target_branch: str) -> None:
        if not isinstance(target_branch, str) or not target_branch:
            raise ContractViolation("controlled merge target branch is empty")
        result = self._git(
            self.target_repository,
            "check-ref-format",
            "--branch",
            target_branch,
            check=False,
        )
        if result.returncode != 0:
            raise ContractViolation("controlled merge target branch is invalid")

    @staticmethod
    def _validate_revision(value: str, field: str) -> None:
        if not isinstance(value, str) or not _REVISION.fullmatch(value):
            raise ContractViolation(f"controlled merge {field} is unsafe")
        if ".." in value or value.endswith(".") or "//" in value:
            raise ContractViolation(f"controlled merge {field} is unsafe")

    def _revision(self, repository: Path, revision: str) -> str:
        self._validate_revision(revision, "revision")
        return self._git_text(
            repository,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        ).strip()

    def _changed_files(
        self, repository: Path, base_commit: str, head_commit: str
    ) -> Tuple[str, ...]:
        result = self._git(
            repository,
            "diff",
            "--name-only",
            "-z",
            "--diff-filter=ACDMRTUXB",
            base_commit,
            head_commit,
        )
        parts = result.stdout.split(b"\x00")
        names = tuple(
            self._decode(part)
            for part in parts
            if part
        )
        return _validate_files(names)

    def _is_ancestor(self, repository: Path, ancestor: str, descendant: str) -> bool:
        result = self._git(
            repository,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise ContractViolation("cannot inspect Git commit ancestry")
        return result.returncode == 0

    def _require_clean(self, repository: Path, label: str) -> None:
        if not self._is_clean(repository):
            raise ContractViolation(f"controlled merge {label} worktree is not clean")

    def _is_clean(self, repository: Path) -> bool:
        result = self._git(
            repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        return not result.stdout

    def _merge_in_progress(self) -> bool:
        raw = self._git_text(
            self.target_repository, "rev-parse", "--git-path", "MERGE_HEAD"
        ).strip()
        path = Path(raw)
        if not path.is_absolute():
            path = self.target_repository / path
        return path.exists()

    def _common_dir(self, repository: Path) -> Path:
        raw = self._git_text(repository, "rev-parse", "--git-common-dir").strip()
        path = Path(raw)
        if not path.is_absolute():
            path = repository / path
        return path.resolve()

    @contextmanager
    def _repository_lock(self):
        common = self._common_dir(self.target_repository)
        lock_path = common / "grapheng-merge.lock"
        lock_path.touch(exist_ok=True)
        with lock_path.open("r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _assert_repository(self, repository: Path) -> None:
        if not repository.is_dir():
            raise ContractViolation(f"Git repository does not exist: {repository}")
        result = self._git(
            repository, "rev-parse", "--is-inside-work-tree", check=False
        )
        if result.returncode != 0 or self._decode(result.stdout).strip() != "true":
            raise ContractViolation(f"path is not a Git worktree: {repository}")

    def _git_text(self, repository: Path, *arguments: str) -> str:
        return self._decode(self._git(repository, *arguments).stdout)

    def _git(
        self,
        repository: Path,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_EDITOR": "true",
                "LC_ALL": "C",
            }
        )
        try:
            result = subprocess.run(
                ("git", "-C", str(repository), *arguments),
                capture_output=True,
                timeout=self.timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ContractViolation(f"cannot execute controlled Git operation: {error}") from error
        if check and result.returncode != 0:
            raise ContractViolation(
                f"controlled Git operation failed: {arguments[0] if arguments else 'git'}"
            )
        return result

    @staticmethod
    def _decode(value: bytes) -> str:
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContractViolation("Git output is not valid UTF-8") from error


def _validate_files(values: Iterable[str]) -> Tuple[str, ...]:
    files = tuple(values)
    if not files:
        raise ContractViolation("controlled merge change-set has no modified files")
    if len(files) != len(set(files)):
        raise ContractViolation("controlled merge files_modified contains duplicates")
    normalized = []
    for value in files:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ContractViolation("controlled merge file path is invalid")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or value != path.as_posix()
            or any(part in ("", ".", "..") for part in path.parts)
        ):
            raise ContractViolation("controlled merge file path is unsafe")
        normalized.append(value)
    return tuple(sorted(normalized))
