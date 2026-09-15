#!/usr/bin/env python3
"""Prove that focused tests kill critical durability and consistency mutations."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    original: str
    replacement: str
    test: str


MUTATIONS = (
    Mutation(
        "lease fencing removed",
        "grapheng/leases.py",
        "    assert_lease(state, token, now=now)\n"
        "    state[\"heartbeat_at\"] = now\n",
        "    state[\"heartbeat_at\"] = now\n",
        "tests/test_control.py::ControlPlaneTests::"
        "test_expired_lease_token_is_invalid_before_takeover",
    ),
    Mutation(
        "protected-path postcondition removed",
        "grapheng/control.py",
        "            if postcondition is not None:\n"
        "                postcondition(result)\n",
        "            if postcondition is not None:\n"
        "                pass\n",
        "tests/test_control.py::ControlPlaneTests::"
        "test_effect_postcondition_failure_preserves_recovery_context",
    ),
    Mutation(
        "outbox acknowledgment idempotency removed",
        "grapheng/durable_outbox.py",
        "            if record[\"state\"] == \"acknowledged\":\n"
        "                if (\n"
        "                    record[\"acknowledgment_digest\"] != digest\n"
        "                    or record[\"acknowledgment\"] != normalized\n"
        "                ):\n"
        "                    raise ContractViolation(\n"
        "                        f\"outbox intent {intent_id} has a different acknowledgment\"\n"
        "                    )\n"
        "                return record\n",
        "            if record[\"state\"] == \"acknowledged\":\n"
        "                return record\n",
        "tests/test_durable_outbox.py::DurableOutboxTests::"
        "test_duplicate_and_out_of_order_acknowledgments_preserve_pending_intents",
    ),
    Mutation(
        "Resident waiting due guard removed",
        "grapheng/resident.py",
        "            if max(\n"
        "                float(persisted_due) if persisted_due is not None else 0.0,\n"
        "                self._waiting_probe_due.get(job_id, 0.0),\n"
        "            )\n"
        "            <= now\n",
        "            if True\n",
        "tests/test_resident.py::ResidentCoordinatorTests::"
        "test_waiting_probe_failures_back_off_and_persist_next_due_time",
    ),
    Mutation(
        "Resident event-driven sleep replaced by fixed polling",
        "grapheng/resident.py",
        "        timeout = min(delays)\n"
        "        return timeout if event_driven else min(self._poll_seconds, timeout)\n",
        "        timeout = min(delays)\n"
        "        return min(self._poll_seconds, timeout)\n",
        "tests/test_resident.py::ResidentCoordinatorTests::"
        "test_event_driven_idle_timeout_uses_next_recovery_sweep",
    ),
    Mutation(
        "Resident cross-process wakeup notification removed",
        "grapheng/resident.py",
        "    def _notify_wakeup(self) -> None:\n"
        "        self._wakeup.set()\n"
        "        if self.wakeup_path.is_symlink() or not self.wakeup_path.is_socket():\n",
        "    def _notify_wakeup(self) -> None:\n"
        "        self._wakeup.set()\n"
        "        return\n"
        "        if self.wakeup_path.is_symlink() or not self.wakeup_path.is_socket():\n",
        "tests/test_resident.py::ResidentCoordinatorTests::"
        "test_schedule_notifies_cross_instance_wakeup_channel",
    ),
    Mutation(
        "Resident queue-state projection precedence removed",
        "grapheng/resident.py",
        "        queue_state_is_authoritative = (\n"
        "            item.get(\"_scheduled\") is not False\n"
        "            and state\n"
        "            in {\n"
        "                \"queued\",\n"
        "                \"waiting\",\n"
        "                \"paused\",\n"
        "                \"pause_requested\",\n"
        "                \"cancel_requested\",\n"
        "                \"succeeded\",\n"
        "                \"failed\",\n"
        "                \"cancelled\",\n"
        "            }\n"
        "            and details_phase != state\n"
        "        )\n",
        "        queue_state_is_authoritative = False\n",
        "tests/test_task_center.py::TaskCenterTests::"
        "test_queue_control_state_overrides_stale_cached_summary_and_next_action",
    ),
    Mutation(
        "Resident terminal-fact archive guard bypassed",
        "grapheng/resident.py",
        "            fact = terminal_fact(str(item[\"reference\"]), str(item[\"state\"]))\n"
        "            if isinstance(fact, Mapping):\n"
        "                evidence[job_id] = dict(fact)\n",
        "            fact = terminal_fact(str(item[\"reference\"]), str(item[\"state\"]))\n"
        "            if not isinstance(fact, Mapping):\n"
        "                fact = {\n"
        "                    \"path\": \"missing-terminal-fact\",\n"
        "                    \"phase\": str(item[\"state\"]),\n"
        "                    \"sha256\": \"0\" * 64,\n"
        "                }\n"
        "            evidence[job_id] = dict(fact)\n",
        "tests/test_resident_archive.py::ResidentQueuePruneTests::"
        "test_prune_only_removes_old_terminal_jobs_with_matching_final_fact",
    ),
    Mutation(
        "context contract verbosity restored",
        "grapheng/context_compiler.py",
        "_TEXT_OUTPUT_PREFIX = \"\\nReturn only JSON with keys:\"\n",
        "_TEXT_OUTPUT_PREFIX = (\n"
        "    \"\\nYou must respond with a JSON object and no other text. \"\n"
        "    \"The JSON object must contain exactly these required output keys:\"\n"
        ")\n",
        "tests/test_context_compiler.py::ContextCompilerTests::"
        "test_fixture_context_stays_below_compact_contract_regression_limits",
    ),
    Mutation(
        "Agent reads projection bypassed",
        "grapheng/runtime.py",
        "        self._allowed_reads = frozenset(node.reads)\n"
        "        self._input_records = artifacts.snapshot(self._allowed_reads)\n",
        "        self._allowed_reads = frozenset(\n"
        "            record.key for record in artifacts.latest_records()\n"
        "        )\n"
        "        self._input_records = artifacts.snapshot(self._allowed_reads)\n",
        "tests/test_context_compiler.py::ContextCompilerTests::"
        "test_reads_projection_omits_undeclared_large_artifacts_before_budgeting",
    ),
    Mutation(
        "unknown economics cost coerced to zero",
        "grapheng/economics.py",
        "        else:\n"
        "            source = \"unknown\"\n"
        "            amount = None\n"
        "            complete = False\n",
        "        else:\n"
        "            source = \"unknown\"\n"
        "            amount = 0.0\n"
        "            complete = False\n",
        "tests/test_economics.py::RunEconomicsTests::"
        "test_measured_estimated_and_unknown_costs_are_distinct",
    ),
)


def apply_mutation(root: Path, mutation: Mutation) -> None:
    path = root / mutation.path
    source = path.read_text(encoding="utf-8")
    count = source.count(mutation.original)
    if count != 1:
        raise RuntimeError(
            f"{mutation.name}: expected one mutation site in {mutation.path}, found {count}"
        )
    path.write_text(source.replace(mutation.original, mutation.replacement), encoding="utf-8")


def mutation_is_killed(repository: Path, mutation: Mutation) -> bool:
    with tempfile.TemporaryDirectory(prefix="agent-os-mutation-") as directory:
        root = Path(directory)
        shutil.copytree(repository / "grapheng", root / "grapheng")
        shutil.copytree(repository / "tests", root / "tests")
        apply_mutation(root, mutation)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(root)
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", mutation.test],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            print(
                f"SURVIVED: {mutation.name}\n{completed.stdout}{completed.stderr}",
                file=sys.stderr,
            )
            return False
        print(f"KILLED: {mutation.name}")
        return True


def main() -> int:
    repository = Path(__file__).resolve().parent.parent
    try:
        results = [mutation_is_killed(repository, mutation) for mutation in MUTATIONS]
    except (OSError, RuntimeError) as error:
        print(f"mutation check could not run: {error}", file=sys.stderr)
        return 2
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
