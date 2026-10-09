import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from grapheng import GraphRuntime, GraphSpec, ModelUsage, NodeOutcome, NodeRegistry, NodeStatus
from grapheng.checkpoint import CheckpointStore
from grapheng.effects import NodeEffectJournal
from tests.test_runtime import spec


class AccountingTests(unittest.TestCase):
    def test_completed_effect_receipt_settles_once_without_replay(self):
        class Crash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = spec([{"id": "work", "kind": "work", "writes": ["value"],
                           "effect": "verified_idempotent", "estimated_tokens": 10,
                           "estimated_cost_usd": 0.5}], max_cost_usd=0.75)
            calls = []

            def handler(context):
                calls.append(context.attempt)
                return NodeOutcome({"value": 1}, tokens_used=10, cost_usd=0.4)

            handler.workspace_identity = {"path": str(root)}
            registry = NodeRegistry()
            registry.register("work", handler)
            lease = {"run_id": "run", "owner_id": "host", "generation": 1, "token_digest": "a" * 64}
            runtime = GraphRuntime(graph, registry, root, effect_lease=lease)

            def crash(stage, _record):
                if stage == "completed":
                    raise Crash()

            runtime.effects = NodeEffectJournal(root / "effects", fault_injector=crash)
            with self.assertRaises(Crash):
                runtime.run(run_id="run")
            resumed = GraphRuntime(graph, registry, root, effect_lease=lease).run(resume=True)
            self.assertTrue(resumed.success)
            self.assertEqual([1], calls)
            self.assertEqual(10, resumed.tokens_used)
            self.assertAlmostEqual(0.4, resumed.cost_usd)
            self.assertTrue(resumed.usage.cost_complete)
            repeated = GraphRuntime(graph, registry, root, effect_lease=lease).run(resume=True)
            self.assertAlmostEqual(0.4, repeated.cost_usd)
            self.assertEqual([1], calls)

    def test_write_ahead_failure_prevents_handler_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            registry = NodeRegistry()
            registry.register("work", lambda context: calls.append(context.attempt) or {})
            runtime = GraphRuntime(
                spec([{"id": "work", "kind": "work"}]), registry, Path(directory)
            )
            save = runtime.checkpoints.save

            def fail_on_launch(checkpoint):
                if checkpoint.in_flight:
                    raise OSError("disk failed")
                save(checkpoint)

            with patch.object(runtime.checkpoints, "save", side_effect=fail_on_launch):
                with self.assertRaisesRegex(OSError, "disk failed"):
                    runtime.run()
            self.assertEqual([], calls)

    def test_killed_process_preserves_reservation_and_unknown_usage(self):
        for maximum in (0.75, None):
            with self.subTest(maximum=maximum), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                document = {
                    "id": "runtime-test", "require_reality_anchor": False,
                    "max_concurrency": 2, "max_cost_usd": maximum,
                    "nodes": [{"id": "work", "kind": "work", "writes": ["value"],
                               "estimated_tokens": 10, "estimated_cost_usd": 0.5}],
                }
                graph = GraphSpec.from_dict(document)
                child_code = """
import json, sys, time
from pathlib import Path
from grapheng import GraphRuntime, GraphSpec, NodeRegistry
root = Path(sys.argv[1])
registry = NodeRegistry()
def handler(context):
    (root / 'started').write_text(str(context.attempt))
    time.sleep(30)
    return {'value': 1}
registry.register('work', handler)
GraphRuntime(GraphSpec.from_dict(json.loads(sys.argv[2])), registry, root).run(run_id='run')
"""
                child = subprocess.Popen(
                    [sys.executable, "-c", child_code, str(root), json.dumps(document)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                try:
                    deadline = time.monotonic() + 10
                    while not (root / "started").exists():
                        if child.poll() is not None or time.monotonic() > deadline:
                            self.fail("child did not launch the handler")
                        time.sleep(0.01)
                    saved = json.loads((root / "checkpoint.json").read_text())
                    self.assertEqual(10, saved["in_flight"]["work"]["tokens"])
                    child.kill()
                    child.communicate(timeout=5)
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)
                calls = []
                registry = NodeRegistry()

                def complete(context, calls=calls):
                    calls.append(context.attempt)
                    return NodeOutcome({"value": 1}, tokens_used=10, cost_usd=0.4)

                registry.register("work", complete)
                runtime = GraphRuntime(graph, registry, root)
                result = runtime.run(resume=True, run_id="run")
                self.assertFalse(result.usage.cost_complete)
                self.assertFalse(result.usage.total_tokens_complete)
                if maximum is None:
                    self.assertTrue(result.success)
                    self.assertEqual([2], calls)
                    self.assertEqual(20, result.tokens_used)
                    self.assertAlmostEqual(0.9, result.cost_usd)
                else:
                    self.assertEqual(NodeStatus.BLOCKED, result.statuses["work"])
                    self.assertEqual([], calls)
                    self.assertAlmostEqual(0.5, result.cost_usd)
                repeated = GraphRuntime(graph, registry, root).run(resume=True, run_id="run")
                self.assertEqual(result.tokens_used, repeated.tokens_used)
                self.assertEqual(result.cost_usd, repeated.cost_usd)

    def test_old_running_checkpoint_is_conservatively_restored_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph = spec([{"id": "work", "kind": "work", "estimated_tokens": 7,
                           "estimated_cost_usd": 0.3}], max_tokens=7)
            calls = []
            registry = NodeRegistry()
            registry.register("work", lambda context: calls.append(context.attempt) or {})
            document = {
                "schema_version": 1, "graph_id": graph.id,
                "graph_fingerprint": graph.fingerprint(), "run_id": "old-run",
                "statuses": {"work": "running"}, "attempts": {"work": 1},
                "tokens_used": 0, "cost_usd": 0, "artifacts": [],
                "usage": ModelUsage.no_call().to_dict(),
            }
            (root / "checkpoint.json").write_text(json.dumps(document))
            store = CheckpointStore(root / "checkpoint.json")
            loaded = store.load()
            self.assertIsNone(loaded.in_flight)
            store.save(loaded)
            result = GraphRuntime(graph, registry, root).run(resume=True)
            self.assertEqual(7, result.tokens_used)
            self.assertAlmostEqual(0.3, result.cost_usd)
            self.assertFalse(result.usage.total_tokens_complete)
            self.assertEqual([], calls)
            self.assertEqual(NodeStatus.BLOCKED, result.statuses["work"])
            again = GraphRuntime(graph, registry, root).run(resume=True)
            self.assertEqual(7, again.tokens_used)
