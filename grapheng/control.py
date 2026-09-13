import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from ._store import atomic_json_write
from .errors import ContractViolation, EffectIndeterminateError
from .events import JsonlEventSink
from .model import GraphSpec
from .policy import GatePolicy
from .publication import VerifiedResultPublisher
from .reuse import VerifiedArtifactCache
from .runtime import CancellationToken, GraphRuntime, NodeRegistry, NodeStatus, RunResult
from .validation import validate_graph

RUN_ID = re.compile(r"^[A-Za-z0-9-]{1,128}$")
TERMINAL_PHASES = {"succeeded", "failed", "cancelled"}


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
    ) -> Any:
        path = self._path(key)
        digest = self._digest(payload)
        with self._lock:
            existing = self._read(path)
            if existing is not None:
                if existing.payload_digest != digest:
                    raise ContractViolation(
                        f"effect key {key} was reused with different input"
                    )
                if existing.status == "completed":
                    return existing.result
                raise EffectIndeterminateError(
                    f"effect {key} is {existing.status}; reconcile it before retrying"
                )
            self._write(path, EffectReceipt(key, digest, "started"))

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
                    ),
                )
            raise

        with self._lock:
            self._write(path, EffectReceipt(key, digest, "completed", result=result))
        return result

    def execute_reconcilable(
        self,
        key: str,
        payload: Any,
        effect: Callable[[], Any],
        reconcile: Callable[[], Optional[Any]],
    ) -> Any:
        path = self._path(key)
        digest = self._digest(payload)
        recovering = False
        with self._lock:
            existing = self._read(path)
            if existing is not None:
                if existing.payload_digest != digest:
                    raise ContractViolation(
                        f"effect key {key} was reused with different input"
                    )
                if existing.status == "completed":
                    return existing.result
                recovering = True
            else:
                self._write(path, EffectReceipt(key, digest, "started"))

        if recovering:
            try:
                recovered = reconcile()
                if recovered is not None:
                    json.dumps(recovered, ensure_ascii=False, sort_keys=True)
                    with self._lock:
                        self._write(
                            path,
                            EffectReceipt(key, digest, "completed", result=recovered),
                        )
                    return recovered
            except Exception as error:
                with self._lock:
                    self._write(
                        path,
                        EffectReceipt(
                            key,
                            digest,
                            "indeterminate",
                            error=f"{type(error).__name__}: {error}",
                        ),
                    )
                raise
            with self._lock:
                self._write(path, EffectReceipt(key, digest, "started"))

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
                    ),
                )
            raise

        with self._lock:
            self._write(path, EffectReceipt(key, digest, "completed", result=result))
        return result

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
    def _read(path: Path) -> Optional[EffectReceipt]:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return EffectReceipt(**value)
        except (OSError, TypeError, json.JSONDecodeError) as error:
            raise ContractViolation(f"cannot read effect receipt {path}: {error}") from error

    @staticmethod
    def _write(path: Path, receipt: EffectReceipt) -> None:
        atomic_json_write(path, asdict(receipt))


