"""Durable state document for one Orca-coordinated run.

The coordinator's crash-safety guarantee rests entirely on this document: it
records which nodes ran, which dispatches are active, which merges are still
awaiting verification, and which effects already happened. Reading it back has
to reject anything that cannot be trusted - a foreign graph, an unsupported
schema, a merge candidate that no longer matches the GraphSpec - because a
silently accepted mismatch would let a resumed run act on stale decisions.

That validation logic is substantial and independent of scheduling, so it lives
here. :class:`OrcaRunStore` is the only place that reads, migrates, validates
and writes the document; the coordinator holds an instance and never touches
the file directly.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Mapping

from ._store import atomic_json_write, read_json_object
from .errors import ContractViolation
from .merge import MergeCandidate, MergeReceipt
from .model import GraphSpec
from .orca import OrcaMaterializedRun
from .orca_protocol import legacy_graph_fingerprint

#: Schema version of the coordinator state document.
ORCA_COORDINATOR_SCHEMA_VERSION = 2

_MERGE_STATUSES = ("awaiting_verification", "merged", "rejected")
_GATE_RESOLUTIONS = ("approved", "denied")


class OrcaRunStore:
    """Reads, validates and writes one coordinator state document."""

    def __init__(
        self,
        path: Path,
        graph: GraphSpec,
        workspace: Path,
        clock: Callable[[], float] = time.time,
    ):
        self.path = Path(path)
        self.graph = graph
        self.workspace = workspace
        self._clock = clock

    def exists(self) -> bool:
        return self.path.exists()

    def create(self) -> Dict[str, Any]:
        """Return a fresh, persisted state document for a new run."""

        state: Dict[str, Any] = {
            "schema_version": ORCA_COORDINATOR_SCHEMA_VERSION,
            "run_id": str(uuid.uuid4()),
            "graph_id": self.graph.id,
            "graph_fingerprint": self.graph.fingerprint(),
            "phase": "queued",
            "created_at": self._clock(),
            "materialized": None,
            "statuses": {node.id: "pending" for node in self.graph.nodes},
            "attempts": {node.id: 0 for node in self.graph.nodes},
            "active_dispatches": {},
            "dispatches": {},
            "retry_of": {},
            "reserved_tokens": {},
            "tokens_used": 0,
            "cost_usd": 0.0,
            "artifacts": [],
            "messages": {},
            "deliveries": {},
            "gate_resolutions": {},
            "merge_candidates": {},
        }
        self.save(state)
        return state

    def save(self, state: Mapping[str, Any]) -> None:
        atomic_json_write(self.path, state, label="Orca coordinator state")

    def read(self) -> Dict[str, Any]:
        """Load the document, migrating and validating before returning it."""

        value = read_json_object(self.path, label="Orca coordinator state")
        state = dict(value)
        schema_version = state.get("schema_version")
        if schema_version == 1:
            self._migrate_v1(state)
        elif schema_version != ORCA_COORDINATOR_SCHEMA_VERSION:
            raise ContractViolation("unsupported Orca coordinator schema")
        self._assert_same_graph(state, migrated_from_v1=schema_version == 1)
        self.validate_merge_state(state)
        return state

    def _migrate_v1(self, state: Dict[str, Any]) -> None:
        if any(node.controlled_merge is not None for node in self.graph.nodes):
            raise ContractViolation(
                "legacy Orca coordinator state cannot resume controlled merges"
            )
        state["schema_version"] = ORCA_COORDINATOR_SCHEMA_VERSION
        state.setdefault("gate_resolutions", {})
        state.setdefault("merge_candidates", {})

    def _assert_same_graph(
        self, state: Dict[str, Any], migrated_from_v1: bool
    ) -> None:
        current = self.graph.fingerprint()
        stored = state.get("graph_fingerprint")
        if migrated_from_v1 and stored == legacy_graph_fingerprint(self.graph):
            # A v1 run fingerprinted the graph without controlled-merge fields.
            # Recognizing that value keeps an upgraded run resumable.
            state["graph_fingerprint"] = current
            stored = current
        if state.get("graph_id") != self.graph.id or stored != current:
            raise ContractViolation(
                "Orca coordinator state does not match GraphSpec fingerprint"
            )

    def validate_materialized(self, materialized: OrcaMaterializedRun) -> None:
        """Reject an Orca run whose tasks or gates differ from the GraphSpec."""

        expected = {node.id for node in self.graph.nodes}
        if set(materialized.task_ids) != expected:
            raise ContractViolation("materialized Orca tasks do not match GraphSpec")
        gated = {node.id for node in self.graph.nodes if node.gate is not None}
        if set(materialized.gate_ids) != gated:
            raise ContractViolation("materialized Orca gates do not match GraphSpec")

    def validate_merge_state(self, state: Mapping[str, Any]) -> None:
        """Reject gate resolutions or merge candidates that cannot be trusted."""

        gate_resolutions = state.get("gate_resolutions")
        candidates = state.get("merge_candidates")
        if not isinstance(gate_resolutions, dict) or not isinstance(candidates, dict):
            raise ContractViolation("invalid controlled merge coordinator state")
        gated = {node.id for node in self.graph.nodes if node.gate is not None}
        if any(
            node_id not in gated or resolution not in _GATE_RESOLUTIONS
            for node_id, resolution in gate_resolutions.items()
        ):
            raise ContractViolation("invalid controlled merge gate resolution state")
        merge_sources = {
            node.id: node.controlled_merge
            for node in self.graph.nodes
            if node.controlled_merge is not None
        }
        for candidate_id, item in candidates.items():
            self._validate_candidate(state, merge_sources, candidate_id, item)

    def _validate_candidate(
        self,
        state: Mapping[str, Any],
        merge_sources: Mapping[str, Any],
        candidate_id: str,
        item: Any,
    ) -> None:
        if not isinstance(item, dict) or not isinstance(item.get("candidate"), dict):
            raise ContractViolation("invalid controlled merge candidate state")
        candidate = MergeCandidate.from_dict(item["candidate"])
        if candidate.candidate_id != candidate_id:
            raise ContractViolation("controlled merge candidate key mismatch")
        spec = merge_sources.get(candidate.source_node_id)
        if spec is None or spec.verifier != candidate.verifier_node_id:
            raise ContractViolation(
                "controlled merge candidate does not match GraphSpec"
            )
        if (
            candidate.run_id != state.get("run_id")
            or Path(candidate.target_repository).resolve() != self.workspace
            or spec.target_branch != candidate.target_branch
        ):
            raise ContractViolation("controlled merge candidate runtime mismatch")
        status = item.get("status")
        if status not in _MERGE_STATUSES:
            raise ContractViolation("invalid controlled merge candidate status")
        receipt_value = item.get("receipt")
        if status == "awaiting_verification" and receipt_value is not None:
            raise ContractViolation("pending controlled merge already has a receipt")
        if status != "awaiting_verification" and receipt_value is None:
            raise ContractViolation("settled controlled merge has no receipt")
        if receipt_value is None:
            return
        receipt = MergeReceipt.from_dict(receipt_value)
        if receipt.candidate_id != candidate.candidate_id or receipt.status != status:
            raise ContractViolation("controlled merge receipt identity mismatch")
