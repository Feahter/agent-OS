"""Persistent local scheduling for Agent OS jobs.

The resident coordinator owns only scheduling metadata. Each job handler keeps
its own execution state and remains responsible for safe recovery.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Protocol, Union

from .errors import ContractViolation
from .model import GraphSpec


RESIDENT_QUEUE_SCHEMA_VERSION = 2
_ACTIVE_STATES = {
    "queued",
    "running",
    "waiting",
    "paused",
    "pause_requested",
    "cancel_requested",
}
_TERMINAL_STATES = {"succeeded", "failed", "cancelled"}
_TASK_ID = re.compile(r"^task-[0-9a-f]{16}$")
_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_JOB_KINDS = {"engineering", "graph", "orca"}


class ResidentJobHandler(Protocol):
    def inspect(self, reference: str) -> str:
        ...

    def execute(
        self, reference: str, control_probe: Callable[[], Optional[str]]
    ) -> Union[str, Mapping[str, Any]]:
        ...

    def record_failure(self, reference: str, failure: str) -> None:
        ...


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent), delete=False
    )
    try:
        with handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    finally:
        if os.path.exists(handle.name):
            os.unlink(handle.name)


@contextmanager
def _file_lock(path: Path, blocking: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as error:
            raise ContractViolation("the resident coordinator is already running") from error
        yield descriptor
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class ResidentCoordinator:
    """Runs heterogeneous jobs from one durable, priority-ordered local queue."""

    def __init__(
        self,
        home: Path,
        task_module_factory: Optional[Callable[[], Any]] = None,
        job_handlers: Optional[Mapping[str, ResidentJobHandler]] = None,
        clock: Callable[[], float] = time.time,
        poll_seconds: float = 0.2,
        heartbeat_seconds: float = 1.0,
    ):
        self.home = home.expanduser().absolute()
        self.root = self.home / "runtime" / "resident"
        if self.root.is_symlink():
            raise ContractViolation("resident runtime root cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        self.queue_path = self.root / "queue.json"
        self.queue_lock_path = self.root / "queue.lock"
        self.instance_lock_path = self.root / "instance.lock"
        self.instance_path = self.root / "instance.json"
        for path in (self.queue_path, self.queue_lock_path, self.instance_lock_path, self.instance_path):
            if path.is_symlink():
                raise ContractViolation("resident runtime files cannot be symlinks")
        self._clock = clock
        self._poll_seconds = float(poll_seconds)
        self._heartbeat_seconds = float(heartbeat_seconds)
        if self._poll_seconds <= 0 or self._heartbeat_seconds <= 0:
            raise ContractViolation("resident timing values must be positive")
        if task_module_factory is None:
            from .tasks import UserTaskModule

            task_module_factory = lambda: UserTaskModule(self.home)
        self._task_module_factory = task_module_factory
        from .resident_jobs import ResidentJobCatalog

        self._job_catalog = ResidentJobCatalog(self.home)
        self._job_handlers = dict(job_handlers or {})
        for kind, handler in self._job_catalog.handlers().items():
            self._job_handlers.setdefault(kind, handler)
        self._job_handlers.setdefault(
            "engineering", _EngineeringJobHandler(self._task_module_factory)
        )
        self._stop = threading.Event()
        if not self.queue_path.exists():
            with _file_lock(self.queue_lock_path):
                if not self.queue_path.exists():
                    self._write_queue(self._empty_queue())

    def submit(self, task_id: str, priority: int = 0) -> Mapping[str, Any]:
        self._validate_task_id(task_id)
        return self.schedule("engineering", task_id, priority)

    def schedule(
        self, kind: str, reference: str, priority: int = 0
    ) -> Mapping[str, Any]:
        self._validate_job(kind, reference)
        if kind not in self._job_handlers:
            raise ContractViolation(f"resident job kind {kind} has no handler")
        priority = self._priority(priority)
        now = self._clock()
        job_id = self._job_id(kind, reference)
        with _file_lock(self.queue_lock_path):
            queue = self._read_queue()
            items = queue["items"]
            existing = items.get(job_id)
            if isinstance(existing, dict) and existing.get("state") in _ACTIVE_STATES:
                raise ContractViolation(f"resident job {job_id} is already scheduled")
            sequence = queue["next_sequence"]
            queue["next_sequence"] = sequence + 1
            items[job_id] = {
                "job_id": job_id,
                "kind": kind,
                "reference": reference,
                "priority": priority,
                "sequence": sequence,
                "state": "queued",
                "requested_action": None,
                "submitted_at": now,
                "updated_at": now,
                "attempts": 0,
                "error": None,
            }
            queue["updated_at"] = now
            self._write_queue(queue)
            return dict(items[job_id])

    def schedule_graph(
        self, graph: GraphSpec, workspace: Path, priority: int = 0
    ) -> Mapping[str, Any]:
        reference = self._job_catalog.graph.prepare(graph, workspace)
        return self.schedule("graph", reference, priority)

    def schedule_orca(
        self, graph: GraphSpec, workspace: Path, priority: int = 0
    ) -> Mapping[str, Any]:
        reference = self._job_catalog.orca.prepare(graph, workspace)
        return self.schedule("orca", reference, priority)

    def inspect(self, task_id: str) -> Optional[Mapping[str, Any]]:
        self._validate_task_id(task_id)
        return self.inspect_job("engineering", task_id)

    def inspect_job(self, kind: str, reference: str) -> Optional[Mapping[str, Any]]:
        self._validate_job(kind, reference)
        job_id = self._job_id(kind, reference)
        with _file_lock(self.queue_lock_path):
            item = self._read_queue()["items"].get(job_id)
            return dict(item) if isinstance(item, dict) else None

    def request(
        self,
        task_id: str,
        action: str,
        priority: Optional[int] = None,
    ) -> Mapping[str, Any]:
        self._validate_task_id(task_id)
        return self.request_job("engineering", task_id, action, priority)

    def request_job(
        self,
        kind: str,
        reference: str,
        action: str,
        priority: Optional[int] = None,
    ) -> Mapping[str, Any]:
        if action not in ("pause", "resume", "cancel", "reprioritize"):
            raise ContractViolation("unsupported resident job control action")
        self._validate_job(kind, reference)
        job_id = self._job_id(kind, reference)
        now = self._clock()
        with _file_lock(self.queue_lock_path):
            queue = self._read_queue()
            item = queue["items"].get(job_id)
            if not isinstance(item, dict):
                raise ContractViolation(f"resident job {job_id} is not scheduled")
            state = item["state"]
            if state in _TERMINAL_STATES:
                raise ContractViolation(f"resident job {job_id} is already {state}")
            if action == "pause":
                if state in ("queued", "waiting"):
                    item["state"] = "paused"
                elif state == "running":
                    item["state"] = "pause_requested"
                else:
                    raise ContractViolation(f"resident job {job_id} cannot pause from {state}")
                item["requested_action"] = "pause"
            elif action == "resume":
                if state not in ("queued", "paused", "pause_requested"):
                    raise ContractViolation(f"resident job {job_id} cannot resume from {state}")
                item["state"] = "queued"
                item["requested_action"] = None
            elif action == "cancel":
                if state == "queued":
                    item["state"] = "cancelled"
                elif state in ("waiting", "paused") and kind == "orca":
                    item["state"] = "cancel_requested"
                elif state in ("waiting", "paused"):
                    item["state"] = "cancelled"
                elif state in ("running", "pause_requested"):
                    item["state"] = "cancel_requested"
                else:
                    raise ContractViolation(f"resident job {job_id} cannot cancel from {state}")
                item["requested_action"] = "cancel"
            else:
                item["priority"] = self._priority(priority)
            item["updated_at"] = now
            queue["updated_at"] = now
            self._write_queue(queue)
            return dict(item)

    def control_probe(self, task_id: str) -> Optional[str]:
        self._validate_task_id(task_id)
        return self.job_control_probe("engineering", task_id)

    def job_control_probe(self, kind: str, reference: str) -> Optional[str]:
        item = self.inspect_job(kind, reference)
        if item is None:
            return None
        if item["state"] in ("cancel_requested", "cancelled") or item.get(
            "requested_action"
        ) == "cancel":
            return "cancel"
        if item["state"] == "pause_requested":
            return "pause"
        return None

    def serve_once(self) -> bool:
        self._refresh_waiting()
        item = self._claim_next()
        if item is None:
            return False
        job_id = str(item["job_id"])
        kind = str(item["kind"])
        reference = str(item["reference"])
        handler = self._job_handlers[kind]
        try:
            cancel = getattr(handler, "cancel", None)
            if item.get("requested_action") == "cancel" and callable(cancel):
                result = cancel(reference)
                phase = (
                    str(result.get("phase", "failed"))
                    if isinstance(result, Mapping)
                    else str(result)
                )
                if phase not in _TERMINAL_STATES:
                    phase = "failed"
                self._settle(job_id, phase, None)
                return True
            phase = str(handler.inspect(reference))
            if phase in _TERMINAL_STATES:
                self._settle(job_id, phase, None)
                return True
            if phase == "waiting":
                self._settle(job_id, "waiting", None)
                return True
            result = handler.execute(
                reference, lambda: self.job_control_probe(kind, reference)
            )
            phase = (
                str(result.get("phase", "failed"))
                if isinstance(result, Mapping)
                else str(result)
            )
            if phase not in _TERMINAL_STATES | {"paused", "waiting"}:
                phase = "failed"
            self._settle(job_id, phase, None)
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            try:
                handler.record_failure(reference, failure)
            finally:
                self._settle(job_id, "failed", failure)
        return True

    def serve_forever(self) -> None:
        with _file_lock(self.instance_lock_path, blocking=False):
            self._recover_interrupted()
            heartbeat = threading.Thread(target=self._heartbeat_loop, daemon=True)
            heartbeat.start()
            try:
                while not self._stop.is_set():
                    if not self.serve_once():
                        self._stop.wait(self._poll_seconds)
            finally:
                self._stop.set()
                heartbeat.join(timeout=max(1.0, self._heartbeat_seconds * 2))
                self.instance_path.unlink(missing_ok=True)

    def stop(self) -> None:
        self._stop.set()

    def start_background(self, timeout: float = 3.0) -> None:
        if self.is_running():
            return
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "grapheng.resident_main",
                "--home",
                str(self.home),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_running():
                return
            time.sleep(0.05)
        raise ContractViolation("resident coordinator did not start")

    def is_running(self) -> bool:
        try:
            value = json.loads(self.instance_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        pid = value.get("pid")
        heartbeat_at = value.get("heartbeat_at")
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or isinstance(heartbeat_at, bool)
            or not isinstance(heartbeat_at, (int, float))
            or self._clock() - float(heartbeat_at) > self._heartbeat_seconds * 4
        ):
            return False
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    def _claim_next(self) -> Optional[Mapping[str, Any]]:
        with _file_lock(self.queue_lock_path):
            queue = self._read_queue()
            candidates = [
                item
                for item in queue["items"].values()
                if isinstance(item, dict)
                and item.get("state") in ("queued", "cancel_requested")
            ]
            if not candidates:
                return None
            item = min(
                candidates,
                key=lambda value: (-int(value["priority"]), int(value["sequence"])),
            )
            now = self._clock()
            item["state"] = "running"
            item["updated_at"] = now
            item["attempts"] = int(item["attempts"]) + 1
            queue["updated_at"] = now
            self._write_queue(queue)
            return dict(item)

    def _settle(self, job_id: str, phase: str, error: Optional[str]) -> None:
        with _file_lock(self.queue_lock_path):
            queue = self._read_queue()
            item = queue["items"].get(job_id)
            if not isinstance(item, dict):
                return
            if phase == "paused":
                item["state"] = "paused"
                item["requested_action"] = "pause"
            else:
                item["state"] = phase
                item["requested_action"] = None
            item["error"] = error
            item["updated_at"] = self._clock()
            queue["updated_at"] = item["updated_at"]
            self._write_queue(queue)

    def _recover_interrupted(self) -> None:
        with _file_lock(self.queue_lock_path):
            queue = self._read_queue()
            changed = False
            now = self._clock()
            for item in queue["items"].values():
                if not isinstance(item, dict):
                    continue
                state = item.get("state")
                if state == "pause_requested":
                    item["state"] = "paused"
                elif state in ("running", "cancel_requested"):
                    item["state"] = "queued"
                else:
                    continue
                item["updated_at"] = now
                changed = True
            if changed:
                queue["updated_at"] = now
                self._write_queue(queue)

    def _refresh_waiting(self) -> None:
        with _file_lock(self.queue_lock_path):
            waiting = [
                (str(item["job_id"]), str(item["kind"]), str(item["reference"]))
                for item in self._read_queue()["items"].values()
                if isinstance(item, dict) and item.get("state") == "waiting"
            ]
        for job_id, kind, reference in waiting:
            phase = str(self._job_handlers[kind].inspect(reference))
            if phase in _TERMINAL_STATES:
                self._settle(job_id, phase, None)
            elif phase != "waiting":
                self._requeue_waiting(job_id)

    def _requeue_waiting(self, job_id: str) -> None:
        with _file_lock(self.queue_lock_path):
            queue = self._read_queue()
            item = queue["items"].get(job_id)
            if not isinstance(item, dict) or item.get("state") != "waiting":
                return
            item["state"] = "queued"
            item["updated_at"] = self._clock()
            queue["updated_at"] = item["updated_at"]
            self._write_queue(queue)

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            _atomic_json_write(
                self.instance_path,
                {
                    "pid": os.getpid(),
                    "heartbeat_at": self._clock(),
                    "queue_schema_version": RESIDENT_QUEUE_SCHEMA_VERSION,
                },
            )
            self._stop.wait(self._heartbeat_seconds)

    def _read_queue(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.queue_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(f"cannot read resident queue: {error}") from error
        migrated_from_v1 = isinstance(value, dict) and value.get("schema_version") == 1
        if migrated_from_v1:
            value = self._migrate_v1_queue(value)
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != RESIDENT_QUEUE_SCHEMA_VERSION
            or isinstance(value.get("next_sequence"), bool)
            or not isinstance(value.get("next_sequence"), int)
            or value.get("next_sequence", 0) < 1
            or not isinstance(value.get("items"), dict)
        ):
            raise ContractViolation("resident queue has an invalid contract")
        for job_id, item in value["items"].items():
            if not isinstance(item, dict) or set(item) != {
                "job_id",
                "kind",
                "reference",
                "priority",
                "sequence",
                "state",
                "requested_action",
                "submitted_at",
                "updated_at",
                "attempts",
                "error",
            }:
                raise ContractViolation("resident queue item has an invalid contract")
            if (
                item["job_id"] != job_id
                or item["kind"] not in _JOB_KINDS
                or not isinstance(item["reference"], str)
                or _REFERENCE.fullmatch(item["reference"]) is None
                or job_id != self._job_id(item["kind"], item["reference"])
                or isinstance(item["priority"], bool)
                or not isinstance(item["priority"], int)
                or not -100 <= item["priority"] <= 100
                or isinstance(item["sequence"], bool)
                or not isinstance(item["sequence"], int)
                or item["sequence"] < 1
                or item["state"] not in _ACTIVE_STATES | _TERMINAL_STATES
                or item["requested_action"] not in (None, "pause", "cancel")
                or isinstance(item["attempts"], bool)
                or not isinstance(item["attempts"], int)
                or item["attempts"] < 0
                or (item["error"] is not None and not isinstance(item["error"], str))
            ):
                raise ContractViolation("resident queue item fields are invalid")
            for field in ("submitted_at", "updated_at"):
                if isinstance(item[field], bool) or not isinstance(
                    item[field], (int, float)
                ):
                    raise ContractViolation("resident queue item timestamps are invalid")
        if migrated_from_v1:
            self._write_queue(value)
        return value

    def _migrate_v1_queue(self, value: Mapping[str, Any]) -> Dict[str, Any]:
        items = value.get("items")
        if not isinstance(items, dict):
            raise ContractViolation("resident queue has an invalid v1 contract")
        migrated = dict(value)
        migrated["schema_version"] = RESIDENT_QUEUE_SCHEMA_VERSION
        migrated["items"] = {}
        for task_id, item in items.items():
            if (
                not isinstance(task_id, str)
                or _TASK_ID.fullmatch(task_id) is None
                or not isinstance(item, dict)
            ):
                raise ContractViolation("resident queue has an invalid v1 task")
            job_id = self._job_id("engineering", task_id)
            converted = dict(item)
            converted.pop("task_id", None)
            converted.update(
                {"job_id": job_id, "kind": "engineering", "reference": task_id}
            )
            migrated["items"][job_id] = converted
        return migrated

    def _write_queue(self, value: Mapping[str, Any]) -> None:
        _atomic_json_write(self.queue_path, value)

    def _empty_queue(self) -> Dict[str, Any]:
        return {
            "schema_version": RESIDENT_QUEUE_SCHEMA_VERSION,
            "next_sequence": 1,
            "updated_at": self._clock(),
            "items": {},
        }

    @staticmethod
    def _priority(value: Optional[int]) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not -100 <= value <= 100:
            raise ContractViolation("task priority must be an integer between -100 and 100")
        return value

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
            raise ContractViolation("invalid resident task id")

    @staticmethod
    def _job_id(kind: str, reference: str) -> str:
        return f"{kind}:{reference}"

    @staticmethod
    def _validate_job(kind: str, reference: str) -> None:
        if kind not in _JOB_KINDS:
            raise ContractViolation("unsupported resident job kind")
        if not isinstance(reference, str) or _REFERENCE.fullmatch(reference) is None:
            raise ContractViolation("invalid resident job reference")


class _EngineeringJobHandler:
    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory

    def inspect(self, task_id: str) -> str:
        phase = str(self._factory().execution_phase(task_id))
        return "queued" if phase in ("running", "paused") else phase

    def execute(
        self, task_id: str, control_probe: Callable[[], Optional[str]]
    ) -> Mapping[str, Any]:
        return self._factory().execute_queued(task_id, control_probe)

    def record_failure(self, task_id: str, failure: str) -> None:
        self._factory().record_queue_failure(task_id, failure)
