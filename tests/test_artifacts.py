import unittest

from grapheng import ArtifactRecord, ArtifactStore, ContractViolation


class ArtifactTests(unittest.TestCase):
    def test_restored_artifact_checksum_is_verified(self):
        record = ArtifactRecord("answer", 42, "worker", 1, "invalid")

        with self.assertRaises(ContractViolation):
            ArtifactStore((record,))

    def test_reads_return_copies(self):
        store = ArtifactStore()
        store.commit_batch({"value": {"items": [1]}}, "worker")

        first = store.read("value")
        first["items"].append(2)

        self.assertEqual({"items": [1]}, store.read("value"))


if __name__ == "__main__":
    unittest.main()
