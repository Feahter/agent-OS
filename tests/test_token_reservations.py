import unittest

from grapheng import (
    GraphSpec,
    HistoricalTokenReservations,
    RouteObservation,
)


def agent_node(*, executor=None, task_type="coding", tools=("read",), model_family="fast"):
    agent = {
        "prompt": "work",
        "task_type": task_type,
        "tools": list(tools),
        "model_family": model_family,
    }
    if executor is not None:
        agent["executor"] = executor
    graph = GraphSpec.from_dict(
        {
            "id": "reservation-test",
            "require_reality_anchor": False,
            "nodes": [
                {
                    "id": "work",
                    "kind": "agent",
                    "writes": ["answer"],
                    "estimated_tokens": 25,
                    "max_tokens": 500,
                    "agent": agent,
                }
            ],
        }
    )
    return graph.nodes[0]


def observation(
    index,
    tokens,
    *,
    executor="codex",
    task_type="coding",
    tools=("read",),
    model_family="fast",
    complete=True,
):
    return RouteObservation(
        observed_at=float(index),
        task_id=f"task-{index}",
        executor_id=executor,
        provider="provider",
        success=True,
        latency_seconds=1.0,
        cost_usd=None,
        data_classification="public",
        task_type=task_type,
        toolset=tools,
        model_family=model_family,
        total_tokens=tokens,
        total_tokens_complete=complete,
    )


class HistoricalTokenReservationTests(unittest.TestCase):
    def test_uses_conservative_upper_bound_until_history_is_sufficient(self):
        policy = HistoricalTokenReservations(
            tuple(observation(index, 20 + index) for index in range(4)),
            min_samples=5,
        )

        decision = policy.reserve(agent_node())

        self.assertEqual(500, decision.tokens)
        self.assertEqual("conservative_upper_bound", decision.source)
        self.assertEqual(4, decision.samples)

    def test_uses_nearest_rank_p95_without_changing_node_hard_limit(self):
        policy = HistoricalTokenReservations(
            tuple(observation(index, (index + 1) * 10) for index in range(20)),
            min_samples=5,
        )

        decision = policy.reserve(agent_node())

        self.assertEqual(190, decision.tokens)
        self.assertEqual("historical_p95", decision.source)
        self.assertEqual(500, decision.hard_limit)
        self.assertEqual(20, decision.samples)

    def test_ignores_incomplete_and_incompatible_samples(self):
        samples = (
            *(observation(index, 100 + index) for index in range(5)),
            observation(10, 499, complete=False),
            observation(11, 499, task_type="research"),
            observation(12, 499, tools=("shell",)),
            observation(13, 499, model_family="large"),
            observation(14, 499, executor="other"),
        )
        policy = HistoricalTokenReservations(samples, min_samples=5)

        decision = policy.reserve(agent_node(executor="codex"))

        self.assertEqual(104, decision.tokens)
        self.assertEqual(5, decision.samples)

    def test_persistent_underprediction_produces_actionable_warning(self):
        samples = tuple(observation(index, 100) for index in range(5))
        policy = HistoricalTokenReservations(
            samples,
            min_samples=5,
            deviation_streak=3,
        )
        decision = policy.reserve(agent_node())

        no_warning = policy.deviation_warning(
            agent_node(), decision, actual_tokens=120, recent_actuals=(100, 130)
        )
        warning = policy.deviation_warning(
            agent_node(), decision, actual_tokens=120, recent_actuals=(130, 140)
        )

        self.assertIsNone(no_warning)
        self.assertEqual("token_reservation_underpredicted", warning.code)
        self.assertEqual(100, warning.reserved_tokens)
        self.assertEqual(120, warning.actual_tokens)
        self.assertIn("review task grouping or reservation history", warning.action)


if __name__ == "__main__":
    unittest.main()
