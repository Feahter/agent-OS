import json
import tempfile
import threading
import unittest
from pathlib import Path

from grapheng import (
    ContractViolation,
    EffectIndeterminateError,
    EffectJournal,
    GraphSpec,
    LocalControlPlane,
    ModelUsage,
    NodeOutcome,
    NodeRegistry,
)


def graph(nodes):
    return GraphSpec.from_dict(
        {
            "id": "control-test",
            "require_reality_anchor": False,
            "nodes": nodes,
        }
    )


class ControlPlaneTests(unittest.TestCase):
    def test_prepared_run_can_be_started_by_a_new_control_plane(self):
        spec = graph([{"id": "work", "kind": "work", "writes": ["answer"]}])
        registry = NodeRegistry()
        registry.register("work", lambda context: {"answer": 42})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creator = LocalControlPlane(root, owner_id="creator")
            run_id = creator.prepare(spec)
            creator.close()
            prepared = LocalControlPlane(root, owner_id="resident")
            self.assertEqual("queued", prepared.inspect(run_id).phase)
            prepared.start(run_id, registry)
            snapshot = prepared.wait(run_id, timeout=2)
            prepared.close()

        self.assertEqual("succeeded", snapshot.phase)
        self.assertEqual(42, snapshot.result["artifacts"]["answer"])

    def test_submit_wait_inspect_and_cursor_events(self):
        spec = graph([{"id": "work", "kind": "work", "writes": ["answer"]}])
        registry = NodeRegistry()
        registry.register("work", lambda context: {"answer": 42})

        with tempfile.TemporaryDirectory() as directory:
            plane = LocalControlPlane(Path(directory), owner_id="controller-test")
            run_id = plane.submit(spec, registry)
            snapshot = plane.wait(run_id, timeout=2)
            first_page = plane.events(run_id)
            second_page = plane.events(run_id, after=first_page.next_cursor)
            plane.close()

        self.assertEqual("succeeded", snapshot.phase)
        self.assertEqual(42, snapshot.result["artifacts"]["answer"])
        self.assertGreater(first_page.next_cursor, 0)
        self.assertEqual((), second_page.events)

    def test_result_checkpoint_and_events_preserve_identical_usage(self):
        spec = graph([{"id": "work", "kind": "work", "writes": ["answer"]}])
        usage = ModelUsage(
            input_tokens=100,
            cached_input_tokens=25,
            output_tokens=10,
            total_tokens=110,
            input_tokens_complete=True,
            cached_input_tokens_complete=True,
            output_tokens_complete=True,
            total_tokens_complete=True,
        )
        registry = NodeRegistry()
        registry.register(
            "work",
            lambda context: NodeOutcome(
                {"answer": 42}, tokens_used=110, usage=usage
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plane = LocalControlPlane(root, owner_id="controller-test")
            run_id = plane.submit(spec, registry)
            snapshot = plane.wait(run_id, timeout=2)
            events = plane.events(run_id).events
            checkpoint = json.loads(
                (
                    root
                    / "runs"
                    / run_id
                    / "runtime"
                    / "checkpoint.json"
                ).read_text(encoding="utf-8")
            )
            plane.close()

        completed = next(
            item for item in events if item["event"] == "node_completed"
        )
        expected = usage.with_accounted_totals(110, 0.0).to_dict()
        self.assertEqual(expected, snapshot.result["usage"])
        self.assertEqual(expected, checkpoint["usage"])
        self.assertEqual(expected, completed["payload"]["usage"])

    def test_cancel_stops_scheduling_new_nodes(self):
        spec = graph(
            [
                {"id": "first", "kind": "first", "writes": ["one"]},
                {
                    "id": "second",
                    "kind": "second",
                    "deps": ["first"],
                    "reads": ["one"],
                    "writes": ["two"],
                },
            ]
        )
        entered = threading.Event()
        release = threading.Event()
        registry = NodeRegistry()

        def first(context):
            entered.set()
            release.wait(timeout=2)
            return {"one": 1}

        registry.register("first", first)
        registry.register("second", lambda context: {"two": 2})

        with tempfile.TemporaryDirectory() as directory:
            plane = LocalControlPlane(Path(directory), owner_id="controller-test")
            run_id = plane.submit(spec, registry)
            self.assertTrue(entered.wait(timeout=1))
            plane.cancel(run_id)
            release.set()
            snapshot = plane.wait(run_id, timeout=2)
            plane.close()

        self.assertEqual("cancelled", snapshot.phase)
        self.assertEqual("cancelled", snapshot.result["statuses"]["second"])

    def test_failed_run_can_resume_with_new_controller(self):
        spec = graph([{"id": "work", "kind": "work", "writes": ["answer"]}])
        failed_registry = NodeRegistry()
        failed_registry.register("work", lambda context: (_ for _ in ()).throw(ValueError("boom")))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = LocalControlPlane(root, owner_id="controller-one")
            run_id = first.submit(spec, failed_registry)
            failed = first.wait(run_id, timeout=2)
            first.close()

            recovered_registry = NodeRegistry()
            recovered_registry.register("work", lambda context: {"answer": "recovered"})
            second = LocalControlPlane(root, owner_id="controller-two")
            second.resume(run_id, recovered_registry)
            recovered = second.wait(run_id, timeout=2)
            second.close()

        self.assertEqual("failed", failed.phase)
        self.assertEqual("succeeded", recovered.phase)
        self.assertEqual(2, recovered.generation)
        self.assertEqual("recovered", recovered.result["artifacts"]["answer"])

    def test_effect_journal_returns_completed_receipt_without_repeating_effect(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            journal = EffectJournal(Path(directory))

            def effect():
                calls.append("called")
                return {"receipt": "ok"}

            first = journal.execute("charge-1", {"amount": 5}, effect)
            second = journal.execute("charge-1", {"amount": 5}, effect)

            with self.assertRaisesRegex(ContractViolation, "different input"):
                journal.execute("charge-1", {"amount": 6}, effect)

        self.assertEqual(first, second)
        self.assertEqual(["called"], calls)

    def test_effect_failure_becomes_indeterminate(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = EffectJournal(Path(directory))

            def fail():
                raise RuntimeError("unknown external state")

            with self.assertRaises(RuntimeError):
                journal.execute("publish-1", {"version": 1}, fail)
            with self.assertRaisesRegex(EffectIndeterminateError, "reconcile"):
                journal.execute("publish-1", {"version": 1}, fail)


if __name__ == "__main__":
    unittest.main()
