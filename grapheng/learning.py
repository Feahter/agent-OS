import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .errors import ContractViolation
from .governance import ProviderGovernanceStore
from .routing import (
    LearnedExecutorEstimate,
    LearnedRoutingPolicy,
    PolicyRouter,
    ProviderPolicy,
    RouteObservation,
)


@dataclass(frozen=True)
class RSICandidate:
    candidate_id: str
    created_at: float
    status: str
    observation_count: int
    policy: LearnedRoutingPolicy
    evaluation: Optional[Mapping[str, Any]] = None
    approved_by: Optional[str] = None
    approved_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["policy"] = self.policy.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RSICandidate":
        return cls(
            candidate_id=str(value["candidate_id"]),
            created_at=float(value["created_at"]),
            status=str(value["status"]),
            observation_count=int(value["observation_count"]),
            policy=LearnedRoutingPolicy.from_dict(value["policy"]),
            evaluation=value.get("evaluation"),
            approved_by=value.get("approved_by"),
            approved_at=(
                None if value.get("approved_at") is None else float(value["approved_at"])
            ),
        )


@dataclass(frozen=True)
class QualityFeedback:
    observed_at: float
    task_id: str
    score: float
    source: str

    def __post_init__(self) -> None:
        if not self.task_id:
            raise ContractViolation("quality feedback task_id cannot be empty")
        if (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
            or not 0 <= self.score <= 1
        ):
            raise ContractViolation("quality feedback score must be between zero and one")
        if not self.source.strip():
            raise ContractViolation("quality feedback source cannot be empty")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QualityFeedback":
        return cls(
            observed_at=float(value["observed_at"]),
            task_id=str(value["task_id"]),
            score=float(value["score"]),
            source=str(value["source"]),
        )


