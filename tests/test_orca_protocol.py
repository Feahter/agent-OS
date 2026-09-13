import unittest

from grapheng import ContractViolation
from grapheng.orca_protocol import (
    digest,
    dispatch_id,
    latest_dispatch,
    message_id,
    normalize_delivery,
    task_id,
)


class NormalizeDeliveryTests(unittest.TestCase):
    def test_nested_delivery_envelope_is_unwrapped(self):
        value = {
            "delivery": {
                "id": "delivery-1",
                "messages": [{"type": "question", "id": "message-1"}],
            }
        }

        self.assertEqual(
            ("delivery-1", ({"type": "question", "id": "message-1"},)),
            normalize_delivery(value),
        )

    def test_camel_and_snake_case_delivery_ids_are_both_accepted(self):
        for key in ("deliveryId", "delivery_id"):
            with self.subTest(key=key):
                resolved = normalize_delivery(
                    {key: "delivery-2", "messages": [{"type": "question"}]}
                )
                self.assertEqual("delivery-2", resolved[0])

    def test_a_bare_actionable_message_is_treated_as_one_message(self):
        resolved = normalize_delivery(
            {"id": "delivery-3", "type": "worker_done", "taskId": "task-1"}
        )

        self.assertEqual("delivery-3", resolved[0])
        self.assertEqual("worker_done", resolved[1][0]["type"])

    def test_an_empty_delivery_is_reported_as_nothing_to_do(self):
        self.assertIsNone(normalize_delivery({"count": 0}))

    def test_a_delivery_without_an_id_is_rejected(self):
        with self.assertRaisesRegex(ContractViolation, "no id"):
            normalize_delivery({"messages": [{"type": "question"}]})

    def test_non_object_messages_are_rejected(self):
        with self.assertRaisesRegex(ContractViolation, "must be an array"):
            normalize_delivery({"id": "d", "messages": "question"})


class IdentifierTests(unittest.TestCase):
    def test_a_real_message_id_is_preferred_over_a_digest(self):
        self.assertEqual("m-1", message_id({"messageId": "m-1"}))

    def test_a_message_without_an_id_gets_a_stable_digest(self):
        first = message_id({"type": "question", "body": "why"})
        second = message_id({"body": "why", "type": "question"})

        self.assertTrue(first.startswith("digest-"))
        self.assertEqual(first, second)

    def test_a_reply_requires_a_real_id(self):
        with self.assertRaisesRegex(ContractViolation, "replyable"):
            message_id({"type": "question"}, require_real=True)

    def test_lifecycle_ids_are_read_from_the_payload_when_absent_on_top(self):
        self.assertEqual("d-1", dispatch_id({"payload": {"dispatch_id": "d-1"}}))
        self.assertEqual("t-1", task_id({"payload": {"taskId": "t-1"}}))

    def test_missing_lifecycle_ids_are_rejected(self):
        with self.assertRaisesRegex(ContractViolation, "dispatch id"):
            dispatch_id({"payload": {}})
        with self.assertRaisesRegex(ContractViolation, "task id"):
            task_id({"type": "worker_done"})

    def test_digest_rejects_unserializable_payloads(self):
        with self.assertRaisesRegex(ContractViolation, "JSON serializable"):
            digest({"value": object()})


class LatestDispatchTests(unittest.TestCase):
    def test_the_highest_attempt_wins(self):
        state = {
            "dispatches": {
                "d-1": {"node_id": "plan", "attempt": 1},
                "d-2": {"node_id": "plan", "attempt": 3},
                "d-3": {"node_id": "review", "attempt": 9},
            }
        }

        self.assertEqual("d-2", latest_dispatch(state, "plan"))

    def test_an_unknown_node_has_no_dispatch(self):
        self.assertIsNone(latest_dispatch({"dispatches": {}}, "plan"))


if __name__ == "__main__":
    unittest.main()
