import hashlib
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from . import orca_protocol
from ._store import file_lock
from .artifacts import ArtifactRecord, ArtifactStore
from .control import EffectJournal, EventPage
from .errors import ContractViolation, MergeRejectedError
from .events import GraphEvent, JsonlEventSink
from .merge import ControlledGitMerger, MergeCandidate, MergeReceipt
from .model import GraphSpec, NodeSpec
from .orca import (
    ORCA_AGENT_IDS,
    OrcaBackend,
    OrcaGraphCompiler,
    OrcaMaterializedRun,
    agent_result_from_worker_done,
    change_set_from_worker_done,
    dispatch_id_from_receipt,
)
from .orca_publication import OrcaPublicationRecorder
from .orca_state import OrcaRunStore
from .policy import DenyNamedGatesPolicy, GateDecision, GatePolicy
from .publication import VerifiedResultPublisher
from .reuse import VerifiedArtifactCache

_TERMINAL_NODE_STATES = {"completed", "failed", "blocked", "cancelled"}
_TERMINAL_PHASES = {"succeeded", "failed", "cancelled"}
_EXECUTOR_IDS = {value: key for key, value in ORCA_AGENT_IDS.items()}


@dataclass(frozen=True)
class OrcaCoordinatorSnapshot:
    run_id: str
    graph_id: str
    phase: str
    statuses: Mapping[str, str]
    attempts: Mapping[str, int]
    active_dispatches: Mapping[str, str]
    pending_questions: Tuple[str, ...]
    pending_escalations: Tuple[str, ...]
    tokens_used: int
    cost_usd: float
    artifacts: Mapping[str, Any]
    merges: Mapping[str, Any]
    next_cursor: int


