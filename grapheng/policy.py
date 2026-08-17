from enum import Enum
from typing import Mapping, Protocol, Set

from .model import NodeSpec


class GateDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class GatePolicy(Protocol):
    def decide(self, node: NodeSpec, artifacts: Mapping[str, object]) -> GateDecision:
        ...


class DenyNamedGatesPolicy:
    def decide(self, node: NodeSpec, artifacts: Mapping[str, object]) -> GateDecision:
        return GateDecision.ALLOW if node.gate is None else GateDecision.DENY


class AllowListGatePolicy:
    def __init__(self, allowed: Set[str]):
        self._allowed = frozenset(allowed)

    def decide(self, node: NodeSpec, artifacts: Mapping[str, object]) -> GateDecision:
        if node.gate is None or node.gate in self._allowed:
            return GateDecision.ALLOW
        return GateDecision.DENY
