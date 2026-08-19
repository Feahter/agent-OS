import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .agents import AgentResult, validate_agent_outputs
from .errors import AgentExecutionError, AgentProtocolError, ContractViolation
from .events import GraphEvent
from .model import GraphSpec, NodeSpec
from .validation import validate_graph


ORCA_AGENT_IDS = {
    "claude-code": "claude",
    "codex": "codex",
    "pi-agent": "pi",
}
ORCA_CAPABILITIES = {
    "model_selection",
    "structured_output",
    "token_usage",
    "tool_policy",
    "workspace_isolation",
}


@dataclass(frozen=True)
class OrcaCompiledTask:
    node_id: str
    spec: str
    deps: Tuple[str, ...]
    gate: Optional[str]
    agent: str
    model: Optional[str]
    workspace_mode: str
    workspace_lineage: str
    workspace_retain: str
    max_attempts: int
    controlled_merge: Optional[Mapping[str, str]] = None

    def to_dict(self) -> Mapping[str, Any]:
        value = {
            "node_id": self.node_id,
            "spec": self.spec,
            "deps": list(self.deps),
            "gate": self.gate,
            "agent": self.agent,
            "model": self.model,
            "workspace": {
                "mode": self.workspace_mode,
                "lineage": self.workspace_lineage,
                "retain": self.workspace_retain,
            },
            "max_attempts": self.max_attempts,
        }
        if self.controlled_merge is not None:
            value["controlled_merge"] = dict(self.controlled_merge)
        return value


@dataclass(frozen=True)
class OrcaPlan:
    graph_id: str
    graph_fingerprint: str
    objective: str
    tasks: Tuple[OrcaCompiledTask, ...]
    scheduler_owner: str = "graph-engineering"
    retry_policy_owner: str = "graph-engineering"
    gate_decision_owner: str = "graph-engineering"
    worker_lifecycle_owner: str = "orca"

    def task_map(self) -> Mapping[str, OrcaCompiledTask]:
        return {task.node_id: task for task in self.tasks}

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "graph_id": self.graph_id,
            "graph_fingerprint": self.graph_fingerprint,
            "objective": self.objective,
            "ownership": {
                "scheduler": self.scheduler_owner,
                "retry_policy": self.retry_policy_owner,
                "gate_decision": self.gate_decision_owner,
                "worker_lifecycle": self.worker_lifecycle_owner,
            },
            "tasks": [task.to_dict() for task in self.tasks],
        }


class OrcaGraphCompiler:
    def __init__(self, default_agent: str = "codex"):
        if default_agent not in ORCA_AGENT_IDS.values():
            raise ContractViolation(f"unsupported default Orca agent: {default_agent}")
        self.default_agent = default_agent

    def compile(self, graph: GraphSpec, objective: Optional[str] = None) -> OrcaPlan:
        validate_graph(graph)
        tasks = tuple(self._compile_node(node) for node in graph.nodes)
        return OrcaPlan(
            graph.id,
            graph.fingerprint(),
            objective or f"Execute Graph Engineering graph {graph.id}",
            tasks,
        )

    def _compile_node(self, node: NodeSpec) -> OrcaCompiledTask:
        if node.kind != "agent" or node.agent is None:
            raise ContractViolation(
                f"Orca backend only accepts agent nodes; {node.id} has kind {node.kind}"
            )
        executor = node.agent.executor
        unsupported = set(node.agent.required_capabilities) - ORCA_CAPABILITIES
        if unsupported:
            raise ContractViolation(
                f"agent node {node.id} requires unsupported Orca capabilities: "
                f"{', '.join(sorted(unsupported))}"
            )
        if node.agent.max_cost_usd is not None:
            raise ContractViolation(
                f"agent node {node.id} sets max_cost_usd, which Orca cannot enforce"
            )
        if node.max_tokens is not None:
            raise ContractViolation(
                f"agent node {node.id} sets max_tokens, which Orca cannot enforce"
            )
        if node.retry.max_attempts > 3:
            raise ContractViolation(
                f"agent node {node.id} exceeds Orca's three-attempt dispatch limit"
            )
        if executor is None:
            agent = self.default_agent
        else:
            try:
                agent = ORCA_AGENT_IDS[executor]
            except KeyError as error:
                raise ContractViolation(
                    f"agent node {node.id} uses executor {executor}, which Orca cannot launch"
                ) from error
        contract = {
            "node_id": node.id,
            "task_type": node.agent.task_type,
            "model_family": node.agent.model_family,
            "reads": list(node.reads),
            "writes": list(node.writes),
            "tools": list(node.agent.tools),
            "result_protocol": {
                "worker_done_payload": {
                    "outputs": {key: "<value>" for key in node.writes},
                    "text": "<final response>",
                    "tokens_used": "<non-negative integer>",
                    "cost_usd": "<optional non-negative number>",
                }
            },
        }
        if node.agent.workspace.mode == "isolated":
            contract["result_protocol"]["worker_done_payload"]["changes"] = {
                "workspace_id": "<Orca worktree id>",
                "base_ref": "<base revision>",
                "head_ref": "<result revision>",
                "files_modified": ["<relative path>"],
                "patch_path": "<optional patch artifact>",
                "conflicts": [],
            }
        if node.controlled_merge is not None:
            contract["controlled_merge"] = {
                "verifier": node.controlled_merge.verifier,
                "target_branch": node.controlled_merge.target_branch,
                "owner": "graph-engineering",
            }
        spec = (
            f"{node.agent.prompt}\n\n"
            "Graph Engineering contract:\n"
            f"{json.dumps(contract, ensure_ascii=False, sort_keys=True)}\n"
            "Report exactly one worker_done. Put the structured result in its JSON payload."
        )
        workspace = node.agent.workspace
        return OrcaCompiledTask(
            node.id,
            spec,
            node.deps,
            node.gate,
            agent,
            node.agent.model,
            workspace.mode,
            workspace.lineage,
            workspace.retain,
            node.retry.max_attempts,
            (
                {
                    "verifier": node.controlled_merge.verifier,
                    "target_branch": node.controlled_merge.target_branch,
                }
                if node.controlled_merge is not None
                else None
            ),
        )


