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
    ProviderPolicy,
)


class RoutedExecutor:
    def __init__(self, executor_id, failures=0):
        self._capabilities = ExecutorCapabilities(
            executor_id,
            ("cost_budget", "structured_output"),
            (),
        )
        self.failures = failures
        self.calls = 0

    @property
    def capabilities(self):
        return self._capabilities

    def execute(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("provider unavailable")
        return AgentResult(self.capabilities.executor_id, {"answer": "ok"}, "ok")


def request(workspace, **overrides):
    values = {
        "task_id": "task",
        "prompt": "work",
        "inputs": {},
        "output_keys": ("answer",),
        "workspace": workspace,
    }
    values.update(overrides)
    return AgentRequest(**values)


class RoutingTests(unittest.TestCase):
    def test_routes_by_data_permission_cost_and_latency(self):
        registry = ExecutorRegistry()
        public = RoutedExecutor("public")
        slow = RoutedExecutor("slow")
        secure = RoutedExecutor("secure")
        registry.register(public, ExecutorProfile("public-provider", 0.01, 1, ("public",)))
        registry.register(
            slow,
            ExecutorProfile("slow-provider", 0.01, 60, ("confidential",)),
        )
        registry.register(
            secure,
            ExecutorProfile("secure-provider", 0.20, 2, ("confidential",)),
        )

        with tempfile.TemporaryDirectory() as directory:
            result = registry.execute(
                request(
                    Path(directory),
                    data_classification="confidential",
                    timeout_seconds=10,
                    max_cost_usd=0.50,
                )
            )

        self.assertEqual("secure", result.executor_id)
        self.assertEqual(0, public.calls)
        self.assertEqual(0, slow.calls)

    def test_rate_limit_routes_to_another_provider(self):
        clock = [0.0]
        router = PolicyRouter(
            {"cheap-provider": ProviderPolicy(requests_per_minute=1)},
            clock=lambda: clock[0],
        )
        registry = ExecutorRegistry(router)
        cheap = RoutedExecutor("cheap")
        backup = RoutedExecutor("backup")
        registry.register(cheap, ExecutorProfile("cheap-provider", 0.01, 1))
        registry.register(backup, ExecutorProfile("backup-provider", 0.10, 1))

        with tempfile.TemporaryDirectory() as directory:
            first = registry.execute(request(Path(directory)))
            second = registry.execute(request(Path(directory)))

        self.assertEqual("cheap", first.executor_id)
        self.assertEqual("backup", second.executor_id)

    def test_circuit_breaker_opens_and_recovers_after_cooldown(self):
        clock = [0.0]
        router = PolicyRouter(
            {"primary-provider": ProviderPolicy(failure_threshold=1, cooldown_seconds=10)},
            clock=lambda: clock[0],
        )
        registry = ExecutorRegistry(router)
        primary = RoutedExecutor("primary", failures=1)
        backup = RoutedExecutor("backup")
        registry.register(primary, ExecutorProfile("primary-provider", 0.01, 1))
        registry.register(backup, ExecutorProfile("backup-provider", 0.10, 1))

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                registry.execute(request(Path(directory)))
            fallback = registry.execute(request(Path(directory)))
            clock[0] = 11.0
            recovered = registry.execute(request(Path(directory)))

        self.assertEqual("backup", fallback.executor_id)
        self.assertEqual("primary", recovered.executor_id)

    def test_reports_policy_rejections(self):
        registry = ExecutorRegistry()
        registry.register(
            RoutedExecutor("public"),
            ExecutorProfile("provider", data_classifications=("public",)),
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ContractViolation, "data=restricted"):
                registry.execute(
                    request(Path(directory), data_classification="restricted")
                )


if __name__ == "__main__":
    unittest.main()
