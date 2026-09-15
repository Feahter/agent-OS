"""Typed contracts for persisted and externally projected JSON documents.

Only stable storage and cross-module boundaries belong here. Runtime working
dictionaries intentionally remain private to their owning modules.
"""

from typing import Any, Dict, List, Mapping, Optional, TypedDict

CHECKPOINT_SCHEMA_VERSION = 1
CONTROL_RUN_SCHEMA_VERSION = 1
EFFECT_RECEIPT_SCHEMA_VERSION = 1


class CheckpointDocumentV1(TypedDict):
    schema_version: int
    graph_id: str
    graph_fingerprint: str
    run_id: str
    statuses: Dict[str, str]
    attempts: Dict[str, int]
    tokens_used: int
    cost_usd: float
    usage: Mapping[str, Any]
    artifacts: List[Mapping[str, Any]]


class ControlRunDocumentV1(TypedDict, total=False):
    schema_version: int
    run_id: str
    graph_id: str
    phase: str
    owner_id: Optional[str]
    lease_expires_at: Optional[float]
    heartbeat_at: Optional[float]
    cancel_requested: bool
    generation: int
    lease_token_digest: Optional[str]
    submitted_at: float
    error: Optional[str]
    result: Optional[Mapping[str, Any]]


class EffectReceiptDocumentV1(TypedDict, total=False):
    schema_version: int
    key: str
    payload_digest: str
    status: str
    result: Any
    error: Optional[str]
    recovery_context: Any
    recovery_history: List[Mapping[str, Any]]


class UserTaskMetadataDocumentV1(TypedDict):
    schema_version: int
    task_id: str
    kind: str
    workspace: str
    policy: str
    created_at: float


class ResidentQueueDocumentV4(TypedDict):
    schema_version: int
    next_sequence: int
    updated_at: float
    items: Dict[str, Dict[str, Any]]


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "CONTROL_RUN_SCHEMA_VERSION",
    "EFFECT_RECEIPT_SCHEMA_VERSION",
    "CheckpointDocumentV1",
    "ControlRunDocumentV1",
    "EffectReceiptDocumentV1",
    "ResidentQueueDocumentV4",
    "UserTaskMetadataDocumentV1",
]
