import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grapheng import ContractViolation, ResidentCoordinator
from grapheng.resident_archive import ResidentQueueArchive, terminal_fact_evidence


def queue_item(job_id="graph:run-1", state="succeeded", sequence=1):
    kind, reference = job_id.split(":", 1)
    return {
        "job_id": job_id,
        "kind": kind,
        "reference": reference,
        "priority": 0,
        "sequence": sequence,
        "state": state,
        "requested_action": None,
        "submitted_at": 1.0,
        "updated_at": 2.0,
        "attempts": 1,
        "error": None,
        "probe_failures": 0,
        "next_probe_at": None,
        "enqueue_intent_id": None,
        "last_control_intent_id": None,
    }


class ArchivableJob:
    def __init__(self, root):
        self.root = root
        self.phases = {}

    def inspect(self, reference):
        return self.phases.get(reference, "queued")

    def execute(self, reference, control_probe):
        self.phases[reference] = "succeeded"
        return {"phase": "succeeded"}

    def record_failure(self, reference, failure):
        self.phases[reference] = "failed"

    def terminal_fact(self, reference, phase):
        return terminal_fact_evidence(self.root / f"{reference}.json", phase)

    def write_fact(self, reference, phase):
        path = self.root / f"{reference}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"phase": phase}), encoding="utf-8")
        self.phases[reference] = phase
        return path


class ResidentQueueArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_archive_is_durable_auditable_and_idempotent(self):
        fact = self.root / "fact.json"
        fact.write_text('{"phase":"succeeded"}', encoding="utf-8")
        evidence = terminal_fact_evidence(fact, "succeeded")
        entries = {
            "graph:run-1": {
                "queue_item": queue_item(),
                "terminal_fact": evidence,
            }
        }
        archive = ResidentQueueArchive(self.root / "archive")

        archive_id, path = archive.store(entries, archived_at=10.0)
        replayed_id, replayed_path = archive.store(entries, archived_at=20.0)

        self.assertEqual(archive_id, replayed_id)
        self.assertEqual(path, replayed_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(10.0, payload["archived_at"])
        self.assertEqual(entries, payload["entries"])
        self.assertEqual(1, len(tuple((self.root / "archive").glob("*.json"))))

    def test_archive_rejects_nonterminal_entries(self):
        fact = self.root / "fact.json"
        fact.write_text('{"phase":"queued"}', encoding="utf-8")
        archive = ResidentQueueArchive(self.root / "archive")

        with self.assertRaisesRegex(ContractViolation, "entry is invalid"):
            archive.store(
                {
                    "graph:run-1": {
                        "queue_item": queue_item(state="queued"),
                        "terminal_fact": {
                            "path": str(fact),
                            "phase": "queued",
                            "sha256": "0" * 64,
                        },
                    }
                },
                archived_at=10.0,
            )


class ResidentQueuePruneTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock = [100.0]
        self.job = ArchivableJob(self.root / "facts")
        self.coordinator = ResidentCoordinator(
            self.root,
            task_module_factory=lambda: object(),
            job_handlers={"graph": self.job},
            clock=lambda: self.clock[0],
        )

    def tearDown(self):
        self.coordinator.projections.close()
        self.temporary.cleanup()

    def _terminal_job(self, reference="run-1", phase="succeeded"):
        scheduled = self.coordinator.schedule("graph", reference)
        self.job.write_fact(reference, phase)
        self.coordinator._settle(scheduled["job_id"], phase, None)
        return scheduled

    def test_prune_only_removes_old_terminal_jobs_with_matching_final_fact(self):
        archived = self._terminal_job()
        missing = self.coordinator.schedule("graph", "run-missing")
        self.coordinator._settle(missing["job_id"], "failed", "failure")
        active = self.coordinator.schedule("graph", "run-active")
        fact_path = self.job.root / "run-1.json"
        self.clock[0] = 1000.0

        result = self.coordinator._prune_terminal_jobs(500.0)

        self.assertEqual(1, result["count"])
        self.assertIsNone(
            self.coordinator.inspect_job("graph", archived["reference"])
        )
        self.assertEqual(
            "failed",
            self.coordinator.inspect_job("graph", missing["reference"])["state"],
        )
        self.assertEqual(
            "queued",
            self.coordinator.inspect_job("graph", active["reference"])["state"],
        )
        self.assertTrue(fact_path.is_file())
        manifest = json.loads(Path(result["archive_path"]).read_text(encoding="utf-8"))
        entry = manifest["entries"][archived["job_id"]]
        self.assertEqual(str(fact_path.absolute()), entry["terminal_fact"]["path"])
        self.assertEqual("succeeded", entry["terminal_fact"]["phase"])

    def test_archive_survives_queue_write_failure_and_is_reused_on_retry(self):
        scheduled = self._terminal_job()
        self.clock[0] = 1000.0
        original_write = self.coordinator._write_queue

        with patch.object(
            self.coordinator,
            "_write_queue",
            side_effect=OSError("queue unavailable"),
        ):
            with self.assertRaisesRegex(OSError, "queue unavailable"):
                self.coordinator._prune_terminal_jobs(500.0)

        archives_after_failure = tuple(self.coordinator._queue_archive.root.glob("*.json"))
        self.assertEqual(1, len(archives_after_failure))
        self.assertIsNotNone(
            self.coordinator.inspect_job("graph", scheduled["reference"])
        )

        with patch.object(self.coordinator, "_write_queue", wraps=original_write):
            result = self.coordinator._prune_terminal_jobs(500.0)

        self.assertEqual(archives_after_failure[0], Path(result["archive_path"]))
        self.assertEqual(1, len(tuple(self.coordinator._queue_archive.root.glob("*.json"))))
        self.assertIsNone(
            self.coordinator.inspect_job("graph", scheduled["reference"])
        )

    def test_invalid_retention_fails_closed(self):
        for value in (-1, float("inf"), True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ContractViolation, "retention"):
                    self.coordinator._prune_terminal_jobs(value)
