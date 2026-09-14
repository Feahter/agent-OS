"""Durable job adapters used by the local resident coordinator."""

import contextlib
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple

from ._store import (
    atomic_json_write,
    read_json_object,
)
from .adapters import discover_local_executors
from .agent_nodes import AgentNodeHandler
from .control import LocalControlPlane
from .coordinator import OrcaCoordinator
from .errors import ContractViolation
from .model import GraphSpec
from .orca import OrcaBackend, OrcaClient, OrcaGraphCompiler
from .os import AgentOS, state_root_for_home
from .runtime import NodeRegistry

_TERMINAL_PHASES = {"succeeded", "failed", "cancelled"}


class ResidentJobCatalog:
    """Creates durable locators and resolves them to the owning job adapter."""

    def __init__(
        self,
        home: Path,
        graph_registry_factory: Optional[Callable[[GraphSpec, Path], NodeRegistry]] = None,
        orca_coordinator_factory: Optional[
            Callable[[GraphSpec, Path, Path], OrcaCoordinator]
        ] = None,
    ):
        self.graph = AdvancedGraphJob(
            home, registry_factory=graph_registry_factory
        )
        self.orca = OrcaResidentJob(
            home, coordinator_factory=orca_coordinator_factory
        )

    def handlers(self) -> Mapping[str, Any]:
        return {"graph": self.graph, "orca": self.orca}


