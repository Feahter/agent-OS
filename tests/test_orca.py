import json
import unittest

from grapheng import (
    AgentProtocolError,
    ContractViolation,
    GraphSpec,
    OrcaBackend,
    OrcaClient,
    OrcaGraphCompiler,
    agent_result_from_worker_done,
    assess_change_set,
    change_set_from_worker_done,
    dispatch_id_from_receipt,
    graph_event_from_orca_message,
)


def graph():
    return GraphSpec.from_dict(
        {
            "id": "orca-graph",
            "require_reality_anchor": False,
            "nodes": [
                {
                    "id": "draft",
                    "kind": "agent",
                    "writes": ["draft"],
                    "retry": {"max_attempts": 2},
                    "agent": {
                        "executor": "codex",
                        "prompt": "draft",
                        "tools": ["read", "write"],
                        "workspace": {"mode": "isolated", "lineage": "child"},
                    },
                },
                {
                    "id": "review",
                    "kind": "agent",
                    "deps": ["draft"],
                    "reads": ["draft"],
                    "writes": ["review"],
                    "gate": "approve-review",
                    "agent": {
                        "executor": "claude-code",
                        "prompt": "review",
                        "tools": ["read"],
                    },
                },
            ],
        }
    )


def controlled_merge_graph():
    return GraphSpec.from_dict(
        {
            "id": "orca-controlled-merge",
            "require_reality_anchor": True,
            "nodes": [
                {
                    "id": "change",
                    "kind": "agent",
                    "writes": ["change_result"],
                    "agent": {
                        "executor": "codex",
                        "prompt": "make change",
                        "workspace": {"mode": "isolated"},
                    },
                    "controlled_merge": {
                        "verifier": "verify",
                        "target_branch": "main",
                    },
                },
                {
                    "id": "verify",
                    "kind": "agent",
                    "deps": ["change"],
                    "reads": ["change_result"],
                    "writes": ["verification"],
                    "gate": "merge-approval",
                    "verifier_for": "change",
                    "reality_anchor": True,
                    "verified_reuse": {
                        "decision_artifact": "verification",
                        "passed_path": ["passed"],
                        "quality_path": ["quality"],
                    },
                    "agent": {"executor": "codex", "prompt": "verify"},
                },
            ],
        }
    )


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.commands = []

    def __call__(self, command, cwd, timeout_seconds):
        self.commands.append(tuple(command))
        return self.responses.pop(0)


