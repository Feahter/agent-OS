import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple, TypeVar, cast

from ._store import atomic_json_write, file_lock
from .errors import ContractViolation, EffectIndeterminateError
from .events import JsonlEventSink
from .leases import (
    LeaseLostError,
    LeaseToken,
    assert_lease,
    claim_lease,
    renew_lease,
)
from .model import GraphSpec
from .policy import GatePolicy
from .publication import VerifiedResultPublisher
from .reuse import VerifiedArtifactCache
from .runtime import CancellationToken, GraphRuntime, NodeRegistry, NodeStatus, RunResult
from .schemas import (
    CONTROL_RUN_SCHEMA_VERSION,
    EFFECT_RECEIPT_SCHEMA_VERSION,
    EffectReceiptDocumentV1,
)
from .state_machine import TERMINAL_PHASES
from .token_reservations import HistoricalTokenReservations
from .validation import validate_graph

RUN_ID = re.compile(r"^[A-Za-z0-9-]{1,128}$")
_MutationResult = TypeVar("_MutationResult")


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    graph_id: str
    phase: str
    owner_id: Optional[str]
    lease_expires_at: Optional[float]
    heartbeat_at: Optional[float]
    cancel_requested: bool
    generation: int
    error: Optional[str]
    result: Optional[Mapping[str, Any]]


@dataclass(frozen=True)
class EventPage:
    events: Tuple[Mapping[str, Any], ...]
    next_cursor: int


@dataclass(frozen=True)
class EffectReceipt:
    key: str
    payload_digest: str
    status: str
    result: Any = None
    error: Optional[str] = None
    recovery_context: Any = None
    recovery_history: Tuple[Mapping[str, Any], ...] = ()
    schema_version: int = EFFECT_RECEIPT_SCHEMA_VERSION


