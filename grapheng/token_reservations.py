import math
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

from .errors import ContractViolation
from .model import NodeSpec
from .routing import RouteObservation


@dataclass(frozen=True)
class TokenReservation:
    """An admission estimate that never replaces a node's hard token limit."""

    tokens: int
    source: str
    samples: int
    hard_limit: Optional[int]
    percentile: Optional[float] = None


@dataclass(frozen=True)
class TokenReservationWarning:
    code: str
    node_id: str
    reserved_tokens: int
    actual_tokens: int
    consecutive_deviations: int
    action: str


class HistoricalTokenReservations:
    """Plans token admission from prompt-free, provider-measured history.

    The planner is deliberately advisory: GraphRuntime still accounts the
    provider's actual usage and enforces NodeSpec.max_tokens after execution.
    """

    def __init__(
        self,
        observations: Sequence[RouteObservation],
        *,
        min_samples: int = 5,
        percentile: float = 0.95,
        max_samples: int = 100,
        deviation_streak: int = 3,
    ):
        if (
            isinstance(min_samples, bool)
            or not isinstance(min_samples, int)
            or min_samples < 1
        ):
            raise ContractViolation("token reservation min_samples must be positive")
        if (
            isinstance(percentile, bool)
            or not isinstance(percentile, (int, float))
            or not math.isfinite(percentile)
            or not 0 < percentile <= 1
        ):
            raise ContractViolation(
                "token reservation percentile must be between zero and one"
            )
        if (
            isinstance(max_samples, bool)
            or not isinstance(max_samples, int)
            or max_samples < min_samples
        ):
            raise ContractViolation(
                "token reservation max_samples must be at least min_samples"
            )
        if (
            isinstance(deviation_streak, bool)
            or not isinstance(deviation_streak, int)
            or deviation_streak < 2
        ):
            raise ContractViolation(
                "token reservation deviation_streak must be at least two"
            )
        self._observations = tuple(observations)
        self.min_samples = min_samples
        self.percentile = float(percentile)
        self.max_samples = max_samples
        self.deviation_streak = deviation_streak
        self._recent_actuals: Dict[
            Tuple[str, Tuple[str, ...], str, Optional[str]], list[int]
        ] = {}
        self._lock = threading.Lock()

    def reserve(self, node: NodeSpec) -> TokenReservation:
        if node.agent is None:
            return TokenReservation(
                tokens=node.estimated_tokens,
                source="declared_estimate",
                samples=0,
                hard_limit=node.max_tokens,
            )
        samples = self._samples(node)
        if len(samples) < self.min_samples:
            upper_bound = (
                node.max_tokens
                if node.max_tokens is not None
                else max((node.estimated_tokens, *samples))
            )
            return TokenReservation(
                tokens=upper_bound,
                source="conservative_upper_bound",
                samples=len(samples),
                hard_limit=node.max_tokens,
            )
        ordered = sorted(samples)
        rank = max(1, math.ceil(self.percentile * len(ordered)))
        predicted = ordered[rank - 1]
        if node.max_tokens is not None:
            predicted = min(predicted, node.max_tokens)
        return TokenReservation(
            tokens=predicted,
            source="historical_p95",
            samples=len(samples),
            hard_limit=node.max_tokens,
            percentile=self.percentile,
        )

    def deviation_warning(
        self,
        node: NodeSpec,
        reservation: TokenReservation,
        actual_tokens: int,
        recent_actuals: Optional[Sequence[int]] = None,
    ) -> Optional[TokenReservationWarning]:
        if node.agent is None or reservation.source != "historical_p95":
            return None
        if (
            isinstance(actual_tokens, bool)
            or not isinstance(actual_tokens, int)
            or actual_tokens < 0
        ):
            raise ContractViolation(
                "token reservation actual_tokens must be a non-negative integer"
            )
        key = self._key(node)
        with self._lock:
            if recent_actuals is None:
                recent = self._recent_actuals.setdefault(key, [])
                recent.append(actual_tokens)
                del recent[: -self.deviation_streak]
                window: Tuple[int, ...] = tuple(recent)
            else:
                window = (
                    *tuple(recent_actuals)[-(self.deviation_streak - 1) :],
                    actual_tokens,
                )
        if len(window) < self.deviation_streak or any(
            value <= reservation.tokens for value in window
        ):
            return None
        return TokenReservationWarning(
            code="token_reservation_underpredicted",
            node_id=node.id,
            reserved_tokens=reservation.tokens,
            actual_tokens=actual_tokens,
            consecutive_deviations=self.deviation_streak,
            action=(
                "review task grouping or reservation history; keep the graph and "
                "node token budgets unchanged"
            ),
        )

    def _samples(self, node: NodeSpec) -> Tuple[int, ...]:
        assert node.agent is not None
        matching = [
            item
            for item in self._observations
            if item.total_tokens_complete
            and item.total_tokens is not None
            and item.task_type == node.agent.task_type
            and tuple(sorted(set(item.toolset)))
            == tuple(sorted(set(node.agent.tools)))
            and item.model_family == node.agent.model_family
            and (
                node.agent.executor is None
                or item.executor_id == node.agent.executor
            )
        ]
        matching.sort(key=lambda item: item.observed_at)
        values = []
        for item in matching[-self.max_samples :]:
            assert item.total_tokens is not None
            values.append(item.total_tokens)
        return tuple(values)

    @staticmethod
    def _key(node: NodeSpec) -> Tuple[str, Tuple[str, ...], str, Optional[str]]:
        assert node.agent is not None
        return (
            node.agent.task_type,
            tuple(sorted(set(node.agent.tools))),
            node.agent.model_family,
            node.agent.executor,
        )
