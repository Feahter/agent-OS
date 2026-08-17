import fcntl
import hashlib
import json
import math
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple

from .errors import ContractViolation


SINGLE_FLIGHT_SCHEMA_VERSION = 1
_VALID_STATES = ("running", "completed", "failed")


def _canonical(value: Any) -> Tuple[Any, str]:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise ContractViolation(
            f"single-flight payload must be JSON serializable: {error}"
        ) from error
    return json.loads(encoded), hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


class RemoteFlightError(RuntimeError):
    def __init__(self, error_type: str, error_digest: str, source_id: str):
        self.error_type = error_type
        self.error_digest = error_digest
        self.source_id = source_id
        super().__init__(
            f"single-flight leader {source_id} failed with {error_type} "
            f"(digest {error_digest})"
        )


class SingleFlightTimeoutError(TimeoutError):
    pass


@dataclass(frozen=True)
class FlightDelivery:
    payload: Mapping[str, Any]
    source_id: str
    coalesced: bool


class SingleFlightCoordinator:
    """Coordinates one transient execution across local Agent OS processes."""

    def __init__(
        self,
        root: Path,
        lease_seconds: float = 30.0,
        heartbeat_interval: Optional[float] = None,
        poll_interval: float = 0.05,
        delivery_ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        for name, value in (
            ("lease_seconds", lease_seconds),
            ("poll_interval", poll_interval),
            ("delivery_ttl_seconds", delivery_ttl_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ContractViolation(f"single-flight {name} must be positive")
        interval = (
            min(float(lease_seconds) / 3.0, 10.0)
            if heartbeat_interval is None
            else heartbeat_interval
        )
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not math.isfinite(interval)
            or interval <= 0
            or interval >= lease_seconds
        ):
            raise ContractViolation(
                "single-flight heartbeat_interval must be positive and shorter than lease"
            )
        self.root = root
        self.states = root / "states"
        self.locks = root / "locks"
        self.states.mkdir(parents=True, exist_ok=True)
        self.locks.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_interval = float(interval)
        self.poll_interval = float(poll_interval)
        self.delivery_ttl_seconds = float(delivery_ttl_seconds)
        self._clock = clock
        self._monotonic = monotonic

    def execute(
        self,
        key: str,
        source_id: str,
        loader: Callable[[], Mapping[str, Any]],
        timeout_seconds: Optional[float] = None,
    ) -> FlightDelivery:
        self._validate_key(key)
        if (
            not isinstance(source_id, str)
            or not source_id.strip()
            or len(source_id) > 512
            or any(ord(char) < 32 for char in source_id)
        ):
            raise ContractViolation("single-flight source_id must be a safe non-empty id")
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ContractViolation("single-flight timeout_seconds must be positive")
        waiter_id = uuid.uuid4().hex
        deadline = (
            None
            if timeout_seconds is None
            else self._monotonic() + float(timeout_seconds)
        )
        while True:
            action, state = self._claim_or_join(key, source_id, waiter_id)
            if action == "leader":
                delivery = self._run_as_leader(key, state, loader)
                if delivery is not None:
                    return delivery
                continue
            if action == "completed":
                result = state.get("result")
                if not isinstance(result, dict):
                    raise ContractViolation("single-flight result must be an object")
                return FlightDelivery(
                    payload=result,
                    source_id=str(state["source_id"]),
                    coalesced=True,
                )
            if action == "failed":
                raise RemoteFlightError(
                    str(state["error_type"]),
                    str(state["error_digest"]),
                    str(state["source_id"]),
                )
            remaining = None if deadline is None else deadline - self._monotonic()
            if remaining is not None and remaining <= 0:
                self._unregister(key, waiter_id)
                raise SingleFlightTimeoutError(
                    f"single-flight wait for {key} exceeded timeout"
                )
            time.sleep(
                self.poll_interval
                if remaining is None
                else min(self.poll_interval, remaining)
            )

    def status(self) -> Mapping[str, int]:
        counts = {
            "running": 0,
            "completed": 0,
            "failed": 0,
            "expired": 0,
            "invalid": 0,
        }
        now = self._clock()
        for path in sorted(self.states.glob("*.json")):
            try:
                state = self._read_state(path)
                status = str(state["status"])
                expiry = (
                    float(state["lease_expires_at"])
                    if status == "running"
                    else float(state["delivery_expires_at"])
                )
                counts["expired" if expiry <= now else status] += 1
            except (OSError, KeyError, TypeError, ValueError, ContractViolation):
                counts["invalid"] += 1
        return counts

    def _claim_or_join(
        self, key: str, source_id: str, waiter_id: str
    ) -> Tuple[str, Mapping[str, Any]]:
        with self._locked(key):
            path = self._state_path(key)
            state = self._read_state(path) if path.exists() else None
            now = self._clock()
            if state is None:
                state = self._running_state(key, source_id, now, ())
                self._write_state(path, state)
                return "leader", state
            status = state["status"]
            if status == "running":
                if float(state["lease_expires_at"]) <= now:
                    waiters = tuple(
                        str(item)
                        for item in state.get("waiters", ())
                        if item != waiter_id
                    )
                    state = self._running_state(key, source_id, now, waiters)
                    self._write_state(path, state)
                    return "leader", state
                waiters = list(state.get("waiters", ()))
                if waiter_id not in waiters:
                    waiters.append(waiter_id)
                    state = dict(state)
                    state["waiters"] = waiters
                    self._write_state(path, state)
                return "wait", state

            delivery_expired = float(state["delivery_expires_at"]) <= now
            waiters = list(state.get("waiters", ()))
            if waiter_id in waiters:
                waiters.remove(waiter_id)
                delivered = dict(state)
                if waiters:
                    state = dict(state)
                    state["waiters"] = waiters
                    self._write_state(path, state)
                else:
                    path.unlink(missing_ok=True)
                return status, delivered
            if waiters and not delivery_expired:
                return "wait", state
            path.unlink(missing_ok=True)
            state = self._running_state(key, source_id, now, ())
            self._write_state(path, state)
            return "leader", state

    def _run_as_leader(
        self,
        key: str,
        state: Mapping[str, Any],
        loader: Callable[[], Mapping[str, Any]],
    ) -> Optional[FlightDelivery]:
        token = str(state["token"])
        source_id = str(state["source_id"])
        stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(key, token, stop),
            daemon=True,
        )
        heartbeat.start()
        try:
            value = loader()
            if not isinstance(value, Mapping):
                raise ContractViolation("single-flight loader must return an object")
            payload, _ = _canonical(dict(value))
        except BaseException as error:
            stop.set()
            heartbeat.join(timeout=max(1.0, self.heartbeat_interval * 2.0))
            committed = self._finish_failure(key, token, error)
            if committed:
                raise
            return None
        stop.set()
        heartbeat.join(timeout=max(1.0, self.heartbeat_interval * 2.0))
        committed = self._finish_success(key, token, payload)
        if not committed:
            return None
        return FlightDelivery(payload=payload, source_id=source_id, coalesced=False)

    def _heartbeat_loop(
        self,
        key: str,
        token: str,
        stop: threading.Event,
    ) -> None:
        while not stop.wait(self.heartbeat_interval):
            try:
                if not self._renew(key, token):
                    return
            except Exception:
                return

    def _renew(self, key: str, token: str) -> bool:
        with self._locked(key):
            path = self._state_path(key)
            if not path.exists():
                return False
            state = self._read_state(path)
            if state["status"] != "running" or state["token"] != token:
                return False
            now = self._clock()
            state = dict(state)
            state["heartbeat_at"] = now
            state["lease_expires_at"] = now + self.lease_seconds
            self._write_state(path, state)
            return True

    def _finish_success(
        self, key: str, token: str, payload: Mapping[str, Any]
    ) -> bool:
        return self._finish(key, token, result=payload)

    def _finish_failure(self, key: str, token: str, error: BaseException) -> bool:
        error_type = type(error).__name__
        _, digest = _canonical({"error_type": error_type, "message": str(error)})
        return self._finish(
            key,
            token,
            error_type=error_type,
            error_digest=digest,
        )

    def _finish(
        self,
        key: str,
        token: str,
        result: Optional[Mapping[str, Any]] = None,
        error_type: Optional[str] = None,
        error_digest: Optional[str] = None,
    ) -> bool:
        with self._locked(key):
            path = self._state_path(key)
            if not path.exists():
                return False
            state = self._read_state(path)
            if state["status"] != "running" or state["token"] != token:
                return False
            now = self._clock()
            finished = dict(state)
            finished["status"] = "completed" if result is not None else "failed"
            finished["heartbeat_at"] = now
            finished["lease_expires_at"] = now
            finished["delivery_expires_at"] = now + self.delivery_ttl_seconds
            if result is not None:
                finished["result"] = dict(result)
            else:
                finished["error_type"] = error_type
                finished["error_digest"] = error_digest
            if finished.get("waiters"):
                self._write_state(path, finished)
            else:
                path.unlink(missing_ok=True)
            return True

    def _unregister(self, key: str, waiter_id: str) -> None:
        with self._locked(key):
            path = self._state_path(key)
            if not path.exists():
                return
            state = self._read_state(path)
            waiters = list(state.get("waiters", ()))
            if waiter_id not in waiters:
                return
            waiters.remove(waiter_id)
            if state["status"] in ("completed", "failed") and not waiters:
                path.unlink(missing_ok=True)
                return
            state = dict(state)
            state["waiters"] = waiters
            self._write_state(path, state)

    def _running_state(
        self,
        key: str,
        source_id: str,
        now: float,
        waiters: Tuple[str, ...],
    ) -> Mapping[str, Any]:
        return {
            "schema_version": SINGLE_FLIGHT_SCHEMA_VERSION,
            "key": key,
            "token": uuid.uuid4().hex,
            "source_id": source_id,
            "status": "running",
            "started_at": now,
            "heartbeat_at": now,
            "lease_expires_at": now + self.lease_seconds,
            "waiters": list(waiters),
        }

    @contextmanager
    def _locked(self, key: str) -> Iterator[None]:
        path = self.locks / f"{key}.lock"
        descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _state_path(self, key: str) -> Path:
        return self.states / f"{key}.json"

    @staticmethod
    def _validate_key(key: str) -> None:
        if (
            not isinstance(key, str)
            or len(key) != 64
            or any(char not in "0123456789abcdef" for char in key)
        ):
            raise ContractViolation("single-flight key must be a sha256 digest")

    def _read_state(self, path: Path) -> Dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(f"invalid single-flight state {path.name}: {error}") from error
        if not isinstance(value, dict):
            raise ContractViolation("single-flight state must be an object")
        checksum = value.get("checksum")
        unsigned = dict(value)
        unsigned.pop("checksum", None)
        _, expected_checksum = _canonical(unsigned)
        if checksum != expected_checksum:
            raise ContractViolation("single-flight state checksum mismatch")
        if value.get("schema_version") != SINGLE_FLIGHT_SCHEMA_VERSION:
            raise ContractViolation("unsupported single-flight state schema")
        if value.get("status") not in _VALID_STATES:
            raise ContractViolation("invalid single-flight state status")
        if value.get("key") != path.stem:
            raise ContractViolation("single-flight state key mismatch")
        waiters = value.get("waiters")
        if not isinstance(waiters, list) or any(
            not isinstance(item, str) for item in waiters
        ):
            raise ContractViolation("single-flight waiters must be an array of ids")
        source_id = value.get("source_id")
        if (
            not isinstance(source_id, str)
            or not source_id
            or len(source_id) > 512
            or any(ord(char) < 32 for char in source_id)
        ):
            raise ContractViolation("single-flight source_id must be a safe non-empty id")
        if not isinstance(value.get("token"), str) or not value["token"]:
            raise ContractViolation("single-flight lease token cannot be empty")
        for name in ("started_at", "heartbeat_at", "lease_expires_at"):
            number = value.get(name)
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(number)
            ):
                raise ContractViolation(f"single-flight {name} must be finite")
        if value["status"] in ("completed", "failed"):
            expiry = value.get("delivery_expires_at")
            if (
                isinstance(expiry, bool)
                or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry)
            ):
                raise ContractViolation(
                    "single-flight delivery_expires_at must be finite"
                )
        if value["status"] == "completed" and not isinstance(
            value.get("result"), dict
        ):
            raise ContractViolation("single-flight completed result must be an object")
        if value["status"] == "failed":
            error_type = value.get("error_type")
            error_digest = value.get("error_digest")
            if not isinstance(error_type, str) or not error_type:
                raise ContractViolation("single-flight error_type cannot be empty")
            if (
                not isinstance(error_digest, str)
                or len(error_digest) != 64
                or any(char not in "0123456789abcdef" for char in error_digest)
            ):
                raise ContractViolation("single-flight error_digest is invalid")
        return value

    @staticmethod
    def _write_state(path: Path, state: Mapping[str, Any]) -> None:
        value = dict(state)
        value.pop("checksum", None)
        _, checksum = _canonical(value)
        value["checksum"] = checksum
        _atomic_json_write(path, value)
