"""Read-only attempt projections; local metadata never replaces execution facts."""

import math
import re
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, Union

from ._store import append_jsonl, encode_json, file_lock, json_digest, read_json_object
from .agents import AgentRequest, ExecutorRegistry, ModelUsage
from .economics import _non_negative_number, _timestamp_seconds
from .engineering import EngineeringPlan, _validated_check_receipt
from .errors import ContractViolation
from .events import JsonlEventSink
from .index import ProjectionIndex
from .learning import RSICandidate
from .optimization import OptimizationCandidate
from .publication import PublicationCandidate
from .routing import DATA_CLASSIFICATIONS

if TYPE_CHECKING:
    from .distribution import DiagnosticCheck

_KINDS = ("events", "publications", "engineering", "learning", "optimization")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_STATUSES = frozenset((
    "unknown", "running", "completed", "failed", "rejected", "cancelled",
    "pending", "ready", "planned", "paused", "succeeded", "passed",
    "timeout", "invalid", "harness_error", "interrupted", "proposed", "evaluated", "approved", "active",
))


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ContractViolation(f"invalid evidence {label}")
    return value


def _digest(value: Any) -> str:
    return json_digest(value, label="evidence")[1]


def _candidate_digest(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"(?:v2:)?[0-9a-f]{64}", value) is None:
        raise ContractViolation("invalid evidence candidate digest")
    return value


def _attempt(scope: str, run_id: str, node_id: str, attempt: int) -> str:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise ContractViolation("invalid evidence attempt number")
    _identifier(run_id, "run_id")
    _identifier(node_id, "node_id")
    return "attempt-" + _digest([scope, run_id, node_id, attempt])


@dataclass(frozen=True)
class EvidenceSource:
    scope: str
    kind: str
    path: Path
    task_type: str = "general"
    policy_digest: Optional[str] = None
    expires_at: Optional[float] = None
    data_classification: Optional[str] = None
    executor_versions: Tuple[Tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.scope, "scope")
        _identifier(self.task_type, "task_type")
        if self.kind not in _KINDS:
            raise ContractViolation("unsupported evidence source kind")
        if self.policy_digest is not None:
            _identifier(self.policy_digest, "policy_digest")
        if self.expires_at is not None and (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (int, float))
            or not math.isfinite(self.expires_at) or self.expires_at < 0
        ):
            raise ContractViolation("evidence expires_at must be finite and non-negative")
        if self.data_classification is not None and self.data_classification not in DATA_CLASSIFICATIONS:
            raise ContractViolation("invalid evidence data_classification")
        if not isinstance(self.executor_versions, tuple):
            raise ContractViolation("evidence executor_versions must be immutable pairs")
        versions = set()
        for item in self.executor_versions:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ContractViolation("invalid evidence executor version")
            executor_id, version = item
            _identifier(executor_id, "executor_id")
            if (executor_id in versions or not isinstance(version, str) or not version.strip()
                    or len(version) > 128):
                raise ContractViolation("invalid or duplicate evidence executor version")
            versions.add(executor_id)


