import hashlib
import json
import multiprocessing
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
    ProviderGovernanceStore,
    ProviderPolicy,
    VerifiedArtifactCache,
)


def _reserve_worker(root, gate, results, now, policy_values):
    try:
        store = ProviderGovernanceStore(Path(root), clock=lambda: now)
        gate.wait(5)
        admission = store.admit(
            "provider", ProviderPolicy(**policy_values), reserve=True
        )
        results.put(
            (
                "ok",
                admission.allowed,
                admission.reason,
                admission.reservation_token,
            )
        )
    except BaseException as error:
        results.put(("error", type(error).__name__, str(error)))


def _failure_worker(root, gate, results, now, threshold):
    try:
        store = ProviderGovernanceStore(Path(root), clock=lambda: now)
        gate.wait(5)
        store.record_failure(
            "provider",
            ProviderPolicy(failure_threshold=threshold),
            None,
        )
        results.put(("ok",))
    except BaseException as error:
        results.put(("error", type(error).__name__, str(error)))


def _digest(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class _Executor:
    def __init__(self):
        self.calls = 0
        self._capabilities = ExecutorCapabilities(
            "executor", ("structured_output",), ()
        )

    @property
    def capabilities(self):
        return self._capabilities

    def execute(self, request):
        self.calls += 1
        return AgentResult("executor", {"answer": "ok"}, "ok", 1, 0.01)


def _request(workspace, task_id="task", **overrides):
    values = {
        "task_id": task_id,
        "prompt": "work",
        "inputs": {},
        "output_keys": ("answer",),
        "workspace": workspace,
    }
    values.update(overrides)
    return AgentRequest(**values)


class ProviderGovernanceTests(unittest.TestCase):
    def _run_workers(self, target, root, count, *args):
        context = multiprocessing.get_context("spawn")
        gate = context.Event()
        results = context.Queue()
        workers = [
            context.Process(target=target, args=(str(root), gate, results, *args))
            for _ in range(count)
        ]
        for worker in workers:
            worker.start()
        gate.set()
        values = [results.get(timeout=10) for _ in workers]
        for worker in workers:
            worker.join(10)
            self.assertEqual(0, worker.exitcode)
        return values

    def test_two_processes_cannot_oversell_last_rate_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            values = self._run_workers(
                _reserve_worker,
                Path(directory) / "governance",
                2,
                100.0,
                {"requests_per_minute": 1},
            )

        self.assertEqual(1, sum(value[:3] == ("ok", True, None) for value in values))
        self.assertEqual(
            1,
            sum(value[:3] == ("ok", False, "rate_limited") for value in values),
        )

    def test_failures_accumulate_across_processes_and_open_circuit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "governance"
            values = self._run_workers(
                _failure_worker, root, 2, 100.0, 2
            )
            store = ProviderGovernanceStore(root, clock=lambda: 100.0)
            admission = store.admit(
                "provider", ProviderPolicy(failure_threshold=2)
            )
            status = store.status()

        self.assertEqual([("ok",), ("ok",)], sorted(values))
        self.assertFalse(admission.allowed)
        self.assertEqual("circuit_open", admission.reason)
        self.assertEqual(
            2, status["providers"]["provider"]["consecutive_failures"]
        )

    def test_new_router_restores_rate_window_and_circuit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            clock = [0.0]
            policy = ProviderPolicy(
                requests_per_minute=2,
                failure_threshold=1,
                cooldown_seconds=10,
            )
            first = PolicyRouter(
                {"provider": policy},
                governance=ProviderGovernanceStore(
                    root / "governance", clock=lambda: clock[0]
                ),
            )
            first.register("primary", ExecutorProfile("provider", 0.01, 1))
            decision = first.select(("primary",), _request(workspace))
            first.record_failure(decision)

            restored = PolicyRouter(
                {"provider": policy},
                governance=ProviderGovernanceStore(
                    root / "governance", clock=lambda: clock[0]
                ),
            )
            restored.register("primary", ExecutorProfile("provider", 0.01, 1))
            with self.assertRaisesRegex(ContractViolation, "circuit_open"):
                restored.select(("primary",), _request(workspace))
            clock[0] = 11.0
            probe = restored.select(("primary",), _request(workspace))

        self.assertIsNotNone(probe.governance_token)

    def test_new_router_restores_request_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            policy = ProviderPolicy(requests_per_minute=1)
            first = PolicyRouter(
                {"provider": policy},
                governance=ProviderGovernanceStore(
                    root / "governance", clock=lambda: 100.0
                ),
            )
            first.register("executor", ExecutorProfile("provider"))
            first.select(("executor",), _request(workspace))

            restored = PolicyRouter(
                {"provider": policy},
                governance=ProviderGovernanceStore(
                    root / "governance", clock=lambda: 100.0
                ),
            )
            restored.register("executor", ExecutorProfile("provider"))
            with self.assertRaisesRegex(ContractViolation, "rate_limited"):
                restored.select(("executor",), _request(workspace))

    def test_only_one_half_open_probe_and_success_closes_circuit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "governance"
            policy = ProviderPolicy(
                failure_threshold=1,
                cooldown_seconds=10,
                probe_timeout_seconds=5,
            )
            store = ProviderGovernanceStore(root, clock=lambda: 100.0)
            store.record_failure("provider", policy, None)
            values = self._run_workers(
                _reserve_worker,
                root,
                2,
                111.0,
                {
                    "failure_threshold": 1,
                    "cooldown_seconds": 10,
                    "probe_timeout_seconds": 5,
                },
            )
            winner = next(value for value in values if value[1] is True)
            token = winner[3]
            ProviderGovernanceStore(root, clock=lambda: 111.0).record_success(
                "provider", token
            )
            admission = ProviderGovernanceStore(
                root, clock=lambda: 111.0
            ).admit("provider", policy)

        self.assertEqual(("ok", True, None), winner[:3])
        self.assertEqual(
            1,
            sum(
                value[:3] == ("ok", False, "circuit_probe_in_progress")
                for value in values
            ),
        )
        self.assertTrue(admission.allowed)
        self.assertEqual(0, admission.consecutive_failures)

    def test_probe_failure_reopens_and_expired_owner_can_be_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "governance"
            now = [100.0]
            policy = ProviderPolicy(
                failure_threshold=1,
                cooldown_seconds=10,
                probe_timeout_seconds=5,
            )
            store = ProviderGovernanceStore(root, clock=lambda: now[0])
            store.record_failure("provider", policy, None)
            now[0] = 111.0
            abandoned = store.admit("provider", policy, reserve=True)
            self.assertTrue(abandoned.allowed)
            now[0] = 117.0
            replacement = store.admit("provider", policy, reserve=True)
            self.assertTrue(replacement.allowed)
            self.assertNotEqual(
                abandoned.reservation_token, replacement.reservation_token
            )
            store.record_failure(
                "provider", policy, replacement.reservation_token
            )
            rejected = store.admit("provider", policy)

        self.assertFalse(rejected.allowed)
        self.assertEqual("circuit_open", rejected.reason)

    def test_wall_clock_rollback_does_not_release_rate_or_circuit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "governance"
            now = [100.0]
            rate_policy = ProviderPolicy(requests_per_minute=1)
            circuit_policy = ProviderPolicy(
                failure_threshold=1, cooldown_seconds=60
            )
            store = ProviderGovernanceStore(root, clock=lambda: now[0])
            store.admit("rate", rate_policy, reserve=True)
            store.record_failure("circuit", circuit_policy, None)
            now[0] = 50.0
            rate = store.admit("rate", rate_policy)
            circuit = store.admit("circuit", circuit_policy)

        self.assertEqual("rate_limited", rate.reason)
        self.assertEqual("circuit_open", circuit.reason)

    def test_corrupt_checksum_and_future_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "governance"
            store = ProviderGovernanceStore(root, clock=lambda: 100.0)
            store.admit("provider", ProviderPolicy(), reserve=True)
            state_path = root / "state.json"
            original = json.loads(state_path.read_text(encoding="utf-8"))

            state_path.write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(ContractViolation, "invalid provider"):
                store.status()

            damaged = dict(original)
            damaged["generation"] += 1
            state_path.write_text(json.dumps(damaged), encoding="utf-8")
            with self.assertRaisesRegex(ContractViolation, "checksum mismatch"):
                store.admit("provider", ProviderPolicy(), reserve=True)

            future = dict(original)
            future["schema_version"] = 99
            future["checksum"] = _digest(
                {key: value for key, value in future.items() if key != "checksum"}
            )
            state_path.write_text(json.dumps(future), encoding="utf-8")
            with self.assertRaisesRegex(ContractViolation, "unsupported"):
                store.status()

    def test_stricter_policy_and_hard_constraints_apply_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            governance_root = root / "governance"
            store = ProviderGovernanceStore(governance_root, clock=lambda: 100.0)
            store.admit(
                "provider", ProviderPolicy(requests_per_minute=3), reserve=True
            )
            stricter = PolicyRouter(
                {"provider": ProviderPolicy(requests_per_minute=1)},
                governance=ProviderGovernanceStore(
                    governance_root, clock=lambda: 100.0
                ),
            )
            stricter.register(
                "executor",
                ExecutorProfile("provider", 1.0, 20, ("public",)),
            )
            with self.assertRaisesRegex(ContractViolation, "data=restricted"):
                stricter.select(
                    ("executor",),
                    _request(workspace, data_classification="restricted"),
                )
            with self.assertRaisesRegex(ContractViolation, "estimated_cost"):
                stricter.select(
                    ("executor",), _request(workspace, max_cost_usd=0.5)
                )
            with self.assertRaisesRegex(ContractViolation, "estimated_latency"):
                stricter.select(
                    ("executor",), _request(workspace, timeout_seconds=10)
                )
            with self.assertRaisesRegex(ContractViolation, "rate_limited"):
                stricter.select(("executor",), _request(workspace))

            failure_root = root / "failure-governance"
            old_policy = ProviderPolicy(failure_threshold=3)
            old_store = ProviderGovernanceStore(
                failure_root, clock=lambda: 100.0
            )
            old_store.record_failure("provider", old_policy, None)
            newly_strict = old_store.admit(
                "provider",
                ProviderPolicy(failure_threshold=1, cooldown_seconds=60),
            )
            strict_status = old_store.status()

        self.assertFalse(newly_strict.allowed)
        self.assertEqual("circuit_open", newly_strict.reason)
        self.assertEqual(
            "open", strict_status["providers"]["provider"]["circuit"]
        )

    def test_verified_reuse_hit_does_not_consume_persistent_rate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            store = ProviderGovernanceStore(
                root / "governance", clock=lambda: 100.0
            )
            router = PolicyRouter(
                {"provider": ProviderPolicy(requests_per_minute=1)},
                governance=store,
            )
            cache = VerifiedArtifactCache(root / "reuse")
            registry = ExecutorRegistry(router, cache)
            executor = _Executor()
            registry.register(executor, ExecutorProfile("provider"))
            first_request = _request(workspace, "first")
            first = registry.execute(first_request)
            cache.publish_verified(first_request, first, "run", "anchor", 1.0)

            hit = registry.execute(_request(workspace, "hit"))
            status = store.status()

        self.assertEqual("hit", hit.reuse_status)
        self.assertEqual(1, executor.calls)
        self.assertEqual(
            1, status["providers"]["provider"]["requests_last_minute"]
        )


if __name__ == "__main__":
    unittest.main()
