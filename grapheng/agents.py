import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

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
    max_tokens: Optional[int] = None
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
        if self.max_tokens is not None and (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens < 1
        ):
            raise ContractViolation("agent max_tokens must be a positive integer")
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
class ModelUsage:
    """Provider-neutral model usage with explicit measurement completeness."""

    input_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    input_tokens_complete: bool = False
    cached_input_tokens_complete: bool = False
    output_tokens_complete: bool = False
    total_tokens_complete: bool = False
    cost_complete: bool = False

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ContractViolation(
                    f"model usage {name} must be a non-negative integer or null"
                )
        if self.cost_usd is not None and (
            isinstance(self.cost_usd, bool)
            or not isinstance(self.cost_usd, (int, float))
            or not math.isfinite(self.cost_usd)
            or self.cost_usd < 0
        ):
            raise ContractViolation(
                "model usage cost_usd must be a finite non-negative number or null"
            )
        if self.cost_usd is not None:
            object.__setattr__(self, "cost_usd", float(self.cost_usd))
        for name in (
            "input_tokens_complete",
            "cached_input_tokens_complete",
            "output_tokens_complete",
            "total_tokens_complete",
            "cost_complete",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ContractViolation(f"model usage {name} must be a boolean")
        for value_name, complete_name in (
            ("input_tokens", "input_tokens_complete"),
            ("cached_input_tokens", "cached_input_tokens_complete"),
            ("output_tokens", "output_tokens_complete"),
            ("total_tokens", "total_tokens_complete"),
            ("cost_usd", "cost_complete"),
        ):
            if getattr(self, complete_name) and getattr(self, value_name) is None:
                raise ContractViolation(
                    f"model usage {complete_name} requires a measured value"
                )

    @classmethod
    def no_call(cls) -> "ModelUsage":
        """Return exact zero usage for work that made no model call."""

        return cls(0, 0, 0, 0, 0.0, True, True, True, True, True)

    @classmethod
    def unknown(cls) -> "ModelUsage":
        return cls()

    @classmethod
    def from_legacy_constructor(
        cls, tokens_used: int, cost_usd: Optional[float]
    ) -> "ModelUsage":
        """Normalize the historical in-memory AgentResult constructor."""

        return cls(
            total_tokens=tokens_used,
            cost_usd=cost_usd,
            total_tokens_complete=True,
            cost_complete=cost_usd is not None,
        )

    @classmethod
    def from_persisted(
        cls,
        value: Any,
        tokens_used: int = 0,
        cost_usd: Optional[float] = None,
        cost_complete: Optional[bool] = None,
    ) -> "ModelUsage":
        """Read new usage or conservatively normalize a legacy persisted record."""

        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            def optional_token(name: str) -> Optional[int]:
                item = value.get(name)
                return item if isinstance(item, int) and not isinstance(item, bool) else None

            raw_cost = value.get("cost_usd")
            measured_cost = (
                float(raw_cost)
                if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
                else None
            )
            total = optional_token("total_tokens")
            if total is None:
                total = tokens_used
            if measured_cost is None:
                measured_cost = cost_usd
            return cls(
                input_tokens=optional_token("input_tokens"),
                cached_input_tokens=optional_token("cached_input_tokens"),
                output_tokens=optional_token("output_tokens"),
                total_tokens=total,
                cost_usd=measured_cost,
                input_tokens_complete=value.get("input_tokens_complete", False),
                cached_input_tokens_complete=value.get(
                    "cached_input_tokens_complete", False
                ),
                output_tokens_complete=value.get("output_tokens_complete", False),
                total_tokens_complete=value.get("total_tokens_complete", False),
                cost_complete=value.get(
                    "cost_complete",
                    cost_complete if cost_complete is not None else False,
                ),
            )
        return cls(
            total_tokens=tokens_used,
            cost_usd=cost_usd,
            total_tokens_complete=False,
            cost_complete=cost_complete if cost_complete is not None else False,
        )

    @classmethod
    def combine(cls, usages: Sequence["ModelUsage"]) -> "ModelUsage":
        items = tuple(usages)
        if not items:
            return cls.no_call()
        contributing = tuple(item for item in items if item != cls.no_call())
        if not contributing:
            return cls.no_call()
        items = contributing

        def aggregate(value_name: str, complete_name: str) -> Tuple[Any, bool]:
            values = [getattr(item, value_name) for item in items]
            known = [value for value in values if value is not None]
            value = sum(known) if known else None
            complete = all(getattr(item, complete_name) for item in items)
            return value, complete

        input_tokens, input_complete = aggregate(
            "input_tokens", "input_tokens_complete"
        )
        cached_tokens, cached_complete = aggregate(
            "cached_input_tokens", "cached_input_tokens_complete"
        )
        output_tokens, output_complete = aggregate(
            "output_tokens", "output_tokens_complete"
        )
        total_tokens, total_complete = aggregate(
            "total_tokens", "total_tokens_complete"
        )
        cost, cost_is_complete = aggregate("cost_usd", "cost_complete")
        return cls(
            input_tokens,
            cached_tokens,
            output_tokens,
            total_tokens,
            cost,
            input_complete,
            cached_complete,
            output_complete,
            total_complete,
            cost_is_complete,
        )

    def with_accounted_totals(
        self, tokens_used: int, cost_usd: float
    ) -> "ModelUsage":
        """Mirror conservative ledger totals without claiming measurement."""

        return ModelUsage(
            self.input_tokens,
            self.cached_input_tokens,
            self.output_tokens,
            tokens_used,
            cost_usd,
            self.input_tokens_complete,
            self.cached_input_tokens_complete,
            self.output_tokens_complete,
            self.total_tokens_complete
            and self.total_tokens is not None
            and self.total_tokens == tokens_used,
            self.cost_complete
            and self.cost_usd is not None
            and self.cost_usd == cost_usd,
        )

    @property
    def complete(self) -> bool:
        return all(
            (
                self.input_tokens_complete,
                self.cached_input_tokens_complete,
                self.output_tokens_complete,
                self.total_tokens_complete,
                self.cost_complete,
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "input_tokens_complete": self.input_tokens_complete,
            "cached_input_tokens_complete": self.cached_input_tokens_complete,
            "output_tokens_complete": self.output_tokens_complete,
            "total_tokens_complete": self.total_tokens_complete,
            "cost_complete": self.cost_complete,
        }


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
    usage: Optional[ModelUsage] = None

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
        usage = self.usage
        if usage is None:
            usage = ModelUsage.from_legacy_constructor(
                self.tokens_used, self.cost_usd
            )
            object.__setattr__(self, "usage", usage)
        elif not isinstance(usage, ModelUsage):
            raise ContractViolation("agent usage must be ModelUsage")
        if usage.total_tokens != self.tokens_used:
            raise ContractViolation("agent usage total_tokens must match tokens_used")
        if usage.cost_usd != self.cost_usd:
            raise ContractViolation("agent usage cost_usd must match cost_usd")


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
        self._executors: Dict[str, AgentExecutor] = {}
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
        if request.max_tokens is not None:
            implied_features.add("token_budget")
        all_features = set(required_features) | implied_features
        candidates = []
        for candidate_id in sorted(self._executors):
            if executor_id is not None and candidate_id != executor_id:
                continue
            executor = self._executors[candidate_id]
            if executor.capabilities.supports(sorted(all_features), request.tools):
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
