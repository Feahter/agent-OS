import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from grapheng import GraphRuntime, ModelUsage, NodeOutcome, NodeRegistry, RetryableNodeError
from grapheng.errors import ContractViolation
from grapheng.events import GraphEvent, JsonlEventSink
from grapheng.evidence import EvidenceProjection, EvidenceSource
from grapheng.index import ProjectionIndex
from tests import test_engineering as engineering_fixtures
from tests import test_evidence_reuse as reuse_fixtures
from tests.test_runtime import spec


class FailedUsageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def runtime(self, handler, *, node_overrides=None, **graph_overrides):
        node = {"id": "work", "kind": "work", "writes": ["value"],
                "estimated_tokens": 5, "estimated_cost_usd": 0.2}
        node.update(node_overrides or {})
        registry = NodeRegistry()
        registry.register("work", handler)
        return GraphRuntime(spec([node], **graph_overrides), registry, self.root)

    def projection(self):
        return EvidenceProjection([EvidenceSource("project", "events", self.root / "events.jsonl")])

    def test_retry_consumption_matches_the_ledger_without_claiming_measurement(self):
        def work(context):
            if context.attempt == 1:
                raise RetryableNodeError("timeout")
            return NodeOutcome({"value": 1}, tokens_used=2, cost_usd=0.1)

        result = self.runtime(work, node_overrides={"retry": {"max_attempts": 2}}).run()
        self.assertTrue(result.success)
        self.assertEqual(7, result.tokens_used)
        self.assertAlmostEqual(0.3, result.cost_usd)
        summary = self.projection().summary()
        self.assertEqual(result.usage.to_dict(), summary["usage"])
        failed = next(r for r in self.projection().snapshot() if r["status"] == "failed")
        self.assertEqual(5, failed["usage"]["total_tokens"])
        self.assertEqual(0.2, failed["usage"]["cost_usd"])
        self.assertFalse(failed["usage"]["total_tokens_complete"])
        self.assertFalse(failed["usage"]["cost_complete"])

    def test_terminal_exceptions_preserve_reservations_as_unknown_usage(self):
        for error in (RetryableNodeError("timeout"), RuntimeError("failed")):
            with self.subTest(error=type(error).__name__):
                def work(_context, error=error):
                    raise error

                result = self.runtime(work).run(run_id="run-" + type(error).__name__)
                record = next(r for r in self.projection().snapshot() if r["run_id"] == result.run_id)
                self.assertFalse(result.success)
                self.assertEqual(result.usage.to_dict(), record["usage"])
                self.assertEqual(5, record["usage"]["total_tokens"])
                self.assertEqual(0.2, record["usage"]["cost_usd"])
                self.assertFalse(record["usage"]["cost_complete"])

    def test_budget_rejection_preserves_measured_usage_without_committed_candidate(self):
        for node_overrides, graph_overrides in (
            ({"max_tokens": 5}, {}),
            ({}, {"max_tokens": 6}),
            ({}, {"max_cost_usd": 0.25}),
        ):
            with self.subTest(node=node_overrides, graph=graph_overrides):
                result = self.runtime(
                    lambda _context: NodeOutcome({"value": "uncommitted"}, tokens_used=7, cost_usd=0.3),
                    node_overrides=node_overrides, **graph_overrides,
                ).run()
                record = next(r for r in self.projection().snapshot() if r["run_id"] == result.run_id)
                self.assertFalse(result.success)
                self.assertNotIn("value", result.artifacts)
                self.assertEqual("budget", record["failure_category"])
                self.assertEqual(result.usage.to_dict(), record["usage"])
                self.assertEqual(7, record["usage"]["total_tokens"])
                self.assertEqual(0.3, record["usage"]["cost_usd"])
                self.assertTrue(record["usage"]["total_tokens_complete"])
                self.assertTrue(record["usage"]["cost_complete"])
                self.assertIsNone(record["candidate_digest"])

    def test_errors_after_settlement_do_not_charge_or_retry_the_attempt_again(self):
        for error in (OSError("commit failed"), RetryableNodeError("commit failed")):
            with self.subTest(error=type(error).__name__):
                runtime = self.runtime(
                    lambda _context: NodeOutcome({"value": 1}, tokens_used=2, cost_usd=0.1),
                    node_overrides={"retry": {"max_attempts": 2}},
                )
                with patch("grapheng.runtime.ArtifactStore.commit_batch", side_effect=error):
                    result = runtime.run()
                record = next(r for r in self.projection().snapshot() if r["run_id"] == result.run_id)
                self.assertFalse(result.success)
                self.assertEqual(1, result.attempts["work"])
                self.assertEqual(2, result.tokens_used)
                self.assertEqual(0.1, result.cost_usd)
                self.assertEqual(result.usage.to_dict(), record["usage"])
                self.assertTrue(record["usage"]["cost_complete"])

    def test_publication_error_overrides_status_without_recounting_completion_usage(self):
        runtime = self.runtime(lambda _context: NodeOutcome({"value": 1}, tokens_used=2, cost_usd=0.1))
        with patch.object(runtime, "_record_publication", side_effect=OSError("publication failed")):
            result = runtime.run()
        summary = self.projection().summary()
        self.assertEqual({"failed": 1}, summary["statuses"])
        self.assertEqual(result.usage.to_dict(), summary["usage"])
        self.assertEqual(2, summary["usage"]["total_tokens"])
        self.assertEqual(0.1, summary["usage"]["cost_usd"])

    def test_old_failures_with_missing_usage_remain_unknown(self):
        sink = JsonlEventSink(self.root / "events.jsonl")
        for attempt, event in ((1, "node_retry"), (2, "node_failed")):
            sink.emit(GraphEvent.create(event, "old-run", "graph", "work", attempt, {"error": "timeout"}))
        usage = self.projection().summary()["usage"]
        self.assertIsNone(usage["total_tokens"])
        self.assertIsNone(usage["cost_usd"])
        self.assertFalse(usage["total_tokens_complete"])
        self.assertFalse(usage["cost_complete"])


class RunSpanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.sources = []

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, events):
        path = self.root / f"events-{len(self.sources)}.jsonl"
        sink = JsonlEventSink(path)
        for event, run_id, second, node, attempt in events:
            sink.emit(GraphEvent(f"2026-10-08T00:00:{second:02d}Z", event, run_id, "graph", node, attempt,
                                 {"usage": ModelUsage.no_call().to_dict()}))
        source = EvidenceSource("project", "events", path)
        self.sources.append(source)
        return source

    def projection(self, sources=None):
        return EvidenceProjection(self.sources if sources is None else sources,
                                  control_path=self.root / "controls.jsonl")

    def test_parallel_nodes_retries_runs_and_duplicate_sources_count_each_run_once(self):
        first = self.source([
            ("run_started", "a", 0, None, None),
            ("node_started", "a", 1, "left", 1), ("node_started", "a", 1, "right", 1),
            ("node_retry", "a", 4, "left", 1), ("node_started", "a", 5, "left", 2),
            ("node_completed", "a", 9, "left", 2), ("node_completed", "a", 9, "right", 1),
            ("run_completed", "a", 10, None, None),
        ])
        self.source([("run_started", "b", 2, None, None), ("run_completed", "b", 7, None, None)])
        projection = self.projection([*self.sources, first])
        clock = projection.summary()["wall_clock"]
        self.assertEqual(15, clock["known_seconds"])
        self.assertEqual(2, clock["known_runs"])
        self.assertEqual(0, clock["unknown_runs"])
        self.assertTrue(clock["complete"])
        self.assertEqual("sum_per_run_spans", clock["aggregation"])
        record = next(r for r in projection.snapshot() if r["node_id"] == "left" and r["attempt"] == 1)
        projection.control(record["attempt_id"], "forget", actor="human")
        self.assertEqual(clock, projection.summary()["wall_clock"])
        self.assertEqual(clock, self.projection(list(reversed([*self.sources, first]))).summary()["wall_clock"])

    def test_resume_only_missing_boundaries_and_empty_sources_preserve_unknown(self):
        empty = self.projection().summary()["wall_clock"]
        self.assertEqual(0, empty["known_seconds"])
        self.assertTrue(empty["complete"])
        self.source([("run_resumed", "resume-only", 3, None, None),
                     ("run_completed", "resume-only", 10, None, None)])
        self.source([("run_started", "unfinished", 0, None, None)])
        clock = self.projection().summary()["wall_clock"]
        self.assertIsNone(clock["known_seconds"])
        self.assertEqual(2, clock["unknown_runs"])
        self.assertFalse(clock["complete"])
        self.source([("run_started", "complete", 0, None, None), ("run_completed", "complete", 4, None, None)])
        clock = self.projection().summary()["wall_clock"]
        self.assertEqual(4, clock["known_seconds"])
        self.assertEqual(1, clock["known_runs"])
        self.assertEqual(2, clock["unknown_runs"])
        self.assertFalse(clock["complete"])

    def test_original_start_spans_resume_and_terminal_replay_does_not_extend_time(self):
        self.source([("run_started", "a", 0, None, None), ("run_resumed", "a", 10, None, None),
                     ("run_completed", "a", 20, None, None), ("run_resumed", "a", 30, None, None),
                     ("run_completed", "a", 40, None, None)])
        self.assertEqual(20, self.projection().summary()["wall_clock"]["known_seconds"])

    def test_resuming_terminal_failure_with_new_work_requires_a_new_completion(self):
        source = self.source([
            ("run_started", "a", 0, None, None), ("node_failed", "a", 9, "work", 1),
            ("run_completed", "a", 10, None, None), ("run_resumed", "a", 20, None, None),
            ("node_started", "a", 21, "work", 2),
        ])
        clock = self.projection().summary()["wall_clock"]
        self.assertFalse(clock["complete"])
        self.assertIsNone(clock["known_seconds"])
        sink = JsonlEventSink(source.path)
        sink.emit(GraphEvent("2026-10-08T00:00:29Z", "node_completed", "a", "graph", "work", 2,
                             {"usage": ModelUsage.no_call().to_dict()}))
        sink.emit(GraphEvent("2026-10-08T00:00:30Z", "run_completed", "a", "graph", None, None, {}))
        clock = self.projection().summary()["wall_clock"]
        self.assertTrue(clock["complete"])
        self.assertEqual(30, clock["known_seconds"])

    def test_disjoint_sources_can_supply_complementary_run_boundaries(self):
        self.source([("run_started", "a", 0, None, None)])
        self.source([("run_completed", "a", 10, None, None)])
        clock = self.projection().summary()["wall_clock"]
        self.assertEqual(10, clock["known_seconds"])
        self.assertTrue(clock["complete"])

    def test_older_complete_source_cannot_hide_a_resumed_unfinished_attempt(self):
        first = self.source([("run_started", "a", 0, None, None), ("node_failed", "a", 9, "work", 1),
                             ("run_completed", "a", 10, None, None)])
        later = self.source([("run_resumed", "a", 20, None, None), ("node_started", "a", 21, "work", 2)])
        for sources in ([first, later], [later, first]):
            with self.subTest(order=sources):
                clock = self.projection(sources).summary()["wall_clock"]
                self.assertFalse(clock["complete"])
                self.assertIsNone(clock["known_seconds"])
        # Without an activity timestamp, ordering across separate sources is unknown.
        events = list(JsonlEventSink(later.path).read())
        events[1].pop("time")
        later.path.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.assertFalse(self.projection().summary()["wall_clock"]["complete"])

    def test_invalid_conflicting_or_inverted_boundaries_are_rejected(self):
        source = self.source([("run_started", "a", 0, None, None), ("run_completed", "a", 10, None, None)])
        original = list(JsonlEventSink(source.path).read())
        for timestamp in ("2026-10-08T00:00:00", "2026-10-09T00:00:10", "invalid", True, 3,
                          "2026-10-07T23:59:59Z"):
            with self.subTest(timestamp=timestamp):
                changed = [{**event, "time": timestamp} if event["event"] == "run_completed" else event
                           for event in original]
                source.path.write_text("".join(json.dumps(event) + "\n" for event in changed))
                with self.assertRaises(ContractViolation):
                    self.projection().summary()
        source.path.write_text("".join(json.dumps(event) + "\n" for event in original))
        other = self.source([("run_started", "a", 1, None, None), ("run_completed", "a", 10, None, None)])
        for sources in ([source, other], [other, source]):
            with self.subTest(order=sources), self.assertRaisesRegex(ContractViolation, "conflicting"):
                self.projection(sources).summary()

    def test_missing_timestamp_is_unknown_and_timezones_are_normalized(self):
        source = self.source([("run_started", "a", 0, None, None), ("run_completed", "a", 10, None, None)])
        events = list(JsonlEventSink(source.path).read())
        events[0]["time"] = "2026-10-08T08:00:00+08:00"
        source.path.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.assertEqual(10, self.projection().summary()["wall_clock"]["known_seconds"])
        events[0].pop("time")
        source.path.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.assertIsNone(self.projection().summary()["wall_clock"]["known_seconds"])
        self.assertFalse(self.projection().summary()["wall_clock"]["complete"])

    def test_completion_cannot_precede_the_activity_it_settles(self):
        self.source([("run_started", "a", 0, None, None), ("node_completed", "a", 11, "work", 1),
                     ("run_completed", "a", 10, None, None)])
        with self.assertRaisesRegex(ContractViolation, "precedes node activity"):
            self.projection().summary()

    def test_scope_is_part_of_run_identity(self):
        source = self.source([("run_started", "a", 0, None, None), ("run_completed", "a", 10, None, None)])
        clock = self.projection([source, replace(source, scope="other")]).summary()["wall_clock"]
        self.assertEqual(20, clock["known_seconds"])
        self.assertEqual(2, clock["known_runs"])

    def test_updated_boundaries_are_read_with_cached_attempts_and_export_does_not_leak_times(self):
        source = self.source([("run_started", "a", 0, None, None), ("node_completed", "a", 9, "work", 1),
                              ("run_completed", "a", 10, None, None)])
        index = ProjectionIndex(self.root / "projection.sqlite")
        projection = self.projection()
        projection.index = index
        try:
            self.assertEqual(10, projection.summary()["wall_clock"]["known_seconds"])
            self.assertEqual(10, projection.summary()["wall_clock"]["known_seconds"])
            self.assertGreater(index.hits, 0)
            source.path.write_text(source.path.read_text().replace("00:00:10Z", "00:00:20Z"))
            self.assertEqual(20, projection.summary()["wall_clock"]["known_seconds"])
            self.assertNotIn("2026-10-08", json.dumps(projection.portable()))
            self.assertNotIn("started_at", json.dumps(projection.portable()))
        finally:
            index.close()


