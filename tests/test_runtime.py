import tempfile
import threading
import unittest
from pathlib import Path

from grapheng import (
    AllowListGatePolicy,
    ContractViolation,
    GraphRuntime,
    GraphSpec,
    NodeOutcome,
    NodeRegistry,
    NodeStatus,
    RetryableNodeError,
)


def spec(nodes, **overrides):
    value = {
        "id": "runtime-test",
        "require_reality_anchor": False,
        "max_concurrency": 2,
        "nodes": nodes,
    }
    value.update(overrides)
    return GraphSpec.from_dict(value)


class RuntimeTests(unittest.TestCase):
    def test_executes_dependencies_and_commits_artifacts(self):
        graph = spec(
            [
                {"id": "seed", "kind": "seed", "writes": ["value"]},
                {
                    "id": "double",
                    "kind": "double",
                    "deps": ["seed"],
                    "reads": ["value"],
                    "writes": ["result"],
                },
            ]
        )
        registry = NodeRegistry()
        registry.register("seed", lambda context: {"value": 21})
        registry.register("double", lambda context: {"result": context.read("value") * 2})

        result = GraphRuntime(graph, registry).run()

        self.assertTrue(result.success)
        self.assertEqual(42, result.artifacts["result"])

    def test_retries_only_retryable_failures(self):
        graph = spec(
            [
                {
                    "id": "flaky",
                    "kind": "flaky",
                    "writes": ["value"],
                    "retry": {"max_attempts": 2},
                }
            ]
        )
        registry = NodeRegistry()

        def flaky(context):
            if context.attempt == 1:
                raise RetryableNodeError("temporary")
            return {"value": "ok"}

        registry.register("flaky", flaky)
        result = GraphRuntime(graph, registry).run()
        self.assertTrue(result.success)
        self.assertEqual(2, result.attempts["flaky"])

    def test_gate_blocks_node_and_descendants(self):
        graph = spec(
            [
                {"id": "approve", "kind": "work", "writes": ["approved"], "gate": "ship"},
                {"id": "ship", "kind": "ship", "deps": ["approve"], "writes": ["shipped"]},
            ]
        )
        registry = NodeRegistry()
        registry.register("work", lambda context: {"approved": True})
        registry.register("ship", lambda context: {"shipped": True})
        result = GraphRuntime(graph, registry).run()
        self.assertEqual(NodeStatus.BLOCKED, result.statuses["approve"])
        self.assertEqual(NodeStatus.BLOCKED, result.statuses["ship"])

    def test_budget_admission_and_usage(self):
        graph = spec(
            [{"id": "costly", "kind": "costly", "writes": ["value"], "estimated_tokens": 4}],
            max_tokens=5,
        )
        registry = NodeRegistry()
        registry.register("costly", lambda context: NodeOutcome({"value": 1}, tokens_used=5))
        result = GraphRuntime(graph, registry).run()
        self.assertTrue(result.success)
        self.assertEqual(5, result.tokens_used)

    def test_dollar_budget_blocks_node_before_execution(self):
        graph = spec(
            [
                {
                    "id": "first",
                    "kind": "first",
                    "writes": ["one"],
                    "estimated_cost_usd": 0.4,
                },
                {
                    "id": "second",
                    "kind": "second",
                    "deps": ["first"],
                    "writes": ["two"],
                    "estimated_cost_usd": 0.7,
                },
            ],
            max_cost_usd=1.0,
        )
        calls = []
        registry = NodeRegistry()
        registry.register(
            "first",
            lambda context: NodeOutcome({"one": 1}, cost_usd=0.4),
        )

        def second(context):
            calls.append("second")
            return NodeOutcome({"two": 2}, cost_usd=0.7)

        registry.register("second", second)

        result = GraphRuntime(graph, registry).run()

        self.assertEqual(NodeStatus.COMPLETED, result.statuses["first"])
        self.assertEqual(NodeStatus.BLOCKED, result.statuses["second"])
        self.assertEqual([], calls)
        self.assertAlmostEqual(0.4, result.cost_usd)

    def test_unknown_cost_consumes_reserved_amount(self):
        graph = spec(
            [
                {
                    "id": "work",
                    "kind": "work",
                    "writes": ["value"],
                    "estimated_cost_usd": 0.25,
                }
            ],
            max_cost_usd=1.0,
        )
        registry = NodeRegistry()
        registry.register("work", lambda context: {"value": 1})

        result = GraphRuntime(graph, registry).run()

        self.assertTrue(result.success)
        self.assertAlmostEqual(0.25, result.cost_usd)

    def test_scheduler_does_not_start_more_than_max_concurrency(self):
        graph = spec(
            [
                {"id": "one", "kind": "work", "writes": ["one"]},
                {"id": "two", "kind": "work", "writes": ["two"]},
                {"id": "three", "kind": "work", "writes": ["three"]},
            ],
            max_concurrency=2,
        )
        registry = NodeRegistry()
        lock = threading.Lock()
        active = 0
        peak = 0
        release = threading.Event()

        def work(context):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    release.set()
            release.wait(timeout=1)
            with lock:
                active -= 1
            return {context.node_id: True}

        registry.register("work", work)
        result = GraphRuntime(graph, registry).run()
        self.assertTrue(result.success)
        self.assertEqual(2, peak)

    def test_rejects_non_integer_token_usage(self):
        with self.assertRaises(ContractViolation):
            NodeOutcome({}, tokens_used=1.5)

    def test_preflights_all_registered_node_kinds(self):
        graph = spec([{"id": "unknown", "kind": "missing"}])

        with self.assertRaisesRegex(ContractViolation, "is not registered"):
            GraphRuntime(graph, NodeRegistry()).run()

    def test_local_runtime_explicitly_rejects_controlled_merge(self):
        graph = spec(
            [
                {
                    "id": "change",
                    "kind": "agent",
                    "writes": ["change_result"],
                    "agent": {
                        "prompt": "change",
                        "workspace": {"mode": "isolated"},
                    },
                    "controlled_merge": {
                        "verifier": "verify",
                        "target_branch": "main",
                    },
                },
                {
                    "id": "verify",
                    "kind": "agent",
                    "deps": ["change"],
                    "reads": ["change_result"],
                    "writes": ["verification"],
                    "gate": "merge-approval",
                    "verifier_for": "change",
                    "reality_anchor": True,
                    "verified_reuse": {
                        "decision_artifact": "verification",
                        "passed_path": ["passed"],
                        "quality_path": ["quality"],
                    },
                    "agent": {"prompt": "verify"},
                },
            ]
        )

        with self.assertRaisesRegex(ContractViolation, "requires OrcaCoordinator"):
            GraphRuntime(graph, NodeRegistry())


if __name__ == "__main__":
    unittest.main()
