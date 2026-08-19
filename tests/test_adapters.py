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
    ClaudeCodeExecutor,
    CodexExecutor,
    PiAgentExecutor,
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

    def test_claude_adapter_normalizes_result_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = ClaudeCodeExecutor((sys.executable, str(FIXTURE), "claude"))
            result = executor.execute(request(Path(directory)))

        self.assertEqual({"answer": "claude"}, result.outputs)
        self.assertEqual(5, result.tokens_used)
        self.assertEqual("claude-session", result.session_id)

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
            executor = CodexExecutor(
                (sys.executable, str(FIXTURE), "codex", "timeout")
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
