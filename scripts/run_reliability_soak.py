#!/usr/bin/env python3
"""Run the deterministic recovery soak and emit auditable JSON evidence."""

from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Mapping

from grapheng._store import atomic_json_write
from grapheng.effects import NodeEffectJournal
from grapheng.events import JsonlEventSink
from grapheng.leases import LeaseLostError, assert_lease, claim_lease
from grapheng.model import GraphSpec
from grapheng.runtime import GraphRuntime, NodeRegistry

CANARY_ADAPTER_PROTOCOLS = {
    "codex": "exec-jsonl-v1",
    "claude-code": "json-envelope-v1",
    "pi-agent": "message-end-jsonl-v1",
    "opencode": "run-jsonl-v1",
    "orca": "orca-json-command-v1",
}
CANARY_TOOL_PROBE_IDS = {
    "codex": "codex",
    "claude-code": "claude",
    "pi-agent": "pi",
    "opencode": "opencode",
    "orca": "orca",
}
CANARY_CRASH_POINTS = (
    "before_write",
    "after_write",
    "before_output_parse",
    "after_output_parse",
    "orca_message",
    "orca_merge",
    "orca_cleanup",
)


class SimulatedProcessDeath(BaseException):
    pass


class StableEffect:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.workspace_identity = {"path": str(workspace.resolve())}
        self.calls = 0

    def __call__(self, context):
        self.calls += 1
        path = self.workspace / "applied-effects.json"
        applied = json.loads(path.read_text()) if path.exists() else []
        if context.effect_id not in applied:
            applied.append(context.effect_id)
            path.write_text(json.dumps(applied), encoding="utf-8")
        return {"effect_value": context.effect_id}


def soak_graph() -> GraphSpec:
    return GraphSpec.from_dict(
        {
            "id": "reliability-soak",
            "require_reality_anchor": False,
            "nodes": [
                {"id": "prepare", "kind": "prepare", "writes": ["prepared"]},
                {
                    "id": "mutate",
                    "kind": "mutate",
                    "deps": ["prepare"],
                    "reads": ["prepared"],
                    "writes": ["effect_value"],
                    "effect": "verified_idempotent",
                },
                {
                    "id": "verify",
                    "kind": "verify",
                    "deps": ["mutate"],
                    "reads": ["effect_value"],
                    "writes": ["verified"],
                },
            ],
        }
    )


def run_iteration(root: Path, index: int) -> Mapping[str, Any]:
    workspace = root / f"iteration-{index:04d}"
    workspace.mkdir(parents=True)
    handler = StableEffect(workspace)
    registry = NodeRegistry()
    registry.register("prepare", lambda _context: {"prepared": True})
    registry.register("mutate", handler)
    registry.register("verify", lambda _context: {"verified": True})
    runtime_root = workspace / "runtime"
    crashed = GraphRuntime(
        soak_graph(),
        registry,
        work_dir=runtime_root,
        effect_lease={
            "run_id": f"run-soak-{index}",
            "owner_id": "owner-one",
            "generation": 1,
            "token_digest": "a" * 64,
        },
    )

    def die_after_effect_commit(stage: str, _record: Mapping[str, Any]) -> None:
        if stage == "completed":
            raise SimulatedProcessDeath()

    crashed.effects = NodeEffectJournal(
        runtime_root / "effects", fault_injector=die_after_effect_commit
    )
    try:
        crashed.run(run_id=f"run-soak-{index}")
    except SimulatedProcessDeath:
        pass
    else:
        raise AssertionError("soak crash point was not reached")

    lease_state: Dict[str, Any] = {
        "run_id": f"run-soak-{index}",
        "phase": "queued",
        "owner_id": None,
        "generation": 1,
    }
    stale = claim_lease(
        lease_state,
        run_id=f"run-soak-{index}",
        owner_id="owner-one",
        lease_seconds=1,
        now=0,
        takeover=False,
    )
    try:
        assert_lease(lease_state, stale, now=1)
    except LeaseLostError:
        pass
    else:
        raise AssertionError("expired soak lease remained valid")
    current = claim_lease(
        lease_state,
        run_id=f"run-soak-{index}",
        owner_id="owner-two",
        lease_seconds=30,
        now=1,
        takeover=True,
    )

    started = time.monotonic()
    result = GraphRuntime(
        soak_graph(),
        registry,
        work_dir=runtime_root,
        effect_lease={
            "run_id": f"run-soak-{index}",
            "owner_id": current.owner_id,
            "generation": current.generation,
            "token_digest": current.digest(),
        },
    ).run(resume=True, run_id=f"run-soak-{index}")
    recovery_ms = (time.monotonic() - started) * 1000
    applied = json.loads((workspace / "applied-effects.json").read_text())
    events = tuple(JsonlEventSink(runtime_root / "events.jsonl").read())
    completed_nodes = [
        event.get("node_id") for event in events if event.get("event") == "node_completed"
    ]
    return {
        "success": result.success,
        "duplicate_effects": max(0, len(applied) - len(set(applied))),
        "effect_calls": handler.calls,
        "recovery_ms": round(recovery_ms, 3),
        "manual_interventions": 0,
        "terminal_divergences": 0 if result.success else 1,
        "checkpoints_observed": len(set(completed_nodes)),
        "lease_generation": current.generation,
    }


