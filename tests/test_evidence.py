import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from grapheng import (
    AgentRequest,
    ContextBudgetExceeded,
    ContextCompiler,
    ContextPolicy,
    ContractViolation,
    GraphRuntime,
    NodeOutcome,
    NodeRegistry,
    RetryableNodeError,
    RSILoop,
    RSIOptimizationLab,
    VerifiedArtifactCache,
    VerifiedResultPublisher,
)
from grapheng._store import encode_json
from grapheng.cli import _dispatch
from grapheng.events import JsonlEventSink
from grapheng.evidence import EvidenceProjection, EvidenceSource
from grapheng.index import ProjectionIndex
from tests import test_engineering as engineering_fixtures
from tests import test_publication as publication_fixtures
from tests.test_learning import observation
from tests.test_runtime import spec


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime_dir = self.root / "runtime"
        registry = NodeRegistry()

        def work(context):
            if context.attempt == 1:
                raise RetryableNodeError("TimeoutError: try again")
            return NodeOutcome({"value": "private-code"}, tokens_used=2, cost_usd=0.1,
                               metadata={"origin": "runtime", "prompt": "/private/project"})

        registry.register("work", work)
        registry.register("deny", lambda context: self.fail("gate denied"))
        graph = spec([
            {"id": "work", "kind": "work", "writes": ["value"], "retry": {"max_attempts": 2}},
            {"id": "deny", "kind": "deny", "gate": "approval"},
        ])
        GraphRuntime(graph, registry, self.runtime_dir).run(run_id="run-a")
        self.source = EvidenceSource("scope-a", "events", self.runtime_dir / "events.jsonl", "fix", "policy-1", 100)
        self.controls = self.root / "evidence-controls.jsonl"
        self.access_log = self.root / "evidence-access.jsonl"
        self.projection = EvidenceProjection([self.source], control_path=self.controls,
                                             access_log=self.access_log, clock=lambda: 50)

    def tearDown(self):
        self.temporary.cleanup()

    def attempts(self):
        return {item["attempt"]: item for item in self.projection.snapshot() if item["node_id"] == "work"}

    def test_existing_events_keep_failed_retried_and_rejected_attempts(self):
        before = self.source.path.read_bytes()
        records = self.projection.snapshot()
        self.assertEqual({"failed", "completed", "rejected"}, {r["status"] for r in records})
        self.assertEqual("timeout", self.attempts()[1]["failure_category"])
        self.assertEqual(records, self.projection.snapshot())
        self.assertEqual(before, self.source.path.read_bytes())
        serialized = encode_json(records)
        self.assertNotIn("private-code", serialized)
        self.assertNotIn("/private/project", serialized)
        self.assertTrue(all(not r["verified"] for r in records))
        self.assertIsNone(self.attempts()[2]["candidate_digest"])

    def test_search_is_bounded_and_falls_back_on_incompatible_or_expired_evidence(self):
        for kwargs in ({"scope": "scope-b"}, {"task_type": "test"}, {"policy_digest": "policy-2"}):
            args = {"scope": "scope-a", "task_type": "fix", "policy_digest": "policy-1", **kwargs}
            self.assertEqual([], self.projection.search(**args)["items"])
        found = self.projection.search(scope="scope-a", task_type="fix", policy_digest="policy-1", max_items=2)
        self.assertEqual(2, len(found["items"]))
        self.assertEqual(None, found["fallback"])
        small = self.projection.search(scope="scope-a", task_type="fix", policy_digest="policy-1", max_bytes=128)
        self.assertLessEqual(len(encode_json(small).encode()), 128)
        self.assertEqual("current_source_fixed_policy", small["fallback"])
        expired = EvidenceProjection([self.source], clock=lambda: 100)
        self.assertEqual([], expired.search(scope="scope-a", task_type="fix", policy_digest="policy-1")["items"])
        with self.assertRaises(ContractViolation):
            self.projection.search(scope="scope-a", task_type="fix", policy_digest="policy-1", max_items=101)

    def test_relations_reject_missing_parents_cross_scope_and_cycles(self):
        attempts = self.attempts()
        one, two = attempts[1]["attempt_id"], attempts[2]["attempt_id"]
        with self.assertRaisesRegex(ContractViolation, "missing parent"):
            self.projection.control(two, "link", actor="human", parent_attempt_id="missing")
        self.projection.control(two, "link", actor="human", parent_attempt_id=one)
        self.assertEqual(one, self.attempts()[2]["parent_attempt_id"])
        with self.assertRaisesRegex(ContractViolation, "cycle"):
            self.projection.control(one, "link", actor="human", parent_attempt_id=two)
        foreign = EvidenceSource("scope-b", "events", self.source.path, "fix", "policy-1", 100)
        projection = EvidenceProjection([self.source, foreign], control_path=self.controls)
        other = next(r for r in projection.snapshot() if r["scope"] == "scope-b")
        with self.assertRaisesRegex(ContractViolation, "crosses scope"):
            projection.control(one, "link", actor="human", parent_attempt_id=other["attempt_id"])

    def test_forgetting_parent_invalidates_descendants_and_cached_search(self):
        index = ProjectionIndex(self.root / "projection.sqlite")
        projection = EvidenceProjection([self.source], index=index, control_path=self.controls, clock=lambda: 50)
        try:
            one, two = self.attempts()[1]["attempt_id"], self.attempts()[2]["attempt_id"]
            projection.control(two, "link", actor="human", parent_attempt_id=one)
            projection.snapshot()
            projection.snapshot()
            self.assertGreater(index.hits, 0)
            facts_before = self.source.path.read_bytes()
            projection.control(one, "forget", actor="human")
            result = projection.search(scope="scope-a", task_type="fix", policy_digest="policy-1")
            self.assertTrue(all(r["node_id"] != "work" for r in result["items"]))
            self.assertEqual(facts_before, self.source.path.read_bytes())
            unavailable = projection.snapshot(include_unavailable=True)
            self.assertEqual(2, sum(not r["available"] for r in unavailable))
        finally:
            index.close()

    def test_correction_versions_reference_history_and_cannot_rewrite_facts(self):
        one, two = self.attempts()[1]["attempt_id"], self.attempts()[2]["attempt_id"]
        first = self.projection.control(two, "link", actor="human", parent_attempt_id=one)
        corrected = self.projection.control(two, "correct", actor="human",
                                             correction={"note": "private free text"})
        self.assertEqual(first["version"], corrected["previous"])
        self.assertNotIn("private free text", self.controls.read_text())
        record = next(r for r in self.projection.snapshot(include_unavailable=True) if r["attempt_id"] == two)
        self.assertEqual("completed", record["status"])
        self.assertFalse(record["available"])
        self.projection.control(two, "forget", actor="human")

    def test_agent_candidate_cannot_self_report_runtime_provenance_or_verification(self):
        lab = RSIOptimizationLab(self.root / "optimization")
        candidate = lab.propose("prompt_template", {"task_type": "fix", "append": "private guidance"}, "reason")
        path = lab.root / "candidates" / f"{candidate.candidate_id}.json"
        value = json.loads(path.read_text())
        value.update(origin="runtime", verified=True, status="approved", evaluation={"passed": True})
        path.write_text(json.dumps(value))
        source = EvidenceSource("scope-a", "optimization", path, "fix", "policy-1", 100)
        projection = EvidenceProjection([source], clock=lambda: 50)
        record = projection.snapshot()[0]
        self.assertEqual("agent_claim", record["origin"])
        self.assertFalse(record["verified"])
        self.assertNotIn("private guidance", encode_json(projection.portable()))
        self.assertNotIn(str(self.root), encode_json(projection.portable()))
        combined = EvidenceProjection([self.source, source], control_path=self.controls, clock=lambda: 50)
        one = self.attempts()[1]["attempt_id"]
        combined.control(one, "link", actor="human", guidance_version=candidate.candidate_id)
        self.assertEqual(candidate.candidate_id, next(r for r in combined.snapshot() if r["attempt_id"] == one)["guidance_version"])
        combined.control(record["attempt_id"], "forget", actor="human")
        self.assertNotIn(one, {r["attempt_id"] for r in combined.snapshot()})

    def test_learning_candidate_projection_does_not_activate_or_verify_it(self):
        loop = RSILoop(self.root / "learning")
        loop.record(observation("local", 0.1, 1, task_type="fix"))
        candidate = loop.propose(min_observations=1, min_samples=1)
        path = loop.root / "candidates" / f"{candidate.candidate_id}.json"
        before = path.read_bytes()
        projection = EvidenceProjection([EvidenceSource("scope-a", "learning", path, "fix", "policy-1", 100)])
        record = projection.snapshot()[0]
        self.assertEqual("agent_claim", record["origin"])
        self.assertFalse(record["verified"])
        self.assertEqual([candidate.candidate_id], record["candidate_refs"])
        self.assertEqual(before, path.read_bytes())
        self.assertIsNone(loop.active_policy())

    def test_incomplete_control_history_is_not_silently_dropped(self):
        attempts = self.attempts()
        one, two = attempts[1]["attempt_id"], attempts[2]["attempt_id"]
        self.projection.control(two, "link", actor="human", parent_attempt_id=one)
        self.controls.write_bytes(self.controls.read_bytes().rstrip(b"\n"))
        with self.assertRaisesRegex(ContractViolation, "incomplete"):
            self.projection.snapshot()
        with self.assertRaisesRegex(ContractViolation, "incomplete"):
            self.projection.control(one, "forget", actor="human")

    def test_context_injection_has_runtime_receipt_and_respects_total_budget(self):
        request = AgentRequest("next-task", "Investigate the failure", {}, ("answer",), self.root)
        compiler = ContextCompiler()
        compiled = compiler.compile_with_evidence(
            request, ContextPolicy(output_contract="prompt"), self.projection,
            scope="scope-a", task_type="fix", policy_digest="policy-1",
        )
        self.assertIn("attempt_evidence", compiled.body)
        self.assertNotIn("attempt_evidence", request.inputs)
        events = tuple(JsonlEventSink(self.access_log).read())
        self.assertEqual("evidence_context_injected", events[-1]["event"])
        self.assertEqual(compiled.fingerprint, events[-1]["context_fingerprint"])
        with self.assertRaises(ContextBudgetExceeded):
            compiler.compile_with_evidence(
                request, ContextPolicy(output_contract="prompt", max_context_bytes=128), self.projection,
                scope="scope-a", task_type="fix", policy_digest="policy-1",
            )
        self.assertEqual("evidence_retrieved", tuple(JsonlEventSink(self.access_log).read())[-1]["event"])

    def test_cli_exposes_existing_facts_without_creating_candidates(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = _dispatch(["evidence", "search", "--scope", "scope-a", "--source", "events",
                              str(self.source.path), "--task-type", "fix", "--policy-digest", "policy-1",
                              "--expires-at", "99999999999", "--max-items", "1"])
        self.assertEqual(0, code)
        self.assertEqual(1, len(json.loads(output.getvalue())["items"]))
        self.assertFalse((self.root / "optimization").exists())

    def test_publication_enriches_same_attempt_without_losing_failed_history(self):
        graph = publication_fixtures.publication_graph()
        workspace = self.root / "workspace"
        workspace.mkdir()
        cache = VerifiedArtifactCache(self.root / "reuse")
        executor = publication_fixtures.MemoryExecutor()
        publisher = VerifiedResultPublisher(cache, self.runtime_dir / "verified-publications.json")
        registry = publication_fixtures.registry_for(graph, executor, cache, workspace)
        GraphRuntime(graph, registry, self.runtime_dir, verified_result_publisher=publisher).run(run_id="publication-run")
        publications = EvidenceSource("scope-a", "publications", self.runtime_dir / "verified-publications.json", "fix", "policy-1", 100)
        projection = EvidenceProjection([self.source, publications])
        reversed_projection = EvidenceProjection([publications, self.source])
        self.assertEqual(projection.snapshot(), reversed_projection.snapshot())
        answer = next(r for r in projection.snapshot() if r["node_id"] == "answer")
        self.assertTrue(answer["verified"])
        self.assertEqual(1, len(answer["candidate_refs"]))
        self.assertEqual(1, len(answer["evaluation_refs"]))
        self.assertEqual(64, len(answer["candidate_digest"]))


class EngineeringProjectionTests(unittest.TestCase):
    setUp = engineering_fixtures.EngineeringTests.setUp
    tearDown = engineering_fixtures.EngineeringTests.tearDown
    policy = engineering_fixtures.EngineeringTests.policy
    workflow = engineering_fixtures.EngineeringTests.workflow
    prepared = engineering_fixtures.EngineeringTests.prepared

    def test_engineering_checks_are_bound_but_not_independent(self):
        workflow, _, plan = self.prepared()
        workflow.execute("human", plan.digest)
        projection = EvidenceProjection([EvidenceSource("project", "engineering", workflow.report_path)])
        records = projection.snapshot()
        self.assertTrue(all(not r["independent"] for r in records))
        check = next(r for r in records if r["status"] == "passed")
        self.assertEqual([plan.digest], check["candidate_refs"])
        self.assertEqual(1, len(check["evaluation_refs"]))
        self.assertIn("binding_digest", check)

    def test_receipt_and_plan_changes_cannot_hide_behind_cached_report(self):
        workflow, _, plan = self.prepared()
        workflow.execute("human", plan.digest)
        index = ProjectionIndex(self.root / "engineering.sqlite")
        projection = EvidenceProjection([EvidenceSource("project", "engineering", workflow.report_path)], index=index)
        try:
            projection.snapshot()
            projection.snapshot()
            self.assertGreater(index.hits, 0)
            receipt_path = next((workflow.task_dir / "checks").glob("*.json"))
            original_receipt = receipt_path.read_bytes()
            value = json.loads(original_receipt)
            value["result"]["returncode"] = 1
            receipt_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractViolation, "result binding mismatch"):
                projection.snapshot()
            receipt_path.write_bytes(original_receipt)
            original_plan = workflow.plan_path.read_bytes()
            value = json.loads(original_plan)
            value["objective"] = "tampered plan"
            workflow.plan_path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ContractViolation, "digest mismatch"):
                projection.snapshot()
        finally:
            index.close()
