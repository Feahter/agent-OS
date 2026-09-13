import tempfile
import unittest
from pathlib import Path

from grapheng.index import ProjectionIndex, fingerprint


class ProjectionIndexTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.source = self.root / "status.json"
        self.source.write_text('{"phase": "queued"}', encoding="utf-8")
        self.index = ProjectionIndex(self.root / "projections.sqlite3")
        self.addCleanup(self.index.close)
        self.addCleanup(self._directory.cleanup)

    def test_unchanged_sources_are_served_from_the_cache(self):
        calls = []

        def compute():
            calls.append(1)
            return {"phase": "queued"}

        first = self.index.resolve("inspect", "task-1", (self.source,), compute)
        second = self.index.resolve("inspect", "task-1", (self.source,), compute)

        self.assertEqual(first, second)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, self.index.hits)
        self.assertEqual(1, self.index.misses)

    def test_a_changed_source_invalidates_the_cache(self):
        calls = []

        def compute():
            calls.append(1)
            return self.source.read_text(encoding="utf-8")

        self.index.resolve("inspect", "task-1", (self.source,), compute)
        # A larger payload changes size even when mtime granularity is coarse.
        self.source.write_text('{"phase": "succeeded", "note": "done"}', encoding="utf-8")
        value = self.index.resolve("inspect", "task-1", (self.source,), compute)

        self.assertEqual(2, len(calls))
        self.assertIn("succeeded", value)

    def test_scopes_do_not_share_entries(self):
        self.index.resolve("inspect", "task-1", (self.source,), lambda: "queued")
        described = self.index.resolve(
            "describe", "task-1", (self.source,), lambda: {"phase": "queued"}
        )

        self.assertEqual({"phase": "queued"}, described)
        self.assertEqual(2, self.index.misses)

    def test_an_absent_source_is_cacheable_until_it_appears(self):
        calls = []

        def compute():
            calls.append(1)
            return "computed"

        absent = self.root / "report.json"
        self.index.resolve("inspect", "task-1", (self.source, absent), compute)
        self.index.resolve("inspect", "task-1", (self.source, absent), compute)

        self.assertEqual(1, len(calls))
        self.assertEqual(1, self.index.hits)

        absent.write_text("{}", encoding="utf-8")
        self.index.resolve("inspect", "task-1", (self.source, absent), compute)

        self.assertEqual(2, len(calls))

    def test_a_source_that_cannot_be_stated_bypasses_the_cache(self):
        # A path whose parent is a regular file raises NotADirectoryError, which
        # is a real error rather than "not written yet".
        not_a_directory = self.root / "regular-file"
        not_a_directory.write_text("x", encoding="utf-8")

        self.assertIsNone(fingerprint((not_a_directory / "status.json",)))

    def test_no_declared_sources_bypasses_the_cache(self):
        self.assertIsNone(fingerprint(()))
        self.index.resolve("inspect", "task-1", (), lambda: "x")

        self.assertEqual(1, self.index.bypasses)

    def test_unserializable_projections_stay_uncached_without_failing(self):
        value = self.index.resolve("inspect", "task-1", (self.source,), lambda: object())

        self.assertIsInstance(value, object)
        self.assertEqual(0, self.index.hits)

    def test_prune_removes_references_that_no_longer_exist(self):
        self.index.resolve("inspect", "task-1", (self.source,), lambda: "a")
        self.index.resolve("inspect", "task-2", (self.source,), lambda: "b")

        removed = self.index.prune("inspect", keep=("task-1",))

        self.assertEqual(1, removed)
        self.assertEqual(1, self.index.stats()["entries"])

    def test_forget_drops_one_entry(self):
        self.index.resolve("inspect", "task-1", (self.source,), lambda: "a")
        self.index.forget("inspect", "task-1")

        self.index.resolve("inspect", "task-1", (self.source,), lambda: "a")

        self.assertEqual(0, self.index.hits)
        self.assertEqual(2, self.index.misses)

    def test_an_unusable_database_falls_back_to_recomputation(self):
        blocked = self.root / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")
        index = ProjectionIndex(blocked / "projections.sqlite3")
        self.addCleanup(index.close)

        value = index.resolve("inspect", "task-1", (self.source,), lambda: "computed")

        self.assertEqual("computed", value)
        self.assertFalse(index.stats()["available"])


class TaskCenterCachingTests(unittest.TestCase):
    def test_center_reuses_projections_across_calls(self):
        from grapheng.resident import ResidentCoordinator

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            tasks_root = home / "tasks"
            task_id = "task-" + "0" * 16
            (tasks_root / task_id).mkdir(parents=True)
            (tasks_root / task_id / "status.json").write_text("{}", encoding="utf-8")

            inspected = []
            described = []

            class Handler:
                def projection_sources(self, reference):
                    return (tasks_root / reference / "status.json",)

                def discover(self):
                    return (task_id,)

                def inspect(self, reference):
                    inspected.append(reference)
                    return "succeeded"

                def describe(self, reference):
                    described.append(reference)
                    return {"phase": "succeeded", "summary": "done"}

                def execute(self, reference, control_probe):
                    raise AssertionError("not used")

                def record_failure(self, reference, failure):
                    raise AssertionError("not used")

            coordinator = ResidentCoordinator(
                home, job_handlers={"engineering": Handler()}
            )
            self.addCleanup(coordinator.projections.close)

            first = coordinator.task_center()
            second = coordinator.task_center()

        self.assertEqual(first["counts"], second["counts"])
        self.assertEqual(1, len(inspected))
        self.assertEqual(1, len(described))


if __name__ == "__main__":
    unittest.main()
