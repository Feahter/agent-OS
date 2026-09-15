"""Durable prepare/execute/reconcile protocol for ordinary Graph node effects."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple

from ._store import atomic_json_write, file_lock, json_digest, read_json_object
from .errors import ContractViolation, EffectIndeterminateError

EFFECT_SCHEMA_VERSION = 1
EFFECT_TYPES = ("read_only", "verified_idempotent", "reconcilable")


class NodeEffectJournal:
    """Own one durable receipt per stable ``run_id + node_id`` effect."""

    def __init__(
        self,
        root: Path,
        fault_injector: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._fault_injector = fault_injector

    @staticmethod
    def effect_id(run_id: str, node_id: str) -> str:
        digest = hashlib.sha256(f"{run_id}:{node_id}".encode()).hexdigest()[:32]
        return f"node-{digest}"

    def execute(
        self,
        *,
        effect_id: str,
        run_id: str,
        node_id: str,
        effect_type: str,
        inputs: Any,
        lease: Mapping[str, Any],
        workspace_identity: Mapping[str, Any],
        invoke: Callable[[], Tuple[Any, Mapping[str, Any]]],
        restore: Callable[[Mapping[str, Any]], Any],
        reconcile: Optional[
            Callable[[Mapping[str, Any]], Optional[Tuple[Any, Mapping[str, Any]]]]
        ] = None,
    ) -> Any:
        if effect_type not in EFFECT_TYPES or effect_type == "read_only":
            raise ContractViolation("node effect journal requires a write effect type")
        normalized_inputs, input_digest = json_digest(inputs, label="effect inputs")
        normalized_lease, _ = json_digest(dict(lease), label="effect lease")
        normalized_workspace, _ = json_digest(
            dict(workspace_identity), label="effect workspace identity"
        )
        path = self.root / f"{effect_id}.json"
        with file_lock(self.root / f"{effect_id}.lock"):
            if path.exists():
                record = dict(read_json_object(path, label="node effect receipt"))
                self._validate(
                    record,
                    effect_id=effect_id,
                    run_id=run_id,
                    node_id=node_id,
                    effect_type=effect_type,
                    inputs=normalized_inputs,
                    input_digest=input_digest,
                    workspace_identity=normalized_workspace,
                )
            else:
                record = {
                    "schema_version": EFFECT_SCHEMA_VERSION,
                    "effect_id": effect_id,
                    "run_id": run_id,
                    "node_id": node_id,
                    "effect_type": effect_type,
                    "state": "prepared",
                    "inputs": normalized_inputs,
                    "input_digest": input_digest,
                    "lease": normalized_lease,
                    "last_lease": normalized_lease,
                    "workspace_identity": normalized_workspace,
                    "outcome": None,
                    "outcome_digest": None,
                    "error": None,
                }
                self._commit(path, record, "prepared")

            state = record["state"]
            if state == "completed":
                return restore(record["outcome"])
            if state == "indeterminate":
                raise EffectIndeterminateError(
                    f"effect {effect_id} is indeterminate; reconcile it before retrying"
                )
            record["last_lease"] = normalized_lease
            if state == "executing" and effect_type == "reconcilable":
                return self._reconcile(path, record, reconcile)

            record.update({"state": "executing", "error": None})
            self._commit(path, record, "executing")
            try:
                value, persisted = invoke()
                normalized_outcome, outcome_digest = json_digest(
                    dict(persisted), label="effect outcome"
                )
            except Exception as error:
                record["error"] = f"{type(error).__name__}: {error}"
                self._commit(path, record, "execution_failed")
                raise
            record.update(
                {
                    "state": "completed",
                    "outcome": normalized_outcome,
                    "outcome_digest": outcome_digest,
                    "error": None,
                }
            )
            self._commit(path, record, "completed")
            return value

    def _reconcile(
        self,
        path: Path,
        record: dict,
        reconcile: Optional[
            Callable[[Mapping[str, Any]], Optional[Tuple[Any, Mapping[str, Any]]]]
        ],
    ) -> Any:
        if reconcile is None:
            return self._indeterminate(path, record, "effect has no reconcile implementation")
        try:
            recovered = reconcile(dict(record))
            if recovered is None:
                return self._indeterminate(
                    path, record, "effect reconcile could not prove an outcome"
                )
            value, persisted = recovered
            normalized_outcome, outcome_digest = json_digest(
                dict(persisted), label="effect outcome"
            )
        except EffectIndeterminateError:
            raise
        except Exception as error:
            return self._indeterminate(
                path,
                record,
                f"effect reconcile failed: {type(error).__name__}: {error}",
            )
        record.update(
            {
                "state": "completed",
                "outcome": normalized_outcome,
                "outcome_digest": outcome_digest,
                "error": None,
            }
        )
        self._commit(path, record, "completed")
        return value

    def _indeterminate(self, path: Path, record: dict, error: str) -> Any:
        record.update({"state": "indeterminate", "error": error})
        self._commit(path, record, "indeterminate")
        raise EffectIndeterminateError(error)

    def _commit(self, path: Path, record: Mapping[str, Any], stage: str) -> None:
        self._write(path, record)
        if self._fault_injector is not None:
            self._fault_injector(stage, dict(record))

    @staticmethod
    def _validate(
        record: Mapping[str, Any],
        *,
        effect_id: str,
        run_id: str,
        node_id: str,
        effect_type: str,
        inputs: Any,
        input_digest: str,
        workspace_identity: Mapping[str, Any],
    ) -> None:
        required = {
            "schema_version",
            "effect_id",
            "run_id",
            "node_id",
            "effect_type",
            "state",
            "inputs",
            "input_digest",
            "lease",
            "last_lease",
            "workspace_identity",
            "outcome",
            "outcome_digest",
            "error",
        }
        if set(record) != required:
            raise ContractViolation("node effect receipt has an invalid contract")
        if (
            record.get("schema_version") != EFFECT_SCHEMA_VERSION
            or record.get("effect_id") != effect_id
            or record.get("run_id") != run_id
            or record.get("node_id") != node_id
            or record.get("effect_type") != effect_type
            or record.get("state")
            not in ("prepared", "executing", "completed", "indeterminate")
            or record.get("inputs") != inputs
            or record.get("input_digest") != input_digest
            or record.get("workspace_identity") != workspace_identity
            or not isinstance(record.get("lease"), dict)
            or not isinstance(record.get("last_lease"), dict)
        ):
            raise ContractViolation("node effect receipt fields do not match execution")
        outcome = record.get("outcome")
        outcome_digest = record.get("outcome_digest")
        if record.get("state") == "completed":
            if not isinstance(outcome, dict) or not isinstance(outcome_digest, str):
                raise ContractViolation("completed node effect has no outcome")
            normalized, digest = json_digest(outcome, label="effect outcome")
            if normalized != outcome or digest != outcome_digest:
                raise ContractViolation("node effect outcome digest does not match")
        elif outcome is not None or outcome_digest is not None:
            raise ContractViolation("unfinished node effect cannot have an outcome")

    @staticmethod
    def _write(path: Path, record: Mapping[str, Any]) -> None:
        atomic_json_write(path, record, label="node effect receipt")


__all__ = ["EFFECT_SCHEMA_VERSION", "EFFECT_TYPES", "NodeEffectJournal"]
