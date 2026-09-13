import hashlib
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .errors import ContractViolation
from .governance import ProviderGovernanceStore, ProviderPolicy

DATA_CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")


def route_context_key(
    task_type: str, tools: Sequence[str], model_family: str
) -> str:
    """Build a prompt-free, deterministic key for task-conditioned learning."""
    payload = "\x1f".join(
        (task_type, model_family, "\x1e".join(sorted(set(tools))))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class ExecutorProfile:
    provider: str
    estimated_cost_usd: float = 0.0
    estimated_latency_seconds: float = 0.0
    data_classifications: Tuple[str, ...] = ("public",)

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ContractViolation("executor provider cannot be empty")
        for name, value in (
            ("estimated_cost_usd", self.estimated_cost_usd),
            ("estimated_latency_seconds", self.estimated_latency_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ContractViolation(f"{name} must be a finite non-negative number")
        invalid = sorted(set(self.data_classifications) - set(DATA_CLASSIFICATIONS))
        if invalid or not self.data_classifications:
            raise ContractViolation(
                "executor data_classifications must be a non-empty subset of: "
                + ", ".join(DATA_CLASSIFICATIONS)
            )


@dataclass(frozen=True)
class RouteDecision:
    executor_id: str
    provider: str
    estimated_cost_usd: float
    estimated_latency_seconds: float
    governance_token: Optional[str] = None


@dataclass(frozen=True)
class LearnedExecutorEstimate:
    samples: int
    success_rate: float
    average_cost_usd: Optional[float]
    average_latency_seconds: float
    average_quality: Optional[float] = None
    quality_samples: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.samples, bool) or not isinstance(self.samples, int) or self.samples < 1:
            raise ContractViolation("learned estimate samples must be a positive integer")
        if (
            isinstance(self.success_rate, bool)
            or not isinstance(self.success_rate, (int, float))
            or not 0 <= self.success_rate <= 1
        ):
            raise ContractViolation("learned success_rate must be between zero and one")
        for name, value in (
            ("average_cost_usd", self.average_cost_usd),
            ("average_latency_seconds", self.average_latency_seconds),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ContractViolation(f"learned {name} must be non-negative")
        if self.average_quality is not None and (
            isinstance(self.average_quality, bool)
            or not isinstance(self.average_quality, (int, float))
            or not math.isfinite(self.average_quality)
            or not 0 <= self.average_quality <= 1
        ):
            raise ContractViolation("learned average_quality must be between zero and one")
        if (
            isinstance(self.quality_samples, bool)
            or not isinstance(self.quality_samples, int)
            or self.quality_samples < 0
            or self.quality_samples > self.samples
        ):
            raise ContractViolation(
                "learned quality_samples must be between zero and samples"
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LearnedExecutorEstimate":
        return cls(
            samples=int(value["samples"]),
            success_rate=float(value["success_rate"]),
            average_cost_usd=(
                None
                if value.get("average_cost_usd") is None
                else float(value["average_cost_usd"])
            ),
            average_latency_seconds=float(value["average_latency_seconds"]),
            average_quality=(
                None
                if value.get("average_quality") is None
                else float(value["average_quality"])
            ),
            quality_samples=int(
                value.get(
                    "quality_samples",
                    value["samples"] if value.get("average_quality") is not None else 0,
                )
            ),
        )


@dataclass(frozen=True)
class LearnedRoutingPolicy:
    version: str
    estimates: Mapping[str, LearnedExecutorEstimate]
    conditioned_estimates: Mapping[
        str, Mapping[str, LearnedExecutorEstimate]
    ] = field(default_factory=dict)
    min_samples: int = 3
    min_success_rate: float = 0.8
    min_quality_score: float = 0.8
    min_quality_samples: int = 3
    rollout_percent: int = 10

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise ContractViolation("learned policy version cannot be empty")
        if isinstance(self.min_samples, bool) or not isinstance(self.min_samples, int) or self.min_samples < 1:
            raise ContractViolation("learned policy min_samples must be positive")
        if not isinstance(self.min_success_rate, (int, float)) or not 0 <= self.min_success_rate <= 1:
            raise ContractViolation("learned policy min_success_rate must be between zero and one")
        if (
            isinstance(self.min_quality_score, bool)
            or not isinstance(self.min_quality_score, (int, float))
            or not 0 <= self.min_quality_score <= 1
        ):
            raise ContractViolation("learned policy min_quality_score must be between zero and one")
        if (
            isinstance(self.min_quality_samples, bool)
            or not isinstance(self.min_quality_samples, int)
            or self.min_quality_samples < 1
        ):
            raise ContractViolation(
                "learned policy min_quality_samples must be positive"
            )
        if (
            isinstance(self.rollout_percent, bool)
            or not isinstance(self.rollout_percent, int)
            or not 1 <= self.rollout_percent <= 100
        ):
            raise ContractViolation("learned policy rollout_percent must be between 1 and 100")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "estimates": {
                key: value.to_dict() for key, value in sorted(self.estimates.items())
            },
            "conditioned_estimates": {
                context: {
                    key: estimate.to_dict()
                    for key, estimate in sorted(estimates.items())
                }
                for context, estimates in sorted(self.conditioned_estimates.items())
            },
            "min_samples": self.min_samples,
            "min_success_rate": self.min_success_rate,
            "min_quality_score": self.min_quality_score,
            "min_quality_samples": self.min_quality_samples,
            "rollout_percent": self.rollout_percent,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LearnedRoutingPolicy":
        estimates = value.get("estimates")
        if not isinstance(estimates, dict):
            raise ContractViolation("learned policy estimates must be an object")
        conditioned = value.get("conditioned_estimates", {})
        if not isinstance(conditioned, dict) or any(
            not isinstance(items, dict) for items in conditioned.values()
        ):
            raise ContractViolation(
                "learned policy conditioned_estimates must be an object of objects"
            )
        return cls(
            version=str(value["version"]),
            estimates={
                str(key): LearnedExecutorEstimate.from_dict(item)
                for key, item in estimates.items()
            },
            conditioned_estimates={
                str(context): {
                    str(key): LearnedExecutorEstimate.from_dict(item)
                    for key, item in items.items()
                }
                for context, items in conditioned.items()
            },
            min_samples=int(value.get("min_samples", 3)),
            min_success_rate=float(value.get("min_success_rate", 0.8)),
            min_quality_score=float(value.get("min_quality_score", 0.8)),
            min_quality_samples=int(value.get("min_quality_samples", 3)),
            rollout_percent=int(value.get("rollout_percent", 10)),
        )


@dataclass(frozen=True)
class RouteObservation:
    observed_at: float
    task_id: str
    executor_id: str
    provider: str
    success: bool
    latency_seconds: float
    cost_usd: Optional[float]
    data_classification: str
    error_type: Optional[str] = None
    task_type: str = "general"
    toolset: Tuple[str, ...] = ()
    model_family: str = "default"

    @property
    def context_key(self) -> str:
        return route_context_key(self.task_type, self.toolset, self.model_family)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RouteObservation":
        return cls(
            observed_at=float(value["observed_at"]),
            task_id=str(value["task_id"]),
            executor_id=str(value["executor_id"]),
            provider=str(value["provider"]),
            success=bool(value["success"]),
            latency_seconds=float(value["latency_seconds"]),
            cost_usd=None if value.get("cost_usd") is None else float(value["cost_usd"]),
            data_classification=str(value["data_classification"]),
            error_type=None if value.get("error_type") is None else str(value["error_type"]),
            task_type=str(value.get("task_type", "general")),
            toolset=tuple(str(item) for item in value.get("toolset", ())),
            model_family=str(value.get("model_family", "default")),
        )


class PolicyRouter:
    """Selects one executor; retry and fallback remain graph-level decisions."""

    def __init__(
        self,
        provider_policies: Optional[Mapping[str, ProviderPolicy]] = None,
        clock: Callable[[], float] = time.monotonic,
        observer: Optional[Callable[[RouteObservation], None]] = None,
        wall_clock: Callable[[], float] = time.time,
        governance: Optional[ProviderGovernanceStore] = None,
    ):
        self._profiles: Dict[str, ExecutorProfile] = {}
        self._policies = dict(provider_policies or {})
        self._wall_clock = wall_clock
        self._observer = observer
        self._governance = governance or ProviderGovernanceStore(clock=clock)
        self._learned_policy: Optional[LearnedRoutingPolicy] = None
        self._lock = threading.Lock()

    def register(self, executor_id: str, profile: ExecutorProfile) -> None:
        if not isinstance(executor_id, str) or not executor_id:
            raise ContractViolation("routing executor_id cannot be empty")
        if executor_id in self._profiles:
            raise ContractViolation(f"routing profile for {executor_id} is already registered")
        self._profiles[executor_id] = profile

    def profiles(self) -> Mapping[str, ExecutorProfile]:
        return dict(self._profiles)

    def apply_policy(self, policy: Optional[LearnedRoutingPolicy]) -> None:
        with self._lock:
            self._learned_policy = policy

    def select(
        self, executor_ids: Sequence[str], request: Any, reserve: bool = True
    ) -> RouteDecision:
        eligible = []
        rejected = []
        with self._lock:
            for executor_id in sorted(executor_ids):
                profile = self._profiles[executor_id]
                reason = self._rejection_reason(profile, request)
                if reason is not None:
                    rejected.append(f"{executor_id}:{reason}")
                    continue
                quality, cost, latency, quality_score = self._learned_rank(
                    executor_id, profile, request
                )
                eligible.append(
                    (
                        quality,
                        cost,
                        latency,
                        quality_score,
                        executor_id,
                        profile,
                    )
                )
            if not reserve and eligible:
                _, cost, latency, _, executor_id, profile = min(eligible)
                return RouteDecision(executor_id, profile.provider, cost, latency)
            unavailable = set()
            while eligible:
                available = []
                for item in eligible:
                    _, _, _, _, executor_id, profile = item
                    if profile.provider in unavailable:
                        continue
                    policy = self._policy(profile.provider)
                    admission = self._governance.admit(profile.provider, policy)
                    if not admission.allowed:
                        rejected.append(f"{executor_id}:{admission.reason}")
                        unavailable.add(profile.provider)
                        continue
                    quality, cost, latency, quality_score, _, _ = item
                    available.append(
                        (
                            quality,
                            (
                                -1
                                if admission.probe_required
                                else admission.consecutive_failures
                            ),
                            cost,
                            latency,
                            quality_score,
                            executor_id,
                            profile,
                        )
                    )
                if not available:
                    break
                _, _, cost, latency, _, executor_id, profile = min(available)
                admission = self._governance.admit(
                    profile.provider, self._policy(profile.provider), reserve=True
                )
                if admission.allowed:
                    return RouteDecision(
                        executor_id,
                        profile.provider,
                        cost,
                        latency,
                        admission.reservation_token,
                    )
                rejected.append(f"{executor_id}:{admission.reason}")
                unavailable.add(profile.provider)
            detail = ", ".join(rejected) or "no capability candidates"
            raise ContractViolation(
                f"no healthy agent executor satisfies routing policy: {detail}"
            )

    def reserve(self, decision: RouteDecision, request: Any) -> RouteDecision:
        with self._lock:
            profile = self._profiles.get(decision.executor_id)
            if profile is None or profile.provider != decision.provider:
                raise ContractViolation("routing decision is not registered")
            reason = self._rejection_reason(profile, request)
            if reason is not None:
                raise ContractViolation(
                    "selected agent executor is no longer available: "
                    f"{decision.executor_id}:{reason}"
                )
            admission = self._governance.admit(
                profile.provider, self._policy(profile.provider), reserve=True
            )
            if not admission.allowed:
                raise ContractViolation(
                    "selected agent executor is no longer available: "
                    f"{decision.executor_id}:{admission.reason}"
                )
            return RouteDecision(
                decision.executor_id,
                decision.provider,
                decision.estimated_cost_usd,
                decision.estimated_latency_seconds,
                admission.reservation_token,
            )

    def record_success(
        self,
        decision: RouteDecision,
        request: Optional[Any] = None,
        latency_seconds: float = 0.0,
        cost_usd: Optional[float] = None,
    ) -> None:
        self._governance.record_success(
            decision.provider, decision.governance_token
        )
        self._observe(decision, request, True, latency_seconds, cost_usd, None)

    def record_failure(
        self,
        decision: RouteDecision,
        request: Optional[Any] = None,
        latency_seconds: float = 0.0,
        error_type: Optional[str] = None,
    ) -> None:
        self._governance.record_failure(
            decision.provider,
            self._policy(decision.provider),
            decision.governance_token,
        )
        self._observe(decision, request, False, latency_seconds, None, error_type)

    def governance_status(self) -> Mapping[str, Any]:
        return self._governance.status()

    def _learned_rank(
        self, executor_id: str, profile: ExecutorProfile, request: Any
    ) -> Tuple[int, float, float, float]:
        policy = self._learned_policy
        if policy is None or not self._in_rollout(policy, request.task_id):
            return 0, profile.estimated_cost_usd, profile.estimated_latency_seconds, 0.0
        context_estimate = policy.conditioned_estimates.get(
            route_context_key(request.task_type, request.tools, request.model_family), {}
        ).get(executor_id)
        estimate = (
            context_estimate
            if context_estimate is not None
            and context_estimate.samples >= policy.min_samples
            else policy.estimates.get(executor_id)
        )
        if estimate is None or estimate.samples < policy.min_samples:
            return 1, profile.estimated_cost_usd, profile.estimated_latency_seconds, 0.0
        meets_quality = (
            estimate.average_quality is not None
            and estimate.quality_samples >= policy.min_quality_samples
            and estimate.average_quality >= policy.min_quality_score
        )
        quality = (
            0
            if estimate.success_rate >= policy.min_success_rate and meets_quality
            else 2
        )
        quality_score = -(estimate.average_quality if estimate.average_quality is not None else 0.5)
        cost = (
            profile.estimated_cost_usd
            if estimate.average_cost_usd is None
            else estimate.average_cost_usd
        )
        return quality, cost, estimate.average_latency_seconds, quality_score

    @staticmethod
    def _in_rollout(policy: LearnedRoutingPolicy, task_id: str) -> bool:
        digest = hashlib.sha256(
            f"{policy.version}:{task_id}".encode()
        ).digest()
        bucket = int.from_bytes(digest[:4], "big") % 100
        return bucket < policy.rollout_percent

    def _observe(
        self,
        decision: RouteDecision,
        request: Optional[Any],
        success: bool,
        latency_seconds: float,
        cost_usd: Optional[float],
        error_type: Optional[str],
    ) -> None:
        if self._observer is None or request is None:
            return
        observation = RouteObservation(
            observed_at=self._wall_clock(),
            task_id=request.task_id,
            executor_id=decision.executor_id,
            provider=decision.provider,
            success=success,
            latency_seconds=max(0.0, float(latency_seconds)),
            cost_usd=cost_usd,
            data_classification=request.data_classification,
            error_type=error_type,
            task_type=request.task_type,
            toolset=tuple(sorted(set(request.tools))),
            model_family=request.model_family,
        )
        try:
            self._observer(observation)
        except Exception:
            # Telemetry must never turn a completed agent call into a failed task.
            return

    def _rejection_reason(
        self,
        profile: ExecutorProfile,
        request: Any,
    ) -> Optional[str]:
        if request.data_classification not in profile.data_classifications:
            return f"data={request.data_classification}"
        if (
            request.max_cost_usd is not None
            and profile.estimated_cost_usd > request.max_cost_usd
        ):
            return "estimated_cost"
        if profile.estimated_latency_seconds > request.timeout_seconds:
            return "estimated_latency"
        return None

    def _policy(self, provider: str) -> ProviderPolicy:
        return self._policies.get(provider, ProviderPolicy())
