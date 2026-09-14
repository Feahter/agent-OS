"""Reproducible, privacy-minimized economics snapshots for benchmark runs."""

from __future__ import annotations

import json
import math
import re
import statistics
import time
from dataclasses import asdict, dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ._store import exclusive_json_write, json_digest, read_json_object
from .agents import ModelUsage
from .errors import ContractViolation
from .model import GraphSpec

BENCHMARK_PROTOCOL_SCHEMA_VERSION = 1
BENCHMARK_SUITE_SCHEMA_VERSION = 1
BENCHMARK_RUN_SCHEMA_VERSION = 2
PRICE_CATALOG_SCHEMA_VERSION = 2
RUN_ECONOMICS_SCHEMA_VERSION = 3
ROI_REPORT_SCHEMA_VERSION = 1

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_USAGE_FIELDS = frozenset(ModelUsage.__dataclass_fields__)
_PRICE_CATALOG_NOT_EFFECTIVE_WARNING = (
    "price catalog is not yet effective at benchmark time"
)
_PRICE_CATALOG_EXPIRED_WARNING = "price catalog expired before benchmark time"
_PRICE_CATALOG_WARNINGS = frozenset(
    (_PRICE_CATALOG_NOT_EFFECTIVE_WARNING, _PRICE_CATALOG_EXPIRED_WARNING)
)


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ContractViolation(
            f"{field} must contain only letters, numbers, dot, underscore, or hyphen"
        )
    return value


