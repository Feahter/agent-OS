import json
import multiprocessing
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from grapheng import (
    ContractViolation,
    EffectIndeterminateError,
    EffectJournal,
    GraphSpec,
    LocalControlPlane,
    ModelUsage,
    NodeOutcome,
    NodeRegistry,
    NodeStatus,
)
from grapheng.leases import LeaseLostError, assert_lease, claim_lease, renew_lease


def graph(nodes):
    return GraphSpec.from_dict(
        {
            "id": "control-test",
            "require_reality_anchor": False,
            "nodes": nodes,
        }
    )


def _cross_process_start(root, run_id, ready, start_signal, outcomes):
    registry = NodeRegistry()

    def execute(_context):
        time.sleep(0.3)
        return {"answer": 42}

    registry.register("work", execute)
    plane = LocalControlPlane(Path(root), owner_id=f"owner-{multiprocessing.current_process().pid}")
    ready.put(True)
    start_signal.wait(timeout=5)
    try:
        plane.start(run_id, registry)
        outcomes.put("started")
        plane.wait(run_id, timeout=5)
    except ContractViolation:
        outcomes.put("rejected")
    finally:
        plane.close()


class ControlPlaneTests(unittest.TestCase):
    def test_expired_lease_token_is_invalid_before_takeover(self):
        state = {
            "run_id": "run-1",
            "phase": "queued",
            "owner_id": None,
            "generation": 1,
        }
        token = claim_lease(
            state,
            run_id="run-1",
            owner_id="owner-one",
            lease_seconds=5,
            now=100,
            takeover=False,
        )

        assert_lease(state, token, now=104.999)
        with self.assertRaises(LeaseLostError):
            assert_lease(state, token, now=105)
        with self.assertRaises(LeaseLostError):
            renew_lease(state, token, now=105, lease_seconds=5)

    def test_only_one_process_can_claim_a_queued_run(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creator = LocalControlPlane(root, owner_id="creator")
            run_id = creator.prepare(
                graph([{"id": "work", "kind": "work", "writes": ["answer"]}])
            )
            creator.close()
            ready = context.Queue()
            start_signal = context.Event()
            outcomes = context.Queue()
            processes = [
                context.Process(
                    target=_cross_process_start,
                    args=(str(root), run_id, ready, start_signal, outcomes),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=5))
            start_signal.set()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(0, process.exitcode)
            observed = sorted(outcomes.get(timeout=2) for _ in processes)

        self.assertEqual(["rejected", "started"], observed)

    def test_takeover_fences_stale_heartbeat_and_increments_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = LocalControlPlane(root, owner_id="owner-one", lease_seconds=30)
            run_id = first.prepare(
                graph([{"id": "work", "kind": "work", "writes": ["answer"]}])
            )
            stale = first._mutate_state(
                run_id,
                lambda state: claim_lease(
                    state,
                    run_id=run_id,
                    owner_id=first.owner_id,
                    lease_seconds=first.lease_seconds,
                    now=time.time(),
                    takeover=False,
                ),
            )
            first._mutate_state(
                run_id, lambda state: state.update(lease_expires_at=0.0)
            )

            second = LocalControlPlane(root, owner_id="owner-two", lease_seconds=30)
            current = second._mutate_state(
                run_id,
                lambda state: claim_lease(
                    state,
                    run_id=run_id,
                    owner_id=second.owner_id,
                    lease_seconds=second.lease_seconds,
                    now=time.time(),
                    takeover=True,
                ),
            )
            with self.assertRaises(LeaseLostError):
                first._mutate_state(
                    run_id,
                    lambda state: renew_lease(
                        state,
                        stale,
                        now=time.time(),
                        lease_seconds=first.lease_seconds,
                    ),
                )
            snapshot = second.inspect(run_id)
            first.close()
            second.close()

        self.assertEqual(2, current.generation)
        self.assertEqual(2, snapshot.generation)
        self.assertEqual("owner-two", snapshot.owner_id)

    def test_takeover_fences_stale_success_and_failure_settle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = LocalControlPlane(root, owner_id="owner-one", lease_seconds=30)
            run_id = first.prepare(
                graph([{"id": "work", "kind": "work", "writes": ["answer"]}])
            )
            stale = first._mutate_state(
                run_id,
                lambda state: claim_lease(
                    state,
                    run_id=run_id,
                    owner_id=first.owner_id,
                    lease_seconds=first.lease_seconds,
                    now=time.time(),
                    takeover=False,
                ),
            )
            first._mutate_state(
                run_id, lambda state: state.update(lease_expires_at=0.0)
            )
            second = LocalControlPlane(root, owner_id="owner-two", lease_seconds=30)
            second._mutate_state(
                run_id,
                lambda state: claim_lease(
                    state,
                    run_id=run_id,
                    owner_id=second.owner_id,
                    lease_seconds=second.lease_seconds,
                    now=time.time(),
                    takeover=True,
                ),
            )
            result = SimpleNamespace(
                statuses={"work": NodeStatus.COMPLETED},
                success=True,
                tokens_used=0,
                cost_usd=0.0,
                usage=ModelUsage.no_call(),
                artifacts={"answer": 42},
            )

            with self.assertRaises(LeaseLostError):
                first._settle_success(run_id, stale, result, False)
            with self.assertRaises(LeaseLostError):
                first._settle_error(run_id, stale, RuntimeError("late failure"))
            snapshot = second.inspect(run_id)
            first.close()
            second.close()

        self.assertEqual("running", snapshot.phase)
        self.assertEqual("owner-two", snapshot.owner_id)
        self.assertIsNone(snapshot.result)
        self.assertIsNone(snapshot.error)

    def test_takeover_stops_stale_owner_before_dispatching_next_node(self):
        spec = graph(
            [
                {"id": "first", "kind": "first", "writes": ["one"]},
                {
                    "id": "second",
                    "kind": "second",
                    "deps": ["first"],
                    "reads": ["one"],
                    "writes": ["two"],
                },
            ]
        )
        entered = threading.Event()
        release = threading.Event()
        second_started = threading.Event()
        registry = NodeRegistry()

        def first_node(_context):
            entered.set()
            release.wait(timeout=2)
            return {"one": 1}

        def second_node(_context):
            second_started.set()
            return {"two": 2}

        registry.register("first", first_node)
        registry.register("second", second_node)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = LocalControlPlane(root, owner_id="owner-one", lease_seconds=30)
            run_id = first.submit(spec, registry)
            self.assertTrue(entered.wait(timeout=1))
            first._mutate_state(
                run_id, lambda state: state.update(lease_expires_at=0.0)
            )
            second = LocalControlPlane(root, owner_id="owner-two", lease_seconds=30)
            second._mutate_state(
                run_id,
                lambda state: claim_lease(
                    state,
                    run_id=run_id,
                    owner_id=second.owner_id,
                    lease_seconds=second.lease_seconds,
                    now=time.time(),
                    takeover=True,
                ),
            )
            release.set()
            first._futures[run_id].result(timeout=2)
            events = first.events(run_id).events
            snapshot = second.inspect(run_id)
            first.close()
            second.close()

        self.assertFalse(second_started.is_set())
        self.assertNotIn("node_completed", {event["event"] for event in events})
        self.assertEqual("running", snapshot.phase)
        self.assertEqual("owner-two", snapshot.owner_id)
        self.assertEqual(2, snapshot.generation)

    def test_prepared_run_can_be_started_by_a_new_control_plane(self):
        spec = graph([{"id": "work", "kind": "work", "writes": ["answer"]}])
        registry = NodeRegistry()
        registry.register("work", lambda context: {"answer": 42})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            creator = LocalControlPlane(root, owner_id="creator")
            run_id = creator.prepare(spec)
            creator.close()
            prepared = LocalControlPlane(root, owner_id="resident")
            self.assertEqual("queued", prepared.inspect(run_id).phase)
            prepared.start(run_id, registry)
            snapshot = prepared.wait(run_id, timeout=2)
            prepared.close()

        self.assertEqual("succeeded", snapshot.phase)
        self.assertEqual(42, snapshot.result["artifacts"]["answer"])

    def test_submit_wait_inspect_and_cursor_events(self):
        spec = graph([{"id": "work", "kind": "work", "writes": ["answer"]}])
        registry = NodeRegistry()
        registry.register("work", lambda context: {"answer": 42})

        with tempfile.TemporaryDirectory() as directory:
            plane = LocalControlPlane(Path(directory), owner_id="controller-test")
            run_id = plane.submit(spec, registry)
            snapshot = plane.wait(run_id, timeout=2)
            first_page = plane.events(run_id)
            second_page = plane.events(run_id, after=first_page.next_cursor)
            plane.close()

        self.assertEqual("succeeded", snapshot.phase)
        self.assertEqual(42, snapshot.result["artifacts"]["answer"])
        self.assertGreater(first_page.next_cursor, 0)
        self.assertEqual((), second_page.events)

    def test_result_checkpoint_and_events_preserve_identical_usage(self):
        spec = graph([{"id": "work", "kind": "work", "writes": ["answer"]}])
        usage = ModelUsage(
            input_tokens=100,
            cached_input_tokens=25,
            output_tokens=10,
            total_tokens=110,
            input_tokens_complete=True,
            cached_input_tokens_complete=True,
            output_tokens_complete=True,
            total_tokens_complete=True,
        )
        registry = NodeRegistry()
        registry.register(
            "work",
            lambda context: NodeOutcome(
                {"answer": 42}, tokens_used=110, usage=usage
            ),
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plane = LocalControlPlane(root, owner_id="controller-test")
            run_id = plane.submit(spec, registry)
            snapshot = plane.wait(run_id, timeout=2)
            events = plane.events(run_id).events
            checkpoint = json.loads(
                (
                    root
                    / "runs"
                    / run_id
                    / "runtime"
                    / "checkpoint.json"
                ).read_text(encoding="utf-8")
            )
            plane.close()

        completed = next(
            item for item in events if item["event"] == "node_completed"
        )
        expected = usage.with_accounted_totals(110, 0.0).to_dict()
        self.assertEqual(expected, snapshot.result["usage"])
        self.assertEqual(expected, checkpoint["usage"])
        self.assertEqual(expected, completed["payload"]["usage"])

    def test_cancel_stops_scheduling_new_nodes(self):
        spec = graph(
            [
                {"id": "first", "kind": "first", "writes": ["one"]},
                {
                    "id": "second",
                    "kind": "second",
                    "deps": ["first"],
                    "reads": ["one"],
                    "writes": ["two"],
                },
            ]
        )
        entered = threading.Event()
        release = threading.Event()
        registry = NodeRegistry()

        def first(context):
            entered.set()
            release.wait(timeout=2)
            return {"one": 1}

        registry.register("first", first)
        registry.register("second", lambda context: {"two": 2})

        with tempfile.TemporaryDirectory() as directory:
            plane = LocalControlPlane(Path(directory), owner_id="controller-test")
            run_id = plane.submit(spec, registry)
            self.assertTrue(entered.wait(timeout=1))
            plane.cancel(run_id)
            release.set()
            snapshot = plane.wait(run_id, timeout=2)
            plane.close()

        self.assertEqual("cancelled", snapshot.phase)
        self.assertEqual("cancelled", snapshot.result["statuses"]["second"])

    def test_failed_run_can_resume_with_new_controller(self):
        spec = graph([{"id": "work", "kind": "work", "writes": ["answer"]}])
        failed_registry = NodeRegistry()
        failed_registry.register("work", lambda context: (_ for _ in ()).throw(ValueError("boom")))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = LocalControlPlane(root, owner_id="controller-one")
            run_id = first.submit(spec, failed_registry)
            failed = first.wait(run_id, timeout=2)
            first.close()

            recovered_registry = NodeRegistry()
            recovered_registry.register("work", lambda context: {"answer": "recovered"})
            second = LocalControlPlane(root, owner_id="controller-two")
            second.resume(run_id, recovered_registry)
            recovered = second.wait(run_id, timeout=2)
            second.close()

        self.assertEqual("failed", failed.phase)
        self.assertEqual("succeeded", recovered.phase)
        self.assertEqual(2, recovered.generation)
        self.assertEqual("recovered", recovered.result["artifacts"]["answer"])

    def test_effect_journal_returns_completed_receipt_without_repeating_effect(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            journal = EffectJournal(Path(directory))

            def effect():
                calls.append("called")
                return {"receipt": "ok"}

            first = journal.execute("charge-1", {"amount": 5}, effect)
            second = journal.execute("charge-1", {"amount": 5}, effect)

            with self.assertRaisesRegex(ContractViolation, "different input"):
                journal.execute("charge-1", {"amount": 6}, effect)

        self.assertEqual(first, second)
        self.assertEqual(["called"], calls)

    def test_effect_failure_becomes_indeterminate(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = EffectJournal(Path(directory))

            def fail():
                raise RuntimeError("unknown external state")

            with self.assertRaises(RuntimeError):
                journal.execute("publish-1", {"version": 1}, fail)
            with self.assertRaisesRegex(EffectIndeterminateError, "reconcile"):
                journal.execute("publish-1", {"version": 1}, fail)

    def test_reconcilable_unknown_outcome_stays_indeterminate_without_replay(self):
        class SimulatedProcessCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as directory:
            journal = EffectJournal(Path(directory))
            calls = []

            def effect():
                calls.append("called")
                raise SimulatedProcessCrash()

            with self.assertRaises(SimulatedProcessCrash):
                journal.execute_reconcilable(
                    "dispatch-1", {"task": "one"}, effect, lambda: None
                )
            with self.assertRaisesRegex(
                EffectIndeterminateError, "could not prove"
            ):
                journal.execute_reconcilable(
                    "dispatch-1", {"task": "one"}, effect, lambda: None
                )
            receipt = journal.inspect("dispatch-1")

        self.assertEqual(["called"], calls)
        self.assertEqual("indeterminate", receipt.status)

    def test_crash_after_prepare_before_request_requires_explicit_recovery(self):
        class SimulatedProcessCrash(BaseException):
            pass

        with tempfile.TemporaryDirectory() as directory:
            journal = EffectJournal(Path(directory))
            calls = []
            original_write = journal._write

            def crash_after_prepare(path, receipt):
                original_write(path, receipt)
                if receipt.status == "started":
                    raise SimulatedProcessCrash()

            with patch.object(journal, "_write", side_effect=crash_after_prepare):
                with self.assertRaises(SimulatedProcessCrash):
                    journal.execute_reconcilable(
                        "dispatch-prepare",
                        {"task": "one"},
                        lambda: calls.append("called"),
                        lambda: None,
                    )
            with self.assertRaises(EffectIndeterminateError):
                journal.execute_reconcilable(
                    "dispatch-prepare",
                    {"task": "one"},
                    lambda: calls.append("called"),
                    lambda: None,
                )

        self.assertEqual([], calls)

    def test_effect_postcondition_failure_preserves_recovery_context(self):
        recovery_context = {
            "workspace_fingerprint": "approved-workspace",
            "protected_snapshot": {"guard.txt": "approved-content"},
        }
        with tempfile.TemporaryDirectory() as directory:
            journal = EffectJournal(Path(directory))

            def reject(_result):
                raise ContractViolation("protected paths changed")

            with self.assertRaisesRegex(ContractViolation, "protected paths changed"):
                journal.execute(
                    "implement-0",
                    {"task_id": "engineering-implement"},
                    lambda: {"summary": "implemented"},
                    reject,
                    recovery_context=recovery_context,
                )
            receipt = journal.inspect("implement-0")

        self.assertEqual("indeterminate", receipt.status)
        self.assertEqual(recovery_context, receipt.recovery_context)

    def test_effect_crash_boundaries_preserve_baseline_and_fail_closed(self):
        class SimulatedProcessCrash(BaseException):
            pass

        def run_crash(crash_point):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                guard = root / "guard.txt"
                guard.write_text("approved", encoding="utf-8")
                recovery_context = {
                    "plan_digest": "approved-plan",
                    "workspace_fingerprint": "approved-workspace",
                    "protected_snapshot": {"guard.txt": "approved"},
                }
                journal = EffectJournal(root / "effects")
                validated = []

                def effect():
                    if crash_point != "after_validation":
                        guard.write_text("unauthorized", encoding="utf-8")
                    if crash_point == "before_agent_return":
                        raise SimulatedProcessCrash()
                    return {"summary": "implemented"}

                def postcondition(_result):
                    if crash_point == "before_validation":
                        raise SimulatedProcessCrash()
                    self.assertEqual("approved", guard.read_text(encoding="utf-8"))
                    validated.append("validated")

                def execute():
                    journal.execute(
                        "implement-0",
                        {"task_id": "engineering-implement"},
                        effect,
                        postcondition,
                        recovery_context=recovery_context,
                    )

                if crash_point == "after_validation":
                    original_write = EffectJournal._write

                    def crash_before_completed(path, receipt):
                        if receipt.status == "completed":
                            raise SimulatedProcessCrash()
                        original_write(path, receipt)

                    with patch.object(
                        journal, "_write", side_effect=crash_before_completed
                    ):
                        with self.assertRaises(SimulatedProcessCrash):
                            execute()
                    guard.write_text("unauthorized", encoding="utf-8")
                else:
                    with self.assertRaises(SimulatedProcessCrash):
                        execute()

                receipt = journal.inspect("implement-0")
                replayed = []
                with self.assertRaisesRegex(EffectIndeterminateError, "reconcile"):
                    journal.execute(
                        "implement-0",
                        {"task_id": "engineering-implement"},
                        lambda: replayed.append("replayed"),
                        recovery_context=recovery_context,
                    )

                self.assertEqual("started", receipt.status)
                self.assertEqual(recovery_context, receipt.recovery_context)
                self.assertEqual([], replayed)
                self.assertEqual(
                    ["validated"] if crash_point == "after_validation" else [],
                    validated,
                )

        for crash_point in (
            "before_agent_return",
            "before_validation",
            "after_validation",
        ):
            with self.subTest(crash_point=crash_point):
                run_crash(crash_point)


if __name__ == "__main__":
    unittest.main()