class EffectJournal:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def execute(
        self,
        key: str,
        payload: Any,
        effect: Callable[[], Any],
        postcondition: Optional[Callable[[Any], None]] = None,
        *,
        recovery_context: Any = None,
    ) -> Any:
        path = self._path(key)
        digest = self._digest(payload)
        normalized_context = self._normalize_recovery_context(recovery_context)
        recovery_history: Tuple[Mapping[str, Any], ...] = ()
        with self._lock:
            existing = self._read(path)
            if existing is not None:
                if existing.payload_digest != digest:
                    raise ContractViolation(
                        f"effect key {key} was reused with different input"
                    )
                if existing.status == "completed":
                    return existing.result
                if existing.status != "reset":
                    raise EffectIndeterminateError(
                        f"effect {key} is {existing.status}; reconcile it before retrying"
                    )
                recovery_history = existing.recovery_history
            self._write(
                path,
                EffectReceipt(
                    key,
                    digest,
                    "started",
                    recovery_context=normalized_context,
                    recovery_history=recovery_history,
                ),
            )

        try:
            result = effect()
            json.dumps(result, ensure_ascii=False, sort_keys=True)
            if postcondition is not None:
                postcondition(result)
        except Exception as error:
            with self._lock:
                self._write(
                    path,
                    EffectReceipt(
                        key,
                        digest,
                        "indeterminate",
                        error=f"{type(error).__name__}: {error}",
                        recovery_context=normalized_context,
                        recovery_history=recovery_history,
                    ),
                )
            raise

        with self._lock:
            self._write(
                path,
                EffectReceipt(
                    key,
                    digest,
                    "completed",
                    result=result,
                    recovery_context=normalized_context,
                    recovery_history=recovery_history,
                ),
            )
        return result

    def execute_reconcilable(
        self,
        key: str,
        payload: Any,
        effect: Callable[[], Any],
        reconcile: Callable[[], Optional[Any]],
        *,
        recovery_context: Any = None,
    ) -> Any:
        path = self._path(key)
        digest = self._digest(payload)
        normalized_context = self._normalize_recovery_context(recovery_context)
        recovering = False
        recovery_history: Tuple[Mapping[str, Any], ...] = ()
        with self._lock:
            existing = self._read(path)
            if existing is not None:
                if existing.payload_digest != digest:
                    raise ContractViolation(
                        f"effect key {key} was reused with different input"
                    )
                if existing.status == "completed":
                    return existing.result
                if existing.status == "reset":
                    recovery_history = existing.recovery_history
                    self._write(
                        path,
                        EffectReceipt(
                            key,
                            digest,
                            "started",
                            recovery_context=normalized_context,
                            recovery_history=recovery_history,
                        ),
                    )
                else:
                    recovering = True
            else:
                self._write(
                    path,
                    EffectReceipt(
                        key,
                        digest,
                        "started",
                        recovery_context=normalized_context,
                    ),
                )

        if recovering:
            return self.reconcile(key, lambda _receipt: reconcile())

        try:
            result = effect()
            json.dumps(result, ensure_ascii=False, sort_keys=True)
        except Exception as error:
            with self._lock:
                self._write(
                    path,
                    EffectReceipt(
                        key,
                        digest,
                        "indeterminate",
                        error=f"{type(error).__name__}: {error}",
                        recovery_context=normalized_context,
                        recovery_history=recovery_history,
                    ),
                )
            raise

        with self._lock:
            self._write(
                path,
                EffectReceipt(
                    key,
                    digest,
                    "completed",
                    result=result,
                    recovery_context=normalized_context,
                    recovery_history=recovery_history,
                ),
            )
        return result

    def reconcile(
        self,
        key: str,
        reconcile: Callable[[EffectReceipt], Optional[Any]],
    ) -> Any:
        path = self._path(key)
        with self._lock:
            existing = self._read(path)
            if existing is None:
                raise ContractViolation(f"effect {key} does not exist")
            if existing.status == "completed":
                return existing.result
            if existing.status == "reset":
                raise EffectIndeterminateError(
                    f"effect {key} was reset and must be explicitly retried"
                )
            original_receipt_digest = self.receipt_digest(existing)
        try:
            recovered = reconcile(existing)
            if recovered is None:
                raise EffectIndeterminateError(
                    f"effect {key} reconcile could not prove an external outcome"
                )
            json.dumps(recovered, ensure_ascii=False, sort_keys=True)
        except Exception as error:
            with self._lock:
                self._write(
                    path,
                    EffectReceipt(
                        key,
                        existing.payload_digest,
                        "indeterminate",
                        error=f"{type(error).__name__}: {error}",
                        recovery_context=existing.recovery_context,
                        recovery_history=existing.recovery_history,
                    ),
                )
            raise
        completed = EffectReceipt(
            key,
            existing.payload_digest,
            "completed",
            result=recovered,
            recovery_context=existing.recovery_context,
            recovery_history=existing.recovery_history,
        )
        with self._lock:
            current = self._read(path)
            if (
                current is None
                or self.receipt_digest(current) != original_receipt_digest
            ):
                raise ContractViolation(f"effect {key} changed during reconciliation")
            self._write(path, completed)
        return recovered

    def reset(
        self,
        key: str,
        *,
        actor: str,
        reason: str,
        expected_receipt_digest: str,
    ) -> EffectReceipt:
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
            raise ContractViolation("effect reset actor must be a non-empty string")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ContractViolation("effect reset reason must be a non-empty string")
        if (
            not isinstance(expected_receipt_digest, str)
            or len(expected_receipt_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in expected_receipt_digest
            )
        ):
            raise ContractViolation("effect reset requires a valid receipt digest")
        path = self._path(key)
        with self._lock:
            existing = self._read(path)
            if existing is None:
                raise ContractViolation(f"effect {key} does not exist")
            for entry in existing.recovery_history:
                if (
                    entry.get("action") == "reset"
                    and entry.get("actor") == actor.strip()
                    and entry.get("reason") == reason.strip()
                    and entry.get("old_receipt_digest")
                    == expected_receipt_digest
                ):
                    return existing
            if existing.status == "completed":
                raise ContractViolation(f"completed effect {key} cannot be reset")
            actual_digest = self.receipt_digest(existing)
            if actual_digest != expected_receipt_digest:
                raise ContractViolation(f"effect {key} receipt digest changed")
            entry = {
                "action": "reset",
                "actor": actor.strip(),
                "reason": reason.strip(),
                "old_receipt_digest": actual_digest,
                "at": time.time(),
            }
            reset = EffectReceipt(
                key,
                existing.payload_digest,
                "reset",
                error="operator authorized retry after indeterminate effect",
                recovery_context=existing.recovery_context,
                recovery_history=(*existing.recovery_history, entry),
            )
            self._write(path, reset)
            return reset

    def inspect_all(self) -> Tuple[EffectReceipt, ...]:
        with self._lock:
            return tuple(
                receipt
                for receipt in (
                    self._read(path) for path in sorted(self.root.glob("*.json"))
                )
                if receipt is not None
            )

    @staticmethod
    def receipt_digest(receipt: EffectReceipt) -> str:
        encoded = json.dumps(
            asdict(receipt),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def inspect(self, key: str) -> Optional[EffectReceipt]:
        with self._lock:
            return self._read(self._path(key))

    def _path(self, key: str) -> Path:
        if not RUN_ID.fullmatch(key):
            raise ContractViolation("effect key must contain only letters, numbers, and hyphens")
        return self.root / f"{key}.json"

    @staticmethod
    def _digest(payload: Any) -> str:
        try:
            encoded = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as error:
            raise ContractViolation(f"effect payload must be JSON serializable: {error}") from error
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_recovery_context(value: Any) -> Any:
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError) as error:
            raise ContractViolation(
                f"effect recovery context must be JSON serializable: {error}"
            ) from error
        return json.loads(encoded)

    @staticmethod
    def _read(path: Path) -> Optional[EffectReceipt]:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            schema_version = value.get("schema_version", 0)
            if (
                isinstance(schema_version, bool)
                or schema_version not in (0, EFFECT_RECEIPT_SCHEMA_VERSION)
            ):
                raise ContractViolation("unsupported effect receipt schema")
            value["schema_version"] = EFFECT_RECEIPT_SCHEMA_VERSION
            if "recovery_history" in value:
                history = value["recovery_history"]
                if not isinstance(history, list) or any(
                    not isinstance(item, dict) for item in history
                ):
                    raise TypeError("recovery_history must be an array of objects")
                value["recovery_history"] = tuple(history)
            return EffectReceipt(**value)
        except (OSError, TypeError, json.JSONDecodeError) as error:
            raise ContractViolation(f"cannot read effect receipt {path}: {error}") from error

    @staticmethod
    def _write(path: Path, receipt: EffectReceipt) -> None:
        if receipt.schema_version != EFFECT_RECEIPT_SCHEMA_VERSION:
            raise ContractViolation("unsupported effect receipt schema")
        document = cast(EffectReceiptDocumentV1, asdict(receipt))
        atomic_json_write(path, document)