class EvidenceProjection:
    """Inspect/search existing facts with bounded, scope-checked local relations.

    Source kind and eligibility metadata come from the host, not agent outputs.
    Project checks remain distinguishable from independent frozen evaluation.
    This interface cannot propose or activate an execution policy.
    """

    def __init__(
        self, sources: Sequence[EvidenceSource], *,
        control_path: Optional[Path] = None,
        index: Optional[ProjectionIndex] = None,
        access_log: Optional[Path] = None,
        executors: Optional[ExecutorRegistry] = None,
        certification: Optional[Callable[[], Sequence["DiagnosticCheck"]]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.sources = tuple(sources)
        self.control_path = control_path
        self.index = index
        self.access_log = access_log
        self.executors = executors
        self.certification = certification
        self._clock = clock

    @staticmethod
    def _base(source: EvidenceSource, run_id: str, node_id: str, attempt: int) -> Dict[str, Any]:
        return {
            "attempt_id": _attempt(source.scope, run_id, node_id, attempt),
            "scope": source.scope, "run_id": run_id, "node_id": node_id, "attempt": attempt,
            "task_type": source.task_type, "policy_digest": source.policy_digest,
            "expires_at": source.expires_at, "origin": "runtime",
            "data_classification": source.data_classification,
            "executor_versions": dict(source.executor_versions),
            "status": "unknown", "failure_category": None,
            "candidate_digest": None, "candidate_refs": [], "evaluation_refs": [],
            "parent_attempt_id": None, "guidance_version": None,
            "relation_origin": None,
            "verified": False, "independent": False, "available": True,
            "executor_id": None,
            "invalidated": False, "corrects_attempt_id": None,
            "account_usage": True,
        }

    def _project(self, source: EvidenceSource, document: Any) -> Tuple[Mapping[str, Any], ...]:
        records: Dict[str, Dict[str, Any]] = {}
        if source.kind == "events":
            statuses = {
                "node_started": "running", "node_completed": "completed",
                "node_failed": "failed", "node_retry": "failed",
                "node_blocked": "rejected", "node_cancelled": "cancelled",
                "node_interrupted": "interrupted",
            }
            for event in document:
                if event.get("event") not in statuses or not event.get("node_id"):
                    continue
                record = self._base(source, event["run_id"], event["node_id"], event.get("attempt") or 0)
                key = record["attempt_id"]
                record = records.get(key, record)
                record["status"] = statuses[event["event"]]
                payload = event.get("payload", {})
                if event["event"] == "node_started":
                    parent = _attempt(source.scope, event["run_id"], event["node_id"], record["attempt"] - 1)
                    if parent in records:
                        record["parent_attempt_id"] = parent
                        record["relation_origin"] = "runtime"
                reason = str(payload.get("reason", "")) + str(payload.get("error", ""))
                if record["status"] in ("failed", "rejected"):
                    lowered = reason.lower()
                    record["failure_category"] = next(
                        (category for category in ("timeout", "budget", "contract", "dependency", "gate")
                         if category in lowered), "unknown"
                    )
                if event["event"] == "node_completed" or (
                    event["event"] in ("node_failed", "node_retry") and payload.get("usage") is not None
                ):
                    # Metadata is agent-controlled. Only explicit runtime usage
                    # and artifact identities are projected; never copy metadata.
                    record["usage"] = ModelUsage.from_persisted(
                        payload.get("usage"), int(payload.get("tokens_used", 0)),
                        payload.get("cost_usd"), None,
                    ).to_dict()
                    if event["event"] == "node_completed":
                        record["candidate_digest"] = _candidate_digest(payload.get("candidate_digest"))
                        executor_id = payload.get("executor_id")
                        if executor_id is not None:
                            record["executor_id"] = _identifier(executor_id, "executor_id")
                elif event["event"] == "node_interrupted":
                    record["usage"] = ModelUsage.unknown().with_accounted_totals(
                        payload.get("reserved_tokens", 0), payload.get("reserved_cost_usd", 0.0),
                    ).to_dict()
                records[key] = record
        elif source.kind == "publications":
            if document.get("schema_version") != 1:
                raise ContractViolation("unsupported evidence publication schema")
            for value in document.get("publications", []):
                candidate = PublicationCandidate.from_dict(value)
                record = self._base(source, candidate.run_id, candidate.node_id, candidate.attempt)
                record.update(
                    status={"published": "completed", "skipped": "rejected"}.get(candidate.status, candidate.status),
                    candidate_digest=_digest([item.checksum for item in candidate.output_artifacts]),
                    candidate_refs=[candidate.publication_id],
                    evaluation_refs=([candidate.verification_id] if candidate.verification_id else []),
                    verified=candidate.status == "published" and candidate.verification_id is not None,
                    failure_category="verification" if candidate.status == "skipped" else None,
                    executor_id=_identifier(candidate.executor_id, "executor_id"),
                    # Legacy publication receipts do not preserve completeness.
                    usage=ModelUsage.from_persisted(None, candidate.tokens_used, candidate.cost_usd).to_dict(),
                )
                records[record["attempt_id"]] = record
        elif source.kind == "engineering":
            plan = EngineeringPlan.from_dict(document["plan"])
            receipts = document["receipts"]
            document = document["report"]
            if document.get("plan_digest") != plan.digest:
                raise ContractViolation("engineering evidence plan binding mismatch")
            run_id = "engineering-" + plan.digest
            record = self._base(source, run_id, "result", 1)
            record.update(
                status=document.get("phase", "unknown"),
                candidate_digest=_candidate_digest(document.get("candidate_digest") or plan.workspace_digest),
                candidate_refs=[plan.digest], policy_digest=plan.policy_digest,
                task_type=plan.intent.get("template", source.task_type),
                verified=document.get("success") is True and document.get("reality_anchor", {}).get("passed") is True,
                failure_category="unknown" if document.get("failure") else None,
                usage=ModelUsage.from_persisted(
                    document.get("usage"), int(document.get("tokens_used", 0)),
                    document.get("cost_usd"), document.get("cost_complete"),
                ).to_dict(),
            )
            records[record["attempt_id"]] = record
            for value in receipts:
                receipt = _validated_check_receipt(value)
                binding = receipt.get("binding", {})
                if binding.get("plan_digest") != plan.digest or binding.get("policy_digest") != plan.policy_digest:
                    raise ContractViolation("engineering check evidence binding mismatch")
                check = self._base(source, run_id, receipt["receipt_id"], 1)
                check.update(
                    status=receipt["status"], candidate_digest=_candidate_digest(binding.get("candidate_digest")),
                    candidate_refs=[plan.digest], evaluation_refs=[receipt["receipt_id"]],
                    policy_digest=plan.policy_digest, task_type=record["task_type"],
                    binding_digest=_digest(binding),
                    account_usage=False,
                    # This source adapter only recognizes project check receipts.
                    # An agent cannot claim independent=True in a local result.
                    independent=False, verified=receipt["status"] == "passed",
                    failure_category=(receipt["status"] if receipt["status"] in ("timeout", "invalid", "harness_error") else None),
                )
                records[check["attempt_id"]] = check
                record["evaluation_refs"].append(receipt["receipt_id"])
        else:
            candidate_data = dict(document)
            candidate_data.pop("origin", None)
            policy_candidate: Union[RSICandidate, OptimizationCandidate]
            candidate_value: Mapping[str, Any]
            if source.kind == "learning":
                candidate_data.pop("schema_version", None)
                policy_candidate = RSICandidate.from_dict(candidate_data)
                candidate_value = policy_candidate.policy.to_dict()
            else:
                candidate_data.pop("schema_version", None)
                policy_candidate = OptimizationCandidate.from_dict(candidate_data)
                candidate_value = policy_candidate.change
            record = self._base(source, source.kind + "-" + policy_candidate.candidate_id, "candidate", 1)
            record.update(
                origin="agent_claim", status=policy_candidate.status,
                candidate_digest=_digest(candidate_value), candidate_refs=[policy_candidate.candidate_id],
                claim_digest=_digest(document.get("rationale", "")),
                # Existing numeric evaluation APIs accept caller measurements.
                # A passed boolean is not independent runtime verification.
                evaluation_refs=([_digest(policy_candidate.evaluation)] if policy_candidate.evaluation else []),
            )
            records[record["attempt_id"]] = record
        for record in records.values():
            if record["status"] not in _STATUSES:
                record["status"] = "unknown"
            record["evaluation_refs"] = [
                _identifier(ref, "evaluation_ref") for ref in record["evaluation_refs"]
            ]
        return tuple(records.values())

    @staticmethod
    def _run_boundaries(source: EvidenceSource, document: Any) -> Dict[str, Dict[str, Any]]:
        runs: Dict[str, Dict[str, Any]] = {}

        def run(run_id: str) -> Dict[str, Any]:
            _identifier(run_id, "run_id")
            return runs.setdefault(run_id, {
                "started_at": None, "finished_at": None, "preparation_seconds": None, "basis": None,
                "last_activity_at": None, "unbounded_activity": None,
            })

        if source.kind == "events":
            for event in document:
                boundary = run(event["run_id"])
                boundary["basis"] = "run_start_to_execution_completion"
                if event.get("event", "").startswith("node_"):
                    # Resume can retry a previously terminal failure. New node
                    # activity requires a new completion before the span is known.
                    boundary["finished_at"] = None
                    if event.get("time") is None:
                        boundary["unbounded_activity"] = True
                    else:
                        activity = _timestamp_seconds(event["time"], "evidence node time")
                        previous_activity = boundary["last_activity_at"]
                        boundary["last_activity_at"] = (activity if previous_activity is None
                                                        else max(activity, previous_activity))
                if event.get("event") not in ("run_started", "run_resumed", "run_completed"):
                    continue
                timestamp = event.get("time")
                seconds = (_timestamp_seconds(timestamp, "evidence run time")
                           if timestamp is not None else None)
                if event["event"] == "run_started":
                    if (boundary["started_at"] is not None and seconds is not None
                            and boundary["started_at"] != seconds):
                        raise ContractViolation("conflicting evidence run start")
                    if seconds is not None:
                        boundary["started_at"] = seconds
                elif event["event"] == "run_completed" and seconds is not None:
                    activity = boundary["last_activity_at"]
                    if activity is not None and activity > seconds:
                        raise ContractViolation("evidence run completion precedes node activity")
                    # Replaying a terminal checkpoint without new node activity
                    # must not extend the completed execution span.
                    previous = boundary["finished_at"]
                    boundary["finished_at"] = seconds if previous is None else min(previous, seconds)
                    boundary["unbounded_activity"] = None
        elif source.kind == "engineering":
            plan = EngineeringPlan.from_dict(document["plan"])
            report = document["report"]
            boundary = run("engineering-" + plan.digest)
            boundary.update(
                started_at=_non_negative_number(document["plan"]["created_at"], "evidence plan created_at"),
                preparation_seconds=plan.preparation_usage["elapsed_seconds"],
                basis="preparation_plus_plan_to_finish",
            )
            if report.get("finished_at") is not None:
                finished = _non_negative_number(report["finished_at"], "evidence engineering finished_at")
                if report.get("phase") in ("succeeded", "failed", "cancelled"):
                    boundary["finished_at"] = finished
        elif source.kind == "publications":
            for item in document.get("publications", []):
                run(item["run_id"])
        else:
            run(source.kind + "-" + document["candidate_id"])
        return runs

    def snapshot(self, *, include_unavailable: bool = False) -> Tuple[Mapping[str, Any], ...]:
        return self._snapshot(include_unavailable=include_unavailable)[0]

    def _snapshot(self, *, include_unavailable: bool) -> Tuple[
        Tuple[Mapping[str, Any], ...], Mapping[Tuple[str, str], Mapping[str, Any]],
    ]:
        records: Dict[str, Dict[str, Any]] = {}
        runs: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for source in self.sources:
            if not source.path.exists():
                continue
            document: Any
            if source.kind == "events":
                document = tuple(JsonlEventSink(source.path).read())
            elif source.kind == "engineering":
                report = read_json_object(source.path, label="evidence source")
                receipt_paths = tuple(sorted((source.path.parent / "checks").glob("*.json")))
                document = {
                    "report": report,
                    "plan": read_json_object(source.path.parent / "plan.json", label="engineering evidence plan"),
                    "receipts": [read_json_object(path, label="engineering check evidence") for path in receipt_paths]
                    if receipt_paths else report.get("check_receipts", []),
                }
            else:
                document = read_json_object(source.path, label="evidence source")
            # Include content and trusted source configuration in the key: merely
            # preserving size/mtime must not reuse changed authority or content.
            reference = _digest([source.kind, source.task_type, source.policy_digest, source.expires_at,
                                 source.data_classification, source.executor_versions, document])
            projected = (self._project(source, document) if self.index is None else
                         self.index.resolve("evidence:" + source.scope, reference, [source.path],
                                            partial(self._project, source, document)))
            for run_id, boundary in self._run_boundaries(source, document).items():
                previous_boundary = runs.setdefault((source.scope, run_id), dict(boundary))
                for field, value in boundary.items():
                    previous_value = previous_boundary[field]
                    if field == "last_activity_at" and value is not None and previous_value is not None:
                        previous_boundary[field] = max(value, previous_value)
                        continue
                    if value is not None and previous_value is not None and value != previous_value:
                        raise ContractViolation("conflicting evidence run boundaries")
                    if value is not None:
                        previous_boundary[field] = value
            for item in projected:
                key = item["attempt_id"]
                previous = records.get(key)
                if previous is not None:
                    if any(previous[field] != item[field] for field in (
                        "task_type", "policy_digest", "expires_at", "data_classification", "executor_versions"
                    )):
                        raise ContractViolation("conflicting evidence source eligibility metadata")
                    if any(previous[field] is not None and item[field] is not None
                           and previous[field] != item[field]
                           for field in ("candidate_digest", "executor_id")):
                        raise ContractViolation("conflicting evidence candidate or executor binding")
                    # Event status and publication status have different meanings;
                    # attach verification without losing execution failures.
                    if source.kind == "publications":
                        item = {**previous, **{k: item[k] for k in (
                            "candidate_digest", "candidate_refs", "evaluation_refs", "verified", "executor_id"
                        )}}
                    elif previous["candidate_refs"] and source.kind == "events":
                        item = {**item, **{k: previous[k] for k in (
                            "candidate_digest", "candidate_refs", "evaluation_refs", "verified", "executor_id"
                        )}}
                records[key] = dict(item)
        for control in self._controls():
            record = records.get(control["attempt_id"])
            if record is None:
                if (control["action"] == "correct"
                        and control["metadata"].get("replacement_attempt_id") in records):
                    raise ContractViolation("evidence correction references missing original")
                continue
            if control["scope"] != record["scope"]:
                raise ContractViolation("evidence control scope mismatch")
            if control["action"] in ("revoke", "forget"):
                record["available"] = False
                record["invalidated"] = True
            elif control["action"] == "link":
                record.update(control["metadata"])
                record["relation_origin"] = "operator_metadata"
            elif control["action"] == "correct":
                record["correction_ref"] = control["version"]
                record["correction_digest"] = control["metadata"]["correction_digest"]
                record["available"] = False
                replacement = control["metadata"].get("replacement_attempt_id")
                if replacement is not None:
                    if replacement not in records:
                        raise ContractViolation("evidence correction references missing replacement")
                    substitute = records[replacement]
                    if substitute["corrects_attempt_id"] not in (None, record["attempt_id"]):
                        raise ContractViolation("evidence replacement already corrects another attempt")
                    record["replacement_attempt_id"] = replacement
                    substitute["corrects_attempt_id"] = record["attempt_id"]
        self._validate_relations(records)
        # Propagate invalidation to a fixed point, independent of source order.
        changed = True
        while changed:
            changed = False
            for record in records.values():
                guidance = record.get("guidance_version")
                parent = record.get("parent_attempt_id")
                corrected = record.get("corrects_attempt_id")
                if ((parent and not records[parent]["available"]
                     and not (parent == corrected and not records[parent]["invalidated"]))
                        or (corrected and records[corrected]["invalidated"])
                        or (guidance is not None and not any(
                            guidance in item["candidate_refs"] and item["scope"] == record["scope"]
                            and item["available"] for item in records.values()))) and not record["invalidated"]:
                    record["available"] = False
                    record["invalidated"] = True
                    changed = True
        for boundary in runs.values():
            start, finish = boundary["started_at"], boundary["finished_at"]
            if start is not None and finish is not None and finish < start:
                raise ContractViolation("evidence run completion precedes start")
            activity = boundary["last_activity_at"]
            if (boundary["unbounded_activity"]
                    or (finish is not None and activity is not None and activity > finish)):
                boundary["finished_at"] = None
        return (tuple(records[key] for key in sorted(records)
                      if include_unavailable or records[key]["available"]), runs)

    @staticmethod
    def _validate_relations(records: Mapping[str, Mapping[str, Any]]) -> None:
        for key, record in records.items():
            corrected = record.get("corrects_attempt_id")
            if corrected is not None:
                original = records.get(corrected)
                if (original is None or any(original[field] != record[field]
                                            for field in ("scope", "task_type", "policy_digest"))):
                    raise ContractViolation("evidence replacement is missing or incompatible")
                if record["origin"] != "runtime" or not record["verified"] or not record["candidate_digest"]:
                    raise ContractViolation("evidence replacement must be a verified runtime candidate")
            guidance = record.get("guidance_version")
            if guidance is not None and not any(
                guidance in item["candidate_refs"] and item["scope"] == record["scope"]
                and item["origin"] == "agent_claim"
                for item in records.values()
            ):
                raise ContractViolation("evidence guidance is missing or incompatible")
            visited = set()
            pending: list[Tuple[str, frozenset[str]]] = [(key, frozenset())]
            while pending:
                current, ancestors = pending.pop()
                if current in ancestors:
                    raise ContractViolation("evidence derivation cycle")
                if current in visited:
                    continue
                visited.add(current)
                if current not in records:
                    raise ContractViolation("evidence relation references missing parent")
                if records[current]["scope"] != record["scope"]:
                    raise ContractViolation("evidence relation crosses scope")
                for field in ("parent_attempt_id", "corrects_attempt_id"):
                    target = records[current].get(field)
                    if target:
                        pending.append((target, ancestors | {current}))

    def _controls(self) -> Tuple[Mapping[str, Any], ...]:
        if self.control_path is None:
            return ()
        if self.control_path.exists():
            data = self.control_path.read_bytes()
            if data and not data.endswith(b"\n"):
                raise ContractViolation("incomplete evidence control history requires recovery")
        records = tuple(JsonlEventSink(self.control_path).read())
        previous = None
        for record in records:
            body = {k: v for k, v in record.items() if k != "version"}
            if (record.get("schema_version") != 1 or record.get("previous") != previous
                    or record.get("version") != _digest(body)
                    or record.get("action") not in ("link", "correct", "revoke", "forget")):
                raise ContractViolation("invalid evidence control history")
            _identifier(record.get("actor"), "actor")
            _identifier(record.get("scope"), "scope")
            _identifier(record.get("attempt_id"), "attempt_id")
            metadata = record.get("metadata")
            expected = ({"parent_attempt_id", "guidance_version"} if record["action"] == "link"
                        else {"correction_digest"} if record["action"] == "correct" else set())
            if record["action"] == "correct" and isinstance(metadata, dict) and "replacement_attempt_id" in metadata:
                expected.add("replacement_attempt_id")
                _identifier(metadata["replacement_attempt_id"], "replacement_attempt_id")
            if not isinstance(metadata, dict) or set(metadata) != expected:
                raise ContractViolation("invalid evidence control metadata")
            if record["action"] == "link":
                for key, value in metadata.items():
                    if value is not None:
                        _identifier(value, key)
            elif record["action"] == "correct":
                _candidate_digest(metadata["correction_digest"])
            previous = record["version"]
        return records

    def control(self, attempt_id: str, action: str, *, actor: str,
                parent_attempt_id: Optional[str] = None,
                guidance_version: Optional[str] = None,
                correction: Optional[Mapping[str, Any]] = None,
                replacement_attempt_id: Optional[str] = None) -> Mapping[str, Any]:
        if self.control_path is None:
            raise ContractViolation("evidence control_path is required for local metadata")
        _identifier(actor, "actor")
        if action not in ("link", "correct", "revoke", "forget"):
            raise ContractViolation("invalid evidence control action")
        if replacement_attempt_id is not None and action != "correct":
            raise ContractViolation("evidence replacement requires a correction")
        with file_lock(self.control_path.with_suffix(".lock")):
            facts = {r["attempt_id"]: dict(r) for r in self.snapshot(include_unavailable=True)}
            if attempt_id not in facts or (not facts[attempt_id]["available"] and action not in ("revoke", "forget")):
                raise ContractViolation("unknown or unavailable evidence attempt")
            metadata: Dict[str, Any] = {}
            if action == "link":
                if parent_attempt_id is not None:
                    _identifier(parent_attempt_id, "parent_attempt_id")
                if guidance_version is not None:
                    _identifier(guidance_version, "guidance_version")
                metadata = {"parent_attempt_id": parent_attempt_id, "guidance_version": guidance_version}
                facts[attempt_id].update(metadata)
                self._validate_relations(facts)
            elif action == "correct":
                if not isinstance(correction, Mapping) or not correction:
                    raise ContractViolation("evidence correction cannot be empty")
                metadata = {"correction_digest": _digest(correction)}
                if replacement_attempt_id is not None:
                    _identifier(replacement_attempt_id, "replacement_attempt_id")
                    if (replacement_attempt_id not in facts or not facts[replacement_attempt_id]["available"]
                            or facts[replacement_attempt_id]["corrects_attempt_id"] is not None):
                        raise ContractViolation("unknown, unavailable or already used evidence replacement")
                    facts[replacement_attempt_id]["corrects_attempt_id"] = attempt_id
                    self._validate_relations(facts)
                    metadata["replacement_attempt_id"] = replacement_attempt_id
            controls = self._controls()
            body = {
                "schema_version": 1, "attempt_id": attempt_id, "scope": facts[attempt_id]["scope"],
                "action": action, "actor": actor, "metadata": metadata,
                "previous": controls[-1]["version"] if controls else None,
            }
            record = {**body, "version": _digest(body)}
            append_jsonl(self.control_path, record, label="evidence control")
            if self.index is not None:
                self.index.prune("evidence:" + facts[attempt_id]["scope"], ())
            return record

    def search(self, *, scope: str, task_type: str, policy_digest: str,
               max_items: int = 10, max_bytes: int = 8192,
               request: Optional[AgentRequest] = None,
               required_features: Sequence[str] = ()) -> Mapping[str, Any]:
        for name, value in (("scope", scope), ("task_type", task_type), ("policy_digest", policy_digest)):
            _identifier(value, name)
        if (isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1
                or max_items > 100 or isinstance(max_bytes, bool)
                or not isinstance(max_bytes, int) or max_bytes < 128 or max_bytes > 1048576):
            raise ContractViolation("invalid evidence retrieval bounds")
        response: Dict[str, Any] = {"schema_version": 1, "items": [], "fallback": "current_source_fixed_policy"}
        eligible = None
        certified_versions: Dict[str, str] = {}
        if required_features and request is None:
            raise ContractViolation("qualified evidence retrieval requires a request")
        if request is not None:
            if request.task_type != task_type:
                raise ContractViolation("evidence request task_type mismatch")
            response["eligibility_checked"] = True
            eligible = set()
            if (request.reuse_allowed and request.reuse_scope == scope
                    and self.executors is not None and self.certification is not None):
                supported = set(self.executors.eligible_executor_ids(request, required_features))
                for check in self.certification():
                    executor_id = check.check_id.removeprefix("adapter:")
                    version = check.details.get("version")
                    if (check.check_id.startswith("adapter:") and check.status == "pass"
                            and check.details.get("support_status") == "verified"
                            and isinstance(version, str) and version.strip()
                            and executor_id in supported):
                        eligible.add(executor_id)
                        certified_versions[executor_id] = version
        now = self._clock()
        for record in self.snapshot():
            if (record["scope"] != scope or record["task_type"] != task_type
                    or record["policy_digest"] != policy_digest or record["expires_at"] is None
                    or record["expires_at"] <= now):
                continue
            if eligible is not None and record["executor_id"] not in eligible:
                continue
            if request is not None and (
                record["executor_versions"].get(record["executor_id"]) != certified_versions.get(record["executor_id"])
                or record["data_classification"] is None
                or DATA_CLASSIFICATIONS.index(record["data_classification"]) > DATA_CLASSIFICATIONS.index(request.data_classification)
            ):
                continue
            proposed = {**response, "items": [*response["items"], record], "fallback": None}
            if len(encode_json(proposed).encode("utf-8")) > max_bytes:
                continue
            response = proposed
            if len(response["items"]) >= max_items:
                break
        if self.access_log is not None:
            append_jsonl(self.access_log, {
                "schema_version": 1, "event": "evidence_retrieved", "origin": "runtime",
                "scope": scope, "task_type": task_type, "policy_digest": policy_digest,
                "attempt_ids": [r["attempt_id"] for r in response["items"]],
                "context_bytes": len(encode_json(response).encode("utf-8")),
                "eligibility_checked": request is not None,
                "observed_at": now,
            }, label="evidence access")
        return response

    def summary(self) -> Mapping[str, Any]:
        """Account all supplied attempts, including withdrawn facts and unknown usage."""
        records, runs = self._snapshot(include_unavailable=True)
        usage = ModelUsage.combine(tuple(
            ModelUsage(**record["usage"])
            if record.get("usage") is not None else ModelUsage.unknown()
            for record in records if record["account_usage"]
        ))
        spans = [boundary["finished_at"] - boundary["started_at"] + (boundary["preparation_seconds"] or 0.0)
                 for boundary in runs.values()
                 if boundary["started_at"] is not None and boundary["finished_at"] is not None]
        unknown_runs = len(runs) - len(spans)
        return {
            "schema_version": 1, "coverage": "supplied_execution_sources",
            "attempts": len(records), "available": sum(record["available"] for record in records),
            "statuses": {status: sum(record["status"] == status for record in records)
                         for status in sorted({record["status"] for record in records})},
            "usage": usage.to_dict(), "roi_admission": "not_evaluated",
            "wall_clock": {
                "aggregation": "sum_per_run_spans", "known_seconds": sum(spans) if spans or not runs else None,
                "complete": unknown_runs == 0, "known_runs": len(spans), "unknown_runs": unknown_runs,
                "bases": {basis: sum(boundary["basis"] == basis for boundary in runs.values())
                          for basis in sorted({boundary["basis"] for boundary in runs.values()
                                               if boundary["basis"] is not None})},
            },
        }

    def portable(self) -> Mapping[str, Any]:
        # A strict allowlist excludes paths, raw IDs, code, prompts, trajectories,
        # commands, outputs and free-text guidance even if a source contains them.
        fields = ("attempt_id", "task_type", "origin", "status", "failure_category",
                  "candidate_digest", "policy_digest", "verified", "independent")
        return {"schema_version": 1, "items": [
            {key: record[key] for key in fields} for record in self.snapshot()
        ]}

    def record_injection(self, selection: Mapping[str, Any], fingerprint: str, context_bytes: int) -> None:
        if self.access_log is not None:
            append_jsonl(self.access_log, {
                "schema_version": 1, "event": "evidence_context_injected", "origin": "runtime",
                "attempt_ids": [r["attempt_id"] for r in selection["items"]],
                "context_fingerprint": fingerprint, "context_bytes": context_bytes,
                "observed_at": self._clock(),
            }, label="evidence context injection")
