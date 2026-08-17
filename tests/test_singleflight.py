import hashlib
import json
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path

from grapheng import (
    AgentRequest,
    AgentResult,
    ContractViolation,
    SingleFlightCoordinator,
    VerifiedArtifactCache,
)


def _cache_worker(
    reuse_root,
    workspace,
    task_id,
    counter,
    release,
    results,
    fail=False,
    max_cost_usd=None,
):
    root = Path(reuse_root)
    coordinator = SingleFlightCoordinator(
        root / "flights",
        lease_seconds=0.25,
        heartbeat_interval=0.05,
        poll_interval=0.01,
        delivery_ttl_seconds=1.0,
    )
    cache = VerifiedArtifactCache(root, singleflight=coordinator)
    request = AgentRequest(
        task_id,
        "do not persist this prompt",
        {"question": "do not persist this input"},
        ("answer",),
        Path(workspace),
        timeout_seconds=5,
        max_cost_usd=max_cost_usd,
        reuse_scope="tenant-a",
    )

    def load():
        with counter.get_lock():
            counter.value += 1
        release.wait(5)
        if fail:
            raise RuntimeError("private failure detail")
        return AgentResult(
            "shared", {"answer": "HELLO"}, "raw response", 17, 0.4
        )

    try:
        result = cache.resolve(request, "shared", load)
        results.put(
            (
                "ok",
                result.reuse_status,
                result.source_task_id,
                result.tokens_used,
                result.cost_usd,
                dict(result.outputs),
            )
        )
    except BaseException as error:
        results.put(("error", type(error).__name__, str(error)))


def _increment_file_counter(path):
    target = Path(path)
    value = int(target.read_text(encoding="utf-8"))
    target.write_text(str(value + 1), encoding="utf-8")


def _crash_worker(flight_root, key, counter_path, started_path):
    coordinator = SingleFlightCoordinator(
        Path(flight_root),
        lease_seconds=0.2,
        heartbeat_interval=0.05,
        poll_interval=0.01,
    )

    def load():
        _increment_file_counter(counter_path)
        Path(started_path).write_text("started", encoding="utf-8")
        os._exit(23)

    coordinator.execute(key, "crashed-leader", load, timeout_seconds=3)


