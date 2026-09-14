import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from grapheng import (
    ContractViolation,
    DesktopNotificationSink,
    ResidentCoordinator,
)
from grapheng.cli import _print_task_center, main


class DiscoverableTasks:
    def __init__(self, root):
        self.tasks_root = root / "tasks"
        self.tasks_root.mkdir(parents=True)
        self.phases = {}

    def add(self, task_id, phase, summary, usage=None):
        (self.tasks_root / task_id).mkdir()
        self.phases[task_id] = {
            "phase": phase,
            "summary": summary,
            "next_action": "approve" if phase == "awaiting_approval" else "status",
            "approval_required": phase == "awaiting_approval",
            "usage": usage or {"tokens_used": 0, "cost_usd": 0.0},
        }

    def status(self, task_id):
        return dict(self.phases[task_id])

    def execution_phase(self, task_id):
        return self.phases[task_id]["phase"]

    def execute_queued(self, task_id, control_probe):
        self.phases[task_id]["phase"] = "succeeded"
        return {"phase": "succeeded"}

    def record_queue_failure(self, task_id, failure):
        self.phases[task_id]["phase"] = "failed"


class CenterJob:
    def __init__(self):
        self.phases = {}
        self.outcomes = {}
        self.details = {}

    def inspect(self, reference):
        return self.phases.get(reference, "queued")

    def execute(self, reference, control_probe):
        outcome = self.outcomes.get(reference, "succeeded")
        if outcome == "raise":
            raise RuntimeError("worker failed")
        self.phases[reference] = outcome
        return {"phase": outcome}

    def record_failure(self, reference, failure):
        self.phases[reference] = "failed"

    def describe(self, reference):
        value = dict(self.details.get(reference, {}))
        value.setdefault("phase", self.inspect(reference))
        return value


class RecordingSink:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []

    def send(self, title, body):
        self.messages.append((title, body))
        if self.fail:
            raise RuntimeError("desktop unavailable")


class TaskCenterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tasks = DiscoverableTasks(self.root)
        self.graph = CenterJob()
        self.orca = CenterJob()

    def tearDown(self):
        self.temporary.cleanup()

    def coordinator(self, sink=None, clock=lambda: 100.0):
        return ResidentCoordinator(
            self.root,
            task_module_factory=lambda: self.tasks,
            job_handlers={"graph": self.graph, "orca": self.orca},
            notification_sink=sink,
            clock=clock,
        )

    def test_unified_summary_projects_fact_owners_and_attention_first(self):
        task_id = "task-0000000000000001"
        self.tasks.add(
            task_id,
            "awaiting_approval",
            "Engineering plan needs approval",
            {"tokens_used": 12, "cost_usd": 0.03},
        )
        self.graph.details["run-1"] = {
            "summary": "Graph needs approval for release",
            "next_action": "approval",
            "approval_required": True,
        }
        self.orca.details["orca-1"] = {
            "summary": "Orca job completed successfully",
            "usage": {"tokens_used": 34, "cost_usd": 0.07},
        }
        coordinator = self.coordinator()
        graph_item = coordinator.schedule("graph", "run-1", priority=9)
        orca_item = coordinator.schedule("orca", "orca-1", priority=3)
        coordinator._settle(graph_item["job_id"], "waiting", None)
        coordinator._settle(orca_item["job_id"], "succeeded", None)

        value = coordinator.task_center()

        self.assertEqual(
            {
                "total": 3,
                "active": 2,
                "needs_attention": 2,
                "succeeded": 1,
                "failed": 0,
                "cancelled": 0,
            },
            value["counts"],
        )
        self.assertEqual(46, value["usage"]["tokens_used"])
        self.assertEqual(0.1, value["usage"]["cost_usd"])
        self.assertEqual(2, value["usage"]["jobs_reported"])
        self.assertFalse(value["usage"]["complete"])
        self.assertFalse(value["usage"]["total_tokens_complete"])
        self.assertFalse(value["usage"]["cost_complete"])
        self.assertEqual(
            {"engineering", "graph"},
            {item["kind"] for item in value["jobs"][:2]},
        )
        engineering = next(
            item for item in value["jobs"] if item["kind"] == "engineering"
        )
        orca = next(item for item in value["jobs"] if item["kind"] == "orca")
        self.assertFalse(engineering["scheduled"])
        self.assertEqual("awaiting_approval", engineering["state"])
        self.assertEqual(12, engineering["usage"]["tokens_used"])
        self.assertEqual(34, orca["usage"]["tokens_used"])

    def test_component_usage_sums_known_subtotals_and_marks_partial_fields(self):
        self.graph.details["one"] = {
            "usage": {
                "tokens_used": 3,
                "cost_usd": 0.0,
                "input_tokens": 2,
                "cached_input_tokens": 0,
                "output_tokens": 1,
                "total_tokens": 3,
                "input_tokens_complete": True,
                "cached_input_tokens_complete": True,
                "output_tokens_complete": True,
                "total_tokens_complete": True,
                "cost_complete": True,
            }
        }
        self.orca.details["two"] = {
            "usage": {
                "tokens_used": 2,
                "cost_usd": 0.0,
                "input_tokens": 2,
                "cached_input_tokens": None,
                "output_tokens": None,
                "total_tokens": 2,
                "input_tokens_complete": True,
                "cached_input_tokens_complete": False,
                "output_tokens_complete": False,
                "total_tokens_complete": True,
                "cost_complete": False,
            }
        }
        coordinator = self.coordinator()
        coordinator.schedule("graph", "one")
        coordinator.schedule("orca", "two")

        usage = coordinator.task_center()["usage"]

        self.assertEqual(4, usage["input_tokens"])
        self.assertTrue(usage["input_tokens_complete"])
        self.assertEqual(1, usage["output_tokens"])
        self.assertFalse(usage["output_tokens_complete"])
        self.assertEqual(5, usage["total_tokens"])
        self.assertTrue(usage["total_tokens_complete"])
        self.assertEqual(0.0, usage["cost_usd"])
        self.assertFalse(usage["cost_complete"])
        self.assertFalse(usage["complete"])

    def test_limit_only_bounds_details_and_invalid_limits_fail_closed(self):
        for index in range(3):
            task_id = f"task-{index + 1:016x}"
            self.tasks.add(task_id, "awaiting_approval", "Plan needs approval")
        coordinator = self.coordinator()

        value = coordinator.task_center(limit=1)

        self.assertEqual(3, value["counts"]["total"])
        self.assertEqual(1, len(value["jobs"]))
        for invalid in (True, 0, 201):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ContractViolation, "between 1 and 200"):
                    coordinator.task_center(invalid)

    def test_queue_control_state_overrides_stale_cached_summary_and_next_action(self):
        self.graph.details["run-1"] = {
            "phase": "queued",
            "summary": "Graph is ready to run",
            "next_action": "status",
        }
        coordinator = self.coordinator()
        coordinator.schedule("graph", "run-1")
        coordinator.task_center()

        coordinator.request_job("graph", "run-1", "pause")
        value = coordinator.task_center()
        job = next(item for item in value["jobs"] if item["reference"] == "run-1")

        self.assertEqual("paused", job["state"])
        self.assertEqual("Graph is paused", job["summary"])
        self.assertEqual("resume", job["next_action"])

    def test_notifications_cover_attention_and_terminal_states(self):
        sink = RecordingSink()
        coordinator = self.coordinator(sink)
        self.graph.phases["waiting-job"] = "waiting"
        self.graph.outcomes.update(
            {
                "paused-job": "paused",
                "success-job": "succeeded",
                "failed-job": "failed",
            }
        )
        for reference in (
            "waiting-job",
            "paused-job",
            "success-job",
            "failed-job",
        ):
            coordinator.schedule("graph", reference)

        for _ in range(4):
            self.assertTrue(coordinator.serve_once())

        self.assertEqual(
            {
                "Agent OS needs your input",
                "Agent OS task paused",
                "Agent OS task completed",
                "Agent OS task failed",
            },
            {title for title, _ in sink.messages},
        )

    def test_notification_is_deduplicated_after_restart_but_new_sequence_sends(self):
        sink = RecordingSink()
        coordinator = self.coordinator(sink)
        coordinator.schedule("graph", "run-1")
        self.assertTrue(coordinator.serve_once())

        restored = self.coordinator(sink)
        restored._settle("graph:run-1", "succeeded", None)
        restored.schedule("graph", "run-1")
        self.assertTrue(restored.serve_once())

        self.assertEqual(2, len(sink.messages))
        journal = json.loads(
            (self.root / "runtime" / "resident" / "notifications.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(2, len(journal["entries"]))

    def test_notification_failure_never_changes_task_terminal_state(self):
        sink = RecordingSink(fail=True)
        coordinator = self.coordinator(sink)
        coordinator.schedule("graph", "run-1")

        self.assertTrue(coordinator.serve_once())

        self.assertEqual("succeeded", coordinator.inspect_job("graph", "run-1")["state"])
        statuses = coordinator._notifications.status()
        self.assertEqual(1, statuses["failed"])

    def test_desktop_adapter_uses_argument_argv_and_bounds_content(self):
        runner = Mock()
        sink = DesktopNotificationSink("linux", "/usr/bin/notify-send", runner)

        sink.send(" title\nwith space ", "body " * 100)

        command = runner.call_args.args[0]
        self.assertEqual("/usr/bin/notify-send", command[0])
        self.assertEqual("title with space", command[1])
        self.assertLessEqual(len(command[2]), 240)
        runner.assert_called_once_with(
            command,
            check=True,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_task_center_cost_rendering_requires_explicit_cost_completeness(self):
        base = {
            "resident_running": True,
            "desktop_notifications": True,
            "counts": {"active": 0, "needs_attention": 0},
            "jobs": [],
        }
        cases = (
            (
                {"tokens_used": 0, "cost_usd": 0.0, "complete": True},
                "cost unknown",
                "$0.0000",
            ),
            (
                {"tokens_used": 2, "cost_usd": 0.04, "complete": True},
                "$0.0400 (partial)",
                None,
            ),
            (
                {
                    "tokens_used": 0,
                    "cost_usd": 0.0,
                    "complete": True,
                    "cost_complete": True,
                },
                "$0.0000",
                "cost unknown",
            ),
        )

        for usage, expected, excluded in cases:
            with self.subTest(usage=usage):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    _print_task_center({**base, "usage": usage}, False)
                self.assertIn(expected, output.getvalue())
                if excluded is not None:
                    self.assertNotIn(excluded, output.getvalue())

    def test_cli_center_has_human_and_json_outputs(self):
        value = {
            "schema_version": 1,
            "generated_at": 100.0,
            "resident_running": True,
            "desktop_notifications": True,
            "counts": {
                "total": 1,
                "active": 1,
                "needs_attention": 1,
                "succeeded": 0,
                "failed": 0,
                "cancelled": 0,
            },
            "usage": {
                "tokens_used": 25,
                "cost_usd": 0.04,
                "jobs_reported": 1,
                "complete": True,
            },
            "jobs": [
                {
                    "job_id": "graph:run-1",
                    "kind": "graph",
                    "reference": "run-1",
                    "state": "waiting",
                    "summary": "Graph needs approval for release",
                    "next_action": "approval",
                    "attention_required": True,
                    "scheduled": True,
                    "priority": 10,
                    "attempts": 1,
                    "updated_at": 100.0,
                    "usage": None,
                }
            ],
        }
        coordinator = Mock()
        coordinator.task_center.return_value = value
        human = io.StringIO()
        with patch(
            "grapheng.cli.ResidentCoordinator", return_value=coordinator
        ), patch.object(
            sys, "argv", ["agent-os", "center", "--limit", "7"]
        ), contextlib.redirect_stdout(human):
            self.assertEqual(0, main())

        machine = io.StringIO()
        with patch(
            "grapheng.cli.ResidentCoordinator", return_value=coordinator
        ), patch.object(
            sys, "argv", ["agent-os", "center", "--json"]
        ), contextlib.redirect_stdout(machine):
            self.assertEqual(0, main())

        self.assertIn("1 active", human.getvalue())
        self.assertIn("25 tokens", human.getvalue())
        self.assertIn("$0.0400 (partial) · complete", human.getvalue())
        self.assertIn("Graph needs approval for release", human.getvalue())
        self.assertEqual(value, json.loads(machine.getvalue()))
        self.assertEqual(2, coordinator.ensure_running.call_count)
        self.assertEqual(
            [call(7), call(20)], coordinator.task_center.call_args_list
        )


if __name__ == "__main__":
    unittest.main()
