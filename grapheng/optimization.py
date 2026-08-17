import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .errors import ContractViolation
from .model import GraphSpec
from .validation import validate_graph


IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
OPTIMIZATION_KINDS = ("prompt_template", "graph_topology")
PROMPT_FAILURE_GUIDANCE = {
    "missing_evidence": "Cite the evidence used for every conclusion.",
    "format_mismatch": "Validate the final output against the declared output contract.",
    "verification_failure": "Check each claim against the available Reality Anchor before finalizing.",
}


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ContractViolation(f"{field} must be a valid identifier")
    return value


def _finite_non_negative(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ContractViolation(f"{field} must be a finite non-negative number")
    return float(value)


def _quality(value: Any, field: str) -> float:
    result = _finite_non_negative(value, field)
    if result > 1:
        raise ContractViolation(f"{field} must be between zero and one")
    return result


@dataclass(frozen=True)
class RegressionCase:
    case_id: str
    fixture_digest: str
    task_type: str
    baseline_quality: float
    baseline_cost_usd: float
    baseline_latency_seconds: float

    def __post_init__(self) -> None:
        _identifier(self.case_id, "regression case_id")
        _identifier(self.task_type, "regression task_type")
        if (
            not isinstance(self.fixture_digest, str)
            or len(self.fixture_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.fixture_digest)
        ):
            raise ContractViolation(
                "regression fixture_digest must be a lowercase SHA-256 digest"
            )
        _quality(self.baseline_quality, "regression baseline_quality")
        _finite_non_negative(
            self.baseline_cost_usd, "regression baseline_cost_usd"
        )
        _finite_non_negative(
            self.baseline_latency_seconds,
            "regression baseline_latency_seconds",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "fixture_digest": self.fixture_digest,
            "task_type": self.task_type,
            "baseline_quality": float(self.baseline_quality),
            "baseline_cost_usd": float(self.baseline_cost_usd),
            "baseline_latency_seconds": float(
                self.baseline_latency_seconds
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RegressionCase":
        return cls(
            case_id=str(value["case_id"]),
            fixture_digest=str(value["fixture_digest"]),
            task_type=str(value["task_type"]),
            baseline_quality=float(value["baseline_quality"]),
            baseline_cost_usd=float(value["baseline_cost_usd"]),
            baseline_latency_seconds=float(value["baseline_latency_seconds"]),
        )


@dataclass(frozen=True)
class FailurePattern:
    task_type: str
    failure_code: str
    occurrences: int
    node_id: Optional[str] = None
    depends_on: Optional[str] = None

    def __post_init__(self) -> None:
        _identifier(self.task_type, "failure pattern task_type")
        allowed = tuple(PROMPT_FAILURE_GUIDANCE) + ("race_condition",)
        if self.failure_code not in allowed:
            raise ContractViolation(
                "failure_code must be missing_evidence, format_mismatch, "
                "verification_failure, or race_condition"
            )
        if (
            isinstance(self.occurrences, bool)
            or not isinstance(self.occurrences, int)
            or self.occurrences < 1
        ):
            raise ContractViolation("failure occurrences must be positive")
        if self.failure_code == "race_condition":
            _identifier(self.node_id, "race node_id")
            _identifier(self.depends_on, "race depends_on")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FailurePattern":
        return cls(
            task_type=str(value["task_type"]),
            failure_code=str(value["failure_code"]),
            occurrences=int(value["occurrences"]),
            node_id=(
                None if value.get("node_id") is None else str(value["node_id"])
            ),
            depends_on=(
                None
                if value.get("depends_on") is None
                else str(value["depends_on"])
            ),
        )


@dataclass(frozen=True)
class RegressionSuite:
    suite_id: str
    created_at: float
    digest: str
    cases: Tuple[RegressionCase, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "created_at": self.created_at,
            "digest": self.digest,
            "cases": [item.to_dict() for item in self.cases],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RegressionSuite":
        return cls(
            suite_id=str(value["suite_id"]),
            created_at=float(value["created_at"]),
            digest=str(value["digest"]),
            cases=tuple(RegressionCase.from_dict(item) for item in value["cases"]),
        )


@dataclass(frozen=True)
class RegressionMeasurement:
    case_id: str
    quality_score: float
    cost_usd: float
    latency_seconds: float
    reality_anchor_passed: bool

    def __post_init__(self) -> None:
        _identifier(self.case_id, "regression measurement case_id")
        _quality(self.quality_score, "regression quality_score")
        _finite_non_negative(self.cost_usd, "regression cost_usd")
        _finite_non_negative(self.latency_seconds, "regression latency_seconds")
        if not isinstance(self.reality_anchor_passed, bool):
            raise ContractViolation("reality_anchor_passed must be a boolean")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RegressionMeasurement":
        return cls(
            case_id=str(value["case_id"]),
            quality_score=float(value["quality_score"]),
            cost_usd=float(value["cost_usd"]),
            latency_seconds=float(value["latency_seconds"]),
            reality_anchor_passed=value["reality_anchor_passed"],
        )


@dataclass(frozen=True)
class OptimizationCandidate:
    candidate_id: str
    created_at: float
    kind: str
    status: str
    change: Mapping[str, Any]
    rationale: str
    rollout_percent: int = 10
    evaluation: Optional[Mapping[str, Any]] = None
    approved_by: Optional[str] = None
    approved_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OptimizationCandidate":
        return cls(
            candidate_id=str(value["candidate_id"]),
            created_at=float(value["created_at"]),
            kind=str(value["kind"]),
            status=str(value["status"]),
            change=dict(value["change"]),
            rationale=str(value["rationale"]),
            rollout_percent=int(value.get("rollout_percent", 10)),
            evaluation=value.get("evaluation"),
            approved_by=value.get("approved_by"),
            approved_at=(
                None
                if value.get("approved_at") is None
                else float(value["approved_at"])
            ),
        )


@dataclass(frozen=True)
class CanaryObservation:
    candidate_id: str
    observed_at: float
    reality_anchor_passed: bool
    quality_score: float
    baseline_quality: float
    cost_usd: float
    baseline_cost_usd: float
    latency_seconds: float
    baseline_latency_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.reality_anchor_passed, bool):
            raise ContractViolation("canary reality_anchor_passed must be a boolean")
        for field, value in (
            ("quality_score", self.quality_score),
            ("baseline_quality", self.baseline_quality),
        ):
            _quality(value, f"canary {field}")
        for field, value in (
            ("cost_usd", self.cost_usd),
            ("baseline_cost_usd", self.baseline_cost_usd),
            ("latency_seconds", self.latency_seconds),
            ("baseline_latency_seconds", self.baseline_latency_seconds),
        ):
            _finite_non_negative(value, f"canary {field}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RSIOptimizationLab:
    """Governed prompt/topology candidates behind one persistent interface."""

    def __init__(self, root: Path, clock=time.time):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()

    def freeze_suite(self, cases: Sequence[RegressionCase]) -> RegressionSuite:
        if not cases:
            raise ContractViolation("regression suite cannot be empty")
        ordered = tuple(sorted(cases, key=lambda item: item.case_id))
        if len({item.case_id for item in ordered}) != len(ordered):
            raise ContractViolation("regression suite case ids must be unique")
        canonical = json.dumps(
            [item.to_dict() for item in ordered],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        suite = RegressionSuite(
            f"suite-{digest[:24]}", self._clock(), digest, ordered
        )
        path = self.root / "suites" / f"{suite.suite_id}.json"
        with self._lock:
            existing = self._read_json(path, None)
            if existing is not None:
                restored = RegressionSuite.from_dict(existing)
                if restored.digest != digest:
                    raise ContractViolation("frozen regression suite digest mismatch")
                return restored
            _atomic_json_write(path, suite.to_dict())
        return suite

    def propose(
        self,
        kind: str,
        change: Mapping[str, Any],
        rationale: str,
        rollout_percent: int = 10,
    ) -> OptimizationCandidate:
        if kind not in OPTIMIZATION_KINDS:
            raise ContractViolation(
                "optimization kind must be prompt_template or graph_topology"
            )
        if not isinstance(rationale, str) or not rationale.strip():
            raise ContractViolation("optimization rationale cannot be empty")
        if (
            isinstance(rollout_percent, bool)
            or not isinstance(rollout_percent, int)
            or not 1 <= rollout_percent <= 100
        ):
            raise ContractViolation(
                "optimization rollout_percent must be between 1 and 100"
            )
        normalized = self._validate_change(kind, change)
        candidate = OptimizationCandidate(
            f"opt-{uuid.uuid4()}",
            self._clock(),
            kind,
            "proposed",
            normalized,
            rationale.strip(),
            rollout_percent,
        )
        self._write_candidate(candidate)
        return candidate

    def propose_from_failures(
        self,
        patterns: Sequence[FailurePattern],
        min_occurrences: int = 3,
        rollout_percent: int = 10,
    ) -> Tuple[OptimizationCandidate, ...]:
        if (
            isinstance(min_occurrences, bool)
            or not isinstance(min_occurrences, int)
            or min_occurrences < 1
        ):
            raise ContractViolation("failure min_occurrences must be positive")
        eligible = [
            item for item in patterns if item.occurrences >= min_occurrences
        ]
        prompt_groups: Dict[str, Dict[str, int]] = {}
        dependencies: Dict[str, Tuple[str, int]] = {}
        for item in eligible:
            if item.failure_code in PROMPT_FAILURE_GUIDANCE:
                codes = prompt_groups.setdefault(item.task_type, {})
                codes[item.failure_code] = codes.get(item.failure_code, 0) + item.occurrences
            else:
                if item.node_id is None or item.depends_on is None:
                    raise ContractViolation(
                        "race condition requires node_id and depends_on"
                    )
                previous = dependencies.get(item.node_id)
                if previous is not None and previous[0] != item.depends_on:
                    raise ContractViolation(
                        f"conflicting learned dependencies for node {item.node_id}"
                    )
                dependencies[item.node_id] = (
                    item.depends_on,
                    (previous[1] if previous is not None else 0)
                    + item.occurrences,
                )
        candidates = []
        for task_type, codes in sorted(prompt_groups.items()):
            guidance = " ".join(
                PROMPT_FAILURE_GUIDANCE[code] for code in sorted(codes)
            )
            evidence = ", ".join(
                f"{code}={count}" for code, count in sorted(codes.items())
            )
            candidates.append(
                self.propose(
                    "prompt_template",
                    {"task_type": task_type, "append": guidance},
                    f"Observed repeated {task_type} failures: {evidence}.",
                    rollout_percent,
                )
            )
        if dependencies:
            evidence = ", ".join(
                f"{node}->{dependency}={count}"
                for node, (dependency, count) in sorted(dependencies.items())
            )
            candidates.append(
                self.propose(
                    "graph_topology",
                    {
                        "add_dependencies": [
                            {"node": node, "depends_on": dependency}
                            for node, (dependency, _) in sorted(
                                dependencies.items()
                            )
                        ]
                    },
                    f"Observed repeated race conditions: {evidence}.",
                    rollout_percent,
                )
            )
        return tuple(candidates)

    def evaluate(
        self,
        candidate_id: str,
        suite_id: str,
        measurements: Sequence[RegressionMeasurement],
        max_quality_regression: float = 0.0,
        max_cost_increase_percent: float = 0.0,
        max_latency_increase_percent: float = 0.0,
    ) -> OptimizationCandidate:
        max_quality_regression = _quality(
            max_quality_regression, "max_quality_regression"
        )
        max_cost_increase_percent = _finite_non_negative(
            max_cost_increase_percent, "max_cost_increase_percent"
        )
        max_latency_increase_percent = _finite_non_negative(
            max_latency_increase_percent, "max_latency_increase_percent"
        )
        with self._lock:
            candidate = self._read_candidate(candidate_id)
            if candidate.status != "proposed":
                raise ContractViolation(
                    f"optimization candidate {candidate_id} must be proposed before evaluation"
                )
            suite = self._read_suite(suite_id)
            by_case = {item.case_id: item for item in measurements}
            expected = {item.case_id for item in suite.cases}
            if len(by_case) != len(measurements) or set(by_case) != expected:
                raise ContractViolation(
                    "regression measurements must cover each frozen case exactly once"
                )
            failures = []
            baseline_cost = 0.0
            measured_cost = 0.0
            baseline_latency = 0.0
            measured_latency = 0.0
            baseline_quality = 0.0
            measured_quality = 0.0
            for case in suite.cases:
                item = by_case[case.case_id]
                baseline_cost += case.baseline_cost_usd
                measured_cost += item.cost_usd
                baseline_latency += case.baseline_latency_seconds
                measured_latency += item.latency_seconds
                baseline_quality += case.baseline_quality
                measured_quality += item.quality_score
                if not item.reality_anchor_passed:
                    failures.append(f"{case.case_id}:reality_anchor_failed")
                if item.quality_score < (
                    case.baseline_quality - max_quality_regression
                ):
                    failures.append(f"{case.case_id}:quality_regression")
            cost_increase = _increase_percent(baseline_cost, measured_cost)
            latency_increase = _increase_percent(
                baseline_latency, measured_latency
            )
            if cost_increase > max_cost_increase_percent:
                failures.append("suite:cost_budget_exceeded")
            if latency_increase > max_latency_increase_percent:
                failures.append("suite:latency_budget_exceeded")
            count = len(suite.cases)
            evaluation = {
                "passed": not failures,
                "suite_id": suite.suite_id,
                "suite_digest": suite.digest,
                "candidate_change_digest": _candidate_change_digest(candidate),
                "cases": count,
                "failures": failures,
                "baseline_average_quality": baseline_quality / count,
                "measured_average_quality": measured_quality / count,
                "baseline_total_cost_usd": baseline_cost,
                "measured_total_cost_usd": measured_cost,
                "cost_increase_percent": cost_increase,
                "baseline_total_latency_seconds": baseline_latency,
                "measured_total_latency_seconds": measured_latency,
                "latency_increase_percent": latency_increase,
                "thresholds": {
                    "max_quality_regression": max_quality_regression,
                    "max_cost_increase_percent": max_cost_increase_percent,
                    "max_latency_increase_percent": max_latency_increase_percent,
                },
                "guardrails": {
                    "frozen_suite": True,
                    "hard_constraints_mutable": False,
                    "human_approval_required": True,
                },
            }
            updated = replace(
                candidate, status="evaluated", evaluation=evaluation
            )
            self._write_candidate(updated)
            return updated

    def approve(self, candidate_id: str, actor: str) -> OptimizationCandidate:
        if not isinstance(actor, str) or not actor.strip():
            raise ContractViolation("optimization approval actor cannot be empty")
        with self._lock:
            candidate = self._read_candidate(candidate_id)
            if candidate.status != "evaluated" or not candidate.evaluation:
                raise ContractViolation(
                    f"optimization candidate {candidate_id} is not evaluated"
                )
            if not candidate.evaluation.get("passed"):
                raise ContractViolation(
                    f"optimization candidate {candidate_id} did not pass evaluation"
                )
            if candidate.evaluation.get(
                "candidate_change_digest"
            ) != _candidate_change_digest(candidate):
                raise ContractViolation(
                    f"optimization candidate {candidate_id} changed after evaluation"
                )
            updated = replace(
                candidate,
                status="approved",
                approved_by=actor.strip(),
                approved_at=self._clock(),
            )
            self._write_candidate(updated)
            return updated

    def activate(self, candidate_id: str) -> OptimizationCandidate:
        with self._lock:
            candidate = self._read_candidate(candidate_id)
            if candidate.status != "approved":
                raise ContractViolation(
                    f"optimization candidate {candidate_id} is not approved"
                )
            if not candidate.evaluation or candidate.evaluation.get(
                "candidate_change_digest"
            ) != _candidate_change_digest(candidate):
                raise ContractViolation(
                    f"optimization candidate {candidate_id} changed after evaluation"
                )
            active = self._read_json(self.root / "active.json", {})
            history = self._read_json(self.root / "history.json", {})
            previous_id = active.get(candidate.kind)
            if previous_id is not None:
                history.setdefault(candidate.kind, []).append(previous_id)
                previous = self._read_candidate(str(previous_id))
                self._write_candidate(replace(previous, status="superseded"))
            active[candidate.kind] = candidate.candidate_id
            _atomic_json_write(self.root / "active.json", active)
            _atomic_json_write(self.root / "history.json", history)
            updated = replace(candidate, status="active")
            self._write_candidate(updated)
            return updated

    def rollback(self, kind: str) -> Optional[OptimizationCandidate]:
        if kind not in OPTIMIZATION_KINDS:
            raise ContractViolation("invalid optimization rollback kind")
        with self._lock:
            active = self._read_json(self.root / "active.json", {})
            candidate_id = active.get(kind)
            if candidate_id is None:
                raise ContractViolation(f"there is no active {kind} candidate")
            current = self._read_candidate(str(candidate_id))
            self._write_candidate(replace(current, status="rolled_back"))
            history = self._read_json(self.root / "history.json", {})
            values = history.get(kind, [])
            previous_id = values.pop() if values else None
            if previous_id is None:
                active.pop(kind, None)
            else:
                active[kind] = previous_id
            history[kind] = values
            _atomic_json_write(self.root / "active.json", active)
            _atomic_json_write(self.root / "history.json", history)
            if previous_id is None:
                return None
            previous = self._read_candidate(str(previous_id))
            restored = replace(previous, status="active")
            self._write_candidate(restored)
            return restored

    def active_candidates(self) -> Mapping[str, OptimizationCandidate]:
        active = self._read_json(self.root / "active.json", {})
        return {
            kind: self._read_candidate(str(candidate_id))
            for kind, candidate_id in active.items()
        }

    def candidates(self) -> Tuple[OptimizationCandidate, ...]:
        directory = self.root / "candidates"
        if not directory.exists():
            return ()
        return tuple(
            self._read_candidate(path.stem)
            for path in sorted(directory.glob("*.json"))
        )

    def apply(
        self, graph: GraphSpec, rollout_key: Optional[str] = None
    ) -> GraphSpec:
        result = graph
        key = rollout_key or graph.id
        active = self.active_candidates()
        for kind in OPTIMIZATION_KINDS:
            candidate = active.get(kind)
            if candidate is not None and self._in_rollout(candidate, key):
                result = self._apply_candidate(result, candidate)
        return result

    def observe_canary(self, observation: CanaryObservation) -> bool:
        candidate = self._read_candidate(observation.candidate_id)
        active = self.active_candidates().get(candidate.kind)
        if active is None or active.candidate_id != candidate.candidate_id:
            raise ContractViolation("canary candidate is not active")
        if not candidate.evaluation:
            raise ContractViolation("active candidate has no evaluation")
        thresholds = candidate.evaluation["thresholds"]
        quality_regression = (
            observation.baseline_quality - observation.quality_score
        )
        cost_increase = _increase_percent(
            observation.baseline_cost_usd, observation.cost_usd
        )
        latency_increase = _increase_percent(
            observation.baseline_latency_seconds,
            observation.latency_seconds,
        )
        healthy = (
            observation.reality_anchor_passed
            and quality_regression <= thresholds["max_quality_regression"]
            and cost_increase <= thresholds["max_cost_increase_percent"]
            and latency_increase
            <= thresholds["max_latency_increase_percent"]
        )
        record = observation.to_dict()
        record.update(
            {
                "healthy": healthy,
                "quality_regression": quality_regression,
                "cost_increase_percent": cost_increase,
                "latency_increase_percent": latency_increase,
            }
        )
        self._append_jsonl(self.root / "canary.jsonl", record)
        if not healthy:
            self.rollback(candidate.kind)
        return healthy

    def _apply_candidate(
        self, graph: GraphSpec, candidate: OptimizationCandidate
    ) -> GraphSpec:
        if candidate.kind == "prompt_template":
            task_type = str(candidate.change["task_type"])
            suffix = str(candidate.change["append"])
            matches = sum(
                1
                for node in graph.nodes
                if node.agent is not None
                and node.agent.task_type == task_type
            )
            if matches == 0:
                raise ContractViolation(
                    f"prompt candidate matches no task_type {task_type} nodes"
                )
            nodes = tuple(
                replace(
                    node,
                    agent=replace(
                        node.agent,
                        prompt=f"{node.agent.prompt}\n\n{suffix}",
                    ),
                )
                if node.agent is not None
                and node.agent.task_type == task_type
                else node
                for node in graph.nodes
            )
            result = replace(graph, nodes=nodes)
        else:
            dependencies = {
                str(item["node"]): str(item["depends_on"])
                for item in candidate.change.get("add_dependencies", [])
            }
            node_ids = {node.id for node in graph.nodes}
            for node_id, dependency in dependencies.items():
                if node_id not in node_ids or dependency not in node_ids:
                    raise ContractViolation(
                        "topology candidate references an unknown graph node"
                    )
                if node_id == dependency:
                    raise ContractViolation(
                        "topology candidate cannot add a self dependency"
                    )
            nodes = tuple(
                replace(
                    node,
                    deps=tuple(
                        sorted(set(node.deps) | {dependencies[node.id]})
                    ),
                )
                if node.id in dependencies
                else node
                for node in graph.nodes
            )
            max_concurrency = int(
                candidate.change.get("max_concurrency", graph.max_concurrency)
            )
            if max_concurrency > graph.max_concurrency:
                raise ContractViolation(
                    "learned topology cannot increase the graph concurrency cap"
                )
            result = replace(
                graph, nodes=nodes, max_concurrency=max_concurrency
            )
        validate_graph(result)
        return result

    @staticmethod
    def _validate_change(kind: str, change: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(change, dict):
            raise ContractViolation("optimization change must be an object")
        if kind == "prompt_template":
            if set(change) != {"task_type", "append"}:
                raise ContractViolation(
                    "prompt change must contain only task_type and append"
                )
            task_type = _identifier(change["task_type"], "prompt task_type")
            suffix = change["append"]
            if (
                not isinstance(suffix, str)
                or not suffix.strip()
                or len(suffix) > 4000
            ):
                raise ContractViolation(
                    "prompt append must be a non-empty string of at most 4000 characters"
                )
            return {"task_type": task_type, "append": suffix.strip()}
        allowed = {"add_dependencies", "max_concurrency"}
        if not change or set(change) - allowed:
            raise ContractViolation(
                "topology change supports only add_dependencies and max_concurrency"
            )
        normalized: Dict[str, Any] = {}
        if "add_dependencies" in change:
            values = change["add_dependencies"]
            if not isinstance(values, list) or not values:
                raise ContractViolation("add_dependencies must be a non-empty array")
            pairs = []
            for item in values:
                if not isinstance(item, dict) or set(item) != {
                    "node",
                    "depends_on",
                }:
                    raise ContractViolation(
                        "each dependency must contain node and depends_on"
                    )
                pairs.append(
                    {
                        "node": _identifier(item["node"], "dependency node"),
                        "depends_on": _identifier(
                            item["depends_on"], "dependency depends_on"
                        ),
                    }
                )
            if len({item["node"] for item in pairs}) != len(pairs):
                raise ContractViolation(
                    "topology change can add at most one dependency per node"
                )
            normalized["add_dependencies"] = pairs
        if "max_concurrency" in change:
            value = change["max_concurrency"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractViolation("max_concurrency must be a positive integer")
            normalized["max_concurrency"] = value
        return normalized

    @staticmethod
    def _in_rollout(candidate: OptimizationCandidate, key: str) -> bool:
        digest = hashlib.sha256(
            f"{candidate.candidate_id}:{key}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:4], "big") % 100 < candidate.rollout_percent

    def _read_suite(self, suite_id: str) -> RegressionSuite:
        if not suite_id.startswith("suite-") or "/" in suite_id:
            raise ContractViolation("invalid regression suite id")
        value = self._read_json(
            self.root / "suites" / f"{suite_id}.json", None
        )
        if value is None:
            raise ContractViolation(f"regression suite {suite_id} does not exist")
        suite = RegressionSuite.from_dict(value)
        canonical = json.dumps(
            [item.to_dict() for item in suite.cases],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        actual_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if (
            actual_digest != suite.digest
            or suite.suite_id != f"suite-{actual_digest[:24]}"
            or suite.suite_id != suite_id
        ):
            raise ContractViolation("frozen regression suite was modified")
        return suite

    def _candidate_path(self, candidate_id: str) -> Path:
        if not candidate_id.startswith("opt-") or "/" in candidate_id:
            raise ContractViolation("invalid optimization candidate id")
        return self.root / "candidates" / f"{candidate_id}.json"

    def _write_candidate(self, candidate: OptimizationCandidate) -> None:
        _atomic_json_write(
            self._candidate_path(candidate.candidate_id), candidate.to_dict()
        )

    def _read_candidate(self, candidate_id: str) -> OptimizationCandidate:
        value = self._read_json(self._candidate_path(candidate_id), None)
        if value is None:
            raise ContractViolation(
                f"optimization candidate {candidate_id} does not exist"
            )
        candidate = OptimizationCandidate.from_dict(value)
        normalized = self._validate_change(candidate.kind, candidate.change)
        if normalized != candidate.change:
            raise ContractViolation(
                f"optimization candidate {candidate_id} contains a non-canonical change"
            )
        if candidate.status not in (
            "proposed",
            "evaluated",
            "approved",
            "active",
            "superseded",
            "rolled_back",
        ):
            raise ContractViolation(
                f"optimization candidate {candidate_id} has invalid status"
            )
        return candidate

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(
                f"invalid optimization state {path.name}: {error}"
            ) from error

    def _append_jsonl(self, path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())


def _increase_percent(baseline: float, measured: float) -> float:
    if baseline == 0:
        return 0.0 if measured == 0 else 1_000_000_000.0
    return (measured - baseline) / baseline * 100.0


def _candidate_change_digest(candidate: OptimizationCandidate) -> str:
    value = json.dumps(
        {
            "kind": candidate.kind,
            "change": candidate.change,
            "rollout_percent": candidate.rollout_percent,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(
            value,
            handle,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)