def _takeover_worker(flight_root, key, counter_path, results):
    coordinator = SingleFlightCoordinator(
        Path(flight_root),
        lease_seconds=0.2,
        heartbeat_interval=0.05,
        poll_interval=0.01,
        delivery_ttl_seconds=1.0,
    )

    def load():
        _increment_file_counter(counter_path)
        return {"answer": "recovered"}

    try:
        delivery = coordinator.execute(
            key, "takeover-leader", load, timeout_seconds=3
        )
        results.put(
            (
                "ok",
                delivery.coalesced,
                delivery.source_id,
                dict(delivery.payload),
            )
        )
    except BaseException as error:
        results.put(("error", type(error).__name__, str(error)))


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _running_state(reuse_root):
    paths = tuple((Path(reuse_root) / "flights" / "states").glob("*.json"))
    if len(paths) != 1:
        return None
    try:
        state = json.loads(paths[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return state if state.get("status") == "running" else None


class CrossProcessSingleFlightTests(unittest.TestCase):
    def setUp(self):
        self.context = multiprocessing.get_context("spawn")

    def test_duplicate_processes_execute_once_while_heartbeat_renews_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reuse_root = root / "reuse"
            workspace = root / "workspace"
            workspace.mkdir()
            counter = self.context.Value("i", 0)
            release = self.context.Event()
            results = self.context.Queue()
            leader = self.context.Process(
                target=_cache_worker,
                args=(reuse_root, workspace, "leader-task", counter, release, results),
            )
            follower = self.context.Process(
                target=_cache_worker,
                args=(reuse_root, workspace, "follower-task", counter, release, results),
            )

            leader.start()
            self.assertTrue(
                _wait_for(lambda: _running_state(reuse_root) is not None),
                "leader never acquired the shared lease",
            )
            follower.start()
            self.assertTrue(
                _wait_for(
                    lambda: bool(
                        (_running_state(reuse_root) or {}).get("waiters")
                    )
                ),
                "follower never registered for delivery",
            )
            state_text = next(
                (reuse_root / "flights" / "states").glob("*.json")
            ).read_text(encoding="utf-8")
            time.sleep(0.4)
            self.assertEqual(1, counter.value)
            release.set()
            leader.join(5)
            follower.join(5)
            delivered = [results.get(timeout=2), results.get(timeout=2)]
            event_lines = (reuse_root / "events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()

        self.assertEqual(0, leader.exitcode)
        self.assertEqual(0, follower.exitcode)
        self.assertEqual(1, counter.value)
        self.assertEqual({"miss", "coalesced"}, {item[1] for item in delivered})
        coalesced = next(item for item in delivered if item[1] == "coalesced")
        self.assertEqual("leader-task", coalesced[2])
        self.assertEqual(0, coalesced[3])
        self.assertEqual(0.0, coalesced[4])
        self.assertEqual({"answer": "HELLO"}, coalesced[5])
        self.assertEqual(2, len(event_lines))
        self.assertTrue(all(isinstance(json.loads(line), dict) for line in event_lines))
        self.assertNotIn("do not persist this prompt", state_text)
        self.assertNotIn("do not persist this input", state_text)

    def test_leader_failure_type_is_propagated_without_raw_error_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reuse_root = root / "reuse"
            workspace = root / "workspace"
            workspace.mkdir()
            counter = self.context.Value("i", 0)
            release = self.context.Event()
            results = self.context.Queue()
            leader = self.context.Process(
                target=_cache_worker,
                args=(
                    reuse_root,
                    workspace,
                    "leader-task",
                    counter,
                    release,
                    results,
                    True,
                ),
            )
            follower = self.context.Process(
                target=_cache_worker,
                args=(
                    reuse_root,
                    workspace,
                    "follower-task",
                    counter,
                    release,
                    results,
                    True,
                ),
            )

            leader.start()
            self.assertTrue(
                _wait_for(lambda: _running_state(reuse_root) is not None)
            )
            follower.start()
            self.assertTrue(
                _wait_for(
                    lambda: bool(
                        (_running_state(reuse_root) or {}).get("waiters")
                    )
                )
            )
            release.set()
            leader.join(5)
            follower.join(5)
            errors = [results.get(timeout=2), results.get(timeout=2)]

        self.assertEqual(1, counter.value)
        self.assertEqual(
            {"RuntimeError", "RemoteFlightError"}, {item[1] for item in errors}
        )
        remote = next(item for item in errors if item[1] == "RemoteFlightError")
        self.assertIn("RuntimeError", remote[2])
        self.assertNotIn("private failure detail", remote[2])

    def test_different_cost_budgets_never_share_a_flight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reuse_root = root / "reuse"
            workspace = root / "workspace"
            workspace.mkdir()
            counter = self.context.Value("i", 0)
            release = self.context.Event()
            results = self.context.Queue()
            first = self.context.Process(
                target=_cache_worker,
                args=(
                    reuse_root,
                    workspace,
                    "budget-one",
                    counter,
                    release,
                    results,
                    False,
                    0.5,
                ),
            )
            second = self.context.Process(
                target=_cache_worker,
                args=(
                    reuse_root,
                    workspace,
                    "budget-two",
                    counter,
                    release,
                    results,
                    False,
                    0.1,
                ),
            )
            first.start()
            second.start()
            self.assertTrue(_wait_for(lambda: counter.value == 2))
            state_paths = tuple(
                (reuse_root / "flights" / "states").glob("*.json")
            )
            release.set()
            first.join(5)
            second.join(5)
            delivered = [results.get(timeout=2), results.get(timeout=2)]

        self.assertEqual(0, first.exitcode)
        self.assertEqual(0, second.exitcode)
        self.assertEqual(2, len(state_paths))
        self.assertEqual(["miss", "miss"], sorted(item[1] for item in delivered))

    def test_expired_crashed_leader_is_replaced_by_one_new_owner(self):
        key = hashlib.sha256(b"takeover").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flight_root = root / "flights"
            counter_path = root / "counter"
            started_path = root / "started"
            counter_path.write_text("0", encoding="utf-8")
            results = self.context.Queue()
            crashed = self.context.Process(
                target=_crash_worker,
                args=(flight_root, key, counter_path, started_path),
            )
            crashed.start()
            self.assertTrue(_wait_for(started_path.exists, timeout=3))
            crashed.join(3)
            takeover = self.context.Process(
                target=_takeover_worker,
                args=(flight_root, key, counter_path, results),
            )
            takeover.start()
            takeover.join(5)
            delivered = results.get(timeout=2)
            remaining_states = tuple((flight_root / "states").glob("*.json"))
            counter_value = counter_path.read_text(encoding="utf-8")

        self.assertEqual(23, crashed.exitcode)
        self.assertEqual(0, takeover.exitcode)
        self.assertEqual("2", counter_value)
        self.assertEqual(
            ("ok", False, "takeover-leader", {"answer": "recovered"}),
            delivered,
        )
        self.assertEqual((), remaining_states)

    def test_tampered_lease_receipt_is_rejected_before_takeover(self):
        key = hashlib.sha256(b"tampered").hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flight_root = root / "flights"
            counter_path = root / "counter"
            started_path = root / "started"
            counter_path.write_text("0", encoding="utf-8")
            crashed = self.context.Process(
                target=_crash_worker,
                args=(flight_root, key, counter_path, started_path),
            )
            crashed.start()
            self.assertTrue(_wait_for(started_path.exists, timeout=3))
            crashed.join(3)
            path = flight_root / "states" / f"{key}.json"
            state = json.loads(path.read_text(encoding="utf-8"))
            state["source_id"] = "forged-owner"
            path.write_text(json.dumps(state), encoding="utf-8")
            coordinator = SingleFlightCoordinator(
                flight_root,
                lease_seconds=0.2,
                heartbeat_interval=0.05,
                poll_interval=0.01,
            )

            with self.assertRaisesRegex(ContractViolation, "checksum mismatch"):
                coordinator.execute(
                    key,
                    "takeover",
                    lambda: {"answer": "unsafe"},
                    timeout_seconds=1,
                )


if __name__ == "__main__":
    unittest.main()
