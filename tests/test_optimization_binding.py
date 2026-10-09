import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from grapheng import AgentOS, CanaryObservation, ContractViolation, RSIOptimizationLab
from grapheng.cli import main
from tests.test_optimization import passing_measurement, regression_case, sample_graph


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def reseal(evaluation):
    evaluation["binding_digest"] = digest({
        key: value for key, value in evaluation.items() if key != "binding_digest"
    })


class OptimizationBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.lab = RSIOptimizationLab(self.root)
        self.suite = self.lab.freeze_suite((regression_case(),))

    def tearDown(self):
        self.temporary.cleanup()

    def evaluated(self, phase="approve", suffix="Use evidence."):
        candidate = self.lab.propose(
            "prompt_template", {"task_type": "coding", "append": suffix},
            "Evaluate grounded output.", rollout_percent=100,
        )
        candidate = self.lab.evaluate(
            candidate.candidate_id, self.suite.suite_id, (passing_measurement(),)
        )
        if phase != "approve":
            candidate = self.lab.approve(candidate.candidate_id, "operator")
        if phase in ("apply", "canary"):
            candidate = self.lab.activate(candidate.candidate_id)
        return candidate

    def candidate_path(self, candidate):
        return self.root / "candidates" / f"{candidate.candidate_id}.json"

    def edit(self, candidate, change, *, rehash=False):
        path = self.candidate_path(candidate)
        value = json.loads(path.read_text())
        change(value)
        if rehash:
            reseal(value["evaluation"])
        path.write_text(json.dumps(value))

    def reuse(self, phase, candidate):
        if phase == "approve":
            return self.lab.approve(candidate.candidate_id, "operator")
        if phase == "activate":
            return self.lab.activate(candidate.candidate_id)
        if phase == "canary":
            return self.lab.observe_canary(CanaryObservation(
                candidate.candidate_id, 100, True, 0.92, 0.9, 0.18, 0.2, 1.8, 2.0
            ))
        return self.lab.apply(sample_graph(), "run")

    def test_receipt_binds_sorted_inputs_policy_and_honest_provenance(self):
        second = replace(regression_case(), case_id="review-basic", fixture_digest="b" * 64)
        suite = self.lab.freeze_suite((second, regression_case()))
        measurements = (replace(passing_measurement(), case_id=second.case_id), passing_measurement())
        candidates = []
        for items in (measurements, tuple(reversed(measurements))):
            candidate = self.lab.propose(
                "prompt_template", {"task_type": "coding", "append": "Use evidence."}, "Check."
            )
            candidates.append(self.lab.evaluate(candidate.candidate_id, suite.suite_id, items))
        evaluation = candidates[0].evaluation
        binding = evaluation["binding"]
        self.assertEqual(1, binding["schema_version"])
        self.assertEqual(candidates[0].candidate_id, binding["candidate_id"])
        self.assertEqual("rsi-regression-metrics-v1", binding["evaluator_version"])
        self.assertEqual("caller_reported", binding["measurement_source"])
        self.assertIs(False, binding["independent"])
        self.assertEqual("not_performed", binding["resource_constraints"]["candidate_execution"])
        self.assertEqual("unknown", binding["resource_constraints"]["isolation"])
        for field in ("cpu_seconds", "memory_bytes", "wall_clock_seconds", "output_bytes"):
            self.assertIsNone(binding["resource_constraints"][field])
        self.assertEqual(["coding-basic", "review-basic"], [m["case_id"] for m in binding["measurements"]])
        self.assertEqual(digest(binding["measurements"]), binding["measurements_digest"])
        self.assertEqual(digest({
            "thresholds": evaluation["thresholds"], "guardrails": evaluation["guardrails"],
        }), binding["policy_digest"])
        self.assertEqual(binding["measurements_digest"], candidates[1].evaluation["binding"]["measurements_digest"])
        self.assertNotEqual(evaluation["binding_digest"], candidates[1].evaluation["binding_digest"])
        approved = self.lab.approve(candidates[0].candidate_id, "operator")
        self.assertEqual(evaluation["binding_digest"], approved.approved_evaluation_digest)

    def test_suite_is_rechecked_at_every_reuse_boundary(self):
        path = self.root / "suites" / f"{self.suite.suite_id}.json"
        original = path.read_text()
        for phase in ("approve", "activate", "apply", "canary"):
            with self.subTest(phase=phase):
                candidate = self.evaluated(phase)
                value = json.loads(original)
                value["cases"][0]["fixture_digest"] = "c" * 64
                path.write_text(json.dumps(value))
                before = self.candidate_path(candidate).read_bytes()
                with self.assertRaisesRegex(ContractViolation, "suite was modified"):
                    self.reuse(phase, candidate)
                self.assertEqual(before, self.candidate_path(candidate).read_bytes())
                path.write_text(original)
        self.assertFalse((self.root / "canary.jsonl").exists())

    def test_refreezing_does_not_trust_a_saved_digest_without_checking_cases(self):
        path = self.root / "suites" / f"{self.suite.suite_id}.json"
        value = json.loads(path.read_text())
        value["cases"][0]["baseline_quality"] = 0.1
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ContractViolation, "suite was modified"):
            self.lab.freeze_suite((regression_case(),))

    def test_self_consistent_empty_or_duplicate_suites_are_invalid(self):
        for cases in ([], [regression_case().to_dict()] * 2):
            with self.subTest(cases=len(cases)):
                suite_digest = digest(cases)
                suite_id = f"suite-{suite_digest[:24]}"
                path = self.root / "suites" / f"{suite_id}.json"
                path.write_text(json.dumps({
                    "suite_id": suite_id, "created_at": 100, "digest": suite_digest, "cases": cases,
                }))
                candidate = self.lab.propose(
                    "prompt_template", {"task_type": "coding", "append": "Use evidence."}, "Check."
                )
                with self.assertRaisesRegex(ContractViolation, "non-empty, unique and ordered"):
                    self.lab.evaluate(candidate.candidate_id, suite_id, ())

    def test_candidate_change_and_rollout_drift_block_reuse(self):
        for phase in ("approve", "activate", "apply"):
            for field in ("change", "rollout_percent"):
                with self.subTest(phase=phase, field=field):
                    candidate = self.evaluated(phase)
                    def change(value, field=field):
                        if field == "change":
                            value["change"]["append"] = "Changed after evaluation."
                        else:
                            value["rollout_percent"] = 99
                    self.edit(candidate, change)
                    with self.assertRaisesRegex(ContractViolation, "changed after evaluation"):
                        self.reuse(phase, candidate)
                    self.candidate_path(candidate).write_text(json.dumps(candidate.to_dict()))

    def test_direct_evaluation_changes_are_rejected_before_state_changes(self):
        for phase in ("approve", "activate", "apply", "canary"):
            for field, value in (("thresholds", {}), ("passed", False), ("measured_total_cost_usd", 0.0)):
                with self.subTest(phase=phase, field=field):
                    candidate = self.evaluated(phase)
                    self.edit(candidate, lambda v, field=field, value=value: v["evaluation"].__setitem__(field, value))
                    before = self.candidate_path(candidate).read_bytes()
                    with self.assertRaisesRegex(ContractViolation, "binding was modified"):
                        self.reuse(phase, candidate)
                    self.assertEqual(before, self.candidate_path(candidate).read_bytes())
                    self.candidate_path(candidate).write_text(json.dumps(candidate.to_dict()))

    def test_rehashed_metadata_cannot_upgrade_source_or_change_evaluator_constraints(self):
        changes = (
            ("schema_version", 2), ("candidate_id", "opt-another"),
            ("evaluator_version", "unrecognized-v2"), ("measurement_source", "runtime"),
            ("independent", True), ("measurements_digest", "f" * 64),
            ("policy_digest", "f" * 64),
            ("resource_constraints", {"candidate_execution": "isolated", "cpu_seconds": 1}),
        )
        for field, value in changes:
            with self.subTest(field=field):
                candidate = self.evaluated()
                self.edit(candidate, lambda v, field=field, value=value: v["evaluation"]["binding"].__setitem__(field, value), rehash=True)
                with self.assertRaisesRegex(ContractViolation, "binding changed after evaluation"):
                    self.lab.approve(candidate.candidate_id, "operator")

    def test_current_evaluator_version_invalidates_old_receipts(self):
        self.evaluated("apply")
        with patch("grapheng.optimization.REGRESSION_EVALUATOR_VERSION", "rsi-regression-metrics-v2"):
            with self.assertRaisesRegex(ContractViolation, "binding changed after evaluation"):
                self.lab.apply(sample_graph(), "run")

    def test_rehashed_guardrails_cannot_relax_fixed_constraints(self):
        for field in ("hard_constraints_mutable", "human_approval_required", "frozen_suite"):
            with self.subTest(field=field):
                candidate = self.evaluated()
                def change(value, field=field):
                    evaluation = value["evaluation"]
                    evaluation["guardrails"][field] = not evaluation["guardrails"][field]
                    evaluation["binding"]["policy_digest"] = digest({
                        "thresholds": evaluation["thresholds"], "guardrails": evaluation["guardrails"],
                    })
                self.edit(candidate, change, rehash=True)
                with self.assertRaisesRegex(ContractViolation, "binding changed after evaluation"):
                    self.lab.approve(candidate.candidate_id, "operator")

    def test_rehashed_inputs_must_cover_frozen_cases_exactly_once(self):
        for mode in ("missing", "duplicate"):
            with self.subTest(mode=mode):
                candidate = self.evaluated()
                def change(value, mode=mode):
                    binding = value["evaluation"]["binding"]
                    binding["measurements"] = [] if mode == "missing" else binding["measurements"] * 2
                    binding["measurements_digest"] = digest(binding["measurements"])
                self.edit(candidate, change, rehash=True)
                with self.assertRaisesRegex(ContractViolation, "each frozen case exactly once"):
                    self.lab.approve(candidate.candidate_id, "operator")

    def test_active_record_identity_and_status_are_checked(self):
        candidate = self.evaluated("apply")
        for field, value in (("candidate_id", "opt-other"), ("status", "superseded")):
            with self.subTest(field=field):
                self.edit(candidate, lambda v, field=field, value=value: v.__setitem__(field, value))
                with self.assertRaisesRegex(ContractViolation, "identity.*mismatch"):
                    self.lab.apply(sample_graph(), "run")
                self.candidate_path(candidate).write_text(json.dumps(candidate.to_dict()))

    def test_measurement_edits_cannot_preserve_stale_aggregate_scores(self):
        candidate = self.evaluated()
        def change(value):
            binding = value["evaluation"]["binding"]
            binding["measurements"][0]["quality_score"] = 0.1
            binding["measurements_digest"] = digest(binding["measurements"])
        self.edit(candidate, change, rehash=True)
        with self.assertRaisesRegex(ContractViolation, "binding changed after evaluation"):
            self.lab.approve(candidate.candidate_id, "operator")

    def test_failed_measurement_cannot_be_promoted_by_rehashing_passed_flag(self):
        candidate = self.lab.propose(
            "prompt_template", {"task_type": "coding", "append": "Use evidence."}, "Check."
        )
        candidate = self.lab.evaluate(candidate.candidate_id, self.suite.suite_id, (
            replace(passing_measurement(), reality_anchor_passed=False),
        ))
        self.edit(candidate, lambda v: v["evaluation"].update(passed=True, failures=[]), rehash=True)
        with self.assertRaisesRegex(ContractViolation, "binding changed after evaluation"):
            self.lab.approve(candidate.candidate_id, "operator")

    def test_changed_complete_evaluation_requires_new_human_approval(self):
        for phase in ("activate", "apply", "canary"):
            with self.subTest(phase=phase):
                candidate = self.evaluated(phase)
                alternate = RSIOptimizationLab(self.root / f"alternate-{phase}")
                alternate.freeze_suite((regression_case(),))
                path = alternate.root / "candidates" / f"{candidate.candidate_id}.json"
                path.parent.mkdir()
                proposed = replace(candidate, status="proposed", evaluation=None)
                path.write_text(json.dumps(proposed.to_dict()))
                changed = alternate.evaluate(
                    candidate.candidate_id, self.suite.suite_id, (passing_measurement(),),
                    max_cost_increase_percent=5.0,
                )
                self.edit(candidate, lambda v, changed=changed: v.__setitem__("evaluation", changed.evaluation))
                with self.assertRaisesRegex(ContractViolation, "human approval"):
                    self.reuse(phase, candidate)
                self.candidate_path(candidate).write_text(json.dumps(candidate.to_dict()))

    def test_legacy_receipts_remain_readable_but_cannot_be_reused(self):
        for phase in ("approve", "activate", "apply"):
            with self.subTest(phase=phase):
                candidate = self.evaluated(phase)
                def make_legacy(value):
                    value["evaluation"].pop("binding")
                    value["evaluation"].pop("binding_digest")
                    value.pop("approved_evaluation_digest")
                self.edit(candidate, make_legacy)
                legacy = next(c for c in self.lab.candidates() if c.candidate_id == candidate.candidate_id)
                self.assertIsNone(legacy.approved_evaluation_digest)
                self.assertTrue(legacy.evaluation["passed"])
                with self.assertRaisesRegex(ContractViolation, "no current binding"):
                    self.reuse(phase, candidate)
                self.candidate_path(candidate).write_text(json.dumps(candidate.to_dict()))

    def test_approval_binding_cannot_be_omitted(self):
        candidate = self.evaluated("activate")
        self.edit(candidate, lambda v: v.pop("approved_evaluation_digest"))
        with self.assertRaisesRegex(ContractViolation, "human approval"):
            self.lab.activate(candidate.candidate_id)

    def test_receipts_cannot_be_copied_between_identical_candidates(self):
        original = self.evaluated()
        other = self.evaluated()
        self.edit(other, lambda v: v.__setitem__("evaluation", original.evaluation))
        with self.assertRaisesRegex(ContractViolation, "binding changed after evaluation"):
            self.lab.approve(other.candidate_id, "operator")

    def test_invalid_rollback_history_disables_current_and_keeps_older_history(self):
        older = self.evaluated("apply", "First stable version.")
        previous = self.evaluated("apply", "Second stable version.")
        current = self.evaluated("apply", "Current version.")
        self.edit(previous, lambda v: v["evaluation"]["thresholds"].update(max_cost_increase_percent=100))
        self.assertIsNone(self.lab.rollback("prompt_template"))
        self.assertEqual({}, self.lab.active_candidates())
        self.assertEqual(sample_graph(), self.lab.apply(sample_graph(), "run"))
        records = {c.candidate_id: c for c in self.lab.candidates()}
        self.assertEqual("rolled_back", records[current.candidate_id].status)
        self.assertEqual("superseded", records[previous.candidate_id].status)
        self.assertEqual([older.candidate_id], json.loads((self.root / "history.json").read_text())["prompt_template"])
        receipt = json.loads((self.root / "rollback.jsonl").read_text())
        self.assertEqual(previous.candidate_id, receipt["rejected_previous_id"])
        self.assertIsNone(receipt["restored_candidate_id"])
        self.assertIn("binding was modified", receipt["rejection_reason"])

    def test_missing_or_legacy_history_does_not_leave_current_active(self):
        for failure in ("missing", "legacy", "malformed", "missing_suite"):
            with self.subTest(failure=failure):
                previous = self.evaluated("apply", "Previous version.")
                current = self.evaluated("apply", "Current version.")
                path = self.candidate_path(previous)
                if failure == "missing":
                    path.unlink()
                elif failure == "legacy":
                    self.edit(previous, lambda v: v["evaluation"].pop("binding"))
                elif failure == "malformed":
                    path.write_text('{"status": "superseded"}')
                else:
                    self.edit(previous, lambda v: v["evaluation"].update(suite_id="suite-missing"), rehash=True)
                self.assertIsNone(self.lab.rollback("prompt_template"))
                self.assertEqual({}, self.lab.active_candidates())
                if path.exists():
                    path.write_text(json.dumps(replace(previous, status="superseded").to_dict()))
                self.assertEqual("rolled_back", next(c for c in self.lab.candidates() if c.candidate_id == current.candidate_id).status)

    def test_rollback_can_disable_invalid_current_without_reusing_its_evaluation(self):
        previous = self.evaluated("apply", "Stable version.")
        current = self.evaluated("apply", "Current version.")
        self.edit(current, lambda v: v["evaluation"].update(passed=False))
        restored = self.lab.rollback("prompt_template")
        self.assertEqual(previous.candidate_id, restored.candidate_id)
        self.assertIn("Stable version.", self.lab.apply(sample_graph(), "run").node_map()["draft"].agent.prompt)

    def test_cli_evaluate_preserves_source_and_activate_reports_suite_drift(self):
        candidate = self.lab.propose(
            "prompt_template", {"task_type": "coding", "append": "Use evidence."}, "Check."
        )
        measurements = self.root / "measurements.json"
        measurements.write_text(json.dumps([passing_measurement().to_dict()]))
        stdout = io.StringIO()
        argv = ["agent-os", "rsi-opt", "evaluate", "--optimization-root", str(self.root),
                "--candidate-id", candidate.candidate_id, "--suite-id", self.suite.suite_id,
                "--measurements", str(measurements)]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(stdout):
            self.assertEqual(0, main())
        evaluation = json.loads(stdout.getvalue())["evaluation"]
        self.assertIs(False, evaluation["binding"]["independent"])
        self.assertEqual("caller_reported", evaluation["binding"]["measurement_source"])
        self.lab.approve(candidate.candidate_id, "operator")
        (self.root / "suites" / f"{self.suite.suite_id}.json").unlink()
        stderr = io.StringIO()
        argv = ["agent-os", "rsi-opt", "activate", "--optimization-root", str(self.root),
                "--candidate-id", candidate.candidate_id]
        with patch.object(sys, "argv", argv), contextlib.redirect_stderr(stderr):
            self.assertEqual(2, main())
        self.assertIn("regression suite", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse((self.root / "active.json").exists())

    def test_portable_state_preserves_approval_bindings_and_excludes_local_rollback_log(self):
        base = self.root
        source = AgentOS(base / "source-state")
        self.root = source.optimization_root
        self.lab = RSIOptimizationLab(self.root)
        self.suite = self.lab.freeze_suite((regression_case(),))
        previous = self.evaluated("apply", "Stable version.")
        bundle = source.export_bundle(base / "active-bundle")
        target = AgentOS(base / "target-state")
        target.import_bundle(bundle)
        imported = RSIOptimizationLab(target.optimization_root).active_candidates()["prompt_template"]
        self.assertEqual(previous.approved_evaluation_digest, imported.approved_evaluation_digest)
        self.assertIn("Stable version.", target.apply(sample_graph(), "run").node_map()["draft"].agent.prompt)
        self.evaluated("apply", "Current version.")
        self.edit(previous, lambda v: v["evaluation"]["binding"].update(independent=True))
        self.assertIsNone(self.lab.rollback("prompt_template"))
        log = self.root / "rollback.jsonl"
        receipt = json.loads(log.read_text())
        receipt["rejection_reason"] = "/private/operator/rollback-debug-path"
        log.write_text(json.dumps(receipt) + "\n")
        bundle = source.export_bundle(base / "rollback-bundle")
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertIn("optimization-rollback-runtime", manifest["excludes"])
        self.assertFalse((bundle / "optimization/rollback.jsonl").exists())
        self.assertNotIn("rollback-debug-path", "\n".join(
            path.read_text() for path in bundle.rglob("*") if path.is_file()
        ))
        restored = AgentOS(base / "rollback-target")
        restored.import_bundle(bundle)
        self.assertEqual(sample_graph(), restored.apply(sample_graph(), "run"))