class ObservationJournal:
    """Append-only, prompt-free routing telemetry for the learning loop."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, observation: RouteObservation) -> None:
        line = json.dumps(
            observation.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def read(self) -> Tuple[RouteObservation, ...]:
        if not self.path.exists():
            return ()
        try:
            with self.path.open(encoding="utf-8") as handle:
                return tuple(
                    RouteObservation.from_dict(json.loads(line))
                    for line in handle
                    if line.strip()
                )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ContractViolation(f"invalid observation journal: {error}") from error


class FeedbackJournal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, feedback: QualityFeedback) -> None:
        line = json.dumps(
            feedback.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def read(self) -> Tuple[QualityFeedback, ...]:
        if not self.path.exists():
            return ()
        try:
            with self.path.open(encoding="utf-8") as handle:
                return tuple(
                    QualityFeedback.from_dict(json.loads(line))
                    for line in handle
                    if line.strip()
                )
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ContractViolation(f"invalid quality feedback journal: {error}") from error


class RSILoop:
    """Versioned observe/evaluate/approve/activate/rollback learning loop."""

    def __init__(self, root: Path, clock=time.time):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal = ObservationJournal(root / "observations.jsonl")
        self.feedback_journal = FeedbackJournal(root / "quality-feedback.jsonl")
        self._clock = clock
        self._lock = threading.Lock()

    def record(self, observation: RouteObservation) -> None:
        self.journal.record(observation)

    def feedback(self, task_id: str, score: float, source: str) -> QualityFeedback:
        item = QualityFeedback(self._clock(), task_id, score, source)
        self.feedback_journal.record(item)
        return item

    def propose(
        self,
        min_observations: int = 6,
        min_samples: int = 3,
        min_success_rate: float = 0.8,
        min_quality_score: float = 0.8,
        min_quality_samples: Optional[int] = None,
        rollout_percent: int = 10,
    ) -> RSICandidate:
        if (
            isinstance(min_observations, bool)
            or not isinstance(min_observations, int)
            or min_observations < 1
        ):
            raise ContractViolation("RSI min_observations must be a positive integer")
        observations = self.journal.read()
        if len(observations) < min_observations:
            raise ContractViolation(
                f"RSI needs at least {min_observations} observations; got {len(observations)}"
            )
        feedback_by_task = {
            item.task_id: item for item in self.feedback_journal.read()
        }
        grouped: Dict[str, list] = {}
        conditioned_groups: Dict[str, Dict[str, list]] = {}
        for observation in observations:
            grouped.setdefault(observation.executor_id, []).append(observation)
            conditioned_groups.setdefault(observation.context_key, {}).setdefault(
                observation.executor_id, []
            ).append(observation)

        def estimate(items: list) -> LearnedExecutorEstimate:
            costs = [item.cost_usd for item in items if item.cost_usd is not None]
            quality = [
                feedback_by_task[item.task_id].score
                for item in items
                if item.task_id in feedback_by_task
            ]
            return LearnedExecutorEstimate(
                samples=len(items),
                success_rate=sum(1 for item in items if item.success) / len(items),
                average_cost_usd=(sum(costs) / len(costs) if costs else None),
                average_latency_seconds=(
                    sum(item.latency_seconds for item in items) / len(items)
                ),
                average_quality=sum(quality) / len(quality) if quality else None,
                quality_samples=len(quality),
            )

        estimates = {
            executor_id: estimate(items)
            for executor_id, items in sorted(grouped.items())
        }
        conditioned_estimates = {
            context: {
                executor_id: estimate(items)
                for executor_id, items in sorted(executors.items())
            }
            for context, executors in sorted(conditioned_groups.items())
        }
        candidate_id = f"rsi-{uuid.uuid4()}"
        policy = LearnedRoutingPolicy(
            version=candidate_id,
            estimates=estimates,
            conditioned_estimates=conditioned_estimates,
            min_samples=min_samples,
            min_success_rate=min_success_rate,
            min_quality_score=min_quality_score,
            min_quality_samples=(
                min_samples if min_quality_samples is None else min_quality_samples
            ),
            rollout_percent=rollout_percent,
        )
        candidate = RSICandidate(
            candidate_id,
            self._clock(),
            "proposed",
            len(observations),
            policy,
        )
        self._write_candidate(candidate)
        return candidate

    def evaluate(self, candidate_id: str) -> RSICandidate:
        with self._lock:
            candidate = self._read_candidate(candidate_id)
            if candidate.status != "proposed":
                raise ContractViolation(
                    f"candidate {candidate_id} must be proposed before evaluation"
                )
            observations = self.journal.read()[: candidate.observation_count]
            reliable = {
                key: estimate
                for key, estimate in candidate.policy.estimates.items()
                if estimate.samples >= candidate.policy.min_samples
                and estimate.success_rate >= candidate.policy.min_success_rate
                and (
                    estimate.average_quality is not None
                    and estimate.quality_samples
                    >= candidate.policy.min_quality_samples
                    and estimate.average_quality >= candidate.policy.min_quality_score
                )
            }
            preferred_id = None
            if reliable:
                preferred_id = min(
                    reliable,
                    key=lambda key: (
                        float("inf")
                        if reliable[key].average_cost_usd is None
                        else reliable[key].average_cost_usd,
                        reliable[key].average_latency_seconds,
                        -(
                            reliable[key].average_quality
                            if reliable[key].average_quality is not None
                            else 0.5
                        ),
                        key,
                    ),
                )
            context_preferences = {}
            for context, estimates in sorted(
                candidate.policy.conditioned_estimates.items()
            ):
                context_reliable = {
                    key: estimate
                    for key, estimate in estimates.items()
                    if estimate.samples >= candidate.policy.min_samples
                    and estimate.success_rate >= candidate.policy.min_success_rate
                    and (
                        estimate.average_quality is not None
                        and estimate.quality_samples
                        >= candidate.policy.min_quality_samples
                        and estimate.average_quality
                        >= candidate.policy.min_quality_score
                    )
                }
                if context_reliable:
                    context_preferences[context] = min(
                        context_reliable,
                        key=lambda key: (
                            float("inf")
                            if context_reliable[key].average_cost_usd is None
                            else context_reliable[key].average_cost_usd,
                            context_reliable[key].average_latency_seconds,
                            -(
                                context_reliable[key].average_quality
                                if context_reliable[key].average_quality is not None
                                else 0.5
                            ),
                            key,
                        ),
                    )
            known_costs = [item.cost_usd for item in observations if item.cost_usd is not None]
            average_cost = sum(known_costs) / len(known_costs) if known_costs else None
            average_latency = (
                sum(item.latency_seconds for item in observations) / len(observations)
                if observations
                else None
            )
            preferred = reliable.get(preferred_id) if preferred_id is not None else None
            projected_cost = preferred.average_cost_usd if preferred is not None else None
            projected_latency = (
                preferred.average_latency_seconds if preferred is not None else None
            )
            evaluation = {
                "passed": bool(reliable),
                "observations": len(observations),
                "reliable_executors": sorted(reliable),
                "preferred_executor": preferred_id,
                "conditioned_contexts": len(
                    candidate.policy.conditioned_estimates
                ),
                "conditioned_preferences": context_preferences,
                "conditioned_coverage_percent": (
                    len(context_preferences)
                    / len(candidate.policy.conditioned_estimates)
                    * 100.0
                    if candidate.policy.conditioned_estimates
                    else 0.0
                ),
                "quality_feedback": len(self.feedback_journal.read()),
                "quality_feedback_coverage_percent": (
                    sum(
                        estimate.quality_samples
                        for estimate in candidate.policy.estimates.values()
                    )
                    / len(observations)
                    * 100.0
                    if observations
                    else 0.0
                ),
                "baseline_average_cost_usd": average_cost,
                "projected_cost_usd": projected_cost,
                "cost_saving_opportunity_percent": _improvement(average_cost, projected_cost),
                "baseline_average_latency_seconds": average_latency,
                "projected_latency_seconds": projected_latency,
                "latency_improvement_opportunity_percent": _improvement(
                    average_latency, projected_latency
                ),
                "guardrails": {
                    "data_permissions_mutable": False,
                    "budget_caps_mutable": False,
                    "provider_limits_mutable": False,
                    "quality_gate_mutable_at_runtime": False,
                    "human_approval_required": True,
                },
            }
            updated = replace(candidate, status="evaluated", evaluation=evaluation)
            self._write_candidate(updated)
            return updated

    def approve(self, candidate_id: str, actor: str) -> RSICandidate:
        if not isinstance(actor, str) or not actor.strip():
            raise ContractViolation("RSI approval actor cannot be empty")
        with self._lock:
            candidate = self._read_candidate(candidate_id)
            if candidate.status != "evaluated" or not candidate.evaluation:
                raise ContractViolation(f"candidate {candidate_id} is not evaluated")
            if not candidate.evaluation.get("passed"):
                raise ContractViolation(f"candidate {candidate_id} did not pass evaluation")
            updated = replace(
                candidate,
                status="approved",
                approved_by=actor.strip(),
                approved_at=self._clock(),
            )
            self._write_candidate(updated)
            return updated

    def activate(self, candidate_id: str) -> LearnedRoutingPolicy:
        with self._lock:
            candidate = self._read_candidate(candidate_id)
            if candidate.status != "approved":
                raise ContractViolation(f"candidate {candidate_id} is not approved")
            active = self._read_json(self.root / "active.json", default=None)
            history = self._read_json(self.root / "history.json", default=[])
            if active is not None:
                history.append(active)
            _atomic_json_write(self.root / "history.json", history)
            _atomic_json_write(
                self.root / "active.json",
                {"candidate_id": candidate_id, "policy": candidate.policy.to_dict()},
            )
            self._write_candidate(replace(candidate, status="active"))
            return candidate.policy

    def rollback(self) -> Optional[LearnedRoutingPolicy]:
        with self._lock:
            active = self._read_json(self.root / "active.json", default=None)
            if active is None:
                raise ContractViolation("there is no active RSI policy to roll back")
            history = self._read_json(self.root / "history.json", default=[])
            current = self._read_candidate(str(active["candidate_id"]))
            self._write_candidate(replace(current, status="rolled_back"))
            previous = history.pop() if history else None
            _atomic_json_write(self.root / "history.json", history)
            _atomic_json_write(self.root / "active.json", previous)
            if previous is None:
                return None
            previous_candidate = self._read_candidate(str(previous["candidate_id"]))
            self._write_candidate(replace(previous_candidate, status="active"))
            return LearnedRoutingPolicy.from_dict(previous["policy"])

    def active_policy(self) -> Optional[LearnedRoutingPolicy]:
        active = self._read_json(self.root / "active.json", default=None)
        if active is None:
            return None
        return LearnedRoutingPolicy.from_dict(active["policy"])

    def apply(self, router: PolicyRouter) -> Optional[LearnedRoutingPolicy]:
        policy = self.active_policy()
        router.apply_policy(policy)
        return policy

    def router(
        self,
        provider_policies: Optional[Mapping[str, ProviderPolicy]] = None,
        governance: Optional[ProviderGovernanceStore] = None,
    ) -> PolicyRouter:
        router = PolicyRouter(
            provider_policies, observer=self.record, governance=governance
        )
        self.apply(router)
        return router

    def candidates(self) -> Tuple[RSICandidate, ...]:
        directory = self.root / "candidates"
        if not directory.exists():
            return ()
        return tuple(
            self._read_candidate(path.stem)
            for path in sorted(directory.glob("*.json"))
        )

    def _candidate_path(self, candidate_id: str) -> Path:
        if not candidate_id.startswith("rsi-") or "/" in candidate_id:
            raise ContractViolation("invalid RSI candidate id")
        return self.root / "candidates" / f"{candidate_id}.json"

    def _write_candidate(self, candidate: RSICandidate) -> None:
        _atomic_json_write(self._candidate_path(candidate.candidate_id), candidate.to_dict())

    def _read_candidate(self, candidate_id: str) -> RSICandidate:
        value = self._read_json(self._candidate_path(candidate_id), default=None)
        if value is None:
            raise ContractViolation(f"RSI candidate {candidate_id} does not exist")
        return RSICandidate.from_dict(value)

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(f"invalid RSI state {path.name}: {error}") from error


def _improvement(baseline: Optional[float], projected: Optional[float]) -> Optional[float]:
    if baseline is None or projected is None or baseline <= 0:
        return None
    return max(-100.0, min(100.0, (baseline - projected) / baseline * 100.0))


def _atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)
