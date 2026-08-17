import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional, Protocol, Sequence, Tuple

from .errors import AgentProtocolError, ContractViolation
from .routing import ExecutorProfile, PolicyRouter

if TYPE_CHECKING:
    from .reuse import VerifiedArtifactCache


def validate_agent_outputs(
    value: Mapping[str, Any], expected: Sequence[str]
) -> Mapping[str, Any]:
    actual_keys = set(value)
    expected_keys = set(expected)
    if actual_keys != expected_keys:
        raise AgentProtocolError(
            f"agent output contract mismatch; expected {sorted(expected_keys)}, "
            f"got {sorted(actual_keys)}"
        )
    return dict(value)


@dataclass(frozen=True)
class ExecutorCapabilities:
    executor_id: str
    features: Tuple[str, ...] = ()
    tools: Tuple[str, ...] = ()

    def supports(self, features: Sequence[str], tools: Sequence[str]) -> bool:
        return set(features).issubset(self.features) and set(tools).issubset(self.tools)


@dataclass(frozen=True)
class AgentRequest:
    task_id: str
    prompt: str
    inputs: Mapping[str, Any]
    output_keys: Tuple[str, ...]
    workspace: Path
    model: Optional[str] = None
    tools: Tuple[str, ...] = ()
    timeout_seconds: int = 300
    max_cost_usd: Optional[float] = None
    data_classification: str = "public"
    task_type: str = "general"
    model_family: str = "default"
    reuse_scope: str = "local"
    reuse_allowed: bool = True

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ContractViolation("agent prompt cannot be empty")
        if not self.output_keys or len(self.output_keys) != len(set(self.output_keys)):
            raise ContractViolation("agent output_keys must be non-empty and unique")
        if not self.workspace.is_dir():
            raise ContractViolation(f"agent workspace does not exist: {self.workspace}")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
        ):
            raise ContractViolation("agent timeout_seconds must be a positive integer")
        if self.max_cost_usd is not None and (
            isinstance(self.max_cost_usd, bool)
            or not isinstance(self.max_cost_usd, (int, float))
            or not math.isfinite(self.max_cost_usd)
            or self.max_cost_usd <= 0
        ):
            raise ContractViolation("agent max_cost_usd must be a finite positive number")
        if self.data_classification not in (
            "public",
            "internal",
            "confidential",
            "restricted",
        ):
            raise ContractViolation("invalid agent data_classification")
        if not isinstance(self.task_type, str) or not self.task_type.strip():
            raise ContractViolation("agent task_type cannot be empty")
        if not isinstance(self.model_family, str) or not self.model_family.strip():
            raise ContractViolation("agent model_family cannot be empty")
        if (
            not isinstance(self.reuse_scope, str)
            or not self.reuse_scope.strip()
            or len(self.reuse_scope) > 128
            or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for char in self.reuse_scope)
        ):
            raise ContractViolation("agent reuse_scope must be a safe non-empty identifier")
        if not isinstance(self.reuse_allowed, bool):
            raise ContractViolation("agent reuse_allowed must be a boolean")


@dataclass(frozen=True)
class AgentResult:
    executor_id: str
    outputs: Mapping[str, Any]
    text: str
    tokens_used: int = 0
    cost_usd: Optional[float] = None
    session_id: Optional[str] = None
    reuse_status: str = "none"
    source_task_id: Optional[str] = None
    source_run_id: Optional[str] = None
    verification_id: Optional[str] = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.tokens_used, bool)
            or not isinstance(self.tokens_used, int)
            or self.tokens_used < 0
        ):
            raise ContractViolation("agent tokens_used must be a non-negative integer")
        if self.cost_usd is not None and (
            isinstance(self.cost_usd, bool)
            or not isinstance(self.cost_usd, (int, float))
            or not math.isfinite(self.cost_usd)
            or self.cost_usd < 0
        ):
            raise ContractViolation("agent cost_usd must be a finite non-negative number")
        if self.reuse_status not in (
            "none",
            "bypassed",
            "miss",
            "hit",
            "coalesced",
        ):
            raise ContractViolation("invalid agent reuse_status")


@dataclass(frozen=True)
class AgentExecution:
    request: AgentRequest
    result: AgentResult


class AgentExecutor(Protocol):
    @property
    def capabilities(self) -> ExecutorCapabilities:
        ...

    def execute(self, request: AgentRequest) -> AgentResult:
        ...


class ExecutorRegistry:
    def __init__(
        self,
        router: Optional[PolicyRouter] = None,
        reuse_store: Optional["VerifiedArtifactCache"] = None,
    ):
        self._executors = {}
        self._router = router or PolicyRouter()
        self._reuse_store = reuse_store

    def register(
        self, executor: AgentExecutor, profile: Optional[ExecutorProfile] = None
    ) -> None:
        executor_id = executor.capabilities.executor_id
        if executor_id in self._executors:
            raise ContractViolation(f"agent executor {executor_id} is already registered")
        self._router.register(
            executor_id,
            profile or ExecutorProfile(provider=executor_id),
        )
        self._executors[executor_id] = executor

    def capabilities(self) -> Tuple[ExecutorCapabilities, ...]:
        return tuple(
            self._executors[executor_id].capabilities
            for executor_id in sorted(self._executors)
        )

    def profiles(self) -> Mapping[str, ExecutorProfile]:
        return self._router.profiles()

    def execute(
        self,
        request: AgentRequest,
        executor_id: Optional[str] = None,
        required_features: Sequence[str] = (),
    ) -> AgentResult:
        implied_features = {"structured_output"}
        if request.model is not None:
            implied_features.add("model_selection")
        if request.tools:
            implied_features.add("tool_policy")
        if request.max_cost_usd is not None:
            implied_features.add("cost_budget")
        all_features = set(required_features) | implied_features
        candidates = []
        for candidate_id in sorted(self._executors):
            if executor_id is not None and candidate_id != executor_id:
                continue
            executor = self._executors[candidate_id]
            if executor.capabilities.supports(all_features, request.tools):
                candidates.append(candidate_id)
        if not candidates:
            requested = executor_id or "auto"
            raise ContractViolation(
                f"no agent executor satisfies executor={requested}, "
                f"features={sorted(all_features)}, tools={sorted(request.tools)}"
            )
        decision = self._router.select(
            candidates, request, reserve=self._reuse_store is None
        )

        def load() -> AgentResult:
            nonlocal decision
            if self._reuse_store is not None:
                try:
                    decision = self._router.reserve(decision, request)
                except ContractViolation:
                    decision = self._router.select(candidates, request)
            started = time.monotonic()
            try:
                result = self._executors[decision.executor_id].execute(request)
            except Exception as error:
                self._router.record_failure(
                    decision,
                    request,
                    time.monotonic() - started,
                    type(error).__name__,
                )
                raise
            self._router.record_success(
                decision,
                request,
                time.monotonic() - started,
                result.cost_usd,
            )
            return result

        if self._reuse_store is None:
            return load()
        return self._reuse_store.resolve(request, decision.executor_id, load)
