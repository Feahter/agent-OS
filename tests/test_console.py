import tempfile
import unittest
from pathlib import Path

from grapheng import (
    AgentOS,
    ApprovalInbox,
    ContractViolation,
    GateDecision,
    GraphSpec,
    LocalControlPlane,
    NodeOutcome,
    NodeRegistry,
    OperationsAPI,
    OperationsConsole,
    OperationsServer,
)


class ConsoleTests(unittest.TestCase):
    @staticmethod
    def blocked_run(root):
        graph = GraphSpec.from_dict(
            {
                "id": "live-console-test",
                "require_reality_anchor": False,
                "nodes": [
                    {
                        "id": "release",
                        "kind": "release",
                        "writes": ["released"],
                        "gate": "ship",
                    }
                ],
            }
        )
        registry = NodeRegistry()
        registry.register("release", lambda context: {"released": True})
        plane = LocalControlPlane(root)
        run_id = plane.submit(graph, registry)
        plane.wait(run_id, timeout=2)
        plane.close()
        return run_id

    def test_console_projects_graph_lineage_approval_replay_and_cost(self):
        graph = GraphSpec.from_dict(
            {
                "id": "console-test",
                "require_reality_anchor": False,
                "max_cost_usd": 1.0,
                "nodes": [
                    {
                        "id": "seed",
                        "kind": "seed",
                        "writes": ["draft"],
                        "estimated_cost_usd": 0.1,
                    },
                    {
                        "id": "release",
                        "kind": "release",
                        "deps": ["seed"],
                        "reads": ["draft"],
                        "writes": ["released"],
                        "gate": "ship",
                    },
                ],
            }
        )
        registry = NodeRegistry()
        registry.register(
            "seed", lambda context: NodeOutcome({"draft": "ready"}, cost_usd=0.1)
        )
        registry.register(
            "release", lambda context: {"released": context.read("draft")}
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plane = LocalControlPlane(root)
            run_id = plane.submit(graph, registry)
            first = plane.wait(run_id, timeout=2)
            inbox = ApprovalInbox(root)
            pending = inbox.list(run_id)
            agent_os = AgentOS(root / "agent-os")
            console = OperationsConsole(root, inbox, agent_os=agent_os)
            snapshot = console.snapshot(run_id)
            output = console.render(run_id, root / "console.html")
            approved = inbox.decide(run_id, "ship", "allow", "operator", "verified")
            policy = inbox.policy_for(run_id)
            resumed = plane.resume(run_id, registry, policy)
            final = plane.wait(run_id, timeout=2)
            plane.close()

            html = output.read_text(encoding="utf-8")

        self.assertEqual("failed", first.phase)
        self.assertEqual(1, len(pending))
        self.assertEqual("pending", pending[0].status)
        self.assertEqual("allow", approved.status)
        self.assertEqual(GateDecision.ALLOW, policy.decide(graph.nodes[1], {}))
        self.assertIn(resumed.phase, ("queued", "running", "succeeded"))
        self.assertEqual("succeeded", final.phase)
        self.assertEqual("blocked", snapshot["nodes"][1]["status"])
        self.assertEqual(["release"], snapshot["artifacts"][0]["consumers"])
        self.assertAlmostEqual(0.1, snapshot["run"]["cost_usd"])
        self.assertEqual("node_blocked", snapshot["replay"][0]["event"])
        self.assertEqual({}, snapshot["optimization"]["active"])
        self.assertEqual(0, snapshot["agent_os"]["reuse"]["entries"]["valid"])
        self.assertEqual(
            0, snapshot["agent_os"]["reuse"]["singleflight"]["running"]
        )
        self.assertIn("Agent OS 控制台", html)
        self.assertIn("安全复用", html)
        self.assertNotIn("ready", html)

    def test_conditional_snapshot_uses_revision_and_event_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = self.blocked_run(root)
            console = OperationsConsole(root)
            first = console.changes(run_id)
            unchanged = console.changes(
                run_id,
                revision=first["revision"],
                after=first["next_cursor"],
            )
            console.approvals.decide(run_id, "ship", "allow", "operator")
            changed = console.changes(
                run_id,
                revision=first["revision"],
                after=first["next_cursor"],
            )

        self.assertTrue(first["changed"])
        self.assertIsNotNone(first["snapshot"])
        self.assertFalse(unchanged["changed"])
        self.assertIsNone(unchanged["snapshot"])
        self.assertEqual([], unchanged["events"])
        self.assertTrue(changed["changed"])
        self.assertEqual("allow", changed["snapshot"]["approvals"][0]["status"])

    def test_live_api_refreshes_and_writes_only_through_approval_inbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = self.blocked_run(root)
            inbox = ApprovalInbox(root)
            api = OperationsAPI(
                OperationsConsole(root, inbox),
                run_id,
                action_token="test-action-token-1234567890",
            )
            html = api.document()
            initial = api.snapshot()
            request = {
                "gate": "ship",
                "decision": "allow",
                "actor": "operator",
                "note": "verified",
            }
            with self.assertRaisesRegex(PermissionError, "invalid_action_token"):
                api.approve("wrong-action-token", request)
            with self.assertRaisesRegex(ContractViolation, "unknown fields"):
                api.approve(api.action_token, {**request, "unexpected": True})
            result = api.approve(api.action_token, request)
            policy_decision = inbox.policy_for(run_id).decide(
                GraphSpec.from_dict(
                    {
                        "id": "policy-check",
                        "require_reality_anchor": False,
                        "nodes": [
                            {
                                "id": "release",
                                "kind": "release",
                                "gate": "ship",
                            }
                        ],
                    }
                ).nodes[0],
                {},
            )

        self.assertIn("Agent OS 实时操作台", html)
        self.assertTrue(initial["changed"])
        self.assertEqual("allow", result["approval"]["status"])
        self.assertEqual(GateDecision.ALLOW, policy_decision)

    def test_live_api_rejects_invalid_token_and_cursor_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = self.blocked_run(root)
            console = OperationsConsole(root)

            with self.assertRaisesRegex(ContractViolation, "too short"):
                OperationsAPI(console, run_id, action_token="short")

            api = OperationsAPI(
                console,
                run_id,
                action_token="test-action-token-1234567890",
            )
            first = api.snapshot()
            with self.assertRaisesRegex(ContractViolation, "non-negative"):
                api.snapshot(after=-1)
            with self.assertRaisesRegex(ContractViolation, "ahead of the run"):
                api.snapshot(after=first["next_cursor"] + 1)

    def test_live_server_rejects_remote_bind_and_run_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_id = self.blocked_run(root)
            console = OperationsConsole(root)

            with self.assertRaisesRegex(ContractViolation, "loopback"):
                OperationsServer(console, run_id, host="0.0.0.0")
            with self.assertRaisesRegex(ContractViolation, "between 0 and 65535"):
                OperationsServer(console, run_id, port=65536)
            with self.assertRaisesRegex(ContractViolation, "one path segment"):
                console.snapshot("../escape")


if __name__ == "__main__":
    unittest.main()
