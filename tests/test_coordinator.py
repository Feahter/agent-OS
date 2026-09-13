import hashlib
import json
import subprocess
import tempfile
import threading
import unittest
from dataclasses import asdict
from pathlib import Path

from grapheng import (
    AllowListGatePolicy,
    ContractViolation,
    GraphSpec,
    GraphValidationError,
    OrcaCoordinator,
    OrcaMaterializedRun,
    VerifiedArtifactCache,
)


def node(
    node_id,
    *,
    deps=(),
    reads=(),
    writes=None,
    attempts=1,
    estimated_tokens=0,
    gate=None,
    workspace=None,
    verifier_for=None,
    reality_anchor=False,
    verified_reuse=None,
    controlled_merge=None,
):
    value = {
        "id": node_id,
        "kind": "agent",
        "deps": list(deps),
        "reads": list(reads),
        "writes": list(writes or (f"{node_id}_out",)),
        "retry": {"max_attempts": attempts},
        "estimated_tokens": estimated_tokens,
        "agent": {"executor": "codex", "prompt": f"run {node_id}"},
    }
    if gate is not None:
        value["gate"] = gate
    if workspace is not None:
        value["agent"]["workspace"] = workspace
    if verifier_for is not None:
        value["verifier_for"] = verifier_for
    if reality_anchor:
        value["reality_anchor"] = True
    if verified_reuse is not None:
        value["verified_reuse"] = verified_reuse
    if controlled_merge is not None:
        value["controlled_merge"] = controlled_merge
    return value


def graph(nodes, *, max_concurrency=1, max_tokens=None, require_anchor=False):
    value = {
        "id": "coordinator-graph",
        "nodes": nodes,
        "max_concurrency": max_concurrency,
        "require_reality_anchor": require_anchor,
    }
    if max_tokens is not None:
        value["max_tokens"] = max_tokens
    return GraphSpec.from_dict(value)


class FakeBackend:
    def __init__(self):
        self.materialize_calls = 0
        self.starts = []
        self.gates = []
        self.deliveries = []
        self.acks = []
        self.replies = []
        self.stops = []
        self.finishes = []

    def materialize(self, plan):
        self.materialize_calls += 1
        return OrcaMaterializedRun(
            "run-orca",
            {task.node_id: f"task-{task.node_id}" for task in plan.tasks},
            {
                task.node_id: f"gate-{task.node_id}"
                for task in plan.tasks
                if task.gate is not None
            },
        )

    def start_worker(
        self, plan, materialized, node_id, attempt=1, retry_of=None
    ):
        dispatch_id = f"dispatch-{node_id}-{attempt}"
        self.starts.append(
            {
                "node_id": node_id,
                "attempt": attempt,
                "retry_of": retry_of,
                "dispatch_id": dispatch_id,
            }
        )
        return {"dispatch": {"id": dispatch_id}}

    def resolve_gate(self, materialized, node_id, resolution):
        self.gates.append((node_id, resolution))
        return {"gate": {"id": materialized.gate_ids[node_id], "status": resolution}}

    def wait_delivery(self, timeout_ms=900000):
        if not self.deliveries:
            return {"count": 0}
        return self.deliveries.pop(0)

    def acknowledge_delivery(self, delivery_id):
        self.acks.append(delivery_id)
        return {"deliveryId": delivery_id, "acknowledged": True}

    def reply(self, message_id, body):
        self.replies.append((message_id, body))
        return {"messageId": message_id, "replied": True}

    def stop_worker(self, dispatch_id):
        self.stops.append(dispatch_id)
        return {"dispatchId": dispatch_id, "status": "stopped"}

    def finish_worker(self, dispatch_id, retain, succeeded):
        self.finishes.append((dispatch_id, retain, succeeded))
        return {
            "dispatchId": dispatch_id,
            "action": "retained"
            if retain == "always" or (retain == "on_failure" and not succeeded)
            else "released",
        }

    def enqueue(self, delivery_id, *messages):
        self.deliveries.append(
            {
                "count": len(messages),
                "delivery": {
                    "deliveryId": delivery_id,
                    "messages": list(messages),
                },
            }
        )


class BlockingWaitBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.wait_started = threading.Event()
        self.release_wait = threading.Event()

    def wait_delivery(self, timeout_ms=900000):
        self.wait_started.set()
        self.release_wait.wait(timeout=5)
        return {"count": 0}


def done(node_id, attempt=1, *, outcome="succeeded", outputs=None, tokens=0, **extra):
    message = {
        "id": f"done-{node_id}-{attempt}-{outcome}",
        "type": "worker_done",
        "taskId": f"task-{node_id}",
        "dispatchId": f"dispatch-{node_id}-{attempt}",
        "outcome": outcome,
        "payload": {
            "outputs": outputs or {f"{node_id}_out": node_id},
            "text": "complete",
            "tokens_used": tokens,
        },
    }
    message.update(extra)
    return message


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


def merge_repository(root):
    repository = root / "repository"
    source = root / "source"
    repository.mkdir()
    git(repository, "init")
    git(repository, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repository, "config", "user.name", "Graph Engineering Test")
    git(repository, "config", "user.email", "grapheng@example.invalid")
    (repository / "value.txt").write_text("base\n", encoding="utf-8")
    git(repository, "add", "value.txt")
    git(repository, "commit", "-m", "base")
    base = git(repository, "rev-parse", "HEAD")
    git(repository, "worktree", "add", "-b", "agent-change", str(source), "main")
    (source / "value.txt").write_text("merged\n", encoding="utf-8")
    git(source, "add", "value.txt")
    git(source, "commit", "-m", "agent change")
    head = git(source, "rev-parse", "HEAD")
    return repository, source, base, head


