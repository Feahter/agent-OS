import sys
import tempfile
import unittest
from pathlib import Path

from grapheng import (
    AgentExecutionError,
    AgentProtocolError,
    AgentRateLimitError,
    AgentRequest,
    AgentTimeoutError,
    ClaudeCodeExecutor,
    CodexExecutor,
    PiAgentExecutor,
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
