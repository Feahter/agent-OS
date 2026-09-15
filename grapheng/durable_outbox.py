"""Durable owner-side intents with idempotent consumer acknowledgments."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Tuple, cast

from ._store import atomic_json_write, file_lock, json_digest, read_json_object
from .errors import ContractViolation

OUTBOX_SCHEMA_VERSION = 1
_INTENT_ID = re.compile(r"^[a-z][a-z0-9-]{7,127}$")
_KINDS = {"enqueue", "control"}
_STATES = {"pending", "acknowledged"}


class DurableOutbox:
    """Append intents under one owner and retain them after acknowledgment."""

    def __init__(
        self, owner_root: Path, clock: Callable[[], float] = time.time
    ) -> None:
        self.root = Path(owner_root) / "outbox"
        self.lock_path = self.root / "outbox.lock"
        self.delivery_lock_path = self.root / "delivery.lock"
        self._clock = clock

    def publish(
        self,
        intent_id: str,
        task_id: str,
        kind: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._validate_identity(intent_id, task_id, kind)
        if not isinstance(payload, Mapping):
            raise ContractViolation("outbox intent payload must be an object")
        normalized_payload, payload_digest = json_digest(
            dict(payload), label="outbox intent payload"
        )
        path = self._path(intent_id)
        with file_lock(self.lock_path):
            if path.exists():
                existing = self._read(path)
                if (
                    existing["task_id"] != task_id
                    or existing["kind"] != kind
                    or existing["payload_digest"] != payload_digest
                    or existing["payload"] != normalized_payload
                ):
                    raise ContractViolation(
                        f"outbox intent {intent_id} was reused with different input"
                    )
                return existing
            sequence = 1 + max(
                (
                    self._read(existing_path)["sequence"]
                    for existing_path in self.root.glob("*.json")
                    if existing_path.is_file()
                ),
                default=0,
            )
            record = {
                "schema_version": OUTBOX_SCHEMA_VERSION,
                "intent_id": intent_id,
                "task_id": task_id,
                "kind": kind,
                "state": "pending",
                "sequence": sequence,
                "payload": normalized_payload,
                "payload_digest": payload_digest,
                "created_at": self._clock(),
                "acknowledged_at": None,
                "acknowledgment": None,
                "acknowledgment_digest": None,
            }
            atomic_json_write(path, record, label="outbox intent")
            return dict(record)

    def acknowledge(
        self, intent_id: str, acknowledgment: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if not isinstance(acknowledgment, Mapping):
            raise ContractViolation("outbox acknowledgment must be an object")
        normalized, digest = json_digest(
            dict(acknowledgment), label="outbox acknowledgment"
        )
        path = self._path(intent_id)
        with file_lock(self.lock_path):
            record = self._read(path)
            if record["state"] == "acknowledged":
                if (
                    record["acknowledgment_digest"] != digest
                    or record["acknowledgment"] != normalized
                ):
                    raise ContractViolation(
                        f"outbox intent {intent_id} has a different acknowledgment"
                    )
                return record
            record.update(
                {
                    "state": "acknowledged",
                    "acknowledged_at": self._clock(),
                    "acknowledgment": normalized,
                    "acknowledgment_digest": digest,
                }
            )
            atomic_json_write(path, record, label="outbox intent")
            return dict(record)

    def pending(self) -> Tuple[Mapping[str, Any], ...]:
        if not self.root.exists():
            return ()
        with file_lock(self.lock_path):
            records = [
                self._read(path)
                for path in sorted(self.root.glob("*.json"))
                if path.is_file()
            ]
        return tuple(
            sorted(
                (record for record in records if record["state"] == "pending"),
                key=lambda record: record["sequence"],
            )
        )

    @contextmanager
    def delivery(self) -> Iterator[None]:
        """Serialize delivery and acknowledgment across recovery processes."""

        with file_lock(self.delivery_lock_path):
            yield

    def inspect(self, intent_id: str) -> Mapping[str, Any]:
        with file_lock(self.lock_path):
            return self._read(self._path(intent_id))

    def _read(self, path: Path) -> Dict[str, Any]:
        value = dict(read_json_object(path, label="outbox intent"))
        required = {
            "schema_version",
            "intent_id",
            "task_id",
            "kind",
            "state",
            "sequence",
            "payload",
            "payload_digest",
            "created_at",
            "acknowledged_at",
            "acknowledgment",
            "acknowledgment_digest",
        }
        if set(value) != required:
            raise ContractViolation("outbox intent has an invalid contract")
        intent_id = value.get("intent_id")
        task_id = value.get("task_id")
        kind = value.get("kind")
        self._validate_identity(intent_id, task_id, kind)
        if path != self._path(cast(str, intent_id)):
            raise ContractViolation("outbox intent path does not match its identity")
        payload = value.get("payload")
        if not isinstance(payload, dict):
            raise ContractViolation("outbox intent payload must be an object")
        normalized_payload, payload_digest = json_digest(
            payload, label="outbox intent payload"
        )
        state = value.get("state")
        sequence = value.get("sequence")
        created_at = value.get("created_at")
        if (
            value.get("schema_version") != OUTBOX_SCHEMA_VERSION
            or state not in _STATES
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
            or value.get("payload_digest") != payload_digest
            or normalized_payload != payload
            or isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
        ):
            raise ContractViolation("outbox intent fields are invalid")
        acknowledged_at = value.get("acknowledged_at")
        acknowledgment = value.get("acknowledgment")
        acknowledgment_digest = value.get("acknowledgment_digest")
        if state == "pending":
            if any(
                item is not None
                for item in (acknowledged_at, acknowledgment, acknowledgment_digest)
            ):
                raise ContractViolation("pending outbox intent has acknowledgment data")
        else:
            if (
                isinstance(acknowledged_at, bool)
                or not isinstance(acknowledged_at, (int, float))
                or not isinstance(acknowledgment, dict)
                or not isinstance(acknowledgment_digest, str)
            ):
                raise ContractViolation("acknowledged outbox intent is incomplete")
            normalized_ack, digest = json_digest(
                acknowledgment, label="outbox acknowledgment"
            )
            if normalized_ack != acknowledgment or digest != acknowledgment_digest:
                raise ContractViolation("outbox acknowledgment digest does not match")
        return value

    def _path(self, intent_id: str) -> Path:
        if not isinstance(intent_id, str) or _INTENT_ID.fullmatch(intent_id) is None:
            raise ContractViolation("invalid outbox intent id")
        return self.root / f"{intent_id}.json"

    @staticmethod
    def _validate_identity(intent_id: Any, task_id: Any, kind: Any) -> None:
        if not isinstance(intent_id, str) or _INTENT_ID.fullmatch(intent_id) is None:
            raise ContractViolation("invalid outbox intent id")
        if not isinstance(task_id, str) or not task_id:
            raise ContractViolation("invalid outbox task id")
        if kind not in _KINDS:
            raise ContractViolation("unsupported outbox intent kind")


__all__ = ["OUTBOX_SCHEMA_VERSION", "DurableOutbox"]
