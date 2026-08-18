"""Stable user task actions over the bounded engineering workflow."""

import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .adapters import discover_local_executors
from .engineering import EngineeringWorkflow, ProjectPolicy
from .errors import ContractViolation
from .learning import RSILoop
from .os import AgentOS


USER_TASK_SCHEMA_VERSION = 1
_TASK_ID = re.compile(r"^task-[0-9a-f]{16}$")
_TERMINAL_PHASES = {"succeeded", "failed", "cancelled"}


def default_agent_os_home() -> Path:
    configured = os.environ.get("AGENT_OS_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".agent-os"


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
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


def _read_json(path: Path, field: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ContractViolation(f"{field} does not exist: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ContractViolation(f"cannot read {field}: {error}") from error
    if not isinstance(value, dict):
        raise ContractViolation(f"{field} must be a JSON object")
    return value


class UserTaskModule:
    """Hides task identity, workflow construction and result projection."""

    def __init__(
        self,
        home: Optional[Path] = None,
        workflow_factory: Optional[
            Callable[[Path, Path, ProjectPolicy, AgentOS], EngineeringWorkflow]
        ] = None,
        clock: Callable[[], float] = time.time,
        id_factory: Optional[Callable[[], str]] = None,
    ):
        self.home = (home or default_agent_os_home()).expanduser().absolute()
        self.tasks_root = self.home / "tasks"
        self.state_root = self.home / "state"
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
        self.agent_os = AgentOS(self.state_root)
        self._workflow_factory = workflow_factory
        self._clock = clock
        self._id_factory = id_factory or (
            lambda: f"task-{uuid.uuid4().hex[:16]}"
        )

    def do(
        self,
        objective: str,
        workspace: Path,
        policy_path: Optional[Path] = None,
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
        task_id, task_dir = self._allocate_task_dir()
        _atomic_json_write(
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
        workflow = self._workflow(task_dir)
        workflow.prepare(objective.strip())
        return self.status(task_id)

    def status(self, task_id: str) -> Mapping[str, Any]:
        task_dir, metadata = self._task(task_id)
        state = self._state(task_dir)
        phase = str(state.get("phase", "preparing"))
        return {
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

    def approve(self, task_id: str, actor: str) -> Mapping[str, Any]:
        if not isinstance(actor, str) or not actor.strip():
            raise ContractViolation("task approval actor cannot be empty")
        task_dir, _ = self._task(task_id)
        state = self._state(task_dir)
        if state.get("phase") != "awaiting_approval":
            raise ContractViolation(
                f"task {task_id} is {state.get('phase', 'preparing')}, not awaiting approval"
            )
        plan = _read_json(task_dir / "plan.json", "task plan")
        digest = plan.get("digest")
        if not isinstance(digest, str) or not digest:
            raise ContractViolation("task plan has no approval digest")
        self._workflow(task_dir).execute(actor.strip(), digest)
        return self.status(task_id)

    def control(
        self, task_id: str, action: str, actor: str
    ) -> Mapping[str, Any]:
        if action != "cancel":
            raise ContractViolation("task control currently supports only cancel")
        if not isinstance(actor, str) or not actor.strip():
            raise ContractViolation("task control actor cannot be empty")
        task_dir, _ = self._task(task_id)
        state = dict(self._state(task_dir))
        if state.get("phase") != "awaiting_approval":
            raise ContractViolation(
                f"task {task_id} cannot be cancelled from phase {state.get('phase', 'preparing')}"
            )
        cancelled_at = self._clock()
        state.update(
            {
                "phase": "cancelled",
                "cancelled_by": actor.strip(),
                "cancelled_at": cancelled_at,
                "updated_at": cancelled_at,
            }
        )
        _atomic_json_write(task_dir / "status.json", state)
        return self.status(task_id)

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
        metadata = _read_json(task_dir / "task.json", "task metadata")
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

    def _workflow(self, task_dir: Path) -> EngineeringWorkflow:
        metadata = _read_json(task_dir / "task.json", "task metadata")
        workspace = Path(str(metadata["workspace"]))
        policy = ProjectPolicy.load(workspace, Path(str(metadata["policy"])))
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

    @staticmethod
    def _state(task_dir: Path) -> Mapping[str, Any]:
        report = task_dir / "report.json"
        status = task_dir / "status.json"
        if report.is_file():
            return _read_json(report, "task report")
        if status.is_file():
            return _read_json(status, "task status")
        return {"phase": "preparing"}

    @staticmethod
    def _usage(state: Mapping[str, Any]) -> Mapping[str, Any]:
        started = state.get("started_at")
        finished = state.get("finished_at")
        duration = None
        if isinstance(started, (int, float)) and isinstance(finished, (int, float)):
            duration = max(0.0, float(finished) - float(started))
        return {
            "agent_calls": int(state.get("agent_calls", 0)),
            "tokens_used": int(state.get("tokens_used", 0)),
            "cost_usd": float(state.get("cost_usd", 0.0)),
            "cost_complete": bool(state.get("cost_complete", False)),
            "duration_seconds": duration,
        }

    @staticmethod
    def _phase_summary(phase: str) -> str:
        return {
            "preparing": "Preparing an execution plan",
            "awaiting_approval": "Plan ready and waiting for approval",
            "running": "Executing the approved plan",
            "succeeded": "Verified result is ready",
            "failed": "Task stopped without a verified result",
            "cancelled": "Task was cancelled before execution",
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
