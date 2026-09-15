import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grapheng import (
    AgentNodeHandler,
    AgentRequest,
    AgentResult,
    AllowListGatePolicy,
    ClaudeCodeExecutor,
    CodexExecutor,
    ContextBudgetExceeded,
    ContextCompiler,
    ContextPolicy,
    ContractViolation,
    ExecutorCapabilities,
    ExecutorRegistry,
    GraphRuntime,
    GraphSpec,
    NodeRegistry,
    OpenCodeExecutor,
    PiAgentExecutor,
)


class MemoryExecutor:
    def __init__(self):
        self.requests = []

    @property
    def capabilities(self):
        return ExecutorCapabilities("memory", ("structured_output",), ())

    def execute(self, request):
        self.requests.append(request)
        return AgentResult("memory", {"answer": request.inputs["question"]}, "done")


class ContextCompilerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.compiler = ContextCompiler()

    def tearDown(self):
        self.temporary.cleanup()

    def request(
        self,
        *,
        task_id="run:node:1",
        inputs=None,
        output_keys=("answer",),
        prompt="Answer the task",
    ):
        return AgentRequest(
            task_id=task_id,
            prompt=prompt,
            inputs={"question": "hello"} if inputs is None else inputs,
            output_keys=output_keys,
            workspace=self.workspace,
        )

    def test_compiles_compact_context_with_exact_byte_attribution(self):
        compiled = self.compiler.compile(
            self.request(inputs={"question": "你好"}),
            ContextPolicy(output_contract="prompt"),
        )

        self.assertEqual(
            'Answer the task\n\nArtifacts:{"question":"你好"}'
            '\nReturn only JSON with keys:["answer"]',
            compiled.body,
        )
        self.assertNotIn('"question": "你好"', compiled.body)
        self.assertEqual(
            compiled.total_bytes,
            compiled.prompt_bytes
            + compiled.artifact_bytes
            + compiled.contract_bytes,
        )
        self.assertEqual(compiled.total_bytes, len(compiled.body.encode("utf-8")))
        self.assertEqual("accepted", compiled.budget.status)
        self.assertGreaterEqual(compiled.budget.remaining_bytes, 0)

    def test_provider_schema_removes_duplicate_output_contract_from_body(self):
        compiled = self.compiler.compile(
            self.request(output_keys=("summary", "answer")),
            ContextPolicy(output_contract="provider_schema"),
        )

        self.assertNotIn("Return only", compiled.body)
        self.assertNotIn("summary", compiled.body)
        self.assertEqual(
            {
                "additionalProperties": False,
                "properties": {"answer": {}, "summary": {}},
                "required": ["answer", "summary"],
                "type": "object",
            },
            json.loads(compiled.output_schema_json),
        )
        self.assertEqual("provider_schema", compiled.inclusions[-1].location)
        self.assertEqual("body.output_contract", compiled.omissions[0].item)
        self.assertEqual(
            compiled.total_bytes,
            len(compiled.body.encode("utf-8"))
            + len(compiled.output_schema_json.encode("utf-8")),
        )

    def test_fixture_context_stays_below_compact_contract_regression_limits(self):
        request = self.request()
        prompt_contract = self.compiler.compile(request, ContextPolicy())
        schema_contract = self.compiler.compile(
            request,
            ContextPolicy(output_contract="provider_schema"),
        )

        # Restoring the former verbose envelope makes both limits fail.  These
        # are byte thresholds, not fabricated token estimates.
        self.assertLessEqual(prompt_contract.total_bytes, 90)
        self.assertLessEqual(schema_contract.total_bytes, 145)

    def test_fingerprint_ignores_runtime_noise_and_mapping_order(self):
        first = self.compiler.compile(
            self.request(
                task_id="run-one",
                inputs={"z": 1, "a": {"two": 2, "one": 1}},
                output_keys=("z", "a"),
            ),
            ContextPolicy(output_contract="provider_schema"),
        )
        second = self.compiler.compile(
            self.request(
                task_id="run-two",
                inputs={"a": {"one": 1, "two": 2}, "z": 1},
                output_keys=("a", "z"),
            ),
            ContextPolicy(output_contract="provider_schema"),
        )

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.body, second.body)
        self.assertEqual(first.output_schema_json, second.output_schema_json)

    def test_artifact_gradient_is_complete_and_monotonic(self):
        previous = 0
        fingerprints = set()
        for size in (1024, 10 * 1024, 100 * 1024):
            with self.subTest(size=size):
                payload = "x" * size
                compiled = self.compiler.compile(
                    self.request(inputs={"payload": payload}),
                    ContextPolicy(max_context_bytes=128 * 1024),
                )
                self.assertIn(payload, compiled.body)
                self.assertGreater(compiled.artifact_bytes, size)
                self.assertGreater(compiled.total_bytes, previous)
                previous = compiled.total_bytes
                fingerprints.add(compiled.fingerprint)
        self.assertEqual(3, len(fingerprints))

    def test_reads_projection_omits_undeclared_large_artifacts_before_budgeting(self):
        graph = GraphSpec.from_dict(
            {
                "id": "context-reads-projection",
                "require_reality_anchor": False,
                "nodes": [
                    {
                        "id": "seed",
                        "kind": "seed",
                        "writes": ["question", "unused"],
                    },
                    {
                        "id": "answer",
                        "kind": "agent",
                        "deps": ["seed"],
                        "reads": ["question"],
                        "writes": ["answer"],
                        "agent": {
                            "executor": "memory",
                            "prompt": "Answer the task",
                        },
                    },
                ],
            }
        )
        memory = MemoryExecutor()
        executors = ExecutorRegistry()
        executors.register(memory)
        nodes = NodeRegistry()
        nodes.register(
            "seed",
            lambda context: {
                "question": "small",
                "unused": "x" * 2048,
            },
        )
        nodes.register("agent", AgentNodeHandler(graph, executors, self.workspace))

        result = GraphRuntime(
            graph,
            nodes,
            work_dir=self.workspace / "projection-state",
        ).run()

        self.assertTrue(result.success)
        compiled = self.compiler.compile(
            memory.requests[0],
            ContextPolicy(max_context_bytes=1024, output_contract="provider_schema"),
        )
        self.assertEqual({"question": "small"}, memory.requests[0].inputs)
        self.assertLess(compiled.total_bytes, 1024)

    def test_budget_overflow_fails_without_truncating_or_starting_adapter(self):
        request = self.request(inputs={"payload": "x" * 2048})
        with self.assertRaises(ContextBudgetExceeded) as raised:
            self.compiler.compile(
                request,
                ContextPolicy(max_context_bytes=1024),
            )
        self.assertGreater(raised.exception.used_bytes, 1024)

        executor = PiAgentExecutor(("pi",), max_context_bytes=1024)
        with patch.object(executor, "run_cli") as run:
            with self.assertRaises(ContextBudgetExceeded):
                executor.execute(request)
        run.assert_not_called()

    def test_rejects_non_json_and_non_finite_artifacts(self):
        for value in ({"bad": object()}, {"bad": float("nan")}):
            with self.subTest(value=value), self.assertRaisesRegex(
                ContractViolation, "finite JSON data"
            ):
                self.compiler.compile(
                    self.request(inputs=value),
                    ContextPolicy(),
                )

    def test_adapters_select_one_shared_contract_transport(self):
        for executor_type in (ClaudeCodeExecutor, CodexExecutor):
            with self.subTest(executor=executor_type.__name__):
                compiled = executor_type(("agent",)).compile_context(self.request())
                self.assertIsNotNone(compiled.output_schema_json)
                self.assertNotIn("Return only", compiled.body)
        for executor_type in (PiAgentExecutor, OpenCodeExecutor):
            with self.subTest(executor=executor_type.__name__):
                compiled = executor_type(("agent",)).compile_context(self.request())
                self.assertIsNone(compiled.output_schema_json)
                self.assertIn("Return only JSON with keys", compiled.body)

    def test_all_adapter_execute_paths_send_the_compiled_body(self):
        request = self.request()
        executors = (
            ClaudeCodeExecutor(("claude",), safe_mode_flag=False),
            PiAgentExecutor(("pi",)),
            CodexExecutor(("codex",)),
            OpenCodeExecutor(("opencode",)),
        )
        for executor in executors:
            with self.subTest(executor=executor.capabilities.executor_id):
                expected = executor.compile_context(request).body
                with patch.object(
                    executor, "run_cli", side_effect=RuntimeError("stop after capture")
                ) as run:
                    with self.assertRaisesRegex(RuntimeError, "stop after capture"):
                        executor.execute(request)
                actual = run.call_args.args[0][-1]
                self.assertEqual(expected, actual)
                self.assertNotIn("Graph execution contract", actual)

    def test_adapter_records_prompt_artifact_contract_bytes_without_content(self):
        executor = PiAgentExecutor(("pi",))
        with patch("grapheng.adapters.telemetry.emit") as emit:
            compiled = executor.compile_context(self.request())

        _, kwargs = emit.call_args
        self.assertEqual("agent.context_compiled", emit.call_args.args[0])
        self.assertEqual(compiled.fingerprint, kwargs["context_fingerprint"])
        self.assertEqual(compiled.prompt_bytes, kwargs["context_prompt_bytes"])
        self.assertEqual(compiled.artifact_bytes, kwargs["context_artifact_bytes"])
        self.assertEqual(compiled.contract_bytes, kwargs["context_contract_bytes"])
        self.assertNotIn("prompt", kwargs)
        self.assertNotIn("workspace", kwargs)

    def test_checkpoint_resume_preserves_context_fingerprint(self):
        graph = GraphSpec.from_dict(
            {
                "id": "context-resume",
                "require_reality_anchor": False,
                "nodes": [
                    {"id": "seed", "kind": "seed", "writes": ["question"]},
                    {
                        "id": "answer",
                        "kind": "agent",
                        "deps": ["seed"],
                        "reads": ["question"],
                        "writes": ["answer"],
                        "gate": "human",
                        "agent": {
                            "executor": "memory",
                            "prompt": "Answer the task",
                        },
                    },
                ],
            }
        )
        memory = MemoryExecutor()
        executors = ExecutorRegistry()
        executors.register(memory)
        nodes = NodeRegistry()
        nodes.register("seed", lambda context: {"question": "stable"})
        state = self.workspace / "state"
        agent_workspace = self.workspace / "workspace"
        agent_workspace.mkdir()
        nodes.register("agent", AgentNodeHandler(graph, executors, agent_workspace))

        first = GraphRuntime(graph, nodes, work_dir=state).run()
        self.assertFalse(first.success)
        self.assertEqual([], memory.requests)
        expected = self.compiler.compile(
            self.request(inputs={"question": "stable"}),
            ContextPolicy(output_contract="provider_schema"),
        )

        second = GraphRuntime(
            graph,
            nodes,
            work_dir=state,
            gate_policy=AllowListGatePolicy({"human"}),
        ).run(resume=True)
        actual = self.compiler.compile(
            memory.requests[0],
            ContextPolicy(output_contract="provider_schema"),
        )

        self.assertTrue(second.success)
        self.assertEqual(expected.fingerprint, actual.fingerprint)


if __name__ == "__main__":
    unittest.main()
