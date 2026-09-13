"""Stable user task actions over the bounded engineering workflow."""

import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import telemetry
from ._store import (
    atomic_json_write,
    read_json_object,
)
from .adapters import discover_local_executors
from .agents import ModelUsage
from .engineering import EngineeringPlan, EngineeringWorkflow, ProjectPolicy
from .errors import ContractViolation
from .intents import IntentCompiler, TaskIntent
from .learning import RSILoop
from .os import AgentOS, state_root_for_home

USER_TASK_SCHEMA_VERSION = 1
_TASK_ID = re.compile(r"^task-[0-9a-f]{16}$")
_TERMINAL_PHASES = {"succeeded", "failed", "cancelled"}


def default_agent_os_home() -> Path:
    configured = os.environ.get("AGENT_OS_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".agent-os"


class UserTaskModule:
    """Hides task identity, workflow construction and result projection."""

    def __init__(
        self,
        home: Optional[Path] = None,
        workflow_factory: Optional[
            Callable[[Path, Path, ProjectPolicy, AgentOS], EngineeringWorkflow]
        ] = None,
        intent_compiler: Optional[IntentCompiler] = None,
        resident_factory: Optional[Callable[[], Any]] = None,
        clock: Callable[[], float] = time.time,
        id_factory: Optional[Callable[[], str]] = None,
    ):
        self.home = (home or default_agent_os_home()).expanduser().absolute()
        self.tasks_root = self.home / "tasks"
        self.state_root = state_root_for_home(self.home)
        for path, field in (
            (self.home, "Agent OS home"),
            (self.tasks_root, "Agent OS tasks root"),
            (self.state_root, "Agent OS state root"),
        ):
            if path.is_symlink():
                raise ContractViolation(f"{field} cannot be a symlink")
            if path.exists() and not path.is_dir():
                raise ContractViolation(f"{field} must be a directory")
        self.tasks_root.mkdir(parents=True, exist_ok=True)
        # Point the telemetry journal at the same home the tasks live in, so a
        # non-default --home never writes its trace somewhere else.
        telemetry.configure(self.home)
        self.agent_os = AgentOS(self.state_root)
        self._workflow_factory = workflow_factory
        self._intent_compiler = intent_compiler or IntentCompiler()
        self._resident_factory = resident_factory
        self._clock = clock
        self._id_factory = id_factory or (
            lambda: f"task-{uuid.uuid4().hex[:16]}"
        )

    def do(
        self,
        objective: str,
        workspace: Path,
        policy_path: Optional[Path] = None,
        template: Optional[str] = None,
        constraints: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        if not isinstance(objective, str) or not objective.strip():
            raise ContractViolation("task objective cannot be empty")
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise ContractViolation(f"task workspace does not exist: {workspace}")
        resolved_policy = (
            policy_path.expanduser().resolve()
            if policy_path is not None
            else workspace / ".agent-os" / "engineering.json"
        )
        configured_policy = None
        if resolved_policy.exists() or resolved_policy.is_symlink():
            configured_policy = ProjectPolicy.load(workspace, resolved_policy)
        elif policy_path is not None:
            raise ContractViolation(
                f"engineering policy does not exist: {resolved_policy}"
            )
        intent = self._intent_compiler.compile(
            objective,
            workspace,
            (
                configured_policy.check_commands
                if configured_policy is not None
                else None
            ),
            template,
            constraints,
        )
        if intent.needs_clarification:
            questions = " ".join(intent.clarification_questions)
            raise ContractViolation(f"task needs clarification before planning: {questions}")
        policy = configured_policy or ProjectPolicy(
            check_commands=intent.verification_commands
        )
        task_id, task_dir = self._allocate_task_dir()
        try:
            atomic_json_write(
                task_dir / "task.json",
                {
                    "schema_version": USER_TASK_SCHEMA_VERSION,
                    "task_id": task_id,
                    "kind": "engineering",
                    "workspace": str(workspace),
                    "policy": str(resolved_policy),
                    "created_at": self._clock(),
                },
            )
            workflow = self._workflow(task_dir, policy)
            workflow.prepare(objective.strip(), intent.to_dict())
        except Exception as error:
            # The caller has not received the task ID yet, so an incomplete
            # preparation cannot be recovered through the public task actions.
            # Remove only the directory allocated by this invocation.
            try:
                shutil.rmtree(task_dir)
            except OSError as cleanup_error:
                raise ContractViolation(
                    f"task preparation failed and cleanup was incomplete: {cleanup_error}"
                ) from error
            raise
        return self.status(task_id)

    def status(self, task_id: str) -> Mapping[str, Any]:
        task_dir, metadata = self._task(task_id)
        state = self._state(task_dir)
        phase = str(state.get("phase", "preparing"))
        value = {
            "schema_version": USER_TASK_SCHEMA_VERSION,
            "task_id": task_id,
            "kind": metadata["kind"],
            "phase": phase,
            "summary": self._phase_summary(phase),
            "next_action": self._next_action(phase),
            "plan_digest": state.get("plan_digest"),
            "progress": {
                "agent_calls": int(state.get("agent_calls", 0)),
                "review_cycles": int(state.get("review_cycles", 0)),
            },
            "usage": self._usage(state),
            "verification": {
                "passed": bool(state.get("reality_anchor", {}).get("passed", False))
                if isinstance(state.get("reality_anchor"), dict)
                else False,
            },
            "approval_required": phase == "awaiting_approval",
            "failure": state.get("failure"),
        }
        plan_path = task_dir / "plan.json"
        if plan_path.is_file():
            engineering_plan = EngineeringPlan.load(plan_path)
            intent = TaskIntent.from_dict(engineering_plan.intent)
            value["intent"] = {
                "template": intent.template,
                "objective": intent.objective,
                "constraints": list(intent.constraints),
                "project_kinds": list(intent.project_kinds),
                "verification_commands": [
                    list(command) for command in intent.verification_commands
                ],
                "mutation_allowed": intent.mutation_allowed,
                "assumptions": list(intent.assumptions),
            }
            value["proposed_plan"] = engineering_plan.plan
        resident = self._resident_if_initialized()
        scheduling = resident.inspect(task_id) if resident is not None else None
        if scheduling is not None:
            resident.ensure_running()
            scheduling = resident.inspect(task_id) or scheduling
            value["scheduling"] = {
                key: scheduling.get(key)
                for key in (
                    "state",
                    "priority",
                    "sequence",
                    "attempts",
                    "requested_action",
                    "error",
                )
            }
            schedule_state = scheduling.get("state")
            if schedule_state == "queued":
                value.update(
                    {
                        "phase": "queued",
                        "summary": self._phase_summary("queued"),
                        "next_action": "status",
                        "approval_required": False,
                    }
                )
            elif schedule_state == "paused":
                value.update(
                    {
                        "phase": "paused",
                        "summary": self._phase_summary("paused"),
                        "next_action": "control",
                        "approval_required": False,
                    }
                )
            elif schedule_state == "pause_requested":
                value["summary"] = "Pause requested; waiting for the next safe checkpoint"
            elif schedule_state == "cancel_requested":
                value["summary"] = "Cancellation requested; waiting for the next safe checkpoint"
        return value

    def approve(
        self,
        task_id: str,
        actor: str,
        background: bool = False,
        priority: int = 0,
    ) -> Mapping[str, Any]:
        if not isinstance(actor, str) or not actor.strip():
            raise ContractViolation("task approval actor cannot be empty")
        task_dir, _ = self._task(task_id)
        state = self._state(task_dir)
        if state.get("phase") != "awaiting_approval":
            raise ContractViolation(
                f"task {task_id} is {state.get('phase', 'preparing')}, not awaiting approval"
            )
        plan = read_json_object(task_dir / "plan.json", label="task plan")
        digest = plan.get("digest")
        if not isinstance(digest, str) or not digest:
            raise ContractViolation("task plan has no approval digest")
        if background:
            approved_at = self._clock()
            approval_path = task_dir / "approval.json"
            approval = {
                "schema_version": USER_TASK_SCHEMA_VERSION,
                "task_id": task_id,
                "actor": actor.strip(),
                "plan_digest": digest,
                "approved_at": approved_at,
            }
            queued_state = dict(state)
            queued_state.update(
                {
                    "phase": "queued",
                    "approved_by": actor.strip(),
                    "approved_at": approved_at,
                    "updated_at": approved_at,
                }
            )
            resident = self._resident()
            # Start the idle coordinator before publishing approval. If startup
            # fails, no durable task or queue transition has happened yet.
            resident.start_background()
            try:
                atomic_json_write(approval_path, approval)
                atomic_json_write(task_dir / "status.json", queued_state)
                resident.submit(task_id, priority)
            except Exception:
                atomic_json_write(task_dir / "status.json", state)
                approval_path.unlink(missing_ok=True)
                raise
            return self.status(task_id)
        self._workflow(task_dir).execute(actor.strip(), digest)
        return self.status(task_id)

    def control(
        self,
        task_id: str,
        action: str,
        actor: str,
        priority: Optional[int] = None,
    ) -> Mapping[str, Any]:
        if action not in ("pause", "resume", "cancel", "reprioritize"):
            raise ContractViolation("unsupported task control action")
        if not isinstance(actor, str) or not actor.strip():
            raise ContractViolation("task control actor cannot be empty")
        task_dir, _ = self._task(task_id)
        state = dict(self._state(task_dir))
        phase = state.get("phase")
        if phase == "awaiting_approval" and action == "cancel":
            cancelled_at = self._clock()
            state.update(
                {
                    "phase": "cancelled",
                    "cancelled_by": actor.strip(),
                    "cancelled_at": cancelled_at,
                    "updated_at": cancelled_at,
                }
            )
            atomic_json_write(task_dir / "status.json", state)
            return self.status(task_id)
        resident = self._resident_if_initialized()
        scheduling = resident.inspect(task_id) if resident is not None else None
        if scheduling is None:
            raise ContractViolation(
                f"task {task_id} cannot be controlled from phase {phase or 'preparing'}"
            )
        updated = resident.request(task_id, action, priority)
        changed_at = self._clock()
        if updated["state"] == "cancelled":
            state.update(
                {
                    "phase": "cancelled",
                    "cancelled_by": actor.strip(),
                    "cancelled_at": changed_at,
                    "updated_at": changed_at,
                }
            )
            atomic_json_write(task_dir / "status.json", state)
        elif updated["state"] == "paused":
            state.update(
                {
                    "phase": "paused",
                    "paused_by": actor.strip(),
                    "paused_at": changed_at,
                    "updated_at": changed_at,
                }
            )
            atomic_json_write(task_dir / "status.json", state)
        elif action == "resume":
            state.update(
                {
                    "phase": "queued",
                    "resumed_by": actor.strip(),
                    "resumed_at": changed_at,
                    "updated_at": changed_at,
                }
            )
            atomic_json_write(task_dir / "status.json", state)
        resident.ensure_running()
        return self.status(task_id)

    def execute_queued(
        self,
        task_id: str,
        control_probe: Callable[[], Optional[str]],
    ) -> Mapping[str, Any]:
        task_dir, _ = self._task(task_id)
        state = self._state(task_dir)
        if state.get("phase") not in ("queued", "running", "paused"):
            raise ContractViolation(
                f"task {task_id} cannot run from phase {state.get('phase', 'preparing')}"
            )
        approval = read_json_object(task_dir / "approval.json", label="task approval")
        if set(approval) != {
            "schema_version",
            "task_id",
            "actor",
            "plan_digest",
            "approved_at",
        }:
            raise ContractViolation("task approval has an invalid contract")
        actor = approval.get("actor")
        digest = approval.get("plan_digest")
        if (
            approval.get("schema_version") != USER_TASK_SCHEMA_VERSION
            or approval.get("task_id") != task_id
            or not isinstance(actor, str)
            or not actor.strip()
            or not isinstance(digest, str)
            or not digest
        ):
            raise ContractViolation("task approval fields are invalid")
        return self._workflow(task_dir).execute(actor, digest, control_probe)

    def record_queue_failure(self, task_id: str, failure: str) -> None:
        task_dir, _ = self._task(task_id)
        state = dict(self._state(task_dir))
        if state.get("phase") in _TERMINAL_PHASES:
            return
        failed_at = self._clock()
        state.update(
            {
                "phase": "failed",
                "success": False,
                "failure": failure,
                "finished_at": failed_at,
                "updated_at": failed_at,
            }
        )
        atomic_json_write(task_dir / "status.json", state)

    def execution_phase(self, task_id: str) -> str:
        task_dir, _ = self._task(task_id)
        return str(self._state(task_dir).get("phase", "preparing"))

    def result(self, task_id: str) -> Mapping[str, Any]:
        task_dir, _ = self._task(task_id)
        state = self._state(task_dir)
        phase = str(state.get("phase", "preparing"))
        if phase not in _TERMINAL_PHASES:
            raise ContractViolation(f"task {task_id} has no final result while {phase}")
        checks = []
        for cycle in state.get("checks", ()):
            if not isinstance(cycle, dict):
                continue
            for item in cycle.get("results", ()):
                if not isinstance(item, dict):
                    continue
                checks.append(
                    {
                        "cycle": cycle.get("cycle"),
                        "command": item.get("command"),
                        "passed": bool(item.get("passed", False)),
                        "returncode": item.get("returncode"),
                        "elapsed_seconds": item.get("elapsed_seconds"),
                    }
                )
        reviews = state.get("reviews", ())
        final_review = reviews[-1] if isinstance(reviews, list) and reviews else None
        artifacts = [
            {
                "name": "approved_plan",
                "path": str((task_dir / "plan.json").resolve()),
                "digest": state.get("plan_digest"),
            }
        ]
        if (task_dir / "report.json").is_file():
            artifacts.append(
                {
                    "name": "execution_report",
                    "path": str((task_dir / "report.json").resolve()),
                }
            )
        return {
            "schema_version": USER_TASK_SCHEMA_VERSION,
            "task_id": task_id,
            "phase": phase,
            "success": bool(state.get("success", phase == "succeeded")),
            "outcome": state.get("implementation"),
            "artifacts": artifacts,
            "verification": {
                "passed": bool(state.get("reality_anchor", {}).get("passed", False))
                if isinstance(state.get("reality_anchor"), dict)
                else False,
                "checks": checks,
                "independent_review": (
                    {
                        "verdict": final_review.get("verdict"),
                        "score": final_review.get("score"),
                        "findings": len(final_review.get("findings", ())),
                    }
                    if isinstance(final_review, dict)
                    else None
                ),
            },
            "usage": self._usage(state),
            "human_intervention": {
                "approved_by": state.get("approved_by"),
                "cancelled_by": state.get("cancelled_by"),
                "review_cycles": int(state.get("review_cycles", 0)),
                "repair_cycles": len(state.get("repairs", ()))
                if isinstance(state.get("repairs", ()), list)
                else 0,
            },
            "failure": state.get("failure"),
        }

    def _allocate_task_dir(self):
        for _ in range(10):
            task_id = self._id_factory()
            self._validate_task_id(task_id)
            task_dir = self.tasks_root / task_id
            try:
                task_dir.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                continue
            return task_id, task_dir
        raise ContractViolation("could not allocate a unique task id")

    def _task(self, task_id: str):
        self._validate_task_id(task_id)
        task_dir = self.tasks_root / task_id
        if task_dir.is_symlink() or not task_dir.is_dir():
            raise ContractViolation(f"task {task_id} does not exist")
        metadata = read_json_object(task_dir / "task.json", label="task metadata")
        required = {
            "schema_version",
            "task_id",
            "kind",
            "workspace",
            "policy",
            "created_at",
        }
        if set(metadata) != required:
            raise ContractViolation("task metadata has an invalid contract")
        if (
            metadata["schema_version"] != USER_TASK_SCHEMA_VERSION
            or metadata["task_id"] != task_id
            or metadata["kind"] != "engineering"
        ):
            raise ContractViolation("task metadata identity does not match")
        if (
            not isinstance(metadata["workspace"], str)
            or not Path(metadata["workspace"]).is_absolute()
            or not isinstance(metadata["policy"], str)
            or not Path(metadata["policy"]).is_absolute()
            or isinstance(metadata["created_at"], bool)
            or not isinstance(metadata["created_at"], (int, float))
        ):
            raise ContractViolation("task metadata fields are invalid")
        return task_dir, metadata

    def _workflow(
        self,
        task_dir: Path,
        policy_override: Optional[ProjectPolicy] = None,
    ) -> EngineeringWorkflow:
        metadata = read_json_object(task_dir / "task.json", label="task metadata")
        workspace = Path(str(metadata["workspace"]))
        policy_path = Path(str(metadata["policy"]))
        if policy_override is not None:
            policy = policy_override
        elif policy_path.exists() or policy_path.is_symlink():
            policy = ProjectPolicy.load(workspace, policy_path)
        else:
            intent = TaskIntent.from_dict(
                EngineeringPlan.load(task_dir / "plan.json").intent
            )
            policy = ProjectPolicy(check_commands=intent.verification_commands)
        if self._workflow_factory is not None:
            return self._workflow_factory(
                workspace, task_dir, policy, self.agent_os
            )
        registry = discover_local_executors(
            self.agent_os.router(), self.agent_os.reuse_store()
        )
        if not registry.capabilities():
            raise ContractViolation("no supported local agent executor was discovered")
        return EngineeringWorkflow(
            workspace,
            task_dir,
            registry,
            policy,
            rsi_loop=RSILoop(self.agent_os.learning_root),
            agent_os_root=self.agent_os.root,
        )

    def _resident(self):
        if self._resident_factory is not None:
            return self._resident_factory()
        from .resident import ResidentCoordinator

        return ResidentCoordinator(self.home)

    def _resident_if_initialized(self):
        if self._resident_factory is not None:
            return self._resident_factory()
        if not (self.home / "runtime" / "resident" / "queue.json").is_file():
            return None
        return self._resident()

    @staticmethod
    def _state(task_dir: Path) -> Mapping[str, Any]:
        report = task_dir / "report.json"
        status = task_dir / "status.json"
        if report.is_file():
            return read_json_object(report, label="task report")
        if status.is_file():
            return read_json_object(status, label="task status")
        return {"phase": "preparing"}

    @staticmethod
    def _usage(state: Mapping[str, Any]) -> Mapping[str, Any]:
        started = state.get("started_at")
        finished = state.get("finished_at")
        duration = None
        if isinstance(started, (int, float)) and isinstance(finished, (int, float)):
            duration = max(0.0, float(finished) - float(started))
        tokens_used = int(state.get("tokens_used", 0))
        cost_usd = float(state.get("cost_usd", 0.0))
        explicit_cost_complete = state.get("cost_complete")
        model_usage = ModelUsage.from_persisted(
            state.get("usage"),
            tokens_used,
            cost_usd,
            explicit_cost_complete
            if isinstance(explicit_cost_complete, bool)
            else None,
        )
        return {
            **model_usage.to_dict(),
            "agent_calls": int(state.get("agent_calls", 0)),
            "tokens_used": tokens_used,
            "cost_usd": cost_usd,
            "cost_complete": model_usage.cost_complete,
            "duration_seconds": duration,
        }

    @staticmethod
    def _phase_summary(phase: str) -> str:
        return {
            "preparing": "Preparing an execution plan",
            "awaiting_approval": "Plan ready and waiting for approval",
            "queued": "Approved task is queued for background execution",
            "running": "Executing the approved plan",
            "paused": "Task is paused at a safe checkpoint",
            "succeeded": "Verified result is ready",
            "failed": "Task stopped without a verified result",
            "cancelled": "Task was cancelled",
        }.get(phase, f"Task is {phase}")

    @staticmethod
    def _next_action(phase: str) -> Optional[str]:
        if phase == "awaiting_approval":
            return "approve"
        if phase in _TERMINAL_PHASES:
            return "result"
        if phase in ("preparing", "running"):
            return "status"
        return None

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
            raise ContractViolation("invalid task id")