def _version(value: Any, field: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ContractViolation(f"{field} must be a version identifier")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ContractViolation(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractViolation(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ContractViolation(f"{field} must include a timezone")
    return value


def _timestamp_seconds(value: Any, field: str) -> float:
    timestamp = _timestamp(value, field)
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ContractViolation(f"{field} must be a lowercase SHA-256 digest")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractViolation(f"{field} must be a non-negative integer")
    return value


def _positive_int(value: Any, field: str) -> int:
    result = _non_negative_int(value, field)
    if result == 0:
        raise ContractViolation(f"{field} must be a positive integer")
    return result


def _optional_non_negative_int(value: Any, field: str) -> Optional[int]:
    if value is None:
        return None
    return _non_negative_int(value, field)


def _non_negative_number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ContractViolation(f"{field} must be a finite non-negative number")
    return float(value)


def _optional_non_negative_number(value: Any, field: str) -> Optional[float]:
    if value is None:
        return None
    return _non_negative_number(value, field)


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContractViolation(f"{field} must be a boolean")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContractViolation(f"{label} has an invalid contract")


def _read_events(path: Path) -> Tuple[Mapping[str, Any], ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise ContractViolation(f"benchmark events do not exist: {path}") from error
    except OSError as error:
        raise ContractViolation(f"cannot read benchmark events: {error}") from error
    events: List[Mapping[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ContractViolation(
                f"invalid benchmark event at line {index}: {error}"
            ) from error
        if not isinstance(value, dict):
            raise ContractViolation(
                f"benchmark event at line {index} must be an object"
            )
        events.append(value)
    return tuple(events)


def _event_seconds(value: Any) -> float:
    timestamp = _timestamp(value, "benchmark event time")
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()


def _usage_from_artifact(value: Any, label: str) -> ModelUsage:
    if not isinstance(value, Mapping):
        raise ContractViolation(f"{label} must be an object")
    tokens = _non_negative_int(value.get("tokens_used"), f"{label} tokens_used")
    cost = _optional_non_negative_number(value.get("cost_usd"), f"{label} cost_usd")
    cost_complete = value.get("cost_complete")
    return ModelUsage.from_persisted(
        value.get("usage"),
        tokens,
        cost,
        cost_complete if isinstance(cost_complete, bool) else None,
    )


@dataclass(frozen=True)
class BenchmarkProtocol:
    """A prompt-free identity for one reproducible benchmark scenario."""

    benchmark_id: str
    scenario_version: str
    provider: str
    model: str
    reasoning: str
    executor: str
    executor_version: str
    dag_fingerprint: str
    input_fingerprint: str
    concurrency_matrix: Tuple[int, ...] = (1, 2)
    repetitions_per_cell: int = 5

    def __post_init__(self) -> None:
        _safe_id(self.benchmark_id, "benchmark_id")
        _safe_id(self.scenario_version, "scenario_version")
        _safe_id(self.provider, "provider")
        _safe_id(self.model, "model")
        _safe_id(self.reasoning, "reasoning")
        _safe_id(self.executor, "executor")
        _version(self.executor_version, "executor_version")
        _digest(self.dag_fingerprint, "dag_fingerprint")
        _digest(self.input_fingerprint, "input_fingerprint")
        if (
            not isinstance(self.concurrency_matrix, tuple)
            or not self.concurrency_matrix
            or len(self.concurrency_matrix) != len(set(self.concurrency_matrix))
        ):
            raise ContractViolation(
                "concurrency_matrix must contain unique positive integers"
            )
        for item in self.concurrency_matrix:
            _positive_int(item, "concurrency_matrix[]")
        _positive_int(self.repetitions_per_cell, "repetitions_per_cell")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": BENCHMARK_PROTOCOL_SCHEMA_VERSION,
            "benchmark_id": self.benchmark_id,
            "scenario_version": self.scenario_version,
            "provider": self.provider,
            "model": self.model,
            "reasoning": self.reasoning,
            "executor": self.executor,
            "executor_version": self.executor_version,
            "dag_fingerprint": self.dag_fingerprint,
            "input_fingerprint": self.input_fingerprint,
            "concurrency_matrix": list(self.concurrency_matrix),
            "repetitions_per_cell": self.repetitions_per_cell,
        }

    @property
    def fingerprint(self) -> str:
        return json_digest(self.to_dict(), label="benchmark protocol")[1]

    @staticmethod
    def graph_fingerprint(graph: GraphSpec) -> str:
        """Identify DAG semantics while leaving concurrency as a matrix dimension."""

        if not isinstance(graph, GraphSpec):
            raise ContractViolation("benchmark graph must be GraphSpec")
        value = asdict(graph)
        value.pop("max_concurrency")
        return json_digest(value, label="benchmark graph")[1]

    @staticmethod
    def inputs_fingerprint(inputs: Any) -> str:
        return json_digest(inputs, label="benchmark inputs")[1]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BenchmarkProtocol:
        fields = set(cls.__dataclass_fields__)
        _exact_fields(value, fields | {"schema_version"}, "benchmark protocol")
        if value["schema_version"] != BENCHMARK_PROTOCOL_SCHEMA_VERSION:
            raise ContractViolation("unsupported benchmark protocol schema_version")
        concurrency = value["concurrency_matrix"]
        if not isinstance(concurrency, list):
            raise ContractViolation("concurrency_matrix must be an array")
        return cls(
            **{
                **{field: value[field] for field in fields if field != "concurrency_matrix"},
                "concurrency_matrix": tuple(concurrency),
            }
        )

    @classmethod
    def load(cls, path: Path) -> BenchmarkProtocol:
        return cls.from_dict(read_json_object(path, label="benchmark protocol"))

    @classmethod
    def freeze(
        cls,
        path: Path,
        *,
        benchmark_id: str,
        scenario_version: str,
        provider: str,
        model: str,
        reasoning: str,
        executor: str,
        executor_version: str,
        graph: GraphSpec,
        inputs: Any,
        concurrency_matrix: Tuple[int, ...] = (1, 2),
        repetitions_per_cell: int = 5,
    ) -> BenchmarkProtocol:
        """Freeze prompt-free benchmark identity without copying raw inputs."""

        protocol = cls(
            benchmark_id=benchmark_id,
            scenario_version=scenario_version,
            provider=provider,
            model=model,
            reasoning=reasoning,
            executor=executor,
            executor_version=executor_version,
            dag_fingerprint=cls.graph_fingerprint(graph),
            input_fingerprint=cls.inputs_fingerprint(inputs),
            concurrency_matrix=concurrency_matrix,
            repetitions_per_cell=repetitions_per_cell,
        )
        if path.exists() or path.is_symlink():
            raise ContractViolation(f"benchmark protocol already exists: {path.name}")
        try:
            exclusive_json_write(path, protocol.to_dict(), label="benchmark protocol")
        except FileExistsError as error:
            raise ContractViolation(
                f"benchmark protocol already exists: {path.name}"
            ) from error
        return protocol


@dataclass(frozen=True)
class BenchmarkSuite:
    """A frozen index requiring all three ROI benchmark scenario classes."""

    suite_id: str
    suite_version: str
    micro: BenchmarkProtocol
    engineering: BenchmarkProtocol
    recovery: BenchmarkProtocol

    def __post_init__(self) -> None:
        _safe_id(self.suite_id, "benchmark suite_id")
        _version(self.suite_version, "benchmark suite_version")
        protocols = (self.micro, self.engineering, self.recovery)
        if any(not isinstance(item, BenchmarkProtocol) for item in protocols):
            raise ContractViolation(
                "benchmark suite scenarios must be BenchmarkProtocol values"
            )
        for item in protocols:
            if not {1, 2}.issubset(item.concurrency_matrix):
                raise ContractViolation(
                    "benchmark suite scenarios require concurrency 1 and 2"
                )
            if item.repetitions_per_cell < 5:
                raise ContractViolation(
                    "benchmark suite scenarios require at least 5 repetitions"
                )
        benchmark_ids = {item.benchmark_id for item in protocols}
        if len(benchmark_ids) != len(protocols):
            raise ContractViolation(
                "benchmark suite scenarios require distinct benchmark_id values"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": BENCHMARK_SUITE_SCHEMA_VERSION,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
            "scenarios": {
                "micro": self.micro.to_dict(),
                "engineering": self.engineering.to_dict(),
                "recovery": self.recovery.to_dict(),
            },
        }

    @property
    def fingerprint(self) -> str:
        return json_digest(self.to_dict(), label="benchmark suite")[1]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BenchmarkSuite:
        _exact_fields(
            value,
            {"schema_version", "suite_id", "suite_version", "scenarios"},
            "benchmark suite",
        )
        if value["schema_version"] != BENCHMARK_SUITE_SCHEMA_VERSION:
            raise ContractViolation("unsupported benchmark suite schema_version")
        scenarios = value["scenarios"]
        _exact_fields(
            scenarios,
            {"micro", "engineering", "recovery"},
            "benchmark suite scenarios",
        )
        return cls(
            suite_id=value["suite_id"],
            suite_version=value["suite_version"],
            micro=BenchmarkProtocol.from_dict(scenarios["micro"]),
            engineering=BenchmarkProtocol.from_dict(scenarios["engineering"]),
            recovery=BenchmarkProtocol.from_dict(scenarios["recovery"]),
        )

    @classmethod
    def load(cls, path: Path) -> BenchmarkSuite:
        return cls.from_dict(read_json_object(path, label="benchmark suite"))

    @classmethod
    def freeze(
        cls,
        path: Path,
        *,
        suite_id: str,
        suite_version: str,
        micro: BenchmarkProtocol,
        engineering: BenchmarkProtocol,
        recovery: BenchmarkProtocol,
    ) -> BenchmarkSuite:
        suite = cls(
            suite_id=suite_id,
            suite_version=suite_version,
            micro=micro,
            engineering=engineering,
            recovery=recovery,
        )
        if path.exists() or path.is_symlink():
            raise ContractViolation(f"benchmark suite already exists: {path.name}")
        try:
            exclusive_json_write(path, suite.to_dict(), label="benchmark suite")
        except FileExistsError as error:
            raise ContractViolation(
                f"benchmark suite already exists: {path.name}"
            ) from error
        return suite


@dataclass(frozen=True)
class PriceCatalog:
    """An explicitly versioned, offline price input for one provider model."""

    catalog_id: str
    version: str
    effective_at: str
    provider: str
    model: str
    input_usd_per_million: float
    cached_input_usd_per_million: float
    output_usd_per_million: float
    cached_input_mode: str
    valid_until: Optional[str] = None

    def __post_init__(self) -> None:
        _safe_id(self.catalog_id, "price catalog_id")
        _safe_id(self.version, "price version")
        effective_at = _timestamp_seconds(
            self.effective_at, "price effective_at"
        )
        if self.valid_until is not None:
            valid_until = _timestamp_seconds(
                self.valid_until, "price valid_until"
            )
            if valid_until <= effective_at:
                raise ContractViolation(
                    "price valid_until must be after effective_at"
                )
        _safe_id(self.provider, "price provider")
        _safe_id(self.model, "price model")
        for field in (
            "input_usd_per_million",
            "cached_input_usd_per_million",
            "output_usd_per_million",
        ):
            _non_negative_number(getattr(self, field), f"price {field}")
        if self.cached_input_mode not in ("included_in_input", "additional"):
            raise ContractViolation(
                "price cached_input_mode must be included_in_input or additional"
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": PRICE_CATALOG_SCHEMA_VERSION,
            "catalog_id": self.catalog_id,
            "version": self.version,
            "effective_at": self.effective_at,
            "provider": self.provider,
            "model": self.model,
            "currency": "USD",
            "input_usd_per_million": self.input_usd_per_million,
            "cached_input_usd_per_million": self.cached_input_usd_per_million,
            "output_usd_per_million": self.output_usd_per_million,
            "cached_input_mode": self.cached_input_mode,
            "valid_until": self.valid_until,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PriceCatalog:
        fields = set(cls.__dataclass_fields__)
        schema_version = value.get("schema_version")
        if schema_version == 1:
            _exact_fields(
                value,
                (fields - {"valid_until"}) | {"schema_version", "currency"},
                "price catalog",
            )
        elif schema_version == PRICE_CATALOG_SCHEMA_VERSION:
            _exact_fields(
                value,
                fields | {"schema_version", "currency"},
                "price catalog",
            )
        else:
            raise ContractViolation("unsupported price catalog schema_version")
        if value["currency"] != "USD":
            raise ContractViolation("price catalog currency must be USD")
        return cls(
            catalog_id=value["catalog_id"],
            version=value["version"],
            effective_at=value["effective_at"],
            provider=value["provider"],
            model=value["model"],
            input_usd_per_million=value["input_usd_per_million"],
            cached_input_usd_per_million=value[
                "cached_input_usd_per_million"
            ],
            output_usd_per_million=value["output_usd_per_million"],
            cached_input_mode=value["cached_input_mode"],
            valid_until=value.get("valid_until"),
        )

    @classmethod
    def load(cls, path: Path) -> PriceCatalog:
        return cls.from_dict(read_json_object(path, label="price catalog"))

    def warning_at(self, timestamp: float) -> Optional[str]:
        timestamp = _non_negative_number(timestamp, "price benchmark time")
        if timestamp < _timestamp_seconds(
            self.effective_at, "price effective_at"
        ):
            return _PRICE_CATALOG_NOT_EFFECTIVE_WARNING
        if self.valid_until is not None and timestamp > _timestamp_seconds(
            self.valid_until, "price valid_until"
        ):
            return _PRICE_CATALOG_EXPIRED_WARNING
        return None


@dataclass(frozen=True)
class ReuseMetrics:
    """Complete-or-unknown reuse counters for one run or node."""

    hit: Optional[int] = None
    miss: Optional[int] = None
    coalesced: Optional[int] = None
    bypassed: Optional[int] = None
    none: Optional[int] = None
    saved_tokens: Optional[int] = None

    def __post_init__(self) -> None:
        for name in ("hit", "miss", "coalesced", "bypassed", "none"):
            _optional_non_negative_int(getattr(self, name), f"reuse {name}")
        _optional_non_negative_int(self.saved_tokens, "reuse saved_tokens")
        counters = (self.hit, self.miss, self.coalesced, self.bypassed, self.none)
        if any(value is None for value in counters) and any(
            value is not None for value in counters
        ):
            raise ContractViolation("reuse counters must be complete or unknown")

    @classmethod
    def known_zero(cls) -> ReuseMetrics:
        return cls(0, 0, 0, 0, 0, 0)

    @classmethod
    def from_statuses(
        cls, statuses: Sequence[str], saved_tokens: Optional[int]
    ) -> ReuseMetrics:
        allowed = ("hit", "miss", "coalesced", "bypassed", "none")
        if any(status not in allowed for status in statuses):
            return cls()
        return cls(
            **{status: statuses.count(status) for status in allowed},
            saved_tokens=saved_tokens,
        )

    @classmethod
    def combine(cls, values: Sequence[ReuseMetrics]) -> ReuseMetrics:
        if not values:
            return cls.known_zero()
        fields = ("hit", "miss", "coalesced", "bypassed", "none")
        if any(getattr(value, name) is None for value in values for name in fields):
            return cls()
        saved_tokens = (
            sum(value.saved_tokens or 0 for value in values)
            if all(value.saved_tokens is not None for value in values)
            else None
        )
        return cls(
            **{
                name: sum(getattr(value, name) for value in values)
                for name in fields
            },
            saved_tokens=saved_tokens,
        )

    def to_dict(self) -> Dict[str, Optional[int]]:
        return {
            "hit": self.hit,
            "miss": self.miss,
            "coalesced": self.coalesced,
            "bypassed": self.bypassed,
            "none": self.none,
            "saved_tokens": self.saved_tokens,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReuseMetrics:
        fields = set(cls.__dataclass_fields__)
        _exact_fields(value, fields, "benchmark reuse metrics")
        return cls(**{name: value[name] for name in fields})


@dataclass(frozen=True)
class BenchmarkNode:
    """Privacy-minimized node economics reconstructed from runtime events."""

    node_id: str
    kind: str
    attempts: int
    usage: ModelUsage
    queue_wait_seconds: Optional[float]
    execution_seconds: Optional[float]
    model_seconds: Optional[float]
    reuse: ReuseMetrics

    def __post_init__(self) -> None:
        _safe_id(self.node_id, "benchmark node_id")
        _safe_id(self.kind, "benchmark node kind")
        _non_negative_int(self.attempts, "benchmark node attempts")
        if not isinstance(self.usage, ModelUsage):
            raise ContractViolation("benchmark node usage must be ModelUsage")
        for name in (
            "queue_wait_seconds",
            "execution_seconds",
            "model_seconds",
        ):
            _optional_non_negative_number(
                getattr(self, name), f"benchmark node {name}"
            )
        if not isinstance(self.reuse, ReuseMetrics):
            raise ContractViolation("benchmark node reuse must be ReuseMetrics")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "attempts": self.attempts,
            "usage": self.usage.to_dict(),
            "queue_wait_seconds": self.queue_wait_seconds,
            "execution_seconds": self.execution_seconds,
            "model_seconds": self.model_seconds,
            "reuse": self.reuse.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BenchmarkNode:
        fields = set(cls.__dataclass_fields__)
        _exact_fields(value, fields, "benchmark node")
        usage = value["usage"]
        _exact_fields(usage, set(_USAGE_FIELDS), "benchmark node usage")
        return cls(
            **{
                **{
                    name: value[name]
                    for name in fields
                    if name not in ("usage", "reuse")
                },
                "usage": ModelUsage(**usage),
                "reuse": ReuseMetrics.from_dict(value["reuse"]),
            }
        )


@dataclass(frozen=True)
class BenchmarkRun:
    """Structured outcome data accepted by :class:`RunEconomics`."""

    run_id: str
    concurrency: int
    success: bool
    verified: bool
    wall_clock_seconds: float
    verification_seconds: Optional[float]
    human_actions: Optional[int]
    repair_loops: Optional[int]
    recoveries: Optional[int]
    model_calls: Optional[int]
    usage: ModelUsage
    queue_wait_seconds: Optional[float] = None
    model_seconds: Optional[float] = None
    critical_path_seconds: Optional[float] = None
    reuse: ReuseMetrics = dataclass_field(default_factory=ReuseMetrics)
    nodes: Tuple[BenchmarkNode, ...] = ()

    def __post_init__(self) -> None:
        _safe_id(self.run_id, "benchmark run_id")
        _positive_int(self.concurrency, "benchmark concurrency")
        _boolean(self.success, "benchmark success")
        _boolean(self.verified, "benchmark verified")
        if self.verified and not self.success:
            raise ContractViolation("a failed benchmark run cannot be verified")
        _non_negative_number(self.wall_clock_seconds, "benchmark wall_clock_seconds")
        _optional_non_negative_number(
            self.verification_seconds, "benchmark verification_seconds"
        )
        for field in (
            "human_actions",
            "repair_loops",
            "recoveries",
            "model_calls",
        ):
            _optional_non_negative_int(getattr(self, field), f"benchmark {field}")
        if not isinstance(self.usage, ModelUsage):
            raise ContractViolation("benchmark usage must be ModelUsage")
        for name in (
            "queue_wait_seconds",
            "model_seconds",
            "critical_path_seconds",
        ):
            _optional_non_negative_number(getattr(self, name), f"benchmark {name}")
        if not isinstance(self.reuse, ReuseMetrics):
            raise ContractViolation("benchmark reuse must be ReuseMetrics")
        if not isinstance(self.nodes, tuple) or any(
            not isinstance(node, BenchmarkNode) for node in self.nodes
        ):
            raise ContractViolation("benchmark nodes must be BenchmarkNode values")
        node_ids = tuple(node.node_id for node in self.nodes)
        if len(node_ids) != len(set(node_ids)):
            raise ContractViolation("benchmark nodes must have unique node_id values")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": BENCHMARK_RUN_SCHEMA_VERSION,
            "run_id": self.run_id,
            "concurrency": self.concurrency,
            "success": self.success,
            "verified": self.verified,
            "wall_clock_seconds": self.wall_clock_seconds,
            "verification_seconds": self.verification_seconds,
            "human_actions": self.human_actions,
            "repair_loops": self.repair_loops,
            "recoveries": self.recoveries,
            "model_calls": self.model_calls,
            "usage": self.usage.to_dict(),
            "queue_wait_seconds": self.queue_wait_seconds,
            "model_seconds": self.model_seconds,
            "critical_path_seconds": self.critical_path_seconds,
            "reuse": self.reuse.to_dict(),
            "nodes": [node.to_dict() for node in self.nodes],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BenchmarkRun:
        fields = set(cls.__dataclass_fields__)
        added_fields = {
            "queue_wait_seconds",
            "model_seconds",
            "critical_path_seconds",
            "reuse",
            "nodes",
        }
        schema_version = value.get("schema_version")
        if schema_version == 1:
            _exact_fields(
                value,
                (fields - added_fields) | {"schema_version"},
                "benchmark run",
            )
        elif schema_version == BENCHMARK_RUN_SCHEMA_VERSION:
            _exact_fields(value, fields | {"schema_version"}, "benchmark run")
        else:
            raise ContractViolation("unsupported benchmark run schema_version")
        raw_usage = value["usage"]
        _exact_fields(raw_usage, set(_USAGE_FIELDS), "benchmark usage")
        raw_nodes = value.get("nodes", [])
        if not isinstance(raw_nodes, list):
            raise ContractViolation("benchmark nodes must be an array")
        return cls(
            run_id=value["run_id"],
            concurrency=value["concurrency"],
            success=value["success"],
            verified=value["verified"],
            wall_clock_seconds=value["wall_clock_seconds"],
            verification_seconds=value["verification_seconds"],
            human_actions=value["human_actions"],
            repair_loops=value["repair_loops"],
            recoveries=value["recoveries"],
            model_calls=value["model_calls"],
            usage=ModelUsage(**raw_usage),
            queue_wait_seconds=value.get("queue_wait_seconds"),
            model_seconds=value.get("model_seconds"),
            critical_path_seconds=value.get("critical_path_seconds"),
            reuse=(
                ReuseMetrics.from_dict(value["reuse"])
                if "reuse" in value
                else ReuseMetrics()
            ),
            nodes=tuple(BenchmarkNode.from_dict(node) for node in raw_nodes),
        )

    @classmethod
    def load(cls, path: Path) -> BenchmarkRun:
        return cls.from_dict(read_json_object(path, label="benchmark run"))


class RunEconomics:
    """Builds and immutably stores one unambiguous economics snapshot per run."""

    def __init__(self, root: Path, clock=time.time):
        if root.is_symlink():
            raise ContractViolation("economics root cannot be a symlink")
        self.root = root
        self.runs_root = root / "runs"
        if self.runs_root.is_symlink():
            raise ContractViolation("economics runs directory cannot be a symlink")
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self._clock = clock

    def record(
        self,
        protocol: BenchmarkProtocol,
        run: BenchmarkRun,
        prices: Optional[PriceCatalog] = None,
    ) -> Mapping[str, Any]:
        if run.concurrency not in protocol.concurrency_matrix:
            raise ContractViolation(
                "benchmark concurrency is not part of the protocol matrix"
            )
        if prices is not None and (
            prices.provider != protocol.provider or prices.model != protocol.model
        ):
            raise ContractViolation("price catalog does not match benchmark provider/model")

        recorded_at = _non_negative_number(
            self._clock(), "economics recorded_at"
        )
        warning = prices.warning_at(recorded_at) if prices is not None else None

        snapshot = {
            "schema_version": RUN_ECONOMICS_SCHEMA_VERSION,
            "run_id": run.run_id,
            "recorded_at": recorded_at,
            "benchmark_protocol": protocol.to_dict(),
            "benchmark_protocol_fingerprint": protocol.fingerprint,
            "outcome": {
                "success": run.success,
                "verified": run.verified,
            },
            "usage": run.usage.to_dict(),
            "cost": self._cost_snapshot(run.usage, prices),
            "latency": {
                "wall_clock_seconds": run.wall_clock_seconds,
                "verification_seconds": run.verification_seconds,
                "queue_wait_seconds": run.queue_wait_seconds,
                "model_seconds": run.model_seconds,
                "critical_path_seconds": run.critical_path_seconds,
            },
            "intervention": {
                "human_actions": run.human_actions,
                "repair_loops": run.repair_loops,
                "recoveries": run.recoveries,
            },
            "model_calls": run.model_calls,
            "concurrency": run.concurrency,
            "reuse": run.reuse.to_dict(),
            "nodes": [node.to_dict() for node in run.nodes],
            "warnings": [warning] if warning is not None else [],
        }
        target = self.runs_root / f"{run.run_id}.json"
        if target.exists() or target.is_symlink():
            raise ContractViolation(f"economics run already exists: {run.run_id}")
        try:
            exclusive_json_write(target, snapshot, label="economics snapshot")
        except FileExistsError as error:
            raise ContractViolation(
                f"economics run already exists: {run.run_id}"
            ) from error
        return snapshot

    def record_control_run(
        self,
        protocol: BenchmarkProtocol,
        run_dir: Path,
        *,
        verified: bool,
        prices: Optional[PriceCatalog] = None,
    ) -> Mapping[str, Any]:
        """Record a completed control-plane run without a hand-authored run file."""

        graph = GraphSpec.from_dict(
            read_json_object(run_dir / "graph.json", label="benchmark graph")
        )
        if protocol.dag_fingerprint not in (
            BenchmarkProtocol.graph_fingerprint(graph),
            graph.fingerprint(),
        ):
            raise ContractViolation(
                "benchmark graph fingerprint does not match the protocol"
            )
        run = self._control_run(run_dir, graph, verified)
        return self.record(protocol, run, prices)

    def reprice(
        self, run_id: str, prices: PriceCatalog
    ) -> Mapping[str, Any]:
        """Estimate one immutable run with an explicitly selected catalog."""

        run_id = _safe_id(run_id, "economics run_id")
        path = self.runs_root / f"{run_id}.json"
        if path.is_symlink():
            raise ContractViolation("economics run cannot be a symlink")
        snapshot = read_json_object(path, label="economics snapshot")
        self._validate_snapshot(snapshot, run_id)
        protocol = BenchmarkProtocol.from_dict(snapshot["benchmark_protocol"])
        if prices.provider != protocol.provider or prices.model != protocol.model:
            raise ContractViolation("price catalog does not match benchmark provider/model")
        usage = snapshot["usage"]
        warning = prices.warning_at(snapshot["recorded_at"])
        return {
            "schema_version": 1,
            "run_id": run_id,
            "benchmark_protocol_fingerprint": snapshot[
                "benchmark_protocol_fingerprint"
            ],
            "original_cost": snapshot["cost"],
            "scenario_estimate": self._estimate_cost(
                ModelUsage(**usage), prices
            ),
            "warnings": [warning] if warning is not None else [],
        }

    @staticmethod
    def _control_run(
        run_dir: Path, graph: GraphSpec, verified: bool
    ) -> BenchmarkRun:
        state = read_json_object(run_dir / "state.json", label="benchmark run state")
        checkpoint = read_json_object(
            run_dir / "runtime" / "checkpoint.json",
            label="benchmark checkpoint",
        )
        events = _read_events(run_dir / "runtime" / "events.jsonl")
        run_id = _safe_id(state.get("run_id"), "benchmark run_id")
        if state.get("graph_id") != graph.id:
            raise ContractViolation("benchmark state graph_id does not match graph")
        if checkpoint.get("run_id") != run_id:
            raise ContractViolation("benchmark checkpoint run_id does not match state")
        if checkpoint.get("graph_id") != graph.id:
            raise ContractViolation("benchmark checkpoint graph_id does not match graph")
        if checkpoint.get("graph_fingerprint") != graph.fingerprint():
            raise ContractViolation(
                "benchmark checkpoint graph fingerprint does not match graph"
            )
        if any(event.get("run_id") != run_id for event in events):
            raise ContractViolation("benchmark events contain a different run_id")

        result = state.get("result")
        if not isinstance(result, Mapping):
            raise ContractViolation("benchmark run has no completed result")
        success = _boolean(result.get("success"), "benchmark result success")
        phase = state.get("phase")
        if success and phase != "succeeded":
            raise ContractViolation("benchmark result does not match terminal phase")
        if not success and phase not in ("cancelled", "failed"):
            raise ContractViolation("benchmark result does not match terminal phase")

        completed = tuple(
            event for event in events if event.get("event") == "run_completed"
        )
        started = tuple(
            event
            for event in events
            if event.get("event") in ("run_started", "run_resumed")
        )
        if not completed or not started:
            raise ContractViolation("benchmark events are missing run boundaries")
        final_payload = completed[-1].get("payload")
        if not isinstance(final_payload, Mapping):
            raise ContractViolation("benchmark run_completed payload must be an object")
        if _boolean(
            final_payload.get("success"), "benchmark run_completed success"
        ) != success:
            raise ContractViolation("benchmark result success disagrees with events")

        result_usage = _usage_from_artifact(result, "benchmark result")
        checkpoint_usage = _usage_from_artifact(
            checkpoint, "benchmark checkpoint"
        )
        event_usage = _usage_from_artifact(
            final_payload, "benchmark run_completed"
        )
        if result_usage != checkpoint_usage or result_usage != event_usage:
            raise ContractViolation(
                "benchmark result, checkpoint, and events usage disagree"
            )

        submitted_at = state.get("submitted_at")
        if (
            isinstance(submitted_at, (int, float))
            and not isinstance(submitted_at, bool)
            and math.isfinite(submitted_at)
            and submitted_at >= 0
        ):
            started_at = float(submitted_at)
        else:
            started_at = _event_seconds(started[0].get("time"))
        completed_at = _event_seconds(completed[-1].get("time"))
        if completed_at < started_at:
            raise ContractViolation("benchmark wall clock cannot be negative")
        (
            node_metrics,
            queue_wait_seconds,
            model_seconds,
            critical_path_seconds,
            reuse,
        ) = RunEconomics._node_metrics(graph, events, started_at)

        return BenchmarkRun(
            run_id=run_id,
            concurrency=graph.max_concurrency,
            success=success,
            verified=verified,
            wall_clock_seconds=completed_at - started_at,
            verification_seconds=None,
            human_actions=None,
            repair_loops=None,
            recoveries=sum(
                event.get("event") == "run_resumed" for event in events
            ),
            model_calls=RunEconomics._model_calls(graph, events),
            usage=result_usage,
            queue_wait_seconds=queue_wait_seconds,
            model_seconds=model_seconds,
            critical_path_seconds=critical_path_seconds,
            reuse=reuse,
            nodes=node_metrics,
        )

    @staticmethod
    def _node_metrics(
        graph: GraphSpec,
        events: Sequence[Mapping[str, Any]],
        submitted_at: float,
    ) -> Tuple[
        Tuple[BenchmarkNode, ...],
        Optional[float],
        Optional[float],
        Optional[float],
        ReuseMetrics,
    ]:
        nodes = graph.node_map()
        starts: Dict[Tuple[str, int], Mapping[str, Any]] = {}
        terminals: Dict[Tuple[str, int], Mapping[str, Any]] = {}
        completion_times: Dict[str, float] = {}
        terminal_names = ("node_completed", "node_retry", "node_failed")
        for event in events:
            event_name = event.get("event")
            if event_name not in ("node_started", *terminal_names):
                continue
            node_id = event.get("node_id")
            if not isinstance(node_id, str) or node_id not in nodes:
                raise ContractViolation("benchmark event contains an unknown node_id")
            attempt = _positive_int(
                event.get("attempt"), "benchmark node event attempt"
            )
            key = (node_id, attempt)
            target = starts if event_name == "node_started" else terminals
            if key in target:
                raise ContractViolation("benchmark node events contain duplicate boundaries")
            target[key] = event
            if event_name == "node_completed":
                completion_times[node_id] = _event_seconds(event.get("time"))
        if set(starts) != set(terminals):
            raise ContractViolation("benchmark node events have incomplete boundaries")

        metrics: List[BenchmarkNode] = []
        for node in graph.nodes:
            keys = sorted(
                (key for key in starts if key[0] == node.id),
                key=lambda key: key[1],
            )
            queue_waits: List[float] = []
            durations: List[float] = []
            usages: List[ModelUsage] = []
            statuses: List[str] = []
            saved_tokens: List[int] = []
            saved_tokens_complete = True
            model_durations: List[float] = []
            model_seconds_complete = True
            previous_terminal: Optional[float] = None
            for index, key in enumerate(keys):
                start_event = starts[key]
                terminal_event = terminals[key]
                start_time = _event_seconds(start_event.get("time"))
                terminal_time = _event_seconds(terminal_event.get("time"))
                if terminal_time < start_time:
                    raise ContractViolation("benchmark node execution cannot be negative")
                duration = terminal_time - start_time
                durations.append(duration)
                if index == 0:
                    dependency_times = [
                        completion_times.get(dependency) for dependency in node.deps
                    ]
                    ready_time = (
                        max(time for time in dependency_times if time is not None)
                        if dependency_times
                        and all(time is not None for time in dependency_times)
                        else submitted_at
                        if not dependency_times
                        else None
                    )
                else:
                    ready_time = previous_terminal
                if ready_time is None:
                    queue_waits = []
                elif start_time < ready_time:
                    raise ContractViolation("benchmark node queue wait cannot be negative")
                elif queue_waits or index == 0:
                    queue_waits.append(start_time - ready_time)
                previous_terminal = terminal_time

                payload = terminal_event.get("payload")
                if terminal_event.get("event") == "node_completed":
                    usages.append(_usage_from_artifact(payload, "benchmark node event"))
                else:
                    usages.append(ModelUsage.unknown())

                if node.agent is None:
                    continue
                metadata = payload.get("metadata") if isinstance(payload, Mapping) else None
                status = metadata.get("reuse_status") if isinstance(metadata, Mapping) else None
                if status not in ("hit", "miss", "coalesced", "bypassed", "none"):
                    statuses.append("unknown")
                    saved_tokens_complete = False
                    model_seconds_complete = False
                    continue
                statuses.append(status)
                if status in ("hit", "coalesced"):
                    if not isinstance(metadata, Mapping):
                        saved_tokens_complete = False
                        continue
                    raw_saved_tokens = metadata.get("reuse_saved_tokens")
                    if raw_saved_tokens is None:
                        saved_tokens_complete = False
                    else:
                        saved_tokens.append(
                            _non_negative_int(
                                raw_saved_tokens,
                                "benchmark node reuse_saved_tokens",
                            )
                        )
                else:
                    saved_tokens.append(0)
                    model_durations.append(duration)

            usage = ModelUsage.combine(usages) if usages else ModelUsage.no_call()
            queue_wait = sum(queue_waits) if len(queue_waits) == len(keys) else None
            execution = sum(durations) if len(durations) == len(keys) else None
            if node.agent is None:
                node_reuse = ReuseMetrics.known_zero()
                model_seconds_value = None
            else:
                node_reuse = ReuseMetrics.from_statuses(
                    statuses,
                    sum(saved_tokens) if saved_tokens_complete else None,
                )
                model_seconds_value = (
                    sum(model_durations) if model_seconds_complete else None
                )
            metrics.append(
                BenchmarkNode(
                    node_id=node.id,
                    kind=node.kind,
                    attempts=len(keys),
                    usage=usage,
                    queue_wait_seconds=queue_wait,
                    execution_seconds=execution,
                    model_seconds=model_seconds_value,
                    reuse=node_reuse,
                )
            )

        queue_wait_seconds = (
            sum(item.queue_wait_seconds or 0.0 for item in metrics)
            if all(item.queue_wait_seconds is not None for item in metrics)
            else None
        )
        agent_metrics = [
            item for item in metrics if nodes[item.node_id].agent is not None
        ]
        model_seconds = (
            sum(item.model_seconds or 0.0 for item in agent_metrics)
            if all(item.model_seconds is not None for item in agent_metrics)
            else None
        )
        critical_path_seconds = RunEconomics._critical_path_seconds(graph, metrics)
        reuse = ReuseMetrics.combine([item.reuse for item in agent_metrics])
        return (
            tuple(metrics),
            queue_wait_seconds,
            model_seconds,
            critical_path_seconds,
            reuse,
        )

    @staticmethod
    def _critical_path_seconds(
        graph: GraphSpec, metrics: Sequence[BenchmarkNode]
    ) -> Optional[float]:
        weights = {item.node_id: item.execution_seconds for item in metrics}
        if any(value is None for value in weights.values()):
            return None
        longest: Dict[str, float] = {}
        unresolved = set(weights)
        while unresolved:
            progressed = False
            for node in graph.nodes:
                if node.id not in unresolved or any(
                    dependency not in longest for dependency in node.deps
                ):
                    continue
                dependency_seconds = max(
                    (longest[dependency] for dependency in node.deps),
                    default=0.0,
                )
                weight = weights[node.id]
                if weight is None:
                    return None
                longest[node.id] = dependency_seconds + weight
                unresolved.remove(node.id)
                progressed = True
            if not progressed:
                raise ContractViolation("benchmark graph cannot resolve a critical path")
        return max(longest.values(), default=0.0)

    @staticmethod
    def _model_calls(
        graph: GraphSpec, events: Sequence[Mapping[str, Any]]
    ) -> Optional[int]:
        agent_nodes = {node.id for node in graph.nodes if node.agent is not None}
        if not agent_nodes:
            return 0
        completed = {
            (event.get("node_id"), event.get("attempt")): event
            for event in events
            if event.get("event") == "node_completed"
            and event.get("node_id") in agent_nodes
        }
        calls = 0
        for event in events:
            if (
                event.get("event") != "node_started"
                or event.get("node_id") not in agent_nodes
            ):
                continue
            outcome = completed.get((event.get("node_id"), event.get("attempt")))
            if outcome is None:
                return None
            payload = outcome.get("payload")
            metadata = payload.get("metadata") if isinstance(payload, Mapping) else None
            reuse_status = (
                metadata.get("reuse_status")
                if isinstance(metadata, Mapping)
                else None
            )
            if reuse_status in ("hit", "coalesced"):
                continue
            if reuse_status not in ("none", "miss", "bypassed"):
                return None
            calls += 1
        return calls

    def report(
        self,
        baseline: BenchmarkProtocol,
        candidate: BenchmarkProtocol,
    ) -> Mapping[str, Any]:
        self._validate_comparison(baseline, candidate)
        snapshots = self._snapshots()
        baseline_runs = tuple(
            item
            for item in snapshots
            if item["benchmark_protocol_fingerprint"] == baseline.fingerprint
        )
        candidate_runs = tuple(
            item
            for item in snapshots
            if item["benchmark_protocol_fingerprint"] == candidate.fingerprint
        )
        if not baseline_runs:
            raise ContractViolation("ROI report has no baseline runs")
        if not candidate_runs:
            raise ContractViolation("ROI report has no candidate runs")

        baseline_summary = self._cohort_summary(baseline, baseline_runs)
        candidate_summary = self._cohort_summary(candidate, candidate_runs)
        return {
            "schema_version": ROI_REPORT_SCHEMA_VERSION,
            "benchmark_id": baseline.benchmark_id,
            "scenario_version": baseline.scenario_version,
            "baseline_protocol_fingerprint": baseline.fingerprint,
            "candidate_protocol_fingerprint": candidate.fingerprint,
            "baseline": baseline_summary,
            "candidate": candidate_summary,
            "roi": self._roi(baseline_summary, candidate_summary),
        }

    @staticmethod
    def _validate_comparison(
        baseline: BenchmarkProtocol, candidate: BenchmarkProtocol
    ) -> None:
        comparable_fields = (
            "benchmark_id",
            "scenario_version",
            "provider",
            "model",
            "reasoning",
            "executor",
            "dag_fingerprint",
            "input_fingerprint",
            "concurrency_matrix",
            "repetitions_per_cell",
        )
        mismatches = [
            field
            for field in comparable_fields
            if getattr(baseline, field) != getattr(candidate, field)
        ]
        if mismatches:
            raise ContractViolation(
                "ROI protocols are not comparable: " + ", ".join(mismatches)
            )
        if baseline.fingerprint == candidate.fingerprint:
            raise ContractViolation("ROI report requires distinct protocol fingerprints")

    def _snapshots(self) -> Tuple[Mapping[str, Any], ...]:
        snapshots = []
        for path in sorted(self.runs_root.glob("*.json")):
            if path.is_symlink():
                raise ContractViolation("economics runs cannot contain symlinks")
            value = read_json_object(path, label="economics snapshot")
            self._validate_snapshot(value, path.stem)
            snapshots.append(value)
        return tuple(snapshots)

    @staticmethod
    def _validate_snapshot(value: Mapping[str, Any], filename: str) -> None:
        expected = {
            "schema_version",
            "run_id",
            "recorded_at",
            "benchmark_protocol",
            "benchmark_protocol_fingerprint",
            "outcome",
            "usage",
            "cost",
            "latency",
            "intervention",
            "model_calls",
            "concurrency",
            "reuse",
            "nodes",
            "warnings",
        }
        schema_version = value.get("schema_version")
        if schema_version == 1:
            _exact_fields(
                value,
                expected - {"reuse", "nodes", "warnings"},
                "economics snapshot",
            )
        elif schema_version == 2:
            _exact_fields(value, expected - {"warnings"}, "economics snapshot")
        elif schema_version == RUN_ECONOMICS_SCHEMA_VERSION:
            _exact_fields(value, expected, "economics snapshot")
        else:
            raise ContractViolation("unsupported economics snapshot schema_version")
        run_id = _safe_id(value["run_id"], "economics run_id")
        if run_id != filename:
            raise ContractViolation("economics snapshot filename does not match run_id")
        _non_negative_number(value["recorded_at"], "economics recorded_at")
        protocol = BenchmarkProtocol.from_dict(value["benchmark_protocol"])
        if value["benchmark_protocol_fingerprint"] != protocol.fingerprint:
            raise ContractViolation("economics snapshot protocol fingerprint mismatch")

        outcome = value["outcome"]
        _exact_fields(outcome, {"success", "verified"}, "economics outcome")
        success = _boolean(outcome["success"], "economics success")
        verified = _boolean(outcome["verified"], "economics verified")
        if verified and not success:
            raise ContractViolation("a failed economics snapshot cannot be verified")

        usage = value["usage"]
        _exact_fields(usage, set(_USAGE_FIELDS), "economics usage")
        ModelUsage(**usage)
        RunEconomics._validate_cost(value["cost"])

        latency = value["latency"]
        latency_fields = {
            "wall_clock_seconds",
            "verification_seconds",
        }
        if schema_version in (2, RUN_ECONOMICS_SCHEMA_VERSION):
            latency_fields.update(
                {
                    "queue_wait_seconds",
                    "model_seconds",
                    "critical_path_seconds",
                }
            )
        _exact_fields(
            latency,
            latency_fields,
            "economics latency",
        )
        _non_negative_number(
            latency["wall_clock_seconds"], "economics wall_clock_seconds"
        )
        _optional_non_negative_number(
            latency["verification_seconds"], "economics verification_seconds"
        )
        if schema_version in (2, RUN_ECONOMICS_SCHEMA_VERSION):
            for field_name in (
                "queue_wait_seconds",
                "model_seconds",
                "critical_path_seconds",
            ):
                _optional_non_negative_number(
                    latency[field_name], f"economics {field_name}"
                )

        intervention = value["intervention"]
        _exact_fields(
            intervention,
            {"human_actions", "repair_loops", "recoveries"},
            "economics intervention",
        )
        for field in ("human_actions", "repair_loops", "recoveries"):
            _optional_non_negative_int(
                intervention[field], f"economics {field}"
            )
        _optional_non_negative_int(value["model_calls"], "economics model_calls")
        if schema_version in (2, RUN_ECONOMICS_SCHEMA_VERSION):
            ReuseMetrics.from_dict(value["reuse"])
            raw_nodes = value["nodes"]
            if not isinstance(raw_nodes, list):
                raise ContractViolation("economics nodes must be an array")
            parsed_nodes = tuple(BenchmarkNode.from_dict(node) for node in raw_nodes)
            if len({node.node_id for node in parsed_nodes}) != len(parsed_nodes):
                raise ContractViolation(
                    "economics nodes must have unique node_id values"
                )
        warnings = value.get("warnings", [])
        if (
            not isinstance(warnings, list)
            or len(warnings) != len(set(warnings))
            or any(item not in _PRICE_CATALOG_WARNINGS for item in warnings)
        ):
            raise ContractViolation("economics warnings have an invalid contract")
        concurrency = _positive_int(value["concurrency"], "economics concurrency")
        if concurrency not in protocol.concurrency_matrix:
            raise ContractViolation(
                "economics concurrency is not part of the protocol matrix"
            )

    @staticmethod
    def _validate_cost(value: Mapping[str, Any]) -> None:
        _exact_fields(
            value,
            {"source", "amount_usd", "complete", "measured", "estimated"},
            "economics cost",
        )
        source = value["source"]
        if source not in ("measured", "estimated", "unknown"):
            raise ContractViolation("economics cost has an invalid source")
        complete = _boolean(value["complete"], "economics cost complete")
        amount = value["amount_usd"]
        if amount is not None:
            _non_negative_number(amount, "economics cost amount_usd")
        if complete and amount is None:
            raise ContractViolation("complete economics cost requires amount_usd")
        if source == "unknown" and (complete or amount is not None):
            raise ContractViolation("unknown economics cost cannot have an amount")
        measured = value["measured"]
        _exact_fields(measured, {"amount_usd", "complete"}, "measured cost")
        measured_complete = _boolean(
            measured["complete"], "measured cost complete"
        )
        if measured["amount_usd"] is not None:
            _non_negative_number(measured["amount_usd"], "measured cost amount")
        if measured_complete and measured["amount_usd"] is None:
            raise ContractViolation("complete measured cost requires an amount")
        estimated = value["estimated"]
        if estimated is not None:
            RunEconomics._validate_estimate(estimated)
        if source == "measured" and (
            not measured_complete or amount != measured["amount_usd"]
        ):
            raise ContractViolation("measured cost source is inconsistent")
        if source == "estimated" and estimated is None:
            raise ContractViolation("estimated cost source requires an estimate")
        if source == "estimated" and (
            complete != estimated["complete"]
            or amount != estimated["amount_usd"]
        ):
            raise ContractViolation("estimated cost source is inconsistent")

    @staticmethod
    def _validate_estimate(value: Mapping[str, Any]) -> None:
        _exact_fields(
            value,
            {
                "amount_usd",
                "partial_amount_usd",
                "complete",
                "missing_components",
                "components_usd",
                "catalog",
            },
            "estimated cost",
        )
        complete = _boolean(value["complete"], "estimated cost complete")
        amount = value["amount_usd"]
        if amount is not None:
            _non_negative_number(amount, "estimated cost amount_usd")
        partial = _non_negative_number(
            value["partial_amount_usd"], "estimated cost partial_amount_usd"
        )
        missing = value["missing_components"]
        if not isinstance(missing, list) or any(
            item not in ("input_tokens", "cached_input_tokens", "output_tokens")
            for item in missing
        ):
            raise ContractViolation("estimated cost has invalid missing_components")
        components = value["components_usd"]
        if not isinstance(components, dict) or any(
            name not in ("input", "cached_input", "output")
            for name in components
        ):
            raise ContractViolation("estimated cost has invalid components_usd")
        for component in components.values():
            _non_negative_number(component, "estimated cost component")
        if complete and (amount is None or missing or amount != partial):
            raise ContractViolation("complete estimated cost is inconsistent")
        if not complete and amount is not None:
            raise ContractViolation("partial estimated cost cannot have amount_usd")
        PriceCatalog.from_dict(value["catalog"])

    @classmethod
    def _cohort_summary(
        cls,
        protocol: BenchmarkProtocol,
        snapshots: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        by_concurrency = {
            str(concurrency): cls._summary(
                tuple(
                    item
                    for item in snapshots
                    if item["concurrency"] == concurrency
                )
            )
            for concurrency in protocol.concurrency_matrix
        }
        return {
            "protocol": protocol.to_dict(),
            "matrix_complete": all(
                item["runs"] >= protocol.repetitions_per_cell
                for item in by_concurrency.values()
            ),
            "overall": cls._summary(snapshots),
            "by_concurrency": by_concurrency,
        }

    @classmethod
    def _summary(
        cls, snapshots: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any]:
        verified = [
            item
            for item in snapshots
            if item["outcome"]["success"] and item["outcome"]["verified"]
        ]
        verified_count = len(verified)
        durations = [item["latency"]["wall_clock_seconds"] for item in snapshots]
        total_tokens = cls._complete_usage_sum(snapshots, "total_tokens")
        input_tokens = cls._complete_usage_sum(snapshots, "input_tokens")
        cached_tokens = cls._complete_usage_sum(snapshots, "cached_input_tokens")
        output_tokens = cls._complete_usage_sum(snapshots, "output_tokens")
        model_calls = cls._complete_optional_sum(snapshots, "model_calls")
        human_actions = cls._complete_nested_sum(
            snapshots, "intervention", "human_actions"
        )
        reuse = {
            field: cls._complete_nested_sum(snapshots, "reuse", field)
            for field in (
                "hit",
                "miss",
                "coalesced",
                "bypassed",
                "none",
                "saved_tokens",
            )
        }
        latency_summaries = {
            field: cls._complete_distribution(snapshots, "latency", field)
            for field in (
                "queue_wait_seconds",
                "model_seconds",
                "critical_path_seconds",
            )
        }
        cost_sources = {
            source: sum(1 for item in snapshots if item["cost"]["source"] == source)
            for source in ("measured", "estimated", "unknown")
        }
        complete_costs = all(item["cost"]["complete"] for item in snapshots)
        used_sources = [source for source, count in cost_sources.items() if count]
        uniform_cost_source = used_sources[0] if len(used_sources) == 1 else "mixed"
        total_cost = (
            sum(item["cost"]["amount_usd"] for item in snapshots)
            if complete_costs
            and uniform_cost_source in ("measured", "estimated")
            else None
        )
        warning_counts = {
            warning: sum(
                warning in item.get("warnings", []) for item in snapshots
            )
            for warning in sorted(_PRICE_CATALOG_WARNINGS)
        }
        warning_counts = {
            warning: count
            for warning, count in warning_counts.items()
            if count
        }
        return {
            "runs": len(snapshots),
            "verified_results": verified_count,
            "verified_success_rate": (
                verified_count / len(snapshots) if snapshots else None
            ),
            "failed_run_ids": [
                item["run_id"] for item in snapshots if item not in verified
            ],
            "warnings": {
                "counts": warning_counts,
                "affected_run_ids": [
                    item["run_id"]
                    for item in snapshots
                    if item.get("warnings", [])
                ],
            },
            "tokens": {
                "input": input_tokens,
                "cached_input": cached_tokens,
                "output": output_tokens,
                "total": total_tokens,
                "per_model_call": cls._divide(total_tokens, model_calls),
                "per_verified_result": cls._divide(total_tokens, verified_count),
            },
            "model_calls": model_calls,
            "reuse": reuse,
            "cost": {
                "sources": cost_sources,
                "source": uniform_cost_source,
                "total_usd": total_cost,
                "per_verified_result_usd": cls._divide(
                    total_cost, verified_count
                ),
            },
            "wall_clock_seconds": {
                "median": statistics.median(durations) if durations else None,
                "p95": cls._percentile(durations, 0.95),
            },
            **latency_summaries,
            "human_actions": {
                "total": human_actions,
                "per_verified_result": cls._divide(
                    human_actions, verified_count
                ),
            },
        }

    @staticmethod
    def _complete_usage_sum(
        snapshots: Sequence[Mapping[str, Any]], field: str
    ) -> Optional[int]:
        complete_field = f"{field}_complete"
        if not snapshots or not all(
            item["usage"][complete_field] for item in snapshots
        ):
            return None
        return sum(item["usage"][field] for item in snapshots)

    @staticmethod
    def _complete_optional_sum(
        snapshots: Sequence[Mapping[str, Any]], field: str
    ) -> Optional[int]:
        values = [item[field] for item in snapshots]
        if not values or any(value is None for value in values):
            return None
        return sum(values)

    @staticmethod
    def _complete_nested_sum(
        snapshots: Sequence[Mapping[str, Any]], parent: str, field: str
    ) -> Optional[int]:
        values: List[Optional[int]] = [
            item[parent][field] if parent in item else None
            for item in snapshots
        ]
        if not values or any(value is None for value in values):
            return None
        return sum(value for value in values if value is not None)

    @classmethod
    def _complete_distribution(
        cls,
        snapshots: Sequence[Mapping[str, Any]],
        parent: str,
        field: str,
    ) -> Mapping[str, Optional[float]]:
        values = [item.get(parent, {}).get(field) for item in snapshots]
        if not values or any(value is None for value in values):
            return {"median": None, "p95": None}
        return {
            "median": statistics.median(values),
            "p95": cls._percentile(values, 0.95),
        }

    @staticmethod
    def _divide(
        numerator: Optional[float], denominator: Optional[int]
    ) -> Optional[float]:
        if numerator is None or denominator is None or denominator == 0:
            return None
        return numerator / denominator

    @staticmethod
    def _percentile(values: Sequence[float], fraction: float) -> Optional[float]:
        if not values:
            return None
        ordered = sorted(values)
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    @classmethod
    def _roi(
        cls, baseline: Mapping[str, Any], candidate: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        baseline_overall = baseline["overall"]
        candidate_overall = candidate["overall"]
        baseline_cost = baseline_overall["cost"]
        candidate_cost = candidate_overall["cost"]
        comparable_cost = (
            baseline_cost["source"] == candidate_cost["source"]
            and baseline_cost["source"] in ("measured", "estimated")
        )
        return {
            "verified_success_rate_delta": cls._difference(
                candidate_overall["verified_success_rate"],
                baseline_overall["verified_success_rate"],
            ),
            "token_reduction_fraction": cls._reduction(
                baseline_overall["tokens"]["per_verified_result"],
                candidate_overall["tokens"]["per_verified_result"],
            ),
            "wall_clock_median_reduction_fraction": cls._reduction(
                baseline_overall["wall_clock_seconds"]["median"],
                candidate_overall["wall_clock_seconds"]["median"],
            ),
            "queue_wait_median_reduction_fraction": cls._reduction(
                baseline_overall["queue_wait_seconds"]["median"],
                candidate_overall["queue_wait_seconds"]["median"],
            ),
            "model_median_reduction_fraction": cls._reduction(
                baseline_overall["model_seconds"]["median"],
                candidate_overall["model_seconds"]["median"],
            ),
            "critical_path_median_reduction_fraction": cls._reduction(
                baseline_overall["critical_path_seconds"]["median"],
                candidate_overall["critical_path_seconds"]["median"],
            ),
            "human_action_reduction_fraction": cls._reduction(
                baseline_overall["human_actions"]["per_verified_result"],
                candidate_overall["human_actions"]["per_verified_result"],
            ),
            "cost_reduction_fraction": (
                cls._reduction(
                    baseline_cost["per_verified_result_usd"],
                    candidate_cost["per_verified_result_usd"],
                )
                if comparable_cost
                else None
            ),
            "cost_roi_unavailable_reason": (
                None if comparable_cost else "cost sources are unknown or not comparable"
            ),
        }

    @staticmethod
    def _difference(
        candidate: Optional[float], baseline: Optional[float]
    ) -> Optional[float]:
        if candidate is None or baseline is None:
            return None
        return candidate - baseline

    @staticmethod
    def _reduction(
        baseline: Optional[float], candidate: Optional[float]
    ) -> Optional[float]:
        if baseline is None or candidate is None or baseline == 0:
            return None
        return (baseline - candidate) / baseline

    @classmethod
    def _cost_snapshot(
        cls, usage: ModelUsage, prices: Optional[PriceCatalog]
    ) -> Mapping[str, Any]:
        measured = {
            "amount_usd": usage.cost_usd,
            "complete": usage.cost_complete,
        }
        estimated = cls._estimate_cost(usage, prices) if prices is not None else None
        if usage.cost_complete:
            source = "measured"
            amount = usage.cost_usd
            complete = True
        elif estimated is not None:
            source = "estimated"
            amount = estimated["amount_usd"] if estimated["complete"] else None
            complete = estimated["complete"]
        else:
            source = "unknown"
            amount = None
            complete = False
        return {
            "source": source,
            "amount_usd": amount,
            "complete": complete,
            "measured": measured,
            "estimated": estimated,
        }

    @staticmethod
    def _estimate_cost(
        usage: ModelUsage, prices: PriceCatalog
    ) -> Mapping[str, Any]:
        missing: List[str] = []
        components: Dict[str, float] = {}
        input_tokens = usage.input_tokens
        cached_tokens = usage.cached_input_tokens

        if prices.cached_input_mode == "included_in_input":
            if (
                usage.input_tokens_complete
                and usage.cached_input_tokens_complete
                and input_tokens is not None
                and cached_tokens is not None
            ):
                if cached_tokens > input_tokens:
                    raise ContractViolation(
                        "cached input tokens cannot exceed included input tokens"
                    )
                components["input"] = (
                    (input_tokens - cached_tokens)
                    * prices.input_usd_per_million
                    / 1_000_000
                )
                components["cached_input"] = (
                    cached_tokens
                    * prices.cached_input_usd_per_million
                    / 1_000_000
                )
            else:
                missing.extend(("input_tokens", "cached_input_tokens"))
        else:
            if usage.input_tokens_complete and input_tokens is not None:
                components["input"] = (
                    input_tokens * prices.input_usd_per_million / 1_000_000
                )
            else:
                missing.append("input_tokens")
            if usage.cached_input_tokens_complete and cached_tokens is not None:
                components["cached_input"] = (
                    cached_tokens
                    * prices.cached_input_usd_per_million
                    / 1_000_000
                )
            else:
                missing.append("cached_input_tokens")

        if usage.output_tokens_complete and usage.output_tokens is not None:
            components["output"] = (
                usage.output_tokens * prices.output_usd_per_million / 1_000_000
            )
        else:
            missing.append("output_tokens")

        partial_amount = sum(components.values())
        complete = not missing
        return {
            "amount_usd": partial_amount if complete else None,
            "partial_amount_usd": partial_amount,
            "complete": complete,
            "missing_components": sorted(set(missing)),
            "components_usd": components,
            "catalog": prices.to_dict(),
        }