class OrcaCoordinator:
    """Owns GE policy while Orca owns Tasks, Dispatches, and workers."""

    def __init__(
        self,
        graph: GraphSpec,
        backend: OrcaBackend,
        root: Path,
        workspace: Path,
        gate_policy: Optional[GatePolicy] = None,
        reuse_store: Optional[VerifiedArtifactCache] = None,
        clock=time.time,
    ):
        if not workspace.is_dir():
            raise ContractViolation(
                f"Orca coordinator workspace does not exist: {workspace}"
            )
        self.graph = graph
        self.plan = OrcaGraphCompiler().compile(graph)
        self.backend = backend
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace = workspace.resolve()
        self.gate_policy = gate_policy or DenyNamedGatesPolicy()
        self._clock = clock
        self._store = OrcaRunStore(
            root / "state.json", graph, self.workspace, clock=clock
        )
        self._lock_path = root / "coordinator.lock"
        self._lock_path.touch(exist_ok=True)
        self._wait_lock_path = root / "delivery-wait.lock"
        self._wait_lock_path.touch(exist_ok=True)
        self._events = JsonlEventSink(root / "events.jsonl")
        self._effects = EffectJournal(root / "effects")
        self._merger = (
            ControlledGitMerger(self.workspace)
            if any(node.controlled_merge is not None for node in graph.nodes)
            else None
        )
        self._publications = OrcaPublicationRecorder(
            graph,
            VerifiedResultPublisher(
                reuse_store, root / "verified-publications.json"
            )
            if reuse_store is not None
            else None,
            self._workspace_for,
            self._emit,
        )

    def start(self) -> OrcaCoordinatorSnapshot:
        with self._locked():
            state = self._load_or_create_state()
            self._ensure_materialized(state)
            self._publications.reconcile(state, self._artifacts(state))
            self._schedule(state)
            self._settle_phase(state)
            self._save(state)
            return self._snapshot(state)

    def advance(self, timeout_ms: int = 900000) -> OrcaCoordinatorSnapshot:
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or timeout_ms < 1
        ):
            raise ContractViolation(
                "Orca coordinator timeout_ms must be a positive integer"
            )
        with self._locked():
            state = self._load_or_create_state()
            self._ensure_materialized(state)
            self._publications.reconcile(state, self._artifacts(state))
            self._schedule(state)
            self._settle_phase(state)
            self._save(state)
            if state["phase"] in _TERMINAL_PHASES:
                return self._snapshot(state)
            if not state["active_dispatches"]:
                return self._snapshot(state)

        # Waiting is an external, potentially long-running operation. Serialize
        # waiters separately so control operations can still acquire the state
        # lock to inspect, answer, pause, or cancel the run.
        with file_lock(self._wait_lock_path):
            with self._locked():
                state = self._store.read()
                if (
                    state["phase"] in _TERMINAL_PHASES
                    or not state["active_dispatches"]
                ):
                    return self._snapshot(state)
            delivery = orca_protocol.normalize_delivery(
                self.backend.wait_delivery(timeout_ms=timeout_ms)
            )
            with self._locked():
                state = self._store.read()
                if delivery is None:
                    if state["phase"] not in _TERMINAL_PHASES:
                        self._emit(
                            state,
                            "orca_wait_checkpoint",
                            payload={"timeout_ms": timeout_ms},
                        )
                        self._save(state)
                    return self._snapshot(state)
                delivery_id, messages = delivery
                self._process_delivery(state, delivery_id, messages)
                if state["phase"] not in _TERMINAL_PHASES:
                    self._schedule(state)
                    self._settle_phase(state)
                self._save(state)
                return self._snapshot(state)

    def run(
        self, timeout_ms: int = 900000, max_deliveries: int = 1000
    ) -> OrcaCoordinatorSnapshot:
        if (
            isinstance(max_deliveries, bool)
            or not isinstance(max_deliveries, int)
            or max_deliveries < 1
        ):
            raise ContractViolation("max_deliveries must be a positive integer")
        snapshot = self.start()
        for _ in range(max_deliveries):
            if snapshot.phase in _TERMINAL_PHASES:
                return snapshot
            if snapshot.pending_questions or snapshot.pending_escalations:
                return snapshot
            snapshot = self.advance(timeout_ms=timeout_ms)
        raise TimeoutError("Orca coordinator exceeded max_deliveries")

    def inspect(self) -> OrcaCoordinatorSnapshot:
        with self._locked():
            return self._snapshot(self._store.read())

    def cancel(self) -> OrcaCoordinatorSnapshot:
        with self._locked():
            state = self._store.read()
            if state["phase"] in _TERMINAL_PHASES:
                return self._snapshot(state)
            for node_id, dispatch_id in tuple(state["active_dispatches"].items()):
                self._effect(
                    "stop",
                    dispatch_id,
                    {"dispatch_id": dispatch_id, "action": "cancel"},
                    lambda dispatch_id=dispatch_id: self.backend.stop_worker(dispatch_id),
                )
                node = self.graph.node_map()[node_id]
                self._finish_dispatch(state, node, dispatch_id, False)
                self._deactivate_dispatch(state, node_id, dispatch_id)
            for node_id, status in tuple(state["statuses"].items()):
                if status not in _TERMINAL_NODE_STATES:
                    state["statuses"][node_id] = "cancelled"
            delivery_ids = set()
            for message in state["messages"].values():
                if message.get("status") != "pending":
                    continue
                message["status"] = "resolved"
                message["resolution"] = "run_cancelled"
                delivery_ids.add(message["delivery_id"])
            for delivery_id in sorted(delivery_ids):
                self._ack_if_resolved(state, delivery_id)
            state["phase"] = "cancelled"
            self._emit(state, "run_cancelled", payload={"backend": "orca"})
            self._save(state)
            return self._snapshot(state)

    def events(self, after: int = 0) -> EventPage:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ContractViolation("event cursor must be a non-negative integer")
        with self._locked():
            self._store.read()
            records = tuple(self._events.read())
            return EventPage(records[after:], len(records))

    def answer_question(self, message_id: str, body: str) -> OrcaCoordinatorSnapshot:
        with self._locked():
            state = self._store.read()
            message = self._pending_message(state, message_id, "question")
            self._effect(
                "reply",
                message_id,
                {"message_id": message_id, "body": body},
                lambda: self.backend.reply(message_id, body),
            )
            message["status"] = "resolved"
            message["resolution"] = "answered"
            node_id = message["node_id"]
            if state["statuses"].get(node_id) == "waiting_for_input":
                state["statuses"][node_id] = "running"
            self._emit(
                state,
                "node_input_supplied",
                node_id,
                message["attempt"],
                {"message_id": message_id},
            )
            self._ack_if_resolved(state, message["delivery_id"])
            self._settle_phase(state)
            self._save(state)
            return self._snapshot(state)

    def resolve_escalation(
        self, message_id: str, action: str, response: Optional[str] = None
    ) -> OrcaCoordinatorSnapshot:
        if action not in ("continue", "retry", "fail"):
            raise ContractViolation(
                "Orca escalation action must be continue, retry, or fail"
            )
        with self._locked():
            state = self._store.read()
            message = self._pending_message(state, message_id, "escalation")
            node_id = message["node_id"]
            dispatch_id = message["dispatch_id"]
            attempt = int(message["attempt"])
            if action == "continue":
                if response is None or not response.strip():
                    raise ContractViolation(
                        "continuing an Orca escalation requires a response"
                    )
                self._effect(
                    "reply",
                    message_id,
                    {"message_id": message_id, "body": response},
                    lambda: self.backend.reply(message_id, response),
                )
                state["statuses"][node_id] = "running"
            else:
                self._effect(
                    "stop",
                    dispatch_id,
                    {"dispatch_id": dispatch_id, "action": action},
                    lambda: self.backend.stop_worker(dispatch_id),
                )
                node = self.graph.node_map()[node_id]
                self._finish_dispatch(state, node, dispatch_id, False)
                self._deactivate_dispatch(state, node_id, dispatch_id)
                if action == "retry" and attempt < node.retry.max_attempts:
                    state["statuses"][node_id] = "pending"
                    state["retry_of"][node_id] = dispatch_id
                    self._emit(
                        state,
                        "node_retry",
                        node_id,
                        attempt,
                        {"backend": "orca", "reason": "escalation"},
                    )
                else:
                    state["statuses"][node_id] = "failed"
                    self._emit(
                        state,
                        "node_failed",
                        node_id,
                        attempt,
                        {"backend": "orca", "reason": "escalation"},
                    )
            message["status"] = "resolved"
            message["resolution"] = action
            self._ack_if_resolved(state, message["delivery_id"])
            self._schedule(state)
            self._settle_phase(state)
            self._save(state)
            return self._snapshot(state)

    def retry(self, node_id: str) -> OrcaCoordinatorSnapshot:
        with self._locked():
            state = self._store.read()
            node = self.graph.node_map().get(node_id)
            if node is None:
                raise ContractViolation(f"unknown Orca coordinator node: {node_id}")
            if state["statuses"].get(node_id) != "failed":
                raise ContractViolation(f"Orca node {node_id} is not retryable")
            if int(state["attempts"][node_id]) >= node.retry.max_attempts:
                raise ContractViolation(
                    f"agent node {node_id} exceeds max_attempts={node.retry.max_attempts}"
                )
            state["statuses"][node_id] = "pending"
            previous = orca_protocol.latest_dispatch(state, node_id)
            if previous is not None:
                state["retry_of"][node_id] = previous
            for descendant in self._descendants(node_id):
                if state["statuses"][descendant] == "blocked":
                    state["statuses"][descendant] = "pending"
            self._schedule(state)
            self._settle_phase(state)
            self._save(state)
            return self._snapshot(state)

    def _load_or_create_state(self) -> Dict[str, Any]:
        if self._store.exists():
            return self._store.read()
        state = self._store.create()
        self._emit(state, "run_started", payload={"backend": "orca"})
        return state

    def _save(self, state: Dict[str, Any]) -> None:
        self._store.save(state)

    def _ensure_materialized(self, state: Dict[str, Any]) -> None:
        if state["materialized"] is not None:
            materialized = OrcaMaterializedRun.from_dict(state["materialized"])
            self._store.validate_materialized(materialized)
            return
        payload = {
            "coordinator_run_id": state["run_id"],
            "graph_id": self.graph.id,
            "graph_fingerprint": self.graph.fingerprint(),
            "plan": self.plan.to_dict(),
        }
        receipt = self._effect(
            "materialize",
            str(state["run_id"]),
            payload,
            lambda: self.backend.materialize(self.plan).to_dict(),
        )
        if not isinstance(receipt, dict):
            raise ContractViolation("Orca materialization returned no receipt")
        materialized = OrcaMaterializedRun.from_dict(receipt)
        self._store.validate_materialized(materialized)
        state["materialized"] = materialized.to_dict()
        state["phase"] = "running"
        self._emit(
            state,
            "orca_run_materialized",
            payload={"orca_run_id": materialized.run_id},
        )
        self._save(state)

    def _schedule(self, state: Dict[str, Any]) -> None:
        changed = True
        while changed:
            changed = False
            for node in self.graph.nodes:
                if state["statuses"][node.id] != "pending":
                    continue
                dependency_states = [state["statuses"][dep] for dep in node.deps]
                if any(
                    item in ("failed", "blocked", "cancelled")
                    for item in dependency_states
                ):
                    state["statuses"][node.id] = "blocked"
                    self._emit(
                        state,
                        "node_blocked",
                        node.id,
                        payload={"backend": "orca", "reason": "dependency"},
                    )
                    changed = True
            running = len(state["active_dispatches"])
            for node in self.graph.nodes:
                if running >= self.graph.max_concurrency:
                    break
                if state["statuses"][node.id] != "pending":
                    continue
                if not all(
                    state["statuses"][dep] == "completed" for dep in node.deps
                ):
                    continue
                if not self._admit_tokens(state, node):
                    if running > 0:
                        continue
                    state["statuses"][node.id] = "blocked"
                    self._emit(
                        state,
                        "node_blocked",
                        node.id,
                        payload={"backend": "orca", "reason": "token_budget"},
                    )
                    changed = True
                    continue
                if node.gate is not None:
                    decision = self.gate_policy.decide(
                        node, self._artifacts(state).values()
                    )
                    resolution = (
                        "approved" if decision is GateDecision.ALLOW else "denied"
                    )
                    self._resolve_gate(state, node, resolution)
                    state["gate_resolutions"][node.id] = resolution
                    if decision is GateDecision.DENY:
                        state["statuses"][node.id] = "blocked"
                        self._emit(
                            state,
                            "node_blocked",
                            node.id,
                            payload={
                                "backend": "orca",
                                "reason": "gate",
                                "gate": node.gate,
                            },
                        )
                        changed = True
                        continue
                self._start_node(state, node)
                running += 1
                changed = True
            if running:
                break

    def _admit_tokens(self, state: Mapping[str, Any], node: NodeSpec) -> bool:
        maximum = self.graph.max_tokens
        if maximum is None:
            return True
        reserved = sum(int(item) for item in state["reserved_tokens"].values())
        return int(state["tokens_used"]) + reserved + node.estimated_tokens <= maximum

    def _resolve_gate(
        self, state: Mapping[str, Any], node: NodeSpec, resolution: str
    ) -> None:
        materialized = OrcaMaterializedRun.from_dict(state["materialized"])
        self._effect(
            "gate",
            materialized.gate_ids[node.id],
            {
                "node_id": node.id,
                "gate_id": materialized.gate_ids[node.id],
                "resolution": resolution,
            },
            lambda: self.backend.resolve_gate(materialized, node.id, resolution),
        )

    def _start_node(self, state: Dict[str, Any], node: NodeSpec) -> None:
        materialized = OrcaMaterializedRun.from_dict(state["materialized"])
        attempt = int(state["attempts"][node.id]) + 1
        retry_of = state["retry_of"].pop(node.id, None)
        inputs = self._artifacts(state).snapshot(node.reads)
        payload = {
            "node_id": node.id,
            "attempt": attempt,
            "retry_of": retry_of,
            "task_id": materialized.task_ids[node.id],
            "input_artifacts": [record.to_dict() for record in inputs],
        }
        receipt = self._effect(
            "dispatch",
            f"{state['run_id']}:{node.id}:{attempt}",
            payload,
            lambda: self.backend.start_worker(
                self.plan,
                materialized,
                node.id,
                attempt=attempt,
                retry_of=retry_of,
            ),
        )
        if not isinstance(receipt, dict):
            raise ContractViolation("Orca worker start returned no receipt")
        dispatch_id = dispatch_id_from_receipt(receipt)
        if dispatch_id in state["dispatches"]:
            existing = state["dispatches"][dispatch_id]
            if existing["node_id"] != node.id or existing["attempt"] != attempt:
                raise ContractViolation("Orca dispatch id was reused across nodes")
        state["attempts"][node.id] = attempt
        state["statuses"][node.id] = "running"
        state["active_dispatches"][node.id] = dispatch_id
        state["reserved_tokens"][node.id] = node.estimated_tokens
        state["dispatches"][dispatch_id] = {
            "node_id": node.id,
            "attempt": attempt,
            "retry_of": retry_of,
            "input_artifacts": [record.to_dict() for record in inputs],
            "cleanup": None,
        }
        self._emit(
            state,
            "node_started",
            node.id,
            attempt,
            {"backend": "orca", "dispatch_id": dispatch_id},
        )
        self._save(state)

    def _process_delivery(
        self,
        state: Dict[str, Any],
        delivery_id: str,
        messages: Sequence[Mapping[str, Any]],
    ) -> None:
        delivery = state["deliveries"].setdefault(
            delivery_id,
            {
                "message_ids": [],
                "message_digest": orca_protocol.digest(messages),
                "acknowledged": False,
            },
        )
        if delivery.get("message_digest") != orca_protocol.digest(messages):
            raise ContractViolation(
                f"Orca delivery {delivery_id} was replayed with different messages"
            )
        if delivery["acknowledged"]:
            return
        for message in messages:
            message_id = orca_protocol.message_id(
                message, require_real=message.get("type") != "worker_done"
            )
            if message_id not in delivery["message_ids"]:
                delivery["message_ids"].append(message_id)
            existing = state["messages"].get(message_id)
            message_digest = orca_protocol.digest(message)
            if existing is not None:
                if existing.get("message_digest") != message_digest:
                    raise ContractViolation(
                        f"Orca message id {message_id} was reused with different content"
                    )
                if existing.get("delivery_id") != delivery_id:
                    raise ContractViolation(
                        f"Orca message {message_id} moved to a different delivery"
                    )
                continue
            self._process_message(
                state, delivery_id, message_id, message_digest, message
            )
            self._save(state)
        self._ack_if_resolved(state, delivery_id)

    def _process_message(
        self,
        state: Dict[str, Any],
        delivery_id: str,
        message_id: str,
        message_digest: str,
        message: Mapping[str, Any],
    ) -> None:
        message_type = message.get("type")
        if message_type not in ("worker_done", "question", "escalation"):
            raise ContractViolation(f"unsupported Orca delivery type: {message_type}")
        dispatch_id = orca_protocol.dispatch_id(message)
        dispatch = state["dispatches"].get(dispatch_id)
        if dispatch is None:
            state["messages"][message_id] = {
                "type": message_type,
                "status": "resolved",
                "resolution": "rejected_unknown_dispatch",
                "delivery_id": delivery_id,
                "message_digest": message_digest,
                "dispatch_id": dispatch_id,
            }
            self._emit(
                state,
                "orca_message_rejected",
                payload={
                    "message_id": message_id,
                    "dispatch_id": dispatch_id,
                    "reason": "unknown_dispatch",
                },
            )
            return
        node_id = dispatch["node_id"]
        attempt = int(dispatch["attempt"])
        active = state["active_dispatches"].get(node_id)
        if active != dispatch_id:
            state["messages"][message_id] = {
                "type": message_type,
                "status": "resolved",
                "resolution": "rejected_stale_dispatch",
                "delivery_id": delivery_id,
                "message_digest": message_digest,
                "dispatch_id": dispatch_id,
                "node_id": node_id,
                "attempt": attempt,
            }
            self._emit(
                state,
                "orca_message_rejected",
                node_id,
                attempt,
                {
                    "message_id": message_id,
                    "dispatch_id": dispatch_id,
                    "reason": "stale_dispatch",
                },
            )
            return
        materialized = OrcaMaterializedRun.from_dict(state["materialized"])
        task_id = orca_protocol.task_id(message)
        if task_id != materialized.task_ids[node_id]:
            raise ContractViolation(
                f"Orca {message_type} task {task_id} does not match node {node_id}"
            )

        record = {
            "type": message_type,
            "status": "pending" if message_type != "worker_done" else "processing",
            "delivery_id": delivery_id,
            "message_digest": message_digest,
            "dispatch_id": dispatch_id,
            "node_id": node_id,
            "attempt": attempt,
            "subject": message.get("subject"),
            "body": message.get("body"),
        }
        state["messages"][message_id] = record
        if message_type == "question":
            state["statuses"][node_id] = "waiting_for_input"
            self._emit(
                state,
                "node_waiting_for_input",
                node_id,
                attempt,
                {
                    "backend": "orca",
                    "message_id": message_id,
                    "dispatch_id": dispatch_id,
                    "subject": message.get("subject"),
                },
            )
            return
        if message_type == "escalation":
            state["statuses"][node_id] = "escalated"
            self._emit(
                state,
                "node_escalated",
                node_id,
                attempt,
                {
                    "backend": "orca",
                    "message_id": message_id,
                    "dispatch_id": dispatch_id,
                    "subject": message.get("subject"),
                },
            )
            return
        self._handle_worker_done(state, message_id, message, dispatch)

    def _handle_worker_done(
        self,
        state: Dict[str, Any],
        message_id: str,
        message: Mapping[str, Any],
        dispatch: Mapping[str, Any],
    ) -> None:
        node_id = str(dispatch["node_id"])
        attempt = int(dispatch["attempt"])
        dispatch_id = orca_protocol.dispatch_id(message)
        node = self.graph.node_map()[node_id]
        succeeded = message.get("outcome") == "succeeded"
        staged_merge = False
        try:
            if not succeeded:
                raise ContractViolation("Orca worker reported a failed outcome")
            result = agent_result_from_worker_done(
                message, self._executor_id(node), node.writes
            )
            change_set = None
            if node.agent is not None and node.agent.workspace.mode == "isolated":
                change_set = change_set_from_worker_done(message)
                if change_set.conflicts:
                    raise ContractViolation("Orca worker change-set has conflicts")
                self._workspace_for(node, change_set.workspace_id)
            projected_tokens = int(state["tokens_used"]) + result.tokens_used
            if node.max_tokens is not None and result.tokens_used > node.max_tokens:
                raise ContractViolation("Orca worker exceeded node token budget")
            if self.graph.max_tokens is not None and projected_tokens > self.graph.max_tokens:
                raise ContractViolation("Orca worker exceeded graph token budget")
            artifacts = self._artifacts(state)
            input_records = tuple(
                ArtifactRecord.from_dict(item)
                for item in dispatch.get("input_artifacts", ())
            )
            output_records = artifacts.commit_batch(result.outputs, node_id)
            if change_set is not None and node.controlled_merge is not None:
                self._stage_controlled_merge(
                    state,
                    node,
                    attempt,
                    dispatch_id,
                    change_set,
                    output_records,
                )
                staged_merge = True
            self._complete_controlled_merges(
                state,
                node,
                attempt,
                input_records,
                output_records,
            )
            state["tokens_used"] = projected_tokens
            if result.cost_usd is not None:
                state["cost_usd"] = float(state["cost_usd"]) + result.cost_usd
            state["artifacts"] = [record.to_dict() for record in artifacts.records()]
            state["statuses"][node_id] = "completed"
            self._publications.record(
                state,
                node,
                attempt,
                result,
                input_records,
                output_records,
                artifacts,
                change_set.workspace_id if change_set is not None else None,
            )
            payload: Dict[str, Any] = {
                "backend": "orca",
                "message_id": message_id,
                "dispatch_id": dispatch_id,
                "tokens_used": result.tokens_used,
                "cost_usd": result.cost_usd,
                "artifacts": [record.key for record in output_records],
            }
            if change_set is not None:
                payload["change_set"] = asdict(change_set)
            self._emit(state, "node_completed", node_id, attempt, payload)
        except Exception as error:
            succeeded = False
            if (
                attempt < node.retry.max_attempts
                and not isinstance(error, MergeRejectedError)
            ):
                state["statuses"][node_id] = "pending"
                state["retry_of"][node_id] = dispatch_id
                self._emit(
                    state,
                    "node_retry",
                    node_id,
                    attempt,
                    {
                        "backend": "orca",
                        "dispatch_id": dispatch_id,
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
            else:
                state["statuses"][node_id] = "failed"
                self._emit(
                    state,
                    "node_failed",
                    node_id,
                    attempt,
                    {
                        "backend": "orca",
                        "dispatch_id": dispatch_id,
                        "error": f"{type(error).__name__}: {error}",
                    },
                )
        self._finish_dispatch(
            state,
            node,
            dispatch_id,
            succeeded,
            retain_for_merge=staged_merge and succeeded,
        )
        self._deactivate_dispatch(state, node_id, dispatch_id)
        state["messages"][message_id]["status"] = "resolved"
        state["messages"][message_id]["resolution"] = (
            "accepted" if succeeded else "failed"
        )

    def _stage_controlled_merge(
        self,
        state: Dict[str, Any],
        node: NodeSpec,
        attempt: int,
        dispatch_id: str,
        change_set,
        output_records: Sequence[ArtifactRecord],
    ) -> None:
        if self._merger is None or node.controlled_merge is None or node.agent is None:
            raise ContractViolation("controlled Git merger is unavailable")
        workspace = self._workspace_for(node, change_set.workspace_id)
        candidate = self._merger.prepare(
            change_set,
            workspace,
            node.controlled_merge.target_branch,
            str(state["run_id"]),
            node.id,
            attempt,
            node.controlled_merge.verifier,
            output_records,
        )
        existing = state["merge_candidates"].get(candidate.candidate_id)
        if existing is not None:
            if existing.get("candidate") != candidate.to_dict():
                raise ContractViolation("controlled merge candidate identity was reused")
            return
        state["merge_candidates"][candidate.candidate_id] = {
            "candidate": candidate.to_dict(),
            "status": "awaiting_verification",
            "source_dispatch_id": dispatch_id,
            "workspace_retain": node.agent.workspace.retain,
            "receipt": None,
            "workspace_release": None,
        }
        self._emit(
            state,
            "change_set_merge_staged",
            node.id,
            attempt,
            {
                "candidate_id": candidate.candidate_id,
                "verifier_node_id": candidate.verifier_node_id,
                "target_branch": candidate.target_branch,
                "target_head": candidate.target_head,
                "head_commit": candidate.head_commit,
                "files_modified": list(candidate.files_modified),
            },
        )

    def _complete_controlled_merges(
        self,
        state: Dict[str, Any],
        verifier: NodeSpec,
        attempt: int,
        input_records: Sequence[ArtifactRecord],
        output_records: Sequence[ArtifactRecord],
    ) -> None:
        matching = [
            record
            for record in state["merge_candidates"].values()
            if record.get("status") == "awaiting_verification"
            and record.get("candidate", {}).get("verifier_node_id") == verifier.id
        ]
        if not matching:
            return
        if len(matching) != 1 or self._merger is None:
            raise ContractViolation("controlled merge verifier has ambiguous candidates")
        record = matching[0]
        candidate = MergeCandidate.from_dict(record["candidate"])
        gate_resolution = state["gate_resolutions"].get(verifier.id)
        payload = {
            "candidate": candidate.to_dict(),
            "verifier_node_id": verifier.id,
            "verifier_attempt": attempt,
            "gate_resolution": gate_resolution,
            "verifier_inputs": [item.to_dict() for item in input_records],
            "verifier_outputs": [item.to_dict() for item in output_records],
        }
        receipt_value = self._reconcilable_effect(
            "merge",
            candidate.candidate_id,
            payload,
            lambda: self._merger.merge(
                candidate,
                verifier,
                attempt,
                gate_resolution,
                input_records,
                output_records,
            ).to_dict(),
            lambda: self._optional_receipt_dict(
                self._merger.reconcile(
                    candidate,
                    verifier,
                    attempt,
                    gate_resolution,
                    input_records,
                    output_records,
                )
            ),
        )
        if not isinstance(receipt_value, dict):
            raise ContractViolation("controlled merge returned no audit receipt")
        receipt = MergeReceipt.from_dict(receipt_value)
        record["status"] = receipt.status
        record["receipt"] = receipt.to_dict()
        event = (
            "change_set_merged"
            if receipt.status == "merged"
            else "change_set_merge_rejected"
        )
        self._emit(
            state,
            event,
            verifier.id,
            attempt,
            {
                "candidate_id": candidate.candidate_id,
                "source_node_id": candidate.source_node_id,
                "target_branch": candidate.target_branch,
                "target_before": receipt.target_before,
                "target_after": receipt.target_after,
                "merge_commit": receipt.merge_commit,
                "verification_id": receipt.authorization.get("verification_id"),
                "gate": receipt.authorization.get("gate"),
                "reason": receipt.reason,
            },
        )
        if receipt.status != "merged":
            raise MergeRejectedError(
                f"controlled merge rejected: {receipt.reason}"
            )
        self._release_merged_workspace(record, candidate)

    def _release_merged_workspace(
        self, record: Dict[str, Any], candidate: MergeCandidate
    ) -> None:
        if record.get("workspace_retain") == "always":
            record["workspace_release"] = {"action": "retained"}
            return
        dispatch_id = str(record["source_dispatch_id"])
        release = self._effect(
            "merge-cleanup",
            candidate.candidate_id,
            {
                "candidate_id": candidate.candidate_id,
                "dispatch_id": dispatch_id,
                "action": "release",
            },
            lambda: self.backend.finish_worker(dispatch_id, "never", True),
        )
        record["workspace_release"] = release

    @staticmethod
    def _optional_receipt_dict(
        receipt: Optional[MergeReceipt],
    ) -> Optional[Mapping[str, Any]]:
        return None if receipt is None else receipt.to_dict()

    def _finish_dispatch(
        self,
        state: Dict[str, Any],
        node: NodeSpec,
        dispatch_id: str,
        succeeded: bool,
        retain_for_merge: bool = False,
    ) -> None:
        configured_retain = (
            node.agent.workspace.retain if node.agent is not None else "never"
        )
        retain = "always" if retain_for_merge else configured_retain
        receipt = self._effect(
            "cleanup",
            dispatch_id,
            {
                "dispatch_id": dispatch_id,
                "retain": retain,
                "succeeded": succeeded,
                "temporary_for_merge": retain_for_merge,
            },
            lambda: self.backend.finish_worker(dispatch_id, retain, succeeded),
        )
        state["dispatches"][dispatch_id]["cleanup"] = receipt

    @staticmethod
    def _deactivate_dispatch(
        state: Dict[str, Any], node_id: str, dispatch_id: str
    ) -> None:
        if state["active_dispatches"].get(node_id) == dispatch_id:
            del state["active_dispatches"][node_id]
        state["reserved_tokens"].pop(node_id, None)

    def _ack_if_resolved(self, state: Dict[str, Any], delivery_id: str) -> None:
        delivery = state["deliveries"][delivery_id]
        if delivery["acknowledged"]:
            return
        if any(
            state["messages"].get(message_id, {}).get("status") != "resolved"
            for message_id in delivery["message_ids"]
        ):
            return
        self._effect(
            "ack",
            delivery_id,
            {"delivery_id": delivery_id},
            lambda: self.backend.acknowledge_delivery(delivery_id),
        )
        delivery["acknowledged"] = True
        self._emit(
            state,
            "orca_delivery_acknowledged",
            payload={"delivery_id": delivery_id},
        )

    @staticmethod
    def _pending_message(
        state: Mapping[str, Any], message_id: str, expected_type: str
    ) -> Dict[str, Any]:
        message = state["messages"].get(message_id)
        if (
            not isinstance(message, dict)
            or message.get("type") != expected_type
            or message.get("status") != "pending"
        ):
            raise ContractViolation(
                f"Orca {expected_type} message is not pending: {message_id}"
            )
        return message

    def _settle_phase(self, state: Dict[str, Any]) -> None:
        pending = [
            item
            for item in state["messages"].values()
            if item.get("status") == "pending"
        ]
        if any(item.get("type") == "question" for item in pending):
            state["phase"] = "waiting_for_input"
        elif any(item.get("type") == "escalation" for item in pending):
            state["phase"] = "escalated"
        else:
            statuses = tuple(state["statuses"].values())
            if statuses and all(item in _TERMINAL_NODE_STATES for item in statuses):
                state["phase"] = (
                    "succeeded"
                    if all(item == "completed" for item in statuses)
                    else "failed"
                )
            else:
                state["phase"] = "running"

    def _snapshot(self, state: Mapping[str, Any]) -> OrcaCoordinatorSnapshot:
        artifacts = self._artifacts(state)
        records = tuple(self._events.read())
        pending_questions = tuple(
            sorted(
                message_id
                for message_id, item in state["messages"].items()
                if item.get("status") == "pending" and item.get("type") == "question"
            )
        )
        pending_escalations = tuple(
            sorted(
                message_id
                for message_id, item in state["messages"].items()
                if item.get("status") == "pending"
                and item.get("type") == "escalation"
            )
        )
        return OrcaCoordinatorSnapshot(
            run_id=str(state["run_id"]),
            graph_id=str(state["graph_id"]),
            phase=str(state["phase"]),
            statuses=dict(state["statuses"]),
            attempts={key: int(value) for key, value in state["attempts"].items()},
            active_dispatches=dict(state["active_dispatches"]),
            pending_questions=pending_questions,
            pending_escalations=pending_escalations,
            tokens_used=int(state["tokens_used"]),
            cost_usd=float(state["cost_usd"]),
            artifacts=artifacts.values(),
            merges={
                candidate_id: {
                    "source_node_id": item["candidate"]["source_node_id"],
                    "verifier_node_id": item["candidate"]["verifier_node_id"],
                    "target_branch": item["candidate"]["target_branch"],
                    "status": item["status"],
                    "receipt": item.get("receipt"),
                    "workspace_release": item.get("workspace_release"),
                }
                for candidate_id, item in sorted(
                    state.get("merge_candidates", {}).items()
                )
            },
            next_cursor=len(records),
        )

    def _artifacts(self, state: Mapping[str, Any]) -> ArtifactStore:
        try:
            return ArtifactStore(
                ArtifactRecord.from_dict(item) for item in state.get("artifacts", ())
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ContractViolation(
                f"invalid Orca coordinator artifacts: {error}"
            ) from error

    def _workspace_for(self, node: NodeSpec, workspace_id: Optional[str]) -> Path:
        if node.agent is None or node.agent.workspace.mode == "shared":
            return self.workspace
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ContractViolation("isolated Orca result has no workspace id")
        if "::" not in workspace_id:
            raise ContractViolation("isolated Orca workspace id must be fully qualified")
        raw_path = workspace_id.split("::", 1)[1]
        workspace = Path(raw_path)
        if not workspace.is_absolute():
            raise ContractViolation("isolated Orca workspace path must be absolute")
        if not workspace.is_dir():
            raise ContractViolation(
                f"isolated Orca result workspace does not exist: {workspace}"
            )
        return workspace.resolve()

    def _executor_id(self, node: NodeSpec) -> str:
        if node.agent is None:
            raise ContractViolation(f"Orca node {node.id} has no agent configuration")
        if node.agent.executor is not None:
            return node.agent.executor
        task_agent = self.plan.task_map()[node.id].agent
        return _EXECUTOR_IDS.get(task_agent, task_agent)

    def _effect(self, action: str, identity: str, payload: Any, effect) -> Any:
        if not isinstance(identity, str) or not identity:
            raise ContractViolation(f"Orca {action} effect identity cannot be empty")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self._effects.execute(
            f"orca-{action}-{digest[:32]}", payload, effect
        )

    def _reconcilable_effect(
        self, action: str, identity: str, payload: Any, effect, reconcile
    ) -> Any:
        if not isinstance(identity, str) or not identity:
            raise ContractViolation(f"Orca {action} effect identity cannot be empty")
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return self._effects.execute_reconcilable(
            f"orca-{action}-{digest[:32]}", payload, effect, reconcile
        )

    def _emit(
        self,
        state: Mapping[str, Any],
        event: str,
        node_id: Optional[str] = None,
        attempt: Optional[int] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self._events.emit(
            GraphEvent.create(
                event,
                str(state["run_id"]),
                self.graph.id,
                node_id,
                attempt,
                payload,
            )
        )

    def _descendants(self, node_id: str) -> Tuple[str, ...]:
        descendants = []
        frontier = [node_id]
        while frontier:
            parent = frontier.pop()
            for node in self.graph.nodes:
                if parent in node.deps and node.id not in descendants:
                    descendants.append(node.id)
                    frontier.append(node.id)
        return tuple(descendants)

    @contextmanager
    def _locked(self):
        with file_lock(self._lock_path):
            yield
