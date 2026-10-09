import json
import subprocess
import sys
import unittest

from grapheng import ContractViolation, EngineeringPlan
from grapheng._store import json_digest
from tests import test_engineering as fixtures


class EngineeringEvidenceTests(unittest.TestCase):
    setUp = fixtures.EngineeringTests.setUp
    tearDown = fixtures.EngineeringTests.tearDown
    policy = fixtures.EngineeringTests.policy
    workflow = fixtures.EngineeringTests.workflow
    prepared = fixtures.EngineeringTests.prepared

    def test_fingerprint_frames_content_and_file_types(self):
        workflow, _ = self.workflow()
        # Adjacent sorted entries reproduce the old unframed byte stream;
        # the existing app.py must not appear between these two files.
        a, b = self.workspace / "z-a", self.workspace / "z-b"
        a.write_bytes(b"z-b\0")
        b.write_bytes(b"")
        first = workflow._workspace_fingerprint()
        a.write_bytes(b"")
        b.write_bytes(b"z-b\0")
        self.assertNotEqual(first, workflow._workspace_fingerprint())
        a.unlink()
        a.symlink_to("z-b")
        link = workflow._workspace_fingerprint()
        a.unlink()
        a.write_bytes(b"z-b")
        self.assertNotEqual(link, workflow._workspace_fingerprint())

    def test_legacy_digest_loads_but_cannot_authorize_new_mutation(self):
        workflow, executor, plan = self.prepared()
        document = plan.to_dict()
        document["workspace_digest"] = "a" * 64
        document["digest"] = json_digest({k: v for k, v in document.items() if k != "digest"})[1]
        workflow.plan_path.write_text(json.dumps(document))
        loaded = EngineeringPlan.load(workflow.plan_path)
        with self.assertRaisesRegex(ContractViolation, "new approved plan"):
            workflow.execute("human", loaded.digest)
        self.assertEqual(2, len(executor.requests))

    def test_each_check_is_persisted_before_later_timeout(self):
        commands = ((sys.executable, "-c", "pass"),
                    (sys.executable, "-c", "raise RuntimeError()"))
        workflow, _, plan = self.prepared(policy=self.policy(check_commands=commands))
        original = workflow._runner

        def runner(command, **kwargs):
            if tuple(command) == commands[1]:
                current = workflow.status()
                self.assertEqual("passed", current["check_receipts"][0]["status"])
                self.assertEqual("running", current["check_receipts"][1]["status"])
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return original(command, **kwargs)

        workflow._runner = runner
        with self.assertRaises(subprocess.TimeoutExpired):
            workflow.execute("human", plan.digest)
        receipts = workflow.status()["check_receipts"]
        self.assertEqual(["passed", "timeout"], [r["status"] for r in receipts])
        self.assertFalse(receipts[0]["binding"]["independent"])
        self.assertIn("candidate_digest", receipts[0]["binding"])
        self.assertIn("evaluator_version", receipts[0]["binding"])

    def test_checks_cannot_mutate_the_candidate_and_claim_success(self):
        command = (sys.executable, "-c", "from pathlib import Path; Path('app.py').write_text('changed')")
        workflow, _, plan = self.prepared(policy=self.policy(command))
        with self.assertRaisesRegex(ContractViolation, "changed the candidate"):
            workflow.execute("human", plan.digest)
        report = workflow.status()
        self.assertFalse(report["success"])
        self.assertEqual("invalid", report["check_receipts"][0]["status"])

    def test_review_cannot_change_candidate_after_checks(self):
        def mutate(_request):
            (self.workspace / "app.py").write_text("changed")
        workflow, _, plan = self.prepared({"review": [mutate]})
        with self.assertRaisesRegex(ContractViolation, "changed after verification"):
            workflow.execute("human", plan.digest)
        self.assertFalse(workflow.status()["success"])

    def test_check_receipts_survive_pause_and_a_later_execution(self):
        workflow, _, plan = self.prepared()

        def pause_after_checks():
            return "pause" if workflow.status().get("check_receipts") else None

        paused = workflow.execute("human", plan.digest, control_probe=pause_after_checks)
        self.assertEqual("paused", paused["phase"])
        first = paused["check_receipts"][0]["receipt_id"]
        receipt_path = self.task_dir / "checks" / f"{first}.json"
        before = receipt_path.read_bytes()
        finished = workflow.execute("human", plan.digest)
        self.assertTrue(finished["success"])
        self.assertEqual(2, len(finished["check_receipts"]))
        self.assertEqual(before, receipt_path.read_bytes())
        self.assertEqual(first, finished["check_receipts"][0]["receipt_id"])

    def test_harness_failure_and_interrupted_checks_remain_distinguishable(self):
        workflow, _, plan = self.prepared()
        original = workflow._runner

        def failing_runner(command, **kwargs):
            if tuple(command) in workflow.policy.check_commands:
                raise OSError("check runner unavailable")
            return original(command, **kwargs)

        workflow._runner = failing_runner
        with self.assertRaises(OSError):
            workflow.execute("human", plan.digest)
        receipt = workflow.status()["check_receipts"][0]
        self.assertEqual("harness_error", receipt["status"])
        self.assertFalse(workflow.status()["success"])
        path = workflow.task_dir / "checks" / f"{receipt['receipt_id']}.json"
        # A saved start with no result represents an interrupted process.
        receipt["status"] = "running"
        receipt["result"] = None
        path.write_text(json.dumps(receipt))
        workflow._runner = original
        finished = workflow.execute("human", plan.digest)
        history = {r["receipt_id"]: r for r in finished["check_receipts"]}
        self.assertEqual("interrupted", history[receipt["receipt_id"]]["status"])
        self.assertIsNone(history[receipt["receipt_id"]]["elapsed_seconds"])
        self.assertTrue(finished["success"])


class CanonicalDigestTests(unittest.TestCase):
    def test_non_finite_values_are_rejected_without_changing_legal_digests(self):
        self.assertEqual(
            "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777",
            json_digest({"b": 2, "a": 1})[1],
        )
        for number in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(number=number), self.assertRaises(ContractViolation):
                json_digest({"nested": [number]})
