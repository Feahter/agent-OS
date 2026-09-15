import json
import tempfile
import unittest
from pathlib import Path

from crash_harness import run_crash_scenario

from grapheng import EffectIndeterminateError, EffectJournal
from grapheng._store import atomic_json_write, read_json_object
from grapheng.durable_outbox import DurableOutbox
from grapheng.effects import NodeEffectJournal
from grapheng.leases import LeaseLostError, assert_lease, claim_lease


class CrashInvariantTests(unittest.TestCase):
    def test_takeover_commit_fences_the_stale_token_after_process_death(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {
                "run_id": "run-crash",
                "phase": "queued",
                "owner_id": None,
                "generation": 1,
            }
            stale = claim_lease(
                state,
                run_id="run-crash",
                owner_id="owner-one",
                lease_seconds=30,
                now=100,
                takeover=False,
            )
            state["lease_expires_at"] = 0.0
            atomic_json_write(root / "lease-state.json", state)

            run_crash_scenario(
                "lease_takeover", root, "takeover_committed"
            )
            recovered = dict(read_json_object(root / "lease-state.json"))

            self.assertEqual(2, recovered["generation"])
            self.assertEqual("owner-two", recovered["owner_id"])
            with self.assertRaises(LeaseLostError):
                assert_lease(recovered, stale, now=201)

    def test_completed_effect_commit_restores_without_a_second_external_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_crash_scenario("effect_commit", root, "completed")
            journal = NodeEffectJournal(root / "effects")
            calls = []

            result = journal.execute(
                effect_id="node-crash-effect",
                run_id="run-crash",
                node_id="mutate",
                effect_type="verified_idempotent",
                inputs={"value": 1},
                lease={
                    "run_id": "run-crash",
                    "owner_id": "owner-two",
                    "generation": 2,
                    "token_digest": "b" * 64,
                },
                workspace_identity={"path": str(root.resolve())},
                invoke=lambda: calls.append("replayed"),
                restore=lambda outcome: dict(outcome),
            )

            self.assertEqual({"answer": "committed"}, result)
            self.assertEqual([], calls)
            self.assertEqual(
                {"effect_id": "node-crash-effect"},
                json.loads((root / "external-effect.json").read_text()),
            )

    def test_outbox_ack_remains_idempotent_after_process_death(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_crash_scenario("outbox_ack", root, "ack_committed")
            outbox = DurableOutbox(root, clock=lambda: 200.0)

            first = outbox.inspect("enqueue-00000001")
            repeated = outbox.acknowledge(
                "enqueue-00000001", {"queue_job_id": "job-crash"}
            )

            self.assertEqual("acknowledged", first["state"])
            self.assertEqual(first, repeated)
            self.assertEqual((), outbox.pending())

    def test_crash_before_protected_postcondition_never_accepts_the_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "guard.txt").write_text("approved", encoding="utf-8")
            run_crash_scenario(
                "protected_postcondition", root, "before_postcondition"
            )
            journal = EffectJournal(root / "engineering-effects")
            replayed = []

            receipt = journal.inspect("implement-0")
            with self.assertRaises(EffectIndeterminateError):
                journal.execute(
                    "implement-0",
                    {"task_id": "engineering-implement"},
                    lambda: replayed.append("replayed"),
                )

            self.assertEqual("started", receipt.status)
            self.assertEqual([], replayed)
            self.assertEqual(
                {"guard.txt": "approved"},
                receipt.recovery_context["protected_snapshot"],
            )

    def test_directory_fsync_crash_leaves_only_complete_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "directory-fsync.json"
            atomic_json_write(target, {"version": 1})

            run_crash_scenario(
                "directory_fsync", root, "before_directory_fsync"
            )

            self.assertIn(json.loads(target.read_text()), ({"version": 1}, {"version": 2}))


if __name__ == "__main__":
    unittest.main()
