import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .errors import GraphValidationError

IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise GraphValidationError((f"{field} must be an object",))
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise GraphValidationError((f"{field} must be a valid identifier",))
    return value


def _optional_identifier(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _identifier(value, field)


def _optional_string(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise GraphValidationError((f"{field} must be a non-empty string",))
    return value


def _required_string(value: Any, field: str) -> str:
    result = _optional_string(value, field)
    if result is None:
        raise GraphValidationError((f"{field} must be a non-empty string",))
    return result


def _string_tuple(value: Any, field: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise GraphValidationError((f"{field} must be an array",))
    result = tuple(_identifier(item, f"{field}[]") for item in value)
    if len(result) != len(set(result)):
        raise GraphValidationError((f"{field} contains duplicates",))
    return result


def _string_value_tuple(value: Any, field: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise GraphValidationError((f"{field} must be an array",))
    result = tuple(_required_string(item, f"{field}[]") for item in value)
    if len(result) != len(set(result)):
        raise GraphValidationError((f"{field} contains duplicates",))
    return result


def _non_empty_string_tuple(value: Any, field: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise GraphValidationError((f"{field} must be a non-empty array",))
    return tuple(_required_string(item, f"{field}[]") for item in value)


def _positive_int(value: Any, field: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GraphValidationError((f"{field} must be a positive integer",))
    return value


def _optional_positive_int(value: Any, field: str) -> Optional[int]:
    if value is None:
        return None
    return _positive_int(value, field, 1)


def _optional_positive_number(value: Any, field: str) -> Optional[float]:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise GraphValidationError((f"{field} must be a positive number",))
    return float(value)


def _non_negative_number(value: Any, field: str, default: float = 0.0) -> float:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise GraphValidationError((f"{field} must be a non-negative number",))
    return float(value)


def _unit_interval(value: Any, field: str, default: float) -> float:
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise GraphValidationError((f"{field} must be between zero and one",))
    return float(value)


def _choice(value: Any, field: str, choices: Tuple[str, ...], default: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str) or value not in choices:
        raise GraphValidationError(
            (f"{field} must be one of: {', '.join(choices)}",)
        )
    return value


@dataclass(frozen=True)
class RetrySpec:
    max_attempts: int = 1

    @classmethod
    def from_dict(cls, value: Any, field: str) -> "RetrySpec":
        if value is None:
            return cls()
        data = _mapping(value, field)
        return cls(max_attempts=_positive_int(data.get("max_attempts"), f"{field}.max_attempts", 1))


@dataclass(frozen=True)
class WorkspaceSpec:
    mode: str = "shared"
    lineage: str = "child"
    retain: str = "on_failure"

    @classmethod
    def from_dict(cls, value: Any, field: str) -> "WorkspaceSpec":
        if value is None:
            return cls()
        data = _mapping(value, field)
        return cls(
            mode=_choice(data.get("mode"), f"{field}.mode", ("shared", "isolated"), "shared"),
            lineage=_choice(
                data.get("lineage"),
                f"{field}.lineage",
                ("child", "top-level"),
                "child",
            ),
            retain=_choice(
                data.get("retain"),
                f"{field}.retain",
                ("always", "never", "on_failure"),
                "on_failure",
            ),
        )


@dataclass(frozen=True)
class AgentSpec:
    prompt: str
    executor: Optional[str] = None
    required_capabilities: Tuple[str, ...] = ()
    tools: Tuple[str, ...] = ()
    model: Optional[str] = None
    timeout_seconds: int = 300
    max_cost_usd: Optional[float] = None
    data_classification: str = "public"
    task_type: str = "general"
    model_family: str = "default"
    reuse_scope: str = "local"
    workspace: WorkspaceSpec = WorkspaceSpec()

    @classmethod
    def from_dict(cls, value: Any, field: str) -> Optional["AgentSpec"]:
        if value is None:
            return None
        data = _mapping(value, field)
        return cls(
            prompt=_required_string(data.get("prompt"), f"{field}.prompt"),
            executor=_optional_identifier(data.get("executor"), f"{field}.executor"),
            required_capabilities=_string_tuple(
                data.get("required_capabilities"), f"{field}.required_capabilities"
            ),
            tools=_string_value_tuple(data.get("tools"), f"{field}.tools"),
            model=_optional_string(data.get("model"), f"{field}.model"),
            timeout_seconds=_positive_int(
                data.get("timeout_seconds"), f"{field}.timeout_seconds", 300
            ),
            max_cost_usd=_optional_positive_number(
                data.get("max_cost_usd"), f"{field}.max_cost_usd"
            ),
            data_classification=_choice(
                data.get("data_classification"),
                f"{field}.data_classification",
                ("public", "internal", "confidential", "restricted"),
                "public",
            ),
            task_type=_identifier(data.get("task_type", "general"), f"{field}.task_type"),
            model_family=_identifier(
                data.get("model_family", "default"), f"{field}.model_family"
            ),
            reuse_scope=_identifier(
                data.get("reuse_scope", "local"), f"{field}.reuse_scope"
            ),
            workspace=WorkspaceSpec.from_dict(data.get("workspace"), f"{field}.workspace"),
        )


@dataclass(frozen=True)
class VerifiedReuseSpec:
    decision_artifact: str
    passed_path: Tuple[str, ...]
    quality_path: Tuple[str, ...]
    minimum_quality_score: float = 0.8

    @classmethod
    def from_dict(cls, value: Any, field: str) -> Optional["VerifiedReuseSpec"]:
        if value is None:
            return None
        data = _mapping(value, field)
        return cls(
            decision_artifact=_identifier(
                data.get("decision_artifact"), f"{field}.decision_artifact"
            ),
            passed_path=_non_empty_string_tuple(
                data.get("passed_path"), f"{field}.passed_path"
            ),
            quality_path=_non_empty_string_tuple(
                data.get("quality_path"), f"{field}.quality_path"
            ),
            minimum_quality_score=_unit_interval(
                data.get("minimum_quality_score"),
                f"{field}.minimum_quality_score",
                0.8,
            ),
        )


@dataclass(frozen=True)
class ControlledMergeSpec:
    verifier: str
    target_branch: str

    @classmethod
    def from_dict(cls, value: Any, field: str) -> Optional["ControlledMergeSpec"]:
        if value is None:
            return None
        data = _mapping(value, field)
        return cls(
            verifier=_identifier(data.get("verifier"), f"{field}.verifier"),
            target_branch=_required_string(
                data.get("target_branch"), f"{field}.target_branch"
            ),
        )


@dataclass(frozen=True)
class NodeSpec:
    id: str
    kind: str
    deps: Tuple[str, ...] = ()
    reads: Tuple[str, ...] = ()
    writes: Tuple[str, ...] = ()
    retry: RetrySpec = RetrySpec()
    estimated_tokens: int = 0
    max_tokens: Optional[int] = None
    estimated_cost_usd: float = 0.0
    gate: Optional[str] = None
    verifier_for: Optional[str] = None
    reality_anchor: bool = False
    agent: Optional[AgentSpec] = None
    verified_reuse: Optional[VerifiedReuseSpec] = None
    controlled_merge: Optional[ControlledMergeSpec] = None

    @classmethod
    def from_dict(cls, value: Any, index: int) -> "NodeSpec":
        field = f"nodes[{index}]"
        data = _mapping(value, field)
        estimated_tokens = data.get("estimated_tokens", 0)
        if isinstance(estimated_tokens, bool) or not isinstance(estimated_tokens, int) or estimated_tokens < 0:
            raise GraphValidationError((f"{field}.estimated_tokens must be a non-negative integer",))
        reality_anchor = data.get("reality_anchor", False)
        if not isinstance(reality_anchor, bool):
            raise GraphValidationError((f"{field}.reality_anchor must be a boolean",))
        return cls(
            id=_identifier(data.get("id"), f"{field}.id"),
            kind=_identifier(data.get("kind"), f"{field}.kind"),
            deps=_string_tuple(data.get("deps"), f"{field}.deps"),
            reads=_string_tuple(data.get("reads"), f"{field}.reads"),
            writes=_string_tuple(data.get("writes"), f"{field}.writes"),
            retry=RetrySpec.from_dict(data.get("retry"), f"{field}.retry"),
            estimated_tokens=estimated_tokens,
            max_tokens=_optional_positive_int(data.get("max_tokens"), f"{field}.max_tokens"),
            estimated_cost_usd=_non_negative_number(
                data.get("estimated_cost_usd"), f"{field}.estimated_cost_usd"
            ),
            gate=_optional_identifier(data.get("gate"), f"{field}.gate"),
            verifier_for=_optional_identifier(data.get("verifier_for"), f"{field}.verifier_for"),
            reality_anchor=reality_anchor,
            agent=AgentSpec.from_dict(data.get("agent"), f"{field}.agent"),
            verified_reuse=VerifiedReuseSpec.from_dict(
                data.get("verified_reuse"), f"{field}.verified_reuse"
            ),
            controlled_merge=ControlledMergeSpec.from_dict(
                data.get("controlled_merge"), f"{field}.controlled_merge"
            ),
        )


@dataclass(frozen=True)
class GraphSpec:
    id: str
    nodes: Tuple[NodeSpec, ...]
    max_concurrency: int = 1
    max_tokens: Optional[int] = None
    max_cost_usd: Optional[float] = None
    require_reality_anchor: bool = True

    @classmethod
    def from_dict(cls, value: Any) -> "GraphSpec":
        data = _mapping(value, "graph")
        raw_nodes = data.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise GraphValidationError(("nodes must be a non-empty array",))
        require_anchor = data.get("require_reality_anchor", True)
        if not isinstance(require_anchor, bool):
            raise GraphValidationError(("require_reality_anchor must be a boolean",))
        return cls(
            id=_identifier(data.get("id"), "id"),
            nodes=tuple(NodeSpec.from_dict(node, index) for index, node in enumerate(raw_nodes)),
            max_concurrency=_positive_int(data.get("max_concurrency"), "max_concurrency", 1),
            max_tokens=_optional_positive_int(data.get("max_tokens"), "max_tokens"),
            max_cost_usd=_optional_positive_number(data.get("max_cost_usd"), "max_cost_usd"),
            require_reality_anchor=require_anchor,
        )

    @classmethod
    def from_json(cls, path: Path) -> "GraphSpec":
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise GraphValidationError((f"cannot read GraphSpec: {error}",)) from error
        return cls.from_dict(value)

    def node_map(self) -> Dict[str, NodeSpec]:
        return {node.id: node for node in self.nodes}

    def node_ids(self) -> Iterable[str]:
        return (node.id for node in self.nodes)

    def fingerprint(self) -> str:
        encoded = json.dumps(
            asdict(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
