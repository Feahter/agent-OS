import tempfile
import unittest
from pathlib import Path

from grapheng import GraphSpec
from grapheng.artifacts import ArtifactStore
from grapheng.orca_publication import DEFERRED_EVENT, OrcaPublicationRecorder


def graph():
    return GraphSpec.from_dict(
        {
            "id": "publication-graph",
            "nodes": [
                {
                    "id": "plan",
                    "kind": "agent",
                    "deps": [],
                    "reads": [],
                    "writes": ["plan_out"],
                    "retry": {"max_attempts": 1},
                    "estimated_tokens": 0,
                    "agent": {"executor": "codex", "prompt": "run plan"},
                }
            ],
            "max_concurrency": 1,
        }
    )


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, state, event, node_id=None, attempt=None, payload=None):
        self.events.append((event, node_id, attempt, payload))


class ExplodingPublisher:
    def stage(self, *args, **kwargs):
        raise RuntimeError("cache is offline")

    def observe_verifier(self, *args, **kwargs):
        raise AssertionError("not reached")

    def reconcile(self, *args, **kwargs):
        raise RuntimeError("cache is offline")


class CountingPublisher:
    def __init__(self):
        self.staged = 0
        self.observed = 0
        self.reconciled = 0

    def stage(self, *args, **kwargs):
        self.staged += 1
        return None

    def observe_verifier(self, *args, **kwargs):
        self.observed += 1
        return None

    def reconcile(self, *args, **kwargs):
        self.reconciled += 1
        return ()


class OrcaPublicationRecorderTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.spec = graph()
        self.node = self.spec.nodes[0]
        self.state = {"run_id": "run-1"}
        self.emit = Recorder()

    def recorder(self, publisher):
        return OrcaPublicationRecorder(
            self.spec, publisher, lambda node, workspace_id: self.workspace, self.emit
        )

    def test_publication_is_skipped_when_no_cache_is_configured(self):
        recorder = self.recorder(None)

        recorder.record(self.state, self.node, 1, object(), (), (), ArtifactStore(), None)
        recorder.reconcile(self.state, ArtifactStore())

        self.assertFalse(recorder.enabled)
        self.assertEqual([], self.emit.events)

    def test_a_result_is_staged_and_then_observed_by_the_verifier(self):
        publisher = CountingPublisher()
        recorder = self.recorder(publisher)

        recorder.record(self.state, self.node, 1, object(), (), (), ArtifactStore(), None)

        self.assertEqual(1, publisher.staged)
        self.assertEqual(1, publisher.observed)
        self.assertEqual([], self.emit.events)

    def test_a_publication_failure_is_deferred_instead_of_failing_the_run(self):
        recorder = self.recorder(ExplodingPublisher())

        recorder.record(self.state, self.node, 2, object(), (), (), ArtifactStore(), None)

        self.assertEqual(1, len(self.emit.events))
        event, node_id, attempt, payload = self.emit.events[0]
        self.assertEqual(DEFERRED_EVENT, event)
        self.assertEqual("plan", node_id)
        self.assertEqual(2, attempt)
        self.assertIn("cache is offline", payload["reason"])

    def test_a_reconcile_failure_is_deferred_without_a_node(self):
        recorder = self.recorder(ExplodingPublisher())

        recorder.reconcile(self.state, ArtifactStore())

        event, node_id, attempt, payload = self.emit.events[0]
        self.assertEqual(DEFERRED_EVENT, event)
        self.assertIsNone(node_id)
        self.assertIsNone(attempt)
        self.assertIn("RuntimeError", payload["reason"])

    def test_reconcile_is_delegated_to_the_publisher(self):
        publisher = CountingPublisher()

        self.recorder(publisher).reconcile(self.state, ArtifactStore())

        self.assertEqual(1, publisher.reconciled)


if __name__ == "__main__":
    unittest.main()
