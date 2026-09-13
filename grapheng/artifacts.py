import hashlib
import json
import threading
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import ContractViolation


def _normalize(value: Any) -> Tuple[Any, str]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ContractViolation(f"artifact values must be JSON serializable: {error}") from error
    normalized = json.loads(encoded)
    checksum = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return normalized, checksum


@dataclass(frozen=True)
class ArtifactRecord:
    key: str
    value: Any
    producer: str
    version: int
    checksum: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactRecord":
        return cls(
            key=str(value["key"]),
            value=value["value"],
            producer=str(value["producer"]),
            version=int(value["version"]),
            checksum=str(value["checksum"]),
        )


class ArtifactStore:
    def __init__(self, records: Iterable[ArtifactRecord] = ()):
        self._lock = threading.Lock()
        self._records: Dict[str, List[ArtifactRecord]] = {}
        for record in sorted(records, key=lambda item: (item.key, item.version)):
            normalized, checksum = _normalize(record.value)
            if checksum != record.checksum:
                raise ContractViolation(f"artifact {record.key} checksum mismatch")
            if record.version < 1:
                raise ContractViolation(f"artifact {record.key} has an invalid version")
            restored = ArtifactRecord(
                record.key,
                normalized,
                record.producer,
                record.version,
                record.checksum,
            )
            versions = self._records.setdefault(record.key, [])
            if any(item.version == restored.version for item in versions):
                raise ContractViolation(
                    f"artifact {record.key} has duplicate version {record.version}"
                )
            versions.append(restored)

    def read(self, key: str) -> Any:
        with self._lock:
            versions = self._records.get(key)
            if not versions:
                raise ContractViolation(f"artifact {key} does not exist")
            normalized, checksum = _normalize(versions[-1].value)
            if checksum != versions[-1].checksum:
                raise ContractViolation(f"artifact {key} checksum mismatch")
            return normalized

    def snapshot(self, keys: Iterable[str]) -> Tuple[ArtifactRecord, ...]:
        with self._lock:
            result = []
            for key in sorted(keys):
                versions = self._records.get(key)
                if not versions:
                    raise ContractViolation(f"artifact {key} does not exist")
                record = versions[-1]
                normalized, checksum = _normalize(record.value)
                if checksum != record.checksum:
                    raise ContractViolation(f"artifact {key} checksum mismatch")
                result.append(
                    ArtifactRecord(
                        record.key,
                        normalized,
                        record.producer,
                        record.version,
                        record.checksum,
                    )
                )
            return tuple(result)

    def record(self, key: str, version: int, checksum: str) -> Optional[ArtifactRecord]:
        with self._lock:
            for record in self._records.get(key, ()):
                if record.version == version and record.checksum == checksum:
                    normalized, actual = _normalize(record.value)
                    if actual != checksum:
                        raise ContractViolation(f"artifact {key} checksum mismatch")
                    return ArtifactRecord(
                        record.key,
                        normalized,
                        record.producer,
                        record.version,
                        record.checksum,
                    )
        return None

    def commit_batch(self, outputs: Mapping[str, Any], producer: str) -> Tuple[ArtifactRecord, ...]:
        normalized = {key: _normalize(value) for key, value in outputs.items()}
        committed = []
        with self._lock:
            for key in sorted(normalized):
                value, checksum = normalized[key]
                version = max(
                    (record.version for record in self._records.get(key, ())),
                    default=0,
                ) + 1
                record = ArtifactRecord(key, value, producer, version, checksum)
                self._records.setdefault(key, []).append(record)
                committed.append(record)
        return tuple(committed)

    def latest_records(self) -> Tuple[ArtifactRecord, ...]:
        return self.snapshot(sorted(self._records))

    def records(self) -> Tuple[ArtifactRecord, ...]:
        with self._lock:
            return tuple(
                ArtifactRecord(
                    record.key,
                    deepcopy(record.value),
                    record.producer,
                    record.version,
                    record.checksum,
                )
                for key in sorted(self._records)
                for record in self._records[key]
            )

    def values(self) -> Dict[str, Any]:
        return {record.key: self.read(record.key) for record in self.latest_records()}

    def latest(self, key: str) -> Optional[ArtifactRecord]:
        with self._lock:
            versions = self._records.get(key)
            if not versions:
                return None
            record = versions[-1]
            normalized, checksum = _normalize(record.value)
            if checksum != record.checksum:
                raise ContractViolation(f"artifact {key} checksum mismatch")
            return ArtifactRecord(
                record.key,
                normalized,
                record.producer,
                record.version,
                record.checksum,
            )
