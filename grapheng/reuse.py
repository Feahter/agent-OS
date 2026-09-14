import hashlib
import json
import math
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence, Tuple

from ._store import (
    atomic_json_write,
    file_lock,
)
from .agents import AgentRequest, AgentResult, validate_agent_outputs
from .errors import ContractViolation
from .singleflight import SingleFlightCoordinator, SingleFlightTimeoutError

REUSE_SCHEMA_VERSION = 1
REUSABLE_CLASSIFICATIONS = ("public", "internal")
MUTATING_TOOLS = frozenset({"edit", "shell", "write"})


def _canonical(value: Any) -> Tuple[Any, str]:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise ContractViolation(
            f"reuse values must be JSON serializable: {error}"
        ) from error
    return json.loads(encoded), hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerifiedReuseRecord:
    key: str
    key_fields: Mapping[str, Any]
    executor_id: str
    outputs: Mapping[str, Any]
    created_at: float
    expires_at: float
    source_task_id: str
    source_run_id: str
    verification_id: str
    quality_score: float
    original_tokens: int
    original_cost_usd: Optional[float]
    checksum: str
    schema_version: int = REUSE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "VerifiedReuseRecord":
        record = cls(
            key=str(value["key"]),
            key_fields=dict(value["key_fields"]),
            executor_id=str(value["executor_id"]),
            outputs=dict(value["outputs"]),
            created_at=float(value["created_at"]),
            expires_at=float(value["expires_at"]),
            source_task_id=str(value["source_task_id"]),
            source_run_id=str(value["source_run_id"]),
            verification_id=str(value["verification_id"]),
            quality_score=float(value["quality_score"]),
            original_tokens=int(value["original_tokens"]),
            original_cost_usd=(
                None
                if value.get("original_cost_usd") is None
                else float(value["original_cost_usd"])
            ),
            checksum=str(value["checksum"]),
            schema_version=int(value.get("schema_version", 0)),
        )
        record.validate()
        return record

    def validate(self) -> None:
        if self.schema_version != REUSE_SCHEMA_VERSION:
            raise ContractViolation("unsupported verified reuse record schema")
        if not self.source_task_id or not self.source_run_id or not self.verification_id:
            raise ContractViolation("verified reuse provenance cannot be empty")
        if (
            not math.isfinite(self.quality_score)
            or not 0 <= self.quality_score <= 1
        ):
            raise ContractViolation("verified reuse quality_score must be between zero and one")
        if self.original_tokens < 0:
            raise ContractViolation("verified reuse original_tokens cannot be negative")
        if self.original_cost_usd is not None and (
            not math.isfinite(self.original_cost_usd) or self.original_cost_usd < 0
        ):
            raise ContractViolation("verified reuse original_cost_usd cannot be negative")
        if self.expires_at <= self.created_at:
            raise ContractViolation("verified reuse expiry must be after creation")
        _, key_digest = _canonical(self.key_fields)
        if key_digest != self.key:
            raise ContractViolation("verified reuse key digest mismatch")
        payload = self.to_dict()
        payload.pop("checksum")
        _, checksum = _canonical(payload)
        if checksum != self.checksum:
            raise ContractViolation("verified reuse checksum mismatch")


@dataclass
class _Flight:
    source_task_id: str
    completed: threading.Event
    result: Optional[AgentResult] = None
    error: Optional[BaseException] = None
    saved_tokens: int = 0
    saved_cost_usd: Optional[float] = None


