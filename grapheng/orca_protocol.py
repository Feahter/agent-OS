"""Decoders for the Orca wire protocol.

Orca reports deliveries, messages and lifecycle identifiers under several
historical field spellings (``id`` / ``messageId`` / ``message_id``, nested
``delivery`` envelopes, ids that live on the message or on its payload).
Normalizing that variance is a self-contained concern: it depends only on the
message shape, never on coordinator state.

Keeping these decoders separate from :mod:`grapheng.coordinator` means the
protocol can be tested directly, and the coordinator is left to express
scheduling and recovery rather than field archaeology.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping, Optional, Tuple

from ._store import encode_json, sha256_hex
from .errors import ContractViolation
from .model import GraphSpec

#: Message types that may arrive as a bare message rather than inside a
#: delivery envelope.
ACTIONABLE_MESSAGE_TYPES = ("worker_done", "question", "escalation")


def digest(value: Any) -> str:
    """Return the canonical digest of an Orca message payload."""

    return sha256_hex(
        encode_json(value, label="Orca message payload").encode("utf-8")
    )


def legacy_graph_fingerprint(graph: GraphSpec) -> str:
    """Fingerprint a graph as it was computed before controlled merges existed.

    Kept so a run started by an older Agent OS can still be recognized after an
    upgrade instead of being rejected as a different graph.
    """

    value = asdict(graph)
    for node in value["nodes"]:
        node.pop("controlled_merge", None)
    return sha256_hex(
        encode_json(value, label="Orca graph").encode("utf-8")
    )


def normalize_delivery(
    value: Mapping[str, Any],
) -> Optional[Tuple[str, Tuple[Mapping[str, Any], ...]]]:
    """Return ``(delivery_id, messages)``, or ``None`` for an empty delivery."""

    if not isinstance(value, dict):
        raise ContractViolation("Orca delivery must be an object")
    if value.get("count") == 0:
        return None
    nested = value.get("delivery")
    delivery = nested if isinstance(nested, dict) else value
    delivery_id = (
        delivery.get("id")
        or delivery.get("deliveryId")
        or delivery.get("delivery_id")
        or value.get("deliveryId")
        or value.get("delivery_id")
    )
    messages = delivery.get("messages") or value.get("messages")
    if messages is None and value.get("type") in ACTIONABLE_MESSAGE_TYPES:
        messages = [value]
    if not isinstance(delivery_id, str) or not delivery_id:
        raise ContractViolation("Orca actionable delivery has no id")
    if not isinstance(messages, list) or not all(
        isinstance(item, dict) for item in messages
    ):
        raise ContractViolation("Orca delivery messages must be an array")
    return delivery_id, tuple(messages)


def message_id(message: Mapping[str, Any], require_real: bool = False) -> str:
    """Return a stable id for one message.

    With ``require_real`` the message must carry a replyable id; otherwise a
    content digest is derived so the message can still be deduplicated.
    """

    identifier = (
        message.get("id") or message.get("messageId") or message.get("message_id")
    )
    if isinstance(identifier, str) and identifier:
        return identifier
    if require_real:
        raise ContractViolation(
            f"Orca {message.get('type')} message has no replyable id"
        )
    return f"digest-{digest(message)}"


def _identifier(
    message: Mapping[str, Any], names: Tuple[str, ...]
) -> Optional[str]:
    payload = message.get("payload")
    for name in names:
        candidate = message.get(name)
        if isinstance(candidate, str) and candidate:
            return candidate
    if isinstance(payload, dict):
        for name in names:
            candidate = payload.get(name)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def dispatch_id(message: Mapping[str, Any]) -> str:
    """Return the dispatch id carried by a lifecycle message."""

    identifier = _identifier(message, ("dispatchId", "dispatch_id"))
    if identifier is None:
        raise ContractViolation("Orca lifecycle message has no dispatch id")
    return identifier


def task_id(message: Mapping[str, Any]) -> str:
    """Return the task id carried by a message."""

    identifier = _identifier(message, ("taskId", "task_id"))
    if identifier is None:
        raise ContractViolation(
            f"Orca {message.get('type')} message has no task id"
        )
    return identifier


def latest_dispatch(state: Mapping[str, Any], node_id: str) -> Optional[str]:
    """Return the newest dispatch id recorded for ``node_id``."""

    candidates = [
        (int(item["attempt"]), identifier)
        for identifier, item in state["dispatches"].items()
        if item.get("node_id") == node_id
    ]
    if not candidates:
        return None
    return max(candidates)[1]
