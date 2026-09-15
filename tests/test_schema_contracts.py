import contextlib
import inspect
import io
import json
import tempfile
import unittest
from pathlib import Path

from grapheng import (
    EffectJournal,
    LocalControlPlane,
    ResidentCoordinator,
    UserTaskModule,
)
from grapheng.checkpoint import CheckpointStore
from grapheng.cli import _dispatch
from grapheng.errors import ContractViolation

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class PersistedSchemaContractTests(unittest.TestCase):
    def test_legacy_checkpoint_fixture_migrates_and_unknown_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            legacy = fixture("checkpoint-v0.json")
            path.write_text(json.dumps(legacy), encoding="utf-8")

            loaded = CheckpointStore(path).load()
            self.assertEqual("legacy-run", loaded.run_id)
            self.assertEqual(1, loaded.to_dict()["schema_version"])

            legacy["schema_version"] = 99
            path.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaisesRegex(ContractViolation, "checkpoint schema"):
                CheckpointStore(path).load()

    def test_legacy_run_fixture_migrates_and_unknown_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "runs" / "legacy-run"
            run_dir.mkdir(parents=True)
            state_path = run_dir / "state.json"
            legacy = fixture("run-state-v0.json")
            state_path.write_text(json.dumps(legacy), encoding="utf-8")
            plane = LocalControlPlane(root)

            self.assertEqual("queued", plane.inspect("legacy-run").phase)
            migrated = plane._read_state("legacy-run")
            self.assertEqual(1, migrated["schema_version"])

            legacy["schema_version"] = 99
            state_path.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaisesRegex(ContractViolation, "run state schema"):
                plane.inspect("legacy-run")
            plane.close()

    def test_legacy_receipt_fixture_migrates_and_unknown_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "legacy-effect.json"
            legacy = fixture("effect-receipt-v0.json")
            path.write_text(json.dumps(legacy), encoding="utf-8")
            journal = EffectJournal(root)

            loaded = journal.inspect("legacy-effect")
            self.assertEqual("completed", loaded.status)
            self.assertEqual(1, loaded.schema_version)

            legacy["schema_version"] = 99
            path.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaisesRegex(ContractViolation, "receipt schema"):
                journal.inspect("legacy-effect")

    def test_task_and_queue_unknown_versions_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tasks = UserTaskModule(root / "task-home")
            task_id = "task-0123456789abcdef"
            task_dir = tasks.tasks_root / task_id
            task_dir.mkdir()
            (task_dir / "task.json").write_text(
                json.dumps(
                    {
                        "schema_version": 99,
                        "task_id": task_id,
                        "kind": "engineering",
                        "workspace": str(root.resolve()),
                        "policy": str((root / "policy.json").resolve()),
                        "created_at": 100.0,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ContractViolation):
                tasks.status(task_id)

            resident = ResidentCoordinator(
                root / "resident-home", task_module_factory=lambda: tasks
            )
            queue = json.loads(resident.queue_path.read_text(encoding="utf-8"))
            queue["schema_version"] = 99
            resident.queue_path.write_text(json.dumps(queue), encoding="utf-8")
            with self.assertRaisesRegex(ContractViolation, "queue.*invalid contract"):
                resident.inspect(task_id)


class PublicApiContractTests(unittest.TestCase):
    def test_minimal_python_api_signatures_match_frozen_fixture(self):
        expected = fixture("public-api-v1.json")["python_signatures"]
        actual = {
            "EffectJournal.inspect": str(inspect.signature(EffectJournal.inspect)),
            "EffectJournal.reconcile": str(inspect.signature(EffectJournal.reconcile)),
            "EffectJournal.reset": str(inspect.signature(EffectJournal.reset)),
            "LocalControlPlane.inspect": str(
                inspect.signature(LocalControlPlane.inspect)
            ),
            "LocalControlPlane.prepare": str(
                inspect.signature(LocalControlPlane.prepare)
            ),
            "ResidentCoordinator.inspect_job": str(
                inspect.signature(ResidentCoordinator.inspect_job)
            ),
            "UserTaskModule.status": str(inspect.signature(UserTaskModule.status)),
        }
        self.assertEqual(expected, actual)

    def test_orca_effect_inspect_json_matches_frozen_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            spec = root / "graph.json"
            spec.write_text(
                json.dumps(
                    {
                        "id": "contract-graph",
                        "require_reality_anchor": False,
                        "nodes": [
                            {
                                "id": "read",
                                "kind": "agent",
                                "writes": ["value"],
                                "agent": {
                                    "executor": "codex",
                                    "prompt": "read only",
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = _dispatch(
                    [
                        "orca-effect",
                        "inspect",
                        str(spec),
                        "--root",
                        str(root / "run"),
                        "--workspace",
                        str(workspace),
                    ]
                )

        self.assertEqual(0, code)
        self.assertEqual(
            fixture("public-api-v1.json")["cli_json"]["orca-effect.inspect"],
            json.loads(output.getvalue()),
        )


if __name__ == "__main__":
    unittest.main()
