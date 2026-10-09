import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from ._store import atomic_json_write
from .agents import ModelUsage
from .artifacts import ArtifactRecord
from .errors import ContractViolation
from .schemas import CHECKPOINT_SCHEMA_VERSION, CheckpointDocumentV1


@dataclass(frozen=True)
class Checkpoint:
    graph_id: str
    graph_fingerprint: str
    run_id: str
    statuses: Mapping[str, str]
    attempts: Mapping[str, int]
    tokens_used: int
    cost_usd: float
    artifacts: Tuple[ArtifactRecord, ...]
    usage: ModelUsage = field(default_factory=ModelUsage.no_call)
    in_flight: Optional[Mapping[str, Mapping[str, Any]]] = None

    def to_dict(self) -> CheckpointDocumentV1:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "graph_id": self.graph_id,
            "graph_fingerprint": self.graph_fingerprint,
            "run_id": self.run_id,
            "statuses": dict(self.statuses),
            "attempts": dict(self.attempts),
            "tokens_used": self.tokens_used,
            "cost_usd": self.cost_usd,
            "usage": self.usage.to_dict(),
            "artifacts": [record.to_dict() for record in self.artifacts],
            "in_flight": dict(self.in_flight) if self.in_flight is not None else None,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Checkpoint":
        schema_version = value.get("schema_version", 0)
        if (
            isinstance(schema_version, bool)
            or schema_version not in (0, CHECKPOINT_SCHEMA_VERSION)
        ):
            raise ContractViolation("unsupported checkpoint schema")
        tokens_used = int(value["tokens_used"])
        cost_usd = float(value.get("cost_usd", 0.0))
        usage = ModelUsage.from_persisted(
            value.get("usage"),
            tokens_used,
            cost_usd,
            value.get("cost_complete")
            if isinstance(value.get("cost_complete"), bool)
            else None,
        )
        in_flight = value.get("in_flight")
        if in_flight is not None:
            if not isinstance(in_flight, dict):
                raise ContractViolation("checkpoint in_flight must be an object")
            if set(in_flight) != {key for key, status in value["statuses"].items() if status == "running"}:
                raise ContractViolation("checkpoint running nodes must have reservations")
            for node_id, reservation in in_flight.items():
                if not isinstance(reservation, dict) or set(reservation) != {
                    "attempt", "tokens", "cost_usd"
                }:
                    raise ContractViolation("invalid checkpoint reservation")
                for key in ("attempt", "tokens"):
                    item = reservation[key]
                    minimum = 1 if key == "attempt" else 0
                    if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
                        raise ContractViolation("invalid checkpoint reservation count")
                cost = reservation["cost_usd"]
                if (
                    isinstance(cost, bool) or not isinstance(cost, (int, float))
                    or not math.isfinite(cost) or cost < 0
                    or value["statuses"].get(node_id) != "running"
                    or reservation["attempt"] != value["attempts"].get(node_id)
                ):
                    raise ContractViolation("checkpoint reservation identity mismatch")
        return cls(
            graph_id=str(value["graph_id"]),
            graph_fingerprint=str(value["graph_fingerprint"]),
            run_id=str(value["run_id"]),
            statuses={str(key): str(item) for key, item in value["statuses"].items()},
            attempts={str(key): int(item) for key, item in value["attempts"].items()},
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            artifacts=tuple(ArtifactRecord.from_dict(item) for item in value["artifacts"]),
            usage=usage,
            in_flight=in_flight,
        )


class CheckpointStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Optional[Checkpoint]:
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return Checkpoint.from_dict(value)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ContractViolation(f"invalid checkpoint: {error}") from error

    def save(self, checkpoint: Checkpoint) -> None:
        atomic_json_write(self.path, checkpoint.to_dict(), label="checkpoint")
