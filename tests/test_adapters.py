import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grapheng import (
    AgentExecutionError,
    AgentOutputTooLargeError,
    AgentProtocolError,
    AgentRateLimitError,
    AgentRequest,
    AgentResult,
    AgentTimeoutError,
    ClaudeCodeExecutor,
    CliAgentAdapter,
    CodexExecutor,
    ContractViolation,
    ExecutorRegistry,
    ModelUsage,
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

        def runner(command, **kwargs):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout="codex-cli 0.149.0-alpha.4.1",
                    stderr="",
                )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="--json --ephemeral --sandbox --output-schema --config",
                stderr="",
            )

        with patch("grapheng.adapters.shutil.which", side_effect=which), patch(
            "grapheng.adapters.subprocess.run",
            side_effect=runner,
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
            def runner(command, _returncode=returncode, **kwargs):
                if command[-1] == "--version":
                    return subprocess.CompletedProcess(
                        command, 0, stdout="opencode 1.18.18", stderr=""
                    )
                return subprocess.CompletedProcess(
                    command,
                    _returncode,
                    stdout="--format --model --pure",
                    stderr="",
                )

            with self.subTest(returncode=returncode), patch(
                "grapheng.adapters.shutil.which", side_effect=which
            ), patch(
                "grapheng.adapters.subprocess.run",
                side_effect=runner,
            ) as run:
                registry = discover_local_executors()

            self.assertEqual(
                expected,
                [item.executor_id for item in registry.capabilities()],
            )
            self.assertIn(
                ["/fake/opencode", "run", "--help"],
                [call.args[0] for call in run.call_args_list],
            )

    def test_discovery_rejects_protocol_compatible_unverified_version(self):
        def which(command):
            return "/fake/codex" if command == "codex" else None

        def runner(command, **kwargs):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(
                    command, 0, stdout="codex-cli 9.9.9", stderr=""
                )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="--json --ephemeral --sandbox --output-schema --config",
                stderr="",
            )

        with patch("grapheng.adapters.shutil.which", side_effect=which), patch(
            "grapheng.adapters.subprocess.run", side_effect=runner
        ):
            registry = discover_local_executors()

        self.assertEqual((), registry.capabilities())

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
            if command[-1] == "--version":
                return subprocess.CompletedProcess(
                    command, 0, stdout="2.1.241 (Claude Code)", stderr=""
                )
            if command[-1] == "--help":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=(
                        "--print --output-format --json-schema "
                        "--no-session-persistence --permission-mode --tools "
                        "--max-budget-usd --effort"
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

        def execution(command, **kwargs):
            return runner(list(command), **kwargs)

        with patch("grapheng.adapters.shutil.which", side_effect=which), patch(
            "grapheng.adapters.subprocess.run", side_effect=runner
        ), patch(
            "grapheng.adapters.run_bounded_process", side_effect=execution
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
        self.assertEqual(2, result.usage.input_tokens)
        self.assertEqual(1, result.usage.cached_input_tokens)
        self.assertEqual(3, result.usage.output_tokens)
        self.assertEqual(7, result.usage.total_tokens)
        self.assertTrue(result.usage.input_tokens_complete)
        self.assertTrue(result.usage.cached_input_tokens_complete)
        self.assertTrue(result.usage.output_tokens_complete)
        self.assertTrue(result.usage.total_tokens_complete)

    def test_pi_adapter_derives_total_only_when_all_provider_components_exist(self):
        messages = (
            (
                {
                    "input": 2,
                    "output": 3,
                    "cacheRead": 1,
                    "cacheWrite": 1,
                    "cost": {"total": 0.02},
                },
                7,
                True,
            ),
            ({"input": 2, "output": 3, "cost": {"total": 0.02}}, 0, False),
        )
        with tempfile.TemporaryDirectory() as directory:
            for usage, expected_total, total_complete in messages:
                event = {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": '{"answer":"pi"}'}
                        ],
                        "stopReason": "stop",
                        "usage": usage,
                    },
                }
                completed = subprocess.CompletedProcess(
                    ("pi",), 0, stdout=json.dumps(event), stderr=""
                )
                executor = PiAgentExecutor(("pi",))
                with self.subTest(usage=usage), patch.object(
                    executor, "run_cli", return_value=completed
                ):
                    result = executor.execute(request(Path(directory)))

                self.assertEqual(expected_total, result.tokens_used)
                self.assertEqual(total_complete, result.usage.total_tokens_complete)
                self.assertEqual(2, result.usage.input_tokens)
                self.assertEqual(3, result.usage.output_tokens)
                self.assertEqual(
                    usage.get("cacheRead"), result.usage.cached_input_tokens
                )
                self.assertEqual(
                    "cacheRead" in usage,
                    result.usage.cached_input_tokens_complete,
                )

    def test_codex_adapter_normalizes_jsonl_result_and_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = CodexExecutor((sys.executable, str(FIXTURE), "codex"))
            result = executor.execute(request(Path(directory)))

        self.assertEqual({"answer": "codex"}, result.outputs)
        self.assertEqual(8, result.tokens_used)
        self.assertEqual("codex-session", result.session_id)
        self.assertEqual(5, result.usage.input_tokens)
        self.assertEqual(3, result.usage.cached_input_tokens)
        self.assertEqual(3, result.usage.output_tokens)
        self.assertEqual(8, result.usage.total_tokens)
        self.assertTrue(result.usage.input_tokens_complete)
        self.assertTrue(result.usage.cached_input_tokens_complete)
        self.assertTrue(result.usage.output_tokens_complete)
        self.assertTrue(result.usage.total_tokens_complete)
        self.assertEqual(
            result.usage.input_tokens + result.usage.output_tokens,
            result.tokens_used,
        )

    def test_supported_adapters_forward_reasoning_effort(self):
        claude = subprocess.CompletedProcess(
            ("claude",),
            0,
            stdout=json.dumps(
                {
                    "is_error": False,
                    "structured_output": {"answer": "claude"},
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ),
            stderr="",
        )
        pi = subprocess.CompletedProcess(
            ("pi",),
            0,
            stdout=json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": '{"answer":"pi"}'}
                        ],
                        "stopReason": "stop",
                        "usage": {
                            "input": 1,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                            "output": 1,
                        },
                    },
                }
            ),
            stderr="",
        )
        codex = subprocess.CompletedProcess(
            ("codex",),
            0,
            stdout="\n".join(
                (
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": '{"answer":"codex"}',
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 1,
                                "cached_input_tokens": 0,
                                "output_tokens": 1,
                                "total_tokens": 2,
                            },
                        }
                    ),
                )
            ),
            stderr="",
        )
        cases = (
            (ClaudeCodeExecutor(("claude",), safe_mode_flag=False), claude, "--effort", "low"),
            (PiAgentExecutor(("pi",)), pi, "--thinking", "low"),
            (
                CodexExecutor(("codex",)),
                codex,
                "--config",
                'model_reasoning_effort="low"',
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for executor, completed, flag, value in cases:
                agent_request = request(workspace)
                agent_request = AgentRequest(
                    task_id=agent_request.task_id,
                    prompt=agent_request.prompt,
                    inputs=agent_request.inputs,
                    output_keys=agent_request.output_keys,
                    workspace=agent_request.workspace,
                    tools=agent_request.tools,
                    timeout_seconds=agent_request.timeout_seconds,
                    reasoning_effort="low",
                )
                with self.subTest(executor=executor.capabilities.executor_id), patch.object(
                    executor, "run_cli", return_value=completed
                ) as run:
                    executor.execute(agent_request)

                arguments = run.call_args.args[0]
                position = arguments.index(flag)
                self.assertEqual(value, arguments[position + 1])

    def test_agent_result_distinguishes_measured_zero_from_unknown_cost(self):
        measured = AgentResult("test", {}, "", cost_usd=0.0)
        unknown = AgentResult("test", {}, "", cost_usd=None)

        self.assertEqual(0.0, measured.usage.cost_usd)
        self.assertTrue(measured.usage.cost_complete)
        self.assertIsNone(unknown.usage.cost_usd)
        self.assertFalse(unknown.usage.cost_complete)

    def test_usage_combination_sums_known_values_without_claiming_completeness(self):
        combined = ModelUsage.combine(
            (
                ModelUsage(
                    input_tokens=2,
                    output_tokens=1,
                    total_tokens=3,
                    cost_usd=0.0,
                    input_tokens_complete=True,
                    output_tokens_complete=True,
                    total_tokens_complete=True,
                    cost_complete=True,
                ),
                ModelUsage(
                    input_tokens=4,
                    total_tokens=4,
                    input_tokens_complete=True,
                    output_tokens_complete=False,
                    total_tokens_complete=True,
                ),
            )
        )

        self.assertEqual(6, combined.input_tokens)
        self.assertTrue(combined.input_tokens_complete)
        self.assertEqual(1, combined.output_tokens)
        self.assertFalse(combined.output_tokens_complete)
        self.assertEqual(0.0, combined.cost_usd)
        self.assertFalse(combined.cost_complete)

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
            "grapheng.adapters.run_bounded_process",
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


    def test_agent_output_ceiling_terminates_the_process(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AGENT_OS_MAX_AGENT_OUTPUT_BYTES": "4096"}
        ):
            executor = PiAgentExecutor(
                (sys.executable, str(FIXTURE), "pi", "flood")
            )
            with self.assertRaises(AgentOutputTooLargeError) as raised:
                executor.execute(request(Path(directory)))

        self.assertIn("output ceiling", str(raised.exception))

    def test_agent_output_ceiling_rejects_an_unusable_configuration(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AGENT_OS_MAX_AGENT_OUTPUT_BYTES": "10"}
        ):
            executor = PiAgentExecutor((sys.executable, str(FIXTURE), "pi"))
            with self.assertRaises(ContractViolation):
                executor.execute(request(Path(directory)))

    def test_declared_exit_codes_outrank_output_heuristics(self):
        executor = PiAgentExecutor(("pi",))
        completed = subprocess.CompletedProcess(
            ("pi",), 127, stdout="", stderr="rate limit exceeded"
        )

        classification = executor.classify_exit(completed)

        self.assertEqual("execution", classification.kind)
        self.assertEqual("exit_code", classification.source)

    def test_rate_limit_heuristic_is_recorded_as_a_heuristic(self):
        executor = PiAgentExecutor(("pi",))
        completed = subprocess.CompletedProcess(
            ("pi",), 29, stdout="", stderr="429 too many requests"
        )

        classification = executor.classify_exit(completed)

        self.assertEqual("rate_limit", classification.kind)
        self.assertEqual("heuristic", classification.source)
        with self.assertRaises(AgentRateLimitError):
            classification.raise_for("pi-agent")

    def test_successful_exit_has_no_classification(self):
        executor = PiAgentExecutor(("pi",))

        self.assertIsNone(
            executor.classify_exit(
                subprocess.CompletedProcess(("pi",), 0, stdout="{}", stderr="")
            )
        )


if __name__ == "__main__":
    unittest.main()