class LocalControlPlane:
    def __init__(
        self,
        root: Path,
        max_workers: int = 2,
        lease_seconds: float = 30.0,
        owner_id: Optional[str] = None,
        reuse_store: Optional[VerifiedArtifactCache] = None,
        token_reservations: Optional[HistoricalTokenReservations] = None,
    ):
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ContractViolation("control plane max_workers must be a positive integer")
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, (int, float))
            or lease_seconds <= 0
        ):
            raise ContractViolation("control plane lease_seconds must be positive")
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.owner_id = owner_id or f"controller-{uuid.uuid4()}"
        self.lease_seconds = float(lease_seconds)
        self.reuse_store = reuse_store
        self.token_reservations = token_reservations
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._futures: Dict[str, Future] = {}
        self._tokens: Dict[str, CancellationToken] = {}
        self._lease_tokens: Dict[str, LeaseToken] = {}
        self._registries: Dict[str, NodeRegistry] = {}
        self._policies: Dict[str, Optional[GatePolicy]] = {}

    def submit(
        self,
        graph: GraphSpec,
        registry: NodeRegistry,
        gate_policy: Optional[GatePolicy] = None,
    ) -> str:
        run_id = self.prepare(graph)
        self.start(run_id, registry, gate_policy)
        return run_id

    def prepare(self, graph: GraphSpec) -> str:
        """Persist a validated run without tying it to the caller process."""
        validate_graph(graph)
        run_id = str(uuid.uuid4())
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=False)
        atomic_json_write(run_dir / "graph.json", asdict(graph))
        now = time.time()
        self._write_state(
            run_id,
            {
                "schema_version": CONTROL_RUN_SCHEMA_VERSION,
                "run_id": run_id,
                "graph_id": graph.id,
                "phase": "queued",
                "owner_id": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
                "cancel_requested": False,
                "generation": 1,
                "lease_token_digest": None,
                "submitted_at": now,
                "error": None,
                "result": None,
            },
        )
        return run_id

    def start(
        self,
        run_id: str,
        registry: NodeRegistry,
        gate_policy: Optional[GatePolicy] = None,
    ) -> RunSnapshot:
        graph = self._read_graph(run_id)

        now = time.time()
        lease_token = self._mutate_state(
            run_id,
            lambda state: claim_lease(
                state,
                run_id=run_id,
                owner_id=self.owner_id,
                lease_seconds=self.lease_seconds,
                now=now,
                takeover=False,
            ),
        )
        self._lease_tokens[run_id] = lease_token
        self._registries[run_id] = registry
        self._policies[run_id] = gate_policy
        self._launch(
            run_id, graph, registry, gate_policy, resume=False, lease_token=lease_token
        )
        return self.inspect(run_id)

    def inspect(self, run_id: str) -> RunSnapshot:
        state = self._read_state(run_id)
        return RunSnapshot(
            run_id=state["run_id"],
            graph_id=state["graph_id"],
            phase=state["phase"],
            owner_id=state.get("owner_id"),
            lease_expires_at=state.get("lease_expires_at"),
            heartbeat_at=state.get("heartbeat_at"),
            cancel_requested=bool(state.get("cancel_requested")),
            generation=int(state.get("generation", 1)),
            error=state.get("error"),
            result=state.get("result"),
        )

    def events(self, run_id: str, after: int = 0) -> EventPage:
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ContractViolation("event cursor must be a non-negative integer")
        self._read_state(run_id)
        path = self._run_dir(run_id) / "runtime" / "events.jsonl"
        records = tuple(JsonlEventSink(path).read()) if path.exists() else ()
        return EventPage(records[after:], len(records))

    def cancel(self, run_id: str) -> RunSnapshot:
        def mutate(state: Dict[str, Any]) -> None:
            if state["phase"] in TERMINAL_PHASES:
                raise ContractViolation(f"run {run_id} is already {state['phase']}")
            state["cancel_requested"] = True
            state["phase"] = "cancelling"

        self._mutate_state(run_id, mutate)
        token = self._tokens.get(run_id)
        if token is not None:
            token.cancel()
        return self.inspect(run_id)

    def resume(
        self,
        run_id: str,
        registry: NodeRegistry,
        gate_policy: Optional[GatePolicy] = None,
    ) -> RunSnapshot:
        graph = self._read_graph(run_id)
        now = time.time()
        lease_token = self._mutate_state(
            run_id,
            lambda state: claim_lease(
                state,
                run_id=run_id,
                owner_id=self.owner_id,
                lease_seconds=self.lease_seconds,
                now=now,
                takeover=True,
            ),
        )
        self._lease_tokens[run_id] = lease_token
        self._registries[run_id] = registry
        self._policies[run_id] = gate_policy
        self._launch(
            run_id, graph, registry, gate_policy, resume=True, lease_token=lease_token
        )
        return self.inspect(run_id)

    def wait(self, run_id: str, timeout: Optional[float] = None) -> RunSnapshot:
        future = self._futures.get(run_id)
        if future is None:
            snapshot = self.inspect(run_id)
            if snapshot.phase not in TERMINAL_PHASES:
                raise ContractViolation(f"run {run_id} is not owned by this control plane")
            return snapshot
        try:
            future.result(timeout=timeout)
        except FutureTimeoutError as error:
            raise TimeoutError(f"run {run_id} did not finish before timeout") from error
        return self.inspect(run_id)

    def close(self) -> None:
        self._executor.shutdown(wait=True)

    def _launch(
        self,
        run_id: str,
        graph: GraphSpec,
        registry: NodeRegistry,
        gate_policy: Optional[GatePolicy],
        resume: bool,
        lease_token: LeaseToken,
    ) -> None:
        token = CancellationToken()
        self._tokens[run_id] = token
        future = self._executor.submit(
            self._execute_run,
            run_id,
            graph,
            registry,
            gate_policy,
            resume,
            token,
            lease_token,
        )
        self._futures[run_id] = future

    def _execute_run(
        self,
        run_id: str,
        graph: GraphSpec,
        registry: NodeRegistry,
        gate_policy: Optional[GatePolicy],
        resume: bool,
        token: CancellationToken,
        lease_token: LeaseToken,
    ) -> None:
        self._verify_lease(run_id, lease_token)
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(run_id, lease_token, token, stop_heartbeat),
            daemon=True,
        )
        heartbeat.start()
        try:
            runtime_dir = self._run_dir(run_id) / "runtime"
            result = GraphRuntime(
                graph,
                registry,
                work_dir=runtime_dir,
                gate_policy=gate_policy,
                verified_result_publisher=(
                    VerifiedResultPublisher(
                        self.reuse_store,
                        runtime_dir / "verified-publications.json",
                    )
                    if self.reuse_store is not None
                    else None
                ),
                token_reservations=self.token_reservations,
                ownership_guard=lambda: self._lease_guard(run_id, lease_token),
                effect_lease=lease_token.persisted_identity(),
            ).run(resume=resume, run_id=run_id, cancellation=token)
            self._settle_success(run_id, lease_token, result, token.cancelled)
        except Exception as error:
            try:
                self._settle_error(run_id, lease_token, error)
            except LeaseLostError:
                token.cancel()
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=max(1.0, self.lease_seconds))
            self._tokens.pop(run_id, None)
            if self._lease_tokens.get(run_id) == lease_token:
                self._lease_tokens.pop(run_id, None)

    def _heartbeat_loop(
        self,
        run_id: str,
        lease_token: LeaseToken,
        cancellation: CancellationToken,
        stop: threading.Event,
    ) -> None:
        interval = max(0.05, min(self.lease_seconds / 3.0, 10.0))
        while not stop.wait(interval):
            try:
                cancel_requested = self._mutate_state(
                    run_id,
                    lambda state: renew_lease(
                        state,
                        lease_token,
                        now=time.time(),
                        lease_seconds=self.lease_seconds,
                    ),
                )
            except LeaseLostError:
                cancellation.cancel()
                return
            if cancel_requested:
                cancellation.cancel()

    def _settle_success(
        self,
        run_id: str,
        lease_token: LeaseToken,
        result: RunResult,
        cancel_requested: bool,
    ) -> None:
        statuses = {node_id: status.value for node_id, status in result.statuses.items()}
        cancelled = cancel_requested or any(
            status is NodeStatus.CANCELLED for status in result.statuses.values()
        )

        def mutate(state: Dict[str, Any]) -> None:
            now = time.time()
            assert_lease(state, lease_token, now=now)
            state.update(
                {
                    "phase": "cancelled" if cancelled else ("succeeded" if result.success else "failed"),
                    "owner_id": None,
                    "lease_expires_at": None,
                    "heartbeat_at": now,
                    "lease_token_digest": None,
                    "result": {
                        "success": result.success,
                        "statuses": statuses,
                        "tokens_used": result.tokens_used,
                        "cost_usd": result.cost_usd,
                        "usage": result.usage.to_dict(),
                        "artifacts": dict(result.artifacts),
                    },
                }
            )

        self._mutate_state(run_id, mutate)

    def _settle_error(
        self, run_id: str, lease_token: LeaseToken, error: Exception
    ) -> None:
        def mutate(state: Dict[str, Any]) -> None:
            now = time.time()
            assert_lease(state, lease_token, now=now)
            state.update(
                {
                    "phase": "failed",
                    "owner_id": None,
                    "lease_expires_at": None,
                    "heartbeat_at": now,
                    "lease_token_digest": None,
                    "error": f"{type(error).__name__}: {error}",
                }
            )

        self._mutate_state(run_id, mutate)

    def _run_dir(self, run_id: str) -> Path:
        if not RUN_ID.fullmatch(run_id):
            raise ContractViolation("invalid run_id")
        return self.root / "runs" / run_id

    def _state_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "state.json"

    def _read_graph(self, run_id: str) -> GraphSpec:
        path = self._run_dir(run_id) / "graph.json"
        try:
            return GraphSpec.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(f"cannot restore graph for run {run_id}: {error}") from error

    def _read_state(self, run_id: str) -> Dict[str, Any]:
        path = self._state_path(run_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(f"cannot read run {run_id}: {error}") from error
        if not isinstance(value, dict):
            raise ContractViolation(f"run {run_id} state must be an object")
        schema_version = value.get("schema_version", 0)
        if (
            isinstance(schema_version, bool)
            or schema_version not in (0, CONTROL_RUN_SCHEMA_VERSION)
        ):
            raise ContractViolation("unsupported run state schema")
        value["schema_version"] = CONTROL_RUN_SCHEMA_VERSION
        return value

    def _write_state(self, run_id: str, state: Mapping[str, Any]) -> None:
        value = dict(state)
        schema_version = value.setdefault(
            "schema_version", CONTROL_RUN_SCHEMA_VERSION
        )
        if schema_version != CONTROL_RUN_SCHEMA_VERSION:
            raise ContractViolation("unsupported run state schema")
        atomic_json_write(self._state_path(run_id), value)

    def _verify_lease(self, run_id: str, lease_token: LeaseToken) -> None:
        with file_lock(self._run_dir(run_id) / "state.lock"):
            assert_lease(self._read_state(run_id), lease_token, now=time.time())

    @contextmanager
    def _lease_guard(
        self, run_id: str, lease_token: LeaseToken
    ) -> Iterator[None]:
        """Fence one short runtime publication or dispatch transaction."""

        with file_lock(self._run_dir(run_id) / "state.lock"):
            assert_lease(self._read_state(run_id), lease_token, now=time.time())
            yield

    def _mutate_state(
        self,
        run_id: str,
        mutation: Callable[[Dict[str, Any]], _MutationResult],
    ) -> _MutationResult:
        with file_lock(self._run_dir(run_id) / "state.lock"):
            state = self._read_state(run_id)
            result = mutation(state)
            self._write_state(run_id, state)
            return result
