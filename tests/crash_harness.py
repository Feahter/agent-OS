"""Reusable subprocess crash-point driver for durability tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CRASH_EXIT_CODE = 86


def run_crash_scenario(
    scenario: str,
    root: Path,
    crash_point: str,
    *,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess:
    """Run one worker and require it to terminate at ``crash_point``.

    A real child process calls ``os._exit`` at the selected boundary, so no
    Python exception unwinding, ``finally`` block or buffered cleanup can make
    the persisted state look safer than abrupt process death would.
    """

    repository = Path(__file__).resolve().parent.parent
    worker = Path(__file__).resolve().parent / "fixtures" / "crash_point_worker.py"
    environment = os.environ.copy()
    previous_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(repository)
        if not previous_path
        else os.pathsep.join((str(repository), previous_path))
    )
    completed = subprocess.run(
        [sys.executable, str(worker), scenario, str(root), crash_point],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != CRASH_EXIT_CODE:
        raise AssertionError(
            f"scenario {scenario!r} did not crash at {crash_point!r}; "
            f"returncode={completed.returncode}, stdout={completed.stdout!r}, "
            f"stderr={completed.stderr!r}"
        )
    return completed
