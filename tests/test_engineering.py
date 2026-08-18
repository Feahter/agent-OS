import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from grapheng import (
    AgentResult,
    ContractViolation,
    EffectIndeterminateError,
    EngineeringPlan,
    EngineeringWorkflow,
    ExecutorCapabilities,
    ExecutorRegistry,
    IntentCompiler,
    ProjectPolicy,
    RSILoop,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ScriptedExecutor:
    def __init__(self, workspace, scripts=None):
        self.workspace = workspace
        self.scripts = {key: list(value) for key, value in (scripts or {}).items()}
        self.requests = []

    @property
    def capabilities(self):
        return ExecutorCapabilities(
            "scripted",
            ("structured_output", "token_usage", "tool_policy"),
            ("read", "shell", "edit", "write"),
        )

    def execute(self, request):
        self.requests.append(request)
        role = request.task_type.split(".")[-1]
        if role == "explore":
            outputs = {"exploration": {"files": ["app.py"]}}
        elif role == "plan":
            outputs = {"plan": {"steps": ["implement", "test"]}}
        elif role == "implement":
            outputs = {"implementation_summary": "implemented"}
        elif role == "repair":
            outputs = {"repair_summary": "repaired"}
        else:
            outputs = {"verdict": "approve", "findings": [], "score": 0.9}
        if self.scripts.get(role):
            action = self.scripts[role].pop(0)
            if isinstance(action, BaseException):
                raise action
            if callable(action):
                replacement = action(request)
                if replacement is not None:
                    outputs = replacement
            else:
                outputs = action
        return AgentResult("scripted", outputs, "done", tokens_used=5, cost_usd=0.01)


class EngineeringTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        self.task_dir = self.root / "task"

    def tearDown(self):
        self.temporary.cleanup()

    def policy(self, command=None, **overrides):
        values = {
            "check_commands": (
                command
                or (
                    sys.executable,
                    "-c",
                    "import pathlib; assert pathlib.Path('app.py').is_file()",
                ),
            ),
            "require_clean_worktree": False,
            "max_review_cycles": 2,
            "max_agent_calls": 8,
            "max_elapsed_seconds": 60,
            "agent_timeout_seconds": 30,
        }
        values.update(overrides)
        return ProjectPolicy(**values)

    def workflow(self, scripts=None, policy=None, rsi=None):
        executor = ScriptedExecutor(self.workspace, scripts)
        registry = ExecutorRegistry()
        registry.register(executor)
        workflow = EngineeringWorkflow(
            self.workspace,
            self.task_dir,
            registry,
            policy or self.policy(),
            rsi_loop=rsi,
        )
        return workflow, executor

    def prepared(self, scripts=None, policy=None, rsi=None):
        workflow, executor = self.workflow(scripts, policy, rsi)
        plan = workflow.prepare("Change the value safely")
        return workflow, executor, plan

    def test_policy_loads_argument_arrays_and_rejects_unsafe_paths(self):
        config = self.workspace / ".agent-os" / "engineering.json"
        config.parent.mkdir()
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "check_commands": [["python3", "-m", "unittest"]],
                    "protected_paths": [".git", "AGENTS.md"],
                    "require_clean_worktree": False,
                }
            ),
            encoding="utf-8",
        )
        loaded = ProjectPolicy.load(self.workspace)
        self.assertEqual(("python3", "-m", "unittest"), loaded.check_commands[0])

        value = loaded.to_dict()
        value["protected_paths"] = ["../secret"]
        with self.assertRaisesRegex(ContractViolation, "unsafe path"):
            ProjectPolicy.from_dict(value)

        executor = ScriptedExecutor(self.workspace)
        registry = ExecutorRegistry()
        registry.register(executor)
        with self.assertRaisesRegex(ContractViolation, "outside the workspace"):
            EngineeringWorkflow(
                self.workspace,
                self.workspace / ".agent-os" / "tasks" / "task-1",
                registry,
                loaded,
            )
        with self.assertRaisesRegex(ContractViolation, "must not contain"):
            EngineeringWorkflow(
                self.workspace,
                self.root,
                registry,
                loaded,
            )

        agent_os_root = self.root / "agent-os"
        agent_os_root.mkdir()
        with self.assertRaisesRegex(ContractViolation, "outside the Agent OS root"):
            EngineeringWorkflow(
                self.workspace,
                agent_os_root / "tasks" / "task-1",
                registry,
                loaded,
                agent_os_root=agent_os_root,
            )
        with self.assertRaisesRegex(ContractViolation, "must not contain the Agent OS"):
            EngineeringWorkflow(
                self.workspace,
                self.root / "task-parent",
                registry,
                loaded,
                agent_os_root=self.root / "task-parent" / "agent-os",
            )

    def test_plan_digest_detects_tampering(self):
        workflow, _, plan = self.prepared()
        value = json.loads(workflow.plan_path.read_text(encoding="utf-8"))
        value["objective"] = "tampered"
        workflow.plan_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(ContractViolation, "digest mismatch"):
            EngineeringPlan.load(workflow.plan_path)
        self.assertEqual(64, len(plan.digest))

    def test_version_two_plan_remains_loadable_for_migration(self):
        workflow, _, plan = self.prepared()
        value = plan.to_dict()
        value.pop("intent")
        value["schema_version"] = 2
        value.pop("digest")
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        value["digest"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        workflow.plan_path.write_text(json.dumps(value), encoding="utf-8")

        loaded = EngineeringPlan.load(workflow.plan_path)

        self.assertEqual("legacy", loaded.intent["project"]["kinds"][0])
        self.assertEqual(plan.objective, loaded.objective)

    def test_task_directory_cannot_be_reused_for_a_new_plan(self):
        workflow, executor, _ = self.prepared()
        calls_after_first_plan = len(executor.requests)

        with self.assertRaisesRegex(ContractViolation, "use a new task_dir"):
            workflow.prepare("A different objective")
        self.assertEqual(calls_after_first_plan, len(executor.requests))

    def test_execution_requires_approval_bound_to_plan(self):
        workflow, _, plan = self.prepared()
        with self.assertRaisesRegex(ContractViolation, "requires approved_by"):
            workflow.execute("")
        with self.assertRaisesRegex(ContractViolation, "requires plan_digest"):
            workflow.execute("operator")
        with self.assertRaisesRegex(ContractViolation, "does not match"):
            workflow.execute("operator", plan_digest="0" * 64)

    def test_clean_checks_and_independent_review_finish_with_reality_anchor(self):
        rsi = RSILoop(self.root / "learning", clock=lambda: 100.0)
        workflow, executor, plan = self.prepared(rsi=rsi)
        report = workflow.execute("operator", plan.digest)

        self.assertTrue(report["success"])
        self.assertEqual("succeeded", report["phase"])
        self.assertTrue(report["reality_anchor"]["passed"])
        self.assertEqual("implemented", report["implementation"]["summary"])
        self.assertEqual(4, report["agent_calls"])
        self.assertEqual(20, report["tokens_used"])
        self.assertTrue(report["cost_complete"])
        self.assertEqual(0.04, report["cost_usd"])
        self.assertEqual(2, report["preparation_usage"]["agent_calls"])
        review = next(item for item in executor.requests if item.task_type == "engineering.review")
        implement = next(
            item for item in executor.requests if item.task_type == "engineering.implement"
        )
        exploration = next(
            item for item in executor.requests if item.task_type == "engineering.explore"
        )
        planning = next(
            item for item in executor.requests if item.task_type == "engineering.plan"
        )
        self.assertEqual(("read",), exploration.tools)
        self.assertEqual(("read",), planning.tools)
        self.assertEqual(("read",), review.tools)
        self.assertTrue(exploration.reuse_allowed)
        self.assertTrue(planning.reuse_allowed)
        self.assertTrue(review.reuse_allowed)
        self.assertFalse(implement.reuse_allowed)
        feedback = rsi.feedback_journal.read()
        self.assertEqual(1, len(feedback))
        self.assertEqual("engineering-independent-review", feedback[0].source)

    def test_failed_check_triggers_repair_then_recheck(self):
        marker = self.workspace / "fixed.txt"

        def repair(_request):
            marker.write_text("ok", encoding="utf-8")

        command = (
            sys.executable,
            "-c",
            "import pathlib; assert pathlib.Path('fixed.txt').read_text() == 'ok'",
        )
        workflow, executor, plan = self.prepared(
            scripts={"repair": [repair]}, policy=self.policy(command)
        )
        report = workflow.execute("operator", plan.digest)

        self.assertTrue(report["success"])
        self.assertEqual(1, report["review_cycles"])
        self.assertFalse(report["checks"][0]["passed"])
        self.assertTrue(report["checks"][1]["passed"])
        self.assertEqual("check_failure", report["repairs"][0]["reason"])
        repair_request = next(
            item for item in executor.requests if item.task_type == "engineering.repair"
        )
        self.assertIn("write", repair_request.tools)
        self.assertFalse(repair_request.reuse_allowed)

    def test_review_findings_trigger_repair_and_fresh_review(self):
        reviews = [
            {"verdict": "changes_requested", "findings": ["missing edge case"], "score": 0.4},
            {"verdict": "approve", "findings": [], "score": 0.95},
        ]
        workflow, _, plan = self.prepared(scripts={"review": reviews})
        report = workflow.execute("operator", plan.digest)

        self.assertTrue(report["success"])
        self.assertEqual(2, len(report["reviews"]))
        self.assertEqual("review_findings", report["repairs"][0]["reason"])

    def test_review_cycle_limit_fails_closed(self):
        finding = {
            "verdict": "changes_requested",
            "findings": ["still broken"],
            "score": 0.1,
        }
        policy = self.policy(max_review_cycles=1)
        workflow, _, plan = self.prepared(
            scripts={"review": [finding, finding]}, policy=policy
        )
        report = workflow.execute("operator", plan.digest)

        self.assertFalse(report["success"])
        self.assertEqual("failed", report["phase"])
        self.assertEqual("review_cycle_limit_exhausted", report["failure"])

    def test_agent_call_limit_fails_closed_before_extra_review(self):
        workflow, executor, plan = self.prepared(policy=self.policy(max_agent_calls=3))

        with self.assertRaisesRegex(ContractViolation, "max_agent_calls exhausted"):
            workflow.execute("operator", plan.digest)

        execution_roles = [
            item.task_type
            for item in executor.requests
            if item.task_type in ("engineering.implement", "engineering.review")
        ]
        self.assertEqual(["engineering.implement"], execution_roles)
        self.assertEqual("failed", workflow.status()["phase"])

    def test_agent_timeout_is_clamped_to_remaining_total_time(self):
        policy = self.policy(max_elapsed_seconds=5, agent_timeout_seconds=30)
        workflow, executor, plan = self.prepared(policy=policy)
        report = workflow.execute("operator", plan.digest)

        self.assertTrue(report["success"])
        execution_requests = [
            item
            for item in executor.requests
            if item.task_type in ("engineering.implement", "engineering.review")
        ]
        self.assertTrue(execution_requests)
        self.assertTrue(
            all(1 <= item.timeout_seconds <= 5 for item in execution_requests)
        )

    def test_protected_path_modification_is_rejected(self):
        protected = self.workspace / "AGENTS.md"
        protected.write_text("rules", encoding="utf-8")

        def modify(_request):
            protected.write_text("changed", encoding="utf-8")

        workflow, _, plan = self.prepared(scripts={"implement": [modify]})
        with self.assertRaisesRegex(ContractViolation, "protected paths changed"):
            workflow.execute("operator", plan.digest)
        self.assertEqual("failed", workflow.status()["phase"])

    def test_linked_worktree_git_metadata_and_pointer_are_protected(self):
        repository = self.root / "repository"
        linked = self.root / "linked"
        repository.mkdir()
        commands = (
            ("git", "init", "-q"),
            ("git", "config", "user.email", "test@example.com"),
            ("git", "config", "user.name", "Test User"),
        )
        for command in commands:
            subprocess.run(command, cwd=str(repository), check=True)
        (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        subprocess.run(("git", "add", "app.py"), cwd=str(repository), check=True)
        subprocess.run(
            ("git", "commit", "-qm", "initial"), cwd=str(repository), check=True
        )
        subprocess.run(
            ("git", "worktree", "add", "-qb", "linked-test", str(linked)),
            cwd=str(repository),
            check=True,
        )

        executor = ScriptedExecutor(linked)
        registry = ExecutorRegistry()
        registry.register(executor)
        workflow = EngineeringWorkflow(
            linked,
            self.root / "linked-task",
            registry,
            self.policy(),
        )
        before = workflow._protected_snapshot()
        marker = linked / ".git"
        declaration = marker.read_text(encoding="utf-8").strip()
        git_dir = Path(declaration.split(":", 1)[1].strip()).resolve()
        commondir_value = (git_dir / "commondir").read_text(encoding="utf-8").strip()
        common_dir = (git_dir / commondir_value).resolve()

        head = git_dir / "HEAD"
        original_head = head.read_text(encoding="utf-8")
        head.write_text("ref: refs/heads/tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(ContractViolation, "\\.git/HEAD"):
            workflow._assert_protected_unchanged(before)
        head.write_text(original_head, encoding="utf-8")

        extra_ref = common_dir / "refs" / "heads" / "audit-tamper"
        extra_ref.write_text("0" * 40 + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ContractViolation, "\\.git/refs"):
            workflow._assert_protected_unchanged(before)
        extra_ref.unlink()

        original_marker = marker.read_text(encoding="utf-8")
        marker.write_text("gitdir: /tmp/tampered-git-dir\n", encoding="utf-8")
        with self.assertRaisesRegex(ContractViolation, "'\\.git'"):
            workflow._assert_protected_unchanged(before)
        marker.write_text(original_marker, encoding="utf-8")

    def test_workspace_change_after_plan_requires_a_new_approval(self):
        workflow, _, plan = self.prepared()
        (self.workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

        with self.assertRaisesRegex(ContractViolation, "workspace changed"):
            workflow.execute("operator", plan.digest)

    def test_research_template_is_read_only_and_still_independently_reviewed(self):
        policy = self.policy()
        intent = IntentCompiler().compile(
            "Research the value flow and recommend next steps",
            self.workspace,
            check_commands=policy.check_commands,
        )
        rsi = RSILoop(self.root / "research-learning", clock=lambda: 100.0)
        workflow, executor = self.workflow(policy=policy, rsi=rsi)
        plan = workflow.prepare(intent.objective, intent.to_dict())

        report = workflow.execute("operator", plan.digest)
        implementation = next(
            item for item in executor.requests if item.task_type == "engineering.implement"
        )

        self.assertTrue(report["success"])
        self.assertEqual(("read", "shell"), implementation.tools)
        self.assertNotIn("edit", implementation.tools)
        self.assertNotIn("write", implementation.tools)
        self.assertTrue(implementation.reuse_allowed)
        self.assertEqual(1, len(rsi.feedback_journal.read()))

    def test_research_template_fails_if_executor_changes_workspace(self):
        policy = self.policy()
        intent = IntentCompiler().compile(
            "调研 value 的数据流",
            self.workspace,
            check_commands=policy.check_commands,
        )

        def mutate_during_research(_request):
            (self.workspace / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

        workflow, _ = self.workflow(
            scripts={"implement": [mutate_during_research]}, policy=policy
        )
        plan = workflow.prepare(intent.objective, intent.to_dict())

        with self.assertRaisesRegex(ContractViolation, "read-only research changed"):
            workflow.execute("operator", plan.digest)

    def test_indeterminate_mutating_effect_is_not_replayed(self):
        def uncertain(_request):
            (self.workspace / "maybe.txt").write_text("changed", encoding="utf-8")
            raise RuntimeError("connection lost")

        workflow, executor, plan = self.prepared(scripts={"implement": [uncertain]})
        with self.assertRaisesRegex(RuntimeError, "connection lost"):
            workflow.execute("operator", plan.digest)
        calls_after_failure = len(executor.requests)

        with self.assertRaises(EffectIndeterminateError):
            workflow.execute("operator", plan.digest)
        self.assertEqual(calls_after_failure, len(executor.requests))

    def test_engineer_cli_initializes_policy_without_overwrite_and_reports_status(self):
        environment = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT))
        command = [
            sys.executable,
            "-m",
            "grapheng.cli",
            "engineer",
            "init",
            "--workspace",
            str(self.workspace),
        ]
        created = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        repeated = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        status = subprocess.run(
            [
                sys.executable,
                "-m",
                "grapheng.cli",
                "engineer",
                "status",
                "--task-dir",
                str(self.task_dir),
            ],
            cwd=str(PROJECT_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        missing_digest = subprocess.run(
            [
                sys.executable,
                "-m",
                "grapheng.cli",
                "engineer",
                "run",
                "--workspace",
                str(self.workspace),
                "--task-dir",
                str(self.task_dir),
                "--approved-by",
                "operator",
            ],
            cwd=str(PROJECT_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        noninteractive_ship = subprocess.run(
            [
                sys.executable,
                "-m",
                "grapheng.cli",
                "engineer",
                "ship",
                "--workspace",
                str(self.workspace),
                "--task-dir",
                str(self.task_dir),
                "--objective",
                "Change the value",
                "--approved-by",
                "operator",
            ],
            cwd=str(PROJECT_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, created.returncode, created.stderr)
        self.assertEqual(2, repeated.returncode)
        self.assertIn("already exists", repeated.stderr)
        self.assertEqual("uninitialized", json.loads(status.stdout)["phase"])
        self.assertEqual(2, missing_digest.returncode)
        self.assertIn("requires --plan-digest", missing_digest.stderr)
        self.assertEqual(2, noninteractive_ship.returncode)
        self.assertIn("requires an interactive terminal", noninteractive_ship.stderr)


if __name__ == "__main__":
    unittest.main()
