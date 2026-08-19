import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grapheng import (
    AgentExecutionError,
    AgentProtocolError,
    AgentRateLimitError,
    AgentRequest,
    AgentTimeoutError,
    CliAgentAdapter,
    ClaudeCodeExecutor,
    CodexExecutor,
    ContractViolation,
    ExecutorRegistry,
    OpenCodeExecutor,
    PiAgentExecutor,
    PolicyRouter,
    discover_local_executors,
)


FIXTURE = Path(__file__).parent / "fixtures" / "fake_agent_cli.py"


def request(workspace, output_keys=("answer",)):
    return AgentRequest(
        task_id="run:node:1",
        prompt="Answer the task",
        inputs={"question": "hello"},
        output_keys=output_keys,
        workspace=workspace,
        tools=("read",),
        timeout_seconds=5,
    )


class AdapterTests(unittest.TestCase):
    def test_discovery_skips_codex_when_safe_startup_probe_reports_missing_binary(self):
        def which(command):
            return "/fake/codex" if command == "codex" else None

        missing_binary = "spawn /fake/vendor/x86_64-apple-darwin/codex ENOENT"
        with patch("grapheng.adapters.shutil.which", side_effect=which), patch(
            "grapheng.adapters.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ("/fake/codex", "exec", "--help"),
                1,
                stdout="",
                stderr=missing_binary,
            ),
        ):
            registry = discover_local_executors()

        self.assertEqual((), registry.capabilities())

    def test_discovery_registers_codex_when_safe_startup_probe_succeeds(self):
        def which(command):
            return "/fake/codex" if command == "codex" else None

        with patch("grapheng.adapters.shutil.which", side_effect=which), patch(
            "grapheng.adapters.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ("/fake/codex", "exec", "--help"),
                0,
                stdout="--json --ephemeral --sandbox --output-schema",
                stderr="",
            ),
        ):
            registry = discover_local_executors()

        self.assertEqual(
            ["codex"],
            [capabilities.executor_id for capabilities in registry.capabilities()],
        )

    def test_discovery_registers_opencode_only_after_safe_run_probe(self):
        def which(command):
            return "/fake/opencode" if command == "opencode" else None

        for returncode, expected in ((0, ["opencode"]), (1, [])):
            with self.subTest(returncode=returncode), patch(
                "grapheng.adapters.shutil.which", side_effect=which
            ), patch(
                "grapheng.adapters.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    ("/fake/opencode", "run", "--help"),
                    returncode,
                    stdout="--format --model",
                    stderr="",
                ),
            ) as run:
                registry = discover_local_executors()

            self.assertEqual(
                expected,
                [item.executor_id for item in registry.capabilities()],
            )
            self.assertEqual(
                ("/fake/opencode", "run", "--help"), run.call_args.args[0]
            )

    def test_claude_adapter_normalizes_result_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = ClaudeCodeExecutor((sys.executable, str(FIXTURE), "claude"))
            result = executor.execute(request(Path(directory)))

        self.assertEqual({"answer": "claude"}, result.outputs)
        self.assertEqual(5, result.tokens_used)
        self.assertEqual("claude-session", result.session_id)

    def test_claude_discovery_falls_back_to_safe_mode_environment(self):
        calls = []

        def which(command):
            return "/fake/claude" if command == "claude" else None

        def runner(command, **kwargs):
            calls.append((tuple(command), kwargs.get("env")))
            if command[-1] == "--help":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        "--print --output-format --json-schema "
                        "--no-session-persistence --permission-mode --tools "
                        "--max-budget-usd"
                    ),
                    stderr="",
                )
            if "--safe-mode" in command:
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="unknown option '--safe-mode'"
                )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "structured_output": {"answer": "claude"},
                        "usage": {"input_tokens": 2, "output_tokens": 3},
                    }
                ),
                stderr="",
            )

        with patch("grapheng.adapters.shutil.which", side_effect=which), patch(
            "grapheng.adapters.subprocess.run", side_effect=runner
        ), tempfile.TemporaryDirectory() as directory:
            registry = discover_local_executors()
            result = registry.execute(
                request(Path(directory)), executor_id="claude-code"
            )

        self.assertEqual({"answer": "claude"}, result.outputs)
        execution_command, environment = calls[-1]
        self.assertNotIn("--safe-mode", execution_command)
        self.assertEqual("1", environment["CLAUDE_CODE_SAFE_MODE"])

    def test_pi_adapter_normalizes_jsonl_result_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = PiAgentExecutor((sys.executable, str(FIXTURE), "pi"))
            result = executor.execute(request(Path(directory)))

        self.assertEqual({"answer": "pi"}, result.outputs)
        self.assertEqual(7, result.tokens_used)
        self.assertEqual(0.02, result.cost_usd)

    def test_codex_adapter_normalizes_jsonl_result_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = CodexExecutor((sys.executable, str(FIXTURE), "codex"))
            result = executor.execute(request(Path(directory)))

        self.assertEqual({"answer": "codex"}, result.outputs)
        self.assertEqual(8, result.tokens_used)
        self.assertEqual("codex-session", result.session_id)

    def test_opencode_adapter_normalizes_jsonl_result_usage_and_session(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = OpenCodeExecutor((sys.executable, str(FIXTURE), "opencode"))
            result = executor.execute(request(Path(directory)))

        self.assertIsInstance(executor, CliAgentAdapter)
        self.assertEqual({"answer": "opencode"}, result.outputs)
        self.assertEqual(11, result.tokens_used)
        self.assertAlmostEqual(0.03, result.cost_usd)
        self.assertEqual("opencode-session", result.session_id)

    def test_opencode_adapter_translates_tools_to_deny_by_default_permissions(self):
        stdout = "\n".join(
            (
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "session",
                        "part": {"text": '{"answer":"ok"}'},
                    }
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "sessionID": "session",
                        "part": {"cost": 0, "tokens": {"total": 1}},
                    }
                ),
            )
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "grapheng.adapters.subprocess.run",
            return_value=subprocess.CompletedProcess(("opencode",), 0, stdout, ""),
        ) as run:
            value = request(Path(directory))
            value = AgentRequest(
                task_id=value.task_id,
                prompt=value.prompt,
                inputs=value.inputs,
                output_keys=value.output_keys,
                workspace=value.workspace,
                model="provider/model",
                tools=("read", "shell", "write"),
                timeout_seconds=value.timeout_seconds,
            )
            OpenCodeExecutor(("opencode",)).execute(value)

        arguments = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        permissions = json.loads(environment["OPENCODE_PERMISSION"])
        self.assertEqual(
            [
                "opencode",
                "run",
                "--format",
                "json",
                "--pure",
                "--model",
                "provider/model",
            ],
            arguments[:-1],
        )
        self.assertEqual("deny", permissions["*"])
        self.assertEqual("allow", permissions["read"])
        self.assertEqual("allow", permissions["glob"])
        self.assertEqual("allow", permissions["bash"])
        self.assertEqual("allow", permissions["edit"])
        self.assertEqual("deny", permissions["question"])
        self.assertEqual("true", environment["OPENCODE_DISABLE_PROJECT_CONFIG"])

    def test_opencode_cost_is_observed_by_rsi_but_not_claimed_as_a_hard_budget(self):
        observations = []
        router = PolicyRouter(observer=observations.append)
        registry = ExecutorRegistry(router)
        registry.register(
            OpenCodeExecutor((sys.executable, str(FIXTURE), "opencode"))
        )
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            result = registry.execute(request(workspace), executor_id="opencode")
            budgeted = request(workspace)
            budgeted = AgentRequest(
                task_id=budgeted.task_id,
                prompt=budgeted.prompt,
                inputs=budgeted.inputs,
                output_keys=budgeted.output_keys,
                workspace=budgeted.workspace,
                tools=budgeted.tools,
                timeout_seconds=budgeted.timeout_seconds,
                max_cost_usd=1.0,
            )
            with self.assertRaisesRegex(ContractViolation, "no agent executor satisfies"):
                registry.execute(budgeted, executor_id="opencode")

        self.assertAlmostEqual(0.03, result.cost_usd)
        self.assertEqual(1, len(observations))
        self.assertEqual("opencode", observations[0].executor_id)
        self.assertTrue(observations[0].success)
        self.assertAlmostEqual(0.03, observations[0].cost_usd)

    def test_opencode_adapter_rejects_unknown_tools_before_process_start(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "grapheng.adapters.subprocess.run"
        ) as run:
            value = request(Path(directory))
            value = AgentRequest(
                task_id=value.task_id,
                prompt=value.prompt,
                inputs=value.inputs,
                output_keys=value.output_keys,
                workspace=value.workspace,
                tools=("browser",),
                timeout_seconds=value.timeout_seconds,
            )
            with self.assertRaisesRegex(ContractViolation, "does not map tool browser"):
                OpenCodeExecutor(("opencode",)).execute(value)

        run.assert_not_called()

    def test_adapter_rejects_output_contract_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = ClaudeCodeExecutor((sys.executable, str(FIXTURE), "claude"))

            with self.assertRaisesRegex(AgentProtocolError, "output contract mismatch"):
                executor.execute(request(Path(directory), output_keys=("different",)))

    def test_cli_faults_are_classified_without_model_calls(self):
        cases = (
            (ClaudeCodeExecutor, "claude", "crash", AgentExecutionError),
            (PiAgentExecutor, "pi", "corrupt", AgentProtocolError),
            (CodexExecutor, "codex", "rate-limit", AgentRateLimitError),
            (OpenCodeExecutor, "opencode", "corrupt", AgentProtocolError),
            (OpenCodeExecutor, "opencode", "event-error", AgentExecutionError),
            (
                OpenCodeExecutor,
                "opencode",
                "event-rate-limit",
                AgentRateLimitError,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for executor_type, mode, fault, error_type in cases:
                with self.subTest(fault=fault):
                    executor = executor_type(
                        (sys.executable, str(FIXTURE), mode, fault)
                    )
                    with self.assertRaises(error_type):
                        executor.execute(request(workspace))

    def test_cli_timeout_is_retryable_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            for executor_type, mode in (
                (CodexExecutor, "codex"),
                (OpenCodeExecutor, "opencode"),
            ):
                with self.subTest(mode=mode):
                    executor = executor_type(
                        (sys.executable, str(FIXTURE), mode, "timeout")
                    )
                    value = request(Path(directory))
                    value = AgentRequest(
                        task_id=value.task_id,
                        prompt=value.prompt,
                        inputs=value.inputs,
                        output_keys=value.output_keys,
                        workspace=value.workspace,
                        tools=value.tools,
                        timeout_seconds=1,
                    )

                    with self.assertRaises(AgentTimeoutError):
                        executor.execute(value)


if __name__ == "__main__":
    unittest.main()
