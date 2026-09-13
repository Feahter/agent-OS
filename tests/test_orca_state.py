import json
import tempfile
import unittest
from pathlib import Path

from grapheng import ContractViolation, GraphSpec, OrcaMaterializedRun
from grapheng.orca_state import ORCA_COORDINATOR_SCHEMA_VERSION, OrcaRunStore


def graph(gate=None):
    node = {
        "id": "plan",
        "kind": "agent",
        "deps": [],
        "reads": [],
        "writes": ["plan_out"],
        "retry": {"max_attempts": 1},
        "estimated_tokens": 0,
        "agent": {"executor": "codex", "prompt": "run plan"},
    }
    if gate is not None:
        node["gate"] = gate
    return GraphSpec.from_dict(
        {"id": "state-graph", "nodes": [node], "max_concurrency": 1}
    )


class OrcaRunStoreTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.addCleanup(self._directory.cleanup)

    def store(self, spec=None, workspace=None):
        return OrcaRunStore(
            self.root / "state.json",
            spec or graph(),
            (workspace or self.workspace).resolve(),
            clock=lambda: 1000.0,
        )

    def test_a_created_document_is_persisted_and_readable(self):
        store = self.store()

        self.assertFalse(store.exists())
        created = store.create()
        self.assertTrue(store.exists())

        loaded = store.read()

        self.assertEqual(created["run_id"], loaded["run_id"])
        self.assertEqual(ORCA_COORDINATOR_SCHEMA_VERSION, loaded["schema_version"])
        self.assertEqual({"plan": "pending"}, loaded["statuses"])

    def test_a_document_from_another_graph_is_rejected(self):
        store = self.store()
        state = store.create()
        state["graph_id"] = "other-graph"
        store.save(state)

        with self.assertRaisesRegex(ContractViolation, "fingerprint"):
            store.read()

    def test_an_unsupported_schema_version_is_rejected(self):
        store = self.store()
        state = store.create()
        state["schema_version"] = 99
        store.save(state)

        with self.assertRaisesRegex(ContractViolation, "unsupported"):
            store.read()

    def test_a_v1_document_is_migrated_and_its_fingerprint_recognized(self):
        from grapheng.orca_protocol import legacy_graph_fingerprint

        spec = graph()
        store = self.store(spec)
        state = store.create()
        state["schema_version"] = 1
        state["graph_fingerprint"] = legacy_graph_fingerprint(spec)
        state.pop("gate_resolutions")
        state.pop("merge_candidates")
        store.save(state)

        loaded = store.read()

        self.assertEqual(ORCA_COORDINATOR_SCHEMA_VERSION, loaded["schema_version"])
        self.assertEqual({}, loaded["gate_resolutions"])
        self.assertEqual({}, loaded["merge_candidates"])

    def test_a_corrupt_document_is_reported_rather_than_crashing(self):
        (self.root / "state.json").write_text("{not json", encoding="utf-8")

        with self.assertRaisesRegex(ContractViolation, "Orca coordinator state"):
            self.store().read()

    def test_a_gate_resolution_for_an_ungated_node_is_rejected(self):
        store = self.store()
        state = store.create()
        state["gate_resolutions"] = {"plan": "approved"}

        with self.assertRaisesRegex(ContractViolation, "gate resolution"):
            store.validate_merge_state(state)

    def test_an_unknown_gate_resolution_value_is_rejected(self):
        store = self.store(graph(gate="release"))
        state = store.create()
        state["gate_resolutions"] = {"plan": "maybe"}

        with self.assertRaisesRegex(ContractViolation, "gate resolution"):
            store.validate_merge_state(state)

    def test_missing_merge_containers_are_rejected(self):
        store = self.store()
        state = store.create()
        state.pop("merge_candidates")

        with self.assertRaisesRegex(ContractViolation, "controlled merge"):
            store.validate_merge_state(state)

    def test_materialized_tasks_must_match_the_graph(self):
        store = self.store()

        store.validate_materialized(
            OrcaMaterializedRun.from_dict(
                {"run_id": "orca-1", "task_ids": {"plan": "t-1"}, "gate_ids": {}}
            )
        )
        with self.assertRaisesRegex(ContractViolation, "tasks do not match"):
            store.validate_materialized(
                OrcaMaterializedRun.from_dict(
                    {"run_id": "orca-1", "task_ids": {"other": "t-1"}, "gate_ids": {}}
                )
            )

    def test_materialized_gates_must_match_the_graph(self):
        store = self.store(graph(gate="release"))

        with self.assertRaisesRegex(ContractViolation, "gates do not match"):
            store.validate_materialized(
                OrcaMaterializedRun.from_dict(
                    {"run_id": "orca-1", "task_ids": {"plan": "t-1"}, "gate_ids": {}}
                )
            )

    def test_saved_documents_use_the_canonical_encoding(self):
        store = self.store()
        store.create()

        text = (self.root / "state.json").read_text(encoding="utf-8")

        self.assertEqual(text, json.dumps(json.loads(text), ensure_ascii=False,
                                         sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    unittest.main()
