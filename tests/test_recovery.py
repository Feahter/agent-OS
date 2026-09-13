import json
import tempfile
import unittest
from pathlib import Path

from grapheng import (
    AllowListGatePolicy,
    ContractViolation,
    GraphRuntime,
    GraphSpec,
    ModelUsage,
    NodeOutcome,
    NodeRegistry,
    NodeStatus,
)


class RecoveryTests(unittest.TestCase):
    def test_resume_skips_completed_nodes_and_rechecks_gate(self):
        graph = GraphSpec.from_dict(
            {
                "id": "resume-test",
                "require_reality_anchor": False,
                "nodes": [
                    {"id": "seed", "kind": "seed", "writes": ["seed"]},
                    {
                        "id": "release",
                        "kind": "release",
                        "deps": ["seed"],
                        "reads": ["seed"],
                        "writes": ["released"],
                        "gate": "human",
                    },
                ],
            }
        )
        calls = {"seed": 0}
        registry = NodeRegistry()

        def seed(context):
            calls["seed"] += 1
            return {"seed": "stable"}

        registry.register("seed", seed)
        registry.register("release", lambda context: {"released": context.read("seed")})

        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            first = GraphRuntime(graph, registry, work_dir=work_dir).run()
            self.assertEqual(NodeStatus.COMPLETED, first.statuses["seed"])
            self.assertEqual(NodeStatus.BLOCKED, first.statuses["release"])

            second = GraphRuntime(
                graph,
                registry,
                work_dir=work_dir,
                gate_policy=AllowListGatePolicy({"human"}),
            ).run(resume=True)

            self.assertTrue(second.success)
            self.assertEqual(1, calls["seed"])
            events = [json.loads(line) for line in (work_dir / "events.jsonl").read_text().splitlines()]
            self.assertTrue(any(event["event"] == "run_resumed" for event in events))
            self.assertTrue(all(event["graph_id"] == "resume-test" for event in events))

    def test_checkpoint_round_trip_and_legacy_defaults_preserve_usage(self):
        graph = GraphSpec.from_dict(
            {
                "id": "usage-recovery",
                "require_reality_anchor": False,
                "nodes": [
                    {"id": "seed", "kind": "seed", "writes": ["seed"]}
                ],
            }
        )
        registry = NodeRegistry()
        registry.register(
            "seed",
            lambda context: NodeOutcome(
                {"seed": True},
                tokens_used=8,
                cost_usd=0.0,
                usage=ModelUsage(
                    input_tokens=5,
                    cached_input_tokens=3,
                    output_tokens=3,
                    total_tokens=8,
                    cost_usd=0.0,
                    input_tokens_complete=True,
                    cached_input_tokens_complete=True,
                    output_tokens_complete=True,
                    total_tokens_complete=True,
                    cost_complete=True,
                ),
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            first = GraphRuntime(graph, registry, work_dir=work_dir).run()
            checkpoint_path = work_dir / "checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertEqual(5, checkpoint["usage"]["input_tokens"])
            self.assertTrue(checkpoint["usage"]["cost_complete"])
            events = [
                json.loads(line)
                for line in (work_dir / "events.jsonl").read_text().splitlines()
            ]
            node_event = next(
                item for item in events if item["event"] == "node_completed"
            )
            run_event = next(
                item for item in events if item["event"] == "run_completed"
            )
            self.assertEqual(3, node_event["payload"]["usage"]["output_tokens"])
            self.assertTrue(run_event["payload"]["usage"]["cost_complete"])

            checkpoint.pop("usage")
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            resumed = GraphRuntime(graph, registry, work_dir=work_dir).run(
                resume=True
            )

        self.assertTrue(first.usage.cost_complete)
        self.assertEqual(8, resumed.usage.total_tokens)
        self.assertFalse(resumed.usage.total_tokens_complete)
        self.assertEqual(0.0, resumed.usage.cost_usd)
        self.assertFalse(resumed.usage.cost_complete)

    def test_resume_rejects_changed_graph_with_same_id(self):
        original = GraphSpec.from_dict(
            {
                "id": "stable-id",
                "require_reality_anchor": False,
                "nodes": [{"id": "seed", "kind": "seed", "writes": ["value"]}],
            }
        )
        changed = GraphSpec.from_dict(
            {
                "id": "stable-id",
                "require_reality_anchor": False,
                "nodes": [{"id": "seed", "kind": "changed", "writes": ["value"]}],
            }
        )
        registry = NodeRegistry()
        registry.register("seed", lambda context: {"value": 1})
        registry.register("changed", lambda context: {"value": 2})

        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            GraphRuntime(original, registry, work_dir=work_dir).run()

            with self.assertRaisesRegex(ContractViolation, "fingerprint"):
                GraphRuntime(changed, registry, work_dir=work_dir).run(resume=True)

    def test_resume_preserves_dollar_budget_usage(self):
        graph = GraphSpec.from_dict(
            {
                "id": "cost-resume",
                "require_reality_anchor": False,
                "max_cost_usd": 1.0,
                "nodes": [
                    {
                        "id": "seed",
                        "kind": "seed",
                        "writes": ["seed"],
                        "estimated_cost_usd": 0.4,
                    },
                    {
                        "id": "release",
                        "kind": "release",
                        "deps": ["seed"],
                        "writes": ["released"],
                        "estimated_cost_usd": 0.5,
                        "gate": "human",
                    },
                ],
            }
        )
        registry = NodeRegistry()
        registry.register(
            "seed", lambda context: NodeOutcome({"seed": True}, cost_usd=0.4)
        )
        registry.register(
            "release",
            lambda context: NodeOutcome({"released": True}, cost_usd=0.5),
        )

        with tempfile.TemporaryDirectory() as directory:
            work_dir = Path(directory)
            first = GraphRuntime(graph, registry, work_dir=work_dir).run()
            second = GraphRuntime(
                graph,
                registry,
                work_dir=work_dir,
                gate_policy=AllowListGatePolicy({"human"}),
            ).run(resume=True)

        self.assertAlmostEqual(0.4, first.cost_usd)
        self.assertAlmostEqual(0.9, second.cost_usd)


if __name__ == "__main__":
    unittest.main()
