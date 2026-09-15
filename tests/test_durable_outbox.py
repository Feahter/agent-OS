import tempfile
import unittest
from pathlib import Path

from grapheng import ContractViolation
from grapheng.durable_outbox import DurableOutbox


class DurableOutboxTests(unittest.TestCase):
    def test_duplicate_and_out_of_order_acknowledgments_preserve_pending_intents(self):
        with tempfile.TemporaryDirectory() as directory:
            outbox = DurableOutbox(Path(directory), clock=lambda: 100.0)
            first = outbox.publish(
                "control-00000001",
                "task-0000000000000001",
                "control",
                {"action": "pause"},
            )
            second = outbox.publish(
                "control-00000002",
                "task-0000000000000001",
                "control",
                {"action": "resume"},
            )

            acknowledged = outbox.acknowledge(
                second["intent_id"], {"state": "queued"}
            )
            replayed = outbox.acknowledge(
                second["intent_id"], {"state": "queued"}
            )

            self.assertEqual(1, first["sequence"])
            self.assertEqual(2, second["sequence"])
            self.assertEqual(acknowledged, replayed)
            self.assertEqual(
                (first["intent_id"],),
                tuple(intent["intent_id"] for intent in outbox.pending()),
            )
            with self.assertRaisesRegex(ContractViolation, "different acknowledgment"):
                outbox.acknowledge(second["intent_id"], {"state": "cancelled"})

            outbox.acknowledge(first["intent_id"], {"state": "paused"})
            self.assertEqual((), outbox.pending())


if __name__ == "__main__":
    unittest.main()
