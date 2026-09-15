#!/usr/bin/env python3
"""Enforce versioned branch-coverage floors for high-risk orchestration code."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Start at the measured CM-09 baseline. Raise one entry whenever tests cover a
# newly exercised branch; never lower a floor to accommodate a regression.
BRANCH_FLOORS = {
    "grapheng/control.py": 60.25,
    "grapheng/runtime.py": 80.88,
    "grapheng/engineering.py": 72.33,
    "grapheng/resident.py": 69.36,
    "grapheng/coordinator.py": 64.80,
}


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "coverage.json")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"cannot read coverage report {report_path}: {error}", file=sys.stderr)
        return 2
    if not report.get("meta", {}).get("branch_coverage"):
        print("coverage report was not collected with branch coverage", file=sys.stderr)
        return 2

    failures = []
    files = report.get("files", {})
    for path, floor in BRANCH_FLOORS.items():
        summary = files.get(path, {}).get("summary")
        if not isinstance(summary, dict):
            failures.append(f"{path}: missing from coverage report")
            continue
        actual = summary.get("percent_branches_covered")
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            failures.append(f"{path}: branch percentage is missing")
            continue
        print(f"{path}: branch coverage {actual:.2f}% (floor {floor:.2f}%)")
        if actual + 1e-9 < floor:
            failures.append(
                f"{path}: branch coverage {actual:.2f}% is below {floor:.2f}%"
            )
    if failures:
        print("high-risk branch coverage regressed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
