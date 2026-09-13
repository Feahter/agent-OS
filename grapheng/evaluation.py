"""Prompt-free user outcome evaluation for Agent OS engineering runs."""

import hashlib
import json
import math
import re
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from ._store import exclusive_json_write, read_json_object
from .engineering import ENGINEERING_REPORT_SCHEMA_VERSION
from .errors import ContractViolation
from .routing import DATA_CLASSIFICATIONS

EVALUATION_CASE_SCHEMA_VERSION = 1
EVALUATION_RECORD_SCHEMA_VERSION = 1
EVALUATION_BASELINE_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIFFICULTIES = ("small", "medium", "large")


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ContractViolation(
            f"{field} must contain only letters, numbers, dot, underscore, or hyphen"
        )
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractViolation(f"{field} must be a non-negative integer")
    return value


def _non_negative_number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ContractViolation(f"{field} must be a finite non-negative number")
    return float(value)


def _unit_interval(value: Any, field: str) -> float:
    number = _non_negative_number(value, field)
    if number > 1:
        raise ContractViolation(f"{field} must be between zero and one")
    return number


def _failure_category(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).lower()
    categories = (
        ("timeout", ("timeout", "elapsed")),
        ("budget", ("budget", "cost", "token")),
        ("verification", ("reality", "verification")),
        ("checks", ("check", "test")),
        ("review", ("review", "finding")),
        ("executor", ("agent", "executor", "protocol", "provider")),
        ("contract", ("contract", "policy", "digest", "workspace")),
        ("recovery", ("resume", "recovery", "receipt")),
    )
    for category, markers in categories:
        if any(marker in text for marker in markers):
            return category
    return "unknown"


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    task_type: str
    difficulty: str
    data_classification: str = "public"
    tags: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _safe_id(self.case_id, "evaluation case_id")
        if not isinstance(self.task_type, str) or not self.task_type.strip():
            raise ContractViolation("evaluation task_type cannot be empty")
        if self.difficulty not in _DIFFICULTIES:
            raise ContractViolation(
                "evaluation difficulty must be small, medium, or large"
            )
        if self.data_classification not in DATA_CLASSIFICATIONS:
            raise ContractViolation("invalid evaluation data_classification")
        if any(
            not isinstance(tag, str) or not tag.strip() or len(tag) > 64
            for tag in self.tags
        ):
            raise ContractViolation("evaluation tags must be short non-empty strings")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": EVALUATION_CASE_SCHEMA_VERSION,
            "case_id": self.case_id,
            "task_type": self.task_type,
            "difficulty": self.difficulty,
            "data_classification": self.data_classification,
            "tags": list(self.tags),
        }

    @property
    def digest(self) -> str:
        return _canonical_digest(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvaluationCase":
        required = {
            "schema_version",
            "case_id",
            "task_type",
            "difficulty",
            "data_classification",
            "tags",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise ContractViolation("evaluation case has an invalid contract")
        if value["schema_version"] != EVALUATION_CASE_SCHEMA_VERSION:
            raise ContractViolation("unsupported evaluation case schema_version")
        tags = value["tags"]
        if not isinstance(tags, list):
            raise ContractViolation("evaluation tags must be an array")
        return cls(
            case_id=value["case_id"],
            task_type=value["task_type"],
            difficulty=value["difficulty"],
            data_classification=value["data_classification"],
            tags=tuple(tags),
        )

    @classmethod
    def load(cls, path: Path) -> "EvaluationCase":
        return cls.from_dict(read_json_object(path, label="evaluation case"))


@dataclass(frozen=True)
class EvaluationRecord:
    record_id: str
    run_id: str
    case_id: str
    case_digest: str
    task_type: str
    difficulty: str
    data_classification: str
    observed_at: float
    duration_seconds: float
    success: bool
    verified: bool
    quality_score: Optional[float]
    agent_calls: int
    tokens_used: int
    cost_usd: float
    cost_complete: bool
    user_inputs: int
    human_decisions: int
    review_cycles: int
    recovery_attempted: bool
    recovery_succeeded: bool
    failure_category: Optional[str]

    def __post_init__(self) -> None:
        _safe_id(self.record_id, "evaluation record_id")
        _safe_id(self.run_id, "evaluation run_id")
        _safe_id(self.case_id, "evaluation case_id")
        if (
            not isinstance(self.case_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.case_digest) is None
        ):
            raise ContractViolation("evaluation case_digest must be a SHA-256 digest")
        if not isinstance(self.task_type, str) or not self.task_type.strip():
            raise ContractViolation("evaluation task_type cannot be empty")
        if self.difficulty not in _DIFFICULTIES:
            raise ContractViolation(
                "evaluation difficulty must be small, medium, or large"
            )
        if self.data_classification not in DATA_CLASSIFICATIONS:
            raise ContractViolation("invalid evaluation data_classification")
        if not isinstance(self.success, bool) or not isinstance(self.verified, bool):
            raise ContractViolation("evaluation success and verified must be booleans")
        if not isinstance(self.cost_complete, bool):
            raise ContractViolation("evaluation cost_complete must be a boolean")
        for field in (
            "recovery_attempted",
            "recovery_succeeded",
        ):
            if not isinstance(getattr(self, field), bool):
                raise ContractViolation(f"evaluation {field} must be a boolean")
        if self.recovery_succeeded and not self.recovery_attempted:
            raise ContractViolation(
                "evaluation recovery cannot succeed when it was not attempted"
            )
        _non_negative_number(self.observed_at, "evaluation observed_at")
        _non_negative_number(self.duration_seconds, "evaluation duration_seconds")
        _non_negative_number(self.cost_usd, "evaluation cost_usd")
        for field in (
            "agent_calls",
            "tokens_used",
            "user_inputs",
            "human_decisions",
            "review_cycles",
        ):
            _non_negative_int(getattr(self, field), f"evaluation {field}")
        if self.quality_score is not None:
            _unit_interval(self.quality_score, "evaluation quality_score")
        if self.failure_category not in (
            None,
            "timeout",
            "budget",
            "verification",
            "checks",
            "review",
            "executor",
            "contract",
            "recovery",
            "unknown",
        ):
            raise ContractViolation("invalid evaluation failure_category")

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": EVALUATION_RECORD_SCHEMA_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvaluationRecord":
        fields = set(cls.__dataclass_fields__)
        if not isinstance(value, dict) or set(value) != fields | {"schema_version"}:
            raise ContractViolation("evaluation record has an invalid contract")
        if value["schema_version"] != EVALUATION_RECORD_SCHEMA_VERSION:
            raise ContractViolation("unsupported evaluation record schema_version")
        return cls(**{field: value[field] for field in fields})

    @classmethod
    def from_engineering_report(
        cls,
        case: EvaluationCase,
        run_id: str,
        report: Mapping[str, Any],
        user_inputs: int,
        human_decisions: int,
        recovery_attempted: bool = False,
        recovery_succeeded: bool = False,
        observed_at: Optional[float] = None,
    ) -> "EvaluationRecord":
        _safe_id(run_id, "evaluation run_id")
        if not isinstance(report, dict):
            raise ContractViolation("engineering evaluation report must be an object")
        if report.get("schema_version") != ENGINEERING_REPORT_SCHEMA_VERSION:
            raise ContractViolation("unsupported engineering evaluation report")
        started = _non_negative_number(
            report.get("started_at"), "engineering report started_at"
        )
        finished = _non_negative_number(
            report.get("finished_at"), "engineering report finished_at"
        )
        if finished < started:
            raise ContractViolation("engineering report finished before it started")
        success = report.get("success")
        if not isinstance(success, bool):
            raise ContractViolation("engineering report success must be a boolean")
        reality_anchor = report.get("reality_anchor")
        if not isinstance(reality_anchor, dict) or not isinstance(
            reality_anchor.get("passed"), bool
        ):
            raise ContractViolation("engineering report has an invalid reality_anchor")
        reviews = report.get("reviews")
        if not isinstance(reviews, list):
            raise ContractViolation("engineering report reviews must be an array")
        quality = None
        if reviews:
            last_review = reviews[-1]
            if not isinstance(last_review, dict) or "score" not in last_review:
                raise ContractViolation("engineering report review has no score")
            quality = _unit_interval(
                last_review["score"], "engineering report review score"
            )
        for field in ("cost_complete",):
            if not isinstance(report.get(field), bool):
                raise ContractViolation(f"engineering report {field} must be a boolean")
        record_id = _canonical_digest(
            {"run_id": run_id, "case_digest": case.digest}
        )[:32]
        return cls(
            record_id=record_id,
            run_id=run_id,
            case_id=case.case_id,
            case_digest=case.digest,
            task_type=case.task_type,
            difficulty=case.difficulty,
            data_classification=case.data_classification,
            observed_at=time.time() if observed_at is None else observed_at,
            duration_seconds=finished - started,
            success=success,
            verified=reality_anchor["passed"],
            quality_score=quality,
            agent_calls=_non_negative_int(
                report.get("agent_calls"), "engineering report agent_calls"
            ),
            tokens_used=_non_negative_int(
                report.get("tokens_used"), "engineering report tokens_used"
            ),
            cost_usd=_non_negative_number(
                report.get("cost_usd"), "engineering report cost_usd"
            ),
            cost_complete=report["cost_complete"],
            user_inputs=_non_negative_int(user_inputs, "evaluation user_inputs"),
            human_decisions=_non_negative_int(
                human_decisions, "evaluation human_decisions"
            ),
            review_cycles=_non_negative_int(
                report.get("review_cycles"), "engineering report review_cycles"
            ),
            recovery_attempted=recovery_attempted,
            recovery_succeeded=recovery_succeeded,
            failure_category=(
                None if success else _failure_category(report.get("failure"))
            ),
        )


class EvaluationLab:
    """Stores privacy-minimized run records and immutable baseline snapshots."""

    def __init__(self, root: Path, clock=time.time):
        if root.is_symlink():
            raise ContractViolation("evaluation root cannot be a symlink")
        self.root = root
        self.records_root = root / "records"
        self.baselines_root = root / "baselines"
        if self.records_root.is_symlink() or self.baselines_root.is_symlink():
            raise ContractViolation("evaluation state directories cannot be symlinks")
        self.records_root.mkdir(parents=True, exist_ok=True)
        self.baselines_root.mkdir(parents=True, exist_ok=True)
        self._clock = clock

    def record(self, record: EvaluationRecord) -> EvaluationRecord:
        target = self.records_root / f"{record.record_id}.json"
        if target.exists() or target.is_symlink():
            raise ContractViolation(
                f"evaluation run already recorded: {record.run_id}"
            )
        try:
            exclusive_json_write(target, record.to_dict())
        except FileExistsError as error:
            raise ContractViolation(
                f"evaluation run already recorded: {record.run_id}"
            ) from error
        return record

    def record_engineering(
        self,
        case: EvaluationCase,
        run_id: str,
        report_path: Path,
        user_inputs: int,
        human_decisions: int,
        recovery_attempted: bool = False,
        recovery_succeeded: bool = False,
    ) -> EvaluationRecord:
        record = EvaluationRecord.from_engineering_report(
            case,
            run_id,
            read_json_object(report_path, label="engineering report"),
            user_inputs,
            human_decisions,
            recovery_attempted,
            recovery_succeeded,
            observed_at=self._clock(),
        )
        return self.record(record)

    def records(self) -> Tuple[EvaluationRecord, ...]:
        records = []
        for path in sorted(self.records_root.glob("*.json")):
            if path.is_symlink():
                raise ContractViolation("evaluation records cannot contain symlinks")
            records.append(
                EvaluationRecord.from_dict(read_json_object(path, label="evaluation record"))
            )
        return tuple(records)

    def summary(self) -> Mapping[str, Any]:
        records = self.records()
        verified = [record for record in records if record.success and record.verified]
        durations = [record.duration_seconds for record in records]
        quality = [
            record.quality_score
            for record in records
            if record.quality_score is not None
        ]
        recoveries = [record for record in records if record.recovery_attempted]
        costs_complete = bool(records) and all(
            record.cost_complete for record in records
        )
        total_cost = sum(record.cost_usd for record in records)
        return {
            "schema_version": EVALUATION_BASELINE_SCHEMA_VERSION,
            "records_digest": _canonical_digest(
                [record.to_dict() for record in records]
            ),
            "runs": len(records),
            "cases": len({record.case_digest for record in records}),
            "verified_results": len(verified),
            "verified_success_rate": (
                len(verified) / len(records) if records else None
            ),
            "false_completions": sum(
                1 for record in records if record.success and not record.verified
            ),
            "average_duration_seconds": (
                sum(durations) / len(durations) if durations else None
            ),
            "p50_duration_seconds": (
                statistics.median(durations) if durations else None
            ),
            "agent_calls": sum(record.agent_calls for record in records),
            "tokens_used": sum(record.tokens_used for record in records),
            "total_cost_usd": total_cost,
            "cost_complete": costs_complete,
            "cost_per_verified_result_usd": (
                total_cost / len(verified)
                if verified and costs_complete
                else None
            ),
            "average_user_inputs": (
                sum(record.user_inputs for record in records) / len(records)
                if records
                else None
            ),
            "average_human_decisions": (
                sum(record.human_decisions for record in records) / len(records)
                if records
                else None
            ),
            "quality_samples": len(quality),
            "average_quality": (
                sum(quality) / len(quality) if quality else None
            ),
            "recovery_attempts": len(recoveries),
            "recovery_success_rate": (
                sum(1 for record in recoveries if record.recovery_succeeded)
                / len(recoveries)
                if recoveries
                else None
            ),
            "failures": {
                category: sum(
                    1 for record in records if record.failure_category == category
                )
                for category in sorted(
                    {
                        record.failure_category
                        for record in records
                        if record.failure_category is not None
                    }
                )
            },
        }

    def create_baseline(self, name: str) -> Mapping[str, Any]:
        _safe_id(name, "evaluation baseline name")
        target = self.baselines_root / f"{name}.json"
        if target.exists() or target.is_symlink():
            raise ContractViolation(f"evaluation baseline already exists: {name}")
        summary = self.summary()
        if summary["runs"] == 0:
            raise ContractViolation("evaluation baseline requires at least one run")
        value = {
            "schema_version": EVALUATION_BASELINE_SCHEMA_VERSION,
            "name": name,
            "created_at": self._clock(),
            "summary": summary,
        }
        try:
            exclusive_json_write(target, value)
        except FileExistsError as error:
            raise ContractViolation(
                f"evaluation baseline already exists: {name}"
            ) from error
        return value

    def status(self) -> Mapping[str, Any]:
        return {
            "summary": self.summary(),
            "baselines": [
                path.stem
                for path in sorted(self.baselines_root.glob("*.json"))
            ],
        }
