import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .artifacts import ArtifactRecord
from .errors import ContractViolation


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "graph_fingerprint": self.graph_fingerprint,
            "run_id": self.run_id,
            "statuses": dict(self.statuses),
            "attempts": dict(self.attempts),
            "tokens_used": self.tokens_used,
            "cost_usd": self.cost_usd,
            "artifacts": [record.to_dict() for record in self.artifacts],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Checkpoint":
        return cls(
            graph_id=str(value["graph_id"]),
            graph_fingerprint=str(value["graph_fingerprint"]),
            run_id=str(value["run_id"]),
            statuses={str(key): str(item) for key, item in value["statuses"].items()},
            attempts={str(key): int(item) for key, item in value["attempts"].items()},
            tokens_used=int(value["tokens_used"]),
            cost_usd=float(value.get("cost_usd", 0.0)),
            artifacts=tuple(ArtifactRecord.from_dict(item) for item in value["artifacts"]),
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent))
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(checkpoint.to_dict(), handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
