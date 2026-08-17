import tempfile
import unittest
from pathlib import Path

from grapheng import (
    AgentRequest,
    AgentResult,
    ContractViolation,
    ExecutorCapabilities,
    ExecutorProfile,
    ExecutorRegistry,
    PolicyRouter,
    RouteObservation,
    RSILoop,
)


class LearningExecutor:
    def __init__(self, executor_id, cost=0.0):
        self._capabilities = ExecutorCapabilities(executor_id, ("structured_output",), ())
        self.cost = cost

    @property
    def capabilities(self):
        return self._capabilities

    def execute(self, request):
        return AgentResult(
            self.capabilities.executor_id,
            {"answer": "ok"},
            "ok",
            cost_usd=self.cost,
        )


def observation(
    executor_id,
    cost,
    latency,
    success=True,
    index=0,
    task_type="general",
    toolset=(),
    model_family="default",
):
    return RouteObservation(
        observed_at=float(index),
        task_id=f"task-{task_type}-{executor_id}-{index}",
        executor_id=executor_id,
        provider=f"provider-{executor_id}",
        success=success,
        latency_seconds=latency,
        cost_usd=cost,
        data_classification="public",
        task_type=task_type,
        toolset=toolset,
        model_family=model_family,
    )


class LearningTests(unittest.TestCase):
    def test_candidate_requires_evaluation_approval_and_can_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            loop = RSILoop(Path(directory))
            for index in range(3):
                slow = observation("slow", 0.5, 5.0, index=index)
                fast = observation("fast", 0.1, 1.0, index=index)
                loop.record(slow)
                loop.record(fast)
                loop.feedback(slow.task_id, 0.9, "reality-anchor")
                loop.feedback(fast.task_id, 0.9, "reality-anchor")

            candidate = loop.propose(
                min_observations=6,
                min_samples=3,
                rollout_percent=100,
            )
            with self.assertRaisesRegex(ContractViolation, "not approved"):
                loop.activate(candidate.candidate_id)
            evaluated = loop.evaluate(candidate.candidate_id)
            approved = loop.approve(candidate.candidate_id, "operator")
            active = loop.activate(candidate.candidate_id)

            self.assertTrue(evaluated.evaluation["passed"])
            self.assertEqual("fast", evaluated.evaluation["preferred_executor"])
            self.assertEqual("operator", approved.approved_by)
            self.assertEqual(candidate.candidate_id, active.version)
            self.assertIsNone(loop.rollback())
            self.assertIsNone(loop.active_policy())

    def test_active_learning_policy_changes_route_without_relaxing_constraints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop = RSILoop(root / "learning")
            for index in range(3):
                static = observation("static-favorite", 0.5, 5.0, index=index)
                learned = observation("learned-favorite", 0.1, 1.0, index=index)
                loop.record(static)
                loop.record(learned)
                loop.feedback(static.task_id, 0.9, "reality-anchor")
                loop.feedback(learned.task_id, 0.9, "reality-anchor")
            candidate = loop.propose(6, 3, rollout_percent=100)
            loop.evaluate(candidate.candidate_id)
            loop.approve(candidate.candidate_id, "operator")
            loop.activate(candidate.candidate_id)

            router = PolicyRouter()
            loop.apply(router)
            registry = ExecutorRegistry(router)
            registry.register(
                LearningExecutor("static-favorite"),
                ExecutorProfile("one", 0.01, 0.1, ("public",)),
            )
            registry.register(
                LearningExecutor("learned-favorite"),
                ExecutorProfile("two", 1.0, 10.0, ("public",)),
            )
            request = AgentRequest(
                "route-task",
                "work",
                {},
                ("answer",),
                root,
            )

            result = registry.execute(request)

            self.assertEqual("learned-favorite", result.executor_id)

    def test_router_writes_prompt_free_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop = RSILoop(root / "learning")
            router = PolicyRouter(observer=loop.record)
            registry = ExecutorRegistry(router)
            registry.register(LearningExecutor("memory", cost=0.2))
            request = AgentRequest(
                "task",
                "secret prompt must not be recorded",
                {"secret": "value"},
                ("answer",),
                root,
            )

            registry.execute(request)
            records = loop.journal.read()
            raw = (root / "learning" / "observations.jsonl").read_text(encoding="utf-8")

            self.assertEqual(1, len(records))
            self.assertEqual("memory", records[0].executor_id)
            self.assertAlmostEqual(0.2, records[0].cost_usd)
            self.assertNotIn("secret prompt", raw)
            self.assertNotIn("value", raw)

    def test_quality_gate_excludes_a_cheaper_low_quality_executor(self):
        with tempfile.TemporaryDirectory() as directory:
            loop = RSILoop(Path(directory))
            for index in range(3):
                cheap = observation("cheap", 0.05, 1.0, index=index)
                quality = observation("quality", 0.20, 2.0, index=index)
                loop.record(cheap)
                loop.record(quality)
                loop.feedback(cheap.task_id, 0.5, "reality-anchor")
                loop.feedback(quality.task_id, 0.95, "reality-anchor")

            candidate = loop.propose(6, 3, rollout_percent=100)
            evaluated = loop.evaluate(candidate.candidate_id)

        self.assertEqual("quality", evaluated.evaluation["preferred_executor"])
        self.assertAlmostEqual(
            0.95,
            candidate.policy.estimates["quality"].average_quality,
        )

    def test_candidate_without_quality_evidence_cannot_pass_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            loop = RSILoop(Path(directory))
            for index in range(3):
                loop.record(observation("fast", 0.1, 1.0, index=index))

            candidate = loop.propose(3, 3, rollout_percent=100)
            evaluated = loop.evaluate(candidate.candidate_id)

        self.assertFalse(evaluated.evaluation["passed"])
        self.assertEqual([], evaluated.evaluation["reliable_executors"])
        self.assertEqual(0.0, evaluated.evaluation["quality_feedback_coverage_percent"])

    def test_cost_is_preferred_after_executors_pass_quality_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            loop = RSILoop(Path(directory))
            for index in range(3):
                pi = observation("pi-agent", 0.08, 1.0, index=index)
                claude = observation("claude-code", 0.22, 2.0, index=index)
                loop.record(pi)
                loop.record(claude)
                loop.feedback(pi.task_id, 0.82, "reality-anchor")
                loop.feedback(claude.task_id, 0.94, "reality-anchor")

            candidate = loop.propose(6, 3, rollout_percent=100)
            evaluated = loop.evaluate(candidate.candidate_id)

        self.assertEqual("pi-agent", evaluated.evaluation["preferred_executor"])
        self.assertGreater(
            evaluated.evaluation["cost_saving_opportunity_percent"], 0
        )

    def test_task_conditioned_policy_uses_local_quality_and_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loop = RSILoop(root / "learning")
            for index in range(3):
                coding_pi = observation(
                    "pi-agent", 0.08, 1.0, index=index, task_type="coding"
                )
                coding_claude = observation(
                    "claude-code", 0.22, 2.0, index=index, task_type="coding"
                )
                research_pi = observation(
                    "pi-agent", 0.05, 1.0, index=index, task_type="research"
                )
                research_claude = observation(
                    "claude-code", 0.20, 2.0, index=index, task_type="research"
                )
                for item, score in (
                    (coding_pi, 0.82),
                    (coding_claude, 0.94),
                    (research_pi, 0.60),
                    (research_claude, 0.90),
                ):
                    loop.record(item)
                    loop.feedback(item.task_id, score, "reality-anchor")

            candidate = loop.propose(12, 3, rollout_percent=100)
            evaluated = loop.evaluate(candidate.candidate_id)
            router = PolicyRouter()
            router.apply_policy(candidate.policy)
            router.register("pi-agent", ExecutorProfile("pi", 0.01, 0.1))
            router.register("claude-code", ExecutorProfile("anthropic", 0.30, 3.0))
            coding = AgentRequest(
                "coding-next", "work", {}, ("answer",), root, task_type="coding"
            )
            research = AgentRequest(
                "research-next", "work", {}, ("answer",), root, task_type="research"
            )

            coding_route = router.select(("pi-agent", "claude-code"), coding)
            research_route = router.select(("pi-agent", "claude-code"), research)

        self.assertEqual(2, evaluated.evaluation["conditioned_contexts"])
        self.assertEqual(100.0, evaluated.evaluation["conditioned_coverage_percent"])
        self.assertEqual("pi-agent", coding_route.executor_id)
        self.assertEqual("claude-code", research_route.executor_id)


if __name__ == "__main__":
    unittest.main()
