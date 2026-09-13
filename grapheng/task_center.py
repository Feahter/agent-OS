"""Task-center notification support for the local resident lifecycle."""

import hashlib
import json
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Protocol

from ._store import (
    atomic_json_write,
    file_lock,
)
from .errors import ContractViolation

TASK_CENTER_SCHEMA_VERSION = 1
RESIDENT_NOTIFICATION_SCHEMA_VERSION = 1
_NOTIFIABLE_STATES = {"waiting", "paused", "succeeded", "failed", "cancelled"}


class NotificationSink(Protocol):
    """Receives a bounded, prompt-free local notification."""

    def send(self, title: str, body: str) -> None:
        ...


class DesktopNotificationSink:
    """Uses the host notification command without invoking a shell."""

    def __init__(
        self,
        style: str,
        executable: str,
        runner: Callable[..., Any] = subprocess.run,
    ):
        if style not in ("macos", "linux"):
            raise ContractViolation("unsupported desktop notification style")
        self.style = style
        self.executable = executable
        self._runner = runner

    @classmethod
    def discover(cls) -> Optional["DesktopNotificationSink"]:
        system = platform.system()
        if system == "Darwin":
            executable = shutil.which("osascript")
            return cls("macos", executable) if executable else None
        if system == "Linux":
            executable = shutil.which("notify-send")
            return cls("linux", executable) if executable else None
        return None

    def send(self, title: str, body: str) -> None:
        title = _bounded_text(title, 80)
        body = _bounded_text(body, 240)
        if self.style == "macos":
            script = (
                f'display notification "{_apple_script_text(body)}" '
                f'with title "{_apple_script_text(title)}"'
            )
            command = [self.executable, "-e", script]
        else:
            command = [self.executable, title, body]
        self._runner(
            command,
            check=True,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class ResidentNotificationJournal:
    """Makes desktop notification attempts durable and at-most-once."""

    def __init__(
        self,
        root: Path,
        sink: NotificationSink,
        clock: Callable[[], float],
        maximum_entries: int = 256,
    ):
        self.path = root / "notifications.json"
        self.lock_path = root / "notifications.lock"
        self.sink = sink
        self.clock = clock
        self.maximum_entries = maximum_entries
        for path in (self.path, self.lock_path):
            if path.is_symlink():
                raise ContractViolation("resident notification files cannot be symlinks")
        if not self.path.exists():
            with file_lock(self.lock_path):
                if not self.path.exists():
                    self._write(self._empty())

    def dispatch(
        self, item: Mapping[str, Any], projection: Mapping[str, Any]
    ) -> bool:
        state = str(projection.get("state", ""))
        if state not in _NOTIFIABLE_STATES:
            return False
        identity = {
            "job_id": str(item["job_id"]),
            "sequence": int(item["sequence"]),
            "state": state,
            "summary": str(projection.get("summary", "")),
            "next_action": projection.get("next_action"),
        }
        encoded = json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        event_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        attempted_at = self.clock()
        with file_lock(self.lock_path):
            journal = self._read()
            if event_id in journal["entries"]:
                return False
            journal["entries"][event_id] = {
                "event_id": event_id,
                "job_id": identity["job_id"],
                "state": state,
                "status": "attempted",
                "attempted_at": attempted_at,
            }
            self._trim(journal)
            self._write(journal)

        title = {
            "waiting": "Agent OS needs your input",
            "paused": "Agent OS task paused",
            "succeeded": "Agent OS task completed",
            "failed": "Agent OS task failed",
            "cancelled": "Agent OS task cancelled",
        }[state]
        body = (
            f"{projection.get('kind', 'job')} {projection.get('reference', '')}: "
            f"{projection.get('summary', state)}"
        )
        status = "delivered"
        try:
            self.sink.send(title, body)
        except Exception:
            status = "failed"
        with file_lock(self.lock_path):
            journal = self._read()
            entry = journal["entries"].get(event_id)
            if isinstance(entry, dict):
                entry["status"] = status
                self._write(journal)
        return status == "delivered"

    def status(self) -> Mapping[str, int]:
        with file_lock(self.lock_path):
            entries = tuple(self._read()["entries"].values())
        return {
            "delivered": sum(item["status"] == "delivered" for item in entries),
            "failed": sum(item["status"] == "failed" for item in entries),
            "attempted": sum(item["status"] == "attempted" for item in entries),
        }

    def _read(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractViolation(
                f"cannot read resident notification journal: {error}"
            ) from error
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "entries"}
            or value.get("schema_version") != RESIDENT_NOTIFICATION_SCHEMA_VERSION
            or not isinstance(value.get("entries"), dict)
        ):
            raise ContractViolation("resident notification journal has an invalid contract")
        for event_id, entry in value["entries"].items():
            if (
                not isinstance(event_id, str)
                or len(event_id) != 64
                or not isinstance(entry, dict)
                or set(entry)
                != {"event_id", "job_id", "state", "status", "attempted_at"}
                or entry["event_id"] != event_id
                or not isinstance(entry["job_id"], str)
                or entry["state"] not in _NOTIFIABLE_STATES
                or entry["status"] not in ("attempted", "delivered", "failed")
                or isinstance(entry["attempted_at"], bool)
                or not isinstance(entry["attempted_at"], (int, float))
            ):
                raise ContractViolation(
                    "resident notification journal entry is invalid"
                )
        return value

    def _write(self, value: Mapping[str, Any]) -> None:
        atomic_json_write(self.path, value)

    def _trim(self, journal: Dict[str, Any]) -> None:
        entries = journal["entries"]
        if len(entries) <= self.maximum_entries:
            return
        ordered = sorted(entries.values(), key=lambda item: item["attempted_at"])
        for entry in ordered[: len(entries) - self.maximum_entries]:
            entries.pop(entry["event_id"], None)

    @staticmethod
    def _empty() -> Dict[str, Any]:
        return {
            "schema_version": RESIDENT_NOTIFICATION_SCHEMA_VERSION,
            "entries": {},
        }


def _bounded_text(value: str, maximum: int) -> str:
    compact = " ".join(str(value).split())
    return compact[:maximum]


def _apple_script_text(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