class AdvancedGraphJob:
    """Restores agent Graph runs through LocalControlPlane and ApprovalInbox."""

    def __init__(
        self,
        home: Path,
        registry_factory: Optional[Callable[[GraphSpec, Path], NodeRegistry]] = None,
    ):
        self.home = home
        self.control_root = home / "runtime" / "graphs"
        self.definition_root = home / "runtime" / "resident" / "definitions" / "graph"
        self.agent_os = AgentOS(state_root_for_home(home))
        self.inbox = self.agent_os.approval_inbox(self.control_root)
        self._registry_factory = registry_factory or self._default_registry

    def prepare(self, graph: GraphSpec, workspace: Path) -> str:
        if any(node.kind != "agent" for node in graph.nodes):
            raise ContractViolation(
                "resident Graph runs currently require reconstructable agent nodes"
            )
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise ContractViolation(f"resident Graph workspace does not exist: {workspace}")
        plane = self._plane()
        try:
            run_id = plane.prepare(graph)
        finally:
            plane.close()
        atomic_json_write(
            self._definition_path(run_id),
            {"run_id": run_id, "workspace": str(workspace)},
        )
        return run_id

    def inspect(self, run_id: str) -> str:
        self._definition(run_id)
        plane = self._plane()
        try:
            snapshot = plane.inspect(run_id)
        finally:
            plane.close()
        if snapshot.phase in ("running", "cancelling"):
            if snapshot.lease_expires_at is not None and snapshot.lease_expires_at > time.time():
                return "waiting"
            return "queued"
        if snapshot.phase != "failed":
            return snapshot.phase
        blocked = self.inbox.list(run_id)
        if not blocked:
            return "failed"
        if any(item.status == "deny" for item in blocked):
            return "failed"
        return "queued" if all(item.status == "allow" for item in blocked) else "waiting"

    def describe(self, run_id: str) -> Mapping[str, Any]:
        phase = self.inspect(run_id)
        approvals = self.inbox.list(run_id)
        pending = tuple(item for item in approvals if item.status == "pending")
        if pending:
            summary = (
                f"Graph needs approval for {pending[0].gate}"
                if len(pending) == 1
                else f"Graph needs {len(pending)} approvals"
            )
        else:
            summary = {
                "queued": "Graph is ready to run",
                "running": "Graph is running",
                "waiting": "Graph needs input",
                "succeeded": "Graph completed successfully",
                "failed": "Graph stopped without a verified result",
                "cancelled": "Graph was cancelled",
            }.get(phase, f"Graph is {phase}")
        return {
            "phase": phase,
            "summary": summary,
            "next_action": "approval" if pending else None,
            "approval_required": bool(pending),
        }

    def execute(
        self, run_id: str, control_probe: Callable[[], Optional[str]]
    ) -> Mapping[str, Any]:
        definition = self._definition(run_id)
        workspace = Path(str(definition["workspace"]))
        graph = GraphSpec.from_json(self.control_root / "runs" / run_id / "graph.json")
        registry = self._registry_factory(graph, workspace)
        plane = self._plane()
        pause_requested = False
        try:
            snapshot = plane.inspect(run_id)
            policy = self.inbox.policy_for(run_id)
            if snapshot.phase == "queued":
                plane.start(run_id, registry, policy)
            else:
                plane.resume(run_id, registry, policy)
            while True:
                try:
                    snapshot = plane.wait(run_id, timeout=0.2)
                    break
                except TimeoutError:
                    action = control_probe()
                    if action == "cancel":
                        with contextlib.suppress(ContractViolation):
                            plane.cancel(run_id)
                    elif action == "pause":
                        pause_requested = True
        finally:
            plane.close()
        if pause_requested:
            return {"phase": "paused"}
        phase = self.inspect(run_id)
        return {"phase": phase, "run_phase": snapshot.phase}

    def record_failure(self, run_id: str, failure: str) -> None:
        self._definition(run_id)

    def _default_registry(self, graph: GraphSpec, workspace: Path) -> NodeRegistry:
        executors = discover_local_executors(
            self.agent_os.router(), self.agent_os.reuse_store()
        )
        if not executors.capabilities():
            raise ContractViolation("no supported local agent executor was discovered")
        registry = NodeRegistry()
        registry.register("agent", AgentNodeHandler(graph, executors, workspace))
        return registry

    def _plane(self) -> LocalControlPlane:
        return LocalControlPlane(
            self.control_root,
            max_workers=1,
            lease_seconds=2.0,
            reuse_store=self.agent_os.reuse_store(),
            token_reservations=self.agent_os.token_reservations(),
        )

    def projection_sources(self, run_id: str) -> Tuple[Path, ...]:
        """Files whose change invalidates a cached projection for ``run_id``."""

        run_root = self.control_root / "runs" / run_id
        return (
            self._definition_path(run_id),
            run_root / "state.json",
            run_root / "approvals.json",
        )

    def _definition_path(self, run_id: str) -> Path:
        return self.definition_root / f"{run_id}.json"

    def _definition(self, run_id: str) -> Mapping[str, Any]:
        value = read_json_object(self._definition_path(run_id), label="resident Graph definition")
        if set(value) != {"run_id", "workspace"} or value.get("run_id") != run_id:
            raise ContractViolation("resident Graph definition has an invalid contract")
        workspace = value.get("workspace")
        if not isinstance(workspace, str) or not Path(workspace).is_dir():
            raise ContractViolation("resident Graph definition has an invalid workspace")
        return value


