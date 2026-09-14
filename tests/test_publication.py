import json
import tempfile
import unittest
from pathlib import Path

from grapheng import (
    AgentNodeHandler,
    AgentRequest,
    AgentResult,
    ExecutorCapabilities,
    ExecutorRegistry,
    GraphRuntime,
    GraphSpec,
    LocalControlPlane,
    NodeRegistry,
    OperationsConsole,
    VerifiedArtifactCache,
    VerifiedResultPublisher,
)


class MemoryExecutor:
    def __init__(self):
        self.calls = 0

    @property
    def capabilities(self):
        return ExecutorCapabilities(
            "memory",
            ("reasoning_control", "structured_output", "tool_policy"),
            ("write",),
        )

    def execute(self, request):
        self.calls += 1
        return AgentResult(
            "memory",
            {"answer": request.inputs["question"].upper()},
            "done",
            tokens_used=7,
            cost_usd=0.2,
        )


class FailingPublicationCache(VerifiedArtifactCache):
    def publish_verified(self, *args, **kwargs):
        raise OSError("temporary publication failure")


def publication_graph(with_overwrite=False, mutating=False):
    nodes = [
        {"id": "seed", "kind": "seed", "writes": ["question"]},
        {
            "id": "answer",
            "kind": "agent",
            "deps": ["seed"],
            "reads": ["question"],
            "writes": ["answer"],
            "agent": {
                "executor": "memory",
                "prompt": "Answer precisely",
                "reuse_scope": "tenant-a",
                "tools": ["write"] if mutating else [],
            },
        },
    ]
    dependency = "answer"
    if with_overwrite:
        nodes.append(
            {
                "id": "rotate-input",
                "kind": "rotate",
                "deps": ["answer"],
                "writes": ["question"],
            }
        )
        dependency = "rotate-input"
    nodes.append(
        {
            "id": "verify",
            "kind": "verify",
            "deps": [dependency],
            "reads": ["answer"],
            "writes": ["verification"],
            "verifier_for": "answer",
            "reality_anchor": True,
            "verified_reuse": {
                "decision_artifact": "verification",
                "passed_path": ["passed"],
                "quality_path": ["quality_score"],
                "minimum_quality_score": 0.9,
            },
        }
    )
    return GraphSpec.from_dict(
        {
            "id": "verified-publication",
            "require_reality_anchor": True,
            "nodes": nodes,
        }
    )


def registry_for(
    graph, executor, cache, workspace, quality=0.95, reasoning_effort=None
):
    executors = ExecutorRegistry(reuse_store=cache)
    executors.register(executor)
    registry = NodeRegistry()
    registry.register("seed", lambda context: {"question": "secret-input"})
    registry.register("rotate", lambda context: {"question": "new-input"})
    registry.register(
        "agent",
        AgentNodeHandler(
            graph, executors, workspace, reasoning_effort=reasoning_effort
        ),
    )
    registry.register(
        "verify",
        lambda context: {
            "verification": {"passed": True, "quality_score": quality}
        },
    )
    return registry


