import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

from grapheng import GraphRuntime, GraphSpec, NodeRegistry
from grapheng.effects import NodeEffectJournal
from grapheng.leases import LeaseLostError


class SimulatedProcessCrash(BaseException):
    pass


def effect_graph(effect):
    return GraphSpec.from_dict(
        {
            "id": "effect-protocol",
            "require_reality_anchor": False,
            "nodes": [
                {
                    "id": "mutate",
                    "kind": "mutate",
                    "writes": ["answer"],
                    "effect": effect,
                }
            ],
        }
    )


class ReconcilableHandler:
    def __init__(self, workspace):
        self.workspace = workspace
        self.workspace_identity = {"path": str(workspace.resolve())}
        self.calls = 0
        self.reconciliations = 0

    def __call__(self, context):
        self.calls += 1
        (self.workspace / "effect.txt").write_text(
            context.effect_id, encoding="utf-8"
        )
        raise SimulatedProcessCrash()

    def reconcile_effect(self, context, intent):
        self.reconciliations += 1
        path = self.workspace / "effect.txt"
        if not path.is_file() or path.read_text(encoding="utf-8") != context.effect_id:
            return None
        return {"answer": "reconciled"}


class UnreconcilableHandler(ReconcilableHandler):
    reconcile_effect = None


class FailingReconcileHandler(ReconcilableHandler):
    def reconcile_effect(self, context, intent):
        self.reconciliations += 1
        raise RuntimeError("external status unavailable")


class VerifiedIdempotentHandler:
    def __init__(self, workspace):
        self.workspace = workspace
        self.workspace_identity = {"path": str(workspace.resolve())}
        self.calls = 0

    def __call__(self, context):
        self.calls += 1
        path = self.workspace / "applied-effects.json"
        applied = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        if context.effect_id not in applied:
            applied.append(context.effect_id)
            path.write_text(json.dumps(applied), encoding="utf-8")
        return {"answer": context.effect_id}


