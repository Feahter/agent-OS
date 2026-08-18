"""Durable job adapters used by the local resident coordinator."""

import json
import os
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .adapters import discover_local_executors
from .agent_nodes import AgentNodeHandler
from .console import ApprovalInbox
from .control import LocalControlPlane
from .coordinator import OrcaCoordinator
from .errors import ContractViolation
from .model import GraphSpec
from .orca import OrcaBackend, OrcaClient, OrcaGraphCompiler
from .os import AgentOS
from .runtime import NodeRegistry


_TERMINAL_PHASES = {"succeeded", "failed", "cancelled"}


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
    except (OSError, json.JSONDecodeError) as error:
        raise ContractViolation(f"cannot read resident {field}: {error}") from error
    if not isinstance(value, dict):
        raise ContractViolation(f"resident {field} must be an object")
    return value


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
        self.agent_os = AgentOS(home / "state")
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
        _atomic_json_write(
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
                        try:
                            plane.cancel(run_id)
                        except ContractViolation:
                            pass
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
        )

    def _definition_path(self, run_id: str) -> Path:
        return self.definition_root / f"{run_id}.json"

    def _definition(self, run_id: str) -> Mapping[str, Any]:
        value = _read_json(self._definition_path(run_id), "Graph definition")
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
        self.agent_os = AgentOS(home / "state")
        self._coordinator_factory = coordinator_factory or self._default_coordinator

    def prepare(self, graph: GraphSpec, workspace: Path) -> str:
        OrcaGraphCompiler().compile(graph)
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise ContractViolation(f"resident Orca workspace does not exist: {workspace}")
        reference = f"orca-{uuid.uuid4().hex[:16]}"
        job_root = self.root / reference
        _atomic_json_write(
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

    def _definition(self, reference: str):
        job_root = self.root / reference
        value = _read_json(job_root / "definition.json", "Orca definition")
        if set(value) != {"reference", "workspace", "graph"} or value.get(
            "reference"
        ) != reference:
            raise ContractViolation("resident Orca definition has an invalid contract")
        workspace = value.get("workspace")
        graph = value.get("graph")
        if not isinstance(workspace, str) or not Path(workspace).is_dir():
            raise ContractViolation("resident Orca definition has an invalid workspace")
        return GraphSpec.from_dict(graph), job_root, Path(workspace)
