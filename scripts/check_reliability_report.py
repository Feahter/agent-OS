#!/usr/bin/env python3
"""Fail the production gate unless soak and real Adapter evidence are complete."""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

REQUIRED_ADAPTERS = {"codex", "claude-code", "pi-agent", "opencode", "orca"}
WAIVABLE_ADAPTERS = {"opencode"}
REQUIRED_PROTOCOLS = {
    "codex": "exec-jsonl-v1",
    "claude-code": "json-envelope-v1",
    "pi-agent": "message-end-jsonl-v1",
    "opencode": "run-jsonl-v1",
    "orca": "orca-json-command-v1",
}
REQUIRED_CRASH_POINTS = {
    "before_write",
    "after_write",
    "before_output_parse",
    "after_output_parse",
    "orca_message",
    "orca_merge",
    "orca_cleanup",
}


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _timestamp(value: Any) -> Optional[datetime]:
    if not _nonempty(value):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _evidence_exists(report_path: Path, value: Any) -> bool:
    if not _nonempty(value):
        return False
    relative = Path(str(value))
    if relative.is_absolute():
        return False
    root = report_path.parent.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return candidate.is_file()


def _identity_failures(canary: Mapping[str, Any]) -> list[str]:
    failures = []
    commit = canary.get("source_commit")
    platform = canary.get("platform")
    started = _timestamp(canary.get("started_at"))
    finished = _timestamp(canary.get("finished_at"))
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40,64}", commit) is None:
        failures.append("real canary identity has no full source commit")
    if canary.get("dirty_worktree") is not False:
        failures.append("real canary identity must come from a clean worktree")
    if not isinstance(platform, Mapping) or not all(
        _nonempty(platform.get(field))
        for field in ("operating_system", "architecture")
    ):
        failures.append("real canary identity has no operating system/architecture")
    if not _nonempty(canary.get("operator")):
        failures.append("real canary identity has no operator")
    timestamps_valid = started is not None and finished is not None
    if timestamps_valid:
        try:
            timestamps_valid = finished >= started
        except TypeError:
            timestamps_valid = False
    if not timestamps_valid:
        failures.append("real canary identity has invalid start/end timestamps")
    return failures


def _authorization_failures(canary: Mapping[str, Any]) -> list[str]:
    authorization = canary.get("authorization")
    if not isinstance(authorization, Mapping):
        return ["real canary has no model-call authorization"]
    maximum = authorization.get("maximum_cost_usd")
    if (
        authorization.get("model_calls") is not True
        or isinstance(maximum, bool)
        or not isinstance(maximum, (int, float))
        or not math.isfinite(maximum)
        or maximum <= 0
        or not _nonempty(authorization.get("reference"))
    ):
        return ["real canary model-call authorization is incomplete"]
    return []


def _waivers(canary: Mapping[str, Any]) -> tuple[set[str], list[str]]:
    value = canary.get("waivers", {})
    if not isinstance(value, Mapping):
        return set(), ["real canary waivers must be an object"]
    unknown = set(value) - WAIVABLE_ADAPTERS
    failures = []
    if unknown:
        failures.append(
            "real canary contains non-waivable Adapter waivers: "
            + ", ".join(sorted(unknown))
        )
    accepted = set()
    required_fields = {
        "approved_by",
        "approved_at",
        "reason",
        "approval_reference",
    }
    for adapter in sorted(set(value) & WAIVABLE_ADAPTERS):
        waiver = value[adapter]
        if (
            not isinstance(waiver, Mapping)
            or set(waiver) != required_fields
            or not _nonempty(waiver.get("approved_by"))
            or _timestamp(waiver.get("approved_at")) is None
            or not _nonempty(waiver.get("reason"))
            or not _nonempty(waiver.get("approval_reference"))
        ):
            failures.append(f"real canary waiver is incomplete: {adapter}")
            continue
        accepted.add(adapter)
    return accepted, failures


def _adapter_failures(
    canary: Mapping[str, Any], report_path: Path, waived_adapters: set[str]
) -> list[str]:
    results = canary.get("adapter_results")
    required_adapters = REQUIRED_ADAPTERS - waived_adapters
    if not isinstance(results, Mapping) or set(results) != required_adapters:
        return ["real canary does not cover every required Adapter"]
    failures = []
    for adapter, protocol in REQUIRED_PROTOCOLS.items():
        if adapter in waived_adapters:
            continue
        result = results.get(adapter)
        if not isinstance(result, Mapping):
            failures.append(f"real canary Adapter result is invalid: {adapter}")
            continue
        if (
            result.get("passed") is not True
            or not _nonempty(result.get("version"))
            or result.get("protocol") != protocol
            or not _evidence_exists(report_path, result.get("evidence_path"))
        ):
            failures.append(
                f"real canary Adapter result lacks version/protocol/evidence: {adapter}"
            )
    return failures


def _crash_failures(canary: Mapping[str, Any], report_path: Path) -> list[str]:
    results = canary.get("crash_results")
    if not isinstance(results, Mapping) or set(results) != REQUIRED_CRASH_POINTS:
        return ["real canary does not cover every required crash point"]
    failures = []
    for crash_point in sorted(REQUIRED_CRASH_POINTS):
        result = results.get(crash_point)
        if not isinstance(result, Mapping):
            failures.append(f"real canary crash result is invalid: {crash_point}")
            continue
        retry_count = result.get("retry_count")
        if (
            result.get("passed") is not True
            or not _nonempty(result.get("effect_identity"))
            or not _nonempty(result.get("receipt_before"))
            or not _nonempty(result.get("receipt_after"))
            or not _nonempty(result.get("external_terminal"))
            or isinstance(retry_count, bool)
            or not isinstance(retry_count, int)
            or retry_count < 0
            or not isinstance(result.get("human_action_required"), bool)
            or not _evidence_exists(report_path, result.get("evidence_path"))
        ):
            failures.append(
                f"real canary crash result lacks outcome/receipt/evidence: {crash_point}"
            )
    return failures


def _canary_failures(canary: Any, report_path: Path) -> list[str]:
    if not isinstance(canary, Mapping) or canary.get("performed") is not True:
        return ["real Adapter/Orca canary was not performed"]
    failures = []
    failures.extend(_identity_failures(canary))
    failures.extend(_authorization_failures(canary))
    waived_adapters, waiver_failures = _waivers(canary)
    failures.extend(waiver_failures)
    failures.extend(_adapter_failures(canary, report_path, waived_adapters))
    failures.extend(_crash_failures(canary, report_path))
    if canary.get("duplicate_effects") != 0:
        failures.append("real canary observed duplicate effects")
    if canary.get("terminal_divergences") != 0:
        failures.append("real canary observed terminal divergence")
    return failures


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_reliability_report.py REPORT.json", file=sys.stderr)
        return 2
    report_path = Path(sys.argv[1])
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"cannot read reliability report: {error}", file=sys.stderr)
        return 2
    failures = []
    if report.get("schema_version") != 1:
        failures.append("unsupported report schema")
    if report.get("iterations", 0) < 100 or report.get("passed") is not True:
        failures.append("deterministic soak did not pass at least 100 iterations")
    for field in ("duplicate_effects", "manual_interventions", "terminal_divergences"):
        if report.get(field) != 0:
            failures.append(f"{field} must be zero")
    failures.extend(_canary_failures(report.get("real_adapter_canary"), report_path))
    if failures:
        print("reliability release gate blocked:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    print("reliability release gate passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
