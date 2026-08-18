import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grapheng import ContractViolation, EvaluationCase, EvaluationLab
from grapheng.cli import main


def engineering_report(**overrides):
    value = {
        "schema_version": 1,
        "phase": "succeeded",
        "objective": "private objective must not be copied",
        "plan_digest": "a" * 64,
        "approved_by": "private operator",
        "started_at": 100.0,
        "finished_at": 112.0,
        "success": True,
        "agent_calls": 4,
        "review_cycles": 0,
        "tokens_used": 120,
        "cost_usd": 0.4,
        "cost_complete": True,
        "preparation_usage": {},
        "checks": [],
        "reviews": [{"score": 0.9}],
        "repairs": [],
        "reality_anchor": {"passed": True},
        "failure": None,
    }
    value.update(overrides)
    return value


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.case = EvaluationCase(
            "fix-small-001", "engineering.fix", "small", tags=("python",)
        )

    def tearDown(self):
        self.temporary.cleanup()

    def write_report(self, value=None):
        path = self.root / "report.json"
        path.write_text(json.dumps(value or engineering_report()), encoding="utf-8")
        return path

    def test_case_contract_excludes_objectives_and_rejects_unknown_fields(self):
        value = self.case.to_dict()
        value["objective"] = "do not persist this"

        with self.assertRaisesRegex(ContractViolation, "invalid contract"):
            EvaluationCase.from_dict(value)

        self.assertNotIn("objective", self.case.to_dict())
        self.assertNotIn("workspace", self.case.to_dict())

    def test_records_engineering_outcome_without_private_report_content(self):
        lab = EvaluationLab(self.root / "evaluation", clock=lambda: 200.0)
        record = lab.record_engineering(
            self.case,
            "run-001",
            self.write_report(),
            user_inputs=2,
            human_decisions=1,
        )
        stored = next((self.root / "evaluation" / "records").glob("*.json"))
        text = stored.read_text(encoding="utf-8")

        self.assertTrue(record.verified)
        self.assertEqual(12.0, record.duration_seconds)
        self.assertEqual(0.9, record.quality_score)
        self.assertNotIn("private objective", text)
        self.assertNotIn("private operator", text)

        summary = lab.summary()
        self.assertEqual(1.0, summary["verified_success_rate"])
        self.assertEqual(0.4, summary["cost_per_verified_result_usd"])
        self.assertEqual(2.0, summary["average_user_inputs"])

    def test_summary_exposes_false_completion_and_incomplete_cost(self):
        lab = EvaluationLab(self.root / "evaluation", clock=lambda: 200.0)
        lab.record_engineering(
            self.case,
            "run-001",
            self.write_report(
                engineering_report(
                    reality_anchor={"passed": False}, cost_complete=False
                )
            ),
            user_inputs=3,
            human_decisions=2,
        )
        summary = lab.summary()

        self.assertEqual(1, summary["false_completions"])
        self.assertFalse(summary["cost_complete"])
        self.assertIsNone(summary["cost_per_verified_result_usd"])

    def test_summary_counts_each_quality_sample_once(self):
        lab = EvaluationLab(self.root / "evaluation", clock=lambda: 200.0)
        report_path = self.write_report()
        lab.record_engineering(
            self.case,
            "run-001",
            report_path,
            user_inputs=2,
            human_decisions=1,
        )
        second_case = EvaluationCase(
            "fix-small-002", "engineering.fix", "small"
        )
        lab.record_engineering(
            second_case,
            "run-002",
            report_path,
            user_inputs=2,
            human_decisions=1,
        )

        summary = lab.summary()
        self.assertEqual(2, summary["quality_samples"])
        self.assertEqual(0.9, summary["average_quality"])

    def test_baselines_are_immutable_and_records_fail_closed(self):
        lab = EvaluationLab(self.root / "evaluation", clock=lambda: 200.0)
        lab.record_engineering(
            self.case,
            "run-001",
            self.write_report(),
            user_inputs=2,
            human_decisions=1,
        )
        baseline = lab.create_baseline("v0.0.1")

        self.assertEqual(1, baseline["summary"]["runs"])
        with self.assertRaisesRegex(ContractViolation, "already exists"):
            lab.create_baseline("v0.0.1")

        record_path = next((self.root / "evaluation" / "records").glob("*.json"))
        record_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ContractViolation, "invalid contract"):
            lab.summary()

    def test_state_directories_cannot_redirect_through_symlinks(self):
        evaluation_root = self.root / "evaluation"
        redirected = self.root / "redirected"
        evaluation_root.mkdir()
        redirected.mkdir()
        (evaluation_root / "records").symlink_to(redirected, target_is_directory=True)

        with self.assertRaisesRegex(ContractViolation, "cannot be symlinks"):
            EvaluationLab(evaluation_root)

    def test_cli_records_and_snapshots_a_baseline(self):
        case_path = self.root / "case.json"
        case_path.write_text(json.dumps(self.case.to_dict()), encoding="utf-8")
        report_path = self.write_report()
        evaluation_root = self.root / "evaluation"
        output = io.StringIO()

        argv = [
            "agent-os",
            "evaluate",
            "record-engineering",
            "--root",
            str(evaluation_root),
            "--case",
            str(case_path),
            "--report",
            str(report_path),
            "--run-id",
            "run-cli",
            "--user-inputs",
            "2",
            "--human-decisions",
            "1",
        ]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            self.assertEqual(0, main())
        self.assertTrue(json.loads(output.getvalue())["verified"])

        output = io.StringIO()
        argv = [
            "agent-os",
            "evaluate",
            "baseline",
            "--root",
            str(evaluation_root),
            "--name",
            "v0.0.1",
        ]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
            self.assertEqual(0, main())
        self.assertEqual("v0.0.1", json.loads(output.getvalue())["name"])


if __name__ == "__main__":
    unittest.main()