class VerifiedArtifactCache:
    """Persists only Reality-Anchor-verified results and coalesces identical calls."""

    def __init__(
        self,
        root: Path,
        ttl_seconds: int = 7 * 24 * 60 * 60,
        clock: Callable[[], float] = time.time,
        singleflight: Optional[SingleFlightCoordinator] = None,
    ):
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, int)
            or ttl_seconds < 1
        ):
            raise ContractViolation("reuse ttl_seconds must be a positive integer")
        self.root = root
        self.entries = root / "entries"
        self.entries.mkdir(parents=True, exist_ok=True)
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._journal_lock = threading.Lock()
        self._flights: Dict[str, _Flight] = {}
        self.singleflight = singleflight or SingleFlightCoordinator(root / "flights")

    def resolve(
        self,
        request: AgentRequest,
        executor_id: str,
        loader: Callable[[], AgentResult],
    ) -> AgentResult:
        bypass_reason = self.bypass_reason(request)
        if bypass_reason is not None:
            result = loader()
            self._record_event(
                request, result.executor_id, "bypassed", bypass_reason
            )
            return replace(
                result, reuse_status="bypassed", reuse_saved_tokens=0
            )

        key, key_fields = self._key(request, executor_id)
        record, miss_reason = self._read(key, key_fields)
        if record is not None:
            self._record_event(
                request,
                executor_id,
                "hit",
                "verified",
                record.original_tokens,
                record.original_cost_usd,
            )
            return self._result_from_record(record)

        with self._lock:
            existing = self._flights.get(key)
            leader = existing is None
            if existing is None:
                flight = _Flight(request.task_id, threading.Event())
                self._flights[key] = flight
            else:
                flight = existing
        if not leader:
            if not flight.completed.wait(request.timeout_seconds):
                raise SingleFlightTimeoutError(
                    f"in-process single-flight wait for {key} exceeded timeout"
                )
            if flight.error is not None:
                raise flight.error
            if flight.result is None:
                raise ContractViolation("coalesced agent execution produced no result")
            self._record_event(
                request,
                executor_id,
                "coalesced",
                "in_flight",
                flight.saved_tokens,
                flight.saved_cost_usd,
            )
            return self._coalesced_result(
                flight.result,
                flight.result.source_task_id or flight.source_task_id,
            )

        try:
            delivery = self.singleflight.execute(
                key,
                request.task_id,
                lambda: self._transient_result(loader()),
                timeout_seconds=request.timeout_seconds,
            )
            result = self._result_from_transient(delivery.payload, executor_id)
            flight.saved_tokens = result.tokens_used
            flight.saved_cost_usd = result.cost_usd
            if delivery.coalesced:
                flight.source_task_id = delivery.source_id
                result = self._coalesced_result(result, delivery.source_id)
                flight.result = result
                self._record_event(
                    request,
                    result.executor_id,
                    "coalesced",
                    "cross_process",
                    flight.saved_tokens,
                    flight.saved_cost_usd,
                )
                return result
            flight.result = result
            self._record_event(request, result.executor_id, "miss", miss_reason)
            return replace(result, reuse_status="miss", reuse_saved_tokens=0)
        except BaseException as error:
            flight.error = error
            raise
        finally:
            flight.completed.set()
            with self._lock:
                self._flights.pop(key, None)

    def publish_verified(
        self,
        request: AgentRequest,
        result: AgentResult,
        source_run_id: str,
        verification_id: str,
        quality_score: float,
    ) -> VerifiedReuseRecord:
        reason = self.bypass_reason(request)
        if reason is not None:
            raise ContractViolation(f"request is not eligible for verified reuse: {reason}")
        if not source_run_id.strip() or not verification_id.strip():
            raise ContractViolation("verified reuse provenance cannot be empty")
        if (
            isinstance(quality_score, bool)
            or not isinstance(quality_score, (int, float))
            or not math.isfinite(quality_score)
            or not 0 <= quality_score <= 1
        ):
            raise ContractViolation("verified reuse quality_score must be between zero and one")
        outputs = validate_agent_outputs(result.outputs, request.output_keys)
        normalized_outputs, _ = _canonical(outputs)
        key, key_fields = self._key(request, result.executor_id)
        existing, _ = self._read(key, key_fields)
        if (
            existing is not None
            and existing.source_task_id == request.task_id
            and existing.source_run_id == source_run_id.strip()
            and existing.verification_id == verification_id.strip()
            and existing.outputs == normalized_outputs
        ):
            self._record_event(
                request, result.executor_id, "publish_replayed", "idempotent"
            )
            return existing
        created_at = self._clock()
        value = {
            "key": key,
            "key_fields": key_fields,
            "executor_id": result.executor_id,
            "outputs": normalized_outputs,
            "created_at": created_at,
            "expires_at": created_at + self._ttl_seconds,
            "source_task_id": request.task_id,
            "source_run_id": source_run_id.strip(),
            "verification_id": verification_id.strip(),
            "quality_score": float(quality_score),
            "original_tokens": result.tokens_used,
            "original_cost_usd": result.cost_usd,
            "schema_version": REUSE_SCHEMA_VERSION,
        }
        _, checksum = _canonical(value)
        record = VerifiedReuseRecord(checksum=checksum, **value)
        record.validate()
        atomic_json_write(self.entries / f"{key}.json", record.to_dict())
        self._record_event(request, result.executor_id, "published", "verified")
        return record

    def status(self) -> Mapping[str, Any]:
        valid = 0
        expired = 0
        invalid = 0
        for path in sorted(self.entries.glob("*.json")):
            try:
                record = VerifiedReuseRecord.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
                if record.expires_at <= self._clock():
                    expired += 1
                else:
                    valid += 1
            except (OSError, ValueError, KeyError, TypeError, ContractViolation):
                invalid += 1
        events = {"hit": 0, "miss": 0, "coalesced": 0, "bypassed": 0}
        saved_tokens = 0
        saved_cost_usd = 0.0
        saved_cost_complete = True
        event_path = self.root / "events.jsonl"
        try:
            with self._journal_guard(shared=True):
                event_lines: Sequence[str] = (
                    event_path.read_text(encoding="utf-8").splitlines()
                    if event_path.exists()
                    else ()
                )
            for line in event_lines:
                if not line.strip():
                    continue
                event = json.loads(line)
                status = event.get("status")
                if status in events:
                    events[status] += 1
                saved_tokens += int(event.get("saved_tokens", 0))
                if status in ("hit", "coalesced"):
                    raw_cost = event.get("saved_cost_usd")
                    if event.get("saved_cost_complete") is not True:
                        # Legacy journals cannot distinguish an unknown cost
                        # from the old zero placeholder, so stay conservative.
                        saved_cost_complete = False
                    if raw_cost is not None:
                        if (
                            isinstance(raw_cost, bool)
                            or not isinstance(raw_cost, (int, float))
                            or not math.isfinite(raw_cost)
                            or raw_cost < 0
                        ):
                            raise ValueError("saved_cost_usd must be non-negative")
                        saved_cost_usd += float(raw_cost)
                    elif event.get("saved_cost_complete") is True:
                        raise ValueError(
                            "saved_cost_complete requires saved_cost_usd"
                        )
        except (OSError, ValueError, TypeError) as error:
            raise ContractViolation(f"invalid reuse event journal: {error}") from error
        return {
            "entries": {"valid": valid, "expired": expired, "invalid": invalid},
            "events": events,
            "saved_tokens": saved_tokens,
            "saved_cost_usd": (
                saved_cost_usd if saved_cost_complete else None
            ),
            "saved_cost_complete": saved_cost_complete,
            "singleflight": self.singleflight.status(),
        }

    @staticmethod
    def bypass_reason(request: AgentRequest) -> Optional[str]:
        """Explain why a request cannot safely skip its Agent execution."""

        if not request.reuse_allowed:
            return "request_disabled"
        if request.data_classification not in REUSABLE_CLASSIFICATIONS:
            return f"classification={request.data_classification}"
        mutating = sorted(set(request.tools) & MUTATING_TOOLS)
        if mutating:
            return f"mutating_tools={','.join(mutating)}"
        return None

    def _read(
        self, key: str, expected_fields: Mapping[str, Any]
    ) -> Tuple[Optional[VerifiedReuseRecord], str]:
        path = self.entries / f"{key}.json"
        if not path.exists():
            return None, "not_found"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return None, "invalid_record"
            record = VerifiedReuseRecord.from_dict(value)
        except (OSError, ValueError, KeyError, TypeError, ContractViolation):
            return None, "invalid_record"
        if record.key_fields != expected_fields:
            return None, "policy_mismatch"
        if record.expires_at <= self._clock():
            return None, "expired"
        return record, "verified"

    @staticmethod
    def _key(
        request: AgentRequest, executor_id: str
    ) -> Tuple[str, Mapping[str, Any]]:
        _, prompt_digest = _canonical(request.prompt)
        _, input_digest = _canonical(request.inputs)
        fields = {
            "prompt_digest": prompt_digest,
            "input_digest": input_digest,
            "task_type": request.task_type,
            "executor_id": executor_id,
            "model_family": request.model_family,
            "model": request.model,
            "reasoning_effort": request.reasoning_effort,
            "max_tokens": request.max_tokens,
            "max_cost_usd": request.max_cost_usd,
            "tools": sorted(set(request.tools)),
            "output_contract": sorted(set(request.output_keys)),
            "data_classification": request.data_classification,
            "reuse_scope": request.reuse_scope,
        }
        _, key = _canonical(fields)
        return key, fields

    @staticmethod
    def _result_from_record(record: VerifiedReuseRecord) -> AgentResult:
        return AgentResult(
            executor_id=record.executor_id,
            outputs=record.outputs,
            text=json.dumps(record.outputs, ensure_ascii=False, sort_keys=True),
            tokens_used=0,
            cost_usd=0.0,
            reuse_status="hit",
            source_task_id=record.source_task_id,
            source_run_id=record.source_run_id,
            verification_id=record.verification_id,
            reuse_saved_tokens=record.original_tokens,
        )

    @staticmethod
    def _transient_result(result: AgentResult) -> Mapping[str, Any]:
        outputs, _ = _canonical(result.outputs)
        return {
            "executor_id": result.executor_id,
            "outputs": outputs,
            "tokens_used": result.tokens_used,
            "cost_usd": result.cost_usd,
        }

    @staticmethod
    def _result_from_transient(
        value: Mapping[str, Any], expected_executor_id: str
    ) -> AgentResult:
        expected = {"executor_id", "outputs", "tokens_used", "cost_usd"}
        if set(value) != expected or value.get("executor_id") != expected_executor_id:
            raise ContractViolation("single-flight agent result contract mismatch")
        outputs = value.get("outputs")
        if not isinstance(outputs, dict):
            raise ContractViolation("single-flight agent outputs must be an object")
        return AgentResult(
            executor_id=expected_executor_id,
            outputs=outputs,
            text=json.dumps(outputs, ensure_ascii=False, sort_keys=True),
            tokens_used=value["tokens_used"],
            cost_usd=value["cost_usd"],
        )

    @staticmethod
    def _coalesced_result(result: AgentResult, source_task_id: str) -> AgentResult:
        outputs, _ = _canonical(result.outputs)
        saved_tokens = (
            result.tokens_used
            if result.tokens_used > 0
            else result.reuse_saved_tokens
        )
        return AgentResult(
            executor_id=result.executor_id,
            outputs=outputs,
            text=json.dumps(outputs, ensure_ascii=False, sort_keys=True),
            tokens_used=0,
            cost_usd=0.0,
            reuse_status="coalesced",
            source_task_id=source_task_id,
            source_run_id=result.source_run_id,
            verification_id=result.verification_id,
            reuse_saved_tokens=saved_tokens,
        )

    def _record_event(
        self,
        request: AgentRequest,
        executor_id: str,
        status: str,
        reason: str,
        saved_tokens: int = 0,
        saved_cost_usd: Optional[float] = None,
    ) -> None:
        event = {
            "observed_at": self._clock(),
            "task_id": request.task_id,
            "executor_id": executor_id,
            "status": status,
            "reason": reason,
            "data_classification": request.data_classification,
            "reuse_scope": request.reuse_scope,
            "saved_tokens": saved_tokens if status in ("hit", "coalesced") else 0,
            "saved_cost_usd": (
                saved_cost_usd
                if status in ("hit", "coalesced")
                else 0.0
            ),
            "saved_cost_complete": (
                saved_cost_usd is not None
                if status in ("hit", "coalesced")
                else True
            ),
        }
        line = json.dumps(
            event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8") + b"\n"
        path = self.root / "events.jsonl"
        with self._journal_guard():
            event_descriptor = os.open(
                str(path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600
            )
            try:
                os.write(event_descriptor, line)
                os.fsync(event_descriptor)
            finally:
                os.close(event_descriptor)

    @contextmanager
    def _journal_guard(self, *, shared: bool = False) -> Iterator[None]:
        lock_path = self.singleflight.root / "events.lock"
        with self._journal_lock:
            with file_lock(lock_path, shared=shared):
                yield


ReuseStore = VerifiedArtifactCache