class NodeEffectProtocolTests(unittest.TestCase):
    @staticmethod
    def lease(generation=1):
        return {
            "run_id": "run-effect",
            "owner_id": f"owner-{generation}",
            "generation": generation,
            "token_digest": str(generation) * 64,
        }

    def test_verified_idempotent_effect_survives_each_receipt_boundary(self):
        for crash_stage in ("prepared", "executing", "completed"):
            with self.subTest(crash_stage=crash_stage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                handler = VerifiedIdempotentHandler(root)
                registry = NodeRegistry()
                registry.register("mutate", handler)
                runtime_dir = root / "runtime"
                runtime = GraphRuntime(
                    effect_graph("verified_idempotent"),
                    registry,
                    work_dir=runtime_dir,
                    effect_lease=self.lease(),
                )

                def crash(stage, _record, expected=crash_stage):
                    if stage == expected:
                        raise SimulatedProcessCrash()

                runtime.effects = NodeEffectJournal(
                    runtime_dir / "effects", fault_injector=crash
                )
                with self.assertRaises(SimulatedProcessCrash):
                    runtime.run(run_id="run-effect")
                receipt_path = next((runtime_dir / "effects").glob("*.json"))
                interrupted = json.loads(receipt_path.read_text(encoding="utf-8"))

                self.assertEqual(crash_stage, interrupted["state"])
                self.assertEqual(
                    1 if crash_stage == "completed" else 0,
                    handler.calls,
                )

                result = GraphRuntime(
                    effect_graph("verified_idempotent"),
                    registry,
                    work_dir=runtime_dir,
                    effect_lease=self.lease(2),
                ).run(resume=True, run_id="run-effect")
                applied = json.loads(
                    (root / "applied-effects.json").read_text(encoding="utf-8")
                )

                self.assertTrue(result.success)
                self.assertEqual(1, len(applied))
                self.assertEqual(applied[0], result.artifacts["answer"])
                self.assertEqual(1, handler.calls)

    def test_recovery_reconciles_executing_effect_without_replaying_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handler = ReconcilableHandler(root)
            registry = NodeRegistry()
            registry.register("mutate", handler)
            runtime_dir = root / "runtime"
            lease = {
                "run_id": "run-effect",
                "owner_id": "owner-one",
                "generation": 1,
                "token_digest": "a" * 64,
            }

            with self.assertRaises(SimulatedProcessCrash):
                GraphRuntime(
                    effect_graph("reconcilable"),
                    registry,
                    work_dir=runtime_dir,
                    effect_lease=lease,
                ).run(run_id="run-effect")
            receipt_path = next((runtime_dir / "effects").glob("*.json"))
            executing = json.loads(receipt_path.read_text(encoding="utf-8"))

            result = GraphRuntime(
                effect_graph("reconcilable"),
                registry,
                work_dir=runtime_dir,
                effect_lease={**lease, "owner_id": "owner-two", "generation": 2},
            ).run(resume=True, run_id="run-effect")

            self.assertTrue(result.success)
            self.assertEqual("reconciled", result.artifacts["answer"])
            self.assertEqual(1, handler.calls)
            self.assertEqual(1, handler.reconciliations)
            self.assertEqual("executing", executing["state"])
            self.assertEqual(lease, executing["lease"])
            self.assertEqual(handler.workspace_identity, executing["workspace_identity"])
            completed = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual("completed", completed["state"])

    def test_unreconcilable_effect_becomes_indeterminate_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handler = UnreconcilableHandler(root)
            registry = NodeRegistry()
            registry.register("mutate", handler)
            runtime_dir = root / "runtime"
            lease = {
                "run_id": "run-effect",
                "owner_id": "owner-one",
                "generation": 1,
                "token_digest": "b" * 64,
            }
            with self.assertRaises(SimulatedProcessCrash):
                GraphRuntime(
                    effect_graph("reconcilable"),
                    registry,
                    work_dir=runtime_dir,
                    effect_lease=lease,
                ).run(run_id="run-effect")

            result = GraphRuntime(
                effect_graph("reconcilable"),
                registry,
                work_dir=runtime_dir,
                effect_lease={**lease, "generation": 2},
            ).run(resume=True, run_id="run-effect")
            receipt_path = next((runtime_dir / "effects").glob("*.json"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

            self.assertFalse(result.success)
            self.assertEqual(1, handler.calls)
            self.assertEqual("indeterminate", receipt["state"])
            self.assertIn("reconcile", receipt["error"])

    def test_reconcile_failure_becomes_indeterminate_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handler = FailingReconcileHandler(root)
            registry = NodeRegistry()
            registry.register("mutate", handler)
            runtime_dir = root / "runtime"
            with self.assertRaises(SimulatedProcessCrash):
                GraphRuntime(
                    effect_graph("reconcilable"),
                    registry,
                    work_dir=runtime_dir,
                    effect_lease=self.lease(),
                ).run(run_id="run-effect")

            result = GraphRuntime(
                effect_graph("reconcilable"),
                registry,
                work_dir=runtime_dir,
                effect_lease=self.lease(2),
            ).run(resume=True, run_id="run-effect")
            receipt_path = next((runtime_dir / "effects").glob("*.json"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

            self.assertFalse(result.success)
            self.assertEqual(1, handler.calls)
            self.assertEqual(1, handler.reconciliations)
            self.assertEqual("indeterminate", receipt["state"])
            self.assertIn("external status unavailable", receipt["error"])

    def test_stale_owner_is_rejected_at_effect_execution_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handler = VerifiedIdempotentHandler(root)
            registry = NodeRegistry()
            registry.register("mutate", handler)
            main_thread = threading.current_thread()

            @contextmanager
            def ownership_guard():
                if threading.current_thread() is not main_thread:
                    raise LeaseLostError("owner was fenced by takeover")
                yield

            runtime_dir = root / "runtime"
            with self.assertRaises(LeaseLostError):
                GraphRuntime(
                    effect_graph("verified_idempotent"),
                    registry,
                    work_dir=runtime_dir,
                    effect_lease=self.lease(),
                    ownership_guard=ownership_guard,
                ).run(run_id="run-effect")
            receipt_path = next((runtime_dir / "effects").glob("*.json"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

            self.assertEqual(0, handler.calls)
            self.assertEqual("executing", receipt["state"])
            self.assertIn("LeaseLostError", receipt["error"])

    def test_effect_lease_must_match_run_before_handler_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handler = VerifiedIdempotentHandler(root)
            registry = NodeRegistry()
            registry.register("mutate", handler)
            lease = dict(self.lease())
            lease["run_id"] = "another-run"
            runtime_dir = root / "runtime"

            result = GraphRuntime(
                effect_graph("verified_idempotent"),
                registry,
                work_dir=runtime_dir,
                effect_lease=lease,
            ).run(run_id="run-effect")

            self.assertFalse(result.success)
            self.assertEqual(0, handler.calls)
            self.assertEqual([], list((runtime_dir / "effects").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
