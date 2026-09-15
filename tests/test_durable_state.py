import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grapheng._store import append_jsonl, atomic_json_write
from grapheng.checkpoint import Checkpoint, CheckpointStore
from grapheng.errors import ContractViolation
from grapheng.events import GraphEvent, JsonlEventSink
from grapheng.model import GraphSpec
from grapheng.runtime import GraphRuntime, NodeRegistry


class DurableStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def checkpoint(run_id="run-1"):
        return Checkpoint(
            graph_id="graph",
            graph_fingerprint="fingerprint",
            run_id=run_id,
            statuses={},
            attempts={},
            tokens_used=0,
            cost_usd=0.0,
            artifacts=(),
        )

    @staticmethod
    def event(name="run_started"):
        return GraphEvent.create(name, "run-1", "graph")

    def test_checkpoint_store_uses_canonical_atomic_json_write(self):
        path = self.root / "checkpoint.json"
        checkpoint = self.checkpoint()

        with patch("grapheng.checkpoint.atomic_json_write") as write:
            CheckpointStore(path).save(checkpoint)

        write.assert_called_once_with(
            path, checkpoint.to_dict(), label="checkpoint"
        )

    def test_event_sink_uses_canonical_durable_append(self):
        path = self.root / "events.jsonl"
        event = self.event()

        with patch("grapheng.events.append_jsonl") as append:
            JsonlEventSink(path).emit(event)

        append.assert_called_once_with(path, event.to_dict(), label="graph event")

    def test_atomic_replace_failure_preserves_old_complete_json(self):
        path = self.root / "checkpoint.json"
        atomic_json_write(path, {"version": 1})

        with patch("grapheng._store.os.replace", side_effect=OSError("replace")):
            with self.assertRaisesRegex(OSError, "replace"):
                atomic_json_write(path, {"version": 2})

        self.assertEqual({"version": 1}, json.loads(path.read_text()))
        self.assertEqual([], list(self.root.glob(".checkpoint.json.*")))

    def test_atomic_file_fsync_failure_preserves_old_complete_json(self):
        path = self.root / "checkpoint.json"
        atomic_json_write(path, {"version": 1})

        with patch("grapheng._store.os.fsync", side_effect=OSError("file fsync")):
            with self.assertRaisesRegex(OSError, "file fsync"):
                atomic_json_write(path, {"version": 2})

        self.assertEqual({"version": 1}, json.loads(path.read_text()))

    def test_atomic_directory_fsync_failure_is_not_reported_as_durable(self):
        path = self.root / "checkpoint.json"

        with patch(
            "grapheng._store.os.fsync",
            side_effect=[None, OSError("directory fsync")],
        ):
            with self.assertRaisesRegex(OSError, "directory fsync"):
                atomic_json_write(path, {"version": 1})

        self.assertEqual({"version": 1}, json.loads(path.read_text()))

    def test_first_event_append_fsyncs_new_directory_entry(self):
        path = self.root / "events.jsonl"

        with patch("grapheng._store._fsync_directory") as fsync_directory:
            append_jsonl(path, {"event": "started"})

        fsync_directory.assert_called_once_with(path.parent)

    def test_event_append_propagates_file_fsync_failure(self):
        path = self.root / "events.jsonl"

        with patch("grapheng._store.os.fsync", side_effect=OSError("event fsync")):
            with self.assertRaisesRegex(OSError, "event fsync"):
                append_jsonl(path, {"event": "started"})

    def test_event_reader_ignores_uncommitted_tail_fragment(self):
        path = self.root / "events.jsonl"
        committed = self.event().to_dict()
        path.write_text(json.dumps(committed) + "\n{\"event\":", encoding="utf-8")

        self.assertEqual((committed,), tuple(JsonlEventSink(path).read()))

    def test_event_reader_rejects_malformed_committed_line(self):
        path = self.root / "events.jsonl"
        path.write_text("{not-json}\n", encoding="utf-8")

        with self.assertRaisesRegex(ContractViolation, "invalid graph event log"):
            tuple(JsonlEventSink(path).read())

    def test_runtime_commits_event_before_corresponding_checkpoint(self):
        graph = GraphSpec.from_dict(
            {
                "id": "durability-order",
                "require_reality_anchor": False,
                "nodes": [{"id": "work", "kind": "work", "writes": ["value"]}],
            }
        )
        registry = NodeRegistry()
        registry.register("work", lambda context: {"value": 1})
        order = []

        with patch(
            "grapheng.events.append_jsonl", side_effect=lambda *args, **kwargs: order.append("event")
        ), patch(
            "grapheng.checkpoint.atomic_json_write",
            side_effect=lambda *args, **kwargs: order.append("checkpoint"),
        ):
            GraphRuntime(graph, registry, work_dir=self.root).run()

        self.assertEqual(
            [
                "event",
                "event",
                "checkpoint",
                "event",
                "checkpoint",
                "event",
                "checkpoint",
            ],
            order,
        )

    def test_checkpoint_remains_authoritative_when_event_log_is_missing(self):
        graph = GraphSpec.from_dict(
            {
                "id": "missing-events",
                "require_reality_anchor": False,
                "nodes": [{"id": "work", "kind": "work", "writes": ["value"]}],
            }
        )
        registry = NodeRegistry()
        calls = []

        def execute(context):
            calls.append(context.node_id)
            return {"value": 1}

        registry.register("work", execute)
        GraphRuntime(graph, registry, work_dir=self.root).run()
        (self.root / "events.jsonl").unlink()

        result = GraphRuntime(graph, registry, work_dir=self.root).run(resume=True)

        self.assertTrue(result.success)
        self.assertEqual(["work"], calls)
        events = tuple(JsonlEventSink(self.root / "events.jsonl").read())
        self.assertEqual("run_resumed", events[0]["event"])


if __name__ == "__main__":
    unittest.main()
