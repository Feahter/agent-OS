import json
import tempfile
import unittest
from pathlib import Path

from grapheng import (
    AgentNodeHandler,
    AgentRequest,
    AgentResult,
    ContractViolation,
    ExecutorCapabilities,
    ExecutorRegistry,
    GraphRuntime,
    GraphSpec,
    NodeRegistry,
)


class MemoryExecutor:
    def __init__(
        self,
        executor_id="memory",
        features=("structured_output", "tool_policy"),
        tools=("read",),
    ):
        self._capabilities = ExecutorCapabilities(executor_id, features, tools)
        self.requests = []

    @property
    def capabilities(self):
        return self._capabilities

    def execute(self, request):
        self.requests.append(request)
        return AgentResult(
            self.capabilities.executor_id,
            {"answer": request.inputs["question"].upper()},
            "done",
            tokens_used=9,
        )


class AgentTests(unittest.TestCase):
    def test_registry_routes_by_capabilities_and_tools(self):
        registry = ExecutorRegistry()
        weak = MemoryExecutor("weak", features=(), tools=())
        strong = MemoryExecutor("strong")
        registry.register(weak)
        registry.register(strong)
        with tempfile.TemporaryDirectory() as directory:
            request = AgentRequest(
                "task",
                "prompt",
                {"question": "hi"},
                ("answer",),
                Path(directory),
                tools=("read",),
            )
            result = registry.execute(request, required_features=("structured_output",))

        self.assertEqual("strong", result.executor_id)

    def test_registry_reports_unsatisfied_requirements(self):
        registry = ExecutorRegistry()
        registry.register(MemoryExecutor())
        with tempfile.TemporaryDirectory() as directory:
            request = AgentRequest("task", "prompt", {}, ("answer",), Path(directory))

            with self.assertRaisesRegex(ContractViolation, "no agent executor satisfies"):
                registry.execute(request, required_features=("streaming",))

    def test_registry_requires_cost_budget_capability_when_requested(self):
        registry = ExecutorRegistry()
        registry.register(MemoryExecutor())
        with tempfile.TemporaryDirectory() as directory:
            request = AgentRequest(
                "task",
                "prompt",
                {},
                ("answer",),
                Path(directory),
                max_cost_usd=1.0,
            )

            with self.assertRaisesRegex(ContractViolation, "cost_budget"):
                registry.execute(request)

    def test_graph_routes_agent_node_and_commits_outputs(self):
        graph = GraphSpec.from_dict(
            {
                "id": "agent-graph",
                "require_reality_anchor": False,
                "max_tokens": 20,
                "nodes": [
                    {"id": "seed", "kind": "seed", "writes": ["question"]},
                    {
                        "id": "answer",
                        "kind": "agent",
                        "deps": ["seed"],
                        "reads": ["question"],
                        "writes": ["answer"],
                        "estimated_tokens": 5,
                        "max_tokens": 10,
                        "agent": {
                            "executor": "memory",
                            "prompt": "Answer",
                            "required_capabilities": ["structured_output"],
                            "tools": ["read"],
                            "task_type": "coding",
                            "model_family": "general-purpose",
                        },
                    },
                ],
            }
        )
        executors = ExecutorRegistry()
        memory = MemoryExecutor()
        executors.register(memory)
        nodes = NodeRegistry()
        nodes.register("seed", lambda context: {"question": "hello"})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            state = root / "state"
            workspace.mkdir()
            nodes.register("agent", AgentNodeHandler(graph, executors, workspace))
            result = GraphRuntime(graph, nodes, work_dir=state).run()
            events = [json.loads(line) for line in (state / "events.jsonl").read_text().splitlines()]

        self.assertTrue(result.success)
        self.assertEqual("HELLO", result.artifacts["answer"])
        self.assertEqual(9, result.tokens_used)
        self.assertEqual({"question": "hello"}, memory.requests[0].inputs)
        self.assertEqual("coding", memory.requests[0].task_type)
        self.assertEqual("general-purpose", memory.requests[0].model_family)
        completed = next(
            event
            for event in events
            if event["event"] == "node_completed" and event["node_id"] == "answer"
        )
        self.assertEqual("memory", completed["payload"]["metadata"]["executor_id"])

    def test_direct_handler_rejects_isolated_workspace(self):
        graph = GraphSpec.from_dict(
            {
                "id": "isolated",
                "require_reality_anchor": False,
                "nodes": [
                    {
                        "id": "worker",
                        "kind": "agent",
                        "writes": ["answer"],
                        "agent": {
                            "executor": "memory",
                            "prompt": "work",
                            "workspace": {"mode": "isolated"},
                        },
                    }
                ],
            }
        )
        executors = ExecutorRegistry()
        executors.register(MemoryExecutor())
        with tempfile.TemporaryDirectory() as directory:
            handler = AgentNodeHandler(graph, executors, Path(directory))
            nodes = NodeRegistry()
            nodes.register("agent", handler)

            result = GraphRuntime(graph, nodes).run()

        self.assertFalse(result.success)


if __name__ == "__main__":
    unittest.main()