class OrcaResidentJob:
    """Restores OrcaCoordinator while leaving dispatch state and effects in Orca."""

    def __init__(
        self,
        home: Path,
        coordinator_factory: Optional[
            Callable[[GraphSpec, Path, Path], OrcaCoordinator]
        ] = None,
    ):
        self.home = home
        self.root = home / "runtime" / "orca"
        self.agent_os = AgentOS(state_root_for_home(home))
        self._coordinator_factory = coordinator_factory or self._default_coordinator

    def prepare(self, graph: GraphSpec, workspace: Path) -> str:
        OrcaGraphCompiler().compile(graph)
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise ContractViolation(f"resident Orca workspace does not exist: {workspace}")
        reference = f"orca-{uuid.uuid4().hex[:16]}"
        job_root = self.root / reference
        atomic_json_write(
            job_root / "definition.json",
            {
                "reference": reference,
                "workspace": str(workspace),
                "graph": asdict(graph),
            },
        )
        return reference

    def inspect(self, reference: str) -> str:
        graph, job_root, workspace = self._definition(reference)
        if not (job_root / "state.json").is_file():
            return "queued"
        snapshot = self._coordinator_factory(graph, job_root, workspace).inspect()
        if snapshot.phase in ("waiting_for_input", "escalated"):
            return "waiting"
        return snapshot.phase

    def describe(self, reference: str) -> Mapping[str, Any]:
        graph, job_root, workspace = self._definition(reference)
        if not (job_root / "state.json").is_file():
            return {
                "phase": "queued",
                "summary": "Orca job is ready to run",
                "next_action": "status",
                "approval_required": False,
            }
        snapshot = self._coordinator_factory(graph, job_root, workspace).inspect()
        if snapshot.pending_questions:
            phase = "waiting"
            summary = (
                "Orca worker needs an answer"
                if len(snapshot.pending_questions) == 1
                else f"Orca workers need {len(snapshot.pending_questions)} answers"
            )
            next_action = "answer"
        elif snapshot.pending_escalations:
            phase = "waiting"
            summary = (
                "Orca worker escalation needs a decision"
                if len(snapshot.pending_escalations) == 1
                else f"{len(snapshot.pending_escalations)} Orca escalations need decisions"
            )
            next_action = "resolve-escalation"
        else:
            phase = snapshot.phase
            summary = {
                "queued": "Orca job is ready to run",
                "running": "Orca job is running",
                "succeeded": "Orca job completed successfully",
                "failed": "Orca job stopped without a verified result",
                "cancelled": "Orca job was cancelled",
            }.get(phase, f"Orca job is {phase}")
            next_action = None
        return {
            "phase": phase,
            "summary": summary,
            "next_action": next_action,
            "approval_required": False,
            "usage": {
                "tokens_used": snapshot.tokens_used,
                "cost_usd": snapshot.cost_usd,
            },
        }

    def execute(
        self, reference: str, control_probe: Callable[[], Optional[str]]
    ) -> Mapping[str, Any]:
        graph, job_root, workspace = self._definition(reference)
        coordinator = self._coordinator_factory(graph, job_root, workspace)
        snapshot = coordinator.start()
        while snapshot.phase not in _TERMINAL_PHASES:
            if snapshot.pending_questions or snapshot.pending_escalations:
                return {"phase": "waiting"}
            action = control_probe()
            if action == "cancel":
                snapshot = coordinator.cancel()
                break
            if action == "pause":
                return {"phase": "paused"}
            snapshot = coordinator.advance(timeout_ms=1000)
        return {"phase": snapshot.phase}

    def cancel(self, reference: str) -> Mapping[str, Any]:
        graph, job_root, workspace = self._definition(reference)
        if not (job_root / "state.json").is_file():
            return {"phase": "cancelled"}
        snapshot = self._coordinator_factory(graph, job_root, workspace).cancel()
        return {"phase": snapshot.phase}

    def record_failure(self, reference: str, failure: str) -> None:
        self._definition(reference)

    def _default_coordinator(
        self, graph: GraphSpec, job_root: Path, workspace: Path
    ) -> OrcaCoordinator:
        return OrcaCoordinator(
            graph,
            OrcaBackend(OrcaClient(cwd=workspace)),
            job_root,
            workspace,
            reuse_store=self.agent_os.reuse_store(),
        )

    def projection_sources(self, reference: str) -> Tuple[Path, ...]:
        """Files whose change invalidates a cached projection for ``reference``."""

        job_root = self.root / reference
        return (job_root / "definition.json", job_root / "state.json")

    def _definition(self, reference: str):
        job_root = self.root / reference
        value = read_json_object(job_root / "definition.json", label="resident Orca definition")
        if set(value) != {"reference", "workspace", "graph"} or value.get(
            "reference"
        ) != reference:
            raise ContractViolation("resident Orca definition has an invalid contract")
        workspace = value.get("workspace")
        graph = value.get("graph")
        if not isinstance(workspace, str) or not Path(workspace).is_dir():
            raise ContractViolation("resident Orca definition has an invalid workspace")
        return GraphSpec.from_dict(graph), job_root, Path(workspace)
