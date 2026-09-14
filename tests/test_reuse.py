import json
import tempfile
import threading
import time
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
    VerifiedArtifactCache,
)


class CountingExecutor:
    def __init__(
        self,
        started=None,
        release=None,
        error=None,
        features=("structured_output", "token_budget"),
    ):
        self._capabilities = ExecutorCapabilities(
            "counting", features, ()
        )
        self.started = started
        self.release = release
        self.error = error
        self.calls = 0

    @property
    def capabilities(self):
        return self._capabilities

    def execute(self, request):
        self.calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            self.release.wait(2)
        if self.error is not None:
            raise self.error
        return AgentResult(
            "counting",
            {"answer": request.inputs["question"].upper()},
            "done",
            tokens_used=11,
            cost_usd=0.25,
        )


def make_request(workspace, task_id="task", **overrides):
    values = {
        "task_id": task_id,
        "prompt": "Answer precisely",
        "inputs": {"question": "hello"},
        "output_keys": ("answer",),
        "workspace": workspace,
        "reuse_scope": "tenant-a",
    }
    values.update(overrides)
    return AgentRequest(**values)


class ReuseTests(unittest.TestCase):
    def test_reasoning_effort_is_part_of_the_verified_reuse_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = CountingExecutor(
                features=(
                    "reasoning_control",
                    "structured_output",
                    "token_budget",
                )
            )
            registry = ExecutorRegistry(reuse_store=cache)
            registry.register(executor)
            low_request = make_request(workspace, reasoning_effort="low")
            low = registry.execute(low_request)
            cache.publish_verified(
                low_request,
                low,
                source_run_id="run-low",
                verification_id="anchor-low",
                quality_score=1.0,
            )

            high = registry.execute(
                make_request(workspace, "run-high", reasoning_effort="high")
            )

        self.assertEqual("miss", high.reuse_status)
        self.assertEqual(2, executor.calls)

    def test_stricter_token_budget_never_reuses_a_looser_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = CountingExecutor()
            registry = ExecutorRegistry(reuse_store=cache)
            registry.register(executor)
            original_request = make_request(workspace, max_tokens=100)
            original = registry.execute(original_request)
            cache.publish_verified(
                original_request,
                original,
                source_run_id="run-1",
                verification_id="anchor-1",
                quality_score=1.0,
            )

            stricter = registry.execute(
                make_request(workspace, "run-2", max_tokens=50)
            )

        self.assertEqual("miss", stricter.reuse_status)
        self.assertEqual(2, executor.calls)

    def test_only_explicitly_verified_result_is_persisted_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = CountingExecutor()
            registry = ExecutorRegistry(reuse_store=cache)
            registry.register(executor)
            original_request = make_request(workspace, "run-1:answer:1")

            first = registry.execute(original_request)
            second = registry.execute(
                make_request(workspace, "run-2:answer:1")
            )
            cache.publish_verified(
                original_request,
                first,
                source_run_id="run-1",
                verification_id="anchor-1",
                quality_score=0.95,
            )
            third = registry.execute(make_request(workspace, "run-3:answer:1"))

            entry = next((root / "reuse" / "entries").glob("*.json"))
            entry_text = entry.read_text(encoding="utf-8")
            stored = json.loads(entry_text)

        self.assertEqual("miss", first.reuse_status)
        self.assertEqual("miss", second.reuse_status)
        self.assertEqual("hit", third.reuse_status)
        self.assertEqual(2, executor.calls)
        self.assertEqual(0, third.tokens_used)
        self.assertEqual(0.0, third.cost_usd)
        self.assertEqual("run-1", third.source_run_id)
        self.assertEqual("anchor-1", third.verification_id)
        self.assertNotIn("Answer precisely", entry_text)
        self.assertNotIn("hello", entry_text)
        self.assertEqual("tenant-a", stored["key_fields"]["reuse_scope"])

    def test_replaying_same_verified_publication_is_idempotent(self):
        clock = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse", clock=lambda: clock[0])
            request = make_request(workspace, "run-1:answer:1")
            result = AgentResult(
                "counting", {"answer": "HELLO"}, "done", 11, 0.25
            )
            first = cache.publish_verified(request, result, "run-1", "anchor-1", 1.0)
            clock[0] = 101.0
            second = cache.publish_verified(request, result, "run-1", "anchor-1", 1.0)
            entries = tuple((root / "reuse" / "entries").glob("*.json"))

        self.assertEqual(first, second)
        self.assertEqual(100.0, second.created_at)
        self.assertEqual(1, len(entries))

    def test_sensitive_requests_bypass_and_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = CountingExecutor()
            registry = ExecutorRegistry(reuse_store=cache)
            registry.register(
                executor,
                ExecutorProfile(
                    "local", data_classifications=("public", "restricted")
                ),
            )
            request = make_request(
                workspace, data_classification="restricted"
            )

            first = registry.execute(request)
            second = registry.execute(request)
            with self.assertRaisesRegex(ContractViolation, "classification=restricted"):
                cache.publish_verified(request, first, "run", "anchor", 1.0)

        self.assertEqual("bypassed", first.reuse_status)
        self.assertEqual("bypassed", second.reuse_status)
        self.assertEqual(2, executor.calls)

    def test_request_can_disable_reuse_for_workspace_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = CountingExecutor()
            registry = ExecutorRegistry(reuse_store=cache)
            registry.register(executor)
            request = make_request(workspace, reuse_allowed=False)

            first = registry.execute(request)
            second = registry.execute(request)
            with self.assertRaisesRegex(ContractViolation, "request_disabled"):
                cache.publish_verified(request, first, "run", "anchor", 1.0)

        self.assertEqual("bypassed", first.reuse_status)
        self.assertEqual("bypassed", second.reuse_status)
        self.assertEqual(2, executor.calls)

    def test_mutating_tools_always_bypass_even_when_caller_allows_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = CountingExecutor()
            request = make_request(workspace, tools=("write",))

            first = cache.resolve(
                request, "counting", lambda: executor.execute(request)
            )
            second = cache.resolve(
                request, "counting", lambda: executor.execute(request)
            )
            with self.assertRaisesRegex(
                ContractViolation, "mutating_tools=write"
            ):
                cache.publish_verified(request, first, "run", "anchor", 1.0)

        self.assertEqual("bypassed", first.reuse_status)
        self.assertEqual("bypassed", second.reuse_status)
        self.assertEqual(2, executor.calls)

    def test_read_only_extraction_can_reuse_and_reports_measured_savings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = CountingExecutor()
            request = make_request(workspace, tools=("read",))

            first = cache.resolve(
                request, "counting", lambda: executor.execute(request)
            )
            cache.publish_verified(request, first, "run", "anchor", 1.0)
            hit = cache.resolve(
                make_request(workspace, "hit", tools=("read",)),
                "counting",
                lambda: executor.execute(request),
            )
            status = cache.status()

        self.assertEqual("hit", hit.reuse_status)
        self.assertEqual(11, hit.reuse_saved_tokens)
        self.assertEqual(1, executor.calls)
        self.assertEqual(11, status["saved_tokens"])
        self.assertEqual(0.25, status["saved_cost_usd"])
        self.assertTrue(status["saved_cost_complete"])

    def test_unknown_source_cost_keeps_saved_cost_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            request = make_request(workspace)
            original = AgentResult(
                "counting", {"answer": "HELLO"}, "done", tokens_used=11
            )

            first = cache.resolve(request, "counting", lambda: original)
            cache.publish_verified(request, first, "run", "anchor", 1.0)
            hit = cache.resolve(
                make_request(workspace, "hit"),
                "counting",
                lambda: original,
            )
            status = cache.status()
            events = [
                json.loads(line)
                for line in (root / "reuse" / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            hit_event = next(item for item in events if item["status"] == "hit")

        self.assertEqual("hit", hit.reuse_status)
        self.assertEqual(11, status["saved_tokens"])
        self.assertIsNone(status["saved_cost_usd"])
        self.assertFalse(status["saved_cost_complete"])
        self.assertIsNone(hit_event["saved_cost_usd"])
        self.assertFalse(hit_event["saved_cost_complete"])

    def test_scope_ttl_and_checksum_prevent_unsafe_reuse(self):
        clock = [10.0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse", ttl_seconds=5, clock=lambda: clock[0])
            executor = CountingExecutor()
            registry = ExecutorRegistry(reuse_store=cache)
            registry.register(executor)
            request = make_request(workspace)
            first = registry.execute(request)
            cache.publish_verified(request, first, "run", "anchor", 1.0)

            different_scope = registry.execute(
                make_request(workspace, "other", reuse_scope="tenant-b")
            )
            clock[0] = 16.0
            expired = registry.execute(make_request(workspace, "expired"))
            path = next((root / "reuse" / "entries").glob("*.json"))
            value = json.loads(path.read_text(encoding="utf-8"))
            value["outputs"]["answer"] = "TAMPERED"
            path.write_text(json.dumps(value), encoding="utf-8")
            corrupted = registry.execute(make_request(workspace, "corrupted"))

        self.assertEqual("miss", different_scope.reuse_status)
        self.assertEqual("miss", expired.reuse_status)
        self.assertEqual("miss", corrupted.reuse_status)
        self.assertEqual(4, executor.calls)

    def test_concurrent_duplicates_execute_once_but_are_not_persisted(self):
        started = threading.Event()
        release = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = CountingExecutor(started, release)
            registry = ExecutorRegistry(reuse_store=cache)
            registry.register(executor)
            results = []

            first_thread = threading.Thread(
                target=lambda: results.append(
                    registry.execute(make_request(workspace, "leader"))
                )
            )
            second_thread = threading.Thread(
                target=lambda: results.append(
                    registry.execute(make_request(workspace, "waiter"))
                )
            )
            first_thread.start()
            self.assertTrue(started.wait(1))
            second_thread.start()
            time.sleep(0.05)
            release.set()
            first_thread.join(2)
            second_thread.join(2)

            entries = tuple((root / "reuse" / "entries").glob("*.json"))

        self.assertEqual(1, executor.calls)
        self.assertEqual({"miss", "coalesced"}, {item.reuse_status for item in results})
        self.assertEqual((), entries)
        coalesced = next(item for item in results if item.reuse_status == "coalesced")
        self.assertEqual(0, coalesced.tokens_used)
        self.assertEqual(11, coalesced.reuse_saved_tokens)
        self.assertEqual("leader", coalesced.source_task_id)

    def test_leader_failure_is_shared_with_waiters(self):
        started = threading.Event()
        release = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = CountingExecutor(started, release, RuntimeError("failed"))
            registry = ExecutorRegistry(reuse_store=cache)
            registry.register(executor)
            errors = []

            def run(task_id):
                try:
                    registry.execute(make_request(workspace, task_id))
                except Exception as error:
                    errors.append(error)

            first = threading.Thread(target=run, args=("leader",))
            second = threading.Thread(target=run, args=("waiter",))
            first.start()
            self.assertTrue(started.wait(1))
            second.start()
            time.sleep(0.05)
            release.set()
            first.join(2)
            second.join(2)

        self.assertEqual(1, executor.calls)
        self.assertEqual(2, len(errors))
        self.assertTrue(all(str(error) == "failed" for error in errors))

    def test_verified_hit_does_not_consume_provider_rate_limit(self):
        router = PolicyRouter(
            {"provider": ProviderPolicy(requests_per_minute=1)},
            clock=lambda: 0.0,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = CountingExecutor()
            registry = ExecutorRegistry(router, cache)
            registry.register(executor, ExecutorProfile("provider"))
            request = make_request(workspace, "first")
            first = registry.execute(request)
            cache.publish_verified(request, first, "run", "anchor", 1.0)

            hit = registry.execute(make_request(workspace, "hit"))

        self.assertEqual("hit", hit.reuse_status)
        self.assertEqual(1, executor.calls)


if __name__ == "__main__":
    unittest.main()
