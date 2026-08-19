import json
import math
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple, Union

from .agents import AgentExecution
from .artifacts import ArtifactRecord, ArtifactStore
from .checkpoint import Checkpoint, CheckpointStore
from .errors import ContractViolation, RetryableNodeError
from .events import EventSink, GraphEvent, JsonlEventSink, NullEventSink
from .model import GraphSpec, NodeSpec
from .policy import DenyNamedGatesPolicy, GateDecision, GatePolicy
from .publication import PublicationEvent, VerifiedResultPublisher
from .validation import validate_graph


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class NodeOutcome:
    outputs: Mapping[str, Any]
    tokens_used: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    cost_usd: Optional[float] = None
    agent_execution: Optional[AgentExecution] = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.tokens_used, bool)
            or not isinstance(self.tokens_used, int)
            or self.tokens_used < 0
        ):
            raise ContractViolation("tokens_used must be a non-negative integer")
        if self.cost_usd is not None and (
            isinstance(self.cost_usd, bool)
            or not isinstance(self.cost_usd, (int, float))
            or not math.isfinite(self.cost_usd)
            or self.cost_usd < 0
        ):
            raise ContractViolation("cost_usd must be a finite non-negative number")
        try:
            json.dumps(self.metadata, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ContractViolation(f"node metadata must be JSON serializable: {error}") from error


class NodeContext:
    def __init__(self, run_id: str, node: NodeSpec, artifacts: ArtifactStore, attempt: int):
        self.run_id = run_id
        self.node_id = node.id
        self.attempt = attempt
        self._allowed_reads = frozenset(node.reads)
        self._input_records = artifacts.snapshot(self._allowed_reads)
        self._inputs = {record.key: record.value for record in self._input_records}

    def read(self, key: str) -> Any:
        if key not in self._allowed_reads:
            raise ContractViolation(f"node {self.node_id} did not declare read access to {key}")
        return json.loads(json.dumps(self._inputs[key], ensure_ascii=False))

    def inputs(self) -> Mapping[str, Any]:
        return {key: self.read(key) for key in sorted(self._allowed_reads)}

    def input_records(self) -> Tuple[ArtifactRecord, ...]:
        return self._input_records


NodeHandler = Callable[[NodeContext], Union[NodeOutcome, Mapping[str, Any]]]


@dataclass(frozen=True)
class _NodeExecution:
    outcome: NodeOutcome
    input_records: Tuple[ArtifactRecord, ...]


class NodeRegistry:
    def __init__(self):
        self._handlers = {}

    def register(self, kind: str, handler: NodeHandler) -> None:
        if kind in self._handlers:
            raise ContractViolation(f"node kind {kind} is already registered")
        self._handlers[kind] = handler

    def resolve(self, kind: str) -> NodeHandler:
        try:
            return self._handlers[kind]
        except KeyError as error:
            raise ContractViolation(f"node kind {kind} is not registered") from error


class BudgetLedger:
    def __init__(self, maximum: Optional[int], used: int = 0):
        self.maximum = maximum
        self.used = used
        self.reserved = 0

    def can_reserve(self, amount: int) -> bool:
        return self.maximum is None or self.used + self.reserved + amount <= self.maximum

    def reserve(self, amount: int) -> None:
        if not self.can_reserve(amount):
            raise ContractViolation("graph token budget admission denied")
        self.reserved += amount

    def settle(self, reserved: int, actual: int) -> bool:
        self.reserved -= reserved
        self.used += actual
        return self.maximum is None or self.used <= self.maximum


class CostBudgetLedger:
    def __init__(self, maximum: Optional[float], used: float = 0.0):
        self.maximum = maximum
        self.used = used
        self.reserved = 0.0

    def can_reserve(self, amount: float) -> bool:
        return self.maximum is None or self.used + self.reserved + amount <= self.maximum

    def reserve(self, amount: float) -> None:
        if not self.can_reserve(amount):
            raise ContractViolation("graph dollar budget admission denied")
        self.reserved += amount

    def settle(self, reserved: float, actual: Optional[float]) -> bool:
        self.reserved -= reserved
        self.used += reserved if actual is None else actual
        return self.maximum is None or self.used <= self.maximum


@dataclass(frozen=True)
class RunResult:
    run_id: str
    graph_id: str
    statuses: Mapping[str, NodeStatus]
    attempts: Mapping[str, int]
    tokens_used: int
    cost_usd: float
    artifacts: Mapping[str, Any]

    @property
    def success(self) -> bool:
        return all(status is NodeStatus.COMPLETED for status in self.statuses.values())


class GraphRuntime:
    def __init__(
        self,
        graph: GraphSpec,
        registry: NodeRegistry,
        work_dir: Optional[Path] = None,
        gate_policy: Optional[GatePolicy] = None,
        event_sink: Optional[EventSink] = None,
        verified_result_publisher: Optional[VerifiedResultPublisher] = None,
    ):
        validate_graph(graph)
        merge_nodes = [node.id for node in graph.nodes if node.controlled_merge is not None]
        if merge_nodes:
            raise ContractViolation(
                "controlled merge requires OrcaCoordinator: " + ", ".join(merge_nodes)
            )
        self.graph = graph
        self.registry = registry
        self.work_dir = work_dir
        self.gate_policy = gate_policy or DenyNamedGatesPolicy()
        if event_sink is not None:
            self.event_sink = event_sink
        elif work_dir is not None:
            self.event_sink = JsonlEventSink(work_dir / "events.jsonl")
        else:
            self.event_sink = NullEventSink()
        self.verified_result_publisher = verified_result_publisher
        self.checkpoints = CheckpointStore(work_dir / "checkpoint.json") if work_dir else None
        self._checkpoint_lock = threading.Lock()

    def _emit(
        self,
        event: str,
        run_id: str,
        node_id: Optional[str] = None,
        attempt: Optional[int] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.event_sink.emit(
            GraphEvent.create(event, run_id, self.graph.id, node_id, attempt, payload)
        )

    def _save(
        self,
        run_id: str,
        statuses: Mapping[str, NodeStatus],
        attempts: Mapping[str, int],
        ledger: BudgetLedger,
        cost_ledger: CostBudgetLedger,
        artifacts: ArtifactStore,
    ) -> None:
        if self.checkpoints is None:
            return
        checkpoint = Checkpoint(
            self.graph.id,
            self.graph.fingerprint(),
            run_id,
            {node_id: status.value for node_id, status in statuses.items()},
            dict(attempts),
            ledger.used,
            cost_ledger.used,
            artifacts.records(),
        )
        with self._checkpoint_lock:
            self.checkpoints.save(checkpoint)

    def _initial_state(self, resume: bool, requested_run_id: Optional[str] = None):
        checkpoint = self.checkpoints.load() if resume and self.checkpoints else None
        if checkpoint is None:
            run_id = requested_run_id or str(uuid.uuid4())
            statuses = {node.id: NodeStatus.PENDING for node in self.graph.nodes}
            attempts = {node.id: 0 for node in self.graph.nodes}
            return (
                run_id,
                statuses,
                attempts,
                BudgetLedger(self.graph.max_tokens),
                CostBudgetLedger(self.graph.max_cost_usd),
                ArtifactStore(),
                False,
            )
        if checkpoint.graph_id != self.graph.id:
            raise ContractViolation("checkpoint graph_id does not match GraphSpec")
        if checkpoint.graph_fingerprint != self.graph.fingerprint():
            raise ContractViolation("checkpoint graph fingerprint does not match GraphSpec")
        if requested_run_id is not None and checkpoint.run_id != requested_run_id:
            raise ContractViolation("checkpoint run_id does not match requested run")
        statuses = {}
        for node in self.graph.nodes:
            restored = NodeStatus(checkpoint.statuses.get(node.id, NodeStatus.PENDING.value))
            statuses[node.id] = (
                restored if restored is NodeStatus.COMPLETED else NodeStatus.PENDING
            )
        attempts = {node.id: checkpoint.attempts.get(node.id, 0) for node in self.graph.nodes}
        return (
            checkpoint.run_id,
            statuses,
            attempts,
            BudgetLedger(self.graph.max_tokens, checkpoint.tokens_used),
            CostBudgetLedger(self.graph.max_cost_usd, checkpoint.cost_usd),
            ArtifactStore(checkpoint.artifacts),
            True,
        )

    @staticmethod
    def _token_reservation(node: NodeSpec) -> int:
        if node.agent is not None and node.max_tokens is not None:
            return node.max_tokens
        return node.estimated_tokens

    @staticmethod
    def _cost_reservation(node: NodeSpec) -> float:
        if node.agent is not None and node.agent.max_cost_usd is not None:
            return node.agent.max_cost_usd
        return node.estimated_cost_usd

    @staticmethod
    def _execute(
        handler: NodeHandler,
        run_id: str,
        node: NodeSpec,
        artifacts: ArtifactStore,
        attempt: int,
    ) -> _NodeExecution:
        context = NodeContext(run_id, node, artifacts, attempt)
        result = handler(context)
        if isinstance(result, NodeOutcome):
            outcome = result
        elif isinstance(result, Mapping):
            outcome = NodeOutcome(result)
        else:
            raise ContractViolation(f"node {node.id} returned an unsupported result")
        expected = set(node.writes)
        actual = set(outcome.outputs)
        if actual != expected:
            raise ContractViolation(
                f"node {node.id} output contract mismatch; expected {sorted(expected)}, got {sorted(actual)}"
            )
        return _NodeExecution(outcome, context.input_records())

    def _emit_publication(self, run_id: str, event: Optional[PublicationEvent]) -> None:
        if event is None:
            return
        self._emit(
            event.event,
            run_id,
            event.node_id,
            event.attempt,
            event.payload,
        )

    def _record_publication(
        self,
        run_id: str,
        node: NodeSpec,
        attempt: int,
        execution: _NodeExecution,
        records: Tuple[ArtifactRecord, ...],
        artifacts: ArtifactStore,
    ) -> None:
        if self.verified_result_publisher is None:
            return
        try:
            self._emit_publication(
                run_id,
                self.verified_result_publisher.stage(
                    self.graph,
                    run_id,
                    node,
                    attempt,
                    execution.outcome.agent_execution,
                    execution.input_records,
                    records,
                ),
            )
            self._emit_publication(
                run_id,
                self.verified_result_publisher.observe_verifier(
                    self.graph,
                    artifacts,
                    run_id,
                    node,
                    attempt,
                    execution.input_records,
                    records,
                ),
            )
        except Exception as error:
            self._emit(
                "verified_result_publish_deferred",
                run_id,
                node.id,
                attempt,
                {"reason": f"{type(error).__name__}: {error}"},
            )

    def run(
        self,
        resume: bool = False,
        run_id: Optional[str] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> RunResult:
        handlers = {node.id: self.registry.resolve(node.kind) for node in self.graph.nodes}
        run_id, statuses, attempts, ledger, cost_ledger, artifacts, resumed = self._initial_state(
            resume, run_id
        )
        nodes = self.graph.node_map()
        self._emit("run_resumed" if resumed else "run_started", run_id)
        if self.verified_result_publisher is not None:
            try:
                for event in self.verified_result_publisher.reconcile(
                    self.graph, artifacts, run_id
                ):
                    self._emit_publication(run_id, event)
            except Exception as error:
                self._emit(
                    "verified_result_publish_deferred",
                    run_id,
                    payload={"reason": f"{type(error).__name__}: {error}"},
                )
        reservations = {}
        futures: Dict[Future, str] = {}

        with ThreadPoolExecutor(max_workers=self.graph.max_concurrency) as executor:
            while True:
                changed = False
                if cancellation is not None and cancellation.cancelled:
                    for node in self.graph.nodes:
                        if statuses[node.id] is NodeStatus.PENDING:
                            statuses[node.id] = NodeStatus.CANCELLED
                            self._emit(
                                "node_cancelled",
                                run_id,
                                node.id,
                                payload={"reason": "run_cancelled"},
                            )
                            changed = True
                for node in self.graph.nodes:
                    if statuses[node.id] is not NodeStatus.PENDING:
                        continue
                    dependency_states = [statuses[dep] for dep in node.deps]
                    if any(state in (NodeStatus.FAILED, NodeStatus.BLOCKED) for state in dependency_states):
                        statuses[node.id] = NodeStatus.BLOCKED
                        self._emit("node_blocked", run_id, node.id, payload={"reason": "dependency"})
                        changed = True

                for node in self.graph.nodes:
                    if statuses[node.id] is not NodeStatus.PENDING:
                        continue
                    if not all(statuses[dep] is NodeStatus.COMPLETED for dep in node.deps):
                        continue
                    if self.gate_policy.decide(node, artifacts.values()) is GateDecision.DENY:
                        statuses[node.id] = NodeStatus.BLOCKED
                        self._emit("node_blocked", run_id, node.id, payload={"reason": "gate", "gate": node.gate})
                        changed = True
                        continue
                    token_reservation = self._token_reservation(node)
                    if not ledger.can_reserve(token_reservation):
                        statuses[node.id] = NodeStatus.BLOCKED
                        self._emit("node_blocked", run_id, node.id, payload={"reason": "token_budget"})
                        changed = True
                        continue
                    cost_reservation = self._cost_reservation(node)
                    if not cost_ledger.can_reserve(cost_reservation):
                        statuses[node.id] = NodeStatus.BLOCKED
                        self._emit("node_blocked", run_id, node.id, payload={"reason": "cost_budget"})
                        changed = True
                        continue
                    if len(futures) >= self.graph.max_concurrency:
                        break
                    attempts[node.id] += 1
                    ledger.reserve(token_reservation)
                    cost_ledger.reserve(cost_reservation)
                    reservations[node.id] = (token_reservation, cost_reservation)
                    statuses[node.id] = NodeStatus.RUNNING
                    handler = handlers[node.id]
                    future = executor.submit(
                        self._execute,
                        handler,
                        run_id,
                        node,
                        artifacts,
                        attempts[node.id],
                    )
                    futures[future] = node.id
                    self._emit("node_started", run_id, node.id, attempts[node.id])
                    changed = True

                if changed:
                    self._save(run_id, statuses, attempts, ledger, cost_ledger, artifacts)
                if not futures:
                    if all(
                        status
                        in (
                            NodeStatus.COMPLETED,
                            NodeStatus.FAILED,
                            NodeStatus.BLOCKED,
                            NodeStatus.CANCELLED,
                        )
                        for status in statuses.values()
                    ):
                        break
                    if changed:
                        # A gate, budget decision, or failed dependency can make
                        # more descendants blockable on the next fixed-point pass.
                        continue
                    raise ContractViolation("scheduler reached a non-terminal deadlock")

                done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
                for future in done:
                    node_id = futures.pop(future)
                    node = nodes[node_id]
                    reserved_tokens, reserved_cost = reservations.pop(node_id)
                    try:
                        execution = future.result()
                        outcome = execution.outcome
                        within_graph_budget = ledger.settle(reserved_tokens, outcome.tokens_used)
                        within_cost_budget = cost_ledger.settle(reserved_cost, outcome.cost_usd)
                        within_node_budget = node.max_tokens is None or outcome.tokens_used <= node.max_tokens
                        within_node_cost = (
                            node.agent is None
                            or node.agent.max_cost_usd is None
                            or outcome.cost_usd is None
                            or outcome.cost_usd <= node.agent.max_cost_usd
                        )
                        if (
                            not within_graph_budget
                            or not within_cost_budget
                            or not within_node_budget
                            or not within_node_cost
                        ):
                            statuses[node_id] = NodeStatus.FAILED
                            self._emit("node_failed", run_id, node_id, attempts[node_id], {"reason": "budget"})
                            continue
                        records = artifacts.commit_batch(outcome.outputs, node_id)
                        statuses[node_id] = NodeStatus.COMPLETED
                        self._emit(
                            "node_completed",
                            run_id,
                            node_id,
                            attempts[node_id],
                            {
                                "tokens_used": outcome.tokens_used,
                                "cost_usd": outcome.cost_usd,
                                "artifacts": [record.key for record in records],
                                "metadata": dict(outcome.metadata),
                            },
                        )
                        self._record_publication(
                            run_id,
                            node,
                            attempts[node_id],
                            execution,
                            records,
                            artifacts,
                        )
                    except RetryableNodeError as error:
                        ledger.settle(reserved_tokens, 0)
                        cost_ledger.settle(reserved_cost, None)
                        if attempts[node_id] < node.retry.max_attempts:
                            statuses[node_id] = NodeStatus.PENDING
                            self._emit("node_retry", run_id, node_id, attempts[node_id], {"error": str(error)})
                        else:
                            statuses[node_id] = NodeStatus.FAILED
                            self._emit("node_failed", run_id, node_id, attempts[node_id], {"error": str(error)})
                    except Exception as error:
                        ledger.settle(reserved_tokens, 0)
                        cost_ledger.settle(reserved_cost, None)
                        statuses[node_id] = NodeStatus.FAILED
                        self._emit(
                            "node_failed",
                            run_id,
                            node_id,
                            attempts[node_id],
                            {"error": f"{type(error).__name__}: {error}"},
                        )
                    finally:
                        self._save(run_id, statuses, attempts, ledger, cost_ledger, artifacts)

        result = RunResult(
            run_id,
            self.graph.id,
            dict(statuses),
            dict(attempts),
            ledger.used,
            cost_ledger.used,
            artifacts.values(),
        )
        self._emit(
            "run_completed",
            run_id,
            payload={
                "success": result.success,
                "cancelled": any(
                    status is NodeStatus.CANCELLED for status in result.statuses.values()
                ),
                "tokens_used": ledger.used,
                "cost_usd": cost_ledger.used,
            },
        )
        self._save(run_id, statuses, attempts, ledger, cost_ledger, artifacts)
        return result
