#!/usr/bin/env python3
"""Fail if a module re-implements a shared durable-state primitive.

These helpers used to be copy-pasted into more than a dozen modules and the
copies drifted: some skipped ``fsync``, none flushed the containing directory,
and temporary-file cleanup differed. Since crash-safe continuation depends on
those details, the primitives now live only in ``grapheng/_store.py``.

Run from the repository root:

    python scripts/check_shared_primitives.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "grapheng"
OWNER = "_store.py"

#: Function names that may only be defined in ``grapheng/_store.py``.
RESERVED_NAMES = {
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
}

#: Private spellings of the same helpers, which is how the duplicates hid.
RESERVED_PREFIXES = ("_atomic_", "_exclusive_")
RESERVED_PRIVATE = {
    "_append_jsonl",
    "_encode_json",
    "_file_lock",
    "_json_digest",
    "_read_json",
    "_read_json_object",
    "_sha256",
}


def offending(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        name = node.name
        if (
            name in RESERVED_NAMES
            or name in RESERVED_PRIVATE
            or name.startswith(RESERVED_PREFIXES)
        ):
            found.append((name, node.lineno))
    return found


def main() -> int:
    failures = []
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == OWNER:
            continue
        for name, lineno in offending(path):
            failures.append(f"{path.relative_to(PACKAGE.parent)}:{lineno}: {name}")
    if failures:
        print(
            "These durable-state primitives may only be defined in "
            f"grapheng/{OWNER}:",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(
            "\nImport them from grapheng._store instead of re-implementing them.",
            file=sys.stderr,
        )
        return 1
    print(f"ok: durable-state primitives are defined only in grapheng/{OWNER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