class OrcaTests(unittest.TestCase):
    def test_compiler_maps_agents_dependencies_workspaces_and_ownership(self):
        plan = OrcaGraphCompiler().compile(graph(), "ship safely")

        self.assertEqual("graph-engineering", plan.scheduler_owner)
        self.assertEqual("graph-engineering", plan.retry_policy_owner)
        self.assertEqual("graph-engineering", plan.gate_decision_owner)
        self.assertEqual("orca", plan.worker_lifecycle_owner)
        self.assertEqual("codex", plan.tasks[0].agent)
        self.assertEqual("isolated", plan.tasks[0].workspace_mode)
        self.assertEqual(2, plan.tasks[0].max_attempts)
        self.assertEqual(("draft",), plan.tasks[1].deps)
        self.assertEqual("claude", plan.tasks[1].agent)

    def test_compiler_projects_controlled_merge_into_plan_and_worker_contract(self):
        plan = OrcaGraphCompiler().compile(controlled_merge_graph())
        change = plan.task_map()["change"]

        self.assertEqual(
            {"verifier": "verify", "target_branch": "main"},
            change.controlled_merge,
        )
        self.assertEqual(change.controlled_merge, change.to_dict()["controlled_merge"])
        self.assertIn('"owner": "graph-engineering"', change.spec)

    def test_compiler_rejects_unenforceable_cost_budget(self):
        value = graph()
        data = {
            "id": value.id,
            "require_reality_anchor": False,
            "nodes": [
                {
                    "id": "costly",
                    "kind": "agent",
                    "writes": ["answer"],
                    "agent": {
                        "executor": "codex",
                        "prompt": "work",
                        "max_cost_usd": 1.0,
                    },
                }
            ],
        }

        with self.assertRaisesRegex(ContractViolation, "cannot enforce"):
            OrcaGraphCompiler().compile(GraphSpec.from_dict(data))

    def test_compiler_rejects_unenforceable_token_budget(self):
        data = {
            "id": "orca-token-budget",
            "require_reality_anchor": False,
            "nodes": [
                {
                    "id": "costly",
                    "kind": "agent",
                    "writes": ["answer"],
                    "max_tokens": 100,
                    "agent": {
                        "executor": "codex",
                        "prompt": "work",
                    },
                }
            ],
        }

        with self.assertRaisesRegex(ContractViolation, "cannot enforce"):
            OrcaGraphCompiler().compile(GraphSpec.from_dict(data))

    def test_backend_materializes_run_tasks_deps_and_gate(self):
        runner = FakeRunner(
            [
                {"ok": True, "result": {"run": {"id": "run-1"}}},
                {"ok": True, "result": {"task": {"id": "task-draft"}}},
                {"ok": True, "result": {"task": {"id": "task-review"}}},
                {"ok": True, "result": {"gate": {"id": "gate-review"}}},
            ]
        )
        backend = OrcaBackend(OrcaClient(("orca",), runner=runner))
        materialized = backend.materialize(OrcaGraphCompiler().compile(graph()))

        self.assertEqual("run-1", materialized.run_id)
        self.assertEqual("task-review", materialized.task_ids["review"])
        self.assertEqual("gate-review", materialized.gate_ids["review"])
        review_command = runner.commands[2]
        deps = review_command[review_command.index("--deps") + 1]
        self.assertEqual(["task-draft"], json.loads(deps))
        self.assertEqual("--json", review_command[-1])

    def test_backend_starts_isolated_worker_and_uses_explicit_retry(self):
        runner = FakeRunner([{"ok": True, "result": {"dispatch": {"id": "dispatch-2"}}}])
        backend = OrcaBackend(OrcaClient(("orca",), runner=runner))
        plan = OrcaGraphCompiler().compile(graph())
        materialized = type(
            "Materialized",
            (),
            {"task_ids": {"draft": "task-draft"}},
        )()

        backend.start_worker(
            plan, materialized, "draft", attempt=2, retry_of="dispatch-1"
        )

        command = runner.commands[0]
        self.assertIn("new-child", command)
        self.assertIn("--setup", command)
        self.assertEqual("ge-draft-a2", command[command.index("--name") + 1])
        self.assertEqual("dispatch-1", command[command.index("--retry-of") + 1])

    def test_backend_wait_ack_reply_stop_and_cleanup_commands_are_explicit(self):
        runner = FakeRunner(
            [
                {"ok": True, "result": {"count": 0}},
                {"ok": True, "result": {"acknowledged": True}},
                {"ok": True, "result": {"message": {"id": "message-1"}}},
                {"ok": True, "result": {"dispatch": {"id": "dispatch-1"}}},
                {"ok": True, "result": {"released": True}},
                {"ok": True, "result": {"retained": True}},
            ]
        )
        backend = OrcaBackend(OrcaClient(("orca",), runner=runner))

        backend.wait_delivery(timeout_ms=1234)
        backend.acknowledge_delivery("delivery-1")
        backend.reply("message-1", "continue")
        backend.stop_worker("dispatch-1")
        backend.finish_worker("dispatch-1", "never", True)
        backend.finish_worker("dispatch-2", "on_failure", False)

        self.assertEqual(
            (
                "orca",
                "orchestration",
                "check",
                "--wait",
                "--types",
                "worker_done,escalation,question",
                "--timeout-ms",
                "1234",
                "--json",
            ),
            runner.commands[0],
        )
        self.assertIn(("--ack", "delivery-1"), tuple(zip(runner.commands[1], runner.commands[1][1:])))
        self.assertIn(("--id", "message-1"), tuple(zip(runner.commands[2], runner.commands[2][1:])))
        self.assertIn("worker-stop", runner.commands[3])
        self.assertIn("worker-release", runner.commands[4])
        self.assertIn("worker-retain", runner.commands[5])

    def test_dispatch_receipt_normalizes_supported_id_shapes(self):
        self.assertEqual(
            "dispatch-1",
            dispatch_id_from_receipt({"dispatch": {"id": "dispatch-1"}}),
        )
        self.assertEqual(
            "dispatch-2", dispatch_id_from_receipt({"dispatchId": "dispatch-2"})
        )

    def test_backend_enforces_retry_limit_and_resolves_gate_explicitly(self):
        runner = FakeRunner([{"ok": True, "result": {"gate": {"status": "resolved"}}}])
        backend = OrcaBackend(OrcaClient(("orca",), runner=runner))
        plan = OrcaGraphCompiler().compile(graph())
        materialized = type(
            "Materialized",
            (),
            {
                "task_ids": {"draft": "task-draft"},
                "gate_ids": {"review": "gate-review"},
            },
        )()

        with self.assertRaisesRegex(ContractViolation, "exceeds max_attempts"):
            backend.start_worker(
                plan, materialized, "draft", attempt=3, retry_of="dispatch-2"
            )

        backend.resolve_gate(materialized, "review", "approved")
        command = runner.commands[0]
        self.assertIn("gate-resolve", command)
        self.assertEqual("gate-review", command[command.index("--id") + 1])

    def test_worker_done_normalizes_result_and_graph_event(self):
        message = {
            "type": "worker_done",
            "outcome": "succeeded",
            "dispatchId": "dispatch-1",
            "subject": "done",
            "body": "complete",
            "payload": {
                "outputs": {"answer": 42},
                "text": "final",
                "tokens_used": 11,
                "cost_usd": 0.03,
            },
        }

        result = agent_result_from_worker_done(message, "codex", ("answer",))
        event = graph_event_from_orca_message(message, "run-1", "graph-1", "node-1", 1)

        self.assertEqual({"answer": 42}, result.outputs)
        self.assertEqual(11, result.tokens_used)
        self.assertEqual("dispatch-1", result.session_id)
        self.assertEqual("node_completed", event.event)
        self.assertEqual("orca", event.payload["backend"])

    def test_worker_done_requires_structured_payload(self):
        with self.assertRaisesRegex(AgentProtocolError, "structured outputs"):
            agent_result_from_worker_done(
                {"type": "worker_done", "outcome": "succeeded"},
                "codex",
                ("answer",),
            )

    def test_change_set_requires_gate_and_blocks_conflicts(self):
        message = {
            "payload": {
                "changes": {
                    "workspace_id": "repo::/tmp/worktree",
                    "base_ref": "abc",
                    "head_ref": "def",
                    "files_modified": ["grapheng/orca.py"],
                    "conflicts": [],
                }
            }
        }
        change_set = change_set_from_worker_done(message)

        self.assertEqual("awaiting_gate", assess_change_set(change_set, False))
        self.assertEqual("ready", assess_change_set(change_set, True))

        conflicted = type(
            "Conflicted", (), {"conflicts": ("grapheng/orca.py",)}
        )()
        self.assertEqual("conflicted", assess_change_set(conflicted, True))


if __name__ == "__main__":
    unittest.main()
