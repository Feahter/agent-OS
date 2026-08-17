import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Protocol


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
        line = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def read(self) -> Iterable[Mapping[str, Any]]:
        if not self.path.exists():
            return ()
        with self.path.open(encoding="utf-8") as handle:
            return tuple(json.loads(line) for line in handle if line.strip())