def probe_installed_tools() -> Mapping[str, Any]:
    probes = {}
    for name, argument in (
        ("codex", "--version"),
        ("claude", "--version"),
        ("pi", "--version"),
        ("opencode", "--version"),
    ):
        command = shutil.which(name)
        if command is None:
            probes[name] = {"installed": False, "version": None}
            continue
        try:
            completed = subprocess.run(
                [command, argument],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            version = (completed.stdout or completed.stderr).strip().splitlines()[-1]
            probes[name] = {
                "installed": True,
                "executable": Path(command).name,
                "returncode": completed.returncode,
                "version": version,
            }
        except (OSError, subprocess.SubprocessError) as error:
            probes[name] = {
                "installed": True,
                "executable": Path(command).name,
                "error": str(error),
            }
    plist = Path("/Applications/Orca.app/Contents/Info.plist")
    if plist.is_file():
        try:
            with plist.open("rb") as handle:
                metadata = plistlib.load(handle)
            probes["orca"] = {
                "installed": True,
                "executable": "orca",
                "version": metadata.get("CFBundleShortVersionString"),
            }
        except (OSError, plistlib.InvalidFileException) as error:
            probes["orca"] = {"installed": True, "error": str(error)}
    else:
        probes["orca"] = {"installed": shutil.which("orca") is not None, "version": None}
    return probes


def pending_real_canary(tool_probes: Mapping[str, Any]) -> Mapping[str, Any]:
    observed_versions = {}
    for adapter, probe_id in CANARY_TOOL_PROBE_IDS.items():
        probe = tool_probes.get(probe_id)
        observed_versions[adapter] = (
            probe.get("version") if isinstance(probe, Mapping) else None
        )
    return {
        "performed": False,
        "reason": "real model and Orca calls require an explicitly authorized canary run",
        "required_adapter_protocols": dict(CANARY_ADAPTER_PROTOCOLS),
        "required_crash_points": list(CANARY_CRASH_POINTS),
        "observed_versions": observed_versions,
    }


def run_soak(iterations: int, root: Path) -> Mapping[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be positive")
    observations = [run_iteration(root, index) for index in range(iterations)]
    recovery_times = [item["recovery_ms"] for item in observations]
    return {
        "schema_version": 1,
        "kind": "deterministic_recovery_soak",
        "iterations": iterations,
        "duplicate_effects": sum(item["duplicate_effects"] for item in observations),
        "manual_interventions": sum(
            item["manual_interventions"] for item in observations
        ),
        "terminal_divergences": sum(
            item["terminal_divergences"] for item in observations
        ),
        "minimum_checkpoints_observed": min(
            item["checkpoints_observed"] for item in observations
        ),
        "maximum_effect_calls_per_iteration": max(
            item["effect_calls"] for item in observations
        ),
        "maximum_lease_generation": max(
            item["lease_generation"] for item in observations
        ),
        "recovery_ms": {
            "minimum": min(recovery_times),
            "maximum": max(recovery_times),
            "average": round(sum(recovery_times) / len(recovery_times), 3),
        },
        "passed": all(item["success"] for item in observations)
        and all(item["duplicate_effects"] == 0 for item in observations)
        and all(item["terminal_divergences"] == 0 for item in observations)
        and all(item["checkpoints_observed"] >= 3 for item in observations),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--probe-installed-tools", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="agent-os-reliability-soak-") as directory:
        report = dict(run_soak(args.iterations, Path(directory)))
    report["tool_probes"] = probe_installed_tools() if args.probe_installed_tools else {}
    report["real_adapter_canary"] = pending_real_canary(report["tool_probes"])
    report["release_gate"] = "blocked_pending_real_adapter_canary"
    if args.output is None:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        atomic_json_write(args.output, report, label="reliability soak report")
        print(args.output)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
