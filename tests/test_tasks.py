import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from grapheng import (
    AgentResult,
    ContractViolation,
    EngineeringWorkflow,
    ExecutorCapabilities,
    ExecutorRegistry,
    ProjectPolicy,
    UserTaskModule,
)
from grapheng.cli import main


class TaskExecutor:
    @property
    def capabilities(self):
        return ExecutorCapabilities(
            "task-test",
            ("structured_output", "token_usage", "tool_policy"),
            ("read", "shell", "edit", "write"),
        )

    def execute(self, request):
        role = request.task_type.split(".")[-1]
        outputs = {
            "explore": {"exploration": {"files": ["app.py"]}},
            "plan": {"plan": {"steps": ["change", "verify"]}},
            "implement": {"implementation_summary": "changed the value"},
            "review": {"verdict": "approve", "findings": [], "score": 0.95},
        }[role]
        return AgentResult(
            "task-test", outputs, "done", tokens_used=5, cost_usd=0.01
        )


class UserTaskModuleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        policy = ProjectPolicy(
            check_commands=(
                (
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('app.py').is_file()",
                ),
            ),
            require_clean_worktree=False,
            max_elapsed_seconds=60,
            agent_timeout_seconds=30,
        )
        policy_path = self.workspace / ".agent-os" / "engineering.json"
        policy_path.parent.mkdir()
        policy_path.write_text(json.dumps(policy.to_dict()), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def module(self, task_id="task-0123456789abcdef"):
        def workflow_factory(workspace, task_dir, policy, agent_os):
            registry = ExecutorRegistry()
            registry.register(TaskExecutor())
            return EngineeringWorkflow(
                workspace,
                task_dir,
                registry,
                policy,
                agent_os_root=agent_os.root,
            )

        return UserTaskModule(
            self.root / "home",
            workflow_factory=workflow_factory,
            clock=lambda: 100.0,
            id_factory=lambda: task_id,
        )

    def test_five_actions_share_one_engineering_task_state(self):
        tasks = self.module()
        submitted = tasks.do("Change the value safely", self.workspace)
        task_id = submitted["task_id"]
        metadata = json.loads(
            (tasks.tasks_root / task_id / "task.json").read_text(encoding="utf-8")
        )

        self.assertEqual("awaiting_approval", submitted["phase"])
        self.assertEqual("approve", submitted["next_action"])
        self.assertNotIn("phase", metadata)
        with self.assertRaisesRegex(ContractViolation, "no final result"):
            tasks.result(task_id)

        approved = tasks.approve(task_id, "operator")
        result = tasks.result(task_id)

        self.assertEqual("succeeded", approved["phase"])
        self.assertEqual("result", approved["next_action"])
        self.assertTrue(result["success"])
        self.assertTrue(result["verification"]["passed"])
        self.assertEqual("changed the value", result["outcome"]["summary"])
        self.assertEqual("approve", result["verification"]["independent_review"]["verdict"])
        self.assertEqual("operator", result["human_intervention"]["approved_by"])
        self.assertEqual(20, result["usage"]["tokens_used"])
        self.assertEqual(2, len(result["artifacts"]))

    def test_control_cancels_only_before_approval(self):
        tasks = self.module()
        task_id = tasks.do("Prepare a safe change", self.workspace)["task_id"]

        cancelled = tasks.control(task_id, "cancel", "operator")
        result = tasks.result(task_id)

        self.assertEqual("cancelled", cancelled["phase"])
        self.assertFalse(result["success"])
        self.assertEqual("operator", result["human_intervention"]["cancelled_by"])
        with self.assertRaisesRegex(ContractViolation, "not awaiting approval"):
            tasks.approve(task_id, "operator")

    def test_task_identity_and_metadata_fail_closed(self):
        tasks = self.module()
        with self.assertRaisesRegex(ContractViolation, "invalid task id"):
            tasks.status("../outside")

        task_id = tasks.do("Prepare a safe change", self.workspace)["task_id"]
        metadata_path = tasks.tasks_root / task_id / "task.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["phase"] = "succeeded"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        with self.assertRaisesRegex(ContractViolation, "invalid contract"):
            tasks.status(task_id)

    def test_portable_state_excludes_operational_task_content(self):
        tasks = self.module()
        objective = "Private objective kept in operational task state"
        tasks.do(objective, self.workspace)

        bundle = tasks.agent_os.export_bundle(self.root / "bundle")
        bundle_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in bundle.rglob("*")
            if path.is_file()
        )

        self.assertNotIn(objective, bundle_text)
        self.assertNotIn(str(self.workspace), bundle_text)
        self.assertFalse((tasks.state_root / "tasks").exists())

    def test_do_infers_policy_and_exposes_reviewable_intent(self):
        policy_path = self.workspace / ".agent-os" / "engineering.json"
        policy_path.unlink()
        (self.workspace / "pyproject.toml").write_text(
            "[project]\nname = 'sample'\n", encoding="utf-8"
        )
        (self.workspace / "tests").mkdir()
        subprocess.run(("git", "init", "-q"), cwd=self.workspace, check=True)
        subprocess.run(("git", "add", "."), cwd=self.workspace, check=True)
        subprocess.run(
            (
                "git", "-c", "user.email=test@example.com", "-c",
                "user.name=Test User", "commit", "-qm", "initial",
            ),
            cwd=self.workspace,
            check=True,
        )
        tasks = self.module()

        submitted = tasks.do(
            "修复 value 计算错误并补回归测试",
            self.workspace,
            constraints=("Keep the public interface stable",),
        )
        intent = submitted["intent"]

        self.assertEqual("fix", intent["template"])
        self.assertEqual(["python"], intent["project_kinds"])
        self.assertEqual(
            [["python3", "-m", "unittest", "discover", "-s", "tests"]],
            intent["verification_commands"],
        )
        self.assertEqual(
            ["Keep the public interface stable"], intent["constraints"]
        )
        self.assertEqual("succeeded", tasks.approve(submitted["task_id"], "operator")["phase"])

    def test_clarification_failure_does_not_allocate_or_call_an_agent(self):
        policy_path = self.workspace / ".agent-os" / "engineering.json"
        policy_path.unlink()
        tasks = self.module()

        with self.assertRaisesRegex(ContractViolation, "needs clarification"):
            tasks.do("处理一下", self.workspace)

        self.assertEqual([], list(tasks.tasks_root.iterdir()))

    def test_intent_tampering_invalidates_approval(self):
        tasks = self.module()
        task_id = tasks.do(
            "Refactor value handling",
            self.workspace,
            constraints=("Preserve behavior",),
        )["task_id"]
        plan_path = tasks.tasks_root / task_id / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["intent"]["constraints"] = ["Changed after planning"]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        with self.assertRaisesRegex(ContractViolation, "digest mismatch"):
            tasks.approve(task_id, "operator")

    def test_cli_has_human_and_machine_readable_status(self):
        value = {
            "task_id": "task-0123456789abcdef",
            "phase": "awaiting_approval",
            "summary": "Plan ready and waiting for approval",
            "next_action": "approve",
            "usage": {"tokens_used": 10, "cost_usd": 0.02},
        }
        tasks = Mock()
        tasks.status.return_value = value
        human = io.StringIO()
        argv = ["agent-os", "status", value["task_id"]]
        with patch(
            "grapheng.cli.UserTaskModule", return_value=tasks
        ), patch.object(sys, "argv", argv), contextlib.redirect_stdout(human):
            self.assertEqual(0, main())

        machine = io.StringIO()
        argv.append("--json")
        with patch(
            "grapheng.cli.UserTaskModule", return_value=tasks
        ), patch.object(sys, "argv", argv), contextlib.redirect_stdout(machine):
            self.assertEqual(0, main())

        self.assertIn("Plan ready and waiting for approval", human.getvalue())
        self.assertEqual(value, json.loads(machine.getvalue()))


if __name__ == "__main__":
    unittest.main()
