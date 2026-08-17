import subprocess
import tempfile
import unittest
from pathlib import Path

from grapheng import (
    ArtifactStore,
    ChangeSetArtifact,
    ContractViolation,
    ControlledGitMerger,
    EffectJournal,
    GraphSpec,
    validate_graph,
)


def git(repository, *arguments):
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


def graph():
    value = GraphSpec.from_dict(
        {
            "id": "controlled-merge",
            "require_reality_anchor": True,
            "nodes": [
                {
                    "id": "change",
                    "kind": "agent",
                    "writes": ["change_result"],
                    "agent": {
                        "executor": "codex",
                        "prompt": "make change",
                        "workspace": {"mode": "isolated"},
                    },
                    "controlled_merge": {
                        "verifier": "verify",
                        "target_branch": "main",
                    },
                },
                {
                    "id": "verify",
                    "kind": "agent",
                    "deps": ["change"],
                    "reads": ["change_result"],
                    "writes": ["verification"],
                    "gate": "merge-approval",
                    "verifier_for": "change",
                    "reality_anchor": True,
                    "verified_reuse": {
                        "decision_artifact": "verification",
                        "passed_path": ["passed"],
                        "quality_path": ["quality"],
                        "minimum_quality_score": 0.9,
                    },
                    "agent": {"executor": "codex", "prompt": "verify change"},
                },
            ],
        }
    )
    validate_graph(value)
    return value


class RepositoryFixture:
    def __init__(self, root):
        self.repository = root / "repository"
        self.source = root / "source"
        self.repository.mkdir()
        git(self.repository, "init")
        git(self.repository, "symbolic-ref", "HEAD", "refs/heads/main")
        git(self.repository, "config", "user.name", "Graph Engineering Test")
        git(self.repository, "config", "user.email", "grapheng@example.invalid")
        (self.repository / "value.txt").write_text("base\n", encoding="utf-8")
        git(self.repository, "add", "value.txt")
        git(self.repository, "commit", "-m", "base")
        self.base = git(self.repository, "rev-parse", "HEAD")
        git(
            self.repository,
            "worktree",
            "add",
            "-b",
            "agent-change",
            str(self.source),
            "main",
        )

    def commit_source(self, value="source\n"):
        (self.source / "value.txt").write_text(value, encoding="utf-8")
        git(self.source, "add", "value.txt")
        git(self.source, "commit", "-m", "agent change")
        return git(self.source, "rev-parse", "HEAD")

    def commit_target(self, value="target\n", filename="value.txt"):
        (self.repository / filename).write_text(value, encoding="utf-8")
        git(self.repository, "add", filename)
        git(self.repository, "commit", "-m", "target change")
        return git(self.repository, "rev-parse", "HEAD")

    def change_set(self, head):
        return ChangeSetArtifact(
            workspace_id=f"repo::{self.source}",
            base_ref=self.base,
            head_ref=head,
            files_modified=("value.txt",),
        )