class LocalControlPlane:
    def __init__(
        self,
        root: Path,
        max_workers: int = 2,
        lease_seconds: float = 30.0,
        owner_id: Optional[str] = None,
        reuse_store: Optional[VerifiedArtifactCache] = None,
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
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._futures: Dict[str, Future] = {}
        self._tokens: Dict[str, CancellationToken] = {}
        self._registries: Dict[str, NodeRegistry] = {}
        self._policies: Dict[str, Optional[GatePolicy]] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

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
                "run_id": run_id,
                "graph_id": graph.id,
                "phase": "queued",
                "owner_id": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
                "cancel_requested": False,
                "generation": 1,
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

        def claim(state: Dict[str, Any]) -> None:
            if state["phase"] != "queued" or state.get("owner_id") is not None:
                raise ContractViolation(
                    f"run {run_id} cannot start from phase {state['phase']}"
                )

        self._mutate_state(run_id, claim)
        self._registries[run_id] = registry
        self._policies[run_id] = gate_policy
        self._launch(run_id, graph, registry, gate_policy, resume=False)
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

        def mutate(state: Dict[str, Any]) -> None:
            phase = state["phase"]
            if phase == "succeeded":
                raise ContractViolation(f"run {run_id} is already succeeded")
            lease = state.get("lease_expires_at")
            if phase in ("running", "cancelling") and lease is not None and lease > now:
                raise ContractViolation(f"run {run_id} still has an active lease")
            state.update(
                {
                    "phase": "queued",
                    "owner_id": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "cancel_requested": False,
                    "generation": int(state.get("generation", 1)) + 1,
                    "error": None,
                }
            )

        self._mutate_state(run_id, mutate)
        self._registries[run_id] = registry
        self._policies[run_id] = gate_policy
        self._launch(run_id, graph, registry, gate_policy, resume=True)
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
    ) -> None:
        now = time.time()

        def claim(state: Dict[str, Any]) -> None:
            state.update(
                {
                    "phase": "running",
                    "owner_id": self.owner_id,
                    "heartbeat_at": now,
                    "lease_expires_at": now + self.lease_seconds,
                }
            )
            if state.get("cancel_requested"):
                token.cancel()

        self._mutate_state(run_id, claim)
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(run_id, stop_heartbeat),
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
            ).run(resume=resume, run_id=run_id, cancellation=token)
            self._settle_success(run_id, result, token.cancelled)
        except Exception as error:
            self._settle_error(run_id, error)
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=max(1.0, self.lease_seconds))
            self._tokens.pop(run_id, None)

    def _heartbeat_mutation(self, now: float) -> Callable[[Dict[str, Any]], None]:
        def mutate(state: Dict[str, Any]) -> None:
            if state.get("owner_id") != self.owner_id:
                return
            if state["phase"] not in ("running", "cancelling"):
                return
            state["heartbeat_at"] = now
            state["lease_expires_at"] = now + self.lease_seconds

        return mutate

    def _heartbeat_loop(self, run_id: str, stop: threading.Event) -> None:
        interval = max(0.05, min(self.lease_seconds / 3.0, 10.0))
        while not stop.wait(interval):
            self._mutate_state(run_id, self._heartbeat_mutation(time.time()))

    def _settle_success(
        self, run_id: str, result: RunResult, cancel_requested: bool
    ) -> None:
        statuses = {node_id: status.value for node_id, status in result.statuses.items()}
        cancelled = cancel_requested or any(
            status is NodeStatus.CANCELLED for status in result.statuses.values()
        )

        def mutate(state: Dict[str, Any]) -> None:
            state.update(
                {
                    "phase": "cancelled" if cancelled else ("succeeded" if result.success else "failed"),
                    "owner_id": None,
                    "lease_expires_at": None,
                    "heartbeat_at": time.time(),
                    "result": {
                        "success": result.success,
                        "statuses": statuses,
                        "tokens_used": result.tokens_used,
                        "cost_usd": result.cost_usd,
                        "artifacts": dict(result.artifacts),
                    },
                }
            )

        self._mutate_state(run_id, mutate)

    def _settle_error(self, run_id: str, error: Exception) -> None:
        def mutate(state: Dict[str, Any]) -> None:
            state.update(
                {
                    "phase": "failed",
                    "owner_id": None,
                    "lease_expires_at": None,
                    "heartbeat_at": time.time(),
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

    def _lock_for(self, run_id: str) -> threading.Lock:
        with self._global_lock:
            return self._locks.setdefault(run_id, threading.Lock())

    def _read_state(self, run_id: str) -> Dict[str, Any]:
        path = self._state_path(run_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(f"cannot read run {run_id}: {error}") from error
        if not isinstance(value, dict):
            raise ContractViolation(f"run {run_id} state must be an object")
        return value

    def _write_state(self, run_id: str, state: Mapping[str, Any]) -> None:
        atomic_json_write(self._state_path(run_id), state)

    def _mutate_state(
        self, run_id: str, mutation: Callable[[Dict[str, Any]], None]
    ) -> None:
        with self._lock_for(run_id):
            state = self._read_state(run_id)
            mutation(state)
            self._write_state(run_id, state)