class OrcaCoordinatorTests(unittest.TestCase):
    def coordinator(self, root, value, backend, **kwargs):
        workspace = root / "workspace"
        workspace.mkdir(exist_ok=True)
        return OrcaCoordinator(
            value, backend, root / "coordinator", workspace, **kwargs
        )

    def test_parallel_wave_and_restart_do_not_repeat_external_effects(self):
        value = graph(
            [node("left"), node("right")], max_concurrency=2
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            first = self.coordinator(root, value, backend)
            snapshot = first.start()
            second = self.coordinator(root, value, backend)
            resumed = second.start()

        self.assertEqual(1, backend.materialize_calls)
        self.assertEqual(["left", "right"], [item["node_id"] for item in backend.starts])
        self.assertEqual(snapshot.active_dispatches, resumed.active_dispatches)

    def test_cancel_stops_active_dispatches_once_and_is_restart_safe(self):
        value = graph([node("work")])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator = self.coordinator(root, value, backend)
            started = coordinator.start()
            cancelled = coordinator.cancel()
            resumed = self.coordinator(root, value, backend).cancel()

        self.assertEqual("running", started.phase)
        self.assertEqual("cancelled", cancelled.phase)
        self.assertEqual("cancelled", resumed.phase)
        self.assertEqual(["dispatch-work-1"], backend.stops)
        self.assertEqual([("dispatch-work-1", "on_failure", False)], backend.finishes)

    def test_blocking_delivery_wait_does_not_block_cancellation(self):
        value = graph([node("work")])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = BlockingWaitBackend()
            coordinator = self.coordinator(root, value, backend)
            coordinator.start()
            advanced = []
            cancelled = []
            advance_thread = threading.Thread(
                target=lambda: advanced.append(coordinator.advance(timeout_ms=1000))
            )
            advance_thread.start()
            self.assertTrue(backend.wait_started.wait(timeout=1))
            cancel_thread = threading.Thread(
                target=lambda: cancelled.append(coordinator.cancel())
            )
            cancel_thread.start()
            cancel_thread.join(timeout=1)
            cancellation_was_responsive = not cancel_thread.is_alive()
            backend.release_wait.set()
            advance_thread.join(timeout=2)
            cancel_thread.join(timeout=2)

        self.assertTrue(cancellation_was_responsive)
        self.assertFalse(advance_thread.is_alive())
        self.assertEqual("cancelled", cancelled[0].phase)
        self.assertEqual("cancelled", advanced[0].phase)

    def test_v1_state_migrates_fingerprint_and_merge_fields_without_replaying(self):
        value = graph([node("work")])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator_root = root / "coordinator"
            first = OrcaCoordinator(
                value, backend, coordinator_root, root
            )
            first.start()
            state_path = coordinator_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            legacy_graph = asdict(value)
            for legacy_node in legacy_graph["nodes"]:
                legacy_node.pop("controlled_merge", None)
            encoded = json.dumps(
                legacy_graph,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            state["schema_version"] = 1
            state["graph_fingerprint"] = hashlib.sha256(
                encoded.encode("utf-8")
            ).hexdigest()
            state.pop("gate_resolutions")
            state.pop("merge_candidates")
            state_path.write_text(json.dumps(state), encoding="utf-8")

            resumed = OrcaCoordinator(
                value, backend, coordinator_root, root
            ).start()
            migrated = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual("running", resumed.phase)
        self.assertEqual(2, migrated["schema_version"])
        self.assertEqual(value.fingerprint(), migrated["graph_fingerprint"])
        self.assertEqual({}, migrated["gate_resolutions"])
        self.assertEqual({}, migrated["merge_candidates"])
        self.assertEqual(1, backend.materialize_calls)
        self.assertEqual(1, len(backend.starts))

    def test_dependency_wave_commits_artifacts_then_starts_dependent_node(self):
        value = graph(
            [
                node("draft", writes=("draft",)),
                node("review", deps=("draft",), reads=("draft",)),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator = self.coordinator(root, value, backend)
            first = coordinator.start()
            backend.enqueue(
                "delivery-1",
                done("draft", outputs={"draft": "ready"}, tokens=3),
            )
            second = coordinator.advance(timeout_ms=10)

        self.assertEqual({"draft": "dispatch-draft-1"}, first.active_dispatches)
        self.assertEqual("completed", second.statuses["draft"])
        self.assertEqual("running", second.statuses["review"])
        self.assertEqual("ready", second.artifacts["draft"])
        self.assertEqual(["delivery-1"], backend.acks)
        self.assertEqual(
            [("dispatch-draft-1", "on_failure", True)], backend.finishes
        )

    def test_failed_worker_retries_with_explicit_retry_of(self):
        value = graph([node("work", attempts=2)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator = self.coordinator(root, value, backend)
            coordinator.start()
            backend.enqueue("delivery-failed", done("work", outcome="failed"))
            snapshot = coordinator.advance(timeout_ms=10)

        self.assertEqual(2, snapshot.attempts["work"])
        self.assertEqual("running", snapshot.statuses["work"])
        self.assertEqual("dispatch-work-1", backend.starts[-1]["retry_of"])
        self.assertEqual(
            [("dispatch-work-1", "on_failure", False)], backend.finishes
        )

    def test_question_replay_waits_for_reply_and_is_processed_once(self):
        value = graph([node("work")])
        question = {
            "id": "question-1",
            "type": "question",
            "taskId": "task-work",
            "dispatchId": "dispatch-work-1",
            "subject": "choose",
            "body": "A or B?",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator = self.coordinator(root, value, backend)
            coordinator.start()
            backend.enqueue("delivery-question", question)
            waiting = coordinator.advance(timeout_ms=10)
            backend.enqueue("delivery-question", question)
            replayed = coordinator.advance(timeout_ms=10)
            waiting_events = [
                item
                for item in coordinator.events().events
                if item["event"] == "node_waiting_for_input"
            ]
            answered = coordinator.answer_question("question-1", "A")

        self.assertEqual(("question-1",), waiting.pending_questions)
        self.assertEqual(waiting.pending_questions, replayed.pending_questions)
        self.assertEqual(1, len(waiting_events))
        self.assertEqual([], backend.acks[:-1])
        self.assertEqual([("question-1", "A")], backend.replies)
        self.assertEqual(["delivery-question"], backend.acks)
        self.assertEqual("running", answered.statuses["work"])

    def test_terminal_node_still_waits_for_an_unanswered_question(self):
        value = graph([node("work")])
        question = {
            "id": "question-before-terminal",
            "type": "question",
            "taskId": "task-work",
            "dispatchId": "dispatch-work-1",
            "body": "confirm completion",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator = self.coordinator(root, value, backend)
            coordinator.start()
            backend.enqueue("delivery-mixed", question, done("work"))

            waiting = coordinator.advance(timeout_ms=10)
            completed = coordinator.answer_question(
                "question-before-terminal", "confirmed"
            )

        self.assertEqual("waiting_for_input", waiting.phase)
        self.assertEqual(("question-before-terminal",), waiting.pending_questions)
        self.assertEqual("succeeded", completed.phase)

    def test_cancel_resolves_pending_messages_and_acknowledges_delivery(self):
        value = graph([node("work")])
        question = {
            "id": "question-cancelled",
            "type": "question",
            "taskId": "task-work",
            "dispatchId": "dispatch-work-1",
            "body": "need input",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator = self.coordinator(root, value, backend)
            coordinator.start()
            backend.enqueue("delivery-question", question)
            coordinator.advance(timeout_ms=10)

            cancelled = coordinator.cancel()

            with self.assertRaisesRegex(ContractViolation, "is not pending"):
                coordinator.answer_question("question-cancelled", "too late")

        self.assertEqual("cancelled", cancelled.phase)
        self.assertEqual(["delivery-question"], backend.acks)

    def test_question_without_real_message_id_is_rejected(self):
        value = graph([node("work")])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator = self.coordinator(root, value, backend)
            coordinator.start()
            backend.enqueue(
                "delivery-question",
                {
                    "type": "question",
                    "dispatchId": "dispatch-work-1",
                    "body": "reply to me",
                },
            )
            with self.assertRaisesRegex(ContractViolation, "replyable id"):
                coordinator.advance(timeout_ms=10)

        self.assertEqual([], backend.replies)
        self.assertEqual([], backend.acks)

    def test_message_task_identity_must_match_active_dispatch(self):
        value = graph([node("work")])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator = self.coordinator(root, value, backend)
            coordinator.start()
            message = done("work")
            message["taskId"] = "task-other"
            backend.enqueue("delivery-wrong-task", message)
            with self.assertRaisesRegex(ContractViolation, "does not match"):
                coordinator.advance(timeout_ms=10)
            snapshot = coordinator.inspect()

        self.assertEqual("running", snapshot.statuses["work"])
        self.assertEqual({}, snapshot.artifacts)
        self.assertEqual([], backend.finishes)
        self.assertEqual([], backend.acks)

    def test_reply_effect_identity_prevents_conflicting_crash_replay(self):
        value = graph([node("work")])
        question = {
            "id": "question-crash",
            "type": "question",
            "taskId": "task-work",
            "dispatchId": "dispatch-work-1",
            "body": "choose",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator = self.coordinator(root, value, backend)
            coordinator.start()
            backend.enqueue("delivery-crash", question)
            coordinator.advance(timeout_ms=10)
            original_save = coordinator._save

            def fail_save(state):
                raise OSError("simulated state-save crash")

            coordinator._save = fail_save
            with self.assertRaisesRegex(OSError, "simulated"):
                coordinator.answer_question("question-crash", "A")
            coordinator._save = original_save

            resumed = self.coordinator(root, value, backend)
            with self.assertRaisesRegex(ContractViolation, "different input"):
                resumed.answer_question("question-crash", "B")
            recovered = resumed.answer_question("question-crash", "A")

        self.assertEqual([("question-crash", "A")], backend.replies)
        self.assertEqual(["delivery-crash"], backend.acks)
        self.assertEqual((), recovered.pending_questions)

    def test_escalation_continue_retry_and_fail(self):
        for action in ("continue", "retry", "fail"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                backend = FakeBackend()
                value = graph([node("work", attempts=2)])
                coordinator = self.coordinator(root, value, backend)
                coordinator.start()
                backend.enqueue(
                    f"delivery-{action}",
                    {
                        "id": f"escalation-{action}",
                        "type": "escalation",
                        "taskId": "task-work",
                        "dispatchId": "dispatch-work-1",
                        "body": "need intervention",
                    },
                )
                coordinator.advance(timeout_ms=10)
                snapshot = coordinator.resolve_escalation(
                    f"escalation-{action}",
                    action,
                    "continue safely" if action == "continue" else None,
                )

                self.assertEqual([f"delivery-{action}"], backend.acks)
                if action == "continue":
                    self.assertEqual("running", snapshot.statuses["work"])
                    self.assertEqual(
                        [("escalation-continue", "continue safely")],
                        backend.replies,
                    )
                    self.assertEqual([], backend.stops)
                elif action == "retry":
                    self.assertEqual("running", snapshot.statuses["work"])
                    self.assertEqual(["dispatch-work-1"], backend.stops)
                    self.assertEqual(
                        "dispatch-work-1", backend.starts[-1]["retry_of"]
                    )
                    self.assertEqual(
                        [("dispatch-work-1", "on_failure", False)],
                        backend.finishes,
                    )
                else:
                    self.assertEqual("failed", snapshot.statuses["work"])
                    self.assertEqual("failed", snapshot.phase)
                    self.assertEqual(["dispatch-work-1"], backend.stops)
                    self.assertEqual(
                        [("dispatch-work-1", "on_failure", False)],
                        backend.finishes,
                    )

    def test_gate_allow_or_deny_is_single_owner_decision(self):
        value = graph([node("work", gate="ship")])
        with tempfile.TemporaryDirectory() as allowed_dir:
            root = Path(allowed_dir)
            allowed_backend = FakeBackend()
            allowed = self.coordinator(
                root,
                value,
                allowed_backend,
                gate_policy=AllowListGatePolicy({"ship"}),
            ).start()
        with tempfile.TemporaryDirectory() as denied_dir:
            root = Path(denied_dir)
            denied_backend = FakeBackend()
            denied_coordinator = self.coordinator(root, value, denied_backend)
            denied = denied_coordinator.start()
            with self.assertRaisesRegex(ContractViolation, "not retryable"):
                denied_coordinator.retry("work")

        self.assertEqual([("work", "approved")], allowed_backend.gates)
        self.assertEqual("running", allowed.statuses["work"])
        self.assertEqual([("work", "denied")], denied_backend.gates)
        self.assertEqual([], denied_backend.starts)
        self.assertEqual("failed", denied.phase)

    def test_unknown_and_stale_dispatch_messages_are_acknowledged_but_ignored(self):
        value = graph([node("work", attempts=2)])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator = self.coordinator(root, value, backend)
            coordinator.start()
            unknown = done("work")
            unknown["id"] = "unknown"
            unknown["dispatchId"] = "dispatch-unknown"
            backend.enqueue("delivery-unknown", unknown)
            after_unknown = coordinator.advance(timeout_ms=10)
            backend.enqueue("delivery-failure", done("work", outcome="failed"))
            coordinator.advance(timeout_ms=10)
            stale = done("work", outputs={"work_out": "stale"})
            stale["id"] = "stale"
            backend.enqueue("delivery-stale", stale)
            after_stale = coordinator.advance(timeout_ms=10)

        self.assertEqual(
            "dispatch-work-1", after_unknown.active_dispatches["work"]
        )
        self.assertEqual("dispatch-work-2", after_stale.active_dispatches["work"])
        self.assertNotIn("work_out", after_stale.artifacts)
        self.assertEqual(
            ["delivery-unknown", "delivery-failure", "delivery-stale"],
            backend.acks,
        )

    def test_delivery_replay_does_not_duplicate_artifact_or_cleanup(self):
        value = graph([node("work")])
        question = {
            "id": "question-before-done",
            "type": "question",
            "taskId": "task-work",
            "dispatchId": "dispatch-work-1",
            "body": "late question",
        }
        completion = done("work", outputs={"work_out": "accepted"})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator = self.coordinator(root, value, backend)
            coordinator.start()
            backend.enqueue("delivery-mixed", question, completion)
            first = coordinator.advance(timeout_ms=10)
            backend.enqueue("delivery-mixed", question, completion)
            replay = coordinator.advance(timeout_ms=10)
            coordinator.answer_question("question-before-done", "ack")

        self.assertEqual("accepted", first.artifacts["work_out"])
        self.assertEqual(first.artifacts, replay.artifacts)
        self.assertEqual(1, len(backend.finishes))
        self.assertEqual(["delivery-mixed"], backend.acks)

    def test_token_budget_fails_before_orca_materialization(self):
        value = graph(
            [
                node("first", estimated_tokens=4),
                node("second", estimated_tokens=4),
            ],
            max_concurrency=2,
            max_tokens=5,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            with self.assertRaisesRegex(GraphValidationError, "requires max_tokens"):
                self.coordinator(root, value, backend)

        self.assertEqual(0, backend.materialize_calls)

    def test_isolated_change_set_conflict_fails_without_committing_outputs(self):
        value = graph(
            [
                node(
                    "work",
                    workspace={
                        "mode": "isolated",
                        "lineage": "child",
                        "retain": "on_failure",
                    },
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            coordinator = self.coordinator(root, value, backend)
            coordinator.start()
            message = done("work")
            message["payload"]["changes"] = {
                "workspace_id": f"repo::{root / 'workspace'}",
                "base_ref": "base",
                "head_ref": "head",
                "files_modified": ["grapheng/coordinator.py"],
                "conflicts": ["grapheng/coordinator.py"],
            }
            backend.enqueue("delivery-conflict", message)
            failed = coordinator.advance(timeout_ms=10)

        self.assertEqual("failed", failed.phase)
        self.assertEqual({}, failed.artifacts)
        self.assertEqual(
            [("dispatch-work-1", "on_failure", False)], backend.finishes
        )

    def test_reality_anchor_publishes_verified_result(self):
        value = graph(
            [
                node("answer", writes=("answer",)),
                node(
                    "verify",
                    deps=("answer",),
                    reads=("answer",),
                    writes=("verification",),
                    verifier_for="answer",
                    reality_anchor=True,
                    verified_reuse={
                        "decision_artifact": "verification",
                        "passed_path": ["passed"],
                        "quality_path": ["quality"],
                        "minimum_quality_score": 0.9,
                    },
                ),
            ],
            require_anchor=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = FakeBackend()
            cache = VerifiedArtifactCache(root / "reuse")
            coordinator = self.coordinator(
                root, value, backend, reuse_store=cache
            )
            coordinator.start()
            backend.enqueue(
                "delivery-answer",
                done("answer", outputs={"answer": "forty-two"}, tokens=4),
            )
            coordinator.advance(timeout_ms=10)
            backend.enqueue(
                "delivery-verify",
                done(
                    "verify",
                    outputs={
                        "verification": {"passed": True, "quality": 0.97}
                    },
                    tokens=2,
                ),
            )
            complete = coordinator.advance(timeout_ms=10)
            publications = tuple((root / "reuse" / "entries").glob("*.json"))
            published_events = [
                item
                for item in coordinator.events().events
                if item["event"] == "verified_result_published"
            ]

        self.assertEqual("succeeded", complete.phase)
        self.assertEqual(1, len(publications))
        self.assertEqual(1, len(published_events))

    def test_controlled_merge_waits_for_existing_gate_and_exact_verification(self):
        value = graph(
            [
                node(
                    "change",
                    writes=("change_result",),
                    workspace={
                        "mode": "isolated",
                        "lineage": "child",
                        "retain": "on_failure",
                    },
                    controlled_merge={
                        "verifier": "verify",
                        "target_branch": "main",
                    },
                ),
                node(
                    "verify",
                    deps=("change",),
                    reads=("change_result",),
                    writes=("verification",),
                    gate="merge-approval",
                    verifier_for="change",
                    reality_anchor=True,
                    verified_reuse={
                        "decision_artifact": "verification",
                        "passed_path": ["passed"],
                        "quality_path": ["quality"],
                        "minimum_quality_score": 0.9,
                    },
                ),
            ],
            require_anchor=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, source, base, head = merge_repository(root)
            backend = FakeBackend()
            coordinator_root = root / "coordinator"
            coordinator = OrcaCoordinator(
                value,
                backend,
                coordinator_root,
                repository,
                gate_policy=AllowListGatePolicy({"merge-approval"}),
            )
            coordinator.start()
            change_done = done(
                "change", outputs={"change_result": {"summary": "changed"}}
            )
            change_done["payload"]["changes"] = {
                "workspace_id": f"repo::{source}",
                "base_ref": base,
                "head_ref": head,
                "files_modified": ["value.txt"],
                "conflicts": [],
            }
            backend.enqueue("delivery-change", change_done)
            staged = coordinator.advance(timeout_ms=10)
            target_before_verification = git(repository, "rev-parse", "HEAD")
            backend.enqueue(
                "delivery-verify",
                done(
                    "verify",
                    outputs={"verification": {"passed": True, "quality": 0.98}},
                ),
            )
            complete = coordinator.advance(timeout_ms=10)
            merged_head = git(repository, "rev-parse", "HEAD")
            resumed = OrcaCoordinator(
                value,
                backend,
                coordinator_root,
                repository,
                gate_policy=AllowListGatePolicy({"merge-approval"}),
            ).start()
            replayed_head = git(repository, "rev-parse", "HEAD")
            merge_events = [
                item
                for item in coordinator.events().events
                if item["event"] == "change_set_merged"
            ]

        self.assertEqual(base, target_before_verification)
        self.assertEqual("running", staged.statuses["verify"])
        self.assertEqual("succeeded", complete.phase)
        self.assertEqual(complete.merges, resumed.merges)
        self.assertEqual(merged_head, replayed_head)
        self.assertEqual(1, len(merge_events))
        self.assertEqual("merged", next(iter(complete.merges.values()))["status"])
        self.assertEqual(
            [
                ("dispatch-change-1", "always", True),
                ("dispatch-change-1", "never", True),
                ("dispatch-verify-1", "on_failure", True),
            ],
            backend.finishes,
        )

    def test_controlled_merge_target_drift_fails_closed_and_keeps_workspace(self):
        value = graph(
            [
                node(
                    "change",
                    writes=("change_result",),
                    workspace={"mode": "isolated", "retain": "on_failure"},
                    controlled_merge={
                        "verifier": "verify",
                        "target_branch": "main",
                    },
                ),
                node(
                    "verify",
                    deps=("change",),
                    reads=("change_result",),
                    writes=("verification",),
                    gate="merge-approval",
                    verifier_for="change",
                    reality_anchor=True,
                    verified_reuse={
                        "decision_artifact": "verification",
                        "passed_path": ["passed"],
                        "quality_path": ["quality"],
                        "minimum_quality_score": 0.9,
                    },
                ),
            ],
            require_anchor=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, source, base, head = merge_repository(root)
            backend = FakeBackend()
            coordinator = OrcaCoordinator(
                value,
                backend,
                root / "coordinator",
                repository,
                gate_policy=AllowListGatePolicy({"merge-approval"}),
            )
            coordinator.start()
            change_done = done(
                "change", outputs={"change_result": {"summary": "changed"}}
            )
            change_done["payload"]["changes"] = {
                "workspace_id": f"repo::{source}",
                "base_ref": base,
                "head_ref": head,
                "files_modified": ["value.txt"],
                "conflicts": [],
            }
            backend.enqueue("delivery-change", change_done)
            coordinator.advance(timeout_ms=10)
            (repository / "other.txt").write_text("drift\n", encoding="utf-8")
            git(repository, "add", "other.txt")
            git(repository, "commit", "-m", "target drift")
            drifted_head = git(repository, "rev-parse", "HEAD")
            backend.enqueue(
                "delivery-verify",
                done(
                    "verify",
                    outputs={"verification": {"passed": True, "quality": 0.98}},
                ),
            )
            failed = coordinator.advance(timeout_ms=10)
            final_head = git(repository, "rev-parse", "HEAD")

        merge = next(iter(failed.merges.values()))
        self.assertEqual("failed", failed.phase)
        self.assertEqual("rejected", merge["status"])
        self.assertEqual("target_head_drift", merge["receipt"]["reason"])
        self.assertEqual(drifted_head, final_head)
        self.assertEqual(
            [
                ("dispatch-change-1", "always", True),
                ("dispatch-verify-1", "on_failure", False),
            ],
            backend.finishes,
        )

    def test_controlled_merge_candidate_state_tampering_is_rejected(self):
        value = graph(
            [
                node(
                    "change",
                    writes=("change_result",),
                    workspace={"mode": "isolated"},
                    controlled_merge={
                        "verifier": "verify",
                        "target_branch": "main",
                    },
                ),
                node(
                    "verify",
                    deps=("change",),
                    reads=("change_result",),
                    writes=("verification",),
                    gate="merge-approval",
                    verifier_for="change",
                    reality_anchor=True,
                    verified_reuse={
                        "decision_artifact": "verification",
                        "passed_path": ["passed"],
                        "quality_path": ["quality"],
                    },
                ),
            ],
            require_anchor=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, source, base, head = merge_repository(root)
            backend = FakeBackend()
            coordinator_root = root / "coordinator"
            coordinator = OrcaCoordinator(
                value,
                backend,
                coordinator_root,
                repository,
                gate_policy=AllowListGatePolicy({"merge-approval"}),
            )
            coordinator.start()
            change_done = done(
                "change", outputs={"change_result": {"summary": "changed"}}
            )
            change_done["payload"]["changes"] = {
                "workspace_id": f"repo::{source}",
                "base_ref": base,
                "head_ref": head,
                "files_modified": ["value.txt"],
                "conflicts": [],
            }
            backend.enqueue("delivery-change", change_done)
            coordinator.advance(timeout_ms=10)
            state_path = coordinator_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            candidate = next(iter(state["merge_candidates"].values()))["candidate"]
            candidate["head_commit"] = base
            state_path.write_text(json.dumps(state), encoding="utf-8")

            resumed = OrcaCoordinator(
                value,
                backend,
                coordinator_root,
                repository,
                gate_policy=AllowListGatePolicy({"merge-approval"}),
            )
            with self.assertRaisesRegex(ContractViolation, "checksum mismatch"):
                resumed.inspect()

    def test_controlled_merge_rejects_coordinator_verification_version_drift(self):
        value = graph(
            [
                node(
                    "change",
                    writes=("change_result",),
                    workspace={"mode": "isolated", "retain": "on_failure"},
                    controlled_merge={
                        "verifier": "verify",
                        "target_branch": "main",
                    },
                ),
                node(
                    "verify",
                    deps=("change",),
                    reads=("change_result",),
                    writes=("verification",),
                    gate="merge-approval",
                    verifier_for="change",
                    reality_anchor=True,
                    verified_reuse={
                        "decision_artifact": "verification",
                        "passed_path": ["passed"],
                        "quality_path": ["quality"],
                    },
                ),
            ],
            require_anchor=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, source, base, head = merge_repository(root)
            backend = FakeBackend()
            coordinator_root = root / "coordinator"
            coordinator = OrcaCoordinator(
                value,
                backend,
                coordinator_root,
                repository,
                gate_policy=AllowListGatePolicy({"merge-approval"}),
            )
            coordinator.start()
            change_done = done(
                "change", outputs={"change_result": {"summary": "changed"}}
            )
            change_done["payload"]["changes"] = {
                "workspace_id": f"repo::{source}",
                "base_ref": base,
                "head_ref": head,
                "files_modified": ["value.txt"],
                "conflicts": [],
            }
            backend.enqueue("delivery-change", change_done)
            coordinator.advance(timeout_ms=10)
            state_path = coordinator_root / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            verifier_input = state["dispatches"]["dispatch-verify-1"][
                "input_artifacts"
            ][0]
            verifier_input["version"] += 1
            state_path.write_text(json.dumps(state), encoding="utf-8")
            backend.enqueue(
                "delivery-verify",
                done(
                    "verify",
                    outputs={"verification": {"passed": True, "quality": 0.98}},
                ),
            )
            failed = coordinator.advance(timeout_ms=10)
            final_head = git(repository, "rev-parse", "HEAD")

        merge = next(iter(failed.merges.values()))
        self.assertEqual("failed", failed.phase)
        self.assertEqual("rejected", merge["status"])
        self.assertEqual("verification_version_mismatch", merge["receipt"]["reason"])
        self.assertEqual(base, final_head)


if __name__ == "__main__":
    unittest.main()
