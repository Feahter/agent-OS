import json
import tempfile
import unittest
from pathlib import Path

from grapheng import (
    CanaryObservation,
    ContractViolation,
    FailurePattern,
    GraphSpec,
    RegressionCase,
    RegressionMeasurement,
    RSIOptimizationLab,
)


def regression_case():
    return RegressionCase(
        "coding-basic",
        "a" * 64,
        "coding",
        0.9,
        0.20,
        2.0,
    )


def passing_measurement():
    return RegressionMeasurement(
        "coding-basic",
        0.92,
        0.18,
        1.8,
        True,
    )


def sample_graph():
    return GraphSpec.from_dict(
        {
            "id": "optimization-graph",
            "require_reality_anchor": False,
            "max_concurrency": 2,
            "nodes": [
                {
                    "id": "draft",
                    "kind": "agent",
                    "writes": ["draft"],
                    "agent": {
                        "prompt": "Prepare a draft.",
                        "task_type": "coding",
                    },
                },
                {
                    "id": "review",
                    "kind": "agent",
                    "writes": ["review"],
                    "agent": {
                        "prompt": "Review the result.",
                        "task_type": "review",
                    },
                },
            ],
        }
    )


class OptimizationTests(unittest.TestCase):
    def test_repeated_failures_generate_governed_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = RSIOptimizationLab(Path(directory))
            candidates = lab.propose_from_failures(
                (
                    FailurePattern("coding", "missing_evidence", 5),
                    FailurePattern("coding", "format_mismatch", 2),
                    FailurePattern(
                        "review",
                        "race_condition",
                        4,
                        node_id="review",
                        depends_on="draft",
                    ),
                ),
                min_occurrences=3,
            )

        self.assertEqual(
            ("prompt_template", "graph_topology"),
            tuple(item.kind for item in candidates),
        )
        self.assertIn("Cite the evidence", candidates[0].change["append"])
        self.assertNotIn("output contract", candidates[0].change["append"])
        self.assertEqual("proposed", candidates[1].status)

    def test_prompt_candidate_requires_frozen_regression_and_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = RSIOptimizationLab(Path(directory))
            suite = lab.freeze_suite((regression_case(),))
            candidate = lab.propose(
                "prompt_template",
                {
                    "task_type": "coding",
                    "append": "Cite the evidence used for every conclusion.",
                },
                "Repeated verification failures lacked evidence references.",
                rollout_percent=100,
            )

            with self.assertRaisesRegex(ContractViolation, "not evaluated"):
                lab.approve(candidate.candidate_id, "operator")
            evaluated = lab.evaluate(
                candidate.candidate_id,
                suite.suite_id,
                (passing_measurement(),),
            )
            approved = lab.approve(candidate.candidate_id, "operator")
            active = lab.activate(candidate.candidate_id)
            optimized = lab.apply(sample_graph(), rollout_key="canary-run")

        self.assertTrue(evaluated.evaluation["passed"])
        self.assertEqual("operator", approved.approved_by)
        self.assertEqual("active", active.status)
        self.assertIn(
            "Cite the evidence",
            optimized.node_map()["draft"].agent.prompt,
        )
        self.assertEqual(
            "Review the result.", optimized.node_map()["review"].agent.prompt
        )
        self.assertEqual(2, optimized.max_concurrency)

    def test_regression_or_budget_failure_blocks_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = RSIOptimizationLab(Path(directory))
            suite = lab.freeze_suite((regression_case(),))
            candidate = lab.propose(
                "prompt_template",
                {"task_type": "coding", "append": "Be concise."},
                "Reduce latency.",
            )
            failed = lab.evaluate(
                candidate.candidate_id,
                suite.suite_id,
                (
                    RegressionMeasurement(
                        "coding-basic", 0.8, 0.30, 3.0, True
                    ),
                ),
            )

            with self.assertRaisesRegex(ContractViolation, "did not pass"):
                lab.approve(candidate.candidate_id, "operator")

        self.assertFalse(failed.evaluation["passed"])
        self.assertIn(
            "coding-basic:quality_regression", failed.evaluation["failures"]
        )
        self.assertIn(
            "suite:cost_budget_exceeded", failed.evaluation["failures"]
        )

    def test_topology_candidate_only_adds_dependencies_and_lowers_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = RSIOptimizationLab(Path(directory))
            suite = lab.freeze_suite((regression_case(),))
            candidate = lab.propose(
                "graph_topology",
                {
                    "add_dependencies": [
                        {"node": "review", "depends_on": "draft"}
                    ],
                    "max_concurrency": 1,
                },
                "Prevent review from racing the draft.",
                rollout_percent=100,
            )
            lab.evaluate(
                candidate.candidate_id,
                suite.suite_id,
                (passing_measurement(),),
            )
            lab.approve(candidate.candidate_id, "operator")
            lab.activate(candidate.candidate_id)

            optimized = lab.apply(sample_graph(), rollout_key="canary-run")

        self.assertEqual(("draft",), optimized.node_map()["review"].deps)
        self.assertEqual(1, optimized.max_concurrency)

    def test_canary_regression_automatically_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = RSIOptimizationLab(Path(directory))
            suite = lab.freeze_suite((regression_case(),))
            previous = lab.propose(
                "prompt_template",
                {"task_type": "coding", "append": "Check the result."},
                "Previous stable candidate.",
                rollout_percent=100,
            )
            lab.evaluate(
                previous.candidate_id,
                suite.suite_id,
                (passing_measurement(),),
                max_cost_increase_percent=10.0,
            )
            lab.approve(previous.candidate_id, "operator")
            lab.activate(previous.candidate_id)
            candidate = lab.propose(
                "prompt_template",
                {"task_type": "coding", "append": "Use evidence."},
                "Improve groundedness.",
                rollout_percent=100,
            )
            lab.evaluate(
                candidate.candidate_id,
                suite.suite_id,
                (passing_measurement(),),
                max_cost_increase_percent=10.0,
            )
            lab.approve(candidate.candidate_id, "operator")
            lab.activate(candidate.candidate_id)

            healthy = lab.observe_canary(
                CanaryObservation(
                    candidate.candidate_id,
                    10.0,
                    True,
                    0.92,
                    0.90,
                    0.30,
                    0.20,
                    1.8,
                    2.0,
                )
            )

            active = lab.active_candidates()
            record = json.loads(
                (Path(directory) / "canary.jsonl").read_text(encoding="utf-8")
            )

        self.assertFalse(healthy)
        self.assertEqual(
            previous.candidate_id, active["prompt_template"].candidate_id
        )
        self.assertFalse(record["healthy"])

    def test_topology_learning_cannot_raise_hard_concurrency_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            lab = RSIOptimizationLab(Path(directory))
            suite = lab.freeze_suite((regression_case(),))
            candidate = lab.propose(
                "graph_topology",
                {"max_concurrency": 3},
                "Attempt to increase throughput.",
                rollout_percent=100,
            )
            lab.evaluate(
                candidate.candidate_id,
                suite.suite_id,
                (passing_measurement(),),
            )
            lab.approve(candidate.candidate_id, "operator")
            lab.activate(candidate.candidate_id)

            with self.assertRaisesRegex(
                ContractViolation, "cannot increase.*concurrency cap"
            ):
                lab.apply(sample_graph(), rollout_key="canary-run")

    def test_suite_tampering_and_forbidden_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lab = RSIOptimizationLab(root)
            suite = lab.freeze_suite((regression_case(),))
            path = root / "suites" / f"{suite.suite_id}.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            value["cases"][0]["baseline_quality"] = 0.1
            path.write_text(json.dumps(value), encoding="utf-8")

            candidate = lab.propose(
                "prompt_template",
                {"task_type": "coding", "append": "Use evidence."},
                "Improve groundedness.",
            )
            with self.assertRaisesRegex(ContractViolation, "was modified"):
                lab.evaluate(
                    candidate.candidate_id,
                    suite.suite_id,
                    (passing_measurement(),),
                )
            with self.assertRaisesRegex(ContractViolation, "contain only"):
                lab.propose(
                    "prompt_template",
                    {
                        "task_type": "coding",
                        "append": "Use evidence.",
                        "max_cost_usd": 10,
                    },
                    "Attempt to loosen a hard limit.",
                )


if __name__ == "__main__":
    unittest.main()
