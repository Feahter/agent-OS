import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol

from ._store import append_jsonl
from .errors import ContractViolation


@dataclass(frozen=True)
class GraphEvent:
    time: str
    event: str
    run_id: str
    graph_id: str
    node_id: Optional[str]
    attempt: Optional[int]
    payload: Mapping[str, Any]

    @classmethod
    def create(
        cls,
        event: str,
        run_id: str,
        graph_id: str,
        node_id: Optional[str] = None,
        attempt: Optional[int] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> "GraphEvent":
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return cls(timestamp, event, run_id, graph_id, node_id, attempt, payload or {})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class EventSink(Protocol):
    def emit(self, event: GraphEvent) -> None:
        ...


class NullEventSink:
    def emit(self, event: GraphEvent) -> None:
        return None


class JsonlEventSink:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: GraphEvent) -> None:
        with self._lock:
            append_jsonl(self.path, event.to_dict(), label="graph event")

    def read(self) -> Iterable[Mapping[str, Any]]:
        if not self.path.exists():
            return ()
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeError) as error:
            raise ContractViolation(f"invalid graph event log: {error}") from error
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines.pop()
        records = []
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ContractViolation(
                    f"invalid graph event log at line {line_number}: {error}"
                ) from error
        return tuple(records)
