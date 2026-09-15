import json
import math
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, ContextManager, Dict, Iterator, Mapping, Optional, Tuple, Union

from . import telemetry
from .agents import AgentExecution, ModelUsage
from .artifacts import ArtifactRecord, ArtifactStore
from .checkpoint import Checkpoint, CheckpointStore
from .effects import NodeEffectJournal
from .errors import ContractViolation, RetryableNodeError
from .events import EventSink, GraphEvent, JsonlEventSink, NullEventSink
from .leases import LeaseLostError
from .model import GraphSpec, NodeSpec
from .policy import DenyNamedGatesPolicy, GateDecision, GatePolicy
from .publication import PublicationEvent, VerifiedResultPublisher
from .token_reservations import HistoricalTokenReservations, TokenReservation
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
    usage: Optional[ModelUsage] = None

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
        usage = self.usage
        if usage is None:
            if self.agent_execution is not None:
                usage = self.agent_execution.result.usage
            elif self.tokens_used == 0 and self.cost_usd is None:
                usage = ModelUsage.no_call()
            else:
                usage = ModelUsage.from_legacy_constructor(self.tokens_used, self.cost_usd)
            object.__setattr__(self, "usage", usage)
        elif not isinstance(usage, ModelUsage):
            raise ContractViolation("node usage must be ModelUsage")
        assert usage is not None
        if self.agent_execution is not None:
            if usage.total_tokens != self.tokens_used:
                raise ContractViolation("node usage total_tokens must match tokens_used")
            if usage.cost_usd != self.cost_usd:
                raise ContractViolation("node usage cost_usd must match cost_usd")
        try:
            json.dumps(self.metadata, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ContractViolation(f"node metadata must be JSON serializable: {error}") from error


class NodeContext:
    def __init__(
        self,
        run_id: str,
        node: NodeSpec,
        artifacts: ArtifactStore,
        attempt: int,
        *,
        effect_id: Optional[str] = None,
    ):
        self.run_id = run_id
        self.node_id = node.id
        self.attempt = attempt
        self.effect_id = effect_id
        self.effect_type = node.effect
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
    usage: ModelUsage = field(default_factory=ModelUsage.no_call)

    @property
    def success(self) -> bool:
        return all(status is NodeStatus.COMPLETED for status in self.statuses.values())


@dataclass
class _RunState:
    run_id: str
    statuses: Dict[str, NodeStatus]
    attempts: Dict[str, int]
    ledger: BudgetLedger
    cost_ledger: CostBudgetLedger
    artifacts: ArtifactStore
    resumed: bool
    usage: ModelUsage
    reservations: Dict[str, Tuple[TokenReservation, float]] = field(default_factory=dict)
    futures: Dict[Future[_NodeExecution], str] = field(default_factory=dict)


class GraphRuntime:
    def __init__(
        self,
        graph: GraphSpec,
        registry: NodeRegistry,
        work_dir: Optional[Path] = None,
        gate_policy: Optional[GatePolicy] = None,
        event_sink: Optional[EventSink] = None,
        verified_result_publisher: Optional[VerifiedResultPublisher] = None,
        token_reservations: Optional[HistoricalTokenReservations] = None,
        ownership_guard: Optional[Callable[[], ContextManager[None]]] = None,
        effect_lease: Optional[Mapping[str, Any]] = None,
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
        self.token_reservations = token_reservations or HistoricalTokenReservations(())
        self.ownership_guard = ownership_guard
        self.checkpoints = CheckpointStore(work_dir / "checkpoint.json") if work_dir else None
        self.effects = NodeEffectJournal(work_dir / "effects") if work_dir else None
        self.effect_lease = dict(effect_lease) if effect_lease is not None else None
        self._checkpoint_lock = threading.Lock()

    @contextmanager
    def _owned(self) -> Iterator[None]:
        if self.ownership_guard is None:
            yield
            return
        with self.ownership_guard():
            yield

    def _emit(
        self,
        event: str,
        run_id: str,
        node_id: Optional[str] = None,
        attempt: Optional[int] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        with self._owned():
            self.event_sink.emit(
                GraphEvent.create(event, run_id, self.graph.id, node_id, attempt, payload)
            )
            # The per-run sink stays the authoritative replay log; telemetry adds a
            # host-wide trace that survives when a run directory is discarded.
            telemetry.emit(
                f"graph.{event}",
                run_id=run_id,
                graph_id=self.graph.id,
                node_id=node_id,
                attempt=attempt,
                reason=(payload or {}).get("reason"),
            )

    def _save(
        self,
        run_id: str,
        statuses: Mapping[str, NodeStatus],
        attempts: Mapping[str, int],
        ledger: BudgetLedger,
        cost_ledger: CostBudgetLedger,
        artifacts: ArtifactStore,
        usage: ModelUsage,
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
            usage,
        )
        with self._owned():
            with self._checkpoint_lock:
                self.checkpoints.save(checkpoint)

    def _initial_state(self, resume: bool, requested_run_id: Optional[str] = None) -> _RunState:
        checkpoint = self.checkpoints.load() if resume and self.checkpoints else None
        if checkpoint is None:
            run_id = requested_run_id or str(uuid.uuid4())
            statuses = {node.id: NodeStatus.PENDING for node in self.graph.nodes}
            attempts = {node.id: 0 for node in self.graph.nodes}
            return _RunState(
                run_id,
                statuses,
                attempts,
                BudgetLedger(self.graph.max_tokens),
                CostBudgetLedger(self.graph.max_cost_usd),
                ArtifactStore(),
                False,
                ModelUsage.no_call(),
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
            statuses[node.id] = restored if restored is NodeStatus.COMPLETED else NodeStatus.PENDING
        attempts = {node.id: checkpoint.attempts.get(node.id, 0) for node in self.graph.nodes}
        return _RunState(
            checkpoint.run_id,
            statuses,
            attempts,
            BudgetLedger(self.graph.max_tokens, checkpoint.tokens_used),
            CostBudgetLedger(self.graph.max_cost_usd, checkpoint.cost_usd),
            ArtifactStore(checkpoint.artifacts),
            True,
            checkpoint.usage,
        )

    def _token_reservation(self, node: NodeSpec) -> TokenReservation:
        return self.token_reservations.reserve(node)

    @staticmethod
    def _cost_reservation(node: NodeSpec) -> float:
        if node.agent is not None and node.agent.max_cost_usd is not None:
            return node.agent.max_cost_usd
        return node.estimated_cost_usd

    def _critical_path_ranks(self) -> Mapping[str, int]:
        nodes = self.graph.node_map()
        remaining_children = {node.id: 0 for node in self.graph.nodes}
        for node in self.graph.nodes:
            for dependency in node.deps:
                remaining_children[dependency] += 1

        ranks = {node.id: 1 for node in self.graph.nodes}
        ready = [node.id for node in self.graph.nodes if remaining_children[node.id] == 0]
        while ready:
            node_id = ready.pop()
            for dependency in nodes[node_id].deps:
                ranks[dependency] = max(ranks[dependency], ranks[node_id] + 1)
                remaining_children[dependency] -= 1
                if remaining_children[dependency] == 0:
                    ready.append(dependency)
        return ranks

    def _execute(
        self,
        handler: NodeHandler,
        run_id: str,
        node: NodeSpec,
        artifacts: ArtifactStore,
        attempt: int,
    ) -> _NodeExecution:
        effect_id = (
            NodeEffectJournal.effect_id(run_id, node.id) if node.effect != "read_only" else None
        )
        context = NodeContext(
            run_id,
            node,
            artifacts,
            attempt,
            effect_id=effect_id,
        )

        def invoke_handler() -> NodeOutcome:
            return self._normalize_outcome(node, handler(context))

        if node.effect == "read_only":
            outcome = invoke_handler()
        else:
            if self.effects is None or self.effect_lease is None:
                raise ContractViolation(
                    f"node {node.id} write effect requires work_dir and effect_lease"
                )
            effect_lease = self._validated_effect_lease(run_id, node.id)
            workspace_identity = getattr(handler, "workspace_identity", None)
            if not isinstance(workspace_identity, Mapping):
                raise ContractViolation(
                    f"node {node.id} write effect handler requires workspace_identity"
                )
            assert effect_id is not None

            def invoke_effect() -> Tuple[NodeOutcome, Mapping[str, Any]]:
                with self._owned():
                    value = invoke_handler()
                return value, self._persist_outcome(value)

            reconcile_handler = getattr(handler, "reconcile_effect", None)

            def reconcile_effect(
                receipt: Mapping[str, Any],
            ) -> Optional[Tuple[NodeOutcome, Mapping[str, Any]]]:
                if not callable(reconcile_handler):
                    return None
                with self._owned():
                    reconciled = reconcile_handler(context, receipt)
                if reconciled is None:
                    return None
                value = self._normalize_outcome(node, reconciled)
                return value, self._persist_outcome(value)

            outcome = self.effects.execute(
                effect_id=effect_id,
                run_id=run_id,
                node_id=node.id,
                effect_type=node.effect,
                inputs=[record.to_dict() for record in context.input_records()],
                lease=effect_lease,
                workspace_identity=workspace_identity,
                invoke=invoke_effect,
                restore=self._restore_outcome,
                reconcile=(reconcile_effect if node.effect == "reconcilable" else None),
            )
        return _NodeExecution(outcome, context.input_records())

    def _validated_effect_lease(self, run_id: str, node_id: str) -> Mapping[str, Any]:
        lease = self.effect_lease
        if lease is None or set(lease) != {
            "run_id",
            "owner_id",
            "generation",
            "token_digest",
        }:
            raise ContractViolation(f"node {node_id} effect lease has an invalid contract")
        generation = lease["generation"]
        digest = lease["token_digest"]
        if (
            lease["run_id"] != run_id
            or not isinstance(lease["owner_id"], str)
            or not lease["owner_id"]
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ContractViolation(f"node {node_id} effect lease does not match this run")
        return lease

    @staticmethod
    def _normalize_outcome(
        node: NodeSpec,
        result: Union[NodeOutcome, Mapping[str, Any]],
    ) -> NodeOutcome:
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
        return outcome

    @staticmethod
    def _persist_outcome(outcome: NodeOutcome) -> Mapping[str, Any]:
        return {
            "outputs": dict(outcome.outputs),
            "tokens_used": outcome.tokens_used,
            "cost_usd": outcome.cost_usd,
            "metadata": dict(outcome.metadata),
            "usage": (outcome.usage or ModelUsage.no_call()).to_dict(),
        }

    @staticmethod
    def _restore_outcome(value: Mapping[str, Any]) -> NodeOutcome:
        if set(value) != {
            "outputs",
            "tokens_used",
            "cost_usd",
            "metadata",
            "usage",
        }:
            raise ContractViolation("persisted node effect outcome has an invalid contract")
        outputs = value["outputs"]
        metadata = value["metadata"]
        if not isinstance(outputs, Mapping) or not isinstance(metadata, Mapping):
            raise ContractViolation("persisted node effect outcome fields are invalid")
        tokens_used = value["tokens_used"]
        cost_usd = value["cost_usd"]
        return NodeOutcome(
            dict(outputs),
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            metadata=dict(metadata),
            usage=ModelUsage.from_persisted(
                value["usage"],
                tokens_used=tokens_used,
                cost_usd=cost_usd,
            ),
        )

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
            with self._owned():
                staged = self.verified_result_publisher.stage(
                    self.graph,
                    run_id,
                    node,
                    attempt,
                    execution.outcome.agent_execution,
                    execution.input_records,
                    records,
                )
            self._emit_publication(
                run_id,
                staged,
            )
            with self._owned():
                observed = self.verified_result_publisher.observe_verifier(
                    self.graph,
                    artifacts,
                    run_id,
                    node,
                    attempt,
                    execution.input_records,
                    records,
                )
            self._emit_publication(
                run_id,
                observed,
            )
        except LeaseLostError:
            raise
        except Exception as error:
            self._emit(
                "verified_result_publish_deferred",
                run_id,
                node.id,
                attempt,
                {"reason": f"{type(error).__name__}: {error}"},
            )

    def _save_state(self, state: _RunState) -> None:
        self._save(
            state.run_id,
            state.statuses,
            state.attempts,
            state.ledger,
            state.cost_ledger,
            state.artifacts,
            state.usage,
        )

    def _reconcile_publications(self, state: _RunState) -> None:
        if self.verified_result_publisher is None:
            return
        try:
            for event in self.verified_result_publisher.reconcile(
                self.graph, state.artifacts, state.run_id
            ):
                self._emit_publication(state.run_id, event)
        except Exception as error:
            self._emit(
                "verified_result_publish_deferred",
                state.run_id,
                payload={"reason": f"{type(error).__name__}: {error}"},
            )

    def _transition_pending_nodes(
        self,
        state: _RunState,
        cancellation: Optional[CancellationToken],
    ) -> bool:
        changed = False
        if cancellation is not None and cancellation.cancelled:
            for node in self.graph.nodes:
                if state.statuses[node.id] is NodeStatus.PENDING:
                    state.statuses[node.id] = NodeStatus.CANCELLED
                    self._emit(
                        "node_cancelled",
                        state.run_id,
                        node.id,
                        payload={"reason": "run_cancelled"},
                    )
                    changed = True
        for node in self.graph.nodes:
            if state.statuses[node.id] is not NodeStatus.PENDING:
                continue
            dependency_states = [state.statuses[dep] for dep in node.deps]
            if any(
                dependency_state in (NodeStatus.FAILED, NodeStatus.BLOCKED)
                for dependency_state in dependency_states
            ):
                state.statuses[node.id] = NodeStatus.BLOCKED
                self._emit(
                    "node_blocked",
                    state.run_id,
                    node.id,
                    payload={"reason": "dependency"},
                )
                changed = True
        return changed

    def _ready_nodes(
        self,
        state: _RunState,
        critical_path_ranks: Mapping[str, int],
        declaration_order: Mapping[str, int],
    ) -> Tuple[NodeSpec, ...]:
        return tuple(
            sorted(
                (
                    node
                    for node in self.graph.nodes
                    if state.statuses[node.id] is NodeStatus.PENDING
                    and all(
                        state.statuses[dependency] is NodeStatus.COMPLETED
                        for dependency in node.deps
                    )
                ),
                key=lambda node: (
                    -critical_path_ranks[node.id],
                    declaration_order[node.id],
                ),
            )
        )

    def _schedule_ready_nodes(
        self,
        state: _RunState,
        executor: ThreadPoolExecutor,
        handlers: Mapping[str, NodeHandler],
        critical_path_ranks: Mapping[str, int],
        declaration_order: Mapping[str, int],
    ) -> bool:
        changed = False
        for node in self._ready_nodes(state, critical_path_ranks, declaration_order):
            if len(state.futures) >= self.graph.max_concurrency:
                break
            if self.gate_policy.decide(node, state.artifacts.values()) is GateDecision.DENY:
                state.statuses[node.id] = NodeStatus.BLOCKED
                self._emit(
                    "node_blocked",
                    state.run_id,
                    node.id,
                    payload={"reason": "gate", "gate": node.gate},
                )
                changed = True
                continue
            token_reservation = self._token_reservation(node)
            if not state.ledger.can_reserve(token_reservation.tokens):
                if state.futures:
                    continue
                state.statuses[node.id] = NodeStatus.BLOCKED
                self._emit(
                    "node_blocked",
                    state.run_id,
                    node.id,
                    payload={"reason": "token_budget"},
                )
                changed = True
                continue
            cost_reservation = self._cost_reservation(node)
            if not state.cost_ledger.can_reserve(cost_reservation):
                if state.futures:
                    continue
                state.statuses[node.id] = NodeStatus.BLOCKED
                self._emit(
                    "node_blocked",
                    state.run_id,
                    node.id,
                    payload={"reason": "cost_budget"},
                )
                changed = True
                continue
            state.attempts[node.id] += 1
            state.ledger.reserve(token_reservation.tokens)
            state.cost_ledger.reserve(cost_reservation)
            state.reservations[node.id] = (token_reservation, cost_reservation)
            state.statuses[node.id] = NodeStatus.RUNNING
            with self._owned():
                future = executor.submit(
                    self._execute,
                    handlers[node.id],
                    state.run_id,
                    node,
                    state.artifacts,
                    state.attempts[node.id],
                )
            state.futures[future] = node.id
            self._emit(
                "node_started",
                state.run_id,
                node.id,
                state.attempts[node.id],
                {
                    "token_reservation": token_reservation.tokens,
                    "token_reservation_source": token_reservation.source,
                    "token_reservation_samples": token_reservation.samples,
                },
            )
            changed = True
        return changed

    @staticmethod
    def _all_terminal(statuses: Mapping[str, NodeStatus]) -> bool:
        return all(
            status
            in (
                NodeStatus.COMPLETED,
                NodeStatus.FAILED,
                NodeStatus.BLOCKED,
                NodeStatus.CANCELLED,
            )
            for status in statuses.values()
        )

    def _record_failed_execution_usage(
        self,
        state: _RunState,
        reserved_tokens: int,
        reserved_cost: float,
    ) -> None:
        state.ledger.settle(reserved_tokens, 0)
        state.cost_ledger.settle(reserved_cost, None)
        state.usage = ModelUsage.combine(
            (state.usage, ModelUsage(total_tokens=0, cost_usd=reserved_cost))
        )

    def _complete_node_execution(
        self,
        state: _RunState,
        node: NodeSpec,
        execution: _NodeExecution,
        token_reservation: TokenReservation,
        reserved_cost: float,
    ) -> None:
        node_id = node.id
        outcome = execution.outcome
        outcome_usage = outcome.usage or ModelUsage.no_call()
        accounted_usage = outcome_usage
        within_graph_budget = state.ledger.settle(token_reservation.tokens, outcome.tokens_used)
        within_cost_budget = state.cost_ledger.settle(reserved_cost, outcome.cost_usd)
        if outcome_usage.total_tokens_complete and outcome_usage.total_tokens is not None:
            warning = self.token_reservations.deviation_warning(
                node, token_reservation, outcome_usage.total_tokens
            )
            if warning is not None:
                self._emit(
                    "token_reservation_warning",
                    state.run_id,
                    node_id,
                    state.attempts[node_id],
                    {
                        "code": warning.code,
                        "reserved_tokens": warning.reserved_tokens,
                        "actual_tokens": warning.actual_tokens,
                        "consecutive_deviations": warning.consecutive_deviations,
                        "action": warning.action,
                    },
                )
        if not (outcome.agent_execution is None and outcome_usage == ModelUsage.no_call()):
            accounted_cost = reserved_cost if outcome.cost_usd is None else outcome.cost_usd
            accounted_usage = outcome_usage.with_accounted_totals(
                outcome.tokens_used, accounted_cost
            )
            state.usage = ModelUsage.combine((state.usage, accounted_usage))
        within_node_budget = node.max_tokens is None or outcome.tokens_used <= node.max_tokens
        within_node_cost = (
            node.agent is None
            or node.agent.max_cost_usd is None
            or outcome.cost_usd is None
            or outcome.cost_usd <= node.agent.max_cost_usd
        )
        if not (
            within_graph_budget and within_cost_budget and within_node_budget and within_node_cost
        ):
            state.statuses[node_id] = NodeStatus.FAILED
            self._emit(
                "node_failed",
                state.run_id,
                node_id,
                state.attempts[node_id],
                {"reason": "budget"},
            )
            return
        records = state.artifacts.commit_batch(outcome.outputs, node_id)
        state.statuses[node_id] = NodeStatus.COMPLETED
        self._emit(
            "node_completed",
            state.run_id,
            node_id,
            state.attempts[node_id],
            {
                "tokens_used": outcome.tokens_used,
                "cost_usd": outcome.cost_usd,
                "usage": accounted_usage.to_dict(),
                "artifacts": [record.key for record in records],
                "metadata": dict(outcome.metadata),
            },
        )
        self._record_publication(
            state.run_id,
            node,
            state.attempts[node_id],
            execution,
            records,
            state.artifacts,
        )

    def _settle_future(
        self,
        state: _RunState,
        future: Future[_NodeExecution],
        nodes: Mapping[str, NodeSpec],
    ) -> None:
        node_id = state.futures.pop(future)
        node = nodes[node_id]
        token_reservation, reserved_cost = state.reservations.pop(node_id)
        try:
            self._complete_node_execution(
                state,
                node,
                future.result(),
                token_reservation,
                reserved_cost,
            )
        except LeaseLostError:
            raise
        except RetryableNodeError as error:
            self._record_failed_execution_usage(state, token_reservation.tokens, reserved_cost)
            if state.attempts[node_id] < node.retry.max_attempts:
                state.statuses[node_id] = NodeStatus.PENDING
                self._emit(
                    "node_retry",
                    state.run_id,
                    node_id,
                    state.attempts[node_id],
                    {"error": str(error)},
                )
            else:
                state.statuses[node_id] = NodeStatus.FAILED
                self._emit(
                    "node_failed",
                    state.run_id,
                    node_id,
                    state.attempts[node_id],
                    {"error": str(error)},
                )
        except Exception as error:
            self._record_failed_execution_usage(state, token_reservation.tokens, reserved_cost)
            state.statuses[node_id] = NodeStatus.FAILED
            self._emit(
                "node_failed",
                state.run_id,
                node_id,
                state.attempts[node_id],
                {"error": f"{type(error).__name__}: {error}"},
            )
        finally:
            self._save_state(state)

    def _finish_run(self, state: _RunState) -> RunResult:
        result = RunResult(
            state.run_id,
            self.graph.id,
            dict(state.statuses),
            dict(state.attempts),
            state.ledger.used,
            state.cost_ledger.used,
            state.artifacts.values(),
            state.usage,
        )
        self._emit(
            "run_completed",
            state.run_id,
            payload={
                "success": result.success,
                "cancelled": any(
                    status is NodeStatus.CANCELLED for status in result.statuses.values()
                ),
                "tokens_used": state.ledger.used,
                "cost_usd": state.cost_ledger.used,
                "usage": state.usage.to_dict(),
            },
        )
        self._save_state(state)
        return result

    def run(
        self,
        resume: bool = False,
        run_id: Optional[str] = None,
        cancellation: Optional[CancellationToken] = None,
    ) -> RunResult:
        handlers = {node.id: self.registry.resolve(node.kind) for node in self.graph.nodes}
        state = self._initial_state(resume, run_id)
        nodes = self.graph.node_map()
        self._emit("run_resumed" if state.resumed else "run_started", state.run_id)
        self._reconcile_publications(state)
        critical_path_ranks = self._critical_path_ranks()
        declaration_order = {node.id: index for index, node in enumerate(self.graph.nodes)}

        with ThreadPoolExecutor(max_workers=self.graph.max_concurrency) as executor:
            while True:
                changed = self._transition_pending_nodes(state, cancellation)
                changed = (
                    self._schedule_ready_nodes(
                        state,
                        executor,
                        handlers,
                        critical_path_ranks,
                        declaration_order,
                    )
                    or changed
                )
                if changed:
                    self._save_state(state)
                if not state.futures:
                    if self._all_terminal(state.statuses):
                        break
                    if changed:
                        # A gate, budget decision, or failed dependency can make
                        # more descendants blockable on the next fixed-point pass.
                        continue
                    raise ContractViolation("scheduler reached a non-terminal deadlock")

                done, _ = wait(tuple(state.futures), return_when=FIRST_COMPLETED)
                for future in done:
                    self._settle_future(state, future, nodes)

        return self._finish_run(state)
