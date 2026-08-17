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


PROVIDER_GOVERNANCE_SCHEMA_VERSION = 1


def _canonical(value: Any) -> Tuple[Any, str]:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise ContractViolation(
            f"provider governance state must be JSON serializable: {error}"
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


def _finite_number(name: str, value: Any, minimum: float = 0.0) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
    ):
        raise ContractViolation(f"provider governance {name} is invalid")
    return float(value)


@dataclass(frozen=True)
class ProviderPolicy:
    requests_per_minute: Optional[int] = None
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0
    probe_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.requests_per_minute is not None and (
            isinstance(self.requests_per_minute, bool)
            or not isinstance(self.requests_per_minute, int)
            or self.requests_per_minute < 1
        ):
            raise ContractViolation("requests_per_minute must be a positive integer")
        if (
            isinstance(self.failure_threshold, bool)
            or not isinstance(self.failure_threshold, int)
            or self.failure_threshold < 1
        ):
            raise ContractViolation("failure_threshold must be a positive integer")
        for name, value in (
            ("cooldown_seconds", self.cooldown_seconds),
            ("probe_timeout_seconds", self.probe_timeout_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ContractViolation(f"{name} must be a finite positive number")


@dataclass(frozen=True)
class ProviderAdmission:
    allowed: bool
    reason: Optional[str]
    consecutive_failures: int
    reservation_token: Optional[str] = None
    probe_required: bool = False


class ProviderGovernanceStore:
    """Owns provider rate, circuit and half-open probe state behind one seam."""

    def __init__(
        self,
        root: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.root = root
        self._clock = clock
        self._thread_lock = threading.RLock()
        self._memory_state: Optional[Mapping[str, Any]] = None
        if root is not None:
            if root.is_symlink():
                raise ContractViolation("provider governance root cannot be a symlink")
            root.mkdir(parents=True, exist_ok=True)
            self.state_path = root / "state.json"
            self.lock_path = root / "state.lock"
        else:
            self.state_path = None
            self.lock_path = None

    def admit(
        self,
        provider: str,
        policy: ProviderPolicy,
        reserve: bool = False,
    ) -> ProviderAdmission:
        self._validate_provider(provider)
        with self._locked():
            state = self._read_state()
            now = self._effective_now(state)
            providers = dict(state["providers"])
            current = self._provider_state(providers.get(provider))
            requests = tuple(
                item for item in current["request_times"] if now - item < 60.0
            )
            opened_at = current["opened_at"]
            threshold_opened = False
            if (
                opened_at is None
                and current["consecutive_failures"] >= policy.failure_threshold
            ):
                opened_at = state["updated_at"]
                threshold_opened = True
            probe = current["probe"]
            probe_active = probe is not None and probe["expires_at"] > now
            needs_probe = False
            reason = None
            if opened_at is not None:
                if now - opened_at < policy.cooldown_seconds:
                    reason = "circuit_open"
                elif probe_active:
                    reason = "circuit_probe_in_progress"
                else:
                    needs_probe = True
            if (
                reason is None
                and policy.requests_per_minute is not None
                and len(requests) >= policy.requests_per_minute
            ):
                reason = "rate_limited"
            if reason is not None:
                if threshold_opened:
                    providers[provider] = {**current, "opened_at": opened_at}
                    self._write_state(state, providers, now)
                return ProviderAdmission(
                    False, reason, current["consecutive_failures"]
                )
            if not reserve:
                if threshold_opened:
                    providers[provider] = {**current, "opened_at": opened_at}
                    self._write_state(state, providers, now)
                return ProviderAdmission(
                    True,
                    None,
                    current["consecutive_failures"],
                    probe_required=needs_probe,
                )

            token = uuid.uuid4().hex
            providers[provider] = {
                "request_times": [*requests, now],
                "consecutive_failures": current["consecutive_failures"],
                "opened_at": opened_at,
                "probe": (
                    {
                        "token": token,
                        "expires_at": now + policy.probe_timeout_seconds,
                    }
                    if needs_probe
                    else (probe if probe_active else None)
                ),
            }
            self._write_state(state, providers, now)
            return ProviderAdmission(
                True,
                None,
                current["consecutive_failures"],
                token,
                needs_probe,
            )

    def record_success(
        self,
        provider: str,
        reservation_token: Optional[str],
    ) -> None:
        self._validate_provider(provider)
        with self._locked():
            state = self._read_state()
            now = self._effective_now(state)
            providers = dict(state["providers"])
            current = self._provider_state(providers.get(provider))
            probe = current["probe"]
            if probe is not None and probe["expires_at"] > now:
                if reservation_token != probe["token"]:
                    return
            providers[provider] = {
                **current,
                "consecutive_failures": 0,
                "opened_at": None,
                "probe": None,
            }
            self._write_state(state, providers, now)

    def record_failure(
        self,
        provider: str,
        policy: ProviderPolicy,
        reservation_token: Optional[str],
    ) -> None:
        self._validate_provider(provider)
        with self._locked():
            state = self._read_state()
            now = self._effective_now(state)
            providers = dict(state["providers"])
            current = self._provider_state(providers.get(provider))
            failures = current["consecutive_failures"] + 1
            opened_at = (
                now if failures >= policy.failure_threshold else current["opened_at"]
            )
            providers[provider] = {
                **current,
                "consecutive_failures": failures,
                "opened_at": opened_at,
                "probe": None if opened_at is not None else current["probe"],
            }
            self._write_state(state, providers, now)

    def status(self) -> Mapping[str, Any]:
        with self._locked():
            state = self._read_state()
            now = self._effective_now(state)
            providers = {}
            for provider, raw in sorted(state["providers"].items()):
                current = self._provider_state(raw)
                requests = sum(
                    1 for item in current["request_times"] if now - item < 60.0
                )
                probe = current["probe"]
                probe_active = probe is not None and probe["expires_at"] > now
                providers[provider] = {
                    "requests_last_minute": requests,
                    "consecutive_failures": current["consecutive_failures"],
                    "circuit": (
                        "half_open"
                        if probe_active
                        else "open" if current["opened_at"] is not None else "closed"
                    ),
                    "opened_at": current["opened_at"],
                    "probe_expires_at": (
                        probe["expires_at"] if probe_active else None
                    ),
                }
            return {
                "schema_version": PROVIDER_GOVERNANCE_SCHEMA_VERSION,
                "generation": state["generation"],
                "updated_at": state["updated_at"],
                "providers": providers,
            }

    def has_runtime_state(self) -> bool:
        with self._locked():
            state = self._read_state()
            return bool(state["generation"] or state["providers"])

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            if self.lock_path is None:
                yield
                return
            with self.lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_state(self) -> Mapping[str, Any]:
        if self.state_path is None:
            return self._memory_state or self._empty_state()
        if not self.state_path.exists():
            return self._empty_state()
        if self.state_path.is_symlink():
            raise ContractViolation("provider governance state cannot be a symlink")
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(
                f"invalid provider governance state: {error}"
            ) from error
        return self._validate_state(value)

    def _write_state(
        self,
        previous: Mapping[str, Any],
        providers: Mapping[str, Any],
        now: float,
    ) -> None:
        payload = {
            "schema_version": PROVIDER_GOVERNANCE_SCHEMA_VERSION,
            "generation": previous["generation"] + 1,
            "updated_at": max(previous["updated_at"], now),
            "providers": providers,
        }
        canonical, checksum = _canonical(payload)
        value = {**canonical, "checksum": checksum}
        if self.state_path is None:
            self._memory_state = value
        else:
            _atomic_json_write(self.state_path, value)

    def _effective_now(self, state: Mapping[str, Any]) -> float:
        now = _finite_number("clock", self._clock())
        return max(now, state["updated_at"])

    @staticmethod
    def _empty_state() -> Mapping[str, Any]:
        return {
            "schema_version": PROVIDER_GOVERNANCE_SCHEMA_VERSION,
            "generation": 0,
            "updated_at": 0.0,
            "providers": {},
            "checksum": None,
        }

    @classmethod
    def _validate_state(cls, value: Any) -> Mapping[str, Any]:
        if not isinstance(value, dict):
            raise ContractViolation("provider governance state must be an object")
        if set(value) != {
            "schema_version",
            "generation",
            "updated_at",
            "providers",
            "checksum",
        }:
            raise ContractViolation("provider governance state fields are invalid")
        if value.get("schema_version") != PROVIDER_GOVERNANCE_SCHEMA_VERSION:
            raise ContractViolation("unsupported provider governance state schema")
        checksum = value.get("checksum")
        payload = {key: item for key, item in value.items() if key != "checksum"}
        canonical, expected = _canonical(payload)
        if not isinstance(checksum, str) or checksum != expected:
            raise ContractViolation("provider governance state checksum mismatch")
        generation = canonical["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ContractViolation("provider governance generation is invalid")
        updated_at = _finite_number("updated_at", canonical["updated_at"])
        raw_providers = canonical["providers"]
        if not isinstance(raw_providers, dict):
            raise ContractViolation("provider governance providers must be an object")
        providers: Dict[str, Any] = {}
        for provider, raw in raw_providers.items():
            cls._validate_provider(provider)
            providers[provider] = cls._provider_state(raw)
        return {
            **canonical,
            "updated_at": updated_at,
            "providers": providers,
            "checksum": checksum,
        }

    @staticmethod
    def _provider_state(raw: Any) -> Mapping[str, Any]:
        if raw is None:
            return {
                "request_times": [],
                "consecutive_failures": 0,
                "opened_at": None,
                "probe": None,
            }
        if not isinstance(raw, dict) or set(raw) != {
            "request_times",
            "consecutive_failures",
            "opened_at",
            "probe",
        }:
            raise ContractViolation("provider governance provider state is invalid")
        request_times = raw["request_times"]
        if not isinstance(request_times, list):
            raise ContractViolation("provider governance request_times must be an array")
        requests = [_finite_number("request timestamp", item) for item in request_times]
        if requests != sorted(requests):
            raise ContractViolation("provider governance request_times must be ordered")
        failures = raw["consecutive_failures"]
        if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
            raise ContractViolation("provider governance failure count is invalid")
        opened_at = raw["opened_at"]
        if opened_at is not None:
            opened_at = _finite_number("opened_at", opened_at)
        probe = raw["probe"]
        if probe is not None:
            if not isinstance(probe, dict) or set(probe) != {"token", "expires_at"}:
                raise ContractViolation("provider governance probe is invalid")
            token = probe["token"]
            if (
                not isinstance(token, str)
                or len(token) != 32
                or any(char not in "0123456789abcdef" for char in token)
            ):
                raise ContractViolation("provider governance probe token is invalid")
            probe = {
                "token": token,
                "expires_at": _finite_number("probe expiry", probe["expires_at"]),
            }
        return {
            "request_times": requests,
            "consecutive_failures": failures,
            "opened_at": opened_at,
            "probe": probe,
        }

    @staticmethod
    def _validate_provider(provider: Any) -> None:
        if (
            not isinstance(provider, str)
            or not provider.strip()
            or len(provider) > 256
            or any(ord(char) < 32 for char in provider)
        ):
            raise ContractViolation("provider governance provider is invalid")
