"""Bounded, approval-gated software engineering workflow for heterogeneous agents."""

import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .agents import AgentRequest, AgentResult, ExecutorRegistry
from .control import EffectJournal
from .errors import ContractViolation
from .intents import TaskIntent
from .learning import RSILoop


ENGINEERING_POLICY_SCHEMA_VERSION = 1
ENGINEERING_PLAN_SCHEMA_VERSION = 3
ENGINEERING_REPORT_SCHEMA_VERSION = 1
_ROLES = ("explore", "plan", "implement", "review", "repair")


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent), delete=False
    )
    try:
        with handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    finally:
        if os.path.exists(handle.name):
            os.unlink(handle.name)


def _safe_relative_path(value: str, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ContractViolation(f"{field} must contain non-empty relative paths")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts or candidate == Path("."):
        raise ContractViolation(f"{field} contains unsafe path: {value}")
    return candidate


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractViolation(f"{field} must be a positive integer")
    return value


def _validated_usage(value: Any, field: str) -> Dict[str, Any]:
    required = {
        "agent_calls",
        "tokens_used",
        "cost_usd",
        "cost_complete",
        "elapsed_seconds",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ContractViolation(f"{field} has an invalid contract")
    for key in ("agent_calls", "tokens_used"):
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ContractViolation(f"{field}.{key} must be a non-negative integer")
    cost = value["cost_usd"]
    if (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise ContractViolation(f"{field}.cost_usd must be finite and non-negative")
    if not isinstance(value["cost_complete"], bool):
        raise ContractViolation(f"{field}.cost_complete must be a boolean")
    elapsed = value["elapsed_seconds"]
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(elapsed)
        or elapsed < 0
    ):
        raise ContractViolation(
            f"{field}.elapsed_seconds must be finite and non-negative"
        )
    return {
        "agent_calls": value["agent_calls"],
        "tokens_used": value["tokens_used"],
        "cost_usd": float(cost),
        "cost_complete": value["cost_complete"],
        "elapsed_seconds": float(elapsed),
    }


@dataclass(frozen=True)
class ProjectPolicy:
    check_commands: Tuple[Tuple[str, ...], ...]
    protected_paths: Tuple[str, ...] = (
        ".git/HEAD",
        ".git/index",
        ".git/config",
        ".git/packed-refs",
        ".git/refs",
        ".agent-os/engineering.json",
        "AGENTS.md",
        "CLAUDE.md",
    )
    instruction_files: Tuple[str, ...] = ("AGENTS.md", "CLAUDE.md")
    role_executors: Mapping[str, str] = None
    require_clean_worktree: bool = True
    max_review_cycles: int = 2
    max_agent_calls: int = 8
    max_elapsed_seconds: int = 1800
    agent_timeout_seconds: int = 600
    data_classification: str = "public"

    def __post_init__(self) -> None:
        if not self.check_commands:
            raise ContractViolation("engineering check_commands cannot be empty")
        for command in self.check_commands:
            if (
                not isinstance(command, tuple)
                or not command
                or any(not isinstance(item, str) or not item for item in command)
            ):
                raise ContractViolation(
                    "engineering check_commands must be non-empty argument arrays"
                )
        for value in self.protected_paths:
            _safe_relative_path(value, "protected_paths")
        for value in self.instruction_files:
            _safe_relative_path(value, "instruction_files")
        if not isinstance(self.require_clean_worktree, bool):
            raise ContractViolation("require_clean_worktree must be a boolean")
        _positive_int(self.max_review_cycles, "max_review_cycles")
        _positive_int(self.max_agent_calls, "max_agent_calls")
        _positive_int(self.max_elapsed_seconds, "max_elapsed_seconds")
        _positive_int(self.agent_timeout_seconds, "agent_timeout_seconds")
        if self.data_classification not in (
            "public",
            "internal",
            "confidential",
            "restricted",
        ):
            raise ContractViolation("invalid engineering data_classification")
        executors = self.role_executors or {}
        unknown = set(executors) - set(_ROLES)
        if unknown or any(
            not isinstance(value, str) or not value.strip()
            for value in executors.values()
        ):
            raise ContractViolation("engineering role_executors is invalid")
        object.__setattr__(self, "role_executors", dict(executors))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProjectPolicy":
        if not isinstance(value, dict):
            raise ContractViolation("engineering policy must be a JSON object")
        allowed = {
            "schema_version",
            "check_commands",
            "protected_paths",
            "instruction_files",
            "role_executors",
            "require_clean_worktree",
            "max_review_cycles",
            "max_agent_calls",
            "max_elapsed_seconds",
            "agent_timeout_seconds",
            "data_classification",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ContractViolation(
                f"engineering policy has unknown fields: {sorted(unknown)}"
            )
        if value.get("schema_version") != ENGINEERING_POLICY_SCHEMA_VERSION:
            raise ContractViolation("unsupported engineering policy schema_version")
        commands = value.get("check_commands")
        if not isinstance(commands, list):
            raise ContractViolation("engineering check_commands must be an array")
        kwargs = {
            key: value[key]
            for key in allowed - {"schema_version", "check_commands"}
            if key in value
        }
        for key in ("protected_paths", "instruction_files"):
            if key in kwargs:
                if not isinstance(kwargs[key], list):
                    raise ContractViolation(f"engineering {key} must be an array")
                kwargs[key] = tuple(kwargs[key])
        return cls(
            tuple(tuple(command) if isinstance(command, list) else command for command in commands),
            **kwargs,
        )

    @classmethod
    def load(cls, workspace: Path, path: Optional[Path] = None) -> "ProjectPolicy":
        workspace = workspace.resolve()
        target = path or workspace / ".agent-os" / "engineering.json"
        try:
            value = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ContractViolation(f"engineering policy does not exist: {target}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(f"cannot read engineering policy {target}: {error}") from error
        return cls.from_dict(value)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": ENGINEERING_POLICY_SCHEMA_VERSION,
            "check_commands": [list(command) for command in self.check_commands],
            "protected_paths": list(self.protected_paths),
            "instruction_files": list(self.instruction_files),
            "role_executors": dict(self.role_executors),
            "require_clean_worktree": self.require_clean_worktree,
            "max_review_cycles": self.max_review_cycles,
            "max_agent_calls": self.max_agent_calls,
            "max_elapsed_seconds": self.max_elapsed_seconds,
            "agent_timeout_seconds": self.agent_timeout_seconds,
            "data_classification": self.data_classification,
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_dict())


@dataclass(frozen=True)
class EngineeringPlan:
    objective: str
    intent: Mapping[str, Any]
    exploration: Any
    plan: Any
    policy_digest: str
    instructions_digest: str
    workspace_digest: str
    preparation_usage: Mapping[str, Any]
    created_at: float
    digest: str

    @classmethod
    def create(
        cls,
        objective: str,
        intent: Mapping[str, Any],
        exploration: Any,
        plan: Any,
        policy_digest: str,
        instructions_digest: str,
        workspace_digest: str,
        preparation_usage: Mapping[str, Any],
        created_at: float,
    ) -> "EngineeringPlan":
        compiled_intent = TaskIntent.from_dict(dict(intent))
        if compiled_intent.objective != objective:
            raise ContractViolation("engineering plan objective does not match task intent")
        body = {
            "schema_version": ENGINEERING_PLAN_SCHEMA_VERSION,
            "objective": objective,
            "intent": compiled_intent.to_dict(),
            "exploration": exploration,
            "plan": plan,
            "policy_digest": policy_digest,
            "instructions_digest": instructions_digest,
            "workspace_digest": workspace_digest,
            "preparation_usage": _validated_usage(
                dict(preparation_usage), "engineering preparation_usage"
            ),
            "created_at": created_at,
        }
        return cls(digest=_canonical_digest(body), **{k: v for k, v in body.items() if k != "schema_version"})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": ENGINEERING_PLAN_SCHEMA_VERSION,
            "objective": self.objective,
            "intent": dict(self.intent),
            "exploration": self.exploration,
            "plan": self.plan,
            "policy_digest": self.policy_digest,
            "instructions_digest": self.instructions_digest,
            "workspace_digest": self.workspace_digest,
            "preparation_usage": dict(self.preparation_usage),
            "created_at": self.created_at,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EngineeringPlan":
        required = {
            "schema_version",
            "objective",
            "intent",
            "exploration",
            "plan",
            "policy_digest",
            "instructions_digest",
            "workspace_digest",
            "preparation_usage",
            "created_at",
            "digest",
        }
        legacy_required = required - {"intent"}
        if not isinstance(value, dict):
            raise ContractViolation("engineering plan has an invalid contract")
        schema_version = value.get("schema_version")
        if schema_version == 2:
            if set(value) != legacy_required:
                raise ContractViolation("engineering plan has an invalid contract")
            body = {key: value[key] for key in legacy_required if key != "digest"}
            if value["digest"] != _canonical_digest(body):
                raise ContractViolation("engineering plan digest mismatch")
            intent = TaskIntent.legacy(str(value["objective"])).to_dict()
        elif schema_version == ENGINEERING_PLAN_SCHEMA_VERSION:
            if set(value) != required:
                raise ContractViolation("engineering plan has an invalid contract")
            body = {key: value[key] for key in required if key != "digest"}
            if value["digest"] != _canonical_digest(body):
                raise ContractViolation("engineering plan digest mismatch")
            intent = TaskIntent.from_dict(value["intent"]).to_dict()
        else:
            raise ContractViolation("unsupported engineering plan schema_version")
        if not isinstance(value["objective"], str) or not value["objective"].strip():
            raise ContractViolation("engineering plan objective cannot be empty")
        if intent["objective"] != value["objective"]:
            raise ContractViolation("engineering plan objective does not match task intent")
        return cls(
            objective=value["objective"],
            intent=intent,
            exploration=value["exploration"],
            plan=value["plan"],
            policy_digest=str(value["policy_digest"]),
            instructions_digest=str(value["instructions_digest"]),
            workspace_digest=str(value["workspace_digest"]),
            preparation_usage=_validated_usage(
                value["preparation_usage"], "engineering preparation_usage"
            ),
            created_at=float(value["created_at"]),
            digest=str(value["digest"]),
        )

    @classmethod
    def load(cls, path: Path) -> "EngineeringPlan":
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError as error:
            raise ContractViolation(f"engineering plan does not exist: {path}") from error
        except json.JSONDecodeError as error:
            raise ContractViolation(f"engineering plan is not valid JSON: {error}") from error


@dataclass(frozen=True)
class CheckResult:
    command: Tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float

    @property
    def passed(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "command": list(self.command), "passed": self.passed}


class _EngineeringPaused(Exception):
    pass


class _EngineeringCancelled(Exception):
    pass


class EngineeringWorkflow:
    """Coordinates a finite engineering loop without changing GraphSpec semantics."""

    def __init__(
        self,
        workspace: Path,
        task_dir: Path,
        executors: ExecutorRegistry,
        policy: ProjectPolicy,
        rsi_loop: Optional[RSILoop] = None,
        agent_os_root: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        if not workspace.is_dir():
            raise ContractViolation(f"engineering workspace does not exist: {workspace}")
        self.workspace = workspace.resolve()
        self.task_dir = task_dir.resolve()
        try:
            self.task_dir.relative_to(self.workspace)
        except ValueError:
            pass
        else:
            raise ContractViolation("engineering task_dir must be outside the workspace")
        try:
            self.workspace.relative_to(self.task_dir)
        except ValueError:
            pass
        else:
            raise ContractViolation(
                "engineering task_dir must not contain the workspace"
            )
        if agent_os_root is not None:
            resolved_agent_os_root = agent_os_root.resolve()
            try:
                self.task_dir.relative_to(resolved_agent_os_root)
            except ValueError:
                pass
            else:
                raise ContractViolation(
                    "engineering task_dir must be outside the Agent OS root"
                )
            try:
                resolved_agent_os_root.relative_to(self.task_dir)
            except ValueError:
                pass
            else:
                raise ContractViolation(
                    "engineering task_dir must not contain the Agent OS root"
                )
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.executors = executors
        self.policy = policy
        self.rsi_loop = rsi_loop
        self._clock = clock
        self._monotonic = monotonic
        self._runner = command_runner
        self.effects = EffectJournal(self.task_dir / "effects")

    @property
    def plan_path(self) -> Path:
        return self.task_dir / "plan.json"

    @property
    def report_path(self) -> Path:
        return self.task_dir / "report.json"

    def prepare(
        self,
        objective: str,
        intent: Optional[Mapping[str, Any]] = None,
    ) -> EngineeringPlan:
        if not isinstance(objective, str) or not objective.strip():
            raise ContractViolation("engineering objective cannot be empty")
        compiled_intent = (
            TaskIntent.from_dict(dict(intent))
            if intent is not None
            else TaskIntent.basic(objective, self.policy.check_commands)
        )
        if compiled_intent.objective != objective.strip():
            raise ContractViolation("engineering objective does not match task intent")
        if compiled_intent.needs_clarification:
            raise ContractViolation("task intent still requires clarification")
        if (
            compiled_intent.project_kinds != ("legacy",)
            and compiled_intent.verification_commands != self.policy.check_commands
        ):
            raise ContractViolation(
                "task intent verification does not match engineering policy"
            )
        self._require_uninitialized_task()
        if self.policy.max_agent_calls < 2:
            raise ContractViolation(
                "engineering max_agent_calls must allow exploration and planning"
            )
        started = self._monotonic()
        self._require_clean_worktree()
        instructions = self._instructions()
        workspace_digest = self._workspace_fingerprint()
        exploration = self._agent_call(
            "explore",
            "Read the project and identify relevant architecture, constraints, risks, and files. Do not modify anything.",
            {
                "task_intent": compiled_intent.to_dict(),
                "project_instructions": instructions,
                "workspace_fingerprint": workspace_digest,
            },
            ("exploration",),
            tools=("read",),
            timeout_seconds=self._remaining_timeout(
                started, self.policy.agent_timeout_seconds
            ),
        )
        proposal = self._agent_call(
            "plan",
            "Produce a small-step implementation plan with explicit verification and rollback points. Do not modify anything.",
            {
                "task_intent": compiled_intent.to_dict(),
                "exploration": exploration.outputs["exploration"],
                "project_instructions": instructions,
                "workspace_fingerprint": workspace_digest,
            },
            ("plan",),
            tools=("read",),
            timeout_seconds=self._remaining_timeout(
                started, self.policy.agent_timeout_seconds
            ),
        )
        self._require_time(started)
        preparation_results = (exploration, proposal)
        known_costs = [
            item.cost_usd for item in preparation_results if item.cost_usd is not None
        ]
        preparation_usage = {
            "agent_calls": len(preparation_results),
            "tokens_used": sum(item.tokens_used for item in preparation_results),
            "cost_usd": sum(known_costs),
            "cost_complete": len(known_costs) == len(preparation_results),
            "elapsed_seconds": self._monotonic() - started,
        }
        plan = EngineeringPlan.create(
            objective.strip(),
            compiled_intent.to_dict(),
            exploration.outputs["exploration"],
            proposal.outputs["plan"],
            self.policy.digest,
            _canonical_digest(instructions),
            workspace_digest,
            preparation_usage,
            self._clock(),
        )
        _atomic_json_write(self.plan_path, plan.to_dict())
        _atomic_json_write(
            self.task_dir / "status.json",
            {
                "phase": "awaiting_approval",
                "plan_digest": plan.digest,
                "agent_calls": preparation_usage["agent_calls"],
                "tokens_used": preparation_usage["tokens_used"],
                "cost_usd": preparation_usage["cost_usd"],
                "cost_complete": preparation_usage["cost_complete"],
                "updated_at": self._clock(),
            },
        )
        return plan

    def execute(
        self,
        approved_by: str,
        plan_digest: Optional[str] = None,
        control_probe: Optional[Callable[[], Optional[str]]] = None,
    ) -> Mapping[str, Any]:
        if not isinstance(approved_by, str) or not approved_by.strip():
            raise ContractViolation("engineering execution requires approved_by")
        if not isinstance(plan_digest, str) or not plan_digest.strip():
            raise ContractViolation("engineering execution requires plan_digest")
        plan = EngineeringPlan.load(self.plan_path)
        if plan_digest != plan.digest:
            raise ContractViolation("engineering approval does not match plan digest")
        if plan.policy_digest != self.policy.digest:
            raise ContractViolation("engineering policy changed after planning")
        compiled_intent = TaskIntent.from_dict(plan.intent)
        if (
            compiled_intent.project_kinds != ("legacy",)
            and compiled_intent.verification_commands != self.policy.check_commands
        ):
            raise ContractViolation(
                "task intent verification does not match engineering policy"
            )
        instructions = self._instructions()
        if plan.instructions_digest != _canonical_digest(instructions):
            raise ContractViolation("project instructions changed after planning")
        implementation_receipt = self.effects.inspect("implement-0")
        if implementation_receipt is None:
            self._require_clean_worktree()
            if plan.workspace_digest != self._workspace_fingerprint():
                raise ContractViolation("engineering workspace changed after planning")
        started = self._monotonic()
        protected = self._protected_snapshot()
        execution_workspace_digest = self._workspace_fingerprint()
        mutation_allowed = compiled_intent.mutation_allowed
        preparation_usage = _validated_usage(
            plan.preparation_usage, "engineering preparation_usage"
        )
        state: Dict[str, Any] = {
            "schema_version": ENGINEERING_REPORT_SCHEMA_VERSION,
            "phase": "running",
            "objective": plan.objective,
            "plan_digest": plan.digest,
            "approved_by": approved_by,
            "started_at": self._clock(),
            "finished_at": None,
            "success": False,
            "agent_calls": preparation_usage["agent_calls"],
            "review_cycles": 0,
            "tokens_used": preparation_usage["tokens_used"],
            "cost_usd": preparation_usage["cost_usd"],
            "cost_complete": preparation_usage["cost_complete"],
            "preparation_usage": preparation_usage,
            "implementation": None,
            "checks": [],
            "reviews": [],
            "repairs": [],
            "reality_anchor": {"passed": False},
            "failure": None,
        }
        last_execution_task = ""
        self._write_running_state(state)
        try:
            self._control_checkpoint(control_probe)
            result = self._bounded_agent_call(
                state,
                started,
                "implement",
                (
                    "Carry out the approved read-only research plan and return evidence-backed findings. Do not modify the workspace."
                    if not mutation_allowed
                    else "Implement the approved plan in small, reviewable changes. Obey project instructions and do not touch protected paths."
                ),
                {
                    "task_intent": compiled_intent.to_dict(),
                    "approved_plan": plan.plan,
                    "exploration": plan.exploration,
                    "project_instructions": instructions,
                    "workspace_fingerprint": plan.workspace_digest,
                },
                ("implementation_summary",),
                tools=(
                    ("read", "shell", "edit", "write")
                    if mutation_allowed
                    else ("read", "shell")
                ),
                mutating=mutation_allowed,
                effect_index=0,
            )
            last_execution_task = result["task_id"]
            state["implementation"] = {
                "task_id": result["task_id"],
                "summary": result["outputs"]["implementation_summary"],
            }
            self._assert_protected_unchanged(protected)
            if (
                not mutation_allowed
                and execution_workspace_digest != self._workspace_fingerprint()
            ):
                raise ContractViolation("read-only research changed the workspace")
            self._control_checkpoint(control_probe)

            while True:
                self._require_time(started)
                checks = self._run_checks(started)
                state["checks"].append(
                    {
                        "cycle": state["review_cycles"],
                        "results": [item.to_dict() for item in checks],
                        "passed": all(item.passed for item in checks),
                    }
                )
                self._write_running_state(state)
                self._control_checkpoint(control_probe)
                if not all(item.passed for item in checks):
                    if not mutation_allowed:
                        state["phase"] = "failed"
                        state["failure"] = "read_only_verification_failed"
                        break
                    repair_input = {
                        "kind": "check_failure",
                        "checks": [item.to_dict() for item in checks if not item.passed],
                    }
                else:
                    review = self._bounded_agent_call(
                        state,
                        started,
                        "review",
                        "Independently review the approved plan, current workspace, and check evidence. Do not modify anything. Approve only when the implementation is correct and complete.",
                        {
                            "task_intent": compiled_intent.to_dict(),
                            "approved_plan": plan.plan,
                            "checks": [item.to_dict() for item in checks],
                            "project_instructions": instructions,
                            "workspace_fingerprint": self._workspace_fingerprint(),
                        },
                        ("verdict", "findings", "score"),
                        tools=("read",),
                    )
                    review_value = review["outputs"]
                    verdict = review_value.get("verdict")
                    findings = review_value.get("findings")
                    score = review_value.get("score")
                    if verdict not in ("approve", "changes_requested"):
                        raise ContractViolation("engineering reviewer returned an invalid verdict")
                    if not isinstance(findings, list):
                        raise ContractViolation("engineering reviewer findings must be an array")
                    if (
                        isinstance(score, bool)
                        or not isinstance(score, (int, float))
                        or not 0 <= score <= 1
                    ):
                        raise ContractViolation("engineering reviewer score must be between zero and one")
                    state["reviews"].append(
                        {
                            "cycle": state["review_cycles"],
                            "task_id": review["task_id"],
                            **review_value,
                        }
                    )
                    self._write_running_state(state)
                    if verdict == "approve" and not findings:
                        state["success"] = True
                        state["phase"] = "succeeded"
                        state["reality_anchor"] = {
                            "passed": True,
                            "checks_passed": True,
                            "review_approved": True,
                            "review_task_id": review["task_id"],
                        }
                        if self.rsi_loop is not None and last_execution_task:
                            self.rsi_loop.feedback(
                                last_execution_task,
                                float(score),
                                "engineering-independent-review",
                            )
                        break
                    self._control_checkpoint(control_probe)
                    if not mutation_allowed:
                        state["phase"] = "failed"
                        state["failure"] = "read_only_review_rejected"
                        break
                    repair_input = {
                        "kind": "review_findings",
                        "review_task_id": review["task_id"],
                        "findings": findings,
                    }

                if state["review_cycles"] >= self.policy.max_review_cycles:
                    state["phase"] = "failed"
                    state["failure"] = "review_cycle_limit_exhausted"
                    break
                state["review_cycles"] += 1
                repair = self._bounded_agent_call(
                    state,
                    started,
                    "repair",
                    "Repair only the reported check failures or review findings. Preserve correct work and do not touch protected paths.",
                    {
                        "task_intent": compiled_intent.to_dict(),
                        "approved_plan": plan.plan,
                        "repair_input": repair_input,
                        "project_instructions": instructions,
                        "workspace_fingerprint": self._workspace_fingerprint(),
                    },
                    ("repair_summary",),
                    tools=("read", "shell", "edit", "write"),
                    mutating=True,
                    effect_index=state["review_cycles"],
                )
                last_execution_task = repair["task_id"]
                state["repairs"].append(
                    {
                        "cycle": state["review_cycles"],
                        "task_id": repair["task_id"],
                        "reason": repair_input["kind"],
                        "summary": repair["outputs"]["repair_summary"],
                    }
                )
                self._assert_protected_unchanged(protected)
                self._write_running_state(state)
                self._control_checkpoint(control_probe)
        except _EngineeringPaused:
            state["phase"] = "paused"
            state["updated_at"] = self._clock()
            self._write_running_state(state)
            return state
        except _EngineeringCancelled:
            state["phase"] = "cancelled"
            state["finished_at"] = self._clock()
            state["failure"] = None
            _atomic_json_write(self.report_path, state)
            self._write_running_state(state)
            return state
        except Exception as error:
            state["phase"] = "failed"
            state["failure"] = f"{type(error).__name__}: {error}"
            state["finished_at"] = self._clock()
            _atomic_json_write(self.report_path, state)
            self._write_running_state(state)
            raise

        state["finished_at"] = self._clock()
        _atomic_json_write(self.report_path, state)
        self._write_running_state(state)
        return state

    @staticmethod
    def _control_checkpoint(
        control_probe: Optional[Callable[[], Optional[str]]]
    ) -> None:
        if control_probe is None:
            return
        action = control_probe()
        if action == "pause":
            raise _EngineeringPaused()
        if action == "cancel":
            raise _EngineeringCancelled()
        if action is not None:
            raise ContractViolation("engineering control probe returned an invalid action")

    def status(self) -> Mapping[str, Any]:
        for path in (self.report_path, self.task_dir / "status.json"):
            if path.exists():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as error:
                    raise ContractViolation(f"cannot read engineering status: {error}") from error
        return {"phase": "uninitialized"}

    def _require_uninitialized_task(self) -> None:
        managed = (
            self.plan_path,
            self.report_path,
            self.task_dir / "status.json",
        )
        if any(path.exists() or path.is_symlink() for path in managed) or any(
            self.effects.root.iterdir()
        ):
            raise ContractViolation(
                "engineering task_dir already belongs to a task; use a new task_dir"
            )

    def _agent_call(
        self,
        role: str,
        prompt: str,
        inputs: Mapping[str, Any],
        output_keys: Tuple[str, ...],
        tools: Tuple[str, ...],
        reuse_allowed: bool = True,
        timeout_seconds: Optional[int] = None,
    ) -> AgentResult:
        task_id = f"engineering-{role}-{_canonical_digest(inputs)[:16]}"
        request = AgentRequest(
            task_id=task_id,
            prompt=prompt,
            inputs=inputs,
            output_keys=output_keys,
            workspace=self.workspace,
            tools=tools,
            timeout_seconds=(
                self.policy.agent_timeout_seconds
                if timeout_seconds is None
                else timeout_seconds
            ),
            data_classification=self.policy.data_classification,
            task_type=f"engineering.{role}",
            reuse_scope="engineering",
            reuse_allowed=reuse_allowed,
        )
        return self.executors.execute(
            request, executor_id=self.policy.role_executors.get(role)
        )

    def _bounded_agent_call(
        self,
        state: Dict[str, Any],
        started: float,
        role: str,
        prompt: str,
        inputs: Mapping[str, Any],
        output_keys: Tuple[str, ...],
        tools: Tuple[str, ...],
        mutating: bool = False,
        effect_index: int = 0,
    ) -> Mapping[str, Any]:
        self._require_time(started)
        if state["agent_calls"] >= self.policy.max_agent_calls:
            raise ContractViolation("engineering max_agent_calls exhausted")
        task_id = f"engineering-{role}-{_canonical_digest(inputs)[:16]}"

        def invoke() -> Mapping[str, Any]:
            result = self._agent_call(
                role,
                prompt,
                inputs,
                output_keys,
                tools,
                reuse_allowed=not mutating,
                timeout_seconds=self._remaining_timeout(
                    started, self.policy.agent_timeout_seconds
                ),
            )
            return {
                "task_id": task_id,
                "executor_id": result.executor_id,
                "outputs": dict(result.outputs),
                "tokens_used": result.tokens_used,
                "cost_usd": result.cost_usd,
            }

        if mutating:
            value = self.effects.execute(
                f"{role}-{effect_index}",
                {"role": role, "task_id": task_id, "inputs": inputs},
                invoke,
            )
        else:
            value = invoke()
        self._require_time(started)
        state["agent_calls"] += 1
        state["tokens_used"] += int(value["tokens_used"])
        if value["cost_usd"] is None:
            state["cost_complete"] = False
        else:
            state["cost_usd"] += float(value["cost_usd"])
        self._write_running_state(state)
        return value

    def _instructions(self) -> Mapping[str, str]:
        values = {}
        for relative in self.policy.instruction_files:
            path = self.workspace / relative
            if path.is_file():
                values[relative] = path.read_text(encoding="utf-8")
        return values

    def _require_clean_worktree(self) -> None:
        if not self.policy.require_clean_worktree:
            return
        completed = self._runner(
            ["git", "status", "--porcelain"],
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise ContractViolation("engineering workspace is not a Git worktree")
        if completed.stdout.strip():
            raise ContractViolation("engineering workspace must be clean before planning or execution")

    def _protected_snapshot(self) -> Mapping[str, str]:
        git_protected = any(
            Path(relative).parts[0] == ".git"
            for relative in self.policy.protected_paths
        )
        git_dirs = self._git_storage_dirs() if git_protected else None
        values = {}
        for relative in self.policy.protected_paths:
            path = self.workspace / relative
            parts = Path(relative).parts
            if git_dirs is not None and len(parts) > 1 and parts[0] == ".git":
                git_dir, common_dir = git_dirs
                suffix = Path(*parts[1:])
                base = (
                    common_dir
                    if suffix.parts[0] in ("config", "packed-refs", "refs")
                    else git_dir
                )
                path = base / suffix
            values[relative] = self._path_digest(path)

        marker = self.workspace / ".git"
        if git_protected and (marker.is_file() or marker.is_symlink()):
            values.setdefault(".git", self._path_digest(marker))
        if git_dirs is not None:
            commondir = git_dirs[0] / "commondir"
            if commondir.is_file() or commondir.is_symlink():
                values.setdefault(".git/commondir", self._path_digest(commondir))
        return values

    def _git_storage_dirs(self) -> Optional[Tuple[Path, Path]]:
        marker = self.workspace / ".git"
        if marker.is_dir():
            git_dir = marker.resolve()
        elif marker.is_file():
            try:
                declaration = marker.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise ContractViolation(f"cannot read Git worktree pointer: {error}") from error
            prefix = "gitdir:"
            if not declaration.lower().startswith(prefix):
                raise ContractViolation("Git worktree pointer has an invalid contract")
            raw_git_dir = declaration[len(prefix):].strip()
            if not raw_git_dir:
                raise ContractViolation("Git worktree pointer has an empty target")
            candidate = Path(raw_git_dir)
            git_dir = (
                candidate if candidate.is_absolute() else marker.parent / candidate
            ).resolve()
        else:
            return None

        commondir_file = git_dir / "commondir"
        if not commondir_file.is_file():
            return git_dir, git_dir
        try:
            raw_common_dir = commondir_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise ContractViolation(f"cannot read Git common directory: {error}") from error
        if not raw_common_dir:
            raise ContractViolation("Git common directory pointer is empty")
        candidate = Path(raw_common_dir)
        common_dir = (
            candidate if candidate.is_absolute() else git_dir / candidate
        ).resolve()
        return git_dir, common_dir

    def _workspace_fingerprint(self) -> str:
        """Fingerprint visible project state while excluding Git and task receipts."""
        digest = hashlib.sha256()
        items = self._git_visible_files()
        if items is None:
            items = tuple(item for item in self.workspace.rglob("*") if item.is_file() or item.is_symlink())
        for item in sorted(items):
            relative = item.relative_to(self.workspace)
            if relative.parts and relative.parts[0] == ".git":
                continue
            digest.update(relative.as_posix().encode("utf-8") + b"\0")
            if item.is_symlink():
                digest.update(b"symlink\0" + os.readlink(item).encode("utf-8"))
            elif item.is_file():
                digest.update(item.read_bytes())
        return digest.hexdigest()

    def _git_visible_files(self) -> Optional[Tuple[Path, ...]]:
        if not (self.workspace / ".git").exists():
            return None
        completed = self._runner(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=str(self.workspace),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            return None
        return tuple(
            self.workspace / relative
            for relative in completed.stdout.split("\0")
            if relative
        )

    def _assert_protected_unchanged(self, before: Mapping[str, str]) -> None:
        after = self._protected_snapshot()
        changed = sorted(
            key
            for key in set(before) | set(after)
            if before.get(key) != after.get(key)
        )
        if changed:
            raise ContractViolation(f"engineering protected paths changed: {changed}")

    @staticmethod
    def _path_digest(path: Path) -> str:
        if path.is_symlink():
            return "symlink:" + os.readlink(path)
        if not path.exists():
            return "missing"
        digest = hashlib.sha256()
        if path.is_file():
            digest.update(b"file\0")
            digest.update(path.read_bytes())
            return digest.hexdigest()
        digest.update(b"dir\0")
        for item in sorted(path.rglob("*")):
            relative = item.relative_to(path).as_posix()
            digest.update(relative.encode("utf-8") + b"\0")
            if item.is_symlink():
                digest.update(b"symlink\0" + os.readlink(item).encode("utf-8"))
            elif item.is_file():
                digest.update(item.read_bytes())
        return digest.hexdigest()

    def _run_checks(self, started: float) -> Tuple[CheckResult, ...]:
        results = []
        for command in self.policy.check_commands:
            self._require_time(started)
            check_started = self._monotonic()
            completed = self._runner(
                list(command),
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=self._remaining_timeout(started),
                check=False,
            )
            results.append(
                CheckResult(
                    command,
                    completed.returncode,
                    (completed.stdout or "")[-4000:],
                    (completed.stderr or "")[-4000:],
                    self._monotonic() - check_started,
                )
            )
        return tuple(results)

    def _require_time(self, started: float) -> None:
        if self._monotonic() - started > self.policy.max_elapsed_seconds:
            raise ContractViolation("engineering max_elapsed_seconds exhausted")

    def _remaining_timeout(
        self, started: float, maximum: Optional[int] = None
    ) -> int:
        remaining = int(
            self.policy.max_elapsed_seconds - (self._monotonic() - started)
        )
        if remaining < 1:
            raise ContractViolation("engineering max_elapsed_seconds exhausted")
        return remaining if maximum is None else min(maximum, remaining)

    def _write_running_state(self, state: Mapping[str, Any]) -> None:
        _atomic_json_write(
            self.task_dir / "status.json",
            {
                "phase": state["phase"],
                "plan_digest": state["plan_digest"],
                "agent_calls": state["agent_calls"],
                "review_cycles": state["review_cycles"],
                "success": state["success"],
                "tokens_used": state["tokens_used"],
                "cost_usd": state["cost_usd"],
                "cost_complete": state["cost_complete"],
                "failure": state["failure"],
                "updated_at": self._clock(),
            },
        )


def default_project_policy() -> ProjectPolicy:
    return ProjectPolicy(
        check_commands=(("python3", "-m", "unittest", "discover", "-s", "tests"),)
    )
