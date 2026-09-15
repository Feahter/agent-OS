"""Fencing tokens and pure lease-state transitions for durable run ownership."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping

from .errors import ContractViolation


class LeaseLostError(ContractViolation):
    """Raised when a stale owner tries to act with an invalid fencing token."""


@dataclass(frozen=True)
class LeaseToken:
    """Opaque capability proving ownership of one run generation."""

    run_id: str
    owner_id: str
    generation: int
    _secret: str = field(repr=False, compare=False)

    @classmethod
    def issue(cls, run_id: str, owner_id: str, generation: int) -> LeaseToken:
        return cls(run_id, owner_id, generation, secrets.token_urlsafe(32))

    def digest(self) -> str:
        return hashlib.sha256(self._secret.encode("utf-8")).hexdigest()

    def persisted_identity(self) -> Mapping[str, Any]:
        """Return the non-forgeable lease identity safe to persist in an intent."""

        return {
            "run_id": self.run_id,
            "owner_id": self.owner_id,
            "generation": self.generation,
            "token_digest": self.digest(),
        }


def claim_lease(
    state: Dict[str, Any],
    *,
    run_id: str,
    owner_id: str,
    lease_seconds: float,
    now: float,
    takeover: bool,
) -> LeaseToken:
    """Claim or take over ``state`` while its caller holds the state-file lock."""

    phase = state["phase"]
    if takeover:
        if phase == "succeeded":
            raise ContractViolation(f"run {run_id} is already succeeded")
        lease_expires_at = state.get("lease_expires_at")
        if (
            phase in ("running", "cancelling")
            and isinstance(lease_expires_at, (int, float))
            and not isinstance(lease_expires_at, bool)
            and float(lease_expires_at) > now
        ):
            raise ContractViolation(f"run {run_id} still has an active lease")
        generation = int(state.get("generation", 1)) + 1
    else:
        if phase != "queued" or state.get("owner_id") is not None:
            raise ContractViolation(f"run {run_id} cannot start from phase {phase}")
        generation = int(state.get("generation", 1))

    token = LeaseToken.issue(run_id, owner_id, generation)
    state.update(
        {
            "phase": "running",
            "owner_id": owner_id,
            "heartbeat_at": now,
            "lease_expires_at": now + lease_seconds,
            "cancel_requested": False,
            "generation": generation,
            "lease_token_digest": token.digest(),
            "error": None,
        }
    )
    return token


def assert_lease(state: Dict[str, Any], token: LeaseToken, *, now: float) -> None:
    """Reject a token unless every persisted fencing field still matches."""

    persisted_digest = state.get("lease_token_digest")
    lease_expires_at = state.get("lease_expires_at")
    valid = (
        state.get("run_id") == token.run_id
        and state.get("owner_id") == token.owner_id
        and int(state.get("generation", 0)) == token.generation
        and isinstance(persisted_digest, str)
        and hmac.compare_digest(persisted_digest, token.digest())
        and isinstance(lease_expires_at, (int, float))
        and not isinstance(lease_expires_at, bool)
        and float(lease_expires_at) > now
        and state.get("phase") in ("running", "cancelling")
    )
    if not valid:
        raise LeaseLostError(
            f"run {token.run_id} lease token is stale for generation {token.generation}"
        )


def renew_lease(
    state: Dict[str, Any], token: LeaseToken, *, now: float, lease_seconds: float
) -> bool:
    """Renew a valid token and return whether cancellation is requested."""

    assert_lease(state, token, now=now)
    state["heartbeat_at"] = now
    state["lease_expires_at"] = now + lease_seconds
    return bool(state.get("cancel_requested"))