class PublicationTests(unittest.TestCase):
    def test_reality_anchor_automatically_publishes_exact_agent_result(self):
        graph = publication_graph()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = MemoryExecutor()
            registry = registry_for(graph, executor, cache, workspace)
            work_dir = root / "run"
            result = GraphRuntime(
                graph,
                registry,
                work_dir=work_dir,
                verified_result_publisher=VerifiedResultPublisher(
                    cache, work_dir / "verified-publications.json"
                ),
            ).run(run_id="run-1")
            request = AgentRequest(
                "run-2:answer:1",
                "Answer precisely",
                {"question": "secret-input"},
                ("answer",),
                workspace,
                reuse_scope="tenant-a",
            )
            reused = ExecutorRegistry(reuse_store=cache)
            reused.register(executor)
            hit = reused.execute(request, executor_id="memory")
            state_text = (work_dir / "verified-publications.json").read_text(
                encoding="utf-8"
            )
            events = [
                json.loads(line)
                for line in (work_dir / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        self.assertTrue(result.success)
        self.assertEqual("hit", hit.reuse_status)
        self.assertEqual({"answer": "SECRET-INPUT"}, hit.outputs)
        self.assertEqual(1, executor.calls)
        self.assertNotIn("Answer precisely", state_text)
        self.assertNotIn("secret-input", state_text)
        self.assertTrue(
            any(event["event"] == "verified_result_published" for event in events)
        )

    def test_low_quality_verification_is_audited_but_not_published(self):
        graph = publication_graph()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = MemoryExecutor()
            registry = registry_for(graph, executor, cache, workspace, quality=0.5)
            work_dir = root / "run"
            result = GraphRuntime(
                graph,
                registry,
                work_dir=work_dir,
                verified_result_publisher=VerifiedResultPublisher(
                    cache, work_dir / "verified-publications.json"
                ),
            ).run(run_id="run-low")
            entries = tuple((root / "reuse" / "entries").glob("*.json"))
            events = (work_dir / "events.jsonl").read_text(encoding="utf-8")

        self.assertTrue(result.success)
        self.assertEqual((), entries)
        self.assertIn("quality_below_threshold", events)

    def test_publication_preserves_reasoning_effort_in_reuse_key(self):
        graph = publication_graph()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = MemoryExecutor()
            registry = registry_for(
                graph, executor, cache, workspace, reasoning_effort="low"
            )
            work_dir = root / "run"
            result = GraphRuntime(
                graph,
                registry,
                work_dir=work_dir,
                verified_result_publisher=VerifiedResultPublisher(
                    cache, work_dir / "verified-publications.json"
                ),
            ).run(run_id="run-reasoning")
            low_request = AgentRequest(
                "later-low",
                "Answer precisely",
                {"question": "secret-input"},
                ("answer",),
                workspace,
                reuse_scope="tenant-a",
                reasoning_effort="low",
            )
            default_request = AgentRequest(
                "later-default",
                "Answer precisely",
                {"question": "secret-input"},
                ("answer",),
                workspace,
                reuse_scope="tenant-a",
            )
            reused = ExecutorRegistry(reuse_store=cache)
            reused.register(executor)
            low = reused.execute(low_request, executor_id="memory")
            default = reused.execute(default_request, executor_id="memory")

        self.assertTrue(result.success)
        self.assertEqual("hit", low.reuse_status)
        self.assertEqual("miss", default.reuse_status)
        self.assertEqual(2, executor.calls)

    def test_workspace_mutating_agent_is_never_staged_for_reuse(self):
        graph = publication_graph(mutating=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = MemoryExecutor()
            registry = registry_for(graph, executor, cache, workspace)
            work_dir = root / "run"
            result = GraphRuntime(
                graph,
                registry,
                work_dir=work_dir,
                verified_result_publisher=VerifiedResultPublisher(
                    cache, work_dir / "verified-publications.json"
                ),
            ).run(run_id="run-mutating")
            entries = tuple((root / "reuse" / "entries").glob("*.json"))
            events = (work_dir / "events.jsonl").read_text(encoding="utf-8")

        self.assertTrue(result.success)
        self.assertEqual(1, executor.calls)
        self.assertEqual((), entries)
        self.assertIn("mutating_tools=write", events)

    def test_resume_retries_ready_publication_with_historical_input_version(self):
        graph = publication_graph(with_overwrite=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            reuse_root = root / "reuse"
            failing_cache = FailingPublicationCache(reuse_root)
            executor = MemoryExecutor()
            registry = registry_for(
                graph, executor, failing_cache, workspace
            )
            work_dir = root / "run"
            first = GraphRuntime(
                graph,
                registry,
                work_dir=work_dir,
                verified_result_publisher=VerifiedResultPublisher(
                    failing_cache, work_dir / "verified-publications.json"
                ),
            ).run(run_id="run-recover")

            cache = VerifiedArtifactCache(reuse_root)
            second = GraphRuntime(
                graph,
                registry,
                work_dir=work_dir,
                verified_result_publisher=VerifiedResultPublisher(
                    cache, work_dir / "verified-publications.json"
                ),
            ).run(resume=True, run_id="run-recover")
            checkpoint = json.loads(
                (work_dir / "checkpoint.json").read_text(encoding="utf-8")
            )
            question_versions = [
                item["version"]
                for item in checkpoint["artifacts"]
                if item["key"] == "question"
            ]
            request = AgentRequest(
                "later",
                "Answer precisely",
                {"question": "secret-input"},
                ("answer",),
                workspace,
                reuse_scope="tenant-a",
            )
            reused = ExecutorRegistry(reuse_store=cache)
            reused.register(executor)
            hit = reused.execute(request, executor_id="memory")

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual([1, 2], question_versions)
        self.assertEqual("hit", hit.reuse_status)
        self.assertEqual(1, executor.calls)

    def test_control_plane_and_console_share_publication_state(self):
        graph = publication_graph()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            cache = VerifiedArtifactCache(root / "reuse")
            executor = MemoryExecutor()
            registry = registry_for(graph, executor, cache, workspace)
            control_root = root / "control"
            plane = LocalControlPlane(control_root, reuse_store=cache)
            try:
                run_id = plane.submit(graph, registry)
                run = plane.wait(run_id, timeout=2)
            finally:
                plane.close()
            console = OperationsConsole(control_root).snapshot(run_id)

        self.assertEqual("succeeded", run.phase)
        self.assertEqual("published", console["publications"][0]["status"])
        self.assertEqual("answer", console["publications"][0]["source_node_id"])


if __name__ == "__main__":
    unittest.main()
