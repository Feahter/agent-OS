import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from .agents import AgentExecution, AgentRequest, AgentResult
from .artifacts import ArtifactRecord, ArtifactStore
from .errors import ContractViolation
from .model import GraphSpec, NodeSpec
from .reuse import VerifiedArtifactCache


PUBLICATION_SCHEMA_VERSION = 1


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


def _publication_id(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _extract(value: Any, path: Sequence[str]) -> Any:
    current = value
    for part in path:
        if not isinstance(current, dict) or part not in current:
            raise ContractViolation(f"verification result is missing path {'.'.join(path)}")
        current = current[part]
    return current


@dataclass(frozen=True)
class ArtifactVersion:
    key: str
    producer: str
    version: int
    checksum: str

    @classmethod
    def from_record(cls, record: ArtifactRecord) -> "ArtifactVersion":
        return cls(record.key, record.producer, record.version, record.checksum)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactVersion":
        return cls(
            str(value["key"]),
            str(value["producer"]),
            int(value["version"]),
            str(value["checksum"]),
        )


@dataclass(frozen=True)
class PublicationCandidate:
    publication_id: str
    run_id: str
    node_id: str
    attempt: int
    task_id: str
    executor_id: str
    workspace: str
    tokens_used: int
    cost_usd: Optional[float]
    input_artifacts: Tuple[ArtifactVersion, ...]
    output_artifacts: Tuple[ArtifactVersion, ...]
    status: str = "pending"
    verifier_id: Optional[str] = None
    verifier_attempt: Optional[int] = None
    verification_id: Optional[str] = None
    quality_score: Optional[float] = None
    reason: Optional[str] = None
    verifier_inputs: Tuple[ArtifactVersion, ...] = ()
    verifier_outputs: Tuple[ArtifactVersion, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PublicationCandidate":
        candidate = cls(
            publication_id=str(value["publication_id"]),
            run_id=str(value["run_id"]),
            node_id=str(value["node_id"]),
            attempt=int(value["attempt"]),
            task_id=str(value["task_id"]),
            executor_id=str(value["executor_id"]),
            workspace=str(value["workspace"]),
            tokens_used=int(value["tokens_used"]),
            cost_usd=(
                None if value.get("cost_usd") is None else float(value["cost_usd"])
            ),
            input_artifacts=tuple(
                ArtifactVersion.from_dict(item)
                for item in value.get("input_artifacts", ())
            ),
            output_artifacts=tuple(
                ArtifactVersion.from_dict(item)
                for item in value.get("output_artifacts", ())
            ),
            status=str(value.get("status", "pending")),
            verifier_id=(
                None if value.get("verifier_id") is None else str(value["verifier_id"])
            ),
            verifier_attempt=(
                None
                if value.get("verifier_attempt") is None
                else int(value["verifier_attempt"])
            ),
            verification_id=(
                None
                if value.get("verification_id") is None
                else str(value["verification_id"])
            ),
            quality_score=(
                None
                if value.get("quality_score") is None
                else float(value["quality_score"])
            ),
            reason=None if value.get("reason") is None else str(value["reason"]),
            verifier_inputs=tuple(
                ArtifactVersion.from_dict(item)
                for item in value.get("verifier_inputs", ())
            ),
            verifier_outputs=tuple(
                ArtifactVersion.from_dict(item)
                for item in value.get("verifier_outputs", ())
            ),
        )
        candidate.validate()
        return candidate

    def validate(self) -> None:
        if self.status not in ("pending", "ready", "published", "skipped"):
            raise ContractViolation("invalid verified publication status")
        if self.attempt < 1 or self.tokens_used < 0:
            raise ContractViolation("invalid verified publication counters")
        if self.cost_usd is not None and (
            not math.isfinite(self.cost_usd) or self.cost_usd < 0
        ):
            raise ContractViolation("invalid verified publication cost")
        if self.quality_score is not None and (
            not math.isfinite(self.quality_score)
            or not 0 <= self.quality_score <= 1
        ):
            raise ContractViolation("invalid verified publication quality score")


@dataclass(frozen=True)
class PublicationEvent:
    event: str
    node_id: Optional[str]
    attempt: Optional[int]
    payload: Mapping[str, Any]


class VerifiedResultPublisher:
    """Binds exact graph artifacts to verified reuse publication receipts."""

    def __init__(self, cache: VerifiedArtifactCache, state_path: Path):
        self._cache = cache
        self._state_path = state_path
        self._candidates: Dict[str, PublicationCandidate] = {}
        if state_path.exists():
            try:
                value = json.loads(state_path.read_text(encoding="utf-8"))
                if value.get("schema_version") != PUBLICATION_SCHEMA_VERSION:
                    raise ContractViolation("unsupported verified publication schema")
                for raw in value.get("publications", ()):
                    candidate = PublicationCandidate.from_dict(raw)
                    self._candidates[candidate.publication_id] = candidate
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise ContractViolation(f"invalid verified publication state: {error}") from error

    def stage(
        self,
        graph: GraphSpec,
        run_id: str,
        node: NodeSpec,
        attempt: int,
        execution: Optional[AgentExecution],
        input_records: Sequence[ArtifactRecord],
        output_records: Sequence[ArtifactRecord],
    ) -> Optional[PublicationEvent]:
        if not any(
            verifier.verified_reuse is not None and verifier.verifier_for == node.id
            for verifier in graph.nodes
        ):
            return None
        if execution is None:
            return self._event(
                "verified_result_stage_skipped",
                node,
                attempt,
                None,
                "missing_agent_execution_receipt",
            )
        if execution.result.reuse_status not in ("none", "miss"):
            return self._event(
                "verified_result_stage_skipped",
                node,
                attempt,
                None,
                f"reuse_status={execution.result.reuse_status}",
            )
        self._validate_execution(node, execution, input_records, output_records)
        identity = {
            "run_id": run_id,
            "node_id": node.id,
            "attempt": attempt,
            "task_id": execution.request.task_id,
            "executor_id": execution.result.executor_id,
            "inputs": [asdict(ArtifactVersion.from_record(item)) for item in input_records],
            "outputs": [asdict(ArtifactVersion.from_record(item)) for item in output_records],
        }
        identifier = _publication_id(identity)
        if identifier in self._candidates:
            return None
        candidate = PublicationCandidate(
            publication_id=identifier,
            run_id=run_id,
            node_id=node.id,
            attempt=attempt,
            task_id=execution.request.task_id,
            executor_id=execution.result.executor_id,
            workspace=str(execution.request.workspace.resolve()),
            tokens_used=execution.result.tokens_used,
            cost_usd=execution.result.cost_usd,
            input_artifacts=tuple(
                ArtifactVersion.from_record(item) for item in input_records
            ),
            output_artifacts=tuple(
                ArtifactVersion.from_record(item) for item in output_records
            ),
        )
        candidate.validate()
        self._candidates[identifier] = candidate
        self._save()
        return self._event(
            "verified_result_staged", node, attempt, candidate, "awaiting_verifier"
        )

    def observe_verifier(
        self,
        graph: GraphSpec,
        artifacts: ArtifactStore,
        run_id: str,
        verifier: NodeSpec,
        attempt: int,
        input_records: Sequence[ArtifactRecord],
        output_records: Sequence[ArtifactRecord],
    ) -> Optional[PublicationEvent]:
        config = verifier.verified_reuse
        if config is None or verifier.verifier_for is None:
            return None
        candidate = self._matching_candidate(
            run_id, verifier.verifier_for, input_records
        )
        if candidate is None:
            return self._event(
                "verified_result_publish_skipped",
                verifier,
                attempt,
                None,
                "no_exact_staged_result",
            )
        if candidate.status == "published":
            return self._event(
                "verified_result_publish_replayed",
                verifier,
                attempt,
                candidate,
                "already_published",
            )
        decision = next(
            (
                record
                for record in output_records
                if record.key == config.decision_artifact
            ),
            None,
        )
        if decision is None:
            return self._skip(candidate, verifier, attempt, "decision_artifact_missing")
        try:
            passed = _extract(decision.value, config.passed_path)
            quality = _extract(decision.value, config.quality_path)
        except ContractViolation as error:
            return self._skip(candidate, verifier, attempt, str(error))
        if passed is not True:
            return self._skip(candidate, verifier, attempt, "verification_not_passed")
        if (
            isinstance(quality, bool)
            or not isinstance(quality, (int, float))
            or not math.isfinite(quality)
            or not 0 <= quality <= 1
        ):
            return self._skip(candidate, verifier, attempt, "invalid_quality_score")
        if quality < config.minimum_quality_score:
            return self._skip(candidate, verifier, attempt, "quality_below_threshold")
        ready = replace(
            candidate,
            status="ready",
            verifier_id=verifier.id,
            verifier_attempt=attempt,
            verification_id=f"{run_id}:{verifier.id}:{attempt}",
            quality_score=float(quality),
            reason=None,
            verifier_inputs=tuple(
                ArtifactVersion.from_record(item) for item in input_records
            ),
            verifier_outputs=tuple(
                ArtifactVersion.from_record(item) for item in output_records
            ),
        )
        self._replace(ready)
        return self._publish(graph, artifacts, ready)

    def reconcile(
        self, graph: GraphSpec, artifacts: ArtifactStore, run_id: str
    ) -> Tuple[PublicationEvent, ...]:
        events = []
        for candidate in tuple(self._candidates.values()):
            if candidate.run_id == run_id and candidate.status == "ready":
                events.append(self._publish(graph, artifacts, candidate))
        return tuple(events)

    def _publish(
        self, graph: GraphSpec, artifacts: ArtifactStore, candidate: PublicationCandidate
    ) -> PublicationEvent:
        try:
            source = graph.node_map()[candidate.node_id]
            spec = source.agent
            if spec is None or candidate.verification_id is None or candidate.quality_score is None:
                raise ContractViolation("verified publication receipt is incomplete")
            inputs = self._resolve(artifacts, candidate.input_artifacts)
            outputs = self._resolve(artifacts, candidate.output_artifacts)
            request = AgentRequest(
                task_id=candidate.task_id,
                prompt=spec.prompt,
                inputs=inputs,
                output_keys=source.writes,
                workspace=Path(candidate.workspace),
                model=spec.model,
                tools=spec.tools,
                timeout_seconds=spec.timeout_seconds,
                max_cost_usd=spec.max_cost_usd,
                data_classification=spec.data_classification,
                task_type=spec.task_type,
                model_family=spec.model_family,
                reuse_scope=spec.reuse_scope,
            )
            result = AgentResult(
                candidate.executor_id,
                outputs,
                json.dumps(outputs, ensure_ascii=False, sort_keys=True),
                candidate.tokens_used,
                candidate.cost_usd,
            )
            record = self._cache.publish_verified(
                request,
                result,
                candidate.run_id,
                candidate.verification_id,
                candidate.quality_score,
            )
        except Exception as error:
            return self._event(
                "verified_result_publish_deferred",
                self._verifier_node(graph, candidate),
                candidate.verifier_attempt,
                candidate,
                f"{type(error).__name__}: {error}",
            )
        published = replace(candidate, status="published", reason=None)
        self._replace(published)
        payload = dict(
            self._event(
                "verified_result_published",
                self._verifier_node(graph, published),
                published.verifier_attempt,
                published,
                "verified",
            ).payload
        )
        payload["reuse_key"] = record.key
        return PublicationEvent(
            "verified_result_published",
            published.verifier_id,
            published.verifier_attempt,
            payload,
        )

    def _matching_candidate(
        self,
        run_id: str,
        source_node_id: str,
        verifier_inputs: Sequence[ArtifactRecord],
    ) -> Optional[PublicationCandidate]:
        observed = {
            (item.key, item.version, item.checksum) for item in verifier_inputs
        }
        matches = [
            candidate
            for candidate in self._candidates.values()
            if candidate.run_id == run_id
            and candidate.node_id == source_node_id
            and all(
                (item.key, item.version, item.checksum) in observed
                for item in candidate.output_artifacts
            )
            and candidate.status in ("pending", "ready", "published")
        ]
        return max(matches, key=lambda item: item.attempt) if matches else None

    @staticmethod
    def _validate_execution(
        node: NodeSpec,
        execution: AgentExecution,
        inputs: Sequence[ArtifactRecord],
        outputs: Sequence[ArtifactRecord],
    ) -> None:
        spec = node.agent
        if spec is None:
            raise ContractViolation("only agent nodes can stage verified reuse")
        expected_inputs = {item.key: item.value for item in inputs}
        expected_outputs = {item.key: item.value for item in outputs}
        if execution.request.prompt != spec.prompt:
            raise ContractViolation("agent publication prompt does not match GraphSpec")
        if dict(execution.request.inputs) != expected_inputs:
            raise ContractViolation("agent publication inputs do not match artifact versions")
        if tuple(execution.request.output_keys) != tuple(node.writes):
            raise ContractViolation("agent publication output contract does not match GraphSpec")
        if dict(execution.result.outputs) != expected_outputs:
            raise ContractViolation("agent publication outputs do not match committed artifacts")

    @staticmethod
    def _resolve(
        artifacts: ArtifactStore, references: Iterable[ArtifactVersion]
    ) -> Mapping[str, Any]:
        result = {}
        for reference in references:
            record = artifacts.record(
                reference.key, reference.version, reference.checksum
            )
            if record is None or record.producer != reference.producer:
                raise ContractViolation(
                    f"artifact version unavailable: {reference.key}@{reference.version}"
                )
            result[reference.key] = record.value
        return result

    def _skip(
        self,
        candidate: PublicationCandidate,
        verifier: NodeSpec,
        attempt: int,
        reason: str,
    ) -> PublicationEvent:
        skipped = replace(
            candidate,
            status="skipped",
            verifier_id=verifier.id,
            verifier_attempt=attempt,
            verification_id=f"{candidate.run_id}:{verifier.id}:{attempt}",
            reason=reason,
        )
        self._replace(skipped)
        return self._event(
            "verified_result_publish_skipped", verifier, attempt, skipped, reason
        )

    def _replace(self, candidate: PublicationCandidate) -> None:
        candidate.validate()
        self._candidates[candidate.publication_id] = candidate
        self._save()

    def _save(self) -> None:
        _atomic_json_write(
            self._state_path,
            {
                "schema_version": PUBLICATION_SCHEMA_VERSION,
                "publications": [
                    item.to_dict()
                    for item in sorted(
                        self._candidates.values(), key=lambda value: value.publication_id
                    )
                ],
            },
        )

    @staticmethod
    def _verifier_node(
        graph: GraphSpec, candidate: PublicationCandidate
    ) -> Optional[NodeSpec]:
        if candidate.verifier_id is None:
            return None
        return graph.node_map().get(candidate.verifier_id)

    @staticmethod
    def _event(
        event: str,
        node: Optional[NodeSpec],
        attempt: Optional[int],
        candidate: Optional[PublicationCandidate],
        reason: str,
    ) -> PublicationEvent:
        payload: Dict[str, Any] = {"reason": reason}
        if candidate is not None:
            payload.update(
                {
                    "publication_id": candidate.publication_id,
                    "source_node_id": candidate.node_id,
                    "source_attempt": candidate.attempt,
                    "verification_id": candidate.verification_id,
                    "quality_score": candidate.quality_score,
                    "input_versions": [
                        {
                            "key": item.key,
                            "version": item.version,
                            "checksum": item.checksum,
                        }
                        for item in candidate.input_artifacts
                    ],
                    "output_versions": [
                        {
                            "key": item.key,
                            "version": item.version,
                            "checksum": item.checksum,
                        }
                        for item in candidate.output_artifacts
                    ],
                }
            )
        return PublicationEvent(event, node.id if node is not None else None, attempt, payload)