OrcaRunner = Callable[[Sequence[str], Optional[Path], int], Mapping[str, Any]]


class OrcaClient:
    def __init__(
        self,
        command: Sequence[str] = ("orca",),
        runner: Optional[OrcaRunner] = None,
        cwd: Optional[Path] = None,
        timeout_seconds: int = 30,
    ):
        if not command:
            raise ContractViolation("Orca command cannot be empty")
        self.command = tuple(command)
        self.runner = runner or self._subprocess_runner
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds

    def call(self, arguments: Sequence[str]) -> Mapping[str, Any]:
        payload = self.runner(
            (*self.command, *arguments, "--json"), self.cwd, self.timeout_seconds
        )
        if not isinstance(payload, dict):
            raise AgentProtocolError("Orca response must be a JSON object")
        if payload.get("ok") is False:
            error = payload.get("error") or payload.get("message") or "unknown error"
            raise AgentExecutionError(f"Orca command failed: {error}")
        result = payload.get("result", payload)
        if not isinstance(result, dict):
            raise AgentProtocolError("Orca result must be a JSON object")
        return result

    @staticmethod
    def _subprocess_runner(
        command: Sequence[str], cwd: Optional[Path], timeout_seconds: int
    ) -> Mapping[str, Any]:
        try:
            completed = subprocess.run(
                list(command),
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            raise AgentExecutionError(f"Orca executable is unavailable: {command[0]}") from error
        except subprocess.TimeoutExpired as error:
            raise AgentExecutionError(
                f"Orca command timed out after {timeout_seconds}s"
            ) from error
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise AgentExecutionError(
                f"Orca exited with {completed.returncode}: {detail[-1000:]}"
            )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise AgentProtocolError(f"invalid Orca JSON response: {error}") from error
        if not isinstance(value, dict):
            raise AgentProtocolError("Orca response must be a JSON object")
        return value


@dataclass(frozen=True)
class OrcaMaterializedRun:
    run_id: str
    task_ids: Mapping[str, str]
    gate_ids: Mapping[str, str]

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "run_id": self.run_id,
            "task_ids": dict(self.task_ids),
            "gate_ids": dict(self.gate_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OrcaMaterializedRun":
        try:
            run_id = value["run_id"]
            task_ids = value["task_ids"]
            gate_ids = value["gate_ids"]
        except KeyError as error:
            raise ContractViolation("materialized Orca run receipt is incomplete") from error
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(task_ids, dict)
            or not isinstance(gate_ids, dict)
            or any(
                not isinstance(key, str)
                or not isinstance(item, str)
                or not key
                or not item
                for values in (task_ids, gate_ids)
                for key, item in values.items()
            )
        ):
            raise ContractViolation("invalid materialized Orca run receipt")
        return cls(run_id, dict(task_ids), dict(gate_ids))


@dataclass(frozen=True)
class ChangeSetArtifact:
    workspace_id: str
    base_ref: str
    head_ref: str
    files_modified: Tuple[str, ...]
    patch_path: Optional[str] = None
    conflicts: Tuple[str, ...] = ()


def change_set_from_worker_done(message: Mapping[str, Any]) -> ChangeSetArtifact:
    payload = message.get("payload")
    changes = payload.get("changes") if isinstance(payload, dict) else None
    if not isinstance(changes, dict):
        raise AgentProtocolError("Orca worker_done has no change-set payload")
    required = ("workspace_id", "base_ref", "head_ref", "files_modified")
    if any(key not in changes for key in required):
        raise AgentProtocolError("Orca change-set payload is incomplete")
    files = changes["files_modified"]
    conflicts = changes.get("conflicts", [])
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise AgentProtocolError("Orca change-set files_modified must be an array of strings")
    if not isinstance(conflicts, list) or not all(
        isinstance(item, str) for item in conflicts
    ):
        raise AgentProtocolError("Orca change-set conflicts must be an array of strings")
    scalar_fields = ("workspace_id", "base_ref", "head_ref")
    if any(not isinstance(changes[key], str) or not changes[key] for key in scalar_fields):
        raise AgentProtocolError("Orca change-set refs must be non-empty strings")
    patch_path = changes.get("patch_path")
    if patch_path is not None and not isinstance(patch_path, str):
        raise AgentProtocolError("Orca change-set patch_path must be a string")
    return ChangeSetArtifact(
        changes["workspace_id"],
        changes["base_ref"],
        changes["head_ref"],
        tuple(files),
        patch_path,
        tuple(conflicts),
    )


def assess_change_set(change_set: ChangeSetArtifact, gate_resolved: bool) -> str:
    if change_set.conflicts:
        return "conflicted"
    if not gate_resolved:
        return "awaiting_gate"
    return "ready"


def _entity_id(result: Mapping[str, Any], entity: str) -> str:
    nested = result.get(entity)
    if isinstance(nested, dict) and nested.get("id"):
        return str(nested["id"])
    for key in (f"{entity}Id", f"{entity}_id", "id"):
        if result.get(key):
            return str(result[key])
    raise AgentProtocolError(f"Orca {entity} result has no id")


class OrcaBackend:
    def __init__(self, client: OrcaClient):
        self.client = client

    def materialize(self, plan: OrcaPlan) -> OrcaMaterializedRun:
        run = self.client.call(
            ("orchestration", "run-create", "--objective", plan.objective)
        )
        run_id = _entity_id(run, "run")
        task_ids: Dict[str, str] = {}
        gate_ids: Dict[str, str] = {}
        for task in plan.tasks:
            arguments = ["orchestration", "task-create", "--spec", task.spec]
            if task.deps:
                arguments.extend(
                    ("--deps", json.dumps([task_ids[dep] for dep in task.deps]))
                )
            created = self.client.call(arguments)
            task_id = _entity_id(created, "task")
            task_ids[task.node_id] = task_id
            if task.gate is not None:
                gate = self.client.call(
                    (
                        "orchestration",
                        "gate-create",
                        "--task",
                        task_id,
                        "--question",
                        task.gate,
                    )
                )
                gate_ids[task.node_id] = _entity_id(gate, "gate")
        return OrcaMaterializedRun(run_id, dict(task_ids), dict(gate_ids))

    def start_worker(
        self,
        plan: OrcaPlan,
        materialized: OrcaMaterializedRun,
        node_id: str,
        attempt: int = 1,
        retry_of: Optional[str] = None,
    ) -> Mapping[str, Any]:
        try:
            task = plan.task_map()[node_id]
            task_id = materialized.task_ids[node_id]
        except KeyError as error:
            raise ContractViolation(f"unknown materialized Orca node: {node_id}") from error
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ContractViolation("Orca worker attempt must be a positive integer")
        if attempt > task.max_attempts:
            raise ContractViolation(
                f"agent node {node_id} exceeds max_attempts={task.max_attempts}"
            )
        if (attempt == 1) != (retry_of is None):
            raise ContractViolation(
                "first Orca attempt cannot use retry_of; later attempts require it"
            )
        arguments = ["orchestration", "worker-start", "--task", task_id]
        if task.workspace_mode == "shared":
            arguments.extend(("--worktree", "current"))
        else:
            placement = "new-child" if task.workspace_lineage == "child" else "new-top-level"
            arguments.extend(
                (
                    "--worktree",
                    placement,
                    "--name",
                    f"ge-{node_id}-a{attempt}",
                    "--setup",
                    "run",
                )
            )
        arguments.extend(("--agent", task.agent))
        if task.model is not None:
            arguments.extend(("--model", task.model))
        if retry_of is not None:
            arguments.extend(("--retry-of", retry_of))
        return self.client.call(arguments)

    def resolve_gate(
        self,
        materialized: OrcaMaterializedRun,
        node_id: str,
        resolution: str,
    ) -> Mapping[str, Any]:
        try:
            gate_id = materialized.gate_ids[node_id]
        except KeyError as error:
            raise ContractViolation(f"Orca node {node_id} has no materialized gate") from error
        if not resolution.strip():
            raise ContractViolation("Orca gate resolution cannot be empty")
        return self.client.call(
            (
                "orchestration",
                "gate-resolve",
                "--id",
                gate_id,
                "--resolution",
                resolution,
            )
        )

    def wait_delivery(self, timeout_ms: int = 900000) -> Mapping[str, Any]:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1:
            raise ContractViolation("Orca wait timeout_ms must be a positive integer")
        return self.client.call(
            (
                "orchestration",
                "check",
                "--wait",
                "--types",
                "worker_done,escalation,question",
                "--timeout-ms",
                str(timeout_ms),
            )
        )

    def acknowledge_delivery(self, delivery_id: str) -> Mapping[str, Any]:
        if not isinstance(delivery_id, str) or not delivery_id.strip():
            raise ContractViolation("Orca delivery_id cannot be empty")
        return self.client.call(
            ("orchestration", "check", "--ack", delivery_id.strip())
        )

    def reply(self, message_id: str, body: str) -> Mapping[str, Any]:
        if not isinstance(message_id, str) or not message_id.strip():
            raise ContractViolation("Orca message_id cannot be empty")
        if not isinstance(body, str) or not body.strip():
            raise ContractViolation("Orca reply body cannot be empty")
        return self.client.call(
            (
                "orchestration",
                "reply",
                "--id",
                message_id.strip(),
                "--body",
                body,
            )
        )

    def stop_worker(self, dispatch_id: str) -> Mapping[str, Any]:
        if not isinstance(dispatch_id, str) or not dispatch_id.strip():
            raise ContractViolation("Orca dispatch_id cannot be empty")
        return self.client.call(
            ("orchestration", "worker-stop", "--dispatch", dispatch_id.strip())
        )

    def finish_worker(
        self, dispatch_id: str, retain: str, succeeded: bool
    ) -> Mapping[str, Any]:
        if not isinstance(dispatch_id, str) or not dispatch_id.strip():
            raise ContractViolation("Orca dispatch_id cannot be empty")
        if retain not in ("always", "never", "on_failure"):
            raise ContractViolation("invalid Orca worker retention policy")
        if not isinstance(succeeded, bool):
            raise ContractViolation("Orca worker succeeded must be a boolean")
        should_retain = retain == "always" or (retain == "on_failure" and not succeeded)
        action = "worker-retain" if should_retain else "worker-release"
        return self.client.call(
            ("orchestration", action, "--dispatch", dispatch_id.strip())
        )


def agent_result_from_worker_done(
    message: Mapping[str, Any], executor_id: str, output_keys: Sequence[str]
) -> AgentResult:
    if message.get("type") != "worker_done":
        raise AgentProtocolError("Orca message is not worker_done")
    if message.get("outcome") != "succeeded":
        raise AgentExecutionError(
            f"Orca worker failed: {message.get('body') or message.get('subject') or ''}"
        )
    payload = message.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("outputs"), dict):
        raise AgentProtocolError("Orca worker_done has no structured outputs payload")
    tokens = payload.get("tokens_used", 0)
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise AgentProtocolError("Orca worker_done tokens_used must be a non-negative integer")
    cost = payload.get("cost_usd")
    if cost is not None and (
        isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0
    ):
        raise AgentProtocolError("Orca worker_done cost_usd must be non-negative")
    text = payload.get("text", message.get("body", ""))
    if not isinstance(text, str):
        raise AgentProtocolError("Orca worker_done text must be a string")
    session = message.get("dispatchId") or message.get("dispatch_id")
    return AgentResult(
        executor_id,
        validate_agent_outputs(payload["outputs"], output_keys),
        text,
        tokens,
        float(cost) if cost is not None else None,
        str(session) if session is not None else None,
    )


def dispatch_id_from_receipt(receipt: Mapping[str, Any]) -> str:
    return _entity_id(receipt, "dispatch")


def graph_event_from_orca_message(
    message: Mapping[str, Any], run_id: str, graph_id: str, node_id: str, attempt: int
) -> GraphEvent:
    message_type = message.get("type")
    if message_type == "worker_done":
        event = "node_completed" if message.get("outcome") == "succeeded" else "node_failed"
    elif message_type == "escalation":
        event = "node_escalated"
    elif message_type == "question":
        event = "node_waiting_for_input"
    else:
        raise AgentProtocolError(f"unsupported Orca message type: {message_type}")
    return GraphEvent.create(
        event,
        run_id,
        graph_id,
        node_id,
        attempt,
        {
            "backend": "orca",
            "subject": message.get("subject"),
            "body": message.get("body"),
            "dispatch_id": message.get("dispatchId") or message.get("dispatch_id"),
        },
    )
