"""Canonical durable-state primitives shared by every Agent OS module.

Every module used to carry its own copy of the atomic-write, JSON-read and
file-lock helpers. The copies had drifted: some skipped ``fsync``, none
flushed the containing directory, and temporary-file cleanup differed. Since
crash-safe continuation is a core contract, those primitives now live here and
nowhere else.

Durability contract for :func:`atomic_bytes_write` and its wrappers:

1. payload is written to a sibling temporary file in the target directory,
2. the file is flushed and ``fsync``-ed,
3. the temporary file is ``os.replace``-d onto the target,
4. the containing directory is ``fsync``-ed so the rename itself survives a
   power loss,
5. the temporary file is removed on every failure path.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Tuple

from .errors import ContractViolation

__all__ = [
    "append_jsonl",
    "atomic_bytes_write",
    "atomic_json_write",
    "atomic_text_write",
    "encode_json",
    "exclusive_json_write",
    "file_lock",
    "json_digest",
    "read_json",
    "read_json_object",
    "sha256_hex",
]


_MISSING = object()


def sha256_hex(data: bytes) -> str:
    """Return the hexadecimal SHA-256 digest of ``data``."""

    return hashlib.sha256(data).hexdigest()


def encode_json(value: Any, *, label: str = "value") -> str:
    """Serialize ``value`` with the canonical Agent OS JSON encoding.

    Canonical means: UTF-8 preserved, keys sorted, no insignificant
    whitespace. Digests computed over this encoding are stable across
    processes and Python versions.
    """

    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError) as error:
        raise ContractViolation(f"{label} must be JSON serializable: {error}") from error


def json_digest(value: Any, *, label: str = "value") -> Tuple[Any, str]:
    """Return ``(normalized_value, digest)`` for ``value``.

    The normalized value is the round-trip of the canonical encoding, so
    callers persist exactly what the digest covers.
    """

    encoded = encode_json(value, label=label)
    return json.loads(encoded), sha256_hex(encoded.encode("utf-8"))


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_bytes_write(path: Path, data: bytes) -> None:
    """Durably replace ``path`` with ``data``."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_text_write(path: Path, value: str) -> None:
    """Durably replace ``path`` with UTF-8 encoded ``value``."""

    atomic_bytes_write(path, value.encode("utf-8"))


def atomic_json_write(path: Path, value: Any, *, label: str = "value") -> None:
    """Durably replace ``path`` with the canonical JSON encoding of ``value``."""

    atomic_bytes_write(path, encode_json(value, label=label).encode("utf-8"))


def exclusive_json_write(path: Path, value: Any, *, label: str = "value") -> None:
    """Durably create ``path``, failing if another writer already created it.

    Uses ``os.link`` rather than ``os.replace`` so a second writer cannot
    silently overwrite an existing record. Callers rely on this for
    append-only stores such as evaluation records.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent)
    )
    temporary = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encode_json(value, label=label).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(str(temporary), str(path))
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def append_jsonl(path: Path, value: Any, *, label: str = "event") -> None:
    """Durably append one canonical JSON line to ``path``.

    A file ``fsync`` commits the line. When this call creates the file, a
    directory ``fsync`` also commits the new directory entry.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    line = (encode_json(value, label=label) + "\n").encode("utf-8")
    with path.open("ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    if created:
        _fsync_directory(path.parent)


def read_json(
    path: Path,
    default: Any = _MISSING,
    *,
    label: Optional[str] = None,
    require_object: bool = False,
) -> Any:
    """Read JSON from ``path``.

    ``default`` is returned when the file does not exist; omitting it makes a
    missing file a :class:`ContractViolation`. ``label`` prefixes error
    messages, and ``require_object`` rejects non-object documents.
    """

    path = Path(path)
    name = label or path.name
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        if default is not _MISSING:
            return default
        raise ContractViolation(f"{name} does not exist: {path}") from error
    except OSError as error:
        raise ContractViolation(f"cannot read {name}: {error}") from error
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ContractViolation(f"cannot read {name}: {error}") from error
    if require_object and not isinstance(value, dict):
        raise ContractViolation(f"{name} must be a JSON object")
    return value


def read_json_object(
    path: Path, default: Any = _MISSING, *, label: Optional[str] = None
) -> Mapping[str, Any]:
    """Read a JSON object from ``path``, rejecting other document types."""

    return read_json(path, default, label=label, require_object=True)


@contextmanager
def file_lock(
    path: Path,
    *,
    blocking: bool = True,
    shared: bool = False,
    busy_message: str = "resource is locked by another process",
    create_parents: bool = True,
) -> Iterator[int]:
    """Hold an advisory ``flock`` on ``path`` for the duration of the block.

    Yields the open descriptor. With ``blocking=False`` a contended lock
    raises :class:`ContractViolation` carrying ``busy_message`` instead of
    surfacing :class:`BlockingIOError`.
    """

    path = Path(path)
    if create_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    if not blocking:
        operation |= fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as error:
            raise ContractViolation(busy_message) from error
        try:
            yield descriptor
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
