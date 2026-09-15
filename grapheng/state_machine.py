"""Pure state-transition rules shared by durable run projections.

Keep persistence, locking and side effects in their owning modules.  This
module only answers whether a transition is legal and which phase wins when a
late control projection races with an observed execution result.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

TERMINAL_PHASES = frozenset({"succeeded", "failed", "cancelled"})


@dataclass(frozen=True)
class ResidentTransition:
    state: str
    requested_action: Optional[str]


_ANY_KIND = "*"
_INTERRUPTED_OWNER = "interrupted-owner"
_RESIDENT_CONTROL_TRANSITIONS: Dict[
    Tuple[str, str, str], ResidentTransition
] = {
    ("pause", "queued", _ANY_KIND): ResidentTransition("paused", "pause"),
    ("pause", "waiting", _ANY_KIND): ResidentTransition("paused", "pause"),
    ("pause", "running", _ANY_KIND): ResidentTransition(
        "pause_requested", "pause"
    ),
    ("resume", "queued", _ANY_KIND): ResidentTransition("queued", None),
    ("resume", "paused", _ANY_KIND): ResidentTransition("queued", None),
    ("resume", "pause_requested", _ANY_KIND): ResidentTransition("queued", None),
    ("resume", "running", _INTERRUPTED_OWNER): ResidentTransition("queued", None),
    ("cancel", "queued", _ANY_KIND): ResidentTransition("cancelled", "cancel"),
    ("cancel", "waiting", _ANY_KIND): ResidentTransition("cancelled", "cancel"),
    ("cancel", "paused", _ANY_KIND): ResidentTransition("cancelled", "cancel"),
    ("cancel", "waiting", "orca"): ResidentTransition(
        "cancel_requested", "cancel"
    ),
    ("cancel", "paused", "orca"): ResidentTransition(
        "cancel_requested", "cancel"
    ),
    ("cancel", "running", _ANY_KIND): ResidentTransition(
        "cancel_requested", "cancel"
    ),
    ("cancel", "pause_requested", _ANY_KIND): ResidentTransition(
        "cancel_requested", "cancel"
    ),
}

_RESIDENT_SETTLEMENT_PRIORITY = {
    "waiting": 10,
    "paused": 20,
    "succeeded": 100,
    "failed": 100,
    "cancelled": 100,
}


def resident_control_transition(
    state: str,
    action: str,
    *,
    kind: str,
    owner_alive: bool,
) -> Optional[ResidentTransition]:
    """Return the legal control transition, leaving error wording to callers."""

    if state in TERMINAL_PHASES:
        return None
    if action == "resume" and state == "running" and not owner_alive:
        scope = _INTERRUPTED_OWNER
    else:
        scope = kind
    return _RESIDENT_CONTROL_TRANSITIONS.get(
        (action, state, scope),
        _RESIDENT_CONTROL_TRANSITIONS.get((action, state, _ANY_KIND)),
    )


def settle_resident_phase(current: str, observed: str) -> str:
    """Keep an accepted terminal result ahead of any late nonterminal view."""

    current_priority = _RESIDENT_SETTLEMENT_PRIORITY.get(current, 0)
    observed_priority = _RESIDENT_SETTLEMENT_PRIORITY.get(observed, 0)
    if current in TERMINAL_PHASES and current_priority >= observed_priority:
        return current
    return observed


def project_execution_phase(execution_phase: str, *, pause_requested: bool) -> str:
    """Project control intent without replacing a real execution terminal."""

    if execution_phase in TERMINAL_PHASES:
        return execution_phase
    if pause_requested and execution_phase == "paused":
        return "paused"
    return execution_phase


__all__ = [
    "TERMINAL_PHASES",
    "ResidentTransition",
    "project_execution_phase",
    "resident_control_transition",
    "settle_resident_phase",
]
