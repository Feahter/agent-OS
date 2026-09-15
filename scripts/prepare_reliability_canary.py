#!/usr/bin/env python3
"""Prepare a fail-closed real Adapter/Orca canary preflight report."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import platform
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from grapheng._store import atomic_json_write

ADAPTER_PROTOCOLS = {
    "codex": "exec-jsonl-v1",
    "claude-code": "json-envelope-v1",
    "pi-agent": "message-end-jsonl-v1",
    "opencode": "run-jsonl-v1",
    "orca": "orca-json-command-v1",
}
TOOL_PROBE_IDS = {
    "codex": "codex",
    "claude-code": "claude",
    "pi-agent": "pi",
    "opencode": "opencode",
    "orca": "orca",
}
CRASH_POINTS = (
    "before_write",
    "after_write",
    "before_output_parse",
    "after_output_parse",
    "orca_message",
    "orca_merge",
    "orca_cleanup",
)
_VERSION_PATTERN = re.compile(
    r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)"
)
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40,64}")
_WAIVER_FIELDS = {
    "approved_by",
    "approved_at",
    "reason",
    "approval_reference",
}


def _observed_version(probe: Any) -> Optional[str]:
    if not isinstance(probe, Mapping):
        return None
    value = probe.get("version")
    if not isinstance(value, str):
        return None
    match = _VERSION_PATTERN.search(value)
    return match.group(1) if match is not None else None


def _certified_versions(
    compatibility_evidence: Mapping[str, Any], adapter: str
) -> set[str]:
    adapters = compatibility_evidence.get("adapters")
    if not isinstance(adapters, Mapping):
        return set()
    records = adapters.get(adapter)
    if not isinstance(records, list):
        return set()
    return {
        str(record["version"])
        for record in records
        if isinstance(record, Mapping)
        and record.get("protocol") == ADAPTER_PROTOCOLS[adapter]
        and isinstance(record.get("version"), str)
    }


def _accepted_waivers(
    waivers: Optional[Mapping[str, Any]],
) -> tuple[set[str], list[str]]:
    if waivers is None:
        return set(), []
    if set(waivers) - {"opencode"}:
        return set(), ["adapter_waiver_not_allowed"]
    accepted = set()
    blockers = []
    for adapter, waiver in waivers.items():
        timestamp_valid = False
        if isinstance(waiver, Mapping):
            approved_at = waiver.get("approved_at")
            if isinstance(approved_at, str):
                try:
                    datetime.fromisoformat(approved_at.replace("Z", "+00:00"))
                    timestamp_valid = True
                except ValueError:
                    pass
        if (
            not isinstance(waiver, Mapping)
            or set(waiver) != _WAIVER_FIELDS
            or not isinstance(waiver.get("approved_by"), str)
            or not waiver["approved_by"].strip()
            or not timestamp_valid
            or not isinstance(waiver.get("reason"), str)
            or not waiver["reason"].strip()
            or not isinstance(waiver.get("approval_reference"), str)
            or not waiver["approval_reference"].strip()
        ):
            blockers.append(f"adapter_waiver_invalid:{adapter}")
            continue
        accepted.add(adapter)
    return accepted, blockers


def build_preflight(
    *,
    tool_probes: Mapping[str, Any],
    compatibility_evidence: Mapping[str, Any],
    source_commit: str,
    dirty_worktree: bool,
    operating_system: str,
    architecture: str,
    operator: str,
    model_calls_authorized: bool,
    maximum_cost_usd: Optional[float],
    authorization_reference: str,
    observed_at: float,
    waivers: Optional[Mapping[str, Any]] = None,
) -> Mapping[str, Any]:
    blockers = []
    waived_adapters, waiver_blockers = _accepted_waivers(waivers)
    blockers.extend(waiver_blockers)
    if _COMMIT_PATTERN.fullmatch(source_commit) is None:
        blockers.append("source_commit_invalid")
    if dirty_worktree:
        blockers.append("dirty_worktree")
    if not operator.strip():
        blockers.append("operator_missing")
    if (
        model_calls_authorized is not True
        or isinstance(maximum_cost_usd, bool)
        or not isinstance(maximum_cost_usd, (int, float))
        or not math.isfinite(maximum_cost_usd)
        or maximum_cost_usd <= 0
        or not authorization_reference.strip()
    ):
        blockers.append("model_call_authorization_missing")

    certified_platform = compatibility_evidence.get("platform")
    if not isinstance(certified_platform, Mapping) or (
        certified_platform.get("operating_system") != operating_system
        or certified_platform.get("architecture") != architecture
    ):
        blockers.append("platform_not_certified")

    tools = {}
    for adapter, probe_id in TOOL_PROBE_IDS.items():
        probe = tool_probes.get(probe_id)
        installed = isinstance(probe, Mapping) and probe.get("installed") is True
        version = _observed_version(probe)
        certified_versions = _certified_versions(compatibility_evidence, adapter)
        certified = installed and version is not None and version in certified_versions
        tools[adapter] = {
            "installed": installed,
            "version": version,
            "protocol": ADAPTER_PROTOCOLS[adapter],
            "certified": certified,
            "certified_versions": sorted(certified_versions),
        }
        if adapter in waived_adapters:
            continue
        if not installed:
            blockers.append(f"tool_missing:{adapter}")
        elif version is None:
            blockers.append(f"tool_version_unknown:{adapter}")
        elif not certified:
            blockers.append(f"tool_version_not_certified:{adapter}:{version}")

    return {
        "schema_version": 1,
        "kind": "real_adapter_orca_canary_preflight",
        "observed_at": observed_at,
        "source_commit": source_commit,
        "dirty_worktree": dirty_worktree,
        "platform": {
            "operating_system": operating_system,
            "architecture": architecture,
        },
        "operator": operator,
        "authorization": {
            "model_calls": model_calls_authorized,
            "maximum_cost_usd": maximum_cost_usd,
            "reference": authorization_reference,
        },
        "tools": tools,
        "waivers": dict(waivers or {}),
        "required_crash_points": list(CRASH_POINTS),
        "model_calls": 0,
        "orca_objects_created": 0,
        "blockers": blockers,
        "ready": not blockers,
    }


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _tool_probes() -> Mapping[str, Any]:
    path = Path(__file__).with_name("run_reliability_soak.py")
    spec = importlib.util.spec_from_file_location("run_reliability_soak", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load reliability tool probes")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.probe_installed_tools()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--operator", default="")
    parser.add_argument("--authorize-model-calls", action="store_true")
    parser.add_argument("--maximum-cost-usd", type=float)
    parser.add_argument("--authorization-reference", default="")
    parser.add_argument("--waive-opencode", action="store_true")
    parser.add_argument("--waiver-approved-by", default="")
    parser.add_argument("--waiver-reference", default="")
    parser.add_argument("--waiver-reason", default="")
    args = parser.parse_args()

    evidence_path = Path(__file__).resolve().parents[1] / "grapheng" / (
        "compatibility-evidence.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    observed_at = time.time()
    waivers = None
    if args.waive_opencode:
        waivers = {
            "opencode": {
                "approved_by": args.waiver_approved_by,
                "approved_at": datetime.fromtimestamp(
                    observed_at, timezone.utc
                ).isoformat().replace("+00:00", "Z"),
                "reason": args.waiver_reason,
                "approval_reference": args.waiver_reference,
            }
        }
    report = build_preflight(
        tool_probes=_tool_probes(),
        compatibility_evidence=evidence,
        source_commit=_git("rev-parse", "HEAD"),
        dirty_worktree=bool(_git("status", "--porcelain")),
        operating_system=platform.system(),
        architecture=platform.machine(),
        operator=args.operator,
        model_calls_authorized=args.authorize_model_calls,
        maximum_cost_usd=args.maximum_cost_usd,
        authorization_reference=args.authorization_reference,
        observed_at=observed_at,
        waivers=waivers,
    )
    atomic_json_write(args.output, report, label="reliability canary preflight")
    print(args.output)
    if report["ready"]:
        return 0
    for blocker in report["blockers"]:
        print(f"blocked: {blocker}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
