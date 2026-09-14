import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from grapheng import (
    AgentOS,
    ContractViolation,
    GraphSpec,
    NodeRegistry,
    OrcaCoordinator,
    OrcaMaterializedRun,
    ResidentCoordinator,
)
from grapheng.resident_jobs import ResidentJobCatalog


class FakeTasks:
    def __init__(self):
        self.executed = []
        self.failures = []
        self.phases = {}

    def execute_queued(self, task_id, control_probe):
        self.executed.append(task_id)
        action = control_probe()
        if action == "pause":
            self.phases[task_id] = "paused"
            return {"phase": "paused"}
        if action == "cancel":
            self.phases[task_id] = "cancelled"
            return {"phase": "cancelled"}
        self.phases[task_id] = "succeeded"
        return {"phase": "succeeded"}

    def execution_phase(self, task_id):
        return self.phases.get(task_id, "queued")

    def record_queue_failure(self, task_id, failure):
        self.failures.append((task_id, failure))


class FakeJob:
    def __init__(self):
        self.phases = {}
        self.executed = []
        self.inspections = []

    def inspect(self, reference):
        self.inspections.append(reference)
        return self.phases.get(reference, "queued")

    def execute(self, reference, control_probe):
        self.executed.append(reference)
        self.phases[reference] = "succeeded"
        return {"phase": "succeeded"}

    def record_failure(self, reference, failure):
        self.phases[reference] = "failed"


class FaultInjectingJob(FakeJob):
    def __init__(self):
        super().__init__()
        self.probe_failures = set()
        self.execute_failures = set()
        self.record_failure_fails = False

    def inspect(self, reference):
        if reference in self.probe_failures:
            self.inspections.append(reference)
            raise RuntimeError("probe unavailable")
        return super().inspect(reference)

    def execute(self, reference, control_probe):
        if reference in self.execute_failures:
            raise RuntimeError("execution failed")
        return super().execute(reference, control_probe)

    def record_failure(self, reference, failure):
        if self.record_failure_fails:
            raise RuntimeError("failure journal unavailable")
        super().record_failure(reference, failure)


class FakeOrcaBackend:
    def __init__(self):
        self.materialize_calls = 0
        self.starts = []
        self.acks = []
        self.finishes = []
        self.stops = []
        self.deliveries = []

    def materialize(self, plan):
        self.materialize_calls += 1
        return OrcaMaterializedRun(
            "run-orca",
            {task.node_id: f"task-{task.node_id}" for task in plan.tasks},
            {},
        )

    def start_worker(self, plan, materialized, node_id, attempt=1, retry_of=None):
        dispatch_id = f"dispatch-{node_id}-{attempt}"
        self.starts.append(dispatch_id)
        return {"dispatch": {"id": dispatch_id}}

    def wait_delivery(self, timeout_ms=900000):
        if not self.deliveries:
            return {"count": 0}
        return self.deliveries.pop(0)

    def acknowledge_delivery(self, delivery_id):
        self.acks.append(delivery_id)
        return {"deliveryId": delivery_id, "acknowledged": True}

    def finish_worker(self, dispatch_id, retain, succeeded):
        self.finishes.append((dispatch_id, retain, succeeded))
        return {"dispatchId": dispatch_id, "action": "released"}

    def stop_worker(self, dispatch_id):
        self.stops.append(dispatch_id)
        return {"dispatchId": dispatch_id, "stopped": True}

    def enqueue_done(self, node_id):
        self.deliveries.append(
            {
                "count": 1,
                "delivery": {
                    "deliveryId": "delivery-1",
                    "messages": [
                        {
                            "id": "done-1",
                            "type": "worker_done",
                            "taskId": f"task-{node_id}",
                            "dispatchId": f"dispatch-{node_id}-1",
                            "outcome": "succeeded",
                            "payload": {
                                "outputs": {"answer": 42},
                                "text": "complete",
                                "tokens_used": 1,
                            },
                        }
                    ],
                },
            }
        )


class ResidentCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tasks = FakeTasks()
        self.coordinator = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            clock=lambda: 100.0,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_legacy_home_is_rejected_before_resident_runtime_or_state_is_created(self):
        legacy_home = self.root / "legacy-home"
        AgentOS(legacy_home)

        with self.assertRaisesRegex(ContractViolation, "legacy Agent OS state"):
            ResidentCoordinator(legacy_home)

        self.assertFalse((legacy_home / "state").exists())
        self.assertFalse((legacy_home / "runtime").exists())

    def test_priority_queue_is_persistent_and_stable(self):
        low = "task-0000000000000001"
        high = "task-0000000000000002"
        self.coordinator.submit(low, priority=-1)
        self.coordinator.submit(high, priority=10)

        restored = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            clock=lambda: 101.0,
        )
        self.assertTrue(restored.serve_once())
        self.assertTrue(restored.serve_once())

        self.assertEqual([high, low], self.tasks.executed)
        self.assertEqual("succeeded", restored.inspect(high)["state"])
        self.assertEqual(1, restored.inspect(high)["attempts"])

    def test_queued_jobs_gain_priority_while_waiting(self):
        clock = [0.0]
        coordinator = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            clock=lambda: clock[0],
        )
        older = "task-0000000000000010"
        newer = "task-0000000000000011"
        coordinator.submit(older, priority=-1)
        clock[0] = 60.0
        coordinator.submit(newer, priority=0)

        claimed = coordinator._claim_next()

        self.assertEqual(older, claimed["reference"])

    def test_cancel_requested_job_preempts_ordinary_queued_work(self):
        handler = FakeJob()
        handler.phases["cancel-me"] = "waiting"
        coordinator = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            job_handlers={"orca": handler},
            clock=lambda: 100.0,
        )
        coordinator.schedule("orca", "cancel-me", priority=-100)
        self.assertTrue(coordinator.serve_once())
        coordinator.schedule("orca", "ordinary", priority=100)
        coordinator.request_job("orca", "cancel-me", "cancel")

        claimed = coordinator._claim_next()

        self.assertEqual("cancel-me", claimed["reference"])

    def test_pause_resume_cancel_and_reprioritize_are_queue_controls(self):
        one = "task-0000000000000001"
        two = "task-0000000000000002"
        self.coordinator.submit(one, priority=0)
        paused = self.coordinator.request(one, "pause")
        self.assertEqual("paused", paused["state"])
        self.assertFalse(self.coordinator.serve_once())

        updated = self.coordinator.request(one, "reprioritize", priority=20)
        self.assertEqual(20, updated["priority"])
        resumed = self.coordinator.request(one, "resume")
        self.assertEqual("queued", resumed["state"])
        self.assertTrue(self.coordinator.serve_once())

        self.coordinator.submit(two)
        cancelled = self.coordinator.request(two, "cancel")
        self.assertEqual("cancelled", cancelled["state"])
        with self.assertRaisesRegex(ContractViolation, "already cancelled"):
            self.coordinator.request(two, "resume")

    def test_interrupted_running_item_is_recovered_without_losing_priority(self):
        task_id = "task-0000000000000003"
        self.coordinator.submit(task_id, priority=7)
        self.assertEqual(task_id, self.coordinator._claim_next()["reference"])

        restored = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            clock=lambda: 102.0,
        )
        restored._recover_interrupted()
        self.assertTrue(restored.serve_once())

        item = restored.inspect(task_id)
        self.assertEqual("succeeded", item["state"])
        self.assertEqual(2, item["attempts"])
        self.assertEqual(7, item["priority"])

    def test_ensure_running_restarts_only_when_durable_work_is_recoverable(self):
        task_id = "task-0000000000000008"
        self.coordinator.start_background = Mock()

        self.assertFalse(self.coordinator.ensure_running())
        self.coordinator.start_background.assert_not_called()

        self.coordinator.submit(task_id, priority=7)
        self.coordinator._claim_next()

        self.assertTrue(self.coordinator.ensure_running())
        self.coordinator.start_background.assert_called_once_with()

    def test_resume_requeues_a_running_job_when_the_resident_is_stale(self):
        task_id = "task-0000000000000009"
        self.coordinator.submit(task_id)
        self.coordinator._claim_next()

        resumed = self.coordinator.request(task_id, "resume")

        self.assertEqual("queued", resumed["state"])
        self.assertIsNone(resumed["requested_action"])

    def test_invalid_priority_fails_closed(self):
        with self.assertRaisesRegex(ContractViolation, "between -100 and 100"):
            self.coordinator.submit("task-0000000000000004", priority=101)

    def test_recovery_reconciles_completed_task_before_reexecution(self):
        task_id = "task-0000000000000005"
        self.coordinator.submit(task_id)
        self.assertEqual(task_id, self.coordinator._claim_next()["reference"])
        self.tasks.phases[task_id] = "succeeded"

        restored = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            clock=lambda: 103.0,
        )
        restored._recover_interrupted()
        self.assertTrue(restored.serve_once())

        self.assertEqual([], self.tasks.executed)
        self.assertEqual("succeeded", restored.inspect(task_id)["state"])

    def test_tampered_queue_fails_closed(self):
        task_id = "task-0000000000000006"
        self.coordinator.submit(task_id)
        value = json.loads(self.coordinator.queue_path.read_text(encoding="utf-8"))
        value["items"][f"engineering:{task_id}"]["priority"] = 1000
        self.coordinator.queue_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(ContractViolation, "fields are invalid"):
            self.coordinator.inspect(task_id)

    def test_heterogeneous_jobs_share_priority_and_waiting_lifecycle(self):
        clock = [104.0]
        graph_job = FakeJob()
        orca_job = FakeJob()
        graph_job.phases["run-1"] = "waiting"
        coordinator = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            job_handlers={"graph": graph_job, "orca": orca_job},
            clock=lambda: clock[0],
        )
        coordinator.schedule("graph", "run-1", priority=20)
        coordinator.schedule("orca", "orca-1", priority=10)

        self.assertTrue(coordinator.serve_once())
        self.assertEqual("waiting", coordinator.inspect_job("graph", "run-1")["state"])
        self.assertTrue(coordinator.serve_once())
        self.assertEqual(["orca-1"], orca_job.executed)

        graph_job.phases["run-1"] = "queued"
        clock[0] = 105.0
        self.assertTrue(coordinator.serve_once())
        self.assertEqual(["run-1"], graph_job.executed)

    def test_waiting_probe_failure_does_not_block_other_jobs(self):
        handler = FaultInjectingJob()
        handler.phases["broken"] = "waiting"
        coordinator = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            job_handlers={"graph": handler},
            clock=lambda: 104.0,
        )
        coordinator.schedule("graph", "broken", priority=10)
        self.assertTrue(coordinator.serve_once())
        handler.probe_failures.add("broken")
        coordinator.schedule("graph", "healthy", priority=0)

        self.assertTrue(coordinator.serve_once())

        self.assertEqual("waiting", coordinator.inspect_job("graph", "broken")["state"])
        self.assertEqual("succeeded", coordinator.inspect_job("graph", "healthy")["state"])

    def test_waiting_probe_failures_back_off_and_persist_next_due_time(self):
        clock = [0.0]
        handler = FaultInjectingJob()
        handler.phases["broken"] = "waiting"
        coordinator = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            job_handlers={"graph": handler},
            clock=lambda: clock[0],
        )
        coordinator.schedule("graph", "broken")
        self.assertTrue(coordinator.serve_once())
        handler.probe_failures.add("broken")

        clock[0] = 1.1
        self.assertFalse(coordinator.serve_once())
        after_failure = coordinator.inspect_job("graph", "broken")
        calls_after_failure = len(handler.inspections)
        for _ in range(20):
            self.assertFalse(coordinator.serve_once())

        self.assertEqual(1, after_failure["probe_failures"])
        self.assertGreater(after_failure["next_probe_at"], clock[0])
        self.assertEqual(calls_after_failure, len(handler.inspections))

        clock[0] = after_failure["next_probe_at"]
        self.assertFalse(coordinator.serve_once())
        self.assertEqual(calls_after_failure + 1, len(handler.inspections))

    def test_failure_recording_error_does_not_escape_daemon_loop(self):
        handler = FaultInjectingJob()
        handler.execute_failures.add("broken")
        handler.record_failure_fails = True
        coordinator = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            job_handlers={"graph": handler},
            clock=lambda: 104.0,
        )
        coordinator.schedule("graph", "broken")

        self.assertTrue(coordinator.serve_once())

        item = coordinator.inspect_job("graph", "broken")
        self.assertEqual("failed", item["state"])
        self.assertIn("execution failed", item["error"])
        self.assertIn("failure journal unavailable", item["error"])

    def test_blocked_telemetry_does_not_hold_queue_lock(self):
        task_id = "task-0000000000000013"
        scheduled = self.coordinator.submit(task_id)
        telemetry_entered = threading.Event()
        release_telemetry = threading.Event()
        inspected = threading.Event()

        def blocking_emit(event, **fields):
            if event == "resident.job_settled":
                telemetry_entered.set()
                release_telemetry.wait(timeout=1)

        with patch("grapheng.resident.telemetry.emit", side_effect=blocking_emit):
            settling = threading.Thread(
                target=self.coordinator._settle,
                args=(scheduled["job_id"], "succeeded", None),
            )
            settling.start()
            self.assertTrue(telemetry_entered.wait(timeout=1))

            def inspect():
                self.coordinator.inspect(task_id)
                inspected.set()

            reading = threading.Thread(target=inspect)
            reading.start()
            self.assertTrue(inspected.wait(timeout=0.2))
            release_telemetry.set()
            settling.join(timeout=1)
            reading.join(timeout=1)

    def test_v1_task_queue_migrates_without_losing_control_state(self):
        task_id = "task-0000000000000007"
        self.coordinator.submit(task_id, priority=7)
        value = json.loads(self.coordinator.queue_path.read_text(encoding="utf-8"))
        item = value["items"].pop(f"engineering:{task_id}")
        item.pop("job_id")
        item.pop("kind")
        item.pop("reference")
        item["task_id"] = task_id
        value["schema_version"] = 1
        value["items"][task_id] = item
        self.coordinator.queue_path.write_text(json.dumps(value), encoding="utf-8")

        restored = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            clock=lambda: 105.0,
        )

        migrated = restored.inspect(task_id)
        self.assertEqual("engineering:task-0000000000000007", migrated["job_id"])
        self.assertEqual(7, migrated["priority"])
        self.assertEqual(0, migrated["probe_failures"])
        self.assertIsNone(migrated["next_probe_at"])

    def test_v2_queue_migrates_without_losing_attempts_or_requested_action(self):
        task_id = "task-0000000000000012"
        self.coordinator.submit(task_id, priority=7)
        self.coordinator.request(task_id, "pause")
        value = json.loads(self.coordinator.queue_path.read_text(encoding="utf-8"))
        item = value["items"][f"engineering:{task_id}"]
        attempts = item["attempts"]
        item.pop("probe_failures")
        item.pop("next_probe_at")
        value["schema_version"] = 2
        self.coordinator.queue_path.write_text(json.dumps(value), encoding="utf-8")

        restored = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            clock=lambda: 105.0,
        )
        migrated = restored.inspect(task_id)

        self.assertEqual(7, migrated["priority"])
        self.assertEqual(attempts, migrated["attempts"])
        self.assertEqual("pause", migrated["requested_action"])
        self.assertEqual(0, migrated["probe_failures"])
        self.assertIsNone(migrated["next_probe_at"])

    def test_graph_approval_resume_does_not_repeat_completed_node(self):
        clock = [106.0]
        workspace = self.root / "workspace"
        workspace.mkdir()
        calls = []
        graph = GraphSpec.from_dict(
            {
                "id": "resident-graph",
                "require_reality_anchor": False,
                "nodes": [
                    {
                        "id": "prepare",
                        "kind": "agent",
                        "writes": ["draft"],
                        "agent": {"prompt": "prepare"},
                    },
                    {
                        "id": "release",
                        "kind": "agent",
                        "deps": ["prepare"],
                        "reads": ["draft"],
                        "writes": ["released"],
                        "gate": "ship",
                        "agent": {"prompt": "release"},
                    },
                ],
            }
        )

        def registry_factory(value, root):
            registry = NodeRegistry()

            def execute(context):
                calls.append(context.node_id)
                if context.node_id == "prepare":
                    return {"draft": "ready"}
                return {"released": context.read("draft")}

            registry.register("agent", execute)
            return registry

        catalog = ResidentJobCatalog(
            self.root, graph_registry_factory=registry_factory
        )
        run_id = catalog.graph.prepare(graph, workspace)
        coordinator = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            job_handlers={"graph": catalog.graph},
            clock=lambda: clock[0],
        )
        coordinator.schedule("graph", run_id)

        self.assertTrue(coordinator.serve_once())
        self.assertEqual("waiting", coordinator.inspect_job("graph", run_id)["state"])
        catalog.graph.inbox.decide(run_id, "ship", "allow", "operator")
        clock[0] = 107.0
        self.assertTrue(coordinator.serve_once())

        self.assertEqual("succeeded", coordinator.inspect_job("graph", run_id)["state"])
        self.assertEqual(["prepare", "release"], calls)

    def test_orca_completion_is_reconciled_after_queue_settlement_crash(self):
        workspace = self.root / "orca-workspace"
        workspace.mkdir()
        graph = GraphSpec.from_dict(
            {
                "id": "resident-orca",
                "require_reality_anchor": False,
                "nodes": [
                    {
                        "id": "work",
                        "kind": "agent",
                        "writes": ["answer"],
                        "agent": {"executor": "codex", "prompt": "work"},
                    }
                ],
            }
        )
        backend = FakeOrcaBackend()
        backend.enqueue_done("work")

        def coordinator_factory(value, job_root, root):
            return OrcaCoordinator(value, backend, job_root, root)

        catalog = ResidentJobCatalog(
            self.root, orca_coordinator_factory=coordinator_factory
        )
        reference = catalog.orca.prepare(graph, workspace)
        first = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            job_handlers={"orca": catalog.orca},
            clock=lambda: 107.0,
        )
        first.schedule("orca", reference)
        claimed = first._claim_next()
        catalog.orca.execute(reference, lambda: None)

        restored = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            job_handlers={"orca": catalog.orca},
            clock=lambda: 108.0,
        )
        restored._recover_interrupted()
        self.assertTrue(restored.serve_once())

        self.assertEqual(reference, claimed["reference"])
        self.assertEqual("succeeded", restored.inspect_job("orca", reference)["state"])
        self.assertEqual(1, backend.materialize_calls)
        self.assertEqual(["dispatch-work-1"], backend.starts)
        self.assertEqual(["delivery-1"], backend.acks)
        self.assertEqual([("dispatch-work-1", "on_failure", True)], backend.finishes)

    def test_orca_cancel_from_recovered_pause_stops_dispatch(self):
        workspace = self.root / "orca-cancel-workspace"
        workspace.mkdir()
        graph = GraphSpec.from_dict(
            {
                "id": "resident-orca-cancel",
                "require_reality_anchor": False,
                "nodes": [
                    {
                        "id": "work",
                        "kind": "agent",
                        "writes": ["answer"],
                        "agent": {"executor": "codex", "prompt": "work"},
                    }
                ],
            }
        )
        backend = FakeOrcaBackend()

        def coordinator_factory(value, job_root, root):
            return OrcaCoordinator(value, backend, job_root, root)

        catalog = ResidentJobCatalog(
            self.root, orca_coordinator_factory=coordinator_factory
        )
        reference = catalog.orca.prepare(graph, workspace)
        first = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            job_handlers={"orca": catalog.orca},
            clock=lambda: 109.0,
        )
        first.schedule("orca", reference)
        first._claim_next()
        value, job_root, root = catalog.orca._definition(reference)
        coordinator_factory(value, job_root, root).start()
        first.request_job("orca", reference, "pause")

        restored = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            job_handlers={"orca": catalog.orca},
            clock=lambda: 110.0,
        )
        restored._recover_interrupted()
        self.assertEqual("paused", restored.inspect_job("orca", reference)["state"])
        requested = restored.request_job("orca", reference, "cancel")
        self.assertEqual("cancel_requested", requested["state"])
        self.assertTrue(restored.serve_once())
        self.assertFalse(restored.serve_once())

        self.assertEqual("cancelled", restored.inspect_job("orca", reference)["state"])
        self.assertEqual(["dispatch-work-1"], backend.stops)
        self.assertEqual([("dispatch-work-1", "on_failure", False)], backend.finishes)


if __name__ == "__main__":
    unittest.main()
