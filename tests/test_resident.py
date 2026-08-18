import json
import tempfile
import unittest
from pathlib import Path

from grapheng import ContractViolation, ResidentCoordinator


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
        self.assertEqual(task_id, self.coordinator._claim_next())

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

    def test_invalid_priority_fails_closed(self):
        with self.assertRaisesRegex(ContractViolation, "between -100 and 100"):
            self.coordinator.submit("task-0000000000000004", priority=101)

    def test_recovery_reconciles_completed_task_before_reexecution(self):
        task_id = "task-0000000000000005"
        self.coordinator.submit(task_id)
        self.assertEqual(task_id, self.coordinator._claim_next())
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
        value["items"][task_id]["priority"] = 1000
        self.coordinator.queue_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(ContractViolation, "fields are invalid"):
            self.coordinator.inspect(task_id)


if __name__ == "__main__":
    unittest.main()