class PublicationSpanTests(unittest.TestCase):
    setUp = reuse_fixtures.QualifiedEvidenceTests.setUp
    tearDown = reuse_fixtures.QualifiedEvidenceTests.tearDown
    certification = reuse_fixtures.QualifiedEvidenceTests.certification
    publish = reuse_fixtures.QualifiedEvidenceTests.publish

    def test_publications_attach_to_event_runs_without_adding_unknown_or_duplicate_time(self):
        for sources in (self.sources, tuple(reversed(self.sources)), (*self.sources, *self.sources)):
            with self.subTest(sources=sources):
                clock = EvidenceProjection(sources).summary()["wall_clock"]
                self.assertEqual(1, clock["known_runs"])
                self.assertEqual(0, clock["unknown_runs"])
                self.assertTrue(clock["complete"])
                self.assertGreater(clock["known_seconds"], 0)
        clock = EvidenceProjection([self.sources[1]]).summary()["wall_clock"]
        self.assertIsNone(clock["known_seconds"])
        self.assertEqual(1, clock["unknown_runs"])
        self.assertFalse(clock["complete"])


class EngineeringSpanTests(unittest.TestCase):
    setUp = engineering_fixtures.EngineeringTests.setUp
    tearDown = engineering_fixtures.EngineeringTests.tearDown
    policy = engineering_fixtures.EngineeringTests.policy
    workflow = engineering_fixtures.EngineeringTests.workflow

    def prepare_with_clock(self):
        workflow, executor = self.workflow()
        self.clock = 100
        self.elapsed = 0
        workflow._clock = lambda: self.clock
        workflow._monotonic = lambda: self.elapsed
        execute = executor.execute

        def timed_execute(request):
            self.elapsed += 1
            return execute(request)

        executor.execute = timed_execute
        return workflow, workflow.prepare("Change safely")

    def projection(self, workflow):
        source = EvidenceSource("project", "engineering", workflow.report_path)
        return EvidenceProjection([source, source])

    def test_preparation_approval_pause_and_resume_are_counted_without_check_duplication(self):
        workflow, plan = self.prepare_with_clock()
        self.assertEqual(2, plan.preparation_usage["elapsed_seconds"])
        self.clock = 110
        paused = workflow.execute("human", plan.digest, control_probe=lambda: (
            "pause" if workflow.status().get("check_receipts") else None
        ))
        self.assertEqual("paused", paused["phase"])
        # Paused status can be supplied without a terminal report.
        workflow.report_path.write_text(json.dumps(paused))
        clock = self.projection(workflow).summary()["wall_clock"]
        self.assertFalse(clock["complete"])
        self.assertIsNone(clock["known_seconds"])
        self.clock = 150
        report = workflow.execute("human", plan.digest)
        self.assertEqual(150, report["started_at"])
        self.assertEqual(150, report["finished_at"])
        self.assertEqual(2, len(report["check_receipts"]))
        summary = self.projection(workflow).summary()
        self.assertEqual(report["usage"], summary["usage"])
        self.assertEqual(52, summary["wall_clock"]["known_seconds"])
        self.assertEqual(1, summary["wall_clock"]["known_runs"])
        self.assertEqual({"preparation_plus_plan_to_finish": 1}, summary["wall_clock"]["bases"])
        self.assertTrue(summary["wall_clock"]["complete"])

    def test_terminal_missing_invalid_and_stale_finish_times_are_distinguished(self):
        workflow, plan = self.prepare_with_clock()
        self.clock = 110
        report = workflow.execute("human", plan.digest)
        for phase in ("succeeded", "failed", "cancelled"):
            with self.subTest(phase=phase):
                workflow.report_path.write_text(json.dumps({**report, "phase": phase}))
                self.assertEqual(12, self.projection(workflow).summary()["wall_clock"]["known_seconds"])
        for finish in (None, True, -1, 99, "110"):
            with self.subTest(finish=finish):
                workflow.report_path.write_text(json.dumps({**report, "finished_at": finish}))
                if finish is None:
                    self.assertFalse(self.projection(workflow).summary()["wall_clock"]["complete"])
                else:
                    with self.assertRaises(ContractViolation):
                        self.projection(workflow).summary()
        workflow.report_path.write_text(json.dumps({**report, "phase": "paused"}))
        self.assertFalse(self.projection(workflow).summary()["wall_clock"]["complete"])
