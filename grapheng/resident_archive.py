"""Durable, append-only archives for terminal Resident queue entries."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

from ._store import (
    exclusive_json_write,
    json_digest,
    read_json_object,
    sha256_hex,
)
from .errors import ContractViolation
from .state_machine import TERMINAL_PHASES

RESIDENT_QUEUE_ARCHIVE_SCHEMA_VERSION = 1


def terminal_fact_evidence(
    path: Path, expected_phase: str
) -> Optional[Mapping[str, str]]:
    """Return immutable evidence for a matching terminal fact, if one exists."""

    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None
    try:
        payload = path.read_bytes()
        value = json.loads(payload)
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("phase") != expected_phase
        or expected_phase not in TERMINAL_PHASES
    ):
        return None
    return {
        "path": str(path.absolute()),
        "phase": expected_phase,
        "sha256": sha256_hex(payload),
    }


class ResidentQueueArchive:
    """Commit terminal queue metadata behind one idempotent archive operation."""

    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ContractViolation("resident queue archive cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)

    def store(
        self,
        entries: Mapping[str, Mapping[str, Any]],
        *,
        archived_at: float,
    ) -> Tuple[str, Path]:
        if (
            not entries
            or isinstance(archived_at, bool)
            or not isinstance(archived_at, (int, float))
            or not math.isfinite(float(archived_at))
        ):
            raise ContractViolation("resident queue archive input is invalid")
        normalized_entries = self._validate_entries(entries)
        normalized, digest = json_digest(
            normalized_entries,
            label="resident queue archive entries",
        )
        archive_id = f"resident-queue-{digest[:32]}"
        path = self.root / f"{archive_id}.json"
        payload = {
            "schema_version": RESIDENT_QUEUE_ARCHIVE_SCHEMA_VERSION,
            "archive_id": archive_id,
            "archived_at": float(archived_at),
            "entries": normalized,
        }
        try:
            exclusive_json_write(path, payload, label="resident queue archive")
        except FileExistsError:
            self._validate_existing(path, archive_id, normalized)
        return archive_id, path

    @staticmethod
    def _validate_entries(
        entries: Mapping[str, Mapping[str, Any]],
    ) -> Mapping[str, Mapping[str, Any]]:
        validated = {}
        for job_id in sorted(entries):
            entry = entries[job_id]
            queue_item = entry.get("queue_item")
            terminal_fact = entry.get("terminal_fact")
            if (
                not isinstance(job_id, str)
                or not isinstance(queue_item, Mapping)
                or queue_item.get("job_id") != job_id
                or queue_item.get("state") not in TERMINAL_PHASES
                or not isinstance(terminal_fact, Mapping)
                or terminal_fact.get("phase") != queue_item.get("state")
                or not isinstance(terminal_fact.get("path"), str)
                or not isinstance(terminal_fact.get("sha256"), str)
                or len(str(terminal_fact["sha256"])) != 64
            ):
                raise ContractViolation("resident queue archive entry is invalid")
            validated[job_id] = {
                "queue_item": dict(queue_item),
                "terminal_fact": dict(terminal_fact),
            }
        return validated

    @staticmethod
    def _validate_existing(
        path: Path,
        archive_id: str,
        entries: Mapping[str, Any],
    ) -> None:
        existing = read_json_object(path, label="resident queue archive")
        if (
            existing.get("schema_version") != RESIDENT_QUEUE_ARCHIVE_SCHEMA_VERSION
            or existing.get("archive_id") != archive_id
            or existing.get("entries") != entries
        ):
            raise ContractViolation("resident queue archive identity collision")
