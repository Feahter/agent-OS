import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grapheng import ContractViolation, telemetry
from grapheng.cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TelemetryTests(unittest.TestCase):
    def test_events_are_appended_as_one_json_object_per_line(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = telemetry.EventJournal(home=Path(directory), clock=lambda: 1000.0)
            journal.emit("resident.job_claimed", job_id="engineering:task-1", attempts=1)
            journal.emit("resident.job_settled", job_id="engineering:task-1", state="succeeded")

            events = journal.read()

        self.assertEqual(2, len(events))
        self.assertEqual("resident.job_claimed", events[0]["kind"])
        self.assertEqual("succeeded", events[1]["state"])
        self.assertEqual(telemetry.TELEMETRY_SCHEMA_VERSION, events[0]["schema_version"])

    def test_absent_fields_are_omitted_and_long_structured_fields_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = telemetry.EventJournal(home=Path(directory), clock=lambda: 1.0)
            journal.emit("graph.node_failed", node_id=None, scope="x" * 5000)

            event = journal.read()[0]

        self.assertNotIn("node_id", event)
        self.assertLess(len(event["scope"]), 600)
        self.assertIn("...", event["scope"])

    def test_free_form_failure_text_is_fingerprinted_not_persisted_or_logged(self):
        secret = "prompt text and provider response must stay private"
        with tempfile.TemporaryDirectory() as directory:
            journal = telemetry.EventJournal(home=Path(directory), clock=lambda: 1.0)
            with self.assertLogs("grapheng", level=logging.WARNING) as captured:
                journal.emit(
                    "agent.failure_classified",
                    level=logging.WARNING,
                    detail=secret,
                    reason=secret,
                    error=secret,
                    failure_kind="execution",
                )
            event = journal.read()[0]
            raw = journal.path_for(1.0).read_text(encoding="utf-8")

        for field in ("detail", "reason", "error"):
            self.assertNotIn(field, event)
            self.assertEqual(len(secret), event[f"{field}_chars"])
            self.assertEqual(64, len(event[f"{field}_sha256"]))
        self.assertNotIn(secret, raw)
        self.assertNotIn(secret, "\n".join(captured.output))

    def test_a_failing_journal_write_never_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "blocked"
            blocked.write_text("not a directory", encoding="utf-8")
            journal = telemetry.EventJournal(home=blocked, clock=lambda: 1.0)

            with self.assertLogs("grapheng", level=logging.DEBUG):
                journal.emit("graph.node_started", node_id="explore")

    def test_journal_can_be_disabled_without_losing_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = telemetry.EventJournal(
                home=Path(directory), clock=lambda: 1.0, enabled=False
            )

            with self.assertLogs("grapheng", level=logging.INFO) as captured:
                journal.emit("graph.node_started", node_id="explore")

            self.assertEqual((), journal.read())
        self.assertIn("graph.node_started", captured.output[0])

    def test_snapshot_counts_events_by_kind(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = telemetry.EventJournal(home=Path(directory))
            journal.emit("agent.call_started", executor_id="codex")
            journal.emit("agent.call_started", executor_id="codex")
            journal.emit("agent.call_finished", executor_id="codex", returncode=0)
            telemetry.configure(home=Path(directory))

            summary = telemetry.snapshot()

        self.assertEqual(3, summary["total"])
        self.assertEqual({"agent.call_finished": 1, "agent.call_started": 2}, summary["counts"])

    def test_demo_keeps_telemetry_in_its_work_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            work = root / "run"
            with patch.dict(os.environ, {"HOME": str(home)}):
                result = main(
                    [
                        "demo",
                        str(PROJECT_ROOT / "examples" / "minimal_graph.json"),
                        "--work-dir",
                        str(work),
                    ]
                )

            self.assertEqual(0, result)
            self.assertFalse((home / ".agent-os").exists())
            self.assertTrue(any((work / "runtime" / "logs").glob("events-*.jsonl")))

    def test_reserved_field_names_are_rejected_rather_than_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = telemetry.EventJournal(home=Path(directory), clock=lambda: 1.0)

            with self.assertRaises(ContractViolation):
                journal.emit("resident.job_scheduled", kind="engineering")

            journal.emit("resident.job_scheduled", job_kind="engineering")
            event = journal.read()[0]

        self.assertEqual("resident.job_scheduled", event["kind"])
        self.assertEqual("engineering", event["job_kind"])


if __name__ == "__main__":
    unittest.main()