class ControlledGitMergerTests(unittest.TestCase):
    def records(self):
        source_store = ArtifactStore()
        source = source_store.commit_batch(
            {"change_result": {"summary": "updated value"}}, "change"
        )
        verification_store = ArtifactStore()
        verification = verification_store.commit_batch(
            {"verification": {"passed": True, "quality": 0.98}}, "verify"
        )
        return source, verification

    def prepare(self, fixture, head, source_records):
        merger = ControlledGitMerger(fixture.repository)
        candidate = merger.prepare(
            fixture.change_set(head),
            fixture.source,
            "main",
            "run-1",
            "change",
            1,
            "verify",
            source_records,
        )
        return merger, candidate

    def test_merge_binds_gate_verification_versions_and_git_commits(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryFixture(Path(directory))
            head = fixture.commit_source()
            source, verification = self.records()
            merger, candidate = self.prepare(fixture, head, source)
            receipt = merger.merge(
                candidate,
                graph().node_map()["verify"],
                1,
                "approved",
                source,
                verification,
            )
            parents = git(
                fixture.repository, "show", "-s", "--format=%P", receipt.merge_commit
            )
            target_value = (fixture.repository / "value.txt").read_text()

        self.assertEqual("merged", receipt.status)
        self.assertEqual(f"{candidate.target_head} {head}", parents)
        self.assertEqual("run-1:verify:1", receipt.authorization["verification_id"])
        self.assertEqual("merge-approval", receipt.authorization["gate"])
        self.assertEqual("source\n", target_value)

    def test_target_drift_and_conflict_are_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryFixture(Path(directory))
            head = fixture.commit_source()
            source, verification = self.records()
            merger, candidate = self.prepare(fixture, head, source)
            drifted = fixture.commit_target("unrelated\n", "other.txt")
            drift = merger.merge(
                candidate,
                graph().node_map()["verify"],
                1,
                "approved",
                source,
                verification,
            )
            drift_head = git(fixture.repository, "rev-parse", "HEAD")

        self.assertEqual("rejected", drift.status)
        self.assertEqual("target_head_drift", drift.reason)
        self.assertEqual(drifted, drift_head)

        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryFixture(Path(directory))
            head = fixture.commit_source("source side\n")
            fixture.commit_target("target side\n")
            source, verification = self.records()
            merger, candidate = self.prepare(fixture, head, source)
            conflict = merger.merge(
                candidate,
                graph().node_map()["verify"],
                1,
                "approved",
                source,
                verification,
            )
            conflict_value = (fixture.repository / "value.txt").read_text()
            conflict_status = git(fixture.repository, "status", "--porcelain")

        self.assertEqual("rejected", conflict.status)
        self.assertEqual("git_merge_conflict_or_hook_rejection", conflict.reason)
        self.assertEqual("target side\n", conflict_value)
        self.assertEqual("", conflict_status)

    def test_version_mismatch_and_changed_file_mismatch_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RepositoryFixture(Path(directory))
            head = fixture.commit_source()
            source, verification = self.records()
            merger, candidate = self.prepare(fixture, head, source)
            wrong = ArtifactStore().commit_batch(
                {"change_result": {"summary": "different"}}, "change"
            )
            rejected = merger.merge(
                candidate,
                graph().node_map()["verify"],
                1,
                "approved",
                wrong,
                verification,
            )
            bad_change_set = ChangeSetArtifact(
                workspace_id=f"repo::{fixture.source}",
                base_ref=fixture.base,
                head_ref=head,
                files_modified=("other.txt",),
            )
            with self.assertRaisesRegex(ContractViolation, "files_modified"):
                merger.prepare(
                    bad_change_set,
                    fixture.source,
                    "main",
                    "run-2",
                    "change",
                    1,
                    "verify",
                    source,
                )
            target_head = git(fixture.repository, "rev-parse", "HEAD")

        self.assertEqual("rejected", rejected.status)
        self.assertEqual("verification_version_mismatch", rejected.reason)
        self.assertEqual(fixture.base, target_head)

    def test_indeterminate_effect_reconciles_exact_merge_without_repeating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = RepositoryFixture(root)
            head = fixture.commit_source()
            source, verification = self.records()
            verifier = graph().node_map()["verify"]
            merger, candidate = self.prepare(fixture, head, source)
            journal = EffectJournal(root / "effects")

            def merge_then_crash():
                merger.merge(
                    candidate,
                    verifier,
                    1,
                    "approved",
                    source,
                    verification,
                )
                raise RuntimeError("simulated crash after Git commit")

            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                journal.execute_reconcilable(
                    "merge-1",
                    candidate.to_dict(),
                    merge_then_crash,
                    lambda: None,
                )
            merged_head = git(fixture.repository, "rev-parse", "HEAD")
            recovered = journal.execute_reconcilable(
                "merge-1",
                candidate.to_dict(),
                lambda: self.fail("merge effect must not repeat"),
                lambda: merger.reconcile(
                    candidate,
                    verifier,
                    1,
                    "approved",
                    source,
                    verification,
                ).to_dict(),
            )

        self.assertEqual("merged", recovered["status"])
        self.assertEqual(merged_head, recovered["merge_commit"])


if __name__ == "__main__":
    unittest.main()
