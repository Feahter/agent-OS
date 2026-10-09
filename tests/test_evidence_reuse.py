import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from grapheng import (
    AgentExecution,
    AgentRequest,
    AgentResult,
    ContextCompiler,
    ContextPolicy,
    ContractViolation,
    ExecutorRegistry,
    GraphRuntime,
    NodeOutcome,
    NodeRegistry,
    VerifiedArtifactCache,
    VerifiedResultPublisher,
)
from grapheng._store import encode_json
from grapheng.cli import _dispatch, main
from grapheng.distribution import AgentOSDistribution, DiagnosticCheck
from grapheng.events import GraphEvent, JsonlEventSink
from grapheng.evidence import EvidenceProjection, EvidenceSource
from grapheng.index import ProjectionIndex
from grapheng.routing import ExecutorProfile
from tests import test_engineering as engineering_fixtures
from tests import test_evidence as evidence_fixtures
from tests import test_publication as publication_fixtures
from tests.test_agents import MemoryExecutor
from tests.test_distribution import PROJECT_ROOT, fake_probe_environment
from tests.test_runtime import spec


class QualifiedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.work_dir = self.root / "run"
        self.executor = publication_fixtures.MemoryExecutor()
        self.publish("run-1")
        self.sources = tuple(EvidenceSource(
            "scope-a", kind, self.work_dir / filename, "fix", "policy-1", 100,
            "public", (("memory", "1.0"),),
        ) for kind, filename in (("events", "events.jsonl"), ("publications", "verified-publications.json")))
        self.registry = ExecutorRegistry()
        self.current_executor = MemoryExecutor("memory", features=(
            "structured_output", "tool_policy", "model_selection", "token_budget", "cost_budget", "reasoning_control",
        ))
        self.registry.register(self.current_executor, ExecutorProfile(
            "provider", estimated_cost_usd=0.2, estimated_latency_seconds=2,
        ))
        self.certified = True
        self.version = "1.0"
        self.access_log = self.root / "access.jsonl"
        self.controls = self.root / "controls.jsonl"
        self.projection = EvidenceProjection(
            self.sources, executors=self.registry, certification=self.certification,
            control_path=self.controls, access_log=self.access_log, clock=lambda: 50,
        )
        self.request = AgentRequest(
            "next", "private task prompt", {}, ("answer",), self.workspace,
            task_type="fix", reuse_scope="scope-a", tools=("read",),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def certification(self):
        return (DiagnosticCheck("adapter:memory", "pass" if self.certified else "warn", "probe", {
            "support_status": "verified" if self.certified else "unverified_version", "version": self.version,
        }),)

    def publish(self, run_id):
        graph = publication_fixtures.publication_graph()
        cache = VerifiedArtifactCache(self.root / "reuse" / run_id)
        registry = publication_fixtures.registry_for(graph, self.executor, cache, self.workspace)
        GraphRuntime(graph, registry, self.work_dir / run_id, event_sink=JsonlEventSink(self.work_dir / "events.jsonl"), verified_result_publisher=VerifiedResultPublisher(
            cache, self.work_dir / "verified-publications.json",
        )).run(run_id=run_id)

    def search(self, **overrides):
        return self.projection.search(
            scope="scope-a", task_type="fix", policy_digest="policy-1",
            request=overrides.pop("request", self.request), **overrides,
        )

    def test_certified_retrieval_and_injection_use_current_eligibility_without_execution(self):
        selection = self.search()
        self.assertEqual(["answer"], [item["node_id"] for item in selection["items"]])
        self.assertTrue(selection["eligibility_checked"])
        compiled = ContextCompiler().compile_with_evidence(
            self.request, ContextPolicy(output_contract="prompt"), self.projection,
            scope="scope-a", task_type="fix", policy_digest="policy-1",
        )
        self.assertIn(selection["items"][0]["attempt_id"], compiled.body)
        self.assertEqual([], self.current_executor.requests)
        self.assertNotIn(self.request.prompt, self.access_log.read_text())
        self.assertEqual("evidence_context_injected", tuple(JsonlEventSink(self.access_log).read())[-1]["event"])

    def test_certification_is_rechecked_after_cache_hit_and_version_drift(self):
        index = ProjectionIndex(self.root / "projection.sqlite")
        self.projection.index = index
        try:
            self.assertTrue(self.search()["items"])
            self.assertTrue(self.search()["items"])
            self.assertGreater(index.hits, 0)
            self.certified = False
            self.assertEqual([], self.search()["items"])
            self.certified = True
            self.version = "2.0"
            self.assertEqual([], self.search()["items"])
        finally:
            index.close()

    def test_cached_sources_cannot_keep_old_qualification_metadata(self):
        index = ProjectionIndex(self.root / "projection.sqlite")
        self.projection.index = index
        try:
            self.assertTrue(self.search()["items"])
            self.assertTrue(self.search()["items"])
            for change in ({"executor_versions": (("memory", "1.1"),)}, {"data_classification": "internal"}):
                with self.subTest(change=change):
                    self.projection.sources = tuple(replace(source, **change) for source in self.sources)
                    self.assertEqual([], self.search()["items"])
        finally:
            index.close()

    def test_qualified_retrieval_keeps_bounds_and_rejects_incomplete_certification(self):
        self.publish("run-2")
        self.assertEqual(1, len(self.search(max_items=1)["items"]))
        small = self.search(max_bytes=128)
        self.assertEqual([], small["items"])
        self.assertLessEqual(len(encode_json(small).encode("utf-8")), 128)
        self.assertEqual("current_source_fixed_policy", small["fallback"])
        self.projection.certification = lambda: (DiagnosticCheck(
            "adapter:memory", "pass", "probe", {"support_status": "verified"},
        ),)
        self.assertEqual([], self.search()["items"])

    def test_capabilities_tools_data_budget_and_scope_requirements_fail_closed(self):
        cases = (
            replace(self.request, tools=("write",)),
            replace(self.request, data_classification="restricted"),
            replace(self.request, max_cost_usd=0.1),
            replace(self.request, timeout_seconds=1),
            replace(self.request, reuse_allowed=False),
            replace(self.request, reuse_scope="another-scope"),
        )
        for request in cases:
            with self.subTest(request=request):
                self.assertEqual([], self.search(request=request)["items"])
        self.assertEqual([], self.search(required_features=("streaming",))["items"])
        weak = ExecutorRegistry()
        weak.register(MemoryExecutor("memory"))
        self.projection.executors = weak
        for request in (
            replace(self.request, model="model"), replace(self.request, max_tokens=10),
            replace(self.request, max_cost_usd=1), replace(self.request, reasoning_effort="high"),
        ):
            with self.subTest(request=request):
                self.assertEqual([], self.search(request=request)["items"])
        self.assertEqual([], self.current_executor.requests)

    def test_missing_host_qualification_and_sensitive_evidence_cannot_be_injected(self):
        for sources in (
            tuple(replace(source, executor_versions=()) for source in self.sources),
            tuple(replace(source, data_classification=None) for source in self.sources),
            tuple(replace(source, data_classification="restricted") for source in self.sources),
        ):
            with self.subTest(sources=sources):
                self.projection.sources = sources
                self.assertEqual([], self.search()["items"])
        self.projection.sources = self.sources
        self.projection.certification = None
        self.assertEqual([], self.search()["items"])
        compiled = ContextCompiler().compile_with_evidence(
            self.request, ContextPolicy(output_contract="prompt"), self.projection,
            scope="scope-a", task_type="fix", policy_digest="policy-1",
        )
        self.assertIn("current_source_fixed_policy", compiled.body)
        self.assertNotIn("candidate_digest", compiled.body)

    def test_conflicting_source_metadata_cannot_overwrite_eligibility(self):
        self.projection.sources = (self.sources[0], replace(self.sources[1], data_classification="restricted"))
        with self.assertRaisesRegex(ContractViolation, "conflicting"):
            self.search()

    def test_publication_cannot_replace_a_different_runtime_candidate_or_executor(self):
        path = self.root / "conflicting-events.jsonl"
        for field, value in (("executor_id", "other"), ("candidate_digest", "b" * 64)):
            events = list(JsonlEventSink(self.sources[0].path).read())
            for event in events:
                if event["event"] == "node_completed" and event["node_id"] == "answer":
                    event["payload"][field] = value
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            self.projection.sources = (replace(self.sources[0], path=path), self.sources[1])
            with self.subTest(field=field), self.assertRaisesRegex(ContractViolation, "binding"):
                self.search()

    def test_correction_reuses_existing_verified_candidate_and_forget_invalidates_it(self):
        self.publish("run-2")
        answers = {item["run_id"]: item for item in self.projection.snapshot() if item["node_id"] == "answer"}
        original, substitute = answers["run-1"]["attempt_id"], answers["run-2"]["attempt_id"]
        before = {source.path: source.path.read_bytes() for source in self.sources}
        receipt = self.projection.control(original, "correct", actor="human",
                                          correction={"note": "private correction"}, replacement_attempt_id=substitute)
        self.assertEqual(substitute, receipt["metadata"]["replacement_attempt_id"])
        items = self.search()["items"]
        self.assertEqual([substitute], [item["attempt_id"] for item in items])
        self.assertEqual(original, items[0]["corrects_attempt_id"])
        self.assertTrue(items[0]["verified"])
        self.assertNotIn("private correction", self.controls.read_text())
        self.assertEqual(before, {source.path: source.path.read_bytes() for source in self.sources})
        self.projection.control(original, "forget", actor="human")
        self.assertEqual([], self.search()["items"])
        self.assertEqual(2, sum(not item["available"] for item in self.projection.snapshot(include_unavailable=True)))

    def test_withdrawal_propagates_across_corrected_chain_and_direct_parent(self):
        self.publish("run-2")
        self.publish("run-3")
        answers = {item["run_id"]: item["attempt_id"] for item in self.projection.snapshot()
                   if item["node_id"] == "answer"}
        one, two, three = (answers["run-" + str(number)] for number in (1, 2, 3))
        for action in ("forget", "revoke"):
            with self.subTest(action=action):
                self.projection.control_path = self.root / (action + "-chain.jsonl")
                self.projection.control(two, "link", actor="human", parent_attempt_id=one)
                self.projection.control(one, "correct", actor="human", correction={"note": "replace"},
                                        replacement_attempt_id=two)
                self.assertIn(two, {item["attempt_id"] for item in self.search()["items"]})
                self.projection.control(two, "correct", actor="human", correction={"note": "replace again"},
                                        replacement_attempt_id=three)
                self.assertEqual([three], [item["attempt_id"] for item in self.search()["items"]])
                self.projection.control(one, action, actor="human")
                self.assertEqual([], self.search()["items"])
                withdrawn = [item for item in self.projection.snapshot(include_unavailable=True)
                             if item["node_id"] == "answer"]
                self.assertTrue(all(item["invalidated"] for item in withdrawn))

    def test_parent_and_correction_cycle_is_rejected_before_history_write(self):
        self.publish("run-2")
        answers = {item["run_id"]: item["attempt_id"] for item in self.projection.snapshot()
                   if item["node_id"] == "answer"}
        one, two = answers["run-1"], answers["run-2"]
        self.projection.control(one, "link", actor="human", parent_attempt_id=two)
        before = self.controls.read_bytes()
        with self.assertRaisesRegex(ContractViolation, "cycle"):
            self.projection.control(one, "correct", actor="human", correction={"note": "replace"},
                                    replacement_attempt_id=two)
        self.assertEqual(before, self.controls.read_bytes())

    def test_omitting_original_source_cannot_restore_a_forgotten_replacement(self):
        self.publish("run-2")
        answers = {item["run_id"]: item["attempt_id"] for item in self.projection.snapshot()
                   if item["node_id"] == "answer"}
        self.projection.control(answers["run-1"], "correct", actor="human", correction={"note": "replace"},
                                replacement_attempt_id=answers["run-2"])
        self.projection.control(answers["run-1"], "forget", actor="human")
        document = json.loads(self.sources[1].path.read_text())
        document["publications"] = [item for item in document["publications"] if item["run_id"] == "run-2"]
        path = self.root / "subset-publications.json"
        path.write_text(json.dumps(document))
        self.projection.sources = (replace(self.sources[1], path=path),)
        with self.assertRaisesRegex(ContractViolation, "missing original"):
            self.search()

    def test_publication_only_accounting_preserves_unknown_completeness(self):
        runtime = self.projection.summary()
        reverse = EvidenceProjection(tuple(reversed(self.sources))).summary()
        self.assertEqual(runtime["usage"], reverse["usage"])
        self.assertEqual(7, runtime["usage"]["total_tokens"])
        self.assertEqual(0.2, runtime["usage"]["cost_usd"])
        self.assertTrue(runtime["usage"]["cost_complete"])
        self.assertTrue(runtime["usage"]["total_tokens_complete"])
        legacy = EvidenceProjection([self.sources[1]]).summary()["usage"]
        self.assertEqual(7, legacy["total_tokens"])
        self.assertEqual(0.2, legacy["cost_usd"])
        self.assertFalse(legacy["cost_complete"])
        self.assertFalse(legacy["total_tokens_complete"])

    def test_correction_rejects_unverified_cross_scope_missing_and_cyclic_replacements(self):
        answer = self.search()["items"][0]
        seed = next(item for item in self.projection.snapshot() if item["node_id"] == "seed")
        for replacement in (seed["attempt_id"], "missing", answer["attempt_id"]):
            with self.subTest(replacement=replacement), self.assertRaises(ContractViolation):
                self.projection.control(answer["attempt_id"], "correct", actor="human",
                                        correction={"note": "replace"}, replacement_attempt_id=replacement)
        foreign = tuple(replace(source, scope="scope-b") for source in self.sources)
        self.projection.sources = (*self.sources, *foreign)
        other = next(item for item in self.projection.snapshot() if item["scope"] == "scope-b" and item["verified"])
        with self.assertRaisesRegex(ContractViolation, "incompatible"):
            self.projection.control(answer["attempt_id"], "correct", actor="human",
                                    correction={"note": "replace"}, replacement_attempt_id=other["attempt_id"])
        self.assertFalse(self.controls.exists())

    def test_actual_probe_detects_drift_and_only_calls_help_and_version(self):
        base = AgentOSDistribution(PROJECT_ROOT)
        which, runner, calls = fake_probe_environment(base)
        version = next(item for item in base.compatibility_matrix()["adapters"] if item["executor_id"] == "codex")["support"]["verified_versions"][0]["version"]
        path = self.root / "codex-events.jsonl"
        JsonlEventSink(path).emit(GraphEvent.create("node_completed", "codex-run", "graph", "answer", 1, {
            "executor_id": "codex", "candidate_digest": "a" * 64,
        }))
        source = EvidenceSource("scope-a", "events", path, "fix", "policy-1", 100,
                                "public", (("codex", version),))
        registry = ExecutorRegistry()
        executor = MemoryExecutor("codex")
        registry.register(executor)
        distribution = AgentOSDistribution(PROJECT_ROOT, which=which, runner=runner)
        projection = EvidenceProjection([source], executors=registry, certification=distribution.executor_checks, clock=lambda: 50)
        args = {"scope": "scope-a", "task_type": "fix", "policy_digest": "policy-1", "request": self.request}
        with patch("platform.machine", return_value="arm64"), patch("platform.system", return_value="Darwin"):
            self.assertEqual(1, len(projection.search(**args)["items"]))
            distribution._which = lambda _command: None
            self.assertEqual([], projection.search(**args)["items"])
        self.assertTrue(all("--help" in command or command[-1] == "--version" for command in calls))
        self.assertEqual([], executor.requests)

    def test_cli_loads_host_request_and_summary_without_model_calls(self):
        path = self.root / "request.json"
        path.write_text(json.dumps({
            "task_id": "next", "prompt": "private task prompt", "inputs": {}, "output_keys": ["answer"],
            "workspace": str(self.workspace), "task_type": "fix", "reuse_scope": "scope-a", "tools": ["read"],
        }))
        args = ["evidence", "search", "--source", "publications", str(self.sources[1].path),
                "--scope", "scope-a", "--task-type", "fix", "--policy-digest", "policy-1",
                "--expires-at", "99999999999", "--request", str(path),
                "--data-classification", "public", "--executor-version", "memory", "1.0"]
        output = io.StringIO()
        with patch("grapheng.cli.discover_local_executors", return_value=self.registry), patch.object(
            AgentOSDistribution, "executor_checks", side_effect=self.certification,
        ), contextlib.redirect_stdout(output):
            self.assertEqual(0, _dispatch(args))
        self.assertEqual(1, len(json.loads(output.getvalue())["items"]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, _dispatch(["evidence", "summary", "--scope", "scope-a", "--source", "events", str(self.sources[0].path)]))
        self.assertEqual("not_evaluated", json.loads(output.getvalue())["roi_admission"])
        path.write_text('{"unexpected": true}')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, main(args))
        self.assertEqual([], self.current_executor.requests)

    def test_cli_invalid_request_types_report_contract_error_without_traceback(self):
        path = self.root / "invalid-request.json"
        request = {"task_id": "next", "prompt": "private task prompt", "inputs": {},
                   "output_keys": ["answer"], "workspace": str(self.workspace)}
        args = ["evidence", "search", "--scope", "scope-a", "--policy-digest", "policy-1", "--request", str(path)]
        for fields in (
            {"task_id": None}, {"prompt": []}, {"inputs": []}, {"output_keys": [1]}, {"tools": "read"},
            {"workspace": 1}, {"timeout_seconds": None}, {"max_tokens": True}, {"max_cost_usd": "1"},
            {"data_classification": []}, {"model": 1}, {"task_type": None}, {"model_family": None},
            {"reuse_scope": None}, {"reuse_allowed": "true"}, {"reasoning_effort": {}},
        ):
            path.write_text(json.dumps({**request, **fields}))
            error = io.StringIO()
            with self.subTest(fields=fields), contextlib.redirect_stderr(error):
                self.assertEqual(2, main(args))
                self.assertNotIn("Traceback", error.getvalue())


class EvidenceAccountingTests(unittest.TestCase):
    setUp = evidence_fixtures.EvidenceTests.setUp
    tearDown = evidence_fixtures.EvidenceTests.tearDown
    attempts = evidence_fixtures.EvidenceTests.attempts

    def test_retry_parent_comes_from_runtime_without_operator_link(self):
        attempts = self.attempts()
        self.assertEqual(attempts[1]["attempt_id"], attempts[2]["parent_attempt_id"])
        self.assertEqual("runtime", attempts[2]["relation_origin"])

    def test_summary_keeps_failed_usage_unknown_and_withdrawn_attempts_in_accounting(self):
        before = self.projection.summary()
        self.assertEqual(3, before["attempts"])
        self.assertEqual({"completed": 1, "failed": 1, "rejected": 1}, before["statuses"])
        self.assertEqual(2, before["usage"]["total_tokens"])
        self.assertEqual(0.1, before["usage"]["cost_usd"])
        self.assertFalse(before["usage"]["cost_complete"])
        self.assertFalse(before["usage"]["total_tokens_complete"])
        self.projection.control(self.attempts()[1]["attempt_id"], "forget", actor="human")
        after = self.projection.summary()
        self.assertEqual(before["usage"], after["usage"])
        self.assertEqual(before["statuses"], after["statuses"])
        self.assertEqual(1, after["available"])

    def test_empty_unknown_and_interrupted_summaries_keep_distinct_measurement_state(self):
        empty = EvidenceProjection([]).summary()
        self.assertEqual(0, empty["attempts"])
        self.assertEqual(0, empty["usage"]["total_tokens"])
        self.assertTrue(empty["usage"]["cost_complete"])
        path = self.root / "unknown-events.jsonl"
        sink = JsonlEventSink(path)
        sink.emit(GraphEvent.create("node_failed", "unknown-run", "graph", "work", 1, {"reason": "timeout"}))
        projection = EvidenceProjection([EvidenceSource("scope-a", "events", path)])
        unknown = projection.summary()["usage"]
        self.assertIsNone(unknown["total_tokens"])
        self.assertIsNone(unknown["cost_usd"])
        self.assertFalse(unknown["cost_complete"])
        self.assertFalse(unknown["total_tokens_complete"])
        sink.emit(GraphEvent.create("node_interrupted", "unknown-run", "graph", "work", 2, {
            "reserved_tokens": 5, "reserved_cost_usd": 0.3,
        }))
        interrupted = projection.summary()["usage"]
        self.assertEqual(5, interrupted["total_tokens"])
        self.assertEqual(0.3, interrupted["cost_usd"])
        self.assertFalse(interrupted["cost_complete"])
        self.assertFalse(interrupted["total_tokens_complete"])

    def test_event_metadata_cannot_forge_candidate_or_executor_binding(self):
        request = AgentRequest("actual", "prompt", {}, ("answer",), self.root)
        result = AgentResult("actual", {"answer": "private output"}, "done", tokens_used=1)
        registry = NodeRegistry()
        registry.register("work", lambda _context: NodeOutcome(
            result.outputs, tokens_used=1, metadata={"executor_id": "forged", "candidate_digest": "a" * 64},
            agent_execution=AgentExecution(request, result), usage=result.usage,
        ))
        directory = self.root / "agent-run"
        GraphRuntime(spec([{"id": "agent", "kind": "work", "writes": ["answer"]}]), registry, directory).run()
        record = EvidenceProjection([EvidenceSource("scope-a", "events", directory / "events.jsonl")]).snapshot()[0]
        self.assertEqual("actual", record["executor_id"])
        self.assertNotEqual("a" * 64, record["candidate_digest"])
        self.assertNotIn("private output", encode_json(record))


class EngineeringAccountingTests(unittest.TestCase):
    setUp = engineering_fixtures.EngineeringTests.setUp
    tearDown = engineering_fixtures.EngineeringTests.tearDown
    policy = engineering_fixtures.EngineeringTests.policy
    workflow = engineering_fixtures.EngineeringTests.workflow
    prepared = engineering_fixtures.EngineeringTests.prepared

    def test_check_records_do_not_double_count_the_engineering_run(self):
        workflow, _, plan = self.prepared()
        report = workflow.execute("human", plan.digest)
        projection = EvidenceProjection([EvidenceSource("project", "engineering", workflow.report_path)])
        summary = projection.summary()
        self.assertEqual(report["usage"], summary["usage"])
        self.assertEqual(1 + len(report["check_receipts"]), summary["attempts"])
        self.assertEqual("not_evaluated", summary["roi_admission"])
        self.assertTrue(all(not record["independent"] for record in projection.snapshot()))
