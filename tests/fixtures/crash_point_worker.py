#!/usr/bin/env python3
"""Child-process scenarios used by the crash-point harness."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

from grapheng import EffectJournal
from grapheng._store import atomic_json_write, file_lock, read_json_object
from grapheng.durable_outbox import DurableOutbox
from grapheng.effects import NodeEffectJournal
from grapheng.leases import claim_lease

CRASH_EXIT_CODE = 86


def crash(selected: str, reached: str) -> None:
    if selected == reached:
        os._exit(CRASH_EXIT_CODE)


def lease_takeover(root: Path, selected: str) -> None:
    state_path = root / "lease-state.json"
    with file_lock(root / "lease-state.lock"):
        state = dict(read_json_object(state_path, label="crash lease state"))
        claim_lease(
            state,
            run_id="run-crash",
            owner_id="owner-two",
            lease_seconds=30,
            now=200,
            takeover=True,
        )
        atomic_json_write(state_path, state, label="crash lease state")
    crash(selected, "takeover_committed")


def effect_commit(root: Path, selected: str) -> None:
    journal = NodeEffectJournal(
        root / "effects",
        fault_injector=lambda stage, _record: crash(selected, stage),
    )

    def invoke():
        (root / "external-effect.json").write_text(
            json.dumps({"effect_id": "node-crash-effect"}), encoding="utf-8"
        )
        return {"answer": "committed"}, {"answer": "committed"}

    journal.execute(
        effect_id="node-crash-effect",
        run_id="run-crash",
        node_id="mutate",
        effect_type="verified_idempotent",
        inputs={"value": 1},
        lease={
            "run_id": "run-crash",
            "owner_id": "owner-one",
            "generation": 1,
            "token_digest": "a" * 64,
        },
        workspace_identity={"path": str(root.resolve())},
        invoke=invoke,
        restore=lambda outcome: dict(outcome),
    )


def outbox_ack(root: Path, selected: str) -> None:
    outbox = DurableOutbox(root, clock=lambda: 100.0)
    outbox.publish(
        "enqueue-00000001",
        "task-crash",
        "enqueue",
        {"queue_job_id": "job-crash"},
    )
    outbox.acknowledge("enqueue-00000001", {"queue_job_id": "job-crash"})
    crash(selected, "ack_committed")


def protected_postcondition(root: Path, selected: str) -> None:
    guard = root / "guard.txt"
    journal = EffectJournal(root / "engineering-effects")

    def effect():
        guard.write_text("unauthorized", encoding="utf-8")
        return {"summary": "changed"}

    def postcondition(_result):
        crash(selected, "before_postcondition")
        if guard.read_text(encoding="utf-8") != "approved":
            raise RuntimeError("protected path changed")

    journal.execute(
        "implement-0",
        {"task_id": "engineering-implement"},
        effect,
        postcondition,
        recovery_context={"protected_snapshot": {"guard.txt": "approved"}},
    )


def directory_fsync(root: Path, selected: str) -> None:
    target = root / "directory-fsync.json"
    original_fsync = os.fsync
    calls = 0

    def faulting_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            crash(selected, "before_directory_fsync")
        original_fsync(descriptor)

    with patch("grapheng._store.os.fsync", side_effect=faulting_fsync):
        atomic_json_write(target, {"version": 2}, label="directory fsync probe")


SCENARIOS = {
    "lease_takeover": lease_takeover,
    "effect_commit": effect_commit,
    "outbox_ack": outbox_ack,
    "protected_postcondition": protected_postcondition,
    "directory_fsync": directory_fsync,
}


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in SCENARIOS:
        return 2
    SCENARIOS[sys.argv[1]](Path(sys.argv[2]), sys.argv[3])
    return 87


if __name__ == "__main__":
    raise SystemExit(main())
